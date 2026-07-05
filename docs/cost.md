<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Cost

> ✅ **Validated against a real deployment.** The figures below are grounded in Azure Cost Management data from a live deployment's resource group (`ActualCost`, May 2026, Central US), mapped to this repo's `cost-optimized` profile. They exclude components the live resource group carries but this repo does not provision (a CI build-agent VM, automation workflows, and a model subscription), plus Cloudflared, which this profile leaves off. Your bill will still vary with region, activity level, and especially Azure Files transaction volume. **LLM token usage is billed separately and is NOT included here.**

Cost depends on your region, your activity level, and which profile you deploy. The table below is the `cost-optimized` default (`infrastructure/profiles/cost-optimized.tfvars`), showing what each line actually cost, per resource, in a live deployment for May 2026. Your own figures will vary, especially Container Apps (idle vs. busy workers) and Azure Files (billed per transaction).

## Cost-optimized profile

| Service | Config | Observed (May 2026) |
|---|---|---|
| Container Apps (Consumption) | PaperClip + Hermes + Honcho + deriver job; two responsive, the rest scale-to-zero | $39 |
| PostgreSQL Flexible Server | Burstable B1ms, 32 GB, no HA | $18 |
| Azure Files (Standard LRS) | SMB share mounted for Hermes/PaperClip state; **billed per transaction** | $17 |
| Container Registry | Basic | $9 |
| Networking | private DNS zone + egress | <$1 |
| Key Vault | Standard, light op volume | <$1 |
| Log Analytics | 30-day retention, 1 GB/day cap | $0 |
| **Infra total (excl. LLM tokens)** | | **~$83** |

That **~$83/month** is comfortably under the $150 target, with the always-on PaperClip container and the Azure Files share the two largest lines. Both scale with how hard you run the platform: Container Apps with worker activity, Azure Files with SMB transaction volume, so a chattier fleet costs more than a quiet one.

### How these were validated

The figures come from Azure Cost Management (`ActualCost`, May 2026), broken out per resource and filtered to exactly what this profile provisions. The four Container Apps in scope (PaperClip $25, Hermes $12, Honcho, and the deriver job) totalled about **$39**, with the always-on PaperClip container the largest single line. Postgres (B1ms / 32 GB / no-HA) was **$18**, the Container Registry (Basic) **$9**, the Azure Files share **$17**, Key Vault **$0.16**, the private DNS zone **$0.51**, and the Log Analytics workspace **$0** (dev-level ingestion stayed within the free allotment). Azure Files was almost entirely SMB transactions rather than the 5 GiB of stored data, which is why it tracks agent activity, not disk usage. The live resource group also ran a Cloudflared sidecar, a chat-bridge container, and a CI build-agent VM (roughly $6, $6, and $10 together with its disk and IP), none of which this profile includes, so they are excluded above.

## What drives cost

**Container Apps** bills on vCPU-seconds and memory-seconds consumed. Services that aren't handling requests should scale to zero. The cost-optimized profile relies on Container Apps' consumption plan, which means you pay nothing for a worker that isn't running, but an always-on replica costs something every hour.

**Azure Files** is priced per transaction, not just per gigabyte stored. The mounted share is small (5 GiB), but every SQLite write, session-file read, and config check is a billable SMB operation. A busy agent fleet drives this line; a mostly-idle one barely registers. It is the easiest line to underestimate because it scales with behavior, not size.

**Log Analytics** is a quiet cost trap. The profile sets `log_daily_quota_gb = 1` as a hard cap, and `log_retention_in_days = 30`. Both levers matter: ingestion is billed per GB, and retention extends storage costs. In the validated month, dev-level ingestion stayed inside the free allotment and cost nothing; the $0–15 range is a ceiling for a verbose workload, not a floor. Remove the cap and a chatty service can climb fast.

**Container Registry** Basic tier covers most dev and small-scale production use; it ran ~$9 with image storage. Upgrade only if you need geo-replication or higher throughput.

**Key Vault** costs are mostly negligible at typical operation volumes, well under $1 in practice. The estimate assumes light usage (hundreds of secret reads per day, not millions).

## How to cut it further

- **Scale-to-zero non-interactive services.** Any worker that runs on-demand rather than continuously should have `min_replicas = 0` in its Container App definition.
- **Right-size CPU and memory.** Start with 0.25 vCPU / 0.5 GB per replica, measure, and only increase what's actually saturated.
- **Watch Azure Files traffic.** The share is transaction-billed. A service reading or writing it in a hot loop shows up on the bill, so batch or cache where you can.
- **Cap Log Analytics** retention and daily quota. The profile already does this; don't undo it without a cost review.
- **Leave `cloudflared_enabled = false`.** The cost-optimized profile skips the Cloudflared sidecar and uses Azure Container Apps managed ingress instead. Adding Cloudflared adds a permanently running container; avoid it unless the tunnel is specifically required.

## Hardened profile

The hardened profile (`infrastructure/profiles/hardened.tfvars`) costs roughly $250+/mo. The main differences:

- PostgreSQL upgrades to B2s with zone-redundant HA (`postgres_high_availability_enabled = true`), which roughly doubles the database compute cost.
- Log retention extends to 90 days with no daily quota cap (`log_daily_quota_gb = -1`). Monitor ingestion volume actively or set an alert.
- Key Vault uses a private endpoint (`key_vault_public_network_access_enabled = false`), adding a small but fixed private DNS + endpoint charge.
- Cloudflared is enabled, adding a permanently running container.

If you're deploying to production and need the security posture, the cost difference is worth it. If you're evaluating the project or running a dev environment, start with cost-optimized.

## Measure your real cost

Use **Azure Cost Management** (portal > Cost Management + Billing > Cost analysis) to see actual spend by resource. Tag your resource group (the Terraform modules use a consistent `project` tag) so you can filter to just this deployment. The same data is available from the CLI. Group by service name over a billing period to reproduce a table like the one above:

```bash
az rest --method post \
  --url "https://management.azure.com/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.CostManagement/query?api-version=2023-11-01" \
  --body '{"type":"ActualCost","timeframe":"Custom","timePeriod":{"from":"2026-05-01T00:00:00Z","to":"2026-05-31T23:59:59Z"},"dataset":{"granularity":"None","aggregation":{"totalCost":{"name":"PreTaxCost","function":"Sum"}},"grouping":[{"type":"Dimension","name":"ServiceName"}]}}'
```

Set a cost alert on the resource group at 80% of your monthly budget. You want to know before you exceed it, not after.
