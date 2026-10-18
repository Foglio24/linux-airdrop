"""Keep queued upload bytes distinct from receiver confirmation and TCP history."""
import io
import os
from pathlib import Path
import socket
import struct
import sys
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "backend/python")]
from opendrop.client import AirDropClient, _TCP_INFO_LAYOUT, tcp_report, tcp_snapshot
import secure_receiver
import transfer_events as events


def tcp_info(*, acked=0, sent=0, retrans_bytes=0, segments=0, retrans=0):
    values = [0] * 50
    values[8] = 2_000_000                 # RTO
    values[23] = 959_000                 # RTT
    values[24] = 296_000                 # RTT variance
    values[26] = 11                      # cwnd
    values[31] = retrans
    values[34] = acked
    values[41] = segments
    values[48] = sent
    values[49] = retrans_bytes
    return struct.pack(_TCP_INFO_LAYOUT, *values)


class UploadConfirmation(unittest.TestCase):
    def client(self):
        client = AirDropClient.__new__(AirDropClient)
        client.config = MagicMock()
        client.http_conn = MagicMock()
        client.http_conn.sock.gettimeout.return_value = None
        client.http_conn.sock.getsockopt.return_value = tcp_info()
        client.start_hook = client.progress_hook = client.waiting_hook = None
        client.http_conn.getresponse.return_value.status = 200
        client.http_conn.getresponse.return_value.read.return_value = b""
        return client

    def test_waiting_is_after_every_body_write_and_before_receiver_response(self):
        client = self.client()
        timeline = []
        client.http_conn.send.side_effect = lambda _: timeline.append("write")
        client.progress_hook = lambda done, total: timeline.append((done, total))
        client.waiting_hook = lambda: timeline.append("waiting")
        response = client.http_conn.getresponse.return_value
        client.http_conn.getresponse.side_effect = lambda: (timeline.append("response"), response)[1]
        payload = b"x" * (AirDropClient.SEND_SLICE + 5)
        with patch("opendrop.client.AirDropUtil.write_debug"):
            success, _ = client.send_POST("/Upload", io.BytesIO(payload), raw=True)
        self.assertTrue(success)
        self.assertEqual(timeline[-2:], ["waiting", "response"])
        # The final progress update refers to the HTTP body queued locally;
        # it has deliberately not been treated as receiver confirmation.
        self.assertEqual(timeline[-3][0], timeline[-3][1])
        self.assertGreater(timeline[-3][0], len(payload))   # includes chunk framing

    def test_a_failed_body_write_never_announces_waiting_or_reads_response(self):
        client = self.client()
        client.http_conn.send.side_effect = socket.timeout("stalled")
        client.waiting_hook = MagicMock()
        with patch("opendrop.client.AirDropUtil.write_debug"):
            with self.assertRaises(socket.timeout):
                client.send_POST("/Upload", io.BytesIO(b"x"), raw=True)
        client.waiting_hook.assert_not_called()
        client.http_conn.getresponse.assert_not_called()
        self.assertIsNone(client.http_conn.sock.gettimeout.return_value)
        self.assertEqual(client.http_conn.sock.settimeout.call_args.args, (None,))

    def test_other_requests_do_not_announce_upload_confirmation(self):
        client = self.client()
        client.waiting_hook = MagicMock()
        with patch("opendrop.client.AirDropUtil.write_debug"):
            client.send_POST("/Discover", b"x")
        client.waiting_hook.assert_not_called()

    def test_sender_mapping_emits_the_confirmation_event(self):
        # Restore the class after installing hooks, including decorated methods,
        # so unrelated tests never inherit this installation.
        names = ("start_hook", "progress_hook", "waiting_hook", "send_ask", "send_upload")
        saved = {name: AirDropClient.__dict__[name] for name in names}
        try:
            secure_receiver._install_sender_hooks()
            out = io.StringIO()
            from contextlib import redirect_stdout
            with patch.dict(os.environ, {"AIRDROP_EVENTS": "1"}), redirect_stdout(out):
                AirDropClient.waiting_hook()
            self.assertEqual(events.parse(out.getvalue())["event"], "send_waiting_confirmation")
        finally:
            for name, value in saved.items():
                setattr(AirDropClient, name, value)


class TcpMeasurements(unittest.TestCase):
    def test_upload_deltas_exclude_discover_and_ask_and_keep_current_gauges(self):
        sock = MagicMock()
        sock.getsockopt.return_value = tcp_info(acked=91_000, sent=92_000,
                                               retrans_bytes=1000, segments=100, retrans=1)
        baseline = tcp_snapshot(sock)
        sock.getsockopt.return_value = tcp_info(acked=265_000, sent=334_000,
                                               retrans_bytes=69_000, segments=248, retrans=52)
        report = tcp_report(sock, baseline)
        self.assertIn("TCP upload: 174000 byte confermati, 148 segmenti inviati, 51 ritrasmessi", report)
        self.assertIn("RTT 959 ms (±296), RTO 2000 ms, cwnd 11", report)
        self.assertIn("byte inviati 242000, ritrasmessi 68000", report)

    def test_socket_or_kernel_without_complete_tcp_info_has_no_report(self):
        sock = MagicMock()
        for raw in (b"", b"\0" * 104):
            sock.getsockopt.return_value = raw
            self.assertIsNone(tcp_snapshot(sock))
            self.assertIsNone(tcp_report(sock))
        sock.getsockopt.side_effect = OSError("unsupported")
        self.assertIsNone(tcp_report(sock))

    def test_missing_baseline_is_explicitly_a_connection_report(self):
        sock = MagicMock()
        sock.getsockopt.return_value = tcp_info(acked=10)
        self.assertIn("TCP connessione: 10 byte confermati", tcp_report(sock))


if __name__ == "__main__":
    unittest.main(verbosity=2)
