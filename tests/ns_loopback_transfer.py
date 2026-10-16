"""Run inside `unshare -Urn` by test_loopback_transfer.py.

A real OpenDrop receiver and a real OpenDrop sender talk over a dummy `awdl0` interface in a private
network namespace: mDNS discovery, TLS, /Discover, /Ask, /Upload, our confirmation hook, progress events
and the files that land on disk. There is no radio and no phone: this proves our own two halves agree with
each other, not that Apple devices accept the result. Prints one JSON object.
"""
import json, os, shutil, signal, subprocess, sys, tempfile, threading, time
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import transfer_events as events

def sh(*a): subprocess.run(a, check=True, capture_output=True)
sh("ip", "link", "set", "lo", "up")            # local delivery to our own address goes through lo
sh("ip", "link", "add", "awdl0", "type", "dummy")
sh("ip", "link", "set", "awdl0", "multicast", "on")
sh("ip", "link", "set", "awdl0", "up")
for _ in range(50):
    out = subprocess.run(["ip", "-6", "addr", "show", "awdl0"], capture_output=True, text=True).stdout
    if "fe80" in out and "tentative" not in out:
        break
    time.sleep(0.1)

work = tempfile.mkdtemp(prefix="loopback-")
res, snd, down = (os.path.join(work, n) for n in ("receiver-state", "sender-state", "downloads"))
for d in (res, snd, down):
    os.makedirs(d)
hook = os.path.join(work, "accept.sh")
with open(hook, "w") as f:
    f.write("#!/bin/sh\nexit 0\n")
os.chmod(hook, 0o755)
runner = os.path.join(ROOT, "backend/opendrop")
base_env = dict(os.environ, PYTHONUNBUFFERED="1", AIRDROP_EVENTS="1", OPENDROP_ANNOUNCE_INTERVAL="0.5")

def start_events_reader(proc, bucket):
    def read():
        for line in proc.stdout:
            e = events.parse(line)
            (bucket["events"] if e else bucket["log"]).append(e or line.rstrip())
    t = threading.Thread(target=read, daemon=True); t.start(); return t

receiver = subprocess.Popen([runner, "receive", "-i", "awdl0", "-n", "loopback-pc", "-m", "MacBookPro"], cwd=down,
                            env=dict(base_env, AIRDROP_STATE_DIR=res, AIRDROP_DOWNLOAD_DIR=down, AIRDROP_CONFIRM=hook),
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
rx = {"events": [], "log": []}
start_events_reader(receiver, rx)
result = {"errors": []}
try:
    time.sleep(2.0)
    # Discovery by mDNS needs two hosts on a real multicast link; here the sender's discovery report is written
    # directly (that file is all `send` ever reads) and everything after discovery is the real code path.
    address = next(line.split()[1].split("/")[0] for line in subprocess.run(["ip", "-6", "addr", "show", "awdl0"], capture_output=True,
                                                                          text=True).stdout.splitlines() if "inet6 fe80" in line)
    peer = {"name": "loopback-pc", "address": address, "port": 8771, "id": "0123456789ab", "flags": 136, "discoverable": True}
    with open(os.path.join(snd, "discover.last.json"), "w") as f:
        json.dump([peer], f)
    result["found"] = True
    src = os.path.join(work, "src"); os.makedirs(os.path.join(src, "Cartella/interna"))
    open(os.path.join(src, "a.txt"), "w").write("ciao dal PC\n")
    with open(os.path.join(src, "big.bin"), "wb") as f: f.write(os.urandom(24 * 1024 * 1024))
    open(os.path.join(src, "Cartella/uno.txt"), "w").write("uno")
    open(os.path.join(src, "Cartella/interna/due.txt"), "w").write("due")

    def send(files):
        proc = subprocess.Popen([runner, "send", "-i", "awdl0", "-n", "loopback-sender", "-r", peer["id"]] + sum((["-f", f] for f in files), []),
                                env=dict(base_env, AIRDROP_STATE_DIR=snd), stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        tx = {"events": [], "log": []}
        t = start_events_reader(proc, tx)
        try: code = proc.wait(timeout=int(os.environ.get("SEND_TIMEOUT", "120")))
        except subprocess.TimeoutExpired: proc.kill(); code = -9
        t.join(5)
        return {"code": code, "events": tx["events"], "log": tx["log"][-12:]}
    result["send_files"] = send([os.path.join(src, "a.txt"), os.path.join(src, "big.bin")])
    time.sleep(1)
    result["send_folder"] = send([os.path.join(src, "Cartella")])
    time.sleep(1)
finally:
    receiver.send_signal(signal.SIGINT)
    try: receiver.wait(timeout=8)
    except subprocess.TimeoutExpired: receiver.kill()
    result["receiver_events"] = rx["events"]
    result["receiver_log_tail"] = rx["log"][-8:]
    result["downloads"] = {os.path.relpath(os.path.join(r, n), down): os.path.getsize(os.path.join(r, n))
                           for r, _, ns in os.walk(down) for n in ns if not os.path.basename(r).startswith(".airdrop")}
    shutil.rmtree(work, ignore_errors=True)
    print("RESULT " + json.dumps(result))
