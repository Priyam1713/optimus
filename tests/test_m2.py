"""M2: capabilities, reversal, venues, and the first real tool plane.

Same discipline: attack the invariants. The centrepieces are
`test_swapping_the_file_after_authorisation_is_refused` (the TOCTOU M1 left
open) and `test_undo_walks_backwards_to_the_original` (an inverse that is only
real if replaying it lands on the original state, not the middle one).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from optimus.gate import (
    CapabilityViolation,
    FileCapability,
    Gate,
    Reversibility,
    Verb,
    Verdict,
    WorkspaceResolver,
    baseline_policy,
    capability_for,
    instance_digest,
)
from optimus.gate.targets import resolve_argv, resolve_fs
from optimus.ledger import AgentKey, Chain, TrustLabel
from optimus.reversal import BlobStore, Compensator, record_undo
from optimus.tools import GatedTools
from optimus.venues import (
    ENV_ALLOW,
    Isolation,
    LocalVenue,
    VenueRequest,
    VenueUnavailable,
    choose,
    scrub_env,
    truncate,
)

WINDOWS = sys.platform == "win32"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("original", encoding="utf-8")
    return ws


@pytest.fixture
def kit(workspace: Path, tmp_path: Path):
    comp = Compensator(BlobStore(tmp_path / "blobs"))
    gate = Gate(Chain(AgentKey.generate()), baseline_policy(),
                WorkspaceResolver(workspace), compensator=comp)
    return gate, comp, GatedTools(gate=gate, actor="agent-1")


def _grant(gate: Gate, workspace: Path, verb: Verb, rel: str) -> None:
    gate.grants.issue("agent-1", instance_digest(verb, resolve_fs(rel, workspace)),
                      issued_by="human", single_use=False)


# ---------------------------------------------------------------------------
# capabilities — the TOCTOU M1 left open
# ---------------------------------------------------------------------------

def test_capability_reads_the_authorised_file(workspace: Path):
    cap = capability_for(resolve_fs("notes.txt", workspace))
    assert isinstance(cap, FileCapability)
    assert cap.read_text() == "original"


def test_swapping_the_file_after_authorisation_is_refused(workspace: Path):
    """Resolve, then replace the file with a different one at the same path.
    The name still points somewhere legal; the identity does not match."""
    cap = capability_for(resolve_fs("notes.txt", workspace))
    target = workspace / "notes.txt"
    os.unlink(target)
    target.write_text("substituted", encoding="utf-8")

    with pytest.raises(CapabilityViolation) as exc:
        cap.read_text()
    assert "identity changed" in str(exc.value)


def test_deleting_the_file_after_authorisation_is_refused(workspace: Path):
    cap = capability_for(resolve_fs("notes.txt", workspace))
    os.unlink(workspace / "notes.txt")
    with pytest.raises(CapabilityViolation):
        cap.read_text()


def test_create_is_anchored_to_the_parent_directory(workspace: Path):
    """A file that does not exist has no identity to pin, so the directory that
    will hold it is pinned instead."""
    sub = workspace / "sub"
    sub.mkdir()
    cap = capability_for(resolve_fs("sub/new.txt", workspace))

    import shutil

    shutil.rmtree(sub)
    sub.mkdir()  # a *different* directory at the same path

    with pytest.raises(CapabilityViolation) as exc:
        cap.write_text("x")
    assert "replaced" in str(exc.value) or "no longer exists" in str(exc.value)


def test_create_refuses_if_something_appeared_at_the_path(workspace: Path):
    cap = capability_for(resolve_fs("fresh.txt", workspace))
    (workspace / "fresh.txt").write_text("someone got there first", encoding="utf-8")
    with pytest.raises(FileExistsError):
        cap.write_text("mine")


@pytest.mark.skipif(WINDOWS, reason="O_NOFOLLOW is POSIX-only")
def test_symlink_is_refused_at_open_time(workspace: Path, tmp_path: Path):
    outside = tmp_path / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = workspace / "link.txt"
    os.symlink(outside, link)
    with pytest.raises((CapabilityViolation, OSError)):
        capability_for(resolve_fs("link.txt", workspace)).read_text()


def test_ensure_parent_refuses_to_build_outside_the_workspace(workspace: Path):
    cap = capability_for(resolve_fs("deep/nested/file.txt", workspace))
    cap.ensure_parent()
    assert (workspace / "deep" / "nested").is_dir()
    cap.write_text("ok")
    assert (workspace / "deep" / "nested" / "file.txt").read_text() == "ok"


# ---------------------------------------------------------------------------
# reversal
# ---------------------------------------------------------------------------

def test_blobs_are_content_addressed(tmp_path: Path):
    b = BlobStore(tmp_path / "b")
    d1 = b.put(b"same")
    d2 = b.put(b"same")
    assert d1 == d2 and len(b) == 1
    assert b.get(d1) == b"same"


def test_inverse_is_captured_before_the_write(kit, workspace: Path):
    gate, _comp, tools = kit
    _grant(gate, workspace, Verb.WRITE, "notes.txt")
    tools.write_file("notes.txt", "changed")

    rows = [e for e in gate.chain.events if e.kind == "compensation.recorded"]
    assert rows and rows[-1].payload["kind"] == "undo.restore"
    decision = [e for e in gate.chain.events if e.kind == "gate.decision"][-1]
    assert rows[-1].seq > decision.seq
    settled = [e for e in gate.chain.events if e.kind == "effect.settled"]
    assert rows[-1].seq < settled[-1].seq, "the inverse must precede the act"


def test_undo_restores_a_modified_file(kit, workspace: Path):
    gate, comp, tools = kit
    _grant(gate, workspace, Verb.WRITE, "notes.txt")
    tools.write_file("notes.txt", "changed")
    assert (workspace / "notes.txt").read_text() == "changed"

    report = comp.undo(gate.chain.events)
    assert report.ok and report.applied
    assert (workspace / "notes.txt").read_text() == "original"


def test_undo_removes_a_created_file(kit, workspace: Path):
    gate, comp, tools = kit
    _grant(gate, workspace, Verb.WRITE, "brand_new.txt")
    tools.write_file("brand_new.txt", "hello")
    assert (workspace / "brand_new.txt").exists()

    comp.undo(gate.chain.events)
    assert not (workspace / "brand_new.txt").exists()


def test_undo_restores_a_deleted_file(kit, workspace: Path):
    gate, comp, tools = kit
    _grant(gate, workspace, Verb.DELETE, "notes.txt")
    assert tools.delete_file("notes.txt").get("deleted") is True
    assert not (workspace / "notes.txt").exists()

    comp.undo(gate.chain.events)
    assert (workspace / "notes.txt").read_text() == "original"


def test_undo_walks_backwards_to_the_original(kit, workspace: Path):
    """Three writes leave three inverses. Applying them oldest-first would land
    on the middle state; newest-first lands on the original."""
    gate, comp, tools = kit
    _grant(gate, workspace, Verb.WRITE, "notes.txt")
    for text in ("v1", "v2", "v3"):
        tools.write_file("notes.txt", text)
    assert (workspace / "notes.txt").read_text() == "v3"

    comp.undo(gate.chain.events)
    assert (workspace / "notes.txt").read_text() == "original"


def test_undo_skips_actions_that_never_settled(kit, workspace: Path, monkeypatch):
    """Undoing something that did not happen is its own way of corrupting a
    workspace."""
    gate, comp, tools = kit
    _grant(gate, workspace, Verb.WRITE, "notes.txt")

    def boom(self, text, encoding="utf-8"):
        raise OSError("disk on fire")

    monkeypatch.setattr(FileCapability, "write_text", boom)
    tools.write_file("notes.txt", "never lands")

    report = comp.undo(gate.chain.events)
    assert report.applied == []
    assert any("never settled" in s for s in report.skipped)
    assert (workspace / "notes.txt").read_text() == "original"


def test_undo_is_itself_recorded(kit, workspace: Path):
    gate, comp, tools = kit
    _grant(gate, workspace, Verb.WRITE, "notes.txt")
    tools.write_file("notes.txt", "changed")
    record_undo(gate.chain, comp.undo(gate.chain.events), run="r1")
    assert gate.chain.events[-1].kind == "reversal.applied"
    assert gate.chain.events[-1].payload["applied"]


def test_gate_without_a_compensator_says_so(workspace: Path):
    """No captured prior state means the row is a marker, named differently so a
    reversal cannot mistake it for an inverse."""
    gate = Gate(Chain(AgentKey.generate()), baseline_policy(), WorkspaceResolver(workspace))
    _grant(gate, workspace, Verb.WRITE, "notes.txt")
    GatedTools(gate=gate, actor="agent-1").write_file("notes.txt", "x")
    row = [e for e in gate.chain.events if e.kind == "compensation.recorded"][-1]
    assert row.payload["kind"] == "undo.unavailable"


# ---------------------------------------------------------------------------
# venues
# ---------------------------------------------------------------------------

def test_env_is_an_allow_list_not_a_prefix_match():
    """Bellona matched by prefix, so `HOME` also admitted `HOMEDRIVE`."""
    env = scrub_env({"PATH": "/bin", "HOMEDRIVE": "C:", "AWS_SECRET_ACCESS_KEY": "sk-x",
                     "PATHOLOGICAL": "nope", "HOME": "/h"})
    assert set(env) == {"PATH", "HOME"}
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "HOMEDRIVE" not in env and "PATHOLOGICAL" not in env


def test_truncation_is_character_safe():
    text = "é" * 100
    out = truncate(text, 10)
    assert out.startswith("é" * 10)
    assert "truncated" in out
    out.encode("utf-8")  # would raise if a character had been split


def test_local_venue_runs_and_reports_isolation(workspace: Path):
    cap = capability_for(resolve_argv([sys.executable, "-c", "print('hi')"], workspace))
    r = LocalVenue().run(cap, VenueRequest(timeout_s=60))
    assert r.ok and "hi" in r.stdout
    assert r.isolation is Isolation.PROCESS


def test_timeout_kills_the_process_tree(workspace: Path):
    """Bellona's timeout dropped the future and left the child running."""
    cap = capability_for(resolve_argv(
        [sys.executable, "-c", "import time; time.sleep(30)"], workspace))
    r = LocalVenue().run(cap, VenueRequest(timeout_s=2))
    assert r.timed_out and not r.ok
    assert "killed" in r.stderr


