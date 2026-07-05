# Cost-optimized profile — targets <$150/mo infra (excludes LLM token usage).
# Confirm the dollar figure against your own Azure bill; see ../../docs/cost.md.
postgres_sku_name                       = "B_Standard_B1ms"
postgres_storage_mb                     = 32768
postgres_high_availability_enabled      = false
log_retention_in_days                   = 30
log_daily_quota_gb                      = 1
cloudflared_enabled                     = false
key_vault_public_network_access_enabled = true
telegram_enabled                        = false
discord_enabled                         = false
