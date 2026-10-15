"""Exercise root bridge ownership and rollback, without touching OS or radio."""
import importlib.util
from pathlib import Path, PurePosixPath
import subprocess
import unittest
from unittest.mock import Mock, patch

MODULE = Path(__file__).resolve().parents[1] / "root_bridge.py"
spec = importlib.util.spec_from_file_location("root_bridge_under_test", MODULE)
bridge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_module)


class FakeWorld:
    def __init__(self, managed="yes", up=True, connected=False):
        self.managed, self.up, self.connected = managed, up, connected
        self.interfaces = {"wlan0"}
        self.files = {
            "/bundle/backend/owl": "binary",
            "/sys/class/net/wlan0/flags": "0x1" if up else "0x0",
            "/sys/kernel/debug/ieee80211/phy0/mt76/runtime-pm": "1\n",
            "/sys/kernel/debug/ieee80211/phy0/mt76/deep-sleep": "1\n",
        }
        self.commands, self.writes, self.events = [], [], []
        self.fail_command, self.fail_write = None, None
        self.process = None
        self.fail_popen = False
        self.external_awdl_on_popen_failure = False
        self.active_monitor = True

    def path(self, value):
        return FakePath(self, value)

    def command(self, args, check=True):
        args = tuple(args)
        self.commands.append(args)
        if args == self.fail_command:
            self.fail_command = None  # One failure, so cleanup gets a fair attempt.
            raise RuntimeError("Injected command failure")
        if args == ("iw", "dev", "wlan0", "info"):
            return "Interface wlan0\n type managed"
        if args == ("iw", "dev", "wlan0", "link"):
            return "Connected to 00:11:22:33:44:55" if self.connected else "Not connected."
        if args == ("iw", "phy", "phy0", "info"):
            return "Device supports active monitor (which will ACK incoming frames)" if self.active_monitor else "Supported interface modes: monitor"
        if args == ("nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", "wlan0"):
            return self.managed
        if args[:5] == ("nmcli", "device", "set", "wlan0", "managed"):
            self.managed = args[5]
        elif args[:4] == ("ip", "link", "set", "wlan0"):
            self.up = args[4] == "up"
        elif args[:4] == ("iw", "phy", "phy0", "interface"):
            self.interfaces.add(args[5])
        elif args[:2] == ("iw", "dev") and args[-1] == "del":
            self.interfaces.discard(args[2])
        elif args[:3] == ("ip", "link", "delete"):
            self.interfaces.discard(args[3])
        elif args == ("ip", "-6", "address", "show", "dev", "awdl0"):
            return "inet6 fe80::1234/64 scope link"
        return ""

    def popen(self, *args, **kwargs):
        self.spawn_args = args[0]
        if self.fail_popen:
            if self.external_awdl_on_popen_failure:
                self.interfaces.add("awdl0")
            raise OSError("Injected spawn failure")
        self.interfaces.add("awdl0")
        self.process = FakeProcess()
        return self.process


class FakePath:
    def __init__(self, world, value):
        self.world = world
        self.value = PurePosixPath(str(value))

    def __truediv__(self, other):
        return FakePath(self.world, self.value / str(other))

    def __str__(self):
        return str(self.value)

    @property
    def name(self):
        return self.value.name

    def resolve(self):
        if str(self).endswith("/device/driver"):
            return FakePath(self.world, "/drivers/mt7921e")
        if str(self).endswith("/phy80211"):
            return FakePath(self.world, "/devices/phy0")
        return self

    def exists(self):
        if str(self).startswith("/sys/class/net/"):
            return self.name in self.world.interfaces
        return str(self) in self.world.files

    def is_file(self):
        return str(self) in self.world.files

    def read_text(self):
        return self.world.files[str(self)]

    def write_text(self, text):
        self.world.writes.append((str(self), text))
        if str(self) == self.world.fail_write:
            self.world.fail_write = None
            raise OSError("Injected debugfs failure")
        self.world.files[str(self)] = text


