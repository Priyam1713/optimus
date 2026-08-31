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
from dataclasses import dataclass, field
from typing import Any, Protocol, Sequence

from ..context.episodes import Episode, EpisodeKind
from ..context.window import CompactionRefused, ContextWindow
from ..gate.gate import Gate
from ..ledger.events import Meter, TrustLabel
from ..tools.budget import ToolBudgetPolicy, ToolSpec
from .llm import LLM, ModelReply, ToolCall, Usage

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
    ):
        self.gate = gate
        self.tools = tools
        self.window = window
        self.llm = llm
        self.limits = limits or LoopLimits()
        self.run_id = run_id or gate.run_id or "run"
        self.system_prompt = system_prompt
        self.invariants = tuple(invariants)
        # Cooperative cancellation. The loop runs in a worker thread under the
        # Harbor adapter, and `asyncio.to_thread` cannot be cancelled — so
        # without this the loop keeps driving a GPU long after the harness that
        # started it has given up on the trial and moved on. A real run did
        # exactly that: Harbor recorded a timeout at 900s and the loop carried
        # on to its own 1800s ceiling.
        self.stop = stop or threading.Event()
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

    def _breaker(self, kind: str, turn: int, detail: str) -> None:
        self._record(
            "loop.breaker", {"kind": kind, "turn": turn, "detail": detail},
            TrustLabel.TRUSTED_LOCAL,
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

        for turn in range(1, self.limits.max_turns + 1):
            out.turns = turn
            if self.stop.is_set():
                out.stop_reason = "cancelled"
                self._breaker("cancelled", turn, "the harness asked the loop to stop")
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

            self._compact_if_needed(turn)

            record = TurnRecord(turn=turn, timestamp=_now_iso())
            self._last_estimate = self._raw_prompt_tokens()
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

                result = self._dispatch(call)
                text = clamp(_observation_text(result), self.limits.observation_chars)
                record.results.append((call.call_id, text))
                if result.get("denied"):
                    out.gate_denials += 1
                    denial_streak += 1
                    if result.get("verdict") == "needs_approval":
                        out.approvals_required += 1
                else:
                    denial_streak = 0
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
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
