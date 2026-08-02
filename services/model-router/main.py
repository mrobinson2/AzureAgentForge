"""
Model Router — Azure AI Foundry Multi-Model Gateway
====================================================
Routes OpenAI-compatible requests to Azure AI Foundry models.
Supports explicit model tiers (gpt4o-mini, phi4) with budget tracking,
plus a passthrough mode for any model deployed in the Foundry project.

Passthrough: if the request's "model" field doesn't match a known tier,
the router forwards it directly to Azure AI Foundry using the shared
project API key. This means deploying a new model in Foundry makes it
instantly available — no router code changes needed.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import traceback
from collections import defaultdict
from datetime import date
from typing import Any

import litellm
from anthropic import AsyncAnthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

# Local module (same directory): pure decision logic for acting budget
# enforcement (A6) — see the "Acting budget enforcement" section below.
import budget_enforcement

# Local modules (same directory): bounded call trace + waste-pattern decision
# logic — see the "Flight Recorder + Waste Breakers" section below.
import flight_recorder
import waste_breakers

# Local modules (same directory): fail-closed resilience pack — three-state
# circuit breakers keyed per upstream credential, plus the scoped paid-action
# kill switch. See the "Fail-Closed Resilience Pack" section below and
# docs/design/router-resilience-pack.md.
import circuit_breaker
import kill_switch

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("router")

# Respect LITELLM_LOG env var instead of deprecated litellm.set_verbose
_log_level = os.environ.get("LOG_LEVEL", "info").lower()
if _log_level == "debug":
    os.environ.setdefault("LITELLM_LOG", "DEBUG")

app = FastAPI(title="Model Router", version="1.3.0")

# ─── Security: API Key Authentication ────────────────────────────────────────
# All /v1/* endpoints require a matching Bearer token. FAIL CLOSED (aaf-0005): if
# ROUTER_API_KEY is unset the router refuses every request (503) rather than
# silently serving unauthenticated traffic. Internal services pass the key.
# ⚠️ DEPLOY ORDER: provision ROUTER_API_KEY (and align every in-mesh caller to
# send it) BEFORE rolling out this build, or live model calls will 503. Treat the
# cutover like a key rotation.
_ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY", "")


def _verify_auth(request: Request) -> None:
    """Require a valid Bearer token OR x-api-key. Fail closed when unconfigured
    (aaf-0005).

    Two header schemes carry the same single ROUTER_API_KEY:
      - `Authorization: Bearer <key>` — OpenAI-style clients (LiteLLM, the
        OpenAI SDK, in-mesh services).
      - `x-api-key: <key>` — Anthropic-native clients. The Anthropic SDK sends
        x-api-key (not Authorization) for any non-anthropic.com base_url, so
        Hermes's `api_mode: anthropic_messages` transport hits /v1/messages with
        x-api-key only. Before this was accepted, every agent model call on the
        cache-friendly Messages path died with 401 while /health stayed green —
        exactly the silent stack-shape breakage the agent-loop canary smoke
        (scripts/smoke-canary.sh) exists to catch.
    """
    if not _ROUTER_API_KEY:
        # Fail CLOSED — an unset key must mean "down", never "open".
        raise HTTPException(
            status_code=503,
            detail="Router authentication not configured (ROUTER_API_KEY unset)",
        )
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    else:
        token = (request.headers.get("x-api-key") or "").strip()
        if not token:
            raise HTTPException(
                status_code=401,
                detail="Missing Authorization header (Bearer) or x-api-key",
            )
    # Constant-time comparison over fixed-length digests to prevent timing attacks.
    expected = hashlib.sha256(_ROUTER_API_KEY.encode()).digest()
    provided = hashlib.sha256(token.encode()).digest()
    if not hmac.compare_digest(expected, provided):
        raise HTTPException(status_code=403, detail="Invalid API key")


# ─── Security: Rate Limiting ────────────────────────────────────────────────
# Simple sliding-window rate limiter. Limits requests per client IP.
_RATE_LIMIT_RPM = int(os.environ.get("RATE_LIMIT_RPM", "60"))  # requests per minute
_rate_windows: dict[str, list[float]] = defaultdict(list)


def _check_rate_limit(request: Request) -> None:
    """Enforce per-IP rate limiting."""
    if _RATE_LIMIT_RPM <= 0:
        return  # Disabled
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = _rate_windows[client_ip]
    # Prune entries older than 60 seconds
    cutoff = now - 60.0
    _rate_windows[client_ip] = [t for t in window if t > cutoff]
    window = _rate_windows[client_ip]
    if len(window) >= _RATE_LIMIT_RPM:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    window.append(now)


# ─── Security: Input Validation ──────────────────────────────────────────────
_MAX_MESSAGES = int(os.environ.get("MAX_MESSAGES", "200"))
_MAX_BODY_TOKENS = int(os.environ.get("MAX_BODY_TOKENS", "200000"))
_MAX_MODEL_NAME_LEN = 128


def _validate_request(body: dict) -> None:
    """Validate request body structure and bounds."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="Request must include non-empty 'messages'")
    if len(messages) > _MAX_MESSAGES:
        raise HTTPException(status_code=400, detail=f"Too many messages (max {_MAX_MESSAGES})")
    for msg in messages:
        if not isinstance(msg, dict) or "role" not in msg:
            raise HTTPException(status_code=400, detail="Each message must be a dict with 'role'")
    model = body.get("model", "")
    if isinstance(model, str) and len(model) > _MAX_MODEL_NAME_LEN:
        raise HTTPException(status_code=400, detail="Model name too long")
    temp = body.get("temperature")
    if temp is not None and (not isinstance(temp, (int, float)) or temp < 0 or temp > 2):
        raise HTTPException(status_code=400, detail="Temperature must be between 0 and 2")
    max_tokens = body.get("max_tokens")
    if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens < 1):
        raise HTTPException(status_code=400, detail="max_tokens must be a positive integer")

# ─── Key Vault unset-secret sentinel ─────────────────────────────────────────
# scripts/seed-keyvault.sh seeds this non-empty placeholder for any external
# secret you did not provide — `az keyvault secret set` rejects an empty value,
# so an unconfigured tier's *_BASE_URL / *_API_KEY arrives here as "__unset__"
# rather than "". Treat it (and blank/whitespace) as "not configured" so the
# tier is skipped, not registered against a bogus endpoint that 5xxs at request
# time. Keep in sync with PLACEHOLDER_VALUE in scripts/seed-keyvault.sh.
_KV_UNSET_SENTINEL = "__unset__"


def _tier_env(name: str, default: str = "") -> str:
    """Read a tier-config env var, normalizing the Key Vault unset-placeholder
    and blank/whitespace to "" so the truthy tier-gating below skips it."""
    val = os.environ.get(name, default)
    if val is None:
        return ""
    val = val.strip()
    return "" if val == _KV_UNSET_SENTINEL else val


# ─── GPT-4o-mini Configuration (primary) ─────────────────────────────────────
# Primary tier activates when either GPT4O_BASE_URL/GPT4O_API_KEY or
# AZURE_FOUNDRY_ENDPOINT/AZURE_FOUNDRY_API_KEY are set (the latter are the
# compose-level vars so `docker compose up` with Foundry creds brings it up).
# A placeholder/blank in the GPT4O_* pair falls through to AZURE_FOUNDRY_* via
# the `or` chain (and the two are aliased in docker-compose.yml).
_GPT4O_BASE_URL = _tier_env("GPT4O_BASE_URL") or _tier_env("AZURE_FOUNDRY_ENDPOINT")
_GPT4O_API_KEY = _tier_env("GPT4O_API_KEY") or _tier_env("AZURE_FOUNDRY_API_KEY")

MODELS: dict[str, dict[str, Any]] = {}

if _GPT4O_BASE_URL and _GPT4O_API_KEY:
    MODELS["gpt4o-mini"] = {
        "litellm_model": "openai/gpt-4o-mini",
        "api_base": _GPT4O_BASE_URL,
        "api_key": _GPT4O_API_KEY,
        "daily_budget": float(os.environ.get("GPT4O_DAILY_BUDGET_USD", "5.00")),
        "max_tokens": int(os.environ.get("GPT4O_MAX_TOKENS", "4096")),
        "context_limit": 128000,
        "timeout_seconds": int(os.environ.get("MODEL_TIMEOUT_SECONDS", "30")),
        "supports_tools": True,
    }
else:
    log.info("tier gpt4o-mini not configured (GPT4O_BASE_URL/GPT4O_API_KEY or AZURE_FOUNDRY_ENDPOINT/AZURE_FOUNDRY_API_KEY); skipping")

_PHI_BASE_URL = _tier_env("PHI_BASE_URL")
_PHI_API_KEY = _tier_env("PHI_API_KEY")
if _PHI_BASE_URL and _PHI_API_KEY:
    MODELS["phi4"] = {
        "litellm_model": f"openai/{os.environ.get('PHI_MODEL', 'Phi-4')}",
        "api_base": _PHI_BASE_URL,
        "api_key": _PHI_API_KEY,
        "daily_budget": float(os.environ.get("PHI_DAILY_BUDGET_USD", "0.50")),
        "max_tokens": int(os.environ.get("PHI_MAX_TOKENS", "2048")),
        "context_limit": 16384,
        "timeout_seconds": int(os.environ.get("MODEL_TIMEOUT_SECONDS", "30")),
        "supports_tools": False,
    }
else:
    log.info("tier phi4 not configured (PHI_BASE_URL/PHI_API_KEY); skipping")

# ─── Foundry tiers wired from per-deployment env vars ────────────────────────
# Each Foundry deployment has its own URL + API key (different project per
# model family). Without these explicit entries, requests for these models
# would fall through to the passthrough path and hit the gpt-4o-mini project's
# endpoint with the wrong deployment name — the upstream returns "deployment
# does not exist" and the router 502s.
#
# Each block is a no-op if its env vars are absent (clean fork support).
# Registration uses the *deployment name* as the MODELS key so explicit
# `model=<deployment-name>` requests resolve directly via select_tier.

def _register_foundry_tier(
    env_prefix: str,
    *,
    default_budget: float,
    default_max_tokens: int = 4096,
    context_limit: int = 128000,
    supports_tools: bool = True,
    fallback: list[str] | None = None,
    litellm_prefix: str = "openai",
) -> None:
    """Register a Foundry-deployed model tier from env vars.

    litellm_prefix selects the LiteLLM provider integration:
      - "openai"     → OpenAI Chat Completion API at <base>/chat/completions.
                       Works for gpt-4o-mini, Phi, grok, Kimi, gpt-5-nano —
                       all the OpenAI-compat deployments on Foundry.
      - "anthropic"  → Anthropic Messages API at <base>/v1/messages, used for
                       Claude models on Foundry. The deployment-side endpoint
                       lives at <foundry-host>/anthropic, *not* /openai/v1.
                       LiteLLM handles the request/response shape translation
                       so the rest of the router (which speaks OpenAI Chat
                       Completion to its callers) is unaffected.

    If <PREFIX>_BASE_URL still contains the `/openai/v1/` suffix (consistent
    KV-secret format across providers), we rewrite to `/anthropic` for the
    Anthropic prefix automatically.
    """
    base_url = _tier_env(f"{env_prefix}_BASE_URL")
    api_key = _tier_env(f"{env_prefix}_API_KEY")
    deployment = _tier_env(f"{env_prefix}_MODEL")
    if not (base_url and api_key and deployment):
        log.info(
            "Foundry tier %s_* not configured — env vars missing, skipping",
            env_prefix,
        )
        return

    if litellm_prefix == "anthropic":
        # Foundry's Anthropic inference API lives at /anthropic on the same
        # AI Services host. KV secrets are stored with /openai/v1/ for
        # consistency; rewrite. Result feeds LiteLLM as api_base; LiteLLM
        # appends /v1/messages internally.
        api_base = re.sub(r"/openai/v1/?$", "/anthropic", base_url.rstrip("/"))
    else:
        api_base = base_url

    MODELS[deployment] = {
        "litellm_model": f"{litellm_prefix}/{deployment}",
        "api_base": api_base,
        "api_key": api_key,
        "daily_budget": float(
            os.environ.get(f"{env_prefix}_DAILY_BUDGET_USD", str(default_budget))
        ),
        "max_tokens": int(
            os.environ.get(f"{env_prefix}_MAX_TOKENS", str(default_max_tokens))
        ),
        "context_limit": context_limit,
        "timeout_seconds": int(os.environ.get("MODEL_TIMEOUT_SECONDS", "60")),
        "supports_tools": supports_tools,
    }
    # Auto-attach fail-soft fallback (registered after _FALLBACK_PREFERENCE
    # is declared below — see the post-init loop).
    MODELS[deployment]["_pending_fallback"] = fallback or ["gpt4o-mini"]
    log.info(
        "registered Foundry tier=%s deployment=%s endpoint=%s litellm_model=%s",
        env_prefix.lower(), deployment, api_base, MODELS[deployment]["litellm_model"],
    )


# Claude deployments on Foundry only expose the Anthropic Messages API
# (chatCompletion capability flag is misleading; OpenAI Chat Completion
# returns "Requested API is currently not supported"). LiteLLM's
# `anthropic/<model>` provider with custom api_base does NOT correctly
# dispatch to Foundry's /anthropic endpoint — every call returns
# `OpenAIException - API deployment for this resource does not exist`
# and silently falls back to gpt4o-mini, downgrading Orchestrator and
# Telegram-via-claude without visible signal. Bypass LiteLLM for any
# `anthropic/`-prefixed tier and call the Anthropic SDK directly against
# the Foundry Anthropic endpoint. See _call_anthropic_direct below.
_register_foundry_tier("CLAUDE", default_budget=0.25, default_max_tokens=4096, litellm_prefix="anthropic")
_register_foundry_tier("KIMI", default_budget=0.25, default_max_tokens=4096)
_register_foundry_tier("GROK", default_budget=2.00, default_max_tokens=4096)

# ─── Ollama (edge) tiers — OPTIONAL ──────────────────────────────────────
# When OLLAMA_BASE_URL is set, register each model in OLLAMA_MODELS as a
# "<model>-local" tier with fallback to gpt4o-mini if the edge host is
# unreachable. When unset (the default Azure-only deployment), the router
# behaves identically to before — no Ollama awareness, no local-host dependency.
#
# This is the toggle for someone forking the repo who wants to host the
# whole platform in Azure: leave OLLAMA_BASE_URL unset and you get a clean
# Foundry-only stack. To enable: see docs/runbooks/mac-edge-platform-side.md.
_OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "")
_OLLAMA_MODELS = os.environ.get("OLLAMA_MODELS", "")
_OLLAMA_FALLBACK_TIER = os.environ.get("OLLAMA_FALLBACK_TIER", "gpt4o-mini")

if _OLLAMA_BASE_URL and _OLLAMA_MODELS:
    for _model_tag in [m.strip() for m in _OLLAMA_MODELS.split(",") if m.strip()]:
        # Tier name: take the part before the colon, append -local.
        # phi4 → phi4-local; qwen2.5:14b → qwen2.5-local; llama3.2:3b → llama3.2-local
        _tier_name = _model_tag.split(":")[0] + "-local"
        MODELS[_tier_name] = {
            # OpenAI-compatible endpoint at /v1 (Ollama exposes one natively)
            "litellm_model": f"openai/{_model_tag}",
            "api_base": _OLLAMA_BASE_URL,
            # Ollama doesn't authenticate but litellm requires a non-empty key
            "api_key": os.environ.get("OLLAMA_API_KEY", "ollama"),
            # Local inference is free at the token level. Set high so
            # is_over_budget never trips on a per-day basis.
            "daily_budget": float(os.environ.get("OLLAMA_DAILY_BUDGET_USD", "1000.0")),
            "max_tokens": int(os.environ.get("OLLAMA_MAX_TOKENS", "2048")),
            "context_limit": int(os.environ.get("OLLAMA_CONTEXT_LIMIT", "32768")),
            "timeout_seconds": int(os.environ.get("OLLAMA_TIMEOUT_SECONDS", "120")),
            # Tool calling on Ollama is per-model and uneven; safer to disable.
            # Agents that need tools should request a Foundry tier instead.
            "supports_tools": False,
            "is_ollama": True,
        }
        log.info(
            "registered Ollama tier=%s model=%s endpoint=%s fallback=%s",
            _tier_name, _model_tag, _OLLAMA_BASE_URL, _OLLAMA_FALLBACK_TIER,
        )
