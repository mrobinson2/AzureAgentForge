<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="../../docs/assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="../../docs/assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Infrastructure Cost Profiles

Two `.tfvars` files are provided so you can apply a consistent cost/security
posture without editing individual variable files each time.

## Profiles

### `cost-optimized.tfvars`
Targets **< $150/mo** in Azure infrastructure spend (LLM token usage billed
separately and excluded from that figure). Confirm against your own Azure bill;
see [`../../docs/cost.md`](../../docs/cost.md) for a breakdown.

| Variable | Value | Trade-off |
|---|---|---|
| `postgres_sku_name` | `B_Standard_B1ms` | Smallest burstable tier; suitable for dev/low-traffic workloads |
| `postgres_storage_mb` | `32768` (32 GB) | Minimum storage; increase if your dataset grows |
| `postgres_high_availability_enabled` | `false` | No zone-redundant standby; expected downtime during failover |
| `log_retention_in_days` | `30` | 30-day log history; cheaper than 90-day |
| `log_daily_quota_gb` | `1` | Hard cap on Log Analytics ingestion to avoid runaway costs |
| `cloudflared_enabled` | `false` | Uses Azure Container Apps managed ingress (no extra container) |
| `key_vault_public_network_access_enabled` | `true` | Public endpoint + firewall rules; no private endpoint required |

### `hardened.tfvars`
Targets **~ $250+/mo** in Azure infrastructure spend. Suitable for
production or compliance-sensitive deployments.

| Variable | Value | Trade-off |
|---|---|---|
| `postgres_sku_name` | `B_Standard_B2s` | Larger burstable tier; more CPU/memory headroom |
| `postgres_storage_mb` | `65536` (64 GB) | More storage headroom |
| `postgres_high_availability_enabled` | `true` | Zone-redundant standby; roughly doubles PostgreSQL compute cost |
| `log_retention_in_days` | `90` | 90-day log retention for audit and compliance |
| `log_daily_quota_gb` | `-1` | Unlimited ingestion (monitor costs separately) |
| `cloudflared_enabled` | `true` | Cloudflare Tunnel container for ingress; no public inbound port |
| `key_vault_public_network_access_enabled` | `false` | Requires a Key Vault private endpoint; primary network cost lever |

> **Note:** PostgreSQL is VNet-injected in both profiles. The Key Vault private
> endpoint (`key_vault_public_network_access_enabled = false`) is the main
> additional network cost in the hardened profile.

## Applying a Profile

Profiles are layered on top of your environment-specific `terraform.tfvars`.
Variables such as `subscription_id`, `location`, and `environment` must be
supplied separately — either in `terraform.tfvars` or via `-var` flags.

```bash
# From infrastructure/environments/dev
terraform apply \
  -var-file=../../profiles/cost-optimized.tfvars \
  -var-file=terraform.tfvars
```

```bash
# Hardened profile
terraform apply \
  -var-file=../../profiles/hardened.tfvars \
  -var-file=terraform.tfvars
```

Later `-var-file` entries win on conflicts, so place `terraform.tfvars` last
to let environment-specific overrides take precedence over profile defaults.
