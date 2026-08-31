"""The run, as it happens.

The ledger already records everything this stream carries, and records it
durably, in order, signed. So this is not a second record — it is a *view*, and
the difference matters enough to state at the top:

**The ledger is lossless and the bus is not.** A subscriber that cannot keep up
loses events. That is a deliberate trade: a live terminal attached to a fast
loop must never be able to slow the loop down, and the alternative to dropping
is back-pressure that does exactly that. What the bus will not do is lose events
*quietly* — every event carries a monotonic `seq`, so a gap is arithmetic, and
every subscription counts its own drops. Anything that needs the whole truth
reads the ledger afterwards with `optimus why`.

Turn numbering comes from the loop and is not recomputed here. That sounds too
obvious to write down, and it is written down because the opposite already
happened once: two renderers disagreed about whether the environment probe was a
turn, and the same run reported 40 turns in one view and 41 in another. One
source, quoted, never re-derived.
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = ["Bus", "EventKind", "RunEvent", "Subscription"]


class EventKind(StrEnum):
    """What happened.

    Names match the ledger's row kinds wherever both record the same thing, so
    that a live view and a post-hoc `optimus why` can be read side by side
    without a translation table.
    """

    # -- kinds that are also ledger rows -------------------------------------
    # These five carry the ledger's own row name as their value, because the
    # loop mirrors them from the single point that writes the ledger. A surface
    # and `optimus why` therefore quote one string rather than two that have to
    # be kept in step by hand.
    RUN_STARTED = "run.started"
    #: One model call, with the provider's usage. Named for the ledger row, not
    #: for the reply, because the row is the thing the meter bills.
    MODEL_CALL = "model.call"
    #: Per-turn context accounting: estimate, bill, calibration, allowance.
    CONTEXT_TURN = "context.turn"
    CONTEXT_COMPACTED = "context.compacted"
    #: A loop breaker fired: no_action, looping, wall_clock, cost_ceiling, ...
    BREAKER = "loop.breaker"

    # -- kinds that exist only on the bus ------------------------------------
    # The ledger has no row for these: a turn boundary is not an effect, and a
    # tool call is recorded by the Gate under its own row when it settles.
    RUN_FINISHED = "run.finished"
    TURN_STARTED = "turn.started"
    TURN_FINISHED = "turn.finished"

    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"

    #: The Gate refused. This is an observation, not an exception.
    GATE_DENIED = "gate.denied"
    #: The Gate parked the action for a human. A surface that can ask should.
    GATE_PARKED = "gate.parked"
    #: A parked action was resolved, by assent or by refusal.
    GATE_RESOLVED = "gate.resolved"

    #: The provider failed. `payload["retryable"]` separates a bad key from a
    #: 503 — the loop treats those very differently and so should a surface.
    MODEL_ERROR = "model.error"

    #: Something steered the run from outside. Mirrored onto the bus so that
    #: every surface sees a steer that arrived through any other surface.
    STEERED = "steered"


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One thing that happened, addressed well enough to render alone."""

    seq: int
    run_id: str
    kind: EventKind
    turn: int = 0
    #: Wall clock, epoch seconds. Float because a turn can be shorter than one.
    ts: float = field(default_factory=time.time)
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "run_id": self.run_id,
            "kind": str(self.kind),
            "turn": self.turn,
            "ts": self.ts,
            "payload": self.payload,
        }