else:
    log.info("Ollama integration disabled (OLLAMA_BASE_URL or OLLAMA_MODELS unset)")

# Retry config for transient upstream failures (cold-start, rate-limit bursts)
_MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "2"))
_RETRY_DELAY = float(os.environ.get("RETRY_DELAY_SECONDS", "0.5"))

# ─── Token Estimation & Context Checks ───────────────────────────────────────
def _estimate_tokens(messages: list[dict]) -> int:
    try:
        return litellm.token_counter(model="gpt-4o-mini", messages=messages)
    except Exception:
        return sum(len(str(m.get("content", ""))) // 3 for m in messages)


def _fits_model(tier: str, estimated_input: int, requested_max: int) -> bool:
    cfg = MODELS[tier]
    effective_output = min(requested_max, cfg["max_tokens"])
    return (estimated_input + effective_output) <= cfg["context_limit"]


_FALLBACK_PREFERENCE = {
    "gpt4o-mini": ["phi4"],
    "phi4": [],
}

# Wire Ollama tiers' fallback to gpt4o-mini so the edge host being offline
# (sleep, OS update, ISP outage) silently degrades to Foundry instead of
# breaking the agent. Built lazily so it only registers tiers that actually
# exist in MODELS (no-op when Ollama integration is disabled).
for _t in list(MODELS.keys()):
    if MODELS[_t].get("is_ollama"):
        _FALLBACK_PREFERENCE[_t] = [_OLLAMA_FALLBACK_TIER] if _OLLAMA_FALLBACK_TIER in MODELS else []

# Drain pending fallbacks left by _register_foundry_tier(). Foundry tiers
# fail-soft to gpt4o-mini by default so a transient Foundry outage doesn't
# brick the dependent agents — they downgrade and complete instead of 502ing.
for _t in list(MODELS.keys()):
    pending = MODELS[_t].pop("_pending_fallback", None)
    if pending is not None:
        _FALLBACK_PREFERENCE[_t] = [f for f in pending if f in MODELS]


def _build_fallback_chain(tier: str, estimated_input: int, requested_max: int) -> list[str]:
    chain = list(_FALLBACK_PREFERENCE.get(tier, []))
    # Passthrough tiers are registered ephemerally by select_tier() when a
    # caller asks for an unknown Foundry deployment. They have no
    # _FALLBACK_PREFERENCE entry, so without this they'd 502 on first failure.
    # Adding gpt4o-mini as fail-soft keeps the request completable, with a
    # clear log line ("passthrough fallback") to surface the misroute.
    cfg = MODELS.get(tier, {})
    if cfg.get("passthrough") and "gpt4o-mini" not in chain and tier != "gpt4o-mini":
        chain.append("gpt4o-mini")
    return [
        t
        for t in chain
        if _fits_model(t, estimated_input, requested_max)
    ]


# ─── Budget Tracking ──────────────────────────────────────────────────────────
_budget_date: str = ""
_spend: dict[str, float] = defaultdict(float)
# Cost governance: per-caller (agent/tenant) daily spend, on top of the per-tier
# caps. The caller id comes from a request header (see resolve_caller_id); an
# empty id is not attributed. A per-caller cap of 0 disables the ceiling (spend
# is still tracked for the daily rollup).
_spend_by_caller: dict[str, float] = defaultdict(float)
PER_CALLER_DAILY_USD: float = float(os.environ.get("PER_CALLER_DAILY_USD", "0") or 0)


def _reset_if_new_day() -> None:
    global _budget_date
    today = str(date.today())
    if today != _budget_date:
        _budget_date = today
        _spend.clear()
        _spend_by_caller.clear()


def record_cost(
    tier: str, cost: float, *, model: str | None = None,
    input_tokens: int = 0, output_tokens: int = 0, run_id: str | None = None,
    caller: str | None = None, correlation_id: str | None = None,
) -> None:
    _reset_if_new_day()
    _spend[tier] += cost
    if caller:
        _spend_by_caller[caller] += cost
    resolved_model = model or MODELS.get(tier, {}).get("litellm_model", tier)
    observe_genai(
        tier=tier, model=resolved_model,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost, run_id=run_id, correlation_id=correlation_id,
    )
    # gen_ai.usage metric points alongside the span (same flag, same fail-open).
    record_genai_metrics(
        tier=tier, model=resolved_model,
        input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost,
    )


def is_over_budget(tier: str) -> bool:
    _reset_if_new_day()
    return _spend.get(tier, 0.0) >= MODELS[tier]["daily_budget"]


def is_over_caller_budget(caller: str | None, *, cap: float | None = None) -> bool:
    """True when a per-caller daily cap is configured (>0) and this caller has
    reached it. Fails OPEN: no caller id or no cap → never over budget, so cost
    governance can't accidentally block traffic when unconfigured."""
    limit = PER_CALLER_DAILY_USD if cap is None else cap
    if not caller or limit <= 0:
        return False
    _reset_if_new_day()
    return _spend_by_caller.get(caller, 0.0) >= limit


def daily_cost_rollup() -> dict[str, object]:
    """Snapshot of today's spend for the cost-rollup surface: total + per-tier +
    per-caller. Pure read of the in-memory ledger (a deploy scrapes/rolls this;
    it is not itself durable)."""
    _reset_if_new_day()
    return {
        "date": _budget_date,
        "total_usd": round(sum(_spend.values()), 6),
        "by_tier": {k: round(v, 6) for k, v in _spend.items()},
        "by_caller": {k: round(v, 6) for k, v in _spend_by_caller.items()},
        "per_caller_daily_cap_usd": PER_CALLER_DAILY_USD,
    }


# ─── Acting budget enforcement (A6) ──────────────────────────────────────────
# Per-tier DAILY budgets historically only WARNED (upstream evidence: a Claude
# tier ran ~25× its daily budget with nothing but budget_exceeded WARN lines —
# real money). BUDGET_ENFORCE_MODE upgrades that to an acting control:
#   warn (default)  — pre-A6 behavior exactly (ship-dark safe): serve + WARN.
#   downgrade       — serve BUDGET_FALLBACK_TIER instead; mark the response
#                     with the X-Router-Budget-Downgrade header + a log line.
#   block           — 429 with a machine-readable budget_exceeded body.
# The decision logic lives in budget_enforcement.py — stdlib-only and
# host-agnostic, vendored verbatim from the upstream private deployment.
# Enforcement wraps select_tier() as its FINAL stage, so every path that
# selects a tier there (the OpenAI-compat /v1/chat/completions path, including
# the Anthropic direct-SDK dispatch inside _call_model, and ephemeral
# passthrough tiers) is covered. Two paths bypass select_tier and get explicit
# checks of their own: the native /v1/messages endpoint (the exact gap class
# that let the upstream Claude tier overrun silently) and /v1/embeddings
# (which has no tier at all — see the embeddings section for its dedicated
# ledger bucket). This router has no selftest/probe traffic, so nothing needs
# a spend exemption.
_BUDGET_ENFORCE_MODE = budget_enforcement.resolve_mode(os.environ.get("BUDGET_ENFORCE_MODE"))
_BUDGET_FALLBACK_TIER = os.environ.get("BUDGET_FALLBACK_TIER", "gpt4o-mini")
log.info(
    "budget_enforce mode=%s fallback_tier=%s",
    _BUDGET_ENFORCE_MODE, _BUDGET_FALLBACK_TIER,
)


def _budget_decision(tier: str) -> budget_enforcement.BudgetDecision:
    """The enforcement decision for serving `tier` right now (pure lookup)."""
    return budget_enforcement.decide(
        tier=tier,
        over_budget=is_over_budget(tier),
        mode=_BUDGET_ENFORCE_MODE,
        fallback_tier=_BUDGET_FALLBACK_TIER,
        registered_tiers=MODELS,
        spent_usd=_spend.get(tier, 0.0),
        limit_usd=MODELS[tier]["daily_budget"],
    )


def _apply_budget_enforcement(tier: str, body: dict) -> str:
    """Acting per-tier daily-budget enforcement (A6) — final select_tier stage.

    warn → serve the requested tier (+ WARN log, exactly the old behavior);
    downgrade → serve the fallback tier and stash a marker in a private body
    slot (same pattern as __router_extra_headers__) so the endpoint can add
    the X-Router-Budget-Downgrade response header; block → raise 429 with the
    machine-readable budget_exceeded detail.
    """
    decision = _budget_decision(tier)
    if decision.action == budget_enforcement.ACTION_ALLOW:
        return tier
    if decision.action == budget_enforcement.ACTION_WARN:
        log.warning("budget_enforce mode=%s tier=%s %s",
                    _BUDGET_ENFORCE_MODE, tier, decision.reason)
        return tier
    if decision.action == budget_enforcement.ACTION_DOWNGRADE:
        log.warning("budget_enforce_downgrade tier=%s→%s %s",
                    tier, decision.serve_tier, decision.reason)
        body["__budget_downgrade__"] = {"from": tier, "to": decision.serve_tier}
        return decision.serve_tier
    # ACTION_BLOCK — FastAPI wraps the dict detail as {"detail": {...}}.
    log.warning("budget_enforce_block tier=%s %s", tier, decision.reason)
    raise HTTPException(status_code=429, detail=budget_enforcement.block_detail(decision))


def _budget_downgrade_headers(body: dict) -> dict[str, str]:
    """Response headers marking a budget downgrade, if one happened for `body`."""
    marker = body.get("__budget_downgrade__")
    if not isinstance(marker, dict):
        return {}
    name, value = budget_enforcement.downgrade_header(
        budget_enforcement.BudgetDecision(
            budget_enforcement.ACTION_DOWNGRADE,
            marker.get("from", ""), marker.get("to", ""), "",
        )
    )
    return {name: value}


def _budget_downgrade_meta(body: dict) -> dict:
    """_router metadata marking a budget downgrade, if one happened for `body`."""
    marker = body.get("__budget_downgrade__")
    if not isinstance(marker, dict) or not marker.get("from"):
        return {}
    return {"budget_downgraded_from": marker["from"]}


# ─── Flight Recorder + Waste Breakers ─────────────────────────────────────────
# A replayable, bounded trace of every /v1/chat/completions and /v1/messages
# call (who called, requested vs served model, tokens, latency, cost estimate,
# outcome, breaker verdicts) plus pattern detection for wasteful call shapes
# (retry storms, oversized prompts, repeated identical calls). Design + full
# rationale: docs/design/router-flight-recorder.md.
#
# FLIGHT_RECORDER_ENABLED defaults ON (unlike OBSERVABILITY_ENABLED's
# default-off OTel tracing) because it is self-contained — no exporter, no
# collector, nothing to stand up — and redacted by default, so leaving it on
# is safe out of the box. Disabling it removes the recorder entirely
# (`_flight_recorder` stays None) for a genuinely zero-overhead no-op path.
FLIGHT_RECORDER_ENABLED = os.environ.get("FLIGHT_RECORDER_ENABLED", "true").strip().lower() in (
    "1", "true", "yes",
)
_FLIGHT_RECORDER_REDACT = os.environ.get("FLIGHT_RECORDER_REDACT", "true").strip().lower() in (
    "1", "true", "yes",
)
_FLIGHT_RECORDER_MAX_EVENTS = int(os.environ.get("FLIGHT_RECORDER_MAX_EVENTS", "500"))
_FLIGHT_RECORDER_JSONL_PATH = os.environ.get("FLIGHT_RECORDER_JSONL_PATH", "").strip() or None
_FLIGHT_RECORDER_JSONL_MAX_BYTES = int(
    os.environ.get("FLIGHT_RECORDER_JSONL_MAX_BYTES", "10000000")
)

_flight_recorder: flight_recorder.FlightRecorder | None = None
_flight_recorder_init_error: str | None = None
if FLIGHT_RECORDER_ENABLED:
    try:
        _flight_recorder = flight_recorder.FlightRecorder(
            max_events=_FLIGHT_RECORDER_MAX_EVENTS,
            redact=_FLIGHT_RECORDER_REDACT,
            jsonl_path=_FLIGHT_RECORDER_JSONL_PATH,
            jsonl_max_bytes=_FLIGHT_RECORDER_JSONL_MAX_BYTES,
        )
    except Exception as exc:  # noqa: BLE001 — a telemetry init failure must not
        # stop the router from booting; it just runs without a flight recorder.
        _flight_recorder_init_error = f"{type(exc).__name__}: {exc}"
        log.error("flight_recorder_init_failed: %s", _flight_recorder_init_error)
log.info(
    "flight_recorder enabled=%s redact=%s max_events=%s jsonl_path=%s",
    FLIGHT_RECORDER_ENABLED, _FLIGHT_RECORDER_REDACT, _FLIGHT_RECORDER_MAX_EVENTS,
    _FLIGHT_RECORDER_JSONL_PATH or "(in-memory only)",
)

# Waste breakers read their history from the flight recorder's ring buffer
# (count_recent / consecutive_failures), so they naturally degrade rather
# than fail when the recorder is disabled: oversized-prompt still works (it
# only needs the current request), repeated-identical-calls and retry-storm
# simply never trip (no history to evaluate against).
WASTE_BREAKERS_ENABLED = os.environ.get("WASTE_BREAKERS_ENABLED", "true").strip().lower() in (
    "1", "true", "yes",
)
_WASTE_BREAKER_ENFORCE_MODE = waste_breakers.resolve_mode(
    os.environ.get("WASTE_BREAKER_ENFORCE_MODE")
)
_WASTE_BREAKER_THRESHOLDS = waste_breakers.BreakerThresholds(
    repeated_identical_calls=int(os.environ.get("WASTE_BREAKER_REPEAT_THRESHOLD", "5")),
    repeated_identical_window_seconds=int(
        os.environ.get("WASTE_BREAKER_REPEAT_WINDOW_SECONDS", "60")
    ),
    retry_storm_calls=int(os.environ.get("WASTE_BREAKER_RETRY_STORM_CALLS", "20")),
    retry_storm_window_seconds=int(
        os.environ.get("WASTE_BREAKER_RETRY_STORM_WINDOW_SECONDS", "60")
    ),
    oversized_prompt_tokens=int(
        os.environ.get("WASTE_BREAKER_OVERSIZED_PROMPT_TOKENS", "100000")
    ),
    consecutive_failures=int(os.environ.get("WASTE_BREAKER_CONSECUTIVE_FAILURES", "4")),
)
log.info(
    "waste_breakers enabled=%s enforce_mode=%s thresholds=%s",
    WASTE_BREAKERS_ENABLED, _WASTE_BREAKER_ENFORCE_MODE, _WASTE_BREAKER_THRESHOLDS,
)


def _flight_context(
    *, endpoint: str, body: dict, headers, caller: str | None,
) -> dict[str, Any]:
    """Per-request facts gathered once at the top of an endpoint, reused by
    both the waste-breaker check and every later _emit_flight_event call."""
    messages = body.get("messages") or []
    return {
        "endpoint": endpoint,
        "caller": caller,
        "correlation_id": resolve_correlation_id(headers),
        "requested_model": body.get("model"),
        "prompt_fingerprint": flight_recorder.prompt_fingerprint(messages, body.get("tools")),
        "prompt_tokens_estimated": _estimate_tokens(messages),
        "message_count": len(messages),
    }


def _waste_breaker_observation(ctx: dict) -> waste_breakers.CallerObservation:
    caller = ctx.get("caller")
    calls_in_window = identical_in_window = consecutive = 0
    if _flight_recorder is not None:
        calls_in_window = _flight_recorder.count_recent(
            caller=caller, seconds=_WASTE_BREAKER_THRESHOLDS.retry_storm_window_seconds,
        )
        identical_in_window = _flight_recorder.count_recent(
            caller=caller,
            fingerprint=ctx.get("prompt_fingerprint"),
            seconds=_WASTE_BREAKER_THRESHOLDS.repeated_identical_window_seconds,
        )
        consecutive = _flight_recorder.consecutive_failures(caller=caller)
    return waste_breakers.CallerObservation(
        caller=caller,
        calls_in_window=calls_in_window,
        identical_calls_in_window=identical_in_window,
        consecutive_failures=consecutive,
        estimated_prompt_tokens=ctx.get("prompt_tokens_estimated", 0),
    )


def _apply_waste_breakers(ctx: dict, t_start: float) -> list[waste_breakers.BreakerVerdict]:
    """Evaluate waste breakers for this request. Always observes; only
    refuses the request when WASTE_BREAKER_ENFORCE_MODE=block AND a breaker
    tripped. Returns every verdict (tripped or not) so the caller can attach
    the full picture to the eventual success/error flight event."""
    if not WASTE_BREAKERS_ENABLED:
        return []
    obs = _waste_breaker_observation(ctx)
    verdicts = waste_breakers.evaluate(obs, _WASTE_BREAKER_THRESHOLDS)
    tripped = waste_breakers.tripped_only(verdicts)
    for v in tripped:
        log.warning(
            "waste_breaker_tripped breaker=%s caller=%s mode=%s detail=%s",
            v.breaker, ctx.get("caller"), _WASTE_BREAKER_ENFORCE_MODE, v.detail,
        )
    if tripped and _WASTE_BREAKER_ENFORCE_MODE == waste_breakers.MODE_BLOCK:
        _emit_flight_event(
            ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
            error_class=f"waste_breaker:{tripped[0].breaker}",
            breaker_verdicts=verdicts,
        )
        raise HTTPException(status_code=429, detail=waste_breakers.trip_detail(tripped[0]))
    return verdicts


def _emit_flight_event(
    ctx: dict,
    *,
    t_start: float,
    outcome: str,
    served_tier: str | None = None,
    served_model: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    error_class: str | None = None,
    breaker_verdicts: list[waste_breakers.BreakerVerdict] | None = None,
    streamed: bool = False,
    prompt_excerpt: str | None = None,
    response_excerpt: str | None = None,
) -> None:
    """Record one call to the flight recorder. No-ops instantly (and for
    free) when the recorder is disabled — this is the zero-overhead path."""
    if _flight_recorder is None:
        return
    _flight_recorder.record(
        correlation_id=ctx.get("correlation_id"),
        caller=ctx.get("caller"),
        endpoint=ctx.get("endpoint"),
        requested_model=ctx.get("requested_model"),
        served_tier=served_tier,
        served_model=served_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=round((time.monotonic() - t_start) * 1000, 2),
        cost_usd=cost_usd,
        outcome=outcome,
        error_class=error_class,
        prompt_fingerprint=ctx.get("prompt_fingerprint"),
        prompt_tokens_estimated=ctx.get("prompt_tokens_estimated"),
        message_count=ctx.get("message_count"),
        breaker_verdicts=[v.as_dict() for v in (breaker_verdicts or [])],
        streamed=streamed,
        prompt_excerpt=prompt_excerpt,
        response_excerpt=response_excerpt,
    )


# ─── Fail-Closed Resilience Pack ──────────────────────────────────────────────
# Two governance controls over upstream dispatch, both refusing rather than
# rerouting. Full design + operator workflow:
# docs/design/router-resilience-pack.md.
#
#   1. CIRCUIT BREAKERS (circuit_breaker.py) — three states per upstream
#      *credential identity*, tripped only by auth failures, quota exhaustion,
#      and connection-level failures. While OPEN the router returns a typed
#      503 and never invokes that upstream. Enabled by default: a breaker is
#      fail-safe by construction, it can only stop calls that are already
#      failing a narrow, explicit set of checks.
#
#   2. KILL SWITCH (kill_switch.py) — scoped operator control over metered
#      dispatch (`paid_fallback`, `all_paid`). Disengaged by default: it is a
#      deliberate incident action, not a posture.
#
# The two compose. `ROUTER_BREAKER_FAIL_CLOSED` (default on) is the seam: when
# a request's primary upstream is OPEN, a *metered* fallback hop is refused
# instead of attempted — the automatic version of engaging `paid_fallback` for
# the duration of one request. Free local tiers still serve, so an edge host
# going offline still degrades to Foundry the way it always did.


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes")


BREAKERS_ENABLED = _flag("ROUTER_BREAKER_ENABLED", True)
_BREAKER_FAIL_CLOSED = _flag("ROUTER_BREAKER_FAIL_CLOSED", True)
_BREAKER_CONFIG = circuit_breaker.BreakerConfig(
    enabled=BREAKERS_ENABLED,
    failure_threshold=circuit_breaker.positive_int(
        os.environ.get("ROUTER_BREAKER_FAILURE_THRESHOLD"), 5
    ),
    cooldown_seconds=circuit_breaker.positive_float(
        os.environ.get("ROUTER_BREAKER_COOLDOWN_SECONDS"), 60.0
    ),
    half_open_probes=circuit_breaker.positive_int(
        os.environ.get("ROUTER_BREAKER_HALF_OPEN_PROBES"), 1
    ),
)


def _on_breaker_transition(event: dict) -> None:
    """Log + record every breaker state change. A trip is a governance event:
    it says the router stopped spending on an upstream, which is exactly the
    thing an operator needs in the trace when they ask why traffic stopped."""
    log.warning(
        "circuit_breaker_transition key=%s %s->%s reason=%s failures=%s trips=%s",
        event["key"], event["from_state"], event["to_state"],
        event.get("reason"), event.get("failures"), event.get("trips"),
    )
    if _flight_recorder is None:
        return
    _flight_recorder.record(
        endpoint="circuit_breaker",
        event_type="breaker_transition",
        outcome=(
            flight_recorder.OUTCOME_ERROR
            if event["to_state"] == circuit_breaker.STATE_OPEN
            else flight_recorder.OUTCOME_SUCCESS
        ),
        error_class=f"breaker_{event['to_state']}",
        breaker_key=event["key"],
        breaker_from_state=event["from_state"],
        breaker_to_state=event["to_state"],
        breaker_reason=event.get("reason"),
        breaker_failures=event.get("failures"),
        breaker_trips=event.get("trips"),
    )


_breakers = circuit_breaker.CircuitBreakerRegistry(
    _BREAKER_CONFIG, on_transition=_on_breaker_transition
)

_KILL_SWITCH_BOOT_SCOPES, _kill_switch_unknown_scopes = kill_switch.parse_scopes(
    os.environ.get("ROUTER_KILL_SWITCH_SCOPES")
)
if _kill_switch_unknown_scopes:
    # Loud, but non-fatal: a typo must not stop the router booting, and must
    # not silently look like an engaged kill switch either.
    log.error(
        "kill_switch_unknown_scopes ignored=%s valid=%s",
        _kill_switch_unknown_scopes, list(kill_switch.ALL_SCOPES),
    )


def _on_kill_switch_event(event: dict) -> None:
    log.warning(
        "kill_switch event=%s scope=%s actor=%s changed=%s engaged=%s reason=%s",
        event["event"], event["scope"], event.get("actor"),
        event.get("changed"), event.get("engaged_scopes"), event.get("reason"),
    )
    if _flight_recorder is None:
        return
    _flight_recorder.record(
        endpoint="kill_switch",
        event_type=event["event"],
        outcome=(
            flight_recorder.OUTCOME_ERROR
            if event["event"] == kill_switch.EVENT_ENGAGED
            else flight_recorder.OUTCOME_SUCCESS
        ),
        error_class=event["event"],
        kill_switch_scope=event["scope"],
        kill_switch_actor=event.get("actor"),
        kill_switch_reason=event.get("reason"),
        kill_switch_changed=event.get("changed"),
        kill_switch_engaged=event.get("engaged_scopes"),
    )


_kill_switch = kill_switch.KillSwitch(
    _KILL_SWITCH_BOOT_SCOPES, on_event=_on_kill_switch_event
)

# Optional second credential for the MUTATING admin route. When unset, flipping
# the kill switch at runtime needs only ROUTER_API_KEY — the same auth every
# /debug/* route already uses, which is the documented pattern here. Set it to
# separate "can call the router" from "can turn the router's spend controls
# off": every in-mesh agent holds ROUTER_API_KEY, and none of them should be
# able to release a kill switch an operator engaged during an incident.
_ROUTER_ADMIN_API_KEY = os.environ.get("ROUTER_ADMIN_API_KEY", "").strip()

log.info(
    "resilience breakers_enabled=%s threshold=%s cooldown_s=%s half_open_probes=%s "
    "fail_closed_fallback=%s kill_switch_boot_scopes=%s admin_key_required=%s",
    BREAKERS_ENABLED, _BREAKER_CONFIG.failure_threshold, _BREAKER_CONFIG.cooldown_seconds,
    _BREAKER_CONFIG.half_open_probes, _BREAKER_FAIL_CLOSED,
    sorted(_KILL_SWITCH_BOOT_SCOPES) or "(none)", bool(_ROUTER_ADMIN_API_KEY),
)


class ResilienceBlocked(HTTPException):
    """A dispatch the resilience pack refused, as a typed 503.

    Its own class (rather than a bare HTTPException) so the fallback loops can
    re-raise it instead of swallowing it into "all tiers failed". A caller
    must be able to tell "your credential is broken / spending is halted"
    apart from "every model we tried errored" — they need different responses
    from a human.
    """

    def __init__(self, *, code: str, detail: dict, headers: dict[str, str] | None = None):
        super().__init__(status_code=503, detail=detail, headers=headers or {})
        self.code = code


def _is_metered_tier(tier: str) -> bool:
    """True when dispatching to `tier` spends real per-token money.

    False only for local Ollama tiers: inference on an edge host the operator
    already owns has zero marginal cost, so it is the free recovery path a
    cost control must never turn into an availability incident.
    """
    cfg = MODELS.get(tier) or {}
    return not bool(cfg.get("is_ollama"))


def _breaker_key(tier: str) -> str:
    """Breaker key for a tier: its upstream credential identity, not its name.
    See circuit_breaker.credential_key for why."""
    cfg = MODELS.get(tier) or {}
    return circuit_breaker.credential_key(
        api_base=cfg.get("api_base"), api_key=cfg.get("api_key")
    )


def _resilience_gate(
    candidate: str, *, primary_tier: str, is_fallback: bool
) -> circuit_breaker.AdmitVerdict:
    """Decide whether ONE upstream dispatch may proceed.

    Raises ResilienceBlocked (typed 503) instead of returning, so there is no
    way to accidentally ignore the verdict and dispatch anyway. Returns the
    breaker's admit verdict, which the caller must hand back to
    _record_dispatch_outcome exactly once (a half-open admission consumes the
    breaker's single probe).

    Order is deliberate: operator intent first, then automatic policy, then
    upstream health. The breaker's admit() call is LAST because it is the only
    step with a side effect — no path may consume a probe and then refuse.
    """
    metered = _is_metered_tier(candidate)
    intent = kill_switch.DispatchIntent(
        tier=candidate, metered=metered, is_fallback=is_fallback, primary_tier=primary_tier,
    )
    decision = _kill_switch.evaluate(intent)
    if decision.blocked:
        detail = kill_switch.block_detail(decision, intent)
        log.warning(
            "dispatch_blocked code=%s scope=%s tier=%s primary=%s",
            kill_switch.KILL_SWITCH_ERROR_CODE, decision.scope, candidate, primary_tier,
        )
        raise ResilienceBlocked(
            code=kill_switch.KILL_SWITCH_ERROR_CODE,
            detail=detail,
            headers={
                "X-Router-Error-Code": kill_switch.KILL_SWITCH_ERROR_CODE,
                "X-Router-Kill-Switch-Scope": decision.scope or "",
                "X-Router-Retryable": "false",
            },
        )

    # Fail-closed cost posture: the primary upstream is shut off, so this
    # request is already an incident. Falling through to a metered model is
    # exactly the silent spend this pack exists to stop. A free local fallback
    # is untouched.
    if (
        is_fallback
        and metered
        and _BREAKER_FAIL_CLOSED
        and _breakers.is_open(_breaker_key(primary_tier))
    ):
        verdict = circuit_breaker.AdmitVerdict(
            allowed=False,
            state=circuit_breaker.STATE_OPEN,
            key=_breaker_key(primary_tier),
        )
        detail = circuit_breaker.open_detail(
            verdict, tier=candidate, fail_closed_fallback=True, primary_tier=primary_tier,
        )
        log.warning(
            "dispatch_blocked code=%s reason=fail_closed_fallback tier=%s primary=%s",
            circuit_breaker.BREAKER_OPEN_ERROR_CODE, candidate, primary_tier,
        )
        raise ResilienceBlocked(
            code=circuit_breaker.BREAKER_OPEN_ERROR_CODE,
            detail=detail,
            headers=_breaker_headers(detail),
        )

    verdict = _breakers.admit(_breaker_key(candidate))
    if not verdict.allowed:
        detail = circuit_breaker.open_detail(
            verdict, tier=candidate, primary_tier=primary_tier,
        )
        log.warning(
            "dispatch_blocked code=%s state=%s tier=%s key=%s",
            circuit_breaker.BREAKER_OPEN_ERROR_CODE, verdict.state, candidate, verdict.key,
        )
        raise ResilienceBlocked(
            code=circuit_breaker.BREAKER_OPEN_ERROR_CODE,
            detail=detail,
            headers=_breaker_headers(detail),
        )
    return verdict


def _breaker_headers(detail: dict) -> dict[str, str]:
    retry_after = max(1, int(round(detail.get("retry_after_seconds") or 0))) if detail.get(
        "retryable"
    ) else 0
    headers = {
        "X-Router-Error-Code": circuit_breaker.BREAKER_OPEN_ERROR_CODE,
        "X-Router-Breaker-State": str(detail.get("breaker_state", "")),
        "X-Router-Retryable": "true" if detail.get("retryable") else "false",
    }
    if retry_after:
        headers["Retry-After"] = str(retry_after)
    return headers


def _record_dispatch_outcome(
    tier: str, verdict: circuit_breaker.AdmitVerdict | None, exc: BaseException | None = None
) -> str | None:
    """Report the result of one admitted dispatch back to its breaker.

    Returns the trip reason recorded, or None when the failure was not
    breaker-eligible. The half-open case is the subtle one: a probe that
    failed for a NON-eligible reason must still resolve the probe, or the
    breaker sits half-open with a verdict nobody ever delivers.
    """
    if not BREAKERS_ENABLED:
        return None
    key = _breaker_key(tier)
    if exc is None:
        _breakers.record_success(key)
        return None
    reason = circuit_breaker.classify_failure(exc)
    if reason is None:
        if verdict is not None and verdict.state == circuit_breaker.STATE_HALF_OPEN:
            _breakers.record_failure(key, reason=circuit_breaker.TRIP_PROBE_FAILED)
            return circuit_breaker.TRIP_PROBE_FAILED
        log.debug(
            "breaker_ignored_failure tier=%s error_class=%s (not a trip signal)",
            tier, type(exc).__name__,
        )
        return None
    _breakers.record_failure(key, reason=reason)
    return reason


async def _guarded_call_model(
    candidate: str, body: dict, *, primary_tier: str, is_fallback: bool
) -> dict:
    """_call_model behind the resilience gate. The gate raises BEFORE the call,
    so an OPEN breaker means the upstream is genuinely never invoked."""
    verdict = _resilience_gate(candidate, primary_tier=primary_tier, is_fallback=is_fallback)
    try:
        result = await _call_model(candidate, body)
    except Exception as exc:
        _record_dispatch_outcome(candidate, verdict, exc)
        raise
    _record_dispatch_outcome(candidate, verdict)
    return result


async def _guarded_open_stream(
    candidate: str, body: dict, *, primary_tier: str, is_fallback: bool
):
    """_open_stream behind the resilience gate. Opening the stream is where the
    upstream connection (and its auth) is actually exercised, so it is the
    right place to score the breaker for the streaming path."""
    verdict = _resilience_gate(candidate, primary_tier=primary_tier, is_fallback=is_fallback)
    try:
        stream_iter = await _open_stream(candidate, body)
    except Exception as exc:
        _record_dispatch_outcome(candidate, verdict, exc)
        raise
    _record_dispatch_outcome(candidate, verdict)
    return stream_iter


def _breaker_state_by_tier() -> dict[str, str]:
    """Per-tier breaker state for the health probe. Several tiers can share one
    state — that is the credential keying working as designed."""
    return {tier: _breakers.state(_breaker_key(tier)) for tier in MODELS}


def _resilience_status(*, include_keys: bool) -> dict:
    status: dict[str, Any] = {
        "circuit_breakers": {
            "enabled": BREAKERS_ENABLED,
            "fail_closed_fallback": _BREAKER_FAIL_CLOSED,
            "failure_threshold": _BREAKER_CONFIG.failure_threshold,
            "cooldown_seconds": _BREAKER_CONFIG.cooldown_seconds,
            "half_open_probes": _BREAKER_CONFIG.half_open_probes,
            "by_tier": _breaker_state_by_tier(),
            "open_keys": len(_breakers.open_keys()),
        },
        "kill_switch": _kill_switch.snapshot(),
    }
    if include_keys:
        # Per-key detail is auth-gated. The key itself is a salted per-process
        # fingerprint (no hostname, no key material), but the trip history it
        # carries is operational detail an unauthenticated probe doesn't need.
        status["circuit_breakers"]["by_key"] = _breakers.snapshot_all()
    return status


# ─── Observability (GenAI semconv) ────────────────────────────────────────────
# One OTel span per model call with GenAI semantic-convention attributes, behind
# OBSERVABILITY_ENABLED (default off), content-redacted (no prompt/response text).
# `gen_ai.*` are the OTel standard attributes; `agent.*` are this repo's custom
# additions (tier + optional run id).

def _genai_system(tier: str) -> str:
    """gen_ai.system: 'anthropic' for Anthropic-dispatched tiers, else Foundry."""
    # _is_anthropic_tier is defined near _call_anthropic_direct (Anthropic-direct section).
    return "anthropic" if _is_anthropic_tier(tier) else "az.ai.foundry"


def genai_semconv_attrs(
    *, tier: str, model: str, input_tokens: int, output_tokens: int,
    cost_usd: float, run_id: str | None = None, correlation_id: str | None = None,
) -> dict[str, object]:
    """Pure mapping → OTel GenAI-semconv span attributes. No I/O."""
    attrs: dict[str, object] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.system": _genai_system(tier),
        "gen_ai.request.model": model or tier,
        "gen_ai.usage.input_tokens": max(0, int(input_tokens or 0)),
        "gen_ai.usage.output_tokens": max(0, int(output_tokens or 0)),
        "gen_ai.usage.cost_usd": max(0.0, round(float(cost_usd or 0.0), 6)),
        "agent.tier": tier,
    }
    if run_id:
        attrs["agent.run_id"] = run_id
    if correlation_id:
        # Threads a request across the caller → router → provider hop so spans,
        # logs, and the caller's own trace can be joined on one id.
        attrs["agent.correlation_id"] = correlation_id
    return attrs


