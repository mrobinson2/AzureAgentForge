# Multi-tenant module — inputs.
#
# Phase 1 of docs/notes/plans/2026-07-22-full-multi-tenant.md: promote the
# experimental/multi-tenant/ reference (control-plane + memory-store) to a
# deployable, FLAG-GATED module. Everything is count-gated on
# multi_tenant_enabled; with it false (the default) the module creates ZERO
# resources, so the single-tenant stack is byte-for-byte unchanged.

variable "multi_tenant_enabled" {
  description = "Master switch. false (default) => the module creates no resources. true => deploys the control-plane + memory-store container apps. Provision the control-plane-operator-key secret and azure_search_endpoint before enabling."
  type        = bool
  default     = false
}

# ── Pass-through platform inputs (from the dev environment composition) ──────
variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "container_app_environment_id" {
  description = "The shared Container Apps Environment id these apps deploy into."
  type        = string
}

variable "container_registry_id" {
  description = "Container Registry id for the AcrPull role assignment."
  type        = string
}

variable "container_registry_login_server" {
  description = "Container Registry login server (e.g. myregistry.azurecr.io)."
  type        = string
}

variable "key_vault_id" {
  description = "Key Vault id for the Secrets User role assignment."
  type        = string
}

variable "key_vault_uri" {
  description = "Key Vault URI (https://<name>.vault.azure.net/) for secret references."
  type        = string
}

variable "app_insights_connection_string" {
  description = "Application Insights connection string for telemetry."
  type        = string
  default     = ""
  sensitive   = true
}

# ── Image tags ──────────────────────────────────────────────────────────────
variable "control_plane_image_tag" {
  description = "control-plane container image tag."
  type        = string
  default     = "latest"
}

variable "memory_store_image_tag" {
  description = "memory-store container image tag."
  type        = string
  default     = "latest"
}

# ── Service config ──────────────────────────────────────────────────────────
variable "azure_search_endpoint" {
  description = "Azure AI Search endpoint the control-plane provisions per-tenant indexes against. REQUIRED when multi_tenant_enabled = true (the control-plane reads AZURE_SEARCH_ENDPOINT at startup)."
  type        = string
  default     = ""
}
