"""Live health checks for everything AirDrop depends on, and a report that can be pasted into a bug report.

Pure Python and independent of GTK. All reading goes through `System`, which can be pointed at a fake
file tree and a fake command runner, so every check is tested without hardware.

Nothing here changes the system: it reads /sys, /proc and the output of `iw dev`, `systemctl is-active`
and `bluetoothctl show`. Those commands run with a short time limit and never in a way that could wait on
a stuck wireless driver for long; a command that does not answer is reported as "non risponde".

States: ok (green), warn (yellow), error (red) and idle (grey, "not in use right now", which is normal
for things that only exist while the PC is visible).
"""
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import time

OK, WARN, ERROR, IDLE = "ok", "warn", "error", "idle"
STATE_LABELS = {OK: "Funziona", WARN: "Attenzione", ERROR: "Errore", IDLE: "Non in uso"}
STATE_ORDER = {ERROR: 0, WARN: 1, OK: 2, IDLE: 3}

NATIVE_MODULES = ("mac80211", "mt792x_lib", "mt7921e")
PCI_NAMES = {"0x0616": "MT7922", "0x7961": "MT7921", "0x7922": "MT7922", "0x0608": "MT7921K"}
AIRDROP_PORT = 8771
MDNS_PORT = 5353
ANNOUNCE_MAX_AGE = 45.0       # seconds without a visible announcement before it is called a problem
OWN_PROCESSES = ("owl-modern", "opendrop_runner.py", "blewake.py", "root_bridge.py")

EXPLAIN = {
    "recovery": "Dopo un blocco del driver Wi-Fi l’app si ferma per proteggere il sistema: serve un riavvio di Ubuntu, che ricarica i driver originali.",
    "card": "AirDrop sul PC usa la scheda Wi-Fi MediaTek MT7921/MT7922. È l’unica scheda supportata.",
    "kernel": "La correzione del driver inclusa nell’app è costruita per una versione precisa del kernel. Con un altro kernel serve un driver che supporti già l’«active monitor».",
    "driver": "Il driver mt7921e comanda la scheda Wi-Fi. Deve essere caricato perché l’app possa usarla.",
    "modules": "Mentre sei visibile, l’app carica temporaneamente tre moduli corretti del driver (mac80211, mt792x-lib, mt7921e). Quando finisci tornano quelli originali di Ubuntu.",
    "owl": "OWL è il programma che parla il protocollo radio AWDL di Apple. Senza OWL l’iPhone non vede il PC.",
    "awdl": "awdl0 è l’interfaccia virtuale creata da OWL: tutto il traffico AirDrop passa da qui.",
    "p2p": "Il gruppo Wi-Fi Direct tiene fermo il canale radio (6), così i trasferimenti riescono. Si crea da solo all’attivazione.",
    "mdns": "Gli annunci mDNS (_airdrop._tcp) sono ciò che fa comparire il PC nel menu AirDrop dell’iPhone. Devono uscire da awdl0, non da altre interfacce.",
    "bluetooth": "Il Bluetooth serve a «svegliare» l’iPhone, che altrimenti non ascolta l’AirDrop.",
    "interface": "Mostra quali interfacce di rete l’app sta usando e i loro numeri di indice, da cui dipende la scelta di quella giusta per gli annunci.",
    "channel": "Il canale Wi-Fi su cui lavora il collegamento AirDrop. Con il Wi-Fi Direct deve essere il 6.",
    "service": "Il servizio che riceve i file (porta 8771). Se non risponde, l’iPhone vede il PC ma non riesce a inviare.",
    "helper": "L’attivazione senza password usa un programma di sistema installato una volta con la password di amministratore.",
}


class Check:
    def __init__(self, ident, title, state, summary, details=(), hint="", code=""):
        self.id, self.title, self.state, self.summary = ident, title, state, summary
        self.details, self.hint, self.code = list(details), hint, code     # code: machine-readable cause
        self.explain = EXPLAIN.get(ident, "")

    def as_dict(self):
        return {"id": self.id, "title": self.title, "state": self.state, "summary": self.summary,
                "details": self.details, "hint": self.hint, "code": self.code}


