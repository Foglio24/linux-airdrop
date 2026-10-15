"""Model module loading, device disappearance and rollback without OS changes."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "root_bridge.py"
spec = importlib.util.spec_from_file_location("driver_trial_under_test", SOURCE)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ModuleWorld:
    KERNEL = "7.3.0-9-generic"
    VERMAGIC = KERNEL + " SMP preempt mod_unload modversions"
    DEPENDENCIES = {
        "mt7921e": ["mt7921_common", "mt792x_lib", "mt76_connac_lib", "mt76"],
        "mt7921_common": ["mt792x_lib", "mt76_connac_lib", "mt76"],
        "mt792x_lib": ["mt76_connac_lib", "mt76", "mac80211"],
        "mt76_connac_lib": ["mt76", "mac80211"],
        "mt76": ["mac80211", "cfg80211"],
        "mac80211": ["cfg80211", "libarc4"],
        "cfg80211": [],
        "libarc4": [],
    }

    def __init__(self, root):
        self.root, self.bundle = root, root / "bundle"
        self.managed, self.up = "yes", True
        self.commands, self.events = [], []
        self.loaded, self.disk_versions = {}, {}
        self.fail_once = None
        self.recursive_remove = False
        self.rename_device = False
        self.name = "wlan0"
        self.modules_dir = self.bundle / "backend/driver-compat"
        self.modules_dir.mkdir(parents=True)
        manifest = {"kernel": self.KERNEL, "modules": {}}
        for name in ("mac80211.ko", "mt792x-lib.ko", "mt7921e.ko"):
            data = ("fake test module " + name).encode()
            (self.modules_dir / name).write_bytes(data)
            manifest["modules"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "vermagic": self.VERMAGIC}
        (self.modules_dir / "manifest.json").write_text(json.dumps(manifest))
        for name in self.DEPENDENCIES:
            self.disk_versions[name] = "DISTRIBUTION_" + name
            self.loaded[name] = self.disk_versions[name]
        self.sys_path("/sys/class/ieee80211/phy0").mkdir(parents=True)
        self.sys_path("/drivers/mt7921e").mkdir(parents=True)
        self.sync_modules()
        self.create_interface(self.name)

    def sys_path(self, value):
        return self.root / str(value).lstrip("/")

    def path(self, value):
        s = str(value)
        return self.sys_path(s) if s.startswith("/sys/") else Path(value)

    def sync_modules(self):
        directory = self.sys_path("/sys/module")
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        for name, version in self.loaded.items():
            d = directory / name
            (d / "holders").mkdir(parents=True)
            (d / "srcversion").write_text(version)
        for holder in self.loaded:
            for dependency in self.DEPENDENCIES[holder]:
                if dependency in self.loaded:
                    (directory / dependency / "holders" / holder).touch()

    def create_interface(self, name):
        directory = self.sys_path("/sys/class/net")
        directory.mkdir(parents=True, exist_ok=True)
        for child in directory.iterdir():
            shutil.rmtree(child)
        self.name = name
        iface = directory / name
        (iface / "device").mkdir(parents=True)
        (iface / "address").write_text("02:11:22:33:44:55")
        (iface / "flags").write_text("0x1" if self.up else "0x0")
        (iface / "device/driver").symlink_to(self.sys_path("/drivers/mt7921e"))
        (iface / "phy80211").symlink_to(self.sys_path("/sys/class/ieee80211/phy0"))

    def remove_module(self, name):
        self.loaded.pop(name, None)
        if name == "mt7921e":
            d = self.sys_path("/sys/class/net")
            for child in d.iterdir():
                shutil.rmtree(child)
        self.sync_modules()

    def load_stock(self, name):
        for dependency in self.DEPENDENCIES[name]:
            if dependency not in self.loaded:
                self.load_stock(dependency)
        self.loaded.setdefault(name, self.disk_versions[name])
        self.sync_modules()
        if name == "mt7921e":
            self.create_interface("wlp2s0" if self.rename_device else self.name)

    def command(self, args, check=True):
        args = tuple(str(a) for a in args)
        self.commands.append(args)
        if args == self.fail_once:
            self.fail_once = None
            raise RuntimeError("Injected module failure")
        if args[:3] == ("modinfo", "-F", "vermagic"):
            return self.VERMAGIC
        if args[:3] == ("modinfo", "-F", "srcversion"):
            return self.disk_versions[args[3]]
        if args[:2] == ("iw", "dev") and args[-1] == "info":
            return "type managed"
        if args[:2] == ("iw", "dev") and args[-1] == "link":
            return "Not connected."
        if args[:2] == ("iw", "phy"):
            return "Device supports active monitor" if self.loaded.get("mt792x_lib", "").startswith("TEST_") else ""
        if args[:3] == ("nmcli", "-g", "GENERAL.NM-MANAGED"):
            return self.managed
        if args[:3] == ("nmcli", "device", "set"):
            self.managed = args[-1]
        elif args[:3] == ("ip", "link", "set"):
            self.up = args[-1] == "up"
        elif args[0] == "rmmod" or args[:2] == ("modprobe", "-r"):
            self.remove_module(args[-1])
            if self.recursive_remove and args[0] == "modprobe":
                # kmod's -r also prunes unused dependencies; insmod will not reload them.
                for name in list(self.loaded):
                    self.remove_module(name)
        elif args[0] == "modprobe":
            self.load_stock(args[1])
        elif args[0] == "insmod":
            names = {"mac80211.ko": "mac80211", "mt792x-lib.ko": "mt792x_lib", "mt7921e.ko": "mt7921e"}
            name = names[Path(args[1]).name]
            missing = set(self.DEPENDENCIES[name]) - set(self.loaded)
            if missing:
                raise RuntimeError("Unknown symbols: " + ",".join(sorted(missing)))
            self.loaded[name] = "TEST_" + name
            self.sync_modules()
            if name == "mt7921e":
                self.create_interface("wlp2s0" if self.rename_device else self.name)
        return ""

    def mutations(self):
        return [c for c in self.commands if c[0] in {"rmmod", "modprobe", "insmod", "ip"} or c[:3] == ("nmcli", "device", "set")]


class DriverTrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.world = ModuleWorld(Path(self.tmp.name))
        for p in [
            patch.object(module, "Path", self.world.path),
            patch.object(module, "BASE", self.world.bundle),
            patch.object(module, "command", self.world.command),
            patch.object(module.platform, "release", lambda: self.world.KERNEL),
            patch.object(module, "emit", lambda event, **data: self.world.events.append((event, data))),
            patch.object(module.time, "sleep", lambda _: None),
        ]:
            p.start()
            self.addCleanup(p.stop)

    def manifest(self):
        p = self.world.modules_dir / "manifest.json"
        return p, json.loads(p.read_text())

    def assert_stock_restored(self):
        self.assertEqual(self.world.loaded, self.world.disk_versions)
        self.assertEqual(self.world.managed, "yes")
        self.assertTrue(self.world.up)

    def assert_refused_without_mutation(self):
        trial = module.DriverTrial("wlan0")
        with self.assertRaises(Exception):
            trial.start()
        trial.cleanup()
        self.assertEqual(self.world.mutations(), [])

    def test_success_restores_original_modules_and_network_state(self):
        trial = module.DriverTrial("wlan0")
        self.assertEqual(trial.start(), "wlan0")
        self.assertEqual(self.world.loaded["mac80211"], "TEST_mac80211")
        self.assertEqual(self.world.loaded["mt792x_lib"], "TEST_mt792x_lib")
        self.assertEqual(self.world.loaded["mt7921e"], "TEST_mt7921e")
        trial.cleanup()
        self.assert_stock_restored()

    def test_halfway_insmod_failure_rolls_back_stock_modules(self):
        self.world.fail_once = ("insmod", str(self.world.modules_dir / "mt792x-lib.ko"))
        trial = module.DriverTrial("wlan0")
        with self.assertRaises(RuntimeError):
            trial.start()
        trial.cleanup()
        self.assert_stock_restored()

    def test_halfway_unload_failure_rolls_back_stock_modules(self):
        self.world.fail_once = ("rmmod", "mt76_connac_lib")
        trial = module.DriverTrial("wlan0")
        with self.assertRaises(RuntimeError):
            trial.start()
        trial.cleanup()
        self.assert_stock_restored()

    def test_final_pci_module_insmod_failure_rolls_back_stock_modules(self):
        self.world.fail_once = ("insmod", str(self.world.modules_dir / "mt7921e.ko"))
        trial = module.DriverTrial("wlan0")
        with self.assertRaises(RuntimeError):
            trial.start()
        trial.cleanup()
        self.assert_stock_restored()

    def test_pci_module_hash_mismatch_refuses_before_network_change(self):
        (self.world.modules_dir / "mt7921e.ko").write_bytes(b"tampered PCI module")
        self.assert_refused_without_mutation()

    def test_prior_unmanaged_down_state_is_preserved(self):
        self.world.managed, self.world.up = "no", False
        self.world.create_interface("wlan0")
        trial = module.DriverTrial("wlan0")
        trial.start()
        trial.cleanup()
        self.assertEqual(self.world.loaded, self.world.disk_versions)
        self.assertEqual(self.world.managed, "no")
        self.assertFalse(self.world.up)

    def test_missing_module_refuses_before_network_change(self):
        (self.world.modules_dir / "mt792x-lib.ko").unlink()
        self.assert_refused_without_mutation()

    def test_kernel_mismatch_refuses_before_network_change(self):
        p, m = self.manifest()
        m["kernel"] = "some-other-kernel"
        p.write_text(json.dumps(m))
        self.assert_refused_without_mutation()

    def test_hash_mismatch_refuses_before_network_change(self):
        (self.world.modules_dir / "mac80211.ko").write_bytes(b"tampered")
        self.assert_refused_without_mutation()

    def test_vermagic_mismatch_refuses_before_network_change(self):
        p, m = self.manifest()
        m["modules"]["mac80211.ko"]["vermagic"] = "wrong"
        p.write_text(json.dumps(m))
        self.assert_refused_without_mutation()

    def test_unknown_holder_refuses_before_network_change(self):
        self.world.sys_path("/sys/module/mac80211/holders/foreign_wifi").touch()
        self.assert_refused_without_mutation()

    def test_two_radios_refuse_before_network_change(self):
        self.world.sys_path("/sys/class/ieee80211/phy1").mkdir()
        self.assert_refused_without_mutation()

    def test_interface_is_reidentified_by_mac_if_name_changes(self):
        self.world.rename_device = True
        trial = module.DriverTrial("wlan0")
        self.assertEqual(trial.start(), "wlp2s0")
        trial.cleanup()
        self.assert_stock_restored()

    def test_remove_never_prunes_cfg80211_or_libarc4_dependencies(self):
        self.world.recursive_remove = True
        trial = module.DriverTrial("wlan0")
        trial.start()
        self.assertTrue({"cfg80211", "libarc4"} <= set(self.world.loaded))
        removed = [c[-1] for c in self.world.commands if c[0] == "rmmod" or c[:2] == ("modprobe", "-r")]
        self.assertTrue(set(removed) <= set(trial.MODULES))
        trial.cleanup()
        self.assert_stock_restored()

    def test_preexisting_custom_loaded_module_is_not_replaced(self):
        self.world.loaded["mac80211"] = "SOME_OTHER_CUSTOM_MODULE"
        self.world.sync_modules()
        self.assert_refused_without_mutation()


if __name__ == "__main__":
    unittest.main(verbosity=2)
