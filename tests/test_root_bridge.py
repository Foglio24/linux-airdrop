"""Exercise root bridge ownership and rollback, without touching OS or radio."""
import importlib.util
import errno
from pathlib import Path, PurePosixPath
import struct
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
        self.dead_binaries = set()      # binaries that exit at once without creating awdl0
        self.spawns = []
        self.external_awdl_on_popen_failure = False
        self.active_monitor = True

    def path(self, value):
        return FakePath(self, value)

    def wifi_radio(self, soft=False, hard=False):
        self.files["/sys/class/rfkill/rfkill0/type"] = "wlan\n"
        self.files["/sys/class/rfkill/rfkill0/name"] = "phy0\n"
        self.files["/sys/class/rfkill/rfkill0/soft"] = "1\n" if soft else "0\n"
        self.files["/sys/class/rfkill/rfkill0/hard"] = "1\n" if hard else "0\n"

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
            self.files["/sys/class/net/wlan0/flags"] = "0x1" if self.up else "0x0"
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
        self.spawns.append(args[0])
        if args[0][0] in self.dead_binaries:
            self.process = FakeProcess()
            self.process.returncode = 1
            return self.process
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

    def iterdir(self):
        prefix = str(self.value).rstrip("/") + "/"
        children = sorted({key[len(prefix):].split("/")[0] for key in self.world.files if key.startswith(prefix)})
        if not children:
            raise FileNotFoundError(str(self))
        return [FakePath(self.world, prefix + child) for child in children]


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

    def test_disabled_radio_is_enabled_for_the_session_and_restored(self):
        self.world.wifi_radio(soft=True)
        b = self.bridge()
        b.start()
        self.assertEqual(self.world.files["/sys/class/rfkill/rfkill0/soft"], "0\n")
        b.cleanup()
        self.assertEqual(self.world.files["/sys/class/rfkill/rfkill0/soft"], "1\n")
        self.restored()

    def test_hard_blocked_radio_refused_without_any_mutation(self):
        self.world.wifi_radio(hard=True)
        b = self.bridge()
        with self.assertRaisesRegex(RuntimeError, "interruttore"):
            b.start()
        b.cleanup()
        self.assertEqual(self.mutations(), [])
        self.assertEqual(self.world.writes, [])
        self.assertEqual(self.world.files["/sys/class/rfkill/rfkill0/soft"], "0\n")

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

    def owl_bridge(self, **kwargs):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        self.world.files["/bundle/backend/owl-nexa"] = "customised binary"
        return bridge_module.Bridge("wlan0", 6, 1234, fixed_radio=True, fixed_ack=True, **kwargs)

    def test_the_customised_owl_is_used_with_its_validated_flags(self):
        b = self.owl_bridge(owl_variant="nexa", owl_extras=bridge_module.owl_extras("2,1", -75, True))
        b.start()
        args = self.world.spawn_args
        self.assertEqual(args[0], "/bundle/backend/owl-nexa")
        self.assertEqual(args[args.index("-G") + 1], "2,1")
        self.assertEqual(args[args.index("-r") + 1], "-75")
        self.assertIn("-I", args)
        for kept in ("-F", "-A", "-E", "-N", "-vv"):
            self.assertIn(kept, args)                        # every existing option is still passed
        b.cleanup()

    def test_the_shipped_owl_stays_the_default(self):
        b = self.owl_bridge()
        b.start()
        self.assertEqual(self.world.spawn_args[0], "/bundle/backend/owl-modern")
        self.assertNotIn("-G", self.world.spawn_args)
        b.cleanup()

    def test_a_missing_customised_binary_falls_back_without_a_spawn(self):
        b = self.owl_bridge(owl_variant="nexa")
        del self.world.files["/bundle/backend/owl-nexa"]
        b.start()
        self.assertEqual([s[0] for s in self.world.spawns], ["/bundle/backend/owl-modern"])
        b.cleanup()

    def test_a_customised_owl_that_cannot_start_is_replaced_by_the_shipped_one(self):
        b = self.owl_bridge(owl_variant="nexa", owl_extras=["-G", "1"])
        self.world.dead_binaries.add("/bundle/backend/owl-nexa")
        b.start()
        self.assertEqual([s[0] for s in self.world.spawns], ["/bundle/backend/owl-nexa", "/bundle/backend/owl-modern"])
        self.assertNotIn("-G", self.world.spawns[1])           # the fallback gets none of the new flags
        self.assertIn("-F", self.world.spawns[1])
        self.assertTrue(any(e[0] == "log" and "owl-nexa non si è avviato" in e[1].get("message", "")
                            for e in self.world.events))
        self.assertIn("ready", [e[0] for e in self.world.events])
        b.cleanup()

    def test_when_both_fail_the_session_fails_and_cleans_up(self):
        b = self.owl_bridge(owl_variant="nexa")
        self.world.dead_binaries.update(("/bundle/backend/owl-nexa", "/bundle/backend/owl-modern"))
        with self.assertRaisesRegex(RuntimeError, "OWL non si è avviato"):
            b.start()
        b.cleanup()
        self.assertEqual(self.world.interfaces, {"wlan0"})

    def test_owl_flags_are_validated(self):
        self.assertEqual(bridge_module.owl_extras(), [])
        self.assertEqual(bridge_module.owl_extras("3"), ["-G", "3"])
        self.assertEqual(bridge_module.owl_extras("0,16", -100, False), ["-G", "0,16", "-r", "-100"])
        for guard in ("17", "-1", "1,2,3", "a", "1;rm", " 1", "1,", ",1", "100"):
            with self.subTest(guard=guard), self.assertRaises(ValueError):
                bridge_module.owl_extras(guard)
        for rssi in (-101, -29, 0, 50):
            with self.subTest(rssi=rssi), self.assertRaises(ValueError):
                bridge_module.owl_extras(None, rssi)

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

    def test_data_rate_is_passed_to_owl_only_when_requested(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        b = bridge_module.Bridge("wlan0", 6, 1234, fixed_radio=True, data_rate="36")
        b.start()
        self.assertEqual(self.world.spawn_args[self.world.spawn_args.index("-R") + 1], "36")
        b.cleanup()

    def test_default_leaves_owl_rate_alone(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        b = bridge_module.Bridge("wlan0", 6, 1234, fixed_radio=True)
        b.start()
        self.assertNotIn("-R", self.world.spawn_args)
        b.cleanup()

    def test_data_rate_syntax_is_validated(self):
        for ok in ("6", "54", "mcs7", "mcs15-sgi"):
            self.assertEqual(bridge_module.data_rate_arg(ok), ok)
        for bad in ("7", "mcs16", "36; rm", "", "-A"):
            with self.assertRaises(Exception):
                bridge_module.data_rate_arg(bad)

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

    def test_go_uses_group_mac_on_paired_monitors_and_group_channel(self):
        b = self.go_bridge()
        b.start()
        self.assertEqual(b.created, ["ldmon0", "ldack0"])
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
        self.world.fail_command = ("ip", "link", "set", "ldack0", "address", "02:11:22:33:44:55")
        with self.assertRaises(RuntimeError):
            b.start()
        b.cleanup()
        self.assertTrue(self.anchor.stopped)
        self.assertEqual(self.world.interfaces, {"wlan0"})
        self.restored()

    def test_native_go_disables_tx_echo_before_first_monitor_up(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        b = bridge_module.Bridge("wlan0", 6, 1234, go_anchor=True, native_driver=True)
        with patch.object(bridge_module, "suppress_monitor_tx_echo",
                          side_effect=lambda: self.world.commands.append(("monitor", "skip-tx"))) as configure:
            b.start()
        configure.assert_called_once_with()
        cmds = self.world.commands
        self.assertLess(cmds.index(("anchor", "start")), cmds.index(("monitor", "skip-tx")))
        self.assertLess(cmds.index(("ip", "link", "set", "ldack0", "address", "02:11:22:33:44:55")),
                        cmds.index(("monitor", "skip-tx")))
        self.assertLess(cmds.index(("monitor", "skip-tx")), cmds.index(("ip", "link", "set", "ldack0", "up")))
        parent_up = cmds.index(("ip", "link", "set", "wlan0", "up"))
        passive_add = cmds.index(("iw", "phy", "phy0", "interface", "add", "ldmon0", "type", "monitor"))
        self.assertLess(parent_up, passive_add)
        b.cleanup()
        cleanup = self.world.commands
        restore = cleanup.index(("nmcli", "device", "set", "wlan0", "managed", "yes"))
        for name in ("ldmon0", "ldack0"):
            self.assertLess(cleanup.index(("iw", "dev", name, "del")), restore)
        self.restored()

    def test_native_monitor_configuration_failure_rolls_back_without_bringing_it_up(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        b = bridge_module.Bridge("wlan0", 6, 1234, go_anchor=True, native_driver=True)
        with patch.object(bridge_module, "suppress_monitor_tx_echo", side_effect=RuntimeError("unsupported flags")):
            with self.assertRaisesRegex(RuntimeError, "unsupported"):
                b.start()
        b.cleanup()
        self.assertNotIn(("ip", "link", "set", "ldack0", "up"), self.world.commands)
        self.assertLess(self.world.commands.index(("iw", "dev", "ldack0", "del")),
                        self.world.commands.index(("anchor", "cleanup")))
        self.assertEqual(self.world.interfaces, {"wlan0"})
        self.restored()

    def test_patched_go_keeps_original_monitor_flags(self):
        b = self.go_bridge()
        with patch.object(bridge_module, "suppress_monitor_tx_echo") as configure:
            b.start()
        configure.assert_not_called()
        b.cleanup()

    def native_go_bridge(self):
        self.world.files["/bundle/backend/owl-modern"] = "modern binary"
        return bridge_module.Bridge("wlan0", 6, 1234, go_anchor=True, native_driver=True)

    def start_native_go(self):
        b = self.native_go_bridge()
        with patch.object(bridge_module, "suppress_monitor_tx_echo"):
            b.start()
        return b

    def test_native_parent_guard_failure_precedes_monitor_creation_and_rolls_back(self):
        b = self.native_go_bridge()
        original = self.world.command
        failed_once = False

        def parent_did_not_come_up(args, check=True):
            nonlocal failed_once
            if tuple(args) == ("ip", "link", "set", "wlan0", "up") and not failed_once:
                failed_once = True
                self.world.commands.append(tuple(args))
                return ""
            return original(args, check=check)

        with patch.object(bridge_module, "command", parent_did_not_come_up):
            with self.assertRaisesRegex(RuntimeError, "principale deve restare attiva"):
                b.start()
            b.cleanup()
        self.assertEqual(b.created, [])
        self.assertFalse(bridge_module.RECOVERY.is_set())
        self.assertFalse(any("interface" in cmd and "add" in cmd for cmd in self.world.commands))
        self.assertTrue(self.anchor.stopped)
        self.restored()

    def test_native_go_crash_keeps_parent_guard_until_both_monitors_are_removed(self):
        self.world.up = False
        self.world.files["/sys/class/net/wlan0/flags"] = "0x0"
        b = self.start_native_go()
        self.assertTrue(self.world.up)
        self.anchor.stopped = True
        with self.assertRaisesRegex(RuntimeError, "Wi-Fi Direct si è interrotto"):
            b.supervise()
        b.cleanup()
        self.assertFalse(bridge_module.RECOVERY.is_set())
        cmds = self.world.commands
        final_down = max(i for i, cmd in enumerate(cmds) if cmd == ("ip", "link", "set", "wlan0", "down"))
        for name in ("ldmon0", "ldack0"):
            self.assertLess(cmds.index(("iw", "dev", name, "del")), final_down)
        self.assertEqual(self.world.interfaces, {"wlan0"})
        self.restored(up=False)
        # A repeated cleanup must not mistake the restored down parent for a
        # lost guard after all monitor interfaces have already been removed.
        b.cleanup()
        self.assertFalse(bridge_module.RECOVERY.is_set())

    def test_native_lost_parent_guard_blocks_monitor_and_anchor_cleanup(self):
        b = self.start_native_go()
        self.world.up = False
        self.world.files["/sys/class/net/wlan0/flags"] = "0x0"
        with self.assertRaisesRegex(RuntimeError, "principale deve restare attiva"):
            b.supervise()
        b.cleanup()
        self.assertTrue(bridge_module.RECOVERY.is_set())
        self.assertFalse(any(cmd[-1:] == ("del",) for cmd in self.world.commands))
        self.assertNotIn(("anchor", "cleanup"), self.world.commands)
        self.assertEqual(self.world.managed, "no")
        self.assertTrue({"ldmon0", "ldack0"}.issubset(self.world.interfaces))

    def test_native_cleanup_checks_parent_guard_before_deleting_either_monitor(self):
        b = self.start_native_go()
        self.world.files["/sys/class/net/wlan0/flags"] = "0x0"
        b.cleanup()
        self.assertTrue(bridge_module.RECOVERY.is_set())
        self.assertFalse(any(cmd[-1:] == ("del",) for cmd in self.world.commands))
        self.assertNotIn(("anchor", "cleanup"), self.world.commands)
        self.assertEqual(self.world.managed, "no")

    def test_native_failed_monitor_removal_preserves_parent_and_blocks_network_restore(self):
        b = self.start_native_go()
        self.world.fail_command = ("iw", "dev", "ldack0", "del")
        b.cleanup()
        self.assertTrue(bridge_module.RECOVERY.is_set())
        self.assertTrue(self.world.up)
        self.assertEqual(self.world.managed, "no")
        self.assertNotIn(("anchor", "cleanup"), self.world.commands)
        self.assertTrue({"ldmon0", "ldack0"}.issubset(self.world.interfaces))

    def test_native_successful_delete_that_leaves_monitor_blocks_anchor_and_network_restore(self):
        b = self.start_native_go()
        original = self.world.command

        def successful_no_op(args, check=True):
            if tuple(args) == ("iw", "dev", "ldack0", "del"):
                self.world.commands.append(tuple(args))
                return ""
            return original(args, check=check)

        with patch.object(bridge_module, "command", successful_no_op):
            b.cleanup()
        self.assertTrue(bridge_module.RECOVERY.is_set())
        self.assertTrue(self.world.up)
        self.assertEqual(self.world.managed, "no")
        self.assertNotIn(("anchor", "cleanup"), self.world.commands)
        self.assertIn("ldack0", b.created)
        self.assertTrue({"ldmon0", "ldack0"}.issubset(self.world.interfaces))

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


class NativeDriverModeTests(unittest.TestCase):
    def setUp(self):
        bridge_module.STOP.clear()
        bridge_module.RECOVERY.clear()

    def run_main(self, args):
        with patch.object(bridge_module.os, "geteuid", return_value=0), \
                patch("builtins.open", return_value=Mock()), \
                patch.object(bridge_module.fcntl, "flock"), \
                patch.object(bridge_module.signal, "signal"), \
                patch.object(bridge_module.sys, "argv", ["root_bridge.py", "--iface", "wlan0", "--guardian", "1234", *args]):
            return bridge_module.main()

    def test_native_monitor_only_and_wrong_channel_refused_before_network_change(self):
        for args in ([],):
            with self.subTest(args=args), patch.object(bridge_module, "command") as command, \
                    patch.object(bridge_module, "Bridge") as bridge, patch.object(bridge_module, "emit") as emit:
                self.assertEqual(self.run_main(args), 1)
                command.assert_not_called()
                bridge.assert_not_called()
                emit.assert_any_call("error", message="Con i driver di sistema usa Wi-Fi Direct sui canali 6, 36 o 44.")

    def test_patched_driver_can_still_use_monitor_only(self):
        with patch.object(bridge_module, "DriverTrial") as trial, patch.object(bridge_module, "Bridge") as bridge:
            trial.return_value.start.return_value = "wlan0"
            self.assertEqual(self.run_main(["--driver-trial"]), 0)
            self.assertFalse(bridge.call_args.kwargs["go_anchor"])
            self.assertFalse(bridge.call_args.kwargs["native_driver"])
            bridge.return_value.start.assert_called_once()
            trial.return_value.cleanup.assert_called_once()


class FakeNetlink:
    port = 2468
    family = 42

    def __init__(self, error=0):
        self.error = error
        self.sent, self.replies, self.timeouts = [], [], []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def bind(self, address):
        self.bound = address

    def getsockname(self):
        return self.port, 0

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def message(self, kind, sequence, payload):
        return struct.pack("=IHHII", 16 + len(payload), kind, 0, sequence, self.port) + payload

    def ack(self, request, error=0):
        seq = struct.unpack_from("=I", request, 8)[0]
        return self.message(2, seq, struct.pack("=i", error) + request[:16])

    def sendto(self, request, target):
        self.sent.append(request)
        self.target = target
        if len(self.sent) == 1:
            # CTRL_CMD_NEWFAMILY plus CTRL_ATTR_FAMILY_ID.
            data = struct.pack("=BBHHHH", 1, 1, 0, 6, 1, self.family) + b"\0\0"
            self.replies.append(self.message(16, 1, data))
        self.replies.append(self.ack(request, self.error if len(self.sent) == 2 else 0))

    def recvmsg(self, _):
        return self.replies.pop(0), [], 0, (0, 0)


class NativeMonitorNetlinkTests(unittest.TestCase):
    def configure(self, channel, up=False):
        knob = Mock()
        knob.__truediv__ = Mock(return_value=knob)
        knob.read_text.return_value = "0x1" if up else "0x0"
        with patch.object(bridge_module, "Path", return_value=knob), \
                patch.object(bridge_module.socket, "socket", return_value=channel), \
                patch.object(bridge_module.socket, "if_nametoindex", return_value=17) as index:
            bridge_module.suppress_monitor_tx_echo()
        index.assert_called_once_with("ldack0")

    def test_active_and_skip_tx_are_sent_together_for_the_owned_monitor(self):
        channel = FakeNetlink()
        self.configure(channel)
        self.assertEqual(len(channel.sent), 2)
        request = channel.sent[1]
        self.assertEqual(struct.unpack_from("=H", request, 4)[0], channel.family)
        self.assertEqual(struct.unpack_from("=B", request, 16)[0], 6)
        attrs = dict(bridge_module._netlink_attributes(request[20:]))
        self.assertEqual(struct.unpack("=I", attrs[3])[0], 17)
        self.assertEqual(dict(bridge_module._netlink_attributes(attrs[23])), {6: b"", 7: b""})
        self.assertTrue(all(0 < value <= 3 for value in channel.timeouts))
        self.assertEqual(channel.target, (0, 0))

    def test_kernel_rejection_is_reported_without_a_fallback(self):
        channel = FakeNetlink(error=-errno.EOPNOTSUPP)
        with self.assertRaisesRegex(RuntimeError, "senza eco TX"):
            self.configure(channel)
        self.assertEqual(len(channel.sent), 2)

    def test_up_monitor_is_refused_before_netlink_mutation(self):
        channel = FakeNetlink()
        with self.assertRaisesRegex(RuntimeError, "ancora disattivata"):
            self.configure(channel, up=True)
        self.assertEqual(channel.sent, [])

    def test_missing_ack_times_out(self):
        channel = FakeNetlink()
        channel.recvmsg = Mock(side_effect=TimeoutError("timeout"))
        with self.assertRaisesRegex(RuntimeError, "senza eco TX"):
            self.configure(channel)

    def test_ack_for_another_sequence_cannot_complete_the_request(self):
        channel = FakeNetlink()
        channel.sendto = Mock()
        request = struct.pack("=IHHII", 20, 42, 5, 2, channel.port) + bytes(4)
        wrong = bytearray(channel.ack(request))
        struct.pack_into("=I", wrong, 8, 99)
        channel.replies = [bytes(wrong), channel.ack(request)]
        self.assertEqual(bridge_module._generic_netlink_request(channel, 42, 6, b"", 2), [])
        self.assertEqual(channel.replies, [])

    def test_ack_original_header_must_match_request(self):
        channel = FakeNetlink()
        channel.sendto = Mock()
        request = struct.pack("=IHHII", 20, 42, 5, 2, channel.port) + bytes(4)
        wrong = bytearray(channel.ack(request))
        struct.pack_into("=H", wrong, 24, 41)
        channel.replies = [bytes(wrong)]
        with self.assertRaisesRegex(RuntimeError, "altro comando"):
            bridge_module._generic_netlink_request(channel, 42, 6, b"", 2)

    def test_truncated_messages_and_attributes_are_rejected(self):
        for packet in (b"short", struct.pack("=IHHII", 99, 42, 0, 2, 2468)):
            with self.subTest(packet=packet):
                channel = FakeNetlink()
                channel.sendto = Mock()
                channel.replies = [packet]
                with self.assertRaisesRegex(RuntimeError, "netlink"):
                    bridge_module._generic_netlink_request(channel, 42, 6, b"", 2)
        for payload in (b"bad", struct.pack("=HH", 99, 1)):
            with self.assertRaisesRegex(RuntimeError, "netlink"):
                list(bridge_module._netlink_attributes(payload))


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
