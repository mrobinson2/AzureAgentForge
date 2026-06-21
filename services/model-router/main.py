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

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
log = logging.getLogger("router")

# Respect LITELLM_LOG env var instead of deprecated litellm.set_verbose
_log_level = os.environ.get("LOG_LEVEL", "info").lower()
if _log_level == "debug":
    os.environ.setdefault("LITELLM_LOG", "DEBUG")

app = FastAPI(title="Model Router", version="1.3.0")

# ─── Security: API Key Authentication ────────────────────────────────────────
# When ROUTER_API_KEY is set, all /v1/* endpoints require a matching Bearer token.
# Internal ACA services pass the key; unauthenticated requests are rejected.
_ROUTER_API_KEY = os.environ.get("ROUTER_API_KEY", "")


def _verify_auth(request: Request) -> None:
    """Verify Bearer token if ROUTER_API_KEY is configured."""
    if not _ROUTER_API_KEY:
        return  # No auth configured — internal-only mode
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    token = auth[7:].strip()
    # Constant-time comparison to prevent timing attacks
    expected = hashlib.sha256(_ROUTER_API_KEY.encode()).digest()
    provided = hashlib.sha256(token.encode()).digest()
    if expected != provided:
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


def _reset_if_new_day() -> None:
    global _budget_date
    today = str(date.today())
    if today != _budget_date:
        _budget_date = today
        _spend.clear()


def record_cost(tier: str, cost: float) -> None:
    _reset_if_new_day()
    _spend[tier] += cost


def is_over_budget(tier: str) -> bool:
    _reset_if_new_day()
    return _spend.get(tier, 0.0) >= MODELS[tier]["daily_budget"]


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
    return MODELS[tier]["litellm_model"].startswith("anthropic/")


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
    client, kwargs: dict[str, Any], tier: str, t_start: float
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
    try:
        async for event in await client.messages.create(stream=True, **kwargs):
            event_type = getattr(event, "type", "message")
            try:
                payload = event.model_dump(exclude_unset=False, mode="json")
            except Exception:
                # Older SDK style fallback
                payload = getattr(event, "to_dict", lambda: {})() or {}
            yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"
        log.info("messages_stream_ok tier=%s latency=%.2fs", tier, time.monotonic() - t_start)
    except Exception as e:
        log.warning("messages_stream_failed tier=%s error=%s", tier, e)
        err_payload = {"type": "error", "error": {"type": "api_error", "message": str(e)}}
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
    last_err: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            if attempt > 0:
                await asyncio.sleep(_RETRY_DELAY * attempt)
                log.info("retry tier=%s attempt=%d/%d", tier, attempt + 1, _MAX_RETRIES + 1)

            if is_anthropic:
                result = await _call_anthropic_direct(tier, body)
                # Anthropic SDK doesn't surface response_cost; skip record_cost
                # (LiteLLM-side cost tracking is currently best-effort anyway).
            else:
                response = await litellm.acompletion(**kwargs)
                record_cost(tier, response._hidden_params.get("response_cost") or 0.0)
                result = response.model_dump()

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
        yield f"data: {json.dumps({'error': str(e), 'tier': tier})}\n\n"
        yield "data: [DONE]\n\n"


