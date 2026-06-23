output "tunnel_id" {
  value       = cloudflare_zero_trust_tunnel_cloudflared.this.id
  description = "The Cloudflare Tunnel ID."
}

output "tunnel_cname" {
  value       = "${cloudflare_zero_trust_tunnel_cloudflared.this.id}.cfargotunnel.com"
  description = "The tunnel's CNAME target (what the DNS record points at)."
}

output "tunnel_token" {
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.this.token
  sensitive   = true
  description = <<-EOT
    The connector token for the cloudflared container app. Wire it to the
    `cf-tunnel-token` Key Vault secret (or the container's TUNNEL_TOKEN env) so the
    connector no longer needs a hand-copied dashboard token.
  EOT
}

output "hostname" {
  value       = var.hostname
  description = "The public hostname routed through the tunnel."
}
