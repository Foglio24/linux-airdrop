#!/usr/bin/python3
"""Root-owned Wi-Fi helper for Linux AirDrop, so activating AirDrop needs no password prompt.

The bridge that drives the Wi-Fi card runs as root. Run from the user's own folder it is a
file any program of that user could rewrite, so it could never be authorised silently. This
module therefore copies exactly the files root needs into /usr/local/lib/linux-airdrop,
owned by root and writable by nobody else, and tells polkit that this one entry point may
be started by one named user without asking for a password.

    sudo python3 system_helper.py install     one-time setup (asks for the admin password once)
    sudo python3 system_helper.py uninstall   removes the helper, the polkit action and the rule
    python3 system_helper.py status           ready / missing / outdated / unsafe / other-user

Only the standard library is used, and everything here is safe to import without root.
"""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile

BUNDLE = Path(__file__).resolve().parent
ACTION_ID = "it.foglio.LinuxAirDrop.bridge"
HELPER_DIR = Path("/usr/local/lib/linux-airdrop")
POLICY = Path("/usr/share/polkit-1/actions/it.foglio.LinuxAirDrop.policy")
RULE = Path("/etc/polkit-1/rules.d/49-linux-airdrop.rules")
ENTRY = "airdrop-bridge"
MANIFEST = "helper-manifest.json"
# Everything root_bridge.py reads or executes. Nothing else is installed.
FILES = (
    "root_bridge.py", "p2p_anchor.py", "awdl_diag.py", "radio_compat.py",
    "backend/owl-modern", "backend/owl",
    "backend/driver-compat/manifest.json", "backend/driver-compat/mac80211.ko",
    "backend/driver-compat/mt792x-lib.ko", "backend/driver-compat/mt7921e.ko",
)
EXECUTABLE = {"backend/owl-modern", "backend/owl", ENTRY}
USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
MAX_FILE = 64 * 1024 * 1024


class HelperError(RuntimeError):
    pass


def paths(prefix=None):
    """The real locations, or the same layout under `prefix` (used by the tests)."""
    base = Path(prefix) if prefix else None

    def under(path):
        return base / path.relative_to("/") if base else path
    return {"dir": under(HELPER_DIR), "policy": under(POLICY), "rule": under(RULE)}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def bundle_hashes(bundle=BUNDLE):
    """Hashes of the files root would run, taken from the user's bundle."""
    result = {}
    for name in FILES:
        path = Path(bundle) / name
        try:
            info = path.lstat()
        except OSError as exc:
            raise HelperError(f"File mancante nel pacchetto: {name}") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise HelperError(f"Non è un file normale: {name}")
        result[name] = sha256(path)
    return result


def entry_script(helper_dir=HELPER_DIR):
    return ("#!/bin/sh\n# Installed by Linux AirDrop (system_helper.py). Owned by root: do not edit.\n"
            f"exec /usr/bin/python3 -I -B {helper_dir}/root_bridge.py \"$@\"\n")


def policy_xml(helper_dir=HELPER_DIR):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<policyconfig>
  <vendor>Linux AirDrop</vendor>
  <action id="{ACTION_ID}">
    <description>Gestire la scheda Wi-Fi per AirDrop</description>
    <message>Linux AirDrop vuole gestire la scheda Wi-Fi</message>
    <defaults>
      <allow_any>no</allow_any>
      <allow_inactive>no</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{helper_dir}/{ENTRY}</annotate>
  </action>
