"""The "Cronologia" page: what was sent and received, with a way to open it and to wipe the list."""
import os
from pathlib import Path

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from fileinfo import format_size, format_when, kind_icon, kind_label, summarize_names

STATUS_TEXT = {"completed": "Completato", "failed": "Fallito", "cancelled": "Annullato",
               "declined": "Rifiutato", "in_progress": "In corso"}
STATUS_CSS = {"completed": "success", "failed": "error", "cancelled": "warning", "declined": "warning", "in_progress": "accent"}
FILTERS = (("Tutti", None), ("Ricevuti", "receive"), ("Inviati", "send"))


def row_texts(t):
    """(title, subtitle) of a stored transfer; pure so it can be tested."""
    names = [f["name"] for f in t["files"]]
    title = summarize_names(names) if names else "Nessun file"
    who = f"{'Ricevuto da' if t['direction'] == 'receive' else 'Inviato a'} {t['peer_name']}"
    total = t.get("total_bytes")
    if total is None and t["files"] and all(f["size"] is not None for f in t["files"]):
        total = sum(f["size"] for f in t["files"])
    parts = [who, format_size(total) if total is not None else None, format_when(t["started_at"])]
    return title, " · ".join(p for p in parts if p)


def paths_of(t):
    """(file to open, folder to show). Either may be None."""
    files = [f["path"] for f in t["files"] if f.get("path")]
    folder = t.get("folder")
    if not folder and files:
        folder = os.path.dirname(files[0])
    single = files[0] if len(t["files"]) == 1 and files else None
    return single, folder


class HistoryPage(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.expanded = set()
        self.filter = None
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        self.remember = Adw.SwitchRow(title="Ricorda i trasferimenti",
                                      subtitle="Salva su questo computer nomi e dimensioni dei file e i dispositivi usati. Mai il contenuto dei file.",
                                      use_markup=False)
        self.remember.set_active(app.store.enabled)
        self.remember.connect("notify::active", self.remember_toggled)
        settings = Adw.PreferencesGroup()
        settings.add(self.remember)
        content.append(settings)

        self.filter_menu = Gtk.DropDown.new_from_strings([label for label, _ in FILTERS])
        self.filter_menu.set_valign(Gtk.Align.CENTER)
        self.filter_menu.connect("notify::selected", self.filter_changed)
        self.clear_button = Gtk.Button(label="Cancella cronologia", valign=Gtk.Align.CENTER, css_classes=["destructive-action"])
        self.clear_button.connect("clicked", self.confirm_clear)
        bar = Gtk.Box(spacing=8)
        bar.append(Gtk.Label(label="Trasferimenti", xalign=0, hexpand=True, css_classes=["heading"]))
        bar.append(self.filter_menu)
        bar.append(self.clear_button)
        content.append(bar)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.empty = Adw.StatusPage(icon_name="document-open-recent-symbolic", title="Nessun trasferimento",
                                    description="I file inviati e ricevuti compariranno qui.")
        self.empty.add_css_class("compact")
        self.stack = Gtk.Stack(vhomogeneous=False)
        self.stack.add_named(self.empty, "empty")
        self.stack.add_named(self.list, "list")
        content.append(self.stack)
        self.note = Gtk.Label(wrap=True, xalign=0, css_classes=["caption", "dim-label"],
                              label="L’elenco resta solo su questo computer. Cancellarlo non elimina i file ricevuti.")
        content.append(self.note)

        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480, margin_top=12, margin_bottom=24,
                          margin_start=12, margin_end=12, child=content)
        self.append(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, vexpand=True))
        self.refresh()

    # ----------------------------------------------------------------- content
    def refresh(self):
        child = self.list.get_first_child()
        while child is not None:
            following = child.get_next_sibling()
            if getattr(child, "tid", None) is not None and child.get_expanded():
                self.expanded.add(child.tid)
            self.list.remove(child)
            child = following
        rows = self.app.store.transfers(self.filter)
        for transfer in rows:
            self.list.append(self.build_row(transfer))
        self.clear_button.set_sensitive(bool(self.app.store.count_transfers()))
        self.stack.set_visible_child_name("list" if rows else "empty")
        if not self.app.store.enabled and not rows:
            self.empty.set_description("La registrazione è disattivata: i nuovi trasferimenti non vengono salvati.")
        else:
            self.empty.set_description("I file inviati e ricevuti compariranno qui.")

    def build_row(self, t):
        title, subtitle = row_texts(t)
        row = Adw.ExpanderRow(title=title, subtitle=subtitle, use_markup=False, title_lines=1, subtitle_lines=2)
        row.tid = t["id"]
        row.set_expanded(t["id"] in self.expanded)
        row.add_prefix(Gtk.Image(icon_name="folder-download-symbolic" if t["direction"] == "receive" else "document-send-symbolic",
                                 css_classes=["dim-label"]))
        status = Gtk.Label(label=STATUS_TEXT.get(t["status"], t["status"]), valign=Gtk.Align.CENTER,
                           css_classes=["caption-heading", STATUS_CSS.get(t["status"], "dim-label")])
        row.add_suffix(status)
        single, folder = paths_of(t)
        if single:
            button = Gtk.Button(icon_name="text-x-generic-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Apri il file")
            button.connect("clicked", lambda *_: self.app.open_path(single))
            row.add_suffix(button)
        if folder:
            button = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Apri la cartella")
            button.connect("clicked", lambda *_: self.app.open_path(folder))
            row.add_suffix(button)
        for entry in t["files"]:
            detail = kind_label(entry["kind"]) + (f" · {format_size(entry['size'])}" if entry["size"] is not None else "")
            child = Adw.ActionRow(title=entry["name"], subtitle=detail, use_markup=False, title_lines=1)
            child.add_prefix(Gtk.Image(icon_name=kind_icon(entry["kind"])))
            if entry.get("path"):
                open_button = Gtk.Button(icon_name="text-x-generic-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                         tooltip_text="Apri il file")
                open_button.connect("clicked", lambda _b, p=entry["path"]: self.app.open_path(p))
                child.add_suffix(open_button)
            row.add_row(child)
        if t["status"] in ("failed", "cancelled", "declined") and t.get("error"):
            row.add_row(Adw.ActionRow(title="Motivo", subtitle=t["error"], use_markup=False, subtitle_lines=3))
        return row

    # ---------------------------------------------------------------- actions
    def filter_changed(self, dropdown, _):
        self.filter = FILTERS[dropdown.get_selected()][1]
        self.refresh()

    def remember_toggled(self, row, _):
        self.app.set_remember(row.get_active())

    def confirm_clear(self, *_):
        dialog = Adw.AlertDialog(heading="Cancellare la cronologia?",
                                 body="Verrà cancellato solo l’elenco dei trasferimenti. I file ricevuti restano dove sono "
                                      "e i dispositivi ricordati non vengono toccati.")
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("clear", "Cancella")
        dialog.set_response_appearance("clear", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(self.app.window, None, self.clear_answered)

    def clear_answered(self, dialog, result):
        if dialog.choose_finish(result) == "clear":
            self.app.store.clear_history()
            self.expanded.clear()
            self.refresh()
            self.app.toast("Cronologia cancellata")
