"""Environment configuration. Secrets-as-files convention: when an env var is
absent we fall back to /secrets/<kv-secret-name> (Key Vault mounts)."""

from __future__ import annotations

import os
from pathlib import Path


def _secret(env_name: str, file_name: str, default: str | None = None) -> str | None:
    val = os.environ.get(env_name)
    if val:
        return val
    p = Path("/secrets") / file_name
    if p.exists():
        return p.read_text().strip()
    return default


def database_url() -> str:
    raw = _secret("DATABASE_URL", "platform-postgresql-connection-string")
    if not raw:
        raise RuntimeError("DATABASE_URL not configured (env or /secrets mount)")
    # Key Vault may store a SQLAlchemy-style URL; asyncpg/psql want plain postgresql://
    return raw.replace("postgresql+psycopg:", "postgresql:")


ROUTER_BASE_URL = os.environ.get("ROUTER_BASE_URL", "http://localhost:8080/v1")
CLASSIFIER_MODEL = os.environ.get("CLASSIFIER_MODEL", "gpt4o-mini")
CLASSIFIER_TIMEOUT_S = float(os.environ.get("CLASSIFIER_TIMEOUT_S", "20"))

# Plane C vector retrieval: the query is embedded through the router's
# /v1/embeddings so it lands in the same space as Honcho's document embeddings
# (an embeddings model, 1536-dim). Gated by MEMORY_VECTOR_RETRIEVAL_ENABLED.
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_TIMEOUT_S = float(os.environ.get("EMBEDDING_TIMEOUT_S", "10"))

# Honcho API base. Injected per environment; the default is the local
# docker-compose service name.
HONCHO_BASE_URL = os.environ.get("HONCHO_BASE_URL", "http://honcho:8000")

# Optional shared-secret for mutating endpoints (auth-proxy injects it).
GOVERNOR_API_KEY = _secret("GOVERNOR_API_KEY", "memory-governor-api-key")

# Planner canary allowlist: comma-separated agent slugs; empty = nobody.
PLANNER_AGENT_ALLOWLIST = {
    s.strip()
    for s in os.environ.get("PLANNER_AGENT_ALLOWLIST", "").split(",")
    if s.strip()
}

FLAG_CACHE_TTL_S = float(os.environ.get("FLAG_CACHE_TTL_S", "60"))

# Dedup guard: trigram similarity above this on same-class recent docs blocks
# persistence (the caller reconfirms the matched memory instead).
DEDUP_SIMILARITY_THRESHOLD = float(os.environ.get("DEDUP_SIMILARITY_THRESHOLD", "0.9"))
DEDUP_LOOKBACK_DAYS = int(os.environ.get("DEDUP_LOOKBACK_DAYS", "90"))

SERVICE_VERSION = "1.0.0"
