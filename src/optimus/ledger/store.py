"""Append-only persistence for the Ledger.

`audit.md` §2.8: Bellona's "tamper-evident audit ledger" was a `Vec` in memory.
It had a hash chain, a Merkle root and a third-party verifier, and all of it
evaporated on restart — which also made its one-way veto unrecoverable, because
the only way out was the restart that destroyed the evidence.

Append-only is enforced in three places, because one is a convention:

1. **The API** exposes no update or delete.
2. **SQLite triggers** raise on UPDATE and DELETE, so a stray `sqlite3` shell
   cannot quietly rewrite a row either.
3. **The chain** makes any successful rewrite detectable anyway.

The third is the real guarantee; the first two mean tampering has to be
deliberate rather than accidental.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .chain import Chain, VerifyReport, verify
from .events import Checkpoint, Event, TrustLabel
from .keys import AgentKey

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY,
    ts_ms      INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    trust      TEXT    NOT NULL,
    payload    TEXT    NOT NULL,
    prev_hash  TEXT    NOT NULL,
    hash       TEXT    NOT NULL UNIQUE,
    signature  TEXT    NOT NULL,
    signer     TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind);

CREATE TABLE IF NOT EXISTS checkpoints (
    head_seq   INTEGER PRIMARY KEY,
    head_hash  TEXT    NOT NULL,
    agent_pubs TEXT    NOT NULL,
    ts_ms      INTEGER NOT NULL,
    owner_pub  TEXT    NOT NULL,
    signature  TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS events_no_update
BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;

CREATE TRIGGER IF NOT EXISTS events_no_delete
BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'the ledger is append-only'); END;
"""


class LedgerStore:
    """Durable chain. Open it, and the chain resumes where it left off."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        # The chain is the integrity story, but a torn write would still cost the
        # tail of a session. Durability here is cheap relative to a model call.
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> LedgerStore:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ---------------------------------------------------------------

    def append(self, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events (seq, ts_ms, kind, trust, payload, prev_hash, hash, signature, signer)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                event.seq, event.ts_ms, event.kind, str(event.trust),
                json.dumps(event.payload, sort_keys=True, separators=(",", ":"), default=str),
                event.prev_hash, event.hash, event.signature, event.signer,
            ),
        )

    def put_checkpoint(self, cp: Checkpoint) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO checkpoints (head_seq, head_hash, agent_pubs, ts_ms, owner_pub, signature)"
            " VALUES (?,?,?,?,?,?)",
            (cp.head_seq, cp.head_hash, json.dumps(list(cp.agent_pubs)), cp.ts_ms,
             cp.owner_pub, cp.signature),
        )

    # -- reads ----------------------------------------------------------------

    def __len__(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def events(self) -> list[Event]:
        return list(self.iter_events())

    def iter_events(self, *, kind: str | None = None) -> Iterator[Event]:
        sql = "SELECT seq, ts_ms, kind, trust, payload, prev_hash, hash, signature, signer FROM events"
        args: tuple[Any, ...] = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (kind,)
        sql += " ORDER BY seq"
        for row in self._db.execute(sql, args):
            yield Event(
                seq=row[0], ts_ms=row[1], kind=row[2], trust=TrustLabel(row[3]),
                payload=json.loads(row[4]), prev_hash=row[5], hash=row[6],
                signature=row[7], signer=row[8],
            )

    def checkpoints(self) -> list[Checkpoint]:
        return [
            Checkpoint(
                head_seq=r[0], head_hash=r[1], agent_pubs=tuple(json.loads(r[2])),
                ts_ms=r[3], owner_pub=r[4], signature=r[5],
            )
            for r in self._db.execute(
                "SELECT head_seq, head_hash, agent_pubs, ts_ms, owner_pub, signature"
                " FROM checkpoints ORDER BY head_seq"
            )
        ]

    def verify(self, *, expected_owner_fingerprint: str) -> VerifyReport:
        return verify(self.events(), self.checkpoints(),
                      expected_owner_fingerprint=expected_owner_fingerprint)


class DurableChain(Chain):
    """A `Chain` that resumes from, and writes through to, a `LedgerStore`.

    Substitutable for `Chain` everywhere — the Gate does not know or care which
    it holds, which is the point of keeping the Gate's dependency on the ledger
    to one method.
    """

    def __init__(self, agent_key: AgentKey, store: LedgerStore):
        super().__init__(agent_key, store.events())
        self._store = store

    @property
    def store(self) -> LedgerStore:
        return self._store

    def append(self, kind: str, payload: dict[str, Any], trust: TrustLabel) -> Event:
        ev = super().append(kind, payload, trust)
        self._store.append(ev)
        return ev
