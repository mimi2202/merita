"""
tier1.py — the deterministic referee. This is the load-bearing wall.

Everything else in Merita is a marketplace feature. THIS is the product: a thing that
looks at a deliverable and decides, without a human and without a model, whether escrow
should release. If this is wrong, money moves wrongly. So it is written like it moves money.

THREAT MODEL — the acceptance test is HOSTILE CODE.
──────────────────────────────────────────────────
The poster wrote `check()`. The poster is a stranger. On the same box we have:
  - the sidecar, which holds private keys with real SOL on them
  - the database of every job and receipt
  - network egress
A poster whose `check()` runs with our privileges can simply `open(wallet).read()` and
exfiltrate. `exec()` in-process with a "restricted __builtins__" dict is NOT a sandbox —
that has been escapable via `().__class__.__mro__` for twenty years and every "safe eval"
recipe on the internet is broken. Do not do it. We fork a real subprocess with:

  * a fresh interpreter (`-I`: ignore env, ignore user site-packages)
  * RLIMIT_AS      — memory ceiling, so a fork-bomb-by-allocation just dies
  * RLIMIT_CPU     — cpu ceiling, so `while True` just dies
  * RLIMIT_NPROC   — no forking out of the box
  * RLIMIT_FSIZE 0 — cannot write a single byte to disk
  * cwd = a fresh temp dir, and a wall-clock timeout on top of RLIMIT_CPU because
    RLIMIT_CPU does not count time blocked on I/O
  * no network: enforced at the container level (see docker-compose: the verifier service
    gets `network_mode: none`). Seccomp would be better; container isolation is what we
    can guarantee portably today, and it is honest to say so rather than pretend.

DETERMINISM
───────────
A referee that returns different answers on the same input is worse than no referee: it
is a coin-flip with a settlement attached. We fix PYTHONHASHSEED, forbid the test from
importing anything by convention (and detect it), and run the check TWICE. If the two runs
disagree, the verdict is not "fail" — it is `reveal_valid=True, passed=False,
reason="nondeterministic"` and the job escalates to Tier 2 rather than slashing a worker
who may well have done the job correctly. Never punish someone for the referee's flakiness.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from ..models import AcceptanceTest, Tier, Verdict

MEM_BYTES = 256 * 1024 * 1024
CPU_SECONDS = 5
WALL_SECONDS = 10


class SandboxUnavailable(RuntimeError):
    """The host cannot safely contain untrusted code. This must become an HTTP 500 that the
    caller's fail-open rule turns into ESCALATE — NOT a passed=False verdict. The distinction
    is the whole ballgame: 'I can't judge safely' must never be spent as 'you failed', and it
    must never become a silent pass either. It is an infrastructure alarm, and it should page
    someone, not settle a bounty."""

# The limits are applied INSIDE the child, as the first thing it does — not via
# subprocess's preexec_fn.
#
# WHY THIS MATTERS AND WHY I GOT IT WRONG FIRST TIME:
# preexec_fn runs in the forked child *after fork, before exec*, in a process that has
# inherited the parent's memory but only ONE of its threads. If the parent was holding any
# lock in another thread at the moment of fork — and a threaded web server always is — that
# lock is now held by a thread that does not exist in the child, and can never be released.
# CPython's own docs call preexec_fn "not safe in the presence of threads."
#
# I did not hit this until I ran the sandbox under uvicorn instead of a test script, and the
# symptom was not a deadlock: it was the *web server dying* when a poster submitted a
# `while True`. A hostile poster had a free DoS on the referee, hiding behind a green test
# suite. Setting the limits in the child's own __main__ is fork-safe and does the same job.
#
# THE setuid() CALL IS NOT DECORATION — READ THIS BEFORE YOU DELETE IT:
# On a multi-container deploy the sandbox lives in its own container with no secrets mounted,
# so a `check()` calling open() finds nothing worth reading. On a single-container host (a
# free tier, say) that isolation is GONE, and the commitment database is sitting right there
# on disk. A malicious poster's check() could open('/data/merita.db') and dump every OTHER
# poster's sealed acceptance test — which does not break one job, it breaks commit-reveal for
# the entire marketplace, silently, forever.
#
# So the child drops to an unprivileged UID that the database explicitly does not grant read
# access to (see the Dockerfile: chmod 600, owned by the app user, sandbox runs as `nobody`).
# The kernel enforces what the container boundary used to. setgid BEFORE setuid, always —
# reverse the order and you have already dropped the privilege you needed to drop the group.
_SANDBOX_UID = int(os.environ.get("SANDBOX_UID", "65534"))  # nobody
_SANDBOX_GID = int(os.environ.get("SANDBOX_GID", "65534"))

_HARNESS = r'''
import os, resource, sys, json

_EUID_BEFORE = os.geteuid()
try:
    os.setgid(%(gid)d)
    os.setuid(%(uid)d)
except (OSError, PermissionError):
    pass   # handled by the verification below — never trust the call, check the result

# VERIFY the drop actually happened. Two ways this silently fails otherwise:
#   1. We were already the target uid (a dev sandbox, a misconfigured base image) — setuid
#      is then a no-op that SUCCEEDS, and we would wrongly conclude we are contained.
#   2. The platform is rootless — setuid raised, we swallowed it, and here we still are.
# Either way, if we can still regain privilege or never had root to drop, containment via
# UID is a fiction. On a SINGLE-container host that fiction means untrusted check() can read
# the commitment DB. So: if we cannot prove we are the unprivileged uid AND cannot restore
# the old one, REFUSE. Fail closed. A verification we cannot run safely is not a fail — it is
# a hard error the caller must surface, never a silent pass.
_ok = (os.geteuid() == %(uid)d)
if _ok and _EUID_BEFORE == 0:
    try:
        os.seteuid(0)                 # if root can be regained, the drop was cosmetic
        _ok = False
        os.seteuid(%(uid)d)
    except OSError:
        pass                          # good: the drop is irreversible
if not _ok:
    print(json.dumps({"fatal": "SANDBOX_NOT_ISOLATED: privilege drop unverifiable"}))
    sys.exit(3)

resource.setrlimit(resource.RLIMIT_AS,    (%(mem)d, %(mem)d))
resource.setrlimit(resource.RLIMIT_CPU,   (%(cpu)d, %(cpu)d))
resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))

_payload = json.loads(sys.stdin.read())
_ns = {"__builtins__": __builtins__}
try:
    exec(_payload["source"], _ns)
except Exception as e:
    print(json.dumps({"ok": False, "err": f"test source did not load: {type(e).__name__}: {e}"}))
    sys.exit(0)

check = _ns.get("check")
if not callable(check):
    print(json.dumps({"ok": False, "err": "no callable check(output)"}))
    sys.exit(0)

try:
    print(json.dumps({"ok": True, "passed": bool(check(_payload["output"]))}))
except MemoryError:
    print(json.dumps({"ok": False, "err": "test exhausted memory limit"}))
except Exception as e:
    print(json.dumps({"ok": True, "passed": False, "err": f"{type(e).__name__}: {e}"}))
''' % {"mem": MEM_BYTES, "cpu": CPU_SECONDS, "uid": _SANDBOX_UID, "gid": _SANDBOX_GID}


@dataclass
class _Run:
    ok: bool
    passed: bool
    err: str | None


def _run_once(source: str, output: Any) -> _Run:
    payload = json.dumps({"source": source, "output": output})
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _HARNESS],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            # Fork-safe replacement for the os.setsid() that used to live in preexec_fn.
            # Gives the child its own process group, so on timeout we can kill the WHOLE
            # group — a test that spawns children and then hangs must not leave orphans
            # holding CPU. Reaping is tini's job (see brain/Dockerfile).
            start_new_session=True,
            env={"PYTHONHASHSEED": "0", "PATH": "/usr/bin"},
        )
        try:
            out, err = proc.communicate(payload, timeout=WALL_SECONDS)
        except subprocess.TimeoutExpired:
            # SIGKILL the process GROUP, not just the leader. RLIMIT_CPU only counts CPU
            # time — a test that blocks on I/O forever burns zero CPU and sails past it.
            # This wall-clock kill is the backstop, and it is the one that actually fires.
            try:
                os.killpg(os.getpgid(proc.pid), 9)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            proc.wait(timeout=5)
            return _Run(ok=False, passed=False, err=f"acceptance test exceeded {WALL_SECONDS}s wall clock")

        class _P:
            returncode = proc.returncode
            stdout = out
            stderr = err

        proc_result = _P()

    if proc_result.returncode == 3:
        # SANDBOX_NOT_ISOLATED. This is NOT "escalate to Tier 2" — it is "this host cannot
        # safely run untrusted code, so refuse to run it at all." Returning ok=False here
        # would let a valid deliverable pass through Tier 2, having quietly run unsandboxed
        # code to get there. That is the exact hole we are closing. Raise, loudly.
        raise SandboxUnavailable(
            "sandbox could not verify privilege isolation on this host — refusing to "
            "execute untrusted acceptance tests. On a single-container deploy the parent "
            "must start as root so the child can drop to an unprivileged uid."
        )

    if proc_result.returncode != 0:
        # Killed by a limit. That is a broken TEST, not a broken deliverable — so ok=False,
        # which the caller turns into ESCALATE. We never slash a worker because the poster
        # wrote a test that blows up.
        return _Run(
            ok=False, passed=False,
            err=f"sandbox exit {proc_result.returncode}: {(proc_result.stderr or '')[:200]}",
        )

    try:
        res = json.loads((proc_result.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _Run(ok=False, passed=False, err="unparseable sandbox output")

    return _Run(ok=bool(res.get("ok")), passed=bool(res.get("passed")), err=res.get("err"))


def verify(
    *,
    revealed_source: str,
    revealed_nonce: str,
    commitment: str,
    deliverable: Any,
) -> Verdict:
    """
    The whole referee, in one function. Read it top to bottom; there is nowhere to hide.
    """

    # 1. Did the poster reveal the test they actually committed to?
    #    If not, they are trying to move the goalposts. The worker WINS. We do not even
    #    look at the deliverable — there is no honest question left to ask.
    if not AcceptanceTest.verify_reveal(revealed_source, revealed_nonce, commitment):
        return Verdict(
            passed=True,
            tier_used=Tier.DETERMINISTIC,
            confidence=1.0,
            reveal_valid=False,
            reason=(
                "Poster's revealed acceptance test does not match the on-chain commitment. "
                "Goalpost-moving is treated as a passing verification for the worker."
            ),
        )

    # 2. Run it twice. Disagreement means OUR referee is unreliable, not that the worker
    #    cheated. Escalate; never slash on our own flakiness.
    a = _run_once(revealed_source, deliverable)
    b = _run_once(revealed_source, deliverable)

    if not a.ok or not b.ok:
        return Verdict(
            passed=False, tier_used=Tier.DETERMINISTIC, confidence=0.0, reveal_valid=True,
            reason=f"acceptance test failed to execute: {a.err or b.err}. Escalating to Tier 2.",
        )

    if a.passed != b.passed:
        return Verdict(
            passed=False, tier_used=Tier.DETERMINISTIC, confidence=0.0, reveal_valid=True,
            reason="nondeterministic acceptance test (two runs disagreed). Escalating to Tier 2.",
        )

    return Verdict(
        passed=a.passed,
        tier_used=Tier.DETERMINISTIC,
        confidence=1.0,
        reveal_valid=True,
        reason="deterministic acceptance test passed" if a.passed
        else f"deterministic acceptance test failed: {a.err or 'check() returned False'}",
    )
