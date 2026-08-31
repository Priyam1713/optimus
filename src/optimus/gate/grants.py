"""Capability grants, bound to one action rather than to a category.

`audit.md` §3.2: Achilles checked grants *before* policy and keyed them on
`(action, scope)`. One human approval of `execute:workspace` therefore switched
the untrusted-content gate off for every execution in that scope until the TTL
expired — a fact its own comment reasoned toward and never stated.

A grant here names an `instance_digest`: this verb, on this resolved target.
Approving one command approves one command.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


def _now() -> float:
    return time.time()


@dataclass(slots=True)
class Grant:
    subject: str
    instance_digest: str
    issued_by: str
    expires_at: float
    single_use: bool = True
    uses: int = 0
    revoked: bool = False

    def active(self) -> bool:
        if self.revoked or _now() >= self.expires_at:
            return False
        return not (self.single_use and self.uses > 0)


class GrantStore:
    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], Grant] = {}

    def issue(
        self,
        subject: str,
        instance_digest: str,
        *,
        issued_by: str,
        ttl_seconds: float = 300.0,
        single_use: bool = True,
    ) -> Grant:
        g = Grant(
            subject=subject,
            instance_digest=instance_digest,
            issued_by=issued_by,
            expires_at=_now() + ttl_seconds,
            single_use=single_use,
        )
        self._grants[(subject, instance_digest)] = g
        return g

    def find(self, subject: str, instance_digest: str) -> Grant | None:
        g = self._grants.get((subject, instance_digest))
        return g if g and g.active() else None

    def consume(self, grant: Grant) -> None:
        grant.uses += 1

    def revoke(self, subject: str, instance_digest: str) -> bool:
        g = self._grants.get((subject, instance_digest))
        if not g:
            return False
        g.revoked = True
        return True

    def revoke_all(self, subject: str | None = None) -> int:
        n = 0
        for (subj, _), g in self._grants.items():
            if (subject is None or subj == subject) and not g.revoked:
                g.revoked = True
                n += 1
        return n
