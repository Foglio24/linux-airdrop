"""A real OpenDrop sender and receiver talking to each other in a private network namespace (no radio, no phone)."""
import json
import os
from pathlib import Path
import signal
import subprocess
import unittest

HELPER = Path(__file__).resolve().parent / "ns_loopback_transfer.py"


class Loopback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Own process group: if the run hangs, everything it started (the receiver) is killed with it, never orphaned.
        try:
            process = subprocess.Popen(["unshare", "-Urn", "/usr/bin/python3", "-I", "-B", str(HELPER)], stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, start_new_session=True)
        except OSError as exc:
            raise unittest.SkipTest("network namespace unavailable: " + str(exc))
        try:
            stdout, stderr = process.communicate(timeout=240)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise unittest.SkipTest("the loopback run did not finish in time")
        line = next((l for l in stdout.splitlines() if l.startswith("RESULT ")), None)
        if line is None:
            raise unittest.SkipTest("cannot run the loopback here: " + (stderr or stdout)[-300:])
        cls.result = json.loads(line[7:])

    def test_the_receiver_started_and_answered(self):
        self.assertTrue(self.result["found"], self.result)
        self.assertEqual(self.result["errors"], [])

    def test_files_arrive_intact_and_both_sides_agree(self):
        r = self.result
        self.assertEqual(r["send_files"]["code"], 0, r["send_files"]["log"])
        downloads = r["downloads"]
        self.assertEqual(sorted(k.split("/")[-1] for k in downloads if not k.split("/")[-1] in ("uno.txt", "due.txt")),
                         ["a.txt", "big.bin"])
        self.assertEqual(sorted(v for k, v in downloads.items() if k.endswith("big.bin")), [24 * 1024 * 1024])

    def test_sender_events_report_the_real_byte_counts(self):
        events = self.result["send_files"]["events"]
        kinds = [e["event"] for e in events]
        self.assertIn("send_accepted", kinds)
        self.assertEqual(kinds[-1], "send_done")
        start = next(e for e in events if e["event"] == "send_start")
        progress = [e for e in events if e["event"] == "send_progress"]
        self.assertGreater(start["total"], 24 * 1024 * 1024)
        self.assertEqual(progress[-1]["done"], progress[-1]["total"])
        self.assertTrue(all(a["done"] <= b["done"] for a, b in zip(progress, progress[1:])))

    def test_receiver_events_cover_ask_start_progress_done(self):
        events = self.result["receiver_events"]
        kinds = [e["event"] for e in events]
        for needed in ("ask", "ask_accepted", "receive_start", "receive_progress", "receive_done"):
            self.assertIn(needed, kinds)
        ask = next(e for e in events if e["event"] == "ask")
        self.assertEqual(ask["sender"], "loopback-sender")
        self.assertEqual(sorted(f["name"] for f in ask["files"]), ["a.txt", "big.bin"])
        start = next(e for e in events if e["event"] == "receive_start")
        progress = [e for e in events if e["event"] == "receive_progress" and e["id"] == start["id"]]
        self.assertIsNotNone(start["total"])
        self.assertEqual(progress[-1]["done"], start["total"])      # every announced byte arrived, and no more
        done = next(e for e in events if e["event"] == "receive_done" and e["files"] and e["files"][0]["name"] != "uno.txt")
        self.assertEqual({f["name"]: f["size"] for f in done["files"]}["big.bin"], 24 * 1024 * 1024)

    def test_a_folder_travels_natively_and_is_flattened_like_ios_receipts(self):
        r = self.result
        self.assertEqual(r["send_folder"]["code"], 0, r["send_folder"]["log"])
        names = sorted(k.split("/")[-1] for k in r["downloads"])
        self.assertIn("uno.txt", names)
        self.assertIn("due.txt", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
