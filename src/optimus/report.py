"""Joining what the verifier decided to what the ledger recorded.

The loop does not grade itself (`loop/agent.py`), so the two halves of every
published number live in different files: Harbor's verifier writes the reward
into a trial's `result.json`, and Optimus writes its metered receipt into that
trial's `agent/optimus-metrics.json`. This module is the join, and it is the last
piece of row 19.

Two things here are not available from Harbor's own reporting.

**pass^k, not just pass@k.** Harbor computes `pass@k` — the unbiased estimate
that *at least one* of k attempts succeeds, which rises towards 1 as you buy more
attempts. `pass^k` is the opposite question: does the harness succeed on *all* k
of them. One measures how cheap a lottery ticket is; the other measures whether
you would put the thing in a pipeline. Both use the same combinatorial estimator
over the same trials, and reporting only the flattering one is a choice.

**Tokens per solved task, no-action turns, refusals, interventions.** These
require the agent to have metered itself honestly on the way past, which is what
the whole Ledger exists for, and to have published the number it actually
recorded rather than a number it computed afterwards.

An unsolved task's tokens still count. `tokens_per_solved_task` divides *total*
spend by tasks solved, never the mean of per-task figures — money burned on
failures is part of what the successes cost you, and averaging per-run numbers is
how that disappears.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Filenames written by `adapters/harbor.py` and by Harbor itself.
METRICS_FILE = "optimus-metrics.json"
RESULT_FILES = ("result.json", "results.json")


# --------------------------------------------------------------------------
# estimators
# --------------------------------------------------------------------------

def pass_at_k(n: int, c: int, k: int) -> float:
    """P(at least one success) over a random k-subset of n trials with c passes.

    The Codex/HumanEval estimator, and the one Harbor already reports.
    """
    if k > n:
        raise ValueError(f"k={k} exceeds n={n}")
    if n - c < k:
        return 1.0
    return 1.0 - math.prod((n - c - i) / (n - i) for i in range(k))


def pass_hat_k(n: int, c: int, k: int) -> float:
    """P(*all* k succeed) over a random k-subset — `pass^k`.

    C(c, k) / C(n, k): the chance that every one of k drawn attempts is a pass.
    Unbiased for the same reason `pass_at_k` is, and a far harder number to move
    by spending more compute, which is precisely why it is the one worth
    publishing next to a reliability claim.
    """
    if k > n:
        raise ValueError(f"k={k} exceeds n={n}")
    if c < k:
        return 0.0
    return math.comb(c, k) / math.comb(n, k)


def eligible_k(max_k: int) -> list[int]:
    """1, then powers of two, then multiples of five — Harbor's own ladder."""
    ks = {1}
    k = 2
    while k <= max_k:
        ks.add(k)
        k *= 2
    k = 5
    while k <= max_k:
        ks.add(k)
        k += 5
    return sorted(ks)


# --------------------------------------------------------------------------
# reading a run off disk
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Trial:
    """One trial: what the verifier said, and what we spent getting there."""

    task: str
    trial_dir: str
    solved: bool
    reward: float | None = None
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def has_receipt(self) -> bool:
        return bool(self.metrics)

    def _int(self, key: str) -> int:
        return int(self.metrics.get(key, 0) or 0)

    @property
    def total_tokens(self) -> int:
        return self._int("total_tokens")

    @property
    def input_tokens(self) -> int:
        return self._int("input_tokens")

    @property
    def cached_tokens(self) -> int:
        return self._int("cached_tokens")

    @property
    def no_action_turns(self) -> int:
        return self._int("no_action_turns")

    @property
    def refusals(self) -> int:
        return self._int("unsafe_attempts_refused")

    @property
    def interventions(self) -> int:
        return self._int("operator_interventions_required")

    @property
    def cost_usd(self) -> float:
        return float(self.metrics.get("cost_usd", 0.0) or 0.0)


def _reward_of(result: dict[str, Any]) -> tuple[bool, float | None]:
    """A trial passed when its single reward is 1.

    Multi-reward tasks are reported as unsolved rather than guessed at, and
    `Report.unscored` counts them, because silently collapsing several rewards
    into a boolean is how a benchmark number stops meaning anything.
    """
    verifier = result.get("verifier_result") or {}
    rewards = verifier.get("rewards")
    if not isinstance(rewards, dict) or len(rewards) != 1:
        return False, None
    value = next(iter(rewards.values()))
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False, None
    return float(value) == 1.0, float(value)


