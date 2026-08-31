"""Recording and applying inverses.

Invariant 4 (`apex.md` §3): reversibility is a declared type, and the inverse is
written to the Ledger *before* the act. This is the module that makes that more
than a promise — capturing an inverse means reading the prior state at
authorisation time, which is the only moment it is still true.

Achilles's `DiffSandbox` is the better answer where it applies: staging changes
outside the workspace means nothing needs undoing, and the human approves an
actual diff (`audit.md` §3.7). But a sandbox covers file mutation and nothing
else — its own docstring says so. Compensation covers what a sandbox cannot:
deletions, and later registry writes, process starts and app state.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ..gate.handle import Compensation
from ..gate.targets import FsTarget, ResolvedTarget
from ..gate.types import Verb
from ..ledger.events import Event, TrustLabel
from .blobs import BlobStore


@dataclass
class UndoReport:
    applied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def render(self) -> str:
        return (
            f"undone={len(self.applied)} skipped={len(self.skipped)} failed={len(self.failed)}"
        )


class Compensator:
    """Captures inverses before an act, and replays them backwards after."""

    def __init__(self, blobs: BlobStore):
        self.blobs = blobs

    # -- capture --------------------------------------------------------------

    def capture(self, verb: Verb, target: ResolvedTarget) -> Compensation | None:
        """Read the prior state while it is still the prior state."""
        if not isinstance(target, FsTarget):
            # Other target kinds get an inverse when their planes land. Returning
            # None means the Gate records no compensation, which is honest: a
            # compensation row that cannot be applied is worse than none.
            return None

        if verb in (Verb.WRITE, Verb.DELETE):
            if target.exists:
                try:
                    with open(target.path, "rb") as fh:
                        digest = self.blobs.put(fh.read())
                except OSError:
                    return None
                return Compensation(
                    kind="undo.restore",
                    payload={"path": target.path, "blob": digest,
                             "workspace": target.workspace},
                )
            return Compensation(
                kind="undo.remove",
                payload={"path": target.path, "workspace": target.workspace},
            )
        return None

    # -- apply ----------------------------------------------------------------

    def apply(self, payload: dict[str, Any]) -> str:
        kind = payload.get("kind") or ""
        body = payload.get("payload") or {}
        path = body.get("path")
        if not path:
            raise ValueError("compensation has no path")

        match kind:
            case "undo.restore":
                digest = body["blob"]
                data = self.blobs.get(digest)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.optimus-undo"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                os.replace(tmp, path)
                return f"restored {path} ({len(data)} bytes)"
            case "undo.remove":
                if os.path.exists(path):
                    os.unlink(path)
                    return f"removed {path}"
                return f"{path} was already absent"
            case _:
                raise ValueError(f"no inverse for {kind!r}")

    # -- replay ---------------------------------------------------------------

    def undo(
        self,
        events: Sequence[Event],
        *,
        since_seq: int = 0,
        only_settled_ok: bool = True,
    ) -> UndoReport:
        """Replay inverses newest-first.

        Backwards matters: two writes to one file leave two compensations, and
        applying them oldest-first would restore the *middle* state. Newest-first
        walks back through them and lands on the original.

        `only_settled_ok` skips compensations for actions that never actually
        settled successfully — undoing something that did not happen is its own
        way of corrupting a workspace.
        """
        report = UndoReport()
        settled_ok = {
            e.payload.get("for_seq")
            for e in events
            if e.kind == "effect.settled" and e.payload.get("ok")
        }

        comps = [e for e in events if e.kind == "compensation.recorded" and e.seq >= since_seq]
        for ev in sorted(comps, key=lambda e: e.seq, reverse=True):
            for_seq = ev.payload.get("for_seq")
            if only_settled_ok and for_seq not in settled_ok:
                report.skipped.append(f"seq {ev.seq}: action never settled ok")
                continue
            try:
                report.applied.append(self.apply(ev.payload))
            except Exception as exc:
                report.failed.append(f"seq {ev.seq}: {type(exc).__name__}: {exc}")
        return report


def record_undo(chain: Any, report: UndoReport, *, run: str = "") -> Event:
    """Put the undo itself on the record.

    An undo is a mutation of the workspace like any other, and a ledger that
    shows the writes but not the reversal tells a false story about the final
    state.
    """
    return chain.append(
        "reversal.applied",
        {"run": run, "applied": report.applied, "skipped": report.skipped,
         "failed": report.failed},
        TrustLabel.TRUSTED_USER,
    )
