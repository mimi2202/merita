"""verify_client.py — handle on the isolated sandbox container.

ONE RULE, and it is the most important rule in the product:

    IF THE SANDBOX IS UNREACHABLE, WE DO NOT FAIL THE WORKER.

An honest worker must never be rejected because our container was OOM-killed. "The referee is
broken" and "the deliverable is bad" are different sentences, and a referee that conflates them
is worse than no referee — it is a coin-flip with money attached, and on a marketplace with
on-chain reputation, being that referee once is being that referee forever.

So on transport failure: confidence=0.0, which the caller reads as ESCALATE, not FAIL.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from merita.models import Tier, Verdict

log = logging.getLogger(__name__)


class VerifierClient:
    def __init__(self, base: str | None = None) -> None:
        self._base = base or os.environ.get("VERIFIER_URL", "http://verifier:9000")
        self._c = httpx.AsyncClient(timeout=45.0)

    async def verify(self, *, revealed_source: str, revealed_nonce: str,
                     commitment: str, deliverable: Any) -> Verdict:
        try:
            r = await self._c.post(f"{self._base}/verify", json={
                "revealed_source": revealed_source,
                "revealed_nonce": revealed_nonce,
                "commitment": commitment,
                "deliverable": deliverable,
            })
            r.raise_for_status()
            return Verdict.model_validate(r.json())
        except Exception as e:
            log.error("sandbox unreachable (%s) — escalating, NOT failing the worker", e)
            return Verdict(
                passed=False, tier_used=Tier.DETERMINISTIC, confidence=0.0, reveal_valid=True,
                reason=f"deterministic sandbox unavailable ({type(e).__name__}); escalate, do not slash",
            )

    async def close(self) -> None:
        await self._c.aclose()