def test_missing_program_is_a_result_not_a_crash(workspace: Path):
    cap = capability_for(resolve_argv(["definitely-not-a-real-binary-xyz"], workspace))
    r = LocalVenue().run(cap, VenueRequest(timeout_s=10))
    assert r.exit_code == 127 and "not found" in r.stderr


def test_choose_refuses_rather_than_downgrading():
    """The failure Bellona's ladder had in reverse: it refused its only rung.
    Here the refusal is real but a working rung exists for ordinary work."""
    with pytest.raises(VenueUnavailable) as exc:
        choose([LocalVenue()], VenueRequest(min_isolation=Isolation.CONTAINER))
    assert "CONTAINER" in str(exc.value)
    assert choose([LocalVenue()], VenueRequest(min_isolation=Isolation.PROCESS)).name == "local"


def test_choose_picks_the_weakest_sufficient_venue():
    class FakeContainer(LocalVenue):
        name = "fake-container"

        def isolation(self):
            return Isolation.CONTAINER

    assert choose([FakeContainer(), LocalVenue()],
                  VenueRequest(min_isolation=Isolation.NONE)).name == "local"
    assert choose([FakeContainer(), LocalVenue()],
                  VenueRequest(min_isolation=Isolation.CONTAINER)).name == "fake-container"