def load_trials(run_dir: str | Path) -> list[Trial]:
    """Walk a Harbor run directory and pair every result with its receipt."""
    root = Path(run_dir)
    trials: list[Trial] = []
    seen: set[Path] = set()
    for name in RESULT_FILES:
        for result_path in sorted(root.rglob(name)):
            trial_dir = result_path.parent
            if trial_dir in seen:
                continue
            seen.add(trial_dir)
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(result, dict) or "task_name" not in result:
                continue
            solved, reward = _reward_of(result)
            trials.append(
                Trial(
                    task=str(result.get("task_name", trial_dir.name)),
                    trial_dir=str(trial_dir),
                    solved=solved,
                    reward=reward,
                    metrics=_load_metrics(trial_dir),
                )
            )
    return trials


def _load_metrics(trial_dir: Path) -> dict[str, Any]:
    for candidate in (trial_dir / "agent" / METRICS_FILE, trial_dir / METRICS_FILE):
        if candidate.is_file():
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return {}
            return loaded if isinstance(loaded, dict) else {}
    # Multi-step trials keep per-step agent dirs.
    for candidate in sorted(trial_dir.glob(f"steps/*/agent/{METRICS_FILE}")):
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict):
            return loaded
    return {}


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

@dataclass(slots=True)
class Report:
    trials: list[Trial]

    # -- grouping -------------------------------------------------------------

    @property
    def by_task(self) -> dict[str, list[Trial]]:
        grouped: dict[str, list[Trial]] = defaultdict(list)
        for t in self.trials:
            grouped[t.task].append(t)
        return dict(grouped)

    @property
    def tasks(self) -> int:
        return len(self.by_task)

    @property
    def solved_trials(self) -> int:
        return sum(1 for t in self.trials if t.solved)

    @property
    def unscored(self) -> int:
        """Trials whose reward could not be read as a single 0/1."""
        return sum(1 for t in self.trials if t.reward is None)

    @property
    def metered_trials(self) -> int:
        """Trials that carry an Optimus receipt, and can therefore be costed."""
        return sum(1 for t in self.trials if t.has_receipt)

    @property
    def without_receipt(self) -> int:
        """Trials with no Optimus metrics file.

        Reported rather than dropped: a token figure computed over a subset of
        the trials, presented as if it covered all of them, is the exact way
        these numbers get quietly flattered.
        """
        return sum(1 for t in self.trials if not t.has_receipt)

    # -- reliability ----------------------------------------------------------

    def _k_ladder(self) -> list[int]:
        grouped = self.by_task
        if not grouped:
            return []
        return eligible_k(min(len(v) for v in grouped.values()))

    def pass_at_k(self) -> dict[int, float]:
        grouped = self.by_task
        return {
            k: sum(
                pass_at_k(len(v), sum(1 for t in v if t.solved), k)
                for v in grouped.values()
            ) / len(grouped)
            for k in self._k_ladder()
        }

    def pass_hat_k(self) -> dict[int, float]:
        grouped = self.by_task
        return {
            k: sum(
                pass_hat_k(len(v), sum(1 for t in v if t.solved), k)
                for v in grouped.values()
            ) / len(grouped)
            for k in self._k_ladder()
        }

    # -- economy --------------------------------------------------------------

    @property
    def total_tokens(self) -> int:
        return sum(t.total_tokens for t in self.trials)

    @property
    def tokens_per_solved_task(self) -> float:
        """Everything spent, over everything solved. Failures included."""
        return (
            self.total_tokens / self.solved_trials
            if self.solved_trials
            else float("inf")
        )

    @property
    def cost_per_solved_task(self) -> float:
        total = sum(t.cost_usd for t in self.trials)
        return total / self.solved_trials if self.solved_trials else float("inf")

    @property
    def no_action_turns_per_task(self) -> float:
        return (
            sum(t.no_action_turns for t in self.trials) / len(self.trials)
            if self.trials
            else 0.0
        )

    @property
    def cache_hit_rate(self) -> float:
        billed = sum(t.input_tokens for t in self.trials)
        return sum(t.cached_tokens for t in self.trials) / billed if billed else 0.0

    @property
    def unsafe_attempts_refused(self) -> int:
        return sum(t.refusals for t in self.trials)

    @property
    def operator_interventions_required(self) -> int:
        return sum(t.interventions for t in self.trials)

    # -- output ---------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "trials": len(self.trials),
            "solved_trials": self.solved_trials,
            "trials_without_receipt": self.without_receipt,
            "trials_unscored": self.unscored,
            "pass_at_k": {str(k): round(v, 4) for k, v in self.pass_at_k().items()},
            "pass_hat_k": {str(k): round(v, 4) for k, v in self.pass_hat_k().items()},
            "metered_trials": self.metered_trials,
            "total_tokens": self.total_tokens if self.metered_trials else None,
            # None, not 0, when nothing was metered: absent evidence must not
            # serialise as a favourable measurement.
            "tokens_per_solved_task": (
                _finite(self.tokens_per_solved_task) if self.metered_trials else None
            ),
            "cost_per_solved_task_usd": (
                _finite(self.cost_per_solved_task) if self.metered_trials else None
            ),
            "no_action_turns_per_task": (
                round(self.no_action_turns_per_task, 3) if self.metered_trials else None
            ),
            "cache_hit_rate": (
                round(self.cache_hit_rate, 4) if self.metered_trials else None
            ),
            "unsafe_attempts_refused": self.unsafe_attempts_refused,
            "operator_interventions_required": self.operator_interventions_required,
        }

    def render(self) -> str:
        d = self.as_dict()
        lines = [
            f"tasks={d['tasks']}  trials={d['trials']}  solved={d['solved_trials']}",
        ]
        if self.without_receipt:
            lines.append(
                f"  !! {self.without_receipt} trial(s) have no Optimus receipt; "
                "token figures below cover only the rest"
            )
        if self.unscored:
            lines.append(
                f"  !! {self.unscored} trial(s) had no single 0/1 reward and count "
                "as unsolved"
            )
        for label, values in (("pass@k ", self.pass_at_k()), ("pass^k ", self.pass_hat_k())):
            if values:
                lines.append(
                    "  " + label
                    + "  ".join(f"k={k}: {v:.3f}" for k, v in sorted(values.items()))
                )
        if not self.metered_trials:
            # Zero receipts means zero *evidence*, not zero cost. Printing
            # "tokens/solved-task 0" here would be the most flattering number
            # available and a wholly invented one — which is the failure this
            # whole module exists to avoid. Happens for real whenever another
            # agent's run is reported: Harbor's own `oracle` writes no receipt.
            lines.append("  economy                 n/a - no Optimus receipts in this run")
            return "\n".join(lines)

        tps = d["tokens_per_solved_task"]
        lines += [
            f"  tokens/solved-task      {'inf' if tps is None else f'{tps:,.0f}'}",
            "  cost/solved-task        "
            + ("inf" if d["cost_per_solved_task_usd"] is None
               else f"${d['cost_per_solved_task_usd']:,.4f}"),
            f"  no-action turns/task    {d['no_action_turns_per_task']:.2f}",
            f"  cache hit rate          {d['cache_hit_rate']:.1%}",
            f"  unsafe attempts refused {d['unsafe_attempts_refused']}",
            f"  operator interventions  {d['operator_interventions_required']}",
        ]
        return "\n".join(lines)


def _finite(value: float) -> float | None:
    """`inf` is not JSON. `null` is, and means the same thing here: no solves."""
    return None if math.isinf(value) or math.isnan(value) else round(value, 6)


def report_for(run_dir: str | Path) -> Report:
    return Report(load_trials(run_dir))


def report_from(trials: Iterable[Trial]) -> Report:
    return Report(list(trials))


__all__: Sequence[str] = (
    "METRICS_FILE",
    "Report",
    "Trial",
    "eligible_k",
    "load_trials",
    "pass_at_k",
    "pass_hat_k",
    "report_for",
    "report_from",
)
