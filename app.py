#!/usr/bin/python3
"""GTK front end for native AirDrop over OWL and hardened OpenDrop."""
import argparse
import json
import os
import platform
from pathlib import Path
import signal
import socket
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parent
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
    folder = BASE / "backend/driver-compat"
    try:
        manifest = json.loads((folder / "manifest.json").read_text())
        return manifest.get("kernel") == platform.release() and all((folder / name).is_file() for name in ("mac80211.ko", "mt792x-lib.ko", "mt7921e.ko"))
    except (OSError, ValueError):
        return False


if "--check" in sys.argv:
    print(json.dumps({"wifi": hardware(), "active_monitor": radio_capability(hardware()), "backend": (BASE / "backend/owl").is_file(),
                      "ios": "27: da verificare su dispositivo reale", "mode": "Apple AirDrop / AWDL",
                      "restart_required": recovery_needed()}, ensure_ascii=False))
    sys.exit(0)

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk
try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix
except (ValueError, ImportError):
    GLibUnix = None

import autostart
import getpass
import system_helper
from tray import TrayIcon

APP_ID = "it.foglio.LinuxAirDrop"
CHANNELS = (6, 36, 44)
DURATIONS = (("25m", "25 minuti", 1500), ("2h", "2 ore", 7200), ("always", "Finché non la interrompo", 0))
# How often the receiver re-announces itself. The phone, when idle, is on our channel for one slot in sixteen
# (about 65 ms per 1.048 s); at 5 s an announcement was heard only by luck, tens of seconds late. A period a
# little under that cycle makes successive announcements sweep through the phone's window instead of missing it.
ANNOUNCE_SECONDS = "0.5"
TRAY_ICONS = {"idle": "linux-airdrop-symbolic", "active": "linux-airdrop-active-symbolic"}
SETTINGS = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "linux-airdrop" / "settings.json"


def load_settings():
    """Small, validated preferences file. Anything unexpected falls back to the defaults."""
    defaults = {"name": "", "channel": 6, "wifi_direct": True, "background": True, "auto_visible": False, "duration": "25m"}
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
    for key in ("wifi_direct", "background", "auto_visible"):
        if isinstance(saved.get(key), bool):
            defaults[key] = saved[key]
    if saved.get("duration") in [key for key, _, _ in DURATIONS]:
        defaults["duration"] = saved["duration"]
    return defaults


