"""The Gate: the only path from an intention to an effect."""

from .capability import (
    ArgvCapability,
    CapabilityViolation,
    FileCapability,
    capability_for,
)
from .gate import Gate, GateOutcome, Ticket
from .grants import Grant, GrantStore
from .handle import Compensation, Handle, HandleError
from .policy import Policy, PolicyError, Rule, RuleEffect, baseline_policy
from .resolvers import WorkspaceResolver
from .targets import (
    ArgvTarget,
    FsTarget,
    OpaqueTarget,
    ResolvedTarget,
    TargetRefused,
    UrlTarget,
    resolve_argv,
    resolve_fs,
    resolve_url,
)
from .types import (
    CapabilityRequest,
    Decision,
    Reversibility,
    Verb,
    Verdict,
    instance_digest,
)

__all__ = [
    "ArgvCapability",
    "ArgvTarget",
    "CapabilityRequest",
    "CapabilityViolation",
    "Compensation",
    "Decision",
    "FileCapability",
    "FsTarget",
    "Gate",
    "GateOutcome",
    "Grant",
    "GrantStore",
    "Handle",
    "HandleError",
    "OpaqueTarget",
    "Policy",
    "PolicyError",
    "ResolvedTarget",
    "Reversibility",
    "Rule",
    "RuleEffect",
    "TargetRefused",
    "Ticket",
    "UrlTarget",
    "Verb",
    "Verdict",
    "WorkspaceResolver",
    "baseline_policy",
    "capability_for",
    "instance_digest",
    "resolve_argv",
    "resolve_fs",
    "resolve_url",
]
