"""The Files (Nautilus) integration, with a stand-in for the Nautilus API (python3-nautilus is not needed to test it)."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
import gi
gi.require_foreign = getattr(gi, "require_foreign", None)


class FakeMenuItem:
    def __init__(self, name, label, tip=""):
        self.name, self.label, self.tip, self.handlers = name, label, tip, []

    def connect(self, signal, handler):
        self.handlers.append((signal, handler))

    def activate(self):
        for signal, handler in self.handlers:
            if signal == "activate":
                handler(self)


class FakeProvider:
    pass


def load_extension():
    fake = types.ModuleType("Nautilus")
    fake.MenuProvider, fake.MenuItem = FakeProvider, FakeMenuItem
    import gi.repository
    gi.repository.Nautilus = fake
    sys.modules["gi.repository.Nautilus"] = fake
    with patch.object(gi, "require_version", lambda *a: None):
        spec = importlib.util.spec_from_file_location("linux_airdrop_nautilus", ROOT / "nautilus/linux_airdrop_nautilus.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


ext = load_extension()


class FakeFile:
    def __init__(self, path, scheme="file"):
        self.path, self.scheme = path, scheme

    def get_uri_scheme(self):
        return self.scheme

    def get_location(self):
        return types.SimpleNamespace(get_path=lambda: self.path)


class Menu(unittest.TestCase):
    def setUp(self):
        self.provider = ext.SendWithAirDrop.__new__(ext.SendWithAirDrop)

    def test_one_entry_for_a_single_file_a_folder_or_a_mixed_selection(self):
        for selection in ([FakeFile("/home/u/a.txt")], [FakeFile("/home/u/Cartella")],
                          [FakeFile("/home/u/a.txt"), FakeFile("/home/u/b.jpg"), FakeFile("/home/u/Cartella")]):
            items = self.provider.get_file_items(selection)
            self.assertEqual([i.label for i in items], ["Invia con Linux AirDrop"])

    def test_the_old_two_argument_form_works_too(self):
        self.assertEqual(len(self.provider.get_file_items(object(), [FakeFile("/x/a")])), 1)

    def test_no_entry_for_remote_or_special_locations_or_empty_selections(self):
        self.assertEqual(self.provider.get_file_items([FakeFile("/a", "file"), FakeFile(None, "smb")]), [])
        self.assertEqual(self.provider.get_file_items([FakeFile("/a", "trash")]), [])
        self.assertEqual(self.provider.get_file_items([FakeFile(None)]), [])
        self.assertEqual(self.provider.get_file_items([]), [])
        self.assertEqual(self.provider.get_background_items(object(), object()), [])

    def test_a_broken_selection_never_raises_into_the_file_manager(self):
        self.assertEqual(self.provider.get_file_items([object()]), [])
        self.assertEqual(self.provider.get_file_items(None), [])

    def test_activating_hands_every_path_to_the_desktop_entry(self):
        info = MagicMock()
        info.launch.return_value = True
        with patch.object(ext.DesktopAppInfo, "new", return_value=info) as lookup:
            item = self.provider.get_file_items([FakeFile("/home/u/a b.txt"), FakeFile("/home/u/Cartella")])[0]
            item.activate()
        lookup.assert_called_once_with("it.foglio.LinuxAirDrop.desktop")
        launched = [f.get_path() for f in info.launch.call_args[0][0]]
        self.assertEqual(launched, ["/home/u/a b.txt", "/home/u/Cartella"])

    def test_without_a_desktop_entry_the_known_launchers_are_tried(self):
        with tempfile.TemporaryDirectory() as folder:
            launcher = Path(folder) / "start.sh"
            launcher.write_text("#!/bin/sh\n")
            launcher.chmod(0o755)
            started = []
            with patch.object(ext.DesktopAppInfo, "new", return_value=None), patch.object(ext, "LAUNCHERS", (str(launcher),)), \
                    patch.object(ext.Gio.Subprocess, "new", lambda argv, flags: started.append(argv)):
                self.assertTrue(ext.send(["/a", "/b"]))
        self.assertEqual(started, [[str(launcher), "/a", "/b"]])

    def test_if_nothing_can_be_started_it_fails_quietly(self):
        with patch.object(ext.DesktopAppInfo, "new", return_value=None), patch.object(ext, "LAUNCHERS", ("/nonexistent",)):
            self.assertFalse(ext.send(["/a"]))


class ScriptFallback(unittest.TestCase):
    SCRIPT = ROOT / "nautilus/scripts/Invia con Linux AirDrop"

    def test_the_script_is_executable_and_valid_shell(self):
        self.assertTrue(os.access(self.SCRIPT, os.X_OK))
        self.assertEqual(subprocess.run(["sh", "-n", str(self.SCRIPT)], capture_output=True).returncode, 0)

    def test_it_passes_every_selected_path_to_gio_launch_with_the_desktop_entry(self):
        with tempfile.TemporaryDirectory() as home:
            apps = Path(home) / ".local/share/applications"
            apps.mkdir(parents=True)
            (apps / "it.foglio.LinuxAirDrop.desktop").write_text("[Desktop Entry]\n")
            fakebin = Path(home) / "bin"
            fakebin.mkdir()
            gio = fakebin / "gio"
            gio.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/args"\n')
            gio.chmod(0o755)
            env = {"HOME": home, "PATH": f"{fakebin}:/usr/bin:/bin"}
            done = subprocess.run([str(self.SCRIPT), "/tmp/with space.txt", "/tmp/Cartella"], env=env, capture_output=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            args = (Path(home) / "args").read_text().splitlines()
        self.assertEqual(args, ["launch", str(apps / "it.foglio.LinuxAirDrop.desktop"), "/tmp/with space.txt", "/tmp/Cartella"])

    def test_with_nothing_selected_it_does_nothing(self):
        self.assertEqual(subprocess.run([str(self.SCRIPT)], capture_output=True, env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}).returncode, 0)

    def test_without_the_app_installed_it_says_so(self):
        with tempfile.TemporaryDirectory() as home:
            done = subprocess.run([str(self.SCRIPT), "/tmp/a"], env={"HOME": home, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True)
        if not Path("/usr/share/applications/it.foglio.LinuxAirDrop.desktop").exists():
            self.assertEqual(done.returncode, 1)
            self.assertIn("non è installato", done.stderr)


class Desktop(unittest.TestCase):
    def test_both_desktop_entries_accept_files(self):
        for path in (ROOT / "packaging/debian/it.foglio.LinuxAirDrop.desktop",):
            exec_line = next(l for l in path.read_text().splitlines() if l.startswith("Exec="))
            self.assertIn("%F", exec_line, path)
        self.assertIn("%F", (ROOT / "install-user.py").read_text())


if __name__ == "__main__":
    unittest.main(verbosity=2)
