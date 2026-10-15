"""A stalled kernel must not trigger another radio request from the GUI."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # the app imports its sibling modules
spec = importlib.util.spec_from_file_location("airdrop_app_recovery", Path(__file__).resolve().parents[1] / "app.py")
app = importlib.util.module_from_spec(spec)
spec.loader.exec_module(app)


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        state_patch = patch.object(app, "STATE", self.state)
        state_patch.start()
        self.addCleanup(state_patch.stop)

    def test_current_boot_marker_blocks_radio_probe_and_activation(self):
        app.require_recovery("Kernel blocked")
        self.assertTrue(app.recovery_needed())
        with patch.object(app.subprocess, "Popen") as spawn:
            self.assertFalse(app.radio_capability("wlp4s0"))
            spawn.assert_not_called()
        window = Mock()
        app.AirDrop.toggle_session(window)
        window.set_status.assert_called_once()

    def test_reboot_releases_guard(self):
        (self.state / "recovery-required.json").write_text(json.dumps({"boot_id": "an-older-boot"}))
        self.assertFalse(app.recovery_needed())

    def test_missing_or_malformed_marker_does_not_request_a_reboot(self):
        self.assertFalse(app.recovery_needed())
        (self.state / "recovery-required.json").write_text("incomplete")
        self.assertFalse(app.recovery_needed())


if __name__ == "__main__":
    unittest.main()
