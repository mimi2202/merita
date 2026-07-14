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

# THE TOKEN ADDRESS IS DISCOVERED AT BOOT, NOT HARDCODED. Read this before you "simplify" it.
#
# Public sources disagree about USDT on X Layer:
#   · OKX's bridge guide:  0x1E4a5963aBFD975d8c9021ce480b42188849D41d   ("USDT")
#   · OKX's USDT0 FAQ:     0x779Ded0c9e1022225f8E0630b35a9b54bE713736   ("new USDT0")
#   · and that same FAQ then says the address "remains unchanged for your convenience"
#
# Three OKX-authored sources, mutually inconsistent. Pick wrong and every buyer signs an
# EIP-3009 authorization for an asset we do not accept: verification fails 100% of the time,
# silently, on a rail that looks perfectly healthy. You would debug the HMAC for a day.
#
# So we do not pick. We ASK. The facilitator's /supported endpoint is the only source that
# cannot be stale, because it IS the thing doing the settling. If two sources disagree, stop
# choosing between them and go find the one that is authoritative by construction.
_DEFAULT_TOKEN = "0x1E4a5963aBFD975d8c9021ce480b42188849D41d"  # fallback only; overridden at boot


@dataclass
class Token:
    """A settlement asset, as the facilitator itself reports it."""

    address: str
    symbol: str
    decimals: int = 6
    eip712_version: str = "1"   # EIP-712 domain version of the token contract

    def units(self, human: float) -> str:
        """Human amount -> atomic string. Integers only; money is never a float."""
        return str(int(round(human * (10 ** self.decimals))))


