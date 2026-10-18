#!/usr/bin/python3
"""GTK front end for native AirDrop over OWL and hardened OpenDrop."""
import argparse
import json
import os
import platform
from pathlib import Path
from dataclasses import replace
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

VERSION = "1.3.0"
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
import radio_compat
STATE = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "linux-airdrop"


def recovery_needed():
    try:
        saved = json.loads((STATE / "recovery-required.json").read_text())
        return saved.get("boot_id") == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except (OSError, ValueError):
        return False


def require_recovery(message):
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "recovery-required.json").write_text(json.dumps({
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "message": message,
    }, ensure_ascii=False))


def hardware():
    for device in Path("/sys/class/net").iterdir():
        if (device / "device/driver").resolve().name == "mt7921e" and (device / "phy80211").exists():
            if device.name not in ("ldmon0", "ldack0"):
                return device.name
    return None


def radio_capability(iface):
    if not iface or recovery_needed():
        return False
    process = None
    phy = (Path("/sys/class/net") / iface / "phy80211").resolve().name
    try:
        process = subprocess.Popen(["iw", "phy", phy, "info"], stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True,
                                   env=dict(os.environ, LANG="C", LC_ALL="C"))
        output, _ = process.communicate(timeout=4)
        return process.returncode == 0 and "supports active monitor" in output.lower()
    except (OSError, subprocess.TimeoutExpired):
        if process is not None and process.poll() is None:
            process.kill()
            try:
                process.communicate(timeout=0.25)
            except subprocess.TimeoutExpired:
                require_recovery("Il driver Wi-Fi non risponde: riavvia Ubuntu prima di riprovare.")
        return False


def driver_trial_available():
    return radio_compat.verify_legacy_bundle(BASE / "backend/driver-compat", platform.release()).verified


def radio_inventory(iface):
    """Read capabilities once; kernel flags are probed by the privileged bridge."""
    driver, pci, output = None, None, ""
    if iface:
        device = Path("/sys/class/net") / iface
        driver = (device / "device/driver").resolve().name
        try:
            pci = (device / "device/device").read_text().strip().lower()
            phy = (device / "phy80211").resolve().name
            done = subprocess.run(["iw", "phy", phy, "info"], capture_output=True, text=True,
                                  timeout=4, env=dict(os.environ, LANG="C", LC_ALL="C"))
            output = done.stdout if done.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            pass
    capabilities = radio_compat.RadioCapabilities(
        driver=driver, hardware_supported=driver == "mt7921e" and pci in radio_compat.SUPPORTED_PCI_IDS,
        pci_id=pci, missing_tools=tuple(name for name in ("iw", "ip", "nmcli", "wpa_supplicant", "wpa_cli", "pkexec")
                                      if shutil.which(name) is None),
        **radio_compat.parse_iw_capabilities(output))
    legacy = radio_compat.verify_legacy_bundle(BASE / "backend/driver-compat", platform.release())
    return capabilities, legacy, radio_compat.select_plan(capabilities, legacy)


if "--check" in sys.argv:
    card = hardware()
    capabilities, legacy, plan = radio_inventory(card)
    print(json.dumps({"wifi": card, "active_monitor": capabilities.active_monitor, "backend": (BASE / "backend/owl").is_file(),
                      "ios": "27: da verificare su dispositivo reale", "mode": "Apple AirDrop / AWDL",
                      "radio_plan": plan.as_dict(), "legacy_verified": legacy.verified,
                      "restart_required": recovery_needed()}, ensure_ascii=False))
    sys.exit(0)

if "--diagnose" in sys.argv:
    # The same health checks as the "Diagnostica" page, as text for a bug report. Read-only; exit status 1 on errors.
    sys.path.insert(0, str(BASE))
    import diagnostics
    import system_helper
    probe = diagnostics.System()
    card = hardware()
    capabilities, legacy, plan = radio_inventory(card)
    facts = {"cli": True, "session_active": probe.exists("sys/class/net/awdl0"), "session_starting": False, "iface": card,
             "channel": 6, "wifi_direct": True, "radio_ready": capabilities.active_monitor, "trial_ready": legacy.verified,
             "radio_plan": plan.as_dict(),
             "receiver_pid": next((pid for pid, _, argv in probe.processes() if any(a.endswith("opendrop_runner.py") for a in argv)), None),
             "ble_running": any(a.endswith("blewake.py") for _, _, argv in probe.processes() for a in argv),
             "helper": system_helper.status(), "recovery_needed": recovery_needed()}
    found = diagnostics.check_all(probe, facts, diagnostics.slow_facts(probe), BASE)
    print(diagnostics.report_text(found, facts, version=VERSION))
    sys.exit(1 if diagnostics.overall(found) == diagnostics.ERROR else 0)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk
try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
except (ValueError, ImportError):
    GLibUnix = None

import autostart
import diagnostics
import getpass
import outgoing
import repair
import system_helper
import transfer_events as tev
from fileinfo import clean_text, format_size, kind_icon, kind_of
from progress import Meter
from store import Store, make_device_key
from tray import TrayIcon
from ui_devices import DevicesPage
from ui_diagnostics import DiagnosticsPage
from ui_nexaawdl import NexaAwdlPage
from ui_history import HistoryPage
from ui_transfers import FINAL_STATES, Transfer, TransferList

APP_ID = "it.foglio.LinuxAirDrop"
CHANNELS = (6, 44)   # the iPhone's channel sequence only ever holds 6, 44, 100 and 149; 36 was never discovered
DURATIONS = (("25m", "25 minuti", 1500), ("2h", "2 ore", 7200), ("always", "Finché non la interrompo", 0))
# How often the receiver re-announces itself. The phone, when idle, is on our channel for one slot in sixteen
# (about 65 ms per 1.048 s); at 5 s an announcement was heard only by luck, tens of seconds late. A period a
# little under that cycle makes successive announcements sweep through the phone's window instead of missing it.
ANNOUNCE_SECONDS = "0.5"
TRAY_ICONS = {"idle": "linux-airdrop-symbolic", "active": "linux-airdrop-active-symbolic"}
SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "linux-airdrop" / "settings.json"


def load_settings():
    """Small, validated preferences file. Anything unexpected falls back to the defaults."""
    defaults = {"name": "", "channel": 6, "wifi_direct": True, "background": True, "auto_visible": False, "duration": "25m",
                "remember": True, "auto_repair": True, "radio_ack": True, "nexa_enabled": True, "nexa_level": "info"}
    try:
        saved = json.loads(SETTINGS.read_text())
    except (OSError, ValueError):
        return defaults
    if not isinstance(saved, dict):
        return defaults
    if isinstance(saved.get("name"), str):
        defaults["name"] = saved["name"].strip()[:64]
    if saved.get("channel") in CHANNELS:
        defaults["channel"] = saved["channel"]
    for key in ("wifi_direct", "background", "auto_visible", "remember", "auto_repair", "radio_ack", "nexa_enabled"):
        if isinstance(saved.get(key), bool):
            defaults[key] = saved[key]
    if saved.get("duration") in [key for key, _, _ in DURATIONS]:
        defaults["duration"] = saved["duration"]
    if saved.get("nexa_level") in ("error", "warning", "info", "debug"):
        defaults["nexa_level"] = saved["nexa_level"]
    return defaults


def save_settings(values):
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def caption(text):
    return Gtk.Label(label=text, wrap=True, xalign=0, css_classes=["caption", "dim-label"])


DROP_CSS = """
.drop-zone { background-color: alpha(@accent_bg_color, 0.18); border: 3px dashed @accent_bg_color; border-radius: 18px; margin: 10px; }
.drop-zone-text { font-weight: 700; font-size: 1.25em; }
.drop-card { background-color: @window_bg_color; border-radius: 14px; padding: 20px 28px; box-shadow: 0 2px 14px alpha(black, 0.35); }
"""
# Sending to an iPhone over AWDL: the range of speeds seen (real transfers and the link emulator), used only to warn
# before a very large send; a send gives up after this long without any progress.
SEND_SPEEDS = (100_000, 300_000)
LARGE_SEND_BYTES = 100_000_000
SEND_STALL_SECONDS = 90
# What a finished transfer maps to in the history (in_progress is only ever a transient state).
FINAL_STATUS = {"done": "completed", "failed": "failed", "cancelled": "cancelled", "declined": "declined"}


class SendJob:
    """One outgoing transfer: what to send, to whom, and the processes involved."""

    def __init__(self, entries, folder_mode="zip"):
        self.id = uuid.uuid4().hex[:12]
        self.entries = entries
        self.folder_mode = folder_mode
        self.workdir = None
        self.peer = None
        self.process = None
        self.cancelled = False
        self.declined = False
        self.abort_reason = None     # set when the session ends under a running send
        self.transfer = None


