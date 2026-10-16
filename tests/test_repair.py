"""Detection of known faults, the attempt policy that prevents restart loops, and the stray-process cleanup."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import diagnostics as dg
import repair
from test_diagnostics import World


def problems(world, facts, known=()):
    checks = dg.check_all(world.system, facts, dg.slow_facts(world.system), base=world.base)
    return {p.id: p for p in repair.detect(checks, facts, world.system, known)}


class Detection(unittest.TestCase):
    def setUp(self):
        self.world = World()

    def tearDown(self):
        self.world.close()

    def visible(self, **extra):
        self.world.session()
        facts = self.world.facts(session_active=True, receiver_pid=200, receiver_ifindex=9, announce_age=3.0, ble_running=True)
        facts.update(extra)
        return facts

    def test_a_healthy_machine_has_no_problems(self):
        self.assertEqual(problems(self.world, self.world.facts()), {})
        self.assertEqual(problems(self.world, self.visible()), {})

    def test_stale_interface_index_is_fixed_by_restarting_only_the_receiver(self):
        found = problems(self.world, self.visible(receiver_ifindex=3))
        self.assertEqual(list(found), ["mdns-interface"])
        self.assertEqual((found["mdns-interface"].fix, found["mdns-interface"].risky), ("restart_receiver", False))

    def test_silent_service_and_dead_receiver_only_restart_the_receiver(self):
        found = problems(self.world, self.visible(announce_age=500))
        self.assertEqual(found["service-silent"].fix, "restart_receiver")
        facts = self.visible(receiver_pid=None)
        self.world.write("proc/net/tcp6", "  sl\n")
        self.assertEqual(problems(self.world, facts)["receiver-down"].fix, "restart_receiver")

    def test_wifi_direct_not_initialised_needs_a_confirmed_session_restart(self):
        facts = self.visible()
        import shutil
        shutil.rmtree(self.world.root / "sys/class/net/p2p-wlp4s0-0")
        found = problems(self.world, facts)
        self.assertEqual((found["wifi-direct"].fix, found["wifi-direct"].risky), ("restart_session", True))

    def test_owl_and_awdl_failures(self):
        import shutil
        facts = self.visible()
        shutil.rmtree(self.world.root / "proc/100")
        self.assertEqual(problems(self.world, facts)["owl-init"].fix, "restart_session")
        self.world.process(100, ["/x/backend/owl-modern"])
        self.world.write("proc/net/if_inet6", "")
        self.assertEqual(problems(self.world, facts)["awdl-missing"].risky, True)

    def test_ble_down_restarts_only_the_bluetooth_signal(self):
        found = problems(self.world, self.visible(ble_running=False))
        self.assertEqual((found["ble-down"].fix, found["ble-down"].risky), ("restart_ble", False))

    def test_leftover_processes_are_listed_only_when_no_session_runs(self):
        self.world.process(300, ["/usr/bin/python3", "-I", "/app/backend/opendrop_runner.py", "receive"])
        self.world.process(301, ["/usr/bin/python3", "/app/backend/blewake.py"])
        self.world.process(302, ["/usr/bin/firefox"])
        found = problems(self.world, self.world.facts())
        self.assertEqual(found["stray-processes"].fix, "kill_strays")
        self.assertTrue(found["stray-processes"].risky)
        self.assertIn("opendrop_runner.py (300)", found["stray-processes"].why)
        self.assertNotIn("firefox", found["stray-processes"].why)
        self.assertEqual(problems(self.world, self.world.facts(), known=(300, 301)), {})
        self.assertNotIn("stray-processes", problems(self.world, self.visible()))

    def test_root_owned_leftovers_are_explained_not_killed(self):
        self.world.process(400, ["/usr/local/lib/linux-airdrop/backend/owl-modern", "-i", "ldack0"])
        mine, root_owned = repair.scan_strays(self.world.system, uid=123456)
        self.assertEqual((mine, root_owned), ([], [(400, "owl-modern")]))
        with unittest.mock.patch.object(repair.os, "getuid", return_value=123456):
            found = problems(self.world, self.world.facts())
        self.assertEqual(found["stray-root"].fix, "none")

    def test_blocked_driver_means_reboot_and_nothing_else(self):
        found = problems(self.world, self.world.facts(recovery_needed=True))
        self.assertEqual(found["reboot"].fix, "none")
        self.world.modules("patched")
        self.world.write("sys/module/mt7921e/srcversion", "O3\n")
        self.assertIn("modules-mixed", problems(self.world, self.world.facts()))


class KillStrays(unittest.TestCase):
    def test_only_our_own_processes_are_ended_and_the_name_is_rechecked(self):
        with tempfile.TemporaryDirectory() as folder:
            fake = Path(folder) / "blewake.py"
            fake.write_text("import time\ntime.sleep(60)\n")
            ours = subprocess.Popen([sys.executable, str(fake)])
            other = subprocess.Popen(["sleep", "60"])
            try:
                ended = repair.kill_strays([ours.pid, other.pid, os.getpid(), 1], wait=2)
                self.assertEqual(ended, [ours.pid])
                self.assertIsNone(other.poll())          # not touched: not one of our programs
                self.assertEqual(ours.wait(timeout=5), -15)
            finally:
                for process in (ours, other):
                    if process.poll() is None:
                        process.kill()
                    process.wait()


class Policy(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1_000_000.0]
        self.attempts = repair.Attempts(self.temp.name, clock=lambda: self.now[0])

    def tearDown(self):
        self.temp.cleanup()

    def test_first_attempt_allowed_then_a_cooling_off_period(self):
        self.assertEqual(self.attempts.allowed("owl-init"), (True, ""))
        self.attempts.record("owl-init", "restart_session", "eseguito")
        allowed, reason = self.attempts.allowed("owl-init")
        self.assertFalse(allowed)
        self.assertIn("Riprova tra", reason)
        self.now[0] += repair.MIN_GAP + 1
        self.assertTrue(self.attempts.allowed("owl-init")[0])

    def test_it_stops_after_three_attempts_and_says_why(self):
        for _ in range(repair.MAX_ATTEMPTS):
            self.assertTrue(self.attempts.allowed("mdns-interface")[0])
            self.attempts.record("mdns-interface", "restart_receiver", "eseguito")
            self.now[0] += repair.MIN_GAP + 1
        allowed, reason = self.attempts.allowed("mdns-interface")
        self.assertFalse(allowed)
        self.assertIn("Non la ripeto", reason)
        self.assertTrue(self.attempts.allowed("ble-down")[0])          # other problems are unaffected

    def test_attempts_expire_and_survive_a_restart_of_the_app(self):
        for _ in range(repair.MAX_ATTEMPTS):
            self.attempts.record("x", "f", "ok")
            self.now[0] += repair.MIN_GAP + 1
        again = repair.Attempts(self.temp.name, clock=lambda: self.now[0])
        self.assertFalse(again.allowed("x")[0])
        self.now[0] += repair.WINDOW
        self.assertTrue(again.allowed("x")[0])

    def test_damaged_state_file_is_ignored(self):
        (Path(self.temp.name) / "repair-state.json").write_text("{not json")
        self.assertTrue(self.attempts.allowed("x")[0])
        (Path(self.temp.name) / "repair-state.json").write_text('{"x": "nope", "y": [1, "a", null]}')
        self.assertTrue(self.attempts.allowed("x")[0])

    def test_every_attempt_is_logged_and_the_log_is_bounded(self):
        self.attempts.record("owl-init", "restart_session", "eseguito\ncon due righe")
        line = self.attempts.log_file.read_text().splitlines()[0]
        self.assertIn("[owl-init] restart_session: eseguito con due righe", line)
        self.attempts.log_file.write_text("x" * (repair.LOG_LIMIT + 10))
        self.attempts.log("nuova riga")
        self.assertTrue((Path(self.temp.name) / "repair.log.1").exists())
        self.assertLess(self.attempts.log_file.stat().st_size, 1000)


if __name__ == "__main__":
    import unittest.mock
    unittest.main(verbosity=2)
