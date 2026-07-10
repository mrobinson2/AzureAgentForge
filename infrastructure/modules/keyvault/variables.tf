variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "prefix" { type = string }

variable "tenant_id" {
  description = "Azure AD tenant ID"
  type        = string
}

variable "public_network_access_enabled" {
  description = "Allow public network access to the Key Vault. Keep true only while callers (e.g. the self-hosted-primary site or the Terraform runner) reach the vault over the public endpoint; combine with network_default_action=\"Deny\" + an allowlist so it is not open to the whole internet (aaf-0013). Set false for the hardened (private-endpoint) profile."
  type        = bool
  default     = true
}

variable "network_default_action" {
  description = "Default Key Vault firewall action. aaf-0013: defaults to \"Deny\" so the vault is not reachable from any network by default — allow specific callers via allowed_ip_ranges / allowed_subnet_ids, plus the AzureServices bypass and any private endpoint. Private-endpoint access is unaffected by Deny."
  type        = string
  default     = "Deny"

  validation {
    condition     = contains(["Allow", "Deny"], var.network_default_action)
    error_message = "network_default_action must be \"Allow\" or \"Deny\"."
  }
}

variable "allowed_ip_ranges" {
  description = "Public IP ranges (CIDRs) allowed through the Key Vault firewall when default_action is Deny. aaf-0013: MUST include the Terraform runner's egress IP (the module reads postgres-admin-password during apply) and any self-hosted-site egress IP that pulls secrets over the public endpoint — otherwise apply/secret-pull fails."
  type        = list(string)
  default     = []
}

variable "allowed_subnet_ids" {
  description = "VNet subnet IDs (e.g. the app subnet) allowed through the Key Vault firewall when default_action is Deny (aaf-0013). Private-endpoint access is unaffected by this list. Operator-gated: pass the app subnet id from the network module."
  type        = list(string)
  default     = []
}

variable "admin_object_ids" {
  description = "Object IDs granted Key Vault Secrets Officer role"
  type        = list(string)
  default     = []
}


variable "tags" {
  type    = map(string)
  default = {}
}
