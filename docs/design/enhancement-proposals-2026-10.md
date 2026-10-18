# AzureAgentForge enhancement proposals — October 2026

Status: **proposal; first slices of all four items implemented 2026-10-17/18 (see “Implementation status” at the end)**

Date: 2026-10-17

This document identifies four enhancements that would add outsized value to
AzureAgentForge relative to their cost. They are deliberately *not* repeats of
the seven-item slate in
[`feature-opportunities-2026-08.md`](feature-opportunities-2026-08.md)
(multi-tenant, routines, ACA sandboxes, approval center, voice, Discord,
prompt-governance loop). Each one below was chosen because the repository
already contains most of the parts and the missing piece is the one that turns
a governance *claim* into a governance *guarantee*, or an operator *question*
into an operator *answer*.

## How these were selected

Read in full or in relevant part: `README.md`, `ROADMAP.md`,
`docs/design/feature-opportunities-2026-08.md`, `docs/design/release-audit-2026-08-23.md`,
`.continue-here.md`, `AAF_Ship_Readiness_Review.md`,
`AAF_vs_Azure_Native_Agentic_Comparison.docx`,
`docs/superpowers/plans/2026-07-19-agent-identity-tool-policy-plane.md`,
`docs/superpowers/plans/2026-07-19-agentops-evaluation-release-control-plane.md`,
`PR_BODY_agent-identity.md`, the agent profiles under `agents/`, the Terraform
under `infrastructure/modules/container-apps/`, the router, watchdog, and
memory-governor sources, and the vendored Hermes MCP documentation and config
loader. Gap claims below cite the file that proves them.

## Shortlist

| Rank | Enhancement | User job | Why it is outsized | What already exists |
| --- | --- | --- | --- | --- |
| 1 | **Per-agent Azure identity** (workload identity per role, brokered through the signed agent assertion) | Give each agent role only the Azure permissions its job needs, enforced by Azure RBAC rather than by prompt text. | Today all 14 roles run under one managed identity, so the headline "delete the resource group" refusal is enforced by prompts, fixtures, and an inert policy seam, not by Azure. This closes that gap and aligns with Entra Agent ID. | `azurerm_user_assigned_identity.hermes`, the uncommitted agent-identity plane (ES256 assertions, PDP, `ActorContext`), Key Vault refs, per-role profiles. |
| 2 | **Governed MCP tool plane** (per-role MCP server grants, secret binding, policy + audit on every MCP call, and AAF exposed as an MCP server) | Let a role reach GitHub, Azure DevOps, Microsoft 365, or an internal API through a standard tool interface with least privilege and an audit trail; let Copilot, VS Code, or Claude delegate into AAF. | Hermes already speaks MCP; AAF never configures it. Profiles grant only `terminal`/`file`/`browser`, which is far coarser than the governance story promises. MCP is also the lingua franca the Foundry / Agent 365 roadmap items need. | Hermes `mcp_servers` config and toolset registry, `write-hermes-config.sh`, `tool-policy.mjs` PEP, `approval.mjs`, flight recorder, the optional APIM/MCP gateway module in the identity patch. |
| 3 | **Operator morning brief** (one scheduled, cross-service digest answering "what happened overnight") | Open Teams, Slack, or Discord and see in one message what ran, what failed, what it cost, what is waiting on a human, and what memory changed. | The README's own problem statement asks this question; the answers exist but are scattered across five services with no composed view. Cheap, highly visible, and it makes every other governance feature legible. | `/digest`, `/escalation-sla`, watchdog scorecards and alert pack, router `/health` and flight recorder, PaperClip runs API, `digest_post.py` delivery pattern, Slack `chat.postMessage`. |
| 4 | **Memory subject erasure and export** (per-user "forget me" and "export my data" across every store) | Honor a deletion or access request for one end user without wiping a workspace. | Required for any customer-facing deployment under GDPR/CCPA-style rules. Nothing in the governor, Honcho wiring, or flight recorder offers it, and the per-user identity work makes it newly possible. | `ActorContext` (hashed subject), governor `sweeper.py` / `vault.py`, Honcho peer model, flight recorder redaction, tenant RLS. |

Items 1 and 2 are the two "tremendous value" bets. Item 3 is the highest
value-per-day-of-work. Item 4 is the compliance blocker that will otherwise
surface the first time a real customer asks.

---

## 1. Per-agent Azure identity

### The gap, with evidence

