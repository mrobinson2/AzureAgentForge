"""Teams bridge (AzureAgentForge) — Microsoft Teams chat surface.

A minimal Bot Framework messaging endpoint that bridges Microsoft Teams to the
agent platform, at parity with the Discord plugin and the Telegram gateway: an
inbound Teams message becomes a PaperClip issue routed to the Orchestrator, and
the agent's reply returns to the channel as an Adaptive Card. Disabled by
default; enable with the `teams_enabled` Terraform variable.

The parse / payload / card helpers are pure and unit-tested offline. The
PaperClip POST is injectable (`issue_poster`) so the endpoint is testable
without a live API, and the endpoint NEVER returns 5xx to Bot Framework (that
triggers an aggressive retry storm) — failures are acked with a body flag.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Optional

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PAPERCLIP_API_URL = os.getenv("PAPERCLIP_API_URL", "http://paperclip:3000")
PAPERCLIP_COMPANY_ID = os.getenv("PAPERCLIP_COMPANY_ID", "")
PAPERCLIP_API_KEY = os.getenv("PAPERCLIP_API_KEY", "")
ORCHESTRATOR_AGENT_ID = os.getenv("ORCHESTRATOR_AGENT_ID", "")

# ── Bot Framework JWT validation ─────────────────────────────────────────────
# Inbound Teams traffic arrives via the Azure Bot Service with an
# `Authorization: Bearer <JWT>` issued by Bot Framework. Validate it before
# acting on the activity: RS256 signature (keys from the Bot Framework JWKS),
# issuer, audience (= this bot's Microsoft App ID), and expiry. Enforced when
# TEAMS_APP_ID is set; with it unset the endpoint is unauthenticated (local/dev
# only) and logs a warning. TEAMS_AUTH_DISABLED=1 is an explicit local escape.
TEAMS_APP_ID = os.getenv("TEAMS_APP_ID", "")
TEAMS_AUTH_DISABLED = os.getenv("TEAMS_AUTH_DISABLED", "").lower() in ("1", "true", "yes")
BOT_FRAMEWORK_ISSUER = os.getenv("BOT_FRAMEWORK_ISSUER", "https://api.botframework.com")
BOT_FRAMEWORK_OPENID = os.getenv(
    "BOT_FRAMEWORK_OPENID_CONFIG",
    "https://login.botframework.com/v1/.well-known/openidconfiguration",
)

app = FastAPI(title="teams-bridge", version="1.0.0")


class AuthError(Exception):
    """Raised when an inbound request fails Bot Framework JWT validation."""


def bearer_token(authorization: str) -> Optional[str]:
    """Extract the token from an `Authorization: Bearer <token>` header."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer "):].strip()
    return token or None