# ─── Correlation id + caller attribution (request-scoped) ─────────────────────
_CORRELATION_HEADERS = ("x-correlation-id", "x-request-id", "traceparent")
_CALLER_HEADERS = ("x-agent-id", "x-tenant-id", "x-caller-id")


def resolve_correlation_id(headers) -> str | None:
    """First non-empty correlation header (case-insensitive), else None. Pure —
    the caller decides whether to mint one when absent."""
    if not headers:
        return None
    get = headers.get
    for h in _CORRELATION_HEADERS:
        v = get(h) or get(h.title())
        if v:
            return str(v)[:200]
    return None


def resolve_caller_id(headers) -> str | None:
    """First non-empty caller header (agent/tenant/caller), for per-caller cost
    attribution. Pure; None when unattributed."""
    if not headers:
        return None
    get = headers.get
    for h in _CALLER_HEADERS:
        v = get(h) or get(h.title())
        if v:
            return str(v)[:200]
    return None


OBSERVABILITY_ENABLED = os.environ.get("OBSERVABILITY_ENABLED", "").strip().lower() in (
    "1", "true", "yes",
)

_tracer = None
_tracer_provider = None
_tracer_initialised = False


def _build_tracer_provider(exporter):
    """Build a SELF-CONTAINED TracerProvider that exports spans via `exporter`.

    Deliberately does NOT use configure_azure_monitor / the global OTel provider.
    configure_azure_monitor was called lazily on the first model call, but OTel
    refuses to override an already-set global TracerProvider — so the Azure span
    processor never attached and spans were never exported (logs + metrics were,
    which masked the bug). Owning our own provider makes span export deterministic
    and unit-testable (swap in an in-memory exporter)."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider


def _init_tracer():
    """Lazily build the Azure Monitor span exporter once. Returns a tracer bound to
    our own provider (see _build_tracer_provider) or None when no connection string
    is set or setup fails. Swap the exporter for any OTLP exporter for a non-Azure
    backend."""
    global _tracer, _tracer_provider, _tracer_initialised
    if _tracer_initialised:
        return _tracer
    _tracer_initialised = True
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return None
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        _tracer_provider = _build_tracer_provider(
            AzureMonitorTraceExporter(connection_string=conn)
        )
        _tracer = _tracer_provider.get_tracer("model-router")
    except Exception as e:  # pragma: no cover - requires live azure.monitor SDK
        log.warning("observability: tracer init failed, disabling: %s", e)
        _tracer = None
    return _tracer


def observe_genai(
    *, tier: str, model: str, input_tokens: int, output_tokens: int,
    cost_usd: float, run_id: str | None = None, correlation_id: str | None = None,
) -> None:
    """Emit one GenAI-semconv span. Flag-gated and FAIL-OPEN: any telemetry error
    is swallowed so it can never break a model call (the router is on every call)."""
    if not OBSERVABILITY_ENABLED:
        return
    try:
        tracer = _init_tracer()
        if tracer is None:
            return
        attrs = genai_semconv_attrs(
            tier=tier, model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, cost_usd=cost_usd, run_id=run_id,
            correlation_id=correlation_id,
        )
        with tracer.start_as_current_span("gen_ai.chat") as span:
            for k, v in attrs.items():
                span.set_attribute(k, v)
    except Exception as e:  # fail-open: never surface telemetry errors
        log.debug("observe_genai swallowed: %s", e)


# ─── gen_ai.usage metrics (counters alongside the span) ───────────────────────
# The v1.3 span is one event per call; these are aggregatable counters (token
# and cost sums by model/tier/system) so a dashboard can chart usage/cost and an
# alert can fire on a spend burn-rate without querying every span.
_meter = None
_meter_provider = None
_meter_initialised = False
_metric_instruments: dict[str, object] = {}


def genai_metric_points(
    *, tier: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float,
) -> list[dict]:
    """Pure mapping → the metric points to record for one model call. Each is
    {instrument, value, attributes}; attributes are low-cardinality (model/tier/
    system) so the series don't explode."""
    dims = {"gen_ai.request.model": model or tier, "agent.tier": tier,
            "gen_ai.system": _genai_system(tier)}
    return [
        {"instrument": "gen_ai.client.token.usage", "value": max(0, int(input_tokens or 0)),
         "attributes": {**dims, "gen_ai.token.type": "input"}},
        {"instrument": "gen_ai.client.token.usage", "value": max(0, int(output_tokens or 0)),
         "attributes": {**dims, "gen_ai.token.type": "output"}},
        {"instrument": "gen_ai.client.cost.usd", "value": max(0.0, round(float(cost_usd or 0.0), 6)),
         "attributes": dims},
    ]


