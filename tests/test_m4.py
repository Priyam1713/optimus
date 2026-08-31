"""M4: surfaces — the event bus, the steering plane, and the protocol emitters.

The tests worth having here are the ones about *pressure*, not about shape. A
bus that carries three events on an idle thread proves nothing; the questions
that matter are what happens when a subscriber stalls, when two threads publish
at once, and whether a surface can tell that it missed something. Those are the
conditions a live view actually meets, and the ones a green suite has a habit of
never reaching (STATUS, house rule 3).
"""

from __future__ import annotations

import json
import threading
import time

import pytest

from optimus.context.window import ContextBudget, ContextWindow
from optimus.gate.envelope import issue
from optimus.gate.gate import Gate
from optimus.gate.policy import benchmark_policy
from optimus.gate.remote import RemoteResolver
from optimus.ledger.chain import Chain
from optimus.ledger.keys import AgentKey, OwnerKey
from optimus.loop.agent import AgentLoop, LoopLimits
from optimus.loop.llm import ModelReply, ScriptedLLM, ToolCall, Usage
from optimus.surface.control import Confidence, Control, Steer, SteerKind
from optimus.surface.events import Bus, EventKind, RunEvent
from optimus.tools.remote import RemoteTools
from optimus.venues.remote import RemoteExec, RemoteVenue

# --------------------------------------------------------------------------
# a loop we can drive, borrowed in shape from tests/test_m3.py
# --------------------------------------------------------------------------

class _Transport:
    def __call__(self, argv, *, cwd, timeout_s):
        return RemoteExec(exit_code=0, stdout="", stderr="")


def _reply(*calls, text="", usage=None, error=""):
    return ModelReply(
        text=text,
        tool_calls=tuple(
            ToolCall(call_id=f"c{i}", name=n, arguments=a)
            for i, (n, a) in enumerate(calls, 1)
        ),
        usage=usage or Usage(input_tokens=100, output_tokens=20, cost_usd=0.001),
        error=error,
    )


def _loop(replies, *, limits=None, bus=None, control=None, stop=None):
    owner = OwnerKey.generate()
    gate = Gate(
        Chain(AgentKey.generate()),
        benchmark_policy(),
        RemoteResolver("/work", venue="harbor"),
        run_id="run-1",
    )
    gate.open_envelope(
        issue(
            owner,
            principal="operator@example",
            actor="agent",
            workspace="/work",
            venues=("harbor",),
            max_actions=500,
            reason="m4 test",
        ),
        owner_fingerprint=owner.fingerprint,
    )
    return AgentLoop(
        gate=gate,
        tools=RemoteTools(
            gate=gate, venue=RemoteVenue(_Transport(), name="harbor"), workspace="/work"
        ),
        window=ContextWindow(ContextBudget(total=32_000, keep_recent=4)),
        llm=ScriptedLLM(list(replies)),
        limits=limits or LoopLimits(),
        run_id="run-1",
        bus=bus,
        control=control,
        stop=stop,
    )


# --------------------------------------------------------------------------
# the bus
# --------------------------------------------------------------------------

def test_subscriber_receives_published_events():
    bus = Bus(run_id="r1")
    sub = bus.subscribe("t")
    bus.publish(EventKind.TURN_STARTED, turn=1)
    bus.publish(EventKind.TURN_FINISHED, turn=1)

    first = sub.get(timeout=1)
    second = sub.get(timeout=1)
    assert first is not None and second is not None
    assert (first.kind, first.turn) == (EventKind.TURN_STARTED, 1)
    assert (second.kind, second.turn) == (EventKind.TURN_FINISHED, 1)
    assert first.run_id == "r1"


def test_seq_is_monotonic_and_gapless_at_the_source():
    bus = Bus()
    sub = bus.subscribe()
    for i in range(50):
        bus.publish(EventKind.TURN_STARTED, turn=i)
    seqs = [sub.get(timeout=1).seq for _ in range(50)]
    assert seqs == list(range(1, 51))


