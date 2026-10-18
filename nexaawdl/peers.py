"""The peer table: who is on the air, what they announced and how fresh it is.

An AWDL peer (a device whose synchronisation frames we hear) is not the same as an AirDrop target: a peer
only becomes `airdrop = "advertised"` once mDNS shows it announcing `_airdrop._tcp`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .election import Candidate
from .frames import ElectionParams, ElectionParamsV2, mac_str
from .sync import AwClock, ClockOffset

PEER_TIMEOUT_US = 20_000_000       # no frame for 20 s: gone
MAX_PEERS = 64


@dataclass
class Peer:
    addr: bytes
    first_seen: int
    last_seen: int
    frames: int = 0
    action_frames: int = 0
    data_frames: int = 0
    rssi: int | None = None
    name: str | None = None
    version: int | None = None
    device_class: int | None = None
    chanseq: object = None                # the channel-sequence TLV (18): primary channels, as in OWL's log
    sync_chanseq: object = None           # the sequence inside the sync parameters: 40 MHz centre channels
    chanseq_changes: int = 0
    candidate: Candidate = None
    clock: AwClock = field(default_factory=AwClock)
    offset: ClockOffset = field(default_factory=ClockOffset)
    airdrop: str = "unknown"              # 'unknown' | 'advertised'
    airdrop_seen: int | None = None
    country: str | None = None
    state: str = "heard"                  # 'heard' | 'synchronised' | 'lost'

    def key(self):
        return mac_str(self.addr)

    def summary(self, now_us):
        return {
            "addr": self.key(), "name": self.name, "state": self.state, "airdrop": self.airdrop,
            "rssi": self.rssi, "frames": self.frames, "action_frames": self.action_frames,
            "data_frames": self.data_frames, "age_s": round((now_us - self.last_seen) / 1e6, 1),
            "sequence": self.chanseq.text() if self.chanseq else None,
            "sync_sequence": self.sync_chanseq.text() if self.sync_chanseq else None,
            "sequence_changes": self.chanseq_changes, "version": self.version, "device_class": self.device_class,
            "country": self.country, "sync": self.clock.quality() if self.clock.synchronised else None,
            "master": mac_str(self.candidate.master) if self.candidate else None,
        }


class PeerTable:
    def __init__(self, timeout_us=PEER_TIMEOUT_US, limit=MAX_PEERS, on_event=None):
        self.timeout_us = timeout_us
        self.limit = limit
        self.peers = {}
        self.on_event = on_event or (lambda kind, **info: None)
        self.dropped = 0

    def touch(self, addr, now_us, rssi=None):
        """Find or create the peer for `addr`; None when the table is full (the frame is still counted)."""
        peer = self.peers.get(addr)
        if peer is None:
            if len(self.peers) >= self.limit:
                self.expire(now_us)
            if len(self.peers) >= self.limit:
                self.dropped += 1
                return None
            peer = self.peers[addr] = Peer(addr, now_us, now_us)
            self.on_event("peer_new", peer=peer.key())
        elif peer.state == "lost":
            peer.state = "heard"
            self.on_event("peer_back", peer=peer.key())
        peer.last_seen = now_us
        peer.frames += 1
        if rssi is not None:
            peer.rssi = rssi
        return peer

    def update_election(self, peer, tlv):
        if isinstance(tlv, ElectionParamsV2):
            peer.candidate = Candidate(peer.addr, tlv.master_addr, tlv.sync_addr, tlv.master_metric,
                                       tlv.master_counter, tlv.self_metric, tlv.self_counter, tlv.distance)
        elif isinstance(tlv, ElectionParams):
            prior = peer.candidate
            counter = prior.master_counter if prior else 0
            peer.candidate = Candidate(peer.addr, tlv.top_master, prior.sync if prior else tlv.top_master,
                                       tlv.top_master_metric, counter, tlv.self_metric, 0, tlv.distance)

    def note_chanseq(self, peer, seq):
        if peer.chanseq is None or peer.chanseq.channels != seq.channels:
            if peer.chanseq is not None:
                peer.chanseq_changes += 1
            self.on_event("sequence", peer=peer.key(), sequence=seq.text())
        peer.chanseq = seq

    def mark_airdrop(self, addr, now_us):
        peer = self.peers.get(addr)
        if peer is not None and peer.airdrop != "advertised":
            peer.airdrop = "advertised"
            self.on_event("airdrop", peer=peer.key())
        if peer is not None:
            peer.airdrop_seen = now_us

    def expire(self, now_us):
        """Mark silent peers lost, and forget those that stayed lost for another timeout. Returns the lost keys."""
        lost = []
        for addr, peer in list(self.peers.items()):
            idle = now_us - peer.last_seen
            if peer.state != "lost" and idle > self.timeout_us:
                peer.state = "lost"
                lost.append(peer.key())
                self.on_event("peer_lost", peer=peer.key())
            elif peer.state == "lost" and idle > 2 * self.timeout_us:
                del self.peers[addr]
        return lost

    def active(self):
        return [p for p in self.peers.values() if p.state != "lost"]

    def snapshot(self, now_us):
        return [p.summary(now_us) for p in sorted(self.peers.values(), key=lambda p: -p.last_seen)]
