"""IPv6 over AWDL: the link-local addressing rule, a bounded IPv6/UDP view and a small mDNS reader.

Used to tell AWDL presence (a peer is synchronising) from service availability (the peer announced
`_airdrop._tcp` over mDNS). Packets are untrusted: every length is checked, compression pointers are
bounded, and failures raise `MalformedFrame`.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from .frames import MalformedFrame

MDNS_PORT = 5353
AIRDROP_PORT = 8771
AIRDROP_SERVICE = "_airdrop._tcp.local"
MAX_NAME = 255
MAX_RECORDS = 64


def link_local(mac):
    """The IPv6 link-local address AWDL derives from a MAC (modified EUI-64), as 16 raw bytes."""
    if len(mac) != 6:
        raise ValueError("MAC must be 6 bytes")
    return bytes((0xFE, 0x80)) + bytes(6) + bytes((mac[0] ^ 0x02,)) + mac[1:3] + bytes((0xFF, 0xFE)) + mac[3:6]


def mac_from_link_local(addr):
    """Inverse of `link_local`; None when `addr` is not a modified-EUI-64 link-local address."""
    if len(addr) != 16 or addr[:2] != b"\xfe\x80" or addr[2:8] != bytes(6) or addr[11:13] != b"\xff\xfe":
        return None
    return bytes((addr[8] ^ 0x02,)) + addr[9:11] + addr[13:16]


@dataclass(frozen=True)
class Ipv6Packet:
    src: bytes
    dst: bytes
    next_header: int
    hop_limit: int
    payload: bytes

    @property
    def multicast(self):
        return self.dst[0] == 0xFF


def parse_ipv6(data):
    data = bytes(data)
    if len(data) < 40:
        raise MalformedFrame("IPv6 header truncated")
    if data[0] >> 4 != 6:
        raise MalformedFrame("not IPv6")
    (plen,) = struct.unpack_from(">H", data, 4)
    if plen > len(data) - 40:
        raise MalformedFrame("IPv6 payload truncated")
    return Ipv6Packet(data[8:24], data[24:40], data[6], data[7], data[40:40 + plen])


@dataclass(frozen=True)
class Udp:
    sport: int
    dport: int
    payload: bytes


def parse_udp(payload):
    if len(payload) < 8:
        raise MalformedFrame("UDP header truncated")
    sport, dport, length = struct.unpack_from(">HHH", payload, 0)
    if length < 8 or length > len(payload):
        raise MalformedFrame("UDP length out of range")
    return Udp(sport, dport, bytes(payload[8:length]))


def _read_name(msg, pos):
    """Read a DNS name with bounded compression; returns (name, position after the name)."""
    labels, jumped, end, hops, total = [], False, pos, 0, 0
    while True:
        if pos >= len(msg):
            raise MalformedFrame("DNS name runs past the message")
        length = msg[pos]
        if length & 0xC0 == 0xC0:
            if pos + 1 >= len(msg):
                raise MalformedFrame("DNS pointer truncated")
            hops += 1
            if hops > 8:
                raise MalformedFrame("DNS pointer loop")
            target = ((length & 0x3F) << 8) | msg[pos + 1]
            if not jumped:
                end = pos + 2
            jumped, pos = True, target
            continue
        if length & 0xC0:
            raise MalformedFrame("bad DNS label")
        pos += 1
        if length == 0:
            if not jumped:
                end = pos
            return ".".join(labels), end
        if pos + length > len(msg):
            raise MalformedFrame("DNS label runs past the message")
        total += length + 1
        if total > MAX_NAME:
            raise MalformedFrame("DNS name too long")
        labels.append(bytes(msg[pos:pos + length]).decode("utf-8", "replace"))
        pos += length


@dataclass(frozen=True)
class Mdns:
    response: bool
    questions: tuple
    answers: tuple          # (name, type, rdata_name or None)


def parse_mdns(msg):
    msg = bytes(msg)
    if len(msg) < 12:
        raise MalformedFrame("DNS header truncated")
    _id, flags, qd, an, ns, ar = struct.unpack_from(">HHHHHH", msg, 0)
    if qd + an + ns + ar > MAX_RECORDS:
        raise MalformedFrame("too many DNS records")
    pos, questions, answers = 12, [], []
    for _ in range(qd):
        name, pos = _read_name(msg, pos)
        if pos + 4 > len(msg):
            raise MalformedFrame("DNS question truncated")
        questions.append((name, struct.unpack_from(">H", msg, pos)[0]))
        pos += 4
    for _ in range(an + ns + ar):
        name, pos = _read_name(msg, pos)
        if pos + 10 > len(msg):
            raise MalformedFrame("DNS record truncated")
        rtype, _cls, _ttl, rdlen = struct.unpack_from(">HHIH", msg, pos)
        pos += 10
        if pos + rdlen > len(msg):
            raise MalformedFrame("DNS rdata truncated")
        target = None
        if rtype in (12, 5):                       # PTR, CNAME
            target, _ = _read_name(msg, pos)
        elif rtype == 33 and rdlen >= 7:           # SRV: priority, weight, port, target
            target, _ = _read_name(msg, pos + 6)
        answers.append((name, rtype, target))
        pos += rdlen
    return Mdns(bool(flags & 0x8000), tuple(questions), tuple(answers))


def announces_airdrop(mdns):
    """True when an mDNS response names the AirDrop service (PTR for `_airdrop._tcp.local`)."""
    return mdns.response and any(name == AIRDROP_SERVICE or (t and t.endswith("." + AIRDROP_SERVICE))
                                 for name, _type, t in mdns.answers)


def asks_for_airdrop(mdns):
    return (not mdns.response) and any(name == AIRDROP_SERVICE for name, _t in mdns.questions)


def classify(ipv6):
    """A short label for traffic statistics: 'mdns', 'airdrop-tcp', 'tcp', 'udp', 'icmpv6' or 'other'."""
    if ipv6.next_header == 17:
        try:
            udp = parse_udp(ipv6.payload)
        except MalformedFrame:
            return "udp"
        return "mdns" if MDNS_PORT in (udp.sport, udp.dport) else "udp"
    if ipv6.next_header == 6 and len(ipv6.payload) >= 4:
        return "airdrop-tcp" if AIRDROP_PORT in struct.unpack_from(">HH", ipv6.payload, 0) else "tcp"
    if ipv6.next_header == 58:
        return "icmpv6"
    return "other"
