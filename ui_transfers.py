"""The "Trasferimenti" list of the main page: one row per send or receive, driven by real byte counts."""
import time

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import GLib, Gtk

from fileinfo import format_duration, format_size, format_speed, summarize_names
from progress import Meter

ACTIVE_STATES = ("asking", "waiting", "queued", "searching", "preparing", "running", "confirming")
FINAL_STATES = ("done", "failed", "cancelled", "declined")


class Transfer:
    """State of one send or receive. No widgets: the row and the history are built from it."""

    def __init__(self, direction, ident, peer=None, model=None, files=(), items=(), state="waiting",
                 device_key=None, identity=None):
        self.direction, self.ident, self.peer, self.model = direction, ident, peer, model
        self.files, self.items = list(files), list(items)
        self.state = state
        self.device_key, self.identity = device_key, identity
        self.meter = Meter()
        self.db_id = None
        self.error = None
        self.folder = None
        self.total = None            # bytes announced for the whole transfer, when known
        self.result_bytes = None     # bytes of the finished files
        self.created = time.monotonic()
        self.finished = None
        self.job = None              # SendJob for sends
        self.cancel_requested = False

    @property
    def key(self):
        return f"{self.direction}:{self.ident}"

    @property
    def active(self):
        return self.state in ACTIVE_STATES

    @property
    def cancellable(self):
        # While the user's own confirmation window is open there is nothing to cancel from here.
        return self.state in ("waiting", "queued", "searching", "preparing", "running", "confirming") and not self.cancel_requested

    def names(self):
        names = [f["name"] for f in self.files] or list(self.items)
        return summarize_names(names)


def present(t, now=None):
    """Text, fraction and style of a transfer's row. Pure, so it can be tested without a display."""
    receiving = t.direction == "receive"
    title = (f"Ricezione da {t.peer}" if t.peer else "Ricezione") if receiving else (f"Invio a {t.peer}" if t.peer else "Invio")
    snap = t.meter.snapshot(now)
    fraction, line, css = None, "", ""
    if t.state == "asking":
        line = "In attesa della tua conferma"
    elif t.state == "waiting":
        line = "Accettato · in attesa dei dati dal mittente" if receiving else "In attesa che l’altro dispositivo accetti"
    elif t.state == "queued":
        line = "In coda"
    elif t.state == "searching":
        line = "Ricerca del dispositivo Apple…"
    elif t.state == "preparing":
        line = "Preparazione dei file…"
    elif t.state == "confirming":
        line = "Invio in completamento · in attesa della conferma del dispositivo…"
    elif t.state == "running":
        parts = []
        if snap["total"]:
            parts.append(f"{format_size(snap['done'])} di {format_size(snap['total'])}")
            parts.append(f"{int(snap['percent'])}%")
            fraction = snap["percent"] / 100.0
        else:
            parts.append(f"{format_size(snap['done'])} {'ricevuti' if receiving else 'inviati'}")
        if snap["speed"] == 0.0:
            parts.append("fermo")           # nothing arrived for a few seconds
        elif snap["speed"] is not None:
            parts.append(format_speed(snap["speed"]))
        if snap["eta"] is not None:
            parts.append("circa " + format_duration(snap["eta"]) + " rimanenti")
        line = " · ".join(parts)
    elif t.state == "done":
        size = t.result_bytes if t.result_bytes is not None else snap["done"]
        line, css, fraction = "Completato" + (f" · {format_size(size)}" if size else ""), "success", 1.0
    elif t.state == "failed":
        line, css = "Non riuscito" + (f" · {t.error}" if t.error else ""), "error"
    elif t.state == "cancelled":
        line, css = "Annullato", "warning"
    elif t.state == "declined":
        line, css = "Rifiutato", "warning"
    return {"title": title, "names": t.names(), "line": line, "fraction": fraction,
            "indeterminate": t.state in ("running", "preparing", "searching", "confirming") and fraction is None,
            "css": css, "icon": "folder-download-symbolic" if receiving else "document-send-symbolic",
            "cancellable": t.cancellable}


class TransferRow(Gtk.ListBoxRow):
    def __init__(self, transfer, on_cancel):
        super().__init__(activatable=False, selectable=False)
        self.transfer = transfer
        outer = Gtk.Box(spacing=12, margin_top=10, margin_bottom=10, margin_start=12, margin_end=8)
        self.icon = Gtk.Image(valign=Gtk.Align.START, margin_top=4, css_classes=["dim-label"])
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3, hexpand=True)
        self.title = Gtk.Label(xalign=0, css_classes=["heading"], ellipsize=3, hexpand=True)
        self.names = Gtk.Label(xalign=0, css_classes=["dim-label"], ellipsize=3)
        self.bar = Gtk.ProgressBar(margin_top=4, margin_bottom=2)
        self.line = Gtk.Label(xalign=0, wrap=True, css_classes=["caption"])
        for widget in (self.title, self.names, self.bar, self.line):
            body.append(widget)
        self.cancel = Gtk.Button(icon_name="window-close-symbolic", valign=Gtk.Align.CENTER,
                                 css_classes=["flat", "circular"], tooltip_text="Annulla il trasferimento")
        self.cancel.connect("clicked", lambda *_: on_cancel(transfer.key))
        for widget in (self.icon, body, self.cancel):
            outer.append(widget)
        self.set_child(outer)
        self.pulse_id = None
        self.update()

    def update(self, now=None):
        view = present(self.transfer, now)
        self.icon.set_from_icon_name(view["icon"])
        self.title.set_label(view["title"])
        self.names.set_label(view["names"])
        self.names.set_visible(bool(view["names"]))
        self.line.set_label(view["line"])
        for name in ("success", "error", "warning", "dim-label"):
            self.line.remove_css_class(name)
        self.line.add_css_class(view["css"] or "dim-label")
        self.cancel.set_visible(view["cancellable"])
        self.bar.set_visible(self.transfer.state not in ("failed", "cancelled", "declined", "asking"))
        if view["fraction"] is not None:
            self.stop_pulse()
            self.bar.set_fraction(view["fraction"])
        elif view["indeterminate"]:
            self.start_pulse()
        else:
            self.stop_pulse()
            self.bar.set_fraction(0.0)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{view['title']}. {view['line']}"])

    def start_pulse(self):
        if self.pulse_id is None:
            self.pulse_id = GLib.timeout_add(150, self.pulse)

    def pulse(self):
        self.bar.pulse()
        return True

    def stop_pulse(self):
        if self.pulse_id is not None:
            GLib.source_remove(self.pulse_id)
            self.pulse_id = None


class TransferList(Gtk.Box):
    """Hidden while empty. `refresh()` is cheap: call it whenever a counter moves."""

    def __init__(self, on_cancel):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8, visible=False)
        self.on_cancel = on_cancel
        self.rows = {}
        self.append(Gtk.Label(label="Trasferimenti", xalign=0, css_classes=["heading"]))
        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.append(self.list)

    def refresh(self, transfers):
        keys = {t.key for t in transfers}
        for key in [k for k in self.rows if k not in keys]:
            row = self.rows.pop(key)
            row.stop_pulse()
            self.list.remove(row)
        for transfer in transfers:
            row = self.rows.get(transfer.key)
            if row is None:
                row = self.rows[transfer.key] = TransferRow(transfer, self.on_cancel)
                self.list.append(row)
            else:
                row.update()
        self.set_visible(bool(self.rows))
