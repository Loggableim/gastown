+++
name = "security-scan"
description = "Daily dependency and static vulnerability scan across rigs"
version = 1

[gate]
type = "cooldown"
duration = "24h"

[tracking]
labels = ["plugin:security-scan", "category:security"]
digest = true

[execution]
timeout = "30m"
notify_on_failure = true
severity = "high"
+++

# Security Scan

Runs once per day and checks rigs for known dependency vulnerabilities and
obvious insecure patterns.

## What it does

1. Per rig, run the scanners that exist for its stack:
   `pip-audit`, `npm audit --json`, `govulncheck ./...`, `gosec ./...`.
2. Skip missing scanners silently but note them in the description.
3. Classify findings: any critical/high advisory is a failure, the rest is a
   note.
4. Record the outcome:
   `gt plugin record-run --plugin security-scan --result success|failure \
     --title "security-scan: <rig>" --description "<counts> critical/high"`
5. On failure, create a bead with label `plugin:security-scan`.

## Status and limits

- Instruction-based plugin (no `run.sh`).
- Scanner availability differs per rig; absence is reported, not treated as
  success.
- No network access beyond the package registries the scanners already use.
