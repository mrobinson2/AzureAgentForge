# aaf-0014: storage-account firewall allowlists. Default-Deny is applied on the
# hermes storage account (see storage.tf); these let the operator scope who may
# reach it. Leave empty to rely on the AzureServices/Logging/Metrics bypass.
# ⚠️ This ACA environment mounts the SMB file share over the Azure backbone, not
# the app subnet — after enabling default-Deny, verify the hermes-data /
# watchdog-state mounts still attach and add the ACA outbound IP(s) here if not.
variable "storage_allowed_ip_ranges" {
  description = "Public IP CIDRs allowed through the hermes storage-account firewall (aaf-0014)."
  type        = list(string)
  default     = []
}

variable "storage_allowed_subnet_ids" {
  description = "VNet subnet IDs allowed through the hermes storage-account firewall (aaf-0014)."
  type        = list(string)
  default     = []
}

variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "prefix" {
  type = string
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "existing_environment_id" {
  description = "If provided, skip creation and reuse the specified Container Apps Environment"
  type        = string
  default     = ""
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "environment_type" {
  description = "Consumption or Dedicated workload profiles"
  type        = string
  default     = "Consumption"
}

variable "infrastructure_subnet_id" {
  type = string
}

variable "internal_load_balancer_enabled" {
  description = "Enable internal load balancer (false = public ingress with IP restrictions)"
  type        = bool
  default     = false
}

variable "gateway_allowed_ip_addresses" {
  description = "List of IP addresses/CIDR blocks allowed to access the gateway (empty = allow all)"
  type        = list(string)
  default     = []
}

variable "log_analytics_workspace_id" {
  type    = string
  default = ""
}

variable "key_vault_id" {
  type = string
}

variable "key_vault_uri" {
  type = string
}

variable "postgres_fqdn" {
  type = string
}

variable "postgres_admin_username" {
  description = "PostgreSQL admin username"
  type        = string
  default     = "postgres"
}

variable "postgres_admin_password" {
  description = "PostgreSQL admin password"
  type        = string
  sensitive   = true
}

# Azure AI Foundry Configuration
variable "ai_foundry_endpoint" {
  description = "Azure AI Foundry endpoint URL"
  type        = string
}

variable "ai_foundry_deployment_id" {
  description = "Azure AI Foundry model deployment ID"
  type        = string
  default     = "qwen-qwen3.5-9b"
}

variable "ai_foundry_resource_id" {
  description = "Azure AI Foundry Cognitive Account resource ID for RBAC"
  type        = string
}

# Container Registry
variable "container_registry_id" {
  description = "Container Registry ID for AcrPull role assignment"
  type        = string
}

variable "create_acr_pull_role" {
  description = "Set to true (default) to create the AcrPull role assignment for the shared managed identity. Set to false when container_registry_id is not yet known at plan time."
  type        = bool
  default     = true
}

variable "container_registry_login_server" {
  description = "Container Registry login server (e.g., myregistry.azurecr.io)"
  type        = string
}

# Container Image Tags
variable "hermes_image_tag" {
  description = "Hermes agent container image tag"
  type        = string
  default     = "latest"
}

variable "honcho_image_tag" {
  description = "Honcho memory service container image tag"
  type        = string
  default     = "latest"
}

variable "router_image_tag" {
  description = "Model router sidecar container image tag"
  type        = string
  default     = "latest"
}

# Multi-model router configuration
variable "grok_model_deployment" {
  description = "Azure AI Foundry deployment name for the primary Grok model (grok-4-1-fast-reasoning)"
  type        = string
  default     = "grok-4-1-fast-reasoning"
}

variable "phi_model_deployment" {
  description = "Azure AI Foundry deployment name for Phi-4 (budget fallback tier)"
  type        = string
  default     = "Phi-4"
}

variable "kimi_model_deployment" {
  description = "Azure AI Foundry deployment name for Kimi-K2.5 (complex coding / technical tier)"
  type        = string
  default     = "Kimi-K2.5"
}

variable "claude_model" {
  description = "Anthropic Claude model ID. claude-sonnet-4-6 recommended; use claude-opus-4-6 to escalate."
  type        = string
  default     = "claude-sonnet-4-6"
}

# Application Insights
variable "app_insights_connection_string" {
  description = "Application Insights connection string for telemetry"
  type        = string
  sensitive   = true
}

variable "observability_enabled" {
  description = "Emit GenAI-semconv spans from the model-router to App Insights."
  type        = bool
  default     = false
}

# Legacy gateway variables (can be removed after migration)
variable "gateway_image" {
  description = "Gateway container image (full path with tag)"
  type        = string
  default     = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
}

variable "gateway_primary_model" {
  description = "Primary AI model for the gateway"
  type        = string
  default     = "kimi-k2.5"
}

variable "gateway_pairing_mode" {
  description = "Telegram pairing mode"
  type        = string
  default     = "pairing"
}

variable "cf_tunnel_token" {
  description = "Cloudflare Tunnel token (Key Vault secret ID)"
  type        = string
  default     = ""
}

variable "enable_debug_sidecar" {
  description = "Deploy netshoot debug sidecar alongside AzureAgentForge (never true in production)"
  type        = bool
  default     = false
}

# Other service images (placeholder until developed)
variable "api_gateway_image" {
  type    = string
  default = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
}

variable "ingest_image" {
  type    = string
  default = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
}

variable "query_image" {
  type    = string
  default = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
}

variable "writeback_image" {
  type    = string
  default = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
}

variable "enable_jobs" {
  type    = bool
  default = false
}

variable "indexer_image" {
  type    = string
  default = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
}

variable "reconciliation_image" {
  type    = string
  default = "mcr.microsoft.com/azuredocs/aci-helloworld:latest"
}

# ── Paperclip Orchestrator ────────────────────────────────────────────────────

variable "paperclip_image_tag" {
  description = "Paperclip container image tag (set by CI/CD pipeline)"
  type        = string
  default     = "latest"
}

variable "paperclip_public_url" {
  description = "Public HTTPS URL of the Paperclip UI (ACA FQDN). Set after first deploy: https://<fqdn>"
  type        = string
  default     = ""
}

variable "paperclip_allowed_hostnames" {
  description = "Comma-separated hostnames Paperclip will accept (for CORS / CSRF). Defaults to public URL hostname."
  type        = string
  default     = "localhost"
}

variable "paperclip_company_id" {
  description = "PaperClip company UUID created during bootstrap. Used by Hermes agents via pc-delegate.sh for issue delegation."
  type        = string
  default     = ""
}

variable "ai_foundry_openai_endpoint" {
  description = "Azure AI Foundry OpenAI-compatible endpoint URL (project-scoped). All models in the project share this endpoint."
  type        = string
}

# ── Environment-specific sizing ──────────────────────────────────────────────
# Override per-environment. Dev defaults are minimal; prod should increase.
# Example: terraform apply -var="honcho_min_replicas=1" -var="honcho_cpu=0.5"

variable "honcho_cpu" {
  description = "Honcho API vCPU allocation (dev=0.25, prod=0.5)"
  type        = number
  default     = 0.25
}

variable "honcho_memory" {
  description = "Honcho API memory allocation (dev=0.5Gi, prod=1Gi)"
  type        = string
  default     = "0.5Gi"
}

variable "honcho_min_replicas" {
  description = "Honcho API minimum replicas (dev=0 for scale-to-zero, prod=1)"
  type        = number
  default     = 0
}

variable "honcho_max_replicas" {
  description = "Honcho API maximum replicas (dev=1, prod=3)"
  type        = number
  default     = 1
}

variable "honcho_deriver_min_replicas" {
  description = "Honcho Deriver minimum replicas (dev=0, prod=1)"
  type        = number
  default     = 0
}

variable "honcho_deriver_enabled" {
  description = "When false, the Honcho Deriver Container App (always-on) is not created. Used to suppress the ~$0.65/day active-CPU cost in dev. When false, consider also enabling honcho_deriver_job_enabled for periodic batch runs."
  type        = bool
  default     = true
}

variable "honcho_deriver_job_enabled" {
  description = "When true, provision a Container Apps Job that runs the deriver on a schedule (replaces the always-on Container App for cost savings). Mutually exclusive with honcho_deriver_enabled — set that to false when using the Job."
  type        = bool
  default     = false
}

variable "honcho_deriver_job_cron" {
  description = "Cron expression for the deriver Job schedule (UTC). Default = hourly at :00. Format: 'minute hour day month weekday'."
  type        = string
  default     = "0 * * * *"
}

variable "honcho_workspace_name" {
  description = "Honcho / governed-memory workspace name (formerly 'app id'). Keep this aligned across Telegram-Hermes, Paperclip, and the self-hosted-site compose (GOVERNOR_WORKSPACE) so the Orchestrator reads/writes the same memory the Telegram bot built up."
  type        = string
  # aaf-0015: NO default. A config default here previously let dev and prod
  # silently resolve to the SAME governed-memory workspace (the recurring scoping
  # trap). Every environment MUST set this explicitly in its tfvars. The
  # validation below fails loud on an empty or placeholder value rather than
  # silently scoping one plausible workspace.
  validation {
    condition     = length(trimspace(var.honcho_workspace_name)) > 0 && !contains(["CHANGEME", "TODO", "hermes-dev-CHANGEME"], var.honcho_workspace_name)
    error_message = "honcho_workspace_name must be set explicitly per environment (e.g. \"hermes\"); empty or placeholder values are rejected (aaf-0015)."
  }
}

# A5: canonical user peer. The ONE deploy-time id for the human principal's
# memory peer, threaded into every container that writes or queries user-scoped
# memory (hermes gateway, paperclip helpers, memory-governor /admit default).
# The default MUST equal the code-level fallback everywhere ("user" — governor
# config.user_peer_id(), pc-memory/pc-honcho, compose) — the previous default
# here was "operator", a divergent placeholder, which is exactly the
# fragmentation trap: components defaulting to DIFFERENT peers means writes
# land where no reader looks. If an existing deployment's memory was built
# under another peer id, set it per environment in tfvars after discovering it
# with `pc-honcho list-peers` in the running paperclip container.
# See docs/design/memory-system.md §18.
variable "honcho_user_peer_id" {
  description = "Canonical Honcho peer ID for the principal user — every component that writes or queries user-scoped memory resolves this single input. Keep aligned with HONCHO_USER_PEER_ID in the compose stacks; empty/placeholder values are rejected."
  type        = string
  default     = "user"
  validation {
    condition     = length(trimspace(var.honcho_user_peer_id)) > 0 && !contains(["CHANGEME", "TODO"], var.honcho_user_peer_id)
    error_message = "honcho_user_peer_id must be a real peer id (default \"user\"); empty or placeholder values fragment user identity across peers."
  }
}

variable "honcho_agent_peer_ids" {
  description = "Comma-separated agent slugs that are legitimate memory peers alongside honcho_user_peer_id. The governor reports a write whose peer is neither — the identity-fragmentation signal from docs/design/memory-system.md §18. Empty (default) declares no roster and reports nothing: a deployment that has not listed its agents should not have every write flagged."
  type        = string
  default     = ""
}

variable "honcho_peer_aliases" {
  description = "Comma-separated alias=canonical peer pairs, rewritten at the governor's admission choke point before a write is stored. Use to fold legacy or per-channel peers (e.g. \"operator=user\") into the canonical identity as they arrive. Empty (default) rewrites nothing."
  type        = string
  default     = ""
}

variable "honcho_deriver_job_timeout_seconds" {
  description = "Max seconds a single deriver Job run can execute before ACA forcibly kills it. The deriver is a long-running poller; the timeout bounds cost per run. 600s = 10 min."
  type        = number
  default     = 600
}

variable "brave_search_enabled" {
  description = "Enable the brave-search wrapper inside paperclip (mounts BRAVE_SEARCH_API_KEY from KV secret platform-brave-search-api-key). DuckDuckGo blocks Azure cloud IPs so this is the practical default; set false only if you want to disable web search entirely."
  type        = bool
  default     = true
}

variable "exa_search_enabled" {
  description = "Enable the exa-search wrapper inside paperclip (mounts EXA_API_KEY from the KV secret exa-api-key). Semantic web search alongside brave-search; opt-in because it needs an Exa key seeded into Key Vault first. Default off."
  type        = bool
  default     = false
}

variable "telegram_enabled" {
  type        = bool
  description = "Enable the Telegram chat surface (agent-runtime Telegram gateway)."
  default     = false
}

variable "discord_enabled" {
  type        = bool
  description = "Enable the Discord chat surface (PaperClip Discord plugin)."
  default     = false
}

variable "teams_enabled" {
  type        = bool
  description = "Enable the Microsoft Teams chat surface (services/teams-bridge Bot Framework endpoint). Internal ingress — expose via the Cloudflare tunnel + add Bot Framework JWT validation before go-live."
  default     = false
}

variable "teams_bridge_image_tag" {
  type        = string
  description = "Image tag for the teams-bridge container."
  default     = "latest"
}

variable "teams_orchestrator_agent_id" {
  type        = string
  description = "Optional agent id to route inbound Teams messages to (the Orchestrator). Empty → PaperClip default routing."
  default     = ""
}

variable "teams_app_id" {
  type        = string
  description = "aaf-0009: the Bot Framework Microsoft App ID (audience for inbound Teams JWTs — services/teams-bridge/main.py TEAMS_APP_ID). The bridge fails closed (503) when this is unset. Not itself a credential (Azure AD verifies the token signature independently), but treated like the other bot identifiers in this module — set per environment, never a real value in this sanitized repo."
  default     = ""
}

variable "slack_enabled" {
  type        = bool
  description = "Enable the Slack chat surface (services/slack-bridge Slack Events API endpoint). Internal ingress — expose via the Cloudflare tunnel + set SLACK_SIGNING_SECRET before go-live."
  default     = false
}

variable "slack_bridge_image_tag" {
  type        = string
  description = "Image tag for the slack-bridge container."
  default     = "latest"
}

variable "slack_orchestrator_agent_id" {
  type        = string
  description = "Optional agent id to route inbound Slack messages to (the Orchestrator). Empty → PaperClip default routing."
  default     = ""
}

variable "paperclip_workspaces_tmpfs" {
  description = "When true, the paperclip entrypoint symlinks /paperclip/instances/<id>/workspaces -> /tmp/paperclip-workspaces so per-agent workspace dirs live on tmpfs (full POSIX) instead of the SMB-mounted Azure File Share (where chmod/chown are mount-time-immutable, causing EACCES for node-user file writes). Workspaces are ephemeral scratch space so loss on container restart is fine; persistent state lives elsewhere (PaperClip DB, git, KV)."
  type        = bool
  default     = false
}

variable "paperclip_cpu" {
  description = "PaperClip vCPU allocation (dev=0.5, prod=1.0)"
  type        = number
  default     = 0.5
}

variable "paperclip_memory" {
  description = "PaperClip memory allocation (dev=1Gi, prod=2Gi)"
  type        = string
  default     = "1Gi"
}

variable "cloudflared_enabled" {
  type        = bool
  description = "Run the Cloudflared tunnel container for ingress (hardened). When false, use Azure Container Apps managed ingress."
  default     = false
}

# ── Governed memory (memory-governor + watchdog) ──────────────────────────────
# All gated OFF by default. Every behavior is additionally feature-flag-gated
# in-app (the migrations seed every flag false), so deploying this stack is a
# no-op until an operator both flips var.memory_governor_enabled and turns flags
# on per environment.

variable "memory_governor_enabled" {
  type        = bool
  description = "Deploy the memory-governor service + sweeper/digest/watchdog jobs. Off by default."
  default     = false
}

variable "memory_governor_image_tag" {
  type        = string
  description = "Image tag for the memory-governor container."
  default     = "latest"
}

variable "watchdog_image_tag" {
  type        = string
  description = "Image tag for the watchdog container."
  default     = "latest"
}

variable "memory_planner_agent_allowlist" {
  type        = string
  description = "Comma-separated agent slugs the retrieval planner may inject for (canary). Empty = nobody, even with MEMORY_PLANNER_ENABLED on."
  default     = ""
}

variable "memory_classifier_daily_budget_usd" {
  type        = string
  description = "Daily budget cap (USD) for the governor's economy classification tier."
  default     = "1.00"
}

variable "budget_enforce_mode" {
  type        = string
  description = "Router acting budget enforcement (A6): warn (default; observe-only, pre-A6 behavior) | downgrade (serve budget_fallback_tier + X-Router-Budget-Downgrade header) | block (429 budget_exceeded). The router fails open to warn on any other value."
  default     = "warn"

  validation {
    condition     = contains(["warn", "downgrade", "block"], var.budget_enforce_mode)
    error_message = "budget_enforce_mode must be one of: warn, downgrade, block."
  }
}

variable "budget_fallback_tier" {
  type        = string
  description = "Tier the router serves instead when budget_enforce_mode=downgrade trips (the designated floor; exempt from enforcement)."
  default     = "gpt4o-mini"
}

variable "embedding_daily_budget_usd" {
  type        = string
  description = "Daily budget cap (USD) for the router's /v1/embeddings ledger bucket (A6). \"0\" disables the cap; spend is still tracked."
  default     = "1.00"
}

variable "memory_sweeper_cron" {
  type        = string
  description = "Cron expression for the nightly TTL sweeper job."
  default     = "0 4 * * *"
}

variable "watchdog_cron" {
  type        = string
  description = "Cron expression for the self-improvement-loop watchdog job."
  default     = "*/10 * * * *"
}

variable "memory_digest_webhook_url" {
  type        = string
  description = "Chat webhook URL for the daily memory digest. Empty = the digest job is not created."
  default     = ""
}

variable "memory_digest_cron" {
  type        = string
  description = "Cron expression for the daily memory digest poster job."
  default     = "0 13 * * *"
}
