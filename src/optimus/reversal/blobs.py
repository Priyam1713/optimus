"""Content-addressed storage for the bytes an undo needs.

A compensation is only real if the thing it restores still exists. Putting file
contents inline in the Ledger would work and would also make the one structure
everything projects from grow without bound, so prior state goes here and the
Ledger carries the hash.

Content addressing means the common case — the agent rewrites the same file
twenty times — stores each distinct version once.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


class BlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        # Two-level fan-out: a flat directory with a hundred thousand entries is
        # slow to enumerate on every filesystem that matters.
        return self.root / digest[:2] / digest[2:]

    def put(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()
        target = self._path(digest)
        if target.exists():
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
        os.replace(tmp, target)
        return digest

    def get(self, digest: str) -> bytes:
        return self._path(digest).read_bytes()

    def has(self, digest: str) -> bool:
        return self._path(digest).exists()

    def __len__(self) -> int:
        return sum(1 for p in self.root.rglob("*") if p.is_file() and p.suffix != ".tmp")
