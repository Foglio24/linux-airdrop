#!/usr/bin/python3
"""The engine as a separate process, and its supervision by the privileged bridge (nothing privileged runs)."""
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import nexa_helpers as h
from test_nexaawdl_engine import IdealPeer

BUNDLE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("root_bridge_nexa_under_test", BUNDLE / "root_bridge.py")
bridge_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge_module)
REAL_PROCESS_OWNER = bridge_module.process_owner


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    return None


class EngineProcess(unittest.TestCase):
    def spawn(self, directory, sock):
        return subprocess.Popen([sys.executable, "-I", "-B", str(BUNDLE / "nexaawdl/run.py"), "--fd", str(sock.fileno()),
                                 "--dir", directory, "--iface", "ldmon0", "--level", "debug"],
                                pass_fds=(sock.fileno(),), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE)

    def read_status(self, directory):
        try:
            return json.loads((Path(directory) / "status.json").read_text())
        except (OSError, ValueError):
            return None

    def test_frames_in_status_out_and_clean_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            proc = self.spawn(tmp, theirs)
            theirs.close()
            try:
                peer = IdealPeer()
                t = int(time.time() * 1e6)
                for i in range(30):
                    ours.send(peer.action(t + i * 90_000))
                ours.send(b"\x00garbage")
                status = wait_for(lambda: (s := self.read_status(tmp)) and s["stats"].get("psf", 0) >= 30 and s)
                self.assertTrue(status, proc.stderr.read1().decode() if proc.poll() is not None else "no status")
                self.assertEqual(status["mode"], "observe")
                self.assertEqual(status["interface"], "ldmon0")
                self.assertEqual(status["peers"][0]["addr"], "02:aa:bb:cc:dd:01")
                self.assertGreaterEqual(status["stats"]["malformed"], 1)
                self.assertFalse(status["capabilities"]["transmit"])
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(proc.wait(timeout=10), 0)
                final = self.read_status(tmp)
                self.assertEqual(final["state"], "stopped")
                events = [json.loads(l) for l in (Path(tmp) / "events.jsonl").read_text().splitlines()]
                self.assertIn("peer_new", {e["kind"] for e in events})
            finally:
                ours.close()
                if proc.poll() is None:
                    proc.kill()
                proc.stderr.close()

    def test_a_closed_capture_socket_is_reported_and_exits_with_code_3(self):
        with tempfile.TemporaryDirectory() as tmp:
            ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            proc = self.spawn(tmp, theirs)
            theirs.close()
            try:
                self.assertTrue(wait_for(lambda: self.read_status(tmp)))
                ours.close()
                self.assertEqual(proc.wait(timeout=10), 3)
                final = self.read_status(tmp)
                self.assertEqual(final["state"], "error")
                self.assertIn("closed", final["errors"][-1]["message"])
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stderr.close()

    def test_no_new_privileges_is_set(self):
        script = ("import ctypes,sys;sys.path.insert(0,%r);from nexaawdl import run;run.harden();"
                  "print(ctypes.CDLL(None).prctl(39,0,0,0,0))" % str(BUNDLE))
        out = subprocess.run([sys.executable, "-I", "-B", "-c", script], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "1")                    # PR_GET_NO_NEW_PRIVS

    def test_event_log_is_bounded(self):
        from nexaawdl import run
        with tempfile.TemporaryDirectory() as tmp:
            log = run.EventLog(Path(tmp) / "events.jsonl")
            with patch.object(run, "EVENTS_LIMIT", 400):
                for i in range(60):
                    log({"i": i, "pad": "x" * 20})
            total = sum(p.stat().st_size for p in Path(tmp).iterdir())
            self.assertLess(total, 1000)
            self.assertTrue((Path(tmp) / "events.jsonl.1").exists())


