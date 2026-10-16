"""The "Diagnostica" page: live checks with green/yellow/red states, a copyable report and guided repairs."""
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

import diagnostics as dg
import repair

ICONS = {dg.OK: "emblem-ok-symbolic", dg.WARN: "dialog-warning-symbolic", dg.ERROR: "dialog-error-symbolic",
         dg.IDLE: "media-playback-pause-symbolic"}
CSS = {dg.OK: "success", dg.WARN: "warning", dg.ERROR: "error", dg.IDLE: "dim-label"}
REFRESH_SECONDS = 4


def summary_text(checks):
    """(state, title, subtitle) of the whole page; pure so it can be tested."""
    errors = sum(c.state == dg.ERROR for c in checks)
    warnings = sum(c.state == dg.WARN for c in checks)
    if errors:
        return dg.ERROR, f"{errors} {'errore' if errors == 1 else 'errori'}" + (f" e {warnings} {'avviso' if warnings == 1 else 'avvisi'}" if warnings else ""), \
            "Tocca un controllo rosso per capire cosa fare."
    if warnings:
        return dg.WARN, f"{warnings} {'avviso' if warnings == 1 else 'avvisi'}", "Niente di grave, ma conviene dare un’occhiata."
    return dg.OK, "Tutto funziona", "I controlli grigi riguardano parti che servono solo quando il PC è visibile."


