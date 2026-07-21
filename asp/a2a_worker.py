"""
a2a_worker.py — delivers A2A task results on-chain.

WHY THIS IS A SEPARATE, LOCAL PROCESS AND NOT PART OF THE RENDER SERVER
──────────────────────────────────────────────────────────────────────
`onchainos agent deliver` signs an on-chain transaction using your logged-in wallet session.
It needs the onchainos binary AND a live authenticated wallet.

Putting either inside merita-asp.onrender.com would hand transaction-signing authority to the
most exposed process in the system — a public HTTP server that, by design, executes acceptance
tests written by strangers. Every other decision in this codebase has kept signing away from
untrusted input (see referee/tier1.py, docker-compose.yml). This is the same rule, and A2A
does not get an exception for being inconvenient.

So the split is:
    RENDER  · public HTTP surface (A2MCP + x402). Holds no wallet. Cannot sign anything.
    LOCAL   · this worker. Holds the wallet session. Never accepts input from strangers.

The worker reaches the referee logic directly (same sandbox, same rules), produces a verdict
document, and hands it to the CLI to deliver.

THE HARD GATE
─────────────
`deliver` only succeeds when the task status is exactly `accepted`. We check FIRST and refuse
otherwise — not because the CLI would fail (it would), but because delivering before escrow is
funded means doing the work unpaid. The check is ours to make, not the CLI's to catch.

ENVIRONMENT
    MERITA_WORKER_TOKEN   shared secret, must match the server's value
    MERITA_SERVER_URL     defaults to https://merita-asp.onrender.com
    MERITA_AGENT_ID       defaults to 5516

USAGE
    python -m asp.a2a_worker --job-id <jobId>
    python -m asp.a2a_worker --event '<job_accepted event JSON>'
    python -m asp.a2a_worker --job-id <jobId> --dry-run     # verify, print, deliver nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("a2a")

AGENT_ID = os.environ.get("MERITA_AGENT_ID", "5516")
CLI = os.environ.get("ONCHAINOS_BIN", "onchainos")
CLI_TIMEOUT_S = 120

# The live server that runs the sandbox. The worker never verifies locally — see
# build_verdict_document() for why (POSIX-only sandbox, and one referee implementation).
SERVER_URL = os.environ.get("MERITA_SERVER_URL", "https://merita-asp.onrender.com")
WORKER_TOKEN = os.environ.get("MERITA_WORKER_TOKEN", "")


class DeliveryError(RuntimeError):
    """Something went wrong delivering. ALWAYS raised loudly — never swallowed.

    The admin's report was 'the process exited without a result'. Silence is the worst
    possible failure here: the buyer's escrow is funded and they are waiting. Every failure
    path in this file logs at ERROR and exits non-zero so a supervisor, a human, or a CI job
    notices immediately."""


# ─────────────────────────────────────────────────────────────────────────────
# CLI plumbing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CliResult:
    ok: bool
    stdout: str
    stderr: str
    code: int


def _run(args: list[str]) -> CliResult:
    """Run an onchainos command. Never raises on non-zero — the caller decides."""
    cmd = [CLI, *args]
    log.info("$ %s", " ".join(cmd))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT_S)
    except FileNotFoundError:
        raise DeliveryError(
            f"'{CLI}' not found on PATH. Install the OnchainOS CLI and log in to your "
            f"Agentic Wallet before running this worker."
        ) from None
    except subprocess.TimeoutExpired:
        raise DeliveryError(f"onchainos command timed out after {CLI_TIMEOUT_S}s: {' '.join(args)}")

    if p.stdout.strip():
        log.debug("stdout: %s", p.stdout.strip()[:600])
    if p.returncode != 0:
        log.warning("exit %d: %s", p.returncode, (p.stderr or p.stdout).strip()[:400])

    return CliResult(ok=p.returncode == 0, stdout=p.stdout, stderr=p.stderr, code=p.returncode)


def _json_from(text: str) -> dict | None:
    """CLI output may be JSON, or JSON embedded in human-readable text. Try both."""
    try:
        return json.loads(text)
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except Exception:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Preflight
# ─────────────────────────────────────────────────────────────────────────────

def gate_check() -> None:
    """Confirm the wallet session is live BEFORE doing any work.

    Without this, we would verify a deliverable, build the document, and only then discover
    the wallet is logged out — having burned the work and left the buyer waiting. Check the
    thing that is easy to check, first."""
    r = _run(["agent", "gate-check", "--role", "asp"])
    if not r.ok:
        raise DeliveryError(
            "wallet gate-check failed — you are probably not logged in to the Agentic Wallet. "
            f"Run the OnchainOS login flow, then retry. Detail: {(r.stderr or r.stdout)[:300]}"
        )
    log.info("gate-check: wallet session OK")


def task_status(job_id: str) -> str:
    """Current status string for the job. Empty string if it can't be determined."""
    r = _run(["agent", "status", job_id, "--agent-id", AGENT_ID])
    if not r.ok:
        raise DeliveryError(f"could not read status for job {job_id}: {(r.stderr or r.stdout)[:300]}")

    data = _json_from(r.stdout) or {}
    status = str(
        data.get("status")
        or data.get("state")
        or (data.get("task") or {}).get("status")
        or ""
    ).lower()

    if not status:
        # Fall back to scanning the text — the CLI's exact JSON shape is not documented and
        # I would rather grep honestly than silently assume 'accepted'.
        text = r.stdout.lower()
        for candidate in ("accepted", "submitted", "completed", "applied", "rejected", "disputed"):
            if candidate in text:
                status = candidate
                break

    log.info("job %s status = %r", job_id, status or "<unknown>")
    return status


