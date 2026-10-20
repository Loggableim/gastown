+++
name = "changelog-enforcer"
description = "Require a CHANGELOG entry for user-facing pull requests"
version = 1

[gate]
type = "cooldown"
duration = "6h"

[tracking]
labels = ["plugin:changelog-enforcer", "category:quality"]
digest = true

[execution]
timeout = "10m"
notify_on_failure = true
severity = "low"
+++

# Changelog Enforcer

Every 6h, checks that user-facing pull requests carry a CHANGELOG entry.

## What it does

1. List open PRs: `gh pr list --state open --json number,title,files`.
2. Skip PRs that only touch tests, docs, CI, or internal tooling.
3. For the remaining PRs, verify that `CHANGELOG.md` is part of the PR diff and
   that the entry describes the change.
4. Record the outcome:
   `gt plugin record-run --plugin changelog-enforcer --result success|failure \
     --title "changelog-enforcer: <pr-reference>" --description "<reason>"`
5. On failure, comment on the PR with a one-line reminder instead of closing
   anything.

## Status and limits

- Instruction-based plugin (no `run.sh`): advisory, no hard block.
- TODO: `run.sh` variant that fails the check deterministically for rigs that
  want it enforced.
