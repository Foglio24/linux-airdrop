"""Checks and preparation of what the user wants to send (drag & drop, file chooser, Nautilus).

Pure Python and independent of GTK, so it can be tested without a display.

An archive is built in memory by the sender, so the total size is limited. Folders are sent either natively
(a folder entry whose contents travel in the archive, as Finder does) or, safer, packed into a ZIP file that is
sent as one ordinary file. Symbolic links and special files are never followed or sent.
"""
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time
import zipfile

from fileinfo import kind_of

MAX_BYTES = 1_000_000_000      # the archive is held in memory (more than once while it is built)
MAX_ITEMS = 512                # top-level items per transfer (the receiving side's own limit)
WALK_LIMIT = 20_000            # entries examined inside folders before giving up on counting


def _walk_size(folder):
    """(bytes, regular files, skipped links/special, truncated) of a folder, never following links."""
    total = files = skipped = 0
    seen = 0
    for root, dirs, names in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for name in names:
            seen += 1
            if seen > WALK_LIMIT:
                return total, files, skipped, True
            full = os.path.join(root, name)
            try:
                info = os.lstat(full)
            except OSError:
                skipped += 1
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                files += 1
            else:
                skipped += 1
    return total, files, skipped, False


def inspect_paths(paths, max_bytes=MAX_BYTES):
    """Describe what would be sent: {'entries', 'total', 'folders', 'skipped': [(name, why)], 'problems': [text]}."""
    entries, skipped, names, seen_real = [], [], set(), set()
    total = folders = ignored_inside = 0
    for raw in paths:
        path = os.fspath(raw)
        label = os.path.basename(path.rstrip("/")) or path
        try:
            real = os.path.realpath(path)
            info = os.stat(real)
        except OSError:
            skipped.append((label, "non esiste più"))
            continue
        if real in seen_real:
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            skipped.append((label, "non è un file normale"))
            continue
        if not os.access(real, os.R_OK):
            skipped.append((label, "non è leggibile"))
            continue
        name = os.path.basename(real)
        if name in names:
            skipped.append((label, "ha lo stesso nome di un altro elemento"))
            continue
        is_dir = stat.S_ISDIR(info.st_mode)
        if is_dir:
            size, count, links, truncated = _walk_size(real)
            ignored_inside += links
            if truncated:
                skipped.append((label, "contiene troppi elementi"))
                continue
            folders += 1
        else:
            size, count = info.st_size, 1
        if len(entries) >= MAX_ITEMS:
            skipped.append((label, "troppi elementi in un solo invio"))
            continue
        seen_real.add(real)
        names.add(name)
        total += size
        entries.append({"path": real, "name": name, "is_dir": is_dir, "kind": kind_of(name, None, is_dir), "size": size, "files": count})
    problems = []
    if not entries:
        problems.append("Non c’è niente da inviare.")
    elif total > max_bytes:
        problems.append(f"Troppo grande: l’invio prepara tutto in memoria e il limite è {max_bytes // 1_000_000} MB.")
    return {"entries": entries, "total": total, "folders": folders, "skipped": skipped,
            "ignored_inside": ignored_inside, "problems": problems}


def make_zip(folder, workdir, cancelled=lambda: False):
    """Pack `folder` into <workdir>/<name>.zip (links and special files left out) and return the path."""
    folder = Path(folder)
    target = Path(workdir) / (folder.name + ".zip")
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        archive.write(folder, folder.name + "/")
        for root, dirs, names in os.walk(folder, followlinks=False):
            dirs.sort()
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
            for name in dirs + sorted(names):
                if cancelled():
                    raise InterruptedError("annullato")
                full = os.path.join(root, name)
                if os.path.islink(full):
                    continue
                relative = os.path.join(folder.name, os.path.relpath(full, folder))
                if os.path.isdir(full):
                    archive.write(full, relative + "/")
                elif os.path.isfile(full):
                    archive.write(full, relative)
    return target


def new_workdir(parent):
    """A private, empty folder for files made only to be sent."""
    parent = Path(parent)
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)
    return Path(tempfile.mkdtemp(prefix="invio-", dir=parent))


def purge_old(parent, max_age=86400):
    """Remove leftovers of earlier runs (a crash while sending); returns how many folders were removed."""
    removed = 0
    parent = Path(parent)
    if not parent.is_dir():
        return 0
    now = time.time()
    for child in parent.iterdir():
        try:
            if child.is_dir() and not child.is_symlink() and now - child.stat().st_mtime > max_age:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            pass
    return removed
