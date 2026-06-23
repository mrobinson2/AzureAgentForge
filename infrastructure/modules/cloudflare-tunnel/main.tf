# Cloudflare Tunnel + ingress + DNS for AzureAgentForge.
#
# Replaces the manual Cloudflare-dashboard steps that the cloudflared container
# app (infrastructure/modules/container-apps/cloudflared.tf) otherwise depends on:
# create the tunnel, copy the connector token, add a public hostname (ingress),
# and create the DNS route. This module manages all of that and outputs the
# connector token, so the only remaining manual step is providing a Cloudflare
# API token to the provider.
#
# Traffic flow it sets up:
#   Internet → Cloudflare edge → Tunnel → cloudflared (ACA) → var.origin_service

# The tunnel. config_src = "cloudflare" means ingress is managed remotely (via the
# _config resource below) rather than by a local connector config file.
resource "cloudflare_zero_trust_tunnel_cloudflared" "this" {
  account_id = var.account_id
  name       = var.tunnel_name
  config_src = "cloudflare"
}

# Ingress rules: the public hostname → the internal origin, with a catch-all 404.
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "this" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id

  config = {
    ingress = [
      {
        hostname = var.hostname
        service  = var.origin_service
      },
      {
        service = "http_status:404"
      },
    ]
  }
}

# The connector token for this tunnel (used by the cloudflared container).
data "cloudflare_zero_trust_tunnel_cloudflared_token" "this" {
  account_id = var.account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.this.id
}

# Proxied CNAME pointing the hostname at the tunnel (<tunnel-id>.cfargotunnel.com).
resource "cloudflare_dns_record" "this" {
  zone_id = var.zone_id
  name    = var.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1 # 1 = automatic, required when proxied
}