</policyconfig>
"""


def rule_js(user):
    if not USER.fullmatch(user):
        raise HelperError("Nome utente non valido")
    return (f"// Linux AirDrop: {user} may start the root-owned Wi-Fi helper without a password.\n"
            "polkit.addRule(function(action, subject) {\n"
            f'    if (action.id == "{ACTION_ID}" &&\n'
            f'        subject.user == "{user}" && subject.local && subject.active) {{\n'
            "        return polkit.Result.YES;\n"
            "    }\n"
            "});\n")


# ------------------------------------------------------------------ status (no root)
def helper_entry(helper_dir=HELPER_DIR):
    return str(Path(helper_dir) / ENTRY)


def authorized(action=ACTION_ID):
    """Ask polkit, without any interaction, whether this process may run the action now.

    /etc/polkit-1/rules.d is not readable by normal users, so the rule file itself cannot be
    inspected; polkit's own answer is the reliable check (and it also covers a rule that is
    present but does not apply to this user or session).
    """
    try:
        done = subprocess.run(["pkcheck", "--action-id", action, "--process", str(os.getpid())],
                              capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def status(user=None, bundle=BUNDLE, prefix=None, owner_uid=0, authorize=None):
    """ready | missing | other-user | outdated | unsafe. Never raises, never needs root."""
    where = paths(prefix)
    folder = where["dir"]
    try:
        manifest = json.loads((folder / MANIFEST).read_text())
        recorded = manifest["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return "missing"
    if not where["policy"].is_file():
        return "missing"
    if manifest.get("user") != (user or getpass.getuser()):
        return "other-user"
    # Anything root runs must belong to root and be writable by nobody else; otherwise it is not trusted.
    for directory, subdirs, names in os.walk(folder):
        for item in [directory, *[os.path.join(directory, n) for n in subdirs + names]]:
            info = os.lstat(item)
            if info.st_uid != owner_uid or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or stat.S_ISLNK(info.st_mode):
                return "unsafe"
    try:
        for name, digest in recorded.items():
            if sha256(folder / name) != digest:
                return "unsafe"
        if bundle_hashes(bundle) != {k: v for k, v in recorded.items() if k in FILES}:
            return "outdated"
    except (OSError, HelperError):
        return "outdated"
    # Last, and only for a helper that is otherwise sound: is polkit really going to say yes?
    if authorize is not None:
        allowed = authorize()
    elif prefix is None:
        allowed = authorized()
    else:
        allowed = where["rule"].is_file()   # staging copy: the rule file is all there is to look at
    return "ready" if allowed else "missing"


# ----------------------------------------------------------------------- install
def resolve_user(requested, environ=None):
    """The one user the rule is for. Under sudo/pkexec it must be the person who asked."""
    environ = os.environ if environ is None else environ
    caller = environ.get("PKEXEC_UID") or environ.get("SUDO_UID")
    if requested is None:
        if caller is None:
            raise HelperError("Indica l'utente con --user")
        requested = pwd.getpwuid(int(caller)).pw_name
    if not USER.fullmatch(requested):
        raise HelperError("Nome utente non valido")
    try:
        entry = pwd.getpwnam(requested)
    except KeyError as exc:
        raise HelperError("Utente inesistente: " + requested) from exc
    if entry.pw_uid < 1000 or entry.pw_name == "root":
        raise HelperError("Solo un utente normale può essere autorizzato")
    if caller is not None and int(caller) != entry.pw_uid:
        raise HelperError("L'utente autorizzato deve essere quello che avvia l'installazione")
    return entry.pw_name


def write_file(path, data, mode, uid=None):
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    os.chmod(path, mode)
    if uid is not None:
        os.chown(path, uid, uid)


def install(user, bundle=BUNDLE, prefix=None, own=True):
    """Copy the root-side files, then register the polkit action and the rule. `own` chowns to root."""
    if own and os.geteuid() != 0:
        raise HelperError("Servono i privilegi di amministratore")
    hashes = bundle_hashes(bundle)
    where = paths(prefix)
    folder, uid = where["dir"], (0 if own else None)
    folder.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".linux-airdrop-", dir=folder.parent))
    try:
        os.chmod(stage, 0o755)
        copied = {}
        for name in FILES:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            source_fd = os.open(Path(bundle) / name, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(source_fd, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise HelperError("Non è un file normale: " + name)
                data = source.read()
            write_file(target, data, 0o755 if name in EXECUTABLE else 0o644, uid)
            copied[name] = hashlib.sha256(data).hexdigest()
            # The hash that is recorded is that of the bytes actually installed.
            if copied[name] != hashes[name]:
                raise HelperError("Il file è cambiato durante la copia: " + name)
        write_file(stage / ENTRY, entry_script(HELPER_DIR).encode(), 0o755, uid)
        write_file(stage / MANIFEST, json.dumps({"user": user, "files": copied}, indent=2).encode() + b"\n", 0o644, uid)
        for directory, subdirs, _ in os.walk(stage):
            for item in [directory, *[os.path.join(directory, d) for d in subdirs]]:
                os.chmod(item, 0o755)
                if uid is not None:
                    os.chown(item, uid, uid)
        old = None
        if folder.exists():
            old = folder.with_name(folder.name + ".old")
            shutil.rmtree(old, ignore_errors=True)
            folder.rename(old)
        stage.rename(folder)
        stage = None
        if old is not None:
            shutil.rmtree(old, ignore_errors=True)
    finally:
        if stage is not None:
            shutil.rmtree(stage, ignore_errors=True)
    for key, text in (("policy", policy_xml(HELPER_DIR)), ("rule", rule_js(user))):
        target = where[key]
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name("." + target.name + ".new")
        temporary.unlink(missing_ok=True)
        write_file(temporary, text.encode(), 0o644, uid)
        os.replace(temporary, target)
    return folder


def uninstall(prefix=None):
    where = paths(prefix)
    shutil.rmtree(where["dir"], ignore_errors=True)
    for key in ("policy", "rule"):
        where[key].unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("install", "uninstall", "status"))
    parser.add_argument("--user", help="utente autorizzato (sudo e pkexec lo ricavano da soli)")
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            print(status())
            return 0
        if os.geteuid() != 0:
            print("Servono i privilegi di amministratore: usa sudo.", file=sys.stderr)
            return 1
        if args.action == "uninstall":
            uninstall()
            print("Helper di sistema rimosso.")
            return 0
        user = resolve_user(args.user)
        install(user)
        print(f"Helper di sistema installato in {HELPER_DIR}. {user} può ora attivare AirDrop senza password.")
        subprocess.run(["pkaction", "--action-id", ACTION_ID], check=False, stdout=subprocess.DEVNULL)
        return 0
    except HelperError as exc:
        print("Errore:", exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
