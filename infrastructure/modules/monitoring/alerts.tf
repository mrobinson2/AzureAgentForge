# Observability: alert rules + workbook over the Log Analytics workspace.
#
# Everything here is additive and count-gated so the default deploy footprint
# is unchanged:
#   - Alert rules + action group exist only when `alert_emails` is non-empty.
#   - The workbook exists only when `enable_observability_workbook = true`.
# A clean fork that sets neither gets exactly the v1.0 monitoring module
# (workspace + Application Insights) with no new resources.
#
# Signals come from Container Apps console logs (the managed environment ships
# stdout/stderr to this workspace as `ContainerAppConsoleLogs_CL`). The
# watchdog emits stable, greppable markers:
#   stdout: "[watchdog] filed [<severity>] <title>"  (one per filed issue)
#   stderr: "[watchdog] <step> failed: ..."          (fetch/token/flag errors)
# and the model-router logs "call_failed"/"primary_failed"/"fallback_failed".
# The queries below match those markers directly.

locals {
  alerts_enabled = length(var.alert_emails) > 0

  # Optional scoping to the watchdog's container app/job. Empty → match across
  # all apps in the workspace. Rendered as a KQL line that is a no-op when blank.
  watchdog_app_filter = (
    var.watchdog_app_name != ""
    ? "| where ContainerAppName_s == \"${var.watchdog_app_name}\""
    : ""
  )
}

# ─── Action group (notification target) ──────────────────────────────────────
resource "azurerm_monitor_action_group" "alerts" {
  count               = local.alerts_enabled ? 1 : 0
  name                = "ag-${var.project}-${var.environment}"
  resource_group_name = var.resource_group_name
  # short_name is capped at 12 chars by Azure.
  short_name = substr("aaf${var.environment}", 0, 12)
  tags       = var.tags

  dynamic "email_receiver" {
    for_each = var.alert_emails
    content {
      name                    = "email-${email_receiver.key}"
      email_address           = email_receiver.value
      use_common_alert_schema = true
    }
  }
}

# ─── Alert: watchdog filed a CRITICAL issue ──────────────────────────────────
# A critical finding (adapter total failure, expired secret, budget blowout)
# means an agent path is already broken. Page on the first occurrence.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "watchdog_critical" {
  count                   = local.alerts_enabled ? 1 : 0
  name                    = "alert-${var.project}-${var.environment}-watchdog-critical"
  resource_group_name     = var.resource_group_name
  location                = var.location
  description             = "The platform watchdog filed a CRITICAL issue — an agent path is broken (adapter failure, expired secret, or budget blowout)."
  display_name            = "Watchdog: critical issue filed (${var.environment})"
  severity                = 1
  enabled                 = true
  evaluation_frequency    = var.alert_evaluation_frequency
  window_duration         = var.alert_window_duration
  scopes                  = [azurerm_log_analytics_workspace.main.id]
  auto_mitigation_enabled = false

  criteria {
    query                   = <<-KQL
      ContainerAppConsoleLogs_CL
      ${local.watchdog_app_filter}
      | where Log_s has "[watchdog] filed [critical]"
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.alerts[0].id]
  }

  tags = var.tags
}

# ─── Alert: Key Vault secret expiry filed ────────────────────────────────────
# The secret-expiry detector files an issue before a credential lapses. Catch
# both the "expires in N days" (high) and "has expired" (critical) variants so
# the warning lands while there's still time to rotate.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "secret_expiry" {
  count                   = local.alerts_enabled ? 1 : 0
  name                    = "alert-${var.project}-${var.environment}-secret-expiry"
  resource_group_name     = var.resource_group_name
  location                = var.location
  description             = "The watchdog flagged a Key Vault secret/cert at or near expiry. Rotate it before the agents that depend on it stall."
  display_name            = "Watchdog: secret expiry (${var.environment})"
  severity                = 2
  enabled                 = true
  evaluation_frequency    = var.alert_evaluation_frequency
  window_duration         = var.alert_window_duration
  scopes                  = [azurerm_log_analytics_workspace.main.id]
  auto_mitigation_enabled = false

  criteria {
    query                   = <<-KQL
      ContainerAppConsoleLogs_CL
      ${local.watchdog_app_filter}
      | where Log_s has "[watchdog] filed" and Log_s has "Key Vault secret"
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.alerts[0].id]
  }

  tags = var.tags
}

# ─── Alert: watchdog run failure ─────────────────────────────────────────────
# The watchdog itself failing (MSI token, run/event fetch, flag check, Key
# Vault list) means the platform is flying blind — no findings get filed at
# all. Surface its own stderr failures so a dead watchdog doesn't pass silently.
resource "azurerm_monitor_scheduled_query_rules_alert_v2" "watchdog_run_failure" {
  count                   = local.alerts_enabled ? 1 : 0
  name                    = "alert-${var.project}-${var.environment}-watchdog-failure"
  resource_group_name     = var.resource_group_name
  location                = var.location
  description             = "The watchdog logged an internal failure (token, fetch, flag check, or Key Vault list). It may not be filing findings — the platform is unmonitored until this clears."
  display_name            = "Watchdog: run failure (${var.environment})"
  severity                = 2
  enabled                 = true
  evaluation_frequency    = var.alert_evaluation_frequency
  window_duration         = var.alert_window_duration
  scopes                  = [azurerm_log_analytics_workspace.main.id]
  auto_mitigation_enabled = false

  criteria {
    query                   = <<-KQL
      ContainerAppConsoleLogs_CL
      ${local.watchdog_app_filter}
      | where Log_s startswith "[watchdog]" and Log_s has_any ("failed", "refusing to run")
    KQL
    time_aggregation_method = "Count"
    threshold               = 0
    operator                = "GreaterThan"

    failing_periods {
      minimum_failing_periods_to_trigger_alert = 1
      number_of_evaluation_periods             = 1
    }
  }

  action {
    action_groups = [azurerm_monitor_action_group.alerts[0].id]
  }

  tags = var.tags
}

