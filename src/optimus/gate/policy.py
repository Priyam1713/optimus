"""The rule engine.

Two things are borrowed wholesale from Bellona, which got this row right
(`audit.md` §2.14):

* **Deny-before-allow is structural.** Rules live in three separate lists and
  are evaluated deny -> approval -> allow. Ordering is a property of the data
  structure, not of the order someone wrote the config file in. This is also why
  no longest-prefix logic is needed: a `deny **/.env` beats a broad allow no
  matter where it sits.
* **A broken rule denies.** An evaluation error yields `__policy_error__`, never
  a skip and never an open.

What is *not* borrowed is CEL-as-a-string. Bellona interpolated Rust booleans
into CEL source (`"... && !{}"`), which is how a config value becomes executable
text. Predicates here are a small structured AST: JSON-serialisable, statically
checkable, and impossible to inject into. A CEL backend can be added behind
`Predicate.evaluate` if a deployment wants the expressiveness.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .types import RULE_DEFAULT_DENY, RULE_POLICY_ERROR, Decision, Verdict


class PolicyError(Exception):
    """Raised by predicate evaluation. Always becomes a denial."""


class RuleEffect(StrEnum):
    DENY = "deny"
    APPROVAL = "approval"
    ALLOW = "allow"


def _glob(value: str, pattern: str) -> bool:
    """Path-aware glob.

    Two deliberate departures from bare `fnmatch`:

    * **Case-insensitive.** These run against Windows paths, and a case-sensitive
      deny rule on a case-insensitive filesystem is a bypass waiting to happen.
    * **A leading `**/` also matches at depth zero.** Bare `fnmatch` says
      `**/.env` does *not* match `.env`, so a deny rule written the obvious way
      silently fails to protect a file in the workspace root. The adversarial
      suite caught exactly that, and a deny rule that quietly does not match is
      worse than no rule at all.
    """
    v = value.lower().replace("\\", "/")
    p = pattern.lower().replace("\\", "/")
    if fnmatch.fnmatch(v, p):
        return True
    # The second case above: a leading `**/` also matches at depth zero.
    return p.startswith("**/") and fnmatch.fnmatch(v, p[3:])


def _lookup(attrs: Mapping[str, Any], key: str) -> Any:
    if key not in attrs:
        raise PolicyError(f"unknown attribute {key!r}")
    return attrs[key]


def _as_str(v: Any) -> str:
    return v if isinstance(v, str) else str(v)


def evaluate(node: Mapping[str, Any], attrs: Mapping[str, Any]) -> bool:
    """Evaluate one predicate node against the flattened attribute map."""
    if not isinstance(node, Mapping) or len(node) != 1:
        raise PolicyError(f"malformed predicate: {node!r}")
    (op, arg), = node.items()

    if op == "all":
        return all(evaluate(n, attrs) for n in arg)
    if op == "any":
        return any(evaluate(n, attrs) for n in arg)
    if op == "not":
        return not evaluate(arg, attrs)
    if op == "always":
        return bool(arg)

    if not isinstance(arg, Mapping) or "attr" not in arg:
        raise PolicyError(f"{op} requires an 'attr'")
    key = arg["attr"]
    value = _lookup(attrs, key)

    match op:
        case "eq":
            return value == arg["value"]
        case "ne":
            return value != arg["value"]
        case "truthy":
            return bool(value)
        case "in":
            options = arg["value"]
            if not isinstance(options, (list, tuple, set)):
                raise PolicyError("'in' requires a list value")
            return value in options
        case "glob":
            return _glob(_as_str(value), _as_str(arg["value"]))
        case "glob_any":
            s = _as_str(value)
            return any(_glob(s, _as_str(p)) for p in arg["value"])
        case "startswith":
            return _as_str(value).lower().startswith(_as_str(arg["value"]).lower())
        case "contains":
            return _as_str(arg["value"]).lower() in _as_str(value).lower()
        case _:
            raise PolicyError(f"unknown operator {op!r}")


@dataclass(frozen=True, slots=True)
class Rule:
    id: str
    effect: RuleEffect
    predicate: Mapping[str, Any]
    reason: str = ""


class Policy:
    """A compiled rule set. An empty policy permits nothing."""

    def __init__(self, rules: Iterable[Rule] = ()):
        self._deny: list[Rule] = []
        self._approval: list[Rule] = []
        self._allow: list[Rule] = []
        for r in rules:
            self.add(r)

    def add(self, rule: Rule) -> None:
        # Fail at load time, not at decision time: a rule set that cannot be
        # evaluated must stop deployment rather than silently refuse everything
        # later, which looks identical to "the agent is broken".
        _typecheck(rule.predicate)
        {
            RuleEffect.DENY: self._deny,
            RuleEffect.APPROVAL: self._approval,
            RuleEffect.ALLOW: self._allow,
        }[rule.effect].append(rule)

    @property
    def rules(self) -> list[Rule]:
        return [*self._deny, *self._approval, *self._allow]

    def __len__(self) -> int:
        return len(self.rules)

    def decide(self, attrs: Mapping[str, Any]) -> Decision:
        for bucket, verdict in (
            (self._deny, Verdict.DENY),
            (self._approval, Verdict.NEEDS_APPROVAL),
            (self._allow, Verdict.ALLOW),
        ):
            for rule in bucket:
                try:
                    hit = evaluate(rule.predicate, attrs)
                except PolicyError as exc:
                    return Decision(
                        Verdict.DENY,
                        RULE_POLICY_ERROR,
                        f"rule {rule.id!r} could not be evaluated: {exc}",
                    )
                if hit:
                    return Decision(
                        verdict,
                        rule.id,
                        rule.reason or f"matched {rule.effect} rule {rule.id!r}",
                    )
        return Decision(Verdict.DENY, RULE_DEFAULT_DENY, "no rule matched")


def _typecheck(node: Any) -> None:
    """Structural validation with no attribute lookup — catches malformed rules
    at load time."""
    if not isinstance(node, Mapping) or len(node) != 1:
        raise PolicyError(f"malformed predicate: {node!r}")
    (op, arg), = node.items()
    if op in {"all", "any"}:
        if not isinstance(arg, Sequence) or isinstance(arg, (str, bytes)):
            raise PolicyError(f"{op} requires a list")
        for n in arg:
            _typecheck(n)
        return
    if op == "not":
        _typecheck(arg)
        return
    if op == "always":
        if not isinstance(arg, bool):
            raise PolicyError("always requires a bool")
        return
    known = {"eq", "ne", "in", "glob", "glob_any", "startswith", "contains", "truthy"}
    if op not in known:
        raise PolicyError(f"unknown operator {op!r}")
    if not isinstance(arg, Mapping) or "attr" not in arg:
        raise PolicyError(f"{op} requires an 'attr'")
    if op != "truthy" and "value" not in arg:
        raise PolicyError(f"{op} requires a 'value'")


# --------------------------------------------------------------------------
# starting policy
# --------------------------------------------------------------------------

def _p(op: str, attr: str, value: Any = None) -> dict[str, Any]:
    return {op: {"attr": attr} if value is None else {"attr": attr, "value": value}}


#: Paths that are never writable from inside a workspace, whatever else allows
#: it. Because deny is evaluated first, this cannot be shadowed by a later rule.
SENSITIVE_GLOBS = [
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/id_rsa*", "**/id_ed25519*",
    "**/.git/hooks/*", "**/.git/config", "**/.ssh/*", "**/.aws/*", "**/.npmrc",
    "**/.optimus/keys/*",
]


def baseline_policy() -> Policy:
    """A defensible default: read freely inside the workspace, stage writes,
    approve execution, refuse the rest by structure.

    Contrast Bellona's shipped default, which denied 18 of its own 24 exposed
    tools and whose `--allow-shell` flag did nothing (`audit.md` §2.11). Every
    verb a tool can declare is classified here, so a registered tool is either
    usable or explicitly refused — never silently unreachable.
    """
    return Policy([
        Rule("deny-sensitive-write", RuleEffect.DENY,
             {"all": [
                 {"in": {"attr": "verb", "value": ["write", "delete"]}},
                 {"glob_any": {"attr": "target.relpath", "value": SENSITIVE_GLOBS}},
             ]},
             "secrets and VCS hooks are never writable from a workspace"),
        Rule("deny-credential-to-model", RuleEffect.DENY,
             {"all": [{"eq": {"attr": "verb", "value": "credential"}},
                      {"truthy": {"attr": "trust.untrusted"}}]},
             "untrusted material may never reach credentials"),

        Rule("approve-execute", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "execute"}}),
        Rule("approve-delete", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "delete"}}),
        Rule("approve-network-send", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "network_send"}}),
        Rule("approve-publish", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "publish"}}),
        Rule("approve-input", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "input"}}),
        Rule("approve-credential", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "credential"}}),

        Rule("allow-read", RuleEffect.ALLOW,
             {"eq": {"attr": "verb", "value": "read"}}),
        Rule("allow-navigate", RuleEffect.ALLOW,
             {"eq": {"attr": "verb", "value": "navigate"}}),
        Rule("allow-invoke", RuleEffect.ALLOW,
             {"eq": {"attr": "verb", "value": "invoke"}}),
        # Writes are free *into an overlay* because nothing real changes there;
        # committing the overlay is the authorised act. Achilles's DiffSandbox
        # insight (`audit.md` §3.7), promoted to the default.
        Rule("allow-overlay-write", RuleEffect.ALLOW,
             {"all": [{"eq": {"attr": "verb", "value": "write"}},
                      {"eq": {"attr": "reversibility", "value": "overlay"}}]}),
        Rule("approve-real-write", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "write"}}),
    ])

#: Paths a benchmark agent has no business touching, whatever the task says.
#: `/tests` and `/solution` are where Harbor stages the grader and the reference
#: solution; `/logs/verifier` is where the reward is written.
GRADER_GLOBS = [
    "/tests/**", "/tests", "/solution/**", "/solution",
    "/logs/verifier/**", "/logs/verifier",
    "C:/tests/**", "C:/solution/**", "C:/logs/verifier/**",
]


def benchmark_policy() -> Policy:
    """The rule set for an unattended run inside a disposable container.

    Say the trade plainly, because the interesting part of this function is what
    it gives up. `baseline_policy` parks execution and real writes for a human to
    approve. There is no human in a Terminal-Bench trial, so this allows both —
    and the thing that makes that defensible is *not* this file. It is:

    * the container, which is the actual wall and is thrown away afterwards; and
    * the owner-signed envelope (`gate/envelope.py`), without which the hard
      untrusted-mutation invariant still parks every one of these actions
      regardless of what the rules below say.

    Policy is the third lock, not the first. What it still contributes here is
    the deny list, which no envelope and no container can override, because deny
    is evaluated first and structurally.

    **The grader rules are tripwires, not walls.** `deny-grader-write` genuinely
    stops a resolved path; `deny-grader-script` pattern-matches shell text, which
    catches an agent that wanders into `/tests` by accident and is stepped around
    by one substitution by an agent that means to. It is included because the
    accident is common and the number is worth publishing, and it is labelled
    this way so nobody reads it as a sandbox.
    """
    rules = list(baseline_policy().rules)
    keep = {
        "deny-sensitive-write",
        "deny-credential-to-model",
        "allow-read",
        "allow-navigate",
        "allow-invoke",
        "allow-overlay-write",
    }
    out = [r for r in rules if r.id in keep]
    out += [
        Rule("deny-grader-write", RuleEffect.DENY,
             {"all": [
                 {"in": {"attr": "verb", "value": ["write", "delete"]}},
                 {"glob_any": {"attr": "target.path", "value": GRADER_GLOBS}},
             ]},
             "the grading harness is never writable by the agent under test"),
        Rule("deny-grader-script", RuleEffect.DENY,
             {"glob_any": {"attr": "target.script",
                           "value": ["*/tests/*", "*/solution/*", "*/logs/verifier/*"]}},
             "a shell command naming the grader's directories (tripwire, not a wall)"),

        # No approval rules: there is nobody to approve. An action either
        # satisfies the deny list and the envelope, or it does not happen.
        Rule("allow-execute", RuleEffect.ALLOW,
             {"eq": {"attr": "verb", "value": "execute"}}),
        Rule("allow-write", RuleEffect.ALLOW,
             {"eq": {"attr": "verb", "value": "write"}}),
        Rule("approve-delete", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "delete"}}),
        Rule("approve-network-send", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "network_send"}}),
        Rule("approve-credential", RuleEffect.APPROVAL,
             {"eq": {"attr": "verb", "value": "credential"}}),
    ]
    return Policy(out)
