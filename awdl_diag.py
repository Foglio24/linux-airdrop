"""Receive-only summaries of IPv6 packets seen on awdl0, for the session log.

Only protocol shape is kept (mDNS names and record types, TCP flags towards the
AirDrop port). Payloads and file data are never stored.
"""
import struct

AIRDROP_PORT = 8771
RECORD_TYPES = {1: "A", 12: "PTR", 16: "TXT", 28: "AAAA", 33: "SRV", 47: "NSEC", 255: "ANY"}
TCP_FLAGS = ((0x02, "SYN"), (0x10, "ACK"), (0x01, "FIN"), (0x04, "RST"), (0x08, "PSH"))
MAX_NAME = 60


def read_name(data, pos):
    """Decode a DNS name at `pos`; return (name, position after it). Bounded, no recursion."""
    labels, end = [], None
    for _ in range(64):
        if pos >= len(data):
            raise ValueError("name outside packet")
        size = data[pos]
        if size == 0:
            return ".".join(labels), (pos + 1 if end is None else end)
        if size & 0xC0 == 0xC0:
            if pos + 1 >= len(data):
                raise ValueError("truncated pointer")
            if end is None:
                end = pos + 2
            pos = ((size & 0x3F) << 8) | data[pos + 1]
        elif size & 0xC0:
            raise ValueError("bad label")
        else:
            if pos + 1 + size > len(data):
                raise ValueError("truncated label")
            labels.append(data[pos + 1:pos + 1 + size].decode("ascii", "replace"))
            pos += 1 + size
    raise ValueError("name too long")


def record(name, rtype):
    return f"{name[:MAX_NAME]}/{RECORD_TYPES.get(rtype, rtype)}"


def mdns_summary(dns):
    """One line: queries as `Q a/PTR`, answers as `R a/SRV`, with known-answer counts."""
    if len(dns) < 12:
        raise ValueError("short dns header")
    _, flags, questions, answers, _, _ = struct.unpack("!6H", dns[:12])
    pos, asked, given = 12, [], []
    for _ in range(min(questions, 6)):
        name, pos = read_name(dns, pos)
        if pos + 4 > len(dns):
            raise ValueError("truncated question")
        (rtype, _), pos = struct.unpack("!HH", dns[pos:pos + 4]), pos + 4
        asked.append(record(name, rtype))
    if questions > 6:
        return "Q " + ",".join(asked) + ",…"
    for _ in range(min(answers, 8)):
        name, pos = read_name(dns, pos)
        if pos + 10 > len(dns):
            break
        rtype, _, _, length = struct.unpack("!HHIH", dns[pos:pos + 10])
        pos += 10 + length
        given.append(record(name, rtype))
    if flags & 0x8000:
        return "R " + ",".join(given) + (",…" if answers > 8 else "")
    text = "Q " + ",".join(asked)
    return text + f" (+{answers} known)" if answers else text


def summarize_packet(packet, outgoing):
    """Return a short bounded description of an IPv6 packet, or None if not interesting."""
    if len(packet) < 40 or packet[0] >> 4 != 6:
        return None
    kind = ("TX " if outgoing else "RX ") + "IPv6/" + str(packet[6])
    try:
        if packet[6] == 17 and len(packet) >= 48:
            src, dst = struct.unpack("!HH", packet[40:44])
            if src == 5353 or dst == 5353:
                return kind + " mDNS " + mdns_summary(packet[48:])
            return kind + f" UDP {src}>{dst}"
        if packet[6] == 6 and len(packet) >= 54:
            src, dst = struct.unpack("!HH", packet[40:44])
            if AIRDROP_PORT in (src, dst):
                flags = "+".join(name for bit, name in TCP_FLAGS if packet[53] & bit) or "none"
                return kind + (f" ->{AIRDROP_PORT} " if dst == AIRDROP_PORT else f" {AIRDROP_PORT}-> ") + flags
            return kind + " TCP other"
        if packet[6] == 58 and len(packet) >= 41:
            return kind + f" ICMPtype={packet[40]}"
    except (ValueError, struct.error):
        return kind + " (non decodificabile)"
    return kind
