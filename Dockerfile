# syntax=docker/dockerfile:1.7
#
# local-budget runtime image. Single-stage: budget's frontend is a single
# committed static file (src/local_budget/web/static/index.html), nothing to build.
#
# The container serves the DETERMINISTIC dashboard ONLY (`budget serve`) — it runs
# NO Claude inference. The conversational agent now lives in a Claude Code session
# via the stdio MCP server (`uv run budget-mcp` + the budget skills), NOT in this
# image — so it needs no Node, no `claude` CLI, no Agent SDK, no OAuth token.

FROM python:3.12-slim AS runtime

# System deps: curl (healthcheck + the uv installer) + ca-certificates (HTTPS).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv into a system path so the non-root user picks it up too.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Non-root user (uid 1001). Defense-in-depth — a container holding a full-PII DB
# on its filesystem should never run as root.
RUN useradd --create-home --shell /bin/bash --uid 1001 app

WORKDIR /app

# Project metadata + lock first so the deps layer caches independent of
# source-only changes. README.md is referenced by pyproject.toml's `readme`.
COPY --chown=app:app pyproject.toml uv.lock README.md ./
COPY --chown=app:app src/ ./src/
RUN chown app:app /app && su app -c "uv sync --frozen --no-dev"

# Container-only env. The host CLI keeps its existing defaults (Path-relative
# data/, 127.0.0.1 serve); these only kick in here.
#  - LOCAL_BUDGET_BRIEFINGS_DIR=/data/briefings keeps briefings under the writable
#    bind mount (the default /briefings would be unmounted + read-only). [CORR-1]
#  - LOCAL_BUDGET_NO_INTAKE=1 disables the raw-file ingestion routes in-container
#    (the I2 boundary); the dashboard stays interactive.
ENV LOCAL_BUDGET_HOST=0.0.0.0 \
    LOCAL_BUDGET_DATA_DIR=/data \
    LOCAL_BUDGET_BRIEFINGS_DIR=/data/briefings \
    LOCAL_BUDGET_NO_INTAKE=1 \
    PYTHONUNBUFFERED=1

# Pre-create the volume mount points so the bind-mounts / named volume attach
# cleanly on first run, owned by `app` so writes succeed.
RUN mkdir -p /data /data/briefings \
    && chown -R app:app /data

USER app

EXPOSE 8770

# Liveness probe — /health is unauthenticated and touches no DB. Traefik has its
# own healthcheck in the compose file; this one is for `docker ps` visibility.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8770/health || exit 1

# Run the console script the venv installed directly — NOT `uv run`. With a
# read_only rootfs, `uv run` tries to create a cache under /home/app/.cache and
# fails; the venv binary needs no cache and is the leaner runtime path anyway.
CMD ["/app/.venv/bin/budget", "serve", "--host", "0.0.0.0", "--port", "8770"]
