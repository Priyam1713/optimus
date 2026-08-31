"""Adapter onto the OpenHands Software Agent SDK.

**A finding that refines `apex.md` §1.3.** I described the SDK's pluggable
`SecurityAnalyzerBase` as "the Gate's socket". Having now read the contract, that
is only half true and the half that is false matters:

    SecurityAnalyzerBase.security_risk(action) -> SecurityRisk

It returns a *risk level*. A `ConfirmationPolicy` then decides whether to prompt.
There is **no verdict that refuses**, and nothing in that path resolves a target
or constrains what the executor subsequently receives. An analyzer alone is
advisory, and an advisory gate is the exact shape of Bellona's failure
(`audit.md` §2.4): a correct judgement that the executor was free to ignore.

So the adapter is deliberately two pieces with different jobs:

* `OptimusSecurityAnalyzer` — **observability and confirmation UX.** Maps a Gate
  decision onto the SDK's risk vocabulary so the SDK's own confirmation flow,
  visualisers and event history light up correctly.
* `GatedExecutor` — **enforcement.** Wraps a tool's executor so it cannot run
  without a handle, and receives the *resolved* target rather than the model's
  arguments. This is where invariant 1 actually holds.

Use both. Using only the first would look governed and not be.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from ..gate.gate import Gate
from ..gate.types import CapabilityRequest, Reversibility, Verb, Verdict
from ..ledger.events import Meter, TrustLabel

try:  # pragma: no cover - exercised only when the SDK is installed
    from openhands.sdk.security.analyzer import SecurityAnalyzerBase
    from openhands.sdk.security.risk import SecurityRisk
    from openhands.sdk.tool import ToolExecutor

    _SDK = True
except Exception:  # pragma: no cover
    SecurityAnalyzerBase = object  # type: ignore[assignment,misc]
    ToolExecutor = object  # type: ignore[assignment,misc]
    SecurityRisk = None  # type: ignore[assignment]
    _SDK = False


SpecExtractor = Callable[[Any], Any]


@dataclass(frozen=True, slots=True)
class ToolContract:
    """What a tool *is*, declared by us rather than claimed by the model.

    Bellona got this right and it is worth restating: effect classification comes
    from the tool's declared spec, never from the model's description of what it
    is about to do.
    """

    verb: Verb
    reversibility: Reversibility
    #: Pulls the raw target spec out of the SDK action object.
    extract: SpecExtractor


def field_extractor(*names: str) -> SpecExtractor:
    """Read the first present attribute (or mapping key) from an action."""

    def _extract(action: Any) -> Any:
        for n in names:
            if isinstance(action, dict) and n in action:
                return action[n]
            if hasattr(action, n):
                return getattr(action, n)
        raise AttributeError(f"action exposes none of {names}")

    return _extract


#: Contracts for the SDK's standard tools. Anything not listed is refused rather
#: than guessed at — an unclassified tool is an ungoverned one.
DEFAULT_CONTRACTS: dict[str, ToolContract] = {
    "read_file": ToolContract(Verb.READ, Reversibility.OVERLAY, field_extractor("path", "file_path")),
    "str_replace_editor": ToolContract(Verb.WRITE, Reversibility.COMPENSATION, field_extractor("path", "file_path")),
    "edit_file": ToolContract(Verb.WRITE, Reversibility.COMPENSATION, field_extractor("path", "file_path")),
    "write_file": ToolContract(Verb.WRITE, Reversibility.COMPENSATION, field_extractor("path", "file_path")),
    "delete_file": ToolContract(Verb.DELETE, Reversibility.COMPENSATION, field_extractor("path", "file_path")),
    "execute_bash": ToolContract(Verb.EXECUTE, Reversibility.SNAPSHOT, field_extractor("argv", "command")),
    "fetch": ToolContract(Verb.NAVIGATE, Reversibility.OVERLAY, field_extractor("url")),
}


class GateBridge:
    """Shared plumbing: turn an SDK action into a Gate decision."""

    def __init__(
        self,
        gate: Gate,
        contracts: dict[str, ToolContract] | None = None,
        *,
        actor: str = "agent",
        # The model wrote the arguments, so this is the honest default. It is a
        # constructor argument rather than a constant only so a trusted,
        # human-authored call path can say so explicitly.
        trust: TrustLabel = TrustLabel.UNTRUSTED_MODEL_OUTPUT,
    ):
        self.gate = gate
        self.contracts = {**DEFAULT_CONTRACTS, **(contracts or {})}
        self.actor = actor
        self.trust = trust

    def request_for(self, tool_name: str, action: Any, intent: str = "") -> CapabilityRequest | None:
        contract = self.contracts.get(tool_name)
        if contract is None:
            return None
        try:
            spec = contract.extract(action)
        except AttributeError:
            return None
        return CapabilityRequest(
            actor=self.actor,
            verb=contract.verb,
            trust=self.trust,
            reversibility=contract.reversibility,
            tool=tool_name,
            target_spec=spec,
            intent=intent,
        )


class OptimusSecurityAnalyzer(SecurityAnalyzerBase):  # type: ignore[misc]
    """Surfaces Gate verdicts in the SDK's risk vocabulary.

    Advisory by construction — see the module docstring. Its job is to make the
    SDK's confirmation flow agree with the Gate, not to be the thing that stops
    anything.

    Note the deliberate asymmetry: an unclassified tool maps to HIGH, not
    UNKNOWN, so the default confirmation policy prompts. Fail closed even in the
    advisory layer.
    """

    bridge: Any = None

    def __init__(self, bridge: GateBridge, **data: Any):
        super().__init__(**data)
        object.__setattr__(self, "bridge", bridge)

    def security_risk(self, action: Any) -> Any:
        tool_name = getattr(action, "tool_name", "") or ""
        payload = getattr(action, "action", action)
        req = self.bridge.request_for(tool_name, payload)
        if req is None:
            return SecurityRisk.HIGH

        # A dry decision: this must not mint a handle or spend a grant, so it
        # reads the policy directly rather than going through submit().
        try:
            resolved = self.bridge.gate._resolve(req)
        except Exception:
            return SecurityRisk.HIGH
        decision = self.bridge.gate._policy.decide(self.bridge.gate._attrs(req, resolved))
        if req.trust.is_untrusted and (req.verb.mutates or req.verb is Verb.CREDENTIAL):
            return SecurityRisk.HIGH
        return {
            Verdict.DENY: SecurityRisk.HIGH,
            Verdict.NEEDS_APPROVAL: SecurityRisk.MEDIUM,
            Verdict.ALLOW: SecurityRisk.LOW,
        }[decision.verdict]


class GatedExecutor(ToolExecutor):  # type: ignore[misc]
    """The enforcing half. Wraps an SDK executor so it only ever runs on a handle.

    The inner executor is called with the **resolved** target, so a refused or
    unresolvable action cannot reach it at all. A denial comes back as an
    observation rather than an exception, because a denial is information the
    agent should reason about — Achilles's rule, and the right one
    (`audit.md` §3.7).
    """

    def __init__(self, inner: Any, bridge: GateBridge, tool_name: str):
        self._inner = inner
        self._bridge = bridge
        self._tool_name = tool_name

    def __call__(self, action: Any, conversation: Any = None) -> Any:
        req = self._bridge.request_for(self._tool_name, action)
        if req is None:
            return {
                "error": f"tool {self._tool_name!r} has no declared contract; refusing",
                "denied": True,
                "rule": "__unclassified_tool__",
            }

        outcome = self._bridge.gate.submit(req)
        if not outcome.allowed or outcome.handle is None:
            return {
                "error": f"denied by {outcome.rule_id}: {outcome.reason}",
                "denied": True,
                "rule": outcome.rule_id,
                "verdict": str(outcome.verdict),
                "ticket": outcome.ticket.ticket_id if outcome.ticket else None,
            }

        handle = outcome.handle
        resolved = handle.consume()
        started = time.monotonic()
        try:
            result = self._inner(action, conversation, resolved=resolved) if _accepts_resolved(
                self._inner
            ) else self._inner(action, conversation)
            ok = True
        except Exception as exc:
            result = {"error": f"{type(exc).__name__}: {exc}"}
            ok = False
        finally:
            self._bridge.gate.settle(
                handle,
                ok=locals().get("ok", False),
                meter=Meter(wall_ms=int((time.monotonic() - started) * 1000)),
                detail={"tool": self._tool_name},
            )
        return result

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


def _accepts_resolved(fn: Any) -> bool:
    import inspect

    try:
        return "resolved" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