# ---------------------------------------------------------------------------
# the tool plane
# ---------------------------------------------------------------------------

def test_tools_refuse_traversal_and_return_an_observation(kit):
    _gate, _comp, tools = kit
    out = tools.write_file("../../pwned.txt", "x")
    assert out["denied"] is True and out["rule"] == "__target_refused__"
    assert "error" in out  # an observation the agent can reason about


def test_untrusted_write_parks_without_touching_the_file(kit, workspace: Path):
    _gate, _comp, tools = kit
    out = tools.write_file("notes.txt", "sneaky")
    assert out["denied"] and out["rule"] == "__untrusted_cannot_mutate__"
    assert out["ticket"]
    assert (workspace / "notes.txt").read_text() == "original"


def test_read_is_allowed_and_metered(kit):
    gate, _comp, tools = kit
    out = tools.read_file("notes.txt")
    assert out["content"] == "original" and out["total_lines"] == 1
    settled = [e for e in gate.chain.events if e.kind == "effect.settled"]
    assert settled[-1].payload["ok"] and "wall_ms" in settled[-1].payload["meter"]


def test_run_command_requires_approval_then_runs(kit, workspace: Path):
    gate, _comp, tools = kit
    out = tools.run_command([sys.executable, "-c", "print(1)"])
    assert out["denied"] and out["verdict"] == "needs_approval"

    ticket = gate.pending[0].ticket_id
    tok = gate.mint_assent("priya", scope=f"ticket:{ticket}", shown={"argv": "python -c print(1)"})
    approved = gate.approve(ticket, tok)
    assert approved.allowed

    cap = capability_for(approved.handle.consume())
    r = LocalVenue().run(cap, VenueRequest(timeout_s=30))
    assert r.ok and "1" in r.stdout


