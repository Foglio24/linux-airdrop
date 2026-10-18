#!/usr/bin/python3
"""NexaAWDL core: window clock, clock offset, election, peer table, IPv6/mDNS reading, hardware probe."""
import struct
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nexa_helpers as h
from nexaawdl import election, frames, hwprobe, peers, sync, transport

TU = 1024


class AwClockTests(unittest.TestCase):
    def synced(self, t=10_000_000, down=20, seq=40):
        clock = sync.AwClock()
        clock.observe(t, down, seq)
        return clock

    def test_first_observation_gives_no_error_and_defines_the_epoch(self):
        clock = sync.AwClock()
        self.assertFalse(clock.synchronised)
        self.assertEqual(clock.observe(1_000_000, 30, 8), 0)
        self.assertTrue(clock.synchronised)

    def test_window_positions_follow_the_announced_phase(self):
        t = 10_000_000
        clock = self.synced(t, down=20, seq=40)
        # 20 TU before an EAW boundary at counter 40: the EAW started 44 TU ago, counter 40.
        now = clock.window(t)
        self.assertEqual(now.eaw_index, 10)
        self.assertEqual(now.to_next_eaw_us, 20 * TU)
        later = clock.window(t + 20 * TU)                    # exactly at the next EAW
        self.assertEqual(later.eaw_index, 11)
        self.assertEqual(later.aw_index, 44)
        self.assertEqual(later.slot, 11 % 16)

    def test_slot_cycle_is_sixteen_extended_windows(self):
        clock = self.synced()
        cycle = 16 * 4 * 16 * TU
        base = clock.window(10_000_000 + 5 * TU)
        again = clock.window(10_000_000 + 5 * TU + cycle)
        self.assertEqual(base.slot, again.slot)
        self.assertEqual(again.eaw_index - base.eaw_index, 16)

    def test_consistent_observations_stay_in_sync(self):
        clock = sync.AwClock()
        t0, eaw = 5_000_000, 64 * TU
        clock.observe(t0, 40, 0)
        for k in range(1, 40):
            t = t0 + k * 10 * TU * 3 + 123
            # what an ideal peer would announce at time t
            window = clock.window(t)
            down = window.to_next_eaw_us // TU
            seq = (window.eaw_index) * 4
            self.assertLessEqual(abs(clock.observe(t, down, seq)), 1)
        q = clock.quality()
        self.assertEqual(q["in_sync"], 100.0)
        self.assertEqual(q["misses"], 0)

    def test_a_shifted_peer_is_counted_as_a_miss(self):
        clock = self.synced(10_000_000, 20, 40)
        window = clock.window(10_000_000 + 5 * TU)
        wrong = window.to_next_eaw_us // TU + 30
        error = clock.observe(10_000_000 + 5 * TU, wrong, 40)
        self.assertGreater(abs(error), sync.SYNC_ERROR_THRESHOLD_TU)
        self.assertEqual(clock.misses, 1)

    def test_changing_the_period_invalidates_the_epoch(self):
        clock = self.synced()
        clock.configure(16, 4)
        self.assertTrue(clock.synchronised)
        clock.configure(16, 2)
        self.assertFalse(clock.synchronised)
        with self.assertRaises(ValueError):
            clock.window(0)

    def test_the_16_bit_counter_wrapping_is_not_a_sync_error(self):
        clock = sync.AwClock()
        t = 10_000_000
        clock.observe(t, 20, 65532)                       # just before the counter wraps
        window = clock.window(t + 50 * TU)                # a few windows later the peer announces small numbers
        self.assertEqual(window.aw_index, 1)               # 65532 + 4 + 1 AW into the next EAW, wrapped at 16 bits
        down = window.to_next_eaw_us // TU
        self.assertEqual(clock.observe(t + 50 * TU, down, window.aw_index), 0)
        self.assertEqual(clock.misses, 0)

    def test_counter_difference_across_many_windows(self):
        clock = self.synced(0, 10, 400)
        far = clock.window(2_000_000_000)
        self.assertEqual(far.aw_index // 4, far.eaw_index)
        self.assertEqual(far.slot, far.eaw_index % 16)


class OffsetTests(unittest.TestCase):
    def test_median_offset_and_spread(self):
        off = sync.ClockOffset()
        self.assertIsNone(off.offset)
        for tsft, jitter in ((1000, 0), (2000, 1), (3000, -1), (4000, 0), (5000, 2)):
            off.add(tsft, tsft + 777 + jitter)
        self.assertEqual(off.offset, 777)
        self.assertEqual(off.spread_us(), 3)

    def test_32_bit_wrap_is_handled(self):
        off = sync.ClockOffset()
        off.add(10, 5)                       # peer clock wrapped below ours
        self.assertEqual(off.offset, (5 - 10) & 0xFFFFFFFF)


class OverlapTests(unittest.TestCase):
    def test_overlap_and_listening(self):
        a = h.sequence((0, 149), (8, 6), (10, 44)).channels
        b = h.sequence((1, 149), (8, 6), (10, 36)).channels
        self.assertEqual(sync.overlap(a, b), [8])
        self.assertTrue(sync.peer_listens_on(a, 8, 6))
        self.assertFalse(sync.peer_listens_on(a, 8, 44))
        self.assertFalse(sync.peer_listens_on(a, 99, 6))
        self.assertFalse(sync.peer_listens_on(a, 3, 0))


class ElectionTests(unittest.TestCase):
    A, B, C = (bytes((0, 0, 0, 0, 0, n)) for n in (1, 2, 3))

    def cand(self, addr, master=None, sync_=None, metric=60, counter=0, height=0):
        return election.Candidate(addr, master or addr, sync_ or addr, metric, counter, metric, counter, height)

    def test_alone_we_are_master(self):
        e = election.Election.new(self.A)
        self.assertFalse(e.run([]))
        self.assertTrue(e.is_master())
        self.assertIsNone(e.following())

    def test_follow_the_peer_with_the_higher_metric(self):
        e = election.Election.new(self.A)
        self.assertTrue(e.run([self.cand(self.B, metric=100), self.cand(self.C, metric=80)]))
        self.assertEqual(e.following(), self.B)
        self.assertEqual(e.me.height, 1)
        self.assertEqual(e.me.master_metric, 100)

    def test_metric_beats_counter_for_apple_and_counter_beats_metric_otherwise(self):
        low_metric_high_counter = self.cand(self.B, metric=70, counter=9)
        high_metric_low_counter = self.cand(self.C, metric=90, counter=1)
        apple = election.Election.new(self.A, metric_first=True)
        apple.run([low_metric_high_counter, high_metric_low_counter])
        self.assertEqual(apple.following(), self.C)
        older = election.Election.new(self.A, metric_first=False)
        older.run([low_metric_high_counter, high_metric_low_counter])
        self.assertEqual(older.following(), self.B)

    def test_tie_goes_to_the_larger_address_and_the_shorter_tree(self):
        e = election.Election.new(self.A)
        e.run([self.cand(self.B, metric=80), self.cand(self.C, metric=80)])
        self.assertEqual(e.following(), self.C)
        deep = self.cand(self.B, metric=80, height=3)
        shallow = self.cand(self.C, metric=80, height=1)
        e2 = election.Election.new(self.A)
        e2.run([deep, shallow])
        self.assertEqual(e2.following(), self.C)

    def test_no_cycles_and_no_deep_trees(self):
        e = election.Election.new(self.A)
        follows_us = self.cand(self.B, master=self.A, sync_=self.A, metric=200)
        too_deep = self.cand(self.C, metric=200, height=election.MAX_TREE_HEIGHT)
        self.assertFalse(e.run([follows_us, too_deep]))
        self.assertTrue(e.is_master())

    def test_reports_change_only_once(self):
        e = election.Election.new(self.A)
        peers_ = [self.cand(self.B, metric=100)]
        self.assertTrue(e.run(peers_))
        self.assertFalse(e.run(peers_))
        self.assertTrue(e.run([]))                      # the master went away: we lead again


class PeerTableTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.table = peers.PeerTable(timeout_us=1000, limit=3, on_event=lambda kind, **i: self.events.append((kind, i)))

    def test_new_peer_and_counters(self):
        p = self.table.touch(h.PEER_A, 10, rssi=-50)
        self.table.touch(h.PEER_A, 20, rssi=-48)
        self.assertEqual((p.frames, p.rssi, p.state), (2, -48, "heard"))
        self.assertEqual([k for k, _ in self.events], ["peer_new"])

    def test_expiry_then_return_then_forget(self):
        self.table.touch(h.PEER_A, 0)
        self.assertEqual(self.table.expire(500), [])
        self.assertEqual(self.table.expire(1500), [h.PEER_A and "02:aa:bb:cc:dd:01"])
        self.assertEqual(self.table.active(), [])
        self.table.touch(h.PEER_A, 1600)
        self.assertEqual(self.table.peers[h.PEER_A].state, "heard")
        self.table.expire(1600 + 1500)
        self.table.expire(1600 + 3200)
        self.assertNotIn(h.PEER_A, self.table.peers)

    def test_table_is_bounded(self):
        for n in range(3):
            self.table.touch(bytes((2, 0, 0, 0, 0, n)), 100)
        self.assertIsNone(self.table.touch(bytes((2, 0, 0, 0, 0, 9)), 100))
        self.assertEqual(self.table.dropped, 1)
        self.assertEqual(len(self.table.peers), 3)

    def test_sequence_changes_are_counted_and_airdrop_is_separate_from_presence(self):
        p = self.table.touch(h.PEER_A, 0)
        self.table.note_chanseq(p, h.sequence((0, 149), (8, 6)))
        self.table.note_chanseq(p, h.sequence((0, 149), (8, 6)))
        self.assertEqual(p.chanseq_changes, 0)
        self.table.note_chanseq(p, h.sequence((0, 44), (8, 6)))
        self.assertEqual(p.chanseq_changes, 1)
        self.assertEqual(p.airdrop, "unknown")              # heard on AWDL, no AirDrop announcement yet
        self.table.mark_airdrop(h.PEER_A, 50)
        self.assertEqual(p.airdrop, "advertised")

    def test_election_tlvs_become_candidates(self):
        p = self.table.touch(h.PEER_A, 0)
        self.table.update_election(p, h.election_v2(master=h.PEER_B, metric=120, counter=4, distance=2))
        self.assertEqual((p.candidate.master, p.candidate.master_metric, p.candidate.height), (h.PEER_B, 120, 2))
        self.table.update_election(p, frames.ElectionParams(0, 1, 1, h.PEER_B, 130, 50))
        self.assertEqual(p.candidate.master_metric, 130)

    def test_snapshot_is_json_friendly(self):
        import json
        p = self.table.touch(h.PEER_A, 0)
        self.table.note_chanseq(p, h.sequence((0, 149)))
        json.dumps(self.table.snapshot(100))


class TransportTests(unittest.TestCase):
    def test_link_local_rule_matches_real_addresses(self):
        # the iPhone of the project's captures: AWDL MAC 32:57:70:60:8d:ec <-> fe80::3057:70ff:fe60:8dec
        mac = bytes.fromhex("325770608dec")
        addr = transport.link_local(mac)
        self.assertEqual(addr.hex(), "fe80000000000000" + "3057" + "70" + "fffe" + "608dec")
        self.assertEqual(transport.mac_from_link_local(addr), mac)
        self.assertIsNone(transport.mac_from_link_local(bytes(16)))

    def test_ipv6_udp_mdns_airdrop_announcement(self):
        packet = transport.parse_ipv6(h.ipv6_udp(h.PEER_A, h.mdns_response()))
        self.assertEqual(transport.classify(packet), "mdns")
        mdns = transport.parse_mdns(transport.parse_udp(packet.payload).payload)
        self.assertTrue(transport.announces_airdrop(mdns))

    def test_other_services_are_not_airdrop(self):
        mdns = transport.parse_mdns(h.mdns_response(service="_spotify-connect._tcp.local", instance="x"))
        self.assertFalse(transport.announces_airdrop(mdns))

    def test_a_question_is_not_an_announcement(self):
        question = struct.pack(">HHHHHH", 0, 0, 1, 0, 0, 0) + b"\x08_airdrop\x04_tcp\x05local\x00" + struct.pack(">HH", 12, 1)
        mdns = transport.parse_mdns(question)
        self.assertFalse(transport.announces_airdrop(mdns))
        self.assertTrue(transport.asks_for_airdrop(mdns))

    def test_hostile_dns(self):
        loop = struct.pack(">HHHHHH", 0, 0x8400, 0, 1, 0, 0) + b"\xc0\x0c" + struct.pack(">HHIH", 12, 1, 1, 0)
        for bad in (b"", b"\0" * 5, loop, struct.pack(">HHHHHH", 0, 0, 5000, 0, 0, 0)):
            with self.assertRaises(frames.MalformedFrame):
                transport.parse_mdns(bad)

    def test_hostile_ip_and_udp(self):
        for bad in (b"", b"\x60" + b"\0" * 10, b"\x40" + b"\0" * 39):
            with self.assertRaises(frames.MalformedFrame):
                transport.parse_ipv6(bad)
        with self.assertRaises(frames.MalformedFrame):
            transport.parse_ipv6(struct.pack(">IHBB", 0x60000000, 500, 17, 1) + bytes(32))
        with self.assertRaises(frames.MalformedFrame):
            transport.parse_udp(b"\0" * 5)

    def test_classification(self):
        tcp = struct.pack(">IHBB", 0x60000000, 20, 6, 64) + bytes(32) + struct.pack(">HH", 51000, 8771) + bytes(16)
        self.assertEqual(transport.classify(transport.parse_ipv6(tcp)), "airdrop-tcp")


class HardwareProbeTests(unittest.TestCase):
    def runner(self, argv):
        name = argv[0]
        files = {"iw": {"dev": "iw_dev.txt", "phy": "iw_phy_mt7922.txt"}, "lspci": "lspci.txt", "nmcli": "nmcli.txt",
                 "ethtool": "ethtool.txt"}
        if name == "iw":
            return h.fixture(files["iw"]["dev" if argv[1] == "dev" else "phy"])
        return h.fixture(files[name]) if name in files else None

    def test_mt7922_report(self):
        report = hwprobe.probe("wlp4s0", run=self.runner, sys_root="/nonexistent")
        d = report.as_dict()
        self.assertEqual(d["pci_id"], "14c3:0616")
        self.assertIn("MT7922", d["chipset"])
        self.assertEqual(d["pci_address"], "04:00.0")
        self.assertEqual(d["firmware"], "____000000-20260224103233")
        self.assertEqual(d["manager_state"], ["wifi", "disconnected"])
        self.assertTrue(d["monitor"] and d["active_monitor"] and d["p2p_go"])
        self.assertEqual(d["verdict"], "capable-needs-live-verification")

    def test_social_channels_are_read_by_number_not_confused_with_6_ghz(self):
        d = hwprobe.probe("wlp4s0", run=self.runner, sys_root="/nonexistent").as_dict()
        for channel in ("6", "44", "149"):
            self.assertTrue(d["social_channels"][int(channel)]["usable"])
        self.assertIn(1, d["bands"]["6"])                    # 6 GHz channel 1 is not lost behind 2.4 GHz channel 1
        self.assertEqual(d["bands"]["2.4"][0], 1)

    def test_combinations_and_limitations(self):
        d = hwprobe.probe("wlp4s0", run=self.runner, sys_root="/nonexistent").as_dict()
        self.assertEqual(max(c["channels"] for c in d["combinations"]), 2)
        self.assertTrue(any("monitor is not listed" in f for f in d["facts"]))
        self.assertTrue(any("frame injection" in n for n in d["not_verified"]))
        self.assertTrue(any("AirDrop interoperability" in n for n in d["not_verified"]))

    def test_missing_monitor_is_a_hardware_limit_and_incapable(self):
        phy = h.fixture("iw_phy_mt7922.txt").replace("\t\t * monitor\n", "")
        run = lambda argv: phy if argv[0] == "iw" and argv[1].startswith("phy") else self.runner(argv)
        d = hwprobe.probe("wlp4s0", run=run, sys_root="/nonexistent").as_dict()
        self.assertFalse(d["monitor"])
        self.assertEqual(d["verdict"], "incapable")
        self.assertTrue(any(l["kind"] == "hardware" for l in d["limitations"]))

    def test_restricted_social_channel_is_a_policy_limit(self):
        phy = h.fixture("iw_phy_mt7922.txt").replace("5220.0 MHz [44] (23.0 dBm)", "5220.0 MHz [44] (23.0 dBm) (no IR)")
        run = lambda argv: phy if argv[0] == "iw" and argv[1].startswith("phy") else self.runner(argv)
        d = hwprobe.probe("wlp4s0", run=run, sys_root="/nonexistent").as_dict()
        self.assertFalse(d["social_channels"][44]["usable"])
        self.assertTrue(any(l["kind"] == "policy" for l in d["limitations"]))

    def test_tools_failing_gives_a_report_instead_of_an_exception(self):
        d = hwprobe.probe(None, run=lambda argv: None, sys_root="/nonexistent").as_dict()
        self.assertEqual(d["verdict"], "incapable")
        self.assertIsNone(d["chipset"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
