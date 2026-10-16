"""Helper for test_single_instance.py: runs the real application class under a test application id."""
import importlib.util
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
tmp, role = sys.argv[1], sys.argv[2]
files = sys.argv[3:]
os.environ["XDG_STATE_HOME"] = tmp + "/state"
os.environ["XDG_CONFIG_HOME"] = tmp + "/config"
sys.path.insert(0, str(ROOT))
sys.argv = ["app.py"]
spec = importlib.util.spec_from_file_location("app_single_instance", ROOT / "app.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
from gi.repository import GLib
m.APP_ID = "it.foglio.LinuxAirDrop.UnitTest"


class App(m.AirDrop):
    def activate(self, *_):
        if self.window is None:
            self.build_window(present=False)       # never put a window on the screen of whoever runs the tests

    def request_send(self, paths):
        with open(tmp + "/received.txt", "a") as out:
            out.write(f"{os.getpid()} " + "|".join(paths) + "\n")
        GLib.timeout_add(300, self.quit)


app = App()
if role == "primary":
    with open(tmp + "/primary.pid", "w") as out:
        out.write(str(os.getpid()))
    GLib.timeout_add_seconds(25, app.quit)         # safety net
sys.exit(app.run(["app.py", *files]))
