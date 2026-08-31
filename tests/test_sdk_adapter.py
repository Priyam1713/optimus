"""The adapter tests, and the one that justifies the adapter's shape.

`test_analyzer_alone_would_not_have_stopped_it` is the important one: it shows
that the SDK's security analyzer path is advisory, so the enforcement has to live
in the executor wrapper. Without that test the design looks like a preference;
with it, it is a requirement.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("openhands.sdk", reason="SDK extra not installed")

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from openhands.sdk.security.risk import SecurityRisk  # noqa: E402

from optimus.adapters.openhands import (  # noqa: E402
    GateBridge,
    GatedExecutor,
    OptimusSecurityAnalyzer,
)
from optimus.gate import Gate, Verb, WorkspaceResolver, baseline_policy, instance_digest  # noqa: E402
from optimus.gate.targets import resolve_fs  # noqa: E402
from optimus.ledger import AgentKey, Chain, TrustLabel  # noqa: E402


class FakeAction:
    def __init__(self, path: str):
        self.path = path


class FakeActionEvent:
    def __init__(self, tool_name: str, action):
        self.tool_name = tool_name
        self.action = action


class RecordingExecutor:
    """Stands in for a real SDK executor and remembers whether it ran."""

    def __init__(self):
        self.calls: list[object] = []

    def __call__(self, action, conversation=None, resolved=None):
        self.calls.append(resolved)
        return {"ok": True, "wrote": getattr(resolved, "path", None)}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "notes.txt").write_text("hi", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("nope", encoding="utf-8")
    return ws


@pytest.fixture
def bridge(workspace: Path) -> GateBridge:
    gate = Gate(Chain(AgentKey.generate()), baseline_policy(), WorkspaceResolver(workspace))
    return GateBridge(gate, actor="agent-1")


def test_traversal_never_reaches_the_inner_executor(bridge: GateBridge):
    inner = RecordingExecutor()
    ex = GatedExecutor(inner, bridge, "write_file")
    out = ex(FakeAction("../../pwned.txt"))
    assert out["denied"] is True
    assert out["rule"] == "__target_refused__"
    assert inner.calls == [], "the executor must never see a refused action"


def test_untrusted_write_parks_instead_of_running(bridge: GateBridge):
    inner = RecordingExecutor()
    ex = GatedExecutor(inner, bridge, "write_file")
    out = ex(FakeAction("notes.txt"))
    assert out["denied"] is True
    assert out["rule"] == "__untrusted_cannot_mutate__"
    assert out["ticket"] is not None
    assert inner.calls == []


def test_granted_write_runs_and_receives_the_resolved_target(bridge: GateBridge, workspace: Path):
    digest = instance_digest(Verb.WRITE, resolve_fs("notes.txt", workspace))
    bridge.gate.grants.issue("agent-1", digest, issued_by="human")

    inner = RecordingExecutor()
    ex = GatedExecutor(inner, bridge, "write_file")
    out = ex(FakeAction("notes.txt"))

    assert out["ok"] is True
    assert len(inner.calls) == 1
    resolved = inner.calls[0]
    # The executor got a canonical path inside the workspace, not the string the
    # model wrote. This is invariant 1 holding at the only place it matters.
    assert Path(resolved.path).is_relative_to(workspace.resolve())  # type: ignore[attr-defined]


def test_read_is_metered_on_settlement(bridge: GateBridge):
    inner = RecordingExecutor()
    ex = GatedExecutor(inner, bridge, "read_file")
    ex(FakeAction("notes.txt"))
    settled = [e for e in bridge.gate.chain.events if e.kind == "effect.settled"]
    assert settled and settled[-1].payload["ok"] is True
    assert "wall_ms" in settled[-1].payload["meter"]


def test_unclassified_tool_is_refused_not_guessed(bridge: GateBridge):
    inner = RecordingExecutor()
    ex = GatedExecutor(inner, bridge, "mystery_tool")
    out = ex(FakeAction("notes.txt"))
    assert out["denied"] is True
    assert out["rule"] == "__unclassified_tool__"
    assert inner.calls == []


# ---------------------------------------------------------------------------
# the finding
# ---------------------------------------------------------------------------

def test_analyzer_reports_risk_but_cannot_refuse(bridge: GateBridge):
    """`SecurityAnalyzerBase.security_risk` returns a level, not a verdict.

    HIGH is the strongest thing it can say, and whether that stops anything is up
    to a ConfirmationPolicy the deployment chooses — `NeverConfirm` is a shipped
    option. So the analyzer cannot be the enforcement point, which is why
    `GatedExecutor` exists.
    """
    analyzer = OptimusSecurityAnalyzer(bridge)
    risk = analyzer.security_risk(FakeActionEvent("write_file", FakeAction("../../pwned.txt")))
    assert risk is SecurityRisk.HIGH

    from openhands.sdk.security.confirmation_policy import NeverConfirm

    assert NeverConfirm().should_confirm(risk) is False, (
        "a deployment can decline to confirm even a HIGH risk - proof that the "
        "analyzer is advisory and enforcement must live in the executor"
    )


def test_analyzer_agrees_with_the_gate_on_safe_reads(bridge: GateBridge):
    analyzer = OptimusSecurityAnalyzer(bridge)
    risk = analyzer.security_risk(FakeActionEvent("read_file", FakeAction("notes.txt")))
    assert risk is SecurityRisk.LOW


def test_analyzer_is_fail_closed_on_unknown_tools(bridge: GateBridge):
    analyzer = OptimusSecurityAnalyzer(bridge)
    assert analyzer.security_risk(FakeActionEvent("mystery", FakeAction("x"))) is SecurityRisk.HIGH


def test_dry_analysis_does_not_spend_a_grant(bridge: GateBridge, workspace: Path):
    """The analyzer runs on every action. If it consumed grants, a single-use
    grant would be gone before the executor ever asked for it."""
    digest = instance_digest(Verb.WRITE, resolve_fs("notes.txt", workspace))
    bridge.gate.grants.issue("agent-1", digest, issued_by="human", single_use=True)

    analyzer = OptimusSecurityAnalyzer(bridge)
    analyzer.security_risk(FakeActionEvent("write_file", FakeAction("notes.txt")))

    inner = RecordingExecutor()
    out = GatedExecutor(inner, bridge, "write_file")(FakeAction("notes.txt"))
    assert out.get("ok") is True, "the grant must survive being analysed"
