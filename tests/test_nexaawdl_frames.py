#!/usr/bin/python3
"""NexaAWDL frame codec: round trips, strict validation, hostile input, and real frames from an iPhone."""
import random
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nexa_helpers as h
from nexaawdl import dot11, frames


class RealFrames(unittest.TestCase):
    """Frames captured from a real iPhone (truncated by the capture at 96 bytes; the AWDL header is intact)."""

    def test_every_captured_action_frame_decodes(self):
        for kind, subtype in (("psf", frames.SUBTYPE_PSF), ("mif", frames.SUBTYPE_MIF)):
            for packet in h.real_frames(kind):
                meta, frame = dot11.split_capture(packet)
                self.assertTrue(frame.is_awdl_action)
                self.assertEqual(frame.addr3, dot11.AWDL_BSSID)
                self.assertEqual(frame.addr1, h.BROADCAST)
                action = frames.parse_action(frame.body)
                self.assertEqual(action.subtype, subtype)
                self.assertEqual(action.version, frames.AWDL_VERSION)

    def test_radiotap_metadata_of_a_real_frame(self):
        meta, frame = dot11.split_capture(h.real_frames("psf")[0])
        self.assertEqual(meta.freq, 2437)
        self.assertIsNotNone(meta.tsft)
        self.assertLess(meta.signal, 0)
        self.assertEqual(meta.rate_mbps, 12.0)       # what this iPhone's frames carried

    def test_transmit_clock_runs_at_our_rate(self):
        """phy_tx minus our hardware timestamp is constant to a few microseconds: the clocks tick together."""
        diffs = []
        for packet in h.real_frames("psf") + h.real_frames("mif"):
            meta, frame = dot11.split_capture(packet)
            action = frames.parse_action(frame.body)
            diffs.append((action.phy_tx - meta.tsft) & 0xFFFFFFFF)
        self.assertLess(max(diffs) - min(diffs), 10_000)        # ms-scale spread would mean different rates

    def test_real_data_frames_are_awdl_data(self):
        for packet in h.real_frames("data_qos"):
            meta, frame = dot11.split_capture(packet)
            self.assertTrue(frame.is_data)
            self.assertEqual(frame.addr3, dot11.AWDL_BSSID)
            self.assertEqual(frame.body[:8], frames.LLC_SNAP)


class RoundTrips(unittest.TestCase):
    def roundtrip(self, tlv):
        body = h.psf([tlv])
        parsed = frames.parse_action(body).tlvs[0]
        self.assertEqual(parsed, tlv)
        self.assertEqual(frames.encode_action(frames.parse_action(body)), body)

    def test_sync_parameters(self):
        self.roundtrip(h.sync_params(down=33, seq=1234))

    def test_channel_sequence_tlv(self):
        self.roundtrip(frames.ChanSeqTlv(h.sequence((0, 44), (8, 6), (10, 44))))

    def test_election_parameters(self):
        self.roundtrip(frames.ElectionParams(0, 5, 2, h.PEER_A, 90, 80))
        self.roundtrip(h.election_v2())

    def test_small_tlvs(self):
        self.roundtrip(frames.VersionTlv(0x30, 7))
        self.roundtrip(frames.ArpaTlv(3, "foglio-pc"))
        self.roundtrip(frames.ServiceParams(b"\0\0\0", 9, 0xFF))
        self.roundtrip(frames.SyncTree((h.PEER_A, h.PEER_B)))

    def test_data_path_state_with_each_optional_field(self):
        flags = (frames.FLAG_COUNTRY | frames.FLAG_SOCIAL_MAP | frames.FLAG_INFRA_INFO | frames.FLAG_INFRA_ADDR
                 | frames.FLAG_AWDL_ADDR)
        self.roundtrip(frames.DataPathState(flags, "IT", 7, h.PEER_B, 36, h.PEER_A, h.PEER_B))
        self.roundtrip(frames.DataPathState(0))

    def test_unknown_tlvs_are_kept_verbatim(self):
        frame = frames.ActionFrame(frames.SUBTYPE_PSF, 1, 2, (frames.UnknownTlv(99, b"\x01\x02\x03"),))
        again = frames.parse_action(frames.encode_action(frame))
        self.assertEqual(again.tlvs[0], frames.UnknownTlv(99, b"\x01\x02\x03"))

    def test_both_channel_encodings(self):
        simple = frames.make_chanseq([6] + [0] * 15, frames.ENC_SIMPLE)
        legacy = frames.make_chanseq([0, 44] + [0] * 14, frames.ENC_LEGACY)
        for seq in (simple, legacy):
            decoded, _ = frames.decode_chanseq(frames.encode_chanseq(seq))
            self.assertEqual(decoded.channels, seq.channels)

    def test_channel_text_matches_the_form_the_logs_use(self):
        seq = h.sequence((0, 44), (2, 149), (8, 6), (10, 44))
        self.assertEqual(seq.text(), "44,0,149,0,0,0,0,0,6,0,44,0,0,0,0,0")
        self.assertEqual(seq.slots_on(6), [8])
        self.assertEqual(seq.distinct(), [6, 44, 149])

    def test_data_frame_header(self):
        payload = b"ipv6-bytes"
        header, rest = frames.parse_data(frames.encode_data(77, frames.ETH_IPV6, payload))
        self.assertEqual((header.sequence, header.ethertype, rest), (77, frames.ETH_IPV6, payload))


