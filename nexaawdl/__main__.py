"""Command line: `python3 -m nexaawdl hardware | analyze FILE.pcap | status [FILE]`.

`hardware` identifies the Wi-Fi card (read-only commands). `analyze` replays a monitor capture through the
engine and prints what it found; capture with `tcpdump -i <monitor> -s 0 -w file.pcap` (a short snapshot
length cuts the TLVs off and leaves only the AWDL header). `status` prints the running engine's snapshot.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import capture, engine, frames, hwprobe, view


def cmd_hardware(args):
    report = hwprobe.probe(args.interface).as_dict()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0
    print(f"{report['chipset']} [{report['pci_id']}] at {report['pci_address']}")
    print(f"interface {report['interface']} (phy{report['phy']}), driver {report['driver']}, firmware {report['firmware']}")
    print(f"kernel {report['kernel']}")
    for kind in ("facts", "inferences"):
        for line in report[kind]:
            print(f"  {kind[:-1]:9} {line}")
    for item in report["limitations"]:
        print(f"  limit ({item['kind']}): {item['text']}")
    for line in report["not_verified"]:
        print(f"  not verified: {line}")
    print("verdict:", report["verdict"])
    return 0


def cmd_analyze(args):
    own = [frames.mac_bytes(m) for m in args.own]
    eng = engine.Engine(engine.EngineConfig(log_level="error"), "capture", own=own)
    eng.start(0)
    last_tick = 0
    count = 0
    for now_us, packet, truncated in capture.PcapSource(args.pcap):
        eng.feed(packet, now_us, truncated)
        count += 1
        if eng.now - last_tick >= 1_000_000:
            last_tick = eng.now
            eng.tick(eng.now)
    eng.tick(eng.now)
    status = eng.status()
    if args.json:
        print(json.dumps(status, indent=2, default=str))
        return 0
    stats = status["stats"]
    print(f"{count} frames: {stats.get('psf', 0)} PSF, {stats.get('mif', 0)} MIF, {stats.get('data', 0)} data, "
          f"{stats.get('malformed', 0)} malformed, {stats.get('truncated', 0)} cut by the capture")
    if stats.get("truncated", 0) * 2 > count:
        print("note: most frames are truncated; capture with -s 0 to see the TLVs (channel sequence, sync parameters)")
    print("state:", status["state"], "-", status["reason"])
    sync = status["sync"]
    print("reference clock:", sync["reference"], "quality:", sync["quality"])
    print("window model check:", sync["window_check"]["consistent_pct"], "% of", sync["window_check"]["checked"], "frames")
    for peer in status["peers"]:
        print(f"peer {peer['addr']} {peer['state']} airdrop={peer['airdrop']} rssi={peer['rssi']} "
              f"action={peer['action_frames']} data={peer['data_frames']} sequence={peer['sequence']}")
    for reason, n in status["malformed"].items():
        print(f"  malformed x{n}: {reason}")
    return 0


def cmd_status(args):
    path = Path(args.file) if args.file else Path.home() / ".local/state/linux-airdrop/nexaawdl/status.json"
    status = view.read_status(path)
    if status is None:
        print("no engine status at", path)
        return 1
    print(view.diagnostics_text(status))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="python3 -m nexaawdl")
    sub = p.add_subparsers(dest="command", required=True)
    h = sub.add_parser("hardware", help="identify the Wi-Fi card and assess AWDL capability")
    h.add_argument("--interface")
    h.add_argument("--json", action="store_true")
    h.set_defaults(func=cmd_hardware)
    a = sub.add_parser("analyze", help="replay a pcap of a monitor interface through the engine")
    a.add_argument("pcap")
    a.add_argument("--json", action="store_true")
    a.add_argument("--own", action="append", default=[], metavar="MAC",
                   help="our own AWDL address (repeatable): frames from it are not peers")
    a.set_defaults(func=cmd_analyze)
    s = sub.add_parser("status", help="print the running engine's status")
    s.add_argument("file", nargs="?")
    s.set_defaults(func=cmd_status)
    args = p.parse_args(argv)
    try:
        return args.func(args)
    except capture.SourceError as exc:
        print("error:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
