"""
models.py — the domain.

The single most important type in this file is `AcceptanceTest`, and the single most
important idea is that the poster COMMITS to it before anyone can see it.

Why. A bounty board with a public acceptance test is not a labour market; it is a quiz
with the answers printed on the back. If a worker can read `assert output["sum"] == 42`,
the rational strategy is to return `{"sum": 42}` without doing the work. Every "agents
hiring agents" demo I have seen has this hole, and every one of them papers over it by
using tasks so trivial that cheating and working cost the same.

So: at post time we publish H(test_source || nonce) on-chain and NOTHING else. The worker
sees only the natural-language spec and the output schema. At verification time the poster
reveals (test_source, nonce); the referee recomputes the hash, checks it against the
on-chain commitment, and only then runs the test. A poster who tries to move the goalposts
after seeing a good deliverable — a real attack, and the reason naive escrow markets fail —
cannot produce a preimage that matches, so the referee refuses to fail the worker and the
escrow releases anyway. Commit-reveal makes the referee honest in BOTH directions.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Rail(str, Enum):
    """The two settlement rails. Chosen per-leg, not per-job."""

    SAP_ESCROW = "sap_escrow"   # poster -> worker.  Bounty category 1.
    ACE_X402 = "ace_x402"       # worker -> Ace.     Bounty category 2.


class BountyState(str, Enum):
    DRAFT = "draft"
    OPEN = "open"                 # escrow funded, discoverable
    CLAIMED = "claimed"           # worker bonded, exclusivity window running
    SUBMITTED = "submitted"       # deliverable in, awaiting referee
    VERIFYING = "verifying"
    SETTLED = "settled"           # passed -> escrow released
    FAILED = "failed"             # failed -> stake slashed, bounty reopens
    QUARANTINED = "quarantined"   # collusion detector said no. Nothing settles.
    EXPIRED = "expired"


class Tier(int, Enum):
    DETERMINISTIC = 1   # machine-checkable. No model in the settlement path.
    ADJUDICATED = 2     # evaluator ensemble + confidence. Escalates when unsure.
    ARBITRATED = 3      # staked humans/agents. The backstop, not the path.


# ─────────────────────────────────────────────────────────────────────────────
# Commit–reveal
# ─────────────────────────────────────────────────────────────────────────────

class AcceptanceTest(BaseModel):
    """
    A pure Python function, as source, named `check(output) -> bool`.

    It runs in a hard sandbox (see referee/tier1.py). It is NOT trusted code: the poster
    supplies it, and a malicious poster would love it if `check` could open a socket or
    read our keypair. It cannot. See the sandbox for exactly why.
    """

    source: str
    nonce: str = Field(default_factory=lambda: secrets.token_hex(16))

    def commitment(self) -> str:
        """H(source || nonce). This — and only this — goes on-chain at post time."""
        h = hashlib.sha256()
        h.update(self.source.encode())
        h.update(b"\x00")           # domain separator; prevents source/nonce boundary shifting
        h.update(self.nonce.encode())
        return h.hexdigest()

    @staticmethod
    def verify_reveal(source: str, nonce: str, commitment: str) -> bool:
        return AcceptanceTest(source=source, nonce=nonce).commitment() == commitment


class Bounty(BaseModel):
    id: str
    poster_agent_pda: str
    poster_wallet_path: str

    title: str
    spec: str                        # natural language. This is ALL the worker sees.
    output_schema: dict[str, Any]    # plus this.
    test_commitment: str             # and this hash. Not the test.

    reward_lamports: int
    stake_lamports: int              # worker's bond. Slashed on garbage / ghosting.
    tier: Tier = Tier.DETERMINISTIC

    state: BountyState = BountyState.DRAFT
    escrow_id: str | None = None
    escrow_lock_sig: str | None = None

    claimed_by_pda: str | None = None
    claimed_at: datetime | None = None
    exclusivity_secs: int = 300

    deliverable: dict[str, Any] | None = None
    submitted_at: datetime | None = None

    verdict: Verdict | None = None
    receipts: list[Receipt] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=_now)


class Verdict(BaseModel):
    passed: bool
    tier_used: Tier
    confidence: float               # 1.0 for deterministic. <1 for adjudicated.
    reason: str
    reveal_valid: bool              # did the poster's reveal match the on-chain commitment?
    ran_at: datetime = Field(default_factory=_now)


class Receipt(BaseModel):
    """
    Proof that value moved. Every leg of every job produces exactly one of these.

    `tx` is non-optional in spirit: a receipt without an on-chain signature is a claim,
    not a receipt, and we refuse to count it toward volume. The `tx: str | None` type
    exists only so we can PERSIST a failed settlement for forensics — never so we can
    quietly pass one off as successful. See ledger.countable_volume().
    """

    rail: Rail
    leg: str                        # "reward" | "referee_fee" | "ace:search.google" | ...
    payer: str
    payee: str
    amount_atomic: int
    token: str                      # "SOL" | "USDC"
    tx: str | None
    settled_at: datetime = Field(default_factory=_now)

    @property
    def is_countable(self) -> bool:
        """Volume we would be willing to defend in front of a judge."""
        return self.tx is not None and self.amount_atomic > 0


class AgentProfile(BaseModel):
    pda: str
    wallet: str
    name: str
    capabilities: list[str]
    reputation: float = 0.0
    jobs_settled: int = 0
    jobs_failed: int = 0
    integrity_score: float = 1.0    # 1.0 = clean. 0.0 = certainly colluding.


Bounty.model_rebuild()
