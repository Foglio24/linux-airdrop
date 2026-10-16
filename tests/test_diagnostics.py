"""Health checks against a fake /sys and /proc tree, so every state is exercised without hardware."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import diagnostics as dg

MANIFEST = {"kernel": "7.3.0-9-generic", "modules": {
    "mac80211.ko": {"srcversion": "AAA"}, "mt792x-lib.ko": {"srcversion": "BBB"}, "mt7921e.ko": {"srcversion": "CCC"}}}
IW = """phy#1
\tInterface ldack0
\t\tifindex 7
\t\taddr 02:00:5e:10:aa:01
\t\ttype monitor
\tInterface p2p-wlp4s0-0
\t\tifindex 5
\t\taddr 02:00:5e:10:aa:01
\t\tssid DIRECT-l4
\t\ttype P2P-GO
\t\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
\tInterface wlp4s0
\t\tifindex 4
\t\ttype managed
"""


class World:
    """A fake machine: /sys, /proc and the application folder, in a temporary directory."""
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "root"
        self.base = Path(self.temp.name) / "app"
        (self.base / "backend/driver-compat").mkdir(parents=True)
        (self.base / "backend/driver-compat/manifest.json").write_text(json.dumps(MANIFEST))
        owl = self.base / "backend/owl-modern"
        owl.write_text("#!/bin/sh\n")
        owl.chmod(0o755)
        self.commands = {"iw": (0, "phy#1\n\tInterface wlp4s0\n\t\tifindex 4\n\t\ttype managed\n"),
                         "systemctl": (0, "active\n"), "bluetoothctl": (0, "Controller\n\tPowered: yes\n")}
        self.system = dg.System(self.root, run=self.run_command, release="7.3.0-9-generic")
        self.card()
        self.bluetooth()
        self.modules("original")
        self.write("proc/net/if_inet6", "")
        self.write("proc/net/tcp6", "  sl  local_address rem_address st\n")
        self.write("proc/net/udp6", "  sl  local_address rem_address st\n")

    def close(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def run_command(self, argv, timeout):
        return self.commands.get(argv[0], (0, ""))

    def card(self, name="wlp4s0", driver="mt7921e", pci="0x0616"):
        base = self.root / "sys/class/net" / name
        (base / "phy80211").mkdir(parents=True, exist_ok=True)
        (base / "device").mkdir(exist_ok=True)
        link = base / "device/driver"
        link.unlink(missing_ok=True)
        link.symlink_to(f"../../../../bus/pci/drivers/{driver}")
        self.write(f"sys/class/net/{name}/device/device", pci + "\n")
        self.write(f"sys/class/net/{name}/ifindex", "4\n")
        if driver == "mt7921e":
            self.write("sys/module/mt7921e/srcversion", "CCC\n")

    def modules(self, kind):
        versions = {"original": ("O1", "O2", "O3"), "patched": ("AAA", "BBB", "CCC")}[kind]
        for name, version in zip(("mac80211", "mt792x_lib", "mt7921e"), versions):
            self.write(f"sys/module/{name}/srcversion", version + "\n")

    def bluetooth(self, adapter=True, blocked=False):
        if adapter:
            (self.root / "sys/class/bluetooth/hci0").mkdir(parents=True, exist_ok=True)
        self.write("sys/class/rfkill/rfkill0/type", "bluetooth\n")
        self.write("sys/class/rfkill/rfkill0/soft", "1\n" if blocked else "0\n")
        self.write("sys/class/rfkill/rfkill0/hard", "0\n")

    def session(self, awdl_index=9, p2p=True, announce=True, receiver=True):
        """Everything a healthy visible session leaves behind."""
        self.modules("patched")
        (self.root / "sys/class/net/awdl0").mkdir(parents=True, exist_ok=True)
        self.write("sys/class/net/awdl0/ifindex", f"{awdl_index}\n")
        self.write("proc/net/if_inet6", f"fe80000000000000020055fffe10aa02 {awdl_index:02x} 40 20 80 awdl0\n")
        if p2p:
            (self.root / "sys/class/net/p2p-wlp4s0-0").mkdir(parents=True, exist_ok=True)
            self.write("proc/sys/net/ipv6/conf/p2p-wlp4s0-0/disable_ipv6", "1\n")
        if receiver:
            self.write("proc/net/tcp6", "  sl  local_address rem_address st\n   0: 00000000000000000000000000000000:2243 0000:0000 0A\n")
            self.write("proc/net/udp6", "  sl  local_address\n   1: 00000000000000000000000000000000:14E9 0000:0000 07\n")
        self.process(100, ["/x/backend/owl-modern", "-i", "ldack0"])
        self.commands = {"iw": (0, IW), "systemctl": (0, "active\n"), "bluetoothctl": (0, "Controller\n\tPowered: yes\n")}

    def process(self, pid, argv):
        folder = self.root / "proc" / str(pid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def facts(self, **extra):
        data = {"session_active": False, "session_starting": False, "iface": "wlp4s0", "channel": 6, "wifi_direct": True,
                "radio_ready": False, "trial_ready": True, "receiver_pid": None, "receiver_ifindex": None,
                "announce_age": None, "ble_running": False, "helper": "ready", "recovery_needed": False}
        data.update(extra)
        return data

    def check(self, facts=None, slow=True):
        facts = facts or self.facts()
        extra = dg.slow_facts(self.system) if slow else {}
        return {c.id: c for c in dg.check_all(self.system, facts, extra, base=self.base)}


class Idle(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def tearDown(self):
        self.world.close()

    def test_a_healthy_idle_machine_has_no_errors_and_normal_things_are_grey(self):
        checks = self.world.check()
        self.assertEqual(dg.overall(checks.values()), dg.OK)
        for ident in ("modules", "owl", "awdl", "mdns", "service", "p2p", "channel"):
            self.assertEqual(checks[ident].state, dg.IDLE, ident)
        self.assertEqual(checks["card"].state, dg.OK)
        self.assertIn("MT7922", checks["card"].summary)
        self.assertEqual(checks["kernel"].state, dg.OK)

    def test_every_requested_area_has_a_check(self):
        ids = {c.id for c in self.world.check().values()}
        self.assertTrue({"card", "kernel", "driver", "modules", "owl", "awdl", "p2p", "mdns", "bluetooth", "interface", "channel", "service"} <= ids)

    def test_missing_card_and_wrong_kernel_are_errors(self):
        world = self.world
        for entry in (world.root / "sys/class/net/wlp4s0").rglob("*"):
            pass
        import shutil
        shutil.rmtree(world.root / "sys/class/net/wlp4s0")
        checks = world.check()
        self.assertEqual(checks["card"].state, dg.ERROR)
        world.system = dg.System(world.root, run=world.run_command, release="7.4.1-generic")
        self.assertEqual(world.check()["kernel"].state, dg.ERROR)
        self.assertEqual(world.check(world.facts(radio_ready=True))["kernel"].state, dg.OK)

    def test_unknown_chip_is_a_warning(self):
        self.world.card(pci="0x9999")
        self.assertEqual(self.world.check()["card"].state, dg.WARN)

    def test_mixed_modules_are_an_error_and_leftover_patched_modules_a_warning(self):
        self.world.write("sys/module/mt7921e/srcversion", "CCC\n")        # patched
        self.assertEqual(self.world.check()["modules"].state, dg.ERROR)    # one patched, two original
        self.world.modules("patched")
        self.assertEqual(self.world.check()["modules"].state, dg.WARN)     # no session but still loaded

    def test_bluetooth_states(self):
        world = self.world
        self.assertEqual(world.check()["bluetooth"].state, dg.OK)
        world.commands["systemctl"] = (3, "inactive\n")
        self.assertEqual(world.check()["bluetooth"].state, dg.ERROR)
        world.commands["systemctl"] = (0, "active\n")
        world.bluetooth(blocked=True)
        self.assertEqual(world.check()["bluetooth"].state, dg.ERROR)
        world.bluetooth(blocked=False)
        world.commands["bluetoothctl"] = (0, "Powered: no\n")
        self.assertEqual(world.check()["bluetooth"].state, dg.ERROR)
        import shutil
        shutil.rmtree(world.root / "sys/class/bluetooth")
        self.assertEqual(world.check()["bluetooth"].state, dg.ERROR)

    def test_hanging_commands_do_not_raise(self):
        self.world.commands = {"iw": (None, "non risponde"), "systemctl": (None, "non risponde"), "bluetoothctl": (None, "x")}
        checks = self.world.check(self.world.facts(session_active=True))
        self.assertEqual(checks["channel"].state, dg.WARN)
        self.assertIn("non risponde", checks["channel"].summary)

    def test_recovery_flag_comes_first(self):
        checks = dg.check_all(self.world.system, self.world.facts(recovery_needed=True), {}, base=self.world.base)
        self.assertEqual(dg.by_severity(checks)[0].id, "recovery")


class Visible(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.world.session()
        self.facts = self.world.facts(session_active=True, receiver_pid=200, receiver_ifindex=9, announce_age=4.0, ble_running=True)

    def tearDown(self):
        self.world.close()

    def test_a_healthy_session_is_all_green(self):
        checks = self.world.check(self.facts)
        bad = {k: (c.state, c.summary) for k, c in checks.items() if c.state in (dg.ERROR, dg.WARN)}
        self.assertEqual(bad, {})
        for ident in ("modules", "owl", "awdl", "p2p", "mdns", "channel", "service"):
            self.assertEqual(checks[ident].state, dg.OK, ident)
        self.assertIn("indice 9", checks["awdl"].summary)
        self.assertIn("6", checks["channel"].summary)

    def test_mdns_on_a_stale_interface_index_is_an_error_with_a_machine_code(self):
        self.facts["receiver_ifindex"] = 8
        check = self.world.check(self.facts)["mdns"]
        self.assertEqual((check.state, check.code), (dg.ERROR, "index"))

    def test_silent_announcements(self):
        self.facts["announce_age"] = 300
        self.assertEqual(self.world.check(self.facts)["mdns"].code, "silent")
        self.facts["announce_age"] = None
        self.assertEqual(self.world.check(self.facts)["mdns"].state, dg.WARN)

    def test_missing_wifi_direct_group_awdl_owl_and_service(self):
        import shutil
        shutil.rmtree(self.world.root / "sys/class/net/p2p-wlp4s0-0")
        self.assertEqual(self.world.check(self.facts)["p2p"].state, dg.ERROR)
        self.world.write("proc/net/if_inet6", "")
        self.assertEqual(self.world.check(self.facts)["awdl"].state, dg.ERROR)
        shutil.rmtree(self.world.root / "proc/100")
        self.assertEqual(self.world.check(self.facts)["owl"].state, dg.ERROR)
        self.world.write("proc/net/tcp6", "  sl\n")
        self.assertEqual(self.world.check(self.facts)["service"].state, dg.WARN)
        self.facts["receiver_pid"] = None
        self.assertEqual(self.world.check(self.facts)["service"].state, dg.ERROR)

    def test_group_with_ipv6_on_is_a_warning_and_wrong_channel_an_error(self):
        self.world.write("proc/sys/net/ipv6/conf/p2p-wlp4s0-0/disable_ipv6", "0\n")
        self.assertEqual(self.world.check(self.facts)["p2p"].state, dg.WARN)
        self.world.commands["iw"] = (0, IW.replace("channel 6 (2437 MHz)", "channel 11 (2462 MHz)"))
        self.assertEqual(self.world.check(self.facts)["channel"].state, dg.ERROR)

    def test_starting_state_is_not_reported_as_failure(self):
        facts = dict(self.facts, session_active=False, session_starting=True)
        import shutil
        shutil.rmtree(self.world.root / "proc/100")
        checks = self.world.check(facts)
        self.assertEqual(checks["owl"].state, dg.WARN)

    def test_no_wake_up_signal_is_a_warning(self):
        self.facts["ble_running"] = False
        self.assertEqual(self.world.check(self.facts)["bluetooth"].state, dg.WARN)


class Parsers(unittest.TestCase):
    def test_iw_dev(self):
        parsed = {i["name"]: i for i in dg.parse_iw_dev(IW)}
        self.assertEqual(parsed["p2p-wlp4s0-0"]["channel"], 6)
        self.assertEqual(parsed["p2p-wlp4s0-0"]["type"], "P2P-GO")
        self.assertEqual(parsed["wlp4s0"]["ifindex"], 4)
        self.assertEqual(dg.parse_iw_dev(""), [])
        self.assertEqual(dg.parse_iw_dev(None), [])

    def test_ports(self):
        tcp = "  sl\n   0: 0000:2243 0000:0000 0A\n   1: 0000:1F90 0000:0000 01\n"
        self.assertEqual(dg.listening_ports(tcp), {0x2243})
        self.assertEqual(dg.listening_ports(tcp, state=None), {0x2243, 0x1F90})
        self.assertEqual(dg.listening_ports("garbage"), set())

    def test_link_local(self):
        text = "fe80000000000000020055fffe10aa02 09 40 20 80 awdl0\n20010db8000000000000000000000001 02 40 00 80 eth0\n"
        self.assertEqual(dg.link_local(text, "awdl0"), (9, "fe80:0000:0000:0000:0200:55ff:fe10:aa02"))
        self.assertIsNone(dg.link_local(text, "eth0"))
        self.assertIsNone(dg.link_local("", "awdl0"))


class Report(unittest.TestCase):
    def test_report_masks_personal_data_and_lists_worst_first(self):
        world = World()
        try:
            home = str(Path.home())
            checks = world.check(world.facts(session_active=True))
            checks["card"].details.append(f"Indirizzo 02:00:5e:10:aa:02 e fe80::5eff:fe10:aa02 in {home}/Scaricati")
            text = dg.report_text(list(checks.values()), world.facts(session_active=True), version="1.1.0")
        finally:
            world.close()
        self.assertNotIn("02:00:5e:10", text)
        self.assertIn("xx:xx:xx:xx:aa:02", text)
        self.assertNotIn("fe80::5eff", text)
        self.assertNotIn(home, text)
        self.assertIn("Versione: 1.1.0", text)
        first = next(line for line in text.splitlines() if line.startswith("["))
        self.assertTrue(first.startswith(("[ERRORE]", "[ATTENZIONE]")))

    def test_redact_is_safe_on_ordinary_text(self):
        self.assertEqual(dg.redact("niente da nascondere", home="/home/x", user="x"), "niente da nascondere")


if __name__ == "__main__":
    unittest.main(verbosity=2)
