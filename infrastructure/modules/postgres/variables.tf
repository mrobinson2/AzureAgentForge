variable "resource_group_name" {
  type = string
}

variable "location" {
  type = string
}

variable "prefix" {
  type = string
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "environment" {
  type    = string
  default = "dev"
}

variable "sku_name" {
  description = "PostgreSQL SKU (e.g., B_Standard_B1ms, GP_Standard_D2s_v3)"
  type        = string
  default     = "B_Standard_B1ms"
}

variable "storage_mb" {
  description = "Storage in MB"
  type        = number
  default     = 32768
}

variable "administrator_login" {
  type = string
}

variable "administrator_password" {
  type      = string
  sensitive = true
}

variable "delegated_subnet_id" {
  description = "Subnet ID for private access"
  type        = string
}

variable "private_dns_zone_id" {
  description = "Private DNS Zone ID for PostgreSQL"
  type        = string
}

variable "databases" {
  description = "Map of database names to configurations"
  type = map(object({
    charset   = string
    collation = string
  }))
  default = {}
}

variable "tenant_id" {
  description = "Azure AD Tenant ID for PostgreSQL authentication"
  type        = string
}

variable "high_availability_enabled" {
  type        = bool
  description = "Enable zone-redundant high availability (hardened profile; roughly doubles compute cost)."
  default     = false
}
