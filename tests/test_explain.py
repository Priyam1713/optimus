"""`optimus why` — reading a run back out of its ledger.

This module exists because every finding in STATUS.md was reached with a
throwaway script against a ledger. The tests therefore care most about the
things those scripts had to get right: attributing rows to the turn they belong
to, and never quietly inventing a turn that did not happen.
"""

from __future__ import annotations

import json

from optimus.explain import Explanation, explain, explain_trial, find_trials, render, render_job
from optimus.ledger.chain import Chain
from optimus.ledger.events import TrustLabel
from optimus.ledger.keys import AgentKey
from optimus.ledger.store import DurableChain, LedgerStore


def _chain() -> Chain:
    return Chain(AgentKey.generate())


def _model_call(chain: Chain, turn: int, tools: list[str], **meter) -> None:
    chain.append(
        "model.call",
        {
            "turn": turn, "model": "m", "tools": tools, "error": meter.pop("error", ""),
            "meter": {"input_tokens": 0, "output_tokens": 0, **meter},
        },
        TrustLabel.UNTRUSTED_MODEL_OUTPUT,
    )


def _a_run(chain: Chain) -> Chain:
    chain.append("run.started", {"run_id": "r1", "model": "qwen"}, TrustLabel.TRUSTED_USER)
    chain.append("model.route", {"engine": "llama_cpp", "model": "qwen35-9b",
                                 "local": True}, TrustLabel.TRUSTED_LOCAL)
    chain.append("envelope.opened", {"envelope_id": "env_x", "workspace": "*"},
                 TrustLabel.TRUSTED_USER)
    # The environment probe: a gated command before any model call.
    chain.append("gate.decision", {"verdict": "allow", "rule": "allow-execute",
                                   "tool": "bash", "target": {"script": "ls"}},
                 TrustLabel.UNTRUSTED_MODEL_OUTPUT)
    chain.append("effect.settled", {"tool": "bash", "ok": True, "meter": {}},
                 TrustLabel.EXECUTION_RESULT)

    _model_call(chain, 1, ["bash"], input_tokens=1_000, cached_tokens=400)
    chain.append("gate.decision", {"verdict": "allow", "rule": "allow-execute",
                                   "tool": "bash", "target": {"script": "ls -la"}},
                 TrustLabel.UNTRUSTED_MODEL_OUTPUT)
    chain.append("effect.settled", {"tool": "bash", "ok": True, "meter": {}},
                 TrustLabel.EXECUTION_RESULT)

    _model_call(chain, 2, ["write_file"], input_tokens=2_000, cached_tokens=1_500)
    chain.append("gate.decision", {"verdict": "deny", "rule": "deny-sensitive-write",
                                   "tool": "write_file", "target": {"relpath": ".env"}},
                 TrustLabel.UNTRUSTED_MODEL_OUTPUT)
    chain.append("effect.settled", {"tool": "write_file", "ok": False, "meter": {}},
                 TrustLabel.EXECUTION_RESULT)
    return chain


class TestAttribution:
    def test_turn_zero_is_the_probe_and_not_a_turn(self):
        """The environment probe runs through the Gate before turn 1.

        Counting it as a turn overstated the count by one and reported a
        no-action turn the loop's own meter said had not happened.
        """
        exp = explain(_a_run(_chain()).events)
        assert [t.number for t in exp.turns if t.number > 0] == [1, 2]
        rendered = render(exp, timeline=False)
        assert "2 turns" in rendered
        assert "priming  1 gated command" in rendered
        assert "no-action" not in rendered

    def test_a_row_carrying_its_own_turn_lands_in_that_turn(self):
        """The bug this renderer shipped with.

        A compaction recorded for turn 21 created a bucket keyed 21 holding a
        turn numbered 20, so the timeline printed 20 twice and 21 never.
        """
        chain = _a_run(_chain())
        chain.append("context.compacted",
                     {"turn": 3, "evicted": 9, "rendered_before": 30_000,
                      "rendered_after": 20_000, "allowance": 28_000},
                     TrustLabel.TRUSTED_LOCAL)
        exp = explain(chain.events)
        numbers = [t.number for t in exp.turns]
        assert numbers == sorted(set(numbers)), "a turn number appeared twice"
        assert 3 in numbers
        assert next(t for t in exp.turns if t.number == 3).compaction["evicted"] == 9

    def test_gate_rows_belong_to_the_turn_that_made_them(self):
        exp = explain(_a_run(_chain()).events)
        second = next(t for t in exp.turns if t.number == 2)
        assert second.tools == ["write_file"]
        assert second.refused == 1
        assert second.failed_effects == 1

    def test_denials_are_grouped_by_rule(self):
        exp = explain(_a_run(_chain()).events)
        assert exp.denials["deny-sensitive-write"] == 1
        assert "deny-sensitive-write" in render(exp, timeline=False)


