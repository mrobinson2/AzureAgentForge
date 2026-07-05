# Outputs for Dev Environment

output "honcho_fqdn" {
  description = "Internal FQDN of the Honcho Container App"
  value       = module.container_apps.honcho_fqdn
}
