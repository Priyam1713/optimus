"""Agent Client Protocol: drive Optimus from Zed, JetBrains, or any ACP client.

[ACP](https://agentclientprotocol.com) is JSON-RPC 2.0 between a code editor and
a coding agent, modelled on LSP and for the same reason — it turns N editors
times M agents into N plus M. Optimus is the *agent* side here; the editor is the
client. (apex §7 calls this an "ACP client", meaning a client of the ecosystem.
The role name in the protocol is Agent, and this file uses the protocol's word.)

## Why this is the most interesting surface in M4

The Gate's hard invariant is that a model-chosen mutation is parked for a human.
Under a benchmark harness there is no human, which is why the autonomy envelope
exists — an owner-signed document that clears exactly that one invariant, in
advance, for a bounded scope.

An editor is the opposite situation: there *is* a human, sitting right there.
And ACP has `session/request_permission`, a round trip that asks them. So under
ACP the Gate can park an action, the editor can ask, and the answer comes back
before anything happens. That is strictly stronger than the envelope, because
the assent names what the person was actually shown rather than what they agreed
to in advance.

The binding is the part to get right, and it is the part every system that gets
this wrong gets wrong in the same way: `mint_assent` is called **only** after a
real `selected` outcome comes back over the wire, and `shown` is the exact
payload that was sent in the request rather than a summary of it. Optimus's own
audit (`docs/audit.md` §2.6) records the failure this avoids — a process that
grants the authorisation it wants, producing a receipt that proves nothing.

## Version

Implements **protocol version 1**, and negotiates honestly: a client asking for
v2 is answered with `protocolVersion: 1`, which is what ACP's negotiation says
to do — respond with the latest version the agent supports and let the client
decide. v2 is a substantial redesign (`session/prompt` no longer signals turn
completion; `fs/*` and `terminal/*` are gone) and is **not** implemented. Saying
so is cheaper than a half-v2 that fails in an editor.

## Transport

stdio, newline-delimited JSON. The spec is explicit that a message MUST NOT
contain an embedded newline, which `json.dumps` guarantees by escaping them
inside strings — as long as nobody pretty-prints, so nobody does.
"""

from __future__ import annotations

import contextlib
import json
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from optimus.surface.control import Control
from optimus.surface.events import Bus, EventKind, RunEvent

__all__ = [
    "PROTOCOL_VERSION",
    "ACPServer",
    "JsonRpcPeer",
    "PermissionBridge",
]

#: The version this implements. See the module docstring on v2.
PROTOCOL_VERSION = 1

#: ACP permission option kinds, from the schema. `allow_always` and
#: `reject_always` are offered but deliberately not *remembered* here — see
#: `PermissionBridge.options`.
_ALLOW_KINDS = frozenset({"allow_once", "allow_always"})


# ==========================================================================
# JSON-RPC over newline-delimited stdio
# ==========================================================================

@dataclass
class _Pending:
    event: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: dict[str, Any] | None = None


