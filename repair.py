"""Known problems, how to recognise them, and a policy that stops repairs from looping.

Detection is pure: it takes the checks from diagnostics.py and the facts the app collected, and returns
`Problem`s. Running a fix needs the app (it owns the processes), so this module only describes them:

    restart_receiver   stop and start the OpenDrop receiver; it re-reads awdl0's index and announces again
    restart_ble        restart the Bluetooth wake-up advertisement
    restart_session    stop the session (the normal restore path) and start it again; may ask for the password
    kill_strays        end leftover OWL-less helpers of this user (receiver/BLE processes nobody owns)
    none               nothing the app may do; the problem text says what the user should do (usually reboot)

Nothing here changes system configuration permanently, and nothing runs as root. Fixes that interrupt the
radio or kill processes are marked `risky` so the app asks first. Every attempt is written to repair.log, and
`Attempts` refuses a fix that has been tried too often, so a persistent fault ends in an explanation, never in
a restart loop.
"""
import json
import os
from pathlib import Path
import signal
import time

import diagnostics as dg

MAX_ATTEMPTS = 3
WINDOW = 1800          # seconds over which attempts are counted
MIN_GAP = 15           # seconds between two attempts at the same problem
LOG_LIMIT = 200_000


class Problem:
    def __init__(self, ident, title, why, fix="none", fix_label="", risky=False, detail=""):
        self.id, self.title, self.why, self.fix = ident, title, why, fix
        self.fix_label, self.risky, self.detail = fix_label, risky, detail

    def as_dict(self):
        return dict(self.__dict__)


def scan_strays(system, known_pids=(), uid=None):
    """Processes of ours that run with nobody to own them: (user-owned, root-owned) lists of (pid, name)."""
    uid = os.getuid() if uid is None else uid
    mine, root_owned = [], []
    for pid, owner, argv in system.processes():
        names = {Path(part).name for part in argv[:3]}
        match = next((n for n in dg.OWN_PROCESSES if n in names), None)
        if not match or pid in known_pids or pid == os.getpid():
            continue
        (mine if owner == uid else root_owned).append((pid, match))
    return mine, root_owned


def detect(checks, facts, system, known_pids=()):
    """Problems worth fixing, most important first."""
    by_id = {c.id: c for c in checks}
    active, starting = bool(facts.get("session_active")), bool(facts.get("session_starting"))
    problems = []

    def add(*args, **kwargs):
        problems.append(Problem(*args, **kwargs))

    if facts.get("recovery_needed"):
        add("reboot", "Serve riavviare Ubuntu", "Il driver Wi-Fi si è bloccato; solo un riavvio ricarica i driver originali.")
    modules = by_id.get("modules")
    if modules and modules.state == dg.ERROR and not (active or starting):
        add("modules-mixed", "Moduli Wi-Fi in uno stato misto", modules.summary + ". Solo un riavvio garantisce i driver originali.",
            detail="\n".join(modules.details))
    mine, root_owned = scan_strays(system, known_pids)
    if not (active or starting):
        if mine:
            names = ", ".join(sorted({f"{n} ({p})" for p, n in mine}))
            add("stray-processes", "Processi di AirDrop rimasti attivi",
                f"Nessuna sessione è in corso ma ci sono ancora processi dell’app: {names}.",
                fix="kill_strays", fix_label="Chiudi i processi rimasti", risky=True)
        if root_owned:
            names = ", ".join(sorted({f"{n} ({p})" for p, n in root_owned}))
            add("stray-root", "Rimasto attivo un programma di sistema",
                f"{names}. Appartiene a root e l’app non può chiuderlo da sola: interrompi la visibilità; se resta, riavvia Ubuntu.")
    if active or starting:
        awdl, owl, p2p, mdns = by_id.get("awdl"), by_id.get("owl"), by_id.get("p2p"), by_id.get("mdns")
        if owl and owl.state == dg.ERROR:
            add("owl-init", "OWL non è partito o si è fermato", owl.summary, fix="restart_session",
                fix_label="Riattiva la visibilità", risky=True)
        elif awdl and awdl.state == dg.ERROR:
            add("awdl-missing", "L’interfaccia AWDL non c’è o non è configurata", awdl.summary, fix="restart_session",
                fix_label="Riattiva la visibilità", risky=True)
        if p2p and p2p.state == dg.ERROR:
            add("wifi-direct", "Il Wi-Fi Direct non è stato inizializzato", p2p.summary, fix="restart_session",
                fix_label="Riattiva la visibilità", risky=True)
        if active and mdns and mdns.state == dg.ERROR:
            wrong = mdns.code == "index"
            add("mdns-interface" if wrong else "service-silent",
                "Annunci mDNS sull’interfaccia sbagliata" if wrong else "Il PC non viene annunciato",
                mdns.summary, fix="restart_receiver", fix_label="Riavvia il ricevitore")
        service = by_id.get("service")
        if active and service and service.state == dg.ERROR:
            add("receiver-down", "Il servizio di ricezione non è in esecuzione", service.summary, fix="restart_receiver",
                fix_label="Riavvia il ricevitore")
        blue = by_id.get("bluetooth")
        if active and blue and blue.state == dg.WARN and "segnale di risveglio" in blue.summary:
            add("ble-down", "Il segnale Bluetooth di risveglio è spento", blue.summary, fix="restart_ble",
                fix_label="Riavvia il segnale Bluetooth")
    rank = {"reboot": 0, "modules-mixed": 1}
    return sorted(problems, key=lambda p: rank.get(p.id, 5))


