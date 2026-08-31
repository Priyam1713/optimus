"""Telling the model how much runway it has left.

The ten-task run found the model flying blind. Two solved tasks ran `bash`
straight into turn 40 and never called `finish`; a third called it *on* turn 40,
the last one available. Nothing in the prompt or the conversation had ever said
how many turns there were, so the model could not tell turn 3 from turn 39.

The tests here are mostly about what the notice refuses to do. Finishing early
buys **tokens, never score** — the verifier grades the container either way,
which is why both tasks that ran out of turns were still marked solved. So a
notice that talked the model into concluding would trade solves for tokens, and
`break-filter-js-from-html` (called `finish` at turn 19, had not solved it)
shows that failure mode is already live.
"""

from __future__ import annotations

from optimus.loop.agent import LoopLimits
from tests.test_m4 import _loop, _reply


def _work(n: int) -> list:
    """`n` turns of distinct work.

    Distinct matters: identical actions trip the repeat breaker and stop the run
    at "looping" around turn 7, long before any budget mark. The first draft of
    this file used `ls` twelve times and the notice appeared to be broken.
    """
    return [_reply(("bash", {"command": f"ls dir{i}"})) for i in range(n)]


def _messages_text(loop) -> str:
    return "\n".join(
        str(m.get("content", "")) for m in loop.messages() if m["role"] == "user"
    )


def _notices(loop) -> list[dict]:
    return [
        e.payload for e in loop.gate.chain.events if e.kind == "loop.budget_notice"
    ]


# --------------------------------------------------------------------------
# the standing budget, in the system prompt
# --------------------------------------------------------------------------

def test_the_system_prompt_states_the_turn_budget():
    """The cheapest half of the fix: say the number once, up front."""
    loop = _loop([_reply(("finish", {"summary": "done"}))],
                 limits=LoopLimits(max_turns=40))
    system = str(loop._system_content())
    assert "at most 40 turns" in system


def test_the_budget_line_tracks_the_actual_limit():
    loop = _loop([_reply(("finish", {"summary": "done"}))],
                 limits=LoopLimits(max_turns=7))
    assert "at most 7 turns" in str(loop._system_content())


def test_the_system_prompt_is_still_stable_across_turns():
    """It has to stay a constant prefix or it stops being cacheable, and cache
    hits are the only reason a 40-turn run is affordable at all (89.9% on the
    ten-task run)."""
    loop = _loop([*_work(3), _reply(("finish", {"summary": "done"}))],
                 limits=LoopLimits(max_turns=6))
    before = str(loop._system_content())
    loop.run("go")
    assert str(loop._system_content()) == before


# --------------------------------------------------------------------------
# the running notices
# --------------------------------------------------------------------------

def test_a_notice_fires_as_the_budget_runs_low():
    loop = _loop(_work(12),
                 limits=LoopLimits(max_turns=12, budget_notices=(3,)))
    loop.run("go")

    notices = _notices(loop)
    assert len(notices) == 1
    # remaining = max_turns - turn + 1, so 3 remain at turn 10.
    assert notices[0]["turn"] == 10
    assert notices[0]["remaining"] == 3
    assert notices[0]["mark"] == 3
    # Every ledger row is stamped with the run it belongs to.
    assert notices[0]["run_id"] == "run-1"


def test_the_notice_reaches_the_model_not_just_the_ledger():
    loop = _loop(_work(12),
                 limits=LoopLimits(max_turns=12, budget_notices=(3,)))
    loop.run("go")
    text = _messages_text(loop)
    assert "Turn 10 of 12" in text
    assert "3 remain" in text


def test_each_mark_fires_exactly_once():
    loop = _loop(_work(20),
                 limits=LoopLimits(max_turns=20, budget_notices=(10, 3)))
    loop.run("go")

    marks = [n["mark"] for n in _notices(loop)]
    assert marks == [10, 3]
    assert len(marks) == len(set(marks))


