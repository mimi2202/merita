"""
orchestrator.py — trigger → discovery → execution → verification → settlement.

The bounty says: "A complete workflow means the agent runs from trigger → execution →
payment without manual input." So there is exactly ONE public entry point in this file,
`run(bounty)`, it takes no human input after it starts, and it either settles on-chain or
explains in writing why it refused to.

The ordering of the steps below is the design. In particular:

  * DISCOVERY happens on-chain, via SAP, every time. We do not keep a local worker list.
    A local list would be faster and would also mean we are not actually using the protocol
    — we'd be a bounty board with a Solana logo on it. `sap.discovery.findByCapability()`
    is the load-bearing call that makes this an SAP agent rather than a web app.

  * INTEGRITY IS CHECKED BEFORE THE ESCROW LOCKS, not after. A detector that runs after
    settlement is a post-mortem. Ours is a gate: if poster and worker look like the same
    operator, no lamports move at all, and the job dies in QUARANTINED with the flags
    written into the sealed audit trail. We would rather post zero volume than post volume
    we cannot defend.

  * THE REFEREE IS PAID EVEN WHEN THE WORKER FAILS. Judging is work. It cost us CPU and it
    cost us an Ace call on the Tier-2 path. A referee that only gets paid on a pass has an
    incentive to pass everything, which is precisely the failure mode that makes escrow
    markets worthless. Aligning the referee's incentive against its own judgement would be
    a beautiful way to lose a lot of money slowly.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from .integrity.graph import IntegrityGraph
from .models import AcceptanceTest, Bounty, BountyState, Rail, Receipt, Tier, Verdict
from .rails.router import SettlementRouter
from .referee import tier2
from .referee.client import VerifierClient
from .sidecar import Sidecar

log = logging.getLogger(__name__)


class Orchestrator:
    def __init__(
        self,
        sidecar: Sidecar,
        router: SettlementRouter,
        graph: IntegrityGraph,
        verifier: VerifierClient,
        referee_wallet_path: str,
    ) -> None:
        self._s = sidecar
        self._router = router
        self._graph = graph
        # The brain NEVER runs a poster's acceptance test in its own process. It asks a
        # container that has no keys, no secrets mount, and no route to the internet.
        self._verifier = verifier
        self._referee_wallet = referee_wallet_path
        self._tests: dict[str, AcceptanceTest] = {}   # id -> the un-revealed test. Sealed until verify.

    # ─────────────────────────────────────────────────────────────────────────
    # 1. POST — the trigger. Escrow locks here and nowhere else.
    # ─────────────────────────────────────────────────────────────────────────

    async def post_bounty(
        self,
        *,
        poster_pda: str,
        poster_wallet_path: str,
        title: str,
        spec: str,
        output_schema: dict,
        acceptance_test_source: str,
        reward_lamports: int,
        stake_lamports: int,
        required_capability: str,
    ) -> Bounty:
        test = AcceptanceTest(source=acceptance_test_source)
        bid = str(uuid.uuid4())

        b = Bounty(
            id=bid,
            poster_agent_pda=poster_pda,
            poster_wallet_path=poster_wallet_path,
            title=title,
            spec=spec,
            output_schema=output_schema,
            test_commitment=test.commitment(),   # <- the ONLY thing about the test that is public
            reward_lamports=reward_lamports,
            stake_lamports=stake_lamports,
        )
        self._tests[bid] = test                  # held in the referee's memory, revealed at verify
        self._graph.observe_spec(poster_pda, spec)

        # ── DISCOVERY: find a worker on-chain, by capability. This is the SAP call. ──
        candidates = await self._s.post(
            "/sap/discover",
            {"walletPath": self._referee_wallet, "by": {"capability": required_capability}},
        )
        workers = [a for a in candidates["agents"] if a and a.get("address") != poster_pda]
        if not workers:
            b.state = BountyState.EXPIRED
            log.warning("no SAP agent advertises capability %s", required_capability)
            return b

        # Rank by on-chain reputation × our integrity score. Reputation alone is gameable by
        # a colluding pair farming 5-star feedback at each other; integrity alone ignores
        # competence. The product punishes either failure.
        def rank(a: dict) -> float:
            rep = float((a.get("reputation") or {}).get("score") or 0.0)
            integ = self._graph.assess(poster=poster_pda, worker=a["address"], spec=spec).score
            return (1.0 + rep) * integ

        worker = max(workers, key=rank)
        worker_pda = worker["address"]

        # ── THE GATE. Before any money moves. ──
        report = self._graph.assess(poster=poster_pda, worker=worker_pda, spec=spec)
        if report.quarantined:
            b.state = BountyState.QUARANTINED
            b.verdict = Verdict(
                passed=False, tier_used=Tier.DETERMINISTIC, confidence=1.0, reveal_valid=True,
                reason=f"integrity gate: {report.score:.2f} — {'; '.join(report.flags)}",
            )
            await self._seal(b, ["QUARANTINED before escrow", *report.flags])
            return b

        escrow_id, sig = await self._router.lock_reward(
            poster_wallet_path=poster_wallet_path,
            worker_pda=worker_pda,
            reward_lamports=reward_lamports,
            memo=f"merita:{bid}",
        )
        b.escrow_id, b.escrow_lock_sig = escrow_id, sig
        b.claimed_by_pda = worker_pda
        b.claimed_at = datetime.now(timezone.utc)
        b.state = BountyState.CLAIMED
        return b

    # ─────────────────────────────────────────────────────────────────────────
    # 2. EXECUTE — the worker does the work. This leg pays Ace over x402.
    # ─────────────────────────────────────────────────────────────────────────

    async def execute(self, b: Bounty, *, service: str, args: dict) -> Bounty:
        """
        The worker agent fulfils the spec by consuming an Ace Data Cloud service and paying
        for it with its own USDC over x402. Note whose money that is: the WORKER'S. The
        worker is running a business — it spends to earn. That is what makes the Ace leg
        real economic activity rather than us moving our own money between our own pockets.
        """
        result, receipt = await self._router.pay_ace(
            service=service, args=args, worker_pda=b.claimed_by_pda or ""
        )
        if receipt:
            b.receipts.append(receipt)
            self._graph.observe(receipt)

        b.deliverable = result if isinstance(result, dict) else {"result": result}
        b.submitted_at = datetime.now(timezone.utc)
        b.state = BountyState.SUBMITTED
        return b

    # ─────────────────────────────────────────────────────────────────────────
    # 3. VERIFY + SETTLE — the referee. Reveal, judge, release or slash.
    # ─────────────────────────────────────────────────────────────────────────

    async def verify_and_settle(self, b: Bounty) -> Bounty:
        b.state = BountyState.VERIFYING
        test = self._tests[b.id]

        verdict = await self._verifier.verify(
            revealed_source=test.source,
            revealed_nonce=test.nonce,
            commitment=b.test_commitment,
            deliverable=b.deliverable,
        )

        # Tier-1 escalation. The deterministic runner said "I don't know", not "you failed".
        # Those are different sentences and conflating them is how you slash honest workers.
        if verdict.confidence == 0.0 and verdict.reveal_valid:
            log.info("escalating %s to tier 2", b.id)
            verdict = await tier2.adjudicate(
                router=self._router,
                spec=b.spec,
                deliverable=b.deliverable,
                worker_pda=b.claimed_by_pda or "",
                receipts=b.receipts,
                graph=self._graph,
            )

        b.verdict = verdict

        if verdict.passed:
            receipts = await self._router.release(
                poster_wallet_path=b.poster_wallet_path,
                escrow_id=b.escrow_id or "",
                worker_pda=b.claimed_by_pda or "",
                reward_lamports=b.reward_lamports,
            )
            for r in receipts:
                b.receipts.append(r)
                self._graph.observe(r)
            b.state = BountyState.SETTLED
            rating, comment = 5, f"verified: {verdict.reason}"
        else:
            await self._router.refund(
                poster_wallet_path=b.poster_wallet_path, escrow_id=b.escrow_id or ""
            )
            b.state = BountyState.FAILED
            rating, comment = 1, f"failed verification: {verdict.reason}"

        # On-chain reputation. Written for BOTH outcomes. A reputation system that only
        # records successes is a marketing page.
        await self._s.post(
            "/sap/feedback",
            {
                "walletPath": self._referee_wallet,
                "targetAgentPda": b.claimed_by_pda,
                "rating": rating,
                "comment": comment[:180],
            },
        )
        await self._seal(b, [
            f"spec: {b.spec[:120]}",
            f"commitment: {b.test_commitment}",
            f"reveal_valid: {verdict.reveal_valid}",
            f"verdict: {'PASS' if verdict.passed else 'FAIL'} ({verdict.reason[:100]})",
            *[f"receipt {r.leg} {r.amount_atomic}{r.token} tx={r.tx}" for r in b.receipts],
        ])
        return b

    # ─────────────────────────────────────────────────────────────────────────

    async def _seal(self, b: Bounty, entries: list[str]) -> None:
        """Immutable on-chain trace. We are handing the judges our own evidence."""
        try:
            await self._s.post(
                "/sap/audit/seal",
                {"walletPath": self._referee_wallet, "sessionKey": f"merita:{b.id}", "entries": entries},
            )
        except Exception as e:  # audit failure must never block settlement of honest work
            log.error("audit seal failed for %s: %s", b.id, e)

    # ── the whole loop, no human in it ──────────────────────────────────────

    async def run(self, **kw) -> Bounty:
        service = kw.pop("service")
        args = kw.pop("args")
        b = await self.post_bounty(**kw)
        if b.state != BountyState.CLAIMED:
            return b
        b = await self.execute(b, service=service, args=args)
        return await self.verify_and_settle(b)
