"""Pre-flight: what would this run be allowed to do?

apex §7 lists a pre-flight dry-run among the M4 surfaces. The useful question it
answers is the one an operator asks before handing an agent an envelope: *of the
things this task will plausibly try, which are allowed, which are refused, and
which stop to ask?*

## A dry run predicts. It does not promise.

This has to be said plainly, because a pre-flight that is read as a guarantee is
worse than none. The Gate deliberately re-resolves and re-decides at the moment
of action — `approve()` says so in as many words, "the world may have changed
while the ticket was parked" — and target resolution touches the filesystem and
DNS. A path that resolves inside the workspace now can be a symlink out of it a
second later; that race is exactly what `gate/capability.py` pins identity to
close. So a prediction made in advance is a prediction, and this module never
uses a stronger word (house rule 5).

## Why it runs against a shadow Gate

`Gate.submit` has effects on purpose. It writes a `gate.decision` row before the
act, it **consumes an instance grant** when one matches, it charges the autonomy
envelope's action ceiling, and it parks a ticket that a human is then expected
to answer. Running a preview through the real Gate would spend all four: the
ledger would carry decisions for actions nobody took, a single-use grant would
be gone before the action that needed it, and a 2,000-action envelope would be
drained by asking questions about it.

So the preview runs against a *shadow*: the same policy and the same resolver,
so the answers are the real answers, with a throwaway chain and a fresh grant
store, so the answers cost nothing. The two places the shadow can differ from
the real thing are stated in `Prediction.caveats` rather than hidden — an
instance grant that the real Gate would consume, and the envelope's remaining
budget.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from optimus.gate.gate import Gate
from optimus.gate.grants import GrantStore
from optimus.gate.types import CapabilityRequest, Verdict
from optimus.ledger.chain import Chain
from optimus.ledger.keys import AgentKey, fingerprint

__all__ = ["DryRun", "Plan", "Prediction"]


@dataclass(frozen=True, slots=True)
class Plan:
    """One thing the run intends to try."""

    request: CapabilityRequest
    note: str = ""


@dataclass(slots=True)
class Prediction:
    """What the Gate would say, and what could still change the answer."""

    request: CapabilityRequest
    verdict: Verdict
    rule_id: str
    reason: str
    note: str = ""
    caveats: list[str] = field(default_factory=list)

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    @property
    def asks(self) -> bool:
        return self.verdict is Verdict.NEEDS_APPROVAL

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.request.tool,
            "verb": str(self.request.verb),
            "target": str(self.request.target_spec),
            "verdict": str(self.verdict),
            "rule": self.rule_id,
            "reason": self.reason,
            "note": self.note,
            "caveats": list(self.caveats),
        }


class DryRun:
    """Ask a shadow of a Gate what it would decide."""

    def __init__(self, gate: Gate):
        self.gate = gate
        self._shadow = self._build_shadow(gate)

    @staticmethod
    def _build_shadow(gate: Gate) -> Gate:
        shadow = Gate(
            # A fresh chain, discarded with this object. Reaching into the real
            # Gate for its policy and resolver is deliberate: sharing those is
            # what makes the answers real, and *not* sharing the chain, the
            # grants or the envelope counter is what makes them free.
            Chain(AgentKey.generate()),
            gate._policy,
            gate._resolve,
            grants=GrantStore(),
            run_id=f"{gate.run_id}-dryrun",
        )
        envelope = gate.envelope
        if envelope is not None:
            # The same document. The fingerprint is derived from the envelope's
            # own key here, which would be circular if this Gate authorized
            # anything — it does not. The real Gate's insistence on a
            # fingerprint that arrives from outside the document is what makes
            # the envelope worth anything, and that path is untouched.
            with contextlib.suppress(Exception):
                shadow.open_envelope(
                    envelope, owner_fingerprint=fingerprint(envelope.owner_pub)
                )
        return shadow

    def predict(self, plan: Plan) -> Prediction:
        outcome = self._shadow.submit(plan.request)
        caveats: list[str] = []

        envelope = self.gate.envelope
        if envelope is not None and outcome.verdict is Verdict.ALLOW:
            remaining = envelope.max_actions - self.gate.envelope_uses
            if remaining <= 0:
                caveats.append(
                    "the real envelope has no actions left; this would be refused"
                )
            elif remaining < 25:
                caveats.append(f"only {remaining} envelope actions remain")
        if plan.request.verb.mutates:
            caveats.append(
                "resolution is re-run at the moment of action, so this is a "
                "prediction rather than a promise"
            )

        return Prediction(
            request=plan.request,
            verdict=outcome.verdict,
            rule_id=outcome.rule_id,
            reason=outcome.reason,
            note=plan.note,
            caveats=caveats,
        )

    def run(self, plans: Iterable[Plan]) -> list[Prediction]:
        return [self.predict(p) for p in plans]

    # -- rendering ------------------------------------------------------------

    @staticmethod
    def render(predictions: Sequence[Prediction]) -> str:
        if not predictions:
            return "nothing to check."
        allowed = sum(1 for p in predictions if p.allowed)
        asks = sum(1 for p in predictions if p.asks)
        denied = len(predictions) - allowed - asks

        mark = {Verdict.ALLOW: "ok  ", Verdict.NEEDS_APPROVAL: "ask ",
                Verdict.DENY: "deny"}
        lines = [
            f"pre-flight: {len(predictions)} action(s) — "
            f"{allowed} allowed, {asks} would ask, {denied} refused",
            "",
        ]
        width = max(len(p.request.tool) for p in predictions)
        for p in predictions:
            lines.append(
                f"  {mark.get(p.verdict, '?')}  {p.request.tool:<{width}}  "
                f"{str(p.request.target_spec)[:48]:<48}  {p.rule_id}"
            )
            if p.reason:
                lines.append(f"        {p.reason[:100]}")
        seen: set[str] = set()
        notes = [c for p in predictions for c in p.caveats
                 if not (c in seen or seen.add(c))]
        if notes:
            lines.append("")
            lines.append("  this is a prediction, not a promise:")
            lines.extend(f"    - {n}" for n in notes)
        return "\n".join(lines)
