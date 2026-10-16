"""The "Dispositivi" page: devices the user has dealt with, and why nothing is accepted automatically."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from fileinfo import device_label, format_when

IDENTITY_TEXT = {"record": "Riconosciuto da un record firmato da Apple (non è una prova di identità)",
                 "name": "Riconosciuto solo dal nome: chiunque può usare lo stesso nome"}

NO_AUTO_ACCEPT = (
    "Ogni trasferimento in arrivo chiede la tua conferma, anche da dispositivi già usati. L’accettazione automatica "
    "sarebbe sicura solo se il PC potesse verificare con certezza chi sta inviando, e AirDrop non lo permette:\n\n"
    "• Il nome del dispositivo lo sceglie chi lo possiede: chiunque può chiamare il proprio telefono come il tuo.\n"
    "• Gli indirizzi di rete AWDL cambiano a ogni sessione e si possono falsificare.\n"
    "• Il record firmato da Apple che alcuni iPhone allegano alle richieste non è legato alla connessione: "
    "chi l’ha ricevuto una volta può ripresentarlo.\n"
    "• La connessione cifrata di AirDrop usa certificati creati dal dispositivo stesso, senza un’autorità che ne garantisca l’identità.\n\n"
    "Per questo la conferma resta sempre manuale. I dettagli tecnici sono nel file docs/TRUSTED-DEVICES.md.")


def describe(device):
    """(title, subtitle) of a remembered device; pure so it can be tested. The second line says how sure we are who it is."""
    parts = [device_label(device.get("model")) if device.get("model") else None,
             "ultima connessione " + format_when(device["last_seen"])]
    count = device["transfer_count"]
    if count:
        detail = []
        if device["received_count"]:
            detail.append(f"{device['received_count']} ricevut{'o' if device['received_count'] == 1 else 'i'}")
        if device["sent_count"]:
            detail.append(f"{device['sent_count']} invia{'to' if device['sent_count'] == 1 else 'ti'}")
        parts.append(f"{count} trasferiment{'o' if count == 1 else 'i'} ({', '.join(detail)})")
        if device.get("last_transfer") and device["last_transfer"] < device["last_seen"] - 60:
            parts.append("ultimo trasferimento " + format_when(device["last_transfer"]))
    else:
        parts.append("nessun trasferimento concluso")
    note = "Riconosciuto da un record firmato da Apple" if device["identity"] == "record" else "Riconosciuto solo dal nome"
    return device["name"], " · ".join(p for p in parts if p) + "\n" + note


class DevicesPage(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.group = Adw.PreferencesGroup(title="Dispositivi recenti",
                                          description="Dispositivi con cui hai ricevuto o inviato file. Servono solo da promemoria.")
        self.forget_all = Gtk.Button(label="Dimentica tutti", valign=Gtk.Align.CENTER, css_classes=["flat"])
        self.forget_all.connect("clicked", self.confirm_forget_all)
        self.group.set_header_suffix(self.forget_all)
        self.rows = []
        self.empty_row = Adw.ActionRow(title="Nessun dispositivo ricordato", use_markup=False,
                                       subtitle="Compariranno qui dopo il primo trasferimento accettato.")
        content.append(self.group)

        trust = Adw.PreferencesGroup(title="Accettazione automatica")
        row = Adw.ActionRow(title="Non disponibile", use_markup=False, activatable=True,
                            subtitle="AirDrop non permette di verificare chi invia: ogni ricezione chiede conferma.")
        row.add_prefix(Gtk.Image(icon_name="security-high-symbolic"))
        row.add_suffix(Gtk.Image(icon_name="help-about-symbolic"))
        row.connect("activated", self.explain)
        trust.add(row)
        content.append(trust)

        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480, margin_top=12, margin_bottom=24,
                          margin_start=12, margin_end=12, child=content)
        self.append(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, vexpand=True))
        self.refresh()

    def refresh(self):
        for row in self.rows:
            self.group.remove(row)
        self.rows = []
        devices = self.app.store.devices()
        for device in devices:
            title, subtitle = describe(device)
            row = Adw.ActionRow(title=title, subtitle=subtitle, use_markup=False, subtitle_lines=4)
            row.add_prefix(Gtk.Image(icon_name="phone-symbolic"))
            if device["identity"] == "name":
                badge = Gtk.Image(icon_name="dialog-question-symbolic", tooltip_text=IDENTITY_TEXT["name"], css_classes=["dim-label"])
            else:
                badge = Gtk.Image(icon_name="emblem-ok-symbolic", tooltip_text=IDENTITY_TEXT["record"], css_classes=["dim-label"])
            row.add_suffix(badge)
            forget = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Dimentica questo dispositivo")
            forget.connect("clicked", lambda _b, key=device["key"], name=device["name"]: self.forget(key, name))
            row.add_suffix(forget)
            self.group.add(row)
            self.rows.append(row)
        if not devices:
            self.group.add(self.empty_row)
            self.rows.append(self.empty_row)
        self.forget_all.set_sensitive(bool(devices))

    def forget(self, key, name):
        self.app.store.forget_device(key)
        self.refresh()
        self.app.toast(f"«{name}» dimenticato")

    def confirm_forget_all(self, *_):
        dialog = Adw.AlertDialog(heading="Dimenticare tutti i dispositivi?",
                                 body="Verrà cancellato solo questo elenco. La cronologia dei trasferimenti non cambia.")
        dialog.add_response("cancel", "Annulla")
        dialog.add_response("forget", "Dimentica tutti")
        dialog.set_response_appearance("forget", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.choose(self.app.window, None, self.forget_answered)

    def forget_answered(self, dialog, result):
        if dialog.choose_finish(result) == "forget":
            self.app.store.forget_devices()
            self.refresh()
            self.app.toast("Dispositivi dimenticati")

    def explain(self, *_):
        dialog = Adw.AlertDialog(heading="Perché non c’è l’accettazione automatica", body=NO_AUTO_ACCEPT)
        dialog.add_response("ok", "Ho capito")
        dialog.set_default_response("ok")
        dialog.present(self.app.window)
