"""sidecar.py — typed HTTP client to the TS chain seam. The brain's only door to Solana."""
from __future__ import annotations

import os
import httpx


class SidecarError(RuntimeError):
    """A chain operation failed. NEVER swallowed: a swallowed settlement error is a lie
    about money, and every downstream number becomes fiction."""


class Sidecar:
    def __init__(self, base: str | None = None, token: str | None = None) -> None:
        self._base = base or os.environ.get("SIDECAR_URL", "http://127.0.0.1:8787")
        self._token = token or os.environ.get("SIDECAR_TOKEN", "")
        self._c = httpx.AsyncClient(timeout=120.0)  # chain confirmations are slow; be patient

    async def post(self, path: str, body: dict) -> dict:
        r = await self._c.post(
            f"{self._base}{path}", json=body, headers={"x-sidecar-token": self._token}
        )
        if r.status_code >= 400:
            raise SidecarError(f"{path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    async def close(self) -> None:
        await self._c.aclose()
