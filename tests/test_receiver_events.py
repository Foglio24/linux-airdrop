#!/usr/bin/python3
"""Progress events, cancellation and the prompt description of the receiver, with locally built archives."""
import contextlib
import http.client
from http.server import ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import plistlib
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

BUNDLE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BUNDLE), str(BUNDLE / "backend/python")]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import secure_receiver as receiver
import transfer_events as events
from test_receiver_security import archive, dvzip, odc


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="events-test-", dir=Path(__file__).parent)
        self.root = Path(self.temp.name)
        receiver.install()
        from opendrop.server import AirDropServerHandler

        class Handler(AirDropServerHandler):
            config = SimpleNamespace(computer_name="test-pc", computer_model="Linux", debug=False)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.approvals = receiver.Approvals()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.out = io.StringIO()
        self.env = patch.dict(os.environ, {"AIRDROP_DOWNLOAD_DIR": str(self.root / "downloads"),
                                           "AIRDROP_CONFIRM": "/bin/true", "AIRDROP_EVENTS": "1"})
        self.env.start()
        self.redirect = contextlib.redirect_stdout(self.out)
        self.redirect.__enter__()

    def tearDown(self):
        self.redirect.__exit__(None, None, None)
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def post(self, path, body, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            conn.request("POST", path, body=body, headers=headers or {})
            response = conn.getresponse()
            response.read()
            return response.status
        except (OSError, http.client.HTTPException):
            return None
        finally:
            conn.close()

    def ask(self, transfer="t-1", **extra):
        body = {"SenderComputerName": "iPhone di Anna", "SenderModelName": "iPhone15,2", "TransferID": {"id": transfer},
                "Files": [{"FileName": "photo.jpg", "FileType": "public.jpeg"}]}
        body.update(extra)
        return self.post("/Ask", plistlib.dumps(body, fmt=plistlib.FMT_BINARY))

    def events(self):
        parsed = [events.parse(line) for line in self.out.getvalue().splitlines()]
        return [e for e in parsed if e]

    def kinds(self):
        return [e["event"] for e in self.events()]


class ReceiveEvents(Harness):
    def test_complete_transfer_reports_real_numbers(self):
        payload = dvzip(archive(odc("photo.jpg", b"x" * 300_000), odc("note.txt", b"hello")))
        self.assertEqual(self.ask(), 200)
        self.assertEqual(self.post("/Upload", payload, {"Content-Type": "application/x-dvzip", "TransferID": "t-1"}), 200)
        seen = self.events()
        self.assertEqual([e["event"] for e in seen][:3], ["ask", "ask_accepted", "receive_start"])
        ask = seen[0]
        self.assertEqual((ask["sender"], ask["model"], ask["identity"]), ("iPhone di Anna", "iPhone15,2", "name"))
        self.assertTrue(ask["device_key"].startswith("name:"))
        self.assertEqual(ask["files"][0]["kind"], "image")
        start = next(e for e in seen if e["event"] == "receive_start")
        self.assertEqual(start["total"], len(payload))                    # Content-Length is the announced size
        progress = [e for e in seen if e["event"] == "receive_progress"]
        self.assertEqual(progress[-1]["done"], len(payload))              # final report equals the bytes that arrived
        self.assertTrue(all(a["done"] <= b["done"] for a, b in zip(progress, progress[1:])))
        done = seen[-1]
        self.assertEqual(done["event"], "receive_done")
        self.assertEqual({f["name"]: f["size"] for f in done["files"]}, {"photo.jpg": 300_000, "note.txt": 5})
        self.assertEqual(done["files"][0]["kind"], "image")
        self.assertTrue((Path(done["folder"]) / "photo.jpg").is_file())

    def test_total_bytes_header_is_used_when_the_body_is_chunked(self):
        payload = dvzip(archive(odc("photo.jpg", b"hello")))
        self.ask()

        def chunks():
            yield payload[:10]
            yield payload[10:]
        status = self.post("/Upload", chunks(), {"Content-Type": "application/x-dvzip", "TransferID": "t-1",
                                                 "TotalBytes": str(len(payload))})
        self.assertEqual(status, 200)
        start = next(e for e in self.events() if e["event"] == "receive_start")
        self.assertEqual(start["total"], len(payload))

    def test_unknown_total_stays_unknown(self):
        self.ask()
        payload = dvzip(archive(odc("photo.jpg", b"hello")))
        self.post("/Upload", iter([payload]), {"Content-Type": "application/x-dvzip", "TransferID": "t-1"})
        start = next(e for e in self.events() if e["event"] == "receive_start")
        self.assertIsNone(start["total"])

    def test_declined_request_is_reported_and_nothing_is_written(self):
        with patch.dict(os.environ, {"AIRDROP_CONFIRM": "/bin/false"}):
            self.assertEqual(self.ask(), 403)
        self.assertEqual(self.kinds(), ["ask", "ask_declined"])
        self.assertFalse((self.root / "downloads").exists())

    def test_failed_upload_reports_failure_and_keeps_partial_data_for_inspection(self):
        self.ask()
        status = self.post("/Upload", dvzip(archive(odc("photo.jpg", b"hello")))[:-3],
                           {"Content-Type": "application/x-dvzip", "TransferID": "t-1"})
        self.assertNotEqual(status, 200)
        failed = self.events()[-1]
        self.assertEqual(failed["event"], "receive_failed")
        self.assertFalse(failed["cancelled"])
        self.assertTrue(failed["error"])
        self.assertTrue(list((self.root / "downloads").glob(".airdrop-incompleto-*")))

    def test_user_cancel_stops_the_transfer_and_removes_the_partial_data(self):
        self.ask("t-2")
        payload = dvzip(archive(odc("big.bin", os.urandom(400_000))))

        def chunks():
            yield payload[:20_000]
            receiver.cancel("t-2")          # what the window's "Annulla" button sends over stdin
            time.sleep(0.2)
            yield payload[20_000:]
        self.post("/Upload", chunks(), {"Content-Type": "application/x-dvzip", "TransferID": "t-2"})
        failed = [e for e in self.events() if e["event"] == "receive_failed"]
        self.assertEqual(len(failed), 1)
        self.assertTrue(failed[0]["cancelled"])
        self.assertEqual(list((self.root / "downloads").iterdir()), [])
        self.assertFalse(receiver.is_cancelled("t-2"))          # forgotten afterwards

    def test_cancel_command_reader(self):
        receiver.start_command_reader(io.StringIO("noise\ncancel abc-1\ncancel too many words\ncancel\n"))
        for _ in range(50):
            if receiver.is_cancelled("abc-1"):
                break
            time.sleep(0.02)
        self.assertTrue(receiver.is_cancelled("abc-1"))
        self.assertFalse(receiver.is_cancelled("noise"))
        receiver._forget_cancel("abc-1")

    def test_shared_link_is_reported_as_done_without_an_upload(self):
        with patch("opendrop.server.open_url") as opened:
            status = self.post("/Ask", plistlib.dumps({"SenderComputerName": "iPhone", "Items": ["https://example.org/a"]},
                                                      fmt=plistlib.FMT_BINARY))
        self.assertEqual(status, 200)
        opened.assert_called_once_with("https://example.org/a")
        done = self.events()[-1]
        self.assertEqual((done["event"], done["files"][0]["kind"]), ("receive_done", "link"))

    def test_an_upload_without_a_transfer_id_header_still_reports_under_the_ask_id(self):
        self.assertEqual(self.post("/Ask", plistlib.dumps({"SenderComputerName": "iPhone", "Files": [{"FileName": "photo.jpg"}]},
                                                          fmt=plistlib.FMT_BINARY)), 200)
        ask_id = self.events()[0]["id"]
        self.assertEqual(self.post("/Upload", dvzip(archive(odc("photo.jpg", b"x"))), {"Content-Type": "application/x-dvzip"}), 200)
        ids = {e["id"] for e in self.events() if e["event"].startswith("receive_")}
        self.assertEqual(ids, {ask_id})

    def test_event_lines_stay_small_however_many_files_arrive(self):
        names = [f"file-{i:03d}.txt" for i in range(120)]
        body = plistlib.dumps({"SenderComputerName": "iPhone", "TransferID": {"id": "t-big"}, "Files": [{"FileName": n} for n in names]},
                              fmt=plistlib.FMT_BINARY)
        self.assertEqual(self.post("/Ask", body), 200)
        self.assertEqual(self.post("/Upload", dvzip(archive(*[odc(n, b"x") for n in names])),
                                   {"Content-Type": "application/x-dvzip", "TransferID": "t-big"}), 200)
        lines = [l for l in self.out.getvalue().splitlines() if l.startswith(events.PREFIX)]
        self.assertTrue(lines)
        self.assertLess(max(len(l) for l in lines), 8000)
        done = [e for e in self.events() if e["event"] == "receive_done"][0]
        self.assertEqual(len(done["files"]), receiver.EVENT_FILES)
        self.assertEqual(done["total"], 120)            # the totals still count every file

    def test_no_events_without_the_environment_flag(self):
        with patch.dict(os.environ):
            os.environ.pop("AIRDROP_EVENTS")
            self.ask()
        self.assertEqual(self.out.getvalue(), "")


class PromptDescription(unittest.TestCase):
    def test_hook_receives_the_description_and_a_thumbnail(self):
        from PIL import Image
        picture = io.BytesIO()
        Image.new("RGB", (640, 480), (200, 40, 40)).save(picture, "JPEG2000")
        with tempfile.TemporaryDirectory(prefix="prompt-test-", dir=Path(__file__).parent) as folder:
            record = Path(folder) / "record.json"
            hook = Path(folder) / "hook.py"
            hook.write_text("#!/usr/bin/python3\nimport json, sys\n"
                            "info = json.load(open(sys.argv[sys.argv.index('--info') + 1]))\n"
                            "thumb = info.get('thumbnail')\n"
                            "info['thumb_bytes'] = len(open(thumb['file'], 'rb').read()) if thumb else None\n"
                            f"json.dump({{'argv': sys.argv[1:3], 'info': info}}, open({str(record)!r}, 'w'))\n")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
            request = {"SenderComputerName": "Anna‮", "SenderModelName": "iPhone15,2",
                       "Files": [{"FileName": "IMG.HEIC", "FileType": "public.heic", "FileSize": 2_000_000},
                                 {"FileName": "doc.pdf", "FileType": "com.adobe.pdf", "FileSize": 1_000}],
                       "FileIcon": picture.getvalue()}
            with patch.dict(os.environ, {"AIRDROP_CONFIRM": str(hook)}):
                self.assertTrue(receiver._confirm(request))
            data = json.loads(record.read_text())
        self.assertEqual(data["argv"][0], "Anna")
        info = data["info"]
        self.assertEqual([f["kind"] for f in info["files"]], ["image", "pdf"])
        self.assertEqual(info["total_size"], 2_001_000)
        self.assertEqual((info["thumbnail"]["width"], info["thumbnail"]["height"]), (256, 192))
        self.assertEqual(info["thumb_bytes"], 12 + 256 * 192 * 4)

    def test_broken_thumbnail_does_not_block_the_prompt(self):
        with patch.dict(os.environ, {"AIRDROP_CONFIRM": "/bin/true"}):
            self.assertTrue(receiver._confirm({"SenderComputerName": "x", "Files": [{"FileName": "a"}], "FileIcon": b"not an image"}))

    def test_missing_hook_refuses(self):
        with patch.dict(os.environ, {"AIRDROP_CONFIRM": "relative/hook"}):
            self.assertFalse(receiver._confirm({"Files": [{"FileName": "a"}]}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
