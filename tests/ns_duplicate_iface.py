"""Run inside `unshare -Urn` by test_zeroconf_interface.py: two interfaces with the same
MAC and link-local address, like the p2p-GO group and awdl0. Prints one line per interface."""
import ipaddress, os, socket, struct, subprocess, sys, threading, time
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND + "/backend/python")
sys.path.insert(0, BACKEND)
from awdl_diag import summarize_packet
from zeroconf import IPVersion, ServiceInfo, Zeroconf
import ifaddr

mode = sys.argv[1]
def sh(*a): subprocess.run(a, check=True)
sh("ip", "link", "add", "p2p0", "type", "dummy")        # created first -> lower ifindex, like the GO
sh("ip", "link", "add", "awdl0", "type", "dummy")
for n in ("p2p0", "awdl0"):
    sh("ip", "link", "set", n, "address", "c6:bf:be:0c:b3:20")
    sh("ip", "link", "set", n, "multicast", "on")
    sh("ip", "link", "set", n, "up")
time.sleep(1.5)
for a in ifaddr.get_adapters():
    if a.name in ("p2p0", "awdl0"):
        print("adapter", a.name, "index", a.index, [i.ip[0] for i in a.ips if i.is_IPv6])

seen = {}
def sniff(name):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3))
    s.bind((name, 3)); s.settimeout(0.5)
    end = time.time() + 6
    while time.time() < end:
        try:
            pkt, addr = s.recvfrom(1500)
        except socket.timeout:
            continue
        if addr[2] == 4:
            k = summarize_packet(pkt, True)
            if k and "5353" not in k and "mDNS" in k and "_airdrop" in k:
                seen.setdefault(name, []).append(k)
ts = [threading.Thread(target=sniff, args=(n,)) for n in ("p2p0", "awdl0")]
[t.start() for t in ts]; time.sleep(0.3)

ip = [i.ip[0] for a in ifaddr.get_adapters() if a.name == "awdl0" for i in a.ips if i.is_IPv6][0]
if mode == "old":
    interfaces = [str(ipaddress.IPv6Address(ip))]
else:
    interfaces = [socket.if_nametoindex("awdl0")]
zc = Zeroconf(interfaces=interfaces, ip_version=IPVersion.V6Only)
info = ServiceInfo("_airdrop._tcp.local.", "abcdef123456._airdrop._tcp.local.", port=8771,
                   properties={"flags": "136"}, server="foglio-pc.local.",
                   addresses=[ipaddress.IPv6Address(ip).packed])
zc.register_service(info)
[t.join() for t in ts]
zc.unregister_all_services(); zc.close()
for n in ("p2p0", "awdl0"):
    print(f"[{mode}] airdrop packets leaving {n}: {len(seen.get(n, []))}", (seen.get(n) or [""])[0][:110])
