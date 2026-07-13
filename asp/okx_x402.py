"""
okx_x402.py — the seller side of x402, against OKX's facilitator on X Layer.

There is no Python SDK (OKX ships Go). That is fine: the seller side of x402 is small, and
writing it against the documented HTTP surface is better than adding a second runtime to a
project that already learned that lesson the hard way.

THE FLOW, and the one ordering decision that matters
────────────────────────────────────────────────────
  1. Buyer calls the tool with no payment.
  2. We answer 402 + PAYMENT-REQUIRED (base64 JSON), listing what we accept.
  3. Buyer's agent signs an EIP-3009 authorization, replays with X-PAYMENT.
  4. We forward the payload VERBATIM to /verify.        ← does NOT move money
  5. verify passes  →  WE DO THE WORK.
  6. Work succeeds  →  /settle.                          ← moves money
  7. We return the result + X-PAYMENT-RESPONSE (the receipt).

Step 5 sits BETWEEN verify and settle deliberately.

Settle-first would be simpler and would also mean charging for verifications that then blow
up in our sandbox. Work-first-settle-never would mean giving the work away when settlement
fails. The x402 design puts the free, non-binding signature check (verify) before the work
and the irreversible money movement (settle) after it, and that ordering is the whole point
of the protocol having two calls instead of one. Respect it.

The residual risk is real and I am naming it rather than hiding it: if /settle fails after
the sandbox has already run, we did the work for free. That is a few milliseconds of CPU.
The inverse — taking money for a verification we could not perform — is a reputational hit
on a marketplace where reputation is literally on-chain. The asymmetry is not close.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

log = logging.getLogger(__name__)

FACILITATOR = "https://web3.okx.com"
X402_VERSION = 2

# X Layer mainnet. CAIP-2. Not a magic number — chain ID 196.
NETWORK = "eip155:196"

# USDG on X Layer, from OKX's own facilitator docs. VERIFY THIS ON THE EXPLORER before you
# take real money: a wrong token address means buyers sign authorizations for an asset you
# do not accept, every payment silently fails verification, and you will spend an afternoon
# blaming your HMAC.
USDG_XLAYER = "0x4ae46a509f6b1d9056937ba4500cb143933d2dc8"


@dataclass(frozen=True)
class Price:
    """What a tool call costs. Atomic units — integers only, never floats.

    Money is never a float. 0.1 + 0.2 != 0.3, and a marketplace that rounds in the buyer's
    favour bleeds, while one that rounds in its own gets reported. Integers, all the way down.
    """

    atomic: str          # e.g. "10000" == 0.01 USDG at 6dp
    asset: str = USDG_XLAYER
    name: str = "USDG"
    version: str = "2"   # EIP-712 domain version for the token contract


class OkxAuth:
    """OKX v5 HMAC. Same scheme as their exchange API."""

    def __init__(self) -> None:
        self.key = os.environ.get("OKX_API_KEY", "")
        self.secret = os.environ.get("OKX_API_SECRET", "")
        self.passphrase = os.environ.get("OKX_API_PASSPHRASE", "")
        self.pay_to = os.environ.get("MERITA_PAYTO_ADDRESS", "")

        if not all([self.key, self.secret, self.passphrase, self.pay_to]):
            # Loud, at boot, not at the first paid call. A payment endpoint that starts
            # cleanly and then 500s on its first real buyer is worse than one that refuses
            # to start: the buyer's agent has already burned a signature and a round-trip.
            log.error(
                "x402 is NOT configured (need OKX_API_KEY / SECRET / PASSPHRASE / "
                "MERITA_PAYTO_ADDRESS). Paid tools will refuse to serve."
            )

    @property
    def configured(self) -> bool:
        return all([self.key, self.secret, self.passphrase, self.pay_to])

    def headers(self, method: str, path: str, body: str) -> dict[str, str]:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
             f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
        msg = f"{ts}{method.upper()}{path}{body}"
        sign = base64.b64encode(
            hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).digest()
        ).decode()
        return {
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "OK-ACCESS-TIMESTAMP": ts,
        }


class Facilitator:
    def __init__(self, auth: OkxAuth | None = None) -> None:
        self._auth = auth or OkxAuth()
        self._c = httpx.AsyncClient(base_url=FACILITATOR, timeout=30.0)

    @property
    def configured(self) -> bool:
        return self._auth.configured

    # ── The 402 challenge ───────────────────────────────────────────────────

    def requirements(self, *, resource_url: str, description: str, price: Price) -> dict[str, Any]:
        return {
            "scheme": "exact",
            "network": NETWORK,
            "amount": price.atomic,
            "asset": price.asset,
            "payTo": self._auth.pay_to,
            "maxTimeoutSeconds": 60,
            "extra": {"name": price.name, "version": price.version},
            "resource": {
                "url": resource_url,
                "description": description,
                "mimeType": "application/json",
            },
        }

    def challenge_header(self, reqs: dict[str, Any]) -> str:
        """PAYMENT-REQUIRED: base64(JSON). The buyer's agent decodes this and signs."""
        body = {"x402Version": X402_VERSION, "accepts": [reqs]}
        return base64.b64encode(json.dumps(body).encode()).decode()

    # ── verify → (work) → settle ────────────────────────────────────────────

    async def verify(self, x_payment_b64: str, reqs: dict[str, Any]) -> tuple[bool, str | None]:
        """Free, non-binding. Does the signature check out? No money moves here."""
        try:
            payload = json.loads(base64.b64decode(x_payment_b64))
        except Exception:
            return False, "malformed X-PAYMENT header"

        ok, data = await self._call("/api/v6/pay/x402/verify", payload, reqs)
        if not ok:
            return False, "facilitator unreachable"
        if data.get("isValid") is True or data.get("success") is True:
            return True, None
        return False, data.get("invalidReason") or data.get("errorReason") or "verification failed"

    async def settle(self, x_payment_b64: str, reqs: dict[str, Any]) -> dict[str, Any] | None:
        """Irreversible. Call this ONLY after the work succeeded."""
        payload = json.loads(base64.b64decode(x_payment_b64))
        ok, data = await self._call("/api/v6/pay/x402/settle", payload, reqs)
        if not ok or not data.get("success"):
            log.error("settle FAILED: %s", data)
            return None
        return data

    @staticmethod
    def receipt_header(settle_data: dict[str, Any]) -> str:
        """X-PAYMENT-RESPONSE. The buyer's proof they paid, and ours that we were paid."""
        return base64.b64encode(json.dumps(settle_data).encode()).decode()

    async def _call(self, path: str, payload: dict, reqs: dict) -> tuple[bool, dict]:
        body = json.dumps(
            {"x402Version": X402_VERSION, "paymentPayload": payload, "paymentRequirements": reqs},
            separators=(",", ":"),
        )
        try:
            r = await self._c.post(path, content=body, headers=self._auth.headers("POST", path, body))
            j = r.json()
        except Exception as e:
            log.error("facilitator %s: %s", path, e)
            return False, {}

        if j.get("code") != "0":
            log.error("facilitator %s rejected: %s", path, j.get("msg"))
            return False, j.get("data") or {}
        return True, j.get("data") or {}

    async def close(self) -> None:
        await self._c.aclose()
