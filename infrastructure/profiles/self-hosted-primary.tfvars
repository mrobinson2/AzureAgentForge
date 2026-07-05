# Self-hosted-primary profile — the cloud side of a "run it on hardware you own,
# keep a warm cloud standby" topology. See the self-hosted-primary ADR in
# docs/design/ and ../../docs/cost.md ("Self-hosted-primary" section).
#
# In this topology an always-on machine you own runs the full compose stack
# (deploy/mac-site/ or deploy/windows-site/) as the PRIMARY, and this cloud
# deployment is a DORMANT warm standby that a lease + one-tap failover
# (scripts/aaf-site) brings up only when the primary is down. Steady-state infra
# is ~$35–45/mo because the compute runs on hardware you already own; the only
# always-on cloud cost is the shared managed PostgreSQL the two sites share.
#
# What this profile expresses (the knobs Terraform exposes):
#   - the smallest Postgres tier, no HA — it is the ONE shared, always-on server
#     both sites use; the database never moves, so a site switch is stateless.
#   - public network access on Key Vault (and, by the same posture, the shared
#     Postgres public endpoint) so the self-hosted host can reach them without a
#     VNet. Lock these down with key_vault_allowed_ip_ranges.
#   - Cloudflared enabled: the standby holds the second connector of the one
#     shared tunnel, so failover is a lease flip, not a DNS change.
#   - minimal logs — the standby is idle most of the time.
#
# What is ARCHITECTURAL, not a variable here (see the ADR):
#   - the standby's scale-to-zero / dormant posture (bring-up is operator-driven
#     via the failover runbook, not a Terraform toggle);
#   - running the stack on owned hardware (that lives in deploy/mac-site/).
#
# Restrict the public endpoints to your home/office egress IPs:
#   key_vault_allowed_ip_ranges = ["203.0.113.0/32"]

postgres_sku_name                       = "B_Standard_B1ms"
postgres_storage_mb                     = 32768
postgres_high_availability_enabled      = false
log_retention_in_days                   = 30
log_daily_quota_gb                      = 1
cloudflared_enabled                     = true
key_vault_public_network_access_enabled = true
key_vault_network_default_action        = "Deny"
telegram_enabled                        = false
discord_enabled                         = false
