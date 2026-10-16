"""The meter must report only what was measured: no invented percentages, speeds or times."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from progress import Meter
import transfer_events as events


class MeterBehaviour(unittest.TestCase):
    def test_percent_and_speed_follow_the_bytes(self):
        m = Meter(total=10_000_000)
        m.update(0, now=0.0)
        m.update(2_000_000, now=1.0)
        m.update(4_000_000, now=2.0)
        self.assertAlmostEqual(m.percent, 40.0)
        self.assertAlmostEqual(m.speed(now=2.0), 2_000_000)
        self.assertAlmostEqual(m.eta(now=2.0), 3.0)

    def test_nothing_is_invented_before_data_exists(self):
        m = Meter(total=1000)
        self.assertEqual(m.percent, 0.0)
        self.assertIsNone(m.speed(now=1.0))
        self.assertIsNone(m.eta(now=1.0))
        m.update(10, now=0.0)
        self.assertIsNone(m.speed(now=0.1))      # one sample is not a speed

    def test_unknown_total_gives_no_percentage_or_eta_but_still_a_speed(self):
        m = Meter()
        m.update(0, now=0.0)
        m.update(1_000_000, now=2.0)
        self.assertIsNone(m.percent)
        self.assertIsNone(m.eta(now=2.0))
        self.assertAlmostEqual(m.speed(now=2.0), 500_000)

    def test_counter_overtaking_the_total_drops_the_total(self):
        # e.g. the announced size was the uncompressed one while the wire carries different bytes
        m = Meter(total=1000)
        m.update(500, now=0.0)
        m.update(1500, now=1.0)
        self.assertTrue(m.overrun)
        self.assertIsNone(m.percent)
        m.update(1600, total=1000, now=2.0)          # a later announcement must not bring it back
        self.assertIsNone(m.percent)

    def test_a_stall_reads_as_zero_speed_and_no_eta(self):
        m = Meter(total=1000)
        m.update(0, now=0.0)
        m.update(100, now=1.0)
        self.assertEqual(m.speed(now=10.0), 0.0)
        self.assertIsNone(m.eta(now=10.0))

    def test_stale_or_invalid_updates_are_ignored(self):
        m = Meter(total=1000)
        m.update(500, now=0.0)
        m.update(100, now=1.0)
        m.update(-5, now=1.0)
        m.update("many", now=1.0)
        self.assertEqual(m.done, 500)

    def test_percent_never_exceeds_100(self):
        m = Meter(total=1000)
        m.update(1000, now=0.0)
        self.assertEqual(m.percent, 100.0)

    def test_speed_uses_a_recent_window(self):
        m = Meter(total=10**9)
        m.update(0, now=0.0)
        m.update(10_000_000, now=1.0)               # a fast start...
        for second in range(2, 20):                 # ...then a slow 100 kB/s
            m.update(10_000_000 + (second - 1) * 100_000, now=float(second))
        self.assertLess(m.speed(now=19.0), 200_000)


class EventLines(unittest.TestCase):
    def line(self, **fields):
        import json
        return events.PREFIX + json.dumps(fields)

    def test_ordinary_lines_are_not_events(self):
        for text in ("File(s) received", "", "@@airdrop not json", events.PREFIX + "[1]",
                     events.PREFIX + '{"event": "format-disk"}', None, 5):
            self.assertIsNone(events.parse(text))

    def test_fields_are_validated_and_bounded(self):
        event = events.parse(self.line(event="receive_progress", id="x" * 500, done=5, total=-3,
                                       error="e" * 900, files=[{"name": "a.txt", "size": "big"}, 7, {"kind": "image"}]))
        self.assertEqual(len(event["id"]), 128)
        self.assertEqual(event["done"], 5)
        self.assertIsNone(event["total"])
        self.assertEqual(len(event["error"]), 300)
        self.assertEqual(event["files"], [{"name": "a.txt", "kind": "other", "size": None, "is_dir": False}])

    def test_booleans_are_not_numbers(self):
        self.assertIsNone(events.parse(self.line(event="send_progress", done=True))["done"])

    def test_emit_only_when_enabled(self):
        import io, os
        from contextlib import redirect_stdout
        os.environ.pop("AIRDROP_EVENTS", None)
        out = io.StringIO()
        with redirect_stdout(out):
            events.emit("ask_accepted", id="1")
        self.assertEqual(out.getvalue(), "")
        os.environ["AIRDROP_EVENTS"] = "1"
        try:
            with redirect_stdout(out):
                events.emit("ask_accepted", id="1", sender="iPhone di Anna")
        finally:
            del os.environ["AIRDROP_EVENTS"]
        parsed = events.parse(out.getvalue().strip())
        self.assertEqual((parsed["event"], parsed["id"], parsed["sender"]), ("ask_accepted", "1", "iPhone di Anna"))


if __name__ == "__main__":
    unittest.main()
