terraform {
  required_version = ">= 1.5.0"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.75"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "azurerm" {
  subscription_id = var.subscription_id
  features {
    key_vault {
      purge_soft_delete_on_destroy    = false
      recover_soft_deleted_key_vaults = true
    }
    resource_group {
      prevent_deletion_if_contains_resources = true
    }
  }
}

# Only used when cloudflare_managed = true. With an empty token and no Cloudflare
# resources instantiated (the module is count-gated off by default), the provider
# is configured but never authenticates. NOTE: the cloudflare provider v5
# validates the token FORMAT at plan time even with no resources, so an empty
# string fails. Substitute a format-valid placeholder when no real token is
# supplied so a tunnel-disabled `terraform plan` succeeds.
provider "cloudflare" {
  api_token = var.cloudflare_api_token != "" ? var.cloudflare_api_token : "0000000000000000000000000000000000000000"
}