`infrastructure/modules/container-apps/hermes.tf` creates one user-assigned
managed identity, `id-hermes-<env>`, and assigns it `AcrPull`,
`Cognitive Services OpenAI User`, and `Key Vault Secrets User`. Every agent
role (Orchestrator, Coder, Infrastructure, Psychology, and the other ten) runs
inside that container app and therefore holds exactly that identity. The
Infrastructure profile (`agents/profiles/infrastructure.AGENTS.md`) uses the
`az` CLI "for all resource operations"; the Coder and Psychology profiles have
no Azure need at all, yet Azure cannot tell them apart.

The governance walkthrough
(`docs/walkthroughs/governance-and-blast-radius.md`) is honest that its layers
are the role model, replay fixtures, router tier mapping, and Terraform
profiles. None of those is an Azure control-plane boundary. The agent-identity
plane in `PR_BODY_agent-identity.md` adds signed per-agent assertions and a
policy decision point, but its PEP "ships inert", and even once wired it
authorizes *tool calls*, not Azure API calls. A compromised or prompt-injected
agent that reaches a shell still has whatever the shared identity has.

The Azure-native comparison report recommends adopting Entra Agent ID for
identity regardless of what else migrates. This enhancement is that
recommendation applied to the stack AAF already runs.

### What to build

1. **One Azure identity per role, declared in the profile.** Extend
   `agents/profile.schema.json` with an optional `azure_identity` block:
   `enabled`, a list of `role_assignments` (`scope`, `role_definition_name`),
   and an optional `key_vault_secrets` allow-list. Terraform iterates the
   profiles and creates a user-assigned managed identity per enabled role with
   exactly those assignments. Roles without the block get no Azure identity.
   Entra Agent ID is the preferred identity kind where the provider supports
   it; a plain user-assigned identity is the fallback so the feature does not
   wait on preview APIs.
2. **A token broker inside the runtime pod.** A small sidecar (or an endpoint
   on the existing router sidecar) accepts the ES256 agent assertion from the
   identity plane, verifies it with the existing Node/Python verifier, and
   exchanges it for an Azure access token for *that role's* identity using
   workload identity federation. The broker never hands out the pod identity's
   token to an agent process. The `az` CLI and Azure SDKs in the agent process
   are pointed at the broker via `AZURE_TOKEN_CREDENTIALS` / a credential
   plugin, so profiles do not change their instructions.
3. **Key Vault scoping follows the identity.** Secrets a role is allowed to
   read are granted to that role's identity only. The shared
   `Key Vault Secrets User` grant on the pod identity is reduced to the secrets
   the platform itself needs (router key, DB URL).
4. **Audit and attribution.** Every token exchange is written to the identity
   plane's decision store with role, release, tenant, requested scope, and
   outcome, so the flight recorder and the morning brief can show "which
   identity did what".
5. **Terraform guardrail.** A CI check (alongside the roster cost gate) fails
   the build if any role identity is granted `Owner`, `Contributor` at
   subscription scope, or any role containing `delete` on a resource group,
   unless an explicit `allow_destructive: true` is present with a reason.

### First slice and acceptance criteria

First slice: per-role identities for `infrastructure` and `security` only,
with the broker in observe mode (logs the exchange, still returns the shared
token) so the live path is unchanged.

1. `terraform plan` on the dev environment shows two new identities with the
   assignments declared in the two profiles and no change to any other
   resource.
2. A token exchange with a valid `infrastructure` assertion succeeds; one with
   a `coder` assertion is refused with a stable error and an audit row.
3. An expired, tenant-mismatched, or `alg=none` assertion is refused before
   any Azure call, proven by the existing adversarial fixtures.
4. The CI guardrail fails a profile that declares subscription-scope
   `Contributor` and passes the committed profiles.
5. The replay fixture for the "delete the resource group" scenario gains a
   second assertion: even with the prompt layer removed, the role's identity
   lacks `Microsoft.Resources/subscriptions/resourceGroups/delete`.

### Risks

- Container Apps allows one set of identities per app; separate role
  identities therefore need either the broker pattern above or one app per
  role. The broker is the cheaper path and keeps the current topology.
- Entra Agent ID GA shape may differ from preview; keep the identity kind
  behind a variable with user-assigned identity as the default.
- Depends on the agent-identity patch landing. The patch currently fails
  `git apply --check` against `main` (conflicts in `ci.yml`, `ROADMAP.md`,
  `dev/main.tf`) and needs a rebase first.

---

## 2. Governed MCP tool plane