class AirDrop(Adw.Application):
    data_rate = None   # OWL -R for data frames; None keeps OWL's default (12 Mbit/s)

    def __init__(self, screenshot=None, activate_radio=False, fixed_radio=True, radio_channel=None, fixed_ack=None, go_anchor=None, background_start=False):
        # HANDLES_OPEN: "Invia con Linux AirDrop" in Files and a second launch with files reach the running instance.
        flags = Gio.ApplicationFlags.HANDLES_OPEN | (Gio.ApplicationFlags.NON_UNIQUE if screenshot else Gio.ApplicationFlags.DEFAULT_FLAGS)
        super().__init__(application_id=APP_ID, flags=flags)
        saved = load_settings()
        self.screenshot = screenshot
        self.activate_radio = activate_radio
        self.fixed_radio = fixed_radio
        # Radio acknowledgements for frames sent to the Apple device (OWL -A). Without them every unicast frame goes out
        # once, and on the short windows in which a phone listens on our channel a lost frame is gone for good: uploads
        # to the iPhone stalled after a few hundred kB. See docs/SENDING.md.
        self.radio_ack = saved["radio_ack"] if fixed_ack is None else fixed_ack
        self.go_anchor = saved["wifi_direct"] if go_anchor is None else go_anchor
        self.radio_channel = saved["channel"] if radio_channel is None else radio_channel
        self.background = saved["background"]
        self.auto_visible = saved["auto_visible"]
        self.duration = saved["duration"]
        # NexaAWDL: the observer engine started by the bridge (see nexaawdl/ and docs/NEXAAWDL.md).
        self.nexa_enabled = saved["nexa_enabled"]
        self.nexa_level = saved["nexa_level"]
        self.nexa_dir = STATE / "nexaawdl"
        self.nexa_status_path = self.nexa_dir / "status.json"
        self.background_start = background_start
        self.tray = None
        self.resident = False
        self.stopping = False
        self.shutting_down = False
        self.user_stop = False
        self.end_reason = None
        self.helper = "missing"
        self.saved_name = saved["name"]
        self.window = None
        self.bridge = None
        self.receiver = None
        self.ble = None
        self.finder = None
        self.sender = None
        self.active = False
        self.closing = False
        self.held = False
        self.started_at = None
        self.last_activity = None
        self.last_error = None
        self.display_name = self.saved_name or socket.gethostname()
        self.log_lines = []
        self.log_buffer = Gtk.TextBuffer()
        self.log_view = None
        self.known_files = set()
        self.iface = hardware()
        self.compat_capabilities, self.compat_legacy, self.compat_plan = radio_inventory(self.iface)
        self.radio_ready = self.compat_plan.mode == "native"
        self.trial_ready = self.compat_legacy.verified
        self.log_dirty = False
        self.session_log = None
        self.go_wanted = self.go_anchor
        download = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or str(Path.home() / "Downloads")
        self.destination = Path(download) / "LinuxAirDrop"
        self.version = VERSION
        # Transfers: live state, the history/devices database, the outgoing queue and repair bookkeeping.
        self.remember = saved["remember"]
        self.auto_repair = saved["auto_repair"]
        self.store = Store(STATE / "history.db", enabled=self.remember)
        self.store.mark_interrupted()
        self.store.prune()
        outgoing.purge_old(STATE / "outgoing")
        self.attempts = repair.Attempts(STATE)
        self.transfers = {}
        self.send_queue = []
        self.current_send = None
        self.asking_start = False
        self.restart_after = False
        self.receiver_ifindex = None
        self.last_announce = None
        self.last_auto_check = 0.0
        self.last_peer = None
        self.transfer_list = None
        self.stack = self.history_page = self.devices_page = self.diag_page = self.drop_zone = None
        self.connect("startup", self.on_startup)
        self.connect("shutdown", self.on_shutdown)
        self.connect("activate", self.activate)
        self.connect("open", self.on_open)

    # ------------------------------------------------------------------ setup
    def on_startup(self, *_):
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(BASE / "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_string(DROP_CSS)
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, callback in (("show", self.act_show), ("stop-session", self.act_stop), ("open-folder", self.open_folder),
                               ("show-log", self.show_log), ("about", self.show_about), ("quit", self.act_quit)):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
        self.set_accels_for_action("app.quit", ["<Primary>q"])
        self.set_accels_for_action("app.show-log", ["<Primary>l"])
        if not self.screenshot:
            self.start_tray()

    def on_shutdown(self, *_):
        self.shutting_down = True
        if self.tray is not None:
            self.tray.stop()

    def activate(self, *_):
        if self.window is not None:
            self.window.present()
            return
        self.build_window(present=not self.background_start)
        if self.background_start:
            # Started at login: no window, just the tray icon (and the session, if so configured).
            self.keep_resident()
            GLib.timeout_add_seconds(20, self.unhide_without_tray)
            if self.auto_visible:
                GLib.timeout_add_seconds(4, self.auto_activate)
        if self.screenshot:
            GLib.timeout_add(900, self.take_screenshot)
        elif self.activate_radio:
            GLib.timeout_add(800, self.activate_once)
        GLib.timeout_add_seconds(2, self.tick)

    def on_open(self, _app, files, _count, _hint):
        """Files handed over by Files ("Invia con Linux AirDrop"), by a second launch or by drag and drop onto the icon."""
        paths = [f.get_path() for f in files if f.get_path()]
        self.activate()
        if paths:
            self.request_send(paths)

    def build_window(self, present=True):
        self.window = Adw.ApplicationWindow(application=self, title="Linux AirDrop", default_width=500, default_height=760)
        self.window.connect("close-request", self.on_close_request)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Registro", "app.show-log")
        menu.append("Apri file ricevuti", "app.open-folder")
        tail = Gio.Menu()
        tail.append("Informazioni su Linux AirDrop", "app.about")
        tail.append("Esci", "app.quit")
        menu.append_section(None, tail)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Menu principale"))
        toolbar.add_top_bar(header)
        self.banner = Adw.Banner(title="", revealed=False, use_markup=False, button_label="Dettagli")
        self.banner.connect("button-clicked", self.show_log)
        toolbar.add_top_bar(self.banner)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)

        # State and the one main action. (AdwStatusPage collapses inside a scrolled view, so it is laid out by hand.)
        header_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, halign=Gtk.Align.CENTER, margin_top=6)
        self.icon = Gtk.Image(icon_name=APP_ID, pixel_size=96, margin_bottom=6)
        self.icon.add_css_class("icon-dropshadow")
        self.title = Gtk.Label(label="Pronto quando vuoi", wrap=True, justify=Gtk.Justification.CENTER, css_classes=["title-1"])
        self.subtitle = Gtk.Label(label="Attiva la visibilità per iniziare.", wrap=True, justify=Gtk.Justification.CENTER)
        self.toggle = Gtk.Button(label="Rendi visibile", halign=Gtk.Align.CENTER, margin_top=10,
                                 css_classes=["pill", "suggested-action"])
        self.toggle.connect("clicked", self.toggle_session)
        status = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER, margin_top=2)
        self.spinner = Adw.Spinner(visible=False)
        self.status = Gtk.Label(label="MT7922 rilevata · collegamento Ethernet disponibile", wrap=True,
                                justify=Gtk.Justification.CENTER, css_classes=["dim-label"])
        status.append(self.spinner)
        status.append(self.status)
        for widget in (self.icon, self.title, self.subtitle, self.toggle, status):
            header_box.append(widget)
        content.append(header_box)

        # Transfers in progress sit right under the state, so they are seen without scrolling.
        self.transfer_list = TransferList(self.cancel_transfer)
        content.append(self.transfer_list)

        # What can be done with a running session.
        actions = Adw.PreferencesGroup()
        self.send_row = Adw.ActionRow(title="Invia file…", subtitle="Scegli i file, poi il dispositivo Apple", activatable=True,
                                      sensitive=False, use_markup=False)
        self.send_row.add_prefix(self.symbol("document-send-symbolic"))
        self.send_row.add_suffix(self.symbol("go-next-symbolic"))
        self.send_row.connect("activated", self.choose_files)
        folder_row = Adw.ActionRow(title="Apri file ricevuti", subtitle="Download/LinuxAirDrop", activatable=True, use_markup=False)
        folder_row.add_prefix(self.symbol("folder-symbolic"))
        folder_row.add_suffix(self.symbol("go-next-symbolic"))
        folder_row.connect("activated", self.open_folder)
        self.send_folder_row = Adw.ActionRow(title="Invia cartella…", subtitle="Come archivio ZIP o come cartella", activatable=True,
                                             sensitive=False, use_markup=False)
        self.send_folder_row.add_prefix(self.symbol("folder-symbolic"))
        self.send_folder_row.add_suffix(self.symbol("go-next-symbolic"))
        self.send_folder_row.connect("activated", self.choose_folders)
        actions.add(self.send_row)
        actions.add(self.send_folder_row)
        actions.add(folder_row)
        content.append(actions)

        # Preferences.
        options = Adw.PreferencesGroup(title="Visibilità")
        self.name = Adw.EntryRow(title="Nome nel menu AirDrop", text=self.display_name)
        self.go_row = Adw.SwitchRow(title="Wi-Fi Direct", subtitle="Consigliato · il canale deve essere consentito nella tua zona", use_markup=False)
        self.go_row.set_active(self.go_anchor)
        self.channel = Adw.ComboRow(title="Canale Wi-Fi", model=Gtk.StringList.new(["6 · 2,4 GHz · scoperta più rapida", "44 · 5 GHz"]))
        self.channel.set_selected(CHANNELS.index(self.radio_channel))
        self.go_row.connect("notify::active", self.go_toggled)
        self.channel.connect("notify::selected", self.channel_changed)
        self.channel_changed()
        self.ack_row = Adw.SwitchRow(title="Conferme radio",
                                     subtitle="Ritrasmette i pacchetti persi verso l’iPhone: invii più affidabili. "
                                              "Vale dalla prossima attivazione",
                                     use_markup=False)
        self.ack_row.set_active(self.radio_ack)
        self.ack_row.connect("notify::active", lambda row, _: (setattr(self, "radio_ack", row.get_active()), self.persist()))
        for row in (self.name, self.go_row, self.channel, self.ack_row):
            options.add(row)
        content.append(options)

        behaviour = Adw.PreferencesGroup(title="Applicazione")
        self.background_row = Adw.SwitchRow(title="Continua in background",
                                            subtitle="Chiudendo la finestra l'app resta attiva, con l'icona nella barra", use_markup=False)
        self.background_row.set_active(self.background)
        self.background_row.connect("notify::active", lambda row, _: (setattr(self, "background", row.get_active()), self.persist()))
        self.autostart_row = Adw.SwitchRow(title="Avvia al login", subtitle="Parte in background, con l'icona nella barra in alto",
                                           use_markup=False)
        self.autostart_row.set_active(autostart.is_enabled())
        self.autostart_row.connect("notify::active", self.autostart_toggled)
        self.auto_visible_row = Adw.SwitchRow(title="Rendi visibile all'avvio", use_markup=False)
        self.auto_visible_row.set_active(self.auto_visible)
        self.auto_visible_row.connect("notify::active", lambda row, _: (setattr(self, "auto_visible", row.get_active()), self.persist()))
        self.duration_row = Adw.ComboRow(title="Durata della visibilità", model=Gtk.StringList.new([label for _, label, _ in DURATIONS]))
        self.duration_row.set_selected([key for key, _, _ in DURATIONS].index(self.duration))
        self.duration_row.connect("notify::selected", self.duration_changed)
        self.repair_row = Adw.SwitchRow(title="Ripara da sola i problemi semplici",
                                        subtitle="Riavvia il ricevitore o il segnale Bluetooth se si fermano, al massimo 3 volte ogni 30 minuti",
                                        use_markup=False)
        self.repair_row.set_active(self.auto_repair)
        self.repair_row.connect("notify::active", lambda row, _: (setattr(self, "auto_repair", row.get_active()), self.persist()))
        self.helper_row = Adw.ActionRow(title="Attivazione senza password", use_markup=False)
        self.helper_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.helper_button.connect("clicked", self.helper_clicked)
        self.helper_row.add_suffix(self.helper_button)
        for row in (self.background_row, self.autostart_row, self.auto_visible_row, self.duration_row, self.repair_row, self.helper_row):
            behaviour.add(row)
        content.append(behaviour)

        # Phone side instructions.
        steps = Adw.PreferencesGroup(title="Sull'iPhone o sull'iPad")
        for number, text in (("1", "Attiva Wi-Fi e Bluetooth e tieni il dispositivo vicino"),
                             ("2", "Imposta AirDrop su «Tutti per 10 minuti»"),
                             ("3", "Apri Condividi → AirDrop e scegli questo PC")):
            row = Adw.ActionRow(title=text, use_markup=False)
            row.add_prefix(Gtk.Label(label=number, css_classes=["dim-label", "heading"], width_chars=2))
            steps.add(row)
        content.append(steps)

        self.note = caption("")
        content.append(self.note)

        clamp = Adw.Clamp(maximum_size=540, tightening_threshold=440, margin_top=6, margin_bottom=24,
                          margin_start=12, margin_end=12, child=content)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, vexpand=True)
        self.stack = Adw.ViewStack()
        self.stack.add_titled_with_icon(scroller, "home", "Principale", "go-home-symbolic")
        self.history_page = HistoryPage(self)
        self.stack.add_titled_with_icon(self.history_page, "history", "Cronologia", "document-open-recent-symbolic")
        self.devices_page = DevicesPage(self)
        self.stack.add_titled_with_icon(self.devices_page, "devices", "Dispositivi", "phone-symbolic")
        self.diag_page = DiagnosticsPage(self)
        self.stack.add_titled_with_icon(self.diag_page, "diagnostics", "Diagnostica", "utilities-system-monitor-symbolic")
        self.nexa_page = NexaAwdlPage(self)
        self.stack.add_titled_with_icon(self.nexa_page, "nexaawdl", "NexaAWDL", "network-wireless-symbolic")
        self.stack.connect("notify::visible-child-name", self.page_changed)
        self.toasts = Adw.ToastOverlay(child=self.stack)
        overlay = Gtk.Overlay(child=self.toasts)
        self.drop_zone = self.build_drop_zone()
        overlay.add_overlay(self.drop_zone)
        target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        target.connect("enter", self.drop_enter)
        target.connect("leave", self.drop_leave)
        target.connect("drop", self.drop_files)
        overlay.add_controller(target)
        toolbar.set_content(overlay)
        toolbar.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.stack, reveal=True))
        self.window.set_content(toolbar)
        self.initial_state()
        self.refresh_helper()
        if present:
            self.window.present()

    # ----------------------------------------------------- preferences and system helper
    def persist(self):
        name = self.name.get_text().strip()[:64] if self.window is not None else self.saved_name
        save_settings({"name": name, "channel": self.current_channel(), "wifi_direct": self.go_wanted,
                       "background": self.background, "auto_visible": self.auto_visible, "duration": self.duration,
                       "remember": self.remember, "auto_repair": self.auto_repair, "radio_ack": self.radio_ack,
                       "nexa_enabled": self.nexa_enabled, "nexa_level": self.nexa_level})

    def duration_changed(self, row, _):
        self.duration = DURATIONS[row.get_selected()][0]
        self.persist()

    def duration_seconds(self):
        return next(seconds for key, _, seconds in DURATIONS if key == self.duration)

    def duration_label(self):
        return next(label for key, label, _ in DURATIONS if key == self.duration)

    def autostart_toggled(self, row, _):
        try:
            autostart.set_enabled(row.get_active(), BASE / "start.sh")
        except OSError as exc:
            self.log("Avvio al login non modificabile: " + str(exc))
            row.set_active(autostart.is_enabled())

    def helper_ready(self):
        return self.helper == "ready"

    def refresh_helper(self):
        self.helper = system_helper.status()
        texts = {
            "ready": ("Attiva: Ubuntu non chiede la password per attivare AirDrop", "Rimuovi"),
            "missing": ("Non configurata: Ubuntu chiede la password a ogni attivazione", "Configura…"),
            "outdated": ("Da aggiornare: l'app è cambiata dopo la configurazione", "Aggiorna…"),
            "other-user": ("Configurata per un altro utente", "Configura…"),
            "unsafe": ("Non sicura: i file di sistema risultano modificati", "Reinstalla…"),
        }
        subtitle, label = texts[self.helper]
        self.helper_row.set_subtitle(subtitle)
        self.helper_button.set_label(label)
        self.auto_visible_row.set_sensitive(self.helper_ready())
        self.auto_visible_row.set_subtitle("Richiede l'attivazione senza password" if not self.helper_ready()
                                           else "Con la durata «Finché non la interrompo» resta sempre attivo")

    def helper_clicked(self, *_):
        removing = self.helper_ready()
        dialog = Adw.AlertDialog(
            heading="Rimuovere l'attivazione senza password?" if removing else "Configurare l'attivazione senza password?",
            body=("Ubuntu tornerà a chiedere la password a ogni attivazione." if removing else
                  "Ubuntu chiederà la password di amministratore una sola volta. Verrà installato in /usr/local/lib/linux-airdrop un piccolo "
                  "programma di proprietà di root, che nessun programma del tuo utente può modificare, e una regola che permette solo a "
                  f"{getpass.getuser()} di avviarlo senza password. Puoi rimuoverlo quando vuoi da qui."))
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("go", "Rimuovi" if removing else "Continua")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE if removing else Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(self.window, None, lambda d, result: d.choose_finish(result) == "go" and self.run_helper("uninstall" if removing else "install"))

    def run_helper(self, action):
        argv = ["pkexec", "/usr/bin/python3", "-I", "-B", str(BASE / "system_helper.py"), action]
        if action == "install":
            argv += ["--user", getpass.getuser()]
        self.helper_button.set_sensitive(False)
        self.set_status("In attesa dell’autenticazione Ubuntu…", busy=True)

        def work():
            try:
                done = subprocess.run(argv, capture_output=True, text=True, timeout=300)
                code, output = done.returncode, (done.stdout + done.stderr)
            except (OSError, subprocess.TimeoutExpired) as exc:
                code, output = 1, str(exc)
            GLib.idle_add(self.helper_done, action, code, output)
        threading.Thread(target=work, daemon=True).start()

    def helper_done(self, action, code, output):
        self.helper_button.set_sensitive(True)
        self.refresh_helper()
        self.set_status("")
        if code in (126, 127):
            self.set_status("Autenticazione annullata.")
        elif code == 0:
            self.toast("Attivazione senza password " + ("rimossa" if action == "uninstall" else "configurata"))
        else:
            self.set_status("Operazione non riuscita. Consulta il registro.", True)
            self.log(output.strip())
        return False

    # -------------------------------------------------------------------- tray icon
    def start_tray(self):
        self.tray = TrayIcon("Linux AirDrop", BASE / "icons", TRAY_ICONS, self.act_show, self.tray_toggle, self.act_quit,
                             on_availability=self.tray_changed)
        try:
            self.tray.start()
        except GLib.Error as exc:
            self.log("Icona nella barra non disponibile: " + str(exc))
            self.tray = None

    def tray_ok(self):
        return self.tray is not None and self.tray.available

    def tray_changed(self, available):
        # If the icon disappears while the window is hidden, nobody could bring the app back: show it.
        if not available and not self.shutting_down and self.window is not None and self.window_hidden() and not self.bridge:
            self.window.present()

    def update_tray(self):
        if self.tray is None:
            return
        if self.active:
            state = (True, False, "Visibile · pronto a ricevere")
        elif self.stopping:
            state = (False, True, "Ripristino del Wi-Fi…")
        elif self.bridge:
            state = (False, True, "Attivazione…")
        else:
            state = (False, False, "Non visibile")
        self.tray.set_state(*state)

    def tray_toggle(self):
        if self.stopping or self.window is None or not self.toggle.get_sensitive():
            return
        self.toggle_session()

    def keep_resident(self):
        # The tray icon (or a running session) is the app's presence: stay alive without a window.
        if not self.resident:
            self.hold()
            self.resident = True

    def unhide_without_tray(self):
        if self.window_hidden() and not self.bridge and not self.tray_ok():
            self.window.present()
        return False

    def auto_activate(self):
        if self.bridge or not self.iface or recovery_needed():
            return False
        self.refresh_helper()
        if not self.helper_ready():
            self.log("Avvio automatico della visibilità saltato: manca l'attivazione senza password.")
            return False
        self.toggle_session()
        return False

    @staticmethod
    def symbol(name):
        return Gtk.Image(icon_name=name, valign=Gtk.Align.CENTER)

    def initial_state(self):
        reason = None
        if recovery_needed():
            reason = "Il driver Wi-Fi si è bloccato. Riavvia Ubuntu prima di riprovare."
            self.show_blocked("Riavvio di Ubuntu necessario", "Il riavvio carica i driver originali di Ubuntu.", reason)
            note = "La prova è sospesa perché il driver Wi-Fi non risponde. Salva il tuo lavoro e riavvia Ubuntu, poi riapri Linux AirDrop. Nessun driver originale su disco è stato sostituito."
        elif not self.iface:
            self.show_blocked("Scheda Wi-Fi non trovata", "Serve una scheda MediaTek MT7921 o MT7922.", "Scheda MediaTek MT7921/MT7922 non trovata.")
            note = "Questa versione supporta soltanto le schede MediaTek MT7921/MT7922."
        elif not self.compat_plan.available:
            self.show_blocked("Collegamento non disponibile", self.compat_plan.summary, self.compat_plan.summary)
            note = " ".join((self.compat_plan.summary, *self.compat_plan.details))
        elif not self.radio_ready and not self.trial_ready:
            self.show_blocked("Collegamento non disponibile", "Il driver deve supportare active monitor per usare AirDrop.", "Driver Wi-Fi da correggere: active monitor non supportato.")
            note = "Il kernel attuale non offre active monitor e non c'è una correzione compatibile con questa versione."
        else:
            note = ("AirDrop usa la scheda Wi-Fi in esclusiva: il Wi-Fi del PC resta scollegato finché la visibilità è attiva, mentre Ethernet continua a funzionare. "
                    "Ubuntu chiederà la password all'attivazione.")
            if not self.radio_ready:
                note += " Il kernel disabilita active monitor, quindi l'app carica una correzione temporanea del driver: i driver originali su disco restano intatti e, se il collegamento si blocca, serve un riavvio."
        self.note.set_label(note)

    def show_blocked(self, title, sub, banner):
        self.title.set_label(title)
        self.subtitle.set_label(sub)
        self.toggle.set_sensitive(False)
        self.banner.set_title(banner)
        self.banner.set_revealed(True)
        self.set_status("", False)

    # ----------------------------------------------------------------- state
    def set_toggle(self, label, style):
        self.toggle.set_label(label)
        for name in ("suggested-action", "destructive-action"):
            self.toggle.remove_css_class(name)
        if style:
            self.toggle.add_css_class(style)
        self.update_tray()

    def set_status(self, text, error=False, busy=False):
        self.status.set_label(text)
        self.status.set_visible(bool(text))
        self.spinner.set_visible(busy)
        if error:
            self.status.remove_css_class("dim-label")
            self.status.add_css_class("error")
        else:
            self.status.remove_css_class("error")
            self.status.add_css_class("dim-label")

    def current_channel(self):
        return CHANNELS[self.channel.get_selected()]

    def channel_changed(self, *_):
        # The privileged preflight checks that this channel permits a GO in the current regulatory domain.
        self.go_row.set_sensitive(False)
        self.go_row.set_active(self.go_wanted)
        self.go_row.set_sensitive(True)

    def go_toggled(self, row, _):
        if row.get_sensitive():
            self.go_wanted = row.get_active()

    def lock_options(self, locked):
        self.name.set_sensitive(not locked)
        self.channel.set_sensitive(not locked)
        self.go_row.set_sensitive(not locked)
        self.ack_row.set_sensitive(not locked)

    def toast(self, text, button=None, action=None):
        item = Adw.Toast(title=text, use_markup=False)
        if button:
            item.set_button_label(button)
            item.set_action_name(action)
        self.toasts.add_toast(item)

    def notify(self, ident, title, body, buttons=(), priority=Gio.NotificationPriority.NORMAL):
        note = Gio.Notification.new(title)
        note.set_body(body)
        note.set_icon(Gio.ThemedIcon.new(APP_ID))
        note.set_priority(priority)
        note.set_default_action("app.show")
        for label, action in buttons:
            note.add_button(label, action)
        self.send_notification(ident, note)

    def window_hidden(self):
        return self.window is None or not self.window.get_visible()

    # -------------------------------------------------------------- actions
    def act_show(self, *_):
        self.activate()

    def act_stop(self, *_):
        if self.bridge:
            self.user_stop = True
            self.stop_session()

    def act_quit(self, *_):
        self.begin_quit()

    def begin_quit(self):
        """Stop the radio session first; the app exits once the Wi-Fi card is restored."""
        if self.bridge:
            self.closing = True
            if self.window is not None:
                self.window.set_visible(False)
            self.stop_session()
        else:
            self.quit()

    def on_close_request(self, *_):
        self.persist()
        if not self.bridge and not self.tray_ok():
            return False   # nothing to keep alive: close the window and exit
        if not self.background:
            self.begin_quit()
            return True
        # Closing the window keeps the app (tray icon, and the session if any) running in the background.
        self.keep_resident()
        self.window.set_visible(False)
        self.background_notice()
        return True

    def background_notice(self):
        if self.active:
            self.notify("session", "AirDrop resta attivo",
                        "Il PC è visibile e continua a ricevere in background. Riapri l'app o interrompi dalla notifica.",
                        buttons=(("Interrompi", "app.stop-session"),))

    def show_about(self, *_):
        about = Adw.AboutDialog(application_name="Linux AirDrop", application_icon=APP_ID, developer_name="foglio",
                                version=VERSION, license_type=Gtk.License.GPL_3_0,
                                comments="Scambia file con iPhone e iPad dal menu AirDrop, sul tuo PC Linux con scheda MediaTek MT7921/MT7922.",
                                website="https://github.com/Foglio24/linux-airdrop")
        about.add_credit_section("Basato su", ["OWL", "OpenDrop", "omdrop-owl"])
        about.present(self.window)

    def show_log(self, *_):
        if self.window is None:
            self.activate()
        dialog = Adw.Dialog(title="Registro", content_width=640, content_height=480)
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copia il registro")
        copy.connect("clicked", self.copy_log)
        header.pack_start(copy)
        toolbar.add_top_bar(header)
        view = Gtk.TextView(buffer=self.log_buffer, editable=False, cursor_visible=False, monospace=True,
                            wrap_mode=Gtk.WrapMode.WORD_CHAR, top_margin=8, bottom_margin=8, left_margin=12, right_margin=12)
        toolbar.set_content(Gtk.ScrolledWindow(child=view))
        dialog.set_child(toolbar)
        self.log_view = view
        dialog.connect("closed", lambda *_: setattr(self, "log_view", None))
        dialog.present(self.window)
        GLib.idle_add(self.scroll_log)

    def copy_log(self, button):
        button.get_clipboard().set("\n".join(self.log_lines))
        self.toast("Registro copiato")

    def scroll_log(self):
        if self.log_view is not None:
            end = self.log_buffer.get_end_iter()
            self.log_view.scroll_to_iter(end, 0.0, False, 0.0, 0.0)
        return False

    def activate_once(self):
        self.toggle_session()
        return False

    def take_screenshot(self):
        width, height = self.window.get_width(), self.window.get_height()
        paintable = Gtk.WidgetPaintable.new(self.window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, width, height)
        texture = self.window.get_native().get_renderer().render_texture(snapshot.to_node(), None)
        texture.save_to_png(str(self.screenshot))
        self.quit()
        return False

    def log(self, line):
        if str(line).startswith("Diagnosi AWDL") and "mDNS R _airdrop._tcp.local" in str(line):
            self.last_announce = time.monotonic()      # the bridge reports announcements it saw leave awdl0
        self.log_lines.append(str(line).rstrip()[:1600])
        self.log_lines = self.log_lines[-400:]
        self.log_buffer.set_text("\n".join(self.log_lines))
        self.log_dirty = True
        GLib.idle_add(self.scroll_log)
        return False

    # -------------------------------------------------------------- session
    def backend_env(self):
        env = os.environ.copy()
        env.update(AIRDROP_STATE_DIR=str(STATE / "protocol"), AIRDROP_DOWNLOAD_DIR=str(self.destination),
                   AIRDROP_CONFIRM=str(BASE / "confirm.py"), PYTHONUNBUFFERED="1",
                   OPENDROP_ANNOUNCE_INTERVAL=ANNOUNCE_SECONDS)
        return env

    def start_command(self, mode, extra=()):
        name = self.display_name
        return [str(BASE / "backend/opendrop"), mode, "-i", "awdl0", "-n", name, "-m", "MacBookPro", *extra]

    def toggle_session(self, *_):
        if recovery_needed():
            self.set_status("Riavvia Ubuntu per sbloccare il driver Wi-Fi prima di riprovare.", True)
            return
        if self.bridge:
            self.user_stop = True
            self.stop_session()
            return
        capabilities = replace(self.compat_capabilities, active_monitor=radio_capability(self.iface))
        self.compat_plan = radio_compat.select_plan(capabilities, self.compat_legacy)
        if not self.compat_plan.available:
            self.set_status(self.compat_plan.summary, True)
            return
        use_trial = self.compat_plan.mode == "legacy"
        if not use_trial and not self.go_row.get_active():
            self.set_status("Con il driver di sistema attiva Wi-Fi Direct.", True)
            return
        if use_trial and not driver_trial_available():
            self.set_status("Driver Wi-Fi senza active monitor: AirDrop non può essere attivato.", True)
            return
        if not (BASE / "backend/owl").is_file() or not (BASE / "backend/opendrop").is_file():
            self.set_status("Backend non pronto. Consulta README e registro.", True)
            return
        STATE.mkdir(parents=True, exist_ok=True)
        # One log per session, so comparisons survive the next run.
        for stale in sorted(STATE.glob("session-*.log"))[:-9]:
            stale.unlink(missing_ok=True)
        self.session_log = STATE / time.strftime("session-%Y%m%d-%H%M%S.log")
        self.log_lines = []
        self.last_error = None
        self.user_stop = False
        self.end_reason = None
        self.display_name = self.name.get_text().strip()[:64] or socket.gethostname()
        self.go_anchor = self.go_row.get_active()
        self.persist()
        self.refresh_helper()
        self.destination.mkdir(parents=True, exist_ok=True)
        self.known_files = {p.name for p in self.destination.iterdir() if not p.name.startswith(".")}
        self.set_toggle("Annulla attivazione", None)
        self.lock_options(True)
        self.title.set_label("Attivazione…")
        if self.helper_ready():
            self.subtitle.set_label("Preparazione della scheda Wi-Fi…")
            self.set_status("Attivazione senza password…", busy=True)
            # The root-owned entry point: nothing of the user's can have been swapped in.
            argv = ["pkexec", system_helper.helper_entry()]
        else:
            self.subtitle.set_label("Autorizza la gestione Wi-Fi nella finestra di Ubuntu.")
            self.set_status("In attesa dell’autenticazione Ubuntu…", busy=True)
            argv = ["pkexec", "/usr/bin/python3", "-I", "-B", str(BASE / "root_bridge.py")]
        limit = self.duration_seconds()
        # The app ends the session at its limit; the bridge keeps a margin as a backstop (0 = until stopped).
        argv += ["--iface", self.iface, "--channel", str(self.current_channel()), "--guardian", str(os.getpid()),
                 "--max-seconds", str(limit + 300 if limit else 0)]
        if use_trial:
            argv.append("--driver-trial")
        if self.go_anchor:
            argv.append("--go-anchor")
        if self.fixed_radio:
            argv.append("--fixed-radio")
            if self.radio_ack:
                argv.append("--fixed-ack")
        if self.data_rate:
            argv += ["--data-rate", self.data_rate]
        argv += self.nexa_arguments()
        self.bridge = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, text=True, bufsize=1)
        if not self.held:
            self.hold()
            self.held = True
        threading.Thread(target=self.read_bridge, args=(self.bridge,), daemon=True).start()

    def read_bridge(self, process):
        for line in process.stdout:
            try:
                event = json.loads(line)
            except ValueError:
                GLib.idle_add(self.log, line)
                continue
            GLib.idle_add(self.bridge_event, event)
        code = process.wait()
        GLib.idle_add(self.bridge_ended, process, code)

    def bridge_event(self, event):
        kind = event.get("event")
        if kind == "radio-capability" and event.get("name") == "skip_tx" and type(event.get("supported")) is bool:
            self.compat_capabilities = replace(self.compat_capabilities,
                                               skip_tx="supported" if event["supported"] else "unsupported")
            self.compat_plan = radio_compat.select_plan(self.compat_capabilities, self.compat_legacy)
            return False
        if kind == "ready":
            if self.closing:
                self.stop_session()
                return False
            self.active = True
            self.started_at = self.last_activity = time.monotonic()
            self.set_toggle("Interrompi visibilità", "destructive-action")
            self.title.set_label("Ricevitore AirDrop attivo")
            self.subtitle.set_label("Apri Condividi → AirDrop sul dispositivo Apple.")
            self.set_status("Ricezione attiva · conferma ogni file sul PC")
            self.send_row.set_sensitive(True)
            if self.window_hidden():
                self.background_notice()
            self.last_announce = None
            self.start_advertisement()
            self.spawn_receiver()
            self.send_folder_row.set_sensitive(True)
            self.pump_send()
        elif kind == "error":
            self.last_error = event.get("message", "Errore")
            self.set_status(self.last_error, True)
            self.log(self.last_error)
        elif kind == "recovery":
            self.last_error = event.get("message", "Riavvio di Ubuntu necessario.")
            require_recovery(self.last_error)
            self.stop_children()
            self.active = False
            self.toggle.set_sensitive(False)
            self.title.set_label("Collegamento sospeso")
            self.subtitle.set_label("Riavvia Ubuntu per sbloccare il driver Wi-Fi.")
            self.set_status(self.last_error, True)
            self.banner.set_title(self.last_error)
            self.banner.set_revealed(True)
            self.log(self.last_error)
            if self.window_hidden():
                self.notify("problem", "Riavvio di Ubuntu necessario", self.last_error,
                            priority=Gio.NotificationPriority.URGENT)
        elif kind == "status":
            self.log(event.get("message", ""))
        elif kind == "peer":
            self.set_status("Dispositivo Apple rilevato · apri Condividi → AirDrop")
        elif kind == "log":
            self.log(event.get("message", ""))
        return False

    def spawn_receiver(self):
        env = self.backend_env()
        env["AIRDROP_EVENTS"] = "1"       # the receiver also prints structured progress events (see transfer_events.py)
        self.receiver = subprocess.Popen(self.start_command("receive"), cwd=self.destination, env=env,
                                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            self.receiver_ifindex = socket.if_nametoindex("awdl0")
        except OSError:
            self.receiver_ifindex = None
        threading.Thread(target=self.read_receiver, args=(self.receiver,), daemon=True).start()

    def read_receiver(self, process):
        for line in process.stdout:
            event = tev.parse(line)
            if event is not None:
                GLib.idle_add(self.receive_event, event)
                continue
            GLib.idle_add(self.log, line)
            lowered = line.lower()
            if "received" in lowered or "upload" in lowered:
                self.last_activity = time.monotonic()
            if "richiesta /ask" in lowered:
                GLib.idle_add(self.incoming_request)
        code = process.wait()
        GLib.idle_add(self.receiver_ended, process, code)

    def incoming_request(self):
        # The confirmation window is a separate process; make sure the user notices it.
        if self.window_hidden():
            self.notify("incoming", "Richiesta di ricezione", "Un dispositivo Apple vuole inviarti dei file. Rispondi nella finestra di conferma.",
                        priority=Gio.NotificationPriority.HIGH)
        return False

    def receiver_ended(self, process, code):
        if self.receiver is process and self.active:
            if self.auto_repair and self.try_auto_repair("receiver-down", "restart_receiver", self.restart_receiver):
                return False
            self.last_error = "Il servizio di ricezione si è interrotto. Consulta il registro."
            self.set_status(self.last_error, True)
            self.stop_session()
        return False

    def bridge_ended(self, process, code):
        if self.bridge is not process:
            return False
        self.bridge = None
        self.stop_children()
        self.active = False
        self.stopping = False
        self.receiver_ifindex = None
        self.last_announce = None
        self.abandon_transfers("Visibilità interrotta durante il trasferimento")
        self.withdraw_notification("session")
        self.set_toggle("Rendi visibile", "suggested-action")
        # stop_session() disables the button while the card is restored; it is the end of that
        # restore, not a timer, that must bring it back (restoring takes several seconds).
        self.toggle.set_sensitive(not recovery_needed())
        self.spinner.set_visible(False)
        self.lock_options(False)
        self.send_row.set_sensitive(False)
        self.send_folder_row.set_sensitive(False)
        self.title.set_label("Pronto quando vuoi")
        self.subtitle.set_label("Attiva la visibilità per iniziare.")
        if code in (126, 127):
            self.set_status("Autenticazione annullata. Puoi riprovare.")
        elif self.last_error:
            self.set_status(self.last_error, True)
        elif code == 0:
            self.set_status("Visibilità interrotta · scheda Wi-Fi ripristinata")
        hidden = self.window_hidden()
        if self.held:
            self.release()
            self.held = False
        if self.restart_after and not self.closing:
            # A repair asked for a fresh session: the card has just been restored by the normal path.
            self.restart_after = False
            GLib.timeout_add_seconds(2, self.restart_now)
            return False
        if self.closing:
            self.quit()
        elif hidden:
            if not self.user_stop:
                self.notify("ended", "AirDrop non è più attivo",
                            self.last_error or self.end_reason or "La visibilità è terminata e il Wi-Fi è stato ripristinato.")
            if not (self.background and self.tray_ok()):
                # No tray icon to come back through: leave instead of lingering windowless.
                self.quit()
        return False

    def stop_children(self):
        for name in ("receiver", "finder", "sender", "ble"):
            process = getattr(self, name)
            if process and process.poll() is None:
                process.send_signal(signal.SIGINT if name in ("receiver", "finder") else signal.SIGTERM)
                # Do not leave a sender waiting forever after a lost radio.
                threading.Thread(target=self.reap, args=(process,), daemon=True).start()
            setattr(self, name, None)

    @staticmethod
    def reap(process):
        try:
            process.wait(timeout=4)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def stop_session(self):
        was_active = self.active
        self.active = False
        self.stopping = True
        self.stop_children()
        if self.bridge and self.bridge.poll() is None:
            try:
                self.bridge.stdin.write("stop\n")
                self.bridge.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
            if not was_active:
                try:
                    self.bridge.terminate()
                except (PermissionError, ProcessLookupError):
                    pass
        self.set_toggle("Ripristino Wi-Fi…", None)
        self.toggle.set_sensitive(False)
        self.set_status("Ripristino della scheda Wi-Fi…", busy=True)
        self.send_row.set_sensitive(False)
        self.send_folder_row.set_sensitive(False)
        GLib.timeout_add_seconds(1, self.unlock_toggle)

    def unlock_toggle(self):
        # bridge_ended restores the button; this only re-enables it if the bridge is already gone.
        if not self.bridge and not recovery_needed():
            self.toggle.set_sensitive(True)
            self.spinner.set_visible(False)
        return False

    # ----------------------------------------------------------------- opening things
    def open_folder(self, *_):
        self.destination.mkdir(parents=True, exist_ok=True)
        try:
            Gio.AppInfo.launch_default_for_uri(self.destination.as_uri(), None)
        except GLib.Error as exc:
            self.log("Impossibile aprire la cartella: " + str(exc))

    def open_path(self, path):
        """Open a received or sent file (or its folder) with the default application, as a double click would."""
        target = Path(path)
        if not target.exists():
            self.toast("Non c’è più: è stato spostato o eliminato")
            return
        try:
            Gio.AppInfo.launch_default_for_uri(target.as_uri(), None)
        except GLib.Error as exc:
            self.toast("Impossibile aprire: " + clean_text(exc.message, 80))

    def alert(self, heading, body):
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "Chiudi")
        dialog.set_default_response("ok")
        dialog.present(self.window)

    # ------------------------------------------------------------------ choosing and dropping files
    def choose_files(self, *_):
        if not self.active:
            return
        dialog = Gtk.FileDialog(title="File da inviare con AirDrop", accept_label="Scegli", modal=True)
        dialog.open_multiple(self.window, None, self.files_chosen)

    def choose_folders(self, *_):
        if not self.active:
            return
        dialog = Gtk.FileDialog(title="Cartelle da inviare con AirDrop", accept_label="Scegli", modal=True)
        dialog.select_multiple_folders(self.window, None, self.folders_chosen)

    def files_chosen(self, dialog, result):
        try:
            model = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        self.request_send([model.get_item(i).get_path() for i in range(model.get_n_items())])

    def folders_chosen(self, dialog, result):
        try:
            model = dialog.select_multiple_folders_finish(result)
        except GLib.Error:
            return
        self.request_send([model.get_item(i).get_path() for i in range(model.get_n_items())])

    def build_drop_zone(self):
        zone = Gtk.Box(css_classes=["drop-zone"], halign=Gtk.Align.FILL, valign=Gtk.Align.FILL, visible=False, can_target=False)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                        hexpand=True, vexpand=True, css_classes=["drop-card"])
        inner.append(Gtk.Image(icon_name="document-send-symbolic", pixel_size=64))
        inner.append(Gtk.Label(label="Rilascia qui i file da inviare", css_classes=["drop-zone-text"]))
        inner.append(Gtk.Label(label="File o cartelle, poi sceglierai l’iPhone o l’iPad", css_classes=["dim-label"]))
        zone.append(inner)
        return zone

    def drop_enter(self, target, x, y):
        self.drop_zone.set_visible(True)
        return Gdk.DragAction.COPY

    def drop_leave(self, target):
        self.drop_zone.set_visible(False)

    def drop_files(self, target, value, x, y):
        self.drop_zone.set_visible(False)
        files = value.get_files() if isinstance(value, Gdk.FileList) else [value]
        paths = [f.get_path() for f in files if f is not None and f.get_path()]
        if not paths:
            self.toast("Non riesco a leggere questi elementi: trascina file che si trovano su questo computer.")
            return False
        self.request_send(paths)
        return True

    # --------------------------------------------------------------------------- the send queue
    def request_send(self, paths):
        """Everything that wants to send something (chooser, drop, Files, command line) comes through here."""
        paths = [p for p in paths if p]
        if self.window is not None and self.window_hidden():
            self.window.present()
        if recovery_needed() or not self.iface:
            self.alert("Invio non disponibile", "Il Wi-Fi non è utilizzabile in questo momento. Apri «Diagnostica» per i dettagli.")
            return
        plan = outgoing.inspect_paths(paths)
        notes = [f"«{name}»: {why}" for name, why in plan["skipped"]]
        if plan["problems"]:
            self.alert("Non posso inviare", "\n".join(plan["problems"] + notes))
        elif plan["total"] > LARGE_SEND_BYTES and not getattr(self, "_large_ok", False):
            self.confirm_large_send(plan, paths)
        elif plan["folders"]:
            self.ask_folder_mode(plan, notes)
        else:
            self.queue_send(plan, "zip", notes)

    def confirm_large_send(self, plan, paths):
        slow, fast = (int(plan["total"] / speed / 60) + 1 for speed in SEND_SPEEDS)
        dialog = Adw.AlertDialog(
            heading="File molto grande",
            body=f"Stai per inviare {format_size(plan['total'])}. Verso l’iPhone questo collegamento va di solito "
                 f"da {format_size(SEND_SPEEDS[0])}/s a {format_size(SEND_SPEEDS[1])}/s, quindi servirebbero circa "
                 f"{fast}–{slow} minuti, con il PC e il telefono vicini e il telefono sbloccato. Più è lungo, più è "
                 "facile che si interrompa. Per file così grandi conviene un altro mezzo (cavo, cloud).")
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("send", "Invia comunque")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(self.window, None, lambda d, r: d.choose_finish(r) == "send" and self.large_send_confirmed(paths))

    def large_send_confirmed(self, paths):
        self._large_ok = True
        try:
            self.request_send(paths)
        finally:
            self._large_ok = False

    def ask_folder_mode(self, plan, notes):
        names = ", ".join(e["name"] for e in plan["entries"] if e["is_dir"])[:120]
        dialog = Adw.AlertDialog(
            heading="Come invio la cartella?",
            body=f"«{names}»\n\nCome archivio ZIP: arriva come un solo file .zip, è il modo collaudato.\n"
                 "Come cartella: arriva come cartella; questo modo non è ancora stato verificato con un iPhone reale.")
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("native", "Come cartella (sperimentale)")
        dialog.add_response("zip", "Come ZIP")
        dialog.set_response_appearance("zip", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("zip")
        dialog.set_close_response("cancel")
        dialog.choose(self.window, None, lambda d, r: self.folder_mode_chosen(d.choose_finish(r), plan, notes))

    def folder_mode_chosen(self, answer, plan, notes):
        if answer in ("zip", "native"):
            self.queue_send(plan, answer, notes)

    def queue_send(self, plan, mode, notes=()):
        job = SendJob(plan["entries"], mode)
        files = [{"name": e["name"], "kind": e["kind"], "size": e["size"], "path": e["path"]} for e in job.entries]
        transfer = Transfer("send", job.id, files=files, state="queued")
        transfer.total, transfer.job, job.transfer = plan["total"], job, transfer
        self.transfers[transfer.key] = transfer
        self.send_queue.append(job)
        self.sync_transfers()
        if notes:
            self.toast(f"{len(notes)} element{'o' if len(notes) == 1 else 'i'} non inviat{'o' if len(notes) == 1 else 'i'}: {notes[0]}")
        self.pump_send()

    def pump_send(self):
        """Start the next queued send once the session is up; offer to start it if it is not."""
        if self.current_send is not None or not self.send_queue:
            return
        if not self.active:
            if self.bridge is None and not self.asking_start and not self.stopping:
                self.confirm_start_for_send()
            return
        job = self.send_queue.pop(0)
        if job.cancelled:
            return self.pump_send()
        self.current_send = job
        self.begin_discover(job)

    def confirm_start_for_send(self):
        self.asking_start = True
        dialog = Adw.AlertDialog(heading="Rendere visibile il PC per inviare?",
                                 body="Per inviare serve attivare AirDrop: il Wi-Fi del PC viene usato in esclusiva finché è visibile "
                                      "(Ethernet continua a funzionare). Poi sceglierai il dispositivo Apple.")
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("go", "Attiva e invia")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("go")
        dialog.set_close_response("cancel")
        dialog.choose(self.window, None, lambda d, r: self.start_for_send_answered(d.choose_finish(r) == "go"))

    def start_for_send_answered(self, go):
        self.asking_start = False
        if go and self.bridge is None:
            self.toggle_session()
        if not go or self.bridge is None:
            # Declined, or the session could not even be started: nothing will ever run these jobs.
            for job in list(self.send_queue):
                self.send_queue.remove(job)
                job.cancelled = True
                self.finish_send(job, "cancelled", "Visibilità non attivata")

    def begin_discover(self, job):
        transfer = job.transfer
        transfer.state = "searching"
        self.sync_transfers()
        self.send_row.set_sensitive(False)
        self.send_folder_row.set_sensitive(False)
        self.set_status("Ricerca iPhone/iPad… Tieni AirDrop su Tutti e chiudi Condividi.", busy=True)
        threading.Thread(target=self.discover, args=(job,), daemon=True).start()

    def discover(self, job):
        if not self.active:
            GLib.idle_add(self.pick_receiver, [], job)
            return
        # An owned BLE advertisement wakes phones without toggling their controller.
        self.start_advertisement()
        report = STATE / "protocol/discover.last.json"
        report.unlink(missing_ok=True)
        self.finder = subprocess.Popen(self.start_command("find"), env=self.backend_env(),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 35
        peers = []

        def read_peers():
            return [p for p in json.loads(report.read_text()) if p.get("discoverable") and p.get("name") != self.display_name]
        while self.active and not job.cancelled and time.monotonic() < deadline:
            try:
                peers = read_peers()
            except (OSError, ValueError):
                peers = []
            if peers:
                time.sleep(3)
                try:
                    peers = read_peers()
                except (OSError, ValueError):
                    pass
                break
            time.sleep(0.5)
        if self.finder and self.finder.poll() is None:
            self.finder.send_signal(signal.SIGINT)
            self.reap(self.finder)
        GLib.idle_add(self.pick_receiver, peers, job)

    def start_advertisement(self):
        ble_path = BASE / "backend/blewake.py"
        if ble_path.exists() and (not self.ble or self.ble.poll() is not None):
            self.ble = subprocess.Popen(["/usr/bin/python3", str(ble_path)], env=self.backend_env(),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=self.read_ble, args=(self.ble,), daemon=True).start()

    def read_ble(self, process):
        for line in process.stdout:
            GLib.idle_add(self.log, "Bluetooth: " + line.rstrip())

    def pick_receiver(self, peers, job):
        self.spinner.set_visible(False)
        self.send_row.set_sensitive(self.active)
        self.send_folder_row.set_sensitive(self.active)
        if job.cancelled:
            self.finish_send(job, "cancelled")
            return False
        if not self.active:
            self.finish_send(job, "failed", "La visibilità si è interrotta")
            return False
        if not peers:
            self.set_status("Nessun destinatario trovato. Sblocca il telefono e verifica AirDrop su Tutti.", True)
            self.finish_send(job, "failed", "Nessun dispositivo Apple trovato")
            return False
        self.set_status("Ricezione attiva · conferma ogni file sul PC")
        self.window.present()
        dialog = Adw.AlertDialog(heading="Scegli il dispositivo Apple",
                                 body="Il telefono deve essere sbloccato, con AirDrop su Tutti.")
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["boxed-list"])
        for peer in peers:
            row = Adw.ActionRow(title=clean_text(peer["name"], 120), use_markup=False, activatable=True)
            row.peer_id, row.peer_name = peer["id"], clean_text(peer["name"], 120)
            listing.append(row)
        chosen = next((r for r in (listing.get_row_at_index(i) for i in range(len(peers))) if r.peer_name == self.last_peer),
                      listing.get_row_at_index(0))
        listing.select_row(chosen)
        dialog.set_extra_child(listing)
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("send", "Invia")
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")
        dialog.choose(self.window, None, self.receiver_chosen, listing, job)
        return False

    def receiver_chosen(self, dialog, result, listing, job):
        row = listing.get_selected_row()
        if dialog.choose_finish(result) != "send" or row is None or not self.active or job.cancelled:
            self.finish_send(job, "cancelled")
            return
        job.peer = {"id": row.peer_id, "name": row.peer_name}
        self.last_peer = row.peer_name
        transfer = job.transfer
        transfer.peer, transfer.state = row.peer_name, "preparing"
        transfer.identity, transfer.device_key = make_device_key(row.peer_name)
        transfer.db_id = self.store.start_transfer("send", row.peer_name, None, transfer.device_key, transfer.total,
                                                   files=transfer.files, folder=str(Path(job.entries[0]["path"]).parent))
        self.send_row.set_sensitive(False)
        self.send_folder_row.set_sensitive(False)
        self.set_status("Invio in corso… Accetta sul dispositivo Apple.", busy=True)
        self.sync_transfers()
        threading.Thread(target=self.prepare_send, args=(job,), daemon=True).start()

    def prepare_send(self, job):
        """Worker thread: zip folders if that is what was asked, then hand over to the main thread."""
        paths = []
        try:
            for entry in job.entries:
                if entry["is_dir"] and job.folder_mode == "zip":
                    if job.workdir is None:
                        job.workdir = outgoing.new_workdir(STATE / "outgoing")
                    paths.append(str(outgoing.make_zip(entry["path"], job.workdir, cancelled=lambda: job.cancelled)))
                else:
                    paths.append(entry["path"])
        except InterruptedError:
            GLib.idle_add(self.finish_send, job, "cancelled", None)
            return
        except (OSError, ValueError) as exc:
            GLib.idle_add(self.finish_send, job, "failed", "Non riesco a preparare i file: " + clean_text(str(exc), 100))
            return
        GLib.idle_add(self.spawn_sender, job, paths)

    def spawn_sender(self, job, paths):
        if job.cancelled or not self.active:
            self.finish_send(job, "cancelled" if job.cancelled else "failed", None if job.cancelled else "La visibilità si è interrotta")
            return False
        extra = ["-r", job.peer["id"]]
        for path in paths:
            extra.extend(["-f", path])
        env = self.backend_env()
        env["AIRDROP_EVENTS"] = "1"
        # Over AWDL a few seconds without an acknowledgement are normal; give up on a send only after
        # a minute and a half of no progress at all (the sender's default is 30 s).
        env["AIRDROP_STALL_TIMEOUT"] = str(SEND_STALL_SECONDS)
        job.process = self.sender = subprocess.Popen(self.start_command("send", extra), env=env, stdin=subprocess.DEVNULL,
                                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        job.transfer.state = "waiting"
        self.sync_transfers()
        threading.Thread(target=self.wait_send, args=(job,), daemon=True).start()
        return False

    def wait_send(self, job):
        for line in job.process.stdout:
            event = tev.parse(line)
            if event is not None:
                GLib.idle_add(self.send_event, job, event)
            else:
                GLib.idle_add(self.log, line)
        GLib.idle_add(self.send_finished, job, job.process.wait())

    def send_event(self, job, event):
        transfer, kind = job.transfer, event["event"]
        if transfer.state in FINAL_STATES:
            return False
        if kind == "send_accepted":
            transfer.state = "preparing"
            if transfer.device_key and transfer.identity:
                self.store.touch_device(transfer.device_key, transfer.identity, transfer.peer)
        elif kind == "send_declined":
            job.declined = True
        elif kind == "send_start":
            transfer.meter = Meter(total=event["total"])
            transfer.meter.update(0)
            transfer.state = "running"
        elif kind == "send_progress":
            transfer.state = "running"
            transfer.meter.update(event["done"] or 0, event["total"])
        elif kind == "send_waiting_confirmation":
            transfer.state = "confirming"
        self.sync_transfers()
        return False

    def send_finished(self, job, code):
        if job.abort_reason:
            self.finish_send(job, "failed", job.abort_reason)
        elif job.cancelled:
            self.finish_send(job, "cancelled")
        elif code == 0:
            self.finish_send(job, "completed")
        elif job.declined:
            self.finish_send(job, "declined", "Il dispositivo ha rifiutato i file")
        else:
            self.finish_send(job, "failed", "La connessione con il dispositivo è caduta o non è riuscita")
        return False

    def finish_send(self, job, status, error=None):
        transfer = job.transfer
        if transfer.state in FINAL_STATES:
            return False
        transfer.state, transfer.error, transfer.finished = {"completed": "done"}.get(status, status), error, time.monotonic()
        total = transfer.total
        if transfer.db_id is None:
            transfer.db_id = self.store.start_transfer("send", transfer.peer or "Nessun dispositivo scelto", None, transfer.device_key,
                                                       total, files=transfer.files)
        self.store.finish_transfer(transfer.db_id, status, error, done_bytes=total if status == "completed" else transfer.meter.done,
                                   total_bytes=total)
        if status == "completed" and transfer.device_key and transfer.identity:
            self.store.touch_device(transfer.device_key, transfer.identity, transfer.peer, None, direction="send")
        if job.workdir is not None:
            shutil.rmtree(job.workdir, ignore_errors=True)
        if self.current_send is job:
            self.current_send = None
        if self.sender is job.process:
            self.sender = None
        self.send_row.set_sensitive(self.active)
        self.send_folder_row.set_sensitive(self.active)
        self.spinner.set_visible(False)
        if status == "completed":
            self.set_status("Ricezione attiva · conferma ogni file sul PC")
            self.toast("Invio completato")
        elif status == "cancelled":
            self.set_status("Invio annullato" if self.active else "")
        else:
            self.set_status("Invio non riuscito o rifiutato" + (f": {error}" if error else "") + ". Consulta il registro.", True)
        self.sync_transfers()
        self.refresh_pages()
        GLib.timeout_add_seconds(12, self.drop_finished, transfer.key)
        self.pump_send()
        return False

    # -------------------------------------------------------------------------- receiving
    def receive_event(self, event):
        kind, ident = event["event"], event["id"] or "?"
        key = f"receive:{ident}"
        transfer = self.transfers.get(key)
        if kind != "receive_progress":      # progress is shown live, not logged
            self.log(f"Ricezione [{ident[:8]}] {kind}" + (f": {clean_text(event['sender'], 60)}" if event["sender"] else "")
                     + (f" · {clean_text(event['error'], 80)}" if event["error"] else ""))
        if kind == "ask":
            transfer = Transfer("receive", ident, peer=clean_text(event["sender"], 120), model=event["model"], files=event["files"],
                                items=event["items"], state="asking", device_key=event["device_key"], identity=event["identity"])
            transfer.total = event["total"]
            self.transfers[key] = transfer
        elif transfer is None or transfer.state in FINAL_STATES:
            return False
        elif kind == "ask_declined":
            self.finish_receive(transfer, "declined", "Rifiutato da te")
        elif kind == "ask_accepted":
            transfer.state = "waiting"
            files = [{"name": f["name"], "kind": f["kind"], "size": f["size"], "path": None} for f in transfer.files] or \
                    [{"name": i, "kind": "link", "size": None, "path": None} for i in transfer.items]
            transfer.db_id = self.store.start_transfer("receive", transfer.peer, transfer.model, transfer.device_key, transfer.total, files=files)
            if transfer.device_key and transfer.identity:
                self.store.touch_device(transfer.device_key, transfer.identity, transfer.peer, transfer.model)
        elif kind == "receive_start":
            transfer.state = "running"
            transfer.meter = Meter(total=event["total"])
            transfer.meter.update(0)
        elif kind == "receive_progress":
            transfer.state = "running"
            transfer.meter.update(event["done"] or 0, event["total"])
        elif kind == "receive_done":
            self.complete_receive(transfer, event)
        elif kind == "receive_failed":
            self.finish_receive(transfer, "cancelled" if event["cancelled"] else "failed", event["error"])
        self.sync_transfers()
        return False

    def complete_receive(self, transfer, event):
        folder = event["folder"]
        files = []
        for entry in event["files"]:
            path = str(Path(folder) / entry["name"]) if folder and entry["kind"] != "link" else None
            files.append({"name": entry["name"], "kind": entry["kind"], "size": entry["size"], "path": path})
        transfer.files = [{k: v for k, v in f.items() if k != "path"} for f in files]
        transfer.folder, transfer.state, transfer.finished = folder, "done", time.monotonic()
        sizes = [f["size"] for f in files if f["size"] is not None]
        transfer.result_bytes = sum(sizes) if sizes else None
        self.store.finish_transfer(transfer.db_id, "completed", done_bytes=transfer.result_bytes, total_bytes=transfer.result_bytes,
                                   files=files, folder=folder)
        if transfer.device_key and transfer.identity:
            self.store.touch_device(transfer.device_key, transfer.identity, transfer.peer, transfer.model, direction="receive")
        try:
            self.known_files = {p.name for p in self.destination.iterdir() if not p.name.startswith(".")}
        except OSError:
            pass
        self.last_activity = time.monotonic()
        names = transfer.names()
        links = all(f["kind"] == "link" for f in files) and bool(files)
        self.set_status(("Collegamento aperto: " if links else "Ricevuto: ") + names)
        if self.window_hidden():
            if links:
                self.notify("received", "Collegamento aperto", names)
            else:
                self.notify("received", "File ricevuto", names, buttons=(("Apri cartella", "app.open-folder"),))
        elif links:
            self.toast("Collegamento aperto nel browser")
        else:
            self.toast("File ricevuto", "Apri cartella", "app.open-folder")
        self.refresh_pages()
        GLib.timeout_add_seconds(12, self.drop_finished, transfer.key)

    def finish_receive(self, transfer, status, error=None):
        if transfer.state in FINAL_STATES:
            return
        transfer.state = {"completed": "done"}.get(status, status)
        transfer.error, transfer.finished = error, time.monotonic()
        files = [{"name": f["name"], "kind": f["kind"], "size": f.get("size"), "path": None} for f in transfer.files]
        if transfer.db_id is None:
            transfer.db_id = self.store.start_transfer("receive", transfer.peer or "Dispositivo sconosciuto", transfer.model,
                                                       transfer.device_key, transfer.total, files=files, status="declined")
        self.store.finish_transfer(transfer.db_id, status, error, done_bytes=transfer.meter.done)
        self.refresh_pages()
        GLib.timeout_add_seconds(12, self.drop_finished, transfer.key)

    # ------------------------------------------------------------------ cancelling and tidying
    def cancel_transfer(self, key):
        transfer = self.transfers.get(key)
        if transfer is None or not transfer.cancellable:
            return
        transfer.cancel_requested = True
        if transfer.direction == "receive":
            try:
                self.receiver.stdin.write(f"cancel {transfer.ident}\n")
                self.receiver.stdin.flush()
            except (AttributeError, OSError, ValueError):
                self.finish_receive(transfer, "cancelled", "Annullato dall’utente")
        else:
            job = transfer.job
            job.cancelled = True
            if job in self.send_queue:
                self.send_queue.remove(job)
                self.finish_send(job, "cancelled")
            elif job.process is not None and job.process.poll() is None:
                job.process.terminate()       # wait_send sees the process end and finishes the job as cancelled
            elif self.finder is not None and self.finder.poll() is None:
                self.finder.send_signal(signal.SIGINT)
        self.sync_transfers()

    def abandon_transfers(self, reason):
        """The session is over: nothing that was queued or running can finish."""
        for job in list(self.send_queue):
            self.send_queue.remove(job)
            job.cancelled = True
            self.finish_send(job, "cancelled", reason)
        job = self.current_send
        if job is not None:
            if job.process is None:
                job.cancelled = True
                self.finish_send(job, "failed", reason)
            else:
                job.abort_reason = reason      # its process was just stopped; wait_send reports the end
        for transfer in list(self.transfers.values()):
            if transfer.direction == "receive" and transfer.active:
                self.finish_receive(transfer, "failed", reason)
        self.sync_transfers()

    def sync_transfers(self):
        if self.transfer_list is not None:
            self.transfer_list.refresh(sorted(self.transfers.values(), key=lambda t: t.created))

    def drop_finished(self, key):
        transfer = self.transfers.get(key)
        if transfer is not None and transfer.state in FINAL_STATES:
            del self.transfers[key]
            self.sync_transfers()
        return False

    def refresh_pages(self):
        for page in (self.history_page, self.devices_page):
            if page is not None:
                page.refresh()

    def set_remember(self, enabled):
        self.remember = enabled
        self.store.set_enabled(enabled)
        self.persist()
        self.refresh_pages()

    def session_is_active(self):
        return bool(self.active or self.bridge)

    def nexa_arguments(self):
        """Arguments that enable the observer engine in the bridge; empty when it is switched off or unusable."""
        if not self.nexa_enabled:
            return []
        try:
            self.nexa_dir.mkdir(parents=True, exist_ok=True)
            self.nexa_dir.chmod(0o700)
            self.nexa_status_path.unlink(missing_ok=True)      # a leftover file would look like a live engine
        except OSError:
            return []
        return ["--nexaawdl-dir", str(self.nexa_dir), "--nexaawdl-code", str(BASE), "--nexaawdl-level", self.nexa_level]

    def page_changed(self, stack, _):
        name = stack.get_visible_child_name()
        if name == "diagnostics":
            self.diag_page.start_auto()
        else:
            self.diag_page.stop_auto()
        if name == "nexaawdl":
            self.nexa_page.start_auto()
        else:
            self.nexa_page.stop_auto()
        if name in ("history", "devices"):
            self.refresh_pages()

    # ------------------------------------------------------------- diagnostics and repair
    def diagnostic_facts(self):
        receiver = self.receiver
        return {"session_active": bool(self.active), "session_starting": bool(self.bridge) and not self.active and not self.stopping,
                "iface": self.iface, "channel": self.current_channel() if self.window is not None else self.radio_channel,
                "wifi_direct": bool(self.go_wanted), "radio_ready": bool(self.radio_ready), "trial_ready": bool(self.trial_ready),
                "receiver_pid": receiver.pid if receiver is not None and receiver.poll() is None and hasattr(receiver, "pid") else None,
                "receiver_ifindex": self.receiver_ifindex,
                "announce_age": None if self.last_announce is None else time.monotonic() - self.last_announce,
                "ble_running": bool(self.ble is not None and self.ble.poll() is None), "helper": self.helper,
                "radio_ack": bool(self.radio_ack), "radio_plan": self.compat_plan.as_dict(),
                "recovery_needed": recovery_needed()}

    def known_pids(self):
        pids = set()
        for name in ("bridge", "receiver", "ble", "sender", "finder"):
            process = getattr(self, name, None)
            if process is not None and getattr(process, "pid", None):
                pids.add(process.pid)
        return pids

    def start_repair(self, problem):
        allowed, why = self.attempts.allowed(problem.id)
        if not allowed:
            self.alert("Non riprovo ancora", why)
            return
        if not problem.risky:
            self.run_fix(problem)
            return
        what = {"restart_session": "L’app interromperà la visibilità, ripristinerà il Wi-Fi con la procedura normale e la riattiverà. "
                                   "Per qualche secondo il PC non sarà visibile; senza l’attivazione senza password Ubuntu chiederà la password.",
                "kill_strays": "Verranno chiusi i processi di AirDrop rimasti attivi senza una sessione. I file già ricevuti non cambiano."
                }.get(problem.fix, "Verrà applicata la riparazione.")
        dialog = Adw.AlertDialog(heading=problem.title, body=problem.why + "\n\n" + what)
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("go", problem.fix_label or "Ripara")
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(self.window, None, lambda d, r: d.choose_finish(r) == "go" and self.run_fix(problem))

    def run_fix(self, problem, automatic=False):
        ok, note = False, ""
        if problem.fix == "restart_receiver":
            ok, note = self.restart_receiver()
        elif problem.fix == "restart_ble":
            ok, note = self.restart_ble()
        elif problem.fix == "restart_session":
            ok, note = self.restart_session()
        elif problem.fix == "kill_strays":
            mine, _ = repair.scan_strays(diagnostics.System(), self.known_pids())
            ended = repair.kill_strays([pid for pid, _ in mine])
            ok, note = len(ended) == len(mine), f"chiusi {len(ended)} di {len(mine)} processi"
        self.attempts.record(problem.id, problem.fix, ("automatico: " if automatic else "") + ("riuscito: " if ok else "non riuscito: ") + note)
        self.toast(("Riparato: " if ok else "Non riuscito: ") + note)
        self.log("Riparazione: " + problem.title + " → " + note)
        if self.diag_page is not None:
            GLib.timeout_add_seconds(3, lambda: (self.diag_page.refresh(), False)[1])
        return ok

    def try_auto_repair(self, problem_id, fix, action):
        """A safe, bounded automatic fix. False when the policy says it was tried too often."""
        allowed, why = self.attempts.allowed(problem_id)
        if not allowed:
            self.attempts.log(f"[{problem_id}] {fix}: automatico NON eseguito ({why})")
            return False
        ok, note = action()
        self.attempts.record(problem_id, fix, "automatico: " + ("riuscito, " if ok else "non riuscito, ") + note)
        self.log("Riparazione automatica: " + note)
        return ok

    def restart_receiver(self):
        if not self.active:
            return False, "la visibilità non è attiva"
        old, self.receiver = self.receiver, None      # the old process's end is then ignored by receiver_ended
        for transfer in list(self.transfers.values()):
            if transfer.direction == "receive" and transfer.active:
                self.finish_receive(transfer, "failed", "Il ricevitore è stato riavviato")
        if old is not None and old.poll() is None:
            old.send_signal(signal.SIGINT)

        def work():
            if old is not None:
                self.reap(old)      # the port is free again only once the old receiver is gone
            GLib.idle_add(self.respawn_receiver)
        threading.Thread(target=work, daemon=True).start()
        return True, "ricevitore riavviato"

    def respawn_receiver(self):
        if self.active and self.receiver is None:
            self.spawn_receiver()
        return False

    def restart_ble(self):
        old, self.ble = self.ble, None
        if old is not None and old.poll() is None:
            old.terminate()

        def work():
            if old is not None:
                self.reap(old)
            GLib.idle_add(self.respawn_ble)
        threading.Thread(target=work, daemon=True).start()
        return True, "segnale Bluetooth riavviato"

    def respawn_ble(self):
        if self.active:
            self.start_advertisement()
        return False

    def restart_session(self):
        if not self.bridge:
            return False, "nessuna sessione da riattivare"
        self.restart_after = True
        self.user_stop = True
        self.stop_session()
        return True, "visibilità interrotta, verrà riattivata"

    def restart_now(self):
        if self.bridge is None and not recovery_needed():
            self.toggle_session()
        return False

    def auto_repair_step(self):
        """Every few seconds while visible: only faults that are certain and cheap to fix, never more than the policy allows."""
        try:
            current = socket.if_nametoindex("awdl0")
        except OSError:
            current = None
        if self.receiver is not None and self.receiver.poll() is None and self.receiver_ifindex and current and current != self.receiver_ifindex:
            self.try_auto_repair("mdns-interface", "restart_receiver", self.restart_receiver)
        elif self.ble is not None and self.ble.poll() is not None:
            self.try_auto_repair("ble-down", "restart_ble", self.restart_ble)

    def show_repair_log(self):
        try:
            text = "\n".join(self.attempts.log_file.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])
        except OSError:
            text = ""
        dialog = Adw.Dialog(title="Registro delle riparazioni", content_width=640, content_height=420)
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            top_margin=8, bottom_margin=8, left_margin=12, right_margin=12)
        view.get_buffer().set_text(text or "Nessuna riparazione finora.")
        toolbar.set_content(Gtk.ScrolledWindow(child=view))
        dialog.set_child(toolbar)
        dialog.present(self.window)

    # ----------------------------------------------------------------- tick
    def tick(self):
        if self.log_dirty:
            STATE.mkdir(parents=True, exist_ok=True)
            text = "\n".join(self.log_lines) + "\n"
            (STATE / "session.log").write_text(text, encoding="utf-8")
            if self.session_log:
                self.session_log.write_text(text, encoding="utf-8")
            self.log_dirty = False
        now = time.monotonic()
        for transfer in list(self.transfers.values()):
            # An approval lasts three minutes on the receiving side; after that the sender has given up.
            if transfer.direction == "receive" and transfer.state == "waiting" and now - transfer.created > 200:
                self.finish_receive(transfer, "failed", "Il mittente non ha inviato i file")
        if self.transfers:
            self.sync_transfers()       # speeds and times move even when no new byte count arrives
        if self.active:
            files = {p.name for p in self.destination.iterdir() if not p.name.startswith(".")}
            new = files - self.known_files
            if new:
                self.known_files = files
                self.last_activity = time.monotonic()
                names = ", ".join(sorted(new))[:180]
                self.set_status("Ricevuto: " + names)
                if self.window_hidden():
                    self.notify("received", "File ricevuto", names, buttons=(("Apri cartella", "app.open-folder"),))
                else:
                    self.toast("File ricevuto", "Apri cartella", "app.open-folder")
            limit = self.duration_seconds()
            if limit and self.started_at and time.monotonic() - self.started_at > limit:
                self.end_reason = f"La visibilità è terminata dopo {self.duration_label()}."
                self.stop_session()
            elif self.auto_repair and now - self.last_auto_check >= 10:
                self.last_auto_check = now
                self.auto_repair_step()
        return True

    def interrupted(self):
        self.begin_quit()
        return False


