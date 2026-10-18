#!/usr/bin/python3
"""Temporary privileged AWDL bridge. No HTTP server runs as root.

The parent speaks newline JSON over stdout, and sends `stop` on stdin.
Only the selected idle Wi-Fi device is released from NetworkManager.
"""
import argparse
import fcntl
import hashlib
import json
import os
import platform
from pathlib import Path
import re
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

BASE = Path(__file__).resolve().parent
# The helper is launched with -I; import only its own reviewed bundle modules.
sys.path.insert(0, str(BASE))
from p2p_anchor import GoAnchor
from awdl_diag import summarize_packet
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
DEFAULT_SECONDS = 1800
MAX_SECONDS = 24 * 3600
STOP = threading.Event()
RECOVERY = threading.Event()
OUTPUT_LOCK = threading.Lock()


def emit(event, **data):
    try:
        with OUTPUT_LOCK:
            print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)
    except BrokenPipeError:
        STOP.set()


def command(args, check=True):
    if RECOVERY.is_set():
        raise RuntimeError("Il driver è bloccato: riavvio di Ubuntu necessario.")
    process = subprocess.Popen(args, env=SAFE_ENV, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        output, error = process.communicate(timeout=12)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            # A task stuck in kernel I/O cannot be killed. Never wait for it
            # indefinitely or issue more radio commands against the same lock.
            RECOVERY.set()
            STOP.set()
            emit("recovery", message="Il driver Wi-Fi è bloccato nel kernel. Riavvia Ubuntu: verranno caricati i moduli originali.")
        raise RuntimeError("Comando Wi-Fi senza risposta: " + " ".join(args))
    if check and process.returncode:
        raise RuntimeError(" ".join(args) + ": " + error.strip()[:600])
    return output.strip()


def _netlink_attributes(payload):
    while payload:
        if len(payload) < 4:
            raise RuntimeError("Attributo netlink incompleto")
        size, kind = struct.unpack_from("=HH", payload)
        padded = (size + 3) & ~3
        if size < 4 or padded > len(payload):
            raise RuntimeError("Lunghezza attributo netlink non valida")
        yield kind & 0x3fff, payload[4:size]
        payload = payload[padded:]


def _generic_netlink_request(channel, family, command_id, attributes, sequence):
    port = channel.getsockname()[0]
    body = struct.pack("=BBH", command_id, 1, 0) + attributes
    request = struct.pack("=IHHII", 16 + len(body), family, 5, sequence, port) + body
    channel.sendto(request, (0, 0))  # NLM_F_REQUEST | NLM_F_ACK
    replies = []
    deadline = time.monotonic() + 3
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Nessuna conferma dal kernel")
        channel.settimeout(remaining)
        packet, _, flags, sender = channel.recvmsg(65536)
        if sender[0] != 0:
            continue
        if flags & socket.MSG_TRUNC:
            raise RuntimeError("Risposta netlink troncata")
        while packet:
            if len(packet) < 16:
                raise RuntimeError("Messaggio netlink incompleto")
            size, kind, _, seq, pid = struct.unpack_from("=IHHII", packet)
            padded = (size + 3) & ~3
            if size < 16 or padded > len(packet):
                raise RuntimeError("Lunghezza messaggio netlink non valida")
            data, packet = packet[16:size], packet[padded:]
            if seq != sequence or pid not in (0, port):
                continue
            if kind == 2:  # NLMSG_ERROR carries both error and success ACKs.
                if len(data) < 20:
                    raise RuntimeError("Conferma netlink incompleta")
                error = struct.unpack_from("=i", data)[0]
                original = struct.unpack_from("=IHHII", data, 4)
                if original[1] != family or original[3] != sequence:
                    raise RuntimeError("Conferma netlink per un altro comando")
                if error:
                    raise OSError(abs(error), os.strerror(abs(error)))
                return replies
            if kind != family or len(data) < 4:
                raise RuntimeError("Risposta netlink inattesa")
            replies.append(data[4:])


def suppress_monitor_tx_echo():
    """Set ACTIVE+SKIP_TX on our down monitor; iw 6.17 lacks SKIP_TX.

    Constants come from Linux's stable nl80211/genetlink UAPI. On stock 7.0,
    stopping an active monitor misses the tx_mntrs decrement. SKIP_TX prevents
    that increment, and OWL already requests incoming frames only.
    """
    def attribute(kind, value=b""):
        size = 4 + len(value)
        return struct.pack("=HH", size, kind) + value + bytes((-size) % 4)

    try:
        if int((Path("/sys/class/net/ldack0") / "flags").read_text().strip(), 16) & 1:
            raise RuntimeError("ldack0 deve essere ancora disattivata")
        index = socket.if_nametoindex("ldack0")
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 16) as channel:
            channel.bind((0, 0))
            replies = _generic_netlink_request(channel, 16, 3, attribute(2, b"nl80211\0"), 1)
            ids = [struct.unpack("=H", value)[0] for reply in replies
                   for kind, value in _netlink_attributes(reply) if kind == 1 and len(value) == 2]
            if len(ids) != 1 or ids[0] < 16:
                raise RuntimeError("Famiglia nl80211 non disponibile")
            # SET_INTERFACE=6, IFINDEX=3, MNTR_FLAGS=23; ACTIVE=6, SKIP_TX=7.
            monitor_flags = attribute(6) + attribute(7)
            attributes = attribute(3, struct.pack("=I", index)) + attribute(23 | 0x8000, monitor_flags)
            _generic_netlink_request(channel, ids[0], 6, attributes, 2)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("Impossibile configurare il monitor Wi-Fi senza eco TX: " + str(exc)) from exc


