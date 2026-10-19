#!/usr/bin/env python3
"""Capture effective OWL build settings and source/binary provenance."""

import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys


BUILD_KEYS = (
    "cc", "cxx", "cppflags", "cflags", "cxxflags", "rtflags", "ldflags",
    "ldlibs", "pkglibs", "libev", "sanitize",
)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def encoded(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def write_json(path, value):
    """Preserve the timestamp of an unchanged config for incremental builds."""
    data = encoded(value)
    if path.exists() and path.read_bytes() == data:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def settings():
    return {key: os.environ["OWL_BUILD_" + key.upper()] for key in BUILD_KEYS}


def compiler_version(command):
    result = subprocess.run(shlex.split(command) + ["--version"], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.stdout.splitlines()[0]


def source_snapshot():
    paths = [Path("Makefile"), Path("tools/build_info.py")]
    for directory in ("daemon", "src", "radiotap"):
        paths.extend(path for path in Path(directory).rglob("*")
                     if path.is_file() and path.suffix in (".c", ".h", ".cpp", ".py"))
    sources = {path.as_posix(): digest(path) for path in sorted(paths)}
    # Names and individual hashes have unambiguous boundaries. Renaming a file
    # or moving bytes between adjacent files changes the aggregate digest.
    return sources, hashlib.sha256(encoded(sources)).hexdigest()


def main():
    mode = sys.argv[1]
    if mode == "config":
        config = settings()
        # Changes to the build helper/rules invalidate all object files, too.
        config["build_rules"] = {name: digest(Path(name))
                                 for name in ("Makefile", "tools/build_info.py")}
        write_json(Path(sys.argv[2]), config)
    elif mode == "record":
        config = settings()
        sources, source_digest = source_snapshot()
        write_json(Path(sys.argv[2]), {
            "compiler": compiler_version(config["cc"]),
            "cxx_compiler": compiler_version(config["cxx"]),
            "flags": config["cflags"],
            "build": config,
            "libev": "static, " + Path(config["libev"]).name,
            "libev_sha256": digest(Path(config["libev"])),
            "source_sha256": source_digest,
            "source_digest_format": "sha256-of-sorted-json-path-digests-v1",
            "sources": sources,
        })
    elif mode == "manifest":
        info = json.loads(Path(sys.argv[2]).read_text())
        binary = Path(sys.argv[3])
        info.update(binary=binary.name, sha256=digest(binary))
        write_json(Path(sys.argv[4]), info)
    else:
        raise SystemExit("expected config, record or manifest")


if __name__ == "__main__":
    main()