class TestHonesty:
    def test_a_run_with_no_ending_says_it_was_killed(self):
        """Rather than leaving the field blank for a reader to read as fine."""
        exp = explain(_a_run(_chain()).events)
        assert exp.finished is False
        assert exp.stop_reason == "killed_before_finishing"
        assert "never wrote its own ending" in render(exp, timeline=False)

    def test_a_finished_run_reports_its_own_stop_reason(self):
        chain = _a_run(_chain())
        chain.append("run.finished", {"stop_reason": "finished", "summary": "did it"},
                     TrustLabel.TRUSTED_LOCAL)
        exp = explain(chain.events)
        assert exp.finished and exp.stop_reason == "finished"
        assert "never wrote its own ending" not in render(exp, timeline=False)

    def test_a_failed_model_call_is_not_reported_as_an_idle_turn(self):
        chain = _a_run(_chain())
        _model_call(chain, 3, [], error="RateLimitError: 429")
        exp = explain(chain.events)
        rendered = render(exp, timeline=False)
        assert "the model call itself failed" in rendered
        assert "no-action turn" not in rendered

    def test_an_ungraded_trial_is_not_reported_as_failed(self):
        exp = explain(_a_run(_chain()).events)
        assert exp.reward is None
        assert "ungraded" in render(exp, timeline=False)


class TestReadingFromDisk:
    def _trial(self, tmp_path, reward=None):
        trial = tmp_path / "task__abc"
        (trial / "agent").mkdir(parents=True)
        store = LedgerStore(trial / "agent" / "ledger.db")
        chain = DurableChain(AgentKey.generate(), store)
        _a_run(chain)
        chain.append("run.finished", {"stop_reason": "finished", "summary": ""},
                     TrustLabel.TRUSTED_LOCAL)
        store.close()
        if reward is not None:
            (trial / "result.json").write_text(json.dumps({
                "task_name": "task",
                "verifier_result": {"rewards": {"reward": reward}},
            }))
        return trial

    def test_a_trial_directory_is_read_end_to_end(self, tmp_path):
        exp = explain_trial(self._trial(tmp_path, reward=1))
        assert exp is not None
        assert exp.engine == "llama_cpp" and exp.local is True
        assert exp.reward == 1.0
        assert "solved" in render(exp, timeline=False)

    def test_an_unsolved_trial_says_so(self, tmp_path):
        exp = explain_trial(self._trial(tmp_path, reward=0))
        assert exp.reward == 0.0
        assert "not solved" in render(exp, timeline=False)

    def test_a_directory_with_no_ledger_returns_nothing(self, tmp_path):
        assert explain_trial(tmp_path) is None

    def test_a_job_directory_finds_every_trial(self, tmp_path):
        job = tmp_path / "job"
        job.mkdir()
        for i in range(3):
            self._trial(job / f"t{i}")
        found = find_trials(job)
        assert len(found) == 3
        rendered = render_job([(n, explain_trial(p)) for n, p in found])
        assert rendered.count("finished") == 3

    def test_a_single_trial_is_found_directly(self, tmp_path):
        trial = self._trial(tmp_path)
        assert [n for n, _ in find_trials(trial)] == [trial.name]


class TestRendering:
    def test_an_empty_ledger_renders_without_crashing(self):
        assert "?" in render(explain([]), timeline=False)

    def test_the_timeline_marks_where_context_was_compacted(self):
        chain = _a_run(_chain())
        chain.append("context.compacted",
                     {"turn": 2, "evicted": 7, "rendered_before": 31_000,
                      "rendered_after": 25_000, "allowance": 28_000},
                     TrustLabel.TRUSTED_LOCAL)
        rendered = render(explain(chain.events))
        assert "compacted 7 episodes, 31000 -> 25000" in rendered

    def test_the_timeline_marks_breakers(self):
        chain = _a_run(_chain())
        chain.append("loop.breaker", {"kind": "repeat", "turn": 2,
                                      "detail": "identical action 4x"},
                     TrustLabel.TRUSTED_LOCAL)
        rendered = render(explain(chain.events))
        assert "<- repeat" in rendered
        assert "identical action 4x" in rendered

    def test_a_clean_run_says_nothing_was_refused(self):
        chain = _chain()
        chain.append("run.started", {"run_id": "r", "model": "m"}, TrustLabel.TRUSTED_USER)
        _model_call(chain, 1, ["bash"], input_tokens=100)
        assert "refused    nothing" in render(explain(chain.events), timeline=False)

    def test_both_views_report_the_same_turn_count(self):
        """`render` excluded the probe bucket and `render_job` did not, so one
        run reported 40 turns in the detail view and 41 in the table."""
        exp = explain(_a_run(_chain()).events)
        assert len(exp.real_turns) == 2
        assert " 2 turns" in render(exp, timeline=False)
        row = render_job([("t", exp)]).splitlines()[1]
        assert row.split()[2] == "2"

    def test_the_job_table_has_one_row_per_trial_plus_a_header(self):
        exp = explain(_a_run(_chain()).events)
        rendered = render_job([("alpha", exp), ("beta", exp)])
        assert len(rendered.splitlines()) == 3
        assert rendered.splitlines()[0].startswith("task")


def test_explanation_defaults_are_safe():
    """Constructed empty, it must still render rather than raise."""
    render(Explanation(), timeline=True)
