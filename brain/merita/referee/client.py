"""
client.py — the brain's handle on the verifier.

One rule, and it is the most important rule in the whole system:

    IF THE VERIFIER IS UNREACHABLE, WE DO NOT FAIL THE WORKER.

An honest worker who did the job perfectly must never lose their stake because our container
was OOM-killed, or the network blipped, or we shipped a bug. "The referee is broken" and
"the deliverable is bad" are different sentences. A market that conflates them destroys its
own supply side — workers learn that doing good work is a coin-flip, and they leave, and
they do not come back.

So on any transport failure we return a Verdict with confidence=0.0 and reveal_valid=True,
which the orchestrator reads as ESCALATE (to Tier 2), not FAIL. The cost of that policy is
that a determined attacker who can DoS our verifier gets their work adjudicated by an LLM
instead of a unit test. That is a real cost and I am paying it deliberately: it is strictly
cheaper than the alternative, which is slashing honest people.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ..models import Tier, Verdict

log = logging.getLogger(__name__)


class VerifierClient:
    def __init__(self, base: str | None = None) -> None:
        self._base = base or os.environ.get("VERIFIER_URL", "http://127.0.0.1:9000")
        # Generous but finite. The sandbox's own wall-clock cap is 10s per run and it runs
        # the check twice; 45s leaves room for scheduling without hanging a settlement forever.
        self._c = httpx.AsyncClient(timeout=45.0)

    async def verify(
        self, *, revealed_source: str, revealed_nonce: str, commitment: str, deliverable: Any
    ) -> Verdict:
        try:
            r = await self._c.post(
                f"{self._base}/verify",
                json={
                    "revealed_source": revealed_source,
                    "revealed_nonce": revealed_nonce,
                    "commitment": commitment,
                    "deliverable": deliverable,
                },
            )
            r.raise_for_status()
            return Verdict.model_validate(r.json())

        except Exception as e:
            log.error("verifier unreachable (%s) — escalating rather than failing worker", e)
            return Verdict(
                passed=False,
                tier_used=Tier.DETERMINISTIC,
                confidence=0.0,        # <- the orchestrator reads this as "escalate", not "fail"
                reveal_valid=True,
                reason=f"deterministic verifier unavailable ({type(e).__name__}); escalating to Tier 2",
            )

    async def close(self) -> None:
        await self._c.aclose()