def _init_meter():
    """Lazily build the Azure Monitor metric exporter + counters once. Mirrors
    _init_tracer: returns None when no connection string is set or setup fails.
    Swap the exporter for any OTLP metric exporter for a non-Azure backend."""
    global _meter, _meter_provider, _meter_initialised
    if _meter_initialised:
        return _meter
    _meter_initialised = True
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        return None
    try:  # pragma: no cover - requires live azure.monitor SDK
        from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        reader = PeriodicExportingMetricReader(
            AzureMonitorMetricExporter(connection_string=conn)
        )
        _meter_provider = MeterProvider(metric_readers=[reader])
        _meter = _meter_provider.get_meter("model-router")
        _metric_instruments["gen_ai.client.token.usage"] = _meter.create_counter(
            "gen_ai.client.token.usage", unit="{token}", description="GenAI tokens by type")
        _metric_instruments["gen_ai.client.cost.usd"] = _meter.create_counter(
            "gen_ai.client.cost.usd", unit="USD", description="GenAI list-price/billed cost")
    except Exception as e:  # pragma: no cover
        log.warning("observability: meter init failed, disabling: %s", e)
        _meter = None
    return _meter


def record_genai_metrics(
    *, tier: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float,
) -> None:
    """Emit the gen_ai.usage metric points. Flag-gated + FAIL-OPEN, exactly like
    observe_genai — telemetry must never break a model call."""
    if not OBSERVABILITY_ENABLED:
        return
    try:
        meter = _init_meter()
        if meter is None:
            return
        for pt in genai_metric_points(
            tier=tier, model=model, input_tokens=input_tokens,
            output_tokens=output_tokens, cost_usd=cost_usd,
        ):
            inst = _metric_instruments.get(pt["instrument"])
            if inst is not None:
                inst.add(pt["value"], pt["attributes"])
    except Exception as e:  # fail-open
        log.debug("record_genai_metrics swallowed: %s", e)


# Anthropic list-price per-million-token rates (input, output). The Anthropic SDK
# does not return response_cost, so cost on this path is a LIST-PRICE ESTIMATE,
# not a billed figure. Substring match on the model name — place MORE-SPECIFIC keys
# first so they win. The "claude-haiku" rate assumes 3.5 Haiku; older/cheaper Haiku
# versions would be over-estimated. Source: anthropic.com/pricing (verified 2026-06-24).
_ANTHROPIC_PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-opus": (15.0, 75.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku": (0.80, 4.0),
}


def _estimate_anthropic_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """List-price estimate in USD for an Anthropic call. Defaults to sonnet rates.
    Input/output tokens only — cache-read/write tokens are excluded from the estimate."""
    name = (model or "").lower()
    in_rate, out_rate = next(
        (rates for key, rates in _ANTHROPIC_PRICING_PER_MTOK.items() if key in name),
        _ANTHROPIC_PRICING_PER_MTOK["claude-sonnet"],
    )
    return (input_tokens or 0) / 1_000_000 * in_rate + (output_tokens or 0) / 1_000_000 * out_rate


def _usage_from_result(result: dict, *, fallback_tier: str) -> tuple[str, int, int]:
    """Pull (model, input_tokens, output_tokens) from an OpenAI-shaped result dict,
    falling back to the tier's configured model name when the result omits it.
    Never raises (defaults gracefully) — it runs on the model-call hot path."""
    usage = result.get("usage") or {}
    fallback_model = (MODELS.get(fallback_tier) or {}).get("litellm_model", fallback_tier)
    model = result.get("model") or fallback_model.split("/", 1)[-1]
    return model, int(usage.get("prompt_tokens", 0) or 0), int(usage.get("completion_tokens", 0) or 0)


def _record_anthropic_usage(tier: str, deployment: str, usage, caller: str | None = None) -> None:
    """Record estimated cost from an Anthropic SDK usage object (best-effort).

    A6: the native /v1/messages path calls the Anthropic SDK directly (it never
    goes through _call_model), so before this its spend was invisible to the
    daily ledger — budget enforcement on that path would have been inert.
    Accepts anything with input_tokens/output_tokens attributes (the SDK usage
    object, or a dict-backed shim from the SSE accumulator). Never raises."""
    try:
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        record_cost(
            tier, _estimate_anthropic_cost(deployment, input_tokens, output_tokens),
            model=deployment, input_tokens=input_tokens, output_tokens=output_tokens,
            caller=caller,
        )
    except Exception as e:  # never let cost accounting break a response
        log.debug("anthropic_cost_estimate_failed tier=%s error=%s", tier, e)


# ─── Routing ──────────────────────────────────────────────────────────────────
# Map agent/persona names to model tiers. Populate via PERSONA_TIERS_JSON env
# var at runtime (JSON object: {"my-agent": "gpt4o-mini", ...}), or extend
# this dict directly in a fork.
import json as _json
PERSONA_TIERS: dict[str, str] = _json.loads(
    os.environ.get("PERSONA_TIERS_JSON", "{}")
)


# ─── Passthrough: forward unknown models directly to Azure AI Foundry ────────
# All models in the Foundry project share the same endpoint and API key.
# When a model name doesn't match a known tier, create an ephemeral config
# and forward the request directly. No code changes needed to add new models.
_FOUNDRY_BASE_URL = os.environ.get(
    "FOUNDRY_BASE_URL",
    _GPT4O_BASE_URL,  # same project endpoint
)
_FOUNDRY_API_KEY = os.environ.get("FOUNDRY_API_KEY", _GPT4O_API_KEY)
_PASSTHROUGH_TIMEOUT = int(os.environ.get("PASSTHROUGH_TIMEOUT_SECONDS", "60"))
_PASSTHROUGH_MAX_TOKENS = int(os.environ.get("PASSTHROUGH_MAX_TOKENS", "4096"))
_PASSTHROUGH_BUDGET = float(os.environ.get("PASSTHROUGH_DAILY_BUDGET_USD", "10.00"))


def _get_passthrough_config(model_name: str) -> dict[str, Any]:
    """Build an ephemeral model config for a Foundry model not in MODELS."""
    return {
        "litellm_model": f"openai/{model_name}",
        "api_base": _FOUNDRY_BASE_URL,
        "api_key": _FOUNDRY_API_KEY,
        "daily_budget": _PASSTHROUGH_BUDGET,
        "max_tokens": _PASSTHROUGH_MAX_TOKENS,
        "context_limit": 128000,
        "timeout_seconds": _PASSTHROUGH_TIMEOUT,
        "supports_tools": True,
        "passthrough": True,
    }


def select_tier(body: dict) -> str:
    # A6 acting budget enforcement runs LAST so the finally-selected tier is
    # always checked against its own daily budget (a pure pass-through in the
    # default warn mode). The pre-existing gpt4o-mini→phi4 over-budget redirect
    # inside _select_tier_base is unchanged — enforcement wraps it.
    return _apply_budget_enforcement(_select_tier_base(body), body)


