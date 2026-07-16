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

# USDT. Not a preference — OKX's ASP listing accepts USDT only, and the fee we register with
# them MUST match the asset our 402 advertises. A listing that says "0.02 USDT" while the
# challenge asks buyers to sign a USDG authorization is a service nobody can pay, that looks
# perfectly healthy from the outside.
#
# The atomic amount is computed at request time against the token the FACILITATOR reports
# (see okx_x402.discover_token) — never against a hardcoded address or decimal count. Three
# OKX docs give three different USDT addresses on X Layer; we refuse to adjudicate and ask
# the settlement layer itself instead.
#
# Priced LOW on purpose. A referee costing a meaningful fraction of the bounty is a referee
# nobody calls on small jobs — and small, high-frequency jobs are where autonomous agent
# labour actually lives. The thesis dies if adjudication is expensive. X Layer's sub-cent gas
# plus OKX's gas subsidy are what make a 2-cent service viable at all.
PRICE_VERIFY = Price(human=0.02)     # 0.02 USDT
PRICE_INTEGRITY = Price(human=0.01)  # 0.01 USDT

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
    Return the payment receipt the paywall ALREADY captured. Does NOT settle again.

    This changed with the "paid three times, zero verdicts" fix. Settlement now happens ONCE,
    in the paywall, before the tool runs (see paywall.py settle_or_accept). By the time we are
    here, the money has moved and the receipt is sitting on the request scope. The old code
    called settle() a SECOND time here — which failed, because the authorization's nonce was
    already spent by the first settlement, and returned None, throwing away a receipt for a
    payment that had genuinely succeeded. One payment, settled once, receipt read once.
    """
    try:
        req = get_http_request()
    except Exception:
        return None

    pay = req.scope.get("merita_payment")
    if not pay:
        return None

    receipt = pay.get("receipt")
    if not receipt:
        return None
    try:
        return fac.receipt_header(receipt)
    except Exception:
        return None


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

SANDBOX_OK: bool | None = None


async def _startup() -> None:
    """Runs on uvicorn's loop, not a throwaway one."""
    global SANDBOX_OK

    if fac.configured:
        await fac.check_supported()
    else:
        log.error("x402 unconfigured — paid tools will refuse to serve")

    # SELF-TEST THE SANDBOX AT BOOT.
    #
    # A real payment settled on-chain before we discovered the sandbox was unreachable. The
    # fail-open rule saved us — "escalate, do not slash" rather than falsely failing an honest
    # worker — but the buyer still paid for a verdict we could not render. Once is a bug.
    # Twice would be a pattern, and on a marketplace with on-chain reputation, a pattern is
    # permanent.
    #
    # So: run a known-good check through the real sandbox at boot, and surface the result on
    # /health. If the sandbox is broken, we want to know while the service is starting — not
    # after someone's money has moved.
    from merita.models import AcceptanceTest  # noqa: PLC0415

    probe = AcceptanceTest(source="def check(o): return o.get('ok') is True")
    try:
        v = await verifier.verify(
            revealed_source=probe.source,
            revealed_nonce=probe.nonce,
            commitment=probe.commitment(),
            deliverable={"ok": True},
        )
        SANDBOX_OK = bool(v.passed and v.confidence == 1.0)
    except Exception as e:
        log.error("sandbox self-test threw: %s", e)
        SANDBOX_OK = False

    if SANDBOX_OK:
        log.info("sandbox: self-test PASSED")
    else:
        log.error(
            "SANDBOX SELF-TEST FAILED. verify_deliverable will take payment and return a "
            "non-verdict. DO NOT serve traffic in this state."
        )


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
        # Surfaced deliberately. If token_discovered is false we are using a FALLBACK address
        # that three OKX docs disagree about, and every payment may silently fail verification.
        # That must be visible on the healthcheck, not buried in a log line nobody reads.
        "settlement_token": fac.token.symbol,
        # This address is an ASSERTION, not a discovery — /supported carries no assets. It is
        # surfaced here so it can never quietly rot: verify it on the explorer, and if OKX
        # migrates USDT (they already have once, to USDT0), set MERITA_SETTLEMENT_ASSET.
        "settlement_asset": fac.token.address,
        "exact_scheme_supported": fac.supported,
        # If this is not true, the referee cannot referee. Everything else being green is
        # worse than useless — it means we will charge for verdicts we cannot render.
        "sandbox": SANDBOX_OK,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))

    # NOTE: the /supported probe used to run here via asyncio.run(). That was the bug.
    # asyncio.run() CLOSES its event loop on exit, and httpx clients are bound to the loop
    # they were created in — so the facilitator client came up dead and every settlement
    # failed with "Event loop is closed" while the service looked perfectly healthy.
    #
    # The probe now runs inside the app's own lifespan (see _startup below), on the same loop
    # uvicorn will serve from. Rule of thumb, learned the expensive way: never asyncio.run()
    # anything in a process that is about to hand a loop to a server.

    # Build the ASGI app, then WRAP it in the paywall. Order matters: the paywall must sit
    # OUTSIDE FastMCP so it can emit a real HTTP 402 before FastMCP ever parses the request.
    # Inside, the best it could do is a JSON-RPC error in a 200 — which no x402 client on
    # earth will recognise as a request for payment.
    app = mcp.http_app()

    # Probe the facilitator on the SERVER's loop, once it is up. Wrapping the existing
    # lifespan rather than replacing it: FastMCP initialises its session manager in there,
    # and dropping that would break every MCP call.
    _inner_lifespan = app.router.lifespan_context

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app_):
        async with _inner_lifespan(app_):
            await _startup()
            yield
            await fac.close()

    app.router.lifespan_context = _lifespan
    def _precheck(tool: str, args: dict) -> tuple[bool, str]:
        """Refuse to charge for a call that cannot produce a result.

        For verify_deliverable: if there is no committed test for this task_id, the verdict is
        a guaranteed 'no test' rejection. Charging 0.02 USDT for that is charging for nothing.
        Return the rejection for free instead. assess_integrity has no such precondition, so it
        passes straight through to payment.
        """
        if tool == "verify_deliverable":
            task_id = (args or {}).get("task_id")
            if not task_id:
                return False, "task_id is required"
            if store.get(task_id) is None:
                return False, (
                    f"no committed acceptance test for task '{task_id}'. Call "
                    f"commit_acceptance_test first (it's free). No payment was taken."
                )
        return True, ""

    app = X402Paywall(app, facilitator=fac, paid_tools=PAID_TOOLS, resource_url=PUBLIC_URL,
                      precheck=_precheck)

    import uvicorn
    log.info("merita up on :%d | x402=%s | tools=%s", port, fac.configured, list(PAID_TOOLS))
    uvicorn.run(app, host="0.0.0.0", port=port)