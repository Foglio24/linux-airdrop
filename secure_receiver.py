"""Local receive protections for the bundled OpenDrop implementation.

The AirDrop discovery and TLS protocol remain in OpenDrop. This module bounds
incoming data, ties uploads to an explicit approval, and writes archives without
letting an archive choose filesystem paths. install() is called before CLI main.
"""
from __future__ import annotations

import collections
import functools
import inspect
import ipaddress
import json
import logging
import os
from pathlib import Path, PurePosixPath
import plistlib
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zlib
from http.server import ThreadingHTTPServer

import fileinfo
import identity
import preview
import store
import transfer_events as events

LOG = logging.getLogger("linux-airdrop.receiver")
CONTROL_LIMIT = 4 * 1024 * 1024
TRANSFER_LIMIT = 2 * 1024 * 1024 * 1024
FILE_LIMIT = 512
IO_BLOCK = 64 * 1024
APPROVAL_SECONDS = 180
READ_TIMEOUT = 30
TRANSFER_SECONDS = 3600
CONFIRM_SECONDS = 120
EVENT_FILES = 40      # file entries per event line (the window shows a summary; the folder holds the rest)
_installed = False


class ReceiveError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def _peer(handler):
    return handler.client_address[0].split("%", 1)[0]


def _transfer_id(value):
    if isinstance(value, dict):
        value = value.get("id")
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise ReceiveError("Identificativo del trasferimento non valido")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


# ------------------------------------------------------------ cancellation and progress reporting
_cancelled = set()
_cancel_lock = threading.Lock()


def cancel(transfer):
    with _cancel_lock:
        _cancelled.add(transfer)


def is_cancelled(transfer):
    with _cancel_lock:
        return transfer in _cancelled


def _forget_cancel(transfer):
    with _cancel_lock:
        _cancelled.discard(transfer)


def start_command_reader(stream=None):
    """Read 'cancel <transfer id>' lines from the window. End of input just ends the reader."""
    stream = stream or os.fdopen(os.dup(0), "r", errors="replace")

    def loop():
        try:
            for line in stream:
                parts = line.split()
                if len(parts) == 2 and parts[0] == "cancel":
                    cancel(parts[1][:128])
        except (OSError, ValueError):
            pass
    threading.Thread(target=loop, name="airdrop-commands", daemon=True).start()


class Progress:
    """Counts the bytes of one upload and reports them at most five times a second."""
    INTERVAL = 0.2

    def __init__(self, transfer, total, clock=time.monotonic):
        self.transfer, self.total, self.clock = transfer, total, clock
        self.done, self.last = 0, 0.0

    def add(self, count):
        self.done += count
        now = self.clock()
        if now - self.last >= self.INTERVAL:
            self.last = now
            events.emit("receive_progress", id=self.transfer, done=self.done, total=self.total)

    def finish(self):
        events.emit("receive_progress", id=self.transfer, done=self.done, total=self.total)


def _expected_bytes(headers):
    """Size of the upload body when the sender says so: Content-Length, else Apple's TotalBytes header."""
    for name in ("Content-Length", "TotalBytes"):
        raw = headers.get(name)
        if raw is not None and re.fullmatch(r"[0-9]{1,15}", raw.strip()):
            return int(raw) or None
    return None



class Approvals:
    """One pending transfer per peer; claim() consumes consent exactly once."""
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = {}

    def approve(self, peer, transfer, request):
        with self.lock:
            now = time.monotonic()
            self.pending = {key: value for key, value in self.pending.items()
                            if value[0] > now and key[0] != peer}
            if len(self.pending) >= 128:
                raise ReceiveError("Troppi trasferimenti in attesa", 503)
            self.pending[(peer, transfer)] = (now + APPROVAL_SECONDS, request)

    def claim(self, peer, transfer):
        with self.lock:
            item = self.pending.pop((peer, transfer), None)
        if item is None or item[0] <= time.monotonic():
            raise ReceiveError("Trasferimento non approvato o approvazione scaduta", 403)
        return item[1]


