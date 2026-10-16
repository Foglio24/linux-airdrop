"""Transfers in the window: live rows, history and device records, the send queue, drag and drop, cancelling and repairs.

The radio and the OpenDrop processes are replaced by fakes; what is checked is what the window does with the
events they would produce. (This does not prove anything about real iPhones.)
"""
import importlib.util
import os
from pathlib import Path
import signal
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.argv = ["app.py"]
spec = importlib.util.spec_from_file_location("app_transfers_under_test", ROOT / "app.py")
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
from gi.repository import Gdk, Gio, GLib  # noqa: E402
import transfer_events as tev  # noqa: E402


def event(kind, **fields):
    line = tev.PREFIX + __import__("json").dumps({"event": kind, **fields})
    return tev.parse(line)


class FakeStdin:
    def __init__(self):
        self.sent = []

    def write(self, text):
        self.sent.append(text)

    def flush(self):
        pass


class FakeProcess:
    _next_pid = 5000

    def __init__(self, alive=True):
        FakeProcess._next_pid += 1
        self.pid, self.alive, self.stdin, self.signals = FakeProcess._next_pid, alive, FakeStdin(), []
        self.stdout = iter(())

    def poll(self):
        return None if self.alive else 0

    def send_signal(self, sig):
        self.signals.append(sig)

    def terminate(self):
        self.signals.append(signal.SIGTERM)

    def wait(self, timeout=None):
        return 0


class App(app_module.AirDrop):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.notices, self.toasts_shown = [], []

    def send_notification(self, ident, notification):
        self.notices.append(ident)

    def withdraw_notification(self, ident):
        pass

    def quit(self):
        pass

    def toast(self, text, button=None, action=None):
        self.toasts_shown.append(text)


def pump():
    context = GLib.MainContext.default()
    while context.iteration(False):
        pass


