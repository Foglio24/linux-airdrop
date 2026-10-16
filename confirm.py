#!/usr/bin/python3
"""A real user confirmation is required for every incoming transfer.

Usage: confirm.py SENDER DETAIL [--info FILE]

Exit status 0 means accepted; anything else (decline, timeout, closing the window) refuses.
`--info` names a JSON file written by the receiver with what the request really contains before any
file is sent (file names and types, sizes when the sender gave them, a thumbnail when it sent one).
Nothing in it is a file's content, and nothing here opens or runs anything that was received.
"""
import json
import os
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk
from fileinfo import clean_text, format_size, kind_icon, kind_label
import preview

APP_ID = "it.foglio.LinuxAirDrop.Confirm"
MAX_ROWS = 6


def load_info(path):
    """The prompt description, re-validated; None if missing or malformed (the plain prompt is shown then)."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = json.loads(handle.read(2_000_000))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    files = []
    for entry in raw.get("files", [])[:64] if isinstance(raw.get("files"), list) else []:
        if isinstance(entry, dict):
            size = entry.get("size")
            files.append({"name": clean_text(entry.get("name"), 255) or "?", "kind": str(entry.get("kind") or "other")[:16],
                          "size": size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None})
    count = raw.get("file_count") if isinstance(raw.get("file_count"), int) else len(files)
    total = raw.get("total_size")
    info = {"sender": clean_text(raw.get("sender"), 120), "model_label": clean_text(raw.get("model_label"), 64),
            "files": files, "file_count": max(count, len(files)),
            "total_size": total if isinstance(total, int) and not isinstance(total, bool) and total >= 0 else None,
            "items": [clean_text(i, 300) for i in raw.get("items", [])[:3] if isinstance(i, str)] if isinstance(raw.get("items"), list) else [],
            "texture": None}
    thumb = raw.get("thumbnail")
    if isinstance(thumb, dict) and isinstance(thumb.get("file"), str):
        try:
            info["texture"] = preview.read_pixels(thumb["file"])
        except (OSError, ValueError):
            pass
    return info


class Confirm(Adw.Application):
    def __init__(self, sender, detail, seconds, info=None):
        # NON_UNIQUE: a second request must never be answered by an earlier window.
        super().__init__(application_id=APP_ID, flags=0x20)  # G_APPLICATION_NON_UNIQUE
        self.sender, self.detail, self.seconds, self.info = sender, detail, seconds, info
        self.accepted = False
        self.connect("activate", self.build)

    def build(self, *_):
        window = Adw.ApplicationWindow(application=self, title="Linux AirDrop", default_width=440,
                                       resizable=False, modal=True)
        window.set_icon_name("it.foglio.LinuxAirDrop")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Linux AirDrop", subtitle="Richiesta di ricezione"))
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=18, margin_bottom=22,
                      margin_start=24, margin_end=24)
        info = self.info
        sender = (info or {}).get("sender") or self.sender
        picture = self.preview_widget(info)
        if picture is not None:
            box.append(picture)
        title = Gtk.Label(label=f"Accettare da {sender}?", wrap=True, justify=Gtk.Justification.CENTER,
                          css_classes=["title-2"])
        box.append(title)
        if info and info["model_label"]:
            box.append(Gtk.Label(label=info["model_label"], css_classes=["dim-label"]))
        if info and (info["files"] or info["items"]):
            box.append(self.listing(info))
            box.append(Gtk.Label(label=self.summary(info), wrap=True, justify=Gtk.Justification.CENTER,
                                 css_classes=["dim-label"]))
        else:
            box.append(Gtk.Label(label=self.detail, wrap=True, justify=Gtk.Justification.CENTER, selectable=False,
                                 css_classes=["dim-label"]))
        box.append(Gtk.Label(label="Il nome del mittente non è verificato: chiunque può usare qualsiasi nome. "
                                   "I file verranno salvati in Download/LinuxAirDrop.",
                             wrap=True, justify=Gtk.Justification.CENTER, css_classes=["caption", "dim-label"]))
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6)
        decline = Gtk.Button(label="Rifiuta", css_classes=["pill"])
        accept = Gtk.Button(label="Accetta", css_classes=["pill", "suggested-action"])
        decline.connect("clicked", lambda *_: self.answer(window, False))
        accept.connect("clicked", lambda *_: self.answer(window, True))
        buttons.append(decline)
        buttons.append(accept)
        box.append(buttons)
        toolbar.set_content(box)
        window.set_content(toolbar)
        # Refusing is the default: a stray Enter must not accept a file transfer.
        window.set_default_widget(decline)
        window.connect("close-request", lambda *_: self.answer(window, False) or True)
        GLib.timeout_add_seconds(self.seconds, lambda: self.answer(window, False) or False)
        window.present()
        decline.grab_focus()
        self.window = window
        shot = os.environ.get("AIRDROP_CONFIRM_SCREENSHOT")     # used by the tests to look at the window
        if shot:
            GLib.timeout_add(700, self.take_screenshot, shot)

    def take_screenshot(self, path):
        paintable = Gtk.WidgetPaintable.new(self.window)
        snapshot = Gtk.Snapshot()
        paintable.snapshot(snapshot, self.window.get_width(), self.window.get_height())
        self.window.get_native().get_renderer().render_texture(snapshot.to_node(), None).save_to_png(path)
        self.quit()
        return False

    @staticmethod
    def preview_widget(info):
        """The sender's thumbnail when it sent one, otherwise a type icon for the first item."""
        if not info:
            return None
        if info["texture"]:
            width, height, pixels = info["texture"]
            texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8, GLib.Bytes.new(pixels), width * 4)
            # A GtkImage shows a paintable at its own size; GtkPicture would stretch it to the window width.
            return Gtk.Image(paintable=texture, pixel_size=max(width, height), halign=Gtk.Align.CENTER,
                             css_classes=["card"], overflow=Gtk.Overflow.HIDDEN)
        if info["files"]:
            kind = "folder" if info["files"][0]["kind"] == "folder" else info["files"][0]["kind"]
            return Gtk.Image(icon_name=kind_icon(kind), pixel_size=64, halign=Gtk.Align.CENTER, css_classes=["dim-label"])
        return None

    @staticmethod
    def listing(info):
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        shown = info["files"][:MAX_ROWS]
        for entry in shown:
            subtitle = kind_label(entry["kind"]) + (f" · {format_size(entry['size'])}" if entry["size"] is not None else "")
            row = Adw.ActionRow(title=entry["name"], subtitle=subtitle, use_markup=False, title_lines=1)
            row.add_prefix(Gtk.Image(icon_name=kind_icon(entry["kind"])))
            rows.append(row)
        for url in info["items"]:
            row = Adw.ActionRow(title=url, subtitle=kind_label("link"), use_markup=False, title_lines=2)
            row.add_prefix(Gtk.Image(icon_name=kind_icon("link")))
            rows.append(row)
        more = info["file_count"] - len(shown)
        if more > 0:
            rows.append(Adw.ActionRow(title=f"e altri {more} file", use_markup=False))
        return rows

    @staticmethod
    def summary(info):
        count = info["file_count"]
        if not count:
            return "Collegamento web"
        text = "1 elemento" if count == 1 else f"{count} elementi"
        if info["total_size"] is not None:
            return f"{text} · {format_size(info['total_size'])}"
        return f"{text} · dimensione non indicata dal mittente"

    def answer(self, window, accepted):
        if not self.accepted:
            self.accepted = accepted
        self.quit()
        return None


if __name__ == "__main__":
    if not Gtk.init_check():
        sys.exit(1)
    args = sys.argv[1:]
    info_path = None
    if "--info" in args:
        position = args.index("--info")
        info_path = args[position + 1] if position + 1 < len(args) else None
        del args[position:position + 2]
    sender = (args[0] if len(args) > 0 else "Dispositivo sconosciuto")[:160]
    detail = (args[1] if len(args) > 1 else "File")[:600]
    try:
        seconds = max(1, min(int(os.environ.get("AIRDROP_CONFIRM_SECONDS", "90")), 600))
    except ValueError:
        seconds = 90
    app = Confirm(sender, detail, seconds, load_info(info_path))
    app.run([sys.argv[0]])
    sys.exit(0 if app.accepted else 1)
