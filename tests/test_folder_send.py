"""Sending folders: what goes into the archive and what is announced in /Ask."""
import io
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

BUNDLE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BUNDLE), str(BUNDLE / "backend/python")]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import libarchive
from opendrop.client import AirDropClient
from opendrop.util import AbsArchiveWrite
import secure_receiver as receiver
import transfer_events as events


def build_archive(paths):
    """The same archive loop send_upload runs, returned as the list of (name, kind, data) it contains."""
    stream = io.BytesIO()
    with libarchive.custom_writer(stream.write, "cpio", archive_write_class=AbsArchiveWrite) as archive:
        for path in paths:
            for source, stored in AirDropClient._archive_members(path):
                archive.add_abs_file(source, stored)
    entries = []
    with libarchive.memory_reader(stream.getvalue()) as reader:
        for entry in reader:
            entries.append((entry.pathname, entry.isdir, b"".join(entry.get_blocks())))
    return entries


class FolderArchive(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def make_tree(self):
        top = self.root / "Vacanze"
        (top / "mare" / "giorno1").mkdir(parents=True)
        (top / "vuota").mkdir()
        (top / "nota.txt").write_text("ciao")
        (top / "mare" / "foto.jpg").write_bytes(b"JPG")
        (top / "mare" / "giorno1" / "dettaglio.txt").write_text("1")
        return top

    def test_a_single_file_is_one_entry_exactly_as_before(self):
        f = self.root / "photo.jpg"
        f.write_bytes(b"data")
        self.assertEqual(list(AirDropClient._archive_members(str(f))), [(str(f), "./photo.jpg")])
        self.assertEqual(build_archive([str(f)]), [("./photo.jpg", False, b"data")])

    def test_a_folder_travels_with_everything_below_it(self):
        entries = build_archive([str(self.make_tree())])
        names = [name for name, _, _ in entries]
        self.assertEqual(names[0], "./Vacanze")
        for expected in ("./Vacanze/nota.txt", "./Vacanze/mare", "./Vacanze/mare/foto.jpg",
                         "./Vacanze/mare/giorno1", "./Vacanze/mare/giorno1/dettaglio.txt", "./Vacanze/vuota"):
            self.assertIn(expected, names)
        by_name = {name: (is_dir, data) for name, is_dir, data in entries}
        self.assertTrue(by_name["./Vacanze/mare"][0])
        self.assertEqual(by_name["./Vacanze/mare/foto.jpg"], (False, b"JPG"))
        self.assertEqual(by_name["./Vacanze/nota.txt"], (False, b"ciao"))
        self.assertEqual(len(names), len(set(names)))

    def test_links_and_special_files_are_never_followed_or_sent(self):
        top = self.make_tree()
        outside = self.root / "secret.txt"
        outside.write_text("do not send")
        (top / "link-to-file").symlink_to(outside)
        (top / "link-to-dir").symlink_to(self.root)
        os.mkfifo(top / "pipe")
        names = [n for n, _, _ in build_archive([str(top)])]
        self.assertFalse(any("link-to" in n or "pipe" in n for n in names))
        self.assertNotIn("do not send", repr(build_archive([str(top)])))

    def test_files_and_folders_can_be_mixed(self):
        f = self.root / "a.txt"
        f.write_text("A")
        names = [n for n, _, _ in build_archive([str(f), str(self.make_tree())])]
        self.assertEqual(names[0], "./a.txt")
        self.assertIn("./Vacanze/nota.txt", names)

    def test_the_receiver_reads_back_what_the_sender_wrote(self):
        # The sender's archive must be acceptable to our own receiving side (flattened, like iOS receipts).
        top = self.make_tree()
        stream = io.BytesIO()
        with libarchive.custom_writer(stream.write, "cpio", archive_write_class=AbsArchiveWrite) as archive:
            for source, stored in AirDropClient._archive_members(str(top)):
                archive.add_abs_file(source, stored)
        cpio = self.root / "out.cpio"
        cpio.write_bytes(stream.getvalue())
        target = self.root / "extracted"
        target.mkdir()
        names = receiver.extract_cpio(cpio, target, [{"FileName": "Vacanze", "FileIsDirectory": True}])
        self.assertEqual(sorted(names), ["dettaglio.txt", "foto.jpg", "nota.txt"])


class AskEntries(unittest.TestCase):
    def test_folder_is_announced_as_a_folder_and_files_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "dir").mkdir()
            (root / "dir" / "x").write_text("x")
            (root / "p.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 32)
            sent = {}
            client = AirDropClient.__new__(AirDropClient)
            client.config = MagicMock(computer_name="pc", computer_model="Linux", service_id="abc", record_data=None)
            client._transfer_id = "T"
            client.send_POST = lambda url, body, **kw: (sent.update(body=plistlib.loads(body)), (True, b""))[1]
            with patch("opendrop.util.AirDropUtil.generate_file_icon", return_value=b"icon"):
                original = AirDropClient.send_ask.__wrapped__ if hasattr(AirDropClient.send_ask, "__wrapped__") else AirDropClient.send_ask
                original(client, [str(root / "dir"), str(root / "p.png")])
        entries = sent["body"]["Files"]
        self.assertEqual((entries[0]["FileName"], entries[0]["FileType"], entries[0]["FileIsDirectory"]), ("dir", "public.folder", True))
        self.assertEqual((entries[1]["FileName"], entries[1]["FileIsDirectory"]), ("p.png", False))
        self.assertNotIn("FileIcon", sent["body"])          # the icon belongs to the first item, which is a folder: none


class SenderEvents(unittest.TestCase):
    def test_hooks_report_real_byte_counts_only_when_enabled(self):
        import contextlib, io
        receiver.install()
        out = io.StringIO()
        with patch.dict(os.environ, {"AIRDROP_EVENTS": "1"}), contextlib.redirect_stdout(out):
            client = AirDropClient.__new__(AirDropClient)
            client.http_conn = MagicMock()
            client.http_conn.sock.gettimeout.return_value = None
            payload = b"z" * (AirDropClient.SEND_SLICE * 3 + 10)
            receiver._send_state["last"] = 0.0
            client._send_watched(payload)
            AirDropClient.start_hook(len(payload))
        parsed = [events.parse(line) for line in out.getvalue().splitlines()]
        progress = [e for e in parsed if e and e["event"] == "send_progress"]
        self.assertEqual(progress[-1]["done"], len(payload))
        self.assertEqual(progress[-1]["total"], len(payload))
        self.assertEqual([e["event"] for e in parsed if e][-1], "send_start")
        out2 = io.StringIO()
        with patch.dict(os.environ), contextlib.redirect_stdout(out2):
            os.environ.pop("AIRDROP_EVENTS", None)
            client._send_watched(payload)
        self.assertEqual(out2.getvalue(), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
