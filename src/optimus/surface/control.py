"""The reverse channel: change a run's mind while it is running.

The loop already had exactly one input — the instruction it started with — and
exactly one control, a `threading.Event` that only the Harbor adapter ever set.
That is enough to *stop* a benchmark trial and nothing else. A person watching a
run wants the other three things: to add a fact the agent does not have, to
correct a plan before it is acted on, and to stop it now.

Those are not the same urgency, so they do not share a queue position. A
correction that arrives during turn 12 should be read at turn 13; a note can
wait for a convenient moment; a cancel should not wait at all. Hence a priority
queue rather than a list, with FIFO order preserved *within* a priority so that
two corrections arrive in the order they were typed.

Cancellation deliberately keeps a plain `threading.Event` at the bottom, exposed
as `Control.stop`, so that `AgentLoop(stop=control.stop)` works and the Harbor
adapter needs no change at all. The adapter sets the Event; the Event is what
the loop already checks; `Control` is a richer way to reach the same bit.
"""

from __future__ import annotations

import heapq
import itertools
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

__all__ = ["Confidence", "Control", "Steer", "SteerKind"]


class SteerKind(IntEnum):
    """Why this arrived, and therefore how soon it is read.

    The value *is* the priority: lower is sooner. Writing it this way means
    there is one number, not a kind and a priority that can disagree.
    """

    #: Stop the run. Read immediately, mid-turn, not at a boundary.
    CANCEL = 0
    #: Take effect before the next model call, ahead of anything queued.
    INTERRUPT = 1
    #: A correction. Delivered at the next turn boundary.
    GUIDANCE = 2
    #: A fact the agent may want. Delivered when there is room.
    NOTE = 3


@dataclass(frozen=True, slots=True)
class Steer:
    """One instruction that arrived from outside the loop."""

    kind: SteerKind
    text: str
    #: Who sent it — "tui", "acp", "http", "cli". Recorded, because an
    #: instruction that changed a trajectory should name its author.
    source: str = "unknown"
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)

    def as_message(self) -> dict[str, str]:
        """Render for the model's message list."""
        prefix = {
            SteerKind.CANCEL: "The operator has stopped this run",
            SteerKind.INTERRUPT: "The operator is interrupting",
            SteerKind.GUIDANCE: "The operator has a correction",
            SteerKind.NOTE: "The operator adds a note",
        }[self.kind]
        return {"role": "user", "content": f"<steer source={self.source!r}>{prefix}: {self.text}</steer>"}

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.name.lower(),
            "text": self.text,
            "source": self.source,
            "ts": self.ts,
            "meta": self.meta,
        }


@dataclass(frozen=True, slots=True)
class Confidence:
    """What the agent thinks of its own footing, and who said so.

    Deliberately carries `source`. A model's self-report and a harness's
    measurement are different claims with different reliability, and a surface
    that renders them identically is asserting something neither of them said.
    """

    value: float
    source: str = "model"
    reason: str = ""
    ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not 0.0 <= self.value <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {self.value}")


class Control:
    """Steering, interruption and cancellation for a run in flight.

    Thread-safe. Every method may be called from a surface's thread while the
    loop runs on its own.
    """

    def __init__(self, *, run_id: str = ""):
        self.run_id = run_id
        #: The bit the loop already checks. Kept as a bare Event so that
        #: existing callers, and the Harbor adapter, need no change.
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self._heap: list[tuple[int, int, Steer]] = []
        self._tie = itertools.count()
        self._confidence: Confidence | None = None
        self._cancel_reason = ""
        #: Every steer that was ever accepted, in arrival order, for the receipt.
        self._history: list[Steer] = []

    # -- inbound --------------------------------------------------------------

    def send(self, steer: Steer) -> None:
        """Queue a steer. A CANCEL also trips the stop bit immediately."""
        with self._lock:
            heapq.heappush(self._heap, (int(steer.kind), next(self._tie), steer))
            self._history.append(steer)
        if steer.kind is SteerKind.CANCEL:
            self._cancel_reason = steer.text or "operator cancelled"
            self.stop.set()

    def send_kind(
        self, kind: SteerKind, text: str, *, source: str = "unknown", **meta: Any
    ) -> Steer:
        """Send a steer of a kind chosen at runtime.

        The named methods below are the ergonomic form. This is the form a
        surface needs when the kind arrived as a string over a socket, and it
        keeps that surface from growing a branch per kind that has to be
        extended every time this enum does.
        """
        steer = Steer(kind, text, source=source, meta=meta)
        self.send(steer)
        return steer

    def enqueue(self, text: str, *, source: str = "unknown", **meta: Any) -> Steer:
        """Add a note, read when there is room."""
        return self.send_kind(SteerKind.NOTE, text, source=source, **meta)

    def guide(self, text: str, *, source: str = "unknown", **meta: Any) -> Steer:
        """Correct the plan, read at the next turn boundary."""
        return self.send_kind(SteerKind.GUIDANCE, text, source=source, **meta)

    def interrupt(self, text: str, *, source: str = "unknown", **meta: Any) -> Steer:
        """Cut ahead of the queue; read before the next model call."""
        return self.send_kind(SteerKind.INTERRUPT, text, source=source, **meta)

    def cancel(self, reason: str = "", *, source: str = "unknown") -> Steer:
        """Stop the run. Takes effect mid-turn, not at a boundary."""
        return self.send_kind(
            SteerKind.CANCEL, reason or "cancelled", source=source
        )

    # -- outbound (the loop reads these) --------------------------------------

    def drain(self, *, limit: int = 8) -> list[Steer]:
        """Take everything queued, highest priority first.

        Called by the loop at a turn boundary. `limit` bounds how much text one
        turn can be handed, because an operator holding a key down should not be
        able to blow the context budget.
        """
        taken: list[Steer] = []
        with self._lock:
            while self._heap and len(taken) < limit:
                taken.append(heapq.heappop(self._heap)[2])
        return taken

    def peek(self) -> list[Steer]:
        """What is queued, without consuming it."""
        with self._lock:
            return [item[2] for item in sorted(self._heap)]

    @property
    def pending(self) -> int:
        with self._lock:
            return len(self._heap)

    @property
    def cancelled(self) -> bool:
        return self.stop.is_set()

    @property
    def cancel_reason(self) -> str:
        return self._cancel_reason

    @property
    def history(self) -> tuple[Steer, ...]:
        with self._lock:
            return tuple(self._history)

    # -- confidence -----------------------------------------------------------

    def signal(self, value: float, *, source: str = "model", reason: str = "") -> Confidence:
        conf = Confidence(value, source=source, reason=reason)
        with self._lock:
            self._confidence = conf
        return conf

    @property
    def confidence(self) -> Confidence | None:
        with self._lock:
            return self._confidence
