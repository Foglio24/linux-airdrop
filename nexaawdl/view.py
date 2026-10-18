"""Turns the engine's status snapshot and the hardware report into the texts the window shows.

Pure functions (no GTK), so they can be tested. The interface is Italian, like the rest of the app.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STATE_LABELS = {
    "stopped": "Fermo", "starting": "In avvio", "listening": "In ascolto", "observing": "Osservazione",
    "synchronised": "Sincronizzato", "degraded": "Sincronizzazione instabile", "error": "Errore",
}
STATE_SEVERITY = {"synchronised": "ok", "observing": "ok", "listening": "idle", "starting": "idle",
                  "stopped": "idle", "degraded": "warn", "error": "error"}
VERDICTS = {
    "capable-needs-live-verification": ("ok", "Hardware idoneo",
                                        "Monitor attivo e Wi-Fi Direct disponibili. L'interoperabilità con i "
                                        "dispositivi Apple va verificata con uno scambio reale."),
    "passive-rx-possible": ("warn", "Solo ricezione passiva",
                            "La scheda può ascoltare AWDL ma mancano funzioni necessarie per partecipare."),
    "incapable": ("error", "Hardware non idoneo", "La scheda non offre la modalità monitor."),
    "unknown": ("idle", "Non ancora verificato", "Il controllo dell'hardware non è stato eseguito."),
}
STALE_SECONDS = 6.0
TRANSMIT_OFF = ("disattivata: l'invio dei frame AWDL e il percorso dati IPv6 restano a OWL, perché l'iniezione dei "
                "frame da un'interfaccia monitor non è stata verificata per questo motore su hardware reale")


def read_status(path, now=None, stale_after=STALE_SECONDS):
    """The engine's last snapshot, or None. Adds `stale` when the file is older than `stale_after` seconds."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("state"), str):
        return None
    updated = data.get("updated")
    data["stale"] = not isinstance(updated, (int, float)) or (time.time() if now is None else now) - updated > stale_after
    return data


def ago(seconds):
    if seconds is None:
        return "mai"
    seconds = float(seconds)
    if seconds < 1:
        return "ora"
    if seconds < 90:
        return f"{int(seconds)} s fa"
    return f"{int(seconds // 60)} min fa"


def general(status, session_active):
    """(severity, title, subtitle) for the general state."""
    if status is None:
        return ("idle", "Motore non attivo",
                "Parte con la visibilità." if not session_active else "In attesa del primo rapporto del motore…")
    if status.get("stale"):
        if not session_active:
            return ("idle", "Motore non attivo", "Ultimo stato: " + STATE_LABELS.get(status["state"], status["state"]) + ".")
        return ("warn", "Il motore non risponde",
                "Nessun aggiornamento recente: AirDrop continua con OWL.")
    state = status["state"]
    return (STATE_SEVERITY.get(state, "idle"), STATE_LABELS.get(state, state), explain(status))


REASONS = {
    "start requested": "Avvio richiesto.", "waiting for AWDL frames": "In attesa di frame AWDL.",
    "AWDL frames heard": "Frame AWDL uditi.", "no AWDL frames recently": "Nessun frame AWDL di recente.",
    "peers heard, no window clock yet": "Dispositivi uditi, orologio delle finestre non ancora disponibile.",
    "collecting synchronisation samples": "Raccolta delle osservazioni di sincronizzazione.",
    "stop requested": "Arresto richiesto.", "restarting": "Riavvio in corso.", "process exiting": "Il motore si è fermato.",
}


def explain(status):
    """The engine's reason for its state, in Italian (the engine itself logs in English)."""
    reason = status.get("reason") or ""
    if status.get("state") in ("synchronised", "degraded"):
        quality = (status.get("sync") or {}).get("quality") or {}
        if quality.get("in_sync") is not None:
            return f"{quality['in_sync']} % delle previsioni entro 3 TU su {quality['samples']} osservazioni."
    if reason.startswith("gave up after"):
        return "Il motore si è fermato dopo troppi riavvii. AirDrop continua con OWL."
    return REASONS.get(reason, reason)