def test_no_notice_when_the_run_ends_before_the_mark():
    """A task solved in four turns should never hear about the budget."""
    loop = _loop([*_work(3), _reply(("finish", {"summary": "done"}))],
                 limits=LoopLimits(max_turns=40, budget_notices=(10, 3)))
    out = loop.run("go")
    assert out.stop_reason == "finished"
    assert _notices(loop) == []
    assert "[harness] Turn" not in _messages_text(loop)


def test_a_mark_at_or_above_the_whole_budget_is_skipped():
    """Otherwise a short run is warned it is nearly over on turn 1, which is
    noise dressed as a warning."""
    loop = _loop(_work(5),
                 limits=LoopLimits(max_turns=5, budget_notices=(10, 3)))
    loop.run("go")
    assert [n["mark"] for n in _notices(loop)] == [3]
    assert all(n["turn"] > 1 for n in _notices(loop))


def test_notices_can_be_turned_off_entirely():
    loop = _loop(_work(12),
                 limits=LoopLimits(max_turns=12, budget_notices=()))
    loop.run("go")
    assert _notices(loop) == []


def test_the_notice_is_counted_before_compaction_decides():
    """It is context like any other. Adding it after the budget check would let
    the turn's request exceed the allowance the plane just enforced — the shape
    of every context finding in this project (M3-13..16)."""
    loop = _loop(_work(12),
                 limits=LoopLimits(max_turns=12, budget_notices=(3,)))
    loop.run("go")

    rows = {e.payload["turn"]: e.payload
            for e in loop.gate.chain.events if e.kind == "context.turn"}
    # The notice lands at turn 10; the context row for that turn is written
    # after it, so the episode count already includes it.
    assert rows[10]["episodes"] > rows[9]["episodes"]


# --------------------------------------------------------------------------
# what it must not do
# --------------------------------------------------------------------------

def test_the_notice_states_facts_and_does_not_urge_a_conclusion():
    """The restraint is the design. Finishing early buys tokens, never score,
    so a notice that talked the model into concluding would trade solves for
    tokens — and a premature `finish` has already happened once for real."""
    loop = _loop(_work(12),
                 limits=LoopLimits(max_turns=12, budget_notices=(3,)))
    loop.run("go")
    text = _messages_text(loop).lower()

    for forbidden in (
        "you are probably done",
        "you appear to be finished",
        "the task looks complete",
        "wrap up",
        "you should call `finish` now",
    ):
        assert forbidden not in text
    # It does say what happens at the ceiling, which is the fact being supplied.
    assert "the run" in text and "stops" in text


def test_the_notice_does_not_stop_the_run_by_itself():
    """Informing the model is not the same as ending its turn. A notice that
    also broke the loop would be a turn ceiling with extra steps."""
    loop = _loop(_work(12),
                 limits=LoopLimits(max_turns=12, budget_notices=(10, 3)))
    out = loop.run("go")
    assert out.turns == 12
    assert out.stop_reason == "max_turns"


def test_the_notice_is_evictable_and_not_an_invariant():
    """A transient fact about runway must not become uncompactable for the rest
    of the run, the way a standing rule is."""
    from optimus.context.episodes import EpisodeKind

    loop = _loop(_work(12),
                 limits=LoopLimits(max_turns=12, budget_notices=(3,)))
    loop.run("go")
    for episode in loop.window.episodes:
        if "[harness] Turn" in episode.content:
            assert episode.kind is EpisodeKind.OBSERVATION
            assert not episode.pinned
            break
    else:
        raise AssertionError("the notice never entered the window")


def test_a_run_that_finishes_normally_is_unaffected():
    loop = _loop([_reply(("bash", {"command": "ls"})),
                  _reply(("finish", {"summary": "did the thing"}))],
                 limits=LoopLimits(max_turns=40))
    out = loop.run("go")
    assert out.stop_reason == "finished"
    assert out.summary == "did the thing"
