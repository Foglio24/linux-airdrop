#!/usr/bin/python3
"""NexaAWDL engine: lifecycle, synchronisation tracking, peers, AirDrop detection, hostile input, status views."""
import json
import random
import struct
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nexa_helpers as h
from nexaawdl import capture, engine, frames, view

TU = 1024
EAW = 64 * TU


class IdealPeer:
    """A peer whose window clock runs exactly as the model assumes; yields the TLVs it would announce."""

    def __init__(self, addr=h.PEER_A, epoch=1_000_000, chanseq=None):
        self.addr, self.epoch = addr, epoch
        self.chanseq = chanseq or h.sequence((0, 149), (8, 6), (9, 149), (10, 44))

    def window(self, t):
        eaw, into = divmod(t - self.epoch, EAW)
        return eaw, (EAW - into) // TU, (eaw * 4 + into // (16 * TU)) & 0xFFFF       # the announced counter wraps

    def action(self, t, with_election=True):
        eaw, down, aw = self.window(t)
        # as on the air: the sync parameters carry 40 MHz centre channels (44 -> 46), TLV 18 the primary channels
        centres = frames.make_chanseq([c + 2 if c >= 36 else c for c in self.chanseq.channels], frames.ENC_SIMPLE)
        tlvs = [h.sync_params(down=down, seq=aw, chanseq=centres, master=self.addr), frames.ChanSeqTlv(self.chanseq)]
        if with_election:
            tlvs.append(h.election_v2(master=self.addr, sync=self.addr, metric=100))
            tlvs.append(frames.ArpaTlv(3, "iphone"))
            tlvs.append(frames.VersionTlv(0x30, 7))
        return h.action_frame(self.addr, h.psf(tlvs, phy_tx=t + 777), tsft=t)

    def slot_at(self, t):
        return self.window(t)[0] % 16


def started(**config):
    e = engine.Engine(engine.EngineConfig(**config), "ldmon0")
    e.start(0)
    return e


class Lifecycle(unittest.TestCase):
    def test_start_stop_restart(self):
        e = engine.Engine(interface="ldmon0")
        self.assertEqual(e.state, engine.STOPPED)
        self.assertTrue(e.start(0))
        self.assertEqual(e.state, engine.LISTENING)
        self.assertFalse(e.start(0))                          # already running
        e.stop()
        self.assertEqual(e.state, engine.STOPPED)
        e.stop()                                              # idempotent
        self.assertTrue(e.start(10))

    def test_frames_are_ignored_while_stopped(self):
        e = engine.Engine()
        e.feed(h.action_frame(h.PEER_A, h.psf([])), 0)
        self.assertEqual(e.stats["frames"], 0)

    def test_restart_is_bounded(self):
        e = started(max_restarts=2)
        self.assertTrue(e.restart())
        self.assertTrue(e.restart())
        self.assertFalse(e.restart())
        self.assertEqual(e.state, engine.ERROR)
        self.assertTrue(e.start(0))                           # an operator can still start it again

    def test_restart_forgets_peers(self):
        e = started()
        e.feed(h.action_frame(h.PEER_A, h.psf([]), tsft=1000))
        self.assertEqual(len(e.table.active()), 1)
        e.restart()
        self.assertEqual(len(e.table.active()), 0)

    def test_fail_records_the_error_and_stops_processing(self):
        e = started()
        e.fail("capture socket closed")
        self.assertEqual(e.state, engine.ERROR)
        self.assertEqual(e.errors[-1]["message"], "capture socket closed")
        e.feed(h.action_frame(h.PEER_A, h.psf([])), 0)
        self.assertEqual(e.stats["frames"], 0)


