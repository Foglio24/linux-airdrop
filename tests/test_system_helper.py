"""The passwordless helper must be root-owned, minimal, tamper-evident and limited to one user.

Everything runs against a temporary prefix: no root, no real /usr/local or /etc is touched.
"""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

MODULE = Path(__file__).resolve().parents[1] / "system_helper.py"
spec = importlib.util.spec_from_file_location("system_helper_under_test", MODULE)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundle = self.root / "bundle"
        for name in helper.FILES:
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("content of " + name).encode())
        self.prefix = self.root / "fs"
        self.where = helper.paths(self.prefix)

    def install(self, user="foglio"):
        return helper.install(user, bundle=self.bundle, prefix=self.prefix, own=False)

    def status(self, user="foglio", authorize=None):
        return helper.status(user, bundle=self.bundle, prefix=self.prefix, owner_uid=os.getuid(), authorize=authorize)


class InstallLayout(Fixture):
    def test_installs_exactly_the_root_side_files_and_nothing_else(self):
        folder = self.install()
        found = {str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file()}
        self.assertEqual(found, set(helper.FILES) | {helper.ENTRY, helper.MANIFEST})

    def test_entry_point_runs_only_the_bridge_in_isolated_mode(self):
        folder = self.install()
        script = (folder / helper.ENTRY).read_text()
        self.assertIn('exec /usr/bin/python3 -I -B /usr/local/lib/linux-airdrop/root_bridge.py "$@"', script)
        self.assertEqual(script.count("exec"), 1)
        self.assertTrue((folder / helper.ENTRY).stat().st_mode & stat.S_IXUSR)

    def test_nothing_is_writable_by_group_or_others_and_modes_are_right(self):
        folder = self.install()
        for item in [folder, *folder.rglob("*")]:
            self.assertFalse(item.lstat().st_mode & (stat.S_IWGRP | stat.S_IWOTH), item)
            self.assertFalse(item.is_symlink(), item)
        self.assertTrue((folder / "backend/owl-modern").stat().st_mode & stat.S_IXUSR)
        self.assertFalse((folder / "backend/driver-compat/mt7921e.ko").stat().st_mode & stat.S_IXUSR)

    def test_manifest_records_the_hash_of_every_installed_file_and_the_user(self):
        folder = self.install()
        manifest = json.loads((folder / helper.MANIFEST).read_text())
        self.assertEqual(manifest["user"], "foglio")
        for name, digest in manifest["files"].items():
            self.assertEqual(helper.sha256(folder / name), digest)

    def test_polkit_policy_authorises_only_the_entry_point_path(self):
        self.install()
        root = ET.fromstring(self.where["policy"].read_text())
        action = root.find("action")
        self.assertEqual(action.get("id"), helper.ACTION_ID)
        annotation = {a.get("key"): a.text for a in action.findall("annotate")}
        self.assertEqual(annotation["org.freedesktop.policykit.exec.path"], "/usr/local/lib/linux-airdrop/airdrop-bridge")
        defaults = {d.tag: d.text for d in action.find("defaults")}
        self.assertEqual(defaults["allow_any"], "no")
        self.assertEqual(defaults["allow_inactive"], "no")
        self.assertEqual(defaults["allow_active"], "auth_admin_keep")  # anyone else still needs the admin password

    def test_rule_names_one_local_active_user_and_one_action(self):
        self.install("foglio")
        rule = self.where["rule"].read_text()
        self.assertIn(f'action.id == "{helper.ACTION_ID}"', rule)
        self.assertIn('subject.user == "foglio"', rule)
        self.assertIn("subject.local && subject.active", rule)
        self.assertEqual(rule.count("polkit.Result.YES"), 1)
        self.assertNotIn("indexOf", rule)

    def test_reinstall_replaces_the_previous_copy_without_leftovers(self):
        self.install("foglio")
        (self.bundle / "root_bridge.py").write_text("new version")
        self.install("foglio")
        self.assertEqual((self.where["dir"] / "root_bridge.py").read_text(), "new version")
        self.assertEqual([p.name for p in self.where["dir"].parent.iterdir()], [self.where["dir"].name])

    def test_uninstall_removes_everything(self):
        self.install()
        helper.uninstall(self.prefix)
        for path in self.where.values():
            self.assertFalse(path.exists())


