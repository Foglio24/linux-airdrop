"""What the user may send: validation, limits and the ZIP form of a folder."""
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import outgoing


class Inspect(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def tree(self):
        top = self.root / "Gita"
        (top / "sub").mkdir(parents=True)
        (top / "a.txt").write_bytes(b"a" * 10)
        (top / "sub" / "b.jpg").write_bytes(b"b" * 90)
        return top

    def test_files_and_folders_with_sizes_and_kinds(self):
        photo = self.root / "p.jpg"
        photo.write_bytes(b"x" * 500)
        plan = outgoing.inspect_paths([photo, self.tree()])
        self.assertEqual([(e["name"], e["kind"], e["size"], e["is_dir"]) for e in plan["entries"]],
                         [("p.jpg", "image", 500, False), ("Gita", "folder", 100, True)])
        self.assertEqual((plan["total"], plan["folders"], plan["problems"]), (600, 1, []))
        self.assertEqual(plan["entries"][1]["files"], 2)

    def test_missing_unreadable_special_and_duplicate_items_are_skipped_with_a_reason(self):
        good = self.root / "good.txt"
        good.write_text("g")
        other = self.root / "elsewhere"
        other.mkdir()
        (other / "good.txt").write_text("same name, other place")
        fifo = self.root / "pipe"
        os.mkfifo(fifo)
        secret = self.root / "secret"
        secret.write_text("s")
        secret.chmod(0)
        try:
            plan = outgoing.inspect_paths([good, good, other / "good.txt", self.root / "gone", fifo, secret])
        finally:
            secret.chmod(0o600)
        self.assertEqual([e["name"] for e in plan["entries"]], ["good.txt"])
        reasons = dict(plan["skipped"])
        self.assertIn("stesso nome", reasons["good.txt"])
        self.assertIn("non esiste", reasons["gone"])
        self.assertIn("normale", reasons["pipe"])
        if os.geteuid() != 0:
            self.assertIn("leggibile", reasons["secret"])

    def test_nothing_to_send_is_a_problem(self):
        self.assertTrue(outgoing.inspect_paths([])["problems"])
        self.assertTrue(outgoing.inspect_paths([self.root / "nope"])["problems"])

    def test_size_limit(self):
        big = self.root / "big.bin"
        big.write_bytes(b"0" * 2000)
        plan = outgoing.inspect_paths([big], max_bytes=1000)
        self.assertEqual(len(plan["problems"]), 1)
        self.assertIn("Troppo grande", plan["problems"][0])

    def test_links_inside_folders_are_not_counted_or_followed(self):
        top = self.tree()
        outside = self.root / "outside.bin"
        outside.write_bytes(b"o" * 5000)
        (top / "link").symlink_to(outside)
        (top / "dirlink").symlink_to(self.root)
        plan = outgoing.inspect_paths([top])
        self.assertEqual(plan["total"], 100)
        self.assertEqual(plan["ignored_inside"], 1)

    def test_a_link_given_directly_is_resolved_to_its_target(self):
        target = self.root / "real.txt"
        target.write_text("hello")
        link = self.root / "alias.txt"
        link.symlink_to(target)
        plan = outgoing.inspect_paths([link])
        self.assertEqual((plan["entries"][0]["name"], plan["entries"][0]["size"]), ("real.txt", 5))

    def test_huge_folders_are_refused_instead_of_walked_forever(self):
        top = self.root / "many"
        top.mkdir()
        for i in range(30):
            (top / f"{i}").write_text("")
        original = outgoing.WALK_LIMIT
        outgoing.WALK_LIMIT = 10
        try:
            plan = outgoing.inspect_paths([top])
        finally:
            outgoing.WALK_LIMIT = original
        self.assertEqual(plan["entries"], [])
        self.assertIn("troppi elementi", plan["skipped"][0][1])


class Zip(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_zip_keeps_the_folder_name_empty_folders_and_skips_links(self):
        top = self.root / "Gita"
        (top / "vuota").mkdir(parents=True)
        (top / "sub").mkdir()
        (top / "sub" / "b.txt").write_text("bee")
        outside = self.root / "outside.txt"
        outside.write_text("secret")
        (top / "link").symlink_to(outside)
        work = outgoing.new_workdir(self.root / "outgoing")
        archive = outgoing.make_zip(top, work)
        self.assertEqual(archive.name, "Gita.zip")
        with zipfile.ZipFile(archive) as z:
            names = sorted(z.namelist())
            self.assertEqual(names, ["Gita/", "Gita/sub/", "Gita/sub/b.txt", "Gita/vuota/"])
            self.assertEqual(z.read("Gita/sub/b.txt"), b"bee")
            self.assertIsNone(z.testzip())

    def test_zip_can_be_cancelled(self):
        top = self.root / "F"
        top.mkdir()
        (top / "x").write_text("x")
        work = outgoing.new_workdir(self.root / "outgoing")
        with self.assertRaises(InterruptedError):
            outgoing.make_zip(top, work, cancelled=lambda: True)

    def test_workdir_is_private_and_old_ones_are_purged(self):
        parent = self.root / "outgoing"
        fresh = outgoing.new_workdir(parent)
        old = outgoing.new_workdir(parent)
        os.utime(old, (time.time() - 3 * 86400,) * 2)
        self.assertEqual(oct(parent.stat().st_mode & 0o777), "0o700")
        self.assertEqual(outgoing.purge_old(parent), 1)
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())
        self.assertEqual(outgoing.purge_old(self.root / "missing"), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
