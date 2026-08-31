"""Key material, and the boundary the Gate is not allowed to cross.

`audit.md` §2.3 recorded the failure this module exists to prevent: Bellona's
gateway generated the agent key *and* the owner key, held both in one in-process
map, signed with both, and its verifier checked each receipt against the public
keys carried inside that same receipt. Anyone could fabricate a chain that
verified.

The rule here: **the Gate process may hold an AgentKey and may know an owner
fingerprint. It may never hold an OwnerKey.** Attesting is a separate operation,
run by a human against a key file the harness cannot read on its own.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def fingerprint(public_hex: str) -> str:
    """Short, human-comparable identity for a public key.

    Used everywhere a human has to confirm "is this the right owner" — the
    argument `verify` refuses to run without.
    """
    return hashlib.sha256(bytes.fromhex(public_hex)).hexdigest()[:16]


class _Signer:
    __slots__ = ("_sk",)

    def __init__(self, sk: Ed25519PrivateKey):
        self._sk = sk

    @property
    def public_hex(self) -> str:
        return self._sk.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        ).hex()

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.public_hex)

    def sign(self, message: bytes) -> str:
        return self._sk.sign(message).hex()

    def _private_bytes(self) -> bytes:
        return self._sk.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )


class AgentKey(_Signer):
    """Signs events. Lives in the Gate process. Rotatable and disposable —
    an agent key is only worth what an owner checkpoint says it is worth."""

    @staticmethod
    def generate() -> AgentKey:
        return AgentKey(Ed25519PrivateKey.generate())

    @staticmethod
    def load_or_create(path: str | Path) -> AgentKey:
        p = Path(path)
        if p.exists():
            return AgentKey(Ed25519PrivateKey.from_private_bytes(p.read_bytes()))
        p.parent.mkdir(parents=True, exist_ok=True)
        key = AgentKey.generate()
        # 0o600 where the platform honours it; on Windows the parent state dir
        # is the real boundary. Written via a fresh fd so the mode applies before
        # any bytes land.
        # O_BINARY matters: on Windows a text-mode fd translates 0x0A to 0x0D0A,
        # and a key whose bytes happen to contain a newline is silently written
        # back one byte longer and fails to load. Caught by the M1 suite on the
        # first restart test.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, key._private_bytes())
        finally:
            os.close(fd)
        return key


class OwnerKey(_Signer):
    """Signs checkpoints. **Must not be constructed inside the Gate.**

    In deployment this is a DPAPI/TPM-backed handle or a key file the operator
    controls. `attest()` is a deliberate human action, not something the loop can
    reach — which is the entire difference between a receipt that proves
    provenance and one that proves only that a program was self-consistent.
    """

    @staticmethod
    def generate() -> OwnerKey:
        return OwnerKey(Ed25519PrivateKey.generate())

    @staticmethod
    def load(path: str | Path) -> OwnerKey:
        return OwnerKey(Ed25519PrivateKey.from_private_bytes(Path(path).read_bytes()))

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        # O_BINARY matters: on Windows a text-mode fd translates 0x0A to 0x0D0A,
        # and a key whose bytes happen to contain a newline is silently written
        # back one byte longer and fails to load. Caught by the M1 suite on the
        # first restart test.
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, self._private_bytes())
        finally:
            os.close(fd)


def verify_signature(public_hex: str, message: bytes, signature_hex: str) -> bool:
    """Constant-answer verification: any malformed input is a failure, never an
    exception that a caller might accidentally treat as success."""
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pk.verify(bytes.fromhex(signature_hex), message)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