def test_a_stalled_subscriber_drops_oldest_and_says_so():
    """The whole point of the bounded queue: the loop must not be stalled.

    A subscriber that never reads gets a truncated view, and the gap is visible
    two ways — its own `dropped` counter, and a jump in `seq`.
    """
    bus = Bus(maxsize=4)
    sub = bus.subscribe()
    for i in range(1, 11):
        bus.publish(EventKind.TURN_STARTED, turn=i)

    assert sub.dropped == 6
    seen = [sub.get(timeout=1) for _ in range(4)]
    turns = [e.turn for e in seen]
    # Oldest discarded, newest kept: a live view stays current.
    assert turns == [7, 8, 9, 10]
    # And the gap is arithmetic, not a guess.
    assert seen[0].seq == 7


def test_publish_never_blocks_on_a_full_subscriber():
    bus = Bus(maxsize=2)
    bus.subscribe()  # never drained
    started = time.monotonic()
    for i in range(2_000):
        bus.publish(EventKind.TURN_STARTED, turn=i)
    # 2000 publishes against a full queue: if this ever back-pressures, the
    # loop's turn latency becomes a function of how fast someone's terminal is.
    assert time.monotonic() - started < 2.0


def test_concurrent_publishers_do_not_lose_or_duplicate_seq():
    bus = Bus(maxsize=10_000)
    sub = bus.subscribe()

    def publisher():
        for _ in range(200):
            bus.publish(EventKind.TOOL_CALL)

    threads = [threading.Thread(target=publisher) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = sorted(sub.get(timeout=1).seq for _ in range(800))
    assert seqs == list(range(1, 801))


def test_run_started_is_replayed_to_a_late_subscriber():
    """A surface attached at turn 30 still needs to know what run this is."""
    bus = Bus(run_id="r2")
    bus.publish(EventKind.RUN_STARTED, model="qwen35-9b")
    bus.publish(EventKind.TURN_STARTED, turn=1)

    late = bus.subscribe("late")
    first = late.get(timeout=1)
    assert first is not None
    assert first.kind is EventKind.RUN_STARTED
    assert first.payload["model"] == "qwen35-9b"
    # Only the labelling event is replayed, not the whole run.
    assert late.get(timeout=0.1) is None


def test_a_sink_that_raises_does_not_break_the_run():
    bus = Bus()
    seen: list[RunEvent] = []

    def broken(_event):
        raise RuntimeError("this surface is having a bad day")

    bus.sink(broken)
    bus.sink(seen.append)
    bus.publish(EventKind.TURN_STARTED, turn=1)
    # The good sink still ran; nothing propagated to the publisher.
    assert len(seen) == 1


def test_detaching_a_sink_stops_delivery():
    bus = Bus()
    seen: list[RunEvent] = []
    detach = bus.sink(seen.append)
    bus.publish(EventKind.TURN_STARTED)
    detach()
    bus.publish(EventKind.TURN_STARTED)
    assert len(seen) == 1


def test_close_ends_iteration():
    bus = Bus()
    sub = bus.subscribe()
    bus.publish(EventKind.TURN_STARTED, turn=1)
    bus.close()
    assert [e.turn for e in sub] == [1]


def test_closing_a_full_subscriber_still_ends_its_iteration():
    """The liveness bug: the sentinel was a `put_nowait` that could fail.

    A subscriber whose queue is full is exactly the one that fell behind, so
    dropping *its* end-of-stream marker leaves the consumer blocked on a `get()`
    nothing will ever answer — a thread hung for the life of the process, on
    precisely the slow surface the drop policy exists to tolerate.
    """
    bus = Bus(maxsize=2)
    sub = bus.subscribe()
    for i in range(20):
        bus.publish(EventKind.TURN_STARTED, turn=i)
    bus.close()

    drained = []
    finished = threading.Event()

    def consume():
        drained.extend(sub)
        finished.set()

    threading.Thread(target=consume, daemon=True).start()
    assert finished.wait(5), "iteration never ended: the close sentinel was lost"


def test_closing_a_full_subscription_directly_also_ends_it():
    bus = Bus(maxsize=2)
    sub = bus.subscribe()
    for i in range(20):
        bus.publish(EventKind.TURN_STARTED, turn=i)
    sub.close()

    finished = threading.Event()
    threading.Thread(
        target=lambda: (list(sub), finished.set()), daemon=True
    ).start()
    assert finished.wait(5)


def test_listen_releases_the_subscription():
    bus = Bus()
    with bus.listen() as sub:
        bus.publish(EventKind.TURN_STARTED)
        assert bus.subscribers == 1
        assert sub.get(timeout=1) is not None
    assert bus.subscribers == 0


def test_event_serialises_to_json():
    bus = Bus(run_id="r3")
    event = bus.publish(EventKind.TOOL_CALL, turn=4, name="bash", command="ls")
    text = json.dumps(event.as_dict())
    back = json.loads(text)
    assert back["kind"] == "tool.call"
    assert back["turn"] == 4
    assert back["payload"]["name"] == "bash"


# --------------------------------------------------------------------------
# control
# --------------------------------------------------------------------------

def test_priority_order_beats_arrival_order():
    c = Control()
    c.enqueue("a note", source="tui")
    c.guide("a correction", source="tui")
    c.interrupt("stop and look at this", source="tui")

    kinds = [s.kind for s in c.drain()]
    assert kinds == [SteerKind.INTERRUPT, SteerKind.GUIDANCE, SteerKind.NOTE]


def test_fifo_within_a_priority():
    """Two corrections must arrive in the order they were typed."""
    c = Control()
    for i in range(5):
        c.guide(f"correction {i}")
    assert [s.text for s in c.drain()] == [f"correction {i}" for i in range(5)]


def test_cancel_trips_the_stop_event_immediately():
    """The Event is the bit the loop already checks, so cancel must set it
    without waiting for anyone to call drain()."""
    c = Control()
    assert not c.cancelled
    c.cancel("operator changed their mind", source="http")
    assert c.cancelled
    assert c.stop.is_set()
    assert c.cancel_reason == "operator changed their mind"


def test_stop_event_is_the_same_object_the_loop_takes():
    c = Control()
    assert isinstance(c.stop, threading.Event)


def test_drain_is_bounded_so_one_operator_cannot_blow_the_budget():
    c = Control()
    for i in range(100):
        c.enqueue(f"note {i}")
    assert len(c.drain(limit=8)) == 8
    assert c.pending == 92


def test_peek_does_not_consume():
    c = Control()
    c.guide("look here")
    assert len(c.peek()) == 1
    assert c.pending == 1
    assert len(c.drain()) == 1
    assert c.pending == 0


def test_steer_renders_a_message_naming_its_source():
    steer = Steer(SteerKind.GUIDANCE, "use the makefile", source="acp")
    msg = steer.as_message()
    assert msg["role"] == "user"
    assert "makefile" in msg["content"]
    assert "acp" in msg["content"]


def test_history_records_every_steer_for_the_receipt():
    c = Control()
    c.guide("one")
    c.interrupt("two")
    c.cancel("three")
    assert [s.text for s in c.history] == ["one", "two", "three"]


def test_confidence_rejects_impossible_values():
    with pytest.raises(ValueError):
        Confidence(1.4)
    with pytest.raises(ValueError):
        Confidence(-0.1)


def test_confidence_keeps_its_source():
    """A model's self-report and a measurement are different claims."""
    c = Control()
    c.signal(0.3, source="model", reason="never seen this build system")
    conf = c.confidence
    assert conf is not None
    assert conf.value == 0.3
    assert conf.source == "model"


def test_control_is_thread_safe_under_contention():
    c = Control()

    def sender(n):
        for i in range(100):
            c.enqueue(f"{n}-{i}")

    threads = [threading.Thread(target=sender, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert c.pending == 400
    assert len(c.drain(limit=400)) == 400


# --------------------------------------------------------------------------
# the loop, wired to both
# --------------------------------------------------------------------------

def test_the_bus_and_the_ledger_cannot_disagree_about_turns():
    """The regression this whole design is shaped around.

    `optimus why` and a live surface previously each derived "how many turns"
    from their own reading, and the same trial reported 40 in one and 41 in the
    other (STATUS, M3-13..16 family). The mirror publishes from the single point
    that writes the ledger, so the two counts are the same arithmetic on the
    same rows — not two answers that happen to agree today.
    """
    bus = Bus()
    sub = bus.subscribe(replay=False)
    loop = _loop(
        [_reply(("bash", {"command": "ls"})), _reply(("finish", {"summary": "done"}))],
        bus=bus,
    )
    out = loop.run("do the thing")
    bus.close()

    events = list(sub)
    ledger_turns = {
        e.payload["turn"] for e in loop.gate.chain.events if e.kind == "model.call"
    }
    bus_turns = {e.turn for e in events if e.kind is EventKind.MODEL_CALL}
    assert ledger_turns == bus_turns == {1, 2}
    assert out.turns == len(bus_turns)


def test_every_mirrored_kind_reaches_the_bus():
    bus = Bus()
    sub = bus.subscribe(replay=False)
    loop = _loop([_reply(("finish", {"summary": "done"}))], bus=bus)
    loop.run("go")
    bus.close()

    kinds = {e.kind for e in sub}
    assert EventKind.RUN_STARTED in kinds
    assert EventKind.MODEL_CALL in kinds
    assert EventKind.CONTEXT_TURN in kinds
    assert EventKind.RUN_FINISHED in kinds
    assert EventKind.TURN_STARTED in kinds
    assert EventKind.TURN_FINISHED in kinds


def test_a_breaker_payload_with_its_own_kind_field_still_publishes():
    """`loop.breaker` rows carry a `kind` of their own, which collides with the
    bus's `kind` argument. That is a `TypeError` at the first breaker, i.e. at
    the first stalled run rather than in any test that only takes happy paths."""
    bus = Bus()
    sub = bus.subscribe(replay=False)
    loop = _loop([_reply(text="thinking...")] * 5,
                 limits=LoopLimits(max_no_action_streak=3), bus=bus)
    out = loop.run("go")
    bus.close()
    assert out.stop_reason == "stalled"

    breakers = [e for e in sub if e.kind is EventKind.BREAKER]
    assert breakers
    # The row's own `kind` survives inside the payload, next to the event kind.
    assert {b.payload["kind"] for b in breakers} == {"no_action"}


def test_tool_calls_and_results_are_published_in_order():
    bus = Bus()
    sub = bus.subscribe(replay=False)
    loop = _loop([
        _reply(("bash", {"command": "ls"})),
        _reply(("finish", {"summary": "done"})),
    ], bus=bus)
    loop.run("go")
    bus.close()

    seen = [e for e in sub if e.kind in (EventKind.TOOL_CALL, EventKind.TOOL_RESULT)]
    assert [e.kind for e in seen] == [EventKind.TOOL_CALL, EventKind.TOOL_RESULT]
    assert seen[0].payload["name"] == "bash"
    assert seen[1].payload["denied"] is False


def test_a_guidance_steer_reaches_the_model():
    control = Control()
    control.guide("use the makefile, not cmake", source="tui")
    loop = _loop([
        _reply(("bash", {"command": "ls"})),
        _reply(("finish", {"summary": "done"})),
    ], control=control)
    loop.run("build it")

    steers = [
        m for m in loop.messages()
        if m["role"] == "user" and "makefile" in str(m.get("content", ""))
    ]
    assert steers, "the correction never reached the model"
    assert "tui" in str(steers[0]["content"])


def test_a_steer_is_recorded_and_published():
    bus, control = Bus(), Control()
    control.guide("look at the logs", source="http")
    sub = bus.subscribe(replay=False)
    loop = _loop([_reply(("finish", {"summary": "done"}))], bus=bus, control=control)
    loop.run("go")
    bus.close()

    rows = [e for e in loop.gate.chain.events if e.kind == "loop.steer"]
    assert len(rows) == 1
    assert rows[0].payload["source"] == "http"
    assert [e.kind for e in sub].count(EventKind.STEERED) == 1


def test_a_steer_is_not_promoted_to_an_invariant():
    """An operator's mid-run aside is not a standing rule. If it were pushed as
    an INVARIANT the context plane would refuse to ever compact it away, and a
    typo would be uncompactable for the rest of the run."""
    from optimus.context.episodes import EpisodeKind

    control = Control()
    control.guide("ignore that last bit")
    loop = _loop([_reply(("finish", {"summary": "done"}))], control=control)
    loop.run("go")

    invariants = [
        e for e in loop.window.episodes if e.kind is EpisodeKind.INVARIANT
    ]
    assert all("ignore that last bit" not in e.content for e in invariants)


def test_cancel_through_control_stops_the_run_without_the_adapter():
    """The ROADMAP item, as a test: nothing here touches the Harbor adapter."""
    control = Control()
    control.cancel("operator hit stop", source="tui")
    loop = _loop([_reply(("bash", {"command": "ls"}))] * 5, control=control)
    out = loop.run("go")

    assert out.stop_reason == "cancelled"
    assert out.turns == 1
    breakers = [
        e for e in loop.gate.chain.events
        if e.kind == "loop.breaker" and e.payload["kind"] == "cancelled"
    ]
    assert breakers and breakers[0].payload["detail"] == "operator hit stop"


def test_control_supplies_the_stop_event_when_none_is_passed():
    control = Control()
    loop = _loop([_reply(("finish", {"summary": "done"}))], control=control)
    assert loop.stop is control.stop


def test_an_explicit_stop_event_still_wins():
    """The Harbor adapter passes its own Event and must keep working."""
    own = threading.Event()
    control = Control()
    loop = _loop([_reply(("finish", {"summary": "done"}))], control=control, stop=own)
    assert loop.stop is own


def test_a_loop_with_no_surfaces_still_runs():
    """Both are optional, and a benchmark trial attaches neither."""
    loop = _loop([_reply(("finish", {"summary": "done"}))])
    out = loop.run("go")
    assert out.stop_reason == "finished"
    assert loop.control is None
    # A bus is always present so the publish sites need no guard; with no
    # subscribers it is a counter increment and two empty tuples.
    assert loop.bus.subscribers == 0


def test_drain_limit_bounds_what_one_turn_absorbs():
    control = Control()
    for i in range(50):
        control.enqueue(f"note {i}")
    loop = _loop([
        _reply(("bash", {"command": "ls"})),
        _reply(("finish", {"summary": "done"})),
    ], control=control)
    loop.run("go")
    # 8 per turn boundary, two boundaries: the operator cannot flood one turn.
    assert control.pending == 50 - 16


# --------------------------------------------------------------------------
# AG-UI
# --------------------------------------------------------------------------

class TestAGUI:
    """The emitter is checked against the protocol's own wire values.

    The trap this suite exists for: AG-UI's prose tables name events
    `RunStarted`, `TextMessageStart` and so on, which are TypeScript class
    names. The `type` discriminator on the wire carries `RUN_STARTED`. An
    emitter written from the tables passes every test that asserts against
    itself and is rejected by every real client.
    """

    def _emit(self, kind, **payload):
        from optimus.surface.agui import AGUIEmitter
        bus = Bus(run_id="r1")
        event = bus.publish(kind, turn=payload.pop("turn", 1), payload=payload)
        return AGUIEmitter(run_id="r1").translate(event)

    def test_wire_type_is_screaming_snake_not_pascal(self):
        out = self._emit(EventKind.RUN_STARTED, model="qwen35-9b")
        assert out[0]["type"] == "RUN_STARTED"
        assert out[0]["type"] != "RunStarted"

    def test_run_started_carries_thread_and_run_ids(self):
        out = self._emit(EventKind.RUN_STARTED)
        assert out[0]["threadId"] and out[0]["runId"] == "r1"

    def test_turn_boundaries_become_steps(self):
        assert self._emit(EventKind.TURN_STARTED, turn=3)[0] == {
            **self._emit(EventKind.TURN_STARTED, turn=3)[0],
            "type": "STEP_STARTED", "stepName": "turn 3",
        }
        assert self._emit(EventKind.TURN_FINISHED, turn=3)[0]["type"] == "STEP_FINISHED"

    def test_a_tool_call_opens_with_start_and_args(self):
        out = self._emit(EventKind.TOOL_CALL, call_id="c1", name="bash", brief="ls")
        assert [e["type"] for e in out] == ["TOOL_CALL_START", "TOOL_CALL_ARGS"]
        assert out[0]["toolCallId"] == "c1"
        assert out[0]["toolCallName"] == "bash"
        assert out[1]["delta"] == "ls"

    def test_a_tool_result_closes_with_end_and_result(self):
        out = self._emit(EventKind.TOOL_RESULT, call_id="c1", name="bash",
                         denied=False, preview="total 0")
        assert [e["type"] for e in out] == ["TOOL_CALL_END", "TOOL_CALL_RESULT"]
        assert out[1]["toolCallId"] == "c1"
        assert out[1]["content"] == "total 0"
        # TOOL_CALL_RESULT requires a messageId; a missing one is a schema error
        # the client reports as a malformed stream.
        assert out[1]["messageId"]

    def test_a_transient_provider_error_is_not_a_run_error(self):
        """RUN_ERROR ends the stream in a conforming client. A 503 that the loop
        retries must not do that."""
        out = self._emit(EventKind.MODEL_CALL, error="503 upstream",
                         meter={"extra": {"retryable": True}})
        assert [e["type"] for e in out] == ["CUSTOM"]
        assert out[0]["name"] == "optimus.providerError"
        assert out[0]["value"]["retryable"] is True

    def test_context_telemetry_becomes_a_state_snapshot(self):
        out = self._emit(EventKind.CONTEXT_TURN, estimated=1000, raw_estimate=800,
                         calibration=1.25, observed_last=990, allowance=28672,
                         episodes=12)
        assert out[0]["type"] == "STATE_SNAPSHOT"
        ctx = out[0]["snapshot"]["context"]
        assert ctx["estimated"] == 1000 and ctx["observedLast"] == 990
        assert ctx["calibration"] == 1.25

    def test_the_gate_is_a_custom_event_because_ag_ui_has_no_permission(self):
        out = self._emit(EventKind.GATE_PARKED, call_id="c1", name="write_file",
                         verdict="needs_approval", reason="irreversible")
        assert out[0]["type"] == "CUSTOM"
        assert out[0]["name"] == "optimus.gate"
        assert out[0]["value"]["outcome"] == "parked"

    def test_run_finished_does_not_claim_the_task_was_solved(self):
        out = self._emit(EventKind.RUN_FINISHED, stop_reason="max_turns",
                         turns=40, summary="", solved=None)
        custom = next(e for e in out if e["type"] == "CUSTOM")
        assert custom["value"]["solved"] is None
        assert out[-1]["type"] == "RUN_FINISHED"

    def test_an_empty_reply_emits_no_text_message(self):
        """TEXT_MESSAGE_CONTENT requires a non-empty delta."""
        from optimus.surface.agui import AGUIEmitter
        assert AGUIEmitter(run_id="r").message(1, "") == []
        out = AGUIEmitter(run_id="r").message(1, "hello")
        assert [e["type"] for e in out] == [
            "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"
        ]
        assert out[1]["delta"] == "hello"
        assert out[0]["role"] == "assistant"
        assert len({e["messageId"] for e in out}) == 1

    def test_sse_frames_end_with_a_blank_line(self):
        from optimus.surface.agui import sse
        frame = sse({"type": "RUN_FINISHED"})
        assert frame.startswith(b"data: ")
        assert frame.endswith(b"\n\n")
        assert json.loads(frame[6:].decode()) == {"type": "RUN_FINISHED"}

    def test_a_whole_run_translates_without_raising(self):
        from optimus.surface.agui import AGUIEmitter
        bus = Bus(run_id="r1")
        sub = bus.subscribe(replay=False)
        loop = _loop([
            _reply(("bash", {"command": "ls"})),
            _reply(("finish", {"summary": "done"})),
        ], bus=bus)
        loop.run("go")
        bus.close()

        emitter = AGUIEmitter(run_id="r1")
        out = list(emitter.stream(sub))
        assert out, "a whole run produced no AG-UI events"
        types = [e["type"] for e in out]
        assert types[0] == "RUN_STARTED"
        assert "RUN_FINISHED" in types
        # Every event carries a type the enum actually defines.
        allowed = {
            "RUN_STARTED", "RUN_FINISHED", "RUN_ERROR", "STEP_STARTED",
            "STEP_FINISHED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END", "TOOL_CALL_START", "TOOL_CALL_ARGS",
            "TOOL_CALL_END", "TOOL_CALL_RESULT", "STATE_SNAPSHOT",
            "STATE_DELTA", "CUSTOM",
        }
        assert set(types) <= allowed
        # And every one is JSON, on one line.
        for event in out:
            assert "\n" not in json.dumps(event)
