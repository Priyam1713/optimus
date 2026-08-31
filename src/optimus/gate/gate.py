"""The Gate: the only path from an intention to an effect.

Pipeline, in order, fail-closed at every step:

    freeze check -> resolve -> hard invariants -> grant -> policy
                 -> audit (before the act) -> issue handle -> settle

Four things it does that neither prior system did:

* **It returns a handle, not a verdict** (`handle.py`).
* **There is no auto-approver.** Bellona's `--yolo` turned every
  `RequireApproval` into an immediate self-approval, collapsing its whole Frenum
  doctrine into one flag (`audit.md` §2.6). There is deliberately no parameter
  here that does that. Assent is minted by a human surface and recorded.
* **Approval re-decides.** `approve()` re-resolves the target and re-runs policy,
  so a ticket parked under a permissive rule set does not execute under it after
  the rules tighten.
* **The freeze is reversible.** Bellona's veto had `raise()` and no `lower()`, so
  the kill switch and the (in-memory) audit trail destroyed each other.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..ledger.chain import Chain
from ..ledger.events import Event, Meter, TrustLabel
from .envelope import Envelope, EnvelopeRefused
from .grants import GrantStore
from .handle import Compensation, Handle, _issue
from .policy import Policy
from .targets import ResolvedTarget, TargetRefused
from .types import (
    RULE_FROZEN,
    RULE_GRANT,
    RULE_IRREVERSIBLE_ASSENT,
    RULE_TARGET_REFUSED,
    RULE_UNTRUSTED_MUTATION,
    CapabilityRequest,
    Decision,
    Reversibility,
    Verb,
    Verdict,
    instance_digest,
)

#: Keys every rule may reference, whatever the target kind. Seeded so that a rule
#: about `target.relpath` evaluates to false on a URL target instead of raising —
#: an evaluation error denies *everything*, which turns one narrow rule into an
#: outage.
_TARGET_DEFAULTS: dict[str, Any] = {
    "target.kind": "", "target.path": "", "target.relpath": "", "target.name": "",
    "target.suffix": "", "target.exists": False, "target.program": "", "target.argv": [],
    "target.cwd": "", "target.url": "", "target.scheme": "", "target.host": "",
    "target.port": 0, "target.ips": [], "target.namespace": "", "target.identity": "",
    # Remote-plane keys (`gate/remote.py`). Seeded here for the same reason as
    # the rest: an unknown attribute raises, and a raise denies *everything*, so
    # one rule mentioning `target.script` would otherwise take the local plane
    # down with it. `pins_identity` defaults to False because "not declared"
    # must not read as "pinned".
    "target.script": "", "target.venue": "", "target.pins_identity": False,
}

Resolver = Callable[[CapabilityRequest], ResolvedTarget]


@dataclass(slots=True)
class Ticket:
    ticket_id: str
    request: CapabilityRequest
    rule_id: str
    created_at: float


@dataclass(slots=True)
class GateOutcome:
    verdict: Verdict
    rule_id: str
    reason: str = ""
    handle: Handle | None = None
    ticket: Ticket | None = None
    ledger_seq: int = -1

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW and self.handle is not None


class Gate:
    def __init__(
        self,
        chain: Chain,
        policy: Policy,
        resolver: Resolver,
        *,
        grants: GrantStore | None = None,
        handle_ttl: float = 120.0,
        compensator: Any | None = None,
        run_id: str = "",
    ):
        self._chain = chain
        self._policy = policy
        self._resolve = resolver
        self._grants = grants or GrantStore()
        # Optional so the Gate stays usable without a state directory; when
        # present it captures the inverse *before* the act, which is the only
        # moment the prior state is still true.
        self._compensator = compensator
        self._tickets: dict[str, Ticket] = {}
        self._assents: dict[str, str] = {}
        self._frozen: str | None = None
        self._handle_ttl = handle_ttl
        # Stamped onto every row so `meter.aggregate` can attribute cost to one
        # run without a second bookkeeping structure to drift from this one.
        self.run_id = run_id
        self._envelope: Envelope | None = None
        self._envelope_used = 0

    # -- ledger ---------------------------------------------------------------

    @property
    def chain(self) -> Chain:
        return self._chain

    @property
    def grants(self) -> GrantStore:
        return self._grants

    def _record(self, kind: str, payload: dict[str, Any], trust: TrustLabel) -> Event:
        if self.run_id:
            payload = {**payload, "run_id": self.run_id}
        return self._chain.append(kind, payload, trust)

    # -- freeze ---------------------------------------------------------------

    @property
    def frozen(self) -> bool:
        return self._frozen is not None

    def freeze(self, reason: str) -> None:
        """Halt everything. Parked tickets die on the record."""
        self._frozen = reason
        self._record("gate.frozen", {"reason": reason}, TrustLabel.TRUSTED_USER)
        for tid, t in list(self._tickets.items()):
            self._record(
                "ticket.cancelled",
                {"ticket": tid, "tool": t.request.tool, "reason": "frozen"},
                TrustLabel.TRUSTED_USER,
            )
        self._tickets.clear()
        self._grants.revoke_all()
        self.close_envelope("frozen")

    def thaw(self, assent: str) -> bool:
        """Lift the freeze. Requires a human assent token, and is recorded."""
        if not self._consume_assent(assent, scope="thaw"):
            return False
        self._record("gate.thawed", {}, TrustLabel.TRUSTED_USER)
        self._frozen = None
        return True

    # -- envelope -------------------------------------------------------------

    @property
    def envelope(self) -> Envelope | None:
        return self._envelope

    @property
    def envelope_uses(self) -> int:
        return self._envelope_used

    def open_envelope(self, envelope: Envelope, *, owner_fingerprint: str) -> None:
        """Admit a signed standing authorisation for an unattended run.

        This is the one door an autonomous run comes through, and it is not a
        door the Gate can open by itself: the envelope must carry a signature
        from the owner key, and the fingerprint it is checked against has to
        arrive out of band. A refusal is recorded and raised — never softened
        into "running without one", because a run that silently lost its
        authorisation is exactly the state nobody notices.
        """
        try:
            envelope.verify(owner_fingerprint)
        except EnvelopeRefused as exc:
            self._record(
                "envelope.refused",
                {"envelope": envelope.envelope_id, "reason": str(exc)},
                TrustLabel.TRUSTED_LOCAL,
            )
            raise
        self._envelope = envelope
        self._envelope_used = 0
        self._record("envelope.opened", envelope.to_dict(), TrustLabel.TRUSTED_USER)

    def close_envelope(self, reason: str = "closed") -> None:
        if self._envelope is None:
            return
        self._record(
            "envelope.closed",
            {"envelope": self._envelope.envelope_id, "uses": self._envelope_used,
             "reason": reason},
            TrustLabel.TRUSTED_USER,
        )
        self._envelope = None

    # -- assent ---------------------------------------------------------------

    def mint_assent(self, principal: str, scope: str, shown: dict[str, Any]) -> str:
        """Issue a one-shot human assent token.

        `shown` is what the person was actually shown — the payload, the diff, the
        command. It is recorded, so "the human approved" is a checkable claim
        about a specific thing rather than a boolean in a log line, which is all
        Bellona's unauthenticated `approver: &str` ever was.
        """
        token = "ast_" + secrets.token_hex(12)
        self._assents[token] = scope
        self._record(
            "assent.minted",
            {"principal": principal, "scope": scope, "shown": shown},
            TrustLabel.TRUSTED_USER,
        )
        return token

    def _consume_assent(self, token: str | None, *, scope: str) -> bool:
        if not token:
            return False
        held = self._assents.get(token)
        if held is None or held != scope:
            return False
        del self._assents[token]
        return True

    # -- the path -------------------------------------------------------------

    def _attrs(self, req: CapabilityRequest, resolved: ResolvedTarget) -> dict[str, Any]:
        attrs: dict[str, Any] = {
            "verb": str(req.verb),
            "verb.mutates": req.verb.mutates,
            "trust": str(req.trust),
            "trust.untrusted": req.trust.is_untrusted,
            "reversibility": str(req.reversibility),
            "tool": req.tool,
            "actor": req.actor,
            "venue": req.venue,
            "intent": req.intent,
            "has_assent": bool(req.assent),
            **_TARGET_DEFAULTS,
        }
        for k, v in resolved.attrs().items():
            attrs[f"target.{k}"] = v
        return attrs

    def submit(self, req: CapabilityRequest) -> GateOutcome:
        if self._frozen is not None:
            ev = self._record(
                "gate.refused",
                {"tool": req.tool, "verb": str(req.verb), "rule": RULE_FROZEN},
                req.trust,
            )
            return GateOutcome(Verdict.DENY, RULE_FROZEN, self._frozen, ledger_seq=ev.seq)

        try:
            resolved = self._resolve(req)
        except TargetRefused as exc:
            ev = self._record(
                "gate.refused",
                {"tool": req.tool, "verb": str(req.verb), "rule": RULE_TARGET_REFUSED,
                 "reason": str(exc), "spec": repr(req.target_spec)[:400]},
                req.trust,
            )
            return GateOutcome(Verdict.DENY, RULE_TARGET_REFUSED, str(exc), ledger_seq=ev.seq)

        digest = instance_digest(req.verb, resolved)
        decision = self._decide(req, resolved, digest)

        # Audit precedes the act, always.
        ev = self._record(
            "gate.decision",
            {
                "tool": req.tool,
                "actor": req.actor,
                "verb": str(req.verb),
                "trust": str(req.trust),
                "reversibility": str(req.reversibility),
                "venue": req.venue,
                "intent": req.intent,
                "target": resolved.attrs(),
                "instance": digest,
                "verdict": str(decision.verdict),
                "rule": decision.rule_id,
                "reason": decision.reason,
            },
            req.trust,
        )

        if decision.verdict is Verdict.DENY:
            return GateOutcome(Verdict.DENY, decision.rule_id, decision.reason, ledger_seq=ev.seq)

        if decision.verdict is Verdict.NEEDS_APPROVAL:
            ticket = Ticket("tkt_" + secrets.token_hex(8), req, decision.rule_id, time.time())
            self._tickets[ticket.ticket_id] = ticket
            return GateOutcome(
                Verdict.NEEDS_APPROVAL, decision.rule_id, decision.reason,
                ticket=ticket, ledger_seq=ev.seq,
            )

        return GateOutcome(
            Verdict.ALLOW, decision.rule_id, decision.reason,
            handle=self._mint(req, resolved, ev.seq, decision.rule_id),
            ledger_seq=ev.seq,
        )

    def _envelope_covers(
        self, req: CapabilityRequest, resolved: ResolvedTarget
    ) -> tuple[bool, str]:
        """Does the envelope reach this action? Asks; does not spend.

        Checking and spending are deliberately two calls. Spending here would
        charge the ceiling for actions the policy then *denies*, which is wrong
        twice over: the field is called `max_actions` and no action occurred, and
        an agent that kept attempting refused work would burn the operator's
        whole budget without ever doing anything. `_envelope_spend` runs only
        once the verdict is ALLOW.
        """
        if self._envelope is None:
            return False, ""
        ok, why_not = self._envelope.covers(req, resolved, used=self._envelope_used)
        if not ok:
            self._record(
                "envelope.exhausted" if "ceiling" in why_not or "expired" in why_not
                else "envelope.out_of_scope",
                {"envelope": self._envelope.envelope_id, "tool": req.tool,
                 "verb": str(req.verb), "reason": why_not},
                req.trust,
            )
            return False, why_not
        return True, ""

    def _envelope_spend(
        self, req: CapabilityRequest, resolved: ResolvedTarget
    ) -> None:
        """Charge one action against the ceiling, and say so on the record."""
        if self._envelope is None:
            return
        self._envelope_used += 1
        self._record(
            "envelope.used",
            {"envelope": self._envelope.envelope_id, "tool": req.tool,
             "verb": str(req.verb), "target": resolved.attrs(),
             "used": self._envelope_used, "of": self._envelope.max_actions},
            req.trust,
        )

    def _decide(self, req: CapabilityRequest, resolved: ResolvedTarget, digest: str) -> Decision:
        untrusted_mutation = req.trust.is_untrusted and (
            req.verb.mutates or req.verb is Verb.CREDENTIAL
        )

        # A grant names this exact instance and was issued by a human. It is the
        # only thing that clears the untrusted gate without a fresh approval.
        grant = self._grants.find(req.actor, digest)
        if grant is not None:
            self._grants.consume(grant)
            return Decision(Verdict.ALLOW, RULE_GRANT, f"instance grant from {grant.issued_by}")

        # Hard invariant, checked before the rules and unreachable by them: no
        # rule can be written that lets untrusted-origin material authorise a
        # mutation. Achilles's crown jewel (`audit.md` §3.7), made unbypassable.
        #
        # An owner-signed envelope is the single exception, and it is narrow by
        # construction: it stands in for the *human*, not for the *rules*, so
        # control falls straight through to `self._policy.decide` below. A denied
        # verb stays denied and an irreversible act still needs assent.
        leaned_on_envelope = False
        if untrusted_mutation:
            covered, why_not = self._envelope_covers(req, resolved)
            if not covered:
                return Decision(
                    Verdict.NEEDS_APPROVAL,
                    RULE_UNTRUSTED_MUTATION,
                    "untrusted-origin material cannot authorise a mutation or "
                    "credential use" + (f" ({why_not})" if why_not else ""),
                )
            leaned_on_envelope = True

        decision = self._policy.decide(self._attrs(req, resolved))

        # Invariant 4: irreversible always needs a human who saw the payload,
        # even when the rules allowed it.
        if req.reversibility.needs_assent and decision.verdict is Verdict.ALLOW:
            return Decision(
                Verdict.NEEDS_APPROVAL,
                RULE_IRREVERSIBLE_ASSENT,
                "irreversible actions require assent showing the actual payload",
            )

        # Charge the envelope only for an action that is actually going to
        # happen. Every earlier return leaves the ceiling untouched.
        if leaned_on_envelope and decision.verdict is Verdict.ALLOW:
            self._envelope_spend(req, resolved)
        return decision

    def _mint(
        self, req: CapabilityRequest, resolved: ResolvedTarget, seq: int, rule_id: str
    ) -> Handle:
        compensation = None
        # Only a mutating verb needs an inverse. Recording an "undo" for a read
        # is noise in the one structure everything else projects from.
        if req.reversibility is Reversibility.COMPENSATION and req.verb.mutates:
            if self._compensator is not None:
                compensation = self._compensator.capture(req.verb, resolved)
            else:
                # Without a compensator there is no captured prior state, so the
                # row is a marker rather than an inverse. Named differently so a
                # reversal never mistakes one for the other.
                compensation = Compensation(
                    kind="undo.unavailable",
                    payload={"target": resolved.attrs(), "tool": req.tool},
                )
            # The inverse is on the record before the act, not after it.
            # `capture` returns None for targets whose plane has no inverse yet;
            # recording nothing is honest, and a reversal will not find a row it
            # cannot apply.
            if compensation is not None:
                self._record(
                    "compensation.recorded",
                    {"for_seq": seq, "kind": compensation.kind, "payload": compensation.payload},
                    req.trust,
                )
        return _issue(
            req, resolved, seq, rule_id,
            ttl_seconds=self._handle_ttl, compensation=compensation,
        )

    # -- approval -------------------------------------------------------------

    @property
    def pending(self) -> list[Ticket]:
        return list(self._tickets.values())

    def approve(self, ticket_id: str, assent: str) -> GateOutcome:
        """Approve a parked ticket. Re-resolves and re-decides.

        There is no `auto_approver` and no principal-as-string. The caller must
        present an assent token minted against this ticket by a surface that
        showed a human the payload.
        """
        if self._frozen is not None:
            return GateOutcome(Verdict.DENY, RULE_FROZEN, self._frozen)

        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return GateOutcome(Verdict.DENY, "__unknown_ticket__", f"no ticket {ticket_id}")
        if not self._consume_assent(assent, scope=f"ticket:{ticket_id}"):
            self._record(
                "approval.refused",
                {"ticket": ticket_id, "reason": "missing or mismatched assent"},
                TrustLabel.TRUSTED_USER,
            )
            return GateOutcome(Verdict.DENY, "__no_assent__", "assent token missing or not for this ticket")

        del self._tickets[ticket_id]
        req = ticket.request

        # Re-resolve: the world may have changed while the ticket was parked.
        try:
            resolved = self._resolve(req)
        except TargetRefused as exc:
            ev = self._record(
                "approval.refused",
                {"ticket": ticket_id, "reason": str(exc)}, req.trust,
            )
            return GateOutcome(Verdict.DENY, RULE_TARGET_REFUSED, str(exc), ledger_seq=ev.seq)

        # Re-decide under the *current* policy, ignoring the untrusted-mutation
        # bar: a human has now assented to this specific action.
        decision = self._policy.decide(self._attrs(req, resolved))
        if decision.verdict is Verdict.DENY:
            ev = self._record(
                "approval.refused",
                {"ticket": ticket_id, "rule": decision.rule_id, "reason": decision.reason},
                req.trust,
            )
            return GateOutcome(Verdict.DENY, decision.rule_id, decision.reason, ledger_seq=ev.seq)

        ev = self._record(
            "approval.granted",
            {"ticket": ticket_id, "tool": req.tool, "verb": str(req.verb),
             "target": resolved.attrs(), "rule": decision.rule_id},
            TrustLabel.TRUSTED_USER,
        )
        return GateOutcome(
            Verdict.ALLOW, decision.rule_id, "approved",
            handle=self._mint(req, resolved, ev.seq, decision.rule_id),
            ledger_seq=ev.seq,
        )

    def reject(self, ticket_id: str, reason: str) -> bool:
        ticket = self._tickets.pop(ticket_id, None)
        if ticket is None:
            return False
        self._record(
            "approval.rejected",
            {"ticket": ticket_id, "tool": ticket.request.tool, "reason": reason},
            TrustLabel.TRUSTED_USER,
        )
        return True

    # -- settlement -----------------------------------------------------------

    def settle(self, handle: Handle, *, ok: bool, meter: Meter | None = None,
               detail: dict[str, Any] | None = None) -> Event:
        """Record the outcome and its cost. Invariant 5: everything is metered."""
        return self._record(
            "effect.settled",
            {
                "handle": handle.handle_id,
                "for_seq": handle.ledger_seq,
                "tool": handle.request.tool,
                "verb": str(handle.request.verb),
                "ok": ok,
                "meter": (meter or Meter()).as_payload(),
                "detail": detail or {},
            },
            TrustLabel.EXECUTION_RESULT,
        )
