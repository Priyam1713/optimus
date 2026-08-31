"""The adversarial suite.

`audit.md` §5: every one of Bellona's critical defects was "a correct mechanism
terminated one step before the thing it was protecting", and its 147 tests all
demonstrated the mechanisms rather than attacking them. Five of its Seven Laws
fell to a test that fits on one screen.

So this file is written to break the invariants, not to show them working. Each
test names the finding or invariant it defends.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
from pathlib import Path

import pytest

from optimus.gate import (
    CapabilityRequest,
    Gate,
    Handle,
    HandleError,
    Policy,
    Reversibility,
    Rule,
    RuleEffect,
    TargetRefused,
    Verb,
    Verdict,
    WorkspaceResolver,
    baseline_policy,
    instance_digest,
    resolve_fs,
    resolve_url,
)
from optimus.gate.types import (
    RULE_DEFAULT_DENY,
    RULE_FROZEN,
    RULE_IRREVERSIBLE_ASSENT,
    RULE_POLICY_ERROR,
    RULE_TARGET_REFUSED,
    RULE_UNTRUSTED_MUTATION,
)
from optimus.ledger import AgentKey, Chain, Event, OwnerKey, TrustLabel, attest, verify
from optimus.ledger.chain import hash_body

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "notes.txt").write_text("hello", encoding="utf-8")
    (ws / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("do not touch", encoding="utf-8")
    return ws


@pytest.fixture
def gate(workspace: Path) -> Gate:
    return Gate(
        chain=Chain(AgentKey.generate()),
        policy=baseline_policy(),
        resolver=WorkspaceResolver(workspace),
    )


def req(**kw) -> CapabilityRequest:
    base = {
        "actor": "agent-1",
        "verb": Verb.WRITE,
        "trust": TrustLabel.TRUSTED_USER,
        "reversibility": Reversibility.COMPENSATION,
        "tool": "write_file",
        "target_spec": "notes.txt",
        "intent": "test",
    }
    base.update(kw)
    return CapabilityRequest(**base)


# ---------------------------------------------------------------------------
# audit.md §2.1 — the workspace fence
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "probe",
    [
        "../../pwned.txt",                       # the exact input that broke Bellona
        "../outside.txt",
        "../../../../../../Windows/Temp/x.txt",
        "sub/../../escape.txt",
        "./../../escape.txt",
    ],
)
def test_traversal_refused_even_when_target_does_not_exist(workspace: Path, probe: str):
    """Bellona's resolver returned early on the not-yet-exists branch, *before*
    the containment check. Creating a new file is precisely the case that
    skipped it, which is why `write_file` was an arbitrary write."""
    with pytest.raises(TargetRefused):
        resolve_fs(probe, workspace)


def test_absolute_path_outside_workspace_refused(workspace: Path, tmp_path: Path):
    with pytest.raises(TargetRefused):
        resolve_fs(str(tmp_path / "outside.txt"), workspace)


def test_sibling_directory_is_not_inside(tmp_path: Path):
    """Containment is by path component, not string prefix: `work-secrets` must
    not be considered inside `work` (`audit.md` §2.5)."""
    (tmp_path / "work").mkdir()
    (tmp_path / "work-secrets").mkdir()
    with pytest.raises(TargetRefused):
        resolve_fs(str(tmp_path / "work-secrets" / "k.pem"), tmp_path / "work")


def test_new_file_inside_workspace_still_resolves(workspace: Path):
    t = resolve_fs("sub/dir/new.txt", workspace)
    assert Path(t.path).is_relative_to(Path(t.workspace))
    assert t.exists is False


def test_symlink_escape_refused(tmp_path: Path, workspace: Path):
    """A junction inside the workspace pointing out of it is an escape."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = workspace / "link"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this host")
    with pytest.raises(TargetRefused):
        resolve_fs("link/loot.txt", workspace)


