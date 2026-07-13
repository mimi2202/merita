# Merita ASP — single container, because free tiers give you one.
#
# The two-container topology (sandbox isolated by a network + mount boundary) is GONE here.
# So the isolation has to be rebuilt with the tools that remain: UIDs and file permissions.
# Read the chmod lines below like they are load-bearing, because they are.

FROM python:3.12-slim

# tini reaps zombies. Without an init, a sandbox subprocess killed by RLIMIT_CPU becomes a
# zombie that PID 1 never reaps. Do that a few hundred times and the container hits its pid
# limit and the referee stops working — at 3am, mid-review, silently.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
      "fastmcp>=2.10" "uvicorn[standard]" httpx pydantic

COPY brain/merita ./merita
COPY asp ./asp

RUN python -m compileall -q merita asp

# ── THE ISOLATION, such as it is ────────────────────────────────────────────
# `merita` runs the server. `nobody` (65534) runs the sandboxed acceptance tests.
#
# The commitment DB is chmod 600, owned by `merita`. A malicious poster's check(), running
# as nobody, calls open('/data/merita.db') and gets EACCES from the kernel. Without this,
# that check() reads every other poster's sealed acceptance test and commit-reveal is over
# for the whole marketplace — not one job, ALL of them, silently.
#
# The env is scrubbed for the child too (see tier1.py), so OKX_API_SECRET is invisible to it.
RUN useradd -r -u 10001 -m -d /home/merita merita \
    && mkdir -p /data \
    && chown merita:merita /data \
    && chmod 700 /data

ENV PYTHONUNBUFFERED=1 \
    MERITA_DB=/data/merita.db \
    SANDBOX_UID=65534 \
    SANDBOX_GID=65534 \
    PORT=8080

# NOTE: we do NOT `USER merita` here. The parent must start as root so the sandbox child can
# setuid(65534) — you cannot drop to a *different* unprivileged user from an unprivileged one.
# The server itself never touches untrusted input with root's hands; the only code that runs
# as root is ours, and the only code that runs untrusted is already down at `nobody`.
# If that trade makes you uneasy, good. It should. On a paid tier, go back to two containers.

EXPOSE 8080
ENTRYPOINT ["tini", "-g", "--"]
CMD ["python", "-m", "asp.server"]