### The gap, with evidence

The vendored Hermes runtime supports MCP natively: `mcp_servers` in
`config.yaml` (stdio and HTTP servers, per-server tool filtering, auto-reload
on config change, see `apps/hermes/src/cli.py` around the `mcp_servers`
watcher and `apps/hermes/src/website/docs/user-guide/features/mcp.md`), and
MCP tools become toolsets in `apps/hermes/src/toolsets.py`.

AAF never uses it. `apps/paperclip/write-hermes-config.sh` writes only the
`model` and `prompt_caching` sections. `agents/profile.schema.json` allows
only `terminal`, `file`, and `browser` toolsets. There is no mention of MCP in
`README.md`, `docs/architecture.md`, or `ROADMAP.md`. The only MCP-related
asset is the optional, off-by-default APIM gateway module inside the
uncommitted identity patch.

The consequence: AAF's governance story is "which tools is each agent allowed
to call", but its real answer is "a shell, or not a shell". A role that needs
GitHub, Azure DevOps, Jira, Microsoft Graph, or an internal API gets it by
having a secret in its environment and a CLI on the image. That is exactly the
posture the identity plane was written to replace.

### What to build

1. **MCP grants in the profile.** Add `mcp_servers` to the profile schema: a
   list of server names from a platform-level registry, each with an optional
   `tools` allow-list and `risk` class (`read`, `write`, `destructive`).
2. **A platform MCP registry.** `agents/mcp-registry.yaml` declares each
   server once: transport (stdio command or HTTP URL), required secrets by
   Key Vault name, default tool filter, and owner. CI validates every profile
   grant against the registry and every required secret against the Key Vault
   secret list the deploy already seeds.
3. **Config generation with secret binding.** `write-hermes-config.sh` (or a
   Python successor, since the shape is now non-trivial) renders the
   `mcp_servers` block for the *role being spawned*, injecting secrets from the
   mounted Key Vault refs and never from the shared environment. With
   per-agent identity (item 1) HTTP servers that accept Entra tokens get the
   role's token from the broker instead of a static secret.
4. **Policy and approval on every MCP call.** Route MCP tool invocations
   through `authorizeToolCall` in `apps/paperclip/tool-policy.mjs` with the
   server and tool name as the resource. `destructive` tools default to
   `require_approval`, which lands in the existing `approval.mjs` seam and
   `escalation_*` events. The decision ID, server, tool, and redacted arguments
   go to the flight recorder.
5. **AAF as an MCP server.** Expose a small, authenticated MCP endpoint on the
   auth-proxy that offers: create issue, get issue status, list pending
   approvals, approve or deny (operator scope only), and query governed
   memory for a workspace. This is what lets Microsoft 365 Copilot, VS Code,
   Claude Code, or a Foundry agent delegate work *into* AAF without a bespoke
   bridge, and it is the cheapest credible path to the roadmap's
   "Foundry Agent Service alignment and Microsoft 365 / Agent 365 publishing".

### First slice and acceptance criteria

First slice: registry, schema, config rendering, and one HTTP server (GitHub
MCP) granted to `coder` with a read-only tool filter. No inbound MCP server
yet.

1. A profile granting an unregistered server, an unlisted tool, or a server
   whose secret is not in the seeded Key Vault list fails CI with the offending
   name.
2. The rendered `config.yaml` for `coder` contains exactly the granted servers
   and filters; the rendered config for `psychology` contains no
   `mcp_servers` block. Both are covered by the existing
   `tests/paperclip/hermes-config-template.test.mjs` pattern.
3. Secrets appear in the rendered config only via Key Vault-mounted values; a
   test asserts the shared `ROUTER_API_KEY` never leaks into an MCP `env`.
4. In the local stack, `coder` can list a repository's pull requests through
   the MCP server and cannot call a filtered write tool; the attempt is denied
   and recorded.
5. The flight recorder shows the MCP call with server, tool, decision ID, and
   cost attribution to the role.

### Risks

- Per-tool interception happens inside the spawned Hermes process, the same
  constraint the identity plane noted. Hermes's per-server tool filter gives a
  hard allow-list without interception; the policy PEP adds the approval and
  audit layer on top. Ship the filter first, the PEP second.
- Stdio servers run inside the agent container and inherit its filesystem.
  Prefer HTTP servers, and run stdio servers only inside the sandbox provider
  once the ACA dynamic-sessions work lands.

---

## 3. Operator morning brief

### The gap, with evidence