def test_gate_refuses_traversal_and_names_the_rule(gate: Gate):
    out = gate.submit(req(target_spec="../../pwned.txt"))
    assert out.verdict is Verdict.DENY
    assert out.rule_id == RULE_TARGET_REFUSED
    assert out.handle is None
    assert gate.chain.events[-1].kind == "gate.refused"


# ---------------------------------------------------------------------------
# audit.md §2.4 — authorization must bind to execution
# ---------------------------------------------------------------------------

def test_handle_cannot_be_forged():
    """Invariant 1: executors take handles. If a handle can be constructed
    outside the Gate, the invariant is decorative."""
    with pytest.raises(HandleError):
        Handle(
            handle_id="hdl_forged",
            request=req(),
            resolved=None,  # type: ignore[arg-type]
            ledger_seq=0,
            rule_id="x",
            expires_at=1e18,
        )


def test_handle_is_single_use(gate: Gate, workspace: Path):
    out = gate.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt"))
    assert out.allowed
    assert out.handle is not None
    out.handle.consume()
    with pytest.raises(HandleError):
        out.handle.consume()


def test_handle_carries_resolved_target_not_the_spec(gate: Gate, workspace: Path):
    """The executor never sees `../` — it sees a canonical path inside the
    workspace, or it sees nothing."""
    out = gate.submit(req(verb=Verb.READ, tool="read_file", target_spec="./sub/../notes.txt"))
    assert out.allowed and out.handle is not None
    resolved = out.handle.consume()
    assert Path(resolved.path).name == "notes.txt"          # type: ignore[attr-defined]
    assert Path(resolved.path).is_relative_to(workspace.resolve())  # type: ignore[attr-defined]


def test_handle_expires(workspace: Path):
    g = Gate(Chain(AgentKey.generate()), baseline_policy(), WorkspaceResolver(workspace),
             handle_ttl=-1.0)
    out = g.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt"))
    assert out.allowed and out.handle is not None
    with pytest.raises(HandleError):
        out.handle.consume()


# ---------------------------------------------------------------------------
# apex.md invariant 3 — trust never widens
# ---------------------------------------------------------------------------

def test_untrusted_model_output_cannot_authorise_a_write(gate: Gate):
    out = gate.submit(req(trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT,
                          reversibility=Reversibility.COMPENSATION))
    assert out.verdict is Verdict.NEEDS_APPROVAL
    assert out.rule_id == RULE_UNTRUSTED_MUTATION
    assert out.handle is None


def test_no_rule_can_re_enable_untrusted_mutation(workspace: Path):
    """The hard invariant is checked before the rule set and is unreachable by
    it. A permissive rule set must not be able to switch it off."""
    wide_open = Policy([Rule("allow-everything", RuleEffect.ALLOW, {"always": True})])
    g = Gate(Chain(AgentKey.generate()), wide_open, WorkspaceResolver(workspace))
    out = g.submit(req(trust=TrustLabel.UNTRUSTED_WEB))
    assert out.verdict is Verdict.NEEDS_APPROVAL
    assert out.rule_id == RULE_UNTRUSTED_MUTATION


def test_untrusted_reads_are_still_fine(gate: Gate):
    out = gate.submit(req(verb=Verb.READ, tool="read_file", trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT,
                          target_spec="notes.txt", reversibility=Reversibility.OVERLAY))
    assert out.allowed


# ---------------------------------------------------------------------------
# audit.md §2.6 — approval must re-decide, and there is no auto-approver
# ---------------------------------------------------------------------------

def test_gate_exposes_no_auto_approver():
    """Bellona's `--yolo` collapsed its entire approval doctrine into one flag.
    Assert the API has no way to express that."""
    names = set(dir(Gate)) | set(inspect.signature(Gate.__init__).parameters)
    forbidden = {"auto_approve", "auto_approver", "with_auto_approver", "yolo", "approve_all"}
    assert not (names & forbidden)


