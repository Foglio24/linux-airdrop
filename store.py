"""Transfer history and remembered devices, kept in a small private SQLite database.

What is stored, and nothing more: when a transfer happened, its direction and outcome, the other device's
display name and model, the file names, kinds and sizes, and where the files are on this computer (to be able
to open them). No file contents, thumbnails, network addresses, MAC addresses, keys or certificates.
The file is created with mode 0600, old entries are pruned, and `clear_history()` / `forget_devices()` wipe it.

A device is identified by `make_device_key()`. That is either a fingerprint derived from the Apple-signed
record the other device sent (`identity == "record"`), or, when there is none, only its display name
(`identity == "name"`). A display name can be copied by anyone, so a "name" key is a memory aid for the
user and is never used to authenticate or to accept anything automatically.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import time

from fileinfo import clean_text

SCHEMA_VERSION = 1
MAX_ROWS = 1000
MAX_AGE_DAYS = 365
STATUSES = ("in_progress", "completed", "failed", "cancelled", "declined")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    direction TEXT NOT NULL CHECK (direction IN ('send', 'receive')),
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed', 'failed', 'cancelled', 'declined')),
    peer_name TEXT NOT NULL,
    peer_model TEXT,
    device_key TEXT,
    total_bytes INTEGER,
    done_bytes INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    folder TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS files (
    transfer_id INTEGER NOT NULL REFERENCES transfers(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    size INTEGER,
    path TEXT,
    PRIMARY KEY (transfer_id, position)
);
CREATE TABLE IF NOT EXISTS devices (
    key TEXT PRIMARY KEY,
    identity TEXT NOT NULL CHECK (identity IN ('record', 'name')),
    name TEXT NOT NULL,
    model TEXT,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    last_transfer REAL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    received_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS transfers_started ON transfers (started_at DESC);
"""


def make_device_key(name, fingerprint=None):
    """(identity, key). `fingerprint` is the hash derived from the sender's Apple-signed record, if there was one."""
    if isinstance(fingerprint, str) and len(fingerprint) >= 16 and all(c in "0123456789abcdef" for c in fingerprint):
        return "record", "rec:" + fingerprint[:32]
    digest = hashlib.sha256(clean_text(name, 120).casefold().encode("utf-8")).hexdigest()
    return "name", "name:" + digest[:32]


