"""Typed, dependency-linked episodes.

`research.md` §4.5 is blunt about the state of the art: the two heuristics
everyone ships — compact when near the ceiling, compact on a fixed interval —
are both content-agnostic and measurably bad. Achilles did better than most with
a deterministic elision that counts what it dropped rather than summarising it
(`audit.md` §3.4), and its reasoning was right: counting "costs nothing and
cannot lie", where a summarising pass costs a generation and can invent a step
that never happened.

What it could not do was choose *well*, because its history was a flat list and
its eviction was positional: a load-bearing decision from turn 3 of a 30-turn run
was dropped with exactly the same indifference as a failed `ls`.

Two additions fix that:

* **Kind**, so eviction can be priority-ordered rather than positional.
* **`depends_on`**, so dropping something a survivor relies on is a detectable
  error rather than a silent one.

And one kind exists purely to close a gap the field has documented and nobody
addresses: `INVARIANT` is never evictable, so a safety constraint cannot be
compacted away (`arXiv:2606.22528`, "Governance Decay").
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Protocol

_ids = itertools.count(1)


class EpisodeKind(StrEnum):
    #: The task contract. What success means. Never evictable.
    CONTRACT = "contract"
    #: A safety constraint or standing instruction. Never evictable.
    INVARIANT = "invariant"
    #: Cheap structured priming of the environment — a file tree, a schema.
    #: Goose buys a 20-40x token advantage largely with this (`research.md` §2.1),
    #: so it is a first-class kind rather than an observation like any other.
    ENVIRONMENT = "environment"
    PLAN = "plan"
    THOUGHT = "thought"
    ACTION = "action"
    OBSERVATION = "observation"
    ERROR = "error"
    #: Produced by compaction. Carries the ids it stands in for.
    SUMMARY = "summary"


#: Retention weight. Eviction takes the lowest first, then the oldest.
#: Errors outrank successful observations on purpose: a failure carries more
#: information per token than a success, and re-discovering it costs a whole
#: turn.
SALIENCE: dict[EpisodeKind, int] = {
    EpisodeKind.THOUGHT: 0,
    EpisodeKind.OBSERVATION: 1,
    EpisodeKind.ACTION: 2,
    EpisodeKind.ENVIRONMENT: 3,
    EpisodeKind.ERROR: 4,
    EpisodeKind.SUMMARY: 5,
    EpisodeKind.PLAN: 6,
    EpisodeKind.CONTRACT: 99,
    EpisodeKind.INVARIANT: 99,
}

PERMANENT: frozenset[EpisodeKind] = frozenset({EpisodeKind.CONTRACT, EpisodeKind.INVARIANT})


class TokenCounter(Protocol):
    def __call__(self, text: str) -> int: ...


def heuristic_tokens(text: str) -> int:
    """~4 characters per token.

    A documented heuristic, good enough for budgeting and never for billing —
    the same call Bellona made, and the right one. Inject a real tokenizer per
    model where the arithmetic has to be exact; `ContextWindow` takes one.
    """
    return max(1, -(-len(text) // 4))


@dataclass(slots=True)
class Episode:
    kind: EpisodeKind
    content: str
    seq: int = 0
    id: str = ""
    tokens: int = 0
    #: Ids this episode's meaning depends on. Dropping a dependency without
    #: leaving a summary that names it is a compaction bug, and `validate()`
    #: treats it as one.
    depends_on: frozenset[str] = field(default_factory=frozenset)
    #: For SUMMARY: the ids it stands in for.
    lineage: tuple[str, ...] = ()
    pinned: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"ep{next(_ids)}"

    @property
    def permanent(self) -> bool:
        return self.pinned or self.kind in PERMANENT

    @property
    def salience(self) -> int:
        return SALIENCE.get(self.kind, 0)

    def render(self) -> str:
        return f"[{self.kind}] {self.content}"
