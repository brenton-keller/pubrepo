# pubrepo

**Work in your messy private repo. Publish a clean, scrubbed public one.
The scrub gate makes leaking physically impossible.**

![pubrepo catching a leak, then publishing clean](docs/demo.gif)

## The problem

Your repo is your workshop. In 2026 that means `CLAUDE.md` files, `.claude/`
directories, agent transcripts, prompt scratchpads, internal hostnames in
configs, and notes-to-self — living in the same tree as the code you want to
share. Publishing the whole thing is out of the question; maintaining a
second cleaned-up repo by hand drifts in a week.

pubrepo publishes the showroom: an explicit allowlist of files, transformed
and scrubbed, pushed to a public repo whose history never contains your
private commits. One Python file, one TOML file, zero dependencies —
*Copybara for humans*.

## Quickstart

You need git and Python 3.11+ (macOS: `brew install pipx`;
Debian/Ubuntu: `sudo apt install pipx`).

```bash
pipx install pubrepo           # recommended: isolated install, on your PATH
# or run without installing:   uvx pubrepo
# or plain pip, inside a venv: pip install pubrepo
#   (bare pip fails on Homebrew/Debian Python: externally-managed-environment)
# or the single file, no installer:
# curl -o ~/.local/bin/pubrepo https://github.com/brenton-keller/pubrepo/releases/latest/download/pubrepo.py
# chmod +x ~/.local/bin/pubrepo
```

Drop a `.publish.toml` in your repo root:

```toml
[publish]
remote  = "https://github.com/you/repo.git"
include = ["src/", "README.md", "pyproject.toml"]

[publish.scrub]
forbidden = ["acme-internal", "10.1.2."]
```

```bash
pubrepo init       # clone the public remote into .publish/
pubrepo --diff     # preview exactly what the world will receive
pubrepo            # fetch-check, rebuild, transform, scrub, commit, push
```

## How it works

Every publish **nukes and rebuilds** the `.publish/` working clone from your
source tree and the manifest. The public repo is a pure function of
*(source, manifest)*: no drift, no orphaned files, trivially auditable.
Unchanged files hash identically, so a docs-only change produces a docs-only
public commit — partial publishes are automatic, not a feature.

Three privacy dimensions:

1. **The scrub gate** — publish-blocking, not advisory. Any text file in the
   publish set containing a forbidden string refuses to publish (exit 4).
2. **History privacy** — the public repo gets snapshot commits. Your private
   commit messages, timestamps, and abandoned experiments never leave home.
3. **Exclude-list privacy** — `.publish.toml` itself can never be published,
   so the public repo never reveals *what you chose to hide*.

And the hard floor: `.git`, the publish workdirs, and the config file are
excluded at every depth, unconditionally — even `include = ["."]` cannot
ship them.

## The scrub gate

Literal, case-insensitive substring matching against an enumerable forbidden
list. `"acme"` catches `ACME`, `acme-prod`, and `ssh://acme/` for free.

