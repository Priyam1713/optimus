"""The hash chain, and the verifier that refuses to be impressed by itself.

Bellona's `verify_export` checked each signature against the public keys carried
inside the record it was checking (`audit.md` §2.3). It therefore returned VALID
for any well-formed chain, including one an attacker generated from scratch.

`verify()` here takes `expected_owner_fingerprint` as a **required** argument and
reports three separate facts that Bellona collapsed into one:

    chain_valid          — the hashes link
    signatures_valid     — each event was signed by the key it names
    attested_through     — how far an *owner* has vouched for those keys

Only the third is evidence about the world. A chain with no checkpoint is
`unattested`, which is not the same as valid and must never render as a tick.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .events import Checkpoint, Event, TrustLabel, canonical
from .keys import AgentKey, OwnerKey, fingerprint, verify_signature

GENESIS = "genesis"


def now_ms() -> int:
    return int(time.time() * 1000)


def hash_body(body: dict[str, Any]) -> str:
    return hashlib.sha256(canonical(body)).hexdigest()


def merkle_root(events: Sequence[Event]) -> str:
    """Exportable fingerprint of a whole chain.

    Bellona computed this and never put it in the export. Here it is what an
    owner checkpoint can be cheaply compared against.
    """
    if not events:
        return GENESIS
    level = [e.hash for e in events]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level), 2):
            left = level[i]
            right = level[i + 1] if i + 1 < len(level) else left
            nxt.append(hashlib.sha256(canonical([left, right])).hexdigest())
        level = nxt
    return level[0]


class Chain:
    """In-memory chain builder. Persistence is `store.LedgerStore`'s job."""

    def __init__(self, agent_key: AgentKey, events: Iterable[Event] = ()):
        self._events: list[Event] = list(events)
        self._agent = agent_key

    @property
    def events(self) -> list[Event]:
        return list(self._events)

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS

    @property
    def head_seq(self) -> int:
        return self._events[-1].seq if self._events else -1

    def append(self, kind: str, payload: dict[str, Any], trust: TrustLabel) -> Event:
        body = {
            "seq": self.head_seq + 1,
            "ts_ms": now_ms(),
            "kind": kind,
            "trust": str(trust),
            "payload": payload,
            "prev_hash": self.head_hash,
        }
        h = hash_body(body)
        ev = Event(
            seq=body["seq"],
            ts_ms=body["ts_ms"],
            kind=kind,
            trust=trust,
            payload=payload,
            prev_hash=body["prev_hash"],
            hash=h,
            signature=self._agent.sign(bytes.fromhex(h)),
            signer=self._agent.public_hex,
        )
        self._events.append(ev)
        return ev


def attest(owner: OwnerKey, events: Sequence[Event]) -> Checkpoint:
    """Produce an owner checkpoint over a chain prefix.

    Deliberately a free function taking an `OwnerKey`: there is no method on the
    Gate that can reach this, because the Gate never holds one.
    """
    if not events:
        raise ValueError("refusing to attest an empty chain")
    head = events[-1]
    agent_pubs = tuple(sorted({e.signer for e in events if e.signer}))
    unsigned = Checkpoint(
        head_seq=head.seq,
        head_hash=head.hash,
        agent_pubs=agent_pubs,
        ts_ms=now_ms(),
        owner_pub=owner.public_hex,
    )
    return replace(unsigned, signature=owner.sign(canonical(unsigned.body())))


@dataclass
class VerifyReport:
    chain_valid: bool = False
    signatures_valid: bool = False
    records: int = 0
    attested_through: int = -1
    owner_fingerprint_matched: bool = False
    failures: list[str] = field(default_factory=list)

    @property
    def unattested_tail(self) -> int:
        return max(0, (self.records - 1) - self.attested_through)

    @property
    def fully_valid(self) -> bool:
        """Every claim must hold, including that an owner vouched for the *whole*
        chain. A partially-attested chain is a real and useful state; it is not
        a valid one."""
        return (
            self.chain_valid
            and self.signatures_valid
            and self.owner_fingerprint_matched
            and self.records > 0
            and self.attested_through == self.records - 1
            and not self.failures
        )

    def render(self) -> str:
        if self.fully_valid:
            status = "VALID"
        elif self.chain_valid and self.signatures_valid:
            status = f"UNATTESTED (tail of {self.unattested_tail})"
        else:
            status = "TAMPERED"
        return (
            f"{status} - records={self.records} chain={self.chain_valid} "
            f"signatures={self.signatures_valid} attested_through={self.attested_through} "
            f"owner_match={self.owner_fingerprint_matched} failures={len(self.failures)}"
        )


def verify(
    events: Sequence[Event],
    checkpoints: Sequence[Checkpoint],
    *,
    expected_owner_fingerprint: str,
) -> VerifyReport:
    """Verify a chain against an owner identity supplied from outside it.

    `expected_owner_fingerprint` is keyword-only and required. There is no
    overload that omits it, because the omission is the vulnerability.
    """
    if not expected_owner_fingerprint:
        raise ValueError(
            "expected_owner_fingerprint is required: verifying a chain against "
            "the keys it carries proves only self-consistency"
        )

    report = VerifyReport(records=len(events))

    # 1. the hashes link, and seq is dense and ordered
    prev = GENESIS
    chain_ok = True
    for i, e in enumerate(events):
        if e.seq != i or e.prev_hash != prev:
            report.failures.append(f"seq {e.seq}: broken link (expected index {i}, prev {prev[:12]})")
            chain_ok = False
            break
        if hash_body(e.body()) != e.hash:
            report.failures.append(f"seq {e.seq}: payload does not match its hash")
            chain_ok = False
            break
        prev = e.hash
    report.chain_valid = chain_ok

    # 2. each event was signed by the key it names
    sigs_ok = True
    for e in events:
        if not e.signature or not e.signer:
            report.failures.append(f"seq {e.seq}: unsigned")
            sigs_ok = False
            continue
        if not verify_signature(e.signer, bytes.fromhex(e.hash), e.signature):
            report.failures.append(f"seq {e.seq}: signature does not verify")
            sigs_ok = False
    report.signatures_valid = sigs_ok

    # 3. an owner we already trust vouched for those agent keys.
    #    This is the step whose absence made Bellona's receipts worthless.
    by_seq = {e.seq: e for e in events}
    for cp in sorted(checkpoints, key=lambda c: c.head_seq):
        if fingerprint(cp.owner_pub) != expected_owner_fingerprint:
            report.failures.append(f"checkpoint@{cp.head_seq}: unexpected owner {fingerprint(cp.owner_pub)}")
            continue
        report.owner_fingerprint_matched = True
        if not verify_signature(cp.owner_pub, canonical(cp.body()), cp.signature):
            report.failures.append(f"checkpoint@{cp.head_seq}: owner signature does not verify")
            continue
        head = by_seq.get(cp.head_seq)
        if head is None or head.hash != cp.head_hash:
            report.failures.append(f"checkpoint@{cp.head_seq}: head hash does not match the chain")
            continue
        covered = events[: cp.head_seq + 1]
        unvouched = {e.signer for e in covered if e.signer} - set(cp.agent_pubs)
        if unvouched:
            report.failures.append(
                f"checkpoint@{cp.head_seq}: chain contains agent keys the owner did not vouch for"
            )
            continue
        report.attested_through = max(report.attested_through, cp.head_seq)

    return report
