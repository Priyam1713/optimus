"""A venue that is somebody else's machine.

`LocalVenue` spawns a process here and reaps its tree. `RemoteVenue` hands an
argv to a callable that runs it *there* — a Harbor container, an SSH host, a
Firecracker VM — and reports back. Everything venue-shaped stays the same:
`choose()` still picks the weakest venue that honestly satisfies the request, and
still refuses rather than downgrading.

Two things this file is careful about, because they are the two ways a remote
venue tells a comfortable lie:

* **Isolation is declared by the caller who built the transport, and it is
  recorded.** The constructor takes `isolation` rather than assuming CONTAINER,
  because "we shelled into a box" and "we exec'd into a disposable container" are
  different walls and only the code that opened the connection knows which one it
  has. Passing a level the transport does not actually provide is a lie the
  ledger will faithfully record as yours.
* **A transport failure is not an exit code.** A command that never ran is
  reported as a venue failure, not as a process that exited non-zero, because the
  agent reasons differently about "your command failed" and "your command did not
  happen".
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..gate.remote import RemoteCapability
from .base import Isolation, VenueRequest, VenueResult, truncate


@dataclass(frozen=True, slots=True)
class RemoteExec:
    """What a transport returns. Deliberately smaller than `VenueResult`."""

    exit_code: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


class Transport(Protocol):
    """Run an argv over there. Synchronous by contract.

    Harbor's environment API is async; `adapters/harbor.py` bridges it onto this
    interface so the rest of Optimus stays synchronous rather than growing an
    async colour it has no other use for.
    """

    def __call__(
        self, argv: Sequence[str], *, cwd: str, timeout_s: float
    ) -> RemoteExec: ...


class TransportFailed(Exception):
    """The command did not run. Distinct from "it ran and failed"."""


class RemoteVenue:
    """Executes through a transport, at a declared isolation level."""

    def __init__(
        self,
        transport: Transport,
        *,
        name: str = "remote",
        isolation: Isolation = Isolation.CONTAINER,
        available: Callable[[], bool] | None = None,
    ):
        self.name = name
        self._transport = transport
        self._isolation = isolation
        self._available = available or (lambda: True)

    def available(self) -> bool:
        return bool(self._available())

    def isolation(self) -> Isolation:
        return self._isolation

    def run(self, cap: RemoteCapability, request: VenueRequest) -> VenueResult:
        cap.verify()
        started = time.monotonic()
        try:
            r = self._transport(
                cap.argv, cwd=cap.cwd, timeout_s=request.timeout_s
            )
        except Exception as exc:  # transport, not process
            raise TransportFailed(f"{type(exc).__name__}: {exc}") from exc
        duration = int((time.monotonic() - started) * 1000)

        limit = request.max_output_bytes
        stderr = truncate(r.stderr, limit)
        if r.timed_out:
            stderr += f"\n[venue] timed out after {request.timeout_s:g}s"
        return VenueResult(
            exit_code=r.exit_code,
            stdout=truncate(r.stdout, limit),
            stderr=stderr,
            venue=self.name,
            isolation=self._isolation,
            timed_out=r.timed_out,
            duration_ms=duration,
        )
