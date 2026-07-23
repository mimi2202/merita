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

import hmac
import logging
import os
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_request
from starlette.responses import JSONResponse

from merita.integrity.graph import IntegrityGraph

from .http_x402 import make_x402_routes
from .okx_x402 import Facilitator, Price
from .paywall import X402Paywall
from .store import CommitStore
from .verify_client import VerifierClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("merita")

PUBLIC_URL = os.environ.get("MERITA_PUBLIC_URL", "https://merita-asp.onrender.com/mcp")

# BUILD MARKER. Bump this on every deploy that changes request handling.
#
# Half this project's debugging time was spent unable to answer "is my fix actually live?".
# A response shape can look right while the code behind it is three commits old. /health now
# reports this, so one curl settles it — no more inferring deployment state from behaviour.
BUILD = "2026-07-23.never-402-a-payment"

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

    # Record to the public verdict log for the explorer. Best-effort; never blocks the verdict.
    try:
        req = get_http_request()
        pay = req.scope.get("merita_payment", {})
        rc = pay.get("receipt") or {}
        store.record_verdict(
            task_id=task_id, passed=verdict.passed, confidence=verdict.confidence,
            reason=verdict.reason, commitment=rec.commitment,
            tx_hash=rc.get("transaction") or rc.get("txHash"),
            amount=(pay.get("reqs") or {}).get("amount"), surface="mcp",
        )
    except Exception as e:
        log.error("verdict log write failed: %s: %s", type(e).__name__, e)

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


@mcp.custom_route("/public/commit", methods=["POST", "OPTIONS"])
async def public_commit(request):
    """
    Free, browser-callable commit endpoint — the interactive demo of commit-reveal.

    Committing is free everywhere in Merita (charging for the handshake would make posters
    skip the step that makes the market honest), so exposing it to a browser costs nothing and
    demonstrates the core idea: seal a test, then discover you cannot change it.

    NAMESPACED, DELIBERATELY. Every public task_id is prefixed 'public:' server-side. Without
    that, a stranger could commit to 'sol-4' from a browser and permanently block a paying
    customer from using that id — a free denial-of-service on real business, delivered through
    a demo widget. Public writes never touch the namespace real buyers use.
    """
    cors = {
        "access-control-allow-origin": "*",
        "access-control-allow-headers": "content-type",
        "access-control-allow-methods": "POST, OPTIONS",
    }
    if request.method == "OPTIONS":
        return JSONResponse({}, headers=cors)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400, headers=cors)

    task_id = str(body.get("task_id") or "").strip()
    source = str(body.get("acceptance_test") or "")
    spec = str(body.get("spec") or "")

    if not task_id or not source:
        return JSONResponse(
            {"error": "task_id and acceptance_test are required"}, status_code=400, headers=cors
        )

    # Bounded input. A demo endpoint is still a public write path.
    if len(source) > 4000 or len(spec) > 2000 or len(task_id) > 80:
        return JSONResponse({"error": "input too large"}, status_code=413, headers=cors)

    namespaced = f"public:{task_id}"
    try:
        commitment = store.commit(task_id=namespaced, source=source, spec=spec)
    except ValueError as e:
        # THE DEMO'S PUNCHLINE: this task already has a different committed test, and it
        # cannot be replaced. Returned as a 409, not a 500 — it is the system working.
        return JSONResponse(
            {"error": str(e), "refused": True, "reason": "commitment_immutable"},
            status_code=409, headers=cors,
        )
    except Exception as e:
        log.error("public commit failed: %s", e)
        return JSONResponse({"error": "commit failed"}, status_code=500, headers=cors)

    return JSONResponse(
        {"task_id": task_id, "commitment": commitment, "refused": False}, headers=cors
    )


