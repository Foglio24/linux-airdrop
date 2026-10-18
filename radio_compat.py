"""Capability-based radio plans and read-only verification of legacy modules.

Selecting a native plan does not prove interoperability. ``probe_required``
means the privileged bridge must negotiate monitor flags on its own down
monitor before the first IFF_UP. A successful SET acknowledgement alone is
insufficient: old parsers ignore unknown flags. No kernel release is used to
infer native capabilities. This module never changes radio state.
"""
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess


SUPPORTED_PCI_IDS = frozenset(("0x0616", "0x7961", "0x7922", "0x0608"))
# The shipped legacy active-monitor patch is explicitly MT7922-only.
LEGACY_PCI_IDS = ("0x0616", "0x7922")
LEGACY_FILES = ("mac80211.ko", "mt792x-lib.ko", "mt7921e.ko")
LEGACY_MODULES = ("mt7921e", "mt7921_common", "mt792x_lib", "mt76_connac_lib", "mt76", "mac80211")
PROBE_STATES = frozenset(("pending", "supported", "unsupported"))
READONLY_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}


@dataclass(frozen=True)
class RadioCapabilities:
    """Observed capabilities, with a three-state monitor-API negotiation.

    ``skip_tx='unsupported'`` specifically means a malformed SKIP_TX flag was
    acknowledged (proving it was ignored), followed by successful ACTIVE-only
    configuration. It does NOT mean an arbitrary SET failure. The old mac80211
    API has no tx_mntrs counter: that counter and SKIP_TX handling were added
    together in upstream commit 754905ce1a327ee3297548e132367038cc62b3d8.
    Any failure to set valid monitor flags must abort activation in the bridge.
    """
    driver: str | None
    hardware_supported: bool
    active_monitor: bool | None
    p2p_go: bool | None
    skip_tx: str = "pending"
    missing_tools: tuple[str, ...] = ()
    monitor: bool | None = True
    pci_id: str | None = None

    def __post_init__(self):
        if self.skip_tx not in PROBE_STATES:
            raise ValueError("skip_tx must be pending, supported or unsupported")


@dataclass(frozen=True)
class LegacyBundle:
    verified: bool = False
    reason: str = "Nessuna correzione del driver verificata per questo kernel."
    code: str = "legacy_unavailable"
    details: tuple[str, ...] = ()
    hardware_ids: tuple[str, ...] = LEGACY_PCI_IDS


@dataclass(frozen=True)
class RadioPlan:
    mode: str
    state: str
    code: str
    summary: str
    details: tuple[str, ...] = ()

    @property
    def available(self):
        return self.mode != "blocked"

    @property
    def ready(self):
        return self.state == "ready"

    @property
    def requires_probe(self):
        return self.state == "probe_required"

    def as_dict(self):
        return {"mode": self.mode, "state": self.state, "code": self.code,
                "summary": self.summary, "details": list(self.details)}


def parse_iw_capabilities(text):
    """Parse successful ``iw phy ... info`` output; empty/invalid output is unknown.

    Individual interface types do not prove that their concurrent combination
    can run. The bridge must still create the group and monitors successfully.
    """
    text = text or ""
    if not re.search(r"(?m)^\s*Wiphy\s+\S+|^\s*Supported interface modes:\s*$", text):
        return {"monitor": None, "active_monitor": None, "p2p_go": None}
    return {"monitor": bool(re.search(r"(?m)^\s*\* monitor\s*$", text)),
            "active_monitor": "supports active monitor" in text.lower(),
            "p2p_go": bool(re.search(r"(?m)^\s*\* P2P-GO\s*$", text))}


