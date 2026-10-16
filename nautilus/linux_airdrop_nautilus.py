"""GNOME Files (Nautilus) context-menu entry: "Invia con Linux AirDrop".

Needs the `python3-nautilus` package (nautilus-python). Select one or more files or folders, right-click,
choose the entry: they are handed to the Linux AirDrop that is already running (or it is started), through its
desktop entry, so no second session is ever created. Only local files get the entry.

This file runs inside Nautilus, so it is deliberately small and never raises: a failure here must not take
the file manager's menus down with it.
"""
import os
from pathlib import Path

import gi
for _version in ("4.1", "4.0", "3.0"):
    try:
        gi.require_version("Nautilus", _version)
        break
    except ValueError:
        continue
from gi.repository import GObject, Gio, GLib, Nautilus
try:
    gi.require_version("GioUnix", "2.0")
    from gi.repository import GioUnix
    DesktopAppInfo = GioUnix.DesktopAppInfo
except (ValueError, ImportError):
    DesktopAppInfo = Gio.DesktopAppInfo

DESKTOP_ID = "it.foglio.LinuxAirDrop.desktop"
LAUNCHERS = ("/usr/bin/linux-airdrop", str(Path.home() / ".local/share/linux-airdrop/start.sh"))
LABEL = "Invia con Linux AirDrop"
TIP = "Invia a un iPhone o iPad con AirDrop"
MAX_ITEMS = 512


def local_paths(files):
    """Paths of the selection, or [] if anything in it is not a local file (trash, network, recent...)."""
    paths = []
    for entry in files:
        if entry.get_uri_scheme() != "file":
            return []
        location = entry.get_location()
        path = location.get_path() if location is not None else None
        if not path:
            return []
        paths.append(path)
    return paths


def send(paths):
    """Hand the paths to Linux AirDrop. Returns True if something was started."""
    try:
        info = DesktopAppInfo.new(DESKTOP_ID)
        if info is not None:
            return bool(info.launch([Gio.File.new_for_path(p) for p in paths], None))
    except GLib.Error:
        pass
    for launcher in LAUNCHERS:
        if os.access(launcher, os.X_OK):
            try:
                Gio.Subprocess.new([launcher, *paths], Gio.SubprocessFlags.NONE)
                return True
            except GLib.Error:
                continue
    return False


class SendWithAirDrop(GObject.GObject, Nautilus.MenuProvider):
    def get_file_items(self, *args):
        # Nautilus 4 calls get_file_items(files); Nautilus 3 calls get_file_items(window, files).
        try:
            paths = local_paths(args[-1])
            if not paths or len(paths) > MAX_ITEMS:
                return []
            item = Nautilus.MenuItem(name="LinuxAirDrop::send", label=LABEL, tip=TIP)
            item.connect("activate", lambda _item: send(paths))
            return [item]
        except Exception:
            return []

    def get_background_items(self, *args):
        return []