def sync_rows(status):
    if not status:
        return []
    sync = status.get("sync") or {}
    quality = sync.get("quality") or {}
    rows = []
    rows.append(("Riferimento", sync.get("reference") or "nessuno"))
    rows.append(("Master annunciato", sync.get("master") or "sconosciuto"))
    if quality.get("in_sync") is not None:
        rows.append(("Previsioni entro 3 TU", f"{quality['in_sync']} % su {quality['samples']} osservazioni"))
        rows.append(("Errore medio", f"{quality['mean_abs_error_tu']} TU"))
    check = sync.get("window_check") or {}
    if check.get("consistent_pct") is not None:
        rows.append(("Frame nel canale atteso", f"{check['consistent_pct']} % su {check['checked']}"))
    return rows


def wireless_rows(status, hw):
    rows = []
    wireless = (status or {}).get("wireless") or {}
    channel = wireless.get("channel")
    rows.append(("Canale operativo", str(channel) if channel else "non ancora rilevato"))
    rows.append(("Modalità dell'interfaccia", "monitor passivo (ascolto)" if status else "—"))
    caps = (status or {}).get("capabilities") or {}
    rows.append(("Ricezione AWDL", "sì" if caps.get("receive") else "no"))
    rows.append(("Trasmissione NexaAWDL", TRANSMIT_OFF if status and not caps.get("transmit") else "sì"))
    if hw:
        rows.append(("Monitor attivo", "sì" if hw.get("active_monitor") else "no"))
        rows.append(("Wi-Fi Direct (P2P-GO)", "sì" if hw.get("p2p_go") else "no"))
        for number, info in sorted((hw.get("social_channels") or {}).items(), key=lambda kv: int(kv[0])):
            state = "disponibile" if info["usable"] else ("con restrizioni" if info["listed"] else "non offerto")
            rows.append((f"Canale sociale {number} ({info['band']})", state))
        for kind, text in [(l["kind"], l["text"]) for l in hw.get("limitations", [])]:
            rows.append((f"Limite ({kind})", text))
    return rows


def hardware_summary(hw):
    if not hw:
        return VERDICTS["unknown"]
    return VERDICTS.get(hw.get("verdict"), VERDICTS["unknown"])


def peer_rows(status):
    """(title, subtitle) per peer, newest first."""
    out = []
    for peer in (status or {}).get("peers", []):
        title = peer.get("name") or peer["addr"]
        airdrop = "AirDrop annunciato" if peer.get("airdrop") == "advertised" else "AirDrop non rilevato"
        lost = peer.get("state") == "lost"
        parts = [{"synchronised": "sincronizzato", "heard": "udito", "lost": "perso"}.get(peer.get("state"), peer.get("state", "")),
                 airdrop]
        if peer.get("rssi") is not None:
            parts.append(f"{peer['rssi']} dBm")
        parts.append("visto " + ago(peer.get("age_s")))
        if peer.get("sequence"):
            parts.append("canali " + peer["sequence"])
        out.append((title if not lost else title + " (perso)", " · ".join(parts), peer["addr"]))
    return out


def diagnostics_text(status):
    """A compact, copyable text report of the engine (no personal data besides the peers' AWDL addresses)."""
    if not status:
        return "Motore NexaAWDL non attivo."
    lines = [f"Stato: {STATE_LABELS.get(status['state'], status['state'])} ({status.get('reason', '')})",
             f"Modalità: {status.get('mode')} · interfaccia {status.get('interface')} · riavvii {status.get('restarts', 0)}"]
    stats = status.get("stats") or {}
    lines.append("Frame: " + ", ".join(f"{k} {v}" for k, v in sorted(stats.items())))
    traffic = status.get("traffic") or {}
    if traffic:
        lines.append("Traffico IPv6: " + ", ".join(f"{k} {v}" for k, v in sorted(traffic.items())))
    for reason, count in (status.get("malformed") or {}).items():
        lines.append(f"Frame non validi ({count}): {reason}")
    for error in status.get("errors", []):
        lines.append(f"Errore [{error.get('kind')}]: {error.get('message')}")
    for event in status.get("events", [])[-15:]:
        extra = {k: v for k, v in event.items() if k not in ("t", "level", "kind")}
        lines.append(f"{event.get('level', 'info'):7} {event.get('kind')} {json.dumps(extra, ensure_ascii=False)}")
    return "\n".join(lines)
