<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Inbound Intake Webhook (reference pattern)

> **Technical reference for contributors.** For the operational overview, start at [README](../../README.md) or [Architecture](../../docs/architecture.md).

> 🚧 **Reference template — not deployed.** This directory documents a generic
> integration pattern and ships a self-contained, unit-tested reference handler.
> It is **not** wired into the compose stack or the Terraform environment — adopt
> it into your own ingress (the platform's auth proxy, an Azure Function, a small
> service) and point a provider at it.

## Overview

Many external channels — a voice-agent vendor, a web form backend, a chat
provider, a phone-tree — capture a structured **intake** from a person and then
POST it to a webhook so the platform can act on it. This pattern is the neutral,
provider-agnostic version of that handoff:

```
inbound webhook  ──▶  verify shared secret  ──▶  dedupe (idempotency key)
                                                        │
                             create work item  ◀──  normalize payload
                             assigned to a handoff agent
```

It is deliberately **vendor-neutral**: there is no provider-specific code and no
assumption about what the intake is *for*. Map your provider's payload in
`normalizeIntake` and everything downstream sees one neutral shape.

## What's here

| Path | What it is |
|------|-----------|
| `reference/handler.mjs` | Framework-free handler: `verifyBearer`, `idempotencyKey`, `normalizeIntake`, `buildHandoffIssue`, and `handleIntakeWebhook` (side effects injected). |
| `reference/handler.test.mjs` | Node built-in test runner (`node --test`), zero dependencies. |
| `reference/example-payload.json` | A generic inbound intake body. |

## The four moving parts

1. **Verify a shared secret.** Most inbound webhooks send a static secret in the
   `Authorization: Bearer …` header rather than HMAC-signing the body. The
   handler compares it in constant time and treats an **empty configured secret
   as "disabled"** — the endpoint is never open without an explicit secret. If
   your provider signs the body instead, swap the check at the same single choke
   point.
2. **Dedupe on an idempotency key.** Providers retry. The handler derives a
   stable key from whatever id the provider supplies (call/submission/message id)
   and falls back to a hash of the body, so a duplicate delivery is a `200`
   no-op instead of a second handoff.
3. **Normalize the payload.** `normalizeIntake` maps the provider's arbitrary
   shape to `{ externalId, contact, summary, transcript, fields, receivedVia }`.
4. **Hand off to an agent.** `buildHandoffIssue` produces the work-item create
   body — **camelCase only**, because the platform API silently drops
   snake_case keys — assigned to a configured handoff agent, with the provider's
   external id kept as `externalRef` for traceability. On an upstream failure the
   idempotency key is **not** recorded, so the provider's retry still lands.

## Payload contract

The handler reads these fields (all optional; adapt to your provider):

| Field | Meaning |
|-------|---------|
| `external_id` / `id` / `submission_id` / `call.id` | provider's id (idempotency + traceability) |
| `contact.{name,email,phone}` (or top-level `name`/`email`/`phone`) | who the intake is about |
| `summary` / `notes` | free-text summary |
| `transcript` | optional link or text |
| `fields` / `answers` | structured answers the intake captured |
| `source` | which channel produced it |

See [`reference/example-payload.json`](reference/example-payload.json).

## Wiring it up (sketch)

```js
import { handleIntakeWebhook } from "./reference/handler.mjs";

// Inside your HTTP handler for POST /api/webhooks/intake:
const result = await handleIntakeWebhook(
  { authorization: req.headers.authorization, body: await readJson(req) },
  {
    expectedSecret: process.env.INTAKE_WEBHOOK_SECRET, // empty => disabled
    handoffAgentId: process.env.INTAKE_HANDOFF_AGENT_ID,
    workItemsUrl:  `${process.env.PLATFORM_API_URL}/api/issues`,
    apiToken:      process.env.PLATFORM_AUTOMATION_TOKEN,
    store,         // any { has(key), add(key) } over idempotency keys
    fetch,
  }
);
res.writeHead(result.status, { "Content-Type": "application/json" });
res.end(JSON.stringify(result.body));
```

Keep `INTAKE_WEBHOOK_SECRET` in Key Vault; leave it unset to keep the endpoint
closed. Store idempotency keys somewhere durable (Redis, a DB table) in
production — the in-memory `Set` in the tests is only for illustration.

## Run the tests

```bash
node --test integrations/webhook-intake/reference/handler.test.mjs
```