class JsonRpcPeer:
    """One end of a JSON-RPC 2.0 conversation over two byte streams.

    Bidirectional: this side both serves requests and makes them. The making
    part is what `session/request_permission` needs, and it is the part a
    one-directional dispatcher cannot do — the agent has to block a tool call on
    an answer from the editor.
    """

    def __init__(self, reader: BinaryIO, writer: BinaryIO):
        self._reader = reader
        self._writer = writer
        self._write_lock = threading.Lock()
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending: dict[Any, _Pending] = {}
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {}
        #: Notifications need no reply and must never produce one.
        self.notifications: dict[str, Callable[[dict[str, Any]], None]] = {}
        self._closed = threading.Event()

    # -- writing --------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        # `separators` keeps it compact; the default `json.dumps` never emits a
        # bare newline, and escapes any inside strings. One line, always.
        line = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        with self._write_lock:
            try:
                self._writer.write(line)
                self._writer.flush()
            except (BrokenPipeError, ValueError):
                # The editor went away mid-write. Nothing to do but stop.
                self._closed.set()

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict[str, Any], *, timeout: float = 300.0) -> Any:
        """Call the client and wait for its answer.

        Blocks the calling thread — which is the point when the caller is a tool
        call waiting on a permission decision. `timeout` exists because an editor
        that is closed while a dialog is open would otherwise hang a run
        forever; a timeout here surfaces as a refusal, never as an approval.
        """
        with self._id_lock:
            self._id += 1
            request_id = self._id
        pending = _Pending()
        self._pending[request_id] = pending
        self._send({
            "jsonrpc": "2.0", "id": request_id, "method": method, "params": params
        })
        if not pending.event.wait(timeout):
            self._pending.pop(request_id, None)
            raise TimeoutError(f"{method} was not answered within {timeout:.0f}s")
        self._pending.pop(request_id, None)
        if pending.error is not None:
            raise RuntimeError(f"{method} failed: {pending.error}")
        return pending.result

    # -- reading --------------------------------------------------------------

    def serve_forever(self) -> None:
        """Read messages until the stream ends."""
        for raw in self._reader:
            if self._closed.is_set():
                return
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                self._send({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                })
                continue
            self._handle(message)

    def _handle(self, message: dict[str, Any]) -> None:
        # A response to something we asked.
        if "method" not in message and "id" in message:
            pending = self._pending.get(message["id"])
            if pending is not None:
                pending.result = message.get("result")
                pending.error = message.get("error")
                pending.event.set()
            return

        method = message.get("method", "")
        params = message.get("params") or {}
        request_id = message.get("id")

        if request_id is None:
            handler = self.notifications.get(method)
            if handler is not None:
                with contextlib.suppress(Exception):
                    handler(params)
            return

        handler = self.handlers.get(method)
        if handler is None:
            self._send({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            })
            return

        # Each request runs on its own thread, and the reader loop goes straight
        # back to reading. This is not a throughput optimisation — it is load
        # bearing twice over:
        #
        # 1. `session/prompt` blocks for the length of a whole run. Handling it
        #    inline means `session/cancel` is not *read* until the run it was
        #    meant to cancel has finished, which is a stop button that works
        #    only once it is pointless.
        # 2. `session/request_permission` is an outbound request made from the
        #    thread running the turn, and its reply arrives here. If that thread
        #    were this thread, it would be waiting on a message only it could
        #    read: a deadlock on the first parked action, and only ever in a
        #    real editor, never in a test that drives one method at a time.
        #
        # JSON-RPC permits out-of-order responses, so nothing is owed to
        # ordering here beyond matching ids, which `id` already does.
        def respond() -> None:
            try:
                result = handler(params)
            except Exception as exc:
                self._send({
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": str(exc)},
                })
                return
            self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

        threading.Thread(
            target=respond, name=f"acp-{method}-{request_id}", daemon=True
        ).start()

    def close(self) -> None:
        self._closed.set()


# ==========================================================================
# the permission bridge
# ==========================================================================

