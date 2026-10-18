"""AWDL action frame codec: strict decoding and encoding of the frame body and its TLVs.

The layouts follow the published AWDL reverse-engineering (Stute et al., "A Billion Open Interfaces
for Eve and Mallory", USENIX Security 2019) and the field offsets used by the OWL implementation; this
module is written independently and checked against frames captured from real Apple devices.

A frame body is what follows the 802.11 management header::

    category(1)=127  OUI(3)=00:17:f2  type(1)=8  version(1)  subtype(1)  reserved(1)
    phy_tx(4, LE)    target_tx(4, LE)   then TLVs: type(1) length(2, LE) value(length)

Everything here treats its input as hostile: bounds are checked before every read, counts and sizes are
capped, unknown TLVs are preserved instead of guessed at, and the only exception raised for bad input is
`MalformedFrame`.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

CATEGORY_VENDOR = 127
AWDL_OUI = bytes((0x00, 0x17, 0xF2))
AWDL_TYPE = 8
AWDL_VERSION = 0x10            # 1.0 in the (major << 4 | minor) encoding seen on the air
SUBTYPE_PSF = 0                # periodic synchronisation frame
SUBTYPE_MIF = 3                # master indication frame
SUBTYPES = {SUBTYPE_PSF: "PSF", SUBTYPE_MIF: "MIF"}
ACTION_HEADER = 16             # bytes before the first TLV
MAX_TLVS = 64
MAX_TLV_LENGTH = 2048
CHANSEQ_SLOTS = 16

TLV_NAMES = {
    0: "SSTH request", 1: "Service request", 2: "Service response", 4: "Synchronisation parameters",
    5: "Election parameters", 6: "Service parameters", 7: "HT capabilities", 8: "Enhanced data rate operation",
    9: "Infra", 10: "Invite", 11: "Debug string", 12: "Data path state", 13: "Encapsulated IP",
    14: "Datapath debug packet", 15: "Datapath debug action frame", 16: "Arpa", 17: "IEEE 802.11 container",
    18: "Channel sequence", 20: "Synchronisation tree", 21: "Version", 22: "Bloom filter", 23: "NAN sync",
    24: "Election parameters v2",
}

ENC_SIMPLE, ENC_LEGACY, ENC_OPCLASS = 0, 1, 3
ENC_SIZE = {ENC_SIMPLE: 1, ENC_LEGACY: 2, ENC_OPCLASS: 2}


class MalformedFrame(ValueError):
    """The bytes are not a valid AWDL frame (or TLV). Never carries a partial result."""


def _need(data, offset, size, what):
    if offset < 0 or size < 0 or offset + size > len(data):
        raise MalformedFrame(f"{what}: truncated (need {size} bytes at {offset}, have {len(data)})")


def mac_str(raw):
    return ":".join(f"{b:02x}" for b in raw)


def mac_bytes(text):
    parts = text.split(":")
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        raise ValueError(f"not a MAC address: {text!r}")
    return bytes(int(p, 16) for p in parts)


# --------------------------------------------------------------------------------------- channel sequence

@dataclass(frozen=True)
class ChannelSequence:
    """The 16 per-slot channels of a device. `channels[i]` is 0 for an empty slot."""
    encoding: int
    channels: tuple
    raw: tuple = ()                    # the original per-slot byte strings, kept so a re-encode is exact
    step_count: int = 0
    duplicate_count: int = 0
    fill_channel: int = 0xFFFF

    def slots_on(self, channel):
        return [i for i, c in enumerate(self.channels) if c == channel]

    def distinct(self):
        return sorted({c for c in self.channels if c})

    def text(self):
        return ",".join(str(c) for c in self.channels)


def decode_chanseq(value, offset=0):
    """Decode a channel sequence structure starting at `offset`; returns (ChannelSequence, end_offset)."""
    _need(value, offset, 6, "channel sequence header")
    count, encoding, duplicate, step, fill = struct.unpack_from("<BBBBH", value, offset)
    if count + 1 != CHANSEQ_SLOTS:
        raise MalformedFrame(f"channel sequence has {count + 1} slots, expected {CHANSEQ_SLOTS}")
    size = ENC_SIZE.get(encoding)
    if size is None:
        raise MalformedFrame(f"unknown channel encoding {encoding}")
    if duplicate:
        raise MalformedFrame("channel sequence with duplicate count is not supported")
    pos = offset + 6
    _need(value, pos, CHANSEQ_SLOTS * size, "channel sequence entries")
    raw, channels = [], []
    for _ in range(CHANSEQ_SLOTS):
        entry = bytes(value[pos:pos + size])
        pos += size
        raw.append(entry)
        # simple: {chan}; legacy: {flags, chan}; opclass: {chan, opclass}
        channels.append(entry[0] if encoding in (ENC_SIMPLE, ENC_OPCLASS) else entry[1])
    return ChannelSequence(encoding, tuple(channels), tuple(raw), step, duplicate, fill), pos


def encode_chanseq(seq):
    size = ENC_SIZE[seq.encoding]
    if len(seq.raw) == CHANSEQ_SLOTS:
        entries = b"".join(seq.raw)
    else:
        entries = b"".join(_encode_slot(seq.encoding, c) for c in seq.channels)
    if len(entries) != CHANSEQ_SLOTS * size:
        raise ValueError("channel sequence must have 16 slots")
    return struct.pack("<BBBBH", CHANSEQ_SLOTS - 1, seq.encoding, seq.duplicate_count, seq.step_count,
                       seq.fill_channel) + entries


OPCLASS = {6: 0x51, 36: 0x80, 44: 0x80, 149: 0x80}      # operating classes of the AWDL social channels


def _encode_slot(encoding, channel):
    if not channel:
        return bytes(ENC_SIZE[encoding])
    if encoding == ENC_SIMPLE:
        return bytes((channel,))
    if encoding == ENC_LEGACY:
        return bytes((0, channel))
    if channel not in OPCLASS:
        raise ValueError(f"no operating class known for channel {channel}")
    return bytes((channel, OPCLASS[channel]))


def make_chanseq(channels, encoding=ENC_OPCLASS, presence_mode=4):
    channels = tuple(channels)
    if len(channels) != CHANSEQ_SLOTS:
        raise ValueError("a channel sequence has 16 slots")
    return ChannelSequence(encoding, channels, tuple(_encode_slot(encoding, c) for c in channels),
                           step_count=presence_mode - 1)


# ---------------------------------------------------------------------------------------------------- TLVs

@dataclass(frozen=True)
class SyncParams:
    TYPE = 4
    next_aw_channel: int
    tx_down_counter: int            # time to the next availability window, in TU
    master_channel: int
    guard_time: int
    aw_period: int                  # TU
    af_period: int                  # TU
    flags: int
    aw_ext_length: int
    aw_com_length: int
    remaining_aw_length: int
    min_ext: int
    max_ext_multicast: int
    max_ext_unicast: int
    max_ext_af: int
    master_addr: bytes
    presence_mode: int
    next_aw_seq: int                # availability-window counter
    ap_alignment: int
    chanseq: ChannelSequence
    reserved: int = 0
    tail: bytes = b"\0\0"           # whatever follows the embedded channel sequence (kept so a re-encode is exact)

    FIXED = 33                      # bytes before the embedded channel sequence

    @classmethod
    def decode(cls, v):
        _need(v, 0, cls.FIXED, "sync parameters")
        (nac, down, mch, guard, awp, afp, flags, ext, com, rem, mine, maxm, maxu, maxa) = struct.unpack_from(
            "<BHBBHHHHHHBBBB", v, 0)
        master = bytes(v[21:27])
        presence, reserved, seq, align = struct.unpack_from("<BBHH", v, 27)
        if not presence or presence > 16:
            raise MalformedFrame(f"presence mode {presence} out of range")
        if not awp:
            raise MalformedFrame("availability window period is zero")
        seq_obj, end = decode_chanseq(v, cls.FIXED)
        return cls(nac, down, mch, guard, awp, afp, flags, ext, com, rem, mine, maxm, maxu, maxa, master,
                   presence, seq, align, seq_obj, reserved, bytes(v[end:]))

    def encode(self):
        head = struct.pack("<BHBBHHHHHHBBBB", self.next_aw_channel, self.tx_down_counter, self.master_channel,
                           self.guard_time, self.aw_period, self.af_period, self.flags, self.aw_ext_length,
                           self.aw_com_length, self.remaining_aw_length, self.min_ext, self.max_ext_multicast,
                           self.max_ext_unicast, self.max_ext_af)
        return (head + self.master_addr + struct.pack("<BBHH", self.presence_mode, self.reserved,
                                                      self.next_aw_seq, self.ap_alignment)
                + encode_chanseq(self.chanseq) + self.tail)


@dataclass(frozen=True)
class ChanSeqTlv:
    TYPE = 18
    chanseq: ChannelSequence

    @classmethod
    def decode(cls, v):
        seq, _ = decode_chanseq(v, 0)
        return cls(seq)

    def encode(self):
        return encode_chanseq(self.chanseq) + b"\0\0\0"


@dataclass(frozen=True)
class ElectionParams:
    TYPE = 5
    flags: int
    ident: int
    distance: int
    top_master: bytes
    top_master_metric: int
    self_metric: int

    @classmethod
    def decode(cls, v):
        _need(v, 0, 19, "election parameters")
        flags, ident, distance, _unknown = struct.unpack_from("<BHBB", v, 0)
        top = bytes(v[5:11])
        tmm, sm = struct.unpack_from("<II", v, 11)
        return cls(flags, ident, distance, top, tmm, sm)

    def encode(self):
        return (struct.pack("<BHBB", self.flags, self.ident, self.distance, 0) + self.top_master
                + struct.pack("<II", self.top_master_metric, self.self_metric) + b"\0\0")


@dataclass(frozen=True)
class ElectionParamsV2:
    TYPE = 24
    master_addr: bytes
    sync_addr: bytes
    master_counter: int
    distance: int
    master_metric: int
    self_metric: int
    self_counter: int

    @classmethod
    def decode(cls, v):
        _need(v, 0, 40, "election parameters v2")
        master, sync = bytes(v[0:6]), bytes(v[6:12])
        mc, dist = struct.unpack_from("<II", v, 12)
        mm, sm = struct.unpack_from("<II", v, 20)
        (sc,) = struct.unpack_from("<I", v, 36)
        return cls(master, sync, mc, dist, mm, sm, sc)

    def encode(self):
        return (self.master_addr + self.sync_addr + struct.pack("<II", self.master_counter, self.distance)
                + struct.pack("<II", self.master_metric, self.self_metric) + bytes(8)
                + struct.pack("<I", self.self_counter))


@dataclass(frozen=True)
class VersionTlv:
    TYPE = 21
    version: int
    device_class: int

    @classmethod
    def decode(cls, v):
        _need(v, 0, 2, "version")
        return cls(v[0], v[1])

    def encode(self):
        return bytes((self.version, self.device_class))


@dataclass(frozen=True)
class ArpaTlv:
    """The peer's host name (`<name>.local`), as announced."""
    TYPE = 16
    flags: int
    name: str

    @classmethod
    def decode(cls, v):
        _need(v, 0, 2, "arpa")
        flags, length = v[0], v[1]
        _need(v, 2, length, "arpa name")
        return cls(flags, bytes(v[2:2 + length]).decode("utf-8", "replace"))

    def encode(self):
        raw = self.name.encode()
        if len(raw) > 255:
            raise ValueError("name too long")
        return bytes((self.flags, len(raw))) + raw + b"\xc0\x0c"


