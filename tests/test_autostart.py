"""The login entry must start the app hidden, quote odd paths safely and be trivially reversible."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

MODULE = Path(__file__).resolve().parents[1] / "autostart.py"
spec = importlib.util.spec_from_file_location("autostart_under_test", MODULE)
autostart = importlib.util.module_from_spec(spec)
spec.loader.exec_module(autostart)


class AutostartEntry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

    def test_enable_writes_a_hidden_start_entry_and_disable_removes_it(self):
        self.assertFalse(autostart.is_enabled(self.home))
        autostart.set_enabled(True, "/opt/airdrop/start.sh", self.home)
        text = autostart.entry_path(self.home).read_text()
        self.assertIn('Exec="/opt/airdrop/start.sh" --background', text)
        self.assertIn("X-GNOME-Autostart-enabled=true", text)
        self.assertIn("X-GNOME-Autostart-Delay=5", text)
        self.assertTrue(autostart.is_enabled(self.home))
        autostart.set_enabled(False, "/opt/airdrop/start.sh", self.home)
        self.assertFalse(autostart.is_enabled(self.home))
        autostart.set_enabled(False, "/opt/airdrop/start.sh", self.home)  # idempotent

    def test_paths_with_spaces_quotes_and_dollars_stay_one_argument(self):
        for path in ("/home/a b/start.sh", '/home/x"y/start.sh', "/home/$USER/s.sh", "/home/100%/s.sh", "/home/`id`/s.sh"):
            exec_line = [l for l in autostart.content(path).splitlines() if l.startswith("Exec=")][0]
            self.assertTrue(exec_line.endswith('" --background'), exec_line)
            self.assertNotIn("`id`", exec_line.replace("\\`id\\`", ""))

    def test_entry_passes_desktop_file_validation_when_the_tool_exists(self):
        autostart.set_enabled(True, "/usr/bin/true", self.home)
        try:
            done = subprocess.run(["desktop-file-validate", str(autostart.entry_path(self.home))], capture_output=True, text=True)
        except OSError:
            self.skipTest("desktop-file-validate not installed")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

    def test_a_hidden_entry_counts_as_disabled(self):
        autostart.set_enabled(True, "/usr/bin/true", self.home)
        path = autostart.entry_path(self.home)
        path.write_text(path.read_text() + "Hidden=true\n")
        self.assertFalse(autostart.is_enabled(self.home))


if __name__ == "__main__":
    unittest.main(verbosity=2)
