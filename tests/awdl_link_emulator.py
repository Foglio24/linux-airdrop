"""AWDL-like link emulator, run inside `unshare -Urn` by test_awdl_link.py (or by hand to compare settings).
by a userspace relay that only carries frames while the "phone" is on our channel (some slots of a 16-slot,
1.048 s period), at 12 Mbit/s, and loses PC->phone frames at random with probability LOSS (no radio retries).
Phone->PC frames are never lost (the phone retries). Prints elapsed time and outcome of one real OpenDrop send.
"""
import json, os, random, select, signal, socket, subprocess, sys, tempfile, threading, time
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOSS = float(os.environ.get("LOSS", "0.2"))
SLOTS = [int(x) for x in os.environ.get("SLOTS", "8,9").split(",")]
SIZE = int(os.environ.get("SIZE_MB", "3")) * 1_000_000
PERIOD, SLOT = 1.048576, 0.065536
RATE = 12e6 / 8          # bytes per second of airtime
random.seed(int(os.environ.get("SEED", "1")))

def sh(*a): subprocess.run(a, check=True, capture_output=True)
def holder():
    pid = os.fork()
    if pid == 0:
        os.unshare(os.CLONE_NEWNET)
        signal.pause()
        os._exit(0)
    time.sleep(0.2)
    return pid
def ns(pid, *a, **kw): return ["nsenter", f"--net=/proc/{pid}/ns/net", *a]

sh("ip", "link", "set", "lo", "up")
S, R = holder(), holder()
for pid, outer in ((S, "s_out"), (R, "r_out")):
    sh("ip", "link", "add", "name", outer, "type", "veth", "peer", "name", outer + "_in")
    sh("ip", "link", "set", outer + "_in", "netns", str(pid))
    sh("ip", "link", "set", outer, "mtu", "1450", "up")
    for cmd in (["ip", "link", "set", "lo", "up"], ["ip", "link", "set", outer + "_in", "name", "awdl0"],
                ["ip", "link", "set", "awdl0", "mtu", "1450", "multicast", "on", "up"],
                # no offloads: the relay must see real, checksummed frames of at most one MTU
                ["ethtool", "-K", "awdl0", "tx", "off", "rx", "off", "gso", "off", "tso", "off", "gro", "off"]):
        sh(*ns(pid, *cmd))
    sh("ethtool", "-K", outer, "tx", "off", "rx", "off", "gso", "off", "tso", "off", "gro", "off")
time.sleep(2.5)

def addr(pid):
    out = subprocess.run(ns(pid, "ip", "-6", "addr", "show", "awdl0"), capture_output=True, text=True).stdout
    return next(l.split()[1].split("/")[0] for l in out.splitlines() if "inet6 fe80" in l)

# ----- relay
socks = {}
for name in ("s_out", "r_out"):
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3)); s.bind((name, 0)); socks[name] = s
queues = {"s_out": [], "r_out": []}       # frames waiting for a window, keyed by where they came from
stats = {"sent": 0, "lost": 0, "back": 0}
lock = threading.Lock()
def window_open(t):
    return int((t % PERIOD) / SLOT) in SLOTS
def reader():
    while True:
        r, _, _ = select.select(list(socks.values()), [], [], 0.05)
        for s in r:
            frame, a = s.recvfrom(65536)
            if a[2] == 4:      # our own transmission seen again
                continue
            src = "s_out" if s is socks["s_out"] else "r_out"
            with lock:
                queues[src].append(frame)
def pump():
    busy_until = 0.0
    while True:
        now = time.monotonic()
        if not window_open(now) or now < busy_until:
            time.sleep(0.0005); continue
        with lock:
            # phone->PC first (it has priority on its own schedule), then PC->phone
            item = None
            if queues["r_out"]:
                item = ("r_out", queues["r_out"].pop(0))
            elif queues["s_out"]:
                item = ("s_out", queues["s_out"].pop(0))
        if not item:
            time.sleep(0.0005); continue
        src, frame = item
        busy_until = now + len(frame) / RATE
        if src == "s_out":
            stats["sent"] += 1
            if random.random() < LOSS:
                stats["lost"] += 1
                continue
            socks["r_out"].send(frame)
        else:
            stats["back"] += 1
            socks["s_out"].send(frame)
threading.Thread(target=reader, daemon=True).start()
threading.Thread(target=pump, daemon=True).start()

work = tempfile.mkdtemp(prefix="emu-")
res, snd, down = (os.path.join(work, n) for n in ("rs", "ss", "dl"))
for d in (res, snd, down): os.makedirs(d)
hook = os.path.join(work, "ok.sh"); open(hook, "w").write("#!/bin/sh\nexit 0\n"); os.chmod(hook, 0o755)
runner = os.path.join(REPO, "backend/opendrop")
env = dict(os.environ, PYTHONUNBUFFERED="1", OPENDROP_ANNOUNCE_INTERVAL="5")
rx = subprocess.Popen(ns(R, runner, "receive", "-i", "awdl0", "-n", "phone", "-m", "iPhone"), cwd=down,
                      env=dict(env, AIRDROP_STATE_DIR=res, AIRDROP_DOWNLOAD_DIR=down, AIRDROP_CONFIRM=hook),
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(3)
json.dump([{"name": "phone", "address": addr(R), "port": 8771, "id": "0123456789ab", "flags": 136, "discoverable": True}],
          open(os.path.join(snd, "discover.last.json"), "w"))
src = os.path.join(work, "payload.bin"); open(src, "wb").write(os.urandom(SIZE))
start = time.monotonic()
tx = subprocess.Popen(ns(S, runner, "send", "-i", "awdl0", "-n", "pc", "-r", "0123456789ab", "-f", src),
                      env=dict(env, AIRDROP_STATE_DIR=snd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    out, _ = tx.communicate(timeout=int(os.environ.get("LIMIT", "400")))
    code = tx.returncode
except subprocess.TimeoutExpired:
    tx.kill(); out, _ = tx.communicate(); code = "timeout"
elapsed = time.monotonic() - start
rx.kill()
got = sum(os.path.getsize(os.path.join(r, n)) for r, _, ns_ in os.walk(down) for n in ns_ if not r.split("/")[-1].startswith(".airdrop"))
tail = out.splitlines()[-6:]
print("RESULT", json.dumps({"loss": LOSS, "slots": SLOTS, "size_mb": SIZE / 1e6, "code": code, "seconds": round(elapsed, 1),
                           "kB_s": round(SIZE / 1000 / elapsed, 1) if code == 0 else None, "received": got == SIZE,
                           "frames": stats, "tune": os.environ.get("AIRDROP_TCP_TUNING", "default"), "tail": tail}))
os.kill(S, 9); os.kill(R, 9)
