# Cost-optimized profile — targets <$150/mo infra (excludes LLM token usage).
# Confirm the dollar figure against your own Azure bill; see ../../docs/cost.md.
postgres_sku_name                       = "B_Standard_B1ms"
postgres_storage_mb                     = 32768
postgres_high_availability_enabled      = false
log_retention_in_days                   = 30
log_daily_quota_gb                      = 1
cloudflared_enabled                     = false
key_vault_public_network_access_enabled = true
# aaf-0013: the Key Vault firewall now defaults to Deny (secure-by-default). With
# public access on and no private endpoint, add your Terraform-runner egress IP
# so apply/secret-pull is not locked out, e.g.:
#   key_vault_allowed_ip_ranges = ["203.0.113.0/32"]   # example (RFC-5737)
telegram_enabled = false
discord_enabled  = false