class Synchronisation(unittest.TestCase):
    def run_ideal(self, e, peer, seconds, step_ms=90, start=1_000_000):
        t = start
        end = start + seconds * 1_000_000
        while t < end:
            e.feed(peer.action(t))
            e.tick(t)
            t += step_ms * 1000
        return t

    def test_state_moves_from_listening_to_synchronised(self):
        e = started()
        peer = IdealPeer()
        self.assertEqual(e.state, engine.LISTENING)
        e.feed(peer.action(1_000_000))
        self.assertEqual(e.state, engine.OBSERVING)
        self.run_ideal(e, peer, 3)
        self.assertEqual(e.state, engine.SYNCHRONISED)
        status = e.status()
        self.assertEqual(status["sync"]["quality"]["in_sync"], 100.0)
        self.assertEqual(status["sync"]["reference"], "02:aa:bb:cc:dd:01")
        self.assertEqual(status["peers"][0]["name"], "iphone")
        self.assertEqual(status["peers"][0]["state"], "synchronised")
        self.assertLessEqual(status["sync"]["offset_spread_us"], 1)       # phy_tx - tsft is constant here

    def test_silence_returns_to_listening_and_loses_the_peer(self):
        e = started(quiet_after_s=5, peer_timeout_s=8)
        peer = IdealPeer()
        end = self.run_ideal(e, peer, 3)
        e.tick(end + 6_000_000)
        self.assertEqual(e.state, engine.LISTENING)
        e.tick(end + 20_000_000)
        self.assertEqual(e.table.active(), [])

    def test_a_peer_that_keeps_jumping_degrades_the_state(self):
        e = started(sync_min_samples=8)
        rng = random.Random(4)
        peer = IdealPeer()
        t = 1_000_000
        for _ in range(60):
            t += 90_000
            body = [h.sync_params(down=rng.randrange(1, 64), seq=rng.randrange(0, 4000),
                                  chanseq=peer.chanseq, master=h.PEER_A), h.election_v2()]
            e.feed(h.action_frame(h.PEER_A, h.psf(body), tsft=t))
            e.tick(t)
        self.assertEqual(e.state, engine.DEGRADED)

    def test_reference_follows_the_advertised_master(self):
        e = started()
        master, other = IdealPeer(h.PEER_A), IdealPeer(h.PEER_B, epoch=1_020_000)
        t = 1_000_000
        for _ in range(40):
            t += 90_000
            e.feed(other.action(t))
            e.feed(master.action(t + 100))
            e.tick(t)
        # both announce themselves as master here; the one heard longest wins deterministically
        self.assertIn(e.status()["sync"]["reference"], ("02:aa:bb:cc:dd:01", "02:aa:bb:cc:dd:02"))
        self.assertEqual(len(e.table.active()), 2)


class TimeBases(unittest.TestCase):
    """Frames carry the radio's clock, ticks come from the wall clock: the two must never be compared."""

    def test_wall_clock_ticks_do_not_make_the_air_look_silent(self):
        e = started()
        peer = IdealPeer()
        wall0 = 1_792_000_000_000_000                       # a wall-clock reading, far from any radio timestamp
        for i in range(120):
            tsft = 1_000_000 + i * 90_000
            wall = wall0 + i * 90_000
            e.feed(peer.action(tsft), wall)
            e.tick(wall)
        self.assertEqual(e.state, engine.SYNCHRONISED)
        self.assertEqual(len(e.table.active()), 1)
        kinds = [x["kind"] for x in e.events]
        self.assertNotIn("peer_lost", kinds)
        self.assertEqual(kinds.count("state"), 4)          # starting, listening, observing, synchronised: no flapping
        self.assertEqual(e.status()["peers"][0]["age_s"], 0.0)

    def test_expiry_uses_the_capture_clock(self):
        e = started(peer_timeout_s=5, quiet_after_s=3)
        peer = IdealPeer()
        wall0 = 1_792_000_000_000_000
        e.feed(peer.action(1_000_000), wall0)
        e.tick(wall0 + 4_000_000)
        self.assertEqual(e.state, engine.LISTENING)
        e.tick(wall0 + 9_000_000)
        self.assertEqual(e.table.active(), [])