def _line(stream, maximum=128):
    value = stream.readline(maximum + 1)
    if len(value) > maximum or not value.endswith(b"\r\n"):
        raise ReceiveError("Framing HTTP incompleto o non valido")
    return value[:-2]


def _exact(stream, length):
    out = bytearray()
    while len(out) < length:
        part = stream.read(min(IO_BLOCK, length - len(out)))
        if not part:
            raise ReceiveError("Trasferimento interrotto")
        out.extend(part)
    return bytes(out)


def _body_parts(handler, limit):
    """Read strict HTTP framing in small pieces, including the final chunk."""
    headers = handler.headers
    encoding = headers.get("Transfer-Encoding", "").strip().lower()
    lengths = headers.get_all("Content-Length", []) if hasattr(headers, "get_all") else []
    if len(lengths) > 1 or (encoding and headers.get("Content-Length") is not None):
        raise ReceiveError("Lunghezza HTTP ambigua")
    deadline = time.monotonic() + TRANSFER_SECONDS
    total = 0
    if encoding:
        if encoding != "chunked":
            raise ReceiveError("Codifica HTTP non supportata")
        while True:
            if time.monotonic() > deadline:
                raise ReceiveError("Tempo massimo del trasferimento superato", 408)
            raw = _line(handler.rfile)
            # Chunk extensions are legal, but never determine allocations.
            token = raw.split(b";", 1)[0]
            if not re.fullmatch(rb"[0-9A-Fa-f]{1,16}", token):
                raise ReceiveError("Dimensione del blocco HTTP non valida")
            length = int(token, 16)
            if total + length > limit:
                raise ReceiveError("Trasferimento troppo grande", 413)
            if length == 0:
                trailer_bytes = 0
                while True:
                    line = _line(handler.rfile, 8192)
                    trailer_bytes += len(line)
                    if trailer_bytes > 16384:
                        raise ReceiveError("Trailer HTTP troppo grande", 413)
                    if not line:
                        return
                    if b":" not in line:
                        raise ReceiveError("Trailer HTTP non valido")
            remain = length
            while remain:
                if time.monotonic() > deadline:
                    raise ReceiveError("Tempo massimo del trasferimento superato", 408)
                part = _exact(handler.rfile, min(IO_BLOCK, remain))
                remain -= len(part)
                total += len(part)
                yield part
            if _exact(handler.rfile, 2) != b"\r\n":
                raise ReceiveError("Separatore HTTP non valido")
    else:
        raw = headers.get("Content-Length")
        if raw is None or not re.fullmatch(r"[0-9]{1,20}", raw.strip()):
            raise ReceiveError("Lunghezza HTTP mancante o non valida")
        length = int(raw)
        if length > limit:
            raise ReceiveError("Trasferimento troppo grande", 413)
        while total < length:
            if time.monotonic() > deadline:
                raise ReceiveError("Tempo massimo del trasferimento superato", 408)
            part = _exact(handler.rfile, min(IO_BLOCK, length - total))
            total += len(part)
            yield part


def _read_request_body(handler):
    return b"".join(_body_parts(handler, CONTROL_LIMIT))


def _name(value):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 4096:
        raise ReceiveError("Nome di file non valido")
    if "\x00" in value or any(ord(c) < 32 for c in value):
        raise ReceiveError("Nome di file non valido")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise ReceiveError("Percorso esterno alla cartella di ricezione")
    return path


def _parse_ask(body):
    try:
        request = plistlib.loads(body)
    except Exception as exc:
        raise ReceiveError("Richiesta AirDrop non valida") from exc
    if not isinstance(request, dict):
        raise ReceiveError("Richiesta AirDrop non valida")
    files = request.get("Files", [])
    items = request.get("Items", [])
    if not isinstance(files, list) or not isinstance(items, list) or len(files) > FILE_LIMIT or len(items) > 32:
        raise ReceiveError("Elenco dei file non valido")
    for entry in files:
        if not isinstance(entry, dict):
            raise ReceiveError("Elenco dei file non valido")
        value = entry.get("FileName") or entry.get("FileBomPath")
        _name(value)
    _transfer_id(request.get("TransferID"))
    if not files and not items:
        raise ReceiveError("Trasferimento vuoto")
    return request