class DiagnosticsPage(Gtk.Box):
    def __init__(self, app, system=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.system = system or dg.System()
        self.checks, self.problems, self.rows = [], [], {}
        self.busy = False
        self.timer = None
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)

        top = Gtk.Box(spacing=14, margin_top=6)
        self.icon = Gtk.Image(icon_name=ICONS[dg.IDLE], pixel_size=40, valign=Gtk.Align.CENTER)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        self.title = Gtk.Label(label="Controllo in corso…", xalign=0, css_classes=["title-2"])
        self.subtitle = Gtk.Label(label="", xalign=0, wrap=True, css_classes=["dim-label"])
        texts.append(self.title)
        texts.append(self.subtitle)
        self.spinner = Adw.Spinner(visible=False, valign=Gtk.Align.CENTER)
        top.append(self.icon)
        top.append(texts)
        top.append(self.spinner)
        content.append(top)

        buttons = Gtk.Box(spacing=8)
        self.refresh_button = Gtk.Button(child=Adw.ButtonContent(label="Aggiorna", icon_name="view-refresh-symbolic"))
        self.refresh_button.connect("clicked", lambda *_: self.refresh())
        self.copy_button = Gtk.Button(child=Adw.ButtonContent(label="Copia rapporto", icon_name="edit-copy-symbolic"),
                                      css_classes=["suggested-action"])
        self.copy_button.connect("clicked", self.copy_report)
        for button in (self.refresh_button, self.copy_button):
            buttons.append(button)
        content.append(buttons)

        self.repair_group = Adw.PreferencesGroup(title="Riparazione", visible=False,
                                                 description="Problemi riconosciuti. Prima di fare qualcosa di rischioso l’app chiede conferma.")
        content.append(self.repair_group)
        self.group = Adw.PreferencesGroup(title="Controlli")
        content.append(self.group)
        log_group = Adw.PreferencesGroup()
        log_row = Adw.ActionRow(title="Registro delle riparazioni", subtitle="Cosa è stato tentato e con quale esito", use_markup=False,
                                activatable=True)
        log_row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
        log_row.connect("activated", lambda *_: app.show_repair_log())
        log_group.add(log_row)
        content.append(log_group)

        clamp = Adw.Clamp(maximum_size=640, tightening_threshold=480, margin_top=12, margin_bottom=24,
                          margin_start=12, margin_end=12, child=content)
        self.append(Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=clamp, vexpand=True))
        self.repair_rows = []

    # ------------------------------------------------------------- refreshing
    def start_auto(self):
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

    def refresh(self):
        """Gather in a worker thread (commands may take a moment) and apply the result on the main thread."""
        if self.busy:
            return
        self.busy = True
        self.spinner.set_visible(True)
        facts = self.app.diagnostic_facts()
        known = self.app.known_pids()

        def work():
            try:
                slow = dg.slow_facts(self.system)
                checks = dg.check_all(self.system, facts, slow)
                problems = repair.detect(checks, facts, self.system, known)
            except Exception as exc:        # a broken check must never take the page down
                checks, problems = [dg.Check("internal", "Diagnostica", dg.WARN, f"Controllo non riuscito: {exc}")], []
            GLib.idle_add(self.apply, checks, problems, facts)
        threading.Thread(target=work, daemon=True).start()

    def apply(self, checks, problems, facts):
        self.busy = False
        self.spinner.set_visible(False)
        self.checks, self.problems, self.facts = checks, problems, facts
        state, title, subtitle = summary_text(checks)
        self.icon.set_from_icon_name(ICONS[state])
        for name in CSS.values():
            self.icon.remove_css_class(name)
        self.icon.add_css_class(CSS[state])
        self.title.set_label(title)
        self.subtitle.set_label(subtitle)
        seen = set()
        for check in checks:
            seen.add(check.id)
            row = self.rows.get(check.id)
            if row is None:
                row = Adw.ActionRow(title=check.title, use_markup=False, activatable=True, subtitle_lines=2)
                row.state_icon = Gtk.Image()
                row.add_prefix(row.state_icon)
                row.add_suffix(Gtk.Image(icon_name="go-next-symbolic", css_classes=["dim-label"]))
                row.connect("activated", lambda r: self.show_check(r.check_id))
                row.check_id = check.id
                self.rows[check.id] = row
                self.group.add(row)
            row.set_subtitle(check.summary)
            row.state_icon.set_from_icon_name(ICONS[check.state])
            for name in CSS.values():
                row.state_icon.remove_css_class(name)
            row.state_icon.add_css_class(CSS[check.state])
            row.state_icon.update_property([Gtk.AccessibleProperty.LABEL], [dg.STATE_LABELS[check.state]])
        for ident in [i for i in self.rows if i not in seen]:
            self.group.remove(self.rows.pop(ident))
        self.show_problems(problems)
        return False

    def show_problems(self, problems):
        for row in self.repair_rows:
            self.repair_group.remove(row)
        self.repair_rows = []
        for problem in problems:
            row = Adw.ActionRow(title=problem.title, subtitle=problem.why, use_markup=False, subtitle_lines=4)
            row.add_prefix(Gtk.Image(icon_name="tools-symbolic" if problem.fix != "none" else "dialog-information-symbolic",
                                     css_classes=["warning"]))
            if problem.fix != "none":
                button = Gtk.Button(label=problem.fix_label or "Ripara", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                button.connect("clicked", lambda _b, p=problem: self.app.start_repair(p))
                row.add_suffix(button)
            self.repair_group.add(row)
            self.repair_rows.append(row)
        self.repair_group.set_visible(bool(problems))

    # ------------------------------------------------------------------ actions
    def show_check(self, ident):
        check = next((c for c in self.checks if c.id == ident), None)
        if check is None:
            return
        paragraphs = [f"{dg.STATE_LABELS[check.state]}: {check.summary}"]
        if check.explain:
            paragraphs.append(check.explain)
        if check.hint and check.state in (dg.WARN, dg.ERROR):
            paragraphs.append("Cosa fare: " + check.hint)
        if check.details:
            paragraphs.append("\n".join(check.details))
        dialog = Adw.AlertDialog(heading=check.title, body="\n\n".join(paragraphs))
        dialog.add_response("close", "Chiudi")
        dialog.set_default_response("close")
        dialog.present(self.app.window)

    def report(self):
        return dg.report_text(self.checks, getattr(self, "facts", self.app.diagnostic_facts()), version=self.app.version)

    def copy_report(self, button):
        if not self.checks:
            return
        button.get_clipboard().set(self.report())
        self.app.toast("Rapporto copiato")