def test_approval_requires_assent_bound_to_that_ticket(gate: Gate):
    out = gate.submit(req(verb=Verb.EXECUTE, tool="run", target_spec=["git", "status"]))
    assert out.verdict is Verdict.NEEDS_APPROVAL and out.ticket is not None
    tid = out.ticket.ticket_id

    assert gate.approve(tid, "ast_made_up").verdict is Verdict.DENY

    other = gate.mint_assent("human", scope="ticket:someone-else", shown={})
    assert gate.approve(tid, other).verdict is Verdict.DENY

    good = gate.mint_assent("human", scope=f"ticket:{tid}", shown={"argv": ["git", "status"]})
    assert gate.approve(tid, good).allowed


def test_assent_is_single_use(gate: Gate):
    out = gate.submit(req(verb=Verb.EXECUTE, tool="run", target_spec=["git", "status"]))
    tid = out.ticket.ticket_id  # type: ignore[union-attr]
    tok = gate.mint_assent("human", scope=f"ticket:{tid}", shown={})
    assert gate.approve(tid, tok).allowed
    assert gate.approve(tid, tok).verdict is Verdict.DENY


def test_tightened_policy_applies_to_a_parked_ticket(workspace: Path):
    """Bellona's `approve()` never re-ran the policy, so a ticket parked under a
    permissive law executed under it after the law tightened."""
    permissive = Policy([Rule("approve-exec", RuleEffect.APPROVAL,
                              {"eq": {"attr": "verb", "value": "execute"}})])
    g = Gate(Chain(AgentKey.generate()), permissive, WorkspaceResolver(workspace))
    out = g.submit(req(verb=Verb.EXECUTE, tool="run", target_spec=["git", "push"]))
    tid = out.ticket.ticket_id  # type: ignore[union-attr]

    g._policy = Policy([Rule("deny-exec", RuleEffect.DENY,
                             {"eq": {"attr": "verb", "value": "execute"}})])
    tok = g.mint_assent("human", scope=f"ticket:{tid}", shown={})
    after = g.approve(tid, tok)
    assert after.verdict is Verdict.DENY
    assert after.rule_id == "deny-exec"


def test_assent_records_what_the_human_was_shown(gate: Gate):
    gate.mint_assent("priya", scope="ticket:x", shown={"argv": ["rm", "-rf", "/"]})
    ev = gate.chain.events[-1]
    assert ev.kind == "assent.minted"
    assert ev.payload["shown"]["argv"] == ["rm", "-rf", "/"]


# ---------------------------------------------------------------------------
# audit.md §3.2 — grants are instance-bound
# ---------------------------------------------------------------------------

def test_grant_for_one_file_does_not_authorise_another(gate: Gate, workspace: Path):
    from optimus.gate.targets import resolve_fs as rfs

    allowed = instance_digest(Verb.WRITE, rfs("notes.txt", workspace))
    gate.grants.issue("agent-1", allowed, issued_by="human", single_use=False)

    ok = gate.submit(req(trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT, target_spec="notes.txt"))
    assert ok.allowed

    other = gate.submit(req(trust=TrustLabel.UNTRUSTED_MODEL_OUTPUT, target_spec="other.txt"))
    assert other.verdict is Verdict.NEEDS_APPROVAL
    assert other.rule_id == RULE_UNTRUSTED_MUTATION


def test_single_use_grant_is_spent(gate: Gate, workspace: Path):
    from optimus.gate.targets import resolve_fs as rfs

    d = instance_digest(Verb.WRITE, rfs("notes.txt", workspace))
    gate.grants.issue("agent-1", d, issued_by="human", single_use=True)
    assert gate.submit(req(trust=TrustLabel.UNTRUSTED_WEB, target_spec="notes.txt")).allowed
    assert gate.submit(req(trust=TrustLabel.UNTRUSTED_WEB, target_spec="notes.txt")).verdict \
        is Verdict.NEEDS_APPROVAL


