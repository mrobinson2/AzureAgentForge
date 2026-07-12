<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/azureagentforge-logo-dark.png">
    <img alt="AzureAgentForge" src="assets/azureagentforge-logo-light.png" width="440">
  </picture>
</p>

# Security

This document covers how secrets are handled, what the network looks like, what to do before running this in production, and the security tradeoff between the two deployment profiles.

Nothing here is a guarantee. It describes the controls the project provides. You are responsible for your deployment.

For the story of how these controls stop a specific dangerous request end to end, see the [governance and blast-radius walkthrough](walkthroughs/governance-and-blast-radius.md). Unfamiliar term? Check the [glossary](GLOSSARY.md).

## Secrets

All secrets go into Azure Key Vault. Container Apps mount them as environment variables at runtime via Key Vault references. Nothing sensitive belongs in source control.

The `.gitignore` excludes `.env`, `.env.*`, `*.tfvars`, `*.pem`, `*.key`, and the `secrets/` directory. The exception is `.env.example` and `infrastructure/profiles/*.tfvars` (the named profiles), which contain only placeholder values.

`.env.example` lists every variable the platform needs, with Key Vault secret names annotated inline. For example:

```
# Key Vault secret: gpt4o-api-key   (the endpoint is the ai_foundry_endpoint
#                                    Terraform variable, not a Key Vault secret)
AZURE_FOUNDRY_API_KEY=<your-foundry-key>
```

The Terraform modules reference Key Vault secret IDs at deploy time; the actual values are never written to Terraform state in plaintext.

`.gitleaks.toml` is present and scans for leaked secrets in CI. It runs on push. If your CI pipeline doesn't call it, wire it in before merging anything to your main branch.

## Network posture

All resources share one VNet. PostgreSQL is deployed with VNet injection in both profiles; it has no public endpoint. It is only reachable from within the VNet.

Ingress into Container Apps works one of two ways depending on the profile:

- **Cost-optimized** (`cloudflared_enabled = false`): uses Azure Container Apps managed ingress. Traffic enters through Azure's load balancer. You control which apps expose an external endpoint.
- **Hardened** (`cloudflared_enabled = true`): runs a Cloudflared tunnel sidecar. No public inbound port; all traffic goes through the Cloudflare Tunnel. Useful if you want to avoid exposing any public IP.

Key Vault network access differs between profiles:

| Profile | Key Vault access | What that means |
|---|---|---|
| `cost-optimized` | Public endpoint + firewall rules (`key_vault_public_network_access_enabled = true`) | Accessible over the internet with Azure firewall IP allowlisting. Simpler to set up; higher exposure surface. |
| `hardened` | Private endpoint only (`key_vault_public_network_access_enabled = false`) | No public route to Key Vault. Requires the private DNS zone and endpoint to be provisioned. |

For a development or personal deployment, the public-endpoint-with-firewall approach is workable if you restrict it to known IPs. For anything handling real user data or secrets with production value, use the hardened profile or add the private endpoint manually.

## Deploy-time safety: the destroy gate

Creating and updating resources is routine. Deleting or replacing them is not.
A replace can mean data loss (a recreated database, a regenerated key), so the
apply path treats destructive plans differently from additive ones.

The Forge Console parses the saved plan with `terraform show -json` and checks
each `resource_changes[].change.actions` for `"delete"`, which catches pure
deletes (`["delete"]`) and both replace orderings (`["delete","create"]`,
`["create","delete"]`). If none are present, apply proceeds with the normal
environment-name confirmation. If any are present, apply is blocked behind a
second, explicit approval that lists exactly which resources would be destroyed
or replaced and requires typing a distinct `approve-destroy` token. The check
runs server-side against the saved plan, and apply only ever runs that saved
plan, so what you approved is what executes.

If you deploy from your own pipeline, reproduce the gate: `terraform plan -out
tfplan`, then fail the run or require manual approval when the plan contains a
delete, and apply the saved `tfplan`. See
[getting-started.md](getting-started.md) for the exact `jq` filter.

## Before production

Work through this before you take this outside a personal dev environment:

- **Rotate every secret.** The `.env.example` values are placeholders. Replace them with real secrets you generate yourself. Any secrets used during initial setup should be rotated before production traffic hits the system.
- **Replace all tokens.** Bot tokens (Telegram, Discord), API keys, and database passwords should be fresh. Assume anything that touched a dev environment is compromised.
- **Restrict Key Vault network access.** Either use the hardened profile or tighten the firewall rules on the cost-optimized profile to your actual IP ranges. "Public + firewall" is only as good as your allowlist.
- **Tighten Postgres access.** PostgreSQL is VNet-injected in both profiles, so external access is blocked by default. Double-check that no firewall rule opens it to `0.0.0.0/0`.
- **Enable GitHub secret scanning and push protection** on your fork. GitHub will block pushes that contain recognized secret patterns. This is free for public repos and available on private repos with the right plan. Turn it on.
- **Review agent toolset grants.** The platform agents have access to tools and APIs scoped in their definitions. Review what each agent can call and trim anything that isn't needed for your use case.

## Profile security tradeoff

See [cost.md](cost.md) for the full cost breakdown. The security summary:

| Profile | Postgres | Key Vault | Ingress | Logs |
|---|---|---|---|---|
| `cost-optimized` | VNet-injected, no HA | Public + firewall | Managed ingress | 30 days, 1 GB/day cap |
| `hardened` | VNet-injected + zone-redundant HA | Private endpoint | Cloudflared tunnel | 90 days, no cap |

The hardened profile is production-appropriate. The cost-optimized profile is fine for development and low-stakes deployments where you've tightened the firewall rules and rotated secrets.
