"""Fingerprint of the Apple-signed sender record, checked with a locally generated test CA."""
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest

BUNDLE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BUNDLE))
import identity


def run(*argv, cwd):
    subprocess.run(argv, cwd=cwd, check=True, capture_output=True)


@unittest.skipUnless(shutil.which("openssl"), "openssl not available")
class RecordFingerprint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        d = cls.dir = Path(cls.temp.name)
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
            "-subj", "/CN=Test Root", "-days", "2", cwd=d)
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "other.key", "-out", "other.pem",
            "-subj", "/CN=Another Root", "-days", "2", cwd=d)
        run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "s.key", "-out", "s.csr", "-subj", "/CN=Signer", cwd=d)
        run("openssl", "x509", "-req", "-in", "s.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "s.pem", "-days", "2", cwd=d)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def record(self, payload):
        (self.dir / "payload").write_bytes(plistlib.dumps(payload))
        run("openssl", "cms", "-sign", "-binary", "-in", "payload", "-signer", "s.pem", "-inkey", "s.key",
            "-certfile", "ca.pem", "-outform", "DER", "-out", "record.cms", "-nodetach", cwd=self.dir)
        return (self.dir / "record.cms").read_bytes()

    PAYLOAD = {"ValidatedEmailHashes": ["a" * 64, "b" * 64], "ValidatedPhoneHashes": [bytes(range(32))]}

    def test_fingerprint_is_stable_and_independent_of_order(self):
        first = identity.inspect(self.record(self.PAYLOAD), self.dir / "ca.pem")
        shuffled = dict(self.PAYLOAD, ValidatedEmailHashes=["b" * 64, "a" * 64])
        second = identity.inspect(self.record(shuffled), self.dir / "ca.pem")
        self.assertTrue(first["verified"])
        self.assertEqual(len(first["fingerprint"]), 32)
        self.assertEqual(first["fingerprint"], second["fingerprint"])

    def test_different_account_gives_a_different_fingerprint(self):
        a = identity.inspect(self.record(self.PAYLOAD), self.dir / "ca.pem")["fingerprint"]
        b = identity.inspect(self.record({"ValidatedEmailHashes": ["c" * 64]}), self.dir / "ca.pem")["fingerprint"]
        self.assertNotEqual(a, b)

    def test_unknown_signer_is_reported_unverified_but_still_fingerprinted(self):
        result = identity.inspect(self.record(self.PAYLOAD), self.dir / "other.pem")
        self.assertFalse(result["verified"])
        self.assertIsNotNone(result["fingerprint"])

    def test_payload_without_contact_hashes_gives_no_fingerprint(self):
        self.assertIsNone(identity.inspect(self.record({"Something": "else"}), self.dir / "ca.pem")["fingerprint"])

    def test_garbage_empty_and_oversized_records_are_harmless(self):
        for blob in (b"garbage" * 100, b"", None, "text", b"\x30\x80" + b"\0" * 50, b"x" * (identity.MAX_RECORD + 1)):
            result = identity.inspect(blob, self.dir / "ca.pem")
            self.assertIsNone(result["fingerprint"])
            self.assertFalse(result["verified"])

    def test_payload_parser_ignores_junk(self):
        self.assertIsNone(identity.fingerprint_from_payload(b"not a plist"))
        self.assertIsNone(identity.fingerprint_from_payload(plistlib.dumps([1, 2])))
        self.assertIsNone(identity.fingerprint_from_payload(plistlib.dumps({"ValidatedEmailHashes": ["short"]})))


if __name__ == "__main__":
    unittest.main(verbosity=2)