FLAG_COUNTRY, FLAG_SOCIAL_MAP, FLAG_INFRA_INFO, FLAG_INFRA_ADDR, FLAG_AWDL_ADDR = 0x0100, 0x0200, 0x0001, 0x0002, 0x0004


@dataclass(frozen=True)
class DataPathState:
    TYPE = 12
    flags: int
    country: str | None = None
    social_channels: int | None = None
    infra_bssid: bytes | None = None
    infra_channel: int | None = None
    infra_addr: bytes | None = None
    awdl_addr: bytes | None = None
    rest: bytes = b""                     # fields after the ones modelled here, kept verbatim

    @classmethod
    def decode(cls, v):
        _need(v, 0, 2, "data path state")
        (flags,) = struct.unpack_from("<H", v, 0)
        pos, out = 2, {}
        if flags & FLAG_COUNTRY:
            _need(v, pos, 3, "country code")
            out["country"] = bytes(v[pos:pos + 3]).rstrip(b"\0 ").decode("ascii", "replace")
            pos += 3
        if flags & FLAG_SOCIAL_MAP:
            _need(v, pos, 2, "social channel map")
            (out["social_channels"],) = struct.unpack_from("<H", v, pos)
            pos += 2
        if flags & FLAG_INFRA_INFO:
            _need(v, pos, 8, "infrastructure info")
            out["infra_bssid"] = bytes(v[pos:pos + 6])
            (out["infra_channel"],) = struct.unpack_from("<H", v, pos + 6)
            pos += 8
        if flags & FLAG_INFRA_ADDR:
            _need(v, pos, 6, "infrastructure address")
            out["infra_addr"] = bytes(v[pos:pos + 6])
            pos += 6
        if flags & FLAG_AWDL_ADDR:
            _need(v, pos, 6, "AWDL address")
            out["awdl_addr"] = bytes(v[pos:pos + 6])
            pos += 6
        return cls(flags, rest=bytes(v[pos:]), **out)

    def encode(self):
        out = struct.pack("<H", self.flags)
        if self.flags & FLAG_COUNTRY:
            out += (self.country or "").encode("ascii").ljust(3, b"\0")[:3]
        if self.flags & FLAG_SOCIAL_MAP:
            out += struct.pack("<H", self.social_channels or 0)
        if self.flags & FLAG_INFRA_INFO:
            out += (self.infra_bssid or bytes(6)) + struct.pack("<H", self.infra_channel or 0)
        if self.flags & FLAG_INFRA_ADDR:
            out += self.infra_addr or bytes(6)
        if self.flags & FLAG_AWDL_ADDR:
            out += self.awdl_addr or bytes(6)
        return out + self.rest