def _select_tier_base(body: dict) -> str:
    tier = (body.get("tier") or body.get("metadata", {}).get("tier", "")).lower()

    if not tier or tier == "auto":
        model_hint = body.get("model", "").lower()
        if "gpt-4o-mini" in model_hint or "4o-mini" in model_hint:
            tier = "gpt4o-mini"
        elif "phi4" in model_hint or "phi-4" in model_hint:
            tier = "phi4"

    # If the caller specified an explicit model name that exactly matches a
    # registered tier (e.g. claude-sonnet-4-6, Kimi-K2.5, grok-4-1-fast-
    # reasoning — registered above by _register_foundry_tier from per-
    # deployment env vars), route to it directly.
    if not tier or tier == "auto":
        raw_model = body.get("model", "").strip()
        if raw_model and raw_model in MODELS:
            return raw_model

    # Explicit model that isn't a registered tier — fall through to passthrough
    # to Foundry. Persona fallback below would otherwise hardcode to gpt4o-mini
    # and silently downgrade an explicit model request to a tier the caller
    # never asked for.
    if not tier or tier == "auto":
        raw_model = body.get("model", "").strip()
        if raw_model:
            MODELS[raw_model] = _get_passthrough_config(raw_model)
            log.warning(
                "passthrough fallback model=%s (no dedicated tier; using "
                "FOUNDRY_BASE_URL — check that a *_BASE_URL env var isn't "
                "missing for this deployment)", raw_model,
            )
            return raw_model

    if not tier or tier == "auto":
        persona = body.get("persona", "").lower()
        tier = PERSONA_TIERS.get(persona, "gpt4o-mini")

    # Safety net — should now only fire when tier came from a tier/metadata
    # field that doesn't match anything (caller bug). Keeps prior behaviour.
    if tier not in MODELS:
        raw_model = body.get("model", "").strip()
        if raw_model:
            MODELS[raw_model] = _get_passthrough_config(raw_model)
            log.info("passthrough model=%s (safety net)", raw_model)
            return raw_model
        tier = "gpt4o-mini"

    if tier == "gpt4o-mini" and is_over_budget(tier):
        return "phi4"

    return tier


# ─── Sanitise response content ────────────────────────────────────────────────
_THINK_RE = re.compile(
    r"<think>.*?</think>|<thinking>.*?</thinking>",
    flags=re.DOTALL | re.IGNORECASE,
)

def _sanitise_content(raw: str, *, strip: bool = True) -> str:
    cleaned = _THINK_RE.sub("", raw)
    return cleaned.strip() if strip else cleaned


def _build_completion_kwargs(tier: str, body: dict, *, stream: bool) -> dict[str, Any]:
    cfg = MODELS[tier]
    kwargs: dict[str, Any] = {
        "model": cfg["litellm_model"],
        "messages": body["messages"],
        "max_tokens": min(body.get("max_tokens", cfg["max_tokens"]), cfg["max_tokens"]),
        "api_key": cfg["api_key"],
        "api_base": cfg.get("api_base"),
        "timeout": cfg["timeout_seconds"],
        "stream": stream,
    }

    # Temperature handling:
    # gpt-5 family (gpt-5-nano, gpt-5-codex, gpt-5.4-nano etc.) only accepts
    # temperature=1 — LiteLLM raises UnsupportedParamsError on any other value.
    # If the router defaults temperature to 0.7 for these, every gpt-5-* call
    # fails and fail-soft cascades to gpt4o-mini, silently downgrading any agent
    # on a gpt-5 tier. So we pick the default temperature per model family below.
    #
    # Use the caller's temperature if provided; otherwise pick a sane default
    # per model family. (gpt-5.1 allows non-1 temperature only when
    # reasoning_effort='none' is also set — out of scope here; callers that
    # need that should set temperature explicitly.)
    deployment = cfg["litellm_model"].split("/", 1)[-1]
    if body.get("temperature") is not None:
        kwargs["temperature"] = body["temperature"]
    elif deployment.lower().startswith("gpt-5"):
        kwargs["temperature"] = 1.0
    else:
        kwargs["temperature"] = 0.7

    # Only forward tools to models that support function calling
    if cfg.get("supports_tools") and body.get("tools"):
        kwargs["tools"] = body["tools"]
        if body.get("tool_choice"):
            kwargs["tool_choice"] = body["tool_choice"]

    return kwargs


# ─── Anthropic direct-call bypass (Foundry /anthropic endpoint) ──────────────
# LiteLLM's anthropic provider mishandles a custom api_base pointing at Azure
# AI Foundry — see comment block above _register_foundry_tier("CLAUDE", ...).
# This helper calls the Anthropic SDK directly and translates request/response
# shapes between OpenAI Chat Completion and Anthropic Messages format.
#
# Coverage:
#   - text content (assistant + user, system message extraction)        ✓
#   - tools / function calling (definitions, tool_choice, tool_use,     ✓
#     tool_result, streaming input_json deltas)
#   - vision / image content blocks (passed through if caller sends     ~
#     Anthropic-format content arrays; OpenAI vision messages with
#     image_url not translated)
#   - computer-use, MCP tools, citations                                 ✗

def _is_anthropic_tier(tier: str) -> bool:
    # .get chain: callers pass ledger-bucket names too (e.g. the A6
    # "embeddings" bucket via record_cost → _genai_system), which have no
    # MODELS entry — treat unknown names as non-Anthropic, never KeyError.
    return MODELS.get(tier, {}).get("litellm_model", "").startswith("anthropic/")


# ─── Tool format translation ──────────────────────────────────────────────────
# OpenAI:   {type: "function", function: {name, description, parameters}}
# Anthropic: {name, description, input_schema}
# Both `parameters` and `input_schema` are JSON Schema, so the body copies
# unchanged. Anthropic requires `input_schema` to be a non-empty object — for
# tools that genuinely take no arguments, OpenAI sends `parameters: {}` which
# Anthropic rejects; pad with `{"type": "object", "properties": {}}`.
def _oai_tools_to_anthropic(openai_tools: list[dict] | None) -> list[dict]:
    if not openai_tools:
        return []
    out: list[dict] = []
    for t in openai_tools:
        if t.get("type") != "function":
            continue
        fn = t.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        schema = fn.get("parameters") or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or not schema.get("type"):
            schema = {"type": "object", "properties": {}}
        tool_dict: dict[str, Any] = {
            "name": name,
            "description": fn.get("description") or "",
            "input_schema": schema,
        }
        # Preserve Anthropic prompt-cache marker if the caller embedded one on
        # the tool envelope or its function block. The Anthropic SDK accepts a
        # `cache_control` field on the tool itself; mark the last tool in the
        # array to cache the entire tool-definitions block.
        cache_mark = t.get("cache_control") or fn.get("cache_control")
        if cache_mark:
            tool_dict["cache_control"] = cache_mark
        out.append(tool_dict)
    return out


# OpenAI tool_choice values: "none" | "auto" | "required" | {type:"function", function:{name}}
# Anthropic tool_choice:     {type:"auto"} | {type:"any"} | {type:"tool", name}
# OpenAI "none" → omit tools entirely from the call (handled by caller).
def _oai_tool_choice_to_anthropic(tc) -> dict | None:
    if tc is None or tc == "auto":
        return {"type": "auto"}
    if tc == "required":
        return {"type": "any"}
    if tc == "none":
        return None
    if isinstance(tc, dict) and tc.get("type") == "function":
        name = (tc.get("function") or {}).get("name")
        if name:
            return {"type": "tool", "name": name}
    return {"type": "auto"}


def _openai_to_anthropic_messages(
    messages: list[dict],
) -> tuple[str | list[dict] | None, list[dict]]:
    """Split out OpenAI-style system message and convert message stream to Anthropic.

    Handles:
      - system role → top-level `system`. If ANY system message ships as a
        list of content blocks (Anthropic-native shape, e.g. when the caller
        is marking a chunk with `cache_control` for prompt caching), the
        return type promotes to `list[dict]` and ALL system content gets
        normalized into blocks so cache markers survive. Otherwise stays a
        joined string for backward compatibility.
      - assistant.tool_calls → Anthropic tool_use content blocks
      - role="tool" (OpenAI tool result) → user message with tool_result block,
        contiguous tool messages are grouped into a single user message so the
        assistant↔user turn alternation Anthropic requires is preserved
    """
    system: str | list[dict] | None = None
    out: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        role = m.get("role")
        content = m.get("content")

        if role == "system":
            # Detect Anthropic-native block shape (list of {type, text,
            # cache_control?}) — promotes `system` to block form so cache
            # markers aren't stringified away.
            if isinstance(content, list):
                blocks = system if isinstance(system, list) else (
                    [{"type": "text", "text": system}] if system else []
                )
                blocks.extend(content)
                system = blocks
            elif isinstance(system, list):
                # Already promoted to blocks; append this string as a new text block.
                text = content if isinstance(content, str) else (
                    json.dumps(content) if content is not None else ""
                )
                system.append({"type": "text", "text": text})
            else:
                text = content if isinstance(content, str) else json.dumps(content)
                system = (system + "\n\n" + text) if system else text
            i += 1
            continue

        if role == "tool":
            # Group contiguous tool results into a single user message
            blocks: list[dict] = []
            while i < n and messages[i].get("role") == "tool":
                tm = messages[i]
                tcontent = tm.get("content")
                if not isinstance(tcontent, str):
                    tcontent = json.dumps(tcontent) if tcontent is not None else ""
                blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id") or "",
                    "content": tcontent,
                })
                i += 1
            out.append({"role": "user", "content": blocks})
            continue

        if role == "assistant":
            blocks: list[dict] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                # Already Anthropic-format content blocks
                blocks.extend(content)
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                args_raw = fn.get("arguments") or "{}"
                if isinstance(args_raw, str):
                    try:
                        args = json.loads(args_raw) if args_raw else {}
                    except json.JSONDecodeError:
                        # Some clients emit a non-JSON arguments string; pass
                        # through under a safe key so the model still sees it.
                        args = {"__raw__": args_raw}
                else:
                    args = args_raw if isinstance(args_raw, dict) else {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id") or "",
                    "name": fn.get("name") or "",
                    "input": args,
                })
            if not blocks:
                # Anthropic rejects empty content; emit a benign text block.
                blocks = [{"type": "text", "text": ""}]
            out.append({"role": "assistant", "content": blocks})
            i += 1
            continue

        if role == "user":
            if isinstance(content, str):
                out.append({"role": "user", "content": content})
            elif isinstance(content, list):
                out.append({"role": "user", "content": content})
            else:
                out.append({"role": "user", "content": str(content or "")})
            i += 1
            continue

        # Unknown role — skip rather than corrupt the alternation
        i += 1

    return system, out


def _anthropic_to_openai_response(resp, deployment: str) -> dict:
    """Translate Anthropic messages.create() result → OpenAI Chat Completion dict.

    Tool_use blocks become OpenAI tool_calls. When tool_use is present,
    message.content is set to null per the OpenAI convention (content and
    tool_calls are mutually presented; some clients dislike empty-string
    content alongside tool_calls).
    """
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in resp.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": getattr(block, "id", "") or "",
                "type": "function",
                "function": {
                    "name": getattr(block, "name", "") or "",
                    "arguments": json.dumps(getattr(block, "input", {}) or {}),
                },
            })

    finish_map = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    text = "".join(text_parts)
    message: dict[str, Any] = {"role": "assistant"}
    if tool_calls:
        message["content"] = text if text else None
        message["tool_calls"] = tool_calls
    else:
        message["content"] = text

    # Cache-token counters are optional on the SDK response object (only
    # populated when the request used cache_control). Surface them in BOTH
    # the OpenAI-standard `prompt_tokens_details.cached_tokens` slot AND as
    # Anthropic-native fields so downstream callers (Hermes, CostGuardian cost
    # tracker) can pick whichever convention they prefer.
    cache_create = getattr(resp.usage, "cache_creation_input_tokens", None) or 0
    cache_read = getattr(resp.usage, "cache_read_input_tokens", None) or 0
    usage: dict[str, Any] = {
        "prompt_tokens": resp.usage.input_tokens,
        "completion_tokens": resp.usage.output_tokens,
        "total_tokens": resp.usage.input_tokens + resp.usage.output_tokens,
    }
    if cache_create or cache_read:
        usage["prompt_tokens_details"] = {"cached_tokens": cache_read}
        usage["cache_creation_input_tokens"] = cache_create
        usage["cache_read_input_tokens"] = cache_read

    return {
        "id": resp.id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": deployment,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_map.get(resp.stop_reason or "end_turn", "stop"),
        }],
        "usage": usage,
    }


def _make_anthropic_client(cfg: dict) -> AsyncAnthropic:
    return AsyncAnthropic(api_key=cfg["api_key"], base_url=cfg["api_base"], timeout=cfg["timeout_seconds"])


def _build_anthropic_kwargs(cfg: dict, body: dict) -> dict[str, Any]:
    """Common kwargs assembly for both the streaming and non-streaming callers."""
    deployment = cfg["litellm_model"].split("/", 1)[1]
    system, messages = _openai_to_anthropic_messages(body["messages"])
    kwargs: dict[str, Any] = {
        "model": deployment,
        "messages": messages,
        "max_tokens": min(body.get("max_tokens", cfg["max_tokens"]), cfg["max_tokens"]),
        "temperature": body.get("temperature", 0.7),
    }
    if system:
        kwargs["system"] = system

    # tool_choice="none" means "don't allow tool use" — strip tools to enforce.
    tc_raw = body.get("tool_choice")
    if tc_raw != "none":
        atools = _oai_tools_to_anthropic(body.get("tools"))
        if atools:
            kwargs["tools"] = atools
            choice = _oai_tool_choice_to_anthropic(tc_raw)
            if choice is not None:
                kwargs["tool_choice"] = choice

    # Prompt-caching beta passthrough. The chat_completions endpoint copies the
    # `anthropic-beta` request header (and any caller-supplied `extra_headers`
    # in the JSON body) into `__router_extra_headers__` so we can forward them
    # to the Anthropic SDK here without changing the call signature. The SDK
    # merges these with its own defaults.
    extra_headers = body.get("__router_extra_headers__") or {}
    if extra_headers:
        kwargs["extra_headers"] = extra_headers
    return kwargs


async def _call_anthropic_direct(tier: str, body: dict) -> dict:
    cfg = MODELS[tier]
    deployment = cfg["litellm_model"].split("/", 1)[1]
    client = _make_anthropic_client(cfg)
    kwargs = _build_anthropic_kwargs(cfg, body)
    resp = await client.messages.create(**kwargs)
    return _anthropic_to_openai_response(resp, deployment)


# ─── Anthropic Messages API passthrough (native shape, no translation) ────────
# Hermes v0.14's _anthropic_prompt_cache_policy() only injects cache_control
# markers when api_mode == "anthropic_messages". Hosting a native /v1/messages
# route here lets PaperClip flip its Hermes config to anthropic_messages mode
# and start benefiting from cross-session prompt caching. The OpenAI-compat
# /v1/chat/completions route stays for non-Claude tiers and for any caller
# that doesn't want to switch.