# ─── Observability workbook ──────────────────────────────────────────────────
# A single pane over watchdog activity, secret expiry, and gateway health.
# Opt-in (off by default) since it's a convenience surface, not a guardrail.
locals {
  # Deterministic GUID for the workbook name (uuid() would churn on every plan).
  workbook_uuid = uuidv5("url", "aaf-observability-${var.project}-${var.environment}")

  workbook_json = jsonencode({
    version = "Notebook/1.0"
    items = [
      {
        type = 1
        content = {
          json = "# AAF Platform Observability — ${upper(var.environment)}\n\nWatchdog findings, Key Vault secret expiry, and model-router gateway health. All tiles query Container Apps console logs (`ContainerAppConsoleLogs_CL`)."
        }
      },
      {
        type = 3
        content = {
          version      = "KqlItem/1.0"
          query        = "ContainerAppConsoleLogs_CL\n| where Log_s startswith '[watchdog] filed'\n| summarize Issues = count() by bin(TimeGenerated, 1h)\n| render timechart"
          size         = 0
          title        = "Watchdog issues filed (per hour)"
          timeContext  = { durationMs = 604800000 }
          queryType    = 0
          resourceType = "microsoft.operationalinsights/workspaces"
        }
      },
      {
        type = 3
        content = {
          version      = "KqlItem/1.0"
          query        = "ContainerAppConsoleLogs_CL\n| where Log_s has '[watchdog] filed'\n| parse Log_s with * '[watchdog] filed [' Severity ']' *\n| summarize Count = count() by Severity\n| render piechart"
          size         = 0
          title        = "Watchdog findings by severity (7d)"
          timeContext  = { durationMs = 604800000 }
          queryType    = 0
          resourceType = "microsoft.operationalinsights/workspaces"
        }
      },
      {
        type = 3
        content = {
          version      = "KqlItem/1.0"
          query        = "ContainerAppConsoleLogs_CL\n| where Log_s has '[watchdog] filed' and Log_s has 'Key Vault secret'\n| project TimeGenerated, Finding = Log_s\n| order by TimeGenerated desc"
          size         = 0
          title        = "Key Vault secret-expiry findings (7d)"
          timeContext  = { durationMs = 604800000 }
          queryType    = 0
          resourceType = "microsoft.operationalinsights/workspaces"
        }
      },
      {
        type = 3
        content = {
          version      = "KqlItem/1.0"
          query        = "ContainerAppConsoleLogs_CL\n| where Log_s startswith '[watchdog]' and Log_s has_any ('failed', 'refusing to run')\n| project TimeGenerated, Error = Log_s\n| order by TimeGenerated desc"
          size         = 0
          title        = "Watchdog run failures (7d)"
          timeContext  = { durationMs = 604800000 }
          queryType    = 0
          resourceType = "microsoft.operationalinsights/workspaces"
        }
      },
      {
        type = 3
        content = {
          version      = "KqlItem/1.0"
          query        = "ContainerAppConsoleLogs_CL\n| where Log_s has_any ('call_failed', 'primary_failed', 'fallback_failed', 'passthrough fallback')\n| summarize Failures = count() by bin(TimeGenerated, 1h)\n| render timechart"
          size         = 0
          title        = "Model-router upstream failures & fallbacks (per hour)"
          timeContext  = { durationMs = 604800000 }
          queryType    = 0
          resourceType = "microsoft.operationalinsights/workspaces"
        }
      }
    ]
  })
}

resource "azurerm_application_insights_workbook" "observability" {
  count               = var.enable_observability_workbook ? 1 : 0
  name                = local.workbook_uuid
  resource_group_name = var.resource_group_name
  location            = var.location
  display_name        = "AAF Platform Observability — ${var.environment}"
  source_id           = lower(azurerm_log_analytics_workspace.main.id)
  data_json           = local.workbook_json
  tags                = var.tags
}

# ─── Outputs ─────────────────────────────────────────────────────────────────
output "action_group_id" {
  description = "Resource id of the alert action group (null when no alert_emails set)."
  value       = local.alerts_enabled ? azurerm_monitor_action_group.alerts[0].id : null
}

output "alert_rule_ids" {
  description = "Resource ids of the scheduled-query alert rules (empty when disabled)."
  value = local.alerts_enabled ? [
    azurerm_monitor_scheduled_query_rules_alert_v2.watchdog_critical[0].id,
    azurerm_monitor_scheduled_query_rules_alert_v2.secret_expiry[0].id,
    azurerm_monitor_scheduled_query_rules_alert_v2.watchdog_run_failure[0].id,
  ] : []
}

output "workbook_id" {
  description = "Resource id of the observability workbook (null when disabled)."
  value       = var.enable_observability_workbook ? azurerm_application_insights_workbook.observability[0].id : null
}
