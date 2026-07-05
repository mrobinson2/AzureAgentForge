// Vendor-neutral inbound-intake webhook — reference handler.
//
// A generic pattern for accepting an inbound webhook from ANY external channel
// (a voice agent, a form backend, a chat provider, a phone-tree vendor) that has
// finished capturing a structured "intake" and wants to hand it off to an agent.
// The flow is: verify a shared secret, dedupe on an idempotency key, normalize
// the provider payload to a neutral shape, then create a work item assigned to a
// configured handoff agent.
//
// This is a REFERENCE, not a wired-in service. It has no provider-specific code
// and no framework dependency: the pure functions below are unit-testable, and
// `handleIntakeWebhook` takes its side effects (fetch, a seen-key store) by
// injection so you can adapt it to whatever runtime you deploy. See README.md.

const BRIDGE_MARKER = "[intake]";

// ── security: shared-secret bearer ──────────────────────────────────────────
// Most inbound webhook providers send a static shared secret in the Authorization
// header rather than HMAC-signing the body. Verify it in constant time. If your
// provider DOES sign the body, replace this with a signature check — the call
// site is the same single choke point.
export function timingSafeEqual(a, b) {
  const sa = String(a);
  const sb = String(b);
  // Compare in constant time regardless of length to avoid leaking length.
  let mismatch = sa.length ^ sb.length;
  for (let i = 0; i < Math.max(sa.length, sb.length); i++) {
    mismatch |= (sa.charCodeAt(i) || 0) ^ (sb.charCodeAt(i) || 0);
  }
  return mismatch === 0;
}

export function verifyBearer(authorizationHeader, expectedSecret) {
  // Empty expected secret = the webhook is disabled (deny everyone). A webhook
  // must never be open without an explicitly configured secret.
  if (!expectedSecret) return false;
  const header = String(authorizationHeader || "");
  const m = header.match(/^Bearer\s+(.+)$/i);
  if (!m) return false;
  return timingSafeEqual(m[1].trim(), expectedSecret);
}

// ── idempotency ─────────────────────────────────────────────────────────────
// Providers retry. Derive a stable key from whatever id the provider supplies
// (call id, submission id, message id); fall back to a hash of the body so a
// payload with no id is still deduped rather than double-processed.
export function idempotencyKey(payload) {
  const id =
    payload?.external_id ??
    payload?.id ??
    payload?.submission_id ??
    payload?.call?.id ??
    null;
  if (id) return `intake:${id}`;
  // Order-independent-ish fallback: a cheap FNV-1a over the serialized body.
  const s = JSON.stringify(payload ?? {});
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return `intake:h${h.toString(16)}`;
}

// ── normalization ───────────────────────────────────────────────────────────
// Map an arbitrary provider payload to a neutral intake shape. Adjust the field
// reads for your provider; downstream code only sees the normalized object.
export function normalizeIntake(payload) {
  const p = payload ?? {};
  return {
    externalId: p.external_id ?? p.id ?? p.submission_id ?? null,
    contact: {
      name: p.contact?.name ?? p.name ?? null,
      email: p.contact?.email ?? p.email ?? null,
      phone: p.contact?.phone ?? p.phone ?? null,
    },
    summary: p.summary ?? p.notes ?? "",
    transcript: p.transcript ?? null,
    // Free-form structured answers the intake captured, provider-shaped.
    fields: p.fields ?? p.answers ?? {},
    receivedVia: p.source ?? "webhook",
  };
}

// ── handoff: build the work item ────────────────────────────────────────────
// Create-body for the platform's work-item API. NOTE: the platform API is
// camelCase-only — snake_case keys are silently dropped — so write camelCase.
export function buildHandoffIssue(intake, { handoffAgentId, title } = {}) {
  const who = intake.contact?.name || intake.contact?.email || intake.contact?.phone || "unknown contact";
  const lines = [
    `${BRIDGE_MARKER} inbound intake handoff`,
    "",
    `Contact: ${who}`,
    intake.summary ? `\nSummary:\n${intake.summary}` : "",
    Object.keys(intake.fields || {}).length
      ? `\nCaptured fields:\n${Object.entries(intake.fields).map(([k, v]) => `- ${k}: ${v}`).join("\n")}`
      : "",
  ];
  return {
    title: title || `Intake handoff — ${who}`,
    description: lines.filter(Boolean).join("\n"),
    // camelCase — assignee + a stable external ref for traceability.
    assigneeAgentId: handoffAgentId ?? null,
    externalRef: intake.externalId ?? null,
  };
}

// ── orchestration ───────────────────────────────────────────────────────────
// Side effects are injected: `store` is any object with async has()/add() over
// idempotency keys (an in-memory Set for tests, Redis/DB in production); `fetch`
// posts the work item. Returns a small result the caller turns into an HTTP
// response.
export async function handleIntakeWebhook(
  { authorization, body },
  { expectedSecret, handoffAgentId, workItemsUrl, apiToken, store, fetch: doFetch, title } = {}
) {
  if (!verifyBearer(authorization, expectedSecret)) {
    return { status: 401, body: { error: "unauthorized" } };
  }
  const key = idempotencyKey(body);
  if (await store.has(key)) {
    return { status: 200, body: { status: "duplicate", key } };
  }
  const intake = normalizeIntake(body);
  const issue = buildHandoffIssue(intake, { handoffAgentId, title });

  const res = await doFetch(workItemsUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${apiToken}`,
    },
    body: JSON.stringify(issue),
  });
  if (!res.ok) {
    // Do NOT record the idempotency key on failure — let the provider retry.
    return { status: 502, body: { error: "handoff_failed", upstream: res.status } };
  }
  await store.add(key);
  return { status: 201, body: { status: "handed_off", key } };
}
