"""M4: the terminal view and the pre-flight.

Both are renderers, so the tests are mostly about what they refuse to say. A
surface that implies a run succeeded, or that a pre-flight is a guarantee, is
worse than no surface — it is a claim the system cannot back.
"""

from __future__ import annotations

import io

from optimus.gate.types import CapabilityRequest, Reversibility, Verb, Verdict
from optimus.ledger.events import TrustLabel
from optimus.surface.dryrun import DryRun, Plan
from optimus.surface.events import Bus, EventKind
from optimus.surface.tui import TUI
from tests.test_m4 import _loop, _reply


def _render(kind, **payload):
    bus = Bus(run_id="r1")
    turn = payload.pop("turn", 1)
    event = bus.publish(kind, turn=turn, payload=payload)
    return TUI(bus, stream=io.StringIO(), color=False).render(event)


# --------------------------------------------------------------------------
# the TUI
# --------------------------------------------------------------------------

class TestTUI:
    def test_colour_is_dropped_when_the_stream_is_not_a_tty(self):
        tui = TUI(Bus(), stream=io.StringIO())
        assert tui.color is False

    def test_no_color_is_honoured(self, monkeypatch):
        class _Tty(io.StringIO):
            def isatty(self):
                return True

        monkeypatch.setenv("NO_COLOR", "1")
        assert TUI(Bus(), stream=_Tty()).color is False

    def test_a_finished_run_does_not_claim_the_task_was_solved(self):
        """The loop knows it stopped; it does not know it succeeded. A view that
        prints a green tick next to `finished` invents the benchmark result the
        rest of the project is careful not to claim."""
        line = _render(EventKind.RUN_FINISHED, stop_reason="finished", turns=12,
                       gate_denials=0, approvals_required=0, compactions=1,
                       wall_ms=91_000)
        assert "finished" in line
        assert "solved: unknown" in line
        assert "the verifier decides" in line

    def test_the_context_line_shows_the_bill_next_to_the_estimate(self):
        line = _render(EventKind.CONTEXT_TURN, estimated=20_000, allowance=28_672,
                       observed_last=19_500, raw_estimate=16_000, calibration=1.25)
        assert "20,000/28,672" in line
        assert "billed_last=19,500" in line

    def test_an_under_estimate_is_called_out(self):
        """The shape of every context bug this project has had: the plane
        believed a request was smaller than the provider charged for."""
        line = _render(EventKind.CONTEXT_TURN, estimated=18_000, allowance=28_672,
                       observed_last=26_000)
        assert "under-estimated by 8,000" in line

    def test_an_over_estimate_is_not_called_out(self):
        line = _render(EventKind.CONTEXT_TURN, estimated=26_000, allowance=28_672,
                       observed_last=18_000)
        assert "under-estimated" not in line

    def test_a_refusal_renders_as_a_refusal_not_a_crash(self):
        line = _render(EventKind.TOOL_RESULT, call_id="c1", name="write_file",
                       denied=True, preview="denied by deny-grader-script")
        assert "refused" in line

    def test_a_parked_action_is_distinguished_from_a_denial(self):
        parked = _render(EventKind.GATE_PARKED, name="write_file",
                         verdict="needs_approval", reason="irreversible")
        denied = _render(EventKind.GATE_DENIED, name="bash",
                         verdict="deny", reason="grader tripwire")
        assert "parked" in parked and "human" in parked
        assert "gate" in denied and "parked" not in denied

    def test_a_nonzero_exit_is_not_rendered_as_success(self):
        ok = _render(EventKind.TOOL_RESULT, call_id="c", name="bash",
                     denied=False, exit_code=0, preview="total 0")
        bad = _render(EventKind.TOOL_RESULT, call_id="c", name="bash",
                      denied=False, exit_code=2, preview="no such file")
        assert "✓" in ok
        assert "exit 2" in bad

    def test_cache_share_is_reported_when_there_is_one(self):
        line = _render(EventKind.MODEL_CALL, model="qwen35-9b",
                       meter={"input_tokens": 10_000, "output_tokens": 500,
                              "extra": {"cached_tokens": 8_000}})
        assert "10,000 in" in line and "80% cached" in line

    def test_a_provider_error_is_shown_rather_than_swallowed(self):
        line = _render(EventKind.MODEL_CALL, error="503 upstream unavailable",
                       meter={})
        assert "503" in line

    def test_it_renders_a_whole_run_without_raising(self):
        bus = Bus(run_id="r1")
        out = io.StringIO()
        tui = TUI(bus, stream=out, color=False).start()
        loop = _loop([
            _reply(("bash", {"command": "ls"})),
            _reply(("finish", {"summary": "done"})),
        ], bus=bus)
        loop.run("go")
        bus.close()
        tui.stop()

        text = out.getvalue()
        assert "▶ run" in text
        assert "turn 1" in text
        assert "bash" in text
        assert "finished" in text

    def test_a_view_that_fell_behind_says_so(self):
        """`dropped` is the difference between an incomplete view and a lying
        one."""
        bus = Bus(run_id="r1", maxsize=2)
        out = io.StringIO()
        tui = TUI(bus, stream=out, color=False)
        sub = bus.subscribe("tui")
        tui._sub = sub
        for i in range(20):
            bus.publish(EventKind.TURN_STARTED, turn=i)
        bus.close()
        tui._pump()
        assert "events dropped" in out.getvalue()
        assert "optimus why" in out.getvalue()


