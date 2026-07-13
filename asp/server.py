"""
server.py — MERITA. An A2MCP Agent Service Provider on OKX.AI.

WHAT THIS SELLS
───────────────
OKX.AI's A2A escrow says, in its own docs: "the provider is paid only after **the user signs
off**." On a platform whose entire pitch is "agents settle payments onchain — without human
handoff." That sign-off is a human, standing in the middle of an autonomous economy, clicking
approve. And the only escape hatch is arbitration, which costs a 5% deposit you forfeit if
you lose — so the rational move for a wronged provider is often to eat the loss. Both sides
know that. It corrodes the whole market.

Merita sells the missing piece: **an impartial referee that decides "is this done right?"**
so sign-off can be automatic. Commit-reveal acceptance tests, executed in a hard sandbox,
with tiered escalation and a signed verdict.

Note what OKX's own A2MCP guide says makes a good service:
    "Verifiable, low-risk results — deterministic return values."
That is not a description we contorted ourselves to fit. It is what a referee IS.

THREE TOOLS, and why each exists
────────────────────────────────
  commit_acceptance_test  (FREE)  — poster commits H(test‖nonce) BEFORE work begins.
  verify_deliverable      (PAID)  — reveal, judge, return a verdict. The product.
  assess_integrity        (PAID)  — collusion / wash-trade screen on a counterparty.

`commit` is free ON PURPOSE. Charging for it would put a toll booth in front of the one
step that makes the whole scheme honest, and posters would skip it. The security property
is worth more than the pennies. Charge for the judgement, not for the handshake.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_http_headers, get_http_request
from starlette.responses import JSONResponse

from .okx_x402 import Facilitator, Price
from .store import CommitStore
from .verify_client import VerifierClient

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("merita")

PUBLIC_URL = os.environ.get("MERITA_PUBLIC_URL", "https://merita.example/mcp")

mcp = FastMCP(
    name="Merita",
    instructions=(
        "Merita is an impartial verification referee for agent-to-agent work. Before hiring "
        "a worker, call commit_acceptance_test with the machine-checkable test you will judge "
        "the deliverable by — this is free, and it commits you to it. After the worker "
        "delivers, call verify_deliverable to get a signed pass/fail verdict you can settle "
        "escrow against. Use assess_integrity to screen a counterparty for wash-trading or "
        "collusion before you fund anything."
    ),
)

fac = Facilitator()
store = CommitStore()
verifier = VerifierClient()

# Priced in USDG atomic units (6dp). 0.02 USDG a verification.
#
# Priced LOW on purpose. A referee that costs a meaningful fraction of the bounty is a
# referee nobody calls on small jobs — and small, high-frequency jobs are where autonomous
# agent labour actually lives. The whole thesis dies if adjudication is expensive. X Layer's
# sub-cent gas + OKX's gas subsidy are what make a 2-cent service viable at all; on a chain
# with real fees this business does not exist.
PRICE_VERIFY = Price(atomic="20000")
PRICE_INTEGRITY = Price(atomic="10000")


# ─────────────────────────────────────────────────────────────────────────────
# The x402 gate. Every paid tool goes through this. There is no second path.
# ─────────────────────────────────────────────────────────────────────────────

class PaymentRequired(Exception):
    def __init__(self, header: str) -> None:
        self.header = header
        super().__init__("payment required")


async def _charge(price: Price, description: str) -> tuple[dict, str]:
    """
    Returns (requirements, x_payment) if the caller has paid. Raises PaymentRequired if not.

    Called BEFORE the work. Runs /verify only — the free signature check. Settlement happens
    after the work succeeds, in _settle(). Never merge these two.
    """
    if not fac.configured:
        raise RuntimeError("x402 not configured — refusing to serve a paid tool for free")

    reqs = fac.requirements(resource_url=PUBLIC_URL, description=description, price=price)
    headers = get_http_headers()
    x_payment = headers.get("x-payment")

    if not x_payment:
        raise PaymentRequired(fac.challenge_header(reqs))

    ok, reason = await fac.verify(x_payment, reqs)
    if not ok:
        log.warning("payment verify failed: %s", reason)
        raise PaymentRequired(fac.challenge_header(reqs))

    return reqs, x_payment


async def _settle(reqs: dict, x_payment: str) -> str | None:
    """Called AFTER the work succeeded. Returns the receipt, or None if settlement failed."""
    data = await fac.settle(x_payment, reqs)
    if not data:
        # We already did the work. We do NOT withhold the result to punish a failed
        # settlement — the buyer signed in good faith and the failure is between us and the
        # facilitator. Log it, eat it, investigate. Holding a verdict hostage over a few
        # cents on a platform with on-chain reputation is a catastrophic trade.
        log.error("settlement failed AFTER work completed — serving result anyway")
        return None
    return fac.receipt_header(data)


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 1 — commit (free)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool
async def commit_acceptance_test(
    task_id: str,
    acceptance_test: str,
    spec: str,
    ctx: Context,
) -> dict[str, Any]:
    """
    Commit to how you will judge a deliverable, BEFORE the worker starts. Free.

    Give me a Python function `def check(output) -> bool` and I return a commitment hash —
    H(test ‖ nonce). I keep the test sealed. Publish the hash to your counterparty (put it in
    the escrow memo) and the worker knows they are being judged by a fixed, pre-registered
    standard they cannot see.

    This cuts BOTH ways, which is the point:
      · The worker cannot reverse-engineer the checker and fake a pass. They must do the work.
      · YOU cannot change the test after seeing a deliverable you'd rather not pay for. At
        verification I recompute the hash; if it doesn't match what you committed to, I rule
        for the WORKER and your escrow releases. Goalpost-moving is not available to you.

    Args:
        task_id: your identifier for this job (the OKX.AI task/job id works well)
        acceptance_test: Python source defining `def check(output) -> bool`
        spec: the natural-language spec the worker is being held to
    """
    commitment = store.commit(task_id=task_id, source=acceptance_test, spec=spec)
    await ctx.info(f"committed test for {task_id}")
    return {
        "task_id": task_id,
        "commitment": commitment,
        "publish_this": commitment,
        "note": (
            "Give this hash to your counterparty before work begins. Neither side can move "
            "the goalposts once it is published."
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 2 — verify (paid). THE PRODUCT.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool
async def verify_deliverable(task_id: str, deliverable: dict, ctx: Context) -> dict[str, Any]:
    """
    Judge a deliverable against the acceptance test you committed to. Returns a verdict you
    can settle escrow against, with no human sign-off.

    The test runs in a hard sandbox: fresh interpreter, no filesystem it cares about, no
    network route out, CPU and memory ceilings, and a wall-clock kill. It runs TWICE — if the
    two runs disagree, I do NOT fail the worker. A nondeterministic test is MY problem, not
    theirs, and the job escalates rather than slashing someone who may have done it perfectly.

    Verdicts:
      passed=true                    → release escrow.
      passed=false                   → reject; the reason tells you why.
      reveal_valid=false             → the committed test doesn't match. Ruled for the worker.
      confidence=0.0                 → I couldn't decide deterministically. Escalate, don't punish.

    Args:
        task_id: the id you used in commit_acceptance_test
        deliverable: the worker's output, as JSON
    """
    reqs, x_payment = await _charge(PRICE_VERIFY, "Merita — verify a deliverable")

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

    receipt = await _settle(reqs, x_payment)

    return {
        "task_id": task_id,
        "passed": verdict.passed,
        "confidence": verdict.confidence,
        "reason": verdict.reason,
        "reveal_valid": verdict.reveal_valid,
        "commitment": rec.commitment,
        "tier": int(verdict.tier_used),
        "settle_escrow": verdict.passed,
        "payment_receipt": receipt,
    }


# ─────────────────────────────────────────────────────────────────────────────
# TOOL 3 — integrity (paid)
# ─────────────────────────────────────────────────────────────────────────────

@mcp.tool
async def assess_integrity(
    counterparty: str,
    my_address: str,
    spec: str,
    ctx: Context,
) -> dict[str, Any]:
    """
    Screen a counterparty for collusion, wash-trading, and sybil patterns before you fund
    escrow. Returns a 0-1 integrity score and specific flags.

    Four signals, combined by MINIMUM rather than average — one strong collusion signal must
    not be dilutable by three weak innocent ones:
      · pairing entropy      — does this agent serve many counterparties, or just one?
      · value-cycle          — does money paid to it come back to you? (that's a wash loop)
      · funding ancestry     — were both wallets funded by the same source? (one operator, two hats)
      · spec diversity       — is the same task being emitted over and over? (farming)

    Args:
        counterparty: the address you're considering transacting with
        my_address: your own address (needed for cycle + ancestry checks)
        spec: the task in question
    """
    _reqs, _x = await _charge(PRICE_INTEGRITY, "Merita — integrity screen")

    from merita.integrity.graph import IntegrityGraph  # noqa: PLC0415

    graph: IntegrityGraph = _GRAPH
    report = graph.assess(poster=my_address, worker=counterparty, spec=spec)
    await ctx.info(f"screened {counterparty[:10]}… → {report.score:.2f}")

    receipt = await _settle(_reqs, _x)
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


from merita.integrity.graph import IntegrityGraph  # noqa: E402

_GRAPH = IntegrityGraph()


# ─────────────────────────────────────────────────────────────────────────────
# HTTP plumbing: turn PaymentRequired into a real 402 with the right header.
# ─────────────────────────────────────────────────────────────────────────────

@mcp.custom_route("/health", methods=["GET"])
async def health(_request) -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "service": "merita",
        "x402_configured": fac.configured,
        "network": "eip155:196",
    })


if __name__ == "__main__":
    # Streamable HTTP — the transport OKX.AI's buyers speak. Not stdio: this must be a
    # public HTTPS endpoint tied to a domain, per the A2MCP requirements.
    mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
