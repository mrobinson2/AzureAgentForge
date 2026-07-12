variable "subscription_id" {
  description = "Azure subscription ID to deploy into. Find it with: az account show --query id -o tsv"
  type        = string
}

variable "project_name" {
  description = "Project name prefix for all resources. Drives GLOBALLY-UNIQUE names — the Key Vault (<project>-<env>-kv) and the storage account (<project><env>sa, dashes stripped, 24 chars) — so the default 'aaf-vault' WILL collide with other adopters. Set it to a value unique to your subscription; scripts/bootstrap.sh preflights availability and tells you if it's taken."
  type        = string
  default     = "aaf-vault"
}

variable "environment" {
  description = "Environment name (dev, staging, prod)"
  type        = string
  default     = "dev"
}

variable "location" {
  description = "Azure region for resources"
  type        = string
  default     = "centralus"
}

variable "cost_center" {
  description = "Cost center for billing attribution"
  type        = string
  default     = "platform"
}

variable "owner_email" {
  description = "Owner email for resource tagging"
  type        = string
  default     = "owner@example.com"
}

variable "postgres_admin_username" {
  description = "PostgreSQL administrator username"
  type        = string
  default     = "dbadmin"
}

variable "container_registry_name" {
  description = "Name for the Azure Container Registry (must be globally unique, alphanumeric only, 5-50 chars)"
  type        = string
  default     = "aafregistry"
}

variable "keyvault_admin_object_ids" {
  description = "List of Azure AD object IDs granted Key Vault Secrets Officer. Add your own user OID and your CI/CD service principal OID here."
  type        = list(string)
  default     = []
}

variable "openai_api_key" {
  description = "OpenAI API Key"
  type        = string
  sensitive   = true
  default     = "" # Can be set via environment variable or TF_VAR_openai_api_key
}

variable "openai_endpoint" {
  description = "Azure OpenAI endpoint (e.g., my-openai-resource)"
  type        = string
  default     = ""
}

variable "key_vault_public_network_access_enabled" {
  description = "Allow public network access to Key Vault (cost-optimized: true; hardened: false, requires private endpoint)."
  type        = bool
  default     = true
}

variable "key_vault_network_default_action" {
  description = "Default Key Vault firewall action. aaf-0013: defaults to \"Deny\" (secure-by-default) — with public access on, add the Terraform-runner / self-hosted-site egress IP to key_vault_allowed_ip_ranges (and/or the app subnet to key_vault_allowed_subnet_ids) so callers are not locked out. The hardened profile uses a private endpoint and is unaffected."
  type        = string
  default     = "Deny"
}

variable "key_vault_allowed_ip_ranges" {
  description = "IP ranges (CIDRs) to allow when network default action is Deny. MUST include the Terraform-runner egress IP so apply can read postgres-admin-password. Example placeholder: [\"203.0.113.0/32\"]."
  type        = list(string)
  default     = []
}

variable "key_vault_allowed_subnet_ids" {
  description = "aaf-0013: VNet subnet IDs (e.g. the app subnet) to allow through the default-Deny Key Vault firewall. Operator-gated; empty relies on the private endpoint or an allowed_ip_ranges entry."
  type        = list(string)
  default     = []
}

# aaf-0014: hermes storage-account firewall allowlists (see
# modules/container-apps/storage.tf). Same Deny-by-default / empty-allowlist
# semantics as the Key Vault pair above: empty relies on the AzureServices /
# Logging / Metrics bypass (which covers the ACA SMB mount over the Azure
# backbone), NOT an implicit allow — add entries only if a caller reaches the
# storage account over the network directly (outside that bypass) and gets
# locked out.
variable "storage_allowed_ip_ranges" {
  description = "Public IP CIDRs allowed through the hermes storage-account firewall when it is Deny-by-default. Example placeholder: [\"203.0.113.0/32\"]."
  type        = list(string)
  default     = []
}

variable "storage_allowed_subnet_ids" {
  description = "aaf-0014: VNet subnet IDs allowed through the hermes storage-account firewall. Operator-gated; empty relies on the AzureServices/Logging/Metrics bypass covering the ACA SMB mount."
  type        = list(string)
  default     = []
}

variable "existing_container_app_environment_id" {
  description = "If set, reuse an existing Container Apps Environment instead of creating a new one. Leave empty to create a new environment."
  type        = string
  default     = ""
}

# Feature Flags
variable "telegram_enabled" {
  description = "Enable the Telegram chat surface (agent-runtime Telegram gateway)."
  type        = bool
  default     = false
}

