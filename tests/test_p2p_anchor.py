"""Exercise owned GO lifetime and rollback with no live radio or root actions."""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

MODULE = Path(__file__).resolve().parents[1] / "p2p_anchor.py"
spec = importlib.util.spec_from_file_location("p2p_anchor_under_test", MODULE)
anchor_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(anchor_module)


class FakeProcess:
    def __init__(self, world):
        self.world = world
        self.returncode = None
        self.terminated = self.killed = False
        self.hung = False
        self.remove_group = True
        self.waits = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if not self.hung:
            self.returncode = -15
            if self.remove_group:
                self.world.devices.pop("radio-go", None)

    def kill(self):
        self.killed = True
        if not self.hung:
            self.returncode = -9

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.hung:
            raise subprocess.TimeoutExpired("owned-supplicant", timeout)
        return self.returncode


class FakeWorld:
    def __init__(self):
        self.devices = {
            "wlan0": {"phy": "phy0", "name": "wlan0", "type": "managed", "mac": "02:00:00:00:00:01", "wdev": "0x1"},
            "p2p-wlan0-foreign": {"phy": "phy1", "name": "foreign-go", "type": "P2P-GO", "mac": "02:00:00:00:00:02", "wdev": "0x2"},
        }
        self.commands, self.events, self.spawns = [], [], []
        self.process = None
        self.create_group = True
        self.add_output = "OK"
        self.status = "mode=P2P GO\naddress=02:00:00:00:00:03\nfreq=2437"
        self.status_sequence = []
        self.fail_spawn = self.fail_nm = self.no_capability = False
        # What `nmcli -g GENERAL.NM-MANAGED device show <group>` prints. Real
        # NetworkManager lists no P2P-GO netdev at all, so the output is empty.
        self.nm_state = ""
        self.fail_nm_show = False
        self.now = 0
        self.frequencies = ("\tFrequencies:\n"
                            "\t\t* 2437 MHz [6] (20.0 dBm)\n"
                            "\t\t* 5180 MHz [36] (23.0 dBm)\n"
                            "\t\t* 5220 MHz [44] (23.0 dBm)\n")

    def monotonic(self):
        self.now += 0.1
        return self.now

    def inventory(self):
        result = []
        for item in self.devices.values():
            result.extend(["phy#" + item["phy"][3:], "\tInterface " + item["name"],
                           "\t\twdev " + item["wdev"], "\t\taddr " + item["mac"],
                           "\t\ttype " + item["type"]])
        return "\n".join(result)

    def command(self, args, check=True):
        args = tuple(str(a) for a in args)
        self.commands.append(args)
        if args == ("iw", "dev"):
            return self.inventory()
        if args == ("iw", "phy", "phy0", "info"):
            return ("Supported interface modes:\n\t* managed\n" +
                    ("" if self.no_capability else "\t* P2P-GO\n") + self.frequencies)
        if args[0].endswith("wpa_cli"):
            if args[-1] == "ping":
                return "PONG"
            if "p2p_group_add" in args:
                if self.create_group and self.add_output == "OK":
                    self.devices["radio-go"] = {"phy": "phy0", "name": "radio-go", "type": "P2P-GO", "mac": "02:00:00:00:00:03", "wdev": "0x5"}
                return self.add_output
            if args[-1] == "status":
                if self.status_sequence:
                    return self.status_sequence.pop(0)
                return self.status
        if args == ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "radio-go"):
            if self.fail_nm_show:
                raise RuntimeError("Injected NM show failure")
            return self.nm_state
        if args[:4] == ("nmcli", "device", "set", "radio-go") and self.fail_nm:
            raise RuntimeError("Injected NM failure")
        if args[:2] == ("iw", "dev") and args[-1] == "del":
            self.devices.pop(args[2], None)
        return ""

    def popen(self, args, **kwargs):
        self.spawns.append((args, kwargs))
        if self.fail_spawn:
            raise OSError("Injected spawn failure")
        config = Path(args[args.index("-c") + 1])
        (config.parent / "control" / "wlan0").touch()
        self.process = FakeProcess(self)
        return self.process


class GoAnchorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.world = FakeWorld()
        self.patches = [
            patch.object(anchor_module, "RUNTIME_BASES", (self.tmp.name,)),
            patch.object(anchor_module, "GLOBAL_CONTROL", self.base / "system-control"),
            patch.object(anchor_module, "IPV6_CONF", self.base / "ipv6-conf"),
            patch.object(anchor_module.os, "geteuid", return_value=0),
            patch.object(anchor_module.shutil, "which", side_effect=lambda name, **_: "/usr/sbin/" + name),
            patch.object(anchor_module.subprocess, "Popen", self.world.popen),
            patch.object(anchor_module.time, "sleep", lambda _: None),
            patch.object(anchor_module.time, "monotonic", self.world.monotonic),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.anchor = anchor_module.GoAnchor("wlan0", "phy0", 6, self.world.command,
                                           lambda event, **data: self.world.events.append((event, data)))

    def mutations(self):
        return [args for args in self.world.commands if args[:2] in (("ip", "link"), ("nmcli", "device"))
                or args[-1:] == ("del",) or "p2p_group_add" in args]

    def test_start_private_foreground_daemon_and_return_group_mac(self):
        self.assertEqual(self.anchor.start(), ("radio-go", "02:00:00:00:00:03"))
        self.assertTrue(self.anchor.alive())
        runtime = self.anchor.runtime
        self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
        self.assertEqual((runtime / "supplicant.conf").stat().st_mode & 0o777, 0o600)
        self.assertEqual((runtime / "supplicant.log").stat().st_mode & 0o777, 0o600)
        config = (runtime / "supplicant.conf").read_text()
        self.assertIn("ctrl_interface=" + str(runtime / "control"), config)
        self.assertNotIn("psk", config)
        argv, options = self.world.spawns[0]
        self.assertNotIn("-B", argv)
        self.assertNotIn("-u", argv)
        self.assertNotIn("-P", argv)
        self.assertTrue(options["close_fds"])
        self.assertEqual(options["stdin"], subprocess.DEVNULL)
        self.assertNotEqual(options["stdout"], subprocess.PIPE)
        self.assertTrue(any("freq=2437" in args for args in self.world.commands))
        self.assertTrue(self.anchor.cleanup())
        self.assertFalse(runtime.exists())

    def test_cleanup_is_idempotent_and_preserves_other_phy(self):
        self.anchor.start()
        process = self.world.process
        self.assertTrue(self.anchor.cleanup())
        after = list(self.world.commands)
        self.assertTrue(self.anchor.cleanup())
        self.assertEqual(after, self.world.commands)
        self.assertTrue(process.terminated)
        self.assertIn("p2p-wlan0-foreign", self.world.devices)
        self.assertFalse(any("foreign-go" in args and args[-1] == "del" for args in self.world.commands))

    def make_anchor(self, channel):
        return anchor_module.GoAnchor("wlan0", "phy0", channel, self.world.command,
                                      lambda event, **data: self.world.events.append((event, data)))

    def test_allowed_5ghz_channels_use_the_requested_frequency_and_keep_ownership_checks(self):
        for channel, frequency in ((36, 5180), (44, 5220)):
            with self.subTest(channel=channel):
                anchor = self.make_anchor(channel)
                self.world.status = f"mode=P2P GO\naddress=02:00:00:00:00:03\nfreq={frequency}"
                self.assertEqual(anchor.start(), ("radio-go", "02:00:00:00:00:03"))
                self.assertTrue(anchor.alive())
                self.assertTrue(any("p2p_group_add" in args and f"freq={frequency}" in args for args in self.world.commands))
                self.assertTrue(any(event == "status" and f"canale {channel}" in data["message"]
                                    for event, data in self.world.events))
                self.assertTrue(anchor.cleanup())
                self.assertIn("p2p-wlan0-foreign", self.world.devices)

    def test_5ghz_status_on_wrong_frequency_is_rejected_and_daemon_stopped(self):
        anchor = self.make_anchor(36)
        self.world.status = "mode=P2P GO\naddress=02:00:00:00:00:03\nfreq=5220"
        with self.assertRaisesRegex(RuntimeError, "canale diverso"):
            anchor.start()
        self.assertTrue(self.world.process.terminated)
        self.assertIsNone(anchor.runtime)

    def test_disabled_no_ir_and_radar_channels_are_refused_before_mutation(self):
        for restriction in ("disabled", "no IR", "NO-IR", "NO_IR", "radar detection", "DFS", "passive scan", "no IBSS"):
            with self.subTest(restriction=restriction):
                self.world.frequencies = f"\tFrequencies:\n\t\t* 5220 MHz [44] (23.0 dBm) ({restriction})\n"
                anchor = self.make_anchor(44)
                with self.assertRaisesRegex(RuntimeError, "restrizioni"):
                    anchor.start()
                self.assertEqual(self.mutations(), [])
                self.assertEqual(self.world.spawns, [])
                self.assertIsNone(anchor.runtime)

    def test_dfs_continuation_rejects_the_channel_even_when_dfs_is_available(self):
        self.world.frequencies = ("\tFrequencies:\n\t\t* 5220 MHz [44] (23.0 dBm)\n"
                                  "\t\t\tDFS state: available (for 100 sec)\n"
                                  "\t\t\tDFS CAC time: 60000 ms\n")
        with self.assertRaisesRegex(RuntimeError, "restrizioni"):
            self.make_anchor(44).start()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.spawns, [])

    def test_missing_or_mislabeled_frequency_is_refused_before_mutation(self):
        for frequencies in ("", "\t* 2437 MHz [6] (20.0 dBm)\n", "\t* 5180 MHz [44] (23.0 dBm)\n"):
            with self.subTest(frequencies=frequencies):
                self.world.frequencies = frequencies
                with self.assertRaises(RuntimeError):
                    self.make_anchor(36).start()
                self.assertEqual(self.mutations(), [])
                self.assertEqual(self.world.spawns, [])

    def test_unrelated_dfs_frequency_does_not_restrict_the_selected_channel(self):
        self.world.frequencies = ("\tFrequencies:\n\t\t* 5220 MHz [44] (23.0 dBm)\n"
                                  "\t\t* 5260 MHz [52] (23.0 dBm) (radar detection)\n"
                                  "\t\t\tDFS state: usable\n")
        self.assertEqual(self.make_anchor(44).preflight()[0]["name"], "wlan0")
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.spawns, [])

    def test_later_section_does_not_become_part_of_the_frequency_record(self):
        self.world.frequencies = ("\tFrequencies:\n\t\t* 5220 MHz [44] (23.0 dBm)\n"
                                  "\tSupported commands:\n\t\t* radar detection\n")
        self.make_anchor(44).preflight()
        self.assertEqual(self.mutations(), [])

    def test_conflicting_duplicate_frequency_records_fail_closed(self):
        self.world.frequencies = ("\tFrequencies:\n\t\t* 5220 MHz [44] (23.0 dBm)\n"
                                  "\t\t* 5220 MHz [44] (disabled)\n")
        with self.assertRaisesRegex(RuntimeError, "restrizioni"):
            self.make_anchor(44).start()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.spawns, [])

    def test_preexisting_group_on_selected_phy_aborts_before_mutation(self):
        self.world.devices["existing"] = {"phy": "phy0", "name": "already-go", "type": "P2P-GO", "mac": "02:00:00:00:00:09", "wdev": "0x9"}
        with self.assertRaisesRegex(RuntimeError, "già attiva"):
            self.anchor.start()
        self.assertTrue(self.anchor.cleanup())
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.spawns, [])
        self.assertIn("existing", self.world.devices)

    def test_missing_binary_aborts_before_mutation(self):
        with patch.object(anchor_module.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "mancante"):
                self.anchor.preflight()
        self.assertEqual(self.mutations(), [])

    def test_unsupported_phy_aborts_before_mutation(self):
        self.world.no_capability = True
        with self.assertRaisesRegex(RuntimeError, "non supporta"):
            self.anchor.start()
        self.assertEqual(self.mutations(), [])

    def test_system_supplicant_control_is_never_used_or_stopped(self):
        control = self.base / "system-control"
        control.mkdir()
        (control / "wlan0").touch()
        with self.assertRaisesRegex(RuntimeError, "ancora controllata"):
            self.anchor.start()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.spawns, [])
        self.assertTrue((control / "wlan0").exists())

    def test_fail_reply_even_with_success_exit_rolls_back_daemon(self):
        self.world.add_output = "FAIL"
        with self.assertRaisesRegex(RuntimeError, "FAIL"):
            self.anchor.start()
        self.assertTrue(self.world.process.terminated)
        self.assertIsNone(self.anchor.runtime)
        self.assertTrue(self.anchor.cleanup())

    def test_failed_spawn_removes_private_runtime(self):
        self.world.fail_spawn = True
        with self.assertRaisesRegex(OSError, "spawn failure"):
            self.anchor.start()
        self.assertEqual(list(self.base.glob("linux-airdrop-p2p-*")), [])
        self.assertFalse(any(args[-1:] == ("del",) for args in self.world.commands))

    def test_missing_group_after_ok_stops_owned_daemon(self):
        self.world.create_group = False
        with self.assertRaisesRegex(RuntimeError, "non ha creato"):
            self.anchor.start()
        self.assertTrue(self.world.process.terminated)
        self.assertTrue(self.anchor.cleanup())

    def test_failure_after_group_identification_rolls_back(self):
        def failing_emit(event, **data):
            if event == "status":
                raise RuntimeError("Injected late failure")
        self.anchor.emit = failing_emit
        with self.assertRaisesRegex(RuntimeError, "late failure"):
            self.anchor.start()
        self.assertTrue(self.world.process.terminated)
        self.assertNotIn("radio-go", self.world.devices)
        self.assertIsNone(self.anchor.runtime)

    def nm_set_commands(self):
        return [args for args in self.world.commands if args[:3] == ("nmcli", "device", "set")]

    def test_group_unknown_to_network_manager_is_not_an_error(self):
        # Regression: a live MT7922 run died on `nmcli device set p2p-wlp4s0-0
        # managed no` ("Device not found") although the group was already up.
        self.assertEqual(self.anchor.start(), ("radio-go", "02:00:00:00:00:03"))
        self.assertEqual(self.nm_set_commands(), [])
        self.assertTrue(self.anchor.alive())
        self.assertTrue(self.anchor.cleanup())

    def test_group_listed_as_managed_is_released_from_network_manager(self):
        self.world.nm_state = "yes"
        self.anchor.start()
        self.assertEqual(self.nm_set_commands(), [("nmcli", "device", "set", "radio-go", "managed", "no")])
        self.assertTrue(self.anchor.cleanup())

    def test_network_manager_set_error_never_aborts_a_running_group(self):
        self.world.nm_state = "yes"
        self.world.fail_nm = True
        self.anchor.start()
        self.assertTrue(self.anchor.alive())
        self.assertTrue(any(event == "log" and "NetworkManager" in data["message"] for event, data in self.world.events))
        self.world.fail_nm = False
        self.assertTrue(self.anchor.cleanup())

    def test_network_manager_query_error_never_aborts_a_running_group(self):
        self.world.fail_nm_show = True
        self.anchor.start()
        self.assertTrue(self.anchor.alive())
        self.assertEqual(self.nm_set_commands(), [])
        self.assertTrue(self.anchor.cleanup())

    def test_group_ipv6_is_switched_off_to_avoid_a_duplicate_link_local(self):
        knob = self.base / "ipv6-conf" / "radio-go"
        knob.mkdir(parents=True)
        (knob / "disable_ipv6").write_text("0\n")
        self.anchor.start()
        self.assertEqual((knob / "disable_ipv6").read_text(), "1\n")
        self.assertTrue(self.anchor.cleanup())

    def test_missing_ipv6_knob_never_aborts_the_group(self):
        self.anchor.start()  # no ipv6 directory at all
        self.assertTrue(self.anchor.alive())
        self.assertTrue(self.anchor.cleanup())

    def test_unwritable_ipv6_knob_is_logged_and_never_aborts_the_group(self):
        knob = self.base / "ipv6-conf" / "radio-go" / "disable_ipv6"
        knob.mkdir(parents=True)  # a directory: writing raises IsADirectoryError, an OSError
        self.anchor.start()
        self.assertTrue(any(e == "log" and "IPv6" in d["message"] for e, d in self.world.events))
        self.assertTrue(self.anchor.cleanup())

    def test_supplicant_log_tail_reaches_the_app_log_with_secrets_hidden(self):
        self.anchor.start()
        (self.anchor.runtime / "supplicant.log").write_text(
            "\n".join("line %d" % i for i in range(40)) +
            '\nP2P-GROUP-STARTED radio-go GO ssid="DIRECT-xx" passphrase="hunter2hunter2" go_dev_addr=aa\n')
        self.assertTrue(self.anchor.cleanup())
        lines = [data["message"] for event, data in self.world.events
                 if event == "log" and data.get("source") == "wpa_supplicant"]
        self.assertEqual(len(lines), 15)
        self.assertIn("line 26", lines[0])
        self.assertTrue(any("passphrase=<nascosto>" in l for l in lines))
        self.assertFalse(any("hunter2" in l for l in lines))

    def test_supplicant_log_is_reported_once_even_when_unload_is_blocked(self):
        self.anchor.start()
        self.world.process.hung = True
        (self.anchor.runtime / "supplicant.log").write_text("only line\n")
        self.assertFalse(self.anchor.cleanup())
        self.assertFalse(self.anchor.cleanup())
        reported = [e for e in self.world.events if e[0] == "log" and e[1].get("source") == "wpa_supplicant"]
        self.assertEqual(len(reported), 1)

    def test_status_on_private_socket_must_prove_group_ownership(self):
        self.world.status = "mode=station\naddress=02:00:00:00:00:03"
        with self.assertRaisesRegex(RuntimeError, "non appartiene"):
            self.anchor.start()
        self.assertTrue(self.world.process.terminated)
        self.assertFalse(any(args[-1:] == ("del",) for args in self.world.commands))

    def test_wait_for_group_socket_and_group_formation_status(self):
        self.world.status_sequence = ["FAIL", "mode=P2P GO - group formation\naddress=02:00:00:00:00:03\nfreq=2437"]
        self.assertEqual(self.anchor.start(), ("radio-go", "02:00:00:00:00:03"))
        status_calls = [args for args in self.world.commands if args[-1] == "status"]
        self.assertEqual(len(status_calls), 3)
        self.assertTrue(self.anchor.cleanup())

    def test_confirmed_wrong_channel_is_rejected(self):
        self.world.status = "mode=P2P GO\naddress=02:00:00:00:00:03\nfreq=5220"
        with self.assertRaisesRegex(RuntimeError, "canale diverso"):
            self.anchor.start()
        self.assertTrue(self.world.process.terminated)

    def test_unnamed_device_does_not_overwrite_named_station(self):
        output = ("phy#0\n\tInterface wlan0\n\t\twdev 0x1\n\t\taddr 02:00:00:00:00:01\n"
                  "\t\ttype managed\n\tUnnamed/non-netdev interface\n\t\twdev 0x2\n"
                  "\t\taddr 02:00:00:00:00:02\n\t\ttype P2P-device\n")
        self.assertEqual(anchor_module.interfaces(output), [
            {"phy": "phy0", "name": "wlan0", "wdev": "0x1", "mac": "02:00:00:00:00:01", "type": "managed"}])

    def test_hung_owned_daemon_blocks_unload_and_keeps_runtime(self):
        self.anchor.start()
        self.world.process.hung = True
        self.assertFalse(self.anchor.cleanup())
        self.assertTrue(self.world.process.terminated)
        self.assertTrue(self.world.process.killed)
        self.assertEqual(self.world.process.waits, [3, 1])
        self.assertTrue(self.anchor.runtime.exists())
        self.assertFalse(any(args[-1:] == ("del",) for args in self.world.commands))
        self.world.process.hung = False
        self.assertTrue(self.anchor.cleanup())

    def test_owned_group_fallback_removed_after_daemon_exits(self):
        self.anchor.start()
        self.world.process.remove_group = False
        self.assertTrue(self.anchor.cleanup())
        self.assertIn(("iw", "dev", "radio-go", "del"), self.world.commands)
        self.assertNotIn("radio-go", self.world.devices)

    def test_replaced_group_identity_is_preserved_and_blocks_unload(self):
        self.anchor.start()
        self.world.process.remove_group = False
        self.world.devices["radio-go"]["wdev"] = "0x99"
        self.assertFalse(self.anchor.cleanup())
        self.assertIn("radio-go", self.world.devices)
        self.assertNotIn(("iw", "dev", "radio-go", "del"), self.world.commands)

    def test_radio_command_failure_still_stops_our_process(self):
        self.anchor.start()
        self.anchor.command = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("driver unresponsive"))
        self.assertFalse(self.anchor.cleanup())
        self.assertTrue(self.world.process.terminated)

    def test_invalid_channel_and_interface_rejected(self):
        for iface, phy, channel in (("bad/name", "phy0", 6), ("wlan0", "other", 6), ("wlan0", "phy0", 149)):
            with self.subTest(iface=iface, phy=phy, channel=channel):
                with self.assertRaises(RuntimeError):
                    anchor_module.GoAnchor(iface, phy, channel, self.world.command, lambda *a, **kw: None)


class RealIwFormatTests(unittest.TestCase):
    """iw prints frequencies with a decimal part: "5220.0 MHz [44]"."""

    def test_decimal_frequencies_are_recognised(self):
        info = "\t\tFrequencies:\n\t\t\t* 2437.0 MHz [6] (20.0 dBm)\n\t\t\t* 5180.0 MHz [36] (23.0 dBm)\n\t\t\t* 5220.0 MHz [44] (23.0 dBm)\n"
        for channel, frequency in ((6, 2437), (36, 5180), (44, 5220)):
            self.assertEqual(anchor_module.permitted_frequency(info, channel), frequency)

    def test_decimal_frequency_with_no_ir_is_refused(self):
        info = "\t\t\t* 5180.0 MHz [36] (23.0 dBm)\n\t\t\t* 5220.0 MHz [44] (23.0 dBm)\n\t\t\t\t(no IR)\n"
        with self.assertRaises(RuntimeError):
            anchor_module.permitted_frequency(info, 44)


if __name__ == "__main__":
    unittest.main()
