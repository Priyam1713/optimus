"""Venues: where an authorised command actually runs.

`audit.md` §2.12 catalogued how Bellona got this wrong, and each mistake has a
counterpart here:

* Its ladder had four rungs and one implementation — and `validate_policy`
  refused that one rung for any policy needing writes or network, so a policy
  that needed anything had no venue at all. Here `LocalVenue` can honestly serve
  write-and-network work, and the fail-closed check refuses only what is
  genuinely unenforceable.
* Its timeout dropped the future and left the child running, because tokio's
  `Command` does not kill on drop. Here the process tree is killed and reaped.
* Its output truncation sliced a string mid-character. Here truncation is
  character-safe.
* Its Job Object was created and never had a process assigned to it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol

from ..gate.capability import ArgvCapability


class Isolation(IntEnum):
    """What a venue can honestly enforce. Ordered weakest to strongest."""

    NONE = 0        # same filesystem, same network, scrubbed environment only
    PROCESS = 1     # + killable process tree, resource ceilings
    CONTAINER = 2   # + own filesystem and network namespace
    MACHINE = 3     # + own kernel


@dataclass(frozen=True, slots=True)
class VenueRequest:
    """What the caller needs, as distinct from what a venue offers."""

    timeout_s: float = 60.0
    max_output_bytes: int = 64_000
    allow_network: bool = True
    writable: bool = True
    min_isolation: Isolation = Isolation.PROCESS


@dataclass(slots=True)
class VenueResult:
    exit_code: int
    stdout: str
    stderr: str
    venue: str
    isolation: Isolation
    timed_out: bool = False
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class VenueUnavailable(Exception):
    """The venue cannot honestly serve this request. Always a refusal.

    Never a downgrade: an agent that asked for container isolation and silently
    got a bare subprocess has been lied to about the walls around it.
    """


class Venue(Protocol):
    name: str

    def available(self) -> bool: ...
    def isolation(self) -> Isolation: ...
    def run(self, cap: ArgvCapability, request: VenueRequest) -> VenueResult: ...


#: Environment variables a child may inherit. Exact names, not prefixes:
#: Bellona matched by prefix, so `HOME` also admitted `HOMEDRIVE`/`HOMEPATH` and
#: `PATH` admitted anything starting with those four letters.
ENV_ALLOW: frozenset[str] = frozenset({
    "PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "TEMP", "TMP",
    "HOME", "USERPROFILE", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ",
    "PATHEXT", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
})


def scrub_env(source: dict[str, str] | None = None, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Exactly the allow-list, plus whatever the caller adds deliberately."""
    src = dict(os.environ if source is None else source)
    out = {k: v for k, v in src.items() if k in ENV_ALLOW}
    if extra:
        out.update(extra)
    return out


def truncate(text: str, limit: int) -> str:
    """Character-safe truncation.

    Bellona sliced bytes at a fixed offset, which panics when the boundary lands
    mid-character — and `from_utf8_lossy` output routinely has multi-byte
    characters. Python would not panic, but it would still cut a grapheme in
    half and corrupt the tail of every large non-ASCII output.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated, {len(text) - limit} more characters]"


def choose(venues: list[Venue], request: VenueRequest) -> Venue:
    """Weakest venue that honestly satisfies the request.

    Weakest rather than strongest on purpose: isolation costs startup time, and
    a system that always reaches for the heaviest option teaches its users to
    turn it off.
    """
    candidates = [
        v for v in venues if v.available() and v.isolation() >= request.min_isolation
    ]
    if not candidates:
        offered = ", ".join(f"{v.name}={v.isolation().name}" for v in venues if v.available())
        raise VenueUnavailable(
            f"no venue provides {request.min_isolation.name} isolation "
            f"(available: {offered or 'none'})"
        )
    return min(candidates, key=lambda v: v.isolation())
