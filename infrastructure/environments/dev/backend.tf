terraform {
  # Remote state in Azure Storage. All values are supplied at init time via
  # -backend-config (see .github/workflows/deploy.yml; provision the backend
  # with scripts/scaffold-cicd.sh). For a local-only first deploy, drop in a
  # backend_override.tf containing `backend "local" {}` (Terraform loads
  # *_override.tf last), then migrate with `terraform init -migrate-state`.
  backend "azurerm" {}
}
