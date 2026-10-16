"""History and remembered devices: persistence, privacy limits, recovery from damage."""
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import store as store_module
from store import Store, make_device_key


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state" / "history.db"
        self.clock = Clock()
        self.store = Store(self.path, clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_database_is_private(self):
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_a_received_transfer_round_trip(self):
        files = [{"name": "foto.jpg", "kind": "image", "size": 2_000_000, "path": "/home/u/Download/LinuxAirDrop/A/foto.jpg"},
                 {"name": "nota.txt", "kind": "text", "size": 12, "path": None}]
        tid = self.store.start_transfer("receive", "iPhone di Anna", "iPhone15,2", "name:abc", total_bytes=2_000_012, files=files)
        self.assertEqual(self.store.get_transfer(tid)["status"], "in_progress")
        self.clock.now += 5
        self.store.finish_transfer(tid, "completed", done_bytes=2_000_012, folder="/home/u/Download/LinuxAirDrop/A")
        row = self.store.transfers()[0]
        self.assertEqual((row["direction"], row["status"], row["peer_name"], row["file_count"]),
                         ("receive", "completed", "iPhone di Anna", 2))
        self.assertEqual([f["name"] for f in row["files"]], ["foto.jpg", "nota.txt"])
        self.assertEqual(row["finished_at"] - row["started_at"], 5)
        self.assertEqual(row["folder"], "/home/u/Download/LinuxAirDrop/A")

    def test_persists_across_reopening(self):
        self.store.start_transfer("send", "iPad", files=[{"name": "a.pdf", "kind": "pdf", "size": 1}])
        self.store.close()
        again = Store(self.path)
        try:
            self.assertEqual(len(again.transfers()), 1)
        finally:
            again.close()

    def test_all_final_states_are_accepted_and_unknown_ones_refused(self):
        for status in ("completed", "failed", "cancelled", "declined"):
            tid = self.store.start_transfer("send", "x")
            self.store.finish_transfer(tid, status)
            self.assertEqual(self.store.get_transfer(tid)["status"], status)
        with self.assertRaises(ValueError):
            self.store.finish_transfer(tid, "in_progress")
        with self.assertRaises(ValueError):
            self.store.finish_transfer(tid, "exploded")
        with self.assertRaises(ValueError):
            self.store.start_transfer("sideways", "x")

    def test_untrusted_text_is_cleaned_and_bounded(self):
        tid = self.store.start_transfer("receive", "Anna‮\x00" + "x" * 500, "iPhone\n", files=[{"name": "a‮gpj.exe\x07", "kind": "image"}])
        self.store.finish_transfer(tid, "failed", error="e" * 1000)
        row = self.store.get_transfer(tid)
        self.assertNotIn("‮", row["peer_name"])
        self.assertLessEqual(len(row["peer_name"]), 120)
        self.assertNotIn("‮", row["files"][0]["name"])
        self.assertLessEqual(len(row["error"]), 200)

    def test_filter_order_and_limit(self):
        for i, direction in enumerate(("send", "receive", "send")):
            self.clock.now += 10
            self.store.start_transfer(direction, f"d{i}")
        self.assertEqual([t["peer_name"] for t in self.store.transfers()], ["d2", "d1", "d0"])
        self.assertEqual([t["peer_name"] for t in self.store.transfers("send")], ["d2", "d0"])
        self.assertEqual(len(self.store.transfers(limit=1)), 1)

    def test_clear_history_removes_rows_and_file_lists_but_not_devices(self):
        tid = self.store.start_transfer("send", "iPad", files=[{"name": "a", "kind": "other"}])
        self.store.touch_device("name:1", "name", "iPad")
        self.store.clear_history()
        self.assertEqual(self.store.transfers(), [])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM files").fetchone()[0], 0)
        self.assertEqual(len(self.store.devices()), 1)

    def test_prune_by_age_and_count(self):
        self.store.start_transfer("send", "old")
        self.clock.now += 400 * 86400
        for i in range(5):
            self.clock.now += 1
            self.store.start_transfer("send", f"n{i}")
        self.store.prune(max_rows=3)
        self.assertEqual([t["peer_name"] for t in self.store.transfers()], ["n4", "n3", "n2"])

    def test_interrupted_transfers_are_marked_failed(self):
        tid = self.store.start_transfer("receive", "iPhone")
        self.assertEqual(self.store.mark_interrupted(), 1)
        row = self.store.get_transfer(tid)
        self.assertEqual(row["status"], "failed")
        self.assertIn("Interrotto", row["error"])

    def test_disabled_store_records_nothing_and_does_not_fail(self):
        off = Store(Path(self.temp.name) / "never.db", enabled=False)
        self.assertIsNone(off.start_transfer("send", "x"))
        off.finish_transfer(None, "completed")
        off.touch_device("k", "name", "x")
        self.assertEqual((off.transfers(), off.devices()), ([], []))
        self.assertFalse((Path(self.temp.name) / "never.db").exists())

    def test_damaged_file_is_moved_aside_and_a_new_one_started(self):
        self.store.close()
        self.path.write_bytes(b"this is not a database" * 100)
        recovered = Store(self.path)
        try:
            self.assertTrue(recovered.enabled)
            self.assertIsNotNone(recovered.problem)
            recovered.start_transfer("send", "x")
            self.assertEqual(len(recovered.transfers()), 1)
            self.assertTrue(list(self.path.parent.glob("history.db.damaged-*")))
        finally:
            recovered.close()

    def test_database_from_the_future_is_not_overwritten(self):
        self.store.close()
        raw = sqlite3.connect(self.path)
        raw.execute(f"PRAGMA user_version = {store_module.SCHEMA_VERSION + 5}")
        raw.commit()
        raw.close()
        again = Store(self.path)
        try:
            self.assertIsNotNone(again.problem)       # moved aside rather than modified in place
            self.assertTrue(list(self.path.parent.glob("history.db.damaged-*")))
        finally:
            again.close()


class Devices(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.clock = Clock()
        self.store = Store(Path(self.temp.name) / "h.db", clock=self.clock)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_name_only_identity_is_marked_weak_and_does_not_depend_on_case(self):
        a = make_device_key("iPhone di Anna")
        b = make_device_key("  IPHONE di anna ")
        self.assertEqual(a, b)
        self.assertEqual(a[0], "name")

    def test_record_fingerprint_gives_a_different_stronger_identity(self):
        fingerprint = "ab" * 16
        identity, key = make_device_key("iPhone di Anna", fingerprint)
        self.assertEqual(identity, "record")
        self.assertNotEqual(key, make_device_key("iPhone di Anna")[1])
        # a renamed phone keeps its identity; same name with another fingerprint is another device
        self.assertEqual(make_device_key("Nuovo nome", fingerprint)[1], key)
        self.assertNotEqual(make_device_key("iPhone di Anna", "cd" * 16)[1], key)

    def test_bad_fingerprints_are_ignored(self):
        for bad in ("short", "zz" * 16, None, 5, "AB" * 16):
            self.assertEqual(make_device_key("x", bad)[0], "name")

    def test_counts_dates_and_forgetting(self):
        identity, key = make_device_key("iPhone di Anna", "ab" * 16)
        self.store.touch_device(key, identity, "iPhone di Anna", "iPhone15,2")
        first = self.clock.now
        self.clock.now += 100
        self.store.touch_device(key, identity, "iPhone di Anna (nuovo)", None, direction="receive")
        self.clock.now += 100
        self.store.touch_device(key, identity, "iPhone di Anna (nuovo)", None, direction="send")
        device = self.store.devices()[0]
        self.assertEqual((device["received_count"], device["sent_count"], device["transfer_count"]), (1, 1, 2))
        self.assertEqual(device["first_seen"], first)
        self.assertEqual(device["last_seen"], first + 200)
        self.assertEqual(device["last_transfer"], first + 200)
        self.assertEqual(device["name"], "iPhone di Anna (nuovo)")
        self.assertEqual(device["model"], "iPhone15,2")        # a missing model does not erase the known one
        self.store.forget_device(key)
        self.assertEqual(self.store.devices(), [])

    def test_forget_all_and_invalid_input(self):
        self.store.touch_device("name:1", "name", "a")
        self.store.touch_device("name:2", "name", "b")
        self.store.touch_device("", "name", "ignored")
        self.store.touch_device("name:3", "bogus", "ignored")
        self.assertEqual(len(self.store.devices()), 2)
        self.store.forget_devices()
        self.assertEqual(self.store.devices(), [])


if __name__ == "__main__":
    unittest.main()
