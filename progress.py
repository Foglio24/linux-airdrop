"""Transfer progress computed only from byte counts that the transfer really reported.

`Meter` is fed with (bytes done, bytes total) taken from the receiver or the sender process, together
with the time they arrived. Nothing is estimated from a model: when the total is unknown (or the
counter overtakes it) there is no percentage and no time estimate, only the bytes and the speed.
"""
import time
from collections import deque

WINDOW = 6.0      # seconds of history the speed is averaged over
STALL = 3.0       # no data for this long counts as a stall (speed 0)
MIN_SPAN = 0.4    # shortest span a speed is computed over


class Meter:
    def __init__(self, total=None, clock=time.monotonic):
        self.clock = clock
        self.total = total if isinstance(total, int) and total > 0 else None
        self.done = 0
        self.started = None
        self.last_update = None
        self.samples = deque()
        self.overrun = False      # the counter went past the announced total: the total was not the wire size

    def update(self, done, total=None, now=None):
        if not isinstance(done, int) or done < 0:
            return
        now = self.clock() if now is None else now
        if isinstance(total, int) and total > 0 and not self.overrun:
            self.total = total
        if done < self.done:      # counters never go back; ignore a stale event
            return
        if self.started is None:
            self.started = now
        self.done = done
        self.last_update = now
        if self.total is not None and done > self.total:
            self.total, self.overrun = None, True
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > WINDOW:
            self.samples.popleft()

    @property
    def percent(self):
        """0..100, or None when the total is not known."""
        if self.total is None:
            return None
        return min(100.0, 100.0 * self.done / self.total)

    @property
    def fraction(self):
        percent = self.percent
        return None if percent is None else percent / 100.0

    def speed(self, now=None):
        """Bytes per second over the recent window, 0 after a stall, None before there is enough data."""
        now = self.clock() if now is None else now
        if len(self.samples) < 2:
            return None
        if self.last_update is not None and now - self.last_update > STALL:
            return 0.0
        first = self.samples[0]
        last = self.samples[-1]
        span = last[0] - first[0]
        if span < MIN_SPAN:
            return None
        return max(0.0, (last[1] - first[1]) / span)

    def eta(self, now=None):
        """Seconds left, only with a known total and a non-zero speed."""
        speed = self.speed(now)
        if self.total is None or not speed:
            return None
        return max(0.0, (self.total - self.done) / speed)

    def snapshot(self, now=None):
        return {"done": self.done, "total": self.total, "percent": self.percent,
                "speed": self.speed(now), "eta": self.eta(now), "overrun": self.overrun}