def save_settings(values):
    try:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(values, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def caption(text):
    return Gtk.Label(label=text, wrap=True, xalign=0, css_classes=["caption", "dim-label"])


class AirDrop(Adw.Application):
    def __init__(self, screenshot=None, activate_radio=False, fixed_radio=True, radio_channel=None, fixed_ack=False, go_anchor=None, background_start=False):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.NON_UNIQUE if screenshot else Gio.ApplicationFlags.DEFAULT_FLAGS)
        saved = load_settings()
        self.screenshot = screenshot
        self.activate_radio = activate_radio
        self.fixed_radio = fixed_radio
        self.fixed_ack = fixed_ack
        self.go_anchor = saved["wifi_direct"] if go_anchor is None else go_anchor
        self.radio_channel = saved["channel"] if radio_channel is None else radio_channel
        self.background = saved["background"]
        self.auto_visible = saved["auto_visible"]
        self.duration = saved["duration"]
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
        self.radio_ready = radio_capability(self.iface)
        self.trial_ready = driver_trial_available()
        self.log_dirty = False
        self.session_log = None
        self.go_wanted = self.go_anchor
        download = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_DOWNLOAD) or str(Path.home() / "Downloads")
        self.destination = Path(download) / "LinuxAirDrop"
        self.connect("startup", self.on_startup)
        self.connect("shutdown", self.on_shutdown)
        self.connect("activate", self.activate)

    # ------------------------------------------------------------------ setup
    def on_startup(self, *_):
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(BASE / "icons"))
        Gtk.Window.set_default_icon_name(APP_ID)
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
        actions.add(self.send_row)
        actions.add(folder_row)
        content.append(actions)

        # Preferences.
        options = Adw.PreferencesGroup(title="Visibilità")
        self.name = Adw.EntryRow(title="Nome nel menu AirDrop", text=self.display_name)
        self.go_row = Adw.SwitchRow(title="Wi-Fi Direct", subtitle="Consigliato · funziona solo sul canale 6", use_markup=False)
        self.go_row.set_active(self.go_anchor)
        self.channel = Adw.ComboRow(title="Canale Wi-Fi", model=Gtk.StringList.new(["6 · consigliato", "36", "44"]))
        self.channel.set_selected(CHANNELS.index(self.radio_channel))
        self.go_row.connect("notify::active", self.go_toggled)
        self.channel.connect("notify::selected", self.channel_changed)
        self.channel_changed()
        for row in (self.name, self.go_row, self.channel):
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
        self.helper_row = Adw.ActionRow(title="Attivazione senza password", use_markup=False)
        self.helper_button = Gtk.Button(valign=Gtk.Align.CENTER)
        self.helper_button.connect("clicked", self.helper_clicked)
        self.helper_row.add_suffix(self.helper_button)
        for row in (self.background_row, self.autostart_row, self.auto_visible_row, self.duration_row, self.helper_row):
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
        self.toasts = Adw.ToastOverlay(child=scroller)
        toolbar.set_content(self.toasts)
        self.window.set_content(toolbar)
        self.initial_state()
        self.refresh_helper()
        if present:
            self.window.present()

    # ----------------------------------------------------- preferences and system helper
    def persist(self):
        name = self.name.get_text().strip()[:64] if self.window is not None else self.saved_name
        save_settings({"name": name, "channel": self.current_channel(), "wifi_direct": self.go_wanted,
                       "background": self.background, "auto_visible": self.auto_visible, "duration": self.duration})

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
        # Wi-Fi Direct exists only on channel 6; remember the user's choice for when they return to it.
        on_six = self.current_channel() == 6
        self.go_row.set_sensitive(False)
        self.go_row.set_active(self.go_wanted and on_six)
        self.go_row.set_sensitive(on_six)

    def go_toggled(self, row, _):
        if row.get_sensitive():
            self.go_wanted = row.get_active()

    def lock_options(self, locked):
        self.name.set_sensitive(not locked)
        self.channel.set_sensitive(not locked)
        self.go_row.set_sensitive(not locked and self.current_channel() == 6)

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
                                version="1.0.0", license_type=Gtk.License.GPL_3_0,
                                comments="Scambia file con iPhone e iPad dal menu AirDrop, sul tuo PC Linux con scheda MediaTek MT7921/MT7922.",
                                website="https://github.com/t4t5/omdrop-owl")
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
        use_trial = not radio_capability(self.iface)
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
        self.go_anchor = self.go_row.get_active() and self.current_channel() == 6
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
            if self.fixed_ack:
                argv.append("--fixed-ack")
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
            self.start_advertisement()
            self.receiver = subprocess.Popen(self.start_command("receive"), cwd=self.destination,
                                             env=self.backend_env(), stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=self.read_receiver, args=(self.receiver,), daemon=True).start()
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

    def read_receiver(self, process):
        for line in process.stdout:
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
        self.withdraw_notification("session")
        self.set_toggle("Rendi visibile", "suggested-action")
        # stop_session() disables the button while the card is restored; it is the end of that
        # restore, not a timer, that must bring it back (restoring takes several seconds).
        self.toggle.set_sensitive(not recovery_needed())
        self.spinner.set_visible(False)
        self.lock_options(False)
        self.send_row.set_sensitive(False)
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
        GLib.timeout_add_seconds(1, self.unlock_toggle)

    def unlock_toggle(self):
        # bridge_ended restores the button; this only re-enables it if the bridge is already gone.
        if not self.bridge and not recovery_needed():
            self.toggle.set_sensitive(True)
            self.spinner.set_visible(False)
        return False

    # ----------------------------------------------------------------- send
    def open_folder(self, *_):
        self.destination.mkdir(parents=True, exist_ok=True)
        try:
            Gio.AppInfo.launch_default_for_uri(self.destination.as_uri(), None)
        except GLib.Error as exc:
            self.log("Impossibile aprire la cartella: " + str(exc))

    def choose_files(self, *_):
        if not self.active:
            return
        dialog = Gtk.FileDialog(title="File da inviare con AirDrop", accept_label="Scegli", modal=True)
        dialog.open_multiple(self.window, None, self.files_chosen)

    def files_chosen(self, dialog, result):
        try:
            model = dialog.open_multiple_finish(result)
        except GLib.Error:
            return
        files = [model.get_item(i).get_path() for i in range(model.get_n_items())]
        files = [path for path in files if path]
        if files and self.active:
            self.send_row.set_sensitive(False)
            self.set_status("Ricerca iPhone/iPad… Tieni AirDrop su Tutti e chiudi Condividi.", busy=True)
            threading.Thread(target=self.discover, args=(files,), daemon=True).start()

    def discover(self, files):
        if not self.active:
            return
        # An owned BLE advertisement wakes phones without toggling their controller.
        self.start_advertisement()
        report = STATE / "protocol/discover.last.json"
        report.unlink(missing_ok=True)
        self.finder = subprocess.Popen(self.start_command("find"), env=self.backend_env(),
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 35
        peers = []
        while self.active and time.monotonic() < deadline:
            try:
                peers = [p for p in json.loads(report.read_text()) if p.get("discoverable") and p.get("name") != self.display_name]
            except (OSError, ValueError):
                peers = []
            if peers:
                time.sleep(3)
                try:
                    peers = [p for p in json.loads(report.read_text()) if p.get("discoverable") and p.get("name") != self.display_name]
                except (OSError, ValueError):
                    pass
                break
            time.sleep(0.5)
        if self.finder and self.finder.poll() is None:
            self.finder.send_signal(signal.SIGINT)
            self.reap(self.finder)
        GLib.idle_add(self.pick_receiver, peers, files)

    def start_advertisement(self):
        ble_path = BASE / "backend/blewake.py"
        if ble_path.exists() and (not self.ble or self.ble.poll() is not None):
            self.ble = subprocess.Popen(["/usr/bin/python3", str(ble_path)], env=self.backend_env(),
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            threading.Thread(target=self.read_ble, args=(self.ble,), daemon=True).start()

    def read_ble(self, process):
        for line in process.stdout:
            GLib.idle_add(self.log, "Bluetooth: " + line.rstrip())

    def pick_receiver(self, peers, files):
        self.send_row.set_sensitive(self.active)
        self.spinner.set_visible(False)
        if not self.active:
            return False
        if not peers:
            self.set_status("Nessun destinatario trovato. Sblocca il telefono e verifica AirDrop su Tutti.", True)
            return False
        self.set_status("Ricezione attiva · conferma ogni file sul PC")
        self.window.present()
        dialog = Adw.AlertDialog(heading="Scegli il dispositivo Apple",
                                 body="Il telefono deve essere sbloccato, con AirDrop su Tutti.")
        listing = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE, css_classes=["boxed-list"])
        for peer in peers:
            row = Adw.ActionRow(title=str(peer["name"]), use_markup=False, activatable=True)
            row.peer_id = peer["id"]
            listing.append(row)
        listing.select_row(listing.get_row_at_index(0))
        dialog.set_extra_child(listing)
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("send", "Invia")
        dialog.set_response_appearance("send", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("send")
        dialog.set_close_response("cancel")
        dialog.choose(self.window, None, self.receiver_chosen, listing, files)
        return False

    def receiver_chosen(self, dialog, result, listing, files):
        if dialog.choose_finish(result) != "send":
            return
        row = listing.get_selected_row()
        if row is None or not self.active:
            return
        extra = ["-r", row.peer_id]
        for file in files:
            extra.extend(["-f", file])
        self.sender = subprocess.Popen(self.start_command("send", extra), env=self.backend_env(),
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.send_row.set_sensitive(False)
        self.set_status("Invio in corso… Accetta sul dispositivo Apple.", busy=True)
        threading.Thread(target=self.wait_send, args=(self.sender,), daemon=True).start()

    def wait_send(self, process):
        for line in process.stdout:
            GLib.idle_add(self.log, line)
        rc = process.wait()
        GLib.idle_add(self.sent, rc)

    def sent(self, rc):
        self.send_row.set_sensitive(self.active)
        self.spinner.set_visible(False)
        if rc == 0:
            self.set_status("Ricezione attiva · conferma ogni file sul PC")
            self.toast("Invio completato")
        else:
            self.set_status("Invio non riuscito o rifiutato. Consulta il registro.", True)
        return False

    # ----------------------------------------------------------------- tick
    def tick(self):
        if self.log_dirty:
            STATE.mkdir(parents=True, exist_ok=True)
            text = "\n".join(self.log_lines) + "\n"
            (STATE / "session.log").write_text(text, encoding="utf-8")
            if self.session_log:
                self.session_log.write_text(text, encoding="utf-8")
            self.log_dirty = False
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
                        help="usa un gruppo Wi-Fi Direct temporaneo (predefinito, solo canale 6)")
    parser.add_argument("--no-go-anchor", dest="go_anchor", action="store_false",
                        help="solo interfacce monitor, senza Wi-Fi Direct")
    parser.add_argument("--fixed-ack", action="store_true")
    parser.add_argument("--fixed-radio", action="store_true", default=True)
    parser.add_argument("--channel-hopping", dest="fixed_radio", action="store_false")
    parser.add_argument("--channel", type=int, choices=CHANNELS, default=None)
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
    watch_signals(app)
    sys.exit(app.run([sys.argv[0]]))
