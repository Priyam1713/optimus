"""Surfaces: everything that watches a run, or steers one.

M4. The loop was built to be driven by a benchmark harness, which wants exactly
one thing from it — a trajectory, once, at the end. Every other way of using an
agent wants the opposite: to see the turn that is happening now, and to change
its mind before the next one.

This package adds that without giving any surface a reference to the loop. One
primitive does the work:

- `events.Bus` — the loop publishes; anyone subscribes. Publication never
  blocks, and a subscriber that cannot keep up drops events *visibly*, because
  a stream that silently loses rows is worse than one that admits it.
- `control.Control` — the reverse channel. Enqueue an instruction, interrupt the
  current turn, or cancel the run, from anywhere, without the Harbor adapter.

Everything else here is a projection of those two:

| Module | What it is |
|---|---|
| `agui` | the bus rendered as AG-UI protocol events |
| `acp` | Agent Client Protocol over stdio, so Zed and JetBrains can drive this |
| `server` | REST for control, SSE for the stream |
| `tui` | the same stream, drawn on a terminal |
| `dryrun` | what a run *would* touch, decided by the Gate, executing nothing |

The core imports nothing outside the standard library, which is the same rule
the Gate and the ledger hold. A surface that needed a web framework would make
the whole harness need one.
"""

from optimus.surface.control import Confidence, Control, Steer, SteerKind
from optimus.surface.events import Bus, EventKind, RunEvent, Subscription

__all__ = [
    "Bus",
    "Confidence",
    "Control",
    "EventKind",
    "RunEvent",
    "Steer",
    "SteerKind",
    "Subscription",
]
