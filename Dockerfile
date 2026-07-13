# Merita ASP — single container, because free tiers give you one.
#
# The two-container topology (sandbox isolated by a network + mount boundary) is not
# available here. So isolation is rebuilt with the tools that remain: UIDs and permissions.
# The `useradd` and the setuid in tier1.py are load-bearing. Read them before you touch them.

FROM python:3.12-slim

# tini reaps zombies. Without an init, a sandbox subprocess killed by RLIMIT_CPU becomes a
# zombie PID 1 never reaps. A few hundred of those and the container hits its pid limit and
# the referee silently stops working — at 3am, mid-review.
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
      "fastmcp>=3.4,<4" "uvicorn[standard]" httpx pydantic \
      "psycopg[binary]" psycopg-pool

COPY brain/merita ./merita
COPY asp ./asp

RUN python -m compileall -q merita asp

# ── THE ISOLATION ───────────────────────────────────────────────────────────
# The server runs as root; the sandboxed acceptance tests drop to `nobody` (65534) and PROVE
# the drop is irreversible before executing a single line (see tier1.py). Untrusted code
# therefore cannot read anything the server can — including, critically, other posters'
# sealed acceptance tests.
#
# The commitment DB is now remote Postgres, so there is no local file to protect. But the
# privilege drop stays: it is what stops a hostile check() from reading the process
# environment through /proc, or anything else we haven't thought of. Defence that only
# covers the threats you enumerated is not defence.
ENV PYTHONUNBUFFERED=1 \
    SANDBOX_UID=65534 \
    SANDBOX_GID=65534 \
    PORT=8080

EXPOSE 8080
ENTRYPOINT ["tini", "-g", "--"]
CMD ["python", "-m", "asp.server"]