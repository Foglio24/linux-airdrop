#!/usr/bin/python3
"""A real user confirmation is required for every incoming transfer.

Exit status 0 means accepted; anything else (decline, timeout, closing the window) refuses.
"""
import os
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

APP_ID = "it.foglio.LinuxAirDrop.Confirm"


class Confirm(Adw.Application):
    def __init__(self, sender, detail, seconds):
        # NON_UNIQUE: a second request must never be answered by an earlier window.
        super().__init__(application_id=APP_ID, flags=0x20)  # G_APPLICATION_NON_UNIQUE
        self.sender, self.detail, self.seconds = sender, detail, seconds
        self.accepted = False
        self.connect("activate", self.build)

    def build(self, *_):
        window = Adw.ApplicationWindow(application=self, title="Linux AirDrop", default_width=420,
                                       resizable=False, modal=True)
        window.set_icon_name("it.foglio.LinuxAirDrop")
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title="Linux AirDrop", subtitle="Richiesta di ricezione"))
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=24,
                      margin_start=24, margin_end=24)
        title = Gtk.Label(label=f"Accettare da {self.sender}?", wrap=True, justify=Gtk.Justification.CENTER,
                          css_classes=["title-2"])
        files = Gtk.Label(label=self.detail, wrap=True, justify=Gtk.Justification.CENTER, selectable=False,
                          css_classes=["dim-label"])
        where = Gtk.Label(label="I file verranno salvati in Download/LinuxAirDrop.", wrap=True,
                          justify=Gtk.Justification.CENTER, css_classes=["caption", "dim-label"])
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER, margin_top=6)
        decline = Gtk.Button(label="Rifiuta", css_classes=["pill"])
        accept = Gtk.Button(label="Accetta", css_classes=["pill", "suggested-action"])
        decline.connect("clicked", lambda *_: self.answer(window, False))
        accept.connect("clicked", lambda *_: self.answer(window, True))
        buttons.append(decline)
        buttons.append(accept)
        for widget in (title, files, where, buttons):
            box.append(widget)
        toolbar.set_content(box)
        window.set_content(toolbar)
        # Refusing is the default: a stray Enter must not accept a file transfer.
        window.set_default_widget(decline)
        window.connect("close-request", lambda *_: self.answer(window, False) or True)
        GLib.timeout_add_seconds(self.seconds, lambda: self.answer(window, False) or False)
        window.present()
        decline.grab_focus()
        self.window = window

    def answer(self, window, accepted):
        if not self.accepted:
            self.accepted = accepted
        self.quit()
        return None


if __name__ == "__main__":
    if not Gtk.init_check():
        sys.exit(1)
    sender = (sys.argv[1] if len(sys.argv) > 1 else "Dispositivo sconosciuto")[:160]
    detail = (sys.argv[2] if len(sys.argv) > 2 else "File")[:600]
    try:
        seconds = max(1, min(int(os.environ.get("AIRDROP_CONFIRM_SECONDS", "90")), 600))
    except ValueError:
        seconds = 90
    app = Confirm(sender, detail, seconds)
    app.run([sys.argv[0]])
    sys.exit(0 if app.accepted else 1)