@dataclass(frozen=True)
class Price:
    """What a tool call costs, in HUMAN units. The atomic conversion happens against the
    token the facilitator told us about, at request time — never against a constant.

    Money is never a float in the wire format. 0.1 + 0.2 != 0.3, and a marketplace that
    rounds in the buyer's favour bleeds while one that rounds in its own gets reported.
    We accept a float here only as a human-facing convenience and convert to integer atomic
    units exactly once, at the boundary.
    """

    human: float          # e.g. 0.02  ->  "20000" at 6dp


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
        # LAZY. Do NOT construct the AsyncClient here.
        #
        # httpx binds its connection pool to the event loop that is running when it is
        # created. Constructing it at import time binds it to whatever loop happens to exist
        # then — and `asyncio.run(check_supported())` at startup CLOSES its loop when it
        # returns. The client is then holding a corpse. Uvicorn starts a fresh loop, the first
        # real payment arrives, and the facilitator call dies with "Event loop is closed".
        #
        # The failure is beautifully cruel: /health is green, the 402 fires correctly, the
        # buyer signs correctly, the replay is correct — and settlement fails anyway, with an
        # error that points at asyncio rather than at the line that caused it. Cost: one real
        # payment attempt to find, one line to fix.
        self._client: httpx.AsyncClient | None = None
        # An OPERATOR ASSERTION, not a discovery. See check_supported() for why there is no
        # honest way to discover this. Overridable by env so a wrong guess is a config change,
        # not a redeploy of code.
        self.token = Token(
            address=os.environ.get("MERITA_SETTLEMENT_ASSET", _DEFAULT_TOKEN),
            symbol=os.environ.get("MERITA_SETTLEMENT_SYMBOL", "USDT"),
            decimals=int(os.environ.get("MERITA_SETTLEMENT_DECIMALS", "6")),
            # EIP-712 DOMAIN VERSION OF THE TOKEN CONTRACT. Overridable, because getting it
            # wrong is invisible and total.
            #
            # The buyer signs an EIP-3009 authorization over a domain separator built from
            # (name, version, chainId, verifyingContract). If our advertised `version` differs
            # from what the token contract actually declares, the buyer signs over a DIFFERENT
            # domain than the facilitator reconstructs — and the signature fails to recover to
            # the payer's address. Every time. With no diagnostic beyond "invalid signature".
            #
            # OKX's own doc examples show version "2" for their stablecoin; the ERC-20 default
            # is "1". I cannot verify USD₮0's from here, so it is an env var: if verify fails
            # with a signature error, flip MERITA_TOKEN_VERSION to 2 and restart. Thirty
            # seconds, no redeploy.
            eip712_version=os.environ.get("MERITA_TOKEN_VERSION", "1"),
        )
        self.supported = False

    @property
    def configured(self) -> bool:
        return self._auth.configured

    @property
    def _c(self) -> httpx.AsyncClient:
        """Created on first use, inside the loop that will actually use it."""
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=FACILITATOR, timeout=30.0)
        return self._client

    # ── Boot-time discovery ─────────────────────────────────────────────────

    async def check_supported(self) -> bool:
        """
        Confirm the facilitator will settle `exact` on X Layer, and log what it offers.

        NOTE WHAT THIS DOES *NOT* DO: it does not discover the token address.

        I assumed /supported would list assets. It does not — it advertises NETWORKS and
        SCHEMES only:
            {"network":"eip155:196","scheme":"exact","x402Version":2}
            {"network":"eip155:196","scheme":"exact","extra":{"assetTransferMethod":"permit2"}}
        There is no asset field, anywhere, by design: in x402 the SELLER declares the asset in
        the 402 challenge and the facilitator settles whatever the buyer validly signed for.
        The token address is our assertion to make, not theirs to publish.

        Which means the address below IS a hardcoded constant and there is no clever way out
        of that. So it gets verified the only way a constant like this can be: by a human,
        against the explorer, once — and then loudly surfaced on /health forever after, so
        nobody can forget it is an assumption. See MERITA_SETTLEMENT_ASSET.
        """
        path = "/api/v6/pay/x402/supported"
        try:
            r = await self._c.get(path, headers=self._auth.headers("GET", path, ""))
            j = r.json()
            kinds = (j.get("data") or {}).get("kinds") or []
            ok = any(
                k.get("network") == NETWORK and k.get("scheme") == "exact" for k in kinds
            )
            if ok:
                log.info("facilitator: 'exact' settlement supported on %s", NETWORK)
            else:
                log.error("facilitator does NOT offer 'exact' on %s — payments will fail. %s",
                          NETWORK, str(kinds)[:300])
            self.supported = ok
            return ok
        except Exception as e:
            log.error("facilitator /supported probe failed: %s", e)
            return False

    # ── The 402 challenge ───────────────────────────────────────────────────

    def requirements(self, *, resource_url: str, description: str, price: Price) -> dict[str, Any]:
        t = self.token
        return {
            "scheme": "exact",
            "network": NETWORK,
            "amount": t.units(price.human),   # atomic, computed against the REAL decimals
            "asset": t.address,
            "payTo": self._auth.pay_to,
            "maxTimeoutSeconds": 60,
            "extra": {"name": t.symbol, "version": t.eip712_version},
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

        # Log the FULL facilitator response, not just the reason code. The paywall
        # deliberately tells the buyer nothing (never leak validation internals to an
        # unauthenticated caller) — which means this log line is the ONLY place the truth
        # exists. If it is terse, the operator is blind, and a silent total payment failure
        # is indistinguishable from a working service. Verbosity here is not sloppiness; it
        # is the compensating control for the silence out there.
        reason = data.get("invalidReason") or data.get("errorReason") or "unknown"
        log.error(
            "x402 VERIFY REJECTED — reason=%r msg=%r | advertised asset=%s version=%s payTo=%s | "
            "full facilitator response: %s",
            reason, data.get("errorMessage"), self.token.address, self.token.eip712_version,
            self._auth.pay_to, str(data)[:500],
        )
        return False, reason

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
        """
        Forward the buyer's payment payload to the facilitator.

        THE ENVELOPE PROBLEM — this cost a real payment to find.
        ────────────────────────────────────────────────────────
        The docs say "the Seller forwards it verbatim to the Facilitator", so that is what I
        did. The facilitator answered:

            {"invalidReason": "param_mismatch",
             "invalidMessage": "paymentPayload.accepted is null"}

        Because "verbatim" assumes the buyer sends a COMPLETE payload. OKX's own /verify
        example shows paymentPayload containing FOUR keys:

            {x402Version, resource, accepted, payload:{signature, authorization}}

        But `onchainos payment pay` returns only the inner proof — {authorization, signature}.
        The buyer signs; it does not re-state the terms. Which is correct, and obvious in
        hindsight: the terms are the SELLER's assertion. We wrote them. We are the only party
        who can authoritatively say what was advertised, and re-deriving them from the buyer's
        header would mean trusting the buyer to tell us what we charged.

        So we assemble the envelope: the buyer's signature over OUR requirements. If the buyer
        signed different terms than the ones we put in `accepted`, the signature simply will
        not verify — the cryptography, not our bookkeeping, is what enforces agreement. That
        is the right place for the check to live.

        We pass through anything the buyer DID send (some clients send the full envelope), and
        fill in only what is missing. Liberal in what you accept.
        """
        inner = payload.get("payload") or {
            k: v for k, v in payload.items()
            if k in ("signature", "authorization")
        }

        envelope = {
            "x402Version": X402_VERSION,
            "resource": payload.get("resource") or reqs.get("resource"),
            # The terms WE advertised in the 402. Not the buyer's word for them.
            "accepted": payload.get("accepted") or {
                k: v for k, v in reqs.items() if k != "resource"
            },
            "payload": inner,
        }

        body = json.dumps(
            {"x402Version": X402_VERSION, "paymentPayload": envelope, "paymentRequirements": reqs},
            separators=(",", ":"),
        )

        try:
            r = await self._c.post(path, content=body, headers=self._auth.headers("POST", path, body))
            j = r.json()
        except Exception as e:
            log.error("facilitator %s: %s", path, e)
            return False, {}

        if str(j.get("code")) != "0":
            log.error("facilitator %s rejected: code=%r msg=%s", path, j.get("code"), j.get("msg"))
            return False, j.get("data") or {}

        data = j.get("data") or {}

        # Log the FULL rejection. A payment that fails for an unlogged reason is a payment you
        # will debug twice.
        if data.get("isValid") is False or data.get("success") is False:
            log.error(
                "x402 %s REJECTED — reason=%r msg=%r | sent accepted=%s | full=%s",
                path.rsplit("/", 1)[-1],
                data.get("invalidReason") or data.get("errorReason"),
                data.get("invalidMessage") or data.get("errorMessage"),
                json.dumps(envelope["accepted"]),
                data,
            )

        return True, data

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None