variable "discord_enabled" {
  description = "Enable the Discord chat surface (PaperClip Discord plugin)."
  type        = bool
  default     = false
}

variable "teams_enabled" {
  description = "Enable the Microsoft Teams chat surface (teams-bridge)."
  type        = bool
  default     = false
}

variable "teams_orchestrator_agent_id" {
  description = "Optional agent id to route inbound Teams messages to. Empty → PaperClip default routing."
  type        = string
  default     = ""
}

variable "teams_app_id" {
  description = "aaf-0009: Bot Framework Microsoft App ID (teams-bridge TEAMS_APP_ID). The bridge fails closed (503) when unset. Required before teams_enabled = true actually serves traffic."
  type        = string
  default     = ""
}

variable "slack_enabled" {
  description = "Enable the Slack chat surface (slack-bridge)."
  type        = bool
  default     = false
}

variable "slack_orchestrator_agent_id" {
  description = "Optional agent id to route inbound Slack messages to. Empty → PaperClip default routing."
  type        = string
  default     = ""
}

# Azure AI Foundry Configuration
variable "ai_foundry_endpoint" {
  description = "Azure AI Foundry OpenAI-compatible endpoint URL"
  type        = string
  default     = ""
}

variable "ai_foundry_deployment_id" {
  description = "Azure AI Foundry model deployment ID (used by Honcho for memory ops)"
  type        = string
  default     = "gpt-4o-mini"
}

# Container Image Tags
variable "hermes_image_tag" {
  description = "Hermes agent container image tag (set by CI/CD pipeline)"
  type        = string
  default     = "latest"
}

variable "honcho_image_tag" {
  description = "Honcho memory service container image tag (set by CI/CD pipeline)"
  type        = string
  default     = "latest"
}

variable "router_image_tag" {
  description = "Model router sidecar container image tag (set by CI/CD pipeline)"
  type        = string
  default     = "latest"
}

variable "paperclip_image_tag" {
  description = "Paperclip orchestrator container image tag (set by CI/CD pipeline)"
  type        = string
  default     = "latest"
}

# Paperclip config
variable "paperclip_public_url" {
  description = "Public HTTPS URL of Paperclip UI via Cloudflare Tunnel."
  type        = string
  default     = "https://app.example.com"
}

variable "paperclip_allowed_hostnames" {
  description = "Single hostname Paperclip accepts for CORS/CSRF (must match PAPERCLIP_PUBLIC_URL hostname). Automation writes bypass this via X-Automation-Sub; only browser sessions need it."
  type        = string
  default     = "app.example.com"
}

# Azure AI Foundry — project-scoped OpenAI-compatible endpoint.
# All models share this endpoint and API key.
variable "ai_foundry_openai_endpoint" {
  description = "Azure AI Foundry OpenAI-compatible endpoint URL (project-scoped)"
  type        = string
  default     = ""
}

# Primary model deployment name
variable "grok_model_deployment" {
  description = "Azure AI Foundry deployment name for the primary model"
  type        = string
  default     = "grok-4-1-fast-reasoning"
}

variable "ai_foundry_resource_id" {
  description = "Azure AI Foundry Cognitive Account resource ID for RBAC role assignments"
  type        = string
  default     = ""
}

# Gateway IP Restrictions
variable "gateway_allowed_ip_addresses" {
  description = "List of IP addresses/CIDR blocks allowed to access the gateway (e.g., [\"192.168.1.0/24\", \"203.0.113.42\"])"
  type        = list(string)
  default     = [] # Empty = no restrictions (set during deployment)
}

# ── Cost knobs ──────────────────────────────────────────────────────────────────

# PostgreSQL tier and HA
variable "postgres_sku_name" {
  description = "PostgreSQL SKU (cost-optimized: B_Standard_B1ms; hardened: GP_Standard_D2s_v3)."
  type        = string
  default     = "B_Standard_B1ms"
}

variable "postgres_storage_mb" {
  description = "PostgreSQL storage in MB (cost-optimized: 32768; hardened: 65536+)."
  type        = number
  default     = 32768
}

variable "postgres_high_availability_enabled" {
  description = "Enable zone-redundant high availability for PostgreSQL (roughly doubles compute cost)."
  type        = bool
  default     = false
}

# Log Analytics retention and daily cap
variable "log_retention_in_days" {
  description = "Log Analytics workspace retention in days (cost-optimized: 30; hardened: 90)."
  type        = number
  default     = 30
}

variable "log_daily_quota_gb" {
  description = "Log Analytics daily ingestion cap in GB (cost-optimized: 1; hardened: -1 for unlimited)."
  type        = number
  default     = 1
}

# Observability: alerts + workbook (opt-in)
variable "alert_emails" {
  description = "Email recipients for the platform alert action group (watchdog critical findings, secret expiry, watchdog run failures). Empty = no action group / no alert rules created."
  type        = list(string)
  default     = []
}

variable "watchdog_app_name" {
  description = "Container App/Job name to scope the alert + workbook log queries (e.g. caj-watchdog-dev). Empty matches across all apps on the unique [watchdog] log markers."
  type        = string
  default     = ""
}

variable "enable_observability_workbook" {
  description = "Create the Azure Monitor observability workbook (watchdog activity, secret expiry, gateway health). Off by default."
  type        = bool
  default     = false
}

# Cloudflared ingress
variable "cloudflared_enabled" {
  description = "Run the Cloudflared tunnel container for ingress (hardened). When false, use Azure Container Apps managed ingress."
  type        = bool
  default     = false
}

# Governed memory
variable "memory_governor_enabled" {
  description = "Deploy the memory-governor service + sweeper/digest/watchdog jobs. Off by default; every behavior is additionally feature-flag-gated in-app."
  type        = bool
  default     = false
}

variable "memory_planner_agent_allowlist" {
  description = "Comma-separated agent slugs the retrieval planner may inject for (canary). Empty = nobody, even with MEMORY_PLANNER_ENABLED on."
  type        = string
  default     = ""
}

variable "honcho_workspace_name" {
  description = "aaf-0015: Honcho / governed-memory workspace name. NO default — set it explicitly per environment in tfvars (generic placeholder \"hermes\" in the example) so dev/prod never silently share a workspace. Empty or CHANGEME/TODO placeholders are rejected by the module validation."
  type        = string
}

variable "honcho_user_peer_id" {
  description = "A5: canonical user peer — the ONE peer id representing the human principal, threaded into hermes, paperclip, and the memory-governor alike. Default \"user\" matches every component's code-level fallback; override in tfvars only if this environment's memory history lives under a different peer (discover with `pc-honcho list-peers`)."
  type        = string
  default     = "user"
}

variable "observability_enabled" {
  description = "Emit GenAI-semconv spans from the model-router to App Insights. Off by default; flip to true in dev.auto.tfvars to activate."
  type        = bool
  default     = false
}

variable "existing_vnet_name" {
  description = "Bring-your-own-VNet: deploy into this existing VNet (e.g. an ALZ-vended one) instead of creating one. Empty creates a new VNet."
  type        = string
  default     = ""
}

variable "existing_vnet_resource_group" {
  description = "Resource group of existing_vnet_name, if different from the deployment RG."
  type        = string
  default     = ""
}

# ── Cloudflare Tunnel (optional Terraform-managed ingress) ───────────────────
# When cloudflare_managed = true, the cloudflare-tunnel module creates the tunnel
# + ingress + DNS and Terraform writes the connector token into the
# cf-tunnel-token Key Vault secret. When false (default), Cloudflare is set up
# out-of-band and that secret is seeded by scripts/seed-keyvault.sh as today.
variable "cloudflare_managed" {
  description = "Manage the Cloudflare Tunnel + ingress + DNS (and the cf-tunnel-token secret) via Terraform. Requires cloudflared_enabled = true to do anything useful."
  type        = bool
  default     = false
}

variable "cloudflare_api_token" {
  description = "Cloudflare API token (scopes: Account → Cloudflare Tunnel: Edit, Zone → DNS: Edit). Only used when cloudflare_managed = true."
  type        = string
  default     = ""
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID. Required when cloudflare_managed = true."
  type        = string
  default     = ""
}

variable "cloudflare_zone_id" {
  description = "Cloudflare DNS zone ID for the public hostname's domain. Required when cloudflare_managed = true."
  type        = string
  default     = ""
}

variable "cloudflare_hostname" {
  description = "Public hostname routed through the tunnel, e.g. app.example.com. Required when cloudflare_managed = true."
  type        = string
  default     = ""
}

variable "cloudflare_origin_service" {
  description = "Internal ACA origin the tunnel proxies to. Empty defaults to the orchestrator (http://ca-paperclip-<env>); set http://ca-teams-bridge-<env> to expose the Teams bridge."
  type        = string
  default     = ""
}