# --------------------------------------------------------------------------
# the pre-flight
# --------------------------------------------------------------------------

def _plan(tool, verb, target, *, reversibility=Reversibility.SNAPSHOT, note=""):
    return Plan(
        CapabilityRequest(
            actor="agent",
            verb=verb,
            trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT,
            reversibility=reversibility,
            tool=tool,
            target_spec=target,
            venue="harbor",
        ),
        note=note,
    )


class TestDryRun:
    def test_it_predicts_without_writing_to_the_real_ledger(self):
        """The whole point: a preview costs nothing. Running it through the real
        Gate would write a decision row for an action nobody took."""
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        before = len(loop.gate.chain.events)
        DryRun(loop.gate).run([
            _plan("read_file", Verb.READ, "notes.txt"),
            _plan("write_file", Verb.WRITE, "out.txt"),
        ])
        assert len(loop.gate.chain.events) == before

    def test_it_does_not_spend_the_envelope(self):
        """A 2,000-action envelope must not be drained by asking about it."""
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        before = loop.gate.envelope_uses
        DryRun(loop.gate).run([_plan("write_file", Verb.WRITE, f"f{i}.txt")
                               for i in range(50)])
        assert loop.gate.envelope_uses == before

    def test_it_does_not_park_tickets_a_human_would_then_be_asked_about(self):
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        DryRun(loop.gate).run([
            _plan("write_file", Verb.WRITE, "x.txt",
                  reversibility=Reversibility.IRREVERSIBLE),
        ])
        assert loop.gate.pending == []

    def test_an_allowed_action_predicts_allow(self):
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        [prediction] = DryRun(loop.gate).run([
            _plan("read_file", Verb.READ, "notes.txt")
        ])
        assert prediction.allowed
        assert prediction.verdict is Verdict.ALLOW

    def test_a_denied_action_predicts_deny_and_names_the_rule(self):
        """The grader tripwire in `benchmark_policy`."""
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        [prediction] = DryRun(loop.gate).run([
            _plan("read_file", Verb.READ, "../outside-the-workspace")
        ])
        assert not prediction.allowed
        assert prediction.rule_id
        assert prediction.reason

    def test_a_mutation_always_carries_the_prediction_caveat(self):
        """Resolution is re-run at the moment of action. A pre-flight that reads
        as a promise is worse than none."""
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        [prediction] = DryRun(loop.gate).run([
            _plan("write_file", Verb.WRITE, "out.txt")
        ])
        assert any("prediction rather than a promise" in c
                   for c in prediction.caveats)

    def test_the_rendering_counts_the_three_outcomes(self):
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        predictions = DryRun(loop.gate).run([
            _plan("read_file", Verb.READ, "a.txt"),
            _plan("write_file", Verb.WRITE, "b.txt"),
            _plan("read_file", Verb.READ, "../escape"),
        ])
        text = DryRun.render(predictions)
        assert "3 action(s)" in text
        assert "allowed" in text and "refused" in text
        assert "prediction, not a promise" in text

    def test_rendering_nothing_is_not_an_error(self):
        assert "nothing to check" in DryRun.render([])

    def test_predictions_serialise(self):
        loop = _loop([_reply(("finish", {"summary": "x"}))])
        [prediction] = DryRun(loop.gate).run([
            _plan("read_file", Verb.READ, "a.txt", note="reads the config")
        ])
        body = prediction.as_dict()
        assert body["tool"] == "read_file"
        assert body["note"] == "reads the config"
        assert "verdict" in body and "rule" in body
