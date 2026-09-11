# Changelog

## [Unreleased] — 1.0.0

First public release as **pubrepo** (previously an internal publishing tool).

### Added
- `--amend` / `--no-amend` flags: `--amend` replaces the last published
  commit instead of appending a new one — full rebuild, same scrub gate,
  push with `--force-with-lease` pinned to the fetched sha. Refuses
  foreign commits (exit 5) and verifies the last commit is a pubrepo
  snapshot. Author identity preserved; author and committer dates
  refreshed. `--no-amend` forces a new commit, overriding the config
  default. Incompatible with `--force-overwrite`, `--stage`, `--dry-run`,
  `--diff`.
- `on_republish` config key (`"new_commit"` | `"amend"`, default
  `"new_commit"`): sets the project default for re-publishing. CLI
  `--amend` / `--no-amend` overrides. On first publish with no prior
  commit, config amend falls back to a new commit automatically.
- Publish log entries now include `amend` (bool) and `amended_from`
  (sha or null) fields on every entry (success, scrub-failed, amend).
- `--stage` flag: performs the full nuke/rebuild (copy, transform, scrub)
  and reports what changed, then stops — no commit, no push, no log entry,
  no tags. Built files stay on disk for inspection. Baseline is the local
  `.publish/` HEAD (remote never contacted). Works with `--json` for
  machine-readable change reports. Incompatible with `--dry-run`, `--diff`,
  `-m`, `--force-overwrite`.
- `integrate` subcommand: prints (never executes) a copy-paste recipe for
  pulling foreign public-repo commits back into source — foreign range
  from the last snapshot, files filtered through the manifest engine,
  conflict surface prediction, `git apply --3way` + `--author` credit.
  The exit-5 divergence refusal now points at it. Workflow doc:
  `docs/integrating-changes.md`.
- `validate` subcommand: every config + environment finding in one pass,
  human or `--json {valid, errors, warnings}`.
- `status` dashboard: config / source / last publish / pending / scrub
  sections; `--check` (exit 0 up-to-date, 4 scrub would fail, 6 pending);
  `--remote` opt-in divergence report; reworked `--json` schema.
- `--diff`: unified diff of the **post-transform** content the public repo
  would receive (on `status` and `publish`; on publish it implies
  `--dry-run` and never publishes).
- Divergence protection: fetch-first before any destructive step, branch
  verification, exit 5 with the foreign commits listed; `--force-overwrite`
  pushes with `--force-with-lease` pinned to the fetched sha.
- Locking: concurrent publishes are refused (exit 3) via flock.
- Publish audit log at `<publish-dir>/.git/pubrepo-log.jsonl`, including
  scrub-blocked attempts.
- Multi-target publishing: `--config .publish-<name>.toml` + `dir` key.
- Enriched public commit messages: source commit/branch, file counts,
  transform count, scrub pattern count.
- Output discipline: `--quiet`, `--verbose` (per-file decisions + timing),
  color on ttys (`NO_COLOR` honored), `--json` purity, phase progress lines.
- Fail-closed scrub: a file the scrubber cannot read blocks the publish.
- Ctrl-C/SIGTERM safety: the publish dir is restored to the last published
  state; exit 130.

### Changed — migration notes
- **Exclude matcher** now uses three documented rules. Bare directory
  patterns like `internal/` match at ANY depth (previously top-level only —
  which was a leak). Every behavior change widens exclusion.
- **`--dry-run` exits 4** when scrub would fail (previously 0 — a bug).
- **Mid-publish git failures exit 2** (previously 1).
- **`status --json` schema** reworked: `last_published` → `last_publish`,
  new `config`/`source.branch`/`changes.publish_dir_dirty`/`remote` fields.
- **`.env.example` / `.env.sample` / `.env.template` now publish** by
  default (previously silently dropped). `.env`, `.envrc`, and other
  `.env.*` stay excluded.
- **Source tags work now**: `published/YYYYMMDDTHHMMSS` (the previous
  colon-containing format was illegal in git and had never created a tag).
- Explicit excludes now beat explicit includes, with a warning.

### Fixed
- `.git`, the publish workdirs, and the config file are hard-excluded at
  every depth and cannot be published — even with `include = ["."]`.
- `keep` entries with `/` are rejected at config load (previously silently
  destroyed); keep-file edits now show in `status`.
- `init` no longer hides clone failures behind a silent fallback.
- Transform rule values are validated at config load.
- Transform target paths are validated: absolute paths and paths that
  escape the publish directory (via `..`) are rejected; overlap with
  `keep` entries (exact or nested) is rejected.
