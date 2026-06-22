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

variable "vnet_address_space" {
  type    = list(string)
  default = ["10.0.0.0/16"]
}

variable "subnet_app_address_prefixes" {
  type    = list(string)
  default = ["10.0.1.0/24"]
}

variable "subnet_db_address_prefixes" {
  type    = list(string)
  default = ["10.0.2.0/24"]
}

variable "subnet_pe_address_prefixes" {
  type    = list(string)
  default = ["10.0.3.0/24"]
}

variable "subnet_admin_address_prefixes" {
  type    = list(string)
  default = ["10.0.4.0/24"]
}

variable "key_vault_private_access" {
  description = "Create the Key Vault private DNS zone and VNet link (hardened profile). Set false when Key Vault uses public network access."
  type        = bool
  default     = false
}

variable "existing_vnet_name" {
  description = "Deploy the platform's subnets into this pre-existing VNet (e.g. an ALZ-vended one) instead of creating a new VNet. Empty (default) creates a new VNet. The VNet must have free address space for the subnet_*_address_prefixes ranges."
  type        = string
  default     = ""
}

variable "existing_vnet_resource_group" {
  description = "Resource group of existing_vnet_name, if it differs from this deployment's resource group. Ignored when existing_vnet_name is empty."
  type        = string
  default     = ""
}
