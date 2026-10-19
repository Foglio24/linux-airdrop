"""Exercise the built CLI without opening or changing a network interface.

Run after `make`: python3 -m unittest discover -s downstream-tests -p test_owl_cli.py
OWL_BINARY can select a separately built sanitizer executable.
"""

import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get("OWL_BINARY", ROOT / "build/owl")).resolve()


class CommandLineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not BINARY.is_file():
            raise RuntimeError(f"Build OWL first with make: missing {BINARY}")

    def run_owl(self, *options):
        return subprocess.run(
            [str(BINARY), *options], capture_output=True, text=True, timeout=5
        )

    def assert_rejected(self, options, diagnostic):
        result = self.run_owl(*options)
        # A signal (negative status), abort, or apparent daemon success is a bug.
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(diagnostic, result.stderr)
        self.assertEqual(result.stdout, "", "Validation must precede the banner/startup")

    def test_help_needs_no_interface_and_preserves_host_option(self):
        result = self.run_owl("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertIn("-h name", result.stdout)
        self.assertIn("-G start[,end]", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_required_interface_is_checked_before_daemonization(self):
        for options in [(), ("-D",), ("-i", ""), ("-D", "-i", "")]:
            with self.subTest(options=options):
                self.assert_rejected(options, "No interface specified")

    def test_valid_settings_reach_required_interface_validation(self):
        # Omitting -i proves parsing succeeds while ensuring no I/O is started.
        options = [
            ("-c", channel) for channel in ("6", "36", "44", "149")
        ] + [
            ("-G", guard) for guard in ("0", "16", "0,16", "16,0", "3,3")
        ] + [
            ("-S", strategy)
            for strategy in ("verbatim", "widen", "intersect", "rotate", "pin")
        ] + [
            ("-R", rate)
            for rate in ("6", "9", "12", "18", "24", "36", "48", "54", "mcs0", "mcs15", "mcs0-sgi", "mcs15-sgi")
        ] + [
            ("-r", "-100"), ("-r", "-30"), ("-W", "0"), ("-W", "16"),
            ("-m", "02:aa:bb:cc:dd:ff"), ("-m", "2:a:b:c:d:e"),
            ("-h", ""), ("-h", "abcdefghijklmno"),
            ("-DdfNFEAIvvvvvvvvvv",),
        ]
        for setting in options:
            with self.subTest(setting=setting):
                self.assert_rejected(setting, "No interface specified")

    def test_numeric_options_reject_junk_empty_and_overflow(self):
        invalid = {
            "-c": ("", "six", "6tail", " 6", "-1", "4294967302", "9" * 80),
            "-r": ("", "-60dBm", " -60", "-101", "-29", "-4294967356", "-" + "9" * 80),
            "-W": ("", "abc", "4slots", " 4", "-1", "17", "4294967296", "9" * 80),
            "-G": ("", "1,", ",1", "1,,2", "1,2,3", "1,no", "1, 2", "17", "1,17", "-1", "1,-1", "9" * 80),
        }
        for option, values in invalid.items():
            for value in values:
                with self.subTest(option=option, value=value):
                    self.assert_rejected((option, value), f"{option} takes")

    def test_unsupported_channel_is_rejected_before_daemonization(self):
        for channel in ("0", "1", "11", "150", "255"):
            with self.subTest(channel=channel):
                self.assert_rejected(("-D", "-c", channel), "Unsupported channel")

    def test_bad_rate_address_and_strategy_are_diagnosed(self):
        invalid = {
            "-R": ("", "7", "12Mbps", " 12", "mcs", "mcs16", "mcs-1", "mcs 1", "mcs7-sgi-extra", "mcs" + "9" * 80),
            "-m": ("", "02:aa:bb:cc:dd", "02:aa:bb:cc:dd:ff:00", "02:aa:bb:cc:dd:ffjunk", "02:aa:bb:cc:dd:ff trailing", "002:aa:bb:cc:dd:ff", "gg:aa:bb:cc:dd:ff"),
        }
        for option, values in invalid.items():
            for value in values:
                with self.subTest(option=option, value=value):
                    self.assert_rejected((option, value), f"{option} takes")
        self.assert_rejected(("-S", "bad"), "Unknown channel sequence strategy")

    def test_names_cannot_overflow_destination_buffers(self):
        self.assert_rejected(("-h", "x" * 16), "-h takes at most 15 bytes")
        self.assert_rejected(("-i", "x" * 4096), "-i takes at most")

    def test_unknown_missing_and_positional_arguments_do_not_abort(self):
        for option in ("-a", "-t", "-z", "--unknown"):
            with self.subTest(option=option):
                self.assert_rejected((option,), "Unknown option")
        for option in ("-i", "-h", "-c", "-m", "-S", "-G", "-r", "-W", "-R"):
            with self.subTest(option=option):
                self.assert_rejected((option,), "needs an argument")
        self.assert_rejected(("extra",), "Unexpected argument")


class CaptureLifecycleTests(unittest.TestCase):
    """Finite files and local pipes exercise startup, EOF, and signal cleanup."""

    @staticmethod
    def header(linktype=127):
        return struct.pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, linktype)

    def run_capture(self, content, *options):
        with tempfile.TemporaryDirectory() as folder:
            capture = Path(folder) / "capture.pcap"
            capture.write_bytes(content)
            return subprocess.run(
                [str(BINARY), "-i", capture.name, "-h", "", *options],
                cwd=folder, capture_output=True, text=True, timeout=5,
            )

    def test_empty_capture_finishes_at_eof(self):
        result = self.run_capture(self.header())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Using savefile", result.stderr)

    def test_validated_tuning_is_applied_to_runtime(self):
        result = self.run_capture(
            self.header(), "-c", "149", "-G", "1,2", "-r", "-90", "-I",
            "-S", "widen", "-W", "16", "-R", "mcs15-sgi", "-FEA",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in (
            "unicast guard: start 1 TU, end 2 TU",
            "RSSI filter: per frame, threshold -90 dBm",
            "channel sequence strategy: widen",
            "widening by at most 16 slot(s)",
            "data frame TX rate: HT MCS 15, 20 MHz, short GI",
            "hardware ACK/retries enabled",
            "election: by metric, then counter",
            "fixed radio: multicast in any slot",
        ):
            self.assertIn(expected, result.stderr)

    def test_real_capture_decodes_peer_and_finishes(self):
        capture = ROOT.parent.parent / "tests/fixtures/nexaawdl/real_full.pcap"
        if not capture.is_file():
            self.skipTest("downstream integration capture is not present")
        result = subprocess.run(
            [str(BINARY), "-i", str(capture), "-h", ""],
            capture_output=True, text=True, timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("add peer", result.stderr)
        self.assertNotIn("ERROR", result.stderr)

    def test_truncated_packet_reports_failure(self):
        # Header promises 20 bytes but the packet data ends after four.
        content = self.header() + struct.pack("<IIII", 0, 0, 20, 20) + b"\0" * 4
        result = self.run_capture(content)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("pcap receive failed", result.stderr)

    def test_wrong_linktype_is_rejected(self):
        result = self.run_capture(self.header(linktype=1))
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("does not contain radiotap frames", result.stderr)

    def test_background_initialization_reports_failure(self):
        result = self.run_capture(self.header(linktype=1), "-D")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("background initialization failed", result.stderr)

    def test_background_initialization_opens_relative_capture(self):
        result = self.run_capture(self.header(), "-D")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_signals_stop_waiting_capture_cleanly(self):
        for stop_signal in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(stop_signal=stop_signal), tempfile.TemporaryDirectory() as folder:
                capture = Path(folder) / "stream.pcap"
                os.mkfifo(capture)
                writer = os.open(capture, os.O_RDWR | os.O_NONBLOCK)
                process = None
                try:
                    os.write(writer, self.header())
                    process = subprocess.Popen(
                        [str(BINARY), "-i", str(capture), "-h", ""],
                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    )
                    diagnostics = b""
                    deadline = time.monotonic() + 5
                    while b"data frame TX rate:" not in diagnostics and time.monotonic() < deadline:
                        if select.select([process.stderr], [], [], 0.1)[0]:
                            chunk = os.read(process.stderr.fileno(), 4096)
                            if not chunk:
                                break
                            diagnostics += chunk
                    self.assertIn(b"data frame TX rate:", diagnostics)
                    # Let startup finish installing its signal watchers. The
                    # open pipe then waits indefinitely without touching radio.
                    time.sleep(0.05)
                    process.send_signal(stop_signal)
                    _, remaining = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, (diagnostics + remaining).decode())
                finally:
                    if process is not None and process.poll() is None:
                        process.kill()
                        process.communicate()
                    os.close(writer)


if __name__ == "__main__":
    unittest.main()