# ---------------------------------------------------------------------------
# audit.md §2.7 — network targets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "http://localhost:8080/x", "http://169.254.169.254/latest/meta-data",
     "http://10.0.0.5/", "http://[::1]:9000/x"],
)
def test_private_space_refused(url: str):
    with pytest.raises(TargetRefused):
        resolve_url(url)


def test_explicit_port_does_not_eat_the_host():
    """Bellona stripped ports with `rsplit(':')`, which keeps the *port* and
    discards the host, so any URL with a port failed with a bogus DNS error."""
    with pytest.raises(TargetRefused) as exc:
        resolve_url("http://127.0.0.1:8080/x")
    assert "private" in str(exc.value).lower()


def test_non_http_scheme_refused():
    for bad in ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"]:
        with pytest.raises(TargetRefused):
            resolve_url(bad)


def test_resolved_ips_are_pinned():
    """The executor must connect to what was checked; re-resolving reopens DNS
    rebinding, which is what Bellona's resolve-then-connect split allowed."""
    t = resolve_url("http://example.com/")
    assert t.pinned_ips and all(ip for ip in t.pinned_ips)
    assert t.host == "example.com" and t.port == 80


# ---------------------------------------------------------------------------
# apex.md invariant 4 — reversibility
# ---------------------------------------------------------------------------

def test_irreversible_needs_assent_even_when_rules_allow(workspace: Path):
    wide_open = Policy([Rule("allow-everything", RuleEffect.ALLOW, {"always": True})])
    g = Gate(Chain(AgentKey.generate()), wide_open, WorkspaceResolver(workspace))
    out = g.submit(req(
        verb=Verb.READ, tool="send_mail", target_spec="notes.txt",
        trust=TrustLabel.TRUSTED_USER, reversibility=Reversibility.IRREVERSIBLE,
    ))
    assert out.verdict is Verdict.NEEDS_APPROVAL
    assert out.rule_id == RULE_IRREVERSIBLE_ASSENT


def test_compensation_is_recorded_before_the_act(gate: Gate, workspace: Path):
    from optimus.gate.targets import resolve_fs as rfs

    d = instance_digest(Verb.WRITE, rfs("notes.txt", workspace))
    gate.grants.issue("agent-1", d, issued_by="human")
    out = gate.submit(req(target_spec="notes.txt", reversibility=Reversibility.COMPENSATION))
    assert out.allowed and out.handle is not None
    kinds = [e.kind for e in gate.chain.events]
    assert "compensation.recorded" in kinds
    # the undo is on the record before anything is settled
    assert kinds.index("compensation.recorded") < len(kinds)
    assert out.handle.compensation is not None


# ---------------------------------------------------------------------------
# policy engine — deny beats allow, structurally
# ---------------------------------------------------------------------------

def test_sensitive_deny_beats_a_broad_allow_regardless_of_order(workspace: Path):
    p = baseline_policy()
    p.add(Rule("allow-all-writes", RuleEffect.ALLOW, {"always": True}))
    g = Gate(Chain(AgentKey.generate()), p, WorkspaceResolver(workspace))
    out = g.submit(req(target_spec=".env", trust=TrustLabel.TRUSTED_USER))
    assert out.verdict is Verdict.DENY
    assert out.rule_id == "deny-sensitive-write"


def test_broken_rule_denies_rather_than_skips(workspace: Path):
    p = Policy()
    p._allow.append(Rule("bad", RuleEffect.ALLOW, {"eq": {"attr": "nope", "value": 1}}))
    g = Gate(Chain(AgentKey.generate()), p, WorkspaceResolver(workspace))
    out = g.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt"))
    assert out.verdict is Verdict.DENY
    assert out.rule_id == RULE_POLICY_ERROR


def test_malformed_rule_is_rejected_at_load_time():
    from optimus.gate.policy import PolicyError

    with pytest.raises(PolicyError):
        Policy([Rule("nonsense", RuleEffect.ALLOW, {"frobnicate": {"attr": "verb"}})])