`README.md` lists "What happened overnight, and which model made which
decision at what cost?" as a founding problem. The data to answer it exists:

- runs and outcomes: PaperClip `/api/companies/{id}/runs`, already polled by
  `services/watchdog/watchdog.py`;
- spend by tier and caller: router `/health` and the flight recorder's
  `cost_usd` / `caller` fields;
- breaker trips, waste breakers, and the paid-action kill switch state in the
  router;
- pending escalations and ack latency: governor `/escalation-sla`;
- memory admissions, contradictions, and review queue: governor `/digest`;
- per-agent track record: `services/watchdog/scorecards.py`.

What does not exist is a single composed view. `digest_post.py` posts only the
memory digest, only to a Discord webhook. The watchdog alert pack fires on
anomalies, so a quiet night produces nothing at all, and an operator has no
confirmation that quiet meant healthy.

### What to build

A scheduled job (same pattern as `digest_post.py`: stdlib only, no-op when the
delivery secret is absent) that assembles one message:

1. **Runs.** Completed, failed, and still-running counts per agent for the
   window, with the three slowest or costliest linked to their PaperClip issue.
2. **Spend.** Cost per tier and per agent, daily-cap headroom, and the
   tenant ledger figures when the durable budget path is enabled.
3. **Guardrails.** Breakers tripped, waste breakers fired, kill-switch state,
   approvals pending with age against SLA, and any budget blocks.
4. **Memory.** The governor digest summary and any contradictions needing
   review.
5. **Delta.** Scorecard changes versus the prior window so a degrading agent is
   visible before the alert pack fires.

Delivery targets: Slack via the existing `chat.postMessage` path, Teams via a
proactive message on the bridge's stored conversation reference, Discord via
webhook, and a Markdown file written to the Obsidian vault the governor
already maintains. The brief carries deep links into the PaperClip UI.

### First slice and acceptance criteria

First slice: runs, spend, and pending approvals, delivered to Slack and the
vault; pure assembly functions tested offline with fixture JSON.

1. Given fixture responses from the three sources, the renderer produces a
   stable Markdown body under 3,900 characters, covered by snapshot tests.
2. Any single source being unreachable produces a brief with that section
   marked unavailable, never an empty or failed post.
3. With no delivery secret configured the job exits 0 and writes nothing.
4. A Terraform `container_app_job` on a cron schedule, flag-gated like the
   digest job, is validated by the existing `terraform validate` CI job.

### Risks

Low. The main design choice is whether to compute inside the watchdog tick or
as a separate job. A separate job keeps the watchdog's alert path untouched.

---

## 4. Memory subject erasure and export

### The gap, with evidence

The ship-readiness review's SR-7 finding (cross-user memory co-mingling)
was answered by the `ActorContext` contract in the identity patch, which gives
each end user a hashed, tenant-scoped subject. That makes it possible, for the
first time, to say which memories, flight-recorder events, and issues belong to
one person. Nothing acts on that: `grep` finds no erase, forget, purge, or
export operation in `services/memory-governor/src/governor/`, Honcho peers are
keyed by agent slug, and the flight recorder's JSONL files are append-only
with redaction but no subject index.

Any deployment that serves real people in a workspace will receive a deletion
or access request. Without this feature the only honest answer is "we delete
the whole workspace".

### What to build

1. **Subject index.** Record `subject_hash` on governor documents, Honcho
   sessions created by the bridges, flight-recorder events, and PaperClip
   issues created from chat surfaces.
2. **Export.** An operator-authenticated endpoint that returns, for one
   subject, every governed memory, session summary, and issue reference as a
   JSON bundle, with the same redaction rules the flight recorder enforces.
3. **Erase.** A two-step operation (request, then confirm with the request ID
   after a configurable hold) that deletes or tombstones those records across
   all stores, writes an immutable erasure event, and reports per-store
   counts. Derived artifacts (digests, scorecards) are regenerated without the
   subject.
4. **Proof.** An offline test seeds two subjects in one workspace, erases one,
   and asserts the other's memories, sessions, and issues are untouched.

### First slice and acceptance criteria

First slice: subject tagging and export only; erase follows once export is
trusted.

1. New memories admitted through a bridge carry `subject_hash`; memories from
   agent-only paths carry `null` and are excluded from subject operations.
2. Export for a subject returns only that subject's records, proven by the
   two-subject fixture.
3. Export is operator-authenticated, tenant-scoped under RLS, and rate-limited.

---

## Recommended sequencing

