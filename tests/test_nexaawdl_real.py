#!/usr/bin/python3
"""NexaAWDL against frames captured from a real iPhone with full-length TLVs (anonymised; see docs/NEXAAWDL.md)."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nexa_helpers as h
from nexaawdl import capture, dot11, engine, frames, transport

PCAP = h.FIXTURES / "real_full.pcap"
PHONE = bytes.fromhex("02aabbccdd01")
OWN = bytes.fromhex("02000000000f")


def real():
    return list(capture.PcapSource(PCAP))


def action_frames():
    for _ts, packet, _tr in real():
        _meta, frame = dot11.split_capture(packet)
        if frame.is_awdl_action:
            yield frame, frames.parse_action(frame.body)


class RealTlvs(unittest.TestCase):
    def test_every_real_action_frame_decodes_and_reencodes_byte_for_byte(self):
        count = 0
        for frame, action in action_frames():
            count += 1
            encoded = frames.encode_action(action)
            self.assertEqual(encoded, frame.body[:len(encoded)])
            self.assertLessEqual(len(frame.body) - len(encoded), 2)       # at most the padding
        self.assertGreater(count, 150)

    def test_the_tlv_inventory_of_a_real_iphone(self):
        _frame, action = next(action_frames())
        kinds = {type(t).__name__ for t in action.tlvs}
        self.assertEqual(kinds, {"SyncParams", "ChanSeqTlv", "ElectionParams", "ElectionParamsV2", "DataPathState",
                                 "ArpaTlv", "VersionTlv", "ServiceParams", "UnknownTlv"})
        self.assertEqual({t.type for t in action.tlvs if isinstance(t, frames.UnknownTlv)}, {2, 7, 17, 33})

    def test_sync_parameters_of_a_real_phone(self):
        _frame, action = next(action_frames())
        sync = action.find(frames.SyncParams)
        self.assertEqual((sync.aw_period, sync.presence_mode, sync.af_period, sync.master_addr), (16, 4, 110, PHONE))
        self.assertEqual(sync.master_channel, 6)
        self.assertEqual(sync.chanseq.encoding, frames.ENC_LEGACY)       # {flags, channel} entries here
        self.assertTrue(sync.next_aw_seq < 65536)

    def test_the_two_channel_lists(self):
        _frame, action = next(action_frames())
        self.assertEqual(action.find(frames.ChanSeqTlv).chanseq.encoding, frames.ENC_OPCLASS)
        primary = action.find(frames.ChanSeqTlv).chanseq.channels
        centres = action.find(frames.SyncParams).chanseq.channels
        # the sync parameters list 40 MHz centre channels (the primary +2 on 5 GHz); channel 6 is unchanged
        for p, c in zip(primary, centres):
            self.assertEqual(c, p + 2 if p >= 36 else p)

    def test_other_real_tlvs(self):
        _frame, action = next(action_frames())
        self.assertEqual(action.find(frames.DataPathState).country, "IT")
        self.assertEqual(action.find(frames.ElectionParams).top_master, PHONE)
        self.assertEqual(action.find(frames.ElectionParamsV2).master_addr, PHONE)
        self.assertEqual(action.find(frames.ArpaTlv).name, "00000000-0000-4000-8000-000000000001")
        self.assertEqual(action.find(frames.VersionTlv).device_class, 2)

    def test_real_ipv6_over_awdl(self):
        classes = set()
        for _ts, packet, _tr in real():
            _meta, frame = dot11.split_capture(packet)
            if frame.is_data:
                header, payload = frames.parse_data(frame.body)
                self.assertEqual(header.ethertype, frames.ETH_IPV6)
                classes.add(transport.classify(transport.parse_ipv6(payload)))
        self.assertIn("airdrop-tcp", classes)


class RealEngineRun(unittest.TestCase):
    def run_engine(self, own=()):
        e = engine.Engine(engine.EngineConfig(log_level="error"), "capture", own=own)
        e.start(0)
        last = 0
        for ts, packet, truncated in real():
            e.feed(packet, ts, truncated)
            if ts - last >= 1_000_000:
                last = ts
                e.tick(ts)
        e.tick(e.now)
        return e

    def test_synchronisation_on_real_frames(self):
        e = self.run_engine(own=[OWN])
        status = e.status()
        self.assertEqual(e.stats["malformed"], 0)
        self.assertEqual(e.stats["internal_errors"], 0)
        self.assertEqual(e.state, engine.SYNCHRONISED)
        self.assertGreaterEqual(status["sync"]["quality"]["in_sync"], 90.0)
        self.assertLess(status["sync"]["quality"]["mean_abs_error_tu"], 3)

    def test_the_window_model_agrees_with_what_the_phone_does(self):
        """Frames from the phone fall in the slots its own sequence lists for the channel they arrived on."""
        e = self.run_engine(own=[OWN])
        check = e.window_check()
        self.assertGreater(check["checked"], 100)
        self.assertGreaterEqual(check["consistent_pct"], 95.0)

    def test_our_own_frames_are_not_a_peer(self):
        with_filter = self.run_engine(own=[OWN])
        self.assertEqual([p["addr"] for p in with_filter.status()["peers"]], ["02:aa:bb:cc:dd:01"])
        self.assertGreater(with_filter.stats["own"], 0)
        without = self.run_engine()
        self.assertEqual(len(without.status()["peers"]), 2)           # the monitor read our own frames back

    def test_the_phone_sequence_as_owl_logs_it(self):
        e = self.run_engine(own=[OWN])
        peer = e.status()["peers"][0]
        # the phone changes its sequence as it gets busy (it was 100,0,149,... when idle); at the end of this
        # window it is the busy one, and the sync-parameter list is always the same slots +2 on 5 GHz
        self.assertEqual(peer["sequence"], "44,44,149,0,0,0,0,0,6,44,44,0,0,0,0,0")
        self.assertEqual(peer["sync_sequence"], "46,46,151,0,0,0,0,0,6,46,46,0,0,0,0,0")
        self.assertGreater(peer["sequence_changes"], 0)
        self.assertEqual(peer["country"], "IT")

    def test_the_peer_clock_relation_on_real_frames(self):
        e = self.run_engine(own=[OWN])
        spread = e.status()["sync"]["offset_spread_us"]
        self.assertLess(spread, 5_000)            # the offset stays within a few ms (it was within 0.7 ms over 12 minutes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
