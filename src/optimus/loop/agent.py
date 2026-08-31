"""The loop. Everything before this drove tools; nothing drove the loop.

`STATUS.md` opened with "no loop and no model call" for three milestones on
purpose — a loop written before the Gate, the Ledger, the context plane and the
meter would have had to grow its own versions of all four, and they would have
been the bad versions. What is left here is therefore small, and every line of it
is one of the four planes being *used*:

    authorize -> act -> observe -> account -> compact -> repeat

**The three things this loop does that a `while True` around a model call does
not:**

1. **It breaks doom loops.** A turn that calls no tool is counted, named and
   acted on. LangChain's no-action middleware moved Terminal-Bench 52.8 -> 66.5
   on harness work alone (`research.md`); repeating an identical action is the
   same pathology with a tool call attached, so it is broken the same way. Both
   breakers write to the Ledger, so "the agent stalled" is a row, not a vibe.

2. **It bounds context instead of accumulating it.** Every turn ends at
   `ContextWindow.ensure_fits`, which is priority-ordered, dependency-aware, and
   refuses a compaction that would lose an invariant. Observations are clamped
   head-and-tail before they are ever admitted, because unbounded tool output is
   where the 20-40x token spread in `research.md` §2.1 actually comes from.

3. **It meters the model call itself.** `effect.settled` covers tools; a model
   call is not an effect, so it gets its own `model.call` row carrying the
   provider's own usage numbers. Invariant 5 says everything is metered, and the
   single largest line item cannot be the exception.

**What it deliberately does not do:** decide whether the task was solved. The
loop knows it stopped; it does not know it succeeded. `run.finished` records
`solved: null` and the verifier's answer is joined in later
(`report.py`). A harness that scores itself is not a harness, it is a claim.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any, Protocol

from ..context.episodes import Episode, EpisodeKind
from ..context.window import CompactionRefused, ContextWindow
from ..gate.gate import Gate
from ..ledger.events import Meter, TrustLabel
from ..surface.control import Control, SteerKind
from ..surface.events import Bus, EventKind
from ..tools.budget import ToolBudgetPolicy, ToolSpec
from .llm import LLM, ModelReply, ToolCall, Usage

#: Ledger row kind -> bus event kind, for the rows a surface can render. A row
#: absent from this map is written to the ledger and not published, which is the
#: right default: the ledger is the record, and the bus is a view of the parts
#: of it someone is watching.
_BUS_KINDS: dict[str, EventKind] = {
    "run.started": EventKind.RUN_STARTED,
    "run.finished": EventKind.RUN_FINISHED,
    "model.call": EventKind.MODEL_CALL,
    "context.turn": EventKind.CONTEXT_TURN,
    "context.compacted": EventKind.CONTEXT_COMPACTED,
    "loop.breaker": EventKind.BREAKER,
}

#: Ceiling on the estimator correction. Beyond this something is wrong with
#: the estimator itself rather than with the tokenizer, and silently
#: multiplying by 20 would just refuse to run at all.
_MAX_CALIBRATION = 4.0

#: Compact to this share of the allowance, not to the brim. Leaving no
#: headroom means the next observation overflows again immediately.
_COMPACTION_HEADROOM = 0.7

#: The standing rules. These become `INVARIANT` episodes, which the context
#: plane will refuse to compact away — which is the entire point of the kind
#: existing (`research.md` §4.5, "Governance Decay").
DEFAULT_INVARIANTS: tuple[str, ...] = (
    "Every action you take is authorized, recorded and metered. A refusal comes "
    "back to you as an ordinary observation; read it, and choose a different "
    "action rather than repeating the refused one.",
    "Never modify, read or reason about the grading harness, its test files, or "
    "anything under a verifier or solution directory. Solve the task itself.",
    "Prefer one command that establishes several facts over several commands "
    "that each establish one. Every round trip is billed.",
)

SYSTEM_PROMPT = """You are Optimus, working in a terminal on one task.

You act only through the provided tools. Think briefly, then call a tool; a turn \
that calls no tool accomplishes nothing and is counted against you.

Working method:
- Establish facts before changing things, and prefer one command that answers \
several questions to several that each answer one.
- Read errors carefully. If a command fails twice the same way, change approach \
rather than repeating it.
- When the task is complete, verify it yourself, then call `finish` with a short \
statement of what you did and how you checked it.

