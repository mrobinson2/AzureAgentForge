# Monitoring module

Log Analytics workspace + Application Insights, plus an **opt-in** observability
layer: alert rules and an Azure Monitor workbook over the platform's Container
Apps console logs.

## What it creates

| Resource | When | Notes |
|---|---|---|
| `azurerm_log_analytics_workspace` | always | Retention + daily cap are cost-profile tunable. |
| `azurerm_application_insights` | always | Workspace-based. |
| `azurerm_monitor_action_group` | `alert_emails` non-empty | Email receivers, common alert schema. |
| 3× `azurerm_monitor_scheduled_query_rules_alert_v2` | `alert_emails` non-empty | Watchdog critical / secret expiry / watchdog run failure. |
| `azurerm_application_insights_workbook` | `enable_observability_workbook = true` | One pane: watchdog activity, secret expiry, gateway health. |

Set neither variable and the module is exactly its v1.0 form (workspace +
App Insights) with no new resources — clean-fork safe.

## The log-marker contract

The alerts and workbook query Container Apps console logs
(`ContainerAppConsoleLogs_CL`, populated because the managed environment is
wired to this workspace). They match the stable markers the services already
emit — no app changes, no structured-logging migration required:

| Signal | Source line | Emitter |
|---|---|---|
| Critical finding filed | `[watchdog] filed [critical] …` | `services/watchdog/watchdog.py` |
| Secret at/near expiry | `[watchdog] filed [...] Key Vault secret '…' …` | `detect_expiring_secrets` → filer |
| Watchdog internal failure | `[watchdog] … failed: …` / `… refusing to run` (stderr) | `services/watchdog/watchdog.py` |
| Gateway upstream failure | `call_failed` / `primary_failed` / `fallback_failed` / `passthrough fallback` | `services/model-router/main.py` |

If you rename these markers, update the queries in `alerts.tf` to match.

## Enabling

```hcl
# infrastructure/environments/dev/<profile>.tfvars  (or -var on the CLI)
alert_emails                  = ["oncall@example.com"]
enable_observability_workbook = true

# Optional: scope queries to just the watchdog job. Left empty, the queries
# match across all apps on the unique [watchdog] markers — which is the safer
# default, since Container App *Job* log rows don't always populate
# ContainerAppName_s.
# watchdog_app_name = "caj-watchdog-dev"
```

`alert_evaluation_frequency` (default `PT15M`) and `alert_window_duration`
(default `PT1H`) tune how often each rule runs and how far back it looks.

## Alert semantics

Each rule fires when its query returns **any** row in the window (`Count > 0`),
with `failing_periods = 1/1` so a single matching log line pages immediately —
these are low-frequency, high-signal events, not noisy metrics. Severities:
watchdog-critical = Sev1, secret-expiry and watchdog-failure = Sev2.
