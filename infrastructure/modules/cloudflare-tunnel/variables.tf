variable "account_id" {
  type        = string
  description = "Cloudflare account ID that owns the tunnel."
}

variable "zone_id" {
  type        = string
  description = "Cloudflare DNS zone ID for the hostname's domain."
}

variable "tunnel_name" {
  type        = string
  default     = "azureagentforge"
  description = "Name of the Cloudflare Tunnel (shown in Zero Trust → Networks → Tunnels)."
}

variable "hostname" {
  type        = string
  description = "Public hostname to route through the tunnel, e.g. app.example.com."
}

variable "origin_service" {
  type        = string
  default     = "http://ca-orchestrator"
  description = <<-EOT
    Internal origin URL the cloudflared connector proxies the hostname to. Use the
    ACA internal DNS name of the service to expose (e.g. http://ca-orchestrator for
    the orchestrator/auth-proxy, or http://ca-teams-bridge for the Teams bridge).
  EOT
}
