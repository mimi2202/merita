"""
server.py — MERITA. An A2MCP Agent Service Provider on OKX.AI.

WHAT THIS SELLS
───────────────
OKX.AI's A2A escrow says, in its own docs: "the provider is paid only after **the user signs
off**." On a platform whose pitch is "agents settle payments onchain — without human handoff."
That sign-off is a human standing in the middle of an autonomous economy, clicking approve.
The only escape hatch is arbitration, which costs a 5% deposit you forfeit if you lose — so a
wronged provider's rational move is often to eat the loss. Both sides know it. It corrodes
the market.

Merita sells the missing piece: an impartial referee that decides "is this done right?", so
sign-off can be automatic. Commit-reveal acceptance tests, executed in a hard sandbox, tiered
escalation, signed verdict.

OKX's own A2MCP guide says a good service has "verifiable, low-risk results — deterministic
return values." That is not a description we contorted ourselves to fit. It is what a referee
structurally IS.

THREE TOOLS
───────────
  commit_acceptance_test  FREE   — poster commits H(test‖nonce) BEFORE work begins
  verify_deliverable      PAID   — reveal, judge, return a settleable verdict. The product.
  assess_integrity        PAID   — collusion / wash-trade screen on a counterparty

`commit` is free deliberately. Charging for it would put a toll booth in front of the one
step that makes the whole scheme honest, and posters would skip it. Charge for the judgement,
not the handshake.

HOW PAYMENT ACTUALLY WORKS HERE — READ THIS BEFORE EDITING
──────────────────────────────────────────────────────────
The x402 402-response CANNOT be raised from inside a tool. Every MCP call is POST /mcp with a
JSON-RPC envelope, so a Python exception inside a tool becomes a JSON-RPC error object inside
an HTTP 200. The buyer's payment layer is watching for HTTP status 402 plus PAYMENT-REQUIRED
/ WWW-Authenticate headers — it will never see them, and your service is unbillable while
looking perfectly healthy.

The status code IS the protocol. So the gate lives in ASGI middleware (paywall.py), BELOW
FastMCP, where a real 402 can be emitted. The middleware peeks the JSON-RPC body to tell a
paid tool from a free one, runs the facilitator's /verify (free, reversible), and stashes the
verified payment on the ASGI scope. The tool then does the work and calls _settle() — which
moves money, and only after the work succeeded.

verify → work → settle. Never merge them. That ordering is why x402 has two calls.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.responses import JSONResponse

from merita.integrity.graph import IntegrityGraph

from .okx_x402 import Facilitator, Price
from .paywall import X402Paywall
from .store import CommitStore
from .verify_client import VerifierClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("merita")

PUBLIC_URL = os.environ.get("MERITA_PUBLIC_URL", "https://merita-asp.onrender.com/mcp")

fac = Facilitator()
store = CommitStore()          # raises at boot if DATABASE_URL is unset. That is intentional.
verifier = VerifierClient()
graph = IntegrityGraph()

# Priced in USDG atomic units (6dp). Two cents a verification.
#
# Priced LOW on purpose. A referee costing a meaningful fraction of the bounty is a referee
# nobody calls on small jobs — and small, high-frequency jobs are where autonomous agent
# labour actually lives. The thesis dies if adjudication is expensive. X Layer's sub-cent gas
# plus OKX's gas subsidy are what make a 2-cent service viable at all; on a chain with real
# fees this business does not exist.
PRICE_VERIFY = Price(atomic="20000")     # 0.02 USDG
PRICE_INTEGRITY = Price(atomic="10000")  # 0.01 USDG

PAID_TOOLS = {
    "verify_deliverable": (PRICE_VERIFY, "Merita — verify a deliverable against a committed test"),
    "assess_integrity": (PRICE_INTEGRITY, "Merita — collusion / wash-trade screen"),
    # commit_acceptance_test is absent → free. See above.
}

mcp = FastMCP(
    name="Merita",
    instructions=(
        "Merita is an impartial verification referee for agent-to-agent work. BEFORE hiring a "
        "worker, call commit_acceptance_test with the machine-checkable test you will judge the "
        "deliverable by — free, and it binds you to it. AFTER the worker delivers, call "
        "verify_deliverable for a signed pass/fail verdict you can settle escrow against with no "
        "human sign-off. Use assess_integrity to screen a counterparty for wash-trading or "
        "collusion before funding anything."
    ),
)


async def _settle() -> str | None:
    """
    Settle the payment the middleware already verified. Called AFTER the work succeeded.

    If settlement fails we STILL return the result. The buyer signed in good faith; the
    failure is between us and the facilitator. Withholding a verdict to punish a failed
    settlement, on a marketplace where reputation is literally on-chain, is a catastrophic
    trade for a few cents.
    """
    try:
        req = get_http_request()
    except Exception:
        return None

    pay = req.scope.get("merita_payment")
    if not pay:
        return None

    data = await fac.settle(pay["x_payment"], pay["reqs"])
    if not data:
        log.error("SETTLEMENT FAILED after work completed — serving result anyway")
        return None
    return fac.receipt_header(data)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1 — commit (free)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool
async def commit_acceptance_test(
    task_id: str, acceptance_test: str, spec: str, ctx: Context
) -> dict[str, Any]:
    """
    Commit to HOW you will judge a deliverable, before the worker starts. Free.

    Give me a Python function `def check(output) -> bool` and I return H(test ‖ nonce). I keep
    the test sealed. Publish the hash to your counterparty (the escrow memo is a good place)
    and the worker knows they are judged by a fixed, pre-registered standard they cannot read.

    This binds BOTH sides, which is the point:
      · The worker cannot reverse-engineer the checker and fake a pass. They must do the work.
      · YOU cannot swap the test after seeing a deliverable you'd rather not pay for. At
        verification I recompute the hash; if it does not match what you committed to, I rule
        for the WORKER and the escrow releases. Goalpost-moving is not available to you.

    Args:
        task_id: your id for this job (the OKX.AI task id works well)
        acceptance_test: Python source defining `def check(output) -> bool`
        spec: the natural-language spec the worker is held to
    """
    try:
        commitment = store.commit(task_id=task_id, source=acceptance_test, spec=spec)
    except ValueError as e:
        return {"error": str(e)}

    graph.observe_spec(task_id, spec)
    await ctx.info(f"sealed acceptance test for {task_id}")
    return {
        "task_id": task_id,
        "commitment": commitment,
        "note": (
            "Publish this hash to your counterparty before work begins. Neither side can move "
            "the goalposts once it is out."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2 — verify (paid). THE PRODUCT.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool
async def verify_deliverable(task_id: str, deliverable: dict, ctx: Context) -> dict[str, Any]:
    """
    Judge a deliverable against the acceptance test you committed to. Returns a verdict you can
    settle escrow against — no human sign-off.

    The test runs in a hard sandbox: fresh interpreter, dropped to an unprivileged uid, CPU and
    memory ceilings, a wall-clock kill, no writable filesystem, scrubbed environment. It runs
    TWICE — and if the two runs disagree I do NOT fail the worker. A nondeterministic test is MY
    problem, not theirs, and the job escalates rather than slashing someone who may well have
    done it perfectly.

    Reading the verdict:
      passed=true         → release escrow.
      passed=false        → reject; `reason` says why.
      reveal_valid=false  → the committed test does not match. Ruled FOR the worker. Release.
      confidence=0.0      → I could not decide deterministically. Escalate — do not punish.

    Args:
        task_id: the id you used in commit_acceptance_test
        deliverable: the worker's output, as JSON
    """
    rec = store.get(task_id)
    if not rec:
        return {
            "error": "no committed acceptance test for this task_id",
            "hint": "call commit_acceptance_test first — I will not judge against a test you never committed to",
        }

    await ctx.info(f"verifying {task_id} in sandbox")
    verdict = await verifier.verify(
        revealed_source=rec.source,
        revealed_nonce=rec.nonce,
        commitment=rec.commitment,
        deliverable=deliverable,
    )

    receipt = await _settle()

    return {
        "task_id": task_id,
        "passed": verdict.passed,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "reveal_valid": verdict.reveal_valid,
        "commitment": rec.commitment,
        "settle_escrow": verdict.passed,
        "payment_receipt": receipt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3 — integrity (paid)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool
async def assess_integrity(
    counterparty: str, my_address: str, spec: str, ctx: Context
) -> dict[str, Any]:
    """
    Screen a counterparty for collusion, wash-trading and sybil patterns BEFORE you fund escrow.

    Four signals, combined by MINIMUM rather than average — one strong collusion signal must not
    be dilutable by three weak innocent ones:
      · pairing entropy     — does this agent serve many counterparties, or only you?
      · value cycles        — does money paid to it come back to you? (that is a wash loop)
      · funding ancestry    — were both wallets funded from one source? (one operator, two hats)
      · spec diversity      — is the same task emitted over and over? (farming)

    Args:
        counterparty: the address you are considering transacting with
        my_address: your own address (needed for the cycle and ancestry checks)
        spec: the task in question
    """
    report = graph.assess(poster=my_address, worker=counterparty, spec=spec)
    await ctx.info(f"screened {counterparty[:10]}… → {report.score:.2f}")

    receipt = await _settle()
    return {
        "counterparty": counterparty,
        "integrity_score": round(report.score, 3),
        "signals": {k: round(v, 3) for k, v in report.signals.items()},
        "flags": report.flags,
        "recommendation": (
            "DO NOT FUND — this looks like collusion or a wash loop" if report.quarantined
            else "proceed with caution — some signals are weak" if not report.leaderboard_eligible
            else "clean"
        ),
        "payment_receipt": receipt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# HTTP
# ─────────────────────────────────────────────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health(_request) -> JSONResponse:
    """Render's health check AND the cron ping that stops the free tier sleeping.

    Reports x402 and DB status honestly. A green /health that hides an unconfigured paywall is
    how you discover, during OKX's review, that nobody can pay you.
    """
    return JSONResponse({
        "ok": True,
        "service": "merita",
        "network": "eip155:196",
        "x402_configured": fac.configured,
        "db": store.health(),
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))

    # Build the ASGI app, then WRAP it in the paywall. Order matters: the paywall must sit
    # OUTSIDE FastMCP so it can emit a real HTTP 402 before FastMCP ever parses the request.
    # Inside, the best it could do is a JSON-RPC error in a 200 — which no x402 client on
    # earth will recognise as a request for payment.
    app = mcp.http_app()
    app = X402Paywall(app, facilitator=fac, paid_tools=PAID_TOOLS, resource_url=PUBLIC_URL)

    import uvicorn
    log.info("merita up on :%d | x402=%s | tools=%s", port, fac.configured, list(PAID_TOOLS))
    uvicorn.run(app, host="0.0.0.0", port=port)