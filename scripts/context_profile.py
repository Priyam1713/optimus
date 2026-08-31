"""Measure what the context plane is worth, in the units that matter.

**What this is and is not.** It models the mechanism that produces the 20-40x
token spread in `research.md` §2.1 — every turn re-sends the accumulated window,
so history growth is multiplied by turn count — over a synthetic trajectory. It
is *not* a benchmark: it says nothing about pass rates, and real numbers come
from Harbor at M3. Its job is to show whether the plane does the thing
[apex.md](../apex.md) §4 says it must, before anything is built on top of it.

    .venv/Scripts/python.exe scripts/context_profile.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from optimus.context import ContextBudget, ContextWindow, EpisodeKind  # noqa: E402
from optimus.context.window import CompactionRefused  # noqa: E402

TURNS = 150
OBSERVATION = "tool result: " + "d" * 1_400   # a file read, a grep hit, a diff
THOUGHT = "reasoning: " + "t" * 300


class NoCompaction(ContextWindow):
    """Accumulate forever. The profile a harness has before anyone looks."""

    def ensure_fits(self, summarizer=None):  # type: ignore[override]
        return None


class Positional(ContextWindow):
    """Reactive condenser: keep the tail, drop the oldest."""

    def _protected_ids(self):
        return {e.id for e in self.episodes[-self.budget.keep_recent:]}

    def _eviction_order(self, candidates):
        return sorted(candidates, key=lambda e: e.seq)


def run(cls, budget: ContextBudget) -> tuple[int, int, int, bool]:
    """Returns (input tokens billed across the run, peak window, compactions, survived)."""
    w = cls(budget)
    w.push(EpisodeKind.CONTRACT, "Ship the release notes for v2.")
    w.push(EpisodeKind.INVARIANT, "Never write outside the workspace.")
    w.push(EpisodeKind.INVARIANT, "Never send anything without assent.")

    billed = peak = compactions = 0
    survived = True
    key = None
    for i in range(TURNS):
        if i == 2:
            key = w.push(EpisodeKind.OBSERVATION, "credentials live in vault/prod " + "k" * 200)
        elif i == TURNS - 2 and key is not None:
            w.push(EpisodeKind.ACTION, "use the credential path", depends_on=frozenset({key.id}))
        else:
            w.push(EpisodeKind.THOUGHT, f"{THOUGHT} {i}")
            w.push(EpisodeKind.OBSERVATION, f"{OBSERVATION} {i}")
        try:
            if w.ensure_fits() is not None:
                compactions += 1
        except CompactionRefused:
            survived = False
        # Every turn re-sends the window. This multiplication is the whole story.
        billed += w.used()
        peak = max(peak, w.used())

    surviving = {e.id for e in w.episodes}
    covered = {i for e in w.episodes for i in e.lineage}
    return {
        "billed": billed,
        "peak": peak,
        "compactions": compactions,
        "compaction_refused": not survived,
        "invariants": sum(1 for e in w.episodes if e.kind is EpisodeKind.INVARIANT),
        # A fact that only *becomes* load-bearing later cannot be protected by a
        # dependency link that did not exist when the compaction ran.
        "late_dep_in_window": key is not None and key.id in surviving,
        "late_dep_recoverable": key is not None and (key.id in surviving or key.id in covered),
    }


def main() -> None:
    budget = ContextBudget(total=32_000, reserve_output=4_000, keep_recent=6)
    rows = [
        ("no compaction", NoCompaction),
        ("positional (reactive condenser)", Positional),
        ("optimus (priority + dependency)", ContextWindow),
    ]

    print(f"{TURNS} turns, {budget.total:,}-token window, {budget.fillable:,} fillable\n")
    hdr = f"{'strategy':34} {'billed input':>13} {'peak':>8} {'cmp':>4} {'inv':>4} {'late dep':>9}"
    print(hdr)
    print("-" * len(hdr))
    results: dict[str, dict] = {}
    for name, cls in rows:
        r = results[name] = run(cls, budget)
        late = "kept" if r["late_dep_in_window"] else (
            "recoverable" if r["late_dep_recoverable"] else "LOST")
        note = " (all compactions refused)" if r["compaction_refused"] else ""
        print(
            f"{name:34} {r['billed']:>13,} {r['peak']:>8,} {r['compactions']:>4} "
            f"{r['invariants']:>4} {late:>9}{note}"
        )
    print("-" * len(hdr))
    naive = results["no compaction"]["billed"]
    ours = results["optimus (priority + dependency)"]["billed"]
    print(f"reduction vs no compaction: {naive / ours:.1f}x\n")

    print("Read this carefully rather than as a win:")
    print()
    print("* The reactive condenser could not compact at all: every attempt would")
    print("  have evicted a safety constraint, so the validator refused it. Without")
    print("  that validator it would have compacted happily and silently lost the")
    print("  constraint - which is the failure 'Governance Decay' describes and")
    print("  which every shipped condenser is exposed to.")
    print()
    print("* 'late dep' is an honest limitation, not a bug. A dependency link")
    print("  protects an episode only if the link exists when compaction runs. A")
    print("  fact that becomes load-bearing 140 turns later cannot be protected")
    print("  retroactively, so it is evicted - but the summary carries its lineage,")
    print("  so it is recoverable from the Ledger rather than gone. Pinning it when")
    print("  it is learned is the cheap fix and is what CONTRACT/INVARIANT are.")
    print()
    print("* This is a mechanism measurement on a synthetic trajectory, not a")
    print("  benchmark. Real tokens-per-solved-task comes from Harbor at M3.")


if __name__ == "__main__":
    main()
