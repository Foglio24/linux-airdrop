"""NexaAWDL engine core: lifecycle state machine, frame dispatch, peer table, synchronisation metrics.

The engine is an *observer and analyst* of the AWDL air: it receives frames from a monitor interface,
decodes and validates them, keeps the peer table and per-peer window clocks, derives the synchronisation
quality, tells AWDL presence from AirDrop availability and publishes a status snapshot. It does not
transmit: sending frames and the IPv6 data path stay with OWL until a transmit path has been verified on
real hardware (see `capabilities()`).

Time: all times are microseconds, on two clocks that must not be mixed. The *hardware* clock is the
radio's own timestamp (TSFT) in each frame: it feeds the window model, which compares frames with each other.
The *capture* clock is the time the frame was read (wall time live, the file's time offline): it feeds
liveness, expiry and the state machine, which `tick()` also receives. A frame without a hardware timestamp
uses its capture time for both, and when no capture time is given the hardware time stands in (as in the
tests). The engine never reads the system clock itself, so it is testable.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass

from . import dot11, transport
from .frames import (ArpaTlv, ChanSeqTlv, DataPathState, ElectionParams, ElectionParamsV2, MalformedFrame,
                     SyncParams, VersionTlv, parse_action, parse_data)
from .peers import PeerTable
from .sync import SLOTS

STOPPED, STARTING, LISTENING, OBSERVING, SYNCHRONISED, DEGRADED, ERROR = (
    "stopped", "starting", "listening", "observing", "synchronised", "degraded", "error")
STATES = (STOPPED, STARTING, LISTENING, OBSERVING, SYNCHRONISED, DEGRADED, ERROR)
LEVELS = {"error": 0, "warning": 1, "info": 2, "debug": 3}


@dataclass
class EngineConfig:
    mode: str = "observe"
    log_level: str = "info"
    peer_timeout_s: float = 20.0
    metrics_interval_s: float = 5.0
    quiet_after_s: float = 10.0          # no AWDL frame for this long: back to `listening`
    sync_min_samples: int = 8
    sync_min_quality: float = 80.0       # percent of predictions within the threshold
    max_restarts: int = 3


def freq_to_channel(freq):
    if freq is None:
        return None
    if 2412 <= freq <= 2472:
        return (freq - 2407) // 5
    if freq == 2484:
        return 14
    if 5000 <= freq < 5900:
        return (freq - 5000) // 5
    return None


def capabilities():
    """What this engine does and does not do. `transmit` is False on purpose and says why."""
    return {
        "receive": True, "decode": True, "peer_tracking": True, "window_timing": True,
        "airdrop_detection": True, "transmit": False,
        "transmit_reason": ("transmitting AWDL frames and the IPv6 data path are left to OWL: injection from a "
                            "monitor interface has not been verified for this engine on real hardware"),
    }


class Engine:
    """Feed it frames with `feed()` and call `tick()` regularly; read `status()`."""

    def __init__(self, config=None, interface=None, emit=None, own=()):
        self.config = config or EngineConfig()
        self.interface = interface
        self.own = {bytes(a) for a in own}      # our own AWDL address(es): the monitor sees our injected frames too
        self.emit = emit or (lambda event: None)
        self.state = STOPPED
        self.state_since = 0
        self.now = 0
        self.started_at = None
        self.restarts = 0
        self.stats = Counter()
        self.traffic = Counter()
        self.malformed = Counter()
        self.channels = Counter()        # frames per channel actually seen on the monitor
        self.events = deque(maxlen=200)
        self.errors = deque(maxlen=50)
        self.table = PeerTable(int(self.config.peer_timeout_s * 1e6), on_event=self._peer_event)
        self.last_awdl = None
        self.last_metrics = None
        self.slot_hits = [0] * SLOTS
        self.window_checked = 0
        self.window_hit = 0
        self.reference = None            # address of the peer whose clock is the reference
        self.master = None               # address of the advertised master
        self.hardware = None
        self.reason = ""
        self._truncated = False

    # -------------------------------------------------------------------------------- lifecycle

    def start(self, now_us=0):
        if self.state not in (STOPPED, ERROR):
            return False
        self.now = now_us
        self.started_at = now_us
        self._set(STARTING, "start requested", now_us)
        self._set(LISTENING, "waiting for AWDL frames", now_us)
        return True

    def stop(self, reason="stop requested"):
        if self.state != STOPPED:
            self._set(STOPPED, reason, self.now)
        return True

    def restart(self, now_us=None):
        """Stop and start again, keeping nothing but the lifetime counters. Bounded by `max_restarts`."""
        self.restarts += 1
        if self.restarts > self.config.max_restarts:
            self._set(ERROR, f"gave up after {self.config.max_restarts} restarts", self.now)
            return False
        self.stop("restarting")
        self.table = PeerTable(int(self.config.peer_timeout_s * 1e6), on_event=self._peer_event)
        self.last_awdl = None
        self.reference = self.master = None
        return self.start(self.now if now_us is None else now_us)

    def fail(self, message, now_us=None):
        self.error("source", message)
        self._set(ERROR, message, self.now if now_us is None else now_us)

    def _set(self, state, reason, now_us):
        if state == self.state:
            return
        previous, self.state, self.state_since, self.reason = self.state, state, now_us, reason
        self.log("info", "state", **{"from": previous, "to": state, "reason": reason})

    # ------------------------------------------------------------------------------------ logging

    def log(self, level, kind, **info):
        if LEVELS.get(level, 2) > LEVELS.get(self.config.log_level, 2):
            return
        event = {"t": self.now, "level": level, "kind": kind, **info}
        self.events.append(event)
        self.emit(event)

    def error(self, kind, message):
        self.errors.append({"t": self.now, "kind": kind, "message": message})
        self.log("error", "error", source=kind, message=message)

    def _peer_event(self, kind, **info):
        self.log("info", kind, **info)

    # ------------------------------------------------------------------------------------ frames

    def feed(self, packet, now_us=None, truncated=False):
        """Process one captured frame (radiotap + 802.11). Never raises for bad input."""
        if self.state in (STOPPED, ERROR):
            return
        self.stats["frames"] += 1
        self._truncated = truncated
        if truncated:
            self.stats["truncated"] += 1
        try:
            meta, frame = dot11.split_capture(packet)
        except MalformedFrame as exc:
            self._bad("radiotap/802.11", exc)
            return
        if meta.bad_fcs:
            self.stats["bad_fcs"] += 1
            return
        t = meta.tsft if meta.tsft is not None else (now_us or 0)      # hardware clock
        wall = now_us if now_us is not None else t                      # capture clock
        channel = freq_to_channel(meta.freq)
        if channel:
            self.channels[channel] += 1
        self.now = max(self.now, wall)
        if frame.addr2 is not None and frame.addr2 in self.own:
            self.stats["own"] += 1               # our own transmission, read back by the monitor: not a peer
            return
        try:
            if frame.is_awdl_action:
                self._action(meta, frame, t, wall)
            elif frame.is_data and frame.addr3 == dot11.AWDL_BSSID:
                self._data(meta, frame, t, wall)
            elif frame.ftype == dot11.FTYPE_CTRL:
                self.stats["control"] += 1
            else:
                self.stats["other"] += 1
        except MalformedFrame as exc:
            self._bad("awdl", exc)
        except Exception as exc:                           # defensive: a frame must never stop the engine
            self.stats["internal_errors"] += 1
            self.error("internal", f"{type(exc).__name__}: {exc}")

    def _bad(self, where, exc):
        if self._truncated:                  # cut short by the capture's snapshot length: not the sender's fault
            self.stats["truncated_unreadable"] += 1
            return
        self.stats["malformed"] += 1
        self.malformed[f"{where}: {str(exc)[:60]}"] += 1
        self.log("debug", "malformed", where=where, reason=str(exc))

    def _action(self, meta, frame, t, wall):
        action = parse_action(frame.body)
        self.stats["psf" if action.kind == "PSF" else "mif"] += 1
        self.last_awdl = wall
        if self.state == LISTENING:
            self._set(OBSERVING, "AWDL frames heard", wall)
        peer = self.table.touch(frame.addr2, wall, meta.signal)
        if peer is None:
            return
        peer.action_frames += 1
        peer.offset.add(t, action.phy_tx)
        for tlv in action.tlvs:
            if isinstance(tlv, SyncParams):
                peer.clock.configure(tlv.aw_period, tlv.presence_mode)
                error = peer.clock.observe(t, tlv.tx_down_counter, tlv.next_aw_seq)
                peer.sync_chanseq = tlv.chanseq          # 40 MHz centre channels: kept apart from the TLV 18 list
                if abs(error) > 3:
                    self.log("debug", "sync_miss", peer=peer.key(), error_tu=error)
                if peer.state == "heard":
                    peer.state = "synchronised"
            elif isinstance(tlv, ChanSeqTlv):
                self.table.note_chanseq(peer, tlv.chanseq)
            elif isinstance(tlv, (ElectionParams, ElectionParamsV2)):
                self.table.update_election(peer, tlv)
            elif isinstance(tlv, VersionTlv):
                peer.version, peer.device_class = tlv.version, tlv.device_class
            elif isinstance(tlv, ArpaTlv):
                peer.name = tlv.name
            elif isinstance(tlv, DataPathState) and tlv.country:
                peer.country = tlv.country

    def _data(self, meta, frame, t, wall):
        self.stats["data"] += 1
        peer = self.table.touch(frame.addr2, wall, meta.signal)
        if peer is None:
            return
        peer.data_frames += 1
        try:
            header, payload = parse_data(frame.body)
        except MalformedFrame as exc:
            self._bad("awdl-data", exc)
            return
        self._check_window(peer, meta, t)
        if header.ethertype != 0x86DD:
            self.traffic["non-ipv6"] += 1
            return
        try:
            packet = transport.parse_ipv6(payload)
        except MalformedFrame as exc:
            self._bad("ipv6", exc)
            return
        label = transport.classify(packet)
        self.traffic[label] += 1
        if label == "mdns":
            try:
                mdns = transport.parse_mdns(transport.parse_udp(packet.payload).payload)
            except MalformedFrame:
                return
            if transport.announces_airdrop(mdns):
                source = transport.mac_from_link_local(packet.src)
                self.table.mark_airdrop(source if source in self.table.peers else frame.addr2, wall)

    def _check_window(self, peer, meta, t):
        """Does a frame from `peer` fall in a slot where its own sequence lists the channel it arrived on?

        This is the engine's falsifiable check of its window model: if the clock and the sequence are right,
        a peer only transmits on channels it advertises for the slot it is in."""
        channel = freq_to_channel(meta.freq)
        if not (peer.clock.synchronised and peer.chanseq and channel):
            return
        slot = peer.clock.window(t).slot
        self.slot_hits[slot] += 1
        self.window_checked += 1
        if peer.chanseq.channels[slot] == channel:
            self.window_hit += 1

    # -------------------------------------------------------------------------------------- timers

    def tick(self, now_us):
        """Run timers: expire peers, move the state machine, publish metrics. Safe to call at any rate."""
        self.now = max(self.now, now_us)
        if self.state in (STOPPED, ERROR):
            return
        self.table.expire(self.now)
        self._pick_reference()
        self._update_state()
        interval = self.config.metrics_interval_s * 1e6
        if self.last_metrics is None or self.now - self.last_metrics >= interval:
            self.last_metrics = self.now
            self.log("debug", "metrics", **self.metrics())

    def _pick_reference(self):
        masters = Counter(p.candidate.master for p in self.table.active() if p.candidate)
        self.master = masters.most_common(1)[0][0] if masters else None
        synced = [p for p in self.table.active() if p.clock.synchronised]
        pick = next((p for p in synced if p.addr == self.master), None)
        if pick is None and synced:
            pick = max(synced, key=lambda p: p.clock.samples)
        self.reference = pick.addr if pick else None

    def _update_state(self):
        if self.state in (STARTING, STOPPED, ERROR):
            return
        quiet = self.last_awdl is None or self.now - self.last_awdl > self.config.quiet_after_s * 1e6
        if quiet or not self.table.active():
            self._set(LISTENING, "no AWDL frames recently", self.now)
            return
        ref = self.table.peers.get(self.reference) if self.reference else None
        if ref is None or not ref.clock.synchronised:
            self._set(OBSERVING, "peers heard, no window clock yet", self.now)
            return
        q = ref.clock.quality()
        enough = q["samples"] >= self.config.sync_min_samples and q["in_sync"] is not None
        if enough and q["in_sync"] >= self.config.sync_min_quality:
            self._set(SYNCHRONISED, f"{q['in_sync']}% of predictions within 3 TU", self.now)
        elif enough:
            self._set(DEGRADED, f"only {q['in_sync']}% of predictions within 3 TU", self.now)
        else:
            self._set(OBSERVING, "collecting synchronisation samples", self.now)

    # --------------------------------------------------------------------------------- reporting

    def metrics(self):
        ref = self.table.peers.get(self.reference) if self.reference else None
        return {
            "frames": self.stats["frames"], "psf": self.stats["psf"], "mif": self.stats["mif"],
            "data": self.stats["data"], "malformed": self.stats["malformed"],
            "peers": len(self.table.active()), "sync": ref.clock.quality() if ref else None,
            "window_check": self.window_check(),
        }

    def window_check(self):
        if not self.window_checked:
            return {"checked": 0, "consistent_pct": None, "slots": list(self.slot_hits)}
        return {"checked": self.window_checked, "consistent_pct": round(100.0 * self.window_hit / self.window_checked, 1),
                "slots": list(self.slot_hits)}

    def status(self, now_us=None):
        now = self.now if now_us is None else now_us
        ref = self.table.peers.get(self.reference) if self.reference else None
        return {
            "state": self.state, "reason": self.reason, "mode": self.config.mode, "interface": self.interface,
            "uptime_s": round((now - self.started_at) / 1e6, 1) if self.started_at is not None and self.state != STOPPED else 0,
            "restarts": self.restarts, "capabilities": capabilities(), "hardware": self.hardware,
            "sync": {"reference": ref.key() if ref else None,
                     "master": ":".join(f"{b:02x}" for b in self.master) if self.master else None,
                     "quality": ref.clock.quality() if ref else None,
                     "offset_spread_us": ref.offset.spread_us() if ref else None,
                     "window_check": self.window_check()},
            "wireless": {"channel": self.channels.most_common(1)[0][0] if self.channels else None,
                         "channels_seen": dict(self.channels), "interface_mode": "monitor",
                         "role": "passive observer"},
            "peers": self.table.snapshot(now), "stats": dict(self.stats), "traffic": dict(self.traffic),
            "malformed": dict(self.malformed.most_common(10)), "table_dropped": self.table.dropped,
            "errors": list(self.errors)[-10:], "events": list(self.events)[-40:],
        }
