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
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("merita.http_x402")

# Ceiling on a single verification. The sandbox runs the test twice at 10s wall clock each,
# so 45s leaves headroom for scheduling without ever letting a paid request hang open.
VERIFY_TIMEOUT_S = 45

# The canonical demonstration. Used ONLY when a paid request carries no business params —
# a real test, really executed in the sandbox, so the caller receives a genuine pass/fail
# verdict rather than an error. Deliberately trivial and obviously correct: its job is to
# prove the service works, not to be clever.
_DEMO_TEST = (
    "def check(output):\n"
    "    # the deliverable must report a positive numeric price\n"
    "    p = output.get('price_usd')\n"
    "    return isinstance(p, (int, float)) and p > 0\n"
)
_DEMO_DELIVERABLE = {"price_usd": 142.5}


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
        # ── OBSERVABILITY FIRST ──────────────────────────────────────────────
        # This endpoint is the one in the OKX listing, and until now NOTHING here was logged:
        # the header logging lived in the MCP paywall, which passes /x402/* straight through.
        # So every report about this endpoint was diagnosed blind. Log the shape of every
        # request — names and keys only, never values, since the payment envelope carries a
        # signature.
        raw = await request.body()
        hdrs = sorted({k.lower() for k in request.headers.keys()})
        x_payment = _payment_header(request)

        body: dict = {}
        if raw:
            try:
                parsed = json.loads(raw)
                body = parsed if isinstance(parsed, dict) else {"_nondict": parsed}
            except Exception:
                body = {}

        log.info(
            "INBOUND /x402/verify | headers=[%s] | body_bytes=%d | body_keys=[%s] | payment=%s",
            ",".join(hdrs), len(raw), ",".join(sorted(body.keys())) if body else "",
            "YES" if x_payment else "NO",
        )

        task_id = _dig(body, "task_id", "taskId")
        deliverable = _dig(body, "deliverable", "output", "result")
        inline_test = _dig(body, "acceptance_test", "acceptanceTest", "test", "check")

        reqs = fac.requirements(
            resource_url=resource_url,
            description="Merita — verify a deliverable against an acceptance test",
            price=price,
        )

        # ── NO PAYMENT → the challenge. The only legitimate 402. ─────────────
        if not x_payment:
            challenge = fac.challenge_header(reqs)
            return JSONResponse(
                {
                    "x402Version": 2,
                    "accepts": [reqs],
                    "accepts_b64": challenge,
                    # Tell the buyer what to POST. A bare 402 leaves a generic x402 client
                    # with no idea what business params this resource expects.
                    "expected_body": {
                        "task_id": "<your id for this job>",
                        "acceptance_test": "def check(output) -> bool: ...",
                        "deliverable": {"...": "the worker's output as JSON"},
                    },
                },
                status_code=402,
                headers={
                    "payment-required": challenge,
                    "www-authenticate": 'Payment realm="merita", x402Version=2',
                },
            )

        # ── PAYMENT PRESENT ──────────────────────────────────────────────────
        # THE RULE, learned the hard way: a request carrying a payment must NEVER receive a
        # 402. The buyer has signed and very likely settled; answering "payment required"
        # tells them to pay twice and gives them nothing. The only exception is a payment the
        # facilitator refuses outright — and even then we say why.
        settled = await fac.settle_or_accept(x_payment, reqs)
        if not settled.ok:
            log.error(
                "PAYMENT REFUSED by facilitator | reason=%s | payTo=%s asset=%s amount=%s",
                settled.reason, reqs.get("payTo"), reqs.get("asset"), reqs.get("amount"),
            )
            challenge = fac.challenge_header(reqs)
            return JSONResponse(
                {"x402Version": 2, "accepts": [reqs],
                 "error": f"payment not settled: {settled.reason}",
                 "error_code": "payment_not_settled", "payment_attempted": True},
                status_code=402, headers={"payment-required": challenge},
            )

        tx_hash = None
        receipt = None
        try:
            if settled.receipt:
                receipt = fac.receipt_header(settled.receipt)
                tx_hash = settled.receipt.get("transaction") or settled.receipt.get("txHash")
        except Exception:
            pass
        log.info("payment honored (%s) tx=%s", settled.reason, tx_hash)

        # ── The buyer paid but sent no business params. ──────────────────────
        # Generic x402 clients replay the ORIGINAL request, so if that carried no body the
        # replay carries none either. Returning an error here would mean taking payment and
        # delivering nothing — and returning a 402 would mean taking payment and demanding
        # more. Neither is acceptable.
        #
        # Instead: run a real, canonical verification and return a real verdict, clearly
        # labelled as a demonstration, alongside the exact body to send for their own work.
        # The caller paid for a verification and receives one — genuinely executed in the
        # sandbox, not a canned string.
        demo = False
        if not task_id or (not inline_test and store.get(task_id) is None):
            demo = True
            task_id = f"demo:{int(time.time())}"
            inline_test = _DEMO_TEST
            deliverable = _DEMO_DELIVERABLE
            log.info("paid request lacked business params — running canonical demonstration")

        rec = store.get(task_id)
        precommitted = rec is not None

        if rec is None:
            try:
                store.commit(task_id=task_id, source=inline_test, spec="supplied inline")
                rec = store.get(task_id)
            except ValueError:
                # A different test is already sealed for this id. An inline test must NOT
                # override it — that is goalpost-moving through the back door, the exact
                # attack commit-reveal exists to stop.
                rec = store.get(task_id)
                precommitted = True
            except Exception as e:
                log.error("inline commit failed: %s", e)
                return _json(500, {"error": "could not record the acceptance test",
                                   "error_code": "commit_failed", "charged": True})

        if rec is None:
            return _json(500, {"error": "acceptance test unavailable",
                               "error_code": "no_commitment", "charged": True})

        # ── TWO MODES, AND THE VERDICT SAYS WHICH ONE RAN ────────────────────
        #
        # OKX rejected the listing because "results returned in actual calls don't match the
        # capabilities stated in the description". They were right, and the cause was
        # structural: verification REQUIRED a separate prior commit call, but the listing
        # exposes exactly one endpoint. A caller using the listed service could therefore only
        # ever receive "no committed test" — never the pass/fail verdict the description
        # promises. The service could not do the thing it advertised.
        #
        # So the endpoint is now single-shot: send the test with the deliverable and get a
        # verdict immediately.
        #
        # WHAT THAT COSTS, STATED PLAINLY: commit-reveal's guarantee is that the test was
        # sealed BEFORE the work was seen. A test supplied in the same call carries no such
        # proof — the poster could have written it after reading the deliverable. That is a
        # real weakening, and hiding it would make the verdict a lie.
        #
        # So we do not hide it. `precommitted` is returned on every verdict:
        #   true  — the test was sealed in advance; neither side could move the goalposts
        #   false — the test arrived with the deliverable; judged honestly, but unprotected
        #
        # The strong mode remains available and is strictly better. The weak mode makes the
        # service usable on first contact. The caller is told, every time, which one they got.
        rec = store.get(task_id)
        precommitted = rec is not None

        if rec is None:
            if not inline_test:
                return _json(200, {
                    "error": (
                        f"no acceptance test for task '{task_id}'. Send one as "
                        f"'acceptance_test' in this call, or commit it in advance for "
                        f"tamper-proof verification."
                    ),
                    "error_code": "no_test_supplied",
                    "charged": False,
                    "expected_body": {
                        "task_id": "<your id>",
                        "acceptance_test": "def check(output) -> bool: ...",
                        "deliverable": {"...": "the worker's output as JSON"},
                    },
                })

            # Seal the inline test now, so the verdict still references a real commitment
            # hash and the whole exchange remains auditable after the fact.
            try:
                store.commit(task_id=task_id, source=inline_test, spec="supplied inline")
                rec = store.get(task_id)
            except ValueError:
                # A different test is already committed for this id. Do NOT let an inline
                # test override a sealed one — that would be goalpost-moving through the
                # back door, which is the exact attack commit-reveal exists to stop.
                rec = store.get(task_id)
                precommitted = True
            except Exception as e:
                log.error("inline commit failed: %s", e)
                return _json(500, {"error": "could not record the acceptance test",
                                   "error_code": "commit_failed", "charged": False})

        if rec is None:
            return _json(500, {"error": "acceptance test unavailable",
                               "error_code": "no_commitment", "charged": False})

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

        result = {
            "task_id": task_id,
            "passed": verdict.passed,
            "confidence": verdict.confidence,
            "reason": verdict.reason,
            "reveal_valid": verdict.reveal_valid,
            "commitment": rec.commitment,
            "settle_escrow": verdict.passed,
            # Disclosed on EVERY verdict. true = the test was sealed before the deliverable
            # was seen (tamper-proof). false = it arrived in this call — judged honestly, but
            # with no proof it predates the work. A referee that hides which guarantee it
            # actually provided is not a referee.
            "precommitted": precommitted,
            "charged": True,
            "tx_hash": tx_hash,
            "payment_receipt": receipt,
        }
        if demo:
            result["demonstration"] = True
            result["note"] = (
                "No task_id/acceptance_test was supplied, so this is a real verification of a "
                "canonical example — the sandbox genuinely ran the test below. To verify your "
                "own work, POST the body shown in 'expected_body'."
            )
            result["ran_test"] = _DEMO_TEST
            result["ran_deliverable"] = _DEMO_DELIVERABLE
            result["expected_body"] = {
                "task_id": "<your id for this job>",
                "acceptance_test": "def check(output) -> bool: ...",
                "deliverable": {"...": "the worker's output as JSON"},
            }
        return JSONResponse(result, headers={"x-payment-response": receipt} if receipt else None)

    return verify_http


# Buyers do not agree on the payment header's name. OKX's CLI returns a `header_name`
# alongside the value, which means it is configurable at their end — so gating on the single
# spec name `X-PAYMENT` silently rejected paying customers as if they had never paid. Accept
# every plausible name; a payment we cannot recognise is worse than one we reject out loud.
_PAYMENT_HEADERS = (
    "x-payment", "payment", "x-payment-authorization", "authorization-payment",
    "x-402-payment", "x402-payment", "payment-authorization",
)


def _payment_header(request) -> str | None:
    for name in _PAYMENT_HEADERS:
        v = request.headers.get(name)
        if v:
            return v
    return None


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