"""Task-scope lifecycle watcher — the Phase 1B scope-close hook.

Closes the "task_scoped memory never expires" gap without patching
PaperClip: every SCOPE_WATCH_INTERVAL_S the governor finds task-scoped
documents with no expires_at, asks the PaperClip API (through the
auth-proxy, with a self-minted automation JWT) whether their issue is
closed, and stamps `expires_at = now() + grace` on the ones that are.
The TTL sweeper then deletes them after the grace period (14d).

Scope ids that don't look like PaperClip issues (no UUID, no issue match)
are left alone — other scope kinds (threads, incidents) can get their own
resolvers later.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path

import httpx

from . import db

log = logging.getLogger("governor.scope_watcher")

SCOPE_WATCH_INTERVAL_S = float(os.environ.get("SCOPE_WATCH_INTERVAL_S", "300"))
TASK_SCOPE_GRACE_DAYS = float(os.environ.get("TASK_SCOPE_GRACE_DAYS", "14"))
PAPERCLIP_BASE_URL = os.environ.get("PAPERCLIP_BASE_URL", "").rstrip("/")
CLOSED_STATUSES = {"done", "cancelled", "closed", "completed"}

JWT_ISSUER = os.environ.get("PAPERCLIP_AUTOMATION_JWT_ISSUER", "automation-agent")
JWT_AUDIENCE = os.environ.get("PAPERCLIP_AUTOMATION_JWT_AUDIENCE", "paperclip-api")


def _jwt_secret() -> str | None:
    val = os.environ.get("PAPERCLIP_AUTOMATION_JWT_SECRET")
    if val:
        return val
    p = Path("/secrets/platform-paperclip-automation-jwt-secret")
    return p.read_text().strip() if p.exists() else None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def mint_jwt(secret: str, ttl_s: int = 300) -> str:
    """Minimal HS256 automation JWT matching auth-proxy verifyJwt:
    iss/aud pinned, short exp, issues:read scope only."""
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    now = int(time.time())
    payload = _b64url(
        json.dumps(
            {
                "sub": "memory-governor",
                "role": "automation",
                "scope": ["issues:read"],
                "iss": JWT_ISSUER,
                "aud": JWT_AUDIENCE,
                "iat": now,
                "exp": now + ttl_s,
            }
        ).encode()
    )
    sig = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{_b64url(sig)}"


async def _issue_status(client: httpx.AsyncClient, token: str, issue_id: str) -> str | None:
    try:
        resp = await client.get(
            f"{PAPERCLIP_BASE_URL}/api/issues/{issue_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 404:
            return "missing"
        resp.raise_for_status()
        body = resp.json()
        # PaperClip responses may nest the issue; read defensively (camelCase).
        issue = body.get("issue", body) if isinstance(body, dict) else {}
        return (issue.get("status") or "").lower() or None
    except Exception as exc:  # noqa: BLE001
        log.warning("issue lookup %s failed: %s", issue_id, exc)
        return None


async def watch_once() -> int:
    """One pass. Returns number of scopes expired."""
    if not PAPERCLIP_BASE_URL:
        return 0
    secret = _jwt_secret()
    if not secret:
        log.debug("no automation JWT secret mounted — scope watcher idle")
        return 0

    p = await db.pool()
    rows = await p.fetch(
        """SELECT DISTINCT memory_scope_id FROM documents
           WHERE memory_class = 'task_scoped'
             AND memory_scope_kind = 'task'
             AND expires_at IS NULL
             AND deleted_at IS NULL
           LIMIT 50"""
    )
    if not rows:
        return 0

    token = mint_jwt(secret)
    expired = 0
    async with httpx.AsyncClient(timeout=10) as client:
        for r in rows:
            scope_id = r["memory_scope_id"]
            status = await _issue_status(client, token, scope_id)
            if status in CLOSED_STATUSES or status == "missing":
                await p.execute(
                    """UPDATE documents
                       SET expires_at = now() + make_interval(days => $2)
                       WHERE memory_class = 'task_scoped'
                         AND memory_scope_kind = 'task'
                         AND memory_scope_id = $1
                         AND expires_at IS NULL""",
                    scope_id,
                    TASK_SCOPE_GRACE_DAYS,
                )
                await db.emit_event(
                    "memory_expire",
                    "scope-watcher",
                    {
                        "phase": "scope_close",
                        "scope_id": scope_id,
                        "issue_status": status,
                        "grace_days": TASK_SCOPE_GRACE_DAYS,
                    },
                    issue_id=scope_id,
                )
                expired += 1
    return expired


async def run_forever() -> None:
    log.info("scope watcher starting (interval %ss)", SCOPE_WATCH_INTERVAL_S)
    while True:
        try:
            n = await watch_once()
            if n:
                log.info("scope watcher: %d closed scopes stamped with grace expiry", n)
        except Exception:  # noqa: BLE001
            log.exception("scope watcher pass failed")
        await asyncio.sleep(SCOPE_WATCH_INTERVAL_S)
