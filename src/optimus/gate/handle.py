"""The capability handle.

Invariant 1 (`apex.md` §3): the Gate returns a *handle*, not a verdict, and
executors accept nothing else.

Why this shape rather than a boolean plus an argument dict — which is what
Bellona did (`audit.md` §2.4): its gate authorised `file://workspace/../../x`,
its ledger recorded `file://workspace`, and its executor received the model's raw
arguments and wrote wherever they pointed. Authorization and execution were two
independent facts joined by convention.

A handle carries the *resolved* target, is single-use, expires, and cannot be
constructed outside this module. An executor that takes only handles cannot act
on unauthorised input, so path traversal and target substitution stop being
classes of bug rather than being defended against case by case.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .targets import ResolvedTarget
from .types import CapabilityRequest

#: Module-private construction token. `Handle(...)` from anywhere else raises.
_MINT = object()


class HandleError(Exception):
    pass


@dataclass(slots=True)
class Compensation:
    """The inverse of an action, recorded to the Ledger *before* the action runs.

    Writing the undo first is the difference between a system that can be undone
    and one that merely intends to be.
    """

    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Handle:
    handle_id: str
    request: CapabilityRequest
    resolved: ResolvedTarget
    ledger_seq: int
    rule_id: str
    expires_at: float
    compensation: Compensation | None = None
    _token: Any = None
    _used: bool = False

    def __post_init__(self) -> None:
        if self._token is not _MINT:
            raise HandleError(
                "handles may only be issued by the Gate; construct one elsewhere "
                "and you have found a bypass, not a shortcut"
            )

    @property
    def spent(self) -> bool:
        return self._used

    def consume(self) -> ResolvedTarget:
        """Redeem the handle for its resolved target. Exactly once."""
        if self._used:
            raise HandleError(f"handle {self.handle_id} was already used")
        if time.time() >= self.expires_at:
            raise HandleError(f"handle {self.handle_id} expired")
        self._used = True
        return self.resolved


def _issue(
    request: CapabilityRequest,
    resolved: ResolvedTarget,
    ledger_seq: int,
    rule_id: str,
    *,
    ttl_seconds: float = 120.0,
    compensation: Compensation | None = None,
) -> Handle:
    return Handle(
        handle_id="hdl_" + secrets.token_hex(8),
        request=request,
        resolved=resolved,
        ledger_seq=ledger_seq,
        rule_id=rule_id,
        expires_at=time.time() + ttl_seconds,
        compensation=compensation,
        _token=_MINT,
    )