def _select_anthropic_tier_for_model(body: dict) -> str:
    """Pick a tier for an Anthropic-native /v1/messages request.

    Honors explicit tier/metadata fields first (same precedence as the
    OpenAI-compat path's select_tier()). Falls back to the model name's
    matching tier in MODELS, then to the default CLAUDE tier as a safety net.
    Validates the selection is actually an Anthropic-backed tier.
    """
    explicit = (body.get("tier") or body.get("metadata", {}).get("tier", "")).lower()
    if explicit and explicit in MODELS and _is_anthropic_tier(explicit):
        return explicit

    raw_model = (body.get("model") or "").strip()
    if raw_model and raw_model in MODELS and _is_anthropic_tier(raw_model):
        return raw_model

    # Substring match against the CLAUDE tier's deployment name (handles e.g.
    # caller sending "claude-sonnet-4" when the registered tier is named
    # "claude-sonnet-4-6"). Falls through to the canonical "claude" tier id
    # registered via _register_foundry_tier.
    if raw_model and "claude" in raw_model.lower():
        if "claude" in MODELS:
            return "claude"
        for tier, cfg in MODELS.items():
            if _is_anthropic_tier(tier):
                return tier

    raise HTTPException(
        status_code=400,
        detail=(
            f"/v1/messages requires an Anthropic-backed model — "
            f"received '{raw_model or 'no model'}'. Use /v1/chat/completions for non-Claude tiers."
        ),
    )


def _build_messages_passthrough_kwargs(cfg: dict, body: dict, request: Request) -> dict[str, Any]:
    """Forward Anthropic-native body straight to AsyncAnthropic — no translation.

    The body shape is already what client.messages.create() expects. We only
    cap max_tokens to the tier's configured ceiling and merge inbound
    `anthropic-beta` / `extra_headers` (cache-TTL beta, etc.) into extra_headers.
    """
    deployment = cfg["litellm_model"].split("/", 1)[1]
    kwargs: dict[str, Any] = {
        "model": deployment,
        "messages": body["messages"],
        "max_tokens": min(body.get("max_tokens", cfg["max_tokens"]), cfg["max_tokens"]),
    }
    # Pass through every known Anthropic Messages field verbatim. Cache markers
    # ride along on system/messages/tools content blocks without any reshape.
    for k in ("system", "tools", "tool_choice", "temperature", "top_p", "top_k",
              "stop_sequences", "metadata", "thinking", "stream"):
        if k in body:
            kwargs[k] = body[k]

    # Merge anthropic-beta header(s) and any caller-supplied extra_headers from
    # the body. Header value wins for the canonical beta slot (OpenAI SDK
    # promotes extra_headers={"anthropic-beta": ...} to HTTP headers anyway).
    extra_headers: dict[str, str] = {}
    body_extra = body.get("extra_headers")
    if isinstance(body_extra, dict):
        for k, v in body_extra.items():
            if isinstance(k, str) and isinstance(v, (str, int, float)):
                extra_headers[k] = str(v)
    beta_hdr = request.headers.get("anthropic-beta")
    if beta_hdr:
        extra_headers["anthropic-beta"] = beta_hdr
    if extra_headers:
        kwargs["extra_headers"] = extra_headers

    return kwargs


async def _stream_anthropic_messages_sse(
    client, kwargs: dict[str, Any], tier: str, t_start: float,
    caller: str | None = None,
    breaker_verdict: "circuit_breaker.AdmitVerdict | None" = None,
):
    """Yield raw Anthropic SSE events as the upstream stream produces them.

    Each event is serialized in the same shape Anthropic's own /v1/messages
    SSE wire format uses, so Hermes' anthropic_messages transport receives
    exactly what it would from api.anthropic.com directly.
    """
    # stream=True is implicit when we use messages.create(stream=True) — strip
    # the literal "stream" key from kwargs before forwarding (the SDK takes
    # it as a separate parameter).
    kwargs = dict(kwargs)
    kwargs.pop("stream", None)
    # A6 usage capture for cost recording: message_start carries the input
    # token count, message_delta carries the (cumulative) output count.
    usage_acc = {"input_tokens": 0, "output_tokens": 0}
    try:
        async for event in await client.messages.create(stream=True, **kwargs):
            event_type = getattr(event, "type", "message")
            try:
                payload = event.model_dump(exclude_unset=False, mode="json")
            except Exception:
                # Older SDK style fallback
                payload = getattr(event, "to_dict", lambda: {})() or {}
            if event_type == "message_start":
                u = (payload.get("message") or {}).get("usage") or {}
                for k in usage_acc:
                    usage_acc[k] = u.get(k) or usage_acc[k]
            elif event_type == "message_delta":
                u = payload.get("usage") or {}
                usage_acc["output_tokens"] = u.get("output_tokens") or usage_acc["output_tokens"]
            yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
        # A6: streamed native Claude calls accrue to the daily ledger too —
        # this is the main production mode (Hermes anthropic_messages), so
        # leaving it untracked would leave enforcement blind on the exact
        # path it was built for.
        _record_anthropic_usage(
            tier, MODELS[tier]["litellm_model"].split("/", 1)[1],
            type("U", (), usage_acc)(), caller=caller,
        )
        # The upstream connection for this path is not established until the
        # stream is iterated, so this is the first point where the breaker can
        # honestly be scored for a native Messages stream.
        _record_dispatch_outcome(tier, breaker_verdict)
        log.info("messages_stream_ok tier=%s latency=%.2fs", tier, time.monotonic() - t_start)
    except Exception as e:
        _record_dispatch_outcome(tier, breaker_verdict, e)
        log.warning("messages_stream_failed tier=%s error=%s", tier, e)
        # aaf-0016: generic client-facing message; real error stays in the log.
        err_payload = {"type": "error", "error": {"type": "api_error", "message": "upstream provider error"}}
        yield f"event: error\ndata: {json.dumps(err_payload)}\n\n"


async def _stream_anthropic_direct(tier: str, body: dict):
    """Async generator yielding OpenAI-format SSE chunk dicts from an Anthropic stream.

    Iterates raw events (not just text_stream) so tool_use blocks can be
    streamed as OpenAI tool_calls deltas. Anthropic streams `content_block_start`
    (tool_use → id + name), `content_block_delta` (input_json_delta →
    partial_json), and `content_block_stop`. Tool indices are remapped from
    Anthropic content-block indices to sequential OpenAI tool_call indices
    so multi-tool responses present as tool_calls[0], [1], … to the caller.
    """
    cfg = MODELS[tier]
    deployment = cfg["litellm_model"].split("/", 1)[1]
    client = _make_anthropic_client(cfg)
    kwargs = _build_anthropic_kwargs(cfg, body)

    chunk_id = f"chatcmpl-{hashlib.sha1(str(time.time()).encode()).hexdigest()[:24]}"
    created = int(time.time())

    def _envelope(delta: dict, finish: str | None = None) -> dict:
        choice: dict[str, Any] = {"index": 0, "delta": delta}
        if finish:
            choice["finish_reason"] = finish
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": deployment,
            "choices": [choice],
        }

    # First chunk announces the assistant role (matches OpenAI streaming convention)
    yield _envelope({"role": "assistant", "content": ""})

    # Anthropic content_block index → OpenAI tool_call index (0, 1, …)
    tool_idx_map: dict[int, int] = {}
    next_tool_idx = 0
    final_stop: str | None = None

    async with client.messages.stream(**kwargs) as stream:
        async for event in stream:
            etype = getattr(event, "type", None)

            if etype == "content_block_start":
                block = getattr(event, "content_block", None)
                btype = getattr(block, "type", None) if block else None
                if btype == "tool_use":
                    cb_index = getattr(event, "index", 0)
                    tool_idx_map[cb_index] = next_tool_idx
                    yield _envelope({
                        "tool_calls": [{
                            "index": next_tool_idx,
                            "id": getattr(block, "id", "") or "",
                            "type": "function",
                            "function": {
                                "name": getattr(block, "name", "") or "",
                                "arguments": "",
                            },
                        }],
                    })
                    next_tool_idx += 1
                # text blocks: nothing to emit at start; deltas carry the text

            elif etype == "content_block_delta":
                delta = getattr(event, "delta", None)
                dtype = getattr(delta, "type", None) if delta else None
                cb_index = getattr(event, "index", 0)
                if dtype == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        yield _envelope({"content": text})
                elif dtype == "input_json_delta":
                    partial = getattr(delta, "partial_json", "") or ""
                    oai_idx = tool_idx_map.get(cb_index)
                    if oai_idx is not None:
                        yield _envelope({
                            "tool_calls": [{
                                "index": oai_idx,
                                "function": {"arguments": partial},
                            }],
                        })

            elif etype == "message_delta":
                # stop_reason becomes definitive here; capture for finish_reason
                msg_delta = getattr(event, "delta", None)
                if msg_delta is not None:
                    sr = getattr(msg_delta, "stop_reason", None)
                    if sr:
                        final_stop = sr

            # message_start / content_block_stop / message_stop: nothing to forward

        if final_stop is None:
            final = await stream.get_final_message()
            final_stop = final.stop_reason or "end_turn"

    finish_map = {
        "end_turn": "stop",
        "max_tokens": "length",
        "stop_sequence": "stop",
        "tool_use": "tool_calls",
    }
    yield _envelope({}, finish=finish_map.get(final_stop, "stop"))


# ─── Call Model (non-streaming) ───────────────────────────────────────────────
async def _call_model(tier: str, body: dict) -> dict:
    """Call model with retry for transient failures (cold-start, 429 bursts)."""
    is_anthropic = _is_anthropic_tier(tier)
    kwargs = None if is_anthropic else _build_completion_kwargs(tier, body, stream=False)
    # aaf-0005: per-caller cost attribution. The endpoint stashes the resolved
    # caller id under a private body key (mirrors __router_extra_headers__) so the
    # ledger accrues without changing this signature (keeps monkeypatched fakes
    # working). record_cost no-ops the caller ledger when this is None.
    caller = body.get("__router_caller__")
    last_err: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            if attempt > 0:
                await asyncio.sleep(_RETRY_DELAY * attempt)
                log.info("retry tier=%s attempt=%d/%d", tier, attempt + 1, _MAX_RETRIES + 1)

            if is_anthropic:
                result = await _call_anthropic_direct(tier, body)
                # Anthropic SDK doesn't surface response_cost — estimate from
                # list price so Claude calls are cost-tracked + observable (B3b).
                _model, _in, _out = _usage_from_result(result, fallback_tier=tier)
                _cost = _estimate_anthropic_cost(_model, _in, _out)
                record_cost(
                    tier, _cost,
                    model=_model, input_tokens=_in, output_tokens=_out,
                    caller=caller,
                )
            else:
                response = await litellm.acompletion(**kwargs)
                result = response.model_dump()
                _model, _in, _out = _usage_from_result(result, fallback_tier=tier)
                _cost = response._hidden_params.get("response_cost") or 0.0
                record_cost(
                    tier, _cost,
                    model=_model, input_tokens=_in, output_tokens=_out,
                    caller=caller,
                )
            # Flight recorder (private key, same convention as
            # __router_caller__ / __router_extra_headers__): stash this
            # attempt's cost so the endpoint can attach it to a flight event
            # without recomputing or changing this function's return shape.
            result["__router_cost_usd__"] = _cost

            for choice in result.get("choices", []):
                msg = choice.get("message") or {}
                raw = msg.get("content") or ""
                if raw:
                    msg["content"] = _sanitise_content(raw)

            return result
        except Exception as e:
            last_err = e
            log.warning("call_failed tier=%s attempt=%d error=%s", tier, attempt + 1, e)

    raise last_err  # type: ignore[misc]


# ─── Call Model (streaming) ───────────────────────────────────────────────────
async def _open_stream(tier: str, body: dict):
    """Establish the upstream streaming connection with retry.
    Returns either a LiteLLM stream iterator (openai-format tiers) or an async
    generator of pre-built openai-format chunk dicts (anthropic-format tiers).
    Raises on persistent failure so the caller can try fallback tiers."""
    is_anthropic = _is_anthropic_tier(tier)

    if is_anthropic:
        # The Anthropic SDK's stream needs to live across the iteration in
        # _iter_stream, so we hand back the unconsumed async generator.
        # Per-attempt retry would need to re-enter the SDK's context manager,
        # so for now we trust the SDK's own retry config (default 2 retries).
        return _stream_anthropic_direct(tier, body)

    kwargs = _build_completion_kwargs(tier, body, stream=True)
    last_err: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            if attempt > 0:
                await asyncio.sleep(_RETRY_DELAY * attempt)
                log.info("stream_retry tier=%s attempt=%d/%d", tier, attempt + 1, _MAX_RETRIES + 1)
            return await litellm.acompletion(**kwargs)
        except Exception as e:
            last_err = e
            log.warning("stream_open_failed tier=%s attempt=%d error=%s", tier, attempt + 1, e)

    raise last_err  # type: ignore[misc]


async def _iter_stream(stream_iter, tier: str):
    """
    Yield SSE lines from an upstream stream.

    Accepts two shapes:
      - LiteLLM async iterator yielding objects with .model_dump()
      - Plain async generator yielding pre-built openai-format chunk dicts
        (used by the anthropic-direct bypass).

    If the upstream stream fails mid-flight, emit an error event and end cleanly.
    """
    is_anthropic = _is_anthropic_tier(tier)
    try:
        async for chunk in stream_iter:
            chunk_dict = chunk if is_anthropic else chunk.model_dump()

            for choice in chunk_dict.get("choices", []):
                delta = choice.get("delta") or {}
                raw = delta.get("content") or ""
                if raw:
                    delta["content"] = _sanitise_content(raw, strip=False)

            yield f"data: {json.dumps(chunk_dict)}\n\n"

        yield "data: [DONE]\n\n"

    except Exception as e:
        log.warning("stream_failed tier=%s error=%s", tier, e)
        log.debug("stream_failed_traceback tier=%s\n%s", tier, traceback.format_exc())
        # aaf-0016: generic client-facing error; the real exception is logged above.
        yield f"data: {json.dumps({'error': 'upstream provider error', 'tier': tier})}\n\n"
        yield "data: [DONE]\n\n"


