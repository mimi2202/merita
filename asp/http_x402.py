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

import asyncio
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("merita.http_x402")

# Ceiling on a single verification. The sandbox runs the test twice at 10s wall clock each,
# so 45s leaves headroom for scheduling without ever letting a paid request hang open.
VERIFY_TIMEOUT_S = 45


def make_x402_routes(*, fac, store, verifier, price, resource_url: str):
    """
    Build the plain-HTTP handler. Dependencies are injected (not imported) so this door
    shares the exact same instances the MCP door uses — one store, one sandbox, one
    facilitator. Divergence between the two surfaces is thus impossible by construction.
    """

    async def verify_http(request: Request) -> JSONResponse:
        # FAIL LOUDLY, NEVER SILENTLY.
        #
        # An OKX admin watched a task reach accepted(1) and then produce nothing — the buyer's
        # process "exited without a result". From the outside, silence is indistinguishable
        # from a hang, a crash, or a server that simply doesn't care. Worse, a buyer who has
        # already settled on-chain and receives silence has paid for a void.
        #
        # So every path out of this handler returns a STRUCTURED, MACHINE-READABLE result:
        # an `error` string, an `error_code` a client can branch on, and `charged` so the buyer
        # always knows whether their money moved. No bare 500s, no empty bodies, no hangs.
        try:
            return await _verify_inner(request)
        except Exception as e:
            # Unhandled = our bug. Say so plainly, with a code, and a 500 the client can see.
            log.exception("verify_http failed unexpectedly")
            return _json(500, {
                "error": f"internal error: {type(e).__name__}: {e}",
                "error_code": "internal_error",
                "charged": None,   # unknown — the client must reconcile against the chain
                "advice": "retry; if this persists the settlement may need manual reconciliation",
            })

    async def _verify_inner(request: Request) -> JSONResponse:
        # ── parse the plain JSON body ────────────────────────────────────────
        try:
            body = await request.json()
        except Exception:
            return _json(400, {
                "error": "body must be JSON: {task_id, deliverable}",
                "error_code": "bad_body", "charged": False,
            })

        task_id = _dig(body, "task_id", "taskId")
        deliverable = _dig(body, "deliverable", "output", "result")

        if not task_id:
            return _json(400, {
                "error": "task_id is required",
                "error_code": "missing_task_id", "charged": False,
            })

        # ── PRE-PAYMENT GATE: never charge for a guaranteed non-verdict ──────
        # Same rule as the MCP door: if there's no committed test, say so for FREE.
        rec = store.get(task_id)
        if rec is None:
            return _json(200, {
                "error": f"no committed acceptance test for task '{task_id}'. "
                         f"Commit one first (free). No payment was taken.",
                "error_code": "no_commitment", "charged": False,
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
                {"x402Version": 2, "accepts": [reqs], "error": settled.reason,
                 "error_code": "payment_not_honored", "charged": False},
                status_code=402,
                headers={"payment-required": challenge},
            )

        # ── PAID. Run the sandbox and return the verdict IN THE BODY. ───────
        # This is the whole point of the plain-HTTP door: synchronous settlement means the
        # deliverable comes back in this 200, not behind a session the buyer never opens.
        # HARD TIMEOUT. The sandbox has its own internal wall clock, but a hung HTTP call to
        # an isolated verifier, or a stuck thread, would otherwise leave the buyer waiting
        # forever on a request they have already paid for. Better a loud, explicit timeout the
        # client can act on than a socket that never closes.
        try:
            verdict = await asyncio.wait_for(
                verifier.verify(
                    revealed_source=rec.source,
                    revealed_nonce=rec.nonce,
                    commitment=rec.commitment,
                    deliverable=deliverable,
                ),
                timeout=VERIFY_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            log.error("verify timed out after %ss for task %s", VERIFY_TIMEOUT_S, task_id)
            # The buyer HAS PAID. Tell them exactly that, and that this is not their fault and
            # not a failed deliverable — it is our infrastructure. Never let a timeout read as
            # a verdict of "failed".
            return _json(503, {
                "task_id": task_id,
                "error": f"verification timed out after {VERIFY_TIMEOUT_S}s",
                "error_code": "verify_timeout",
                "charged": True,
                "passed": None,
                "advice": "this is a referee-side failure, not a failed deliverable; retry or "
                          "escalate — do not treat as a rejection",
            })

        receipt = None
        tx_hash = None
        try:
            if settled.receipt:
                receipt = fac.receipt_header(settled.receipt)
                tx_hash = settled.receipt.get("transaction") or settled.receipt.get("txHash")
        except Exception:
            pass

        # Record to the public verdict log (the explorer reads this). Best-effort.
        try:
            store.record_verdict(
                task_id=task_id, passed=verdict.passed, confidence=verdict.confidence,
                reason=verdict.reason, commitment=rec.commitment, tx_hash=tx_hash,
                amount=reqs.get("amount"), surface="http",
            )
        except Exception as e:
            log.error("verdict log write failed: %s: %s", type(e).__name__, e)

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


def _dig(obj, *keys, _depth: int = 0):
    """
    Find the first of `keys` anywhere in a nested JSON body.

    BE LIBERAL IN WHAT YOU ACCEPT. Different buyers wrap the same business arguments
    differently, and an OKX admin hit exactly this: the x402 replay may arrive as our flat
    {task_id, deliverable}, as a JSON-RPC envelope
    ({"params":{"name":...,"arguments":{task_id, deliverable}}}), or echoed inside an x402
    accepts/challenge structure. Rejecting a paid request because the arguments were one level
    deeper than expected is a terrible reason to fail — the buyer has already moved money.

    Bounded depth so a hostile or cyclic body cannot spin us.
    """
    if _depth > 6 or not isinstance(obj, dict):
        return None
    for k in keys:
        if k in obj and obj[k] not in (None, ""):
            return obj[k]
    for v in obj.values():
        if isinstance(v, dict):
            found = _dig(v, *keys, _depth=_depth + 1)
            if found is not None:
                return found
        elif isinstance(v, list):
            for item in v:
                found = _dig(item, *keys, _depth=_depth + 1)
                if found is not None:
                    return found
    return None


def _json(status: int, obj: dict) -> JSONResponse:
    return JSONResponse(obj, status_code=status)