You cannot ask a human anything. There is nobody there."""


class ToolPlane(Protocol):
    """What the loop needs from a tool surface.

    `tools.remote.RemoteTools` and `tools.std.GatedTools` both satisfy this, so
    the same loop runs against a Harbor container and against this machine. The
    only difference either can express is what a denial says.
    """

    def bash(self, command: str, *, timeout_s: float | None = ...) -> dict[str, Any]: ...
    def read_file(self, path: str, *, offset: int = ..., limit: int = ...) -> dict[str, Any]: ...
    def write_file(self, path: str, content: str) -> dict[str, Any]: ...


# --------------------------------------------------------------------------
# the tool surface the model sees
# --------------------------------------------------------------------------

TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Run a shell command in the workspace and return its exit code, "
                "stdout and stderr. Output is truncated in the middle if long."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command."},
                    "timeout_s": {
                        "type": "number",
                        "description": "Seconds before the command is killed. Default 120.",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file from the workspace, by line window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "First line, 0-based."},
                    "limit": {"type": "integer", "description": "How many lines. Default 400."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write a whole text file, creating parent directories. Use this "
                "instead of a shell heredoc: it is cheaper and cannot be mangled "
                "by quoting."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "Declare the task complete. Call this only after verifying the "
                "result yourself."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "What you did and how you verified it.",
                    }
                },
                "required": ["summary"],
            },
        },
    },
)

_BUDGET_SPECS = tuple(
    ToolSpec(
        name=s["function"]["name"],
        description=s["function"]["description"],
        schema=s["function"]["parameters"],
    )
    for s in TOOL_SCHEMAS
)


# --------------------------------------------------------------------------
# limits and outcome
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class LoopLimits:
    max_turns: int = 60
    #: Consecutive turns with no tool call before the run is abandoned. One
    #: idle turn gets a correction; a third means the model is not going to
    #: recover and every further turn is pure cost.
    max_no_action_streak: int = 3
    #: Identical (tool, arguments) repeats before the loop says so, and again
    #: before it gives up.
    max_repeat_action: int = 3
    max_wall_s: float = 1_800.0
    #: 0 disables. A ceiling in dollars is the only limit a finance team reads.
    max_cost_usd: float = 0.0
    #: Consecutive gate refusals before the run is abandoned. An agent whose
    #: every action is refused cannot succeed, and each further turn is pure
    #: cost: a real trial spent thirty turns and 232K tokens against an expired
    #: envelope — correctly refused every time, with no way to notice.
    max_consecutive_denials: int = 6
    #: Consecutive *fatal* provider failures tolerated. One is enough: a 404 for
    #: a model that does not exist, or a rejected key, returns the same answer
    #: however many times it is asked. A real run spent ten minutes and three
    #: attempts discovering that.
    max_provider_errors: int = 1
    #: Consecutive *transient* failures — rate limits, 503s, dropped
    #: connections — tolerated before giving up. Much higher, because a run that
    #: has already done real work is worth waiting for: the same real run died
    #: on turn 12 of 14 to a rate limit, having completed eleven good turns.
    max_transient_errors: int = 8
    #: Exponential backoff between transient retries, capped.
    retry_backoff_s: float = 4.0
    max_backoff_s: float = 120.0
    #: Characters of a single observation admitted to context. The middle is
    #: dropped, never the head or the tail: a compiler names the file at the top
    #: and the failure at the bottom.
    observation_chars: int = 6_000
    #: Turns-remaining marks at which the model is told how much runway is left.
    #:
    #: The ten-task run found the model flying blind: two solved tasks ran bash
    #: to turn 40 and never called `finish`, because nothing in the prompt or
    #: the conversation ever said how many turns there were. A third called
    #: `finish` on turn 40 — the last one available. The model was not refusing
    #: to conclude; it had no idea it was nearly out of road.
    #:
    #: Two marks rather than one per turn: a notice every turn would add forty
    #: messages to a budget this project spends real effort bounding, and the
    #: information only changes behaviour near the end.
    budget_notices: tuple[int, ...] = (10, 3)


@dataclass(slots=True)
class TurnRecord:
    """One turn, in the shape a trajectory writer needs."""

    turn: int
    timestamp: str
    text: str = ""
    reasoning: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    results: list[tuple[str, str]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    breaker: str = ""
    error: str = ""


@dataclass(slots=True)
class RunOutcome:
    """What the loop knows when it stops. Notably absent: whether it worked."""

    run_id: str
    turns: int = 0
    stop_reason: str = ""
    summary: str = ""
    usage: Usage = field(default_factory=Usage)
    no_action_turns: int = 0
    repeated_actions: int = 0
    provider_errors: int = 0
    gate_denials: int = 0
    approvals_required: int = 0
    compactions: int = 0
    tool_calls: int = 0
    wall_ms: int = 0
    #: Wall time spent waiting out transient provider failures. Reported apart
    #: from `wall_ms` so a slow run is not mistaken for a slow harness.
    backoff_ms: int = 0
    steps: list[TurnRecord] = field(default_factory=list)

    @property
    def finished_cleanly(self) -> bool:
        return self.stop_reason == "finished"

    def render(self) -> str:
        return (
            f"run={self.run_id} stop={self.stop_reason} turns={self.turns} "
            f"tokens={self.usage.total_tokens:,} (cache {self.usage.cached_tokens:,}) "
            f"cost=${self.usage.cost_usd:.4f} no_action={self.no_action_turns} "
            f"repeats={self.repeated_actions} denials={self.gate_denials} "
            f"approvals_needed={self.approvals_required} "
            f"provider_errors={self.provider_errors} backoff={self.backoff_ms}ms"
        )


# --------------------------------------------------------------------------
# text handling
# --------------------------------------------------------------------------

def clamp(text: str, limit: int) -> str:
    """Drop the middle, never the ends.

    A tail-truncating clamp throws away the error and keeps the banner; a
    head-truncating one throws away the command that produced it. Build output
    puts the identifying information at the top and the reason for failure at the
    bottom, so the only safe thing to lose is the part in between — and the note
    says exactly how much was lost.
    """
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    dropped = len(text) - limit
    return (
        text[:head]
        + f"\n...[{dropped:,} characters elided from the middle]...\n"
        + text[-tail:]
    )


def _digest(name: str, arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps([name, arguments], sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _observation_text(result: dict[str, Any]) -> str:
    """Render a tool result for the model. Compact, and never a Python repr."""
    if "stdout" in result or "exit_code" in result:
        parts = [f"exit_code={result.get('exit_code')}"]
        if result.get("timed_out"):
            parts.append("TIMED OUT")
        out = (result.get("stdout") or "").rstrip()
        err = (result.get("stderr") or "").rstrip()
        body = "\n".join(p for p in (out, ("stderr:\n" + err) if err else "") if p)
        return " ".join(parts) + ("\n" + body if body else "\n(no output)")
    return json.dumps(result, indent=None, default=str)


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

class AgentLoop:
    """Drives a model against a tool plane, under a Gate, inside a budget."""

    def __init__(
        self,
        *,
        gate: Gate,
        tools: ToolPlane,
        window: ContextWindow,
        llm: LLM,
        limits: LoopLimits | None = None,
        run_id: str = "",
        system_prompt: str = SYSTEM_PROMPT,
        invariants: Sequence[str] = DEFAULT_INVARIANTS,
        stop: threading.Event | None = None,
        bus: Bus | None = None,
        control: Control | None = None,
    ):
        self.gate = gate
        self.tools = tools
        self.window = window
        self.llm = llm
        self.limits = limits or LoopLimits()
        self.run_id = run_id or gate.run_id or "run"
        self.system_prompt = system_prompt
        self.invariants = tuple(invariants)
        # M4. Both optional, and both cheap when nobody is attached: a benchmark
        # trial has nobody watching and nobody steering, and must not pay for
        # the possibility that someone might be.
        self.control = control
        self.bus = bus or Bus(run_id=self.run_id)
        # Cooperative cancellation. The loop runs in a worker thread under the
        # Harbor adapter, and `asyncio.to_thread` cannot be cancelled — so
        # without this the loop keeps driving a GPU long after the harness that
        # started it has given up on the trial and moved on. A real run did
        # exactly that: Harbor recorded a timeout at 900s and the loop carried
        # on to its own 1800s ceiling.
        #
        # A `Control` supplies this Event when one is not passed explicitly,
        # which is the whole of "a cancellation path that does not depend on the
        # adapter": the adapter still sets a bare Event, and a TUI, an HTTP
        # client or an ACP editor now sets the same bit through `Control`. One
        # bit, several doors — not a second mechanism that can disagree.
        self.stop = stop or (control.stop if control else None) or threading.Event()
        self._overhead: int | None = None
        #: Ratio between what the provider says a prompt cost and what this
        #: process estimated. Starts honest-but-blind at 1.0 and is corrected
        #: from the first real reply.
        self._calibration = 1.0
        self._last_estimate = 0
        #: The provider's own measurement of the last prompt, and the episode
        #: total at that moment, so growth since can be added to it.
        self._observed_prompt = 0
        self._observed_at = 0
        self._outcome = RunOutcome(run_id=self.run_id)

    # -- prompt ---------------------------------------------------------------

    def _system_content(self) -> Any:
        text = self.system_prompt
        # How much road there is. Constant for the run, so it stays part of the
        # stable cache prefix and costs one line once rather than per turn.
        #
        # Saying it at all is the point: without this the model cannot tell
        # turn 3 from turn 39, and the ten-task run showed exactly what that
        # produces — solved tasks running bash into the ceiling because nothing
        # ever indicated the end was near.
        text += (
            f"\n\nYou have at most {self.limits.max_turns} turns. The run stops "
            "there whether or not you have called `finish`, and work left "
            "unfinished at that point is lost."
        )
        if self.invariants:
            text += "\n\nStanding rules:\n" + "\n".join(
                f"- {rule}" for rule in self.invariants
            )
        if getattr(self.llm, "wants_cache_hints", False):
            # A stable prefix is the difference between a 40-turn run costing
            # O(n) and O(n^2) in input tokens. The marker is inert on providers
            # that cache by prefix automatically.
            return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
        return text

    def messages(self) -> list[dict[str, Any]]:
        """Render the surviving episodes as a well-formed conversation.

        The repair pass at the end exists because eviction and the chat wire
        format disagree about what an atom is. `ContextWindow` evicts an action
        and its observation independently — deliberately, since they carry
        different salience — but every provider rejects an assistant tool call
        with no matching result, or a result with no matching call. So pairing is
        re-established here, deterministically, by demoting the orphaned half to
        plain text rather than by weakening the eviction policy that made the
        right call in the first place.
        """
        rendered: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_content()}
        ]
        raw: list[dict[str, Any]] = []
        for ep in self.window.episodes:
            # Episodes already carried by the system block are budgeted here and
            # sent once. Skipping this is a quiet, expensive bug: the standing
            # rules would go out in the system prompt *and* again as user turns,
            # every turn, in the one part of the prompt caching does not help.
            if ep.meta.get("in_system"):
                continue
            message = ep.meta.get("message")
            raw.append(
                dict(message) if isinstance(message, dict)
                else {"role": "user", "content": ep.render()}
            )

        result_ids = {
            m.get("tool_call_id") for m in raw if m.get("role") == "tool"
        }
        call_ids = {
            tc.get("id")
            for m in raw
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        }

        for message in raw:
            role = message.get("role")
            if role == "assistant" and message.get("tool_calls"):
                kept = [tc for tc in message["tool_calls"] if tc.get("id") in result_ids]
                if kept:
                    rendered.append({**message, "tool_calls": kept})
                    continue
                names = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in message["tool_calls"]
                )
                text = message.get("content") or ""
                rendered.append(
                    {"role": "assistant",
                     "content": f"{text}\n[called {names}; result compacted away]".strip()}
                )
            elif role == "tool" and message.get("tool_call_id") not in call_ids:
                rendered.append(
                    {"role": "user",
                     "content": f"[result of a compacted call]\n{message.get('content', '')}"}
                )
            else:
                rendered.append(message)
        return rendered

    # -- episodes -------------------------------------------------------------

    def _push(
        self,
        kind: EpisodeKind,
        content: str,
        *,
        message: dict[str, Any] | None = None,
        depends_on: Sequence[str] = (),
        pinned: bool = False,
        in_system: bool = False,
    ) -> Episode:
        meta: dict[str, Any] = {}
        if message:
            meta["message"] = message
        if in_system:
            meta["in_system"] = True
        return self.window.add(
            Episode(
                kind=kind,
                content=content,
                depends_on=frozenset(depends_on),
                pinned=pinned,
                meta=meta,
            )
        )

    # -- ledger ---------------------------------------------------------------

    def _record(self, kind: str, payload: dict[str, Any], trust: TrustLabel) -> None:
        self.gate.chain.append(
            kind, {**payload, "run_id": self.run_id}, trust
        )
        # And mirror it onto the bus, from the one place that writes the ledger.
        #
        # This is deliberately not a second set of publish calls scattered
        # through the run. Two renderers that each derive "what happened" from
        # their own reading of the loop is exactly how the same trial came to
        # report 40 turns in one view and 41 in another. Here a live surface and
        # a post-hoc `optimus why` are reading the same row, under the same
        # name, and there is no arithmetic in between for them to disagree on.
        event_kind = _BUS_KINDS.get(kind)
        if event_kind is not None:
            # `turn` is lifted out of the payload rather than duplicated into
            # it: the event addresses a turn, and the payload describes what
            # happened in it. Leaving it in both places is two fields that can
            # drift apart, which is the whole failure this mirror exists to
            # avoid, reintroduced one level down.
            rest = {k: v for k, v in payload.items() if k != "turn"}
            self.bus.publish(
                event_kind, turn=int(payload.get("turn") or 0), payload=rest
            )

    def _record_model_call(self, turn: int, reply: ModelReply) -> None:
        """The one row that makes tokens-per-solved-task a measurement.

        `effect.settled` covers tools. A model call is not an effect and would
        otherwise be the single largest unmetered line item in the system.
        """
        meter = Meter(
            input_tokens=reply.usage.input_tokens,
            output_tokens=reply.usage.output_tokens,
            wall_ms=reply.usage.wall_ms,
            # `idled`, not `not acted`: a call that failed did not idle. Counting
            # it as an idle turn is how the headline metric quietly stops
            # meaning what it says.
            no_action=reply.idled,
            extra={
                "cached_tokens": reply.usage.cached_tokens,
                "cost_usd": reply.usage.cost_usd,
                "provider_error": bool(reply.error),
                "retryable": reply.retryable,
            },
        )
        self._record(
            "model.call",
            {
                "turn": turn,
                "model": self.llm.model,
                "finish_reason": reply.finish_reason,
                "tools": [c.name for c in reply.tool_calls],
                "error": reply.error,
                "meter": meter.as_payload(),
            },
            TrustLabel.UNTRUSTED_MODEL_OUTPUT,
        )

    def _record_context(self, turn: int) -> None:
        """What the context plane believed, every turn, next to what was true.

        Four consecutive bugs (`STATUS.md` M3-13 to M3-16) were all one
        component measuring faithfully in a unit nothing downstream was billed
        in, and each took a separate run and a hand-written ledger query to
        find. They were only ever *one* run apart from being obvious: put the
        estimate and the provider's own number in the same row, every turn, and
        a gap that opens between them is visible on the first trial rather than
        the fourth.

        `context.compacted` already recorded these, but only on turns where
        compaction ran — which on the runs that mattered was none of them.
        """
        self._record(
            "context.turn",
            {
                "turn": turn,
                # What we think the next request costs, corrected.
                "estimated": self.prompt_tokens(),
                # The same estimate before correction, so the correction itself
                # is auditable rather than folded invisibly into one number.
                "raw_estimate": self._last_estimate,
                "calibration": round(self._calibration, 4),
                # What the provider charged for the last one. Truth.
                "observed_last": self._observed_prompt,
                "allowance": self.window.budget.fillable,
                "episode_tokens": self.window.used(),
                "episodes": len(self.window.episodes),
            },
            TrustLabel.TRUSTED_LOCAL,
        )

    def _breaker(self, kind: str, turn: int, detail: str) -> None:
        self._record(
            "loop.breaker", {"kind": kind, "turn": turn, "detail": detail},
            TrustLabel.TRUSTED_LOCAL,
        )

    # -- turn budget ----------------------------------------------------------

    def _budget_notice(self, turn: int, announced: set[int]) -> None:
        """Tell the model how much runway is left, once per mark.

        Deliberately **only the facts**. This does not say "you are probably
        done", does not suggest the task looks complete, and does not ask the
        model to wrap up — it reports the turn count and what happens at the
        ceiling, and leaves the judgement where it belongs.

        That restraint is not politeness, it is the difference between closing
        an information gap and steering the outcome. In the same run that showed
        two solved tasks never calling `finish`, a third task called `finish` at
        turn 19 and had **not** solved it. A nudge that encourages concluding
        would make that failure more common, and every premature `finish` on a
        task the model would have solved by turn 30 is a solve thrown away.
        Finishing early buys tokens, never score: the verifier grades the
        container either way, which is why both tasks that ran out of turns were
        still marked solved. So the honest intervention is to hand over a number
        the model cannot otherwise see and let it decide.
        """
        remaining = self.limits.max_turns - turn + 1
        for mark in sorted(self.limits.budget_notices, reverse=True):
            # A mark at or above the whole budget would fire on turn 1, which is
            # noise rather than a warning.
            if mark >= self.limits.max_turns or mark in announced:
                continue
            if remaining > mark:
                continue
            announced.add(mark)
            text = (
                f"[harness] Turn {turn} of {self.limits.max_turns}; "
                f"{remaining} remain. At turn {self.limits.max_turns} the run "
                "stops whether or not you have called `finish`. If the task is "
                "complete and you have checked it, call `finish`. If it is not, "
                "spend what is left on the part that matters most."
            )
            self._push(
                EpisodeKind.OBSERVATION, text,
                message={"role": "user", "content": text},
            )
            self._record(
                "loop.budget_notice",
                {"turn": turn, "remaining": remaining, "mark": mark},
                TrustLabel.TRUSTED_LOCAL,
            )
            return

    # -- steering (M4) --------------------------------------------------------

    def _cancel_detail(self) -> str:
        """Who stopped this, and why, when anyone said."""
        if self.control and self.control.cancel_reason:
            return self.control.cancel_reason
        return "the harness asked the loop to stop"

    def _absorb_steers(self, turn: int) -> None:
        """Read what an operator sent, and put it in front of the model.

        A steer enters the window as an ordinary `OBSERVATION` episode carrying
        a `user` message, which means it is budgeted, compactable and auditable
        by exactly the machinery that already handles everything else. It is
        emphatically *not* an invariant: an operator's mid-run aside is not a
        standing rule, and quietly promoting it to one would make a typo
        uncompactable for the rest of the run.

        The trust label is the interesting part. A steer arrives over a socket
        from a surface, so it is not `TRUSTED_LOCAL`, but it is a human's words
        and not the model's, so it is not `UNTRUSTED_MODEL_OUTPUT` either. It is
        `TRUSTED_USER` — the same label the original instruction carries, which
        is what it actually is: more instruction, arriving late.
        """
        if self.control is None:
            return
        steers = self.control.drain()
        if not steers:
            return
        for steer in steers:
            if steer.kind is SteerKind.CANCEL:
                # The stop bit is already set; nothing to inject.
                continue
            message = steer.as_message()
            self._push(
                EpisodeKind.OBSERVATION,
                message["content"],
                message=message,
            )
            self._record(
                "loop.steer",
                {
                    "turn": turn,
                    "kind": steer.kind.name.lower(),
                    "source": steer.source,
                    "text": steer.text[:2_000],
                },
                TrustLabel.TRUSTED_USER,
            )
            self.bus.publish(
                EventKind.STEERED, turn=turn, payload=steer.as_dict()
            )

    # -- the run --------------------------------------------------------------

    def run(self, instruction: str, *, environment: str = "") -> RunOutcome:
        started = time.monotonic()
        out = self._outcome

        decision = ToolBudgetPolicy(self.window.budget).choose(
            _BUDGET_SPECS, expected_calls=1, count_tokens=self.window.count_tokens
        )
        self._record(
            "run.started",
            {
                "model": self.llm.model,
                "instruction": instruction[:4_000],
                "limits": {
                    "max_turns": self.limits.max_turns,
                    "max_wall_s": self.limits.max_wall_s,
                    "max_cost_usd": self.limits.max_cost_usd,
                },
                "tool_mode": str(decision.mode),
                "tool_schema_tokens": decision.schema_tokens,
                "tool_allowance": decision.allowance,
                "envelope": (
                    self.gate.envelope.envelope_id if self.gate.envelope else None
                ),
            },
            TrustLabel.TRUSTED_USER,
        )

        # The task contract and the standing rules are never evictable. That is
        # a property of the kind, not of a flag set here.
        contract = self._push(
            EpisodeKind.CONTRACT, instruction,
            message={"role": "user", "content": instruction},
        )
        for rule in self.invariants:
            # In the window so the budget knows what they cost and the validator
            # will refuse to compact them away; not in the message list, because
            # `_system_content` already sends them.
            self._push(EpisodeKind.INVARIANT, rule, in_system=True)
        if environment:
            self._push(
                EpisodeKind.ENVIRONMENT,
                environment,
                message={
                    "role": "user",
                    "content": f"<environment>\n{environment}\n</environment>",
                },
                depends_on=[contract.id],
            )

        no_action_streak = 0
        denial_streak = 0
        repeat_streak = 0
        last_digest = ""
        transient_streak = 0
        announced_marks: set[int] = set()

        for turn in range(1, self.limits.max_turns + 1):
            out.turns = turn
            if self.stop.is_set():
                out.stop_reason = "cancelled"
                self._breaker("cancelled", turn, self._cancel_detail())
                break
            elapsed = time.monotonic() - started
            if elapsed > self.limits.max_wall_s:
                out.stop_reason = "wall_clock"
                self._breaker("wall_clock", turn, f"{elapsed:.0f}s elapsed")
                break
            if self.limits.max_cost_usd and out.usage.cost_usd >= self.limits.max_cost_usd:
                out.stop_reason = "cost_ceiling"
                self._breaker("cost_ceiling", turn, f"${out.usage.cost_usd:.4f}")
                break

            # Ahead of the compaction check on purpose. The notice is context
            # like any other; adding it afterwards would let this turn's request
            # exceed the allowance the plane just finished enforcing. Small,
            # but this project has four separate findings (M3-13..16) about
            # numbers that were correct everywhere except where they met.
            self._budget_notice(turn, announced_marks)

            self._compact_if_needed(turn)

            # Anything an operator sent while the last turn was in flight is
            # read here, at the boundary, before the model is asked anything.
            # A correction that arrives during turn 12 is acted on in turn 13,
            # which is the earliest point at which acting on it is coherent.
            self._absorb_steers(turn)
            if self.stop.is_set():
                out.stop_reason = "cancelled"
                self._breaker("cancelled", turn, self._cancel_detail())
                break

            self.bus.publish(EventKind.TURN_STARTED, turn=turn)
            record = TurnRecord(turn=turn, timestamp=_now_iso())
            self._last_estimate = self._raw_prompt_tokens()
            self._record_context(turn)
            reply = self.llm.complete(self.messages(), TOOL_SCHEMAS)
            if not reply.error and reply.usage.input_tokens:
                self._calibrate(self._last_estimate, reply.usage.input_tokens)
                self._observed_prompt = reply.usage.input_tokens
                self._observed_at = self.window.used()
            out.usage = out.usage.merged(reply.usage)
            record.usage = reply.usage
            record.text = reply.text
            record.reasoning = reply.reasoning
            self._record_model_call(turn, reply)

            # -- provider failure ---------------------------------------------
            if reply.error:
                out.provider_errors += 1
                record.error = reply.error
                record.breaker = "provider_retry" if reply.retryable else "provider_fatal"
                out.steps.append(record)

                if not reply.retryable:
                    # Nothing to wait for. Asking a fourth time for a model that
                    # does not exist produces the fourth identical 404.
                    out.stop_reason = "provider_error"
                    self._breaker("provider_fatal", turn, reply.error)
                    break

                transient_streak += 1
                if transient_streak >= self.limits.max_transient_errors:
                    out.stop_reason = "provider_unavailable"
                    self._breaker(
                        "provider_unavailable", turn,
                        f"{transient_streak} consecutive transient failures: {reply.error}",
                    )
                    break

                # Nothing is pushed to the context: a rate limit is the
                # harness's problem, not something the model needs to reason
                # about, and telling it would spend tokens to no purpose.
                # Honour what the provider asked for, when it said. Our own
                # exponential curve is a fallback for providers that do not, and
                # the floor for ones that ask for less than we would already
                # wait — a real 429 carried "Please retry in 59.28s".
                delay = min(
                    self.limits.retry_backoff_s * (2 ** (transient_streak - 1)),
                    self.limits.max_backoff_s,
                )
                delay = max(delay, min(reply.retry_after_s, self.limits.max_backoff_s))
                self._breaker(
                    "provider_retry", turn,
                    f"transient failure {transient_streak}, waiting {delay:.0f}s"
                    + (f" (provider asked for {reply.retry_after_s:.0f}s)"
                       if reply.retry_after_s else ""),
                )
                out.backoff_ms += int(delay * 1000)
                time.sleep(delay)
                continue
            transient_streak = 0

            # -- no action -----------------------------------------------------
            if not reply.acted:
                no_action_streak += 1
                out.no_action_turns += 1
                record.breaker = "no_action"
                out.steps.append(record)
                self._push(
                    EpisodeKind.THOUGHT, reply.text or "(empty turn)",
                    message={"role": "assistant", "content": reply.text or "..."},
                )
                if no_action_streak >= self.limits.max_no_action_streak:
                    out.stop_reason = "stalled"
                    self._breaker(
                        "no_action", turn,
                        f"{no_action_streak} consecutive turns without a tool call",
                    )
                    break
                self._breaker("no_action", turn, f"streak={no_action_streak}")
                self._push(
                    EpisodeKind.ERROR,
                    "no tool was called; the turn accomplished nothing",
                    message={
                        "role": "user",
                        "content": (
                            "[harness] That turn called no tool, so nothing happened "
                            "and it was billed anyway. Either call a tool now, or "
                            "call `finish` if the task is genuinely complete."
                        ),
                    },
                )
                continue
            no_action_streak = 0

            # -- act ------------------------------------------------------------
            assistant_message = _assistant_message(reply)
            action = self._push(
                EpisodeKind.ACTION,
                "; ".join(f"{c.name}({_brief(c.arguments)})" for c in reply.tool_calls),
                message=assistant_message,
            )
            record.calls = list(reply.tool_calls)

            finished = False
            for call in reply.tool_calls:
                out.tool_calls += 1
                if call.name == "finish":
                    out.summary = str(call.arguments.get("summary", "")).strip()
                    finished = True
                    record.results.append((call.call_id, "acknowledged"))
                    self._push(
                        EpisodeKind.OBSERVATION, "finish acknowledged",
                        message={"role": "tool", "tool_call_id": call.call_id,
                                 "content": "acknowledged"},
                        depends_on=[action.id],
                    )
                    continue

                self.bus.publish(
                    EventKind.TOOL_CALL, turn=turn,
                    call_id=call.call_id, name=call.name,
                    brief=_brief(call.arguments),
                )
                result = self._dispatch(call)
                text = clamp(_observation_text(result), self.limits.observation_chars)
                record.results.append((call.call_id, text))
                if result.get("denied"):
                    out.gate_denials += 1
                    denial_streak += 1
                    if result.get("verdict") == "needs_approval":
                        out.approvals_required += 1
                    # A parked action and a refused one look identical to the
                    # loop — both come back as an observation and the run
                    # continues. They are not identical to a *surface*: one of
                    # them is a question somebody could answer. ACP has a
                    # round trip for exactly this.
                    self.bus.publish(
                        EventKind.GATE_PARKED
                        if result.get("verdict") == "needs_approval"
                        else EventKind.GATE_DENIED,
                        turn=turn,
                        call_id=call.call_id, name=call.name,
                        verdict=result.get("verdict", ""),
                        reason=result.get("reason", "") or text[:500],
                    )
                else:
                    denial_streak = 0
                self.bus.publish(
                    EventKind.TOOL_RESULT, turn=turn,
                    call_id=call.call_id, name=call.name,
                    denied=bool(result.get("denied")),
                    exit_code=result.get("exit_code"),
                    # Clamped hard: a surface renders a preview and reads the
                    # ledger for the rest. The bus is not a transport for a
                    # 6,000-character build log times four subscribers.
                    preview=text[:1_000],
                )
                kind = (
                    EpisodeKind.ERROR
                    if result.get("denied") or result.get("error")
                    or result.get("exit_code") not in (0, None)
                    else EpisodeKind.OBSERVATION
                )
                self._push(
                    kind, text,
                    message={"role": "tool", "tool_call_id": call.call_id, "content": text},
                    depends_on=[action.id],
                )

            if denial_streak >= self.limits.max_consecutive_denials:
                out.stop_reason = "blocked"
                self._breaker(
                    "blocked", turn,
                    f"{denial_streak} consecutive refusals; the run cannot succeed",
                )
                out.steps.append(record)
                break

            # -- repetition -----------------------------------------------------
            digest = _digest(
                reply.tool_calls[0].name, reply.tool_calls[0].arguments
            )
            if digest == last_digest:
                repeat_streak += 1
            else:
                repeat_streak, last_digest = 0, digest
            if repeat_streak and repeat_streak % self.limits.max_repeat_action == 0:
                out.repeated_actions += 1
                record.breaker = "repeat"
                self._breaker("repeat", turn, f"identical action {repeat_streak + 1}x")
                self._push(
                    EpisodeKind.ERROR,
                    f"the same action has now run {repeat_streak + 1} times",
                    message={
                        "role": "user",
                        "content": (
                            f"[harness] You have run that exact command "
                            f"{repeat_streak + 1} times and the result has not "
                            "changed. Change approach; repeating it will not help."
                        ),
                    },
                )
            if repeat_streak >= self.limits.max_repeat_action * 2:
                out.stop_reason = "looping"
                self._breaker("looping", turn, f"identical action {repeat_streak + 1}x")
                out.steps.append(record)
                break

            out.steps.append(record)
            self.bus.publish(
                EventKind.TURN_FINISHED, turn=turn,
                tool_calls=len(record.calls),
                breaker=record.breaker,
                tokens=record.usage.total_tokens,
                cost_usd=out.usage.cost_usd,
            )
            if finished:
                out.stop_reason = "finished"
                break
        else:
            out.stop_reason = out.stop_reason or "max_turns"

        if not out.stop_reason:
            out.stop_reason = "max_turns"
        out.wall_ms = int((time.monotonic() - started) * 1000)

        self._record(
            "run.finished",
            {
                "stop_reason": out.stop_reason,
                "turns": out.turns,
                "summary": out.summary[:2_000],
                # Not a modest omission — the loop genuinely does not know. The
                # verifier decides, and `report.py` joins its answer to this row.
                "solved": None,
                "no_action_turns": out.no_action_turns,
                "repeated_actions": out.repeated_actions,
                "gate_denials": out.gate_denials,
                "approvals_required": out.approvals_required,
                "compactions": out.compactions,
                "wall_ms": out.wall_ms,
                "provider_errors": out.provider_errors,
                "backoff_ms": out.backoff_ms,
            },
            TrustLabel.TRUSTED_LOCAL,
        )
        return out

    def compactions_seen(self) -> int:
        return self._outcome.compactions

    def request_stop(self) -> None:
        """Ask the loop to stop at the next turn boundary.

        Cooperative on purpose: the loop finishes the turn it is in, records
        `run.finished`, and lets the caller write a receipt. Killing the thread
        would leave a half-written ledger and no numbers at all.
        """
        self.stop.set()

    # -- pieces ---------------------------------------------------------------

    def prompt_tokens(self) -> int:
        """What will actually be sent, corrected by what the provider last billed.

        The raw estimate is a token count over the serialised messages. It is
        systematically *low*, for two reasons that no amount of care in this
        process can fix: `litellm.token_counter` does not know a local model id
        and falls back to a generic tokenizer, and the server renders the
        conversation and the tool schemas through its own chat template, which
        is a different string from the JSON we can see.

        So the estimate is calibrated against the only authority that knows —
        the `prompt_tokens` the provider reports for the call we just made. A
        real local run sent 31,921 tokens into a 32,768 window believing it was
        under a 28,672 allowance, because the raw estimate was out by roughly a
        third. Calibration converges within a turn or two and is recorded, so
        the correction is auditable rather than a fudge factor.
        """
        estimate = int(self._raw_prompt_tokens() * self._calibration)
        return max(estimate, self._observed_floor())

    def _observed_floor(self) -> int:
        """The last prompt the provider actually measured, plus what grew since.

        A multiplicative correction is the wrong model and a real run proved it:
        the ratio was learned as 1.22 on an 838-token prompt, where a fixed
        ~185-token template overhead is 22% — and it is under 1% of a
        25,000-token one. Scaling a fraction that only ever applied at small
        sizes is how the estimate drifted back under the allowance while the
        real prompt climbed from 28,160 to 32,424 and the server refused.

        So the floor is anchored, not extrapolated: take the size the provider
        reported for the last call, and add the episode tokens added since.
        Compaction moves it back down by the same arithmetic, because the delta
        is signed. Ground truth plus a measured delta beats a ratio.
        """
        if not self._observed_prompt:
            return 0
        return max(0, self._observed_prompt + self.window.used() - self._observed_at)

    def _raw_prompt_tokens(self) -> int:
        """The uncorrected estimate: what this process can see for itself.

        `ContextWindow.used()` sums `episode.content`, which is the right unit
        for the *plane* and the wrong one for the *wire*. The rendered request
        also carries the system block, the standing rules, the tool schemas, and
        a JSON envelope around every message and tool call. On a real local run
        that gap was roughly 3,000 tokens: the window believed it was inside a
        24,768-token allowance while the server was receiving 28,025 and
        eventually refused with "Context size has been exceeded" — after zero
        compactions.

        Budgeting against one number and being billed on another is the exact
        failure this plane exists to prevent, so the loop measures the request.
        """
        body = json.dumps(self.messages(), default=str)
        return self.window.count_tokens(body) + self._fixed_overhead()

    def _calibrate(self, estimated: int, observed: int) -> None:
        """Correct the estimator against the provider's own count.

        Deliberately asymmetric. An under-estimate ends the run — the server
        refuses the request outright — while an over-estimate only compacts a
        little early, so a correction upward is taken immediately and a
        correction downward decays slowly. Clamped below at 1.0, because
        claiming the prompt is smaller than what we can already see would be
        arguing with arithmetic.
        """
        if estimated <= 0 or observed <= 0:
            return
        ratio = observed / estimated
        previous = self._calibration
        if ratio > previous:
            self._calibration = min(ratio, _MAX_CALIBRATION)
        else:
            self._calibration = max(1.0, previous * 0.9 + ratio * 0.1)
        if abs(self._calibration - previous) > 0.05:
            self._record(
                "context.calibrated",
                {"estimated": estimated, "observed": observed,
                 "from": round(previous, 3), "to": round(self._calibration, 3)},
                TrustLabel.TRUSTED_LOCAL,
            )

    def _fixed_overhead(self) -> int:
        """Tool schemas: sent every turn, counted by nothing else."""
        if self._overhead is None:
            self._overhead = self.window.count_tokens(
                json.dumps(list(TOOL_SCHEMAS), default=str)
            )
        return self._overhead

    def _compact_if_needed(self, turn: int) -> None:
        """Compact until the *rendered request* fits, not until the episodes do.

        Loops, because one compaction targets `budget.target` measured in
        episode tokens and the overhead means that can still leave the wire
        over budget. Bounded, so a window that cannot shrink further gives up
        rather than spinning.
        """
        allowance = self.window.budget.fillable
        for _ in range(4):
            rendered = self.prompt_tokens()
            if rendered <= allowance:
                return
            # Ask for eviction in the window's units, sized by how far the
            # *rendered* request has to fall. The fixed overhead does not
            # shrink, so aim well under the allowance rather than just under it
            # — otherwise every turn shaves a few tokens and immediately
            # overflows again.
            shrink_to = allowance * _COMPACTION_HEADROOM / max(rendered, 1)
            episode_target = int(self.window.used() * shrink_to)
            try:
                report = self.window.compact(target=episode_target)
            except CompactionRefused as exc:
                # The validator did its job. Refusing is correct and the run
                # continues over budget rather than silently losing an
                # invariant; the provider's own limit is the backstop.
                self._breaker("compaction_refused", turn, str(exc))
                return
            if not report.evicted:
                self._breaker(
                    "context_full", turn,
                    f"rendered request is {rendered} tokens against an allowance "
                    f"of {allowance}, and nothing further is evictable",
                )
                return
            self._outcome.compactions += 1
            self._record(
                "context.compacted",
                {
                    "turn": turn,
                    "evicted": report.evicted,
                    "episode_tokens_before": report.tokens_before,
                    "episode_tokens_after": report.tokens_after,
                    # The number that actually matters, and the one the old
                    # accounting never looked at.
                    "rendered_before": rendered,
                    "rendered_after": self.prompt_tokens(),
                    "allowance": allowance,
                    "by_kind": report.by_kind,
                    "summary_id": report.summary_id,
                },
                TrustLabel.TRUSTED_LOCAL,
            )

    def _dispatch(self, call: ToolCall) -> dict[str, Any]:
        """Model output -> tool plane. Every bad argument is an observation."""
        args = call.arguments
        if "__unparsed__" in args:
            return {"error": "your tool arguments were not valid JSON; resend them",
                    "raw": str(args["__unparsed__"])[:500]}
        try:
            match call.name:
                case "bash":
                    command = args.get("command")
                    if not isinstance(command, str) or not command.strip():
                        return {"error": "bash needs a non-empty 'command' string"}
                    timeout = args.get("timeout_s")
                    return self.tools.bash(
                        command,
                        timeout_s=float(timeout) if timeout is not None else None,
                    )
                case "read_file":
                    path = args.get("path")
                    if not isinstance(path, str) or not path:
                        return {"error": "read_file needs a 'path' string"}
                    return self.tools.read_file(
                        path,
                        offset=int(args.get("offset") or 0),
                        limit=int(args.get("limit") or 400),
                    )
                case "write_file":
                    path, content = args.get("path"), args.get("content")
                    if not isinstance(path, str) or not path:
                        return {"error": "write_file needs a 'path' string"}
                    if not isinstance(content, str):
                        return {"error": "write_file needs a 'content' string"}
                    return self.tools.write_file(path, content)
                case _:
                    return {"error": f"no tool named {call.name!r}"}
        except (ValueError, TypeError) as exc:
            return {"error": f"bad arguments for {call.name}: {exc}"}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _assistant_message(reply: ModelReply) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": reply.text or "",
        "tool_calls": [
            {
                "id": c.call_id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in reply.tool_calls
        ],
    }


def _brief(arguments: dict[str, Any], limit: int = 120) -> str:
    text = json.dumps(arguments, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()
