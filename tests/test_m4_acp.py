"""M4: the Agent Client Protocol surface.

Split from `test_m4.py` because the security-critical half of this file is not
really about ACP at all — it is about the one place in the project where an
assent token is minted from something other than a person at the CLI. Every test
in `TestACPPermissionBinding` is about *not* minting one, because the failure
mode `docs/audit.md` §2.6 records is a process granting itself the authorisation
it wants and producing a receipt that proves nothing.

The protocol tests use two real `os.pipe()`s rather than a mock, because the
thing most likely to be wrong in a stdio protocol is the framing, and a mock
that hands dicts straight across proves nothing about newline delimiting.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading

from optimus.surface.acp import (
    ACPServer,
    JsonRpcPeer,
    PermissionBridge,
    _prompt_text,
    _stop_reason,
)
from tests.test_m4 import _loop, _reply

_ACP_STOP_REASONS = {
    "end_turn", "max_tokens", "max_turn_requests", "refusal", "cancelled",
}


class _Wire:
    """A real bidirectional pipe pair between a test client and the server."""

    def __init__(self):
        c2s_r, c2s_w = os.pipe()
        s2c_r, s2c_w = os.pipe()
        self.server_reader = os.fdopen(c2s_r, "rb")
        self.client_writer = os.fdopen(c2s_w, "wb")
        self.client_reader = os.fdopen(s2c_r, "rb")
        self.server_writer = os.fdopen(s2c_w, "wb")

    def send(self, message):
        self.client_writer.write(
            json.dumps(message, separators=(",", ":")).encode() + b"\n"
        )
        self.client_writer.flush()

    def recv(self):
        line = self.client_reader.readline()
        return json.loads(line) if line.strip() else None

    def until(self, request_id):
        """Collect session/update notifications until the reply arrives."""
        updates = []
        while True:
            message = self.recv()
            if message is None:
                raise AssertionError("the server closed before replying")
            if message.get("method") == "session/update":
                updates.append(message["params"]["update"])
            elif message.get("id") == request_id:
                return updates, message

    def close(self):
        for handle in (self.client_writer, self.server_writer):
            with contextlib.suppress(OSError):
                handle.close()


def _default_factory(session_id, text, bus, control):
    def run():
        return _loop(
            [_reply(("finish", {"summary": "done"}))], bus=bus, control=control
        ).run(text)
    return run


def _server(wire, run_factory=None):
    server = ACPServer(
        run_factory=run_factory or _default_factory,
        reader=wire.server_reader,
        writer=wire.server_writer,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _new_session(wire):
    wire.send({"jsonrpc": "2.0", "id": 1, "method": "session/new", "params": {}})
    return wire.recv()["result"]["sessionId"]


# --------------------------------------------------------------------------
# framing and lifecycle
# --------------------------------------------------------------------------

class TestACPProtocol:
    def test_messages_are_one_line_with_no_embedded_newline(self):
        """The spec is explicit that a message MUST NOT contain an embedded
        newline. A tool observation full of them is the obvious way to break
        that, so the encoder has to escape rather than emit."""
        out = io.BytesIO()
        peer = JsonRpcPeer(io.BytesIO(b""), out)
        body = "line one\nline two\nline three"
        peer.notify("session/update", {"text": body})
        raw = out.getvalue()
        assert raw.count(b"\n") == 1
        assert raw.endswith(b"\n")
        assert json.loads(raw)["params"]["text"] == body

    def test_initialize_reports_the_version_and_no_auth_methods(self):
        wire = _Wire()
        _server(wire)
        wire.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": 1, "clientCapabilities": {}}})
        reply = wire.recv()
        assert reply["id"] == 1
        assert reply["result"]["protocolVersion"] == 1
        # A local-first agent driving a local model has nothing to authenticate
        # against. An empty list is the right answer, not a placeholder.
        assert reply["result"]["authMethods"] == []
        assert reply["result"]["agentInfo"]["name"] == "optimus"
        wire.close()

    def test_a_v2_client_is_answered_with_the_version_implemented(self):
        """Honest negotiation beats a half-v2 that fails inside an editor."""
        wire = _Wire()
        _server(wire)
        wire.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": 2}})
        assert wire.recv()["result"]["protocolVersion"] == 1
        wire.close()

    def test_unknown_methods_get_a_proper_jsonrpc_error(self):
        wire = _Wire()
        _server(wire)
        wire.send({"jsonrpc": "2.0", "id": 7, "method": "session/teleport",
                   "params": {}})
        assert wire.recv()["error"]["code"] == -32601
        wire.close()

    def test_malformed_json_does_not_kill_the_server(self):
        wire = _Wire()
        _server(wire)
        wire.client_writer.write(b"{not json at all\n")
        wire.client_writer.flush()
        assert wire.recv()["error"]["code"] == -32700
        # Still serving afterwards.
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        assert wire.recv()["id"] == 2
        wire.close()

    def test_a_handler_that_raises_becomes_an_error_not_a_dead_server(self):
        wire = _Wire()
        _server(wire)
        # No such session: the handler raises ValueError.
        wire.send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                   "params": {"sessionId": "nope", "prompt": []}})
        reply = wire.recv()
        assert reply["error"]["code"] == -32603
        wire.send({"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {}})
        assert wire.recv()["id"] == 4
        wire.close()

    def test_a_notification_never_gets_a_reply(self):
        """`session/cancel` is a notification. Replying to one is a protocol
        violation that some clients treat as a fatal desync."""
        wire = _Wire()
        _server(wire)
        session_id = _new_session(wire)
        wire.send({"jsonrpc": "2.0", "method": "session/cancel",
                   "params": {"sessionId": session_id}})
        # Prove nothing came back by round-tripping a request behind it and
        # getting *that* answer first.
        wire.send({"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}})
        assert wire.recv()["id"] == 9
        wire.close()

    def test_session_cancel_stops_the_run_without_the_harbor_adapter(self):
        wire = _Wire()
        server = _server(wire)
        session_id = _new_session(wire)
        wire.send({"jsonrpc": "2.0", "method": "session/cancel",
                   "params": {"sessionId": session_id}})
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}})
        wire.recv()
        assert server.sessions[session_id].control.cancelled
        assert server.sessions[session_id].control.cancel_reason
        wire.close()

    def test_a_prompt_runs_and_reports_a_stop_reason_acp_defines(self):
        wire = _Wire()
        _server(wire)
        session_id = _new_session(wire)
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                   "params": {"sessionId": session_id,
                              "prompt": [{"type": "text", "text": "do the thing"}]}})
        _updates, final = wire.until(2)
        assert final["result"]["stopReason"] in _ACP_STOP_REASONS
        assert final["result"]["stopReason"] == "end_turn"
        wire.close()

    def test_tool_calls_stream_as_session_updates(self):
        wire = _Wire()

        def factory(session_id, text, bus, control):
            def run():
                return _loop([
                    _reply(("bash", {"command": "ls"})),
                    _reply(("finish", {"summary": "done"})),
                ], bus=bus, control=control).run(text)
            return run

        _server(wire, factory)
        session_id = _new_session(wire)
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                   "params": {"sessionId": session_id,
                              "prompt": [{"type": "text", "text": "go"}]}})
        updates, _final = wire.until(2)

        kinds = [u["sessionUpdate"] for u in updates]
        assert "tool_call" in kinds
        assert "tool_call_update" in kinds
        call = next(u for u in updates if u["sessionUpdate"] == "tool_call")
        # bash -> execute, from the schema's own kind list.
        assert call["kind"] == "execute"
        assert call["status"] == "in_progress"
        done = next(u for u in updates if u["sessionUpdate"] == "tool_call_update")
        assert done["status"] == "completed"
        assert done["toolCallId"] == call["toolCallId"]
        wire.close()

    def test_prompt_text_is_flattened_from_content_blocks(self):
        assert _prompt_text([
            {"type": "text", "text": "first"},
            {"type": "image", "data": "..."},
            {"type": "text", "text": "second"},
        ]) == "first\nsecond"

    def test_a_prompt_during_a_live_turn_is_a_steer_not_a_second_run(self):
        wire = _Wire()
        started = threading.Event()
        release = threading.Event()

        def factory(session_id, text, bus, control):
            def run():
                started.set()
                release.wait(5)
                return _loop([_reply(("finish", {"summary": "done"}))],
                             bus=bus, control=control).run(text)
            return run

        server = _server(wire, factory)
        session_id = _new_session(wire)
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                   "params": {"sessionId": session_id,
                              "prompt": [{"type": "text", "text": "go"}]}})
        assert started.wait(5)
        wire.send({"jsonrpc": "2.0", "id": 3, "method": "session/prompt",
                   "params": {"sessionId": session_id,
                              "prompt": [{"type": "text", "text": "actually, stop"}]}})
        # The second prompt is answered immediately, as a steer.
        _updates, second = wire.until(3)
        assert second["result"]["stopReason"] == "end_turn"
        queued = server.sessions[session_id].control.peek()
        assert any("actually, stop" in s.text for s in queued)
        release.set()
        wire.close()


# --------------------------------------------------------------------------
# the permission binding
# --------------------------------------------------------------------------

class _Ticket:
    ticket_id = "tkt_1"


class TestACPPermissionBinding:
    def _bridge_and_gate(self, response, *, raises=None):
        class _Peer:
            def request(self, method, params, *, timeout=300.0):
                if raises is not None:
                    raise raises
                return response

        loop = _loop([_reply(("finish", {"summary": "x"}))])
        return PermissionBridge(_Peer()), loop.gate

    def _ask(self, bridge, gate):
        return bridge.ask(
            gate, "sess_1", _Ticket(),
            title="write /work/notes.txt",
            raw_input={"tool": "write_file", "path": "notes.txt"},
        )

    def _minted(self, gate):
        return [e for e in gate.chain.events if e.kind == "assent.minted"]

    def test_an_allow_selection_mints_an_assent(self):
        bridge, gate = self._bridge_and_gate(
            {"outcome": {"outcome": "selected", "optionId": "allow"}}
        )
        token = self._ask(bridge, gate)
        assert token and token.startswith("ast_")

    def test_the_assent_records_exactly_what_was_shown(self):
        """Not a summary of the payload — the payload, as it was sent."""
        bridge, gate = self._bridge_and_gate(
            {"outcome": {"outcome": "selected", "optionId": "allow"}}
        )
        self._ask(bridge, gate)
        minted = self._minted(gate)
        assert len(minted) == 1
        shown = minted[0].payload["shown"]
        assert shown["surface"] == "acp"
        assert shown["params"]["toolCall"]["rawInput"]["path"] == "notes.txt"
        assert shown["params"]["toolCall"]["title"] == "write /work/notes.txt"
        # The choices offered are part of the record: "they approved" means
        # little without what the alternatives were.
        assert shown["params"]["options"]

    def test_a_rejection_mints_nothing(self):
        bridge, gate = self._bridge_and_gate(
            {"outcome": {"outcome": "selected", "optionId": "reject"}}
        )
        assert self._ask(bridge, gate) is None
        assert not self._minted(gate)

    def test_a_cancelled_dialog_mints_nothing(self):
        bridge, gate = self._bridge_and_gate({"outcome": {"outcome": "cancelled"}})
        assert self._ask(bridge, gate) is None
        assert not self._minted(gate)

    def test_a_timeout_mints_nothing(self):
        """An editor closed with the dialog open must not read as approval."""
        bridge, gate = self._bridge_and_gate(None, raises=TimeoutError("no answer"))
        assert self._ask(bridge, gate) is None
        assert not self._minted(gate)

    def test_a_transport_error_mints_nothing(self):
        bridge, gate = self._bridge_and_gate(None, raises=RuntimeError("pipe died"))
        assert self._ask(bridge, gate) is None
        assert not self._minted(gate)

    def test_an_unexpected_response_shape_mints_nothing(self):
        """Approval is never *inferred* from a shape we did not expect."""
        for response in (
            {},
            None,
            {"outcome": "weird"},
            {"result": "ok"},
            {"outcome": {"outcome": "selected"}},          # no optionId
            {"outcome": {"optionId": "allow"}},            # no outcome kind
        ):
            bridge, gate = self._bridge_and_gate(response)
            assert self._ask(bridge, gate) is None, response
            assert not self._minted(gate), response

    def test_an_option_we_never_offered_is_not_a_yes(self):
        bridge, gate = self._bridge_and_gate(
            {"outcome": {"outcome": "selected", "optionId": "allow_everything"}}
        )
        assert self._ask(bridge, gate) is None
        assert not self._minted(gate)

    def test_allow_always_is_honoured_once_and_recorded_as_downgraded(self):
        """ACP defines the option and editors display it. Remembering it would
        mean this process deciding that some future action needs no human, which
        is precisely the invariant the Gate exists to hold."""
        bridge, gate = self._bridge_and_gate(
            {"outcome": {"outcome": "selected", "optionId": "allow_always"}}
        )
        assert self._ask(bridge, gate) is not None
        shown = self._minted(gate)[0].payload["shown"]
        assert shown["treated_as"] == "allow_once"
        assert shown["downgraded_from"] == "allow_always"
        assert bridge.decisions[-1]["downgraded"] is True

    def test_a_flattened_outcome_is_accepted_too(self):
        """Some clients flatten the outcome object. Accepting both shapes is
        fine; inferring approval from neither is the rule."""
        bridge, gate = self._bridge_and_gate(
            {"outcome": "selected", "optionId": "allow"}
        )
        assert self._ask(bridge, gate) is not None

    def test_the_token_opens_that_ticket_and_no_other(self):
        bridge, gate = self._bridge_and_gate(
            {"outcome": {"outcome": "selected", "optionId": "allow"}}
        )
        token = self._ask(bridge, gate)
        assert not gate.approve("tkt_other", token).allowed

    def test_an_assent_is_single_use(self):
        bridge, gate = self._bridge_and_gate(
            {"outcome": {"outcome": "selected", "optionId": "allow"}}
        )
        token = self._ask(bridge, gate)
        gate.approve("tkt_1", token)
        assert not gate.approve("tkt_1", token).allowed

    def test_every_refusal_path_is_recorded(self):
        """A decision that produced no token is still a decision, and a run that
        stalled because nobody answered should say so rather than look idle."""
        bridge, gate = self._bridge_and_gate({"outcome": {"outcome": "cancelled"}})
        self._ask(bridge, gate)
        assert bridge.decisions[-1]["outcome"] == "cancelled"


class TestACPPermissionDuringALiveTurn:
    """The deadlock regression, end to end over the real pipes.

    `session/request_permission` is an outbound request made from the thread
    running the turn, and its reply arrives on the reader thread. Handle
    `session/prompt` inline on that same reader thread and the turn waits for a
    message only the thread it is blocking could read. The failure needs a
    parked action *during* a live prompt to appear at all, so it shows up in an
    editor and in no test that drives one method at a time.
    """

    def test_a_permission_round_trip_completes_mid_turn(self):
        wire = _Wire()
        asked = threading.Event()
        got_token: list[object] = []

        def factory(session_id, text, bus, control):
            def run():
                loop = _loop([_reply(("finish", {"summary": "done"}))],
                             bus=bus, control=control)
                # Ask the editor from inside the turn, exactly as a parked
                # action would.
                token = server.permissions.ask(
                    loop.gate, session_id, _Ticket(),
                    title="write /work/notes.txt",
                    raw_input={"tool": "write_file", "path": "notes.txt"},
                    timeout=10,
                )
                got_token.append(token)
                return loop.run(text)
            return run

        server = _server(wire, factory)
        session_id = _new_session(wire)
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                   "params": {"sessionId": session_id,
                              "prompt": [{"type": "text", "text": "go"}]}})

        # The agent asks; the client answers; only then does the turn finish.
        request = None
        while request is None:
            message = wire.recv()
            if message.get("method") == "session/request_permission":
                request = message
        asked.set()
        assert request["params"]["sessionId"] == session_id
        assert request["params"]["toolCall"]["kind"] == "edit"
        assert {o["kind"] for o in request["params"]["options"]} == {
            "allow_once", "allow_always", "reject_once"
        }
        wire.send({"jsonrpc": "2.0", "id": request["id"],
                   "result": {"outcome": {"outcome": "selected",
                                          "optionId": "allow"}}})

        _updates, final = wire.until(2)
        assert final["result"]["stopReason"] == "end_turn"
        assert got_token and got_token[0] and got_token[0].startswith("ast_")
        wire.close()

    def test_a_rejection_mid_turn_yields_no_token_and_still_completes(self):
        wire = _Wire()
        got_token: list[object] = []

        def factory(session_id, text, bus, control):
            def run():
                loop = _loop([_reply(("finish", {"summary": "done"}))],
                             bus=bus, control=control)
                got_token.append(server.permissions.ask(
                    loop.gate, session_id, _Ticket(),
                    title="rm -rf /work",
                    raw_input={"tool": "bash", "command": "rm -rf /work"},
                    timeout=10,
                ))
                return loop.run(text)
            return run

        server = _server(wire, factory)
        session_id = _new_session(wire)
        wire.send({"jsonrpc": "2.0", "id": 2, "method": "session/prompt",
                   "params": {"sessionId": session_id,
                              "prompt": [{"type": "text", "text": "go"}]}})

        request = None
        while request is None:
            message = wire.recv()
            if message.get("method") == "session/request_permission":
                request = message
        wire.send({"jsonrpc": "2.0", "id": request["id"],
                   "result": {"outcome": {"outcome": "selected",
                                          "optionId": "reject"}}})

        _updates, final = wire.until(2)
        assert final["result"]["stopReason"] in _ACP_STOP_REASONS
        assert got_token == [None]
        wire.close()


class TestACPStopReasons:
    def test_every_loop_reason_maps_into_acps_five(self):
        for reason in ("finished", "max_turns", "stalled", "looping", "blocked",
                       "cost_ceiling", "wall_clock", "provider_error",
                       "provider_unavailable", "cancelled", "something_new"):
            assert _stop_reason(reason, False) in _ACP_STOP_REASONS, reason

    def test_blocked_is_a_refusal_not_a_clean_end(self):
        """Every action refused is not the agent choosing to stop, and an editor
        told `end_turn` renders it as success."""
        assert _stop_reason("blocked", False) == "refusal"
        assert _stop_reason("finished", False) == "end_turn"

    def test_cancellation_wins_over_whatever_the_loop_said(self):
        assert _stop_reason("finished", True) == "cancelled"