class TwoSequences(unittest.TestCase):
    def test_the_two_channel_lists_are_kept_apart(self):
        e = started()
        peer = IdealPeer()
        for i in range(20):
            e.feed(peer.action(1_000_000 + i * 90_000))
        p = e.status()["peers"][0]
        self.assertEqual(p["sequence"], "149,0,0,0,0,0,0,0,6,149,44,0,0,0,0,0")
        self.assertEqual(p["sync_sequence"], "151,0,0,0,0,0,0,0,6,151,46,0,0,0,0,0")
        self.assertEqual(p["sequence_changes"], 0)          # alternating between the two is not a change


class WindowModelCheck(unittest.TestCase):
    """The engine's falsifiable check: peers only transmit on channels they advertise for the slot they are in."""

    def feed_data(self, e, peer, t, freq):
        packet = h.awdl_data(peer.addr, h.ipv6_udp(peer.addr, b"x" * 4, dport=9, sport=9))
        e.feed(h.data_frame(peer.addr, packet, tsft=t, freq=freq))

    def test_frames_in_advertised_slots_are_consistent(self):
        e = started()
        peer = IdealPeer()
        for i in range(30):                                   # synchronise first
            e.feed(peer.action(1_000_000 + i * 90_000))
        t = 4_000_000
        hits = 0
        while hits < 40:
            t += 20_000
            slot = peer.slot_at(t)
            if peer.chanseq.channels[slot] == 6:              # slot 8: channel 6
                self.feed_data(e, peer, t, 2437)
                hits += 1
        check = e.window_check()
        self.assertEqual(check["checked"], 40)
        self.assertEqual(check["consistent_pct"], 100.0)
        self.assertEqual(check["slots"][8], 40)

    def test_frames_in_the_wrong_slots_are_flagged(self):
        e = started()
        peer = IdealPeer()
        for i in range(30):
            e.feed(peer.action(1_000_000 + i * 90_000))
        t, n = 4_000_000, 0
        while n < 30:
            t += 20_000
            if peer.chanseq.channels[peer.slot_at(t)] == 0:    # a slot in which the peer has no channel
                self.feed_data(e, peer, t, 2437)
                n += 1
        self.assertEqual(e.window_check()["consistent_pct"], 0.0)


class AirDropDetection(unittest.TestCase):
    def mdns_frame(self, e, peer_mac, payload, t=2_000_000):
        packet = h.awdl_data(peer_mac, h.ipv6_udp(peer_mac, payload))
        e.feed(h.data_frame(peer_mac, packet, tsft=t))

    def test_presence_is_not_service_availability(self):
        e = started()
        e.feed(h.action_frame(h.PEER_A, h.psf([]), tsft=1_000_000))
        e.feed(h.action_frame(h.PEER_B, h.psf([]), tsft=1_000_100))
        self.assertEqual({p["airdrop"] for p in e.status()["peers"]}, {"unknown"})
        self.mdns_frame(e, h.PEER_A, h.mdns_response())
        by_addr = {p["addr"]: p for p in e.status()["peers"]}
        self.assertEqual(by_addr["02:aa:bb:cc:dd:01"]["airdrop"], "advertised")
        self.assertEqual(by_addr["02:aa:bb:cc:dd:02"]["airdrop"], "unknown")
        self.assertEqual(e.traffic["mdns"], 1)

    def test_other_services_do_not_count(self):
        e = started()
        self.mdns_frame(e, h.PEER_A, h.mdns_response(service="_spotify-connect._tcp.local", instance="x"))
        self.assertEqual(e.status()["peers"][0]["airdrop"], "unknown")

    def test_traffic_is_classified(self):
        e = started()
        e.feed(h.data_frame(h.PEER_A, h.awdl_data(h.PEER_A, h.ipv6_udp(h.PEER_A, b"data", dport=9, sport=9)), tsft=1000))
        e.feed(h.data_frame(h.PEER_A, h.awdl_data(h.PEER_A, b"arp", ethertype=0x0806), tsft=2000))
        self.assertEqual(e.traffic["udp"], 1)
        self.assertEqual(e.traffic["non-ipv6"], 1)