# ─── FastAPI Endpoints ────────────────────────────────────────────────────────
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    _verify_auth(request)
    _check_rate_limit(request)
    t_start = time.monotonic()
    body = await request.json()
    _validate_request(body)

    tier = select_tier(body)
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

    estimated = _estimate_tokens(body["messages"])
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

        for candidate in [tier] + fallback_chain:
            try:
                stream_iter = await _open_stream(candidate, body)
                log.info(
                    "streaming tier=%s latency=%.2fs",
                    candidate,
                    time.monotonic() - t_start,
                )
                return StreamingResponse(
                    _iter_stream(stream_iter, candidate),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                    },
                )
            except Exception as e:
                last_error = e
                log.warning("stream_init_failed tier=%s error=%s", candidate, e)
                log.debug(
                    "stream_init_failed_traceback tier=%s\n%s",
                    candidate,
                    traceback.format_exc(),
                )

        raise HTTPException(
            status_code=502,
            detail=f"All tiers failed to initialise stream: {last_error}",
        )

    # Non-streaming path
    try:
        result = await _call_model(tier, body)
        result["_router"] = {"tier": tier, "estimated_input_tokens": estimated}
        log.info("success tier=%s latency=%.2fs", tier, time.monotonic() - t_start)
        return JSONResponse(content=result)
    except Exception as e:
        log.warning("primary_failed tier=%s error=%s", tier, e)
        log.debug("primary_failed_traceback tier=%s\n%s", tier, traceback.format_exc())

    for fb in fallback_chain:
        try:
            result = await _call_model(fb, body)
            result["_router"] = {
                "tier": fb,
                "fallback_from": tier,
                "estimated_input_tokens": estimated,
            }
            log.info("fallback_success tier=%s latency=%.2fs", fb, time.monotonic() - t_start)
            return JSONResponse(content=result)
        except Exception as e:
            log.warning("fallback_failed tier=%s error=%s", fb, e)
            log.debug("fallback_failed_traceback tier=%s\n%s", fb, traceback.format_exc())

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

    tier = _select_anthropic_tier_for_model(body)
    if not _is_anthropic_tier(tier):
        # _select_anthropic_tier_for_model already raises 400 for non-Anthropic
        # selections, but defend against tier table edits that could slip a
        # non-Anthropic tier through.
        raise HTTPException(
            status_code=400,
            detail=f"Tier {tier!r} is not an Anthropic-backed tier",
        )

    cfg = MODELS[tier]
    stream = bool(body.get("stream", False))

    estimated = _estimate_tokens(body["messages"])
    requested_max = body.get("max_tokens", cfg["max_tokens"])
    if not _fits_model(tier, estimated, requested_max):
        raise HTTPException(status_code=413, detail="Request exceeds model context limit")

    log.info(
        "messages tier=%s stream=%s input_tokens≈%s tools=%s max_tokens=%s",
        tier, stream, estimated, len(body.get("tools", []) or []), requested_max,
    )

    client = _make_anthropic_client(cfg)
    kwargs = _build_messages_passthrough_kwargs(cfg, body, request)

    if stream:
        return StreamingResponse(
            _stream_anthropic_messages_sse(client, kwargs, tier, t_start),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )

    # Non-streaming path. The SDK's stream kwarg is treated as a separate arg;
    # strip it before forwarding (we already branched on it).
    kwargs.pop("stream", None)
    try:
        resp = await client.messages.create(**kwargs)
    except Exception as e:
        log.warning("messages_failed tier=%s error=%s", tier, e)
        log.debug("messages_failed_traceback tier=%s\n%s", tier, traceback.format_exc())
        # Surface Anthropic-shaped error so callers' anthropic_messages
        # transport can parse the payload the same way it parses upstream
        # errors. status_code may not always be on the exception — default 502.
        status = getattr(e, "status_code", 502) or 502
        err_body = {
            "type": "error",
            "error": {"type": "api_error", "message": str(e)},
        }
        return JSONResponse(status_code=status, content=err_body)

    try:
        result = resp.model_dump(exclude_unset=False, mode="json")
    except Exception:
        result = getattr(resp, "to_dict", lambda: {})() or {}
    result["_router"] = {"tier": tier, "estimated_input_tokens": estimated}
    log.info("messages success tier=%s latency=%.2fs", tier, time.monotonic() - t_start)
    return JSONResponse(content=result)


# ─── Embeddings ──────────────────────────────────────────────────────────────
# The memory governor's Plane C vector retrieval embeds its query through here
# so the "never call a provider directly" principle holds and the model is
# pinned to match Honcho's document embeddings (same 1536-dim vector space).
# Disabled (503) unless an embedding key is set. EMBEDDING_BASE_URL unset ->
# OpenAI.com; set it to point at an Azure/Foundry deployment of the same model.
_EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
_EMBED_API_KEY = _tier_env("EMBEDDING_API_KEY") or _tier_env("OPENAI_API_KEY")
_EMBED_API_BASE = os.environ.get("EMBEDDING_BASE_URL") or None
_EMBED_TIMEOUT_S = int(os.environ.get("EMBEDDING_TIMEOUT_SECONDS", "20"))
_EMBED_MAX_INPUTS = int(os.environ.get("EMBEDDING_MAX_INPUTS", "256"))


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
    try:
        resp = await litellm.aembedding(
            model=_EMBED_MODEL,
            input=inp,
            api_key=_EMBED_API_KEY,
            api_base=_EMBED_API_BASE,
            timeout=_EMBED_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("embeddings call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"embedding provider error: {exc}")
    try:
        payload = resp.model_dump()
    except AttributeError:
        payload = resp if isinstance(resp, dict) else dict(resp)
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
    _reset_if_new_day()
    return {
        "status": "ok",
        "date": _budget_date,
        "budgets": {
            t: {
                "spent": _spend.get(t, 0.0),
                "limit": MODELS[t]["daily_budget"],
                "over_budget": is_over_budget(t),
            }
            for t in MODELS
        },
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