# Memory Governor — governed memory layer (see docs/design/memory-system.md).
# Runs alongside the model-router sidecar; the governor calls it on
# localhost:8080 for classification (an economy tier, budget-enforced).

FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

FROM base AS builder
WORKDIR /build
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt

FROM base AS production
WORKDIR /app

RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

COPY --from=builder /install /usr/local
COPY --chown=appuser:appgroup src/governor /app/governor
# The schema overlay the governor applies on startup. governor/migrate.py
# resolves MIGRATIONS_DIR relative to its own file — /app/governor/migrate.py →
# parents[2] → "/" — so the SQL must land at /migrations, mirroring the repo
# layout where it sits beside src/. Without this the image ships no migrations,
# apply() finds nothing, and every flag lookup fails on a missing feature_flags
# table while the boot log still says the schema is up to date.
COPY --chown=appuser:appgroup migrations /migrations

USER appuser

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/healthz')" || exit 1

CMD ["uvicorn", "governor.main:app", "--host", "0.0.0.0", "--port", "8090"]
