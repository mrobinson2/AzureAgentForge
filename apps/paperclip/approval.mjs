/**
 * Human-in-the-loop action-approval seam — the provider-pluggable boundary for
 * gating an agent ACTION (not infrastructure) behind human approval, beyond the
 * v1.1 destroy-aware infra gate.
 *
 * This is the CONTRACT, the default `auto` gate, an explicit `allow` bypass, and
 * a `webhook` gate that delegates the decision to an external approver (Slack/
 * Discord button, a review queue, an ops endpoint). Like the sandbox seam it
 * ships UNWIRED: importing it changes no runtime behavior. To enable, wire
 * gate.requestApproval(action) into the action path that should be gated — e.g.
 * an outbound message send or a destructive tool call in the auth-proxy /
 * adapter — and deny the action when { approved:false } comes back.
 *
 * Design (HITL-correct AND inert-by-default):
 *   - An action is `{ kind, summary?, payload?, agent? }`.
 *   - Only action kinds listed in APPROVAL_REQUIRED_KINDS are gated. The default
 *     is EMPTY, so nothing is gated and every action passes — the seam is inert
 *     until an operator opts a kind in.
 *   - Once a kind is gated, it MUST have a real approver. A gated action under
 *     the default `auto` provider FAILS CLOSED (denied), so you can never think
 *     you have a gate when no approver is wired.
 *   - Every provider FAILS CLOSED on error/timeout/malformed reply: the default
 *     for a human gate is "don't do it".
 */

/** Parse APPROVAL_REQUIRED_KINDS ("a,b,c") into a Set. */
function parseRequiredKinds(raw) {
  return new Set(
    String(raw || "")
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean),
  );
}

class BaseGate {
  constructor(config = {}) {
    this.provider = "base";
    this.requiredKinds =
      config.requiredKinds instanceof Set
        ? config.requiredKinds
        : parseRequiredKinds(
            config.requiredKinds ?? process.env.APPROVAL_REQUIRED_KINDS,
          );
  }

  /** True when this action's kind is opted into the human gate. */
  requiresApproval(action) {
    return this.requiredKinds.has((action && action.kind) || "");
  }

  /**
   * @param {{kind:string, summary?:string, payload?:object, agent?:string}} action
   * @returns {Promise<{approved:boolean, reason:string, provider:string}>}
   */
  async requestApproval(action) {
    if (!this.requiresApproval(action)) {
      return { approved: true, reason: "not gated", provider: this.provider };
    }
    return this._decide(action);
  }

  // Overridden per provider. Only reached for gated actions.
  async _decide(action) {
    return {
      approved: false,
      reason: "no approver configured",
      provider: this.provider,
    };
  }
}

/**
 * Default gate. Non-gated actions pass; a gated action FAILS CLOSED because
 * `auto` has no human approver wired. This is what makes "gated a kind but
 * forgot to configure an approver" a safe denial rather than a silent bypass.
 */
class AutoGate extends BaseGate {
  constructor(config = {}) {
    super(config);
    this.provider = "auto";
  }

  async _decide() {
    return {
      approved: false,
      reason:
        "action kind requires approval but APPROVAL_PROVIDER is 'auto' (no approver wired)",
      provider: this.provider,
    };
  }
}

/**
 * Explicit bypass: approves gated actions too. An intentional opt-in for trusted
 * or non-interactive environments — never the default, so a bypass is always a
 * deliberate operator choice.
 */
class AllowGate extends BaseGate {
  constructor(config = {}) {
    super(config);
    this.provider = "allow";
  }

  async _decide() {
    return { approved: true, reason: "auto-approved (allow)", provider: this.provider };
  }
}

/**
 * Delegates the decision to an external approver over HTTP. POSTs the action to
 * `endpoint` and expects `{ approved:boolean, reason?:string }`. Injected
 * transport (defaults to global fetch) so it is fully unit-testable offline.
 * FAILS CLOSED: a missing endpoint, non-200, malformed reply, timeout, or
 * transport error all resolve to denied — never an unhandled rejection.
 */
class WebhookGate extends BaseGate {
  constructor(config = {}) {
    super(config);
    this.provider = "webhook";
    this.endpoint = config.endpoint || process.env.APPROVAL_WEBHOOK_URL || "";
    this.transport = config.transport || ((url, init) => fetch(url, init));
    this.timeoutMs = config.timeoutMs || 0;
    this.getToken = config.getToken || null;
  }

  async _decide(action) {
    if (!this.endpoint) {
      return {
        approved: false,
        reason: "webhook gate has no endpoint configured",
        provider: this.provider,
      };
    }
    try {
      const headers = { "Content-Type": "application/json" };
      if (this.getToken) headers.Authorization = `Bearer ${await this.getToken()}`;
      const init = {
        method: "POST",
        headers,
        body: JSON.stringify({
          kind: action.kind,
          summary: action.summary || "",
          payload: action.payload ?? null,
          agent: action.agent || null,
        }),
      };
      if (this.timeoutMs && typeof AbortSignal?.timeout === "function") {
        init.signal = AbortSignal.timeout(this.timeoutMs);
      }
      const resp = await this.transport(this.endpoint, init);
      const status = resp && typeof resp.status === "number" ? resp.status : 0;
      const data =
        resp && typeof resp.json === "function" ? await resp.json() : {};
      // Fail closed: only an explicit boolean-true approval passes.
      const approved = status === 200 && data && data.approved === true;
      return {
        approved,
        reason:
          (data && typeof data.reason === "string" && data.reason) ||
          (approved ? "approved by webhook" : `denied by webhook (status ${status})`),
        provider: this.provider,
      };
    } catch (err) {
      return {
        approved: false,
        reason: `webhook gate error: ${String((err && err.message) || err)}`,
        provider: this.provider,
      };
    }
  }
}

// Provider registry. Default is `auto` (gated actions fail closed). `allow` is a
// deliberate bypass; `webhook` delegates to an external approver. An unknown
// provider fails LOUD rather than silently degrading to a bypass.
const APPROVAL_PROVIDERS = {
  auto: AutoGate,
  allow: AllowGate,
  webhook: WebhookGate,
};

/**
 * Construct the approval gate for the configured provider.
 * @param {string} [provider=process.env.APPROVAL_PROVIDER||"auto"]
 * @param {object} [config]
 */
function createApprovalGate(
  provider = process.env.APPROVAL_PROVIDER || "auto",
  config = {},
) {
  const Ctor = APPROVAL_PROVIDERS[provider];
  if (!Ctor) {
    throw new Error(
      `Unknown APPROVAL_PROVIDER: '${provider}' (known: ${Object.keys(APPROVAL_PROVIDERS).join(", ")})`,
    );
  }
  return new Ctor(config);
}

export {
  createApprovalGate,
  AutoGate,
  AllowGate,
  WebhookGate,
  APPROVAL_PROVIDERS,
  parseRequiredKinds,
};