@dataclass(frozen=True)
class ServiceParams:
    TYPE = 6
    unknown: bytes
    sui: int
    bitmask: int
    tail: bytes = b""

    @classmethod
    def decode(cls, v):
        _need(v, 0, 9, "service parameters")
        (sui, bitmask) = struct.unpack_from("<HI", v, 3)
        return cls(bytes(v[0:3]), sui, bitmask, bytes(v[9:]))

    def encode(self):
        return self.unknown + struct.pack("<HI", self.sui, self.bitmask) + self.tail


@dataclass(frozen=True)
class SyncTree:
    TYPE = 20
    addresses: tuple

    @classmethod
    def decode(cls, v):
        if len(v) % 6:
            raise MalformedFrame("synchronisation tree is not a whole number of addresses")
        if len(v) // 6 > 32:
            raise MalformedFrame("synchronisation tree too deep")
        return cls(tuple(bytes(v[i:i + 6]) for i in range(0, len(v), 6)))

    def encode(self):
        return b"".join(self.addresses)


@dataclass(frozen=True)
class UnknownTlv:
    """A TLV this codec does not interpret. Kept verbatim so nothing is lost or invented."""
    type: int
    data: bytes

    def encode(self):
        return self.data


KNOWN = {c.TYPE: c for c in (SyncParams, ChanSeqTlv, ElectionParams, ElectionParamsV2, VersionTlv, ArpaTlv,
                             DataPathState, ServiceParams, SyncTree)}


