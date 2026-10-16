"""Sending over an AWDL-like link: the TCP settings of the sender, and the link emulator.

The emulator cases use real OpenDrop processes over a relay that models the phone's listening windows and frame
loss (see awdl_link_emulator.py). They explain the 16 October failure (an upload to the iPhone stalled after
192 kB) and why radio acknowledgements fix it; they do not prove what the MT7922 does on the air.
Set AIRDROP_SLOW_TESTS=1 to also run the slow lossy cases (a few minutes).
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend/python")]
from opendrop import client
EMULATOR = Path(__file__).resolve().parent / "awdl_link_emulator.py"


class SenderSocket(unittest.TestCase):
    def test_retransmission_settings_are_applied_and_can_be_turned_off(self):
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            client.tune_awdl_socket(s)
            self.assertEqual(s.getsockopt(socket.IPPROTO_TCP, client.TCP_THIN_LINEAR_TIMEOUTS), 1)
            self.assertEqual(s.getsockopt(socket.IPPROTO_TCP, client.TCP_RTO_MAX_MS), 2000)
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s, patch.dict(os.environ, {"AIRDROP_TCP_TUNING": "0"}):
            default_ceiling = s.getsockopt(socket.IPPROTO_TCP, client.TCP_RTO_MAX_MS)
            client.tune_awdl_socket(s)
            self.assertEqual(s.getsockopt(socket.IPPROTO_TCP, client.TCP_THIN_LINEAR_TIMEOUTS), 0)
            self.assertEqual(s.getsockopt(socket.IPPROTO_TCP, client.TCP_RTO_MAX_MS), default_ceiling)

    def test_a_kernel_without_the_options_is_not_a_failure(self):
        sock = MagicMock()
        sock.setsockopt.side_effect = OSError("not supported")
        client.tune_awdl_socket(sock)          # must not raise

    def test_tcp_report_reads_the_kernels_counters(self):
        server = socket.socket(socket.AF_INET6)
        server.bind(("::1", 0))
        server.listen(1)
        received = []

        def serve():
            conn, _ = server.accept()
            total = 0
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                total += len(data)
            received.append(total)
            conn.close()
        thread = threading.Thread(target=serve)
        thread.start()
        with socket.socket(socket.AF_INET6) as c:
            c.connect(("::1", server.getsockname()[1]))
            c.sendall(b"x" * 500_000)
            c.shutdown(socket.SHUT_WR)
            thread.join()             # every byte has arrived: the counters are final
            report = client.tcp_report(c)
        server.close()
        self.assertEqual(received, [500_000])
        self.assertIn("ritrasmessi 0", report)
        self.assertIn("byte inviati 500000", report)
        self.assertIsNone(client.tcp_report(MagicMock(getsockopt=MagicMock(side_effect=OSError))))

    def test_a_stalled_upload_logs_the_report_before_giving_up(self):
        sock = MagicMock()
        sock.gettimeout.return_value = None
        sender = client.AirDropClient.__new__(client.AirDropClient)
        sender.http_conn = MagicMock(sock=sock)
        sender.http_conn.send.side_effect = socket.timeout("stalled")
        with patch.object(client, "tcp_report", return_value="TCP: prova"), self.assertLogs("opendrop.client", "INFO") as logs, \
                self.assertRaises(socket.timeout):
            sender._send_watched(b"x" * 10)
        self.assertTrue(any("TCP: prova" in line for line in logs.output))

    def test_a_successful_upload_is_reported_after_the_receiver_answers(self):
        source = (ROOT / "backend/python/opendrop/client.py").read_text()
        post = source[source.index("def send_POST"):source.index("def send_discover")]
        self.assertLess(post.index("getresponse()"), post.index("log_tcp_report(sock"))
        self.assertLess(post.index("log_tcp_report(sock"), post.index("http_resp.read()"))
        self.assertIsNone(client.tcp_report(None))        # a closed or missing socket is not an error


def emulate(**settings):
    env = dict(os.environ, **{key: str(value) for key, value in settings.items()})
    try:
        done = subprocess.run(["unshare", "-Urn", "/usr/bin/python3", "-I", "-B", str(EMULATOR)], env=env, capture_output=True,
                              text=True, timeout=int(settings.get("LIMIT", 120)) + 60, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise unittest.SkipTest("emulator unavailable: " + str(exc))
    line = next((l for l in done.stdout.splitlines() if l.startswith("RESULT ")), None)
    if line is None:
        raise unittest.SkipTest("emulator could not run here (needs unshare, veth, ethtool): " + done.stderr[-300:])
    return json.loads(line[7:])


class LinkEmulation(unittest.TestCase):
    def test_with_radio_acknowledgements_a_send_completes_even_on_the_phones_idle_schedule(self):
        result = emulate(SLOTS="8,9", LOSS="0.03", SIZE_MB="1", LIMIT="90", SEED="5")
        self.assertEqual(result["code"], 0, result)
        self.assertTrue(result["received"])
        self.assertGreater(result["kB_s"], 50)
        self.assertTrue(any("ritrasmessi" in line for line in result["tail"]))      # the TCP report is in the log

    @unittest.skipUnless(os.environ.get("AIRDROP_SLOW_TESTS"), "slow: set AIRDROP_SLOW_TESTS=1")
    def test_without_them_the_same_send_stalls_like_on_16_october(self):
        result = emulate(SLOTS="8,9", LOSS="0.3", SIZE_MB="2", LIMIT="300", SEED="7", AIRDROP_STALL_TIMEOUT="30")
        self.assertNotEqual(result["code"], 0, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
