"""AG-UI: the bus, in the shape a frontend already understands.

[AG-UI](https://docs.ag-ui.com) is an event protocol between an agent backend
and a UI. Speaking it means any AG-UI frontend can render an Optimus run without
knowing anything about Optimus, which is the whole reason to speak someone
else's protocol rather than invent a wire format.

Two things about this translation are worth stating, because both are places
where a plausible-looking emitter would be wrong.

**The wire values are `SCREAMING_SNAKE_CASE`.** The prose documentation tables
name the events `RunStarted`, `TextMessageStart` and so on, which are the
TypeScript *class* names. The `type` discriminator actually carries
`"RUN_STARTED"` and `"TEXT_MESSAGE_START"`. An emitter written from the tables
produces events that no AG-UI client will match, and it will look correct in
every test that only asserts against itself.

**AG-UI has no permission round trip.** It is a protocol for *showing* a user
what an agent is doing, not for asking them to authorize it. So a Gate refusal
and a parked action are emitted as `CUSTOM` events, which is honest, rather than
mapped onto some adjacent event that would imply an answer is expected. The
protocol that does have that round trip is ACP, and `surface/acp.py` uses it.
House rule 5: never name a weak guarantee after a strong one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from typing import Any

from optimus.surface.events import EventKind, RunEvent

__all__ = ["AGUIEmitter", "sse"]


class AGUIEmitter:
    """Translates `RunEvent`s into AG-UI events.

    Stateful, because AG-UI is a streaming protocol built on paired start/end
    events with correlating ids, while the bus carries whole facts. The emitter
    holds the little state needed to open and close those pairs correctly.
    """

    def __init__(self, *, thread_id: str = "", run_id: str = ""):
        self.thread_id = thread_id or run_id or "optimus"
        self.run_id = run_id or "run"
        self._open_tool_calls: set[str] = set()
        self._message_seq = 0

    # -- ids ------------------------------------------------------------------

    def _next_message_id(self, turn: int) -> str:
        self._message_seq += 1
        return f"{self.run_id}-t{turn}-m{self._message_seq}"

    # -- translation ----------------------------------------------------------

    def translate(self, event: RunEvent) -> list[dict[str, Any]]:
        """One bus event to zero or more AG-UI events."""
        run_id = event.run_id or self.run_id
        base = {"timestamp": int(event.ts * 1000)}
        p = event.payload

        match event.kind:
            case EventKind.RUN_STARTED:
                return [
                    {**base, "type": "RUN_STARTED",
                     "threadId": self.thread_id, "runId": run_id}
                ]

            case EventKind.RUN_FINISHED:
                out: list[dict[str, Any]] = []
                # The stop reason is the single most useful thing about a
                # finished run and RUN_FINISHED has nowhere to put it.
                out.append({
                    **base, "type": "CUSTOM", "name": "optimus.outcome",
                    "value": {
                        "stopReason": p.get("stop_reason", ""),
                        "turns": p.get("turns", 0),
                        "summary": p.get("summary", ""),
                        # Deliberately forwarded as-is, including `None`. The
                        # loop does not know whether the task was solved and
                        # must not be rendered as if it claimed either answer.
                        "solved": p.get("solved"),
                        "gateDenials": p.get("gate_denials", 0),
                        "approvalsRequired": p.get("approvals_required", 0),
                    },
                })
                out.append({
                    **base, "type": "RUN_FINISHED",
                    "threadId": self.thread_id, "runId": run_id,
                })
                return out

            case EventKind.TURN_STARTED:
                return [{**base, "type": "STEP_STARTED",
                         "stepName": f"turn {event.turn}"}]

            case EventKind.TURN_FINISHED:
                return [{**base, "type": "STEP_FINISHED",
                         "stepName": f"turn {event.turn}"}]

            case EventKind.MODEL_CALL:
                if p.get("error"):
                    # Not RUN_ERROR: a transient provider failure is retried and
                    # the run continues. Calling it a run error would end the
                    # stream in every client that reads the protocol properly.
                    return [{
                        **base, "type": "CUSTOM", "name": "optimus.providerError",
                        "value": {
                            "message": p["error"],
                            "retryable": bool(p.get("meter", {})
                                              .get("extra", {}).get("retryable")),
                            "turn": event.turn,
                        },
                    }]
                return self._usage_events(base, p, event.turn)

            case EventKind.MODEL_ERROR:
                return [{**base, "type": "RUN_ERROR",
                         "message": str(p.get("error", "provider failed")),
                         "code": "provider_error"}]

            case EventKind.TOOL_CALL:
                call_id = str(p.get("call_id", ""))
                self._open_tool_calls.add(call_id)
                return [
                    {**base, "type": "TOOL_CALL_START",
                     "toolCallId": call_id,
                     "toolCallName": str(p.get("name", "tool"))},
                    {**base, "type": "TOOL_CALL_ARGS",
                     "toolCallId": call_id,
                     "delta": str(p.get("brief", ""))},
                ]

            case EventKind.TOOL_RESULT:
                call_id = str(p.get("call_id", ""))
                self._open_tool_calls.discard(call_id)
                return [
                    {**base, "type": "TOOL_CALL_END", "toolCallId": call_id},
                    {**base, "type": "TOOL_CALL_RESULT",
                     "messageId": self._next_message_id(event.turn),
                     "toolCallId": call_id,
                     "content": str(p.get("preview", ""))},
                ]

            case EventKind.GATE_DENIED | EventKind.GATE_PARKED:
                return [{
                    **base, "type": "CUSTOM",
                    "name": "optimus.gate",
                    "value": {
                        "outcome": ("parked"
                                    if event.kind is EventKind.GATE_PARKED
                                    else "denied"),
                        "toolCallId": p.get("call_id", ""),
                        "tool": p.get("name", ""),
                        "verdict": p.get("verdict", ""),
                        "reason": p.get("reason", ""),
                        "turn": event.turn,
                    },
                }]

            case EventKind.CONTEXT_TURN:
                # The estimate-next-to-the-bill row, which is the number four
                # separate bugs were invisible without. A frontend that renders
                # state gets it live rather than from a post-hoc ledger query.
                return [{
                    **base, "type": "STATE_SNAPSHOT",
                    "snapshot": {
                        "turn": event.turn,
                        "context": {
                            "estimated": p.get("estimated", 0),
                            "rawEstimate": p.get("raw_estimate", 0),
                            "calibration": p.get("calibration", 1.0),
                            "observedLast": p.get("observed_last", 0),
                            "allowance": p.get("allowance", 0),
                            "episodes": p.get("episodes", 0),
                        },
                    },
                }]

            case EventKind.CONTEXT_COMPACTED:
                return [{**base, "type": "CUSTOM", "name": "optimus.compacted",
                         "value": {"turn": event.turn, **p}}]

            case EventKind.BREAKER:
                return [{**base, "type": "CUSTOM", "name": "optimus.breaker",
                         "value": {"kind": p.get("kind", ""),
                                   "detail": p.get("detail", ""),
                                   "turn": event.turn}}]

            case EventKind.STEERED:
                return [{**base, "type": "CUSTOM", "name": "optimus.steered",
                         "value": {**p, "turn": event.turn}}]

        return []  # pragma: no cover - every kind above is handled

    def _usage_events(
        self, base: dict[str, Any], p: dict[str, Any], turn: int
    ) -> list[dict[str, Any]]:
        """A model reply, as a text message plus its meter."""
        events: list[dict[str, Any]] = []
        meter = p.get("meter") or {}
        extra = meter.get("extra") or {}
        events.append({
            **base, "type": "CUSTOM", "name": "optimus.usage",
            "value": {
                "turn": turn,
                "model": p.get("model", ""),
                "inputTokens": meter.get("input_tokens", 0),
                "outputTokens": meter.get("output_tokens", 0),
                "cachedTokens": extra.get("cached_tokens", 0),
                "costUsd": extra.get("cost_usd", 0.0),
                "finishReason": p.get("finish_reason", ""),
            },
        })
        return events

    def message(self, turn: int, text: str) -> list[dict[str, Any]]:
        """Emit assistant text as a complete start/content/end triple.

        The loop receives whole replies rather than a token stream, so there is
        nothing to stream. Emitting one content event with the whole body is
        valid AG-UI and is what a non-streaming backend is supposed to do.
        `TEXT_MESSAGE_CONTENT` requires a non-empty delta, so an empty reply
        emits nothing at all rather than an event the schema rejects.
        """
        if not text:
            return []
        mid = self._next_message_id(turn)
        return [
            {"type": "TEXT_MESSAGE_START", "messageId": mid, "role": "assistant"},
            {"type": "TEXT_MESSAGE_CONTENT", "messageId": mid, "delta": text},
            {"type": "TEXT_MESSAGE_END", "messageId": mid},
        ]

    def stream(self, events: Iterable[RunEvent]) -> Iterator[dict[str, Any]]:
        for event in events:
            yield from self.translate(event)


def sse(event: dict[str, Any]) -> bytes:
    """One AG-UI event as a Server-Sent Events frame.

    SSE is AG-UI's default transport and needs no dependency at all, which is
    why the server in this package speaks it. The blank line terminates the
    frame; without it a client buffers forever waiting for one.
    """
    return f"data: {json.dumps(event, separators=(',', ':'))}\n\n".encode()