def tlv_type(tlv):
    return tlv.type if isinstance(tlv, UnknownTlv) else tlv.TYPE


def decode_tlv(type_, value):
    cls = KNOWN.get(type_)
    if cls is None:
        return UnknownTlv(type_, bytes(value))
    return cls.decode(value)


def decode_tlvs(data, offset=0):
    """Decode every TLV in `data[offset:]`. Fewer than three trailing bytes are padding."""
    out, pos = [], offset
    while len(data) - pos >= 3:
        if len(out) >= MAX_TLVS:
            raise MalformedFrame("too many TLVs")
        type_ = data[pos]
        (length,) = struct.unpack_from("<H", data, pos + 1)
        if length > MAX_TLV_LENGTH:
            raise MalformedFrame(f"TLV {type_} claims {length} bytes")
        _need(data, pos + 3, length, f"TLV {type_}")
        try:
            out.append(decode_tlv(type_, memoryview(data)[pos + 3:pos + 3 + length]))
        except MalformedFrame:
            raise
        except (struct.error, IndexError, UnicodeError) as exc:       # defensive: never leak other types
            raise MalformedFrame(f"TLV {type_}: {exc}") from exc
        pos += 3 + length
    return out


# -------------------------------------------------------------------------------------------- action frame

@dataclass(frozen=True)
class ActionFrame:
    subtype: int
    phy_tx: int
    target_tx: int
    tlvs: tuple = ()
    version: int = AWDL_VERSION

    @property
    def kind(self):
        return SUBTYPES.get(self.subtype, f"subtype {self.subtype}")

    def find(self, cls):
        return next((t for t in self.tlvs if isinstance(t, cls)), None)

    def all(self, cls):
        return [t for t in self.tlvs if isinstance(t, cls)]