@mcp.custom_route("/public/verify", methods=["POST", "OPTIONS"])
async def public_verify(request):
    """
    Free verification — DEMO NAMESPACE ONLY. Powers the browser protocol inspector.

    THE OBVIOUS OBJECTION: does a free verify endpoint undercut the paid one? It would, if it
    could judge anything. It can't. It only serves task_ids that were committed through
    /public/commit, which the server namespaces to 'public:*'. Real buyers commit through the
    paid MCP tool into the unprefixed namespace, and those ids are unreachable here.

    So the two paths cannot touch: a stranger can play with a sandbox test they wrote
    themselves, and cannot verify a single piece of real work without paying. The demo gets to
    show the whole loop; the business keeps its revenue. If the namespaces ever merged, this
    endpoint would become a free bypass — which is exactly why the prefix is applied
    server-side and never accepted from the client.
    """
    cors = {"access-control-allow-origin": "*"}
    if request.method == "OPTIONS":
        return JSONResponse({}, headers=cors)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400, headers=cors)

    task_id = str(body.get("task_id") or "").strip()
    if not task_id:
        return JSONResponse({"error": "task_id required"}, status_code=400, headers=cors)

    # Namespace is applied HERE, server-side. Never taken from the client — a client that
    # could choose its own prefix could reach real commitments and verify them for free.
    namespaced = f"public:{task_id}"
    rec = store.get(namespaced)
    if rec is None:
        return JSONResponse(
            {"error": f"no sealed test for demo task '{task_id}' — seal one first",
             "error_code": "no_commitment"},
            status_code=404, headers=cors,
        )

    verdict = await verifier.verify(
        revealed_source=rec.source,
        revealed_nonce=rec.nonce,
        commitment=rec.commitment,
        deliverable=body.get("deliverable"),
    )

    try:
        store.record_verdict(
            task_id=namespaced, passed=verdict.passed, confidence=verdict.confidence,
            reason=verdict.reason, commitment=rec.commitment, tx_hash=None,
            amount=None, surface="demo",
        )
    except Exception as e:
        log.error("verdict log write failed: %s: %s", type(e).__name__, e)

    return JSONResponse({
        "task_id": task_id,
        "commitment": rec.commitment,
        "passed": verdict.passed,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "reveal_valid": verdict.reveal_valid,
        "demo": True,
    }, headers=cors)


@mcp.custom_route("/internal/verify", methods=["POST"])
async def internal_verify(request):
    """
    Verdict endpoint for Merita's OWN A2A delivery worker. Token-gated, not payment-gated.

    WHY THIS EXISTS: the A2A worker (asp/a2a_worker.py) runs locally, holds the wallet, and
    delivers task results on-chain. It needs a verdict. Making it pay the public x402 price
    would mean Merita paying Merita — economically meaningless, and on a marketplace that
    disqualifies self-dealing, actively harmful: our own integrity graph would flag the
    resulting wallet-to-wallet loop. So the worker authenticates as us instead of paying as
    a stranger.

    FAILS CLOSED. If MERITA_WORKER_TOKEN is unset, this endpoint refuses every request. An
    un-gated free verification endpoint sitting next to a paid one is a bypass, and the kind
    that gets found. No token, no service — never a default-open.

    It grants no extra power: it still requires a committed acceptance test, and it runs the
    identical sandbox the paid path uses. It skips the paywall, not the referee.
    """
    token = os.environ.get("MERITA_WORKER_TOKEN", "")
    if not token:
        return JSONResponse(
            {"error": "internal endpoint disabled (MERITA_WORKER_TOKEN unset)"}, status_code=503
        )

    supplied = request.headers.get("x-worker-token", "")
    # Constant-time compare: a plain == leaks token bytes through timing to anyone patient.
    if not hmac.compare_digest(supplied, token):
        log.warning("internal/verify: bad worker token from %s", request.client)
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    task_id = body.get("task_id")
    if not task_id:
        return JSONResponse({"error": "task_id required"}, status_code=400)

    rec = store.get(task_id)
    if rec is None:
        return JSONResponse(
            {"error": f"no committed acceptance test for task '{task_id}'", "verdict": None},
            status_code=404,
        )

    verdict = await verifier.verify(
        revealed_source=rec.source,
        revealed_nonce=rec.nonce,
        commitment=rec.commitment,
        deliverable=body.get("deliverable"),
    )

    try:
        store.record_verdict(
            task_id=task_id, passed=verdict.passed, confidence=verdict.confidence,
            reason=verdict.reason, commitment=rec.commitment, tx_hash=None,
            amount=None, surface="a2a",
        )
    except Exception as e:
        # LOG, never swallow. A bare `except: pass` here once hid an AttributeError from a
        # half-deployed store module: verdicts silently stopped being recorded, /feed 500'd,
        # and nothing said why. Best-effort must still be loud-effort.
        log.error("verdict log write failed: %s: %s", type(e).__name__, e)

    return JSONResponse({
        "task_id": task_id,
        "commitment": rec.commitment,
        "passed": verdict.passed,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "reveal_valid": verdict.reveal_valid,
    })