# ─── FastAPI Endpoints ────────────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _verify_auth(request)
    _check_rate_limit(request)
    t_start = time.monotonic()
    body = await request.json()
    _validate_request(body)

    # aaf-0005: wire the per-caller cost ledger into the request path. Resolve the
    # caller (agent/tenant) id, reject over-budget callers (429) BEFORE spending on
    # platform credentials, and stash the id so _call_model -> record_cost accrues
    # it. Fails open when PER_CALLER_DAILY_USD is unset or the request is
    # unattributed (is_over_caller_budget returns False), so this can never block
    # traffic when cost governance is not configured.
    caller = resolve_caller_id(request.headers)
    if is_over_caller_budget(caller):
        raise HTTPException(status_code=429, detail="Per-caller daily budget exceeded")
    if caller:
        body["__router_caller__"] = caller

    # Flight Recorder + Waste Breakers: gather the per-request facts once
    # (fingerprint, token estimate, correlation id) and evaluate waste
    # patterns BEFORE any tier/budget spend — a 429 here (enforce mode only;
    # default is observe-only) never touches a model credential.
    fc_ctx = _flight_context(
        endpoint="chat_completions", body=body, headers=request.headers, caller=caller,
    )
    breaker_verdicts = _apply_waste_breakers(fc_ctx, t_start)

    tier = select_tier(body)
    # A6: select_tier stashes a __budget_downgrade__ marker in the body when
    # enforcement swapped the tier; surface it as a response header + _router
    # metadata so callers can see the downgrade (empty dicts otherwise).
    bd_headers = _budget_downgrade_headers(body)
    bd_meta = _budget_downgrade_meta(body)
    stream = bool(body.get("stream", False))

    # Capture prompt-cache (and other Anthropic-beta) markers BEFORE handing the
    # body to per-tier callers. The OpenAI client convention is to send these
    # either as an HTTP header (`anthropic-beta`) OR as a JSON body field
    # (`extra_headers: {...}` via OpenAI SDK's extra_body). Accept either,
    # merge into a private body slot that _build_anthropic_kwargs reads.
    extra_headers: dict[str, str] = {}
    body_extra = body.get("extra_headers")
    if isinstance(body_extra, dict):
        for k, v in body_extra.items():
            if isinstance(k, str) and isinstance(v, (str, int, float)):
                extra_headers[k] = str(v)
    beta_hdr = request.headers.get("anthropic-beta")
    if beta_hdr:
        # Header value wins over body for the canonical beta slot — clients
        # using the OpenAI SDK's `extra_headers={"anthropic-beta": ...}` end
        # up here anyway since the SDK promotes those to HTTP headers.
        extra_headers["anthropic-beta"] = beta_hdr
    if extra_headers:
        body["__router_extra_headers__"] = extra_headers

    estimated = fc_ctx["prompt_tokens_estimated"]
    requested_max = body.get("max_tokens", MODELS[tier]["max_tokens"])

    if not _fits_model(tier, estimated, requested_max):
        raise HTTPException(status_code=413, detail="Request exceeds model context limit")

    fallback_chain = _build_fallback_chain(tier, estimated, requested_max)

    log.info(
        "routing tier=%s stream=%s fallback=%s input_tokens≈%s tools=%s max_tokens=%s",
        tier,
        stream,
        fallback_chain,
        estimated,
        len(body.get("tools", []) or []),
        requested_max,
    )

    if stream:
        last_error: Exception | None = None
        # A resilience refusal is remembered, not raised inline: a later
        # candidate may still be servable (a free local tier is exempt from
        # the paid-fallback rules), and only if the whole chain is exhausted
        # does the typed 503 become the response. What it must never do is
        # collapse into the generic 502 below — "spending is halted" and
        # "every model errored" are different incidents.
        blocked: ResilienceBlocked | None = None

        for position, candidate in enumerate([tier] + fallback_chain):
            try:
                stream_iter = await _guarded_open_stream(
                    candidate, body, primary_tier=tier, is_fallback=position > 0,
                )
            except ResilienceBlocked as rb:
                blocked = rb
                continue
            except Exception as e:
                last_error = e
                log.warning("stream_init_failed tier=%s error=%s", candidate, e)
                log.debug(
                    "stream_init_failed_traceback tier=%s\n%s",
                    candidate,
                    traceback.format_exc(),
                )
                continue

            log.info(
                "streaming tier=%s latency=%.2fs",
                candidate,
                time.monotonic() - t_start,
            )
            # Streaming responses are consumed by the caller after we
            # return, so real usage/cost isn't known here — record what
            # IS known now (tier, tokens estimate, latency-to-open) and
            # flag it as an estimate via streamed=True.
            _emit_flight_event(
                fc_ctx, t_start=t_start,
                outcome=(
                    flight_recorder.OUTCOME_DOWNGRADED if candidate != tier
                    else flight_recorder.OUTCOME_SUCCESS
                ),
                served_tier=candidate, served_model=MODELS.get(candidate, {}).get("litellm_model"),
                input_tokens=estimated, breaker_verdicts=breaker_verdicts, streamed=True,
            )
            return StreamingResponse(
                _iter_stream(stream_iter, candidate),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    **bd_headers,
                },
            )

        if blocked is not None:
            _emit_flight_event(
                fc_ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
                input_tokens=estimated, error_class=f"resilience_block:{blocked.code}",
                breaker_verdicts=breaker_verdicts, streamed=True,
            )
            raise blocked

        # aaf-0016: don't echo the raw last_error string to the client; it's logged
        # per-tier above.
        _emit_flight_event(
            fc_ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
            input_tokens=estimated, error_class="all_tiers_failed_to_stream",
            breaker_verdicts=breaker_verdicts, streamed=True,
        )
        raise HTTPException(
            status_code=502,
            detail="All tiers failed to initialise stream",
        )

    # Non-streaming path. One loop over [primary] + fallbacks so every hop goes
    # through the same resilience gate; position 0 is the caller's originally
    # selected tier, everything after it is a fallback dispatch.
    blocked = None
    for position, candidate in enumerate([tier] + fallback_chain):
        try:
            result = await _guarded_call_model(
                candidate, body, primary_tier=tier, is_fallback=position > 0,
            )
        except ResilienceBlocked as rb:
            blocked = rb
            continue
        except Exception as e:
            if position == 0:
                log.warning("primary_failed tier=%s error=%s", candidate, e)
                log.debug("primary_failed_traceback tier=%s\n%s", candidate, traceback.format_exc())
            else:
                log.warning("fallback_failed tier=%s error=%s", candidate, e)
                log.debug(
                    "fallback_failed_traceback tier=%s\n%s", candidate, traceback.format_exc()
                )
            continue

        router_meta: dict[str, Any] = {"tier": candidate}
        if position > 0:
            router_meta["fallback_from"] = tier
        router_meta["estimated_input_tokens"] = estimated
        router_meta.update(bd_meta)
        result["_router"] = router_meta
        log.info(
            "%s tier=%s latency=%.2fs",
            "success" if position == 0 else "fallback_success",
            candidate, time.monotonic() - t_start,
        )
        _model, _in, _out = _usage_from_result(result, fallback_tier=candidate)
        _emit_flight_event(
            fc_ctx, t_start=t_start,
            outcome=(
                flight_recorder.OUTCOME_DOWNGRADED
                if (position > 0 or bd_meta)
                else flight_recorder.OUTCOME_SUCCESS
            ),
            served_tier=candidate, served_model=_model, input_tokens=_in, output_tokens=_out,
            cost_usd=result.pop("__router_cost_usd__", 0.0), breaker_verdicts=breaker_verdicts,
        )
        return JSONResponse(content=result, headers=bd_headers)

    if blocked is not None:
        _emit_flight_event(
            fc_ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
            input_tokens=estimated, error_class=f"resilience_block:{blocked.code}",
            breaker_verdicts=breaker_verdicts,
        )
        raise blocked

    _emit_flight_event(
        fc_ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
        input_tokens=estimated, error_class="all_tiers_failed",
        breaker_verdicts=breaker_verdicts,
    )
    raise HTTPException(status_code=502, detail="All tiers failed")


@app.post("/v1/messages")
async def messages(request: Request):
    """Anthropic-native Messages API endpoint.

    Accepts the Anthropic /v1/messages body shape directly — no OpenAI →
    Anthropic translation. Forwards verbatim to AsyncAnthropic, including
    `cache_control` markers on system/messages/tools and any `anthropic-beta`
    headers from the caller. This is the cache-friendly path; Hermes v0.14
    only injects cache markers when `api_mode == "anthropic_messages"`, so
    PaperClip's Hermes config should point at this endpoint for Claude calls.
    """
    _verify_auth(request)
    _check_rate_limit(request)
    t_start = time.monotonic()
    body = await request.json()

    # Reuse the same shape validation (messages list, max_tokens, etc.).
    _validate_request(body)
    if body.get("max_tokens") is None:
        raise HTTPException(status_code=400, detail="Request must include max_tokens")

    # aaf-0005: same per-caller budget gate as /v1/chat/completions.
    caller = resolve_caller_id(request.headers)
    if is_over_caller_budget(caller):
        raise HTTPException(status_code=429, detail="Per-caller daily budget exceeded")

    # Flight Recorder + Waste Breakers: same pre-spend check as
    # /v1/chat/completions — see that endpoint for the rationale.
    fc_ctx = _flight_context(
        endpoint="messages", body=body, headers=request.headers, caller=caller,
    )
    breaker_verdicts = _apply_waste_breakers(fc_ctx, t_start)

    tier = _select_anthropic_tier_for_model(body)
    if not _is_anthropic_tier(tier):
        # _select_anthropic_tier_for_model already raises 400 for non-Anthropic
        # selections, but defend against tier table edits that could slip a
        # non-Anthropic tier through.
        raise HTTPException(
            status_code=400,
            detail=f"Tier {tier!r} is not an Anthropic-backed tier",
        )

    # Acting budget enforcement (A6): the native Messages path bypasses
    # select_tier (tier comes from _select_anthropic_tier_for_model above) —
    # the exact gap class that let the upstream deployment's Claude tier
    # overrun its daily budget with only WARN lines — so it gets an explicit
    # check here. The response must stay Anthropic-shaped end to end, so a
    # downgrade can only serve another Anthropic-backed tier: the configured
    # fallback participates only when it is Anthropic-backed; otherwise
    # decide() sees no usable fallback and degrades to warn (a non-servable
    # fallback must never strand a request). block returns an Anthropic-shaped
    # 429 so anthropic_messages transports parse it like any upstream error.
    _msg_fallback = (
        _BUDGET_FALLBACK_TIER
        if _BUDGET_FALLBACK_TIER in MODELS and _is_anthropic_tier(_BUDGET_FALLBACK_TIER)
        else None
    )
    decision = budget_enforcement.decide(
        tier=tier,
        over_budget=is_over_budget(tier),
        mode=_BUDGET_ENFORCE_MODE,
        fallback_tier=_msg_fallback,
        registered_tiers=MODELS,
        spent_usd=_spend.get(tier, 0.0),
        limit_usd=MODELS[tier]["daily_budget"],
    )
    bd_headers: dict[str, str] = {}
    bd_meta: dict[str, str] = {}
    if decision.action == budget_enforcement.ACTION_BLOCK:
        log.warning("budget_enforce_block tier=%s %s (messages)", tier, decision.reason)
        _emit_flight_event(
            fc_ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
            error_class="budget_blocked", breaker_verdicts=breaker_verdicts,
        )
        return JSONResponse(
            status_code=429,
            content={
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": decision.reason,
                    "detail": budget_enforcement.block_detail(decision),
                },
            },
        )
    if decision.action == budget_enforcement.ACTION_DOWNGRADE:
        log.warning("budget_enforce_downgrade tier=%s→%s %s (messages)",
                    tier, decision.serve_tier, decision.reason)
        name, value = budget_enforcement.downgrade_header(decision)
        bd_headers[name] = value
        bd_meta["budget_downgraded_from"] = tier
        tier = decision.serve_tier
    elif decision.action == budget_enforcement.ACTION_WARN:
        log.warning("budget_enforce mode=%s tier=%s %s (messages)",
                    _BUDGET_ENFORCE_MODE, tier, decision.reason)

    cfg = MODELS[tier]
    stream = bool(body.get("stream", False))

    estimated = fc_ctx["prompt_tokens_estimated"]
    requested_max = body.get("max_tokens", cfg["max_tokens"])
    if not _fits_model(tier, estimated, requested_max):
        raise HTTPException(status_code=413, detail="Request exceeds model context limit")

    log.info(
        "messages tier=%s stream=%s input_tokens≈%s tools=%s max_tokens=%s",
        tier, stream, estimated, len(body.get("tools", []) or []), requested_max,
    )

    client = _make_anthropic_client(cfg)
    kwargs = _build_messages_passthrough_kwargs(cfg, body, request)

    # Resilience gate. This endpoint has no fallback chain — the response must
    # stay Anthropic-shaped end to end — so only the `all_paid` kill-switch
    # scope and this tier's own breaker can refuse here. The refusal is
    # returned in the Anthropic error envelope for the same reason the budget
    # block above is: an anthropic_messages transport must be able to parse it
    # like any other upstream error.
    try:
        breaker_verdict = _resilience_gate(tier, primary_tier=tier, is_fallback=False)
    except ResilienceBlocked as rb:
        _emit_flight_event(
            fc_ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
            served_tier=tier, error_class=f"resilience_block:{rb.code}",
            breaker_verdicts=breaker_verdicts,
        )
        return JSONResponse(
            status_code=503,
            content={
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": rb.detail.get("message", "dispatch refused"),
                    "detail": rb.detail,
                },
            },
            headers=rb.headers,
        )

    if stream:
        _emit_flight_event(
            fc_ctx, t_start=t_start,
            outcome=flight_recorder.OUTCOME_DOWNGRADED if bd_meta else flight_recorder.OUTCOME_SUCCESS,
            served_tier=tier, served_model=cfg["litellm_model"].split("/", 1)[-1],
            input_tokens=estimated, breaker_verdicts=breaker_verdicts, streamed=True,
        )
        return StreamingResponse(
            _stream_anthropic_messages_sse(
                client, kwargs, tier, t_start, caller=caller,
                breaker_verdict=breaker_verdict,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                **bd_headers,
            },
        )

    # Non-streaming path. The SDK's stream kwarg is treated as a separate arg;
    # strip it before forwarding (we already branched on it).
    kwargs.pop("stream", None)
    try:
        resp = await client.messages.create(**kwargs)
    except Exception as e:
        _record_dispatch_outcome(tier, breaker_verdict, e)
        log.warning("messages_failed tier=%s error=%s", tier, e)
        log.debug("messages_failed_traceback tier=%s\n%s", tier, traceback.format_exc())
        # Surface Anthropic-shaped error so callers' anthropic_messages
        # transport can parse the payload the same way it parses upstream
        # errors. status_code may not always be on the exception — default 502.
        status = getattr(e, "status_code", 502) or 502
        # aaf-0016: preserve the Anthropic-shaped error envelope callers parse, but
        # return a generic message instead of the raw exception string (which can
        # carry endpoint/internal detail). The real error is logged above.
        err_body = {
            "type": "error",
            "error": {"type": "api_error", "message": "upstream provider error"},
        }
        _emit_flight_event(
            fc_ctx, t_start=t_start, outcome=flight_recorder.OUTCOME_ERROR,
            served_tier=tier, error_class=type(e).__name__, breaker_verdicts=breaker_verdicts,
        )
        return JSONResponse(status_code=status, content=err_body)

    _record_dispatch_outcome(tier, breaker_verdict)

    # A6: accrue this call's spend to the daily ledger — the native path never
    # goes through _call_model, so without this the enforcement above would
    # never see the spend it is supposed to act on.
    _deployment = cfg["litellm_model"].split("/", 1)[1]
    _record_anthropic_usage(tier, _deployment, getattr(resp, "usage", None), caller=caller)

    try:
        result = resp.model_dump(exclude_unset=False, mode="json")
    except Exception:
        result = getattr(resp, "to_dict", lambda: {})() or {}
    result["_router"] = {"tier": tier, "estimated_input_tokens": estimated, **bd_meta}
    log.info("messages success tier=%s latency=%.2fs", tier, time.monotonic() - t_start)
    _usage = getattr(resp, "usage", None)
    _in = int(getattr(_usage, "input_tokens", 0) or 0)
    _out = int(getattr(_usage, "output_tokens", 0) or 0)
    _emit_flight_event(
        fc_ctx, t_start=t_start,
        outcome=flight_recorder.OUTCOME_DOWNGRADED if bd_meta else flight_recorder.OUTCOME_SUCCESS,
        served_tier=tier, served_model=_deployment, input_tokens=_in, output_tokens=_out,
        cost_usd=_estimate_anthropic_cost(_deployment, _in, _out), breaker_verdicts=breaker_verdicts,
    )
    return JSONResponse(content=result, headers=bd_headers)


# ─── Embeddings ──────────────────────────────────────────────────────────────
# The memory governor's Plane C vector retrieval embeds its query through here
# so the "never call a provider directly" principle holds and the model is
# pinned to match Honcho's document embeddings (same 1536-dim vector space).
# Disabled (503) unless an embedding key is set — fail LOUD, never degrade
# silently. Provider-flexible: EMBEDDING_BASE_URL unset -> api.openai.com;
# point it at any OpenAI-compatible deployment of the SAME model instead
# (e.g. an Azure AI Foundry text-embedding-3-small deployment — see
# docs/walkthroughs/azure-foundry-embeddings.md), so forks don't carry a hard
# OpenAI-billing dependency.
_EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
_EMBED_API_KEY = _tier_env("EMBEDDING_API_KEY") or _tier_env("OPENAI_API_KEY")
_EMBED_API_BASE = os.environ.get("EMBEDDING_BASE_URL") or None
_EMBED_TIMEOUT_S = int(os.environ.get("EMBEDDING_TIMEOUT_SECONDS", "20"))
_EMBED_MAX_INPUTS = int(os.environ.get("EMBEDDING_MAX_INPUTS", "256"))