def _confirm(request, info=None, icon=None):
    """Ask the user, in a separate window, whether to accept. True only for an explicit yes."""
    hook = os.environ.get("AIRDROP_CONFIRM")
    if not hook or not os.path.isabs(hook):
        LOG.error("Manca il programma di conferma; trasferimento rifiutato")
        return False
    if info is None:
        info, icon = preview.describe(request)
    names = [f["name"] for f in info["files"]]
    if names:
        detail = ", ".join(names[:3])
        if len(names) > 3:
            detail += f" (+{info['file_count'] - 3})"
    else:
        from opendrop.server import ask_items
        urls = ask_items(request)
        if not urls:
            raise ReceiveError("Link AirDrop non supportato")
        detail = urls[0]
    # Control characters must not reach a terminal, log, or UI prompt.
    clean = lambda value: "".join(c if ord(c) >= 32 and ord(c) != 127 else " " for c in value)
    try:
        with tempfile.TemporaryDirectory(prefix="airdrop-prompt-") as folder:
            argv = [hook, clean(info["sender"])[:256], clean(detail)[:4096]]
            shown = {key: info[key] for key in ("sender", "model", "model_label", "files", "file_count", "total_size", "items")}
            thumbnail = preview.make_thumbnail(icon, folder)
            if thumbnail:
                shown["thumbnail"] = thumbnail
            description = Path(folder) / "prompt.json"
            description.write_text(json.dumps(shown, ensure_ascii=False), encoding="utf-8")
            argv += ["--info", str(description)]
            result = subprocess.run(argv, timeout=CONFIRM_SECONDS, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        LOG.exception("Conferma non disponibile; trasferimento rifiutato")
        return False


def _response(handler, status, body=b"", close=False):
    handler.send_response(status)
    handler.send_header("Content-Length", str(len(body)))
    if body:
        handler.send_header("Content-Type", "application/octet-stream")
    if close:
        handler.send_header("Connection", "close")
    handler.end_headers()
    if body:
        handler.wfile.write(body)


def _sender_identity(request, info):
    """(identity kind, device key) for the remembered-devices list. Never used to decide whether to accept."""
    record = request.get("SenderRecordData")
    found = {"present": False, "fingerprint": None}
    if isinstance(record, (bytes, bytearray)) and record:
        ca = Path(__file__).resolve().parent / "backend/python/opendrop/certs/apple_root_ca.pem"
        found = identity.inspect(record, ca)
        LOG.info("Il mittente ha allegato un record Apple (%d byte, %s)", len(record),
                 "impronta ricavata" if found["fingerprint"] else "impronta non ricavabile; uso il solo nome")
    return store.make_device_key(info["sender"], found.get("fingerprint"))


def _handle_ask(handler):
    body = handler._read_request_body()
    request = _parse_ask(body)
    info, icon = preview.describe(request)
    transfer = _transfer_id(request.get("TransferID")) or uuid.uuid4().hex
    kind, key = _sender_identity(request, info)
    LOG.info("Campi della richiesta: %s", ", ".join(sorted(str(k) for k in request)[:40]))
    # The id travels with the approved request, so the upload (whose header may lack one) reports under the same id.
    request["_event_id"] = transfer
    # Event lines are bounded so each is written in one piece next to ordinary log output.
    events.emit("ask", id=transfer, sender=info["sender"], model=info["model"], device_key=key, identity=kind,
                files=info["files"][:EVENT_FILES], items=info["items"][:4], total=info["total_size"])
    if not _confirm(request, info, icon):
        events.emit("ask_declined", id=transfer)
        raise ReceiveError("Trasferimento rifiutato", 403)
    events.emit("ask_accepted", id=transfer)
    if request.get("Files"):
        handler.server.approvals.approve(_peer(handler), _transfer_id(request.get("TransferID")), request)
    else:
        from opendrop.server import ask_items, open_url
        urls = ask_items(request)
        for url in urls:
            if len(url) > 8192:
                raise ReceiveError("Link troppo lungo")
            open_url(url)
        events.emit("receive_done", id=transfer, folder=None, total=0, done=0,
                    files=[{"name": fileinfo.clean_text(u, 300), "kind": "link", "size": None} for u in urls[:8]])
    reply = plistlib.dumps({"ReceiverModelName": handler.config.computer_model,
                           "ReceiverComputerName": handler.config.computer_name}, fmt=plistlib.FMT_BINARY)
    _response(handler, 200, reply)


def decode_archive(raw_path, content_type, decoded_path, limit=TRANSFER_LIMIT):
    """Expand dvzip or gzip into a bounded on-disk cpio archive."""
    total = 0
    with open(raw_path, "rb") as src, open(decoded_path, "xb") as dst:
        if content_type == "application/x-dvzip":
            while True:
                header = src.read(4)
                if not header:
                    break
                if len(header) != 4:
                    raise ReceiveError("Archivio dvzip troncato")
                value = struct.unpack(">I", header)[0]
                stored, length = bool(value >> 31), value & 0x7fffffff
                if length == 0:
                    if src.read(1):
                        raise ReceiveError("Dati dopo la fine dell'archivio dvzip")
                    break
                if length > 1024 * 1024:
                    raise ReceiveError("Blocco dvzip troppo grande", 413)
                data = _exact(src, length)
                if not stored:
                    decoder = zlib.decompressobj()
                    data = decoder.decompress(data, 128 * 1024 + 1)
                    if not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
                        raise ReceiveError("Blocco dvzip incompleto o non valido")
                if len(data) > 128 * 1024 or total + len(data) > limit:
                    raise ReceiveError("Archivio decompresso troppo grande", 413)
                dst.write(data)
                total += len(data)
        else:
            magic = src.read(2)
            src.seek(0)
            if magic == b"\x1f\x8b":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                while True:
                    chunk = src.read(IO_BLOCK)
                    if not chunk:
                        break
                    data = decoder.decompress(chunk, min(IO_BLOCK, limit - total + 1))
                    while True:
                        total += len(data)
                        if total > limit:
                            raise ReceiveError("Archivio decompresso troppo grande", 413)
                        dst.write(data)
                        if not decoder.unconsumed_tail:
                            break
                        data = decoder.decompress(decoder.unconsumed_tail, min(IO_BLOCK, limit - total + 1))
                    if decoder.unused_data:
                        raise ReceiveError("Dati dopo la fine dell'archivio gzip")
                if not decoder.eof:
                    raise ReceiveError("Archivio gzip troncato")
            else:
                while True:
                    chunk = src.read(IO_BLOCK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise ReceiveError("Archivio troppo grande", 413)
                    dst.write(chunk)
    if not total:
        raise ReceiveError("Archivio vuoto")


def _number(raw, base):
    digits = rb"[0-7]+" if base == 8 else rb"[0-9a-fA-F]+"
    if not re.fullmatch(digits, raw):
        raise ReceiveError("Intestazione cpio non valida")
    return int(raw, base)


def _unique_name(name, existing):
    candidate, counter = name, 1
    stem, suffix = os.path.splitext(name)
    while candidate in existing:
        candidate = f"{stem}-{counter}{suffix}"
        counter += 1
    existing.add(candidate)
    return candidate


def extract_cpio(archive_path, destination, requested=None, limit=TRANSFER_LIMIT):
    """Extract ODC/newc regular files, without libarchive disk extraction.

    The complete cpio trailer and every declared byte are required. Each output
    has a fresh name and mode 0600. Directories are flattened like iOS receipts.
    """
    output_names, source_names, existing = [], [], set()
    total, entries = 0, 0
    with open(archive_path, "rb") as archive:
        while True:
            magic = _exact(archive, 6)
            if magic == b"070707":
                header = magic + _exact(archive, 70)
                mode = _number(header[18:24], 8)
                nlink = _number(header[36:42], 8)
                namesize = _number(header[59:65], 8)
                size = _number(header[65:76], 8)
                checksum, alignment = None, 1
            elif magic in (b"070701", b"070702"):
                header = magic + _exact(archive, 104)
                mode = _number(header[14:22], 16)
                nlink = _number(header[38:46], 16)
                size = _number(header[54:62], 16)
                namesize = _number(header[94:102], 16)
                checksum = _number(header[102:110], 16) if magic == b"070702" else None
                alignment = 4
            else:
                raise ReceiveError("Formato cpio non supportato o archivio troncato")
            if not 1 <= namesize <= 4096 or total + size > limit:
                raise ReceiveError("Dimensioni cpio non valide", 413)
            raw_name = _exact(archive, namesize)
            if raw_name[-1:] != b"\x00" or b"\x00" in raw_name[:-1]:
                raise ReceiveError("Nome cpio non valido")
            try:
                name = raw_name[:-1].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ReceiveError("Nome cpio non valido") from exc
            if alignment == 4:
                _exact(archive, -(110 + namesize) % 4)
            if name == "TRAILER!!!":
                if size:
                    raise ReceiveError("Trailer cpio non valido")
                # Writers may append zero padding to their final block.
                tail = archive.read(IO_BLOCK + 1)
                if len(tail) > IO_BLOCK or tail.strip(b"\x00"):
                    raise ReceiveError("Dati dopo il trailer cpio")
                break
            path = _name(name)
            entries += 1
            if entries > FILE_LIMIT * 8:
                raise ReceiveError("Troppi elementi nell'archivio", 413)
            kind = mode & 0o170000
            if kind == 0o040000:
                if size:
                    raise ReceiveError("Directory cpio con contenuto non valido")
                continue
            if kind != 0o100000 or nlink > 1:
                raise ReceiveError("Link e file speciali non sono supportati")
            base = path.name
            if not base or base in (".", ".."):
                raise ReceiveError("Nome di file non valido")
            keep = not base.startswith("._")
            target = None
            if keep:
                if len(output_names) >= FILE_LIMIT:
                    raise ReceiveError("Troppi file nell'archivio", 413)
                target_name = _unique_name(base, existing)
                target = os.open(Path(destination) / target_name,
                                 os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                output_names.append(target_name)
                source_names.append((base, path.parts))
            remaining, actual_checksum = size, 0
            try:
                while remaining:
                    data = _exact(archive, min(IO_BLOCK, remaining))
                    remaining -= len(data)
                    total += len(data)
                    actual_checksum = (actual_checksum + sum(data)) & 0xffffffff
                    if target is not None:
                        view = memoryview(data)
                        while view:
                            count = os.write(target, view)
                            view = view[count:]
                if target is not None:
                    os.fsync(target)
            finally:
                if target is not None:
                    os.close(target)
            if checksum is not None and actual_checksum != checksum:
                raise ReceiveError("Checksum cpio non valido")
            if alignment == 4:
                _exact(archive, -size % 4)
    if not output_names:
        raise ReceiveError("Nessun file completo nel trasferimento")
    if requested:
        present = collections.Counter(base for base, _ in source_names)
        for entry in requested:
            expected = _name(entry.get("FileName") or entry.get("FileBomPath")).name
            if entry.get("FileIsDirectory"):
                if not any(expected in parts[:-1] for _, parts in source_names):
                    raise ReceiveError("Directory richiesta mancante nell'archivio")
            elif present[expected]:
                present[expected] -= 1
            else:
                raise ReceiveError("Un file approvato manca nell'archivio")
    return output_names


def _handle_upload(handler):
    transfer = _transfer_id(handler.headers.get("TransferID"))
    request = handler.server.approvals.claim(_peer(handler), transfer)
    transfer = request.get("_event_id") or transfer or uuid.uuid4().hex
    try:
        _receive_upload(handler, request, transfer)
    except Exception as exc:
        cancelled = is_cancelled(transfer)
        events.emit("receive_failed", id=transfer, cancelled=cancelled,
                    error="Annullato dall’utente" if cancelled else
                    (str(exc) if isinstance(exc, ReceiveError) else "Trasferimento interrotto"))
        raise
    finally:
        _forget_cancel(transfer)


def _receive_upload(handler, request, transfer):
    content_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if content_type not in ("application/x-cpio", "application/x-dvzip"):
        raise ReceiveError("Formato del trasferimento non supportato", 406)
    destination = Path(os.environ.get("AIRDROP_DOWNLOAD_DIR") or os.getcwd()).expanduser()
    destination.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".airdrop-incompleto-", dir=destination))
    raw_path, decoded_path = staging / "trasferimento.raw", staging / "archivio.cpio"
    files_dir = staging / "file"
    files_dir.mkdir(mode=0o700)
    try:
        progress = Progress(transfer, _expected_bytes(handler.headers))
        events.emit("receive_start", id=transfer, total=progress.total)
        with open(raw_path, "xb") as raw:
            if handler.headers.get("Expect", "").strip().lower() == "100-continue":
                _response(handler, 100)
            for chunk in _body_parts(handler, TRANSFER_LIMIT):
                if is_cancelled(transfer):
                    raise ReceiveError("Trasferimento annullato dall’utente", 499)
                raw.write(chunk)
                progress.add(len(chunk))
            raw.flush()
            os.fsync(raw.fileno())
        progress.finish()
        decode_archive(raw_path, content_type, decoded_path)
        names = extract_cpio(decoded_path, files_dir, request.get("Files"))
        # A freshly created final directory makes collisions impossible; link()
        # refuses existing names and no archive names become directory paths.
        final_dir = Path(tempfile.mkdtemp(prefix="AirDrop-" + time.strftime("%Y%m%d-%H%M%S-") , dir=destination))
        try:
            for name in names:
                os.link(files_dir / name, final_dir / name, follow_symlinks=False)
        except Exception:
            shutil.rmtree(final_dir)
            raise
        listing = []
        for name in names:
            size = (final_dir / name).stat().st_size
            listing.append({"name": name, "kind": fileinfo.kind_of(name), "size": size})
        shutil.rmtree(staging)
        LOG.info("File(s) received: %s (%s)", ", ".join(names), final_dir)
        events.emit("receive_done", id=transfer, folder=str(final_dir), files=listing[:EVENT_FILES],
                    done=sum(entry["size"] for entry in listing), total=sum(entry["size"] for entry in listing))
        _response(handler, 200, close=True)
    except Exception:
        if is_cancelled(transfer):
            shutil.rmtree(staging, ignore_errors=True)    # the user asked for it: nothing worth keeping
        else:
            LOG.error("Trasferimento incompleto conservato in %s", staging)
        raise


class SafeHTTPServer(ThreadingHTTPServer):
    address_family = socket.AF_INET6
    daemon_threads = True
    allow_reuse_address = False
    request_queue_size = 16

    def __init__(self, address, handler, context):
        self.context = context
        self.approvals = Approvals()
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, handler)

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        super().server_bind()

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            LOG.info("AirDrop: connessione in ingresso, avvio TLS")
            request.settimeout(READ_TIMEOUT)
            request = self.context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            request.do_handshake()
            LOG.info("AirDrop: connessione TLS stabilita")
            super().process_request_thread(request, client_address)
        except (OSError, ValueError) as exc:
            LOG.warning("AirDrop: connessione interrotta (%s: %s)", type(exc).__name__, exc)
            self.shutdown_request(request)
        finally:
            self.slots.release()


