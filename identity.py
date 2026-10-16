"""A stable, account-level fingerprint of the Apple-signed record a sender may attach to its requests.

Apple devices that are signed in to iCloud put a "validation record" in /Discover and /Ask (`SenderRecordData`):
a CMS signed document, issued by Apple, whose payload lists hashes of the sender's verified Apple ID contact
identifiers. This module turns such a record into a short fingerprint that stays the same between sessions, so
that a device can be recognised by something stronger than its display name.

What this is NOT: authentication. The record is a bearer document, not bound to the TLS session and not signed
by the device that presents it, so anyone who has ever received it (for instance from a phone set to receive
from "Everyone") can present it again. The fingerprint is therefore only a label for remembering devices. It is
never used to accept a transfer automatically (see docs/TRUSTED-DEVICES.md).

The payload layout is taken from published AirDrop research and has not yet been checked against a record from
the author's own phone; if the layout differs, `fingerprint()` returns None and the device is remembered by name
only (marked as such). Parsing uses the system `openssl` in a subprocess with a time limit.
"""
import hashlib
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

MAX_RECORD = 64 * 1024
HASH_KEYS = ("ValidatedEmailHashes", "ValidatedPhoneHashes", "SenderEmailHash", "SenderPhoneHash",
             "ValidatedEmailHash", "ValidatedPhoneHash")


def _collect(value, out):
    if isinstance(value, (bytes, bytearray)):
        out.add(bytes(value).hex())
    elif isinstance(value, str):
        out.add(value.lower())
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect(item, out)


def fingerprint_from_payload(payload):
    """Fingerprint (32 hex chars) of the contact hashes inside a record's plist payload, or None."""
    try:
        data = plistlib.loads(payload)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    hashes = set()
    for key in HASH_KEYS:
        _collect(data.get(key), hashes)
    hashes = {h for h in hashes if 16 <= len(h) <= 128}
    if not hashes:
        return None
    return hashlib.sha256("\n".join(sorted(hashes)).encode()).hexdigest()[:32]


def inspect(record, ca_file=None, timeout=5):
    """{'fingerprint': hex|None, 'verified': bool, 'present': True}. Never raises, never blocks longer than `timeout`.

    `verified` only says that Apple's signature chain checked out against `ca_file`; see the module note.
    """
    result = {"present": True, "fingerprint": None, "verified": False}
    openssl = shutil.which("openssl")
    if not isinstance(record, (bytes, bytearray)) or not record or len(record) > MAX_RECORD or not openssl:
        return result
    try:
        with tempfile.TemporaryDirectory(prefix="airdrop-record-") as folder:
            source, content = Path(folder) / "record.cms", Path(folder) / "payload"
            source.write_bytes(bytes(record))
            if ca_file and Path(ca_file).is_file():
                checked = subprocess.run([openssl, "cms", "-verify", "-inform", "DER", "-in", str(source), "-CAfile", str(ca_file),
                                          "-purpose", "any", "-out", str(content)], capture_output=True, timeout=timeout)
                result["verified"] = checked.returncode == 0 and content.is_file()
            if not result["verified"]:
                content.unlink(missing_ok=True)
                subprocess.run([openssl, "cms", "-verify", "-noverify", "-inform", "DER", "-in", str(source),
                                "-out", str(content)], capture_output=True, timeout=timeout)
            if content.is_file() and content.stat().st_size <= MAX_RECORD:
                result["fingerprint"] = fingerprint_from_payload(content.read_bytes())
    except (OSError, subprocess.SubprocessError):
        pass
    return result
