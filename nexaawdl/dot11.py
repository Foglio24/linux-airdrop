"""Radiotap and IEEE 802.11 header decoding for frames read from a monitor interface.

Only what the engine needs: capture metadata (hardware timestamp, signal, rate, channel, flags) and the
802.11 header fields that identify AWDL frames. Input is untrusted; every read is bounds checked and the
only exception raised is `MalformedFrame`.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .frames import MalformedFrame

AWDL_BSSID = bytes((0x00, 0x25, 0x00, 0xFF, 0x94, 0x73))

FTYPE_MGMT, FTYPE_CTRL, FTYPE_DATA = 0, 1, 2
STYPE_ACTION = 13
STYPE_QOS_DATA = 8
STYPE_ACK, STYPE_BLOCK_ACK_REQ, STYPE_BLOCK_ACK, STYPE_RTS, STYPE_CTS = 13, 8, 9, 11, 12   # control subtypes

FLAG_FCS_AT_END = 0x10
FLAG_BAD_FCS = 0x40

# (alignment, size) of the radiotap fields of the first presence word, indexed by bit number.
_FIELDS = {0: (8, 8), 1: (1, 1), 2: (1, 1), 3: (2, 4), 4: (2, 2), 5: (1, 1), 6: (1, 1), 7: (2, 2), 8: (2, 2),
           9: (2, 2), 10: (1, 1), 11: (1, 1), 12: (1, 1), 13: (1, 1), 14: (2, 2), 15: (2, 2), 16: (1, 1),
           17: (1, 1), 18: (4, 8), 19: (1, 3), 20: (4, 8), 21: (2, 12), 22: (8, 12), 23: (2, 12), 24: (2, 12),
           25: (2, 6), 26: (1, 1), 27: (2, 4)}


@dataclass(frozen=True)
class RxMeta:
    """What the radio told us about one received frame."""
    length: int                          # radiotap header length
    tsft: int | None = None              # hardware (MAC) timestamp, microseconds
    flags: int = 0
    rate_mbps: float | None = None
    freq: int | None = None
    signal: int | None = None            # dBm
    mcs: int | None = None
    ampdu: bool = False
    vht: bool = False
    he: bool = False

    @property
    def fcs_at_end(self):
        return bool(self.flags & FLAG_FCS_AT_END)

    @property
    def bad_fcs(self):
        return bool(self.flags & FLAG_BAD_FCS)

    def phy(self):
        if self.he:
            return "HE"
        if self.vht:
            return "VHT"
        if self.mcs is not None:
            return f"HT mcs{self.mcs}"
        if self.rate_mbps is not None:
            return f"legacy {self.rate_mbps:g}"
        return "?"


def parse_radiotap(packet):
    packet = bytes(packet)
    if len(packet) < 8 or packet[0] != 0:
        raise MalformedFrame("not a radiotap header")
    (length,) = struct.unpack_from("<H", packet, 2)
    if length < 8 or length > len(packet):
        raise MalformedFrame("radiotap length out of range")
    pos, presents = 4, []
    while True:
        if pos + 4 > length or len(presents) >= 8:
            raise MalformedFrame("radiotap presence words run past the header")
        (word,) = struct.unpack_from("<I", packet, pos)
        presents.append(word)
        pos += 4
        if not word & 0x80000000:
            break
    word0 = presents[0]
    values = {}
    for bit in range(28):
        if not word0 & (1 << bit):
            continue
        align, size = _FIELDS[bit]
        pos = (pos + align - 1) // align * align
        if pos + size > length:
            raise MalformedFrame("radiotap field runs past the header")
        values[bit] = packet[pos:pos + size]
        pos += size
    mcs = values[19][2] if 19 in values and values[19][0] & 0x02 else None
    return RxMeta(
        length=length,
        tsft=struct.unpack("<Q", values[0])[0] if 0 in values else None,
        flags=values[1][0] if 1 in values else 0,
        rate_mbps=values[2][0] / 2 if 2 in values else None,
        freq=struct.unpack_from("<H", values[3])[0] if 3 in values else None,
        signal=struct.unpack("<b", values[5])[0] if 5 in values else None,
        mcs=mcs, ampdu=20 in values, vht=21 in values, he=23 in values)


@dataclass(frozen=True)
class Dot11:
    ftype: int
    stype: int
    retry: bool
    addr1: bytes                          # receiver
    addr2: bytes | None                   # transmitter (absent in ACK/CTS)
    addr3: bytes | None
    sequence: int | None
    body: bytes                           # frame body (after the MAC header)
    qos: int | None = None

    @property
    def is_awdl_action(self):
        return (self.ftype == FTYPE_MGMT and self.stype == STYPE_ACTION and self.addr3 == AWDL_BSSID
                and len(self.body) >= 1 and self.body[0] == 127)

    @property
    def is_data(self):
        return self.ftype == FTYPE_DATA


def parse_80211(frame, meta=None):
    """Split an 802.11 frame (radiotap already removed). Drops the FCS when the radio says it is present."""
    frame = bytes(frame)
    if meta is not None and meta.fcs_at_end:
        if len(frame) < 4:
            raise MalformedFrame("frame shorter than its FCS")
        frame = frame[:-4]
    if len(frame) < 10:
        raise MalformedFrame("802.11 frame too short")
    (fc,) = struct.unpack_from("<H", frame, 0)
    ftype, stype = (fc >> 2) & 3, (fc >> 4) & 0xF
    retry = bool(fc & 0x0800)
    if ftype == FTYPE_CTRL:
        addr2 = frame[10:16] if len(frame) >= 16 and stype not in (STYPE_ACK, STYPE_CTS) else None
        return Dot11(ftype, stype, retry, frame[4:10], addr2, None, None, b"")
    if len(frame) < 24:
        raise MalformedFrame("802.11 header truncated")
    (seqctl,) = struct.unpack_from("<H", frame, 22)
    header = 24
    qos = None
    if ftype == FTYPE_DATA:
        if (fc & 0x0300) == 0x0300:                       # 4-address frame
            header += 6
        if stype & STYPE_QOS_DATA:
            if len(frame) < header + 2:
                raise MalformedFrame("QoS control truncated")
            (qos,) = struct.unpack_from("<H", frame, header)
            header += 2
    if fc & 0x8000:                                       # HT control present
        header += 4
    if len(frame) < header:
        raise MalformedFrame("802.11 header truncated")
    return Dot11(ftype, stype, retry, frame[4:10], frame[10:16], frame[16:22], seqctl >> 4, frame[header:], qos)


def split_capture(packet):
    """radiotap + 802.11 in one call: (RxMeta, Dot11)."""
    meta = parse_radiotap(packet)
    return meta, parse_80211(bytes(packet)[meta.length:], meta)