def _int(value):
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class Store:
    def __init__(self, path, enabled=True, clock=time.time):
        self.path = Path(path)
        self.enabled = enabled
        self.clock = clock
        self.db = None
        self.problem = None
        if enabled:
            self._open()

    # ----------------------------------------------------------------- connection
    def _open(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._connect()
        except (OSError, sqlite3.Error) as first:
            # A damaged file is moved aside (never deleted) and a fresh one is started.
            try:
                if self.db is not None:
                    self.db.close()
                    self.db = None
                if self.path.exists():
                    self.path.replace(self.path.with_name(f"{self.path.name}.damaged-{int(self.clock())}"))
                self._connect()
            except (OSError, sqlite3.Error) as second:
                self.db = None
                self.problem = f"{type(second).__name__}: {second}"
                self.enabled = False
            else:
                self.problem = f"database ricreato dopo un errore ({type(first).__name__})"

    def _connect(self):
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)
        os.chmod(self.path, 0o600)
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = NORMAL")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise sqlite3.DatabaseError("database creato da una versione più recente")
            db.executescript(_SCHEMA)
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.commit()
        except BaseException:
            db.close()
            raise
        self.db = db

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def _run(self, sql, args=(), commit=False):
        if not self.enabled or self.db is None:
            return None
        try:
            cursor = self.db.execute(sql, args)
            if commit:
                self.db.commit()
            return cursor
        except sqlite3.Error as exc:
            self.problem = f"{type(exc).__name__}: {exc}"
            return None

    def set_enabled(self, enabled):
        """Switch recording on or off. Turning it off keeps what is already stored until it is cleared."""
        if enabled and self.db is None:
            self.enabled = True
            self._open()
        else:
            self.enabled = enabled and self.db is not None

    # -------------------------------------------------------------------- transfers
    def start_transfer(self, direction, peer_name, peer_model=None, device_key=None, total_bytes=None,
                       files=(), folder=None, status="in_progress"):
        if direction not in ("send", "receive") or status not in STATUSES:
            raise ValueError("direzione o stato non validi")
        now = self.clock()
        cursor = self._run(
            "INSERT INTO transfers (started_at, finished_at, direction, status, peer_name, peer_model, device_key,"
            " total_bytes, file_count, folder) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, None if status == "in_progress" else now, direction, status, clean_text(peer_name, 120) or "Dispositivo sconosciuto",
             clean_text(peer_model, 64) or None, device_key, _int(total_bytes), len(files), folder))
        if cursor is None:
            return None
        tid = cursor.lastrowid
        self._store_files(tid, files)
        self.db.commit()
        return tid

    def _store_files(self, tid, files):
        self._run("DELETE FROM files WHERE transfer_id = ?", (tid,))
        for position, entry in enumerate(list(files)[:512]):
            self._run("INSERT INTO files (transfer_id, position, name, kind, size, path) VALUES (?,?,?,?,?,?)",
                      (tid, position, clean_text(entry.get("name"), 255) or "?", clean_text(entry.get("kind"), 16) or "other",
                       _int(entry.get("size")), entry.get("path") if isinstance(entry.get("path"), str) else None))

    def finish_transfer(self, tid, status, error=None, done_bytes=None, total_bytes=None, files=None, folder=None):
        if tid is None:
            return
        if status not in STATUSES or status == "in_progress":
            raise ValueError("stato finale non valido")
        self._run("UPDATE transfers SET status = ?, finished_at = ?, error = ?,"
                  " done_bytes = COALESCE(?, done_bytes), total_bytes = COALESCE(?, total_bytes),"
                  " folder = COALESCE(?, folder), file_count = COALESCE(?, file_count) WHERE id = ?",
                  (status, self.clock(), clean_text(error, 200) or None, _int(done_bytes), _int(total_bytes), folder,
                   len(files) if files is not None else None, tid))
        if files is not None:
            self._store_files(tid, files)
        if self.db is not None:
            self.db.commit()

    def _row(self, row):
        data = dict(row)
        data["files"] = [dict(f) for f in self._run(
            "SELECT name, kind, size, path FROM files WHERE transfer_id = ? ORDER BY position", (row["id"],)).fetchall()]
        return data

    def transfers(self, direction=None, limit=300):
        if direction in ("send", "receive"):
            cursor = self._run("SELECT * FROM transfers WHERE direction = ? ORDER BY started_at DESC, id DESC LIMIT ?",
                               (direction, int(limit)))
        else:
            cursor = self._run("SELECT * FROM transfers ORDER BY started_at DESC, id DESC LIMIT ?", (int(limit),))
        return [self._row(row) for row in cursor.fetchall()] if cursor else []

    def get_transfer(self, tid):
        cursor = self._run("SELECT * FROM transfers WHERE id = ?", (tid,))
        row = cursor.fetchone() if cursor else None
        return self._row(row) if row else None

    def count_transfers(self):
        cursor = self._run("SELECT COUNT(*) FROM transfers")
        return cursor.fetchone()[0] if cursor else 0

    def clear_history(self):
        self._run("DELETE FROM files", commit=False)
        self._run("DELETE FROM transfers", commit=True)

    def mark_interrupted(self):
        """Entries left 'in progress' by a crash or a power cut cannot ever finish: mark them failed."""
        cursor = self._run("UPDATE transfers SET status = 'failed', finished_at = COALESCE(finished_at, ?),"
                           " error = 'Interrotto: l’app si è chiusa durante il trasferimento' WHERE status = 'in_progress'",
                           (self.clock(),), commit=True)
        return cursor.rowcount if cursor else 0

    def prune(self, max_rows=MAX_ROWS, max_age_days=MAX_AGE_DAYS):
        self._run("DELETE FROM transfers WHERE started_at < ?", (self.clock() - max_age_days * 86400,))
        self._run("DELETE FROM transfers WHERE id NOT IN (SELECT id FROM transfers ORDER BY started_at DESC, id DESC LIMIT ?)",
                  (int(max_rows),), commit=True)

    # --------------------------------------------------------------------- devices
    def touch_device(self, key, identity, name, model=None, direction=None):
        """Remember a device the user dealt with. `direction` counts one finished transfer in that direction."""
        if not key or identity not in ("record", "name"):
            return
        now = self.clock()
        name, model = clean_text(name, 120) or "Dispositivo sconosciuto", clean_text(model, 64) or None
        self._run(
            "INSERT INTO devices (key, identity, name, model, first_seen, last_seen) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET name = excluded.name, model = COALESCE(excluded.model, devices.model),"
            " last_seen = excluded.last_seen",
            (key, identity, name, model, now, now))
        if direction in ("send", "receive"):
            column = "sent_count" if direction == "send" else "received_count"
            self._run(f"UPDATE devices SET {column} = {column} + 1, last_transfer = ? WHERE key = ?", (now, key))
        if self.db is not None:
            self.db.commit()

    def devices(self):
        cursor = self._run("SELECT * FROM devices ORDER BY last_seen DESC")
        result = []
        for row in cursor.fetchall() if cursor else []:
            data = dict(row)
            data["transfer_count"] = data["sent_count"] + data["received_count"]
            result.append(data)
        return result

    def forget_device(self, key):
        self._run("DELETE FROM devices WHERE key = ?", (key,), commit=True)

    def forget_devices(self):
        self._run("DELETE FROM devices", commit=True)
