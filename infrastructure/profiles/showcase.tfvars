# Showcase profile — the full differentiator turned on. Extends the
# cost-optimized footprint by deploying the memory-governor (AAF's governed-
# memory layer) as an always-on service plus its sweeper/digest/watchdog jobs.
#
# COST: adds an always-on Container App (the governor + its router sidecar) and
# embedding/LLM token spend on top of the <$150/mo cost-optimized target — so
# this profile is NOT the <$150 profile. Size it against your own Azure bill;
# see ../../docs/cost.md.
#
# NOT a flag-flip on its own. After deploying with this profile you must also:
#   1. Provision a `text-embedding-3-small`-class embedding deployment and put a
#      real key in the `openai-api-key` Key Vault secret (else Plane C silently
#      falls back to trigram — watch the governor's /healthz `embedding` block
#      and the trigram-fallback watchdog finding).
#   2. Flip the in-DB feature flags per environment (all seed OFF): start with
#      AGENT_EVENTS_ENABLED + MEMORY_CLASSES_ENABLED, then MEMORY_PLANNER_ENABLED,
#      then MEMORY_VECTOR_RETRIEVAL_ENABLED once embeddings are live.
#   3. Canary the planner via MEMORY_PLANNER_AGENT_ALLOWLIST (empty = nobody).

# Baseline: inherit the cost-optimized infra sizing.
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

# The differentiator: deploy the governed-memory service. Every in-app behavior
# is still feature-flag-gated (migrations seed all flags OFF), so the service
# runs inert until an operator flips flags per the checklist above.
memory_governor_enabled = true

# Observability on too, so you can watch the governor operate (GenAI spans +
# the /healthz embedding block + watchdog findings).
observability_enabled = true
