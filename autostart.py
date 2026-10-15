"""Per-user autostart entry: starts Linux AirDrop hidden at login, with its tray icon."""
import os
from pathlib import Path

APP_ID = "it.foglio.LinuxAirDrop"


def entry_path(config_home=None):
    base = Path(config_home) if config_home else Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    return base / "autostart" / f"{APP_ID}.desktop"


def quoted(value):
    # Desktop Entry Exec quoting, including reserved characters and field codes.
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`").replace("$", "\\$").replace("%", "%%") + '"'


def content(start_script):
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=Linux AirDrop",
        "Comment=Avvia Linux AirDrop in background, con l'icona nella barra",
        "Exec=" + quoted(start_script) + " --background", "Icon=" + APP_ID, "Terminal=false", "NoDisplay=true",
        "X-GNOME-Autostart-enabled=true", "X-GNOME-Autostart-Delay=5", "X-GNOME-UsesNotifications=true", "",
    ])


def is_enabled(config_home=None):
    try:
        lines = entry_path(config_home).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    return "X-GNOME-Autostart-enabled=true" in lines and "Hidden=true" not in lines


def set_enabled(enabled, start_script, config_home=None):
    path = entry_path(config_home)
    if not enabled:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("." + path.name + ".new")
    temporary.write_text(content(start_script), encoding="utf-8")
    os.replace(temporary, path)