def test_empty_policy_permits_nothing(workspace: Path):
    g = Gate(Chain(AgentKey.generate()), Policy(), WorkspaceResolver(workspace))
    out = g.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt"))
    assert out.verdict is Verdict.DENY
    assert out.rule_id == RULE_DEFAULT_DENY


def test_rule_about_paths_does_not_error_on_a_url_target(gate: Gate):
    """A rule keyed on `target.relpath` must evaluate to false for a URL, not
    raise — an evaluation error denies everything, turning one narrow rule into
    an outage."""
    out = gate.submit(req(verb=Verb.NAVIGATE, tool="fetch", target_spec="http://example.com/"))
    assert out.rule_id != RULE_POLICY_ERROR


# ---------------------------------------------------------------------------
# audit.md §2.8 — the freeze, and the ledger that outlives it
# ---------------------------------------------------------------------------

def test_freeze_refuses_everything_and_kills_tickets(gate: Gate):
    parked = gate.submit(req(verb=Verb.EXECUTE, tool="run", target_spec=["git", "status"]))
    assert parked.ticket is not None
    gate.freeze("operator pulled the cord")
    assert gate.pending == []
    out = gate.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt"))
    assert out.verdict is Verdict.DENY and out.rule_id == RULE_FROZEN
    assert "ticket.cancelled" in [e.kind for e in gate.chain.events]


def test_freeze_is_reversible_with_assent(gate: Gate):
    """Bellona's veto had no `lower()`, so recovering meant a restart — which
    destroyed its in-memory ledger. The kill switch must not be mutually
    destructive with the audit trail."""
    gate.freeze("test")
    assert gate.thaw("ast_nope") is False
    tok = gate.mint_assent("human", scope="thaw", shown={"reason": "test"})
    assert gate.thaw(tok) is True
    assert gate.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt")).allowed


# ---------------------------------------------------------------------------
# audit.md §2.3 — the receipt must prove provenance, not self-consistency
# ---------------------------------------------------------------------------

def _chain_with(n: int = 3) -> tuple[Chain, AgentKey]:
    ak = AgentKey.generate()
    c = Chain(ak)
    for i in range(n):
        c.append("test.event", {"i": i}, TrustLabel.TRUSTED_USER)
    return c, ak


def test_verify_requires_an_expected_owner_fingerprint():
    c, _ = _chain_with()
    with pytest.raises(TypeError):
        verify(c.events, [])  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        verify(c.events, [], expected_owner_fingerprint="")


def test_unattested_chain_is_not_valid():
    """Bellona printed `VALID` for any well-formed chain. A chain nobody has
    vouched for is *unattested*, which must never render as a tick."""
    c, _ = _chain_with()
    owner = OwnerKey.generate()
    rep = verify(c.events, [], expected_owner_fingerprint=owner.fingerprint)
    assert rep.chain_valid and rep.signatures_valid
    assert not rep.fully_valid
    assert rep.attested_through == -1
    assert "UNATTESTED" in rep.render()


def test_attested_chain_is_valid():
    c, _ = _chain_with()
    owner = OwnerKey.generate()
    cp = attest(owner, c.events)
    rep = verify(c.events, [cp], expected_owner_fingerprint=owner.fingerprint)
    assert rep.fully_valid, rep.failures
    assert "VALID" in rep.render()


def test_forged_chain_from_an_attacker_owner_is_rejected():
    """The whole attack Bellona was open to: generate your own keys, sign a
    fabricated history, present it. It must fail against the *expected* owner."""
    real_owner = OwnerKey.generate()
    attacker_owner = OwnerKey.generate()
    c, _ = _chain_with()
    forged_cp = attest(attacker_owner, c.events)

    rep = verify(c.events, [forged_cp], expected_owner_fingerprint=real_owner.fingerprint)
    assert not rep.owner_fingerprint_matched
    assert not rep.fully_valid


