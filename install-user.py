#!/usr/bin/python3
"""Install this bundle and launcher for the current Ubuntu user, without sudo."""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

import argparse

parser = argparse.ArgumentParser(description="Installa Linux AirDrop per l'utente corrente, senza sudo.")
parser.add_argument("--autostart", dest="autostart", action="store_true", default=None, help="avvia l'app in background al login")
parser.add_argument("--no-autostart", dest="autostart", action="store_false", help="non avviarla al login")
parser.add_argument("--no-files-menu", dest="files_menu", action="store_false", default=True,
                    help="non aggiungere «Invia con Linux AirDrop» al menu di Files")
options = parser.parse_args()
source = Path(__file__).resolve().parent
sys.path.insert(0, str(source))
import autostart
data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
destination = data / "linux-airdrop"
data.mkdir(parents=True, exist_ok=True)
stage = Path(tempfile.mkdtemp(prefix="linux-airdrop-install-", dir=data))
try:
    shutil.copytree(source, stage / "app", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # An older run as root could leave root-owned files in the previous copy;
    # never let removing it abort the install. Use a name that cannot collide.
    previous = data / f"linux-airdrop.previous-{os.getpid()}"
    if destination.exists():
        destination.rename(previous)
    (stage / "app").rename(destination)
    if previous.exists():
        shutil.rmtree(previous, ignore_errors=True)
        if previous.exists():
            print("Nota: la vecchia copia non è stata rimossa del tutto (contiene file di root):", previous)
            print("Eliminala con: sudo rm -rf", previous)
finally:
    shutil.rmtree(stage)
for relative in ("start.sh", "confirm.py", "backend/opendrop", "backend/owl"):
    (destination / relative).chmod(0o755)
if (destination / "backend/owl-modern").exists():
    (destination / "backend/owl-modern").chmod(0o755)
# The icon lives in the user's icon theme so the shell, dock and notifications find it by name.
icons = data / "icons/hicolor/scalable/apps"
icons.mkdir(parents=True, exist_ok=True)
shutil.copyfile(destination / "icons/it.foglio.LinuxAirDrop.svg", icons / "it.foglio.LinuxAirDrop.svg")
symbolic = data / "icons/hicolor/symbolic/apps"
symbolic.mkdir(parents=True, exist_ok=True)
for name in ("linux-airdrop-symbolic.svg", "linux-airdrop-active-symbolic.svg"):
    shutil.copyfile(destination / "icons" / name, symbolic / name)
subprocess.run(["gtk-update-icon-cache", "-f", "-t", str(data / "icons/hicolor")], check=False,
               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
apps = data / "applications"
apps.mkdir(exist_ok=True)
def quoted(value):
    # Desktop Entry Exec quoting, including reserved characters and field codes.
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%') + '"'
desktop = apps / "it.foglio.LinuxAirDrop.desktop"
desktop.write_text("\n".join([
    "[Desktop Entry]", "Type=Application", "Name=Linux AirDrop",
    "Comment=Scambia file dal menu AirDrop di iPhone e iPad",
    "Exec=" + quoted(destination / "start.sh") + " %F", "Icon=it.foglio.LinuxAirDrop",
    "Terminal=false", "Categories=GNOME;GTK;Network;FileTransfer;", "StartupNotify=true",
    "StartupWMClass=it.foglio.LinuxAirDrop", "X-GNOME-UsesNotifications=true",
    "Keywords=AirDrop;Apple;iPhone;iPad;file;", "",
]), encoding="utf-8")
subprocess.run(["desktop-file-validate", str(desktop)], check=True)
subprocess.run(["update-desktop-database", str(apps)], check=False, stdout=subprocess.DEVNULL)
if options.files_menu:
    # GNOME Files (Nautilus): a script that works everywhere (right-click > Scripts) and, where python3-nautilus
    # is installed, a real entry in the context menu. Both hand the selection to the running Linux AirDrop.
    scripts = data / "nautilus/scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "Invia con Linux AirDrop"
    shutil.copyfile(destination / "nautilus/scripts/Invia con Linux AirDrop", script)
    script.chmod(0o755)
    extensions = data / "nautilus-python/extensions"
    extensions.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(destination / "nautilus/linux_airdrop_nautilus.py", extensions / "linux_airdrop_nautilus.py")
    print("Menu di Files: «Script → Invia con Linux AirDrop» è pronto.")
    if not Path("/usr/lib/x86_64-linux-gnu/nautilus/extensions-4/libnautilus-python.so").exists():
        print("Per la voce direttamente nel menu: sudo apt install python3-nautilus, poi chiudi Files con «nautilus -q».")
if options.autostart is not None:
    autostart.set_enabled(options.autostart, destination / "start.sh")
    print("Avvio al login:", "attivato" if options.autostart else "disattivato")
print("Installato:", destination)
print("Apri «Linux AirDrop» dal menu applicazioni di Ubuntu.")