def watch_signals(app):
    for sig in (signal.SIGTERM, signal.SIGINT):
        if GLibUnix is not None:
            GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, sig, app.interrupted)
        else:
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, sig, app.interrupted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--background", action="store_true", help="parte senza finestra, solo con l'icona nella barra")
    parser.add_argument("--go-anchor", dest="go_anchor", action="store_true", default=None,
                        help="usa un gruppo Wi-Fi Direct temporaneo (predefinito, canali 6 o 44)")
    parser.add_argument("--no-go-anchor", dest="go_anchor", action="store_false",
                        help="solo interfacce monitor, senza Wi-Fi Direct")
    parser.add_argument("--fixed-ack", dest="fixed_ack", action="store_true", default=None,
                        help="conferme radio per i pacchetti inviati (predefinito: come nelle impostazioni)")
    parser.add_argument("--no-fixed-ack", dest="fixed_ack", action="store_false", help="nessuna conferma radio")
    parser.add_argument("--data-rate", default=os.environ.get("LINUX_AIRDROP_RATE"),
                        help="velocità radio dei dati verso il telefono: 6..54 Mbit/s, mcsN o mcsN-sgi (predefinito 12)")
    parser.add_argument("--fixed-radio", action="store_true", default=True)
    parser.add_argument("--channel-hopping", dest="fixed_radio", action="store_false")
    parser.add_argument("--channel", type=int, choices=CHANNELS, default=None)
    parser.add_argument("--diagnose", action="store_true", help="stampa il rapporto diagnostico ed esce")
    parser.add_argument("files", nargs="*", help="file o cartelle da inviare (è ciò che usa «Invia con Linux AirDrop»)")
    args = parser.parse_args()
    if recovery_needed() and not args.screenshot:
        message = "Il driver Wi-Fi è bloccato. Salva il tuo lavoro e riavvia Ubuntu, poi riapri Linux AirDrop. I driver originali su disco sono intatti."
        print(message, file=sys.stderr)
        # GTK image decoding also creates a network namespace on this Ubuntu.
        # Do not open a window against the known stalled kernel network lock.
        try:
            subprocess.Popen(["notify-send", "--app-name=Linux AirDrop",
                              "Riavvio di Ubuntu necessario", message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
        sys.exit(78)
    app = AirDrop(args.screenshot, args.activate, args.fixed_radio, args.channel, args.fixed_ack, args.go_anchor, args.background)
    app.data_rate = args.data_rate
    watch_signals(app)
    # Paths go to the running instance, if there is one: no second session is ever started.
    sys.exit(app.run([sys.argv[0], *(str(Path(f).expanduser().absolute()) for f in args.files)]))
