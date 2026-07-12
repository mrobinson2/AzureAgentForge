<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Model Router

An OpenAI-compatible HTTP gateway (FastAPI, version 1.3.0) that routes chat-completion requests to Azure AI Foundry deployments. Callers speak the OpenAI Chat Completions API; the router selects the appropriate backend tier, enforces per-tier token-budget limits, and falls back to a cheaper tier if the primary is unavailable or the request exceeds its context window.

## Provider configuration

The router uses Azure AI Foundry as its primary backend. All env vars are loaded at startup; the service will not start if a required var is absent.

### GPT-4o-mini (primary — required)

| Env var | Purpose |
|---|---|
| `GPT4O_API_KEY` | API key for the gpt-4o-mini Foundry project (**required**) |
| `GPT4O_BASE_URL` | Foundry endpoint for gpt-4o-mini (or set `AZURE_FOUNDRY_ENDPOINT` as an alias) |
| `GPT4O_DAILY_BUDGET_USD` | Per-day spend cap in USD (default: `5.00`) |
| `GPT4O_MAX_TOKENS` | Max output tokens (default: `4096`) |

### Phi-4 (required at startup)

| Env var | Purpose |
|---|---|
| `PHI_BASE_URL` | Foundry endpoint for the Phi-4 deployment (**required**) |
| `PHI_API_KEY` | API key (**required**) |
| `PHI_MODEL` | Deployment name (default: `Phi-4`) |
| `PHI_DAILY_BUDGET_USD` | Per-day spend cap (default: `0.50`) |
| `PHI_MAX_TOKENS` | Max output tokens (default: `2048`). Context limit: 16 384 tokens. |

### Additional Foundry tiers (optional — registered when env vars are present)

Each optional tier is registered via three env vars: `<PREFIX>_BASE_URL`, `<PREFIX>_API_KEY`, and `<PREFIX>_MODEL`. If any of the three is absent the tier is silently skipped, so forks that don't need a given model incur no changes.

| Prefix | Model family | Notes |
|---|---|---|
| `CLAUDE` | Claude (Anthropic Messages API) | Routed via direct Anthropic SDK call; bypasses LiteLLM for Foundry's `/anthropic` endpoint |
| `KIMI` | Kimi K2 | OpenAI-compat endpoint |
| `GROK` | Grok | OpenAI-compat endpoint |

Per-tier optional overrides: `<PREFIX>_DAILY_BUDGET_USD`, `<PREFIX>_MAX_TOKENS`.

Shared timeout across all Foundry tiers: `MODEL_TIMEOUT_SECONDS` (default: `60`).

### Ollama edge tiers (optional)

Set `OLLAMA_BASE_URL` and `OLLAMA_MODELS` (comma-separated model tags) to register local inference tiers. Each model tag `<name>[:<variant>]` becomes a `<name>-local` tier. Ollama tiers fall back to `gpt4o-mini` (or the value of `OLLAMA_FALLBACK_TIER`) when the edge host is unreachable. Leaving `OLLAMA_BASE_URL` unset gives a clean Foundry-only stack.

### Embeddings (optional — provider-flexible)

`POST /v1/embeddings` is an OpenAI-compatible passthrough used by the memory governor's vector retrieval. It is **disabled (503) until a key is set** — it fails loud rather than silently degrading memory search. The upstream is provider-flexible: any OpenAI-compatible endpoint serving the same model works, so forks are not tied to OpenAI billing. The Azure AI Foundry path is documented end-to-end in [`docs/walkthroughs/azure-foundry-embeddings.md`](../../docs/walkthroughs/azure-foundry-embeddings.md).