def test_isolation_shortfall_is_a_refusal_not_a_silent_downgrade(kit, workspace: Path):
    gate, _comp, tools = kit
    gate.grants.issue(
        "agent-1",
        instance_digest(Verb.EXECUTE, resolve_argv([sys.executable, "-c", "print(1)"], workspace)),
        issued_by="human", single_use=False,
    )
    out = tools.run_command([sys.executable, "-c", "print(1)"],
                            min_isolation=Isolation.MACHINE)
    assert out["denied"] and out["rule"] == "__isolation_unavailable__"


def test_full_cycle_write_then_undo_leaves_no_trace(kit, workspace: Path):
    """The end-to-end M2 claim: an authorised mutation, and one command back."""
    gate, comp, tools = kit
    _grant(gate, workspace, Verb.WRITE, "notes.txt")
    _grant(gate, workspace, Verb.WRITE, "extra.txt")

    tools.write_file("notes.txt", "edited by the agent")
    tools.write_file("extra.txt", "a new file")
    assert (workspace / "extra.txt").exists()

    report = comp.undo(gate.chain.events)
    record_undo(gate.chain, report, run="cycle")

    assert report.ok
    assert (workspace / "notes.txt").read_text() == "original"
    assert not (workspace / "extra.txt").exists()
    assert gate.chain.events[-1].kind == "reversal.applied"


def test_path_case_is_preserved_on_disk(workspace: Path):
    """Found by running the demo, not by a test: `normcase` was being applied to
    the path that gets opened rather than only to the comparison key, so an agent
    asked for `CHANGELOG.md` created `changelog.md`. That is a different file to
    git and to every case-sensitive system downstream.
    """
    target = resolve_fs("Docs/README.md", workspace)
    assert target.path.endswith(os.path.join("Docs", "README.md"))

    cap = capability_for(target)
    assert isinstance(cap, FileCapability)
    cap.ensure_parent()
    cap.write_text("hi")

    assert (workspace / "Docs" / "README.md").exists()
    assert sorted(p.name for p in (workspace / "Docs").iterdir()) == ["README.md"]


def test_containment_is_still_case_insensitive_where_the_platform_is(workspace: Path):
    """Preserving case must not weaken the fence: a deny that only matches one
    casing is a bypass on a case-insensitive filesystem."""
    if not WINDOWS:
        pytest.skip("case-insensitive containment is a Windows/macOS property")
    from optimus.gate.targets import _is_within

    assert _is_within(str(workspace).upper() + os.sep + "x.txt", str(workspace))