def verify_jwt(token: str, app_id: str, signing_key: Any, *,
               issuer: str = BOT_FRAMEWORK_ISSUER, leeway: int = 300) -> dict:
    """Validate a Bot Framework JWT given its signing key. Pure (no network) —
    checks RS256 signature, issuer, audience (= the bot's App ID), and expiry,
    and requires those claims be present. Raises AuthError on any failure. The
    JWKS fetch that resolves the key lives in `_signing_key_for`."""
    try:
        return jwt.decode(
            token, signing_key, algorithms=["RS256"], audience=app_id,
            issuer=issuer, leeway=leeway,
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.PyJWTError as exc:  # bad signature / aud / iss / expired / malformed
        raise AuthError(str(exc)) from exc


_jwks_client: Any = None


def _signing_key_for(token: str) -> Any:
    """Resolve the RSA signing key for a token from the Bot Framework JWKS
    (cached PyJWKClient). Network — not exercised by the offline tests."""
    global _jwks_client
    if _jwks_client is None:
        meta = httpx.get(BOT_FRAMEWORK_OPENID, timeout=10.0).json()
        _jwks_client = jwt.PyJWKClient(meta["jwks_uri"])
    return _jwks_client.get_signing_key_from_jwt(token).key


def authenticate(authorization: str) -> None:
    """Enforce Bot Framework auth on an inbound request when configured. No-op
    (with a warning) when TEAMS_APP_ID is unset, or when TEAMS_AUTH_DISABLED."""
    if TEAMS_AUTH_DISABLED:
        return
    if not TEAMS_APP_ID:
        print("[teams-bridge] WARNING: TEAMS_APP_ID unset — /api/messages is "
              "UNAUTHENTICATED. Set TEAMS_APP_ID (the bot's Microsoft App ID) "
              "before exposing this endpoint.")
        return
    token = bearer_token(authorization)
    if not token:
        raise AuthError("missing bearer token")
    verify_jwt(token, TEAMS_APP_ID, _signing_key_for(token))


def parse_activity(body: Any) -> Optional[dict]:
    """Pull the routable bits from a Bot Framework activity. Returns None for
    anything that isn't a non-empty `message` (typing, conversationUpdate, …),
    so those are silently acked and ignored."""
    if not isinstance(body, dict) or body.get("type") != "message":
        return None
    text = (body.get("text") or "").strip()
    if not text:
        return None
    frm = body.get("from") or {}
    conv = body.get("conversation") or {}
    return {
        "text": text,
        "user": frm.get("name") or frm.get("id") or "teams-user",
        "conversation_id": conv.get("id") or "",
        "service_url": body.get("serviceUrl") or "",
    }


def build_issue_payload(parsed: dict, company_id: str, agent_id: str = "") -> dict:
    """The camelCase PaperClip issue an inbound Teams message creates (camelCase
    matters — the API's validation drops snake_case fields)."""
    payload: dict = {
        "title": parsed["text"][:120],
        "description": (
            f"{parsed['text']}\n\n"
            f"_via Microsoft Teams — {parsed['user']} "
            f"(conversation `{parsed['conversation_id']}`)_"
        ),
        "status": "todo",
        "companyId": company_id,
        "metadata": {"surface": "teams", "conversationId": parsed["conversation_id"]},
    }
    if agent_id:
        payload["assigneeId"] = agent_id
    return payload


def build_card(text: str) -> dict:
    """A minimal Adaptive Card the bridge posts back to the Teams channel."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "body": [{"type": "TextBlock", "text": text, "wrap": True}],
                },
            }
        ],
    }


def _post_issue(payload: dict) -> int:
    """Default issue poster — POST to the PaperClip API. Returns the status code."""
    headers = {"Content-Type": "application/json"}
    if PAPERCLIP_API_KEY:
        headers["Authorization"] = f"Bearer {PAPERCLIP_API_KEY}"
    resp = httpx.post(
        f"{PAPERCLIP_API_URL}/api/issues", json=payload, headers=headers, timeout=10.0
    )
    return resp.status_code


# Injectable for tests; production uses _post_issue.
issue_poster: Callable[[dict], int] = _post_issue


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "surface": "teams"}


@app.post("/api/messages")
async def messages(request: Request) -> JSONResponse:
    # Authenticate the Bot Framework caller first (401, not 5xx — Bot Framework
    # treats 401 as "re-auth", and never retry-storms on it the way it does 5xx).
    try:
        authenticate(request.headers.get("authorization", ""))
    except AuthError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    body = await request.json()
    parsed = parse_activity(body)
    if parsed is None:
        return JSONResponse({"ignored": True}, status_code=200)
    payload = build_issue_payload(parsed, PAPERCLIP_COMPANY_ID, ORCHESTRATOR_AGENT_ID)
    try:
        code = issue_poster(payload)
    except Exception:  # noqa: BLE001 — never 5xx to Bot Framework; it retry-storms
        return JSONResponse({"queued": False, "error": "bridge_post_failed"}, status_code=200)
    return JSONResponse({"queued": 200 <= code < 300, "issueStatus": code}, status_code=200)