# ─────────────────────────────────────────────────────────────────────────────
# The work
# ─────────────────────────────────────────────────────────────────────────────

def build_verdict_document(job_id: str, task_id: str | None, deliverable: dict | None) -> dict:
    """
    Get Merita's verdict for this job from the live server.

    WHY OVER HTTP AND NOT BY IMPORTING THE REFEREE:
    the sandbox uses POSIX resource limits and process groups — it does not run on Windows,
    and the limits ARE the sandbox, so there is no honest way to stub them out. Rather than
    force this worker onto Linux, it asks the server that already runs the sandbox correctly.
    One referee, one implementation, one set of guarantees; the worker is a thin client.

    It authenticates with a shared token rather than paying the public price. Merita paying
    Merita would be a meaningless wallet-to-wallet loop — and on a marketplace that
    disqualifies self-dealing, our own integrity graph would rightly flag it.
    """
    if not task_id:
        return {
            "service": "Merita — Acceptance Test Verification",
            "job_id": job_id,
            "verdict": None,
            "error": "no task_id supplied with this job; nothing to verify",
            "how_to_use": (
                "Commit a machine-checkable acceptance test first (free), then submit the "
                "worker's deliverable. Merita runs it in a hardened sandbox and returns a "
                "signed pass/fail verdict."
            ),
        }

    if not WORKER_TOKEN:
        raise DeliveryError(
            "MERITA_WORKER_TOKEN is not set. The worker authenticates to the server with it; "
            "without it there is no way to obtain a verdict. Set the same value here and in "
            "the server's environment."
        )

    import httpx  # noqa: PLC0415

    url = f"{SERVER_URL.rstrip('/')}/internal/verify"
    log.info("fetching verdict from %s", url)
    try:
        # Generous timeout: the sandbox runs the test twice at 10s wall clock each, and a
        # free-tier host may be cold-starting on top of that.
        r = httpx.post(
            url,
            headers={"x-worker-token": WORKER_TOKEN},
            json={"task_id": task_id, "deliverable": deliverable},
            timeout=90.0,
        )
    except Exception as e:
        raise DeliveryError(f"could not reach the verifier at {url}: {e}") from e

    if r.status_code == 401:
        raise DeliveryError("verifier rejected the worker token — check MERITA_WORKER_TOKEN "
                            "matches the value set on the server")
    if r.status_code == 503:
        raise DeliveryError("verifier's internal endpoint is disabled — set MERITA_WORKER_TOKEN "
                            "in the server environment and redeploy")
    if r.status_code == 404:
        return {
            "service": "Merita — Acceptance Test Verification",
            "job_id": job_id,
            "task_id": task_id,
            "verdict": None,
            "error": f"no committed acceptance test for task '{task_id}'",
        }
    if r.status_code != 200:
        raise DeliveryError(f"verifier returned {r.status_code}: {r.text[:300]}")

    v = r.json()
    return {
        "service": "Merita — Acceptance Test Verification",
        "job_id": job_id,
        "task_id": task_id,
        "commitment": v.get("commitment"),
        "verdict": {
            "passed": v.get("passed"),
            "confidence": v.get("confidence"),
            "reason": v.get("reason"),
            "reveal_valid": v.get("reveal_valid"),
        },
        "settle_escrow": bool(v.get("passed")),
        "note": (
            "The acceptance test was committed as a hash before work began and revealed only "
            "at verification. If reveal_valid is false, the poster changed the test after the "
            "fact and the ruling goes to the worker."
        ),
    }