It **fails closed**: a file the scrubber cannot read blocks the publish
exactly like a hit would. Binary files (null byte in the first 8 KiB) skip
scrub and transforms by documented policy — they can't leak readable
strings, and your `.git` history (the binary leak that scrub couldn't catch)
is hard-excluded instead.

No regex, deliberately: a forbidden list is enumerable, regex in config is a
footgun, and user-supplied patterns are a ReDoS surface. For *class*
patterns ("any email", "any private IP", entropy detection), pair pubrepo
with [gitleaks](https://github.com/gitleaks/gitleaks) — run it over
`.publish/` before pushing.

## Transforms

Per-file rewrites applied after copy, before scrub:

```toml
[publish.transforms]
"pyproject.toml" = [
  { find = "internal-package-name", replace = "public-name" },        # literal, all occurrences
  { strip_between = ["<!-- INTERNAL -->", "<!-- /INTERNAL -->"] },    # inclusive removal, all blocks
]
```

The scrub gate runs on the *post-transform* content, and `--diff` previews
the *post-transform* result — what the public repo will actually receive.
An orphaned start marker (missing end marker) leaves content in place and
warns; the scrub gate is the backstop.

## Pattern rules (exclude)

Three rules, in order:

| Pattern | Rule | Matches |
|---|---|---|
| `internal/` | trailing `/` → dirs-only, any depth | `internal/`, `src/internal/` |
| `src/gen/*.py` | contains `/` → anchored to repo root | `src/gen/foo.py`, not `other/gen/foo.py` |
| `*.snap` | bare name → basename at any depth | `a.snap`, `deep/b.snap` |

Case-sensitive, like git. `* ? [ ]` are live (`[[]` for a literal `[`);
`*` crosses `/` in anchored patterns; there is no `**`. `include` entries
are **literal paths, never patterns** — the manifest is an explicit
allowlist by design.

Precedence: hard-excludes → your excludes (beat your includes, with a
warning) → your includes (beat the default excludes at the entry level) →
default excludes (`__pycache__`, `*.pyc`, `.venv`, `.claude`, `.env`,
`.envrc`, `.env.*` except `.env.example`/`.env.sample`/`.env.template`, …)
filtering everything walked.

## One-way mirror

The public repo is a projection, not a collaboration space. Transforms
aren't invertible — a PR diff against transformed content has no
well-defined preimage in your source. So:

- pubrepo **fetches before touching anything** and refuses to publish over
  commits it didn't create (exit 5, with the foreign commits listed).
- When someone PRs your public repo: thank them, apply the change to your
  *source* repo, republish with `--force-overwrite`. **`pubrepo integrate`
  prints a copy-paste recipe** — foreign range, manifest-filtered file
  list, `git apply --3way`, `--author` credit — never executes. Full
  workflow: [docs/integrating-changes.md](docs/integrating-changes.md).
- `--force-overwrite` pushes with `--force-with-lease` pinned to the exact
  sha you were shown — a commit landing in between is refused, never
  silently destroyed. On a non-diverged remote it degrades to a normal push.
- Recommended: enable branch protection on the public repo (PRs only).

## CI integration

`status --check` and `--dry-run` are built for automation:

| Exit | Meaning |
|---|---|
| 0 | success / up to date |
| 1 | config error |
| 2 | publish failure (git, push, remote unreachable, wrong branch) |
| 3 | another publish holds the lock |
| 4 | scrub would fail / failed |
| 5 | public repo diverged |
| 6 | changes pending (`status --check`) |
| 130 | interrupted (publish dir restored) |

Pre-push hook (block pushes while a leak exists):

```bash
#!/bin/sh
exec pubrepo --dry-run --quiet
```

GitHub Action step:

```yaml
- name: pubrepo gate
  run: pip install pubrepo && pubrepo --dry-run   # exit 4 fails the job
```

`--json` gives machine-readable output on `status`, `validate`, and
`--dry-run`; with it, stdout carries only the JSON document.

## vs. the alternatives

| | pubrepo | Copybara | git filter-repo | subtree split | hand-rolled |
|---|---|---|---|---|---|
| Setup | 1 file + 1 TOML | JVM + Bazel-ish config | one-shot tool | git built-in | yours forever |
| Scrub gate | blocking | rule-dependent | no | no | if you wrote one |
| History privacy | snapshots | configurable | ships rewritten history | ships history | varies |
| Transforms | find/replace + strip | rich | path/blob rewrite | no | varies |
| Continuous publishing | yes | yes | no | partial | varies |
| Dependencies | none (stdlib) | JVM | python pkg | none | varies |

## Config reference

Every field, type, default, and validation rule: [docs/config.md](docs/config.md).
`pubrepo validate` reports every config and environment finding at once.

## FAQ

**Multiple public repos from one source?** One config per target:
`.publish-sdk.toml` with `dir = ".publish-sdk"`, then
`pubrepo publish --config .publish-sdk.toml`. Targets are isolated by
construction — neither can ship the other's workdir or config.

**Non-GitHub remotes?** Any git URL — GitLab, Bitbucket, self-hosted.

**Rollback?** `cd .publish && git revert HEAD && git push`. The next publish
re-asserts the source state.

**Someone pushed commits to my public repo — now what?** `pubrepo
integrate` prints the exact commands to pull their work back into your
source repo (junk filtered through your manifest, conflicts predicted,
credit preserved), then republish. See
[docs/integrating-changes.md](docs/integrating-changes.md).

**Why was my publish blocked?** Run `pubrepo --dry-run` — it lists every
file and matched pattern; `pubrepo validate` checks the config itself.

**Does `--dry-run` check for divergence?** No — dry-run is local-only and
never contacts the remote. Use `pubrepo status --remote` before publishing.

**Why doesn't `--diff` show my keep-file edits?** `keep` files are
user-managed in `.publish/`; `--diff` shows source→publish deltas. `status`
reports uncommitted publish-dir changes separately.

**Flags before the command?** Put them after: `pubrepo status -v`, not
`pubrepo -v status` (the bare-`pubrepo` shorthand claims the first slot).

**Windows?** Best effort: locking is a no-op and CI doesn't test it.

## Meta

This repository is published by pubrepo itself — the manifest and forbidden
list live in the private source repo, where you can't see them. That's the
point. The first publish used the installed tool; every publish after that
uses the version being published.

License: MIT
