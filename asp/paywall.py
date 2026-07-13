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
    def __init__(self, app: ASGIApp, *, facilitator, paid_tools: PaidTools, resource_url: str) -> None:
        self.app = app
        self.fac = facilitator
        self.paid = paid_tools
        self.resource_url = resource_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)

        body = await _drain(receive)

        tool = _tool_name(body)
        if tool is None or tool not in self.paid:
            # tools/list, initialize, ping, and every free tool sail straight through.
            # This is what keeps MCP discovery working — and discovery has to work, or OKX
            # cannot review the listing and the submission is invalid.
            return await self.app(scope, _replay(body), send)

        price, description = self.paid[tool]

        if not self.fac.configured:
            log.error("paid tool %s called but x402 is unconfigured — refusing", tool)
            return await _json(send, 503, {"error": "payment not configured"})

        reqs = self.fac.requirements(
            resource_url=self.resource_url, description=description, price=price
        )
        x_payment = _header(scope, b"x-payment")

        if not x_payment:
            return await _402(send, self.fac.challenge_header(reqs))

        ok, reason = await self.fac.verify(x_payment, reqs)
        if not ok:
            log.warning("x402 verify failed for %s: %s", tool, reason)
            return await _402(send, self.fac.challenge_header(reqs))

        # PAID AND VERIFIED — but NOT yet settled. Settlement happens after the work
        # succeeds, in the tool itself, via the context we stash here. verify() is free and
        # reversible; settle() moves money and is not. Never merge them.
        scope["merita_payment"] = {"reqs": reqs, "x_payment": x_payment}
        await self.app(scope, _replay(body), send)


# ── ASGI plumbing ────────────────────────────────────────────────────────────

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


def _replay(body: bytes) -> Receive:
    """Hand the buffered body downstream as if it were never read."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def _tool_name(body: bytes) -> str | None:
    try:
        rpc = json.loads(body)
    except Exception:
        return None
    if not isinstance(rpc, dict) or rpc.get("method") != "tools/call":
        return None
    return (rpc.get("params") or {}).get("name")


def _header(scope: Scope, name: bytes) -> str | None:
    for k, v in scope.get("headers", []):
        if k.lower() == name:
            return v.decode()
    return None


async def _402(send: Send, challenge: str) -> None:
    """A real HTTP 402, with the headers OKX's payment layer actually looks for.

    Both PAYMENT-REQUIRED and WWW-Authenticate are emitted. OKX's agent-payments skill checks
    WWW-Authenticate first, then PAYMENT-REQUIRED, then a body with x402Version — so we
    satisfy all three and let the buyer's client pick whichever it prefers. Being liberal in
    what you emit costs nothing here and buys compatibility with clients we cannot test against.
    """
    payload = json.dumps({
        "x402Version": 2,
        "error": "payment required",
        "accepts_b64": challenge,
    }).encode()

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