def select_plan(capabilities, legacy=None):
    """Choose a plan from observed facts, preferring native over legacy.

    ``hardware_supported`` must come from actual card identity, not the name of
    a kernel. ``legacy.verified`` is only for a bundle whose checks below passed;
    the bridge repeats those checks immediately before loading anything.
    """
    cap, legacy = capabilities, legacy or LegacyBundle()

    def blocked(code, summary, details=()):
        return RadioPlan("blocked", "blocked", code, summary, tuple(details))

    if not cap.driver:
        return blocked("driver_missing", "Il driver della scheda Wi-Fi non è disponibile.")
    if cap.driver != "mt7921e" or not cap.hardware_supported:
        return blocked("hardware_unsupported", "Serve una scheda MediaTek MT7921/MT7922 con driver mt7921e.")
    if cap.missing_tools:
        return blocked("tools_missing", "Mancano programmi necessari per gestire il collegamento Wi-Fi.",
                       tuple(sorted(set(cap.missing_tools))))
    if cap.monitor is None or cap.active_monitor is None or cap.p2p_go is None:
        return blocked("capabilities_unknown", "Le capacità del driver Wi-Fi non sono state lette.",
                       ("Ripeti il controllo del driver prima di attivare AirDrop.",))

    if cap.monitor and cap.active_monitor and cap.p2p_go:
        if cap.skip_tx == "pending":
            return RadioPlan("native", "probe_required", "native_probe_required",
                             "Il driver offre monitor attivo e Wi-Fi Direct; resta la verifica dei flag del kernel.",
                             ("Il bridge negozia ACTIVE e SKIP_TX sul proprio monitor disattivato prima di attivarlo.",
                              "Il gruppo Wi-Fi Direct e le interfacce devono poter funzionare insieme sul canale scelto (6 o 44)."))
        if cap.skip_tx == "unsupported":
            return RadioPlan("native", "ready", "native_legacy_api",
                             "Monitor attivo e Wi-Fi Direct verificati con l'API monitor precedente.",
                             ("Il kernel non riconosce SKIP_TX: viene usato ACTIVE e OWL ignora gli echi dei propri pacchetti trasmessi.",
                              "L'API precedente non include il contatore tx_mntrs e non richiede la sua protezione con SKIP_TX.",
                              "Usa Wi-Fi Direct sul canale scelto (6 o 44); la compatibilità con il dispositivo Apple richiede una prova reale."))
        return RadioPlan("native", "ready", "native_ready",
                         "Monitor attivo, Wi-Fi Direct e flag del kernel verificati.",
                         ("Usa Wi-Fi Direct sul canale scelto (6 o 44); la compatibilità con il dispositivo Apple richiede una prova reale.",))

    if not cap.monitor:
        code, summary = "monitor_missing", "Il driver non offre la modalità monitor."
    elif not cap.active_monitor:
        code, summary = "active_monitor_missing", "Il driver non offre il monitor attivo necessario per AirDrop."
    elif not cap.p2p_go:
        code, summary = "p2p_go_missing", "Il driver non offre il gruppo Wi-Fi Direct necessario alla modalità nativa."
    pci = cap.pci_id.lower() if isinstance(cap.pci_id, str) else None
    if legacy.verified and pci in legacy.hardware_ids:
        return RadioPlan("legacy", "ready", "legacy_ready",
                         "È disponibile una correzione temporanea del driver verificata per questo kernel e questa scheda.",
                         (summary, *legacy.details,
                          "Il bridge ripete le verifiche del bundle e dei driver originali prima del caricamento."))
    details = [legacy.reason]
    if legacy.verified:
        details.append("La correzione inclusa è limitata a MT7922; non è verificata per questa scheda.")
    return blocked(code, summary, details)


def _run_readonly(argv, timeout):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL,
                              env=dict(READONLY_ENV))
        return done.returncode, done.stdout + done.stderr
    except subprocess.TimeoutExpired:
        return None, "Il controllo del modulo non risponde."
    except OSError:
        return None, "modinfo non disponibile."


def verify_legacy_bundle(directory, kernel, run=None, module_root="/sys/module"):
    """Read-only bundle, ABI identity and loaded-stock-driver checks.

    ``directory`` is backend/driver-compat. ``run(argv, timeout)`` returns
    ``(returncode, combined_output)`` and is injectable for tests. Hashes are
    consistency checks against the manifest, not a cryptographic publisher
    signature. No compile, insmod, rmmod or network command is performed.
    """
    directory, module_root = Path(directory), Path(module_root)
    run = run or _run_readonly

    def failure(code, reason, *details):
        return LegacyBundle(False, reason, code, tuple(details))

    def modinfo(field, target):
        try:
            code, output = run(["modinfo", "-F", field, str(target)], 3)
        except (OSError, subprocess.TimeoutExpired, TimeoutError):
            return None
        return output.strip() if code == 0 and isinstance(output, str) else None

    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if not isinstance(manifest, dict) or manifest.get("kernel") != kernel:
            return failure("legacy_kernel_mismatch", "La correzione inclusa è per un altro kernel.")
        records = manifest.get("modules")
        if not isinstance(records, dict):
            return failure("legacy_manifest_invalid", "Il manifesto della correzione del driver non è valido.")
        for name in LEGACY_FILES:
            path, record = directory / name, records.get(name)
            if not isinstance(record, dict):
                return failure("legacy_manifest_invalid", "Il manifesto della correzione del driver è incompleto.", name)
            digest, vermagic, source = record.get("sha256"), record.get("vermagic"), record.get("srcversion")
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                    or not isinstance(vermagic, str) or not vermagic.split() or vermagic.split()[0] != kernel
                    or not isinstance(source, str) or not source.strip()):
                return failure("legacy_manifest_invalid", "Il manifesto della correzione del driver non è valido.", name)
            if path.is_symlink() or not path.is_file():
                return failure("legacy_module_missing", "Un modulo della correzione manca o non è un file regolare.", name)
            with path.open("rb") as stream:
                actual_hash = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual_hash != digest:
                return failure("legacy_hash_mismatch", "Un modulo della correzione è modificato o danneggiato.", name)
            actual_vermagic, actual_source = modinfo("vermagic", path), modinfo("srcversion", path)
            if actual_vermagic is None or actual_source is None:
                return failure("legacy_modinfo_failed", "Non è stato possibile verificare i moduli con modinfo.", name)
            if actual_vermagic != vermagic:
                return failure("legacy_vermagic_mismatch", "Un modulo della correzione non corrisponde al kernel dichiarato.", name)
            if actual_source != source:
                return failure("legacy_srcversion_mismatch", "La versione di un modulo della correzione non coincide con il manifesto.", name)
        for name in LEGACY_MODULES:
            path = module_root / name
            if not path.exists():
                continue
            unknown = {item.name for item in (path / "holders").iterdir()} - set(LEGACY_MODULES)
            if unknown:
                return failure("legacy_module_in_use", "Un modulo Wi-Fi è usato da un altro driver.", name, *sorted(unknown))
            loaded = (path / "srcversion").read_text().strip()
            original = modinfo("srcversion", name)
            if not loaded or original is None:
                return failure("legacy_stock_unverified", "Non è stato possibile verificare i driver originali caricati.", name)
            if loaded != original:
                return failure("legacy_stock_mismatch", "È già caricato un driver diverso dall'originale su disco.", name)
        hardware_ids = manifest.get("hardware_ids", LEGACY_PCI_IDS)
        if (not isinstance(hardware_ids, (list, tuple)) or not hardware_ids
                or any(not isinstance(pci, str) or not re.fullmatch(r"0x[0-9a-fA-F]{4}", pci) for pci in hardware_ids)):
            return failure("legacy_manifest_invalid", "Gli identificativi hardware della correzione non sono validi.")
        return LegacyBundle(True, "Correzione verificata.", "legacy_verified",
                            (f"Kernel: {kernel}", "SHA256, vermagic, srcversion e driver originali verificati."),
                            tuple(pci.lower() for pci in hardware_ids))
    except (OSError, ValueError, TypeError, UnicodeError) as exc:
        return failure("legacy_read_failed", "La correzione del driver non può essere verificata.", type(exc).__name__)
