"""A run's AirDrop service must not outlive it on the phone for more than a couple of minutes."""
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/python"))
from zeroconf import ServiceInfo  # noqa: E402


class ServiceLifetime(unittest.TestCase):
    def test_bundled_opendrop_registers_short_lived_records(self):
        source = (ROOT / "backend/python/opendrop/server.py").read_text()
        self.assertIn("host_ttl=120", source)
        self.assertIn("other_ttl=120", source)

    def test_every_record_of_the_service_expires_in_two_minutes(self):
        info = ServiceInfo("_airdrop._tcp.local.", "abcdef123456._airdrop._tcp.local.", port=8771,
                           properties={b"flags": b"136"}, server="pc.local.", addresses=[bytes(16)],
                           host_ttl=120, other_ttl=120)
        records = [info.dns_pointer(), info.dns_service(), info.dns_text(), *info.dns_addresses()]
        self.assertEqual({record.ttl for record in records}, {120})


if __name__ == "__main__":
    unittest.main(verbosity=2)