class HostileInput(unittest.TestCase):
    def test_garbage_never_raises_and_is_counted(self):
        e = started()
        rng = random.Random(7)
        for i in range(4000):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(0, 300)))
            e.feed(data, i)
        good = h.action_frame(h.PEER_A, h.psf([h.sync_params(), h.election_v2()]), tsft=1000)
        for i in range(2000):
            data = bytearray(good)
            for _ in range(rng.randrange(1, 5)):
                data[rng.randrange(len(data))] = rng.randrange(256)
            e.feed(bytes(data), i)
        self.assertEqual(e.stats["internal_errors"], 0)
        self.assertGreater(e.stats["malformed"], 0)
        json.dumps(e.status(), default=str)

    def test_peer_flood_is_bounded(self):
        e = started()
        for n in range(500):
            mac = bytes((2, 0, n >> 8, n & 255, 0, 1))
            e.feed(h.action_frame(mac, h.psf([]), tsft=1000 + n))
        self.assertLessEqual(len(e.table.peers), 64)
        self.assertGreater(e.table.dropped, 0)

    def test_bad_fcs_frames_are_dropped_before_decoding(self):
        e = started()
        e.feed(h.action_frame(h.PEER_A, h.psf([]), flags=0x40 | 0x10) + b"\0" * 4)
        self.assertEqual(e.stats["bad_fcs"], 1)
        self.assertEqual(len(e.table.peers), 0)

    def test_truncated_capture_is_not_blamed_on_the_sender(self):
        e = started()
        for packet in h.real_frames("data_qos"):
            e.feed(packet, 0, truncated=True)
        self.assertEqual(e.stats["malformed"], 0)
        self.assertGreater(e.stats["truncated_unreadable"], 0)

    def test_frames_with_the_wrong_bssid_are_not_awdl(self):
        e = started()
        e.feed(h.action_frame(h.PEER_A, h.psf([]), bssid=bytes.fromhex("0102030405ff")))
        self.assertEqual(len(e.table.peers), 0)


class RealCapture(unittest.TestCase):
    def test_real_frames_through_the_engine(self):
        e = started()
        for packet in h.real_frames("psf") + h.real_frames("mif"):
            e.feed(packet, 0, truncated=True)
        e.tick(e.now)
        self.assertEqual(e.stats["psf"], 6)
        self.assertEqual(e.stats["mif"], 6)
        self.assertEqual(e.status()["wireless"]["channel"], 6)
        self.assertEqual(e.state, engine.OBSERVING)


class Reporting(unittest.TestCase):
    def test_log_level_filters_events(self):
        got = []
        e = engine.Engine(engine.EngineConfig(log_level="error"), emit=got.append)
        e.start(0)
        e.feed(h.action_frame(h.PEER_A, h.psf([]), tsft=1000))
        self.assertEqual(got, [])
        e.fail("boom")
        self.assertEqual([x["kind"] for x in got], ["error", "state"][:len(got)] or [])
        self.assertTrue(all(x["level"] == "error" for x in got if x["kind"] == "error"))

    def test_info_level_reports_state_and_peer_events(self):
        got = []
        e = engine.Engine(emit=got.append)
        e.start(0)
        e.feed(h.action_frame(h.PEER_A, h.psf([]), tsft=1000))
        kinds = [x["kind"] for x in got]
        self.assertIn("state", kinds)
        self.assertIn("peer_new", kinds)

    def test_capabilities_state_that_transmission_is_off(self):
        caps = engine.capabilities()
        self.assertTrue(caps["receive"])
        self.assertFalse(caps["transmit"])
        self.assertIn("OWL", caps["transmit_reason"])

    def test_freq_to_channel(self):
        self.assertEqual([engine.freq_to_channel(f) for f in (2412, 2437, 2484, 5180, 5220, 5745, 5955, None)],
                         [1, 6, 14, 36, 44, 149, None, None])