@mcp.custom_route("/feed", methods=["GET"])
async def feed(_request) -> JSONResponse:
    """Public, read-only verdict feed. The explorer polls this. Contains NO secrets —
    no test source, no nonce, no keys — only the public record of what was judged and the
    on-chain tx that paid for it. CORS-open because it's meant to be read from a browser."""
    try:
        return JSONResponse(
            {"verdicts": store.feed(50), "stats": store.stats()},
            headers={"access-control-allow-origin": "*"},
        )
    except AttributeError as e:
        # The classic half-deploy: server.py is new, store.py is old. Say so explicitly
        # instead of returning an opaque 500 that could mean anything.
        log.error("/feed unavailable — store module is out of date: %s", e)
        return JSONResponse(
            {"verdicts": [], "stats": {"total": 0, "passed": 0, "settled": 0},
             "error": "verdict store not migrated — deploy the current asp/store.py"},
            status_code=503, headers={"access-control-allow-origin": "*"},
        )
    except Exception as e:
        log.exception("/feed failed")
        return JSONResponse(
            {"verdicts": [], "stats": {"total": 0, "passed": 0, "settled": 0},
             "error": f"{type(e).__name__}: {e}"},
            status_code=500, headers={"access-control-allow-origin": "*"},
        )


@mcp.custom_route("/x402/verify", methods=["POST"])
async def x402_verify(request):
    """Plain-HTTP x402 door for the OKX buyer CLI (task-402-pay).

    The MCP door (/mcp) requires a session + text/event-stream, which the plain-HTTP buyer
    does not speak. This route takes a plain JSON POST, honors the settled payment, and
    returns the verdict IN THE BODY — synchronous settlement, per OKX's own x402 docs. Same
    store, sandbox, and facilitator as /mcp; only the entrance differs.
    """
    handler = make_x402_routes(
        fac=fac, store=store, verifier=verifier,
        price=PRICE_VERIFY, resource_url=PUBLIC_URL.replace("/mcp", "/x402/verify"),
    )
    return await handler(request)


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
        "build": BUILD,
        # Which header names this build will accept a payment under. If a buyer's header is
        # not in this list, its payment is invisible to us and it will be re-challenged.
        "accepted_payment_headers": [
            "x-payment", "payment", "x-payment-authorization", "authorization-payment",
            "x-402-payment", "x402-payment", "payment-authorization",
        ],
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

    # CORS, OUTERMOST. A browser sends an OPTIONS preflight before any POST carrying
    # Content-Type: application/json, and that preflight must be answered with
    # Access-Control-Allow-* headers before the request reaches a route at all.
    #
    # I originally hand-rolled OPTIONS inside /public/commit. That is fragile: the preflight
    # has to survive every layer above the route, and one early return without the headers
    # kills it — which is exactly what happened. Real middleware handles the whole preflight
    # dance once, for every route, and cannot be bypassed by a branch someone adds later.
    #
    # allow_origins=["*"] is right here. Every browser-reachable endpoint is either public
    # read-only (/feed) or free and namespaced (/public/commit), and none accept credentials.
    # The paid and internal paths are gated by payment and by token — not by who is asking.
    from starlette.middleware.cors import CORSMiddleware  # noqa: PLC0415

    app = CORSMiddleware(
        app,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        max_age=3600,
    )

    import uvicorn
    log.info("merita up on :%d | x402=%s | tools=%s", port, fac.configured, list(PAID_TOOLS))
    uvicorn.run(app, host="0.0.0.0", port=port)