def parse_action(body):
    """Decode an AWDL action frame body (starting at the category byte)."""
    body = bytes(body)
    _need(body, 0, ACTION_HEADER, "action header")
    if body[0] != CATEGORY_VENDOR or body[1:4] != AWDL_OUI or body[4] != AWDL_TYPE:
        raise MalformedFrame("not an AWDL action frame")
    version, subtype = body[5], body[6]
    if version != AWDL_VERSION:
        raise MalformedFrame(f"unsupported AWDL version {version:#x}")
    if subtype not in SUBTYPES:
        raise MalformedFrame(f"unsupported AWDL action subtype {subtype}")
    phy_tx, target_tx = struct.unpack_from("<II", body, 8)
    return ActionFrame(subtype, phy_tx, target_tx, tuple(decode_tlvs(body, ACTION_HEADER)), version)


def encode_action(frame):
    out = bytearray(bytes((CATEGORY_VENDOR,)) + AWDL_OUI + bytes((AWDL_TYPE, frame.version, frame.subtype, 0)))
    out += struct.pack("<II", frame.phy_tx & 0xFFFFFFFF, frame.target_tx & 0xFFFFFFFF)
    for tlv in frame.tlvs:
        value = tlv.encode()
        if len(value) > MAX_TLV_LENGTH:
            raise ValueError("TLV too long")
        out += struct.pack("<BH", tlv_type(tlv), len(value)) + value
    return bytes(out)


# --------------------------------------------------------------------------------------------- data frames

AWDL_DATA_HEAD = 0x0403
LLC_SNAP = bytes((0xAA, 0xAA, 0x03, 0x00, 0x17, 0xF2, 0x08, 0x00))
ETH_IPV6 = 0x86DD


@dataclass(frozen=True)
class DataHeader:
    """The 8-byte AWDL data header that follows the LLC/SNAP header of a data frame."""
    sequence: int
    ethertype: int


def parse_data(payload):
    """Decode LLC/SNAP + AWDL data header; returns (DataHeader, ethernet payload)."""
    payload = bytes(payload)
    _need(payload, 0, 16, "data frame")
    if payload[:8] != LLC_SNAP:
        raise MalformedFrame("not an AWDL data frame (LLC/SNAP mismatch)")
    head, seq, _pad = struct.unpack_from("<HHH", payload, 8)
    if head != AWDL_DATA_HEAD:
        raise MalformedFrame(f"unexpected AWDL data head {head:#06x}")
    (ethertype,) = struct.unpack_from(">H", payload, 14)
    return DataHeader(seq, ethertype), payload[16:]


def encode_data(sequence, ethertype, payload):
    return LLC_SNAP + struct.pack("<HHH", AWDL_DATA_HEAD, sequence & 0xFFFF, 0) + struct.pack(">H", ethertype) + payload
