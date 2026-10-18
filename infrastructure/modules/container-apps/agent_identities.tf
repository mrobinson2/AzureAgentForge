# Per-agent Azure identities — docs/design/enhancement-proposals-2026-10.md §1.
#
# Today every agent role runs under the single id-hermes-<env> identity, so
# Azure cannot tell the Infrastructure agent from the Psychology agent. This
# file materializes one user-assigned managed identity per role that opts in
# via `azure_identity` in agents/profiles/<role>.yaml, with EXACTLY the role
# assignments the profile declares (scripts/agent-identity-guard.py refuses
# broad or privileged roles at review time).
#
# Gated OFF by default (var.agent_identities_enabled). First slice: the
# identities and grants exist and are auditable; the runtime token broker that
# exchanges a signed agent assertion for a role-scoped token is the follow-up,
# so no agent process holds these identities yet.

locals {
  agent_profiles_dir = "${path.module}/../../../agents/profiles"
  agent_profiles = {
    for f in fileset(local.agent_profiles_dir, "*.yaml") :
    trimsuffix(f, ".yaml") => yamldecode(file("${local.agent_profiles_dir}/${f}"))
  }
  agent_identity_profiles = var.agent_identities_enabled ? {
    for slug, p in local.agent_profiles : slug => p if try(p.azure_identity.enabled, false)
  } : {}

  agent_rg_scope = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/resourceGroups/${var.resource_group_name}"

  agent_role_assignments = merge([
    for slug, p in local.agent_identity_profiles : {
      for idx, ra in p.azure_identity.role_assignments :
      "${slug}-${idx}" => {
        slug  = slug
        role  = ra.role
        scope = ra.scope == "key_vault" ? var.key_vault_id : local.agent_rg_scope
      }
    }
  ]...)
}

resource "azurerm_user_assigned_identity" "agent" {
  for_each            = local.agent_identity_profiles
  name                = "id-agent-${each.key}-${var.environment}"
  resource_group_name = var.resource_group_name
  location            = var.location
  tags                = merge(var.tags, { aaf_agent_role = each.key })
}

resource "azurerm_role_assignment" "agent" {
  for_each             = local.agent_role_assignments
  scope                = each.value.scope
  role_definition_name = each.value.role
  principal_id         = azurerm_user_assigned_identity.agent[each.value.slug].principal_id
}

output "agent_identity_client_ids" {
  description = "Per-agent managed identity client IDs by role slug (empty until agent_identities_enabled)."
  value       = { for k, v in azurerm_user_assigned_identity.agent : k => v.client_id }
}