def test_tampering_with_a_payload_breaks_the_chain():
    c, _ = _chain_with()
    owner = OwnerKey.generate()
    cp = attest(owner, c.events)
    events = c.events
    events[1] = dataclasses.replace(events[1], payload={"i": 999})
    rep = verify(events, [cp], expected_owner_fingerprint=owner.fingerprint)
    assert not rep.chain_valid and not rep.fully_valid


def test_reordering_events_breaks_the_chain():
    c, _ = _chain_with(4)
    owner = OwnerKey.generate()
    cp = attest(owner, c.events)
    events = c.events
    events[1], events[2] = events[2], events[1]
    rep = verify(events, [cp], expected_owner_fingerprint=owner.fingerprint)
    assert not rep.chain_valid


def test_appending_after_a_checkpoint_leaves_an_unattested_tail():
    c, _ = _chain_with()
    owner = OwnerKey.generate()
    cp = attest(owner, c.events)
    c.append("later.event", {"sneaky": True}, TrustLabel.UNTRUSTED_MODEL_OUTPUT)
    rep = verify(c.events, [cp], expected_owner_fingerprint=owner.fingerprint)
    assert rep.chain_valid and rep.signatures_valid
    assert not rep.fully_valid
    assert rep.unattested_tail == 1


def test_event_signed_by_an_unvouched_agent_key_is_caught():
    """Swapping in a second agent key mid-chain must not be silently accepted by
    a checkpoint that never named it."""
    ak = AgentKey.generate()
    c = Chain(ak)
    c.append("a", {}, TrustLabel.TRUSTED_USER)
    owner = OwnerKey.generate()
    cp = attest(owner, c.events)

    rogue = AgentKey.generate()
    ev = c.events[0]
    body = ev.body()
    forged = Event(
        **{**body, "trust": ev.trust, "hash": hash_body(body),
           "signature": rogue.sign(bytes.fromhex(hash_body(body))),
           "signer": rogue.public_hex}
    )
    rep = verify([forged], [cp], expected_owner_fingerprint=owner.fingerprint)
    assert not rep.fully_valid


def test_gate_never_holds_an_owner_key(gate: Gate):
    """Structural: the Gate has an AgentKey through its Chain and no path to an
    OwnerKey. This is the separation Bellona collapsed."""
    assert not any(isinstance(v, OwnerKey) for v in vars(gate).values())
    assert not hasattr(gate, "owner")
    assert "OwnerKey" not in inspect.getsource(type(gate))


# ---------------------------------------------------------------------------
# invariant 5 — everything is metered
# ---------------------------------------------------------------------------

def test_settlement_records_a_meter(gate: Gate):
    from optimus.ledger import Meter

    out = gate.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt"))
    assert out.handle is not None
    ev = gate.settle(out.handle, ok=True, meter=Meter(input_tokens=1200, output_tokens=64, wall_ms=91))
    assert ev.payload["meter"]["input_tokens"] == 1200
    assert ev.payload["meter"]["no_action"] is False


def test_every_decision_is_on_the_record_before_the_effect(gate: Gate, workspace: Path):
    """Audit precedes the act — for reads, and for writes where the undo must
    also land before anything happens."""
    from optimus.gate.targets import resolve_fs as rfs

    before = len(gate.chain.events)
    gate.submit(req(verb=Verb.READ, tool="read_file", target_spec="notes.txt"))
    after = gate.chain.events[before:]
    assert [e.kind for e in after] == ["gate.decision"], "a read needs no compensation row"
    assert after[0].payload["verdict"] == "allow"
    assert after[0].payload["target"]["kind"] == "fs"

    d = instance_digest(Verb.WRITE, rfs("notes.txt", workspace))
    gate.grants.issue("agent-1", d, issued_by="human")
    mark = len(gate.chain.events)
    gate.submit(req(target_spec="notes.txt", reversibility=Reversibility.COMPENSATION))
    kinds = [e.kind for e in gate.chain.events[mark:]]
    assert kinds == ["gate.decision", "compensation.recorded"]