# ------------------------------------------------------------------------------- system access
class System:
    """Reads files and runs commands, relative to `root` so tests can use a fake tree."""

    def __init__(self, root="/", run=None, release=None):
        self.root = Path(root)
        self._run = run
        self._release = release

    def path(self, name):
        return self.root / str(name).lstrip("/")

    def read(self, name):
        try:
            return self.path(name).read_text(errors="replace").strip()
        except OSError:
            return None

    def exists(self, name):
        return self.path(name).exists()

    def listdir(self, name):
        try:
            return sorted(entry.name for entry in self.path(name).iterdir())
        except OSError:
            return []

    def run(self, argv, timeout=3):
        """(return code, output). (None, reason) when the command is missing or does not answer in time."""
        if self._run is not None:
            return self._run(argv, timeout)
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                  env=dict(os.environ, LANG="C", LC_ALL="C"))
            return done.returncode, done.stdout + done.stderr
        except subprocess.TimeoutExpired:
            return None, "non risponde"
        except OSError:
            return None, "comando non disponibile"

    def release(self):
        return self._release or platform.release()

    def processes(self):
        """[(pid, uid, [argv...])] for every process whose command line can be read."""
        found = []
        for name in self.listdir("proc"):
            if not name.isdigit():
                continue
            try:
                raw = self.path(f"proc/{name}/cmdline").read_bytes()
                uid = self.path(f"proc/{name}").stat().st_uid
            except OSError:
                continue
            argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
            if argv:
                found.append((int(name), uid, argv))
        return found


# ---------------------------------------------------------------------------------- parsers
def parse_iw_dev(text):
    """Interfaces from `iw dev`: [{'name', 'type', 'ifindex', 'channel', 'mhz', 'addr'}]."""
    result, current = [], None
    for line in (text or "").splitlines():
        line = line.strip()
        match = re.match(r"Interface (\S+)", line)
        if match:
            current = {"name": match.group(1), "type": None, "ifindex": None, "channel": None, "mhz": None, "addr": None}
            result.append(current)
        elif current is not None:
            if line.startswith("type "):
                current["type"] = line.split(None, 1)[1]
            elif line.startswith("ifindex "):
                current["ifindex"] = int(line.split()[1])
            elif line.startswith("addr "):
                current["addr"] = line.split()[1]
            else:
                match = re.match(r"channel (\d+) \((\d+) MHz\)", line)
                if match:
                    current["channel"], current["mhz"] = int(match.group(1)), int(match.group(2))
    return result


def listening_ports(text, state="0A"):
    """Local ports of sockets in the given state from /proc/net/tcp6 (LISTEN) or udp6 (any state)."""
    ports = set()
    for line in (text or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 3 and ":" in fields[1] and (state is None or fields[3] == state):
            try:
                ports.add(int(fields[1].rsplit(":", 1)[1], 16))
            except ValueError:
                pass
    return ports


def link_local(text, interface):
    """(ifindex, address) of the fe80:: address of an interface from /proc/net/if_inet6, or None."""
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) >= 6 and fields[5] == interface and fields[0].startswith("fe80"):
            raw = fields[0]
            return int(fields[1], 16), ":".join(raw[i:i + 4] for i in range(0, 32, 4))
    return None


# --------------------------------------------------------------------------------- slow facts
def slow_facts(system):
    """The few things that need a command. Run these off the main thread, not more than every few seconds."""
    facts = {}
    code, output = system.run(["iw", "dev"], timeout=3)
    facts["iw"] = {"ok": code == 0, "interfaces": parse_iw_dev(output) if code == 0 else [], "error": None if code == 0 else output}
    code, output = system.run(["systemctl", "is-active", "bluetooth"], timeout=3)
    facts["bluetooth_service"] = output.strip().splitlines()[0] if output.strip() else ("unknown" if code is None else "inactive")
    code, output = system.run(["bluetoothctl", "show"], timeout=3)
    facts["bluetooth_powered"] = None if code != 0 else ("Powered: yes" in output)
    return facts