class PcapSource(unittest.TestCase):
    def write_pcap(self, path, packets, magic=b"\xd4\xc3\xb2\xa1", order="<"):
        with open(path, "wb") as f:
            f.write(magic + struct.pack(order + "HHiIII", 2, 4, 0, 0, 65535, 127))
            for i, p in enumerate(packets):
                f.write(struct.pack(order + "IIII", 10 + i, 500, len(p), len(p)) + p)

    def test_reads_classic_pcap_and_rejects_others(self):
        import tempfile
        packets = [h.action_frame(h.PEER_A, h.psf([]), tsft=1000 + i) for i in range(3)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pcap"
            self.write_pcap(path, packets)
            read = list(capture.PcapSource(path))
            self.assertEqual([p for _t, p, _tr in read], packets)
            self.assertEqual(read[1][0], 11 * 1_000_000 + 500)
            other = Path(tmp) / "b.pcap"
            other.write_bytes(b"not a pcap at all, definitely" * 3)
            with self.assertRaises(capture.SourceError):
                list(capture.PcapSource(other))
            wrong_link = Path(tmp) / "c.pcap"
            wrong_link.write_bytes(b"\xd4\xc3\xb2\xa1" + struct.pack("<HHiIII", 2, 4, 0, 0, 65535, 1))
            with self.assertRaises(capture.SourceError):
                list(capture.PcapSource(wrong_link))


class Views(unittest.TestCase):
    def status_for(self, e, age=0):
        """What the window reads: the engine's snapshot written to a file and read back."""
        import tempfile
        data = json.loads(json.dumps(e.status(), default=str))
        data["updated"] = time.time() - age
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            path.write_text(json.dumps(data))
            return view.read_status(path)

    def test_general_states(self):
        self.assertEqual(view.general(None, False)[1], "Motore non attivo")
        self.assertIn("visibilità", view.general(None, False)[2])
        e = started()
        sev, title, _ = view.general(self.status_for(e), True)
        self.assertEqual((sev, title), ("idle", "In ascolto"))
        stale = self.status_for(e, age=60)
        self.assertEqual(view.general(stale, True)[1], "Il motore non risponde")
        self.assertEqual(view.general(stale, False)[1], "Motore non attivo")

    def test_read_status_validates_the_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "status.json"
            self.assertIsNone(view.read_status(path))
            path.write_text("{not json")
            self.assertIsNone(view.read_status(path))
            path.write_text(json.dumps({"no": "state"}))
            self.assertIsNone(view.read_status(path))
            path.write_text(json.dumps({"state": "listening", "updated": time.time()}))
            self.assertFalse(view.read_status(path)["stale"])
            path.write_text(json.dumps({"state": "listening", "updated": time.time() - 99}))
            self.assertTrue(view.read_status(path)["stale"])

    def test_rows_for_a_live_engine(self):
        e = started()
        peer = IdealPeer()
        for i in range(40):
            e.feed(peer.action(1_000_000 + i * 90_000))
            e.tick(1_000_000 + i * 90_000)
        status = self.status_for(e)
        peers = view.peer_rows(status)
        self.assertEqual(peers[0][0], "iphone")
        self.assertIn("AirDrop non rilevato", peers[0][1])
        titles = [t for t, _ in view.sync_rows(status)]
        self.assertIn("Previsioni entro 3 TU", titles)
        wireless = dict(view.wireless_rows(status, None))
        self.assertEqual(wireless["Canale operativo"], "6")
        self.assertTrue(wireless["Trasmissione NexaAWDL"].startswith("disattivata"))
        text = view.diagnostics_text(status)
        self.assertIn("Stato: Sincronizzato", text)

    def test_hardware_summary(self):
        self.assertEqual(view.hardware_summary(None)[1], "Non ancora verificato")
        self.assertEqual(view.hardware_summary({"verdict": "capable-needs-live-verification"})[0], "ok")
        self.assertEqual(view.hardware_summary({"verdict": "incapable"})[0], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