class PermissionBridge:
    """Turns a parked Gate ticket into a question an editor asks a human.

    This is the only place in the project where an assent token is minted from
    something other than a direct human action at the CLI, so it is the only
    place where the assent chain could be quietly broken. Three rules hold it:

    1. **Mint only on a `selected` outcome naming an allow option.** A
       `cancelled` outcome, a timeout, a transport error and a reject option all
       take the same path: no token, and the Gate's park stands as a denial.
    2. **`shown` is the payload that was sent**, byte for byte, not a
       re-description of it. "The human approved" then names a specific thing
       that is in the ledger next to what they saw.
    3. **`allow_always` is offered but not remembered.** ACP defines the option
       and editors display it; honouring it would mean this process deciding, on
       its own, that a future action needs no human. That is exactly the
       invariant the Gate exists to hold, so the option is treated as
       `allow_once` and the difference is recorded rather than silently dropped.
    """

    def __init__(self, peer: JsonRpcPeer, *, principal: str = "acp-client"):
        self.peer = peer
        self.principal = principal
        #: Requests answered, for the receipt and for tests.
        self.decisions: list[dict[str, Any]] = []

    @staticmethod
    def options() -> list[dict[str, str]]:
        return [
            {"optionId": "allow", "name": "Allow once", "kind": "allow_once"},
            {"optionId": "allow_always", "name": "Always allow", "kind": "allow_always"},
            {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
        ]

    def ask(
        self,
        gate: Any,
        session_id: str,
        ticket: Any,
        *,
        title: str,
        raw_input: dict[str, Any],
        timeout: float = 300.0,
    ) -> str | None:
        """Ask the human. Returns an assent token, or `None` for anything else.

        `None` is the answer for a rejection, a cancellation, a timeout and a
        dead socket alike. Collapsing those four into one negative is deliberate:
        every way of *not* getting a yes has to mean no, and a bridge that
        distinguishes them is a bridge with three chances to accidentally
        produce a token.
        """
        tool_call = {
            "toolCallId": getattr(ticket, "ticket_id", ""),
            "title": title,
            "kind": _acp_kind(raw_input.get("tool", "")),
            "status": "pending",
            "rawInput": raw_input,
        }
        params = {
            "sessionId": session_id,
            "toolCall": tool_call,
            "options": self.options(),
        }
        try:
            response = self.peer.request(
                "session/request_permission", params, timeout=timeout
            )
        except (TimeoutError, RuntimeError) as exc:
            self.decisions.append({"ticket": tool_call["toolCallId"],
                                   "outcome": "unanswered", "detail": str(exc)})
            return None

        outcome = (response or {}).get("outcome") or {}
        # The schema nests the outcome; some clients flatten it. Accept both,
        # but never *infer* approval from a shape we did not expect.
        if not isinstance(outcome, dict):
            outcome = {}
        kind = outcome.get("outcome") or (response or {}).get("outcome")
        option_id = outcome.get("optionId") or (response or {}).get("optionId")

        if kind != "selected" or not option_id:
            self.decisions.append({"ticket": tool_call["toolCallId"],
                                   "outcome": "cancelled"})
            return None

        chosen = next(
            (o for o in self.options() if o["optionId"] == option_id), None
        )
        if chosen is None or chosen["kind"] not in _ALLOW_KINDS:
            self.decisions.append({"ticket": tool_call["toolCallId"],
                                   "outcome": "rejected", "optionId": option_id})
            return None

        # Rule 3, recorded rather than dropped.
        downgraded = chosen["kind"] == "allow_always"
        token = gate.mint_assent(
            self.principal,
            f"ticket:{tool_call['toolCallId']}",
            # Rule 2: what they were shown, as it was sent.
            {
                "surface": "acp",
                "sessionId": session_id,
                "params": params,
                "optionId": option_id,
                "treated_as": "allow_once",
                "downgraded_from": "allow_always" if downgraded else None,
            },
        )
        self.decisions.append({
            "ticket": tool_call["toolCallId"], "outcome": "approved",
            "optionId": option_id, "downgraded": downgraded,
        })
        return token


def _acp_kind(tool: str) -> str:
    """Optimus tool name to an ACP tool-call kind, for the editor's icon."""
    return {
        "read_file": "read",
        "list_dir": "read",
        "write_file": "edit",
        "delete_file": "delete",
        "bash": "execute",
    }.get(tool, "other")


# ==========================================================================
# the agent
# ==========================================================================

@dataclass
class _Session:
    session_id: str
    cwd: str
    control: Control
    bus: Bus
    thread: threading.Thread | None = None


class ACPServer:
    """The Agent half of ACP, over stdio.

    `run_factory` is handed the session and the prompt text and returns a
    callable that performs the run. Injecting it keeps this file free of engine
    configuration, and lets a test drive the whole protocol against a scripted
    loop.
    """

    def __init__(
        self,
        *,
        run_factory: Callable[[str, str, Bus, Control], Callable[[], Any]],
        reader: BinaryIO | None = None,
        writer: BinaryIO | None = None,
        agent_name: str = "optimus",
        agent_version: str = "0.0.1",
    ):
        self.peer = JsonRpcPeer(
            reader or sys.stdin.buffer, writer or sys.stdout.buffer
        )
        self.run_factory = run_factory
        self.agent_name = agent_name
        self.agent_version = agent_version
        self.permissions = PermissionBridge(self.peer)
        self.sessions: dict[str, _Session] = {}
        self._client_capabilities: dict[str, Any] = {}

        self.peer.handlers.update({
            "initialize": self._initialize,
            "session/new": self._session_new,
            "session/prompt": self._session_prompt,
        })
        self.peer.notifications.update({
            "session/cancel": self._session_cancel,
        })

    # -- lifecycle ------------------------------------------------------------

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        asked = params.get("protocolVersion", PROTOCOL_VERSION)
        self._client_capabilities = params.get("clientCapabilities") or {}
        # Honest negotiation: answer with what is actually implemented. A client
        # asking for 2 gets 1 and decides for itself whether to proceed.
        return {
            "protocolVersion": min(int(asked or PROTOCOL_VERSION), PROTOCOL_VERSION),
            "agentInfo": {
                "name": self.agent_name,
                "title": "Optimus",
                "version": self.agent_version,
            },
            "agentCapabilities": {
                "loadSession": False,
                "promptCapabilities": {"image": False, "audio": False,
                                       "embeddedContext": False},
            },
            # Nothing to authenticate against: this is a local-first agent that
            # drives a local model. An empty list is the correct answer, not a
            # placeholder.
            "authMethods": [],
        }

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = f"sess_{uuid.uuid4().hex[:16]}"
        control = Control(run_id=session_id)
        bus = Bus(run_id=session_id)
        self.sessions[session_id] = _Session(
            session_id=session_id,
            cwd=params.get("cwd", ""),
            control=control,
            bus=bus,
        )
        return {"sessionId": session_id}

    def _session_cancel(self, params: dict[str, Any]) -> None:
        """A notification, so it returns nothing and must never reply.

        This is the cancellation path that does not go through the Harbor
        adapter: the editor's stop button sets the same `threading.Event` the
        loop already polls.
        """
        session = self.sessions.get(params.get("sessionId", ""))
        if session is not None:
            session.control.cancel("the editor cancelled the turn", source="acp")

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session = self.sessions.get(params.get("sessionId", ""))
        if session is None:
            raise ValueError("unknown session")
        text = _prompt_text(params.get("prompt") or [])

        # A prompt while a turn is in flight is a steer, not a second run.
        if session.thread is not None and session.thread.is_alive():
            session.control.interrupt(text, source="acp")
            return {"stopReason": "end_turn"}

        forwarder = _Forwarder(self.peer, session.session_id)
        detach = session.bus.sink(forwarder.forward)
        runner = self.run_factory(session.session_id, text, session.bus, session.control)

        outcome: dict[str, Any] = {}

        def drive() -> None:
            try:
                result = runner()
                outcome["stop_reason"] = getattr(result, "stop_reason", "")
            finally:
                detach()

        thread = threading.Thread(target=drive, name=f"acp-{session.session_id}")
        session.thread = thread
        thread.start()
        thread.join()

        return {"stopReason": _stop_reason(
            outcome.get("stop_reason", ""), session.control.cancelled
        )}

    def serve_forever(self) -> None:
        self.peer.serve_forever()


class _Forwarder:
    """Bus events to `session/update` notifications."""

    def __init__(self, peer: JsonRpcPeer, session_id: str):
        self.peer = peer
        self.session_id = session_id

    def forward(self, event: RunEvent) -> None:
        update = self._update(event)
        if update is None:
            return
        self.peer.notify(
            "session/update", {"sessionId": self.session_id, "update": update}
        )

    def _update(self, event: RunEvent) -> dict[str, Any] | None:
        p = event.payload
        match event.kind:
            case EventKind.TOOL_CALL:
                return {
                    "sessionUpdate": "tool_call",
                    "toolCallId": str(p.get("call_id", "")),
                    "title": f"{p.get('name', 'tool')}: {p.get('brief', '')}",
                    "kind": _acp_kind(str(p.get("name", ""))),
                    "status": "in_progress",
                }
            case EventKind.TOOL_RESULT:
                return {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": str(p.get("call_id", "")),
                    "status": "failed" if p.get("denied") else "completed",
                    "content": [{
                        "type": "content",
                        "content": {"type": "text", "text": str(p.get("preview", ""))},
                    }],
                }
            case EventKind.MODEL_CALL:
                usage = p.get("meter") or {}
                if not usage:
                    return None
                return {
                    "sessionUpdate": "usage_update",
                    "usage": {
                        "inputTokens": usage.get("input_tokens", 0),
                        "outputTokens": usage.get("output_tokens", 0),
                    },
                }
        return None


def _prompt_text(blocks: list[dict[str, Any]]) -> str:
    """Flatten ACP content blocks to the text the loop takes."""
    parts = [
        str(b.get("text", "")) for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "\n".join(p for p in parts if p)


def _stop_reason(loop_reason: str, cancelled: bool) -> str:
    """Optimus stop reasons to ACP's five.

    ACP allows `end_turn`, `max_tokens`, `max_turn_requests`, `refusal` and
    `cancelled`. Optimus's own reasons are finer-grained, and the mapping is
    lossy in one direction that matters: `blocked` — every action refused —
    becomes `refusal`, because that is what actually happened, rather than
    `end_turn`, which would tell the editor the agent chose to stop.
    """
    if cancelled or loop_reason == "cancelled":
        return "cancelled"
    return {
        "finished": "end_turn",
        "max_turns": "max_turn_requests",
        "stalled": "max_turn_requests",
        "looping": "max_turn_requests",
        "blocked": "refusal",
        "cost_ceiling": "refusal",
        "wall_clock": "cancelled",
        "provider_error": "refusal",
        "provider_unavailable": "refusal",
    }.get(loop_reason, "end_turn")
