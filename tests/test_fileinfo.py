"""File kinds, display-safe text and number formatting."""
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fileinfo as fi


class Kinds(unittest.TestCase):
    def test_uti_wins_over_a_misleading_extension(self):
        self.assertEqual(fi.kind_of("document.txt", "public.jpeg"), "image")
        self.assertEqual(fi.kind_of("a", "com.adobe.pdf"), "pdf")
        self.assertEqual(fi.kind_of("a", "public.mpeg-4"), "video")
        self.assertEqual(fi.kind_of("a", "public.zip-archive"), "archive")

    def test_generic_uti_falls_back_to_the_extension(self):
        self.assertEqual(fi.kind_of("holiday.MOV", "public.data"), "video")
        self.assertEqual(fi.kind_of("song.flac", "public.content"), "audio")
        self.assertEqual(fi.kind_of("report.pdf"), "pdf")
        self.assertEqual(fi.kind_of("backup.tar"), "archive")
        self.assertEqual(fi.kind_of("mystery.zzz"), "other")
        self.assertEqual(fi.kind_of("noextension"), "other")

    def test_directories_are_folders_whatever_the_name(self):
        self.assertEqual(fi.kind_of("photos.zip", "public.zip-archive", is_dir=True), "folder")

    def test_every_kind_has_label_and_icon(self):
        for kind in fi.KINDS:
            self.assertTrue(fi.kind_label(kind))
            self.assertTrue(fi.kind_icon(kind).endswith("-symbolic"))
        self.assertEqual(fi.kind_label("nonsense"), "File")


class Text(unittest.TestCase):
    def test_bidi_override_and_controls_are_removed(self):
        spoof = "photo‮gpj.exe"
        self.assertEqual(fi.clean_text(spoof), "photogpj.exe")
        self.assertEqual(fi.clean_text("a\x00b\x1b[31mc\nd"), "a b [31mc d")
        self.assertEqual(fi.clean_text("zero​width"), "zerowidth")

    def test_long_text_is_cut_and_non_text_is_safe(self):
        self.assertEqual(len(fi.clean_text("x" * 500, 50)), 50)
        self.assertEqual(fi.clean_text(None), "")
        self.assertEqual(fi.clean_text(12), "12")

    def test_summary(self):
        self.assertEqual(fi.summarize_names(["a.txt"]), "a.txt")
        self.assertEqual(fi.summarize_names(["a", "b", "c", "d"]), "a, b e altri 2")
        self.assertEqual(fi.summarize_names([]), "Nessun nome")

    def test_device_label(self):
        self.assertEqual(fi.device_label("iPhone15,2"), "iPhone")
        self.assertEqual(fi.device_label("MacBookPro"), "Mac portatile")
        self.assertEqual(fi.device_label(""), "Dispositivo Apple")
        self.assertEqual(fi.device_label("Strano"), "Strano")


class Numbers(unittest.TestCase):
    def test_sizes_use_decimal_units_and_comma(self):
        self.assertEqual(fi.format_size(0), "0 byte")
        self.assertEqual(fi.format_size(999), "999 byte")
        self.assertEqual(fi.format_size(1500), "1,5 kB")
        self.assertEqual(fi.format_size(12_400_000), "12,4 MB")
        self.assertEqual(fi.format_size(2_000_000_000), "2 GB")
        self.assertEqual(fi.format_size(None), "—")

    def test_speed(self):
        self.assertEqual(fi.format_speed(3_200_000), "3,2 MB/s")
        self.assertEqual(fi.format_speed(512), "512 B/s")
        self.assertEqual(fi.format_speed(None), "—")

    def test_duration(self):
        self.assertEqual(fi.format_duration(12), "12 s")
        self.assertEqual(fi.format_duration(125), "2 min 5 s")
        self.assertEqual(fi.format_duration(3780), "1 h 3 min")
        self.assertEqual(fi.format_duration(None), "—")

    def test_when(self):
        now = time.mktime((2026, 10, 16, 15, 0, 0, 0, 0, -1))
        self.assertEqual(fi.format_when(now - 3600, now), "oggi 14:00")
        self.assertEqual(fi.format_when(now - 86400, now), "ieri 15:00")
        self.assertEqual(fi.format_when(time.mktime((2026, 3, 2, 9, 5, 0, 0, 0, -1)), now), "2 mar 09:05")
        self.assertEqual(fi.format_when(time.mktime((2025, 3, 2, 9, 5, 0, 0, 0, -1)), now), "2 mar 2025")


if __name__ == "__main__":
    unittest.main()
