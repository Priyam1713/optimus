"""The first real tool plane — every tool behind the Gate, none beside it.

Two rules this module exists to hold:

* **A tool never receives the caller's string.** It receives a capability
  (`gate/capability.py`) whose target was resolved and whose identity is
  re-checked at the moment of use. Adding a tool therefore cannot create a new
  authority path, which is the property Achilles named and kept
  (`audit.md` §3.7) and Bellona lost by handing executors raw params.
* **A denial is an observation.** It comes back as data the agent can reason
  about and adapt to, not an exception that ends the run.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..gate.capability import ArgvCapability, CapabilityViolation, FileCapability, capability_for
from ..gate.gate import Gate
from ..gate.types import CapabilityRequest, Reversibility, Verb
from ..ledger.events import Meter, TrustLabel
from ..venues.base import Isolation, Venue, VenueRequest, VenueUnavailable, choose
from ..venues.local import LocalVenue

MAX_READ_CHARS = 20_000


def _denied(outcome: Any) -> dict[str, Any]:
    return {
        "error": f"denied by {outcome.rule_id}: {outcome.reason}",
        "denied": True,
        "rule": outcome.rule_id,
        "verdict": str(outcome.verdict),
        "ticket": outcome.ticket.ticket_id if outcome.ticket else None,
    }


@dataclass
class GatedTools:
    """A small, honest tool surface. Everything here goes through `gate`."""

    gate: Gate
    actor: str = "agent"
    #: The model wrote the arguments. Saying otherwise is how the untrusted
    #: gate stops meaning anything.
    trust: TrustLabel = TrustLabel.UNTRUSTED_MODEL_OUTPUT
    venues: Sequence[Venue] | None = None

    def _venues(self) -> list[Venue]:
        return list(self.venues) if self.venues is not None else [LocalVenue()]

    def _submit(self, verb: Verb, tool: str, spec: Any, rev: Reversibility, intent: str = ""):
        return self.gate.submit(CapabilityRequest(
            actor=self.actor, verb=verb, trust=self.trust, reversibility=rev,
            tool=tool, target_spec=spec, intent=intent,
        ))

    # -- files ----------------------------------------------------------------

    def read_file(self, path: str, *, offset: int = 0, limit: int = 400) -> dict[str, Any]:
        out = self._submit(Verb.READ, "read_file", path, Reversibility.OVERLAY)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        try:
            cap = capability_for(out.handle.consume())
            assert isinstance(cap, FileCapability)
            text = cap.read_text()
            lines = text.splitlines()
            window = lines[offset: offset + limit]
            result = {
                "path": os.path.relpath(cap.path, cap.target.workspace).replace("\\", "/"),
                "total_lines": len(lines),
                "offset": offset,
                "returned": len(window),
                "content": "\n".join(window)[:MAX_READ_CHARS],
            }
            ok = True
        except (OSError, CapabilityViolation) as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "read_file"})
        return result

    def write_file(self, path: str, content: str) -> dict[str, Any]:
        out = self._submit(Verb.WRITE, "write_file", path, Reversibility.COMPENSATION)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        try:
            cap = capability_for(out.handle.consume())
            assert isinstance(cap, FileCapability)
            cap.ensure_parent()
            written = cap.write_text(content)
            result = {"written": True, "bytes": written,
                      "undo": out.handle.compensation.kind if out.handle.compensation else None}
            ok = True
        except (OSError, CapabilityViolation) as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "write_file"})
        return result

    def delete_file(self, path: str) -> dict[str, Any]:
        out = self._submit(Verb.DELETE, "delete_file", path, Reversibility.COMPENSATION)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        try:
            cap = capability_for(out.handle.consume())
            assert isinstance(cap, FileCapability)
            cap.unlink()
            result, ok = {"deleted": True}, True
        except (OSError, CapabilityViolation) as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "delete_file"})
        return result

    # -- execution ------------------------------------------------------------

    def run_command(
        self,
        argv: Sequence[str],
        *,
        timeout_s: float = 60.0,
        min_isolation: Isolation = Isolation.PROCESS,
        allow_network: bool = True,
    ) -> dict[str, Any]:
        out = self._submit(Verb.EXECUTE, "run_command", list(argv), Reversibility.SNAPSHOT)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        request = VenueRequest(timeout_s=timeout_s, min_isolation=min_isolation,
                               allow_network=allow_network)
        try:
            cap = capability_for(out.handle.consume())
            assert isinstance(cap, ArgvCapability)
            venue = choose(self._venues(), request)
            r = venue.run(cap, request)
            result = {
                "exit_code": r.exit_code, "stdout": r.stdout, "stderr": r.stderr,
                "venue": r.venue, "isolation": r.isolation.name,
                "timed_out": r.timed_out, "duration_ms": r.duration_ms,
            }
            ok = r.ok
        except VenueUnavailable as exc:
            # Refuse rather than downgrade: an agent told it had container
            # isolation, running in a bare subprocess, has been lied to.
            result, ok = {"error": str(exc), "denied": True, "rule": "__isolation_unavailable__"}, False
        except (OSError, CapabilityViolation) as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "run_command"})
        return result

    # -- listing --------------------------------------------------------------

    def list_dir(self, path: str = ".", *, limit: int = 500) -> dict[str, Any]:
        out = self._submit(Verb.READ, "list_dir", path, Reversibility.OVERLAY)
        if not out.allowed or out.handle is None:
            return _denied(out)
        started = time.monotonic()
        try:
            target = out.handle.consume()
            root = target.path  # type: ignore[attr-defined]
            entries: list[str] = []
            for name in sorted(os.listdir(root))[:limit]:
                full = os.path.join(root, name)
                entries.append(name + ("/" if os.path.isdir(full) else ""))
            result, ok = {"path": path, "entries": entries, "count": len(entries)}, True
        except OSError as exc:
            result, ok = {"error": f"{type(exc).__name__}: {exc}"}, False
        self.gate.settle(out.handle, ok=ok, meter=Meter(wall_ms=_ms(started)),
                         detail={"tool": "list_dir"})
        return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