1. **Rebase and land the agent-identity patch.** Items 1, 2, and 4 all build
   on its assertions, PDP, and `ActorContext`. It is the enabling dependency.
2. **Operator morning brief** in parallel, since it has no dependency and
   gives immediate visible value.
3. **Governed MCP tool plane, first slice** (registry, schema, rendering,
   one read-only server). This is the fastest route to a visibly richer
   governance story.
4. **Per-agent Azure identity, first slice** (two roles, broker in observe
   mode). Then switch the broker to enforce and extend to all roles.
5. **Erasure and export**, before any deployment that serves more than one
   human per workspace.

## Explicitly not selected here

- The evaluation release control plane
  (`docs/superpowers/plans/2026-07-19-agentops-evaluation-release-control-plane.md`)
  is already planned and still worthwhile; it is omitted only because it is not
  a new identification.
- Streaming usage-aware budget settlement is a known follow-on in the
  multi-tenant track and should stay there.
- The seven items in the August slate remain valid and are not re-argued.

## Implementation status (2026-10-17)

| Item | Shipped | Not yet |
| --- | --- | --- |
| 1. Per-agent Azure identity | `azure_identity` profile block; `infrastructure` and `security` opted in with read-only grants; `scripts/agent-identity-guard.py` in CI; `agent_identities.tf` behind `agent_identities_enabled`; dev plan saved (5 adds). | Token broker and workload-identity exchange; Key Vault scoping of the shared pod identity; replay-fixture assertion. Applying the RBAC plan is an operator action. |
| 2. Governed MCP tool plane | Registry + schema; `mcp_servers` grants validated; `render-hermes-mcp.py` wired into `write-hermes-config.sh`; `deepwiki` (no credential) and `github` (token-gated) registered; Terraform `mcp_servers_enabled` / `mcp_github_enabled`; 17 tests. | Per-role enforcement through the policy PEP; AAF as an MCP server; stdio servers inside the sandbox provider. |
| 3. Operator morning brief | `governor/morning_brief.py` + 15 tests; `caj-morning-brief-<env>` job; deployed to dev and run once successfully. | Teams proactive delivery; Obsidian vault file; scorecard deltas. Runs section needs `paperclip_company_id` set in tfvars (same gap that idles the watchdog job). |
| 4. Erasure and export | `governor/subject.py` (peppered subject hash); `/admit` tags `internal_metadata.subject_hash`; `GET /subjects/{hash}/export`; two-step `POST /subjects/{hash}/erase` with counts-only audit event; 11 offline tests. | Subject tagging in the Slack/Teams bridges and flight recorder; PaperClip issue references; retention hold policy. |

Two pre-existing defects surfaced by the first live brief run and are **not**
fixed here: the governor's `/digest` returns 500 because the live database has
no `agent_events` / `feature_flags` tables (the governed-memory migrations were
never applied to the dev database), and `paperclip_company_id` is unset in the
dev tfvars so neither the watchdog nor the brief can read PaperClip runs.

## Operator runbook: finishing the dev deploy (2026-10-18)

Status after the second pass: every Key Vault write path (direct
`az keyvault secret set` and the project's own `scripts/seed-keyvault.sh`)
and every RBAC apply were refused by the auto-mode permission boundary, so
the PaperClip roll-forward was re-cut to need neither.

- **PaperClip signing key.** The entrypoint now derives a stable subkey from
  `BETTER_AUTH_SECRET` when `PAPERCLIP_DECISION_SIGNING_SECRET` is absent, so
  the `31c6f1b` image (MCP plane + fallback) boots on the SMB share without a
  new vault secret. To move to a dedicated secret later: run
  `scripts/seed-keyvault.sh --vault aaf-vault-dev-kv` (it now generates
  `paperclip-decision-signing-secret`), set
  `paperclip_decision_signing_secret_enabled = true`, and re-apply paperclip.
- **Per-agent identities** still need a human apply (5 adds, RBAC):

  ```bash
  terraform -chdir=infrastructure/environments/dev apply tfplan-identities
  ```

  Re-plan with the same two `-target`s if the saved plan has gone stale.
- **Watchdog and morning-brief runs section**: set `paperclip_company_id` in
  the dev tfvars (PaperClip is internal-ingress only and Postgres is VNet-only,
  so the id must be read from inside the environment).
- **Governor `/digest` 500**: apply `infrastructure/migrations/` to the Honcho
  database; the live database has no `agent_events` / `feature_flags` tables.
