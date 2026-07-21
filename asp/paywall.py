"""
paywall.py — the x402 gate.

THE PROBLEM NOBODY WARNS YOU ABOUT
──────────────────────────────────
x402 is an HTTP protocol: an unpaid request gets `402 Payment Required` with a
`PAYMENT-REQUIRED` header, the buyer signs, replays with `X-PAYMENT`, and gets the resource.
Clean — when each resource is its own URL.

MCP is not like that. **Every** tool call is `POST /mcp` with a JSON-RPC envelope:

    {"method": "tools/call", "params": {"name": "verify_deliverable", "arguments": {...}}}

So an HTTP middleware sees one URL and cannot tell a free `tools/list` from a paid
`verify_deliverable`. And a paywall that cannot tell them apart is useless in both
directions: gate the whole endpoint and buyers can't even *discover* your tools (MCP
handshake dies, OKX's review fails you); gate nothing and you work for free.

The only correct place to make the decision is *inside the envelope*. So this middleware
peeks at the JSON-RPC body, and only then decides.

WHY THIS IS MIDDLEWARE AND NOT A CHECK INSIDE THE TOOL
──────────────────────────────────────────────────────
A `raise PaymentRequired` inside a FastMCP tool becomes a JSON-RPC *error object* inside a
`200 OK`. That is not x402. The buyer's payment layer is watching for an HTTP 402 status and
a PAYMENT-REQUIRED header — OKX's own agent-payments skill checks exactly those, in that
order — and it will never see them. You'd have written a paywall that no buyer can pay.

The status code IS the protocol. It has to be emitted at the ASGI layer, before FastMCP ever
sees the request. Hence: read the body, decide, and either short-circuit with a real 402 or
replay the body downstream untouched.

That last part is the bit that bites: ASGI request bodies are a one-shot stream. Read it to
inspect it and it is GONE — FastMCP receives an empty body and every call fails with a
baffling parse error. So we buffer it and hand a fresh `receive` callable downstream. Six
lines, and an afternoon of confusion if you skip them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger(__name__)

# Tool name -> (price, human description). Anything not in here is FREE.
# `commit_acceptance_test` is deliberately absent: charging for the commit step would put a
# toll booth in front of the one action that makes the market honest, and posters would skip
# it. Charge for the judgement, not the handshake.
PaidTools = dict[str, tuple[Any, str]]


class X402Paywall:
    def __init__(self, app: ASGIApp, *, facilitator, paid_tools: PaidTools, resource_url: str,
                 precheck=None) -> None:
        self.app = app
        self.fac = facilitator
        self.paid = paid_tools
        self.resource_url = resource_url
        # Optional (tool_name, arguments) -> (ok: bool, reason: str). Runs BEFORE payment.
        #
        # WHY THIS EXISTS: charging for a call that CANNOT succeed is theft, however small.
        # verify_deliverable for a task_id that was never committed is a guaranteed "no test"
        # rejection — and the old flow charged 0.02 USDT for it anyway. A referee that bills
        # you to tell you it can't judge is not a referee anyone calls twice.
        #
        # So the paywall asks this hook "will this call actually produce a result?" before it
        # asks for money. If not, it returns the reason for FREE — no 402, no charge. The hook
        # is injected rather than hard-coded so the paywall stays business-logic-agnostic:
        # it knows how to take payment, not what Merita sells.
        self._precheck = precheck

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # The plain-HTTP x402 door (/x402/*) handles its OWN payment lifecycle end-to-end
        # (see http_x402.py). The MCP paywall must not touch it, or it would double-gate the
        # request — issue a 402 for a call that already carries its own settled payment. Two
        # doors, two independent payment paths, one brain.
        path = scope.get("path", "")
        if path.startswith("/x402/"):
            return await self.app(scope, receive, send)

        # ── x402 DISCOVERY PROBE — must be answered BEFORE FastMCP sees the request ──
        #
        # THE BUG (reported by an OKX admin): the buyer's x402 tooling probes /mcp to ask
        # "are you an x402 service?" — WITHOUT MCP's required Accept header. FastMCP runs its
        # own content-negotiation first and answers 406 Not Acceptable. The probe never
        # reaches the code that would issue the 402, so the buyer concludes we are not a valid
        # x402 service. From their tooling, we simply are not one.
        #
        # The root cause: the x402 challenge was gated behind MCP's Accept negotiation. x402
        # is an HTTP-level protocol and its challenge must live ABOVE the MCP layer, not
        # inside it. A payment protocol that only works if you already speak the app protocol
        # is not a payment protocol — it's a private handshake.
        #
        # So: if a request looks like an x402 probe (not an MCP call), answer it here with a
        # real 402 + accepts[], and never let FastMCP's Accept check run. We advertise the
        # verify tool's price — it is the canonical paid resource. A probe is any request that
        # is NOT a well-formed MCP POST: a GET, or a POST whose body is not JSON-RPC.
        if self._looks_like_x402_probe(scope):
            price, desc = self.paid.get("verify_deliverable", (next(iter(self.paid.values()))))
            reqs = self.fac.requirements(resource_url=self.resource_url, description=desc, price=price)
            return await _402(send, self.fac.challenge_header(reqs), reqs)

        if scope["method"] != "POST":
            return await self.app(scope, receive, send)

        body = await _drain(receive)

        # ── THE TRANSPORT DISCRIMINATOR ──────────────────────────────────────
        #
        # Route by the ACCEPT HEADER, not by body shape. This is the fix for a 406 an OKX
        # admin hit on every call:
        #
        #   "Client must accept both application/json and text/event-stream"
        #
        # MCP's streamable-HTTP transport REQUIRES a client to accept text/event-stream.
        # OKX's x402 buyer (task-402-pay) sends `Accept: application/json` only — so FastMCP
        # rejected it at the transport layer before any of our payment logic ran. The payment
        # was signed and never settled, and no verdict was ever produced.
        #
        # My previous fix discriminated on the BODY (JSON-RPC vs not), which missed this
        # entirely: the buyer sends a perfectly valid JSON-RPC body. It sailed past the check
        # and straight into the 406.
        #
        # The header is the honest signal. A client that cannot accept text/event-stream is
        # NOT an MCP streamable-HTTP client, whatever its body looks like, and must never be
        # handed to FastMCP. Route it to the plain-HTTP x402 door, which answers stateless
        # JSON — no session, no SSE.
        _p = scope.get("path", "").rstrip("/")
        _is_mcp = _p.endswith("/mcp") or _p == "/mcp"

        if _is_mcp and not _accepts_sse(scope):
            if _header(scope, b"x-payment"):
                # Paid replay from a plain-HTTP buyer -> serve the verdict as stateless JSON.
                rewritten = dict(scope)
                rewritten["path"] = "/x402/verify"
                rewritten["raw_path"] = b"/x402/verify"
                log.info("plain-HTTP buyer (no SSE) with payment -> /x402/verify")
                return await self.app(rewritten, _replay(body, receive), send)

            # No payment yet -> the x402 challenge. Also stateless JSON.
            price, desc = self.paid.get("verify_deliverable", (next(iter(self.paid.values()))))
            reqs = self.fac.requirements(resource_url=self.resource_url, description=desc, price=price)
            log.info("plain-HTTP buyer (no SSE), unpaid -> 402 challenge")
            return await _402(send, self.fac.challenge_header(reqs), reqs)

        # Everything below here is a real MCP client (it accepts text/event-stream).
        if _is_mcp and _tool_name(body) is None and not _is_mcp_rpc(body):
            price, desc = self.paid.get("verify_deliverable", (next(iter(self.paid.values()))))
            reqs = self.fac.requirements(resource_url=self.resource_url, description=desc, price=price)
            return await _402(send, self.fac.challenge_header(reqs), reqs)

        tool = _tool_name(body)
        if tool is None or tool not in self.paid:
            # tools/list, initialize, ping, and every free tool sail straight through.
            # This is what keeps MCP discovery working — and discovery has to work, or OKX
            # cannot review the listing and the submission is invalid.
            return await self.app(scope, _replay(body, receive), send)

        price, description = self.paid[tool]

        if not self.fac.configured:
            log.error("paid tool %s called but x402 is unconfigured — refusing", tool)
            return await _json(send, 503, {"error": "payment not configured"})

        # ── PRE-PAYMENT GATE: never charge for a call that cannot produce a result. ──
        # Runs before the 402. If verify_deliverable names a task with no committed test, the
        # answer is a free, honest rejection — not a paid non-verdict. This is the
        # double-charge-on-rejection fix.
        if self._precheck is not None:
            args = _tool_args(body)
            try:
                ok, reason = self._precheck(tool, args)
            except Exception as e:
                # A broken precheck must NOT block a legitimate paid call. Fail open: if we
                # can't determine that the call is doomed, let it proceed to payment.
                log.error("precheck raised (%s) — allowing call through to payment", e)
                ok, reason = True, ""
            if not ok:
                log.info("free-rejecting %s before payment: %s", tool, reason)
                return await _json(send, 200, {
                    "jsonrpc": "2.0",
                    "id": _rpc_id(body),
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps({
                                "error": reason,
                                "charged": False,
                                "hint": "no payment was taken — resolve the above and retry",
                            }),
                        }],
                    },
                })

        reqs = self.fac.requirements(
            resource_url=self.resource_url, description=description, price=price
        )
        x_payment = _header(scope, b"x-payment")

        if not x_payment:
            return await _402(send, self.fac.challenge_header(reqs), reqs)

        # ── HONOR THE PAYMENT — the permanent fix ────────────────────────────
        #
        # THE BUG (reported by an OKX admin, after paying THREE times for ZERO verdicts):
        # the old flow was verify() -> run tool -> settle(). It assumed WE control settlement
        # timing. We do not. OKX's buyer tooling SETTLES the payment on-chain first (tx
        # 0x657e3c...), THEN replays with X-PAYMENT. By then the EIP-3009 authorization is
        # spent — its nonce is burned on-chain — so our verify() call returned "invalid", we
        # re-issued a 402, and the buyer was told to pay for something they had already paid
        # for. Forever. Every settled payment looked to us like a fresh non-payment.
        #
        # The root confusion: verify() checks an UNSPENT authorization; it necessarily FAILS
        # for an already-settled one. Using verify() as the gate makes "already paid" and
        # "never paid" indistinguishable — and resolves both to "pay again".
        #
        # THE FIX: gate on SETTLEMENT, not on an unspent authorization. Call settle(), and
        # treat "already settled" as SUCCESS. This is correct for BOTH buyer behaviours:
        #   · buyer settled first (admin's tooling): settle() reports already-done -> proceed
        #   · buyer expects us to settle:            settle() does it now          -> proceed
        # Either way, a real on-chain payment for our terms results in the work being done and
        # the verdict returned in the 200 body. There is no path left where a paid buyer is
        # re-challenged.
        settled = await self.fac.settle_or_accept(x_payment, reqs)
        if not settled.ok:
            # SAY WHY. A bare 402 after a valid signed authorization tells the buyer "pay
            # again", which is both wrong and useless — they already paid, and now neither
            # side knows what broke. Every report we got back said only "returned 402", so
            # every diagnosis was a guess.
            #
            # The facilitator's reason goes in the body. The buyer's tooling surfaces it, the
            # next bug report contains the actual cause, and nobody has to infer anything.
            log.warning("x402 payment NOT honored for %s: %s | payTo=%s asset=%s amount=%s",
                        tool, settled.reason, reqs.get("payTo"), reqs.get("asset"),
                        reqs.get("amount"))
            return await _402(
                send, self.fac.challenge_header(reqs), reqs,
                error=f"payment not settled: {settled.reason}",
            )

        # Paid and settled. Stash the receipt so the tool can return it in the 200 body.
        # NOTHING further to settle — settlement is now behind us, by design.
        scope["merita_payment"] = {
            "reqs": reqs,
            "x_payment": x_payment,
            "receipt": settled.receipt,
            "already_settled": True,
        }
        await self.app(scope, _replay(body, receive), send)


    def _looks_like_x402_probe(self, scope: Scope) -> bool:
        """
        A GET to the MCP RESOURCE PATH is an x402 discovery probe. A GET to anything else —
        /health above all — is not, and must pass through untouched.

        THE BUG THIS GUARD FIXES, WHICH I CAUSED AND CAUGHT IN ONE DEPLOY:
        the first version treated EVERY GET as a probe. Render health-checks /health with a
        GET. So /health started returning 402, Render concluded the service was unhealthy, and
        the deploy hung forever — the new code could never go live because the platform's
        liveness gate was answering "payment required" to the platform itself. A paywall that
        bills the landlord does not get to stay in the building.

        So: probes are scoped to the MCP path only. Everything else is none of the paywall's
        business.
        """
        path = scope.get("path", "")
        is_mcp_path = path.rstrip("/").endswith("/mcp") or path.rstrip("/") == "/mcp"
        if not is_mcp_path:
            return False

        # A request carrying a payment is NEVER a probe — it is a replay collecting a result.
        # Without this, a buyer who signals x402 in its headers AND pays would be re-challenged
        # forever, having already settled on-chain.
        for k, v in scope.get("headers", []):
            if k.lower() == b"x-payment" and v:
                return False

        if scope["method"] == "GET":
            return True
        for k, v in scope.get("headers", []):
            lk = k.lower()
            if lk in (b"x-402", b"x402-version") or (lk == b"accept" and b"x402" in v.lower()):
                return True
        return False


# ── ASGI plumbing ────────────────────────────────────────────────────────────

def _accepts_sse(scope: Scope) -> bool:
    """True if the client accepts text/event-stream — i.e. it is a real MCP streamable-HTTP
    client. MCP mandates it; OKX's plain x402 buyer does not send it. A missing Accept header
    is treated as NOT accepting SSE, which is the safe read: a client that did not ask for a
    stream should not be handed one."""
    for k, v in scope.get("headers", []):
        if k.lower() == b"accept":
            return b"text/event-stream" in v.lower()
    return False


def _is_mcp_rpc(body: bytes) -> bool:
    """True if the body is a JSON-RPC envelope (any method). Distinguishes a real MCP call
    from an x402 probe that happens to POST. If it is not JSON-RPC, it is not for FastMCP."""
    try:
        j = json.loads(body)
    except Exception:
        return False
    return isinstance(j, dict) and j.get("jsonrpc") == "2.0" and "method" in j


async def _drain(receive: Receive) -> bytes:
    """Read the whole body. It is a stream; once read it is gone, hence _replay()."""
    chunks: list[bytes] = []
    while True:
        msg = await receive()
        if msg["type"] != "http.request":
            break
        chunks.append(msg.get("body", b""))
        if not msg.get("more_body", False):
            break
    return b"".join(chunks)


def _replay(body: bytes, original: Receive) -> Receive:
    """
    Hand the buffered body downstream as if it were never read — then GET OUT OF THE WAY.

    THE BUG THIS FIXES COST US HALF A DAY, AND IT IS SUBTLE ENOUGH TO DESERVE THE ESSAY.
    ────────────────────────────────────────────────────────────────────────────────────
    The first version returned {"type": "http.disconnect"} on every call after the body:

        async def receive():
            if sent: return {"type": "http.disconnect"}    # <- a lie
            ...

    That looks harmless. The body has been delivered; what else could the app want?

    It wants to know if the CLIENT IS STILL THERE. In ASGI, an app streaming a long-lived
    response polls receive() to detect a hang-up. FastMCP's SSE transport does exactly this
    while it streams. My middleware answered that poll with "the client disconnected" — so
    FastMCP dutifully tore down the stream BEFORE writing the response event.

    The symptom was maddening and pointed everywhere except here: the HTTP 200 went out, the
    mcp-session-id header went out, the SSE stream opened... and then nothing. curl looked
    fine, because we were only ever inspecting headers with -i and -D. Any client that
    actually WAITS for the body — i.e. every real MCP client — hung and reported "failed to
    connect." We blamed DNS, then a VPN, then Render's cold starts, then Claude Code itself.

    Two lessons, and I'd rather write them down than pretend I knew:
      1. A middleware that swallows a request stream must forward the REST of that stream,
         not fabricate its end. `receive` is a channel, not a one-shot.
      2. `curl -i` proving "the server responds" proves the server responds WITH HEADERS.
         It is not a test of the body. Test what the client actually consumes.
    """
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        # Body's been replayed. Everything after this — including a REAL http.disconnect when
        # the client genuinely leaves — is the transport's business, not ours. Delegate.
        return await original()

    return receive


def _tool_name(body: bytes) -> str | None:
    try:
        rpc = json.loads(body)
    except Exception:
        return None
    if not isinstance(rpc, dict) or rpc.get("method") != "tools/call":
        return None
    return (rpc.get("params") or {}).get("name")


def _tool_args(body: bytes) -> dict:
    try:
        rpc = json.loads(body)
        return (rpc.get("params") or {}).get("arguments") or {}
    except Exception:
        return {}


def _rpc_id(body: bytes):
    try:
        return json.loads(body).get("id")
    except Exception:
        return None


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return v.decode()
    return None


async def _402(send: Send, challenge: str, reqs: dict | None = None, error: str | None = None) -> None:
    """A real HTTP 402 that satisfies EVERY x402 client shape we know of.

    An OKX admin reported that discovery probes never saw a 402 (they got 406 first — fixed
    upstream in __call__). But there was a second latent issue: our 402 body only carried the
    challenge base64-encoded as `accepts_b64`. The standard x402 discovery flow expects the
    `accepts` ARRAY in plaintext in the body — that is what `x402-check`/`x402-validate` parse.
    A client reading the body for `accepts` found only an opaque blob.

    So we now emit, belt AND braces:
      · header  PAYMENT-REQUIRED  = the base64 challenge (skill reads this)
      · header  WWW-Authenticate  = the scheme signal (some clients gate on this)
      · body    accepts[]         = the plaintext requirements array (validators read this)
      · body    x402Version, accepts_b64 (backward compat with what we already shipped)

    Being maximally liberal in what we emit costs a few bytes and buys compatibility with
    every buyer tool, including the ones we cannot test against. For a payment endpoint,
    "works with the client the judge happens to use" is not optional.
    """
    body_obj: dict = {
        "x402Version": 2,
        # If a payment was attempted and FAILED, say so specifically. "payment required" is
        # correct for a first request and actively misleading for a failed settlement.
        "error": error or "payment required",
        "payment_attempted": error is not None,
        "accepts_b64": challenge,
    }
    if reqs is not None:
        body_obj["accepts"] = [reqs]      # <- the plaintext array x402 validators look for

    payload = json.dumps(body_obj).encode()

    await send({
        "type": "http.response.start",
        "status": 402,
        "headers": [
            (b"content-type", b"application/json"),
            (b"payment-required", challenge.encode()),
            (b"www-authenticate", b'Payment realm="merita", x402Version=2'),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})


async def _json(send: Send, status: int, obj: dict) -> None:
    payload = json.dumps(obj).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload})