# ONE image, TWO roles: `brain` (orchestrator) and `verifier` (blast chamber).
#
# Deliberate. If they were separate images they would drift — someone patches a dependency
# in one and not the other, and six weeks later the sandbox the brain THINKS it is talking
# to is not the sandbox that exists. Same bytes, different command, different capabilities.
# The privilege difference lives entirely in docker-compose.yml, where it is auditable in
# one screenful, rather than smeared across two Dockerfiles.

FROM python:3.12-slim AS base

# tini: without an init, a wedged sandbox subprocess becomes a zombie and PID 1 (uvicorn)
# never reaps it. Do that a few hundred times and the container hits its pids_limit and the
# referee stops working — at 3am, on mainnet, mid-settlement. `tini -g` reaps the whole
# process group, which is exactly why tier1.py calls os.setsid().
RUN apt-get update && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependency layer first, so editing merita/*.py doesn't reinstall FastAPI every build.
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx pydantic

COPY merita ./merita

# Precompile. Matters for the verifier: it runs read_only, so it CANNOT write .pyc at
# runtime — without this it re-parses every module on every cold start, forever.
RUN python -m compileall -q merita

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["tini", "-g", "--"]
CMD ["uvicorn", "merita.main:app", "--host", "0.0.0.0", "--port", "8000"]
