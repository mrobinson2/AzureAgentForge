variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "prefix" { type = string }

variable "tenant_id" {
  description = "Azure AD tenant ID"
  type        = string
}

variable "public_network_access_enabled" {
  description = "Allow public network access to the Key Vault. Set false for the hardened (private-endpoint) profile."
  type        = bool
  default     = true
}

variable "network_default_action" {
  description = "Default Key Vault firewall action"
  type        = string
  default     = "Allow"
}

variable "allowed_ip_ranges" {
  description = "IP ranges to allow through the Key Vault firewall"
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