# ── Embeddings daily budget (A6 acting enforcement) ──────────────────────────
# /v1/embeddings bypasses tier selection entirely (no MODELS entry), so its
# spend was previously invisible to cost governance — the same "spend with no
# acting control" gap class A6 closes for the chat tiers. Embeddings spend now
# accrues to a dedicated "embeddings" ledger bucket (it shows up in
# daily_cost_rollup like any tier), capped by EMBEDDING_DAILY_BUDGET_USD; a
# cap of 0 disables the ceiling (spend is still tracked), mirroring
# PER_CALLER_DAILY_USD's convention. There is no meaningful downgrade target —
# a different embedding model would land queries in a different vector space
# than the stored document embeddings (the whole point of the model pin) — so
# in downgrade mode this path degrades to warn (decide() gets
# fallback_tier=None); block mode 429s with the same machine-readable body as
# the chat paths. Cost comes from LiteLLM's response_cost when available, else
# a list-price token estimate at EMBEDDING_PRICE_PER_MTOK (default matches
# text-embedding-3-small; adjust it alongside EMBEDDING_MODEL).
_EMBED_LEDGER_BUCKET = "embeddings"
_EMBED_DAILY_BUDGET_USD = float(os.environ.get("EMBEDDING_DAILY_BUDGET_USD", "1.00") or 0)
_EMBED_PRICE_PER_MTOK = float(os.environ.get("EMBEDDING_PRICE_PER_MTOK", "0.02") or 0)
log.info(
    "budget_enforce embeddings_daily_budget_usd=%.2f (0 disables the cap)",
    _EMBED_DAILY_BUDGET_USD,
)


def is_embeddings_over_budget() -> bool:
    """True when the embeddings bucket has reached its daily cap. A cap of 0
    (or negative) disables the ceiling — mirrors PER_CALLER_DAILY_USD."""
    if _EMBED_DAILY_BUDGET_USD <= 0:
        return False
    _reset_if_new_day()
    return _spend.get(_EMBED_LEDGER_BUCKET, 0.0) >= _EMBED_DAILY_BUDGET_USD


def _embedding_cost(resp, payload: dict) -> tuple[float, int]:
    """(cost_usd, input_tokens) for one embeddings call. Prefers LiteLLM's
    response_cost; falls back to a list-price token estimate. Defaults
    gracefully — it runs on the hot path after a successful upstream call."""
    usage = payload.get("usage") or {}
    try:
        tokens = int(usage.get("prompt_tokens") or usage.get("total_tokens") or 0)
    except (TypeError, ValueError):
        tokens = 0
    try:
        cost = float((getattr(resp, "_hidden_params", None) or {}).get("response_cost") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0:
        cost = tokens / 1_000_000 * _EMBED_PRICE_PER_MTOK
    return cost, tokens


def _pin_embedding_provider(model: str) -> str:
    """Pin LiteLLM's provider detection to the OpenAI-compatible Bearer path.

    LOAD-BEARING — ported from the upstream private deployment's incident
    learnings. When EMBEDDING_BASE_URL points at an azure.com host and the
    model string carries no ``provider/`` prefix, LiteLLM's provider detection
    flips to its AZURE provider and authenticates with an ``api-key`` header.
    Azure AI Foundry's OpenAI-compatible ``/openai/v1`` endpoint rejects that
    request with ``400 unknown_model``. Prepending ``openai/`` pins detection
    to the OpenAI-compatible provider, so auth stays ``Authorization: Bearer``
    and the Foundry deployment resolves.

    An explicit ``provider/`` prefix in EMBEDDING_MODEL is honored unchanged
    (e.g. ``azure/<deployment>`` for a classic Azure OpenAI resource that
    genuinely wants api-key-header auth).
    """
    if "/" in model:
        return model
    return f"openai/{model}"


# What is actually handed to LiteLLM; _EMBED_MODEL stays the operator-facing
# model name (and the `model` field reported back to callers).
_EMBED_LITELLM_MODEL = _pin_embedding_provider(_EMBED_MODEL)


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    """OpenAI-compatible embeddings passthrough.

    Used by the memory governor's Plane C vector retrieval; pins the model so the
    query embedding lands in the same space as Honcho's document embeddings.
    503 when no embedding key is configured (clean fork/disable)."""
    _verify_auth(request)
    _check_rate_limit(request)
    if not _EMBED_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="embeddings not configured (set EMBEDDING_API_KEY or OPENAI_API_KEY)",
        )
    body = await request.json()
    inp = body.get("input")
    if inp is None or (isinstance(inp, str) and not inp.strip()):
        raise HTTPException(status_code=400, detail="'input' is required")
    if isinstance(inp, list) and len(inp) > _EMBED_MAX_INPUTS:
        raise HTTPException(status_code=400, detail=f"too many inputs (max {_EMBED_MAX_INPUTS})")

    # aaf-0005: same per-caller budget gate as the chat endpoints — a caller
    # over its daily cap must not keep spending through the embeddings path.
    caller = resolve_caller_id(request.headers)
    if is_over_caller_budget(caller):
        raise HTTPException(status_code=429, detail="Per-caller daily budget exceeded")

    # Acting budget enforcement (A6). fallback_tier=None: there is no
    # same-vector-space downgrade target (see the bucket's comment block), so
    # downgrade mode degrades to warn here; block refuses with the same
    # machine-readable 429 body as the chat paths.
    decision = budget_enforcement.decide(
        tier=_EMBED_LEDGER_BUCKET,
        over_budget=is_embeddings_over_budget(),
        mode=_BUDGET_ENFORCE_MODE,
        fallback_tier=None,
        registered_tiers=MODELS,
        spent_usd=_spend.get(_EMBED_LEDGER_BUCKET, 0.0),
        limit_usd=_EMBED_DAILY_BUDGET_USD,
    )
    if decision.action == budget_enforcement.ACTION_BLOCK:
        log.warning("budget_enforce_block tier=%s %s (embeddings)",
                    _EMBED_LEDGER_BUCKET, decision.reason)
        raise HTTPException(status_code=429, detail=budget_enforcement.block_detail(decision))
    if decision.action != budget_enforcement.ACTION_ALLOW:
        log.warning("budget_enforce mode=%s tier=%s %s (embeddings)",
                    _BUDGET_ENFORCE_MODE, _EMBED_LEDGER_BUCKET, decision.reason)

    try:
        # _EMBED_LITELLM_MODEL (not _EMBED_MODEL): the openai/ prefix pin is
        # load-bearing against azure.com api_bases — see _pin_embedding_provider.
        resp = await litellm.aembedding(
            model=_EMBED_LITELLM_MODEL,
            input=inp,
            api_key=_EMBED_API_KEY,
            api_base=_EMBED_API_BASE,
            timeout=_EMBED_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        # aaf-0016: log the real upstream error, return a generic message so the
        # provider's exception text (endpoint, key hints, internals) isn't echoed.
        log.warning("embeddings call failed: %s", exc)
        raise HTTPException(status_code=502, detail="embedding provider error")
    try:
        payload = resp.model_dump()
    except AttributeError:
        payload = resp if isinstance(resp, dict) else dict(resp)

    # A6: embeddings spend accrues to the ledger (dedicated bucket + the
    # per-caller ledger when the request is attributed).
    cost, tokens = _embedding_cost(resp, payload)
    record_cost(
        _EMBED_LEDGER_BUCKET, cost, model=_EMBED_MODEL, input_tokens=tokens,
        caller=caller, correlation_id=resolve_correlation_id(request.headers),
    )

    data = payload.get("data") or []
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": d.get("index", i), "embedding": list(d["embedding"])}
            for i, d in enumerate(data)
        ],
        "model": payload.get("model") or _EMBED_MODEL,
        "usage": payload.get("usage") or {},
    }


@app.get("/health")
async def health():
    # aaf-0016: /health is unauthenticated (liveness/readiness probe), so it must
    # not leak per-tier dollar spend or configured budgets — those disclose usage
    # volume and cost posture to anyone who can reach the endpoint. Report only a
    # per-tier over_budget boolean (what a probe actually needs). The authoritative
    # spend figures stay on the in-process ledger (daily_cost_rollup / record_cost),
    # reachable only in-process, not over HTTP.
    _reset_if_new_day()
    # Resilience state rides alongside `tiers` rather than inside it: an
    # operator watching a credential outage needs "which upstreams is the
    # router refusing to call, and is the kill switch engaged" from the same
    # probe that tells them the service is alive. Breaker keys are salted
    # per-process fingerprints (no hostname, no key material), and per-key trip
    # history stays on the auth-gated /debug/circuit-breakers route.
    return {
        "status": "ok",
        "date": _budget_date,
        "tiers": {t: {"over_budget": is_over_budget(t)} for t in MODELS},
        **_resilience_status(include_keys=False),
    }


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": [{"id": k} for k in MODELS]}


@app.get("/version")
async def version():
    return {"version": "router-1.1.1"}


@app.get("/v1/models/{model_id}")
async def get_model(model_id: str):
    if model_id in MODELS:
        return {"id": model_id, "object": "model"}
    raise HTTPException(status_code=404, detail="Model not found")


# ─── Flight Recorder debug surface ────────────────────────────────────────────
# One-command way to inspect the trace: `curl -H "Authorization: Bearer
# $ROUTER_API_KEY" http://localhost:8080/debug/flight-recorder`. Auth-gated
# like every /v1/* route — traces carry caller ids and cost estimates, so
# this must not be reachable without the router credential (same posture
# note as /health's redacted spend figures).
@app.get("/debug/flight-recorder")
async def debug_flight_recorder(request: Request, limit: int = 50, caller: str | None = None):
    _verify_auth(request)
    if _flight_recorder is None:
        raise HTTPException(
            status_code=404,
            detail="Flight recorder is disabled (FLIGHT_RECORDER_ENABLED=false)",
        )
    return {
        "enabled": True,
        "stats": _flight_recorder.stats(),
        "waste_breakers": {
            "enabled": WASTE_BREAKERS_ENABLED,
            "enforce_mode": _WASTE_BREAKER_ENFORCE_MODE,
            "thresholds": {
                "repeated_identical_calls": _WASTE_BREAKER_THRESHOLDS.repeated_identical_calls,
                "repeated_identical_window_seconds":
                    _WASTE_BREAKER_THRESHOLDS.repeated_identical_window_seconds,
                "retry_storm_calls": _WASTE_BREAKER_THRESHOLDS.retry_storm_calls,
                "retry_storm_window_seconds": _WASTE_BREAKER_THRESHOLDS.retry_storm_window_seconds,
                "oversized_prompt_tokens": _WASTE_BREAKER_THRESHOLDS.oversized_prompt_tokens,
                "consecutive_failures": _WASTE_BREAKER_THRESHOLDS.consecutive_failures,
            },
        },
        "recent": _flight_recorder.recent(limit=limit, caller=caller),
    }


@app.get("/debug/flight-recorder/{event_id}")
async def debug_flight_recorder_event(event_id: str, request: Request):
    _verify_auth(request)
    if _flight_recorder is None:
        raise HTTPException(
            status_code=404,
            detail="Flight recorder is disabled (FLIGHT_RECORDER_ENABLED=false)",
        )
    event = _flight_recorder.get(event_id)
    if event is None:
        raise HTTPException(
            status_code=404,
            detail="Event not found (may have rotated out of the ring buffer)",
        )
    return event


# ─── Resilience operator surface ──────────────────────────────────────────────
# Read routes are auth-gated with ROUTER_API_KEY, the same posture as the
# /debug/flight-recorder routes. The MUTATING routes additionally require
# X-Router-Admin-Key when ROUTER_ADMIN_API_KEY is set — see _verify_admin.

def _verify_admin(request: Request) -> None:
    """Gate a state-changing operator action.

    Always requires the ordinary router credential first. When
    ROUTER_ADMIN_API_KEY is configured it ALSO requires a matching
    X-Router-Admin-Key header, which is what separates "may call the router"
    from "may turn the router's spend controls off". Left unset, this behaves
    exactly like the existing /debug/* routes — deliberately, so the feature
    is usable out of the box — but a deployment where every agent holds
    ROUTER_API_KEY should set it.
    """
    _verify_auth(request)
    if not _ROUTER_ADMIN_API_KEY:
        return
    provided = (request.headers.get("x-router-admin-key") or "").strip()
    if not provided:
        raise HTTPException(
            status_code=403,
            detail="Missing X-Router-Admin-Key (required when ROUTER_ADMIN_API_KEY is set)",
        )
    expected = hashlib.sha256(_ROUTER_ADMIN_API_KEY.encode()).digest()
    if not hmac.compare_digest(expected, hashlib.sha256(provided.encode()).digest()):
        raise HTTPException(status_code=403, detail="Invalid admin key")


@app.get("/debug/circuit-breakers")
async def debug_circuit_breakers(request: Request):
    _verify_auth(request)
    return _resilience_status(include_keys=True)


@app.post("/debug/circuit-breakers/reset")
async def debug_circuit_breakers_reset(request: Request):
    """Force breakers back to CLOSED after the underlying problem is fixed —
    rotating a credential shouldn't mean waiting out a cooldown. Body may carry
    {"key": "cred:..."} to reset one; omit it to reset all."""
    _verify_admin(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — an empty body means "reset everything"
        body = {}
    key = (body or {}).get("key")
    cleared = _breakers.reset(key if isinstance(key, str) and key else None)
    log.warning("circuit_breaker_reset key=%s cleared=%s", key or "(all)", cleared)
    return {"ok": True, "cleared": cleared, **_resilience_status(include_keys=True)}


@app.get("/debug/kill-switch")
async def debug_kill_switch_state(request: Request):
    _verify_auth(request)
    return {"kill_switch": _kill_switch.snapshot(), "scopes_available": list(kill_switch.ALL_SCOPES)}


@app.post("/debug/kill-switch")
async def debug_kill_switch_set(request: Request):
    """Engage or release one kill-switch scope at runtime.

    Body: {"scope": "paid_fallback"|"all_paid", "action": "engage"|"release",
           "reason": "<free text>", "actor": "<who>"}

    Both the engagement and the release are recorded to the flight recorder,
    so the post-incident question "who stopped paid dispatch, when, and why"
    has an answer that does not depend on anyone remembering.
    """
    _verify_admin(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body is a client error, not a 500
        raise HTTPException(status_code=400, detail="Request body must be JSON")

    scope = str((body or {}).get("scope") or "").strip().lower()
    action = str((body or {}).get("action") or "").strip().lower()
    if scope not in kill_switch.ALL_SCOPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope {scope!r}; valid scopes: {list(kill_switch.ALL_SCOPES)}",
        )
    if action not in kill_switch.ALL_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown action {action!r}; valid actions: {list(kill_switch.ALL_ACTIONS)}",
        )

    reason = (body or {}).get("reason")
    actor = request.headers.get("x-router-actor") or (body or {}).get("actor")
    if action == kill_switch.ACTION_ENGAGE:
        event = _kill_switch.engage(scope, actor=actor, reason=reason)
    else:
        event = _kill_switch.release(scope, actor=actor, reason=reason)
    return {"ok": True, "event": event, "kill_switch": _kill_switch.snapshot()}