def kill_strays(pids, system=None, uid=None, wait=3.0):
    """TERM then KILL the given user-owned processes after re-checking each is still what it was. Returns the pids ended."""
    from diagnostics import System
    system = system or System()
    uid = os.getuid() if uid is None else uid
    current = {pid: (owner, argv) for pid, owner, argv in system.processes()}
    targets = []
    for pid in pids:
        owner_argv = current.get(pid)
        if not owner_argv or owner_argv[0] != uid or pid == os.getpid():
            continue
        if not any(Path(part).name in dg.OWN_PROCESSES for part in owner_argv[1][:3]):
            continue      # the number now belongs to something else
        targets.append(pid)
    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and any(_alive(pid) for pid in targets):
        time.sleep(0.1)
    for pid in targets:
        if _alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    return [pid for pid in targets if not _alive(pid)]


def _alive(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:        # a zombie has not been reaped yet but is already gone for our purposes
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1][0] != "Z"
    except (OSError, IndexError):
        return True


class Attempts:
    """Remembers repair attempts on disk, refuses too frequent ones, and keeps a readable log."""

    def __init__(self, folder, clock=time.time):
        self.folder, self.clock = Path(folder), clock
        self.state_file = self.folder / "repair-state.json"
        self.log_file = self.folder / "repair.log"

    def _load(self):
        try:
            data = json.loads(self.state_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def allowed(self, problem):
        """(True, '') or (False, reason in Italian)."""
        now = self.clock()
        recent = [t for t in self._load().get(problem, []) if isinstance(t, (int, float)) and now - t < WINDOW]
        if recent and now - max(recent) < MIN_GAP:
            return False, f"Riprova tra {int(MIN_GAP - (now - max(recent))) + 1} secondi: l’ultimo tentativo è appena finito."
        if len(recent) >= MAX_ATTEMPTS:
            return False, ("Ho già provato questa riparazione {n} volte negli ultimi {m} minuti senza successo. "
                           "Non la ripeto ancora: copia il rapporto diagnostico e, se serve, riavvia Ubuntu.").format(n=len(recent), m=WINDOW // 60)
        return True, ""

    def record(self, problem, fix, outcome):
        now = self.clock()
        data = self._load()
        data = {key: [t for t in value if isinstance(t, (int, float)) and now - t < WINDOW] for key, value in data.items()
                if isinstance(value, list)}
        data.setdefault(problem, []).append(now)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(data))
        except OSError:
            pass
        self.log(f"[{problem}] {fix}: {outcome}")

    def log(self, line):
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            if self.log_file.exists() and self.log_file.stat().st_size > LOG_LIMIT:
                self.log_file.replace(self.log_file.with_suffix(".log.1"))
            with open(self.log_file, "a", encoding="utf-8") as handle:
                handle.write(time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.clock())) + " " + line.replace("\n", " ") + "\n")
        except OSError:
            pass

    def reset(self):
        self.state_file.unlink(missing_ok=True)