# ------------------------------------------------------------------------------------- checks
def _manifest(base):
    try:
        return json.loads((Path(base) / "backend/driver-compat/manifest.json").read_text())
    except (OSError, ValueError):
        return {}


def _find_card(system, preferred=None):
    for name in ([preferred] if preferred else []) + system.listdir("sys/class/net"):
        if name and system.exists(f"sys/class/net/{name}/phy80211"):
            target = system.path(f"sys/class/net/{name}/device/driver")
            try:
                driver = Path(os.readlink(target)).name
            except OSError:
                continue
            if driver == "mt7921e" and not name.startswith(("ldmon", "ldack", "p2p-")):
                return name
    return None


def _stage(facts):
    return facts.get("session_active") or facts.get("session_starting")


def check_all(system, facts, slow=None, base=None):
    """Every check, worst first. `facts` comes from the app (see app.diagnostic_facts); `slow` from slow_facts()."""
    base = base or Path(__file__).resolve().parent
    slow = slow or {}
    manifest = _manifest(base)
    checks = []
    active, starting = bool(facts.get("session_active")), bool(facts.get("session_starting"))
    in_use = active or starting
    net = "sys/class/net"
    inet6 = system.read("proc/net/if_inet6") or ""
    iw = {item["name"]: item for item in slow.get("iw", {}).get("interfaces", [])}

    if facts.get("recovery_needed"):
        checks.append(Check("recovery", "Riavvio necessario", ERROR, "Il driver Wi-Fi si è bloccato",
                            hint="Salva il lavoro e riavvia Ubuntu: carica di nuovo i driver originali."))

    # --- card
    card = _find_card(system, facts.get("iface"))
    if card:
        pci = system.read(f"{net}/{card}/device/device") or ""
        chip = PCI_NAMES.get(pci.lower(), f"MediaTek (id {pci or '?'})")
        checks.append(Check("card", "Scheda MediaTek MT7922", OK if "MT792" in chip else WARN,
                            f"{chip} rilevata ({card})",
                            [f"Interfaccia: {card}", f"Identificativo PCI: {pci or 'sconosciuto'}"],
                            "" if "MT792" in chip else "Il modello non è tra quelli provati: potrebbe non funzionare."))
    else:
        checks.append(Check("card", "Scheda MediaTek MT7922", ERROR, "Nessuna scheda MediaTek MT7921/MT7922 trovata",
                            hint="Questa app funziona solo con schede MediaTek MT7921/MT7922. Con un adattatore USB o un’altra scheda non è utilizzabile."))

    expected = {name.replace("-", "_").removesuffix(".ko"): data.get("srcversion")
                for name, data in manifest.get("modules", {}).items()}
    present = {name: system.read(f"sys/module/{name}/srcversion") for name in NATIVE_MODULES}
    patched = {name: bool(expected.get(name) and present.get(name) == expected[name]) for name in NATIVE_MODULES}
    lines = [f"{name}: {'modificato' if patched[name] else 'originale' if present[name] else 'non caricato'}" for name in NATIVE_MODULES]
    count = sum(patched.values())

    # --- kernel
    release = system.release()
    wanted = manifest.get("kernel")
    # The stock driver supports AirDrop by itself only if the support does not come from our patched modules.
    native = bool(facts.get("radio_ready")) and count < len(NATIVE_MODULES)
    if native:
        checks.append(Check("kernel", "Versione del kernel", OK, f"Linux {release}: il driver supporta già AirDrop",
                            [f"Kernel: {release}"]))
    elif wanted and release == wanted and facts.get("trial_ready"):
        checks.append(Check("kernel", "Versione del kernel", OK, f"Linux {release}: la correzione del driver è compatibile",
                            [f"Kernel: {release}", f"Correzione costruita per: {wanted}"]))
    else:
        checks.append(Check("kernel", "Versione del kernel", ERROR, f"Linux {release}: nessuna correzione compatibile",
                            [f"Kernel: {release}", f"Correzione costruita per: {wanted or 'nessuna'}"],
                            "Il driver di questo kernel non supporta l’«active monitor» e la correzione inclusa è per un altro kernel."))

    # --- driver
    loaded = system.exists("sys/module/mt7921e")
    checks.append(Check("driver", "Driver wireless", OK if loaded else ERROR,
                        "mt7921e caricato" if loaded else "mt7921e non è caricato",
                        [f"Srcversion: {system.read('sys/module/mt7921e/srcversion') or 'non disponibile'}"],
                        "" if loaded else "Il driver della scheda non è in memoria: riavvia Ubuntu o ricarica il modulo mt7921e."))

    # --- modified modules
    if native:
        checks.append(Check("modules", "Moduli modificati", IDLE, "Non servono: il driver di sistema basta", lines))
    elif count == len(NATIVE_MODULES):
        checks.append(Check("modules", "Moduli modificati", OK if in_use else WARN,
                            "Correzione temporanea del driver caricata" if in_use else "Moduli modificati ancora caricati senza sessione",
                            lines, "" if in_use else "Interrompi la visibilità; se restano caricati, un riavvio di Ubuntu ripristina i driver originali."))
    elif count == 0:
        if in_use:
            checks.append(Check("modules", "Moduli modificati", ERROR, "La correzione del driver non risulta caricata", lines,
                                "L’attivazione dovrebbe caricarla da sola. Guarda il registro o prova «Ripara»."))
        else:
            checks.append(Check("modules", "Moduli modificati", IDLE, "Driver originali di Ubuntu (normale quando non sei visibile)", lines))
    else:
        checks.append(Check("modules", "Moduli modificati", ERROR, "Stato misto: solo alcuni moduli sono quelli modificati", lines,
                            "Riavvia Ubuntu per tornare ai driver originali. Non provare ad attivare AirDrop in questo stato."))

    # --- OWL
    owl_binary = Path(base) / "backend/owl-modern"
    ack_line = None
    if "radio_ack" in facts:
        ack_line = "Conferme radio per l’invio: " + ("attive" if facts["radio_ack"] else "disattivate (gli invii verso l’iPhone si bloccano facilmente)")
    owl_running = any(Path(argv[0]).name == "owl-modern" for _, _, argv in system.processes())
    if not owl_binary.is_file() or not os.access(owl_binary, os.X_OK):
        checks.append(Check("owl", "OWL", ERROR, "Il programma OWL non è nell’app o non è eseguibile", [str(owl_binary)],
                            "Reinstalla Linux AirDrop."))
    elif in_use:
        checks.append(Check("owl", "OWL", OK if owl_running else (WARN if starting and not active else ERROR),
                            "In esecuzione" if owl_running else ("Si sta avviando…" if starting and not active else "Non è in esecuzione"),
                            [ack_line] if ack_line else [],
                            hint="" if owl_running or (starting and not active) else "La sessione risulta attiva ma OWL si è fermato: serve riattivare la visibilità."))
    else:
        checks.append(Check("owl", "OWL", IDLE, "Presente, non in esecuzione (normale quando non sei visibile)"))

    # --- AWDL
    awdl = link_local(inet6, "awdl0") if system.exists(f"{net}/awdl0") else None
    awdl_index = None
    if awdl is not None:
        awdl_index = awdl[0]
    elif system.exists(f"{net}/awdl0"):
        try:
            awdl_index = int(system.read(f"{net}/awdl0/ifindex") or 0) or None
        except ValueError:
            pass
    if awdl is not None:
        checks.append(Check("awdl", "AWDL", OK, f"awdl0 attiva (indice {awdl_index})", [f"Indirizzo: {awdl[1]}"]))
    elif system.exists(f"{net}/awdl0"):
        checks.append(Check("awdl", "AWDL", WARN if starting and not active else ERROR, "awdl0 esiste ma non ha un indirizzo IPv6",
                            hint="OWL non l’ha ancora configurata o si è bloccato. Attendi qualche secondo; se resta così riattiva la visibilità."))
    elif in_use:
        checks.append(Check("awdl", "AWDL", WARN if starting and not active else ERROR, "awdl0 non esiste",
                            hint="Il collegamento AWDL non è stato creato: riattiva la visibilità."))
    else:
        checks.append(Check("awdl", "AWDL", IDLE, "Nessuna interfaccia awdl0 (normale quando non sei visibile)"))

    # --- Wi-Fi Direct
    groups = [name for name in system.listdir(net) if name.startswith("p2p-")]
    want_p2p = bool(facts.get("wifi_direct")) and facts.get("channel") == 6
    if not want_p2p:
        checks.append(Check("p2p", "Wi-Fi Direct", IDLE, "Disattivato nelle impostazioni (solo monitor)"))
    elif groups:
        v6 = [system.read(f"proc/sys/net/ipv6/conf/{g}/disable_ipv6") for g in groups]
        leaking = any(value == "0" for value in v6)
        checks.append(Check("p2p", "Wi-Fi Direct", WARN if leaking else OK,
                            f"Gruppo {groups[0]} attivo" + (" ma con IPv6 acceso" if leaking else ""),
                            [f"Interfaccia: {g}" for g in groups],
                            "Con IPv6 acceso sul gruppo gli annunci rischiano di uscire dall’interfaccia sbagliata." if leaking else ""))
    elif in_use:
        checks.append(Check("p2p", "Wi-Fi Direct", WARN if starting and not active else ERROR, "Il gruppo Wi-Fi Direct non è stato creato",
                            hint="Il Wi-Fi Direct è attivo nelle impostazioni ma il gruppo non c’è. Riattiva la visibilità."))
    else:
        checks.append(Check("p2p", "Wi-Fi Direct", IDLE, "Si crea all’attivazione (impostazione attiva)"))

    # --- mDNS
    age = facts.get("announce_age")
    receiver_index = facts.get("receiver_ifindex")
    udp = listening_ports(system.read("proc/net/udp6"), state=None)
    details = [f"Porta mDNS 5353 aperta: {'sì' if MDNS_PORT in udp else 'no'}"]
    if receiver_index and awdl_index:
        details.append(f"Indice usato dal ricevitore: {receiver_index} · indice di awdl0 ora: {awdl_index}")
    if facts.get("cli") and active:
        checks.append(Check("mdns", "mDNS", IDLE, "Gli annunci si possono verificare solo dentro l’app (pagina Diagnostica)", details))
    elif not active:
        checks.append(Check("mdns", "mDNS", IDLE, "Gli annunci partono quando il PC è visibile", details))
    elif receiver_index and awdl_index and receiver_index != awdl_index:
        checks.append(Check("mdns", "mDNS", ERROR, "Il ricevitore annuncia su un’interfaccia che non esiste più", details,
                            "awdl0 è stata ricreata con un altro indice: riavviare il ricevitore la rilega a quella giusta.",
                            code="index"))
    elif age is None:
        checks.append(Check("mdns", "mDNS", WARN, "Ancora nessun annuncio visto", details,
                            "Normale nei primi secondi. Se dura più di un minuto il PC non comparirà sull’iPhone."))
    elif age > ANNOUNCE_MAX_AGE:
        checks.append(Check("mdns", "mDNS", ERROR, f"Nessun annuncio da {int(age)} secondi", details,
                            "Il PC potrebbe non comparire sull’iPhone: riavviare il ricevitore rimette in moto gli annunci.",
                            code="silent"))
    else:
        checks.append(Check("mdns", "mDNS", OK, "Annunci _airdrop._tcp in corso su awdl0", details + [f"Ultimo annuncio visto {int(age)} s fa"]))

    # --- Bluetooth
    adapters = [n for n in system.listdir("sys/class/bluetooth") if re.fullmatch(r"hci\d+", n)]
    blocked = []
    for entry in system.listdir("sys/class/rfkill"):
        if system.read(f"sys/class/rfkill/{entry}/type") == "bluetooth":
            if system.read(f"sys/class/rfkill/{entry}/soft") == "1" or system.read(f"sys/class/rfkill/{entry}/hard") == "1":
                blocked.append(entry)
    service = slow.get("bluetooth_service")
    powered = slow.get("bluetooth_powered")
    ble = bool(facts.get("ble_running"))
    if not adapters:
        checks.append(Check("bluetooth", "Bluetooth", ERROR, "Nessun adattatore Bluetooth trovato",
                            hint="Senza Bluetooth l’iPhone non viene svegliato e potrebbe non vedere il PC."))
    elif blocked:
        checks.append(Check("bluetooth", "Bluetooth", ERROR, "Il Bluetooth è bloccato (modalità aereo o interruttore)", [f"rfkill: {', '.join(blocked)}"],
                            "Attiva il Bluetooth dalle impostazioni di Ubuntu."))
    elif service and service not in ("active", "unknown"):
        checks.append(Check("bluetooth", "Bluetooth", ERROR, f"Il servizio bluetooth è «{service}»", [f"Adattatore: {adapters[0]}"],
                            "Avvia il servizio: sudo systemctl start bluetooth"))
    elif powered is False:
        checks.append(Check("bluetooth", "Bluetooth", ERROR, "L’adattatore Bluetooth è spento", [f"Adattatore: {adapters[0]}"],
                            "Accendi il Bluetooth dalle impostazioni di Ubuntu."))
    elif active and not ble:
        checks.append(Check("bluetooth", "Bluetooth", WARN, "Pronto, ma il segnale di risveglio non è attivo", [f"Adattatore: {adapters[0]}"],
                            "«Ripara» può riavviare il segnale Bluetooth senza toccare il Wi-Fi."))
    else:
        checks.append(Check("bluetooth", "Bluetooth", OK, "Pronto" + (" · segnale di risveglio attivo" if ble else ""), [f"Adattatore: {adapters[0]}"]))

    # --- selected interface
    index = None
    if card:
        try:
            index = int(system.read(f"{net}/{card}/ifindex") or 0) or None
        except ValueError:
            pass
    lines = [f"Scheda Wi-Fi: {card or 'non trovata'}" + (f" (indice {index})" if index else ""),
             f"AWDL: awdl0" + (f" (indice {awdl_index})" if awdl_index else " (assente)")]
    if groups:
        lines.append("Gruppo Wi-Fi Direct: " + ", ".join(groups))
    checks.append(Check("interface", "Interfaccia di rete selezionata", OK if card else ERROR,
                        f"{card or 'nessuna'} → awdl0" if card else "Nessuna interfaccia Wi-Fi utilizzabile", lines))

    # --- channel
    configured = facts.get("channel")
    seen = None
    for name in (groups + ["ldack0", "ldmon0"]):
        if name in iw and iw[name]["channel"]:
            seen = iw[name]
            break
    if not in_use:
        checks.append(Check("channel", "Canale wireless", IDLE, f"Canale {configured or 6} (si imposta all’attivazione)"))
    elif not slow.get("iw", {}).get("ok", True):
        checks.append(Check("channel", "Canale wireless", WARN, f"Lettura del canale non riuscita ({slow['iw'].get('error')})",
                            hint="Se «iw» non risponde il driver potrebbe essere bloccato: interrompi la visibilità e riavvia se persiste."))
    elif seen is None:
        checks.append(Check("channel", "Canale wireless", WARN, "Canale non ancora rilevato", [f"Impostato: {configured}"]))
    elif seen["channel"] != configured:
        checks.append(Check("channel", "Canale wireless", ERROR, f"In uso il canale {seen['channel']}, impostato {configured}",
                            [f"{seen['name']}: canale {seen['channel']} ({seen['mhz']} MHz)"],
                            "Il collegamento non è sul canale previsto: riattiva la visibilità."))
    else:
        checks.append(Check("channel", "Canale wireless", OK, f"Canale {seen['channel']} ({seen['mhz']} MHz)",
                            [f"Letto da {seen['name']}"]))

    # --- AirDrop service
    tcp = listening_ports(system.read("proc/net/tcp6"))
    listening = AIRDROP_PORT in tcp
    receiver = facts.get("receiver_pid")
    if not active:
        checks.append(Check("service", "Servizio AirDrop", IDLE, "Il ricevitore parte quando il PC è visibile"))
    elif receiver and listening:
        checks.append(Check("service", "Servizio AirDrop", OK, f"In ascolto sulla porta {AIRDROP_PORT}", [f"Processo: {receiver}"]))
    elif receiver:
        checks.append(Check("service", "Servizio AirDrop", WARN, f"Il ricevitore gira ma la porta {AIRDROP_PORT} non risulta in ascolto",
                            hint="Può essere ancora in avvio; se resta così riavvia il ricevitore."))
    else:
        checks.append(Check("service", "Servizio AirDrop", ERROR, "Il ricevitore non è in esecuzione",
                            hint="Riattiva la visibilità oppure usa «Ripara»."))

    # --- passwordless helper
    helper = facts.get("helper")
    texts = {"ready": (OK, "Attiva: nessuna password all’attivazione"),
             "missing": (IDLE, "Non configurata: la password serve a ogni attivazione"),
             "outdated": (WARN, "Da aggiornare dopo l’aggiornamento dell’app"),
             "other-user": (IDLE, "Configurata per un altro utente"),
             "unsafe": (ERROR, "File di sistema modificati: non viene usata")}
    if helper in texts:
        state, text = texts[helper]
        checks.append(Check("helper", "Attivazione senza password", state, text,
                            hint="Dal menu dell’app puoi aggiornarla o rimuoverla." if state in (WARN, ERROR) else ""))

    return checks