def inspect_interface(iface, require_active=True):
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,15}", iface):
        raise RuntimeError("Nome interfaccia non valido")
    device = Path("/sys/class/net") / iface
    driver = (device / "device/driver").resolve().name
    if driver != "mt7921e":
        raise RuntimeError("Questa versione supporta soltanto MediaTek MT7921/MT7922 (mt7921e).")
    phy = (device / "phy80211").resolve().name
    if not re.fullmatch(r"phy\d+", phy):
        raise RuntimeError("Scheda Wi-Fi non trovata")
    info = command(["iw", "dev", iface, "info"])
    if not re.search(r"\btype managed\b", info):
        raise RuntimeError("La scheda Wi-Fi non è in modalità normale.")
    if "Connected to" in command(["iw", "dev", iface, "link"]):
        raise RuntimeError("Disconnetti prima il Wi-Fi: AirDrop usa la scheda in esclusiva. Puoi usare Ethernet.")
    capabilities = command(["iw", "phy", phy, "info"])
    if require_active and "supports active monitor" not in capabilities.lower():
        raise RuntimeError("Il driver attuale non supporta active monitor, necessario per AirDrop. Nessuna modifica alla rete è stata eseguita.")
    return device, phy


class DriverTrial:
    """Load matching test modules in memory, then restore distribution modules."""
    MODULES = ("mt7921e", "mt7921_common", "mt792x_lib", "mt76_connac_lib", "mt76", "mac80211")

    def __init__(self, iface):
        self.iface = iface
        self.mac = None
        self.managed = None
        self.was_up = False
        self.touched = False
        self.nm_touched = False
        self.original_versions = {}

    def start(self):
        device, _ = inspect_interface(self.iface, require_active=False)
        if len(list(Path("/sys/class/ieee80211").iterdir())) != 1:
            raise RuntimeError("La prova del driver richiede una sola scheda Wi-Fi nel sistema.")
        modules = BASE / "backend/driver-compat"
        manifest = json.loads((modules / "manifest.json").read_text())
        if manifest.get("kernel") != platform.release():
            raise RuntimeError("Moduli di prova non adatti al kernel attualmente avviato.")
        for name in ("mac80211.ko", "mt792x-lib.ko", "mt7921e.ko"):
            path = modules / name
            expected = manifest["modules"][name]
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
                raise RuntimeError("Modulo di prova modificato o danneggiato: " + name)
            if command(["modinfo", "-F", "vermagic", str(path)]) != expected["vermagic"]:
                raise RuntimeError("Versione del modulo di prova non valida")
        for module in self.MODULES:
            path = Path("/sys/module") / module
            if path.exists():
                unknown = {p.name for p in (path / "holders").iterdir()} - set(self.MODULES)
                if unknown:
                    raise RuntimeError("Modulo Wi-Fi usato da altri driver: " + ", ".join(sorted(unknown)))
                loaded = (path / "srcversion").read_text().strip()
                on_disk = command(["modinfo", "-F", "srcversion", module])
                if loaded != on_disk:
                    raise RuntimeError("È già caricato un driver diverso da quello su disco: " + module)
                self.original_versions[module] = loaded
        self.mac = (device / "address").read_text().strip()
        self.was_up = bool(int((device / "flags").read_text().strip(), 16) & 1)
        self.managed = command(["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", self.iface])
        if self.managed not in ("yes", "no"):
            raise RuntimeError("Stato NetworkManager non disponibile")
        emit("status", message="Prova temporanea: caricamento dei moduli Wi-Fi corretti…")
        self.nm_touched = True
        command(["nmcli", "device", "set", self.iface, "managed", "no"])
        command(["ip", "link", "set", self.iface, "down"])
        self.touched = True
        self.unload()
        command(["insmod", str(modules / "mac80211.ko")])
        command(["modprobe", "mt76"])
        command(["modprobe", "mt76_connac_lib"])
        command(["insmod", str(modules / "mt792x-lib.ko")])
        command(["modprobe", "mt7921_common"])
        command(["insmod", str(modules / "mt7921e.ko")])
        self.iface = self.wait_interface()
        command(["nmcli", "device", "set", self.iface, "managed", "no"])
        command(["ip", "link", "set", self.iface, "down"])
        inspect_interface(self.iface)
        emit("status", message="Driver di prova caricato; active monitor disponibile.")
        return self.iface

    def wait_interface(self):
        for _ in range(80):
            for path in Path("/sys/class/net").iterdir():
                try:
                    if (path / "device/driver").resolve().name == "mt7921e" and (path / "address").read_text().strip() == self.mac:
                        managed = command(["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", path.name], check=False)
                        if managed in ("yes", "no"):
                            return path.name
                except OSError:
                    pass
            time.sleep(0.1)
        raise RuntimeError("La scheda Wi-Fi non è ricomparsa dopo il caricamento del driver.")

    def unload(self):
        for module in self.MODULES:
            if (Path("/sys/module") / module).exists():
                command(["rmmod", module])

    def cleanup(self):
        if RECOVERY.is_set():
            return
        if self.touched:
            emit("status", message="Ripristino dei moduli Wi-Fi originali di Ubuntu…")
            try:
                self.unload()
            except Exception as exc:
                emit("error", message="Impossibile rimuovere un modulo di prova: " + str(exc))
            if RECOVERY.is_set():
                return
            try:
                command(["modprobe", "mt7921e"])
                self.iface = self.wait_interface()
                for module in ("mac80211", "mt792x_lib", "mt7921e"):
                    actual = (Path("/sys/module") / module / "srcversion").read_text().strip()
                    if actual != self.original_versions.get(module):
                        raise RuntimeError("Il modulo originale non è stato ripristinato: " + module)
                emit("status", message="Moduli originali ripristinati. Nessun file di sistema sostituito.")
            except Exception as exc:
                emit("error", message="Ripristino driver incompleto; un riavvio caricherà i moduli originali. " + str(exc))
        if self.nm_touched:
            for args in (["nmcli", "device", "set", self.iface, "managed", self.managed],
                         ["ip", "link", "set", self.iface, "up" if self.was_up else "down"]):
                try:
                    command(args)
                except Exception as exc:
                    emit("error", message="Ripristino scheda Wi-Fi: " + str(exc))


