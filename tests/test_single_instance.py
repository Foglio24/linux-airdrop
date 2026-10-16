"""A second launch with files (what "Invia con Linux AirDrop" does) must reach the running instance, not start another."""
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

HELPER = Path(__file__).resolve().parent / "single_instance_helper.py"


class SingleInstance(unittest.TestCase):
    def test_files_go_to_the_running_instance(self):
        with tempfile.TemporaryDirectory() as tmp:
            primary = subprocess.Popen(["/usr/bin/python3", "-B", str(HELPER), tmp, "primary"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.time() + 15
                while not (Path(tmp) / "primary.pid").exists() and time.time() < deadline:
                    time.sleep(0.1)
                if primary.poll() is not None:
                    self.skipTest("no session bus or display for the test application: " + primary.stderr.read().decode()[-200:])
                time.sleep(1.5)       # let it register on the bus
                second = subprocess.run(["/usr/bin/python3", "-B", str(HELPER), tmp, "second", "/tmp/uno file.txt", "/tmp/due"],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(second.returncode, 0, second.stderr[-300:])
                primary.wait(timeout=20)
            finally:
                if primary.poll() is None:
                    primary.kill()
                    primary.wait()
            lines = (Path(tmp) / "received.txt").read_text().splitlines()
            primary_pid = (Path(tmp) / "primary.pid").read_text()
        self.assertEqual(len(lines), 1)
        pid, paths = lines[0].split(" ", 1)
        self.assertEqual(pid, primary_pid)                       # handled by the first process, not by the second
        self.assertEqual(paths, "/tmp/uno file.txt|/tmp/due")


if __name__ == "__main__":
    unittest.main(verbosity=2)
