"""Packet summaries for the AWDL session log: decode names, never keep payloads."""
import importlib.util
from pathlib import Path
import struct
import unittest

MODULE = Path(__file__).resolve().parents[1] / "awdl_diag.py"
spec = importlib.util.spec_from_file_location("awdl_diag_under_test", MODULE)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def name(text):
    return b"".join(bytes([len(p)]) + p.encode() for p in text.split(".")) + b"\0"


def ipv6(next_header, payload):
    return bytes([0x60, 0, 0, 0]) + struct.pack("!HBB", len(payload), next_header, 255) + b"\0" * 32 + payload


def udp(src, dst, dns):
    return ipv6(17, struct.pack("!HHHH", src, dst, 8 + len(dns), 0) + dns)


def tcp(src, dst, flags):
    header = struct.pack("!HHIIBBHHH", src, dst, 1, 0, 5 << 4, flags, 1000, 0, 0)
    return ipv6(6, header)


def question(text, rtype):
    return name(text) + struct.pack("!HH", rtype, 1)


def answer(text, rtype, rdata=b"\0\0\0\0"):
    return name(text) + struct.pack("!HHIH", rtype, 1, 120, len(rdata)) + rdata


class AwdlDiagnostics(unittest.TestCase):
    def test_ptr_query_for_airdrop_is_named(self):
        dns = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + question("_airdrop._tcp.local", 12)
        self.assertEqual(diag.summarize_packet(udp(5353, 5353, dns), False),
                         "RX IPv6/17 mDNS Q _airdrop._tcp.local/PTR")

    def test_known_answers_are_counted_not_listed(self):
        dns = (struct.pack("!6H", 0, 0, 1, 2, 0, 0) + question("_airdrop._tcp.local", 12)
               + answer("_airdrop._tcp.local", 12) * 2)
        self.assertEqual(diag.summarize_packet(udp(5353, 5353, dns), False),
                         "RX IPv6/17 mDNS Q _airdrop._tcp.local/PTR (+2 known)")

    def test_response_lists_record_types_and_follows_compression(self):
        body = answer("x.local", 33)
        pointer = b"\xc0\x0c"  # points at the first name, right after the 12-byte header
        second = pointer + struct.pack("!HHIH", 28, 1, 120, 16) + b"\0" * 16
        dns = struct.pack("!6H", 0, 0x8400, 0, 2, 0, 0) + body + second
        self.assertEqual(diag.summarize_packet(udp(5353, 5353, dns), True),
                         "TX IPv6/17 mDNS R x.local/SRV,x.local/AAAA")

    def test_tcp_to_airdrop_port_reports_flags_without_port_noise(self):
        self.assertEqual(diag.summarize_packet(tcp(49289, 8771, 0x02), False), "RX IPv6/6 ->8771 SYN")
        self.assertEqual(diag.summarize_packet(tcp(8771, 49289, 0x12), True), "TX IPv6/6 8771-> SYN+ACK")
        self.assertEqual(diag.summarize_packet(tcp(1111, 2222, 0x10), False), "RX IPv6/6 TCP other")

    def test_hostile_packets_never_raise_or_loop(self):
        loop = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + b"\xc0\x0c"  # pointer to itself
        for dns in (loop, b"\x00" * 4, struct.pack("!6H", 0, 0, 1, 0, 0, 0) + b"\x3f" + b"a" * 5,
                    struct.pack("!6H", 0, 0, 1, 0, 0, 0) + b"\x80", b""):
            result = diag.summarize_packet(udp(5353, 5353, dns), False)
            self.assertTrue(result.startswith("RX IPv6/17"), result)
        self.assertIsNone(diag.summarize_packet(b"", False))
        self.assertIsNone(diag.summarize_packet(b"\x45" + b"\0" * 60, False))

    def test_long_names_are_bounded(self):
        long = ".".join(["a" * 50] * 4)
        dns = struct.pack("!6H", 0, 0, 1, 0, 0, 0) + question(long, 16)
        summary = diag.summarize_packet(udp(5353, 5353, dns), False)
        self.assertLess(len(summary), 100)

    def test_icmp_and_plain_udp_stay_compact(self):
        self.assertEqual(diag.summarize_packet(ipv6(58, bytes([133]) + b"\0" * 7), True), "TX IPv6/58 ICMPtype=133")
        self.assertEqual(diag.summarize_packet(udp(57154, 3702, b"x" * 8), True), "TX IPv6/17 UDP 57154>3702")


if __name__ == "__main__":
    unittest.main(verbosity=2)
