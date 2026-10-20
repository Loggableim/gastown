+++
name = "lint-gate"
description = "Report lint failures on open PR branches before merge"
version = 1

[gate]
type = "cooldown"
duration = "6h"

[tracking]
labels = ["plugin:lint-gate", "category:quality"]
digest = true

[execution]
timeout = "10m"
notify_on_failure = true
severity = "medium"
+++

# Lint Gate

Runs during Deacon patrol (6h cooldown) and surfaces lint failures on open
PR branches before they reach a merge queue.

## What it does

1. List open PRs in each rig with a merge queue:
   `gh pr list --state open --json number,headRefName,title --limit 50`
2. Detect the project linter (first match wins):
   `make lint`, `golangci-lint run`, `npm run lint`, `ruff check .`
3. Run the linter against the PR branch head.
4. Record the outcome:
   `gt plugin record-run --plugin lint-gate --result success|failure \
     --title "lint-gate: <pr-reference>" --description "<one-line summary>"`

## Status and limits

- Instruction-based plugin: no `run.sh`, so the dog interprets these steps.
  There is no deterministic exit code and therefore **no hard merge block** yet.
- TODO for hard blocking: add `run.sh` that exits non-zero on lint errors and
  wire it as a pre-merge gate.