class Validation(unittest.TestCase):
    def bad(self, data):
        with self.assertRaises(frames.MalformedFrame):
            frames.parse_action(data)

    def test_header_checks(self):
        good = h.psf([])
        self.bad(good[:15])
        self.bad(bytes((0,)) + good[1:])                         # wrong category
        self.bad(good[:1] + b"\x00\x17\xf3" + good[4:])           # wrong OUI
        self.bad(good[:5] + b"\x20" + good[6:])                   # unsupported version
        self.bad(good[:6] + b"\x07" + good[7:])                   # unsupported subtype

    def test_tlv_bounds(self):
        good = h.psf([frames.VersionTlv(1, 2)])
        self.bad(good[:-1])                                       # cut inside a TLV
        self.bad(good[:16] + struct.pack("<BH", 21, 60000) + b"\0\0")   # absurd length
        self.bad(h.psf([]) + struct.pack("<BH", 21, 10) + b"\0")  # claims more than present

    def test_malformed_tlv_contents(self):
        def one(type_, value):
            return h.psf([]) + struct.pack("<BH", type_, len(value)) + value
        self.bad(one(4, b"\0" * 10))                              # sync parameters too short
        body = bytearray(frames.SyncParams.encode(h.sync_params()))
        body[27] = 0                                              # presence mode zero
        self.bad(one(4, bytes(body)))
        body = bytearray(frames.SyncParams.encode(h.sync_params()))
        body[5:7] = b"\0\0"                                       # zero AW period
        self.bad(one(4, bytes(body)))
        bad_seq = bytearray(frames.encode_chanseq(h.sequence((0, 6))))
        bad_seq[0] = 7                                            # not 16 slots
        self.bad(one(18, bytes(bad_seq) + b"\0\0\0"))
        bad_seq = bytearray(frames.encode_chanseq(h.sequence((0, 6))))
        bad_seq[1] = 9                                            # unknown encoding
        self.bad(one(18, bytes(bad_seq) + b"\0\0\0"))
        self.bad(one(20, b"\0" * 7))                              # sync tree not whole addresses
        self.bad(one(12, struct.pack("<H", frames.FLAG_INFRA_INFO)))   # flagged field missing

    def test_too_many_tlvs(self):
        many = h.psf([]) + b"".join(struct.pack("<BH", 99, 0) for _ in range(frames.MAX_TLVS + 1))
        self.bad(many)

    def test_short_trailing_bytes_are_padding(self):
        parsed = frames.parse_action(h.psf([frames.VersionTlv(1, 2)]) + b"\0\0")
        self.assertEqual(len(parsed.tlvs), 1)

    def test_data_frame_checks(self):
        with self.assertRaises(frames.MalformedFrame):
            frames.parse_data(b"\0" * 20)
        with self.assertRaises(frames.MalformedFrame):
            frames.parse_data(frames.LLC_SNAP + struct.pack("<HHH", 0x0404, 0, 0) + b"\x86\xdd")
        with self.assertRaises(frames.MalformedFrame):
            frames.parse_data(b"short")


class Fuzzing(unittest.TestCase):
    """Whatever the input, the only exception is MalformedFrame."""

    def valid_frames(self):
        return [h.psf([h.sync_params(), frames.ChanSeqTlv(h.sequence((0, 44), (8, 6))), h.election_v2(),
                       frames.VersionTlv(0x30, 7), frames.ArpaTlv(3, "peer"),
                       frames.DataPathState(frames.FLAG_COUNTRY | frames.FLAG_SOCIAL_MAP, "IT", 7)])]

    def test_random_bytes(self):
        rng = random.Random(1)
        for _ in range(3000):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 200)))
            try:
                frames.parse_action(data)
            except frames.MalformedFrame:
                pass

    def test_every_truncation_of_a_valid_frame(self):
        for good in self.valid_frames():
            for cut in range(len(good)):
                try:
                    frames.parse_action(good[:cut])
                except frames.MalformedFrame:
                    pass

    def test_single_byte_corruption(self):
        rng = random.Random(2)
        for good in self.valid_frames():
            for _ in range(3000):
                data = bytearray(good)
                for _ in range(rng.randrange(1, 4)):
                    data[rng.randrange(len(data))] = rng.randrange(256)
                try:
                    frames.parse_action(bytes(data))
                except frames.MalformedFrame:
                    pass

    def test_radiotap_and_80211_garbage(self):
        rng = random.Random(3)
        for _ in range(3000):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 120)))
            try:
                dot11.split_capture(data)
            except frames.MalformedFrame:
                pass
        good = h.action_frame(h.PEER_A, h.psf([h.sync_params()]))
        for cut in range(len(good)):
            try:
                dot11.split_capture(good[:cut])
            except frames.MalformedFrame:
                pass


class Radiotap(unittest.TestCase):
    def test_synthetic_metadata(self):
        packet = h.action_frame(h.PEER_A, h.psf([]), tsft=123456, freq=5220, signal=-61)
        meta, frame = dot11.split_capture(packet)
        self.assertEqual((meta.tsft, meta.freq, meta.signal), (123456, 5220, -61))
        self.assertEqual(frame.addr2, h.PEER_A)
        self.assertTrue(frame.is_awdl_action)

    def test_bad_fcs_flag_and_fcs_removal(self):
        packet = h.action_frame(h.PEER_A, h.psf([]), flags=dot11.FLAG_BAD_FCS | dot11.FLAG_FCS_AT_END) + b"\0\0\0\0"
        meta, frame = dot11.split_capture(packet)
        self.assertTrue(meta.bad_fcs and meta.fcs_at_end)
        self.assertEqual(len(frame.body), len(h.psf([])))        # the four FCS bytes are not part of the body

    def test_non_awdl_action_frame_is_not_awdl(self):
        packet = h.action_frame(h.PEER_A, h.psf([]), bssid=bytes.fromhex("0102030405ff"))
        _meta, frame = dot11.split_capture(packet)
        self.assertFalse(frame.is_awdl_action)


if __name__ == "__main__":
    unittest.main(verbosity=2)
