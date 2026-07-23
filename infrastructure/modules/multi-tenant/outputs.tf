# Multi-tenant module — outputs. Null-safe when the module is disabled (the
# count-gated resources produce empty lists, so index-0 is guarded with try()).

output "enabled" {
  description = "Whether the module deployed anything."
  value       = var.multi_tenant_enabled
}

output "control_plane_fqdn" {
  description = "Internal FQDN of the control-plane app, or empty when disabled."
  value       = try(azurerm_container_app.control_plane[0].ingress[0].fqdn, "")
}

output "memory_store_fqdn" {
  description = "Internal FQDN of the memory-store app, or empty when disabled."
  value       = try(azurerm_container_app.memory_store[0].ingress[0].fqdn, "")
}
