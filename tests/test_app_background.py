"""Window and background behaviour of the GTK app, with a fake radio bridge (no root, no Wi-Fi)."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))   # the app imports its sibling modules
sys.argv = ["app.py"]
spec = importlib.util.spec_from_file_location("app_under_test", ROOT / "app.py")
app_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app_module)
from gi.repository import Gdk, GLib  # noqa: E402  (after the module selected GTK 4)


class FakeStdin:
    def __init__(self):
        self.sent = []

    def write(self, text):
        self.sent.append(text)

    def flush(self):
        pass


class FakeBridge:
    def __init__(self):
        self.stdin = FakeStdin()

    def poll(self):
        return None

    def terminate(self):
        pass


class FakeTray:
    def __init__(self, available=True):
        self.available = available
        self.states = []

    def set_state(self, visible, busy, text):
        self.states.append((visible, busy, text))

    def stop(self):
        pass


class FakeProcess(FakeBridge):
    """A bridge process whose output ends immediately; records how it was started."""
    def __init__(self, argv):
        super().__init__()
        self.argv = argv
        self.stdout = iter(())

    def wait(self):
        return 0


class RecordingApp(app_module.AirDrop):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.notices, self.withdrawn, self.quit_calls = [], [], 0

    def send_notification(self, ident, notification):
        buttons = [notification.get_buttons_count() if hasattr(notification, "get_buttons_count") else None]
        self.notices.append((ident, buttons))

    def withdraw_notification(self, ident):
        self.withdrawn.append(ident)

    def quit(self):
        self.quit_calls += 1


@unittest.skipIf(Gdk.Display.get_default() is None, "no display available")
class BackgroundBehaviour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # One GApplication per process: a second one would try to export the same D-Bus object path.
        cls.tmp = tempfile.TemporaryDirectory()
        cls.base = Path(cls.tmp.name)
        app_module.SETTINGS = cls.base / "config/settings.json"
        app_module.STATE = cls.base / "state"
        cls.app = RecordingApp(screenshot=cls.base / "unused.png")
        cls.app.register(None)
        cls.app.build_window()

    @classmethod
    def tearDownClass(cls):
        cls.app.window.destroy()
        cls.tmp.cleanup()

    def setUp(self):
        app = self.app
        app.destination = self.base / ("downloads-" + self.id().rsplit(".", 1)[-1])
        app.window.set_visible(True)
        app.bridge, app.active, app.closing = None, False, False
        app.background_row.set_active(True)
        app.known_files = set()
        app.log_lines = []
        app.notices, app.withdrawn, app.quit_calls = [], [], 0
        app.tray, app.user_stop, app.end_reason, app.stopping = None, False, None, False
        app.toggle.set_sensitive(True)
        app.started_at = None
        app.auto_visible, app.duration, app.helper = False, "25m", "missing"
        if app.held:
            app.release()
            app.held = False
        if app.resident:
            app.release()
            app.resident = False

    def start_fake_session(self):
        self.app.bridge = FakeBridge()
        self.app.active = True
        self.app.held = False
        self.app.hold()
        self.app.held = True

    def end_session(self, code=0):
        bridge = self.app.bridge
        self.app.bridge_ended(bridge, code)

    def test_closing_without_a_session_just_closes(self):
        self.assertFalse(self.app.on_close_request())

    def test_closing_during_a_session_keeps_it_running_in_background(self):
        self.start_fake_session()
        self.assertTrue(self.app.on_close_request())
        self.assertFalse(self.app.window.get_visible())
        self.assertIsNotNone(self.app.bridge)
        self.assertEqual(self.app.bridge.stdin.sent, [])
        self.assertEqual([n[0] for n in self.app.notices], ["session"])
        self.assertEqual(self.app.quit_calls, 0)

    def test_launching_again_brings_the_window_back_with_state_intact(self):
        self.start_fake_session()
        self.app.on_close_request()
        self.app.act_show()
        self.assertTrue(self.app.window.get_visible())
        self.assertTrue(self.app.active)

    def test_background_can_be_turned_off_so_closing_stops_the_session(self):
        self.start_fake_session()
        self.app.background_row.set_active(False)
        self.assertTrue(self.app.on_close_request())
        self.assertTrue(self.app.closing)
        self.assertEqual(self.app.bridge.stdin.sent, ["stop\n"])
        self.end_session()
        self.assertEqual(self.app.quit_calls, 1)

    def test_session_ending_while_hidden_notifies_and_exits(self):
        self.start_fake_session()
        self.app.on_close_request()
        self.end_session()
        self.assertIn("session", self.app.withdrawn)
        self.assertEqual(self.app.notices[-1][0], "ended")
        self.assertEqual(self.app.quit_calls, 1)

    def test_visibility_can_be_started_again_after_the_card_is_restored(self):
        # Regression: stop_session() disabled the button and only a 1-second timer re-enabled it, long before
        # the Wi-Fi restore finished, so after "Interrompi" the app could not be made visible again.
        self.start_fake_session()
        self.app.stop_session()
        self.assertFalse(self.app.toggle.get_sensitive())      # nothing may be started while restoring
        self.assertFalse(self.app.unlock_toggle() or self.app.toggle.get_sensitive())   # the old timer must not unlock it early
        self.end_session()
        self.assertTrue(self.app.toggle.get_sensitive())
        self.assertFalse(self.app.spinner.get_visible())
        started = []
        with patch.object(self.app, "toggle_session", lambda *_: started.append(1)):
            self.app.tray = FakeTray()
            self.app.tray_toggle()
        self.assertEqual(started, [1])

    def test_session_ending_with_the_window_open_keeps_the_app_open(self):
        self.start_fake_session()
        self.end_session()
        self.assertEqual(self.app.quit_calls, 0)
        self.assertEqual(self.app.toggle.get_label(), "Rendi visibile")

    def test_explicit_quit_stops_the_radio_first(self):
        self.start_fake_session()
        self.app.act_quit()
        self.assertEqual(self.app.bridge.stdin.sent, ["stop\n"])
        self.assertEqual(self.app.quit_calls, 0)
        self.end_session()
        self.assertEqual(self.app.quit_calls, 1)

    def test_quit_without_a_session_exits_immediately(self):
        self.app.act_quit()
        self.assertEqual(self.app.quit_calls, 1)

    def test_stop_from_the_notification_works_while_hidden_and_the_app_then_exits(self):
        # Regression: stop_session() clears `active` before the bridge ends, which left a windowless app running.
        self.start_fake_session()
        self.app.on_close_request()
        self.app.act_stop()
        self.assertEqual(self.app.bridge.stdin.sent, ["stop\n"])
        self.assertFalse(self.app.active)
        self.end_session()
        self.assertNotIn("ended", [n[0] for n in self.app.notices])   # the user asked for it: no need to announce it
        self.assertEqual(self.app.quit_calls, 1)

    def test_failed_activation_while_hidden_does_not_leave_a_windowless_app(self):
        self.app.bridge = FakeBridge()
        self.app.held = False
        self.app.hold()
        self.app.held = True
        self.app.window.set_visible(False)
        self.app.last_error = "Autenticazione negata"
        self.end_session(code=1)
        self.assertEqual(self.app.quit_calls, 1)
        self.assertEqual(self.app.notices[-1][0], "ended")

    def test_incoming_request_notifies_only_when_hidden(self):
        self.start_fake_session()
        self.app.incoming_request()
        self.assertEqual(self.app.notices, [])
        self.app.window.set_visible(False)
        self.app.incoming_request()
        self.assertEqual([n[0] for n in self.app.notices], ["incoming"])

    def test_received_file_notifies_when_hidden_and_toasts_when_visible(self):
        self.start_fake_session()
        self.app.destination.mkdir(parents=True)
        (self.app.destination / "AirDrop-1").mkdir()
        self.app.tick()
        self.assertEqual(self.app.notices, [])
        self.app.window.set_visible(False)
        (self.app.destination / "AirDrop-2").mkdir()
        self.app.tick()
        self.assertEqual([n[0] for n in self.app.notices], ["received"])

    # ------------------------------------------------------------------ tray icon
    def test_with_a_tray_icon_closing_the_window_keeps_the_app_alive_even_without_a_session(self):
        self.app.tray = FakeTray()
        self.assertTrue(self.app.on_close_request())
        self.assertFalse(self.app.window.get_visible())
        self.assertTrue(self.app.resident)
        self.assertEqual(self.app.quit_calls, 0)

    def test_without_the_background_option_closing_quits_even_with_a_tray(self):
        self.app.tray = FakeTray()
        self.app.background_row.set_active(False)
        self.assertTrue(self.app.on_close_request())
        self.assertEqual(self.app.quit_calls, 1)

    def test_a_tray_that_is_not_available_does_not_keep_an_invisible_app_alive(self):
        self.app.tray = FakeTray(available=False)
        self.assertFalse(self.app.on_close_request())

    def test_session_ending_while_hidden_with_a_tray_keeps_running_and_announces_only_surprises(self):
        self.app.tray = FakeTray()
        self.start_fake_session()
        self.app.on_close_request()
        self.app.end_reason = "La visibilità è terminata dopo 25 minuti."
        self.end_session()
        self.assertEqual(self.app.quit_calls, 0)
        self.assertEqual(self.app.notices[-1][0], "ended")

    def test_stopping_from_the_tray_is_silent_and_the_app_stays(self):
        self.app.tray = FakeTray()
        self.start_fake_session()
        self.app.on_close_request()
        self.app.notices.clear()
        self.app.tray_toggle()
        self.assertEqual(self.app.bridge.stdin.sent, ["stop\n"])
        self.assertTrue(self.app.stopping)
        self.end_session()
        self.assertEqual(self.app.notices, [])
        self.assertEqual(self.app.quit_calls, 0)
        self.assertFalse(self.app.stopping)

    def test_tray_reflects_activation_visibility_and_restore(self):
        self.app.tray = FakeTray()
        self.start_fake_session()
        self.app.set_toggle("x", None)
        self.assertEqual(self.app.tray.states[-1], (True, False, "Visibile · pronto a ricevere"))
        self.app.stop_session()
        self.assertEqual(self.app.tray.states[-1], (False, True, "Ripristino del Wi-Fi…"))
        self.end_session()
        self.assertEqual(self.app.tray.states[-1], (False, False, "Non visibile"))

    def test_losing_the_tray_while_hidden_and_idle_brings_the_window_back(self):
        self.app.window.set_visible(False)
        self.app.tray_changed(False)
        self.assertTrue(self.app.window.get_visible())

    # ------------------------------------------------- start without password, duration
    def start_with(self, helper, duration):
        self.app.helper, self.app.duration = helper, duration
        started = []

        def popen(argv, **kwargs):
            started.append(argv)
            return FakeProcess(argv)

        with patch.object(app_module, "radio_capability", return_value=True), \
                patch.object(app_module.system_helper, "status", return_value=helper), \
                patch.object(app_module.subprocess, "Popen", popen):
            self.app.toggle_session()
        self.app.bridge = None   # the fake never speaks; do not leave it for the next test
        return started[0]

    def test_a_configured_helper_is_started_through_its_root_owned_entry_point(self):
        argv = self.start_with("ready", "25m")
        self.assertEqual(argv[:2], ["pkexec", "/usr/local/lib/linux-airdrop/airdrop-bridge"])
        self.assertNotIn("python3", " ".join(argv))

    def test_native_driver_requires_wifi_direct_before_starting_a_bridge(self):
        old_channel, old_go = self.app.channel.get_selected(), self.app.go_wanted
        try:
            for selected, enabled in ((0, False), (1, False)):
                with self.subTest(channel=selected, wifi_direct=enabled):
                    self.app.channel.set_selected(selected)
                    self.app.go_row.set_active(enabled)
                    with patch.object(app_module, "radio_capability", return_value=True), \
                            patch.object(app_module.subprocess, "Popen") as spawn:
                        self.app.toggle_session()
                    spawn.assert_not_called()
                    self.assertIsNone(self.app.bridge)
                    self.assertIn("Wi-Fi Direct", self.app.status.get_label())
        finally:
            self.app.go_wanted = old_go
            self.app.channel.set_selected(old_channel)
            self.app.channel_changed()

    def test_patched_driver_still_allows_monitor_only_mode(self):
        old_go = self.app.go_row.get_active()
        started = []
        try:
            self.app.go_row.set_active(False)
            with patch.object(app_module, "radio_capability", return_value=False), \
                    patch.object(self.app, "compat_capabilities", app_module.radio_compat.RadioCapabilities(
                        "mt7921e", True, False, True, pci_id="0x0616")), \
                    patch.object(self.app, "compat_legacy", app_module.radio_compat.LegacyBundle(verified=True)), \
                    patch.object(app_module, "driver_trial_available", return_value=True), \
                    patch.object(app_module.system_helper, "status", return_value="ready"), \
                    patch.object(app_module.subprocess, "Popen", lambda argv, **kwargs: started.append(argv) or FakeProcess(argv)):
                self.app.toggle_session()
            self.assertIn("--driver-trial", started[0])
            self.assertNotIn("--go-anchor", started[0])
        finally:
            self.app.bridge = None
            self.app.go_row.set_active(old_go)

    def test_nexaawdl_arguments_follow_the_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = (self.app.nexa_dir, self.app.nexa_status_path, self.app.nexa_enabled, self.app.nexa_level)
            self.app.nexa_dir = Path(tmp) / "nexaawdl"
            self.app.nexa_status_path = self.app.nexa_dir / "status.json"
            try:
                self.app.nexa_enabled, self.app.nexa_level = True, "debug"
                self.app.nexa_dir.mkdir()
                self.app.nexa_status_path.write_text("{}")            # left by an earlier session
                argv = self.app.nexa_arguments()
                self.assertEqual(argv, ["--nexaawdl-dir", str(self.app.nexa_dir), "--nexaawdl-code",
                                        str(app_module.BASE), "--nexaawdl-level", "debug"])
                self.assertEqual(self.app.nexa_dir.stat().st_mode & 0o777, 0o700)
                self.assertFalse(self.app.nexa_status_path.exists())
                started = self.start_with("ready", "25m")
                self.assertIn("--nexaawdl-dir", started)
                self.app.nexa_enabled = False
                self.assertEqual(self.app.nexa_arguments(), [])
                self.assertNotIn("--nexaawdl-dir", self.start_with("ready", "25m"))
            finally:
                self.app.nexa_dir, self.app.nexa_status_path, self.app.nexa_enabled, self.app.nexa_level = old

    def test_native_go_can_select_a_five_ghz_channel_and_pass_it_to_the_bridge(self):
        old_channel = self.app.channel.get_selected()
        try:
            self.app.channel.set_selected(1)
            self.app.go_row.set_active(True)
            argv = self.start_with("ready", "25m")
            self.assertIn("--go-anchor", argv)
            self.assertEqual(argv[argv.index("--channel") + 1], "44")
        finally:
            self.app.channel.set_selected(old_channel)

    def test_without_the_helper_the_old_password_path_is_used(self):
        argv = self.start_with("missing", "25m")
        self.assertEqual(argv[:4], ["pkexec", "/usr/bin/python3", "-I", "-B"])
        self.assertTrue(argv[4].endswith("root_bridge.py"))

    def test_other_helper_states_never_use_the_system_entry_point(self):
        for state in ("outdated", "unsafe", "other-user"):
            self.assertNotIn("/usr/local/lib/linux-airdrop/airdrop-bridge", self.start_with(state, "25m"), state)

    def test_radio_acknowledgements_are_requested_by_default_and_can_be_switched_off(self):
        # Without them every frame to the iPhone is sent once; uploads stalled after a few hundred kB (docs/SENDING.md).
        self.app.ack_row.set_active(True)
        self.assertIn("--fixed-ack", self.start_with("ready", "25m"))
        self.app.ack_row.set_active(False)
        self.assertNotIn("--fixed-ack", self.start_with("ready", "25m"))
        self.assertFalse(app_module.load_settings()["radio_ack"])
        self.app.ack_row.set_active(True)
        self.assertTrue(app_module.load_settings()["radio_ack"])

    def test_session_length_is_passed_to_the_bridge_with_a_margin(self):
        for duration, expected in (("25m", "1800"), ("2h", "7500"), ("always", "0")):
            argv = self.start_with("ready", duration)
            self.assertEqual(argv[argv.index("--max-seconds") + 1], expected, duration)

    def test_the_app_ends_a_timed_session_and_explains_why(self):
        self.start_fake_session()
        self.app.started_at = time.monotonic() - 1501
        self.app.destination.mkdir(parents=True, exist_ok=True)
        self.app.tick()
        self.assertEqual(self.app.bridge.stdin.sent, ["stop\n"])
        self.assertIn("25 minuti", self.app.end_reason)

    def test_an_unlimited_session_is_never_ended_by_the_app(self):
        self.app.duration = "always"
        self.start_fake_session()
        self.app.started_at = time.monotonic() - 10 ** 6
        self.app.destination.mkdir(parents=True, exist_ok=True)
        self.app.tick()
        self.assertEqual(self.app.bridge.stdin.sent, [])

    def test_visible_at_start_needs_the_helper(self):
        calls = []
        with patch.object(self.app, "toggle_session", lambda *_: calls.append(1)), \
                patch.object(app_module.system_helper, "status", return_value="missing"):
            self.app.auto_activate()
        self.assertEqual(calls, [])
        with patch.object(self.app, "toggle_session", lambda *_: calls.append(1)), \
                patch.object(app_module.system_helper, "status", return_value="ready"):
            self.app.auto_activate()
        self.assertEqual(calls, [1])

    def test_receiver_announces_often_enough_to_hit_the_phones_short_window(self):
        env = self.app.backend_env()
        self.assertEqual(float(env["OPENDROP_ANNOUNCE_INTERVAL"]), 0.5)
        self.assertTrue(env["AIRDROP_CONFIRM"].endswith("confirm.py"))   # asking before every receive is untouched

    def test_log_and_about_dialogs_open_without_error(self):
        self.app.log("prova")
        self.app.show_log()
        self.app.show_about()
        GLib.MainContext.default().iteration(False)
        self.assertEqual(self.app.log_buffer.get_char_count(), len("prova"))


class SettingsFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original = app_module.SETTINGS
        app_module.SETTINGS = Path(self.tmp.name) / "linux-airdrop/settings.json"
        self.addCleanup(setattr, app_module, "SETTINGS", self.original)

    def test_round_trip(self):
        values = {"name": "Studio", "channel": 44, "wifi_direct": False, "background": False,
                  "auto_visible": True, "duration": "always", "remember": False, "auto_repair": False,
                  "radio_ack": False, "nexa_enabled": False, "nexa_level": "debug"}
        app_module.save_settings(values)
        self.assertEqual(app_module.load_settings(), values)

    def test_missing_corrupt_or_hostile_files_fall_back_to_defaults(self):
        defaults = {"name": "", "channel": 6, "wifi_direct": True, "background": True,
                    "auto_visible": False, "duration": "25m", "remember": True, "auto_repair": True,
                    "radio_ack": True, "nexa_enabled": True, "nexa_level": "info"}
        self.assertEqual(app_module.load_settings(), defaults)
        app_module.SETTINGS.parent.mkdir(parents=True)
        for text in ("not json", "[1, 2]", json.dumps({"name": 7, "channel": 99, "wifi_direct": "yes", "background": None,
                                                    "auto_visible": 1, "duration": "forever", "remember": "no",
                                                    "auto_repair": 0, "radio_ack": "sì",
                                                    "nexa_enabled": "yes", "nexa_level": "everything"})):
            app_module.SETTINGS.write_text(text)
            self.assertEqual(app_module.load_settings(), defaults)

    def test_nexaawdl_settings_are_validated(self):
        app_module.SETTINGS.parent.mkdir(parents=True)
        app_module.SETTINGS.write_text(json.dumps({"nexa_enabled": False, "nexa_level": "warning"}))
        loaded = app_module.load_settings()
        self.assertEqual((loaded["nexa_enabled"], loaded["nexa_level"]), (False, "warning"))

    def test_name_is_trimmed_and_bounded(self):
        app_module.SETTINGS.parent.mkdir(parents=True)
        app_module.SETTINGS.write_text(json.dumps({"name": "  " + "x" * 200 + "  "}))
        self.assertEqual(len(app_module.load_settings()["name"]), 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
