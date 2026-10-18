"""Frame sources for the engine: a pcap file (offline analysis, tests) or a monitor-interface socket.

`open_monitor_socket` needs CAP_NET_RAW and is the only privileged operation of the engine: the privileged
bridge calls it and hands the descriptor to the unprivileged engine process, which never needs a capability.
"""
from __future__ import annotations

import socket
import struct
import time

LINKTYPE_RADIOTAP = 127
ETH_P_ALL = 0x0003
SOL_PACKET = 263
PACKET_IGNORE_OUTGOING = 23
MAX_FRAME = 65536


class SourceError(OSError):
    """The frame source failed in a way the engine should report (and may retry)."""


def open_monitor_socket(ifname, rcvbuf=4 * 1024 * 1024):
    """A raw socket bound to a monitor interface; frames arrive with their radiotap header. Needs CAP_NET_RAW."""
    sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
        try:
            sock.setsockopt(SOL_PACKET, PACKET_IGNORE_OUTGOING, 1)        # never read back our own injections
        except OSError:
            pass
        sock.bind((ifname, 0))
    except OSError:
        sock.close()
        raise
    return sock


class PcapSource:
    """Reads a classic pcap file (little or big endian, micro or nanosecond) of radiotap frames."""

    def __init__(self, path):
        self.path = path

    def __iter__(self):
        with open(self.path, "rb") as f:
            head = f.read(24)
            if len(head) < 24:
                raise SourceError("pcap header truncated")
            magic = head[:4]
            order = {b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
                     b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">"}.get(magic)
            if order is None:
                raise SourceError("not a classic pcap file")
            nano = magic in (b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d")
            (link,) = struct.unpack_from(order + "I", head, 20)
            if link != LINKTYPE_RADIOTAP:
                raise SourceError(f"link type {link}, expected radiotap ({LINKTYPE_RADIOTAP})")
            while True:
                rec = f.read(16)
                if len(rec) < 16:
                    return
                sec, frac, incl, orig = struct.unpack(order + "IIII", rec)
                if incl > MAX_FRAME:
                    raise SourceError("pcap record larger than any frame")
                data = f.read(incl)
                if len(data) < incl:
                    return
                yield sec * 1_000_000 + (frac // 1000 if nano else frac), data, orig > incl


class SocketSource:
    """Frames from an already open packet socket (a descriptor inherited from the privileged bridge)."""

    def __init__(self, sock, timeout=1.0):
        self.sock = sock
        self.sock.settimeout(timeout)

    def __iter__(self):
        while True:
            try:
                data = self.sock.recv(MAX_FRAME)
            except socket.timeout:
                yield None, None, False                    # lets the engine run its timers while the air is quiet
                continue
            except OSError as exc:
                raise SourceError(str(exc)) from exc
            if not data:
                raise SourceError("capture socket closed")
            yield time.time_ns() // 1000, data, False
