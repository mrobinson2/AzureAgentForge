# Honcho Memory Service Dockerfile
# Follows the upstream plastic-labs/honcho build pattern (uv, Python 3.13, src layout).
# Port 8000 matches the upstream default. Health check uses /openapi.json which
# FastAPI always serves — no custom /health route needed.

FROM ghcr.io/astral-sh/uv:0.11.24 AS uv

FROM python:3.13-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# ── Build stage: install deps via uv into a venv ─────────────────────────────
FROM base AS builder

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Sync only dependencies first (no project install) for better layer caching
COPY apps/honcho/src/uv.lock apps/honcho/src/pyproject.toml /app/
RUN uv sync --frozen --no-install-project --no-group dev

# Copy source needed for the project install step
COPY apps/honcho/src/src/ /app/src/
RUN uv sync --frozen --no-group dev

# ── Production stage ──────────────────────────────────────────────────────────
FROM base AS production

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app

RUN groupadd --system app && \
    adduser --system --group app && \
    apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Copy the full venv and app source from builder
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app apps/honcho/src/src/ /app/src/
COPY --chown=app:app apps/honcho/src/migrations/ /app/migrations/
COPY --chown=app:app apps/honcho/src/alembic.ini /app/alembic.ini

# Copy our custom entrypoint that runs alembic before starting the server
COPY --chown=app:app apps/honcho/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

RUN mkdir -p /tmp/honcho && chown -R app:app /tmp/honcho

# venv takes precedence; PYTHONPATH lets `src.X` imports resolve from /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app"

USER app

EXPOSE 8000

# /openapi.json is always present on a FastAPI app — no custom /health needed
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/openapi.json')" || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["fastapi", "run", "--host", "0.0.0.0", "src/main.py"]
