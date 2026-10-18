"""The "NexaAWDL Engine" page: state, wireless, peers, diagnostics and settings of the AWDL engine."""
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from nexaawdl import view

ICONS = {"ok": "emblem-ok-symbolic", "warn": "dialog-warning-symbolic", "error": "dialog-error-symbolic",
         "idle": "media-playback-pause-symbolic"}
CSS = {"ok": "success", "warn": "warning", "error": "error", "idle": "dim-label"}
REFRESH_SECONDS = 2
LEVELS = (("error", "Solo errori"), ("warning", "Avvisi"), ("info", "Informazioni"), ("debug", "Dettagliato"))


class NexaAwdlPage(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.timer = None
        self.hardware = None
        self.hw_busy = False
        self.status = None
        self.peer_rows, self.rows = [], {}
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        top = Gtk.Box(spacing=14, margin_top=6)
        self.icon = Gtk.Image(icon_name=ICONS["idle"], pixel_size=40, valign=Gtk.Align.CENTER)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(label="Motore non attivo", xalign=0, css_classes=["title-2"])
        self.subtitle = Gtk.Label(label="", xalign=0, wrap=True, css_classes=["dim-label"])
        texts.append(self.title)
        texts.append(self.subtitle)
        top.append(self.icon)
        top.append(texts)
        content.append(top)

        self.general_group = Adw.PreferencesGroup(title="Stato generale")
        self.wireless_group = Adw.PreferencesGroup(title="Wireless")
        self.peers_group = Adw.PreferencesGroup(title="Dispositivi", description="Dispositivi AWDL uditi e disponibilità del servizio AirDrop. "
                                                "Sono due cose diverse: un dispositivo può sincronizzarsi senza offrire AirDrop.")
        self.diag_group = Adw.PreferencesGroup(title="Diagnostica")
        for group in (self.general_group, self.wireless_group, self.peers_group, self.diag_group):
            content.append(group)

        self.copy_button = Gtk.Button(child=Adw.ButtonContent(label="Copia diagnostica", icon_name="edit-copy-symbolic"),
                                      halign=Gtk.Align.START)
        self.copy_button.connect("clicked", self.copy_diagnostics)
        content.append(self.copy_button)

        settings = Adw.PreferencesGroup(title="Impostazioni",
                                        description="Il motore osserva l'aria AWDL senza trasmettere. OWL continua a gestire invio e ricezione: "
                                                    "se il motore si ferma, AirDrop non cambia.")
        self.enabled_row = Adw.SwitchRow(title="Attivazione automatica", subtitle="Si avvia con la visibilità. Vale dalla prossima attivazione.")
        self.enabled_row.set_active(app.nexa_enabled)
        self.enabled_row.connect("notify::active", self.enabled_changed)
        self.level_row = Adw.ComboRow(title="Livello del registro", model=Gtk.StringList.new([name for _, name in LEVELS]))
        self.level_row.set_selected([key for key, _ in LEVELS].index(app.nexa_level))
        self.level_row.connect("notify::selected", self.level_changed)
        mode_row = Adw.ActionRow(title="Modalità di funzionamento", subtitle="Osservazione (sola ricezione). La trasmissione AWDL propria non è attiva: "
                                 "non è stata verificata su questo hardware.", use_markup=False, subtitle_lines=3)
        fallback_row = Adw.ActionRow(title="Fallback", subtitle="Sempre attivo: OWL e il sistema attuale non dipendono dal motore.", use_markup=False)
        reset_row = Adw.ActionRow(title="Ripristina impostazioni", subtitle="Attivazione automatica e livello del registro ai valori predefiniti", use_markup=False)
        reset = Gtk.Button(label="Ripristina", valign=Gtk.Align.CENTER)
        reset.connect("clicked", self.reset_settings)
        reset_row.add_suffix(reset)
        for row in (self.enabled_row, self.level_row, mode_row, fallback_row, reset_row):
            settings.add(row)
        content.append(settings)

        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480, margin_top=12, margin_bottom=24,
                          margin_start=12, margin_end=12, child=content)
        self.append(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, vexpand=True))
        self.general_rows, self.wireless_rows, self.peer_widgets, self.diag_rows = [], [], [], []

    # ------------------------------------------------------------------------------ refreshing
    def start_auto(self):
        self.load_hardware()
        self.refresh()
        if self.timer is None:
            self.timer = GLib.timeout_add_seconds(REFRESH_SECONDS, self.tick)

    def stop_auto(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None

    def tick(self):
        self.refresh()
        return True

    def load_hardware(self):
        """The hardware report comes from read-only tools, so it is gathered once in a worker thread."""
        if self.hardware is not None or self.hw_busy:
            return
        self.hw_busy = True

        def work():
            try:
                from nexaawdl import hwprobe
                report = hwprobe.probe(self.app.iface).as_dict()
            except Exception as exc:            # never take the page down
                report = {"verdict": "unknown", "limitations": [{"kind": "software", "text": f"controllo non riuscito: {exc}"}]}
            GLib.idle_add(self.set_hardware, report)
        threading.Thread(target=work, daemon=True).start()

    def set_hardware(self, report):
        self.hardware, self.hw_busy = report, False
        self.refresh()
        return False

    def refresh(self):
        self.status = view.read_status(self.app.nexa_status_path)
        severity, title, subtitle = view.general(self.status, self.app.session_is_active())
        self.icon.set_from_icon_name(ICONS[severity])
        for name in CSS.values():
            self.icon.remove_css_class(name)
        self.icon.add_css_class(CSS[severity])
        self.title.set_label(title)
        self.subtitle.set_label(subtitle)
        self.fill(self.general_group, self.general_rows, self.general_items())
        self.fill(self.wireless_group, self.wireless_rows, view.wireless_rows(self.status, self.hardware))
        self.fill(self.diag_group, self.diag_rows, self.diag_items())
        self.fill_peers()

    def general_items(self):
        status, hw = self.status, self.hardware
        verdict = view.hardware_summary(hw)
        items = [("Compatibilità hardware", f"{verdict[1]}. {verdict[2]}")]
        if hw:
            items.insert(0, ("Chipset e driver", f"{hw.get('chipset') or 'sconosciuto'} · {hw.get('pci_id') or '?'} · driver {hw.get('driver') or '?'}"))
            items.append(("Firmware e kernel", f"{hw.get('firmware') or 'sconosciuto'} · kernel {hw.get('kernel')}"))
        items.append(("Interfaccia usata", (status or {}).get("interface") or "—"))
        items += view.sync_rows(status)
        return items

    def diag_items(self):
        status = self.status
        if not status:
            return [("Eventi", "Nessun evento: il motore non è in esecuzione.")]
        stats = status.get("stats") or {}
        items = [("Frame ricevuti", f"{stats.get('frames', 0)} · PSF {stats.get('psf', 0)} · MIF {stats.get('mif', 0)} · dati {stats.get('data', 0)}"),
                 ("Frame non validi", str(stats.get("malformed", 0))), ("Frame con FCS errato", str(stats.get("bad_fcs", 0)))]
        traffic = status.get("traffic") or {}
        if traffic:
            items.append(("Traffico IPv6", ", ".join(f"{k} {v}" for k, v in sorted(traffic.items()))))
        for error in status.get("errors", [])[-3:]:
            items.append((f"Errore · {error.get('kind')}", str(error.get("message"))))
        events = [e for e in status.get("events", []) if e.get("kind") not in ("metrics",)][-5:]
        for event in reversed(events):
            extra = {k: v for k, v in event.items() if k not in ("t", "level", "kind")}
            items.append((f"Evento · {event.get('kind')}", ", ".join(f"{k}: {v}" for k, v in extra.items()) or "—"))
        return items

    def fill(self, group, rows, items):
        """Keep one row per item, updating text in place so the list does not flicker."""
        while len(rows) < len(items):
            row = Adw.ActionRow(use_markup=False, subtitle_lines=3, css_classes=["property"])
            rows.append(row)
            group.add(row)
        while len(rows) > len(items):
            group.remove(rows.pop())
        for row, (title, subtitle) in zip(rows, items):
            row.set_title(title)
            row.set_subtitle(subtitle)

    def fill_peers(self):
        peers = view.peer_rows(self.status)
        items = [(title, sub) for title, sub, _addr in peers] or [("Nessun dispositivo", "Nessun dispositivo AWDL udito finora.")]
        self.fill(self.peers_group, self.peer_widgets, items)

    # -------------------------------------------------------------------------------- settings
    def enabled_changed(self, row, _):
        self.app.nexa_enabled = row.get_active()
        self.app.persist()

    def level_changed(self, row, _):
        self.app.nexa_level = LEVELS[row.get_selected()][0]
        self.app.persist()

    def reset_settings(self, _button):
        self.enabled_row.set_active(True)
        self.level_row.set_selected([key for key, _ in LEVELS].index("info"))
        self.app.toast("Impostazioni del motore ripristinate")

    def copy_diagnostics(self, button):
        button.get_clipboard().set(view.diagnostics_text(self.status))
        self.app.toast("Diagnostica copiata")