class Bridge:
    def __init__(self, iface, channel, guardian, active_only=False, fixed_radio=False, fixed_ack=False, go_anchor=False,
                 max_seconds=DEFAULT_SECONDS, native_driver=False, data_rate=None):
        self.iface, self.channel, self.guardian = iface, channel, guardian
        self.max_seconds = max_seconds
        self.active_only = active_only
        self.native_driver = native_driver
        self.data_rate = data_rate
        self.fixed_radio = fixed_radio or go_anchor
        self.go_anchor = go_anchor
        self.anchor = None
        self.anchor_interface = None
        self.fixed_ack = fixed_ack
        self.created = []
        self.pm = {}
        self.owl = None
        self.nm_touched = False
        self.managed = None
        self.was_up = False
        self.awdl_owned = False
        self.native_cleanup_blocked = False

    def require_parent_up(self):
        """An up managed vif keeps paired monitors from acquiring a hidden owner."""
        if not (self.go_anchor and self.native_driver):
            return
        try:
            up = bool(int((Path("/sys/class/net") / self.iface / "flags").read_text().strip(), 16) & 1)
        except (OSError, ValueError):
            up = False
        if up:
            return
        message = "La scheda Wi-Fi principale deve restare attiva durante la sessione."
        if self.created:
            self.native_cleanup_blocked = True
            RECOVERY.set()
            STOP.set()
            emit("recovery", message=message + " Riavvia il sistema prima di riprovare.")
        raise RuntimeError(message)

    def start(self):
        device, phy = inspect_interface(self.iface)
        for name in ("ldmon0", "ldack0", "awdl0"):
            if (Path("/sys/class/net") / name).exists():
                raise RuntimeError(f"{name} è già in uso. Chiudi l'altra sessione AWDL.")
        modern = BASE / "backend/owl-modern"
        owl = modern if modern.is_file() else BASE / "backend/owl"
        if not owl.is_file():
            raise RuntimeError("Backend OWL mancante")
        if self.go_anchor:
            self.anchor = GoAnchor(self.iface, phy, self.channel, command, emit)
            self.anchor.preflight()
        self.was_up = bool(int((device / "flags").read_text().strip(), 16) & 1)
        self.managed = command(["nmcli", "-g", "GENERAL.NM-MANAGED", "device", "show", self.iface])
        if self.managed not in ("yes", "no"):
            raise RuntimeError("Impossibile leggere lo stato NetworkManager")
        emit("status", message="Preparazione della scheda Wi-Fi…")
        self.nm_touched = True
        command(["nmcli", "device", "set", self.iface, "managed", "no"])
        command(["ip", "link", "set", self.iface, "down"])
        # debugfs is normally mounted by Ubuntu. Do not remount system filesystems.
        debug = Path("/sys/kernel/debug/ieee80211") / phy / "mt76"
        for key in ("runtime-pm", "deep-sleep"):
            path = debug / key
            if path.is_file():
                self.pm[path] = path.read_text().strip()
                path.write_text("0\n")
        frequency = {6: 2437, 36: 5180, 44: 5220}[self.channel]
        group_mac = None
        if self.anchor:
            emit("status", message="Creazione del collegamento Wi-Fi Direct temporaneo…")
            group, group_mac = self.anchor.start()
            self.anchor_interface = group
            details = command(["iw", "dev", group, "info"], check=False)
            where = next((l.strip() for l in details.splitlines() if l.strip().startswith("channel")), f"canale {self.channel}")
            emit("log", source="wifi-direct", message=f"Gruppo Wi-Fi Direct {group} pronto: {where}")
            if self.native_driver:
                # The managed vif counts even while unassociated. Keep it up
                # until both monitors are gone, including if the GO process dies.
                command(["ip", "link", "set", self.iface, "up"])
                self.require_parent_up()
        if not self.active_only:
            self.require_parent_up()
            command(["iw", "phy", phy, "interface", "add", "ldmon0", "type", "monitor"])
            self.created.append("ldmon0")
            if group_mac:
                command(["ip", "link", "set", "ldmon0", "address", group_mac])
            command(["ip", "link", "set", "ldmon0", "up"])
            if not self.anchor:
                command(["iw", "dev", "ldmon0", "set", "freq", str(frequency)])
        # Without a GO, tune the plain monitor before adding the active one.
        command(["iw", "phy", phy, "interface", "add", "ldack0", "type", "monitor", "flags", "active"])
        self.created.append("ldack0")
        if group_mac:
            command(["ip", "link", "set", "ldack0", "address", group_mac])
        if self.go_anchor and self.native_driver:
            suppress_monitor_tx_echo()
        command(["ip", "link", "set", "ldack0", "up"])
        if not self.anchor:
            command(["iw", "dev", "ldack0", "set", "freq", str(frequency)])
        for path in self.pm:
            path.write_text("0\n")
        env = dict(SAFE_ENV)
        election = ["-E"] if modern.is_file() else []
        if self.fixed_radio and not modern.is_file():
            raise RuntimeError("La prova a canale fisso richiede il backend aggiornato.")
        strategy = "intersect" if self.fixed_radio else "verbatim"
        if self.fixed_radio:
            election.append("-F")
            if self.fixed_ack:
                election.append("-A")
        if self.data_rate:
            election += ["-R", self.data_rate]
        self.owl = subprocess.Popen([str(owl), "-i", "ldack0", "-c", str(self.channel), "-N", "-S", strategy, *election, "-vv"],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.awdl_owned = True
        threading.Thread(target=self.read_owl, daemon=True).start()
        for _ in range(100):
            if STOP.is_set():
                return
            if self.owl.poll() is not None:
                raise RuntimeError("OWL non si è avviato: consulta il registro nell'app.")
            if Path("/sys/class/net/awdl0").exists():
                command(["ip", "link", "set", "awdl0", "up"])
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("OWL non ha creato l'interfaccia awdl0.")
        for _ in range(50):
            addr = command(["ip", "-6", "address", "show", "dev", "awdl0"])
            if "inet6 fe80:" in addr and "tentative" not in addr:
                threading.Thread(target=self.observe_discovery, daemon=True).start()
                emit("ready", interface="awdl0", channel=self.channel)
                return
            time.sleep(0.1)
        raise RuntimeError("IPv6 locale AWDL non disponibile")

    def observe_discovery(self):
        """Bounded, receive-only protocol summaries for the whole session; never retains payloads."""
        counts, reported = {}, {}
        try:
            with socket.socket(socket.AF_PACKET, socket.SOCK_DGRAM, socket.htons(3)) as probe:
                probe.bind(("awdl0", 3))
                probe.settimeout(1)
                report_at = time.monotonic() + 10
                while not STOP.is_set() and self.owl.poll() is None:
                    try:
                        packet, address = probe.recvfrom(1500)
                    except socket.timeout:
                        packet = b""
                    if packet and address[0] == "awdl0":
                        kind = summarize_packet(packet, address[2] == 4)
                        if kind and (kind in counts or len(counts) < 80):
                            counts[kind] = counts.get(kind, 0) + 1
                    if time.monotonic() >= report_at:
                        report_at = time.monotonic() + 10
                        changed = {k: v for k, v in counts.items() if reported.get(k) != v}
                        reported.update(changed)
                        chunk = {}
                        for kind, count in sorted(changed.items()):
                            chunk[kind] = count
                            if len(json.dumps(chunk, ensure_ascii=False)) > 1100:
                                emit("log", source="discovery", message="Diagnosi AWDL: " + json.dumps(chunk, ensure_ascii=False))
                                chunk = {}
                        if chunk:
                            emit("log", source="discovery", message="Diagnosi AWDL: " + json.dumps(chunk, ensure_ascii=False))
        except OSError as exc:
            emit("log", source="discovery", message="Diagnosi AWDL terminata: " + str(exc))

    def read_owl(self):
        observed = False
        for line in self.owl.stdout:
            if ("TRACE:" not in line and "DEBUG:" not in line) or "changed channel sequence" in line:
                emit("log", source="owl", message=line.rstrip()[:1500])
            if "add peer" in line.lower() or (not observed and "awdl_action: receive" in line):
                observed = True
                emit("peer", message="Dispositivo Apple rilevato")

    def supervise(self):
        # 0 means "until stopped"; the guardian check below still ends the session if the app dies.
        deadline = time.monotonic() + self.max_seconds if self.max_seconds else None
        while not STOP.is_set() and (deadline is None or time.monotonic() < deadline):
            self.require_parent_up()
            if self.owl.poll() is not None:
                raise RuntimeError("Il collegamento AWDL si è interrotto.")
            if self.anchor and (not self.anchor.alive() or
                                any(not (Path("/sys/class/net") / name).exists()
                                    for name in [*self.created, self.anchor_interface])):
                raise RuntimeError("Il gruppo Wi-Fi Direct si è interrotto.")
            try:
                os.kill(self.guardian, 0)
            except ProcessLookupError:
                break
            readable, _, _ = select.select([sys.stdin], [], [], 0.5)
            if readable:
                line = sys.stdin.readline()
                if not line or line.strip() == "stop":
                    break

    def cleanup_anchor(self):
        if not self.anchor:
            return True
        if self.anchor.cleanup():
            return True
        RECOVERY.set()
        STOP.set()
        emit("recovery", message="Ripristino Wi-Fi Direct incompleto. Riavvia Ubuntu per ripristinare la scheda Wi-Fi.")
        return False

    def cleanup(self):
        emit("status", message="Ripristino della scheda Wi-Fi…")
        if self.owl and self.owl.poll() is None:
            try:
                self.owl.terminate()
                try:
                    self.owl.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.owl.kill()
                    self.owl.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired) as exc:
                emit("log", source="cleanup", message="Arresto OWL: " + str(exc))
        if self.awdl_owned and Path("/sys/class/net/awdl0").exists():
            try:
                command(["ip", "link", "delete", "awdl0"], check=False)
            except Exception as exc:
                emit("log", source="cleanup", message=str(exc))
        if self.native_cleanup_blocked:
            return
        if RECOVERY.is_set():
            self.cleanup_anchor()
            return
        for name in reversed(self.created.copy()):
            try:
                self.require_parent_up()
            except RuntimeError:
                # Removing any other vif after losing the guard could exercise
                # the hidden monitor cleanup path. Recovery requires a reboot.
                return
            try:
                command(["iw", "dev", name, "del"])
            except Exception as exc:
                emit("log", source="cleanup", message=str(exc))
            remaining = (Path("/sys/class/net") / name).exists()
            if self.go_anchor and self.native_driver and remaining:
                self.native_cleanup_blocked = True
                RECOVERY.set()
                STOP.set()
                emit("recovery", message="Rimozione monitor incompleta. Riavvia il sistema per ripristinare la scheda Wi-Fi.")
                return
            if not remaining:
                self.created.remove(name)
            if RECOVERY.is_set():
                self.cleanup_anchor()
                return
        if self.go_anchor and self.native_driver and any(
                (Path("/sys/class/net") / name).exists() for name in self.created):
            self.native_cleanup_blocked = True
            RECOVERY.set()
            STOP.set()
            emit("recovery", message="Rimozione monitor incompleta. Riavvia il sistema per ripristinare la scheda Wi-Fi.")
            return
        if not self.cleanup_anchor():
            return
        for path, previous in self.pm.items():
            try:
                path.write_text(previous + "\n")
            except OSError as exc:
                emit("log", source="cleanup", message=str(exc))
        if self.nm_touched:
            try:
                command(["ip", "link", "set", self.iface, "up" if self.was_up else "down"])
            except Exception as exc:
                emit("error", message="Ripristino stato Wi-Fi incompleto: " + str(exc))
            try:
                command(["nmcli", "device", "set", self.iface, "managed", self.managed])
            except Exception as exc:
                emit("error", message="Ripristino Wi-Fi incompleto: " + str(exc))
        emit("stopped")


