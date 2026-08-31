"""Event vocabulary for the Ledger.

Invariant 2 (apex.md §3): the Ledger is the *only* system of record. Memory,
sessions, audit, replay, undo and cost accounting are projections over these
events, never independent stores.

Invariant 3: trust is provenance and it never widens. Every event carries the
trust label of the material that produced it, so a projection can always answer
"where did this come from" without re-deriving it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TrustLabel(StrEnum):
    """Provenance of the material that motivated an action.

    Adopted from Achilles (`audit.md` §3.7), which is the only one of the two
    prior systems that enforced this consistently. The set is closed on purpose:
    a new source of input must be classified before it can be used.
    """

    TRUSTED_USER = "trusted_user"
    TRUSTED_LOCAL = "trusted_local"
    EXECUTION_RESULT = "execution_result"
    VERIFIED_RESULT = "verified_result"

    UNTRUSTED_MODEL_OUTPUT = "untrusted_model_output"
    UNTRUSTED_WEB = "untrusted_web"
    UNTRUSTED_DOCUMENT = "untrusted_document"
    UNTRUSTED_EMAIL = "untrusted_email"
    UNTRUSTED_COLLABORATION = "untrusted_collaboration"
    UNTRUSTED_MCP = "untrusted_mcp"

    @property
    def is_untrusted(self) -> bool:
        """Anything not positively classified as trusted is untrusted.

        Deliberately expressed as a negative test over a small allow-set rather
        than a positive test over the untrusted set: adding a new label without
        classifying it must fail closed, not open.
        """
        return self not in {
            TrustLabel.TRUSTED_USER,
            TrustLabel.TRUSTED_LOCAL,
            TrustLabel.EXECUTION_RESULT,
            TrustLabel.VERIFIED_RESULT,
        }


def canonical(obj: Any) -> bytes:
    """Byte-stable serialisation for hashing and signing.

    Sorted keys, no insignificant whitespace, ASCII-escaped. Bellona hashed with
    one serialiser and verified with another (`audit.md` §2.8 neighbourhood);
    having exactly one function that both sides call removes that whole class of
    "verification drifts from production" bug.
    """
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class Event:
    """One immutable row. `hash` commits to the row *and* its predecessor."""

    seq: int
    ts_ms: int
    kind: str
    trust: TrustLabel
    payload: dict[str, Any]
    prev_hash: str
    hash: str
    #: Ed25519 signature over `hash`, by the agent key named in `signer`.
    signature: str = ""
    #: Hex public key of the agent that signed. Meaningless until an owner-signed
    #: checkpoint vouches for it — see `chain.verify`.
    signer: str = ""

    def body(self) -> dict[str, Any]:
        """Exactly the fields the hash commits to."""
        return {
            "seq": self.seq,
            "ts_ms": self.ts_ms,
            "kind": self.kind,
            "trust": str(self.trust),
            "payload": self.payload,
            "prev_hash": self.prev_hash,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "hash": self.hash, "signature": self.signature, "signer": self.signer}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Event:
        return Event(
            seq=int(d["seq"]),
            ts_ms=int(d["ts_ms"]),
            kind=str(d["kind"]),
            trust=TrustLabel(d["trust"]),
            payload=d.get("payload") or {},
            prev_hash=str(d["prev_hash"]),
            hash=str(d["hash"]),
            signature=str(d.get("signature", "")),
            signer=str(d.get("signer", "")),
        )


@dataclass(frozen=True, slots=True)
class Checkpoint:
    """An owner attestation over a prefix of the chain.

    This is the piece Bellona was missing (`audit.md` §2.3). Its gateway minted
    both the agent key *and* the owner key and signed both halves itself, so a
    receipt only ever proved internal consistency. Here the owner key lives
    outside the Gate process entirely: the Gate holds a fingerprint, and only an
    out-of-band `attest` step can produce one of these. A chain with no valid
    checkpoint verifies as *unattested*, never as valid.
    """

    head_seq: int
    head_hash: str
    #: Agent public keys this owner vouches for, up to `head_seq`.
    agent_pubs: tuple[str, ...]
    ts_ms: int
    owner_pub: str
    signature: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "head_seq": self.head_seq,
            "head_hash": self.head_hash,
            "agent_pubs": list(self.agent_pubs),
            "ts_ms": self.ts_ms,
            "owner_pub": self.owner_pub,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.body(), "signature": self.signature}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Checkpoint:
        return Checkpoint(
            head_seq=int(d["head_seq"]),
            head_hash=str(d["head_hash"]),
            agent_pubs=tuple(d.get("agent_pubs") or ()),
            ts_ms=int(d["ts_ms"]),
            owner_pub=str(d["owner_pub"]),
            signature=str(d.get("signature", "")),
        )


@dataclass
class Meter:
    """Per-action cost. Invariant 5: a capability that cannot be metered cannot
    be promoted (`apex.md` §3).

    `no_action` exists because the Scaffold Effect study found idle turns to be
    one of the two drivers of the 20-40x token spread between harnesses
    (`research.md` §2.1), and no shipping harness reports it.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    wall_ms: int = 0
    no_action: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "wall_ms": self.wall_ms,
            "no_action": self.no_action,
            **self.extra,
        }
