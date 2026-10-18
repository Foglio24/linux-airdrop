"""Radio plans depend on observed capabilities, never a native kernel allowlist."""
from dataclasses import replace
import hashlib
import itertools
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import radio_compat as rc


NATIVE = rc.RadioCapabilities("mt7921e", True, True, True, pci_id="0x0616")
LEGACY = rc.LegacyBundle(verified=True, reason="Verified bundle")
IW = """Wiphy phy5
    Supported interface modes:
         * managed
         * monitor
         * P2P-GO
    Device supports active monitor (which will ACK incoming frames)
"""


class RadioPlanTests(unittest.TestCase):
    def test_native_requires_the_privileged_flag_probe_before_claiming_ready(self):
        plan = rc.select_plan(NATIVE)
        self.assertEqual((plan.mode, plan.state), ("native", "probe_required"))
        self.assertTrue(plan.available)
        self.assertTrue(plan.requires_probe)
        self.assertFalse(plan.ready)
        self.assertIn("disattivato", " ".join(plan.details))

    def test_only_observed_supported_flags_produce_native_ready(self):
        plan = rc.select_plan(replace(NATIVE, skip_tx="supported"))
        self.assertEqual((plan.mode, plan.state), ("native", "ready"))
        self.assertTrue(plan.ready)
        self.assertFalse(plan.requires_probe)
        self.assertIn("prova reale", " ".join(plan.details))

    def test_native_is_preferred_to_a_matching_legacy_bundle(self):
        for probe in rc.PROBE_STATES:
            with self.subTest(probe=probe):
                self.assertEqual(rc.select_plan(replace(NATIVE, skip_tx=probe), LEGACY).mode, "native")

    def test_all_native_requirements_are_independent(self):
        for monitor, active, go, skip in itertools.product((False, True), (False, True), (False, True), rc.PROBE_STATES):
            with self.subTest(monitor=monitor, active=active, go=go, skip=skip):
                cap = replace(NATIVE, monitor=monitor, active_monitor=active, p2p_go=go, skip_tx=skip)
                plan = rc.select_plan(cap)
                self.assertEqual(plan.mode == "native", monitor and active and go)

    def test_missing_capabilities_have_specific_messages(self):
        changes = (({"monitor": False}, "monitor_missing"),
                   ({"active_monitor": False}, "active_monitor_missing"),
                   ({"p2p_go": False}, "p2p_go_missing"))
        for values, code in changes:
            with self.subTest(values=values):
                plan = rc.select_plan(replace(NATIVE, **values))
                self.assertEqual((plan.mode, plan.code), ("blocked", code))
                self.assertFalse(plan.available)

    def test_unknown_facts_do_not_trigger_driver_replacement(self):
        for name in ("monitor", "active_monitor", "p2p_go"):
            with self.subTest(name=name):
                plan = rc.select_plan(replace(NATIVE, **{name: None}), LEGACY)
                self.assertEqual(plan.code, "capabilities_unknown")

    def test_base_tools_missing_prevent_legacy_downgrade(self):
        plan = rc.select_plan(replace(NATIVE, active_monitor=False, missing_tools=("iw", "ip", "iw")), LEGACY)
        self.assertEqual(plan.code, "tools_missing")
        self.assertEqual(plan.details, ("ip", "iw"))

    def test_wrong_or_missing_driver_and_unsupported_hardware_are_blocked(self):
        for values, code in (({"driver": None}, "driver_missing"),
                             ({"driver": "iwlwifi"}, "hardware_unsupported"),
                             ({"hardware_supported": False}, "hardware_unsupported")):
            with self.subTest(values=values):
                self.assertEqual(rc.select_plan(replace(NATIVE, **values), LEGACY).code, code)

    def test_legacy_is_available_only_after_verified_checks(self):
        cap = replace(NATIVE, active_monitor=False)
        for bundle in (None, rc.LegacyBundle(), rc.LegacyBundle(reason="Hash mismatch")):
            with self.subTest(bundle=bundle):
                self.assertEqual(rc.select_plan(cap, bundle).mode, "blocked")
        self.assertEqual(rc.select_plan(cap, LEGACY).mode, "legacy")

    def test_observed_old_api_uses_native_active_only_without_loading_modules(self):
        plan = rc.select_plan(replace(NATIVE, skip_tx="unsupported"), LEGACY)
        self.assertEqual((plan.mode, plan.state, plan.code), ("native", "ready", "native_legacy_api"))
        self.assertFalse(plan.requires_probe)
        self.assertIn("tx_mntrs", " ".join(plan.details))
        self.assertIn("OWL ignora", " ".join(plan.details))

    def test_mt7921_is_a_native_candidate_but_current_legacy_patch_is_mt7922_only(self):
        for pci in ("0x7961", "0x0608"):
            cap = replace(NATIVE, pci_id=pci)
            self.assertEqual(rc.select_plan(cap).mode, "native")
            self.assertEqual(rc.select_plan(replace(cap, active_monitor=False), LEGACY).mode, "blocked")

    def test_unknown_pci_cannot_use_legacy_even_when_caller_knows_the_family(self):
        self.assertEqual(rc.select_plan(replace(NATIVE, active_monitor=False, pci_id=None), LEGACY).mode, "blocked")

    def test_pci_ids_are_compared_without_letter_case(self):
        custom = replace(LEGACY, hardware_ids=("0x0616", "0xabcd"))
        self.assertEqual(rc.select_plan(replace(NATIVE, active_monitor=False, pci_id="0xABCD"), custom).mode, "legacy")

    def test_future_explicit_bundle_hardware_metadata_is_used(self):
        bundle = replace(LEGACY, hardware_ids=("0x7961",))
        self.assertEqual(rc.select_plan(replace(NATIVE, active_monitor=False, pci_id="0x7961"), bundle).mode, "legacy")
        self.assertEqual(rc.select_plan(replace(NATIVE, active_monitor=False), bundle).mode, "blocked")

    def test_invalid_probe_result_is_rejected(self):
        for value in ("yes", "", True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                replace(NATIVE, skip_tx=value)

    def test_diagnostic_payload_is_json_serializable(self):
        report = rc.select_plan(NATIVE).as_dict()
        self.assertEqual(json.loads(json.dumps(report))["state"], "probe_required")

    def test_parser_extracts_capabilities_and_ignores_mentions_in_other_lines(self):
        self.assertEqual(rc.parse_iw_capabilities(IW), {"monitor": True, "active_monitor": True, "p2p_go": True})
        self.assertEqual(rc.parse_iw_capabilities("Wiphy phy1\n * managed\n note: P2P-GO\n * monitor-extra\n"),
                         {"monitor": False, "active_monitor": False, "p2p_go": False})

    def test_parser_empty_or_failed_output_is_unknown(self):
        for text in (None, "", "command failed: Operation not supported", "Device supports active monitor"):
            with self.subTest(text=text):
                self.assertEqual(rc.parse_iw_capabilities(text), {"monitor": None, "active_monitor": None, "p2p_go": None})


class BundleWorld:
    kernel = "example-release-with-any-vendor-suffix"
    vermagic = kernel + " SMP preempt mod_unload modversions"

    def __init__(self, root):
        self.directory, self.module_root = root / "driver-compat", root / "sys/module"
        self.directory.mkdir()
        self.module_root.mkdir(parents=True)
        self.manifest = {"kernel": self.kernel, "modules": {}}
        self.info, self.calls = {}, []
        for index, name in enumerate(rc.LEGACY_FILES):
            path = self.directory / name
            payload = ("test module " + name).encode()
            path.write_bytes(payload)
            source = str(index + 1) * 24
            self.manifest["modules"][name] = {"sha256": hashlib.sha256(payload).hexdigest(),
                                                "vermagic": self.vermagic, "srcversion": source}
            self.info[("vermagic", str(path))] = self.vermagic
            self.info[("srcversion", str(path))] = source
        self.save()

    def save(self):
        (self.directory / "manifest.json").write_text(json.dumps(self.manifest))

    def loaded(self, name, version="original", holders=()):
        path = self.module_root / name
        (path / "holders").mkdir(parents=True)
        for holder in holders:
            (path / "holders" / holder).touch()
        (path / "srcversion").write_text(version)
        self.info[("srcversion", name)] = version

    def run(self, argv, timeout):
        self.calls.append((argv, timeout))
        value = self.info.get((argv[2], argv[3]))
        return (0, value + "\n") if value is not None else (1, "module not found")

    def verify(self, **kwargs):
        return rc.verify_legacy_bundle(self.directory, self.kernel, run=kwargs.get("run", self.run),
                                       module_root=self.module_root)


class LegacyVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.world = BundleWorld(Path(self.temp.name))

    def test_matching_hash_abi_and_source_versions_are_verified_read_only(self):
        for name in rc.LEGACY_MODULES:
            self.world.loaded(name)
        result = self.world.verify()
        self.assertTrue(result.verified)
        self.assertEqual(result.hardware_ids, rc.LEGACY_PCI_IDS)
        self.assertEqual(len(self.world.calls), 12)
        for argv, timeout in self.world.calls:
            self.assertEqual(argv[:2], ["modinfo", "-F"])
            self.assertEqual(timeout, 3)

    def test_kernel_mismatch_does_not_even_run_modinfo(self):
        result = rc.verify_legacy_bundle(self.world.directory, "another-kernel", run=self.world.run)
        self.assertEqual(result.code, "legacy_kernel_mismatch")
        self.assertEqual(self.world.calls, [])

    def test_only_hash_and_existing_files_are_insufficient(self):
        del self.world.manifest["modules"]["mac80211.ko"]["srcversion"]
        self.world.save()
        self.assertFalse(self.world.verify().verified)

    def test_modified_module_is_rejected_before_modinfo(self):
        (self.world.directory / "mac80211.ko").write_bytes(b"changed")
        self.assertEqual(self.world.verify().code, "legacy_hash_mismatch")
        self.assertEqual(self.world.calls, [])

    def test_module_missing_or_symlink_is_rejected(self):
        path = self.world.directory / "mac80211.ko"
        path.unlink()
        self.assertEqual(self.world.verify().code, "legacy_module_missing")
        target = self.world.directory / "untrusted"
        target.write_text("contents")
        path.symlink_to(target)
        self.assertEqual(self.world.verify().code, "legacy_module_missing")

    def test_lying_manifest_vermagic_cannot_approve_another_kernel_module(self):
        path = self.world.directory / "mac80211.ko"
        self.world.info[("vermagic", str(path))] = "other-kernel SMP"
        self.assertEqual(self.world.verify().code, "legacy_vermagic_mismatch")

    def test_manifest_vermagic_must_contain_the_actual_running_release(self):
        self.world.manifest["modules"]["mac80211.ko"]["vermagic"] = "other-kernel SMP"
        self.world.save()
        self.assertEqual(self.world.verify().code, "legacy_manifest_invalid")

    def test_bundle_srcversion_is_compared_with_modinfo(self):
        path = self.world.directory / "mt7921e.ko"
        self.world.info[("srcversion", str(path))] = "another source version"
        self.assertEqual(self.world.verify().code, "legacy_srcversion_mismatch")

    def test_loaded_modules_must_match_original_distribution_files(self):
        self.world.loaded("mt7921e", "modified")
        self.world.info[("srcversion", "mt7921e")] = "original"
        self.assertEqual(self.world.verify().code, "legacy_stock_mismatch")

    def test_module_used_by_another_driver_is_rejected(self):
        self.world.loaded("mac80211", holders=("iwlwifi",))
        self.assertEqual(self.world.verify().code, "legacy_module_in_use")

    def test_expected_dependency_holders_are_allowed(self):
        self.world.loaded("mac80211", holders=("mt76",))
        self.assertTrue(self.world.verify().verified)

    def test_missing_modinfo_and_timeouts_fail_closed(self):
        runners = (lambda *_: (None, "not available"), lambda *_: (1, "error"))
        for run in runners:
            with self.subTest(run=run):
                self.assertEqual(self.world.verify(run=run).code, "legacy_modinfo_failed")
        for exception in (TimeoutError(), subprocess.TimeoutExpired("modinfo", 3), FileNotFoundError()):
            def run(*_, exception=exception):
                raise exception
            with self.subTest(exception=exception):
                self.assertEqual(self.world.verify(run=run).code, "legacy_modinfo_failed")

    def test_malformed_or_incomplete_manifests_fail_closed(self):
        for content in ("broken", "[]", '{"kernel": "x"}', "null"):
            with self.subTest(content=content):
                (self.world.directory / "manifest.json").write_text(content)
                self.assertFalse(self.world.verify().verified)

    def test_future_manifest_can_state_an_explicit_hardware_scope(self):
        self.world.manifest["hardware_ids"] = ["0x0616", "0x7961"]
        self.world.save()
        self.assertEqual(self.world.verify().hardware_ids, ("0x0616", "0x7961"))

    def test_invalid_hardware_scope_is_rejected(self):
        for value in ([], ["all"], "0x0616", [None]):
            with self.subTest(value=value):
                self.world.manifest["hardware_ids"] = value
                self.world.save()
                self.assertEqual(self.world.verify().code, "legacy_manifest_invalid")

    def test_default_modinfo_runner_uses_a_fixed_system_environment(self):
        with patch.object(rc.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "version\n", "")) as run:
            self.assertEqual(rc._run_readonly(["modinfo", "-F", "srcversion", "mt7921e"], 3), (0, "version\n"))
        self.assertEqual(run.call_args.kwargs["env"], rc.READONLY_ENV)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 3)


if __name__ == "__main__":
    unittest.main()