def by_severity(checks):
    """Worst first; the order inside each group is the logical order of check_all()."""
    return sorted(checks, key=lambda c: STATE_ORDER[c.state])


def overall(checks):
    states = {c.state for c in checks}
    return ERROR if ERROR in states else WARN if WARN in states else OK


# ------------------------------------------------------------------------------------- report
_MAC = re.compile(r"\b([0-9a-fA-F]{2}[:-]){3}([0-9a-fA-F]{2}[:-]){2}[0-9a-fA-F]{2}\b")
_V6 = re.compile(r"\bfe80(?::[0-9a-fA-F]{0,4}){2,7}\b")


def redact(text, home=None, user=None):
    """Hide what does not help a bug report: MAC and link-local addresses, home folder, user name."""
    home = home or str(Path.home())
    user = user or os.environ.get("USER") or Path.home().name
    text = _MAC.sub(lambda m: "xx:xx:xx:xx:" + m.group(0)[-5:].replace("-", ":"), text)
    text = _V6.sub("fe80::…", text)
    if home and home != "/":
        text = text.replace(home, "~")
    if user and len(user) > 2:
        text = re.sub(rf"\b{re.escape(user)}\b", "utente", text)
    return text


def report_text(checks, facts, version="", now=None):
    """Plain-text report: summary first, then one block per check. Personal data is masked."""
    when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now if now is not None else time.time()))
    lines = ["Rapporto diagnostico di Linux AirDrop", f"Versione: {version or 'sconosciuta'} · {when}",
             f"Kernel: {platform.release()} · Python {platform.python_version()}",
             "Sessione: " + ("attiva" if facts.get("session_active") else "in avvio" if facts.get("session_starting") else "non attiva"),
             f"Canale impostato: {facts.get('channel')} · Wi-Fi Direct: {'sì' if facts.get('wifi_direct') else 'no'}"
             + (f" · Conferme radio: {'sì' if facts.get('radio_ack') else 'no'}" if "radio_ack" in facts else ""), ""]
    for check in by_severity(checks):
        lines.append(f"[{STATE_LABELS[check.state].upper()}] {check.title}: {check.summary}")
        lines.extend("    " + line for line in check.details)
        if check.hint and check.state in (WARN, ERROR):
            lines.append("    Cosa fare: " + check.hint)
    lines.append("")
    lines.append("Gli indirizzi MAC, gli indirizzi IPv6 locali, la cartella personale e il nome utente sono stati mascherati.")
    return redact("\n".join(lines))
