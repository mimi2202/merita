"""
store.py — the sealed commitment ledger.

WHY THIS IS POSTGRES AND NOT THE SQLITE FILE IT USED TO BE
──────────────────────────────────────────────────────────
It was SQLite on a mounted disk. Then the free tier turned out not to support disks, and the
tempting move was "fine, put the .db on ephemeral storage and ship it." That would have been
a catastrophe hiding as a config tweak.

Render's free instances spin down after ~15 minutes idle. Ephemeral disk + spin-down means
the database is destroyed roughly every hour. Picture the failure: a poster commits an
acceptance test, publishes the hash, funds escrow, and waits for a worker. Twenty minutes of
quiet. The instance sleeps. The disk evaporates. The worker delivers, the poster calls
verify_deliverable — and Merita says "I have no commitment for that task."

There is now live escrow riding on a hash the referee cannot resolve. The poster cannot
release, the worker cannot be paid, and the ONE property Merita sells — that the standard was
fixed in advance and both sides are bound to it — is gone. Not degraded. Gone. And it would
have failed silently, intermittently, and only under exactly the conditions a demo doesn't
reproduce.

Durability is not an operational nicety here. It IS the product. So: external Postgres.

STILL NO READ PATH FOR `source`
───────────────────────────────
There is deliberately no method that returns a commitment's test source to a caller. Not for
the poster who wrote it (they have it), and certainly not for anyone else. `get()` is used
only internally, at verification, in the same process that immediately ships it to the
sandbox. Enforced by having no other function, rather than by a comment saying "don't."
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
from dataclasses import dataclass

import psycopg
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)

DSN = os.environ.get("DATABASE_URL", "")


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
    def __init__(self, dsn: str | None = None) -> None:
        dsn = dsn or DSN
        if not dsn:
            # Fail at BOOT, not at the first commit. A referee that starts cleanly and then
            # cannot persist is a referee that will take a commitment, tell the poster it is
            # sealed, and lose it. Refuse to run instead.
            raise RuntimeError(
                "DATABASE_URL is not set. Merita will not start without durable storage: "
                "a lost commitment strands live escrow. Provision Postgres (Neon is free)."
            )

        # Small pool: the free tier has few connections and we are not high-throughput.
        # `check` reconnects transparently after Neon suspends an idle branch — without it,
        # the first request after a quiet spell dies on a stale socket. That request is
        # disproportionately likely to be the OKX reviewer.
        self._pool = ConnectionPool(dsn, min_size=1, max_size=4, check=ConnectionPool.check_connection)
        self._migrate()

    def _migrate(self) -> None:
        with self._pool.connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS commitments (
                    task_id     TEXT PRIMARY KEY,
                    source      TEXT NOT NULL,
                    nonce       TEXT NOT NULL,
                    commitment  TEXT NOT NULL,
                    spec        TEXT NOT NULL,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
        log.info("commitment store ready")

    def commit(self, *, task_id: str, source: str, spec: str) -> str:
        """
        Idempotent. Re-committing the same task_id with the SAME test returns the same hash.
        Re-committing with a DIFFERENT test is REFUSED.

        That refusal is the point. Goalpost-moving usually happens at verification, and the
        hash check catches it there. But a poster could also try it earlier — overwrite the
        commitment before the worker delivers, then reveal the new test and have the hashes
        match perfectly. The ON CONFLICT DO NOTHING below closes that door at the database
        level, atomically, so it cannot be lost to a race between two concurrent commits.
        Enforced by the primary key, not by a check-then-write that a scheduler could split.
        """
        nonce = secrets.token_hex(16)
        c = _commitment(source, nonce)

        with self._pool.connection() as conn:
            row = conn.execute(
                """
                INSERT INTO commitments (task_id, source, nonce, commitment, spec)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO NOTHING
                RETURNING commitment
                """,
                (task_id, source, nonce, c, spec),
            ).fetchone()

            if row:
                return row[0]          # fresh commit

            # Already exists. Same test → return the existing hash. Different test → refuse.
            existing = conn.execute(
                "SELECT source, commitment FROM commitments WHERE task_id = %s", (task_id,)
            ).fetchone()

        if existing and existing[0] == source:
            return existing[1]

        raise ValueError(
            f"task '{task_id}' already has a committed acceptance test, and it is not this "
            f"one. A commitment cannot be replaced — that is the entire point of committing."
        )

    def get(self, task_id: str) -> Commitment | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT task_id, source, nonce, commitment, spec FROM commitments WHERE task_id = %s",
                (task_id,),
            ).fetchone()
        return Commitment(*row) if row else None

    def health(self) -> bool:
        try:
            with self._pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.Error:
            return False