class Subscription:
    """One consumer's window onto the bus.

    Bounded. When the queue is full the *oldest* event is discarded rather than
    the newest, because every surface this exists for is showing someone what is
    happening now, and the stalest event is the cheapest thing to lose.
    """

    __slots__ = ("_bus", "_q", "closed", "dropped", "name")

    def __init__(self, bus: Bus, name: str, maxsize: int):
        self._bus = bus
        self._q: queue.Queue[RunEvent | None] = queue.Queue(maxsize=maxsize)
        self.name = name
        #: Events discarded because this consumer fell behind. Non-zero means
        #: the view is incomplete, and a surface should say so rather than
        #: present a gap-free-looking picture.
        self.dropped = 0
        self.closed = False

    def _offer(self, event: RunEvent) -> None:
        """Never blocks. Called on the loop's thread, so it must not."""
        try:
            self._q.put_nowait(event)
        except queue.Full:
            try:
                self._q.get_nowait()
                self.dropped += 1
            except queue.Empty:  # pragma: no cover - a drain raced us; fine
                pass
            try:
                self._q.put_nowait(event)
            except queue.Full:  # pragma: no cover - still full; drop the new one
                self.dropped += 1

    def get(self, timeout: float | None = None) -> RunEvent | None:
        """Next event, or `None` when the bus closes or the wait expires."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def __iter__(self) -> Iterator[RunEvent]:
        while True:
            event = self._q.get()
            if event is None:
                return
            yield event

    def _close_sentinel(self) -> None:
        """Deliver the end-of-stream marker, evicting to make room if needed.

        This must never be a `put_nowait` that can fail. A subscriber whose
        queue is full is exactly the one that fell behind, and dropping *its*
        sentinel leaves its consumer blocked on a `get()` that nothing will ever
        answer — a hung thread for the life of the process, on precisely the
        slow surface the drop policy exists to tolerate.
        """
        while True:
            try:
                self._q.put_nowait(None)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                    self.dropped += 1
                except queue.Empty:  # pragma: no cover - drained by the consumer
                    return

    def close(self) -> None:
        self._bus._drop(self)


class Bus:
    """Publish-subscribe, thread-safe, and never blocking on publish.

    The loop holds one of these and calls `publish` from its own thread. Any
    number of surfaces subscribe from any number of others.
    """

    def __init__(self, *, run_id: str = "", maxsize: int = 1_024):
        self.run_id = run_id
        self._maxsize = maxsize
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []
        #: Kept so that a surface attaching mid-run can be shown how it began
        #: rather than joining an unlabelled stream of turn numbers.
        self._replay: list[RunEvent] = []
        self._replay_kinds = {EventKind.RUN_STARTED}
        self._sinks: list[Callable[[RunEvent], None]] = []

    # -- publishing -----------------------------------------------------------

    def publish(
        self,
        kind: EventKind,
        *,
        turn: int = 0,
        run_id: str = "",
        payload: dict[str, Any] | None = None,
        **extra: Any,
    ) -> RunEvent:
        """Publish one event. Never blocks, and never raises on a subscriber.

        Payloads arrive two ways on purpose. `**extra` is the ergonomic form for
        a literal call site that knows its own field names. `payload=` is the
        form for a dict that came from somewhere else — a ledger row, say, whose
        keys are arbitrary and quite reasonably include `kind` and `turn`.
        Splatting one of those into this signature is a `TypeError` waiting for
        the first payload that happens to collide, and the loop's own breaker
        rows collide immediately.
        """
        body = dict(payload) if payload else {}
        body.update(extra)
        event = RunEvent(
            seq=next(self._seq),
            run_id=run_id or self.run_id,
            kind=kind,
            turn=turn,
            payload=body,
        )
        with self._lock:
            subs = tuple(self._subs)
            sinks = tuple(self._sinks)
            if kind in self._replay_kinds:
                self._replay.append(event)
        for sub in subs:
            sub._offer(event)
        for sink in sinks:
            # A sink is someone else's code on the loop's thread. It does not
            # get to end the run by raising, and it does not get to be the
            # reason a turn failed.
            with suppress(Exception):
                sink(event)
        return event

    def sink(self, fn: Callable[[RunEvent], None]) -> Callable[[], None]:
        """Attach a synchronous callback. Returns a function that detaches it.

        Unlike a subscription this is lossless, because it runs inline — which
        also means a slow sink slows the loop. Use it for cheap work (a counter,
        an append) and a subscription for anything that talks to a socket.
        """
        with self._lock:
            self._sinks.append(fn)

        def detach() -> None:
            with self._lock:
                if fn in self._sinks:
                    self._sinks.remove(fn)

        return detach

    # -- subscribing ----------------------------------------------------------

    def subscribe(self, name: str = "", *, replay: bool = True) -> Subscription:
        sub = Subscription(self, name or f"sub-{len(self._subs)}", self._maxsize)
        with self._lock:
            self._subs.append(sub)
            history = tuple(self._replay) if replay else ()
        for event in history:
            sub._offer(event)
        return sub

    @contextmanager
    def listen(self, name: str = "", *, replay: bool = True) -> Iterator[Subscription]:
        sub = self.subscribe(name, replay=replay)
        try:
            yield sub
        finally:
            sub.close()

    def _drop(self, sub: Subscription) -> None:
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)
        sub.closed = True
        sub._close_sentinel()

    def close(self) -> None:
        """End every subscription's iteration."""
        with self._lock:
            subs = tuple(self._subs)
            self._subs.clear()
        for sub in subs:
            sub.closed = True
            sub._close_sentinel()

    @property
    def subscribers(self) -> int:
        with self._lock:
            return len(self._subs)
