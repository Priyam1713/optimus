"""Cost accounting, as a projection over the Ledger.

Invariant 2 (`apex.md` §3) says every other store is a projection of the event
log. This module is the first one, and it is deliberately the *cost* projection,
because `research.md` §2.1 is the finding that should govern the whole project:

    Goose          28-37K tokens per solved task
    OpenHands-SDK  ~841K
    OpenCode       1.1-1.5M

with pass rates differing by 0-8pp. A 20-40x spread produced by context
accumulation and idle turns, and **nobody publishes either number**. Row 19 of
the Apex bill of materials is the one with no incumbent, and it is won by
measuring rather than by inventing anything.

Nothing here computes; it reads what the Gate already recorded. A metric that
requires its own bookkeeping drifts from the thing it measures.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .ledger.events import Event


@dataclass
class RunMeter:
    """One run's cost, read back from the log."""

    run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    wall_ms: int = 0
    actions: int = 0
    settled_ok: int = 0
    denials: int = 0
    approvals_required: int = 0
    #: Turns that produced no action at all. Goose 0.2-0.3/task, OpenCode
    #: 2.0-2.16 — a 10x difference that shows up nowhere in any leaderboard.
    no_action_turns: int = 0
    #: Input tokens served from cache. Reported separately because a harness
    #: that prints `input_tokens` without saying how many were cache hits has
    #: published a number nobody can compare to anyone else's.
    cached_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0
    #: Turns the loop's breakers had to intervene on, and actions the Gate
    #: refused outright. Both are receipts, not diagnostics.
    breakers_fired: int = 0
    #: Model calls that never reached the provider. Kept apart from
    #: `no_action_turns` because they are not the same thing and conflating them
    #: makes the published figure incomparable to anyone else's — a real
    #: Terminal-Bench run reported 3 idle turns when the model had idled zero
    #: times and been rate-limited three.
    provider_errors: int = 0
    stop_reason: str = ""
    solved: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def tokens_per_solved_task(self) -> float:
        """The headline. Unsolved runs cost infinity, which is the honest number:
        tokens spent without solving anything bought nothing."""
        return float(self.total_tokens) if self.solved else float("inf")

    def render(self) -> str:
        tps = "inf" if not self.solved else f"{self.total_tokens:,}"
        return (
            f"run={self.run_id or '-'} solved={self.solved} tokens={self.total_tokens:,} "
            f"(per solved: {tps}) cached={self.cached_tokens:,} cost=${self.cost_usd:.4f} "
            f"no_action={self.no_action_turns} provider_errors={self.provider_errors} "
            f"turns={self.turns} actions={self.actions} "
            f"denials={self.denials} wall={self.wall_ms}ms"
        )


@dataclass
class SuiteMeter:
    """Across a task set — the shape a benchmark row needs."""

    runs: list[RunMeter] = field(default_factory=list)

    @property
    def solved(self) -> int:
        return sum(1 for r in self.runs if r.solved)

    @property
    def total_tokens(self) -> int:
        return sum(r.total_tokens for r in self.runs)

    @property
    def pass_rate(self) -> float:
        return (self.solved / len(self.runs)) if self.runs else 0.0

    @property
    def tokens_per_solved_task(self) -> float:
        """Total spend divided by tasks actually solved.

        Deliberately not the mean of per-run figures: tokens burned on failures
        are part of what a task cost you, and averaging per-run numbers hides
        them.
        """
        return float(self.total_tokens) / self.solved if self.solved else float("inf")

    @property
    def no_action_turns_per_task(self) -> float:
        return (sum(r.no_action_turns for r in self.runs) / len(self.runs)) if self.runs else 0.0

    @property
    def provider_errors(self) -> int:
        return sum(r.provider_errors for r in self.runs)

    @property
    def total_cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.runs)

    @property
    def cost_per_solved_task(self) -> float:
        return (self.total_cost_usd / self.solved) if self.solved else float("inf")

    @property
    def cache_hit_rate(self) -> float:
        """Cached share of input tokens, across the suite.

        The single number that says whether a long-horizon harness is paying
        O(n) or O(n^2) for its own history.
        """
        billed = sum(r.input_tokens for r in self.runs)
        return (sum(r.cached_tokens for r in self.runs) / billed) if billed else 0.0

    @property
    def denials(self) -> int:
        return sum(r.denials for r in self.runs)

    @property
    def approvals_required(self) -> int:
        """Operator interventions the run would have needed.

        Zero under a signed envelope, and a large number without one. Publishing
        it is what stops "fully autonomous" from being an unfalsifiable claim.
        """
        return sum(r.approvals_required for r in self.runs)

    def render(self) -> str:
        tps = "inf" if not self.solved else f"{self.tokens_per_solved_task:,.0f}"
        return (
            f"tasks={len(self.runs)} solved={self.solved} pass_rate={self.pass_rate:.1%} "
            f"tokens/solved={tps} no_action/task={self.no_action_turns_per_task:.2f} "
            f"cache_hit={self.cache_hit_rate:.1%} denials={self.denials} "
            f"approvals_needed={self.approvals_required}"
        )


def aggregate(events: Iterable[Event], *, run_id: str = "", solved: bool | None = None) -> RunMeter:
    """Fold settlement and decision rows into one run's cost."""
    m = RunMeter(run_id=run_id)
    for ev in events:
        if run_id and ev.payload.get("run_id", run_id) != run_id:
            continue
        match ev.kind:
            case "gate.decision":
                m.actions += 1
                verdict = ev.payload.get("verdict", "")
                if verdict == "deny":
                    m.denials += 1
                elif verdict == "needs_approval":
                    m.approvals_required += 1
            case "gate.refused":
                m.actions += 1
                m.denials += 1
            case "effect.settled":
                _fold_meter(m, ev.payload.get("meter") or {})
                if ev.payload.get("ok"):
                    m.settled_ok += 1
            case "model.call":
                # The loop's own row. Tools are effects and get `effect.settled`;
                # a model call is not an effect, and leaving it out would mean
                # the largest line item in the system never reached the metric
                # named after it.
                meter = ev.payload.get("meter") or {}
                _fold_meter(m, meter)
                m.turns += 1
                if meter.get("provider_error") or ev.payload.get("error"):
                    m.provider_errors += 1
            case "loop.breaker":
                m.breakers_fired += 1
            case "run.finished":
                m.stop_reason = str(ev.payload.get("stop_reason", ""))
                # `solved` is null on the loop's own row by design: the loop does
                # not grade itself. A caller who knows the verdict passes it in.
                solved_row = ev.payload.get("solved")
                if solved_row is not None:
                    m.solved = bool(solved_row)
    if solved is not None:
        m.solved = solved
    return m


def _fold_meter(m: RunMeter, meter: dict) -> None:
    m.input_tokens += int(meter.get("input_tokens", 0) or 0)
    m.output_tokens += int(meter.get("output_tokens", 0) or 0)
    m.wall_ms += int(meter.get("wall_ms", 0) or 0)
    m.cached_tokens += int(meter.get("cached_tokens", 0) or 0)
    m.cost_usd += float(meter.get("cost_usd", 0.0) or 0.0)
    if meter.get("no_action"):
        m.no_action_turns += 1


def suite(runs: Sequence[RunMeter]) -> SuiteMeter:
    return SuiteMeter(list(runs))
