"""M1: persistence, the context plane, metering, and the tool budget policy.

Same discipline as the M0 suite — written to break the invariants rather than to
demonstrate them. The centrepiece is
`test_positional_eviction_would_lose_an_invariant_and_is_refused`, which shows
the failure mode the field has documented and nobody implements a defence for.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from optimus.context import (
    CompactionRefused,
    ContextBudget,
    ContextWindow,
    Episode,
    EpisodeKind,
)
from optimus.gate import (
    CapabilityRequest,
    Gate,
    Reversibility,
    Verb,
    Verdict,
    WorkspaceResolver,
    baseline_policy,
)
from optimus.ledger import (
    AgentKey,
    Chain,
    DurableChain,
    LedgerStore,
    OwnerKey,
    TrustLabel,
    attest,
)
from optimus.meter import aggregate, suite
from optimus.tools import ToolBudgetPolicy, ToolMode, ToolSpec


# ---------------------------------------------------------------------------
# persistence — audit.md §2.8
# ---------------------------------------------------------------------------

def test_chain_survives_restart(tmp_path: Path):
    """Bellona's ledger was a Vec in memory; a restart destroyed the evidence,
    which was also the only way to clear its one-way veto."""
    db = tmp_path / "ledger.db"
    key = AgentKey.load_or_create(tmp_path / "agent.key")

    with LedgerStore(db) as store:
        chain = DurableChain(key, store)
        chain.append("a", {"i": 0}, TrustLabel.TRUSTED_USER)
        chain.append("b", {"i": 1}, TrustLabel.TRUSTED_USER)
        head = chain.head_hash

    with LedgerStore(db) as store:
        resumed = DurableChain(AgentKey.load_or_create(tmp_path / "agent.key"), store)
        assert len(store) == 2
        assert resumed.head_hash == head
        ev = resumed.append("c", {"i": 2}, TrustLabel.TRUSTED_USER)
        assert ev.seq == 2 and ev.prev_hash == head


def test_store_refuses_update_and_delete(tmp_path: Path):
    """Triggers, not just an API that omits the verbs."""
    with LedgerStore(tmp_path / "l.db") as store:
        DurableChain(AgentKey.generate(), store).append("a", {}, TrustLabel.TRUSTED_USER)
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute("UPDATE events SET kind='forged' WHERE seq=0")
        with pytest.raises(sqlite3.IntegrityError):
            store._db.execute("DELETE FROM events WHERE seq=0")


def test_persisted_chain_verifies_and_detects_tampering(tmp_path: Path):
    owner = OwnerKey.generate()
    db = tmp_path / "l.db"
    with LedgerStore(db) as store:
        chain = DurableChain(AgentKey.generate(), store)
        for i in range(3):
            chain.append("row", {"i": i}, TrustLabel.TRUSTED_USER)
        store.put_checkpoint(attest(owner, store.events()))
        assert store.verify(expected_owner_fingerprint=owner.fingerprint).fully_valid

    # Tampering has to defeat the triggers first; even then the chain catches it.
    raw = sqlite3.connect(db)
    raw.executescript("DROP TRIGGER events_no_update; UPDATE events SET payload='{\"i\":99}' WHERE seq=1;")
    raw.commit()
    raw.close()

    with LedgerStore(db) as store:
        rep = store.verify(expected_owner_fingerprint=owner.fingerprint)
        assert not rep.chain_valid and not rep.fully_valid


def test_gate_works_against_a_durable_chain(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hi", encoding="utf-8")
    with LedgerStore(tmp_path / "l.db") as store:
        gate = Gate(DurableChain(AgentKey.generate(), store),
                    baseline_policy(), WorkspaceResolver(ws))
        gate.submit(CapabilityRequest(
            actor="a", verb=Verb.READ, trust=TrustLabel.TRUSTED_USER,
            reversibility=Reversibility.OVERLAY, tool="read_file", target_spec="notes.txt",
        ))
        assert [e.kind for e in store.events()] == ["gate.decision"]


# ---------------------------------------------------------------------------
# the context plane — research.md §4.5
# ---------------------------------------------------------------------------

def _loaded_window(budget: ContextBudget | None = None) -> ContextWindow:
    w = ContextWindow(budget or ContextBudget(total=4_000, reserve_output=1_000, keep_recent=3))
    w.push(EpisodeKind.CONTRACT, "Ship the release notes for v2.")
    w.push(EpisodeKind.INVARIANT, "Never write outside the workspace.")
    w.push(EpisodeKind.INVARIANT, "Never send email without assent.")
    for i in range(30):
        w.push(EpisodeKind.THOUGHT, f"thinking about step {i} " + "x" * 200)
        w.push(EpisodeKind.OBSERVATION, f"observation {i} " + "y" * 200)
    return w


def test_invariants_and_contract_are_never_evicted():
    w = _loaded_window()
    before = {e.id for e in w.episodes if e.kind in (EpisodeKind.INVARIANT, EpisodeKind.CONTRACT)}
    report = w.compact()
    after = {e.id for e in w.episodes}
    assert report.evicted > 0
    assert before <= after
    assert report.invariants_preserved and report.contract_preserved


def test_compaction_gets_under_budget():
    w = _loaded_window()
    assert w.over_budget()
    report = w.compact()
    assert w.used() <= w.budget.fillable
    assert report.within_budget
    assert report.tokens_saved > 0


def test_a_dependency_from_turn_three_survives_to_turn_thirty():
    """The whole point of dependency links. Positional eviction drops this;
    priority-with-closure keeps it."""
    w = ContextWindow(ContextBudget(total=3_000, reserve_output=500, keep_recent=2))
    w.push(EpisodeKind.CONTRACT, "task")
    decision = w.push(EpisodeKind.OBSERVATION, "the API key lives in vault/prod " + "z" * 100)
    for i in range(40):
        w.push(EpisodeKind.THOUGHT, f"noise {i} " + "n" * 150)
    final = w.push(EpisodeKind.ACTION, "use the key", depends_on=frozenset({decision.id}))

    w.compact()
    surviving = {e.id for e in w.episodes}
    assert decision.id in surviving, "a load-bearing observation was dropped"
    assert final.id in surviving


def test_errors_outlive_successful_observations():
    w = ContextWindow(ContextBudget(total=2_500, reserve_output=500, keep_recent=1))
    w.push(EpisodeKind.CONTRACT, "task")
    err = w.push(EpisodeKind.ERROR, "build failed: missing symbol foo " + "e" * 100)
    for i in range(30):
        w.push(EpisodeKind.OBSERVATION, f"ok {i} " + "o" * 150)
    w.compact()
    assert err.id in {e.id for e in w.episodes}


def test_positional_eviction_would_lose_an_invariant_and_is_refused():
    """The Governance Decay failure, staged.

    A window that protects only the recent tail — Achilles's approach, and every
    reactive condenser's — evicts a safety constraint stated early in the run.
    The validation pass catches it and the compaction is *refused* rather than
    silently shipped.
    """

    class PositionalWindow(ContextWindow):
        """A reactive condenser: protect the recent tail, drop the oldest."""

        def _protected_ids(self):
            return {e.id for e in self.episodes[-self.budget.keep_recent:]}

        def _eviction_order(self, candidates):
            return sorted(candidates, key=lambda e: e.seq)

    w = PositionalWindow(ContextBudget(total=2_000, reserve_output=500, keep_recent=2))
    w.push(EpisodeKind.INVARIANT, "Never delete the production database.")
    for i in range(30):
        w.push(EpisodeKind.THOUGHT, f"step {i} " + "s" * 150)

    with pytest.raises(CompactionRefused) as exc:
        w.compact()
    assert "invariants=False" in str(exc.value)
    # And the window is untouched: a refused compaction changes nothing.
    assert any(e.kind is EpisodeKind.INVARIANT for e in w.episodes)


def test_compaction_is_deterministic():
    """Same history, same result — otherwise a replay is not a replay."""
    a, b = _loaded_window(), _loaded_window()
    ra, rb = a.compact(), b.compact()
    assert ra.evicted == rb.evicted and ra.by_kind == rb.by_kind
    assert [(e.kind, e.content) for e in a.episodes] == [(e.kind, e.content) for e in b.episodes]


def test_the_note_counts_rather_than_summarises():
    w = _loaded_window()
    report = w.compact()
    note = report.note()
    assert str(report.evicted) in note
    assert report.summary_id in note
    assert "thought" in note or "observation" in note


def test_summary_carries_lineage_for_everything_it_replaced():
    w = _loaded_window()
    report = w.compact()
    summary = next(e for e in w.episodes if e.id == report.summary_id)
    assert len(summary.lineage) == report.evicted


def test_ensure_fits_is_a_no_op_when_under_budget():
    w = ContextWindow(ContextBudget(total=100_000, reserve_output=1_000))
    w.push(EpisodeKind.CONTRACT, "small")
    assert w.ensure_fits() is None
    assert len(w.episodes) == 1


def test_pinned_episode_is_permanent_whatever_its_kind():
    w = ContextWindow(ContextBudget(total=2_000, reserve_output=500, keep_recent=1))
    w.push(EpisodeKind.CONTRACT, "task")
    pin = w.push(EpisodeKind.THOUGHT, "remember this exactly " + "p" * 100, pinned=True)
    for i in range(30):
        w.push(EpisodeKind.THOUGHT, f"noise {i} " + "n" * 150)
    w.compact()
    assert pin.id in {e.id for e in w.episodes}


# ---------------------------------------------------------------------------
# metering — research.md §2.1
# ---------------------------------------------------------------------------

def test_meter_reads_cost_back_out_of_the_ledger(tmp_path: Path):
    from optimus.ledger.events import Meter

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.txt").write_text("x", encoding="utf-8")
    gate = Gate(Chain(AgentKey.generate()), baseline_policy(), WorkspaceResolver(ws))

    out = gate.submit(CapabilityRequest(
        actor="a", verb=Verb.READ, trust=TrustLabel.TRUSTED_USER,
        reversibility=Reversibility.OVERLAY, tool="read_file", target_spec="f.txt",
    ))
    gate.settle(out.handle, ok=True, meter=Meter(input_tokens=9_000, output_tokens=400, wall_ms=120))
    gate.submit(CapabilityRequest(
        actor="a", verb=Verb.WRITE, trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT,
        reversibility=Reversibility.COMPENSATION, tool="write_file", target_spec="../escape.txt",
    ))

    m = aggregate(gate.chain.events, solved=True)
    assert m.total_tokens == 9_400
    assert m.denials == 1
    assert m.tokens_per_solved_task == 9_400.0


def test_unsolved_run_costs_infinity():
    m = aggregate([], solved=False)
    assert m.tokens_per_solved_task == float("inf")


def test_suite_charges_failures_to_the_solved_tasks():
    """Tokens burned on failures are part of what a solved task cost. Averaging
    per-run figures would hide them."""
    from optimus.meter import RunMeter

    s = suite([
        RunMeter(input_tokens=10_000, solved=True),
        RunMeter(input_tokens=30_000, solved=False),
        RunMeter(input_tokens=10_000, solved=True),
    ])
    assert s.pass_rate == pytest.approx(2 / 3)
    assert s.tokens_per_solved_task == 25_000.0


def test_no_action_turns_are_counted():
    from optimus.ledger.events import Event, Meter

    events = [
        Event(seq=0, ts_ms=0, kind="effect.settled", trust=TrustLabel.EXECUTION_RESULT,
              payload={"ok": True, "meter": Meter(no_action=True).as_payload()},
              prev_hash="genesis", hash="h0"),
    ]
    assert aggregate(events).no_action_turns == 1


# ---------------------------------------------------------------------------
# tool budget policy — research.md §4.4
# ---------------------------------------------------------------------------

def _specs(n: int, *, big: bool = False) -> list[ToolSpec]:
    schema = {"path": "string"} if not big else {f"field_{i}": "string" for i in range(40)}
    return [ToolSpec(f"tool_{i}", "does a thing" * (20 if big else 1), schema) for i in range(n)]


def test_small_surface_unchained_uses_direct():
    p = ToolBudgetPolicy(ContextBudget(total=128_000))
    d = p.choose(_specs(4), expected_calls=1)
    assert d.mode is ToolMode.DIRECT
    assert d.schema_tokens <= d.allowance


def test_large_surface_defers_to_search():
    p = ToolBudgetPolicy(ContextBudget(total=8_000))
    d = p.choose(_specs(120, big=True), expected_calls=1)
    assert d.mode is ToolMode.SEARCH
    assert d.schema_tokens > d.allowance
    assert "tool_0 --" in d.rendered


def test_chained_work_uses_code_mode():
    p = ToolBudgetPolicy(ContextBudget(total=128_000))
    d = p.choose(_specs(6), expected_calls=8)
    assert d.mode is ToolMode.CODE
    assert "def tool_0(" in d.rendered


def test_single_tool_never_pays_code_mode_overhead():
    """Research is explicit that discovery-inspect-execute is a loss for a tiny
    surface, however chained the task is."""
    p = ToolBudgetPolicy(ContextBudget(total=128_000))
    d = p.choose(_specs(1), expected_calls=10)
    assert d.mode is ToolMode.DIRECT


def test_facades_are_generated_from_the_same_specs():
    p = ToolBudgetPolicy(ContextBudget(total=128_000))
    specs = [ToolSpec("grep", "search", {"pattern": "string", "limit": 10, "regex": True})]
    d = p.choose(specs + _specs(3), expected_calls=5)
    assert d.mode is ToolMode.CODE
    assert "def grep(limit: int, pattern: str, regex: bool)" in d.rendered


def test_evicted_episode_stays_recoverable_through_lineage():
    """The honest answer to a limitation rather than a claim it does not exist.

    A dependency link protects an episode only if it exists when compaction runs.
    A fact that becomes load-bearing 100 turns later is evicted — but the summary
    that replaced it names its id, so it is recoverable rather than gone.
    """
    w = ContextWindow(ContextBudget(total=2_000, reserve_output=500, keep_recent=2))
    w.push(EpisodeKind.CONTRACT, "task")
    fact = w.push(EpisodeKind.OBSERVATION, "the key is in vault/prod " + "v" * 150)
    # Same kind as the fact, so salience cannot save it and the tie breaks on
    # age — the fact is the oldest observation, and nothing yet depends on it.
    for i in range(30):
        w.push(EpisodeKind.OBSERVATION, f"noise {i} " + "n" * 150)

    w.compact()
    assert fact.id not in {e.id for e in w.episodes}, "unprotected old episode should evict"
    assert w.covers(fact.id), "but a summary must still name it"

    back = w.rehydrate(fact)
    assert back.pinned and back.id == fact.id
    assert fact.id in {e.id for e in w.episodes}


def test_rehydrated_episode_survives_the_next_compaction():
    w = ContextWindow(ContextBudget(total=2_000, reserve_output=500, keep_recent=2))
    w.push(EpisodeKind.CONTRACT, "task")
    fact = w.push(EpisodeKind.OBSERVATION, "load-bearing " + "v" * 150)
    for i in range(30):
        w.push(EpisodeKind.OBSERVATION, f"noise {i} " + "n" * 150)
    w.compact()
    assert fact.id not in {e.id for e in w.episodes}
    w.rehydrate(fact)
    for i in range(30):
        w.push(EpisodeKind.OBSERVATION, f"more noise {i} " + "n" * 150)
    w.compact()
    assert fact.id in {e.id for e in w.episodes}