class BridgeSupervision(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = str(Path(self.tmp.name) / "state")
        os.mkdir(self.dir)
        self.cfg = bridge_module.nexaawdl_config(self.dir, str(BUNDLE), "info")
        self.events = []
        patches = [patch.object(bridge_module, "emit", lambda kind, **info: self.events.append((kind, info))),
                   patch.object(bridge_module, "open_monitor_socket", lambda ifname: Mock(fileno=lambda: 9)),
                   patch.object(bridge_module, "process_owner", lambda pid: (self.cfg["uid"], self.cfg["gid"]))]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def bridge(self, config="default"):
        return bridge_module.Bridge("wlan0", 6, os.getpid(), nexaawdl=self.cfg if config == "default" else config)

    def messages(self):
        return [info["message"] for kind, info in self.events if kind == "log" and info.get("source") in ("nexaawdl", "cleanup")]

    def test_config_validation(self):
        good = bridge_module.nexaawdl_config(self.dir, str(BUNDLE), "debug")
        self.assertEqual(good["level"], "debug")
        for args in (("relative/dir", str(BUNDLE), "info"), (self.dir, "relative", "info"),
                     (self.dir, str(BUNDLE), "verbose"), (self.dir, str(BUNDLE / "tests"), "info")):
            with self.assertRaises(ValueError):
                bridge_module.nexaawdl_config(*args)
        with self.assertRaises(OSError):
            bridge_module.nexaawdl_config(self.dir + "/missing", str(BUNDLE), "info")
        regular = Path(self.tmp.name) / "file"
        regular.write_text("x")
        with self.assertRaises(ValueError):
            bridge_module.nexaawdl_config(str(regular), str(BUNDLE), "info")

    def test_symlinked_state_directory_is_not_followed(self):
        link = Path(self.tmp.name) / "link"
        link.symlink_to(self.dir)
        with self.assertRaises(ValueError):
            bridge_module.nexaawdl_config(str(link), str(BUNDLE), "info")

    def test_started_as_the_desktop_user_with_the_socket_inherited_and_no_other_files(self):
        b = self.bridge()
        with patch.object(bridge_module.subprocess, "Popen") as popen:
            b.start_nexaawdl()
        argv, kwargs = popen.call_args
        self.assertEqual(argv[0][:3], ["/usr/bin/python3", "-I", "-B"])
        self.assertEqual(argv[0][3], str(BUNDLE / "nexaawdl/run.py"))
        self.assertEqual(kwargs["user"], self.cfg["uid"])
        self.assertEqual(kwargs["group"], self.cfg["gid"])
        self.assertEqual(kwargs["extra_groups"], [])
        self.assertEqual(kwargs["pass_fds"], (9,))
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["cwd"], "/")
        self.assertNotIn("LD_PRELOAD", kwargs["env"])
        self.assertTrue(any("sola osservazione" in m for m in self.messages()))

    def test_falls_back_to_the_active_monitor_when_there_is_no_passive_one(self):
        b = self.bridge()
        tried = []

        def opener(name):
            tried.append(name)
            if name == "ldmon0":
                raise OSError("No such device")
            return Mock(fileno=lambda: 11)

        with patch.object(bridge_module, "open_monitor_socket", opener), \
                patch.object(bridge_module.subprocess, "Popen") as popen:
            b.start_nexaawdl()
        self.assertEqual(tried, ["ldmon0", "ldack0"])
        self.assertEqual(popen.call_args[0][0][popen.call_args[0][0].index("--iface") + 1], "ldack0")

    def test_no_monitor_at_all_is_not_fatal(self):
        b = self.bridge()
        with patch.object(bridge_module, "open_monitor_socket", side_effect=OSError("No such device")), \
                patch.object(bridge_module.subprocess, "Popen") as popen:
            b.start_nexaawdl()
        popen.assert_not_called()
        self.assertTrue(any("nessuna interfaccia monitor" in m for m in self.messages()))

    def test_never_runs_when_the_state_directory_belongs_to_root_or_another_user(self):
        b = self.bridge({**self.cfg, "uid": 0})
        with patch.object(bridge_module.subprocess, "Popen") as popen:
            b.start_nexaawdl()
        popen.assert_not_called()
        self.assertIsNone(b.nexaawdl)
        other = self.bridge()
        with patch.object(bridge_module, "process_owner", lambda pid: (self.cfg["uid"] + 1, 1)), \
                patch.object(bridge_module.subprocess, "Popen") as popen:
            other.start_nexaawdl()
        popen.assert_not_called()
        self.assertTrue(any("non appartiene" in m for m in self.messages()))

    def test_failure_to_start_is_not_fatal(self):
        b = self.bridge()
        with patch.object(bridge_module.subprocess, "Popen", side_effect=OSError("boom")):
            b.start_nexaawdl()                                  # must not raise
        self.assertIsNone(b.nexa)
        self.assertIsNone(b.nexaawdl)
        self.assertTrue(any("AirDrop continua con OWL" in m for m in self.messages()))

    def test_disabled_by_default(self):
        b = self.bridge(None)
        with patch.object(bridge_module.subprocess, "Popen") as popen:
            b.start_nexaawdl()
        popen.assert_not_called()

    def test_crashes_are_restarted_a_few_times_then_given_up(self):
        b = self.bridge()
        procs = []

        def crashed(*a, **k):
            p = Mock(returncode=3)
            p.poll.return_value = 3
            procs.append(p)
            return p

        with patch.object(bridge_module.subprocess, "Popen", side_effect=crashed):
            b.start_nexaawdl()
            for _ in range(10):
                b.watch_nexaawdl()
        self.assertEqual(len(procs), 1 + bridge_module.NEXA_RESTARTS)
        self.assertIsNone(b.nexaawdl)
        self.assertTrue(any("fermato" in m and "OWL" in m for m in self.messages()))

    def test_a_running_engine_is_left_alone(self):
        b = self.bridge()
        alive = Mock()
        alive.poll.return_value = None
        with patch.object(bridge_module.subprocess, "Popen", return_value=alive) as popen:
            b.start_nexaawdl()
            b.watch_nexaawdl()
            b.start_nexaawdl()                                  # no second engine
        self.assertEqual(popen.call_count, 1)

    def test_cleanup_stops_the_engine_before_anything_else(self):
        b = self.bridge()
        order = []
        proc = Mock()
        proc.poll.return_value = None
        proc.terminate.side_effect = lambda: order.append("terminate")
        proc.wait.return_value = 0
        sock = Mock()
        sock.close.side_effect = lambda: order.append("socket closed")
        b.nexa, b.nexa_sock = proc, sock
        b.owl = Mock()
        b.owl.poll.return_value = None
        b.owl.terminate.side_effect = lambda: order.append("owl terminate")
        b.owl.wait.return_value = 0
        with patch.object(bridge_module, "command"), patch.object(bridge_module, "RECOVERY") as recovery:
            recovery.is_set.return_value = False
            b.cleanup()
        self.assertEqual(order[:3], ["terminate", "socket closed", "owl terminate"])
        self.assertIsNone(b.nexa)

    def test_stop_kills_an_engine_that_ignores_sigterm(self):
        b = self.bridge()
        proc = Mock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("x", 3), 0]
        b.nexa, b.nexa_sock = proc, Mock()
        b.stop_nexaawdl()
        proc.kill.assert_called_once()

    def test_process_owner_reads_proc(self):
        owner = REAL_PROCESS_OWNER(os.getpid())
        self.assertEqual(owner, (os.getuid(), os.getgid()))
        self.assertIsNone(REAL_PROCESS_OWNER(0x7FFFFFFF))


if __name__ == "__main__":
    unittest.main(verbosity=2)