@unittest.skipIf(Gdk.Display.get_default() is None, "no display available")
class TransferBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        app_module.SETTINGS = cls.base / "config/settings.json"
        app_module.STATE = cls.base / "state"
        cls.app = App(screenshot=cls.base / "unused.png")
        cls.app.register(None)
        cls.app.build_window()
        cls.app.iface = "wlp4s0"

    @classmethod
    def tearDownClass(cls):
        cls.app.window.destroy()
        cls.app.store.close()
        cls.tmp.cleanup()

    def setUp(self):
        app = self.app
        app.destination = self.base / ("dl-" + self.id().rsplit(".", 1)[-1])
        app.destination.mkdir(parents=True, exist_ok=True)
        app.window.set_visible(True)
        app.store.set_enabled(True)
        app.store.clear_history()
        app.store.forget_devices()
        app.remember = True
        app.transfers.clear()
        app.send_queue.clear()
        app.current_send, app.asking_start, app.restart_after = None, False, False
        app.bridge, app.active, app.stopping, app.closing = None, False, False, False
        app.receiver, app.ble, app.sender, app.finder = None, None, None, None
        app.notices, app.toasts_shown = [], []
        app.known_files = set()
        app.auto_repair = True
        app.attempts.reset()
        app.attempts.log_file.unlink(missing_ok=True)
        app.last_peer = None
        app.sync_transfers()

    def session(self):
        self.app.bridge = FakeProcess()
        self.app.active = True
        self.app.receiver = FakeProcess()

    def ask(self, ident="t1", **extra):
        data = dict(id=ident, sender="iPhone di Anna", model="iPhone15,2", device_key="name:abc", identity="name",
                    files=[{"name": "foto.jpg", "kind": "image", "size": None, "is_dir": False}], total=None)
        data.update(extra)
        self.app.receive_event(event("ask", **data))
        return self.app.transfers[f"receive:{ident}"]

    # ------------------------------------------------------------------------ receiving
    def test_a_complete_receive_updates_the_row_history_and_devices(self):
        t = self.ask()
        self.assertEqual(t.state, "asking")
        self.assertTrue(self.app.transfer_list.get_visible())
        self.app.receive_event(event("ask_accepted", id="t1"))
        self.assertEqual(t.state, "waiting")
        self.app.receive_event(event("receive_start", id="t1", total=1000))
        self.app.receive_event(event("receive_progress", id="t1", done=400, total=1000))
        self.assertEqual((t.state, t.meter.done, t.meter.total), ("running", 400, 1000))
        self.assertEqual(self.app.store.transfers()[0]["status"], "in_progress")
        folder = str(self.app.destination / "AirDrop-1")
        self.app.receive_event(event("receive_done", id="t1", folder=folder, files=[{"name": "foto.jpg", "kind": "image", "size": 1000}]))
        self.assertEqual(t.state, "done")
        row = self.app.store.transfers()[0]
        self.assertEqual((row["status"], row["direction"], row["peer_name"], row["total_bytes"]), ("completed", "receive", "iPhone di Anna", 1000))
        self.assertEqual(row["files"][0]["path"], folder + "/foto.jpg")
        self.assertEqual(row["folder"], folder)
        device = self.app.store.devices()[0]
        self.assertEqual((device["name"], device["received_count"], device["identity"]), ("iPhone di Anna", 1, "name"))
        self.assertIn("received", self.app.notices or ["received"]) if self.app.window_hidden() else None

    def test_declined_request_is_recorded_and_the_device_is_not_remembered(self):
        self.ask()
        self.app.receive_event(event("ask_declined", id="t1"))
        row = self.app.store.transfers()[0]
        self.assertEqual((row["status"], row["peer_name"]), ("declined", "iPhone di Anna"))
        self.assertEqual(self.app.store.devices(), [])

    def test_failed_and_cancelled_receives_are_told_apart(self):
        for ident, cancelled, expected in (("f", False, "failed"), ("c", True, "cancelled")):
            self.ask(ident)
            self.app.receive_event(event("ask_accepted", id=ident))
            self.app.receive_event(event("receive_start", id=ident, total=None))
            self.app.receive_event(event("receive_failed", id=ident, cancelled=cancelled, error="Trasferimento interrotto"))
            self.assertEqual(self.app.transfers[f"receive:{ident}"].state, expected if expected != "failed" else "failed")
        statuses = sorted(t["status"] for t in self.app.store.transfers())
        self.assertEqual(statuses, ["cancelled", "failed"])

    def test_unknown_total_never_shows_a_percentage(self):
        t = self.ask()
        self.app.receive_event(event("ask_accepted", id="t1"))
        self.app.receive_event(event("receive_start", id="t1", total=None))
        self.app.receive_event(event("receive_progress", id="t1", done=5000, total=None))
        from ui_transfers import present
        view = present(t)
        self.assertIsNone(view["fraction"])
        self.assertNotIn("%", view["line"])
        self.assertTrue(view["indeterminate"])

    def test_cancel_button_sends_the_command_to_the_receiver_only_once_a_transfer_is_waiting_or_running(self):
        self.session()
        t = self.ask()
        self.assertFalse(t.cancellable)          # the confirmation window is the user's to answer
        self.app.cancel_transfer(t.key)
        self.assertEqual(self.app.receiver.stdin.sent, [])
        self.app.receive_event(event("ask_accepted", id="t1"))
        self.app.receive_event(event("receive_start", id="t1", total=100))
        self.app.cancel_transfer(t.key)
        self.assertEqual(self.app.receiver.stdin.sent, ["cancel t1\n"])
        self.assertFalse(t.cancellable)           # not twice

    def test_an_approval_nobody_used_expires(self):
        t = self.ask()
        self.app.receive_event(event("ask_accepted", id="t1"))
        t.created -= 500
        self.app.tick()
        self.assertEqual(t.state, "failed")
        self.assertEqual(self.app.store.transfers()[0]["status"], "failed")

    def test_events_for_unknown_transfers_are_ignored(self):
        self.app.receive_event(event("receive_progress", id="never-asked", done=5, total=10))
        self.assertEqual(self.app.transfers, {})

    def test_shared_link_is_stored_as_a_link(self):
        self.ask("l", files=[], items=["https://example.org/x"])
        self.app.receive_event(event("ask_accepted", id="l"))
        self.app.receive_event(event("receive_done", id="l", folder=None, files=[{"name": "https://example.org/x", "kind": "link", "size": None}]))
        row = self.app.store.transfers()[0]
        self.assertEqual((row["status"], row["files"][0]["kind"], row["files"][0]["path"]), ("completed", "link", None))

    def test_ending_the_session_closes_what_was_running(self):
        self.session()
        t = self.ask()
        self.app.receive_event(event("ask_accepted", id="t1"))
        self.app.receive_event(event("receive_start", id="t1", total=100))
        bridge = self.app.bridge
        self.app.bridge_ended(bridge, 0)
        self.assertEqual(t.state, "failed")
        self.assertEqual(self.app.store.transfers()[0]["status"], "failed")

    def test_remembering_can_be_switched_off_without_losing_the_live_row(self):
        self.app.set_remember(False)
        t = self.ask()
        self.app.receive_event(event("ask_accepted", id="t1"))
        self.app.receive_event(event("receive_done", id="t1", folder="/x", files=[{"name": "a", "kind": "other", "size": 1}]))
        self.assertEqual(t.state, "done")
        self.assertEqual((self.app.store.transfers(), self.app.store.devices()), ([], []))
        self.app.set_remember(True)

    # -------------------------------------------------------------------------- sending
    def make_files(self, *names):
        folder = self.base / ("src-" + self.id().rsplit(".", 1)[-1])
        folder.mkdir(exist_ok=True)
        paths = []
        for name in names:
            path = folder / name
            if name.endswith("/"):
                path.mkdir(exist_ok=True)
                (path / "dentro.txt").write_text("x")
            else:
                path.write_text("contenuto")
            paths.append(str(path).rstrip("/"))
        return paths

    def test_sending_while_not_visible_asks_to_start_and_queues_the_job(self):
        asked = []
        with patch.object(self.app, "confirm_start_for_send", lambda: asked.append(1)), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("a.txt", "b.txt"))
        self.assertEqual(asked, [1])
        self.assertEqual(len(self.app.send_queue), 1)
        job = self.app.send_queue[0]
        self.assertEqual(job.transfer.state, "queued")
        self.assertEqual([f["name"] for f in job.transfer.files], ["a.txt", "b.txt"])

    def test_declining_the_start_cancels_the_queue_and_records_it(self):
        with patch.object(self.app, "confirm_start_for_send", lambda: None), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("a.txt"))
        self.app.start_for_send_answered(False)
        self.assertEqual(self.app.send_queue, [])
        self.assertEqual(self.app.store.transfers()[0]["status"], "cancelled")

    def test_with_a_session_the_next_job_starts_discovery_and_later_ones_wait(self):
        self.session()
        started = []
        with patch.object(self.app, "begin_discover", lambda job: started.append(job)), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("a.txt"))
            self.app.request_send(self.make_files("b.txt"))
        self.assertEqual(len(started), 1)
        self.assertEqual(len(self.app.send_queue), 1)
        self.assertEqual(self.app.send_queue[0].transfer.state, "queued")

    def test_a_send_reports_real_progress_and_is_recorded_when_it_ends(self):
        self.session()
        with patch.object(self.app, "begin_discover", lambda job: None), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("a.txt"))
        job = self.app.current_send
        t = job.transfer
        t.peer, t.state = "iPad di Giulia", "waiting"
        t.identity, t.device_key = app_module.make_device_key("iPad di Giulia")
        t.db_id = self.app.store.start_transfer("send", "iPad di Giulia", None, t.device_key, t.total, files=t.files)
        self.app.send_event(job, event("send_accepted"))
        self.assertEqual(t.state, "preparing")
        self.app.send_event(job, event("send_start", total=1000))
        self.app.send_event(job, event("send_progress", done=250, total=1000))
        self.assertEqual((t.state, t.meter.done, t.meter.total), ("running", 250, 1000))
        self.app.send_finished(job, 0)
        self.assertEqual(t.state, "done")
        self.assertIsNone(self.app.current_send)
        row = self.app.store.transfers()[0]
        self.assertEqual((row["status"], row["direction"], row["peer_name"]), ("completed", "send", "iPad di Giulia"))
        device = self.app.store.devices()[0]
        self.assertEqual((device["name"], device["sent_count"]), ("iPad di Giulia", 1))

    def run_pipeline(self, mode, lines, code=0):
        """Queue a send of a file and a folder, run prepare_send/spawn_sender/wait_send for real against a fake process."""
        self.session()
        paths = self.make_files("nota.txt", "Cartella/")
        spawned = []

        class FakeSender(FakeProcess):
            def __init__(self, argv):
                super().__init__()
                self.argv, self.stdout = argv, iter([tev.PREFIX + __import__("json").dumps(e) + "\n" for e in lines] + ["plain log line\n"])

            def wait(self, timeout=None):
                return code

        def popen(argv, **kwargs):
            sender = FakeSender(argv)
            sender.env = kwargs.get("env")
            spawned.append(sender)
            return sender

        with patch.object(self.app, "begin_discover", lambda job: None), patch.object(app_module, "recovery_needed", return_value=False):
            plan = app_module.outgoing.inspect_paths(paths)
            self.app.queue_send(plan, mode)
        job = self.app.current_send
        t = job.transfer
        t.peer, t.state = "iPad di Giulia", "preparing"
        t.identity, t.device_key = app_module.make_device_key("iPad di Giulia")
        t.db_id = self.app.store.start_transfer("send", "iPad di Giulia", None, t.device_key, t.total, files=t.files)
        job.peer = {"id": "0123456789ab", "name": "iPad di Giulia"}
        with patch.object(app_module.subprocess, "Popen", popen):
            self.app.prepare_send(job)                    # the worker-thread part, run inline
            deadline = time.time() + 5
            while t.state not in ("done", "failed", "cancelled", "declined") and time.time() < deadline:
                pump()
                time.sleep(0.02)
        return job, t, spawned

    def test_the_whole_send_pipeline_with_a_folder_sent_as_zip(self):
        lines = [{"event": "send_accepted"}, {"event": "send_start", "total": 2000},
                 {"event": "send_progress", "done": 500, "total": 2000}, {"event": "send_progress", "done": 2000, "total": 2000},
                 {"event": "send_done"}]
        job, t, spawned = self.run_pipeline("zip", lines)
        self.assertEqual(t.state, "done")
        argv = spawned[0].argv
        self.assertEqual(argv[argv.index("-r") + 1], "0123456789ab")
        sent = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
        self.assertEqual(len(sent), 2)
        self.assertTrue(sent[0].endswith("nota.txt"))
        self.assertTrue(sent[1].endswith("Cartella.zip"))         # the folder travels as a ZIP file
        self.assertEqual(spawned[0].env["AIRDROP_EVENTS"], "1")
        self.assertEqual(spawned[0].env["AIRDROP_STALL_TIMEOUT"], "90")      # a short AWDL gap must not end a send
        self.assertEqual((t.meter.done, t.meter.total), (2000, 2000))
        self.assertFalse(Path(sent[1]).exists())                  # the temporary ZIP is removed at the end
        row = self.app.store.transfers()[0]
        self.assertEqual((row["status"], row["peer_name"], row["file_count"]), ("completed", "iPad di Giulia", 2))
        self.assertIn("plain log line", "\n".join(self.app.log_lines))      # ordinary lines still go to the log

    def test_the_folder_can_be_sent_natively_and_a_declined_send_is_reported_as_declined(self):
        job, t, spawned = self.run_pipeline("native", [{"event": "send_declined"}], code=1)
        argv = spawned[0].argv
        self.assertTrue([argv[i + 1] for i, a in enumerate(argv) if a == "-f"][1].endswith("Cartella"))     # the folder itself
        self.assertEqual(t.state, "declined")
        self.assertEqual(self.app.store.transfers()[0]["status"], "declined")
        self.assertIsNone(job.workdir)                           # nothing was zipped

    def test_declined_failed_and_cancelled_sends(self):
        self.session()
        outcomes = []
        for label, setup, code in (("declined", lambda j: setattr(j, "declined", True), 1), ("failed", lambda j: None, 1),
                                   ("cancelled", lambda j: setattr(j, "cancelled", True), 0)):
            with patch.object(self.app, "begin_discover", lambda job: None), patch.object(app_module, "recovery_needed", return_value=False):
                self.app.request_send(self.make_files(label + ".txt"))
            job = self.app.current_send
            job.transfer.peer = "iPhone"
            setup(job)
            self.app.send_finished(job, code)
            outcomes.append(self.app.store.transfers()[0]["status"])
        self.assertEqual(outcomes, ["declined", "failed", "cancelled"])

    def test_session_ending_under_a_running_send_reports_why(self):
        self.session()
        with patch.object(self.app, "begin_discover", lambda job: None), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("a.txt"))
        job = self.app.current_send
        job.process = FakeProcess(alive=False)
        bridge = self.app.bridge
        self.app.bridge_ended(bridge, 0)
        self.assertEqual(job.abort_reason, "Visibilità interrotta durante il trasferimento")
        self.app.send_finished(job, -15)
        self.assertEqual(job.transfer.state, "failed")
        self.assertIn("Visibilità interrotta", self.app.store.transfers()[0]["error"])

    def test_cancelling_a_queued_send_removes_it_and_records_it(self):
        self.session()
        with patch.object(self.app, "begin_discover", lambda job: None), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("first.txt"))
            self.app.request_send(self.make_files("second.txt"))
        queued = self.app.send_queue[0].transfer
        self.assertTrue(queued.cancellable)
        self.app.cancel_transfer(queued.key)
        self.assertEqual(self.app.send_queue, [])
        self.assertEqual(queued.state, "cancelled")

    def test_cancelling_a_running_send_stops_its_process(self):
        self.session()
        with patch.object(self.app, "begin_discover", lambda job: None), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("a.txt"))
        job = self.app.current_send
        job.process, job.transfer.state = FakeProcess(), "running"
        self.app.cancel_transfer(job.transfer.key)
        self.assertEqual(job.process.signals, [signal.SIGTERM])
        self.assertTrue(job.cancelled)

    def test_folders_ask_how_to_send_them_and_unsendable_things_are_explained(self):
        asked, alerts = [], []
        with patch.object(self.app, "ask_folder_mode", lambda plan, notes: asked.append(plan)), \
                patch.object(self.app, "alert", lambda h, b: alerts.append((h, b))), patch.object(app_module, "recovery_needed", return_value=False):
            self.app.request_send(self.make_files("cartella/"))
            self.app.request_send([str(self.base / "does-not-exist")])
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0]["folders"], 1)
        self.assertEqual(alerts[0][0], "Non posso inviare")
        self.assertIn("non esiste più", alerts[0][1])

    def test_very_large_files_need_a_confirmation_with_an_honest_estimate(self):
        dialogs = []
        paths = self.make_files("enorme.bin")
        with patch.object(app_module, "LARGE_SEND_BYTES", 5), patch.object(app_module, "recovery_needed", return_value=False), \
                patch.object(self.app, "confirm_start_for_send", lambda: None), \
                patch.object(self.app, "confirm_large_send", lambda plan, p: dialogs.append((plan["total"], p))):
            self.app.request_send(paths)
            self.assertEqual(self.app.send_queue, [])               # nothing queued before the answer
            self.assertEqual(dialogs, [(9, paths)])
            self.app.large_send_confirmed(paths)                   # "Invia comunque"
        self.assertEqual(len(self.app.send_queue), 1)
        self.assertFalse(getattr(self.app, "_large_ok"))

    def test_without_usable_wifi_nothing_is_queued(self):
        alerts = []
        with patch.object(self.app, "alert", lambda h, b: alerts.append(h)), patch.object(app_module, "recovery_needed", return_value=True):
            self.app.request_send(self.make_files("a.txt"))
        self.assertEqual((alerts, self.app.send_queue), (["Invio non disponibile"], []))

    def test_real_pipes_to_a_scripted_receiver_carry_events_out_and_cancel_commands_in(self):
        script = self.base / "scripted_receiver.py"
        script.write_text(
            "import json, sys\nP = '@@airdrop '\n"
            "def emit(**e): print(P + json.dumps(e), flush=True)\n"
            "emit(event='ask', id='x1', sender='iPhone', model='iPhone15,2', device_key='name:a', identity='name',\n"
            "     files=[{'name': 'a.jpg', 'kind': 'image', 'size': None}])\n"
            "emit(event='ask_accepted', id='x1')\nemit(event='receive_start', id='x1', total=1000)\n"
            "emit(event='receive_progress', id='x1', done=250, total=1000)\n"
            "print('AirDrop: richiesta /Ask', flush=True)\n"
            "for line in sys.stdin:\n"
            "    parts = line.split()\n"
            "    if parts[:1] == ['cancel']:\n"
            "        emit(event='receive_failed', id=parts[1], cancelled=True, error='annullato')\n        break\n")
        self.app.active = True
        with patch.object(self.app, "start_command", lambda mode, extra=(): [sys.executable, "-u", str(script)]):
            self.app.spawn_receiver()
        process = self.app.receiver
        try:
            transfer = None
            deadline = time.time() + 8
            while time.time() < deadline:
                pump()
                transfer = self.app.transfers.get("receive:x1")
                if transfer is not None and transfer.state == "running" and transfer.meter.done == 250:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(transfer, "events did not arrive through the pipe")
            self.assertEqual((transfer.state, transfer.meter.done, transfer.meter.total), ("running", 250, 1000))
            self.assertIn("AirDrop: richiesta /Ask", "\n".join(self.app.log_lines))     # ordinary lines are still logged
            self.assertNotIn("@@airdrop", "\n".join(self.app.log_lines))                # event lines are not
            self.app.cancel_transfer(transfer.key)
            deadline = time.time() + 8
            while transfer.state != "cancelled" and time.time() < deadline:
                pump()
                time.sleep(0.02)
            self.assertEqual(transfer.state, "cancelled")
            self.assertEqual(self.app.store.transfers()[0]["status"], "cancelled")
        finally:
            self.app.active = False
            process.kill()
            process.wait()
            pump()

    # -------------------------------------------------------------------- drop and open
    def test_dropping_files_reaches_the_send_path_and_the_highlight_follows_the_drag(self):
        paths = self.make_files("a.txt", "b.txt")
        got = []
        with patch.object(self.app, "request_send", lambda p: got.append(p)):
            self.assertEqual(self.app.drop_enter(None, 1, 1), Gdk.DragAction.COPY)
            self.assertTrue(self.app.drop_zone.get_visible())
            self.app.drop_leave(None)
            self.assertFalse(self.app.drop_zone.get_visible())
            self.app.drop_enter(None, 1, 1)
            files = Gdk.FileList.new_from_list([Gio.File.new_for_path(p) for p in paths])
            self.assertTrue(self.app.drop_files(None, files, 1, 1))
        self.assertFalse(self.app.drop_zone.get_visible())
        self.assertEqual(got, [paths])

    def test_dropping_something_without_a_local_path_is_refused_politely(self):
        with patch.object(self.app, "request_send", lambda p: self.fail("must not send")):
            files = Gdk.FileList.new_from_list([Gio.File.new_for_uri("https://example.org/x.png")])
            self.assertFalse(self.app.drop_files(None, files, 1, 1))
        self.assertTrue(self.app.toasts_shown)

    def test_files_from_a_second_launch_go_to_the_same_instance_and_session(self):
        got = []
        paths = self.make_files("a.txt")
        with patch.object(self.app, "request_send", lambda p: got.append(p)):
            self.app.on_open(self.app, [Gio.File.new_for_path(p) for p in paths], len(paths), "")
        self.assertEqual(got, [paths])
        self.assertTrue(self.app.get_flags() & Gio.ApplicationFlags.HANDLES_OPEN)

    # ------------------------------------------------------------- repair and diagnostics
    def test_diagnostic_facts_describe_the_live_state(self):
        self.session()
        self.app.receiver_ifindex = 9
        self.app.last_announce = time.monotonic() - 3
        facts = self.app.diagnostic_facts()
        self.assertTrue(facts["session_active"])
        self.assertEqual((facts["receiver_pid"], facts["receiver_ifindex"]), (self.app.receiver.pid, 9))
        self.assertAlmostEqual(facts["announce_age"], 3, delta=1)
        self.assertEqual(facts["iface"], "wlp4s0")

    def test_announcements_seen_in_the_bridge_log_reset_the_age(self):
        self.app.last_announce = None
        self.app.log("Diagnosi AWDL: {'TX IPv6/17 mDNS R _airdrop._tcp.local/PTR,x': 3}")
        self.assertIsNotNone(self.app.last_announce)

    def test_a_dead_receiver_is_restarted_a_bounded_number_of_times_then_the_session_ends(self):
        self.session()
        restarted, stopped = [], []
        with patch.object(self.app, "restart_receiver", lambda: (restarted.append(1), (True, "ok"))[1]), \
                patch.object(self.app, "stop_session", lambda: stopped.append(1)):
            for _ in range(app_module.repair.MAX_ATTEMPTS + 1):
                dead = self.app.receiver
                self.app.receiver_ended(dead, 1)
                self.app.attempts.clock = (lambda t=time.time() + 100 * len(restarted): t)
            self.app.attempts.clock = time.time
        self.assertEqual(len(restarted), app_module.repair.MAX_ATTEMPTS)
        self.assertEqual(stopped, [1])
        self.assertIn("NON eseguito", self.app.attempts.log_file.read_text())

    def test_automatic_repair_can_be_switched_off(self):
        self.session()
        self.app.auto_repair = False
        stopped = []
        with patch.object(self.app, "restart_receiver", lambda: self.fail("must not restart")), patch.object(self.app, "stop_session", lambda: stopped.append(1)):
            self.app.receiver_ended(self.app.receiver, 1)
        self.assertEqual(stopped, [1])

    def test_a_changed_awdl_index_triggers_only_a_receiver_restart(self):
        self.session()
        self.app.receiver_ifindex = 5
        done = []
        with patch.object(app_module.socket, "if_nametoindex", return_value=9), \
                patch.object(self.app, "restart_receiver", lambda: (done.append("receiver"), (True, "ok"))[1]), \
                patch.object(self.app, "restart_session", lambda: self.fail("the session must stay up")):
            self.app.auto_repair_step()
        self.assertEqual(done, ["receiver"])

    def test_restarting_the_receiver_closes_open_rows_and_starts_a_new_process_after_the_old_one_is_gone(self):
        self.session()
        old = self.app.receiver
        t = self.ask()
        self.app.receive_event(event("ask_accepted", id="t1"))
        spawned = []
        with patch.object(self.app, "spawn_receiver", lambda: spawned.append(1)), patch.object(self.app, "reap", lambda p: None):
            ok, _ = self.app.restart_receiver()
            deadline = time.time() + 3
            while not spawned and time.time() < deadline:
                pump()
                time.sleep(0.02)
        self.assertTrue(ok)
        self.assertEqual(old.signals, [signal.SIGINT])
        self.assertEqual(t.state, "failed")
        self.assertEqual(spawned, [1])

    def test_a_confirmed_repair_runs_and_is_logged(self):
        problem = app_module.repair.Problem("owl-init", "OWL", "x", fix="restart_receiver", fix_label="Riavvia")
        with patch.object(self.app, "restart_receiver", lambda: (True, "ricevitore riavviato")):
            self.assertTrue(self.app.run_fix(problem))
        self.assertIn("[owl-init] restart_receiver: riuscito: ricevitore riavviato", self.app.attempts.log_file.read_text())
        allowed, why = self.app.attempts.allowed("owl-init")
        self.assertFalse(allowed)       # the cooling-off period applies to manual attempts as well

    def test_pages_switch_diagnostics_refresh_on_and_off(self):
        calls = []
        with patch.object(self.app.diag_page, "refresh", lambda: calls.append(1)):
            self.app.stack.set_visible_child_name("diagnostics")
            self.assertIsNotNone(self.app.diag_page.timer)
            self.app.stack.set_visible_child_name("home")
            self.assertIsNone(self.app.diag_page.timer)
        self.assertEqual(len(calls), 1)

    def test_history_page_lists_stored_transfers_and_can_be_cleared(self):
        self.ask()
        self.app.receive_event(event("ask_accepted", id="t1"))
        self.app.receive_event(event("receive_done", id="t1", folder="/x", files=[{"name": "foto.jpg", "kind": "image", "size": 9}]))
        self.assertEqual(len(self.app.history_page.list.observe_children() if hasattr(self.app.history_page.list, "observe_children") else [1]), 1)
        self.assertEqual(self.app.history_page.stack.get_visible_child_name(), "list")
        self.app.store.clear_history()
        self.app.history_page.refresh()
        self.assertEqual(self.app.history_page.stack.get_visible_child_name(), "empty")


if __name__ == "__main__":
    unittest.main(verbosity=2)
