# syntax=docker/dockerfile:1.7
# Hermes Agent Dockerfile — gateway/background-worker mode
# Runs the upstream NousResearch hermes-agent messaging gateway.
# No HTTP server, no EXPOSE, no HEALTHCHECK — this is a long-running worker.

# Python 3.13: several security fix versions in constraints.txt (e.g. the
# updated cryptography/aiohttp stack) require interpreter >=3.13.
FROM python:3.13-slim AS base

LABEL org.opencontainers.image.source="https://github.com/AzureAgentForge/AzureAgentForge"
LABEL org.opencontainers.image.description="Hermes AI Agent — Messaging Gateway"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ── Build stage ───────────────────────────────────────────────────────────────
FROM base AS builder

WORKDIR /build

# gcc + git are needed by some native dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY apps/hermes/src/ .

# AAF security override: pin vulnerable transitive deps to advisory fix versions
# WITHOUT editing the upstream submodule. See services/agent-runtime/constraints.txt.
COPY services/agent-runtime/constraints.txt /tmp/aaf-constraints.txt
RUN pip install --prefix=/install -c /tmp/aaf-constraints.txt ".[messaging,honcho,cron]"

# ── Production stage ─────────────────────────────────────────────────────────
FROM base AS production

WORKDIR /app

# Create non-root user
RUN groupadd --gid 1001 appgroup && \
    useradd --uid 1001 --gid appgroup --shell /bin/bash --create-home appuser

# curl + ca-certificates (needed for gws download)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy Python packages from builder
COPY --from=builder /install /usr/local

# Copy source (for gateway/, agent/, etc.)
COPY --from=builder /build .

# Parent-repo skill overrides: overlay on top of the upstream submodule copy.
# Customizations (automation headers, dynamic helper path resolution) that must
# not be pushed upstream. Skills land at /app/skills/ — same path the DELEGATE_HELPER
# resolver checks for standalone Hermes (/app/skills/playbooks/agent-delegate/...).
COPY apps/hermes/overrides/skills skills/

# Runtime directories
RUN mkdir -p /home/appuser/.hermes /home/appuser/.honcho && \
    chown -R appuser:appgroup /home/appuser

# Entry point (repo-owned, lives beside this Dockerfile; build context is repo root)
COPY --chown=appuser:appgroup services/agent-runtime/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── Google Workspace CLI (gws) ───────────────────────────────────────────────
# Binary-only install. Credentials are injected at runtime via secret mount.
RUN curl -L "https://github.com/googleworkspace/cli/releases/download/v0.22.3/google-workspace-cli-x86_64-unknown-linux-gnu.tar.gz" \
    -o /tmp/gws.tar.gz \
    && tar -xzf /tmp/gws.tar.gz --strip-components=1 -C /usr/local/bin \
    && chmod +x /usr/local/bin/gws \
    && rm /tmp/gws.tar.gz \
    && echo "✅ gws installed successfully: $(gws --version)"

USER appuser

# OPENAI_MODEL can be changed at runtime via `az containerapp update`
ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "gateway.run"]