DATA_RATE = re.compile(r"(?:6|9|12|18|24|36|48|54|mcs(?:[0-9]|1[0-5])(?:-sgi)?)\Z")


def data_rate_arg(value):
    if not DATA_RATE.fullmatch(value):
        raise argparse.ArgumentTypeError("rate non valido: " + value)
    return value


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--iface", required=True)
    p.add_argument("--channel", type=int, choices=[6, 36, 44], default=6)
    p.add_argument("--guardian", type=int, required=True)
    p.add_argument("--driver-trial", action="store_true")
    p.add_argument("--fixed-radio", action="store_true")
    p.add_argument("--fixed-ack", action="store_true")
    p.add_argument("--go-anchor", action="store_true")
    p.add_argument("--data-rate", type=data_rate_arg, default=None,
                   help="PHY rate of data frames sent to the phone (OWL -R): 6..54 Mbit/s, mcsN or mcsN-sgi")
    p.add_argument("--max-seconds", type=int, default=DEFAULT_SECONDS,
                   help="session length; 0 keeps it until stopped (at most %d otherwise)" % MAX_SECONDS)
    args = p.parse_args()
    if not 0 <= args.max_seconds <= MAX_SECONDS:
        p.error("--max-seconds fuori intervallo")
    if os.geteuid() != 0 or args.guardian <= 1:
        emit("error", message="Attiva il collegamento dall'app con autenticazione Ubuntu.")
        return 1
    lock = open("/run/linux-airdrop.lock", "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        emit("error", message="Un'altra sessione Linux AirDrop è già attiva.")
        return 1
    trial = DriverTrial(args.iface) if args.driver_trial else None
    bridge = None
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, lambda *_: STOP.set())
    rc = 0
    try:
        if not args.driver_trial and (not args.go_anchor or args.channel not in (6, 36, 44)):
            raise RuntimeError("Con i driver di sistema usa Wi-Fi Direct sui canali 6, 36 o 44.")
        if args.go_anchor:
            if not (BASE / "backend/owl-modern").is_file():
                raise RuntimeError("Wi-Fi Direct richiede il backend OWL aggiornato.")
            _, phy = inspect_interface(args.iface, require_active=False)
            for name in ("ldmon0", "ldack0", "awdl0"):
                if (Path("/sys/class/net") / name).exists():
                    raise RuntimeError(f"{name} è già in uso. Chiudi l’altra sessione AWDL.")
            GoAnchor(args.iface, phy, args.channel, command, emit).preflight()
        iface = trial.start() if trial else args.iface
        bridge = Bridge(iface, args.channel, args.guardian, active_only=False, fixed_radio=args.fixed_radio, fixed_ack=args.fixed_ack, go_anchor=args.go_anchor, max_seconds=args.max_seconds, native_driver=not args.driver_trial, data_rate=args.data_rate)
        bridge.start()
        if not STOP.is_set():
            bridge.supervise()
    except Exception as exc:
        emit("error", message=str(exc))
        rc = 1
    finally:
        if bridge:
            bridge.cleanup()
        if trial:
            trial.cleanup()
        lock.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
