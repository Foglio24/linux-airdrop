"""Master / synchronisation-tree election.

Every node advertises the top master it follows, that master's metric and counter, and its own distance
(tree height) from it. A node follows the best candidate among its valid peers (itself included). Apple
devices rank candidates by metric first and counter second; the opposite order is available for peers
that follow the older rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field

MAX_TREE_HEIGHT = 10
METRIC_INIT = 60
COUNTER_INIT = 0


def _cmp(a, b):
    return (a > b) - (a < b)


@dataclass
class Candidate:
    """What a node says about its place in the tree."""
    addr: bytes                       # the node's own AWDL address
    master: bytes                     # the top master it follows
    sync: bytes                       # the node it synchronises to
    master_metric: int = METRIC_INIT
    master_counter: int = COUNTER_INIT
    self_metric: int = METRIC_INIT
    self_counter: int = COUNTER_INIT
    height: int = 0


@dataclass
class Election:
    me: Candidate
    metric_first: bool = True
    history: list = field(default_factory=list)

    @classmethod
    def new(cls, addr, metric_first=True):
        return cls(Candidate(addr, addr, addr), metric_first)

    def _rank(self, a, b):
        first = (_cmp(a.master_metric, b.master_metric), _cmp(a.master_counter, b.master_counter))
        return first[0] or first[1] if self.metric_first else first[1] or first[0]

    def run(self, peers):
        """Re-evaluate the tree from `peers` (iterable of Candidate); returns True when the master changed."""
        before = (self.me.master, self.me.sync)
        me = self.me
        me.master = me.sync = me.addr
        me.master_metric, me.master_counter, me.height = me.self_metric, me.self_counter, 0
        best = me
        for peer in peers:
            if peer.height + 1 > MAX_TREE_HEIGHT:
                continue                                  # the tree would get too deep
            if peer.sync == me.addr:
                continue                                  # it follows us: no cycles
            rank = self._rank(peer, best)
            if rank < 0:
                continue
            if rank == 0:
                if peer.height > best.height:
                    continue                              # do not lengthen the tree
                if peer.height == best.height and peer.addr <= best.addr:
                    continue                              # tie: the larger address wins
            best = peer
        if best is not me:
            me.master, me.sync = best.master, best.addr
            me.master_metric, me.master_counter, me.height = best.master_metric, best.master_counter, best.height + 1
        changed = (me.master, me.sync) != before
        if changed:
            self.history.append((me.master, me.sync))
            del self.history[:-16]
        return changed

    def is_master(self):
        return self.me.sync == self.me.addr

    def following(self):
        """The address of the node we synchronise to, or None when we are our own master."""
        return None if self.is_master() else self.me.sync
