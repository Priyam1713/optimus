"""Reading a run back out of its ledger.

Every diagnosis in this project's findings list — the four context bugs, the
expired envelope, the uncancellable thread — was reached by writing a throwaway
script against a trial's ledger. Ten runs meant ten scripts. That does not scale
to 89 tasks times 5 attempts, and it means the answer lives in a terminal
scrollback rather than in the repository.

So this is that script, made permanent. It reads only; it computes nothing the
ledger does not already contain, and it does not touch the trial.

The organising question is **"where did the turns and the tokens go, and what
stopped it"**, because that is what every one of those investigations turned out
to be asking. What it deliberately does *not* do is judge: it prints the growth
curve and marks the compaction points, and leaves the reader to notice that the
curve went somewhere it should not have.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ledger.events import Event
from .ledger.store import LedgerStore

#: Where a trial keeps its ledger, relative to the trial directory.
LEDGER_NAMES = ("agent/ledger.db", "ledger.db")


# --------------------------------------------------------------------------
# one turn, assembled from the rows that belong to it
# --------------------------------------------------------------------------

@dataclass
class Turn:
    """What happened on one turn, in the order it happened."""

    number: int
    tools: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    wall_ms: int = 0
    error: str = ""
    decisions: list[tuple[str, str, str]] = field(default_factory=list)  # verdict, rule, target
    settled: list[tuple[str, bool]] = field(default_factory=list)        # tool, ok
    breakers: list[tuple[str, str]] = field(default_factory=list)        # kind, detail
    compaction: dict[str, Any] | None = None
    #: What the context plane believed before this turn's call, from
    #: `context.turn`. Absent on ledgers written before that row existed.
    context: dict[str, Any] | None = None

    @property
    def estimate_gap(self) -> int | None:
        """Estimated prompt size minus what the provider actually charged.

        Negative means the harness was under-counting — the direction that ends
        a run, because compaction never fires and the server refuses.
        """
        if not self.context or not self.input_tokens:
            return None
        return int(self.context.get("estimated", 0)) - self.input_tokens

    @property
    def refused(self) -> int:
        return sum(1 for verdict, _, _ in self.decisions if verdict != "allow")

    @property
    def failed_effects(self) -> int:
        return sum(1 for _, ok in self.settled if not ok)


@dataclass
class Explanation:
    """A whole run, reassembled."""

    source: str = ""
    run_id: str = ""
    model: str = ""
    engine: str = ""
    local: bool | None = None
    workspace: str = ""
    envelope: str = ""
    envelope_uses: int = 0
    stop_reason: str = ""
    summary: str = ""
    finished: bool = False
    turns: list[Turn] = field(default_factory=list)
    denials: Counter = field(default_factory=Counter)
    events: int = 0
    reward: float | None = None

    @property
    def real_turns(self) -> list[Turn]:
        """Turns the model actually took.

        Turn 0 is the bucket for rows written before the first model call — the
        environment probe. It is not a turn, and one renderer excluding it while
        another did not is how the same run reported 40 turns in one view and 41
        in another. One property, so they cannot disagree.
        """
        return [t for t in self.turns if t.number > 0]

    @property
    def setup(self) -> Turn | None:
        return next((t for t in self.turns if t.number == 0), None)

    @property
    def total_tokens(self) -> int:
        return sum(t.input_tokens + t.output_tokens for t in self.real_turns)


def _ledger_path(target: str | Path) -> Path | None:
    p = Path(target)
    if p.is_file():
        return p
    for name in LEDGER_NAMES:
        if (p / name).is_file():
            return p / name
    return None


def _reward_for(trial_dir: Path) -> float | None:
    for name in ("result.json", "results.json"):
        candidate = trial_dir / name
        if not candidate.is_file():
            continue
        try:
            rewards = (
                json.loads(candidate.read_text(encoding="utf-8")).get("verifier_result")
                or {}
            ).get("rewards")
        except (OSError, ValueError):
            return None
        if isinstance(rewards, dict) and len(rewards) == 1:
            value = next(iter(rewards.values()))
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
    return None


def _bucket(turns: dict[int, Turn], number: int) -> Turn:
    """The turn with this number, created if new.

    A bucket's key and its own `number` have to agree. They did not: a
    compaction recorded for turn 21 created a bucket keyed 21 holding a `Turn`
    numbered 20, so the timeline printed turn 20 twice and turn 21 never.
    """
    turn = turns.get(number)
    if turn is None:
        turn = turns[number] = Turn(number=number)
    return turn


def explain(events: Sequence[Event], *, source: str = "") -> Explanation:
    """Fold a ledger into a per-turn account.

    Rows are attributed to the turn that was open when they were written:
    `model.call` carries an explicit turn number, and the gate and effect rows
    between one model call and the next belong to the turn that made it. That
    ordering is only reliable because the ledger is append-only and
    single-writer — the same property that makes it verifiable.
    """
    out = Explanation(source=source, events=len(events))
    by_number: dict[int, Turn] = {}
    current = 0

    for event in events:
        payload = event.payload
        match event.kind:
            case "run.started":
                out.run_id = payload.get("run_id", "")
                out.model = payload.get("model", "")
            case "model.route":
                out.engine = payload.get("engine", "")
                out.model = payload.get("model") or out.model
                out.local = payload.get("local")
            case "envelope.opened":
                out.envelope = payload.get("envelope_id", "")
                out.workspace = payload.get("workspace", "") or out.workspace
            case "envelope.used":
                out.envelope_uses += 1
            case "model.call":
                current = int(payload.get("turn", current + 1))
                meter = payload.get("meter") or {}
                turn = _bucket(by_number, current)
                turn.tools = list(payload.get("tools") or [])
                turn.input_tokens = int(meter.get("input_tokens", 0) or 0)
                turn.output_tokens = int(meter.get("output_tokens", 0) or 0)
                turn.cached_tokens = int(meter.get("cached_tokens", 0) or 0)
                turn.wall_ms = int(meter.get("wall_ms", 0) or 0)
                turn.error = str(payload.get("error") or "")
            case "gate.decision":
                turn = _bucket(by_number, current)
                target = payload.get("target") or {}
                turn.decisions.append((
                    payload.get("verdict", "?"),
                    payload.get("rule", "?"),
                    str(target.get("relpath") or target.get("script")
                        or target.get("program") or ""),
                ))
                if payload.get("verdict") != "allow":
                    out.denials[payload.get("rule", "?")] += 1
            case "gate.refused":
                turn = _bucket(by_number, current)
                turn.decisions.append(("refused", payload.get("rule", "?"),
                                       str(payload.get("reason", ""))[:60]))
                out.denials[payload.get("rule", "?")] += 1
            case "effect.settled":
                turn = _bucket(by_number, current)
                turn.settled.append((payload.get("tool", "?"), bool(payload.get("ok"))))
            case "loop.breaker":
                turn = _bucket(by_number, int(payload.get("turn", current)))
                turn.breakers.append((payload.get("kind", "?"),
                                      str(payload.get("detail", ""))))
            case "context.turn":
                _bucket(by_number, int(payload.get("turn", current))).context = dict(payload)
            case "context.compacted":
                turn = _bucket(by_number, int(payload.get("turn", current)))
                turn.compaction = dict(payload)
            case "run.finished":
                out.finished = True
                out.stop_reason = payload.get("stop_reason", "")
                out.summary = str(payload.get("summary") or "")

    out.turns = [by_number[n] for n in sorted(by_number)]
    if not out.stop_reason:
        # A run that never wrote its own ending was killed. Say that, rather
        # than leaving the field blank for a reader to misread as "fine".
        out.stop_reason = "killed_before_finishing"
    return out


def explain_trial(target: str | Path) -> Explanation | None:
    """Read a trial directory (or a ledger file) into an `Explanation`."""
    ledger = _ledger_path(target)
    if ledger is None:
        return None
    store = LedgerStore(ledger)
    try:
        result = explain(store.events(), source=str(ledger))
    finally:
        store.close()
    trial_dir = ledger.parent.parent if ledger.parent.name == "agent" else ledger.parent
    result.reward = _reward_for(trial_dir)
    return result


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def _bar(value: int, peak: int, width: int = 24) -> str:
    if peak <= 0:
        return ""
    filled = max(1, round(width * value / peak)) if value else 0
    return "#" * filled + "." * (width - filled)


def render(exp: Explanation, *, timeline: bool = True) -> str:
    lines: list[str] = []
    verdict = (
        "solved" if exp.reward == 1.0
        else "not solved" if exp.reward == 0.0
        else "ungraded"
    )
    where = f"{exp.engine}:{exp.model}" if exp.engine else (exp.model or "?")
    real_turns = exp.real_turns
    setup = exp.setup
    lines += [
        f"run      {exp.run_id or '?'}",
        f"model    {where}" + ("  (local)" if exp.local else ""),
        f"stopped  {exp.stop_reason}"
        + ("" if exp.finished else "   <- the loop never wrote its own ending"),
        f"verifier {verdict}",
        f"ledger   {exp.events} rows, {len(real_turns)} turns"
        + (f", envelope {exp.envelope} used {exp.envelope_uses}x" if exp.envelope else ""),
    ]
    if exp.summary:
        lines.append(f"summary  {exp.summary[:120]}")

    if not exp.turns:
        return "\n".join(lines)

    if setup and setup.settled:
        # The environment probe: one gated command before the first model call,
        # cheap structured priming rather than five exploratory turns
        # (`research.md` §2.1). Not a turn, and counting it as one overstated
        # them by one.
        lines.append(f"priming  {len(setup.settled)} gated command(s) before turn 1")

    # -- where the turns went ------------------------------------------------
    tools = Counter(t for turn in real_turns for t in turn.tools)
    idle = sum(1 for t in real_turns if not t.tools and not t.error)
    errored = sum(1 for t in real_turns if t.error)
    lines += ["", "where the turns went"]
    for name, n in tools.most_common():
        lines.append(f"  {n:>4}  {name}")
    if idle:
        lines.append(f"  {idle:>4}  (no tool called - a no-action turn)")
    if errored:
        lines.append(f"  {errored:>4}  (the model call itself failed)")

    # -- where the tokens went -----------------------------------------------
    peak = max((t.input_tokens for t in real_turns), default=0)
    billed = sum(t.input_tokens for t in real_turns)
    cached = sum(t.cached_tokens for t in real_turns)
    lines += [
        "",
        f"where the tokens went    {exp.total_tokens:,} total, "
        f"{cached:,} cached ({cached / billed:.0%} of input)"
        if billed else "where the tokens went    nothing billed",
    ]
    if timeline and peak:
        lines.append(f"  prompt size per turn, peak {peak:,}")
        for turn in real_turns:
            mark = ""
            if turn.compaction:
                mark = (f"  <- compacted {turn.compaction.get('evicted')} episodes, "
                        f"{turn.compaction.get('rendered_before')} -> "
                        f"{turn.compaction.get('rendered_after')}")
            elif turn.error:
                mark = f"  <- {turn.error.split(':')[0][:40]}"
            elif turn.breakers:
                mark = "  <- " + ", ".join(k for k, _ in turn.breakers)
            elif turn.refused:
                mark = f"  <- {turn.refused} refused"
            lines.append(
                f"  {turn.number:>3} {_bar(turn.input_tokens, peak)} "
                f"{turn.input_tokens:>7,}{mark}"
            )

    # -- did the plane know what it was sending? ------------------------------
    gaps = [(t.number, t.estimate_gap, t.input_tokens)
            for t in real_turns if t.estimate_gap is not None]
    if gaps:
        worst = min(gaps, key=lambda g: g[1])
        allowance = next(
            (t.context.get("allowance") for t in real_turns if t.context), 0
        )
        lines += [
            "",
            "what the context plane believed vs what it was charged",
            f"  allowance {allowance:,}   worst under-estimate {worst[1]:+,} "
            f"on turn {worst[0]} (charged {worst[2]:,})",
        ]
        if worst[1] < 0:
            # The shape of every context failure this project has had.
            lines.append(
                "  ! the plane believed the request was smaller than it was, so "
                "compaction fires late or never"
            )

    # -- what the Gate refused ------------------------------------------------
    if exp.denials:
        lines += ["", "what the Gate refused"]
        for rule, n in exp.denials.most_common():
            lines.append(f"  {n:>4}  {rule}")
    else:
        lines += ["", "what the Gate refused    nothing"]

    # -- how it ended ---------------------------------------------------------
    breakers = [(t.number, k, d) for t in exp.turns for k, d in t.breakers]
    if breakers:
        lines += ["", "breakers"]
        for number, kind, detail in breakers[-8:]:
            lines.append(f"  turn {number:>3}  {kind}: {detail[:96]}")
    return "\n".join(lines)


def render_job(explanations: Sequence[tuple[str, Explanation]]) -> str:
    """One line per trial, for a whole job."""
    lines = [
        f"{'task':<34} {'stopped':<22} {'turns':>5} {'tokens':>9} "
        f"{'ok/act':>7} {'refused':>7}  verdict"
    ]
    for name, exp in explanations:
        settled = sum(len(t.settled) for t in exp.real_turns)
        ok = sum(sum(1 for _, good in t.settled if good) for t in exp.real_turns)
        verdict = (
            "SOLVED" if exp.reward == 1.0
            else "-" if exp.reward == 0.0
            else "ungraded"
        )
        lines.append(
            f"{name[:34]:<34} {exp.stop_reason[:22]:<22} {len(exp.real_turns):>5} "
            f"{exp.total_tokens:>9,} {f'{ok}/{settled}':>7} "
            f"{sum(exp.denials.values()):>7}  {verdict}"
        )
    return "\n".join(lines)


def find_trials(root: str | Path) -> list[tuple[str, Path]]:
    """Every trial directory under a job directory, in name order.

    Direct children first, because that is a single-step Harbor job's layout and
    the common case. Falling back to a recursive search matters for multi-step
    trials, which relocate their output into `steps/<name>/agent/` — a layout
    the direct scan misses entirely, and silently.
    """
    base = Path(root)
    if not base.is_dir():
        return []
    if _ledger_path(base) is not None:
        return [(base.name, base)]

    direct = [
        (d.name, d)
        for d in sorted(base.iterdir())
        if d.is_dir() and _ledger_path(d) is not None
    ]
    if direct:
        return direct

    nested: list[tuple[str, Path]] = []
    for ledger in sorted(base.rglob("ledger.db")):
        trial = ledger.parent.parent if ledger.parent.name == "agent" else ledger.parent
        label = str(trial.relative_to(base)).replace("\\", "/")
        nested.append((label, trial))
    return nested
