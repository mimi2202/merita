"""
http_x402.py — the PLAIN-HTTP x402 door.

WHY THIS EXISTS (an OKX admin found the wall):
──────────────────────────────────────────────
Merita's /mcp endpoint is an MCP StreamableHTTP server. That is the right surface for an
MCP client like Claude Code — and it works; we settled real payments through it. But OKX's
BUYER tooling (`agent task-402-pay`) is not an MCP client. It speaks plain HTTP x402:

    POST a plain JSON body, with `Accept: application/json` (NOT text/event-stream),
    replay with X-PAYMENT after settling, expect the RESULT in the 200 body.

Against /mcp that buyer got:
    · plain JSON body        -> 402 (our paywall, but it never reached a session)
    · MCP-framed body        -> 406 (-32600: "must accept application/json AND text/event-stream")

i.e. the verdict was permanently trapped behind an MCP session handshake the buyer does not
perform. OKX's own docs confirm the fix: a seller "offer[s] an API OR MCP service", and the
plain-HTTP path uses SYNCHRONOUS settlement — pay, server confirms on-chain, server returns
the resource in the body. The A2MCP and plain-HTTP surfaces share ONE wire format; they
differ only in whether a session wraps them.

So this is a second door into the SAME brain: same commitment store, same sandbox, same
facilitator, same settle-or-accept logic. No business logic is duplicated — only the
entrance is simpler. An agent that speaks MCP uses /mcp; an agent that speaks plain x402
uses /x402/verify. Both get the identical signed verdict.

THE FLOW (synchronous settlement, per OKX docs):
    1. POST /x402/verify  {task_id, deliverable}   no X-PAYMENT
       -> 402 + accepts[]  (in body AND headers; Accept: application/json only)
    2. buyer signs, settles, replays with X-PAYMENT
    3. we honor the settled payment, run the sandbox, and return the VERDICT in the 200 body
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("merita.http_x402")


def make_x402_routes(*, fac, store, verifier, price, resource_url: str):
    """
    Build the plain-HTTP handler. Dependencies are injected (not imported) so this door
    shares the exact same instances the MCP door uses — one store, one sandbox, one
    facilitator. Divergence between the two surfaces is thus impossible by construction.
    """

    async def verify_http(request: Request) -> JSONResponse:
        # ── parse the plain JSON body ────────────────────────────────────────
        try:
            body = await request.json()
        except Exception:
            return _json(400, {"error": "body must be JSON: {task_id, deliverable}"})

        task_id = body.get("task_id")
        deliverable = body.get("deliverable")
        if not task_id:
            return _json(400, {"error": "task_id is required"})

        # ── PRE-PAYMENT GATE: never charge for a guaranteed non-verdict ──────
        # Same rule as the MCP door: if there's no committed test, say so for FREE.
        rec = store.get(task_id)
        if rec is None:
            return _json(200, {
                "error": f"no committed acceptance test for task '{task_id}'. "
                         f"Commit one first (free). No payment was taken.",
                "charged": False,
            })

        reqs = fac.requirements(
            resource_url=resource_url,
            description="Merita — verify a deliverable against a committed test",
            price=price,
        )

        # ── x402 challenge: no payment yet -> 402 with accepts[] ─────────────
        # Plain HTTP. The accepts array is in the BODY (what task-402-pay reads) AND the
        # headers (belt and braces). Crucially: NO requirement on Accept: text/event-stream.
        x_payment = request.headers.get("x-payment")
        if not x_payment:
            challenge = fac.challenge_header(reqs)
            return JSONResponse(
                {"x402Version": 2, "accepts": [reqs], "accepts_b64": challenge},
                status_code=402,
                headers={
                    "payment-required": challenge,
                    "www-authenticate": 'Payment realm="merita", x402Version=2',
                },
            )

        # ── honor the settled payment (same logic as the MCP door) ──────────
        settled = await fac.settle_or_accept(x_payment, reqs)
        if not settled.ok:
            log.warning("plain-http x402 payment not honored: %s", settled.reason)
            challenge = fac.challenge_header(reqs)
            return JSONResponse(
                {"x402Version": 2, "accepts": [reqs], "error": settled.reason},
                status_code=402,
                headers={"payment-required": challenge},
            )

        # ── PAID. Run the sandbox and return the verdict IN THE BODY. ───────
        # This is the whole point of the plain-HTTP door: synchronous settlement means the
        # deliverable comes back in this 200, not behind a session the buyer never opens.
        verdict = await verifier.verify(
            revealed_source=rec.source,
            revealed_nonce=rec.nonce,
            commitment=rec.commitment,
            deliverable=deliverable,
        )

        receipt = None
        try:
            receipt = fac.receipt_header(settled.receipt) if settled.receipt else None
        except Exception:
            pass

        return JSONResponse({
            "task_id": task_id,
            "passed": verdict.passed,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "reveal_valid": verdict.reveal_valid,
            "commitment": rec.commitment,
            "settle_escrow": verdict.passed,
            "charged": True,
            "payment_receipt": receipt,
        }, headers={"x-payment-response": receipt} if receipt else None)

    return verify_http


def _json(status: int, obj: dict) -> JSONResponse:
    return JSONResponse(obj, status_code=status)