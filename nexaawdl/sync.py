"""AWDL time model: availability windows, extended windows and channel slots.

Definitions (all times are microseconds on the capturing radio's own clock, 1 TU = 1024 us):

* an availability window (AW) lasts `aw_period` TU (16);
* with presence mode *p* (4), *p* AWs make an extended AW (EAW, 64 TU);
* the 16 entries of a channel sequence map to 16 consecutive EAWs, so a full cycle lasts 16 x 64 TU = 1.05 s.

A synchronisation-parameters TLV says how many TU remain until the next EAW boundary and which AW
counter the current EAW starts at. From one observation the model knows where every window falls;
the error of the next observation against that prediction is the synchronisation quality.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

TU_US = 1024
SLOTS = 16
SYNC_ERROR_THRESHOLD_TU = 3        # a prediction off by more than this counts as a sync miss
AW_COUNTER_MODULUS = 1 << 16       # the announced AW counter is 16 bits wide and wraps


def _eaw_tu(aw_period, presence):
    return aw_period * presence


@dataclass(frozen=True)
class WindowInfo:
    aw_index: int          # running AW counter
    eaw_index: int         # running EAW counter (aw_index // presence)
    slot: int              # position in the 16-entry channel sequence
    to_next_aw_us: int
    to_next_eaw_us: int


class AwClock:
    """Tracks one device's window timing from its synchronisation parameters."""

    def __init__(self, aw_period=16, presence=4):
        self.aw_period = aw_period
        self.presence = presence
        self.epoch_us = None            # start of a known EAW
        self.epoch_aw = 0               # AW counter at that moment (multiple of `presence`)
        self.samples = 0
        self.misses = 0
        self.last_error_tu = 0
        self._errors = deque(maxlen=64)

    @property
    def synchronised(self):
        return self.epoch_us is not None

    def configure(self, aw_period, presence):
        """Adopt the period/presence announced by the peer; a change invalidates the old epoch."""
        if (aw_period, presence) != (self.aw_period, self.presence):
            self.aw_period, self.presence = aw_period, presence
            self.epoch_us = None

    def predict_error_tu(self, t_us, tx_down_counter, aw_seq):
        """How far (TU) an observation is from what the current model predicts. 0 when unsynchronised."""
        if self.epoch_us is None:
            return 0
        eaw = _eaw_tu(self.aw_period, self.presence)
        now = self.window(t_us)
        # the counters wrap at 16 bits: compare them modulo the number of EAWs in one wrap, signed
        modulus = AW_COUNTER_MODULUS // self.presence
        windows = ((aw_seq // self.presence) - now.eaw_index) % modulus
        if windows > modulus // 2:
            windows -= modulus
        return windows * eaw - (tx_down_counter - now.to_next_eaw_us // TU_US)

    def observe(self, t_us, tx_down_counter, aw_seq):
        """Feed one synchronisation observation; returns the prediction error in TU (0 for the first)."""
        error = self.predict_error_tu(t_us, tx_down_counter, aw_seq)
        self.samples += 1
        if self.epoch_us is not None:
            self.last_error_tu = error
            self._errors.append(error)
            if abs(error) > SYNC_ERROR_THRESHOLD_TU:
                self.misses += 1
        eaw_us = _eaw_tu(self.aw_period, self.presence) * TU_US
        self.epoch_us = t_us - (eaw_us - tx_down_counter * TU_US)
        self.epoch_aw = aw_seq - aw_seq % self.presence
        return error

    def window(self, t_us):
        if self.epoch_us is None:
            raise ValueError("clock not synchronised")
        eaw_us = _eaw_tu(self.aw_period, self.presence) * TU_US
        aw_us = self.aw_period * TU_US
        since = t_us - self.epoch_us
        eaw_n, into = divmod(since, eaw_us)
        aw_index = (self.epoch_aw + eaw_n * self.presence + into // aw_us) % AW_COUNTER_MODULUS
        eaw_index = aw_index // self.presence
        return WindowInfo(aw_index, eaw_index, eaw_index % SLOTS, aw_us - into % aw_us, eaw_us - into)

    def quality(self):
        """Share of predictions inside the threshold, mean absolute error (TU) and sample count."""
        if not self._errors:
            return {"samples": self.samples, "misses": self.misses, "mean_abs_error_tu": None, "in_sync": None}
        mean = sum(abs(e) for e in self._errors) / len(self._errors)
        return {"samples": self.samples, "misses": self.misses, "mean_abs_error_tu": round(mean, 2),
                "in_sync": round(100.0 * sum(abs(e) <= SYNC_ERROR_THRESHOLD_TU for e in self._errors)
                                 / len(self._errors), 1)}


class ClockOffset:
    """Relation between a peer's transmit clock (`phy_tx` of its action frames) and ours.

    Measured on a capture of this project (an iPhone, 6864 action frames over 729 s): `phy_tx - tsft` stayed
    within -0.57 ms .. +0.13 ms of its median (sigma 0.12 ms, 51 % within 50 us, 0.5 ppm drift), apart from one
    isolated 2^22 us jump. The two clocks tick at the same rate, and a median of recent differences is an
    offset far finer than an availability window (65 ms). `spread_us()` reports the spread actually seen.
    """

    def __init__(self, keep=32):
        self._diffs = deque(maxlen=keep)

    def add(self, local_tsft_us, peer_phy_tx_us):
        self._diffs.append((peer_phy_tx_us - local_tsft_us) & 0xFFFFFFFF)

    @property
    def offset(self):
        if not self._diffs:
            return None
        ordered = sorted(self._diffs)
        return ordered[len(ordered) // 2]

    def spread_us(self):
        """Distance between the smallest and largest recent difference (wraps handled by the 32-bit mask)."""
        if len(self._diffs) < 2:
            return None
        d = sorted(self._diffs)
        return d[-1] - d[0]


def peer_listens_on(chan_for_slot, slot, channel):
    """True when `chan_for_slot[slot]` is `channel` (a peer's sequence entry for that EAW)."""
    return bool(channel) and 0 <= slot < SLOTS and chan_for_slot[slot] == channel


def overlap(a, b):
    """Slots in which two channel sequences are on the same non-empty channel."""
    return [i for i in range(SLOTS) if a[i] and a[i] == b[i]]
