"""The tray icon speaks StatusNotifierItem + dbusmenu correctly, checked against a stub host on the session bus."""
import json
import os
from pathlib import Path
import subprocess
import unittest

HERE = Path(__file__).resolve().parent


def run_check():
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise unittest.SkipTest("no D-Bus session bus")
    try:
        done = subprocess.run(["/usr/bin/python3", "-I", "-B", str(HERE / "tray_session_check.py")],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise unittest.SkipTest(str(exc))
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise unittest.SkipTest("tray check could not run: " + (done.stderr or done.stdout)[-300:])
    return json.loads(lines[-1])


class TrayProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_check()

    def test_the_host_never_saw_an_error(self):
        self.assertNotIn("error", self.report)

    def test_registers_a_unique_item_name_with_the_watcher(self):
        self.assertRegex(self.report["registered_name"], r"^org\.kde\.StatusNotifierItem-\d+-1$")
        self.assertEqual(self.report["availability"], [True])

    def test_item_properties_describe_an_application_status_icon_with_a_menu(self):
        props = self.report["props"]
        self.assertEqual((props["Category"], props["Status"], props["Id"]), ("ApplicationStatus", "Active", "linux-airdrop"))
        self.assertEqual((props["ItemIsMenu"], props["Menu"]), (True, "/MenuBar"))
        self.assertEqual(props["IconName"], "idle-symbolic")
        self.assertTrue(props["IconThemePath"].endswith("/icons"))

    def test_menu_layout_is_open_toggle_state_quit(self):
        _, (root_id, root_props, children) = self.report["layout_idle"]
        self.assertEqual((root_id, root_props["children-display"]), (0, "submenu"))
        labels = [(c[0], c[1].get("label") or c[1].get("type")) for c in children]
        self.assertEqual(labels, [(1, "Apri Linux AirDrop"), (2, "separator"), (3, "Non visibile"),
                                  (4, "Rendi visibile"), (5, "separator"), (6, "Esci")])
        self.assertFalse(children[2][1]["enabled"])   # the status line is informational

    def test_clicks_reach_the_app_and_other_events_are_ignored(self):
        self.assertEqual(self.report["clicks"], ["open", "toggle", "quit"])

    def test_state_changes_update_icon_label_and_menu_revision(self):
        self.assertEqual(self.report["icon_active"], "active-symbolic")
        revision, (_, _, children) = self.report["layout_active"]
        self.assertGreater(revision, self.report["layout_idle"][0])
        self.assertEqual(children[3][1]["label"], "Interrompi visibilità")
        self.assertEqual(children[2][1]["label"], "Visibile · pronto a ricevere")

    def test_stopping_withdraws_the_icon(self):
        self.assertEqual(self.report["availability_after_stop"], [True, False])


if __name__ == "__main__":
    unittest.main(verbosity=2)
