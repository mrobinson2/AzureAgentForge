variable "registry_name" {
  description = "Name of the Azure Container Registry (must be globally unique, 5-50 alphanumeric characters)"
  type        = string
}

variable "resource_group_name" {
  description = "Name of the resource group"
  type        = string
}

variable "location" {
  description = "Azure region for the registry"
  type        = string
}

variable "sku" {
  description = "SKU for the registry (Basic, Standard, or Premium)"
  type        = string
  default     = "Standard"
}

variable "admin_enabled" {
  description = "Enable admin user for the registry"
  type        = bool
  default     = true
}

variable "tags" {
  description = "Tags to apply to the registry"
  type        = map(string)
  default     = {}
}