def deliver(job_id: str, doc: dict, message: str | None = None, dry_run: bool = False) -> None:
    """Write the verdict to a file and deliver it on-chain."""
    passed = (doc.get("verdict") or {}).get("passed")
    if message is None:
        if passed is True:
            message = "Verification complete — PASSED. Verdict and commitment attached."
        elif passed is False:
            message = "Verification complete — FAILED. Verdict, reason, and commitment attached."
        else:
            message = "Could not render a verdict; details attached."

    tmp = Path(tempfile.gettempdir()) / f"merita-verdict-{job_id}.json"
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    log.info("verdict document -> %s", tmp)

    if dry_run:
        log.info("DRY RUN — not delivering. Document:\n%s", json.dumps(doc, indent=2))
        return

    r = _run([
        "agent", "deliver", job_id,
        "--file", str(tmp),
        "--message", message,
        "--agent-id", AGENT_ID,
    ])
    if not r.ok:
        raise DeliveryError(
            f"deliver failed for job {job_id} (exit {r.code}): {(r.stderr or r.stdout)[:400]}"
        )

    log.info("DELIVERED job %s — task should now be submitted(2)", job_id)


# ─────────────────────────────────────────────────────────────────────────────
# Entry
# ─────────────────────────────────────────────────────────────────────────────

def process(job_id: str, task_id: str | None, deliverable: dict | None, dry_run: bool) -> None:
    gate_check()

    status = task_status(job_id)
    if status != "accepted":
        # THE HARD GATE. Refuse to work before escrow is funded. The CLI enforces this too,
        # but relying on someone else's guard to protect your own economics is how you end up
        # delivering for free.
        raise DeliveryError(
            f"job {job_id} is '{status or 'unknown'}', not 'accepted'. Refusing to deliver: "
            f"delivering before escrow is funded means working unpaid."
        )

    doc = build_verdict_document(job_id, task_id, deliverable)
    deliver(job_id, doc, dry_run=dry_run)


def main() -> int:
    ap = argparse.ArgumentParser(description="Merita A2A delivery worker")
    ap.add_argument("--job-id", help="jobId (0x… hex or task-001 string)")
    ap.add_argument("--event", help="raw job_accepted event JSON")
    ap.add_argument("--task-id", help="Merita task_id whose committed test to run")
    ap.add_argument("--deliverable", help="deliverable JSON to judge (or @path/to/file.json)")
    ap.add_argument("--dry-run", action="store_true", help="verify and print; deliver nothing")
    a = ap.parse_args()

    job_id, task_id, deliverable = a.job_id, a.task_id, None

    if a.event:
        ev = _json_from(a.event) or {}
        msg = ev.get("message") or ev
        job_id = job_id or ev.get("jobId") or msg.get("jobId")
        task_id = task_id or msg.get("task_id") or msg.get("taskId")
        deliverable = msg.get("deliverable")

    if a.deliverable:
        raw = a.deliverable
        if raw.startswith("@"):
            raw = Path(raw[1:]).read_text(encoding="utf-8")
        deliverable = _json_from(raw)

    if not job_id:
        log.error("no --job-id (and none found in --event)")
        return 2

    try:
        process(job_id, task_id, deliverable, a.dry_run)
    except DeliveryError as e:
        # FAIL LOUDLY. Non-zero exit, clear message. Never exit 0 having done nothing.
        log.error("DELIVERY FAILED: %s", e)
        return 1
    except Exception as e:
        log.exception("UNEXPECTED FAILURE: %s", e)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())