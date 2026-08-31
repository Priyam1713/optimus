"""Standing authorisation for an unattended run.

M3 runs the loop inside a Harbor container with no human present. That collides
head-on with the Gate's hardest invariant: `_decide` sends every mutation
motivated by untrusted material — which is every action a model chooses — to
`NEEDS_APPROVAL`, and there is deliberately no auto-approver (`audit.md` §2.6,
Bellona's `--yolo`). Without something here, a benchmark run parks on turn one
and scores zero.

The wrong fixes are the obvious ones: a `--yolo` flag, an `auto_approve`
callback, or letting the adapter mint its own assent token. All three are the
same bug — the process that wants the authorisation also grants it, so the
receipt proves nothing.

An envelope is the right shape instead:

* **It is signed by the owner key**, which by construction lives outside every
  process the Gate can reach (`ledger/keys.py`, `cli.py::attest`). The Gate holds
  a fingerprint and can only *verify*. An adapter cannot forge one, and neither
  can the loop.
* **It is narrow and declared**: one actor, an explicit verb set, named venues,
  one workspace, an action ceiling and an expiry. It is not "approve everything";
  it is "this agent may run these verbs in this disposable place until this
  time, at most this many times".
* **It clears exactly one thing** — the untrusted-mutation invariant — and then
  falls through to the ordinary policy. Deny rules still deny. Irreversible
  actions still require assent showing the payload. An envelope is a reason to
  skip the *human*, never a reason to skip the *rules*.
* **Every use is on the record**, counted against the ceiling, and names the
  envelope id. "The agent acted autonomously" becomes a checkable claim about a
  specific signed document rather than a flag someone set.

Freezing the Gate closes it, like a grant.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from ..ledger.chain import now_ms
from ..ledger.events import canonical
from ..ledger.keys import OwnerKey, fingerprint, verify_signature
from .targets import ResolvedTarget
from .types import CapabilityRequest, Verb


class EnvelopeRefused(Exception):
    """An envelope that does not verify. Never a warning, never a downgrade."""


#: Workspace value meaning "any path inside the named venues".
#:
#: Needed because a benchmark suite does not have one workspace: none of
#: Terminal-Bench 2.0's 89 tasks declares a `workdir`, so each inherits its own
#: image's `WORKDIR` and the path is not known until the container is running.
#: An envelope naming a fixed path would cover none of them.
#:
#: This is a real widening and it is written into the signed document rather than
#: inferred from a blank field, so `envelope.opened` in the ledger says plainly
#: what was authorised. What it gives up is a second, defence-in-depth check on
#: the path; what still bounds the action is the venue clause, plus
#: `RemoteResolver`'s own containment — which is the *primary* check, is
#: constructed by harness code from the container's real working directory, and
#: is untouched by this. Verbs, ceiling, expiry and actor all still apply.
ANY_WORKSPACE = "*"


@dataclass(frozen=True, slots=True)
class Envelope:
    """A signed, bounded, standing authorisation to act without a human."""

    envelope_id: str
    #: Who issued it. Recorded so the receipt names a person, not a process.
    principal: str
    #: The single agent actor it covers.
    actor: str
    #: Verbs it clears. Anything absent still parks.
    verbs: tuple[str, ...]
    #: Venue names it is valid in, by name. Set by harness code, never by the
    #: model — a target string the model wrote cannot claim to be in a container.
    venues: tuple[str, ...]
    #: Workspace root. Filesystem targets outside it are not covered even when
    #: the resolver would have allowed them.
    workspace: str
    max_actions: int
    expires_ms: int
    reason: str = ""
    #: Isolation level the harness *observed* the venue reporting when the
    #: envelope was requested. Recorded, not trusted: it is evidence in the
    #: ledger, not an input to the decision.
    observed_isolation: str = ""
    owner_pub: str = ""
    signature: str = ""

    def body(self) -> dict[str, Any]:
        """Exactly what the signature commits to."""
        return {
            "envelope_id": self.envelope_id,
            "principal": self.principal,
            "actor": self.actor,
            "verbs": list(self.verbs),
            "venues": list(self.venues),
            "workspace": self.workspace,
            "max_actions": self.max_actions,
            "expires_ms": self.expires_ms,
            "reason": self.reason,
            "observed_isolation": self.observed_isolation,
            "owner_pub": self.owner_pub,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "signature": self.signature}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Envelope:
        return Envelope(
            envelope_id=str(d["envelope_id"]),
            principal=str(d["principal"]),
            actor=str(d["actor"]),
            verbs=tuple(d.get("verbs") or ()),
            venues=tuple(d.get("venues") or ()),
            workspace=str(d.get("workspace", "")),
            max_actions=int(d["max_actions"]),
            expires_ms=int(d["expires_ms"]),
            reason=str(d.get("reason", "")),
            observed_isolation=str(d.get("observed_isolation", "")),
            owner_pub=str(d.get("owner_pub", "")),
            signature=str(d.get("signature", "")),
        )

    # -- verification ---------------------------------------------------------

    def verify(self, expected_owner_fingerprint: str) -> None:
        """Refuse unless this was signed by *the* owner, named out of band.

        The fingerprint argument is required for the same reason `chain.verify`
        requires one: checking a document against the key it carries proves only
        that whoever wrote it owned a key.
        """
        if not self.signature or not self.owner_pub:
            raise EnvelopeRefused("envelope is unsigned")
        if not expected_owner_fingerprint:
            raise EnvelopeRefused(
                "an owner fingerprint is required; verifying an envelope against "
                "the key it carries proves only that someone signed something"
            )
        if fingerprint(self.owner_pub) != expected_owner_fingerprint:
            raise EnvelopeRefused(
                f"envelope was signed by {fingerprint(self.owner_pub)}, "
                f"not by {expected_owner_fingerprint}"
            )
        if not verify_signature(self.owner_pub, canonical(self.body()), self.signature):
            raise EnvelopeRefused("envelope signature does not verify")
        # Expiry is checked here, at admission, and not only per-action in
        # `covers`. A real run opened a two-day-expired envelope, logged
        # "envelope opened", and then refused all 31 of its actions one at a
        # time over nine minutes of local inference. Everything downstream was
        # correct; what was wrong was that the door said yes.
        remaining_ms = self.expires_ms - now_ms()
        if remaining_ms <= 0:
            raise EnvelopeRefused(
                f"envelope expired {-remaining_ms // 60_000} minutes ago; "
                "issue a fresh one with `optimus envelope`"
            )

    # -- coverage -------------------------------------------------------------

    def covers(
        self,
        req: CapabilityRequest,
        resolved: ResolvedTarget,
        *,
        used: int,
        now: int | None = None,
    ) -> tuple[bool, str]:
        """Does this envelope reach this exact action? Returns (yes, why not)."""
        stamp = now_ms() if now is None else now
        if stamp >= self.expires_ms:
            return False, "envelope expired"
        if used >= self.max_actions:
            return False, f"envelope action ceiling reached ({self.max_actions})"
        if req.actor != self.actor:
            return False, f"envelope covers actor {self.actor!r}, not {req.actor!r}"
        if str(req.verb) not in self.verbs:
            return False, f"envelope does not cover verb {req.verb}"
        if req.venue not in self.venues:
            return False, f"envelope does not cover venue {req.venue!r}"
        # A target that carries a workspace must carry *this* one, unless the
        # envelope explicitly declared venue scope. Targets with no workspace of
        # their own (argv, url, opaque) are bounded by the venue clause alone,
        # which is what actually confines them.
        if self.workspace == ANY_WORKSPACE:
            return True, ""
        workspace = getattr(resolved, "workspace", None)
        # An empty workspace string lands here too, and refusing it is right: a
        # filesystem target with no workspace was not contained by anything, and
        # an envelope is not the place to start trusting it.
        if self.workspace and isinstance(workspace, str) and workspace != self.workspace:
            return False, "target is outside the envelope's workspace"
        return True, ""

    @property
    def venue_scoped(self) -> bool:
        """True when this envelope bounds by venue rather than by path."""
        return self.workspace == ANY_WORKSPACE

    def describe(self) -> str:
        """One line for a human deciding whether to hand this to a run."""
        where = (
            f"any path in {'/'.join(self.venues)}"
            if self.venue_scoped
            else f"{self.workspace} in {'/'.join(self.venues)}"
        )
        return (
            f"{self.envelope_id}: {self.actor} may {', '.join(self.verbs)} "
            f"in {where}; {self.max_actions} actions max"
        )


DEFAULT_VERBS: tuple[str, ...] = (str(Verb.READ), str(Verb.WRITE), str(Verb.EXECUTE))


def issue(
    owner: OwnerKey,
    *,
    principal: str,
    actor: str,
    workspace: str,
    venues: tuple[str, ...],
    verbs: tuple[str, ...] = DEFAULT_VERBS,
    max_actions: int = 500,
    ttl_ms: int = 6 * 60 * 60 * 1000,
    reason: str = "",
    observed_isolation: str = "",
) -> Envelope:
    """Mint one. Only reachable from a command a person runs — see `cli.py`.

    This signature takes an `OwnerKey`, which is the whole enforcement mechanism:
    nothing on the Gate's side of the process boundary can call it, because
    nothing there holds one.
    """
    draft = Envelope(
        envelope_id="env_" + secrets.token_hex(8),
        principal=principal,
        actor=actor,
        verbs=tuple(verbs),
        venues=tuple(venues),
        workspace=workspace,
        max_actions=max_actions,
        expires_ms=now_ms() + ttl_ms,
        reason=reason,
        observed_isolation=observed_isolation,
        owner_pub=owner.public_hex,
    )
    signed = {**draft.to_dict(), "signature": owner.sign(canonical(draft.body()))}
    return Envelope.from_dict(signed)
