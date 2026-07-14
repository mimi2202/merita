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

import asyncio
import logging
import os
from typing import Any

import httpx

from merita.models import Tier, Verdict
from merita.referee import tier1

log = logging.getLogger(__name__)


class VerifierClient:
    """
    Runs an acceptance test and returns a verdict.

    TWO MODES, and the default changed when the deploy did.

    Originally the sandbox lived in its OWN container — no secrets mounted, no route to the
    internet — and this class dialled it over HTTP at http://verifier:9000. That is still the
    right architecture and docker-compose.yml still builds it.

    But Render's free tier gives you ONE container. The `verifier` host does not exist there,
    so this client was calling into the void: ConnectError on every verification, forever.
    A real x402 payment settled on-chain before I found it — the fail-open rule correctly
    returned "escalate, do not slash" instead of failing an honest worker, which is the only
    reason this was a bug and not a scandal.

    So: if VERIFIER_URL is set, use the isolated container (the strong configuration). If it
    is not, run tier1 IN-PROCESS — where containment comes from the sandbox's own privilege
    drop and rlimits (see tier1.py), not from a container boundary. Weaker. Honest about it.
    Never silently broken.
    """

    def __init__(self, base: str | None = None) -> None:
        self._base = base or os.environ.get("VERIFIER_URL", "")
        self._c = httpx.AsyncClient(timeout=45.0) if self._base else None
        if self._base:
            log.info("verifier: isolated container at %s", self._base)
        else:
            log.warning(
                "verifier: IN-PROCESS (no VERIFIER_URL). Untrusted acceptance tests are "
                "contained by privilege-drop + rlimits only, not by a container boundary. "
                "Acceptable on a single-container host; use docker-compose in production."
            )

    async def verify(self, *, revealed_source: str, revealed_nonce: str,
                     commitment: str, deliverable: Any) -> Verdict:
        try:
            if self._c is None:
                # In-process. tier1.verify() blocks for up to 20s (two runs x a 10s wall
                # clock), so it MUST go to a thread — running it on the event loop would
                # freeze every other request, including /health, and Render would conclude
                # the service is dead while it is merely busy.
                return await asyncio.to_thread(
                    tier1.verify,
                    revealed_source=revealed_source,
                    revealed_nonce=revealed_nonce,
                    commitment=commitment,
                    deliverable=deliverable,
                )

            r = await self._c.post(f"{self._base}/verify", json={
                "revealed_source": revealed_source,
                "revealed_nonce": revealed_nonce,
                "commitment": commitment,
                "deliverable": deliverable,
            })
            r.raise_for_status()
            return Verdict.model_validate(r.json())

        except tier1.SandboxUnavailable as e:
            # The HOST cannot safely contain untrusted code. Not the worker's fault. Escalate.
            log.error("SANDBOX NOT ISOLATED: %s", e)
            return Verdict(
                passed=False, tier_used=Tier.DETERMINISTIC, confidence=0.0, reveal_valid=True,
                reason="sandbox isolation unverifiable on this host; escalate, do not slash",
            )
        except Exception as e:
            log.error("sandbox unreachable (%s) — escalating, NOT failing the worker", e)
            return Verdict(
                passed=False, tier_used=Tier.DETERMINISTIC, confidence=0.0, reveal_valid=True,
                reason=f"deterministic sandbox unavailable ({type(e).__name__}); escalate, do not slash",
            )

    async def close(self) -> None:
        if self._c is not None:
            await self._c.aclose()