<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Embeddings from Azure AI Foundry, end to end

The model-router exposes an OpenAI-compatible `POST /v1/embeddings` passthrough
that the memory governor's vector retrieval uses for query embeddings. By
default it points at `api.openai.com`, which quietly makes an OpenAI billing
account a hard dependency of the memory system. This walkthrough moves that
dependency onto Azure AI Foundry — the same place the rest of the platform's
models already live — by deploying `text-embedding-3-small` in Foundry and
pointing the router at it.

Two properties make this a safe swap:

- **Same model, same vector space.** A Foundry deployment of
  `text-embedding-3-small` produces the same 1536-dimension embeddings as
  OpenAI's hosted copy, so documents embedded before the switch remain
  searchable after it. Do not swap in a *different* embedding model without
  re-embedding your corpus.
- **Fail loud.** The endpoint answers `503` until an embedding key is
  configured, and `502` when the upstream errors. Memory search degrading
  silently is the worst failure mode this design guards against — this
  hardening is ported from the upstream private deployment's incident
  learnings, where an exhausted embedding account (`429 insufficient_quota`)
  silently emptied every memory lookup.

## 1. Deploy `text-embedding-3-small` in Foundry

In the [Azure AI Foundry portal](https://ai.azure.com): open your project →
**Models + endpoints** → **Deploy model** → search for
`text-embedding-3-small` → deploy it with the deployment name
`text-embedding-3-small` (keeping the deployment name identical to the model
name means `EMBEDDING_MODEL` needs no override).

Or with the CLI, against the Azure OpenAI/AI Services resource that backs your
Foundry project:

```bash
az cognitiveservices account deployment create \
  --resource-group <your-rg> \
  --name <your-ai-services-resource> \
  --deployment-name text-embedding-3-small \
  --model-name text-embedding-3-small \
  --model-version "1" \
  --model-format OpenAI \
  --sku-name Standard \
  --sku-capacity 120
```

You need two values from the resource:

- **Endpoint**: `https://<your-resource>.services.ai.azure.com` — the router
  wants the OpenAI-compatible route on it: `https://<your-resource>.services.ai.azure.com/openai/v1`
- **API key**: portal → resource → *Keys and Endpoint*, or
  `az cognitiveservices account keys list --resource-group <your-rg> --name <your-ai-services-resource>`

## 2. Configure the router

Set three env vars on the model-router service (in `.env` for the local
compose stack, or as Container App secrets/env in Azure):

```bash
EMBEDDING_BASE_URL=https://<your-resource>.services.ai.azure.com/openai/v1
EMBEDDING_API_KEY=<your-foundry-api-key>
EMBEDDING_MODEL=text-embedding-3-small   # default; set only if your deployment name differs
```

That's the whole cutover. Leaving `EMBEDDING_BASE_URL` unset keeps the OpenAI
path; unsetting `EMBEDDING_API_KEY` (with no `OPENAI_API_KEY` fallback)
disables the endpoint with a clear `503`.

## 3. Verify: a 1536-dim vector through the router

```bash
curl -s http://localhost:8080/v1/embeddings \
  -H "Authorization: Bearer $ROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input": "hello from foundry"}' \
  | python3 -c "import json,sys; b=json.load(sys.stdin); print(b['model'], len(b['data'][0]['embedding']))"
```

Expected output:

```
text-embedding-3-small 1536
```

A `1536` there means the query embedding is landing in the same vector space
as the stored document embeddings — retrieval keeps working across the
provider switch.

## Troubleshooting

### `400 unknown_model` from Foundry

This is the one that looks inexplicable — the deployment exists, the key is
right, and Foundry still answers `unknown_model`. The cause is
[LiteLLM](../GLOSSARY.md#litellm) provider detection: when the `api_base`
points at an **azure.com** host and
the model string has no `provider/` prefix, LiteLLM flips to its AZURE
provider, which authenticates with an `api-key` **header**. Foundry's
OpenAI-compatible `/openai/v1` endpoint expects `Authorization: Bearer` and
rejects the api-key-header request with `400 unknown_model`.

The router pins this for you: `_pin_embedding_provider()` in
[`services/model-router/main.py`](../../services/model-router/main.py)
prepends `openai/` to a bare `EMBEDDING_MODEL` so LiteLLM stays on the
OpenAI-compatible Bearer-auth path. (Lesson ported from the upstream private
deployment's incident learnings.) If you hit `unknown_model` anyway, check
that you haven't set `EMBEDDING_MODEL` to an explicit `azure/...` value — an
explicit provider prefix is honored as-is, which re-exposes the api-key-header
behavior. `azure/<deployment>` is only correct for a classic Azure OpenAI
resource endpoint (`https://<name>.openai.azure.com`), not for Foundry's
unified `/openai/v1` route.

### `503 embeddings not configured`

No embedding key reached the router. Set `EMBEDDING_API_KEY` (or rely on the
`OPENAI_API_KEY` fallback) on the **model-router** service and restart it.
This is deliberate fail-closed behavior — better a loud 503 than memory
search quietly returning nothing.

### `502 embedding provider error`

The upstream call failed; the router logs the real provider error server-side
(and deliberately does not echo it to callers). Check the router logs. Common
causes: wrong `EMBEDDING_BASE_URL` (missing the `/openai/v1` suffix), a
deployment name that doesn't match `EMBEDDING_MODEL`, an exhausted quota
(`429 insufficient_quota` — the incident that motivated this endpoint), or
Foundry capacity throttling.

### Vector length isn't 1536

You deployed a different embedding model. `text-embedding-3-small` is pinned
throughout the platform (Honcho's document embeddings share the space);
changing models means re-embedding every stored document.