class FakeProcess:
    def __init__(self):
        self.stdout = []
        self.returncode = None
        self.terminated, self.killed, self.waits = False, False, []
        self.termination_error = False
        self.timeout_once = False
        self.timeout_after_kill = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.termination_error:
            raise ProcessLookupError("Injected termination race")
        if not self.timeout_once:
            self.returncode = -15

    def wait(self, timeout):
        self.waits.append(timeout)
        if self.killed and self.timeout_after_kill:
            raise subprocess.TimeoutExpired("owl", timeout)
        if self.timeout_once:
            self.timeout_once = False
            raise subprocess.TimeoutExpired("owl", timeout)
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class FakeThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class FakeAnchor:
    def __init__(self, world):
        self.world = world
        self.started = False
        self.stopped = False
        self.fail_preflight = False
        self.fail_start = False
        self.fail_cleanup = False

    def preflight(self):
        self.world.commands.append(("anchor", "preflight"))
        if self.fail_preflight:
            raise RuntimeError("Existing group")

    def start(self):
        self.world.commands.append(("anchor", "start"))
        self.started = True
        self.world.interfaces.add("p2p-wlan0-0")
        if self.fail_start:
            raise RuntimeError("Partial group startup")
        return "p2p-wlan0-0", "02:11:22:33:44:55"

    def cleanup(self):
        self.world.commands.append(("anchor", "cleanup"))
        self.stopped = True
        if self.fail_cleanup:
            return False
        if self.started:
            self.world.interfaces.discard("p2p-wlan0-0")
        return True

    def alive(self):
        return self.started and not self.stopped


