"""AirDrop mDNS must leave awdl0 even when the Wi-Fi Direct group shares its link-local address."""
from pathlib import Path
import re
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPER = Path(__file__).resolve().parent / "ns_duplicate_iface.py"


def run(mode):
    try:
        done = subprocess.run(["unshare", "-Urn", "/usr/bin/python3", "-I", "-B", str(HELPER), mode],
                              capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise unittest.SkipTest("network namespace unavailable: " + str(exc))
    if "adapter awdl0" not in done.stdout:
        raise unittest.SkipTest("cannot create dummy interfaces here: " + (done.stderr or done.stdout)[-200:])
    return {name: int(count) for name, count in re.findall(r"leaving (\w+): (\d+)", done.stdout)}


class ZeroconfInterfaceSelection(unittest.TestCase):
    def test_selecting_by_address_picks_the_lower_index_interface(self):
        # The bug: documents why OpenDrop must not pass the address to zeroconf.
        counts = run("old")
        self.assertGreater(counts["p2p0"], 0)
        self.assertEqual(counts["awdl0"], 0)

    def test_selecting_by_index_announces_on_awdl0_only(self):
        counts = run("new")
        self.assertGreater(counts["awdl0"], 0)
        self.assertEqual(counts["p2p0"], 0)

    def test_bundled_opendrop_selects_the_interface_by_index(self):
        for name, variable in (("server.py", "self.config"), ("client.py", "config")):
            source = (ROOT / "backend/python/opendrop" / name).read_text()
            self.assertIn(f"interfaces=[socket.if_nametoindex({variable}.interface)]", source)
            self.assertNotIn("interfaces=[str(self.ip_addr)]", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
