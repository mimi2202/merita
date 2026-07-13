"""
service.py — the blast chamber.

This process exists for exactly one reason: to run `check()` written by a stranger.

WHY IT IS A SEPARATE SERVICE AND NOT A FUNCTION CALL
────────────────────────────────────────────────────
tier1.py's sandbox is good — it kills infinite loops, memory bombs and disk writes. But my
own test suite proved it cannot stop `open()`. You cannot stop `open()` from inside Python;
every "restricted __builtins__" recipe is escapable via `().__class__.__mro__` and has been
for twenty years. So the containment has to happen below the language: a process whose
*filesystem does not contain anything worth reading*, and whose *network cannot reach
anything worth spending*.

Hence the container this runs in (see docker-compose.yml):
  · ./secrets is NOT MOUNTED. The wallet does not exist in this container's universe.
  · It sits on an `internal: true` network. No route to the internet, so nothing it manages
    to read can be exfiltrated.
  · The sidecar — the only process holding keys — is NOT on that network. Untrusted code
    cannot reach the thing that signs transactions even by name.
  · cap_drop ALL, no-new-privileges, user 65534. Nothing to escalate to, nothing to escalate
    from.

The one residual hole: this container CAN reach `brain`, because brain has to be able to
reach it. So brain's mutating endpoint requires a token that this container is never given.
That is stated plainly rather than hidden, because a security boundary you can't articulate
is a security boundary you don't have.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI
from pydantic import BaseModel

from . import tier1
from ..models import Verdict
from fastapi import HTTPException

logging.basicConfig(level=logging.INFO)
app = FastAPI(title="merita-verifier")


class VerifyReq(BaseModel):
    revealed_source: str
    revealed_nonce: str
    commitment: str
    deliverable: object


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "role": "verifier"}


@app.post("/verify")
def verify(req: VerifyReq) -> Verdict:
    # `def`, NOT `async def`. This is load-bearing.
    #
    # tier1.verify() blocks for up to 20 seconds (two runs × a 10s wall-clock cap). Declared
    # `async def`, that blocking happens ON THE EVENT LOOP, and one poster submitting a slow
    # test freezes every other verification in the process — including the /health probe, so
    # the thing looks dead to its orchestrator while it is merely busy.
    #
    # Declared `def`, FastAPI runs it in a threadpool and the loop stays responsive. This is
    # the one place in the codebase where writing LESS async is the correct call, and it is
    # exactly the place a reflexive `async def` would have quietly cost us the demo.
    #
    # No try/except either: if the sandbox itself explodes, the caller gets a 500 and the
    # orchestrator ESCALATES. It must never receive a `passed=False` that would slash a
    # worker for our own infrastructure falling over.
    # No try/except around normal verdicts — a bad deliverable is a valid passed=False, not
    # an error. But SandboxUnavailable is different: the HOST is unsafe, not the deliverable.
    # Turn it into a 503 so the caller's fail-open rule ESCALATES (never a silent pass, never
    # an unearned slash). This is the alarm that says "your deploy is misconfigured", and it
    # should be impossible to mistake for a verdict.
    try:
        return tier1.verify(
            revealed_source=req.revealed_source,
            revealed_nonce=req.revealed_nonce,
            commitment=req.commitment,
            deliverable=req.deliverable,
        )
    except tier1.SandboxUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("VERIFIER_PORT", "9000")))
