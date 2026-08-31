"""The context window, its budget, and the compaction that has to prove itself.

This is the row [apex.md](../../../docs/apex.md) §4 names as the debt: adopting the
OpenHands SDK means inheriting roughly 840K tokens per solved task until its
condenser is replaced, against Goose's 28-37K at a pass rate within 0-8pp
(`research.md` §2.1). So the plane has three jobs, in order of how much they are
worth:

1. **Keep the context small** — eviction is priority-ordered and dependency-aware,
   not positional.
2. **Prove nothing load-bearing was lost** — every compaction is validated, and a
   compaction that would drop an invariant is *refused*, not shipped. No harness
   surveyed in `research.md` does this; the failure mode has a paper
   ("Governance Decay") and no implementation.
3. **Say exactly what it dropped** — deterministically, by counting rather than
   summarising, so the note cannot hallucinate a step that never happened.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from .episodes import Episode, EpisodeKind, TokenCounter, heuristic_tokens


class CompactionRefused(Exception):
    """A compaction that would have lost something load-bearing.

    Raised rather than logged. A context that quietly lost its safety constraints
    still runs, and that is precisely the failure this plane exists to prevent.
    """


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """How the window is allowed to be spent."""

    total: int = 128_000
    #: Held back for the model's reply; never fillable by history.
    reserve_output: int = 8_000
    #: Turns always kept whole, however low their salience.
    keep_recent: int = 6
    #: Compaction targets this fraction of the fillable budget, so it does not
    #: re-trigger on the very next turn.
    target_ratio: float = 0.6

    @property
    def fillable(self) -> int:
        return max(0, self.total - self.reserve_output)

    @property
    def target(self) -> int:
        return int(self.fillable * self.target_ratio)


@dataclass
class CompactionReport:
    """What happened, in numbers that cannot lie."""

    evicted: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    summary_id: str | None = None
    invariants_preserved: bool = True
    contract_preserved: bool = True
    dependencies_intact: bool = True
    within_budget: bool = True

    @property
    def tokens_saved(self) -> int:
        return self.tokens_before - self.tokens_after

    @property
    def valid(self) -> bool:
        return (
            self.invariants_preserved
            and self.contract_preserved
            and self.dependencies_intact
            and self.within_budget
        )

    def note(self) -> str:
        """The line the model sees in place of what was dropped.

        Achilles's insight, kept: counting the elided calls costs nothing and
        cannot invent a step. The lineage id makes it recoverable from the
        Ledger, which its version could not do.
        """
        if not self.evicted:
            return ""
        parts = ", ".join(f"{n} {k}" for k, n in sorted(self.by_kind.items()))
        return f"[compacted {self.evicted} episodes ({parts}); recoverable via {self.summary_id}]"


Summarizer = Callable[[Sequence[Episode]], str]


def counting_summarizer(evicted: Sequence[Episode]) -> str:
    """The default. Deterministic, free, and incapable of hallucinating."""
    counts = Counter(str(e.kind) for e in evicted)
    parts = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    errors = [e for e in evicted if e.kind is EpisodeKind.ERROR]
    tail = ""
    if errors:
        # Errors are the one thing worth carrying a fragment of: re-discovering a
        # failure costs a whole turn.
        tail = " | last error: " + errors[-1].content[:200]
    return f"{len(evicted)} earlier episodes ({parts}){tail}"


class ContextWindow:
    """Ordered episodes under a budget, with compaction that validates itself."""

    def __init__(
        self,
        budget: ContextBudget | None = None,
        *,
        count_tokens: TokenCounter = heuristic_tokens,
    ):
        self.budget = budget or ContextBudget()
        self._count = count_tokens
        self._episodes: list[Episode] = []
        self._next_seq = 0

    # -- construction ---------------------------------------------------------

    def add(self, episode: Episode) -> Episode:
        episode.seq = self._next_seq
        self._next_seq += 1
        if not episode.tokens:
            episode.tokens = self._count(episode.content)
        self._episodes.append(episode)
        return episode

    def push(self, kind: EpisodeKind, content: str, **kw) -> Episode:
        return self.add(Episode(kind=kind, content=content, **kw))

    @property
    def episodes(self) -> list[Episode]:
        return list(self._episodes)

    @property
    def count_tokens(self) -> TokenCounter:
        """The counter this window budgets in.

        Exposed because anything else deciding how much context something costs
        — the tool-budget policy, most obviously — has to use the same one, or
        the two will disagree about whether the window is full.
        """
        return self._count

    def used(self) -> int:
        return sum(e.tokens for e in self._episodes)

    def over_budget(self) -> bool:
        return self.used() > self.budget.fillable

    def by_kind(self, kind: EpisodeKind) -> list[Episode]:
        return [e for e in self._episodes if e.kind is kind]

    def render(self) -> str:
        return "\n".join(e.render() for e in self._episodes)

    # -- compaction -----------------------------------------------------------

    def _protected_ids(self) -> set[str]:
        """Permanent episodes, the recent tail, and everything they depend on.

        The transitive closure is the part positional eviction cannot express: if
        turn 28 depends on a decision made at turn 3, turn 3 is protected however
        old it is.
        """
        protected = {e.id for e in self._episodes if e.permanent}
        if self.budget.keep_recent:
            protected |= {e.id for e in self._episodes[-self.budget.keep_recent:]}

        by_id = {e.id: e for e in self._episodes}
        frontier = list(protected)
        while frontier:
            ep = by_id.get(frontier.pop())
            if ep is None:
                continue
            for dep in ep.depends_on:
                if dep not in protected and dep in by_id:
                    protected.add(dep)
                    frontier.append(dep)
        return protected

    def _eviction_order(self, candidates: list[Episode]) -> list[Episode]:
        """Which unprotected episode goes first.

        Lowest salience, then oldest. Deterministic, so the same history always
        compacts the same way — which is what makes a replay a replay.

        Overridable because the eviction *policy* is the part worth experimenting
        with, and because a worse policy is the only honest way to test that the
        validation pass in `_validate` actually catches something. A purely
        positional order — drop the oldest, which is what every reactive
        condenser does — is exactly the policy that loses a safety constraint
        stated early in a long run.
        """
        return sorted(candidates, key=lambda e: (e.salience, e.seq))

    def compact(
        self,
        summarizer: Summarizer = counting_summarizer,
        *,
        target: int | None = None,
    ) -> CompactionReport:
        """Evict down to `target` episode tokens, or to the budget's own target.

        The parameter exists because this window's units are not the only ones
        that matter. `used()` sums `episode.content`, while what a provider
        actually receives also carries a system block, tool schemas and a JSON
        envelope per message. A caller that measures the real request — as
        `AgentLoop` does — can find itself over the provider's limit while
        `used()` is comfortably under `budget.target`, at which point the loop
        below breaks immediately and evicts nothing.

        That is not hypothetical: a real run reported "rendered request is
        29,623 tokens against an allowance of 28,672, and nothing further is
        evictable" on seven consecutive turns, because this method was asked to
        compact and correctly concluded, in its own units, that there was
        nothing to do.
        """
        report = CompactionReport(tokens_before=self.used())
        report.tokens_after = report.tokens_before

        protected = self._protected_ids()
        candidates = [e for e in self._episodes if e.id not in protected]
        if not candidates:
            report.within_budget = self.used() <= self.budget.fillable
            return report

        candidates = self._eviction_order(candidates)

        goal = self.budget.target if target is None else max(0, int(target))
        evicted: list[Episode] = []
        running = self.used()
        for ep in candidates:
            if running <= goal:
                break
            evicted.append(ep)
            running -= ep.tokens

        if not evicted:
            report.within_budget = self.used() <= self.budget.fillable
            return report

        evicted_ids = {e.id for e in evicted}
        anchor = min(e.seq for e in evicted)
        kept = [e for e in self._episodes if e.id not in evicted_ids]

        summary = Episode(
            kind=EpisodeKind.SUMMARY,
            content=summarizer(sorted(evicted, key=lambda e: e.seq)),
            lineage=tuple(sorted(evicted_ids)),
            seq=anchor,
        )
        summary.tokens = self._count(summary.content)

        # Insert where the earliest evicted episode sat, so ordering still tells
        # the truth about when things happened.
        position = next((i for i, e in enumerate(kept) if e.seq > anchor), len(kept))
        kept.insert(position, summary)

        previous = self._episodes
        self._episodes = kept

        report.evicted = len(evicted)
        report.by_kind = dict(Counter(str(e.kind) for e in evicted))
        report.summary_id = summary.id
        report.tokens_after = self.used()

        self._validate(report, previous)
        if not report.valid:
            # Put it back. A refused compaction is a budget problem to solve
            # elsewhere; a silent one is a correctness problem forever.
            self._episodes = previous
            raise CompactionRefused(
                "compaction would have lost load-bearing context: "
                f"invariants={report.invariants_preserved} contract={report.contract_preserved} "
                f"dependencies={report.dependencies_intact} budget={report.within_budget}"
            )
        return report

    def _validate(self, report: CompactionReport, previous: Sequence[Episode]) -> None:
        """The pass that makes this plane different from every shipped condenser."""
        surviving = {e.id for e in self._episodes}
        covered = {i for e in self._episodes for i in e.lineage}

        before_inv = {e.id for e in previous if e.kind is EpisodeKind.INVARIANT}
        report.invariants_preserved = before_inv <= surviving

        before_contract = {e.id for e in previous if e.kind is EpisodeKind.CONTRACT}
        report.contract_preserved = before_contract <= surviving

        # A dependency may vanish only if a summary explicitly stands in for it.
        dangling = {
            dep
            for e in self._episodes
            for dep in e.depends_on
            if dep not in surviving and dep not in covered
        }
        report.dependencies_intact = not dangling

        report.within_budget = self.used() <= self.budget.fillable

    def rehydrate(self, episode: Episode, *, pin: bool = True) -> Episode:
        """Bring an evicted episode back, from the Ledger, on demand.

        The known limitation of dependency-aware eviction is that it cannot see
        the future: a fact that only *becomes* load-bearing many turns later had
        no link protecting it when compaction ran, so it goes. What saves it from
        being lost is that the summary standing in its place carries its lineage,
        so the id is still addressable in the Ledger.

        Pinning on the way back in is the default, because an episode worth
        recovering once is worth keeping.
        """
        restored = Episode(
            kind=episode.kind,
            content=episode.content,
            id=episode.id,
            tokens=episode.tokens,
            depends_on=episode.depends_on,
            lineage=episode.lineage,
            pinned=pin,
            meta={**episode.meta, "rehydrated": True},
        )
        return self.add(restored)

    def covers(self, episode_id: str) -> bool:
        """Is this id either present, or named by a summary that replaced it?"""
        return any(
            e.id == episode_id or episode_id in e.lineage for e in self._episodes
        )

    def ensure_fits(self, summarizer: Summarizer = counting_summarizer) -> CompactionReport | None:
        """Compact only if over budget. Returns None when nothing was needed."""
        if not self.over_budget():
            return None
        return self.compact(summarizer)
