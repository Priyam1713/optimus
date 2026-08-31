"""The vocabulary the Gate reasons in."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..ledger.events import TrustLabel, canonical
from .targets import ResolvedTarget


class Verb(StrEnum):
    """What an action does, independent of which tool does it.

    Classification comes from the *tool's declared spec*, never from the model's
    claim about itself — Bellona got this right and it is worth keeping.
    """

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"
    INVOKE = "invoke"          # MCP / plugin call
    INPUT = "input"            # keyboard, mouse, UIA invoke
    NAVIGATE = "navigate"      # fetch / browse, read-only
    NETWORK_SEND = "network_send"
    CREDENTIAL = "credential"
    PUBLISH = "publish"

    @property
    def mutates(self) -> bool:
        """Conservative: anything not positively classified as a read mutates."""
        return self not in {Verb.READ, Verb.NAVIGATE}


class Reversibility(StrEnum):
    """Invariant 4 (`apex.md` §3): reversibility is a declared type and policy
    keys on it. Declaring wrongly is a bug the verifier can catch, which is why
    it is a field and not a guess."""

    OVERLAY = "overlay"              # staged in a diff sandbox; nothing real changed
    COMPENSATION = "compensation"    # an inverse exists and is recorded before the act
    SNAPSHOT = "snapshot"            # only undoable by restoring a whole venue
    IRREVERSIBLE = "irreversible"    # sent mail, payment, external delete

    @property
    def needs_assent(self) -> bool:
        return self is Reversibility.IRREVERSIBLE


class Verdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"


# Sentinel rule ids for structural outcomes. Every refusal names one.
RULE_DEFAULT_DENY = "__default_deny__"
RULE_POLICY_ERROR = "__policy_error__"
RULE_UNTRUSTED_MUTATION = "__untrusted_cannot_mutate__"
RULE_IRREVERSIBLE_ASSENT = "__irreversible_needs_assent__"
RULE_TARGET_REFUSED = "__target_refused__"
RULE_GRANT = "__instance_grant__"
RULE_FROZEN = "__frozen__"


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    """A proposed effect, before resolution.

    `target_spec` is whatever the caller supplied — typically model output, and
    therefore untrusted. It is never handed onward; the Gate replaces it with a
    `ResolvedTarget`.
    """

    actor: str
    verb: Verb
    trust: TrustLabel
    reversibility: Reversibility
    tool: str
    target_spec: Any
    intent: str = ""
    venue: str = "local"
    #: Set only by a human-facing surface that actually showed a person the
    #: payload. Nothing derived from model output may set this.
    assent: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Decision:
    verdict: Verdict
    rule_id: str
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW


def instance_digest(verb: Verb, target: ResolvedTarget) -> str:
    """Identity of *this exact action*, not its category.

    `audit.md` §3.2: Achilles bound grants to `(action, scope)`, so one approved
    `execute:workspace` switched the untrusted-content gate off for everything in
    that scope until the TTL expired. A grant here names one resolved target.
    """
    return hashlib.sha256(canonical({"verb": str(verb), "target": target.attrs()})).hexdigest()