| Env var | Purpose |
|---|---|
| `EMBEDDING_API_KEY` | API key for the embeddings upstream (falls back to `OPENAI_API_KEY`; unset → endpoint answers 503) |
| `EMBEDDING_BASE_URL` | OpenAI-compatible base URL. Unset → `api.openai.com`. Point it at an Azure AI Foundry `/openai/v1` endpoint to serve embeddings from Foundry |
| `EMBEDDING_MODEL` | Model / deployment name (default: `text-embedding-3-small` — matches Honcho's 1536-dim document-embedding space) |
| `EMBEDDING_TIMEOUT_SECONDS` | Upstream timeout (default: `20`) |
| `EMBEDDING_MAX_INPUTS` | Max inputs per request (default: `256`) |
| `EMBEDDING_DAILY_BUDGET_USD` | Daily cap for the `embeddings` ledger bucket (default: `1.00`; `0` disables the cap — spend is still tracked) |
| `EMBEDDING_PRICE_PER_MTOK` | List-price fallback for cost estimation when LiteLLM reports no `response_cost` (default: `0.02`, matching `text-embedding-3-small`; adjust alongside `EMBEDDING_MODEL`) |

**Provider-detection pin**: the router prepends `openai/` to a bare `EMBEDDING_MODEL` before handing it to LiteLLM. This is load-bearing for the Foundry path — with an `azure.com` `EMBEDDING_BASE_URL` and no provider prefix, LiteLLM flips to its AZURE provider (`api-key` header auth) and Foundry's OpenAI-compatible endpoint rejects the call with `400 unknown_model`. The `openai/` prefix keeps auth on `Authorization: Bearer`. An explicit `provider/` prefix in `EMBEDDING_MODEL` is honored unchanged.

## Routing

### How a tier is selected

`select_tier(body)` resolves the tier in this order:

1. Explicit `tier` field on the request body (or `metadata.tier`).
2. Model-hint shortcuts: a `model` value containing `gpt-4o-mini` / `4o-mini` maps to `gpt4o-mini`; `phi4` / `phi-4` maps to `phi4`.
3. Exact match of `model` against a registered tier name (deployment-name passthrough).
4. If the `model` value is not a registered tier, an ephemeral passthrough config is created and the request is forwarded directly to the shared Foundry endpoint (`FOUNDRY_BASE_URL` / `FOUNDRY_API_KEY`, defaulting to the gpt-4o-mini project values).
5. Persona lookup via `PERSONA_TIERS_JSON` (see below). Falls back to `gpt4o-mini` when the persona is unknown.

### Persona → tier mapping

Set `PERSONA_TIERS_JSON` to a JSON object mapping agent/persona names to tier keys:

```json
{"orchestrator": "claude-sonnet-4-6", "coder": "gpt-4o-mini"}
```

The tier value must be a key present in `MODELS` at request time (i.e. a registered tier such as `gpt4o-mini`, `phi4`, or an optional tier you enabled — **not** the abstract `frontier`/`standard`/`economy` labels used in agent profiles). `persona-tiers.example.json` ships a working default that targets only the two always-registered tiers (`gpt4o-mini` for higher-value roles, `phi4` for economy roles), so it routes correctly on a vanilla Foundry-only stack; repoint roles at richer tiers (e.g. a `CLAUDE` or `KIMI` tier) once you register them. For Kimi: set `KIMI_BASE_URL`, `KIMI_API_KEY`, and `KIMI_MODEL=kimi-k2`, then point a role at its deployment name, e.g. `{"researcher": "kimi-k2"}`.

### Fallback chain

`_build_fallback_chain(tier, estimated_input, requested_max)` returns the ordered list of tiers to try if the primary fails. Built-in preferences:

- `gpt4o-mini` → `phi4`
- `phi4` → *(none)*
- Foundry optional tiers → `gpt4o-mini`
- Ollama local tiers → `gpt4o-mini` (or `OLLAMA_FALLBACK_TIER`)
- Ephemeral passthrough tiers → `gpt4o-mini`

Tiers that cannot fit the request (input + max_tokens > context_limit) are pruned from the chain.

## Budget enforcement (`BUDGET_ENFORCE_MODE`)

Per-tier daily budgets are tracked on every path; **by default they only warn** (the pre-existing behavior, ship-dark safe). `BUDGET_ENFORCE_MODE` upgrades the budgets from observability to an acting control:

| Mode | Over-budget behavior |
|---|---|
| `warn` *(default)* | Serve the requested tier and emit a `budget_enforce` WARN log — behavior identical to before this feature existed |
| `downgrade` | Serve `BUDGET_FALLBACK_TIER` (default: `gpt4o-mini`) instead; the response carries the `X-Router-Budget-Downgrade: <from>-><to>` header and `_router.budget_downgraded_from` metadata |
| `block` | Refuse with HTTP 429 and a machine-readable `budget_exceeded` body (`error`, `tier`, `spent_usd`, `limit_usd`, `mode`) |

Semantics:

- **All paths are covered.** Enforcement wraps `select_tier` (so `/v1/chat/completions` — including Claude tiers dispatched through the direct Anthropic SDK — and ephemeral passthrough tiers are checked), plus explicit checks on the two paths that bypass tier selection: the native `/v1/messages` endpoint and `/v1/embeddings`.
- **The fallback tier is exempt** — it is the designated floor; blocking it would turn "over budget" into an outage. A misconfigured fallback (unregistered, or the same tier that is over budget) degrades to `warn` rather than stranding requests. An invalid `BUDGET_ENFORCE_MODE` value fails open to `warn`.
- **Native `/v1/messages`**: responses must stay Anthropic-shaped, so `downgrade` only takes effect there when `BUDGET_FALLBACK_TIER` is itself an Anthropic-backed tier; otherwise the request degrades to `warn`. `block` returns an Anthropic-shaped 429 (`{"type": "error", "error": {"type": "rate_limit_error", ...}}`) so `anthropic_messages` transports parse it like any upstream error. This path also now records its spend to the daily ledger (streaming and non-streaming) — previously it recorded nothing, which is exactly the gap class this feature closes.
- **`/v1/embeddings`**: spend accrues to a dedicated `embeddings` ledger bucket capped by `EMBEDDING_DAILY_BUDGET_USD`. There is no same-vector-space downgrade target (the model is pinned so query embeddings match the stored document space), so `downgrade` degrades to `warn` on this path; `block` 429s.

| Env var | Purpose |
|---|---|
| `BUDGET_ENFORCE_MODE` | `warn` (default) \| `downgrade` \| `block` |
| `BUDGET_FALLBACK_TIER` | Tier served in `downgrade` mode (default: `gpt4o-mini`) |
| `EMBEDDING_DAILY_BUDGET_USD` | Daily cap for the embeddings bucket (default: `1.00`; `0` disables) |

## Security

- **API-key auth**: set `ROUTER_API_KEY` to require a matching credential on all `/v1/*` requests — either `Authorization: Bearer <key>` (OpenAI-style clients) or `x-api-key: <key>` (Anthropic-native clients; the Anthropic SDK sends x-api-key for any custom base_url, which is how Hermes's `anthropic_messages` transport reaches `/v1/messages`). Fails closed (503) when unset.
- **Rate limiting**: `RATE_LIMIT_RPM` (default: 60) enforces a per-IP sliding-window limit.
- **Input validation**: message count capped at `MAX_MESSAGES` (default: 200); total tokens at `MAX_BODY_TOKENS` (default: 200 000).

## Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/v1/chat/completions` | OpenAI-compatible chat completions (streaming and non-streaming) |
| `POST` | `/v1/messages` | Anthropic Messages API passthrough (Claude tiers only) |
| `POST` | `/v1/embeddings` | OpenAI-compatible embeddings passthrough (503 until `EMBEDDING_API_KEY` is set; see [Embeddings](#embeddings-optional--provider-flexible)) |
