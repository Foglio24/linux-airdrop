#!/usr/bin/python3
"""Meaningful receive boundary checks, with locally constructed archives."""
import contextlib
from email.message import Message
import http.client
from http.server import ThreadingHTTPServer
import io
import os
from pathlib import Path
import plistlib
import socket
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

BUNDLE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BUNDLE), str(BUNDLE / "backend/python")]
import secure_receiver as receiver


def odc(name, data=b"", mode=0o100644, nlink=1):
    raw_name = name.encode() + b"\0"
    numbers = [(0, 6), (1, 6), (mode, 6), (0, 6), (0, 6),
               (nlink, 6), (0, 6), (0, 11), (len(raw_name), 6), (len(data), 11)]
    header = b"070707" + "".join(f"{value:0{width}o}" for value, width in numbers).encode()
    assert len(header) == 76
    return header + raw_name + data


def archive(*entries):
    return b"".join(entries) + odc("TRAILER!!!")


def dvzip(data):
    out = bytearray()
    for pos in range(0, len(data), 128 * 1024):
        block = zlib.compress(data[pos:pos + 128 * 1024])
        out += struct.pack(">I", len(block)) + block
    return bytes(out)


class ReceiverBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="receiver-test-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def extract(self, content, requested=None):
        raw = self.root / "test.cpio"
        raw.write_bytes(content)
        dest = self.root / "files"
        dest.mkdir(exist_ok=True)
        return receiver.extract_cpio(raw, dest, requested)

    def test_traversal_is_rejected_before_writing(self):
        with self.assertRaises(receiver.ReceiveError):
            self.extract(archive(odc("../../outside.txt", b"unsafe")))
        self.assertEqual(list((self.root / "files").iterdir()), [])
        self.assertFalse((self.root.parent / "outside.txt").exists())

    def test_absolute_paths_and_symlinks_are_rejected(self):
        for member in [odc("/tmp/airdrop-unsafe.txt", b"unsafe"),
                       odc("link", b"../../outside", 0o120777),
                       odc("hardlink", b"unsafe", nlink=2)]:
            with self.subTest(member=member[:100]), self.assertRaises(receiver.ReceiveError):
                self.extract(archive(member))

    def test_duplicate_names_are_separate_and_existing_file_untouched(self):
        dest = self.root / "files"
        dest.mkdir()
        (dest / "same.txt").write_text("existing")
        # Existing names cannot be overwritten even if an unexpected caller
        # gives extraction a non-empty directory.
        with self.assertRaises(FileExistsError):
            self.extract(archive(odc("same.txt", b"one")))
        self.assertEqual((dest / "same.txt").read_text(), "existing")
        (dest / "same.txt").unlink()
        names = self.extract(archive(odc("folderA/same.txt", b"one"),
                                     odc("folderB/same.txt", b"two")))
        self.assertEqual(names, ["same.txt", "same-1.txt"])
        self.assertEqual((dest / names[0]).read_bytes(), b"one")
        self.assertEqual((dest / names[1]).read_bytes(), b"two")

    def test_truncated_cpio_requires_complete_file_and_trailer(self):
        valid = archive(odc("photo.jpg", b"photo bytes"))
        for cut in [40, 90, len(valid) - 1]:
            with self.subTest(cut=cut):
                # Distinct outputs because failed extractions intentionally keep
                # their isolated partial files.
                dest = self.root / f"cut-{cut}"
                dest.mkdir()
                raw = self.root / f"cut-{cut}.cpio"
                raw.write_bytes(valid[:cut])
                with self.assertRaises(receiver.ReceiveError):
                    receiver.extract_cpio(raw, dest)

    def test_missing_approved_file_is_failure(self):
        with self.assertRaises(receiver.ReceiveError):
            self.extract(archive(odc("one.txt", b"one")),
                         [{"FileName": "one.txt"}, {"FileName": "two.txt"}])

    def test_dvzip_complete_and_truncated(self):
        content = archive(odc("photo.jpg", b"photo bytes"))
        raw = self.root / "test.dvzip"
        raw.write_bytes(dvzip(content))
        decoded = self.root / "decoded.cpio"
        receiver.decode_archive(raw, "application/x-dvzip", decoded)
        self.assertEqual(decoded.read_bytes(), content)
        raw.write_bytes(dvzip(content)[:-1])
        with self.assertRaises(receiver.ReceiveError):
            receiver.decode_archive(raw, "application/x-dvzip", self.root / "cut.cpio")

    def test_dvzip_decompression_limit(self):
        raw = self.root / "bomb.dvzip"
        block = zlib.compress(b"a" * (128 * 1024 + 1))
        raw.write_bytes(struct.pack(">I", len(block)) + block)
        with self.assertRaises(receiver.ReceiveError):
            receiver.decode_archive(raw, "application/x-dvzip", self.root / "bomb.cpio")

    def test_chunk_body_rejects_negative_missing_end_and_oversize(self):
        for data in [b"-1\r\n", b"3\r\nabc\r\n", b"100\r\n"]:
            headers = Message()
            headers["Transfer-Encoding"] = "chunked"
            handler = SimpleNamespace(headers=headers, rfile=io.BytesIO(data))
            with self.subTest(data=data), self.assertRaises(receiver.ReceiveError):
                list(receiver._body_parts(handler, 16))
        headers = Message()
        headers["Transfer-Encoding"] = "chunked"
        handler = SimpleNamespace(headers=headers, rfile=io.BytesIO(b"3\r\nabc\r\n0\r\n\r\n"))
        self.assertEqual(b"".join(receiver._body_parts(handler, 16)), b"abc")

    def test_registry_requires_peer_id_and_one_upload(self):
        approvals = receiver.Approvals()
        with self.assertRaises(receiver.ReceiveError):
            approvals.claim("peer", "id")
        approvals.approve("peer", "id", {"Files": [{"FileName": "one"}]})
        with self.assertRaises(receiver.ReceiveError):
            approvals.claim("different-peer", "id")
        with self.assertRaises(receiver.ReceiveError):
            approvals.claim("peer", "different-id")
        self.assertIn("Files", approvals.claim("peer", "id"))
        with self.assertRaises(receiver.ReceiveError):
            approvals.claim("peer", "id")
        approvals.approve("peer", "id", {})
        with patch.object(receiver.time, "monotonic", return_value=10 ** 15):
            with self.assertRaises(receiver.ReceiveError):
                approvals.claim("peer", "id")

    def test_http_upload_cannot_bypass_ask_and_complete_flow(self):
        receiver.install()
        from opendrop.server import AirDropServerHandler
        class Handler(AirDropServerHandler):
            config = SimpleNamespace(computer_name="test-pc", computer_model="Linux", debug=False)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.approvals = receiver.Approvals()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        raw = dvzip(archive(odc("photo.jpg", b"photo bytes")))

        def post(path, body, headers=None):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("POST", path, body=body, headers=headers or {})
            response = conn.getresponse()
            status = response.status
            response.read()
            conn.close()
            return status

        try:
            with patch.dict(os.environ, {"AIRDROP_DOWNLOAD_DIR": str(self.root / "downloads"),
                                         "AIRDROP_CONFIRM": "/bin/true"}):
                headers = {"Content-Type": "application/x-dvzip", "TransferID": "transfer-1"}
                self.assertEqual(post("/Upload", raw, headers), 403)
                self.assertFalse((self.root / "downloads").exists())
                body = plistlib.dumps({"SenderComputerName": "Test iPhone", "TransferID": {"id": "transfer-1"},
                                       "Files": [{"FileName": "photo.jpg"}]}, fmt=plistlib.FMT_BINARY)
                self.assertEqual(post("/Ask", body), 200)
                self.assertEqual(post("/Upload", raw, headers), 200)
                directories = list((self.root / "downloads").iterdir())
                self.assertEqual(len(directories), 1)
                self.assertEqual((directories[0] / "photo.jpg").read_bytes(), b"photo bytes")
                self.assertEqual(post("/Upload", raw, headers), 403)
                self.assertEqual(post("/Ask", plistlib.dumps({"Files": "bad"})), 400)
        finally:
            server.shutdown()
            server.server_close()


class TailAcknowledgement(unittest.TestCase):
    """While an upload is silent, our cumulative ACK is repeated (keepalive probes) instead of sent once."""

    def silent_peer_ack_age(self, keepalive):
        with socket.create_server(("127.0.0.1", 0)) as server:
            client = socket.create_connection(server.getsockname())
            accepted, _ = server.accept()
            with client, accepted:
                if keepalive:
                    receiver._keep_acknowledging(accepted)
                client.sendall(b"body bytes")
                accepted.recv(64)
                time.sleep(3.2)
                info = accepted.getsockopt(socket.IPPROTO_TCP, socket.TCP_INFO, 104)
                return struct.unpack_from("8B13I", info)[8 + 12]   # tcpi_last_ack_recv, ms

    def test_options_are_set_on_the_upload_socket(self):
        with socket.socket() as sock:
            receiver._keep_acknowledging(sock)
            self.assertEqual(sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE), 1)
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE), 1)
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL), 1)
            self.assertEqual(sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT), receiver.READ_TIMEOUT)

    def test_a_silent_sender_keeps_receiving_our_ack(self):
        # Every probe makes the peer answer, so the last ACK we received stays recent.
        self.assertGreater(self.silent_peer_ack_age(False), 2500)
        self.assertLess(self.silent_peer_ack_age(True), 1500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
