+++
name = "deploy-notifier"
description = "Notify an ops webhook about merges that should trigger a deploy"
version = 1

[gate]
type = "cooldown"
duration = "30m"

[tracking]
labels = ["plugin:deploy-notifier", "category:ops"]
digest = true

[execution]
timeout = "5m"
notify_on_failure = false
severity = "low"
+++

# Deploy Notifier

Every 30 minutes, checks for merges since the last run and posts a short
summary to the ops webhook.

## What it does

1. Determine merges since the last recorded run:
   `gh pr list --state merged --json number,title,mergedAt,baseRefName`
2. Filter to the branches that drive deployments for this rig.
3. If `DEPLOY_WEBHOOK_URL` is set in the environment, POST a compact summary
   (repo, PR numbers, merge time). The URL is read from the environment only.
4. If `DEPLOY_WEBHOOK_URL` is missing, record `skipped` and stop quietly.
5. Record the outcome:
   `gt plugin record-run --plugin deploy-notifier --result success|skipped \
     --title "deploy-notifier" --description "<n> merges"`

## Status and limits

- **Needs an explicit go-ahead before first activation**: posting to an
  external webhook touches an external service.
- No secret is stored in this repository. Only the environment variable name
  `DEPLOY_WEBHOOK_URL` is referenced; the value belongs in the environment or
  vault.
- Instruction-based plugin (no `run.sh`).
