"""Pre-acceptance preview: display-safe description and the sandboxed thumbnail decoder."""
import io
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

BUNDLE = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(BUNDLE), str(BUNDLE / "backend/python")]
import preview


def jp2(width=640, height=480, color=(10, 90, 200)):
    from PIL import Image
    data = io.BytesIO()
    Image.new("RGB", (width, height), color).save(data, "JPEG2000")
    return data.getvalue()


class Describe(unittest.TestCase):
    def test_names_types_sizes_and_totals(self):
        info, icon = preview.describe({
            "SenderComputerName": "iPhone di Anna", "SenderModelName": "iPhone15,2",
            "Files": [{"FileName": "a.heic", "FileType": "public.heic", "FileSize": 1000},
                      {"FileName": "Gita", "FileType": "public.folder", "FileIsDirectory": True, "FileSize": 24},
                      {"FileName": "Relazione.pdf", "FileType": "com.adobe.pdf", "FileSize": 500}]})
        self.assertEqual([f["kind"] for f in info["files"]], ["image", "folder", "pdf"])
        self.assertEqual(info["total_size"], 1524)
        self.assertEqual(info["model_label"], "iPhone")
        self.assertIsNone(icon)

    def test_total_is_unknown_unless_every_size_is_known(self):
        info, _ = preview.describe({"Files": [{"FileName": "a", "FileSize": 5}, {"FileName": "b"}]})
        self.assertIsNone(info["total_size"])
        self.assertEqual([f["size"] for f in info["files"]], [5, None])

    def test_nothing_is_invented_for_an_empty_request(self):
        info, icon = preview.describe({})
        self.assertEqual((info["files"], info["items"], info["total_size"], info["sender"]), ([], [], None, "Dispositivo sconosciuto"))

    def test_hostile_text_and_types_are_neutralised(self):
        info, _ = preview.describe({"SenderComputerName": "Apple Support‮\x1b[31m",
                                    "Files": ["not a dict", {"FileName": "../../etc/passwd‮gpj.exe", "FileSize": -1, "FileType": 7},
                                              {"FileName": "x" * 5000, "FileSize": True}] + [{"FileName": "n"}] * 600})
        self.assertNotIn("‮", info["sender"])
        self.assertNotIn("\x1b", info["sender"])
        self.assertEqual(info["files"][0]["name"], "passwdgpj.exe")      # a path is reduced to its last component
        self.assertIsNone(info["files"][0]["size"])
        self.assertIsNone(info["files"][1]["size"])
        self.assertLessEqual(len(info["files"][1]["name"]), 255)
        self.assertLessEqual(len(info["files"]), preview.MAX_FILES_LISTED)
        self.assertEqual(info["file_count"], 603)

    def test_links_are_listed_and_cleaned(self):
        info, _ = preview.describe({"Items": ["https://example.org/‮a", {"URL": "http://b.example"}, 5, b"https://c.example"]})
        self.assertEqual(len(info["items"]), 3)
        self.assertTrue(all("‮" not in item for item in info["items"]))

    def test_icon_is_returned_only_when_plausible(self):
        self.assertIsNotNone(preview.describe({"FileIcon": b"abc"})[1])
        self.assertIsNone(preview.describe({"FileIcon": b""})[1])
        self.assertIsNone(preview.describe({"FileIcon": "text"})[1])
        self.assertIsNone(preview.describe({"FileIcon": b"x" * (preview.MAX_ICON_BYTES + 1)})[1])


class Thumbnail(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.dir = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_valid_image_becomes_a_bounded_thumbnail(self):
        result = preview.make_thumbnail(jp2(1000, 500), self.dir)
        self.assertEqual((result["width"], result["height"]), (256, 128))
        width, height, pixels = preview.read_pixels(result["file"])
        self.assertEqual(len(pixels), width * height * 4)
        self.assertEqual(tuple(pixels[:3]), (10, 90, 200))
        self.assertFalse((self.dir / "icon.bin").exists())          # the input copy is not left behind

    def test_png_and_jpeg_are_accepted_but_other_formats_are_not(self):
        from PIL import Image
        for fmt in ("PNG", "JPEG"):
            data = io.BytesIO()
            Image.new("RGB", (40, 30), (1, 2, 3)).save(data, fmt)
            self.assertIsNotNone(preview.make_thumbnail(data.getvalue(), self.dir), fmt)
        for fmt in ("BMP", "GIF", "TIFF"):
            data = io.BytesIO()
            Image.new("RGB", (40, 30)).save(data, fmt)
            self.assertIsNone(preview.make_thumbnail(data.getvalue(), self.dir), fmt)

    def test_garbage_empty_and_oversized_input_give_no_thumbnail(self):
        self.assertIsNone(preview.make_thumbnail(b"", self.dir))
        self.assertIsNone(preview.make_thumbnail(b"definitely not an image" * 50, self.dir))
        self.assertIsNone(preview.make_thumbnail(b"\x00" * (preview.MAX_ICON_BYTES + 1), self.dir))
        self.assertIsNone(preview.make_thumbnail(jp2()[:200], self.dir))      # truncated

    def test_huge_declared_dimensions_are_refused_before_decoding(self):
        from PIL import Image
        data = io.BytesIO()
        Image.new("1", (30000, 30000)).save(data, "PNG")        # tiny file, 900 megapixels once decoded
        self.assertLess(len(data.getvalue()), preview.MAX_ICON_BYTES)
        self.assertIsNone(preview.make_thumbnail(data.getvalue(), self.dir))

    def test_a_hung_decoder_is_killed_and_reported_as_no_thumbnail(self):
        import subprocess
        with patch.object(preview.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 6)):
            self.assertIsNone(preview.make_thumbnail(jp2(), self.dir))

    def test_forged_output_files_are_rejected(self):
        for name, content in (("short", b"ADRP"), ("magic", b"NOPE" + struct.pack(">II", 1, 1) + b"\0" * 4),
                              ("big", preview.MAGIC + struct.pack(">II", 9999, 9999) + b"\0" * 16),
                              ("size", preview.MAGIC + struct.pack(">II", 2, 2) + b"\0" * 5)):
            path = self.dir / name
            path.write_bytes(content)
            with self.subTest(name), self.assertRaises(ValueError):
                preview.read_header(path)

    def test_decoder_runs_with_resource_limits(self):
        import resource
        calls = []
        with patch.object(resource, "setrlimit", side_effect=lambda which, value: calls.append((which, value))):
            preview._limit_resources()
        limits = dict(calls)
        self.assertEqual(limits[resource.RLIMIT_CPU], (5, 5))
        self.assertEqual(limits[resource.RLIMIT_CORE], (0, 0))
        self.assertIn(resource.RLIMIT_AS, limits)


if __name__ == "__main__":
    unittest.main(verbosity=2)