def _init_server(server):
    if server.config.interface != "awdl0" or not ipaddress.IPv6Address(server.ip_addr).is_link_local:
        raise RuntimeError("Il ricevitore nativo richiede l'interfaccia awdl0 con IPv6 link-local")
    address = (str(server.ip_addr), server.config.port, 0, socket.if_nametoindex("awdl0"))
    return SafeHTTPServer(address, server.Handler, server.config.get_ssl_context())


def _expect_continue(handler):
    if handler.path == "/Upload":
        # Consent and an output file are checked in handle_upload first.
        return True
    if handler.path in ("/Ask", "/Discover"):
        _response(handler, 100)
        return True
    _response(handler, 400, close=True)
    return False


def install():
    """Apply local receiver hooks; safe to call before send/find too."""
    global _installed
    if _installed:
        return
    from opendrop.config import AirDropConfig
    from opendrop.server import AirDropServer, AirDropServerHandler
    original_init = AirDropConfig.__init__
    signature = inspect.signature(original_init)

    @functools.wraps(original_init)
    def config_init(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        if "airdrop_dir" not in bound.arguments:
            kwargs["airdrop_dir"] = os.environ.get("AIRDROP_STATE_DIR") or "~/.local/state/linux-airdrop/protocol"
        return original_init(self, *args, **kwargs)

    AirDropConfig.__init__ = config_init
    AirDropServer._init_server = _init_server
    AirDropServerHandler._read_request_body = _read_request_body
    AirDropServerHandler.handle_ask = _handle_ask
    AirDropServerHandler.handle_upload = _handle_upload
    # BaseHTTPRequestHandler normally sends 100 before do_POST. Delay until
    # /Upload approval is checked and its private raw output is ready.
    AirDropServerHandler.handle_expect_100 = _expect_continue
    AirDropServerHandler.timeout = READ_TIMEOUT
    original_post = AirDropServerHandler.do_POST

    def guarded_post(self):
        try:
            LOG.info("AirDrop: richiesta %s", self.path[:80])
            original_post(self)
        except (ReceiveError, OSError, ValueError, plistlib.InvalidFileException) as exc:
            status = exc.status if isinstance(exc, ReceiveError) else (408 if isinstance(exc, TimeoutError) else 400)
            LOG.warning("Ricezione rifiutata: %s", exc)
            try:
                _response(self, status, close=True)
            except OSError:
                self.close_connection = True

    AirDropServerHandler.do_POST = guarded_post
    _install_sender_hooks()
    if events.enabled() and not sys.stdin.isatty():
        try:
            start_command_reader()
        except OSError:
            pass
    _installed = True


_send_state = {"last": 0.0}


def _install_sender_hooks():
    """Report the real progress of an outgoing transfer (only printed when events are enabled)."""
    from opendrop.client import AirDropClient

    def started(total):
        events.emit("send_start", total=total)

    def moved(done, total):
        now = time.monotonic()
        if done >= total or now - _send_state["last"] >= Progress.INTERVAL:
            _send_state["last"] = now
            events.emit("send_progress", done=done, total=total)

    AirDropClient.start_hook = staticmethod(started)
    AirDropClient.progress_hook = staticmethod(moved)
    original_ask, original_upload = AirDropClient.send_ask, AirDropClient.send_upload

    @functools.wraps(original_ask)
    def send_ask(self, *args, **kwargs):
        accepted = original_ask(self, *args, **kwargs)
        events.emit("send_accepted" if accepted else "send_declined")
        return accepted

    @functools.wraps(original_upload)
    def send_upload(self, *args, **kwargs):
        done = original_upload(self, *args, **kwargs)
        events.emit("send_done" if done else "send_failed", error=None if done else "Il trasferimento non è andato a buon fine")
        return done

    AirDropClient.send_ask, AirDropClient.send_upload = send_ask, send_upload
