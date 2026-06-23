# cloudflare-tunnel

Manages the Cloudflare side of public ingress so the `cloudflared` container app
(`infrastructure/modules/container-apps/cloudflared.tf`) doesn't depend on
hand-clicking the Cloudflare dashboard. It creates:

1. the **Tunnel** (`cloudflare_zero_trust_tunnel_cloudflared`),
2. its **ingress config** (`..._config`) — the public hostname → an internal ACA
   origin, with a catch-all 404,
3. the proxied **DNS** `CNAME` (`cloudflare_dns_record`) → `<tunnel>.cfargotunnel.com`,

and outputs the **connector token** so the manual `cf-tunnel-token` Key Vault seed
goes away.

> Traffic: Internet → Cloudflare edge → Tunnel → cloudflared (ACA) → `origin_service`.

## Inputs

| Variable | Required | Default | Description |
|---|---|---|---|
| `account_id` | yes | — | Cloudflare account ID that owns the tunnel. |
| `zone_id` | yes | — | DNS zone ID for the hostname's domain. |
| `hostname` | yes | — | Public hostname, e.g. `app.example.com`. |
| `tunnel_name` | no | `azureagentforge` | Tunnel name (Zero Trust → Networks → Tunnels). |
| `origin_service` | no | `http://ca-orchestrator` | Internal ACA origin the connector proxies to (e.g. `http://ca-teams-bridge` for Teams). |

## Outputs

`tunnel_id`, `tunnel_cname`, `hostname`, and `tunnel_token` (**sensitive** — the
connector token).

## Wiring it in

This module is standalone so the new provider dependency is opt-in. To use it:

1. **Add the provider** to your root environment (`infrastructure/environments/dev/providers.tf`):

   ```hcl
   cloudflare = {
     source  = "cloudflare/cloudflare"
     version = "~> 5.0"
   }
   ```

   ```hcl
   provider "cloudflare" {
     api_token = var.cloudflare_api_token # scopes: Account → Cloudflare Tunnel: Edit, Zone → DNS: Edit
   }
   ```

2. **Call the module:**

   ```hcl
   module "cloudflare_tunnel" {
     source         = "../../modules/cloudflare-tunnel"
     account_id     = var.cloudflare_account_id
     zone_id        = var.cloudflare_zone_id
     hostname       = var.public_hostname        # e.g. app.example.com
     origin_service = "http://ca-orchestrator"   # or http://ca-teams-bridge, etc.
   }
   ```

3. **Feed the token to cloudflared.** The connector reads `cf-tunnel-token` from
   Key Vault today. Either push the module's `tunnel_token` output into that secret
   (e.g. an `azurerm_key_vault_secret` with `value = module.cloudflare_tunnel.tunnel_token`)
   or pass it to the container's `TUNNEL_TOKEN` directly. Once managed here, the
   manual dashboard token copy is no longer needed.

Gate the whole thing behind your existing `cloudflared_enabled` (or a new
`cloudflare_managed`) flag so deployments that don't use Cloudflare are unaffected.

## Validation

`terraform validate` passes against `cloudflare/cloudflare ~> 5.0` (verified on
5.21.0). It is **schema-validated only** — a live `plan`/`apply` needs a real API
token, account, and zone, which is the operator's wiring step above.

## Provider note

Pinned to the Cloudflare provider **v5** (resource names differ from v4 —
`cloudflare_dns_record`, `cloudflare_zero_trust_tunnel_cloudflared*`).
