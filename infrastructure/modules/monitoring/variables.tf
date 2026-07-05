variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "project" { type = string }
variable "environment" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}

variable "log_retention_in_days" {
  description = "Log Analytics workspace retention in days (cost-optimized default: 30)."
  type        = number
  default     = 30
}

variable "log_daily_quota_gb" {
  description = "Log Analytics daily ingestion cap in GB (-1 = unlimited; cost-optimized default: 1)."
  type        = number
  default     = 1
}

# ─── Observability: alerts + workbook (all opt-in) ───────────────────────────

variable "alert_emails" {
  description = "Email recipients for the platform alert action group. Empty (default) means no action group and no alert rules are created — the module behaves exactly as v1.0."
  type        = list(string)
  default     = []
}

variable "watchdog_app_name" {
  description = "Container App (or Job) name of the watchdog, used to scope the alert/workbook log queries. Empty matches across all apps in the workspace."
  type        = string
  default     = ""
}

variable "alert_evaluation_frequency" {
  description = "How often the scheduled-query alert rules run (ISO 8601 duration). Must be <= alert_window_duration."
  type        = string
  default     = "PT15M"
}

variable "alert_window_duration" {
  description = "Look-back window each scheduled-query alert evaluates (ISO 8601 duration)."
  type        = string
  default     = "PT1H"
}

variable "enable_observability_workbook" {
  description = "Create the Azure Monitor workbook (watchdog activity, secret expiry, gateway health). Off by default — it's a convenience surface, not a guardrail."
  type        = bool
  default     = false
}
