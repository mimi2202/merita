"""store.py — the sealed commitment ledger.

Holds acceptance tests between commit and reveal. The ONLY thing that leaves this store
before verification is the hash. If a poster could read back their own test they could still
not cheat (they wrote it), but if a WORKER could, the entire scheme collapses — so there is
no read path that returns `source`, by construction, not by discipline.

SQLite, not a dict. A referee that forgets its commitments when the process restarts is a
referee that cannot be trusted across a deploy, and "we lost your commitment, please trust us"
is not a sentence you can say to a counterparty who has money in escrow.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from dataclasses import dataclass

DB = os.environ.get("MERITA_DB", "/data/merita.db")


def _commitment(source: str, nonce: str) -> str:
    h = hashlib.sha256()
    h.update(source.encode())
    h.update(b"\x00")          # domain separator: stops source/nonce boundary shifting
    h.update(nonce.encode())
    return h.hexdigest()


@dataclass(frozen=True)
class Commitment:
    task_id: str
    source: str
    nonce: str
    commitment: str
    spec: str


class CommitStore:
    def __init__(self, path: str = DB) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS commitments (
                task_id     TEXT PRIMARY KEY,
                source      TEXT NOT NULL,
                nonce       TEXT NOT NULL,
                commitment  TEXT NOT NULL,
                spec        TEXT NOT NULL,
                created_at  INTEGER DEFAULT (unixepoch())
            )
        """)
        self._db.commit()

    def commit(self, *, task_id: str, source: str, spec: str) -> str:
        """Idempotent. Re-committing the SAME task_id with a DIFFERENT test is refused —
        that is goalpost-moving attempted one step earlier, and it does not get to succeed
        just because it happened before the worker delivered."""
        existing = self.get(task_id)
        if existing:
            if existing.source != source:
                raise ValueError(
                    f"task {task_id} already has a committed test. You cannot replace it."
                )
            return existing.commitment

        nonce = secrets.token_hex(16)
        c = _commitment(source, nonce)
        self._db.execute(
            "INSERT INTO commitments (task_id, source, nonce, commitment, spec) VALUES (?,?,?,?,?)",
            (task_id, source, nonce, c, spec),
        )
        self._db.commit()
        return c

    def get(self, task_id: str) -> Commitment | None:
        row = self._db.execute(
            "SELECT task_id, source, nonce, commitment, spec FROM commitments WHERE task_id=?",
            (task_id,),
        ).fetchone()
        return Commitment(*row) if row else None
