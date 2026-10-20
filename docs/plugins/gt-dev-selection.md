# Plugin selection for the GT dev setup

Decision by Cid on 2026-10-20 21:26 (Europe/Vienna): all stock plugins plus
numbers 14-27 go into this fork. Numbers 28-33 are not part of this selection.

## How plugins are activated (verified against the loader)

Source of truth: `internal/plugin/scanner.go` and `internal/plugin/types.go`
in this repository.

- A plugin is a directory containing `plugin.md` with TOML frontmatter between
  `+++` fences. Required frontmatter field: `name`. `run.sh` is optional.
- Discovery is directory-based: town level `~/gt/plugins/<name>/`, rig level
  `<rig>/plugins/<name>/`. On a name collision the rig-level plugin wins.
- There is no central enable/disable file. Gates decide when a plugin runs:
  `cooldown`, `cron`, `condition`, `event`, `manual`.
- With `run.sh`, the dog executes the script. Without `run.sh`, the dog
  interprets the markdown instructions.

## Selection

### Stock plugins (1-13) - unchanged, already present in `plugins/`

compactor-dog, dolt-archive, dolt-backup, dolt-log-rotate, dolt-snapshots,
git-hygiene, github-sheriff, gitignore-reconcile, quality-review, rebuild-gt,
stuck-agent-dog, submodule-commit, tool-updater.

Note: the five dolt plugins are only meaningful once dolt/beads is installed on
the box. Until then they may report errors; that is expected.

### Added in this branch (14-19)

| Nr | Plugin | Gate | Notes |
|----|--------|------|-------|
| 14 | lint-gate | cooldown 6h | instruction-based, no hard block yet |
| 15 | coverage-gate | cooldown 6h | instruction-based, needs a test runner per rig |
| 16 | security-scan | cooldown 24h | instruction-based, scanners per rig |
| 17 | deploy-notifier | cooldown 30m | needs explicit approval + `DEPLOY_WEBHOOK_URL` in env |
| 18 | cost-alert | cron 09:00 | blocked until the usage data source is decided |
| 19 | changelog-enforcer | cooldown 6h | instruction-based, advisory |

All six are instruction-based (no `run.sh`). Deterministic blocking requires a
follow-up `run.sh` per plugin that exits non-zero.

### Skills (20-24)

`ghi-list`, `pr-list`, `crew-commit`, `pr-sheriff` were already present in
`.claude/skills/`. This branch adds the missing official skill `convoy`,
copied verbatim from `docs/skills/convoy/SKILL.md` (same repository, MIT).

### Packs (25-27) - not vendored

`gastown-publish/n-skills` (packs: orchestration, gastown,
open-source-maintainer) publishes **without a license file** (`license: null`
via the GitHub API). Copying that content into a public fork is not covered by
any license, so it is deliberately not vendored here. Options:

1. Ask upstream for a license, then vendor.
2. Use the upstream marketplace/install mechanism instead of copying.
3. Re-implement the parts we need ourselves.

### Not in this selection

28 (dev-browser), 29 (community pack), 30-33 (GUIs). Reserved for a later
decision.