class BridgeRollbackTests(unittest.TestCase):
    def setUp(self):
        self.world = FakeWorld()
        self.anchor = FakeAnchor(self.world)
        bridge_module.STOP.clear()
        bridge_module.RECOVERY.clear()
        self.patches = [
            patch.object(bridge_module, "GoAnchor", return_value=self.anchor),
            patch.object(bridge_module, "Path", self.world.path),
            patch.object(bridge_module, "BASE", self.world.path("/bundle")),
            patch.object(bridge_module, "command", self.world.command),
            patch.object(bridge_module, "emit", lambda event, **data: self.world.events.append((event, data))),
            patch.object(bridge_module.subprocess, "Popen", self.world.popen),
            patch.object(bridge_module.threading, "Thread", FakeThread),
            patch.object(bridge_module.time, "sleep", lambda _: None),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def bridge(self):
        return bridge_module.Bridge("wlan0", 6, 1234)

    def restored(self, managed="yes", up=True):
        self.assertEqual(self.world.managed, managed)
        self.assertEqual(self.world.up, up)
        self.assertEqual(self.world.files["/sys/kernel/debug/ieee80211/phy0/mt76/runtime-pm"], "1\n")
        self.assertEqual(self.world.files["/sys/kernel/debug/ieee80211/phy0/mt76/deep-sleep"], "1\n")

    def mutations(self):
        return [args for args in self.world.commands if
                args[:3] in (("ip", "link", "set"), ("ip", "link", "delete"))
                or args[:3] == ("nmcli", "device", "set")
                or "add" in args or args[-1:] == ("del",)]

    def test_connected_wifi_refused_without_any_mutation(self):
        self.world.connected = True
        b = self.bridge()
        with self.assertRaisesRegex(RuntimeError, "Disconnetti"):
            b.start()
        b.cleanup()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.writes, [])

    def test_unsupported_active_monitor_refused_without_any_mutation(self):
        self.world.active_monitor = False
        b = self.bridge()
        with self.assertRaisesRegex(RuntimeError, "active monitor"):
            b.start()
        b.cleanup()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.writes, [])

    def test_existing_awdl_refused_and_never_deleted(self):
        self.world.interfaces.add("awdl0")
        b = self.bridge()
        with self.assertRaisesRegex(RuntimeError, "già in uso"):
            b.start()
        b.cleanup()
        self.assertEqual(self.mutations(), [])
        self.assertIn("awdl0", self.world.interfaces)

    def test_mid_setup_failure_restores_pm_link_and_networkmanager(self):
        self.world.fail_command = ("iw", "dev", "ldmon0", "set", "freq", "2437")
        b = self.bridge()
        with self.assertRaises(RuntimeError):
            b.start()
        b.cleanup()
        self.restored()
        self.assertEqual(self.world.interfaces, {"wlan0"})
        deletions = [c for c in self.world.commands if c[-1:] == ("del",)]
        self.assertEqual(deletions, [("iw", "dev", "ldmon0", "del")])

    def test_failure_after_ack_creation_deletes_both_in_reverse_order(self):
        self.world.managed, self.world.up = "no", False
        self.world.files["/sys/class/net/wlan0/flags"] = "0x0"
        self.world.fail_command = ("ip", "link", "set", "ldack0", "up")
        b = self.bridge()
        with self.assertRaises(RuntimeError):
            b.start()
        b.cleanup()
        self.restored(managed="no", up=False)
        deletions = [c for c in self.world.commands if c[-1:] == ("del",)]
        self.assertEqual(deletions, [("iw", "dev", "ldack0", "del"), ("iw", "dev", "ldmon0", "del")])

    def test_debugfs_write_failure_restores_already_touched_pm(self):
        self.world.fail_write = "/sys/kernel/debug/ieee80211/phy0/mt76/deep-sleep"
        b = self.bridge()
        with self.assertRaises(OSError):
            b.start()
        b.cleanup()
        self.restored()
        self.assertEqual(self.world.interfaces, {"wlan0"})

    def test_nmcli_initial_failure_attempts_restore_but_never_creates_vif(self):
        self.world.fail_command = ("nmcli", "device", "set", "wlan0", "managed", "no")
        b = self.bridge()
        with self.assertRaises(RuntimeError):
            b.start()
        b.cleanup()
        self.restored()
        self.assertEqual(self.world.interfaces, {"wlan0"})

    def test_success_cleanup_kills_only_owned_process_and_restores_state(self):
        b = self.bridge()
        unrelated = FakeProcess()
        b.start()
        self.assertTrue(any(e[0] == "ready" for e in self.world.events))
        b.cleanup()
        self.assertTrue(self.world.process.terminated)
        self.assertFalse(unrelated.terminated)
        self.assertFalse(unrelated.killed)
        self.assertEqual(self.world.interfaces, {"wlan0"})
        self.restored()

    def test_patched_driver_uses_single_active_monitor_and_explicit_channel(self):
        b = bridge_module.Bridge("wlan0", 6, 1234, active_only=True)
        b.start()
        self.assertEqual(b.created, ["ldack0"])
        self.assertIn(("iw", "dev", "ldack0", "set", "freq", "2437"), self.world.commands)
        self.assertFalse(any("ldmon0" in c for c in self.world.commands))
        b.cleanup()
        self.restored()

    def test_modern_backend_uses_metric_election_and_keeps_channel_hopping(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        b = self.bridge()
        b.start()
        self.assertEqual(self.world.spawn_args[0], "/bundle/backend/owl-modern")
        self.assertIn("-E", self.world.spawn_args)
        self.assertNotIn("-F", self.world.spawn_args)
        b.cleanup()

    def test_explicit_fixed_radio_uses_intersection_of_peer_slots(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        b = bridge_module.Bridge("wlan0", 6, 1234, fixed_radio=True)
        b.start()
        self.assertIn("-F", self.world.spawn_args)
        self.assertEqual(self.world.spawn_args[self.world.spawn_args.index("-S") + 1], "intersect")
        b.cleanup()

    def test_fixed_radio_ack_experiment_is_explicit(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        b = bridge_module.Bridge("wlan0", 6, 1234, fixed_radio=True, fixed_ack=True)
        b.start()
        self.assertIn("-A", self.world.spawn_args)
        self.assertIn("-F", self.world.spawn_args)
        b.cleanup()

    def go_bridge(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        return bridge_module.Bridge("wlan0", 6, 1234, go_anchor=True)

    def test_go_preflight_failure_precedes_any_network_change(self):
        b = self.go_bridge()
        self.anchor.fail_preflight = True
        with self.assertRaises(RuntimeError):
            b.start()
        b.cleanup()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.writes, [])

    def test_go_uses_group_mac_on_both_monitors_and_group_channel(self):
        b = self.go_bridge()
        b.start()
        for name in ("ldmon0", "ldack0"):
            alias = ("ip", "link", "set", name, "address", "02:11:22:33:44:55")
            up = ("ip", "link", "set", name, "up")
            self.assertLess(self.world.commands.index(alias), self.world.commands.index(up))
        self.assertFalse(any("freq" in cmd for cmd in self.world.commands))
        self.assertIn("-F", self.world.spawn_args)
        self.assertNotIn("-A", self.world.spawn_args)
        b.cleanup()
        cleanup_index = self.world.commands.index(("anchor", "cleanup"))
        for name in ("ldmon0", "ldack0"):
            self.assertLess(self.world.commands.index(("iw", "dev", name, "del")), cleanup_index)
        self.assertEqual(self.world.interfaces, {"wlan0"})
        self.restored()

    def test_go_partial_startup_rolls_back_before_network_restore(self):
        b = self.go_bridge()
        self.anchor.fail_start = True
        with self.assertRaises(RuntimeError):
            b.start()
        b.cleanup()
        self.assertTrue(self.anchor.stopped)
        self.assertEqual(self.world.interfaces, {"wlan0"})
        self.restored()

    def test_go_monitor_alias_failure_removes_group_and_created_monitor(self):
        b = self.go_bridge()
        self.world.fail_command = ("ip", "link", "set", "ldmon0", "address", "02:11:22:33:44:55")
        with self.assertRaises(RuntimeError):
            b.start()
        b.cleanup()
        self.assertTrue(self.anchor.stopped)
        self.assertEqual(self.world.interfaces, {"wlan0"})
        self.restored()

    def test_go_unstopped_owner_prevents_network_and_driver_reload(self):
        b = self.go_bridge()
        b.start()
        self.anchor.fail_cleanup = True
        b.cleanup()
        self.assertTrue(bridge_module.RECOVERY.is_set())
        self.assertEqual(self.world.managed, "no")
        trial = bridge_module.DriverTrial("wlan0")
        trial.touched = True
        commands_before = list(self.world.commands)
        trial.cleanup()
        self.assertEqual(self.world.commands, commands_before)
        bridge_module.RECOVERY.clear()

    def test_owned_process_timeout_gets_kill_then_network_restoration(self):
        b = self.bridge()
        b.start()
        self.world.process.timeout_once = True
        b.cleanup()
        self.assertTrue(self.world.process.killed)
        self.assertEqual(self.world.process.waits, [5, 3])
        self.restored()

    def test_spawn_failure_does_not_delete_concurrently_created_external_awdl(self):
        self.world.fail_popen = True
        self.world.external_awdl_on_popen_failure = True
        b = self.bridge()
        with self.assertRaises(OSError):
            b.start()
        b.cleanup()
        self.assertIn("awdl0", self.world.interfaces)
        self.restored()

    def test_restore_nmcli_still_attempted_if_link_restore_fails(self):
        b = self.bridge()
        b.start()
        self.world.fail_command = ("ip", "link", "set", "wlan0", "up")
        b.cleanup()
        self.assertEqual(self.world.managed, "yes")

    def test_termination_race_does_not_skip_network_restoration(self):
        b = self.bridge()
        b.start()
        self.world.process.termination_error = True
        b.cleanup()
        self.restored()
        self.assertEqual(self.world.interfaces, {"wlan0"})

    def test_wait_timeout_after_kill_does_not_skip_network_restoration(self):
        b = self.bridge()
        b.start()
        self.world.process.timeout_once = True
        self.world.process.timeout_after_kill = True
        b.cleanup()
        self.restored()

    def test_vif_delete_failure_still_restores_pm_and_network(self):
        b = self.bridge()
        b.start()
        self.world.fail_command = ("iw", "dev", "ldack0", "del")
        b.cleanup()
        self.restored()
        self.assertNotIn("ldmon0", self.world.interfaces)

    def test_one_pm_restore_failure_still_restores_other_pm_and_network(self):
        b = self.bridge()
        b.start()
        self.world.fail_write = "/sys/kernel/debug/ieee80211/phy0/mt76/runtime-pm"
        b.cleanup()
        self.assertEqual(self.world.managed, "yes")
        self.assertTrue(self.world.up)
        self.assertEqual(self.world.files["/sys/kernel/debug/ieee80211/phy0/mt76/deep-sleep"], "1\n")


class CommandTimeoutTests(unittest.TestCase):
    def setUp(self):
        bridge_module.RECOVERY.clear()
        bridge_module.STOP.clear()
        self.addCleanup(bridge_module.RECOVERY.clear)
        self.addCleanup(bridge_module.STOP.clear)

    def test_unkillable_kernel_command_reports_recovery_without_waiting_forever(self):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("iw", 12), subprocess.TimeoutExpired("iw", 1)]
        with patch.object(bridge_module.subprocess, "Popen", return_value=process) as spawn, \
             patch.object(bridge_module, "emit") as emit:
            with self.assertRaisesRegex(RuntimeError, "senza risposta"):
                bridge_module.command(["iw", "dev"])
            process.kill.assert_called_once()
            self.assertEqual(process.communicate.call_count, 2)
            self.assertTrue(bridge_module.RECOVERY.is_set())
            self.assertEqual(emit.call_args.args[0], "recovery")
            with self.assertRaisesRegex(RuntimeError, "riavvio"):
                bridge_module.command(["rmmod", "mac80211"])
            self.assertEqual(spawn.call_count, 1)

    def test_responsive_timeout_does_not_mark_kernel_recovery(self):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("iw", 12), ("", "")]
        with patch.object(bridge_module.subprocess, "Popen", return_value=process):
            with self.assertRaises(RuntimeError):
                bridge_module.command(["iw", "dev"])
        self.assertFalse(bridge_module.RECOVERY.is_set())

    def test_recovery_skips_module_reload_and_debugfs_writes(self):
        bridge_module.RECOVERY.set()
        trial = bridge_module.DriverTrial("wlan0")
        trial.touched = True
        with patch.object(bridge_module, "command") as command:
            trial.cleanup()
        command.assert_not_called()


class SessionLengthTests(unittest.TestCase):
    """supervise() ends on the deadline, never on 0 (= until stopped), and still follows the app that started it."""

    def setUp(self):
        bridge_module.STOP.clear()
        self.addCleanup(bridge_module.STOP.clear)
        self.now = 0.0

    def clock(self):
        self.now += 1.0
        return self.now

    def run_supervise(self, max_seconds, stop_after=None, guardian_alive=True):
        bridge = bridge_module.Bridge("wlan0", 6, 1234, max_seconds=max_seconds)
        bridge.owl = Mock()
        bridge.owl.poll.return_value = None
        calls = {"n": 0}

        def select(*_):
            calls["n"] += 1
            if stop_after and calls["n"] >= stop_after:
                bridge_module.STOP.set()
            return [], [], []

        def kill(pid, sig):
            if not guardian_alive:
                raise ProcessLookupError

        with patch.object(bridge_module.time, "monotonic", self.clock), \
                patch.object(bridge_module.select, "select", select), \
                patch.object(bridge_module.os, "kill", kill):
            bridge.supervise()
        return calls["n"]

    def test_default_session_is_thirty_minutes(self):
        self.assertEqual(bridge_module.Bridge("wlan0", 6, 1234).max_seconds, 1800)

    def test_bounded_session_ends_at_the_deadline(self):
        # Each loop iteration consumes two clock ticks, so ~30 iterations reach a 60 s limit.
        iterations = self.run_supervise(60, stop_after=10000)
        self.assertLess(iterations, 100)

    def test_zero_means_until_stopped(self):
        self.assertEqual(self.run_supervise(0, stop_after=5000), 5000)

    def test_session_still_ends_when_the_app_that_started_it_dies(self):
        self.assertEqual(self.run_supervise(0, stop_after=5000, guardian_alive=False), 0)

    def test_out_of_range_length_is_refused_before_anything_runs(self):
        for value in ("-1", str(bridge_module.MAX_SECONDS + 1)):
            with patch.object(bridge_module.sys, "argv", ["root_bridge.py", "--iface", "wlan0", "--guardian", "1234",
                                                         "--max-seconds", value]):
                with self.assertRaises(SystemExit) as raised:
                    bridge_module.main()
            self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
