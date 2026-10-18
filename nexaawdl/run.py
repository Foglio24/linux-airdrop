"""Entry point of the unprivileged engine process.

The privileged bridge opens the monitor socket (the only thing that needs CAP_NET_RAW), starts this script
as the desktop user with the descriptor inherited, and supervises it. Here nothing privileged remains:
no_new_privs is set, core dumps are off, and the process only reads frames, writes two files in the user's
state directory and exits on SIGTERM or when the socket fails.

    python3 -I -B run.py --fd N --dir STATE_DIR [--iface NAME] [--level info] [--mode observe]
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import resource
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nexaawdl import capture, engine          # noqa: E402

STATUS_NAME = "status.json"
EVENTS_NAME = "events.jsonl"
EVENTS_LIMIT = 1_000_000
PR_SET_NO_NEW_PRIVS = 38


def harden():
    try:
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    except (OSError, AttributeError):
        pass
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    except (OSError, ValueError):
        pass


def own_addresses(iface):
    """The monitor interface carries our AWDL address (the bridge sets it): frames from it are our own."""
    try:
        text = Path("/sys/class/net", iface, "address").read_text().strip()
        raw = bytes(int(p, 16) for p in text.split(":"))
    except (OSError, ValueError, TypeError):
        return ()
    return (raw,) if len(raw) == 6 else ()


def write_atomic(path, text):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


class EventLog:
    """Append-only JSON lines with a size cap: one previous generation is kept."""

    def __init__(self, path):
        self.path = path
        self.size = path.stat().st_size if path.exists() else 0

    def __call__(self, event):
        line = json.dumps(event, separators=(",", ":"), default=str) + "\n"
        if self.size + len(line) > EVENTS_LIMIT:
            os.replace(self.path, self.path.with_suffix(".jsonl.1"))
            self.size = 0
        with open(self.path, "a") as f:
            f.write(line)
        self.size += len(line)


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--fd", type=int, required=True)
    p.add_argument("--dir", required=True)
    p.add_argument("--iface")
    p.add_argument("--level", default="info", choices=sorted(engine.LEVELS))
    p.add_argument("--mode", default="observe", choices=("observe",))
    args = p.parse_args(argv)
    harden()
    state_dir = Path(args.dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(fileno=args.fd)
    running = True

    def stop(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    eng = engine.Engine(engine.EngineConfig(mode=args.mode, log_level=args.level), args.iface, EventLog(state_dir / EVENTS_NAME),
                        own=own_addresses(args.iface))
    eng.start(time.time_ns() // 1000)
    last_status = 0.0
    exit_code = 0
    try:
        for now_us, packet, truncated in capture.SocketSource(sock):
            if not running:
                break
            if packet is not None:
                eng.feed(packet, now_us, truncated)
            eng.tick(time.time_ns() // 1000)
            if time.monotonic() - last_status >= 1.0:
                last_status = time.monotonic()
                write_atomic(state_dir / STATUS_NAME, json.dumps({**eng.status(), "updated": time.time()}, default=str))
    except capture.SourceError as exc:
        eng.fail(str(exc))
        exit_code = 3
    finally:
        if eng.state != engine.ERROR:               # an error is the last word: the window shows why it ended
            eng.stop("process exiting")
        write_atomic(state_dir / STATUS_NAME, json.dumps({**eng.status(), "updated": time.time()}, default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
