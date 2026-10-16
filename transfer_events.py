"""Structured events from the send/receive processes to the window.

The OpenDrop processes print ordinary log lines. When the environment variable AIRDROP_EVENTS is set
they also print lines that start with PREFIX followed by one JSON object, which the window parses with
`parse()`. A line that does not parse is simply a log line, so a plain command-line run is unchanged.

`parse()` is strict on purpose: the events come from a process we started, but the values inside them
(names, errors) originate from another device, so every field is type-checked and bounded.
"""
import json
import os
import sys
import threading

PREFIX = "@@airdrop "
KINDS = frozenset({
    "ask", "ask_accepted", "ask_declined",
    "receive_start", "receive_progress", "receive_done", "receive_failed",
    "send_accepted", "send_declined", "send_start", "send_progress", "send_done", "send_failed",
})
_lock = threading.Lock()


def enabled():
    return bool(os.environ.get("AIRDROP_EVENTS"))


def emit(kind, **fields):
    """Print one event line (only when events are enabled). Never raises: reporting must not break a transfer."""
    if not enabled():
        return
    try:
        line = PREFIX + json.dumps({"event": kind, **fields}, ensure_ascii=False, separators=(",", ":"))
        with _lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()
    except (OSError, ValueError, TypeError):
        pass


def _text(value, limit):
    return value[:limit] if isinstance(value, str) else None


def _count(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < 1 << 50 else None


def _files(value):
    result = []
    if isinstance(value, list):
        for entry in value[:512]:
            if not isinstance(entry, dict):
                continue
            name = _text(entry.get("name"), 400)
            if not name:
                continue
            result.append({"name": name, "kind": _text(entry.get("kind"), 16) or "other",
                           "size": _count(entry.get("size")), "is_dir": entry.get("is_dir") is True})
    return result


def parse(line):
    """The event as a validated dict, or None for an ordinary line."""
    if not isinstance(line, str) or not line.startswith(PREFIX):
        return None
    try:
        raw = json.loads(line[len(PREFIX):])
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("event") not in KINDS:
        return None
    event = {"event": raw["event"],
             "id": _text(raw.get("id"), 128),
             "sender": _text(raw.get("sender"), 256),
             "model": _text(raw.get("model"), 64),
             "device_key": _text(raw.get("device_key"), 80),
             "identity": raw.get("identity") if raw.get("identity") in ("record", "name") else None,
             "done": _count(raw.get("done")),
             "total": _count(raw.get("total")),
             "folder": _text(raw.get("folder"), 4096),
             "error": _text(raw.get("error"), 300),
             "cancelled": raw.get("cancelled") is True,
             "files": _files(raw.get("files")),
             "items": [i[:2000] for i in raw.get("items", [])[:8] if isinstance(i, str)]
             if isinstance(raw.get("items"), list) else []}
    return event
