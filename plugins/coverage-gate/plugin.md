+++
name = "coverage-gate"
description = "Flag pull requests that drop test coverage below the project threshold"
version = 1

[gate]
type = "cooldown"
duration = "6h"

[tracking]
labels = ["plugin:coverage-gate", "category:quality"]
digest = true

[execution]
timeout = "15m"
notify_on_failure = true
severity = "medium"
+++

# Coverage Gate

Runs every 6h and flags PRs whose test coverage falls below the project
threshold.

## What it does

1. Read the threshold from the rig: `coverageThreshold` in `package.json`,
   `codecov.yml`, or a documented rig default.
2. Produce a coverage number for open PR branches:
   `go test -cover ./...`, `npm test -- --coverage`, or `pytest --cov`.
3. Compare against the threshold and against the target branch baseline.
4. Record the outcome:
   `gt plugin record-run --plugin coverage-gate --result success|failure \
     --title "coverage-gate: <pr-reference>" --description "<coverage> vs <threshold>"`
5. On failure, create a bead with label `plugin:coverage-gate` so the work is
   trackable rather than only reported.

## Status and limits

- Instruction-based plugin (no `run.sh`): reports, does not hard-block.
- TODO: `run.sh` with per-rig coverage command detection and non-zero exit.
- Coverage tooling must exist per rig; if a rig has no test runner, record
  `blocked` instead of `failure`.