class InstallRefusals(Fixture):
    def test_a_symlinked_file_in_the_bundle_is_never_followed(self):
        secret = self.root / "secret"
        secret.write_text("not for root's helper")
        target = self.bundle / "p2p_anchor.py"
        target.unlink()
        target.symlink_to(secret)
        with self.assertRaises(helper.HelperError):
            self.install()
        self.assertFalse(self.where["dir"].exists())
        self.assertFalse(self.where["rule"].exists())

    def test_a_missing_file_leaves_an_existing_install_untouched(self):
        self.install()
        before = (self.where["dir"] / "root_bridge.py").read_text()
        (self.bundle / "awdl_diag.py").unlink()
        with self.assertRaises(helper.HelperError):
            self.install()
        self.assertEqual((self.where["dir"] / "root_bridge.py").read_text(), before)
        self.assertEqual(self.status(), "outdated")  # the bundle is incomplete now, so the helper is not current...
        (self.bundle / "awdl_diag.py").write_text("content of awdl_diag.py")
        self.assertEqual(self.status(), "ready")  # ...but the installed copy itself was never damaged

    def test_needs_administrator_rights_unless_staging(self):
        with patch.object(helper.os, "geteuid", return_value=1000):
            with self.assertRaises(helper.HelperError):
                helper.install("foglio", bundle=self.bundle, prefix=self.prefix, own=True)

    def test_user_names_that_could_inject_into_the_rule_are_refused(self):
        for bad in ('foglio"; polkit.Result.YES; "', "../x", "Foglio", "", "a b", "x" * 40, "root\n"):
            with self.assertRaises(helper.HelperError, msg=bad):
                helper.rule_js(bad)

    def test_only_a_normal_existing_user_who_started_the_install_can_be_authorised(self):
        with self.assertRaisesRegex(helper.HelperError, "normale"):
            helper.resolve_user("root", environ={})
        with self.assertRaisesRegex(helper.HelperError, "inesistente"):
            helper.resolve_user("no_such_user_zz", environ={})
        me = helper.pwd.getpwuid(os.getuid()).pw_name
        if os.getuid() >= 1000 and helper.USER.fullmatch(me):
            self.assertEqual(helper.resolve_user(me, environ={}), me)
            self.assertEqual(helper.resolve_user(None, environ={"PKEXEC_UID": str(os.getuid())}), me)
            with self.assertRaisesRegex(helper.HelperError, "avvia"):
                helper.resolve_user(me, environ={"SUDO_UID": "0"})


class Status(Fixture):
    def test_missing_before_install(self):
        self.assertEqual(self.status(), "missing")

    def test_ready_after_install(self):
        self.install()
        self.assertEqual(self.status(), "ready")

    def test_other_user_when_installed_for_someone_else(self):
        self.install("alice")
        self.assertEqual(self.status("foglio"), "other-user")

    def test_outdated_when_the_bundle_changes(self):
        self.install()
        (self.bundle / "backend/owl-modern").write_text("rebuilt")
        self.assertEqual(self.status(), "outdated")

    def test_unsafe_when_an_installed_file_is_tampered_with(self):
        self.install()
        target = self.where["dir"] / "root_bridge.py"
        target.chmod(0o644)
        target.write_text("evil")
        self.assertEqual(self.status(), "unsafe")

    def test_unsafe_when_anyone_else_can_write_to_the_helper_folder(self):
        self.install()
        self.where["dir"].chmod(0o777)
        self.assertEqual(self.status(), "unsafe")

    def test_unsafe_when_not_owned_by_root(self):
        self.install()
        self.assertEqual(helper.status("foglio", bundle=self.bundle, prefix=self.prefix, owner_uid=0), "unsafe")

    def test_missing_when_the_polkit_rule_is_gone(self):
        self.install()
        self.where["rule"].unlink()
        self.assertEqual(self.status(), "missing")

    def test_polkits_own_answer_decides_when_the_rule_cannot_be_read(self):
        # Regression: /etc/polkit-1/rules.d is root-only, so the app used to report "missing" for a working helper.
        self.install()
        self.assertEqual(self.status(authorize=lambda: True), "ready")
        self.assertEqual(self.status(authorize=lambda: False), "missing")

    def test_real_mode_asks_polkit_and_never_looks_for_the_rule_file(self):
        folder = self.install()
        calls = []
        with patch.object(helper, "paths", return_value={**self.where, "rule": self.root / "unreadable/rule"}), \
                patch.object(helper.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertEqual(helper.status("foglio", bundle=self.bundle, prefix=None, owner_uid=os.getuid()), "ready")
            self.assertEqual(run.call_args[0][0][:3], ["pkcheck", "--action-id", helper.ACTION_ID])
            run.return_value.returncode = 2   # polkit would have to ask for a password
            self.assertEqual(helper.status("foglio", bundle=self.bundle, prefix=None, owner_uid=os.getuid()), "missing")

    def test_pkcheck_failures_mean_not_ready(self):
        self.install()
        with patch.object(helper, "paths", return_value=self.where):
            for failure in (FileNotFoundError(), helper.subprocess.TimeoutExpired("pkcheck", 10)):
                with patch.object(helper.subprocess, "run", side_effect=failure):
                    self.assertEqual(helper.status("foglio", bundle=self.bundle, prefix=None, owner_uid=os.getuid()), "missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)
