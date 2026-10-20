+++
name = "cost-alert"
description = "Warn when the daily token or usage budget is exceeded"
version = 1

[gate]
type = "cron"
schedule = "0 9 * * *"

[tracking]
labels = ["plugin:cost-alert", "category:cost"]
digest = true

[execution]
timeout = "5m"
notify_on_failure = true
severity = "high"
+++

# Cost Alert

Runs daily at 09:00 and warns when the previous day's token or usage spend
exceeded the configured budget.

## What it does

1. Read the threshold from `COST_ALERT_DAILY_BUDGET` (environment variable
   name only; the value is never stored in this repository).
2. Read yesterday's usage from the agreed source (see TODO below).
3. Compare and classify: under budget -> `success`, over budget -> `failure`
   escalated to the operator.
4. Record the outcome:
   `gt plugin record-run --plugin cost-alert --result success|failure \
     --title "cost-alert: <date>" --description "<spend> / <budget>"`

## Status and limits

- **Blocked until the data source is decided** (the plugin as written cannot
  produce numbers by itself). Candidate sources: provider billing export,
  proxy/gateway request logs, or per-agent counters maintained by the town.
- Until then, record `blocked` with a one-line reason instead of guessing.
- Instruction-based plugin (no `run.sh`).
