# pubrepo

**Work in your messy private repo. Publish a clean, scrubbed public one.**
The scrub gate refuses to publish when any string on your forbidden list
appears, case-insensitively, in a file it publishes or in the commit message
you give it. Binary files it cannot scan block the publish until you list
them as known-safe.

![pubrepo catching a leak, then publishing clean](https://raw.githubusercontent.com/brenton-keller/pubrepo/v1.1.0/docs/demo.gif)

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
# curl -o ~/.local/bin/pubrepo https://raw.githubusercontent.com/brenton-keller/pubrepo/v1.1.0/pubrepo.py
# chmod +x ~/.local/bin/pubrepo
```

Drop a `.publish.toml` in your repo root (or in the subdirectory you publish
from):

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
pubrepo --stage    # build .publish/ on disk for hands-on inspection
pubrepo publish    # confirm, then fetch, build, transform, scrub, commit, push
```

`publish` shows the preview and asks `Publish? [y/N]`. Off a terminal
(scripts, CI) it needs `--yes`: `pubrepo publish --yes`. Without it, it
prints a preview and the command to run, and publishes nothing.
Bare `pubrepo` shows `status`. See
[Confirmation and scripts](#confirmation-and-scripts).

## How it works

Every publish builds the next snapshot **on top of the current public
branch** and pushes it as a fast-forward. pubrepo computes what your source
tree and manifest publish (after transforms), lays that over the public
tree, and commits the result in `.publish/`. Unchanged files hash
identically, so a docs-only change produces a docs-only public commit.

Files on the public branch that your manifest doesn't cover — a host-created
`LICENSE`, a collaborator's notes file — are **carried forward byte for
byte**. pubrepo changes or removes a public file only when it can verify that
it wrote the current content itself, or when you recorded a decision for that
file with `pubrepo pull`. Everything else is a question pubrepo asks you,
never something it does silently.

Routine commands — publish, release, previews, `status`, `pull` — never
rewrite public history; publish and release push plain fast-forwards. The
only rewrites are an explicit, confirmed `--amend` of your own last
snapshot and a planned, confirmed `repair`.

Three privacy dimensions:

1. **The scrub gate** — publish-blocking, not advisory. Content pubrepo would
   write that contains a forbidden string, or that it cannot read, refuses
   to publish (exit 4).
2. **History privacy** — the public repo gets snapshot commits. Your private
   commit messages, timestamps, and abandoned experiments never leave home.
3. **Exclude-list privacy** — `.publish.toml` itself can never be published,
   so the public repo never reveals *what you chose to hide*.

And the hard floor: `.git`, the publish workdirs, and the config file are
excluded at every depth, unconditionally — even `include = ["."]` cannot
ship them.

**Private records.** pubrepo keeps small records of what each snapshot
contained under `refs/pubrepo/*` in your source repo. They are commit
objects on no branch. pubrepo syncs them to a remote of your source repo
(`[publish] records_remote`, else the branch's push remote) so other
machines and CI recognize your snapshots exactly, but only if every URL of
that remote is listed exactly in `[publish] private_remotes` and none is a
publish remote. Otherwise pubrepo warns, prints the `private_remotes` line
to add (once per clone), and verifies snapshots against your source history
instead, as it does when sync fails. On a terminal, `pubrepo init` offers to
add that line for you (default No).

**Bookmarks.** After each clean publish pubrepo bookmarks the published
source commit as the local ref `refs/pubrepo/bookmarks/<timestamp>`. It
stays local unless `[publish] tag_remote` names a remote that passes the
same `private_remotes` check (see
[docs/config.md](https://github.com/brenton-keller/pubrepo/blob/v1.1.0/docs/config.md#private-refs-and-private_remotes)).
1.0.0 and 1.0.1 created a `published/<timestamp>` tag instead and pushed it
to `origin`, which publishes private history if `origin` is public. Run
`pubrepo validate` to list leftover local tags, and
`pubrepo validate --remote` to list such tags on remotes that must not hold
them; both print the cleanup commands.

## The scrub gate

Literal, case-insensitive substring matching against an enumerable forbidden
list. `"acme"` catches `ACME`, `acme-prod`, and `ssh://acme/` for free. It
runs on every file pubrepo would write, after transforms, and on `keep`
files.

It also checks the commit message you give with `-m` (title and body), and
the title `--amend` reuses when you give no `-m`. A hit exits 4 before
anything is committed or pushed; the error names the number of forbidden
strings and their lines, never the strings. `--dry-run -m` and `--diff -m`
check it too. The generated parts of the message (the `publish: <time>`
title, the `Source:`, `Files:`, `Transforms:` and `Scrub:` lines) are not
scanned.

It **fails closed**:

- **Text** that contains a forbidden string blocks (exit 4). A file the
  scanner cannot read blocks exactly like a hit.
- **Office files** (`.docx`, `.xlsx`, `.pptx` and other OOXML/OPC
  containers) are opened in memory and every XML part is scanned —
  including comments and attribute values. Nested containers are walked
  with size and depth limits; exceeding a limit, or a corrupt file, blocks
  (and cannot be allowlisted).
- **Other binaries** — images, PDFs, non-Office zips, encrypted or legacy
  Office files (reported as "encrypted or legacy Office container") —
  cannot be scanned, so they block until you list them in
  `[publish.scrub] allow_opaque`. Allowlisted files are reported as
  "not scanned (allowlisted)" on every run.
- **Conflict markers** — a `<<<<<<<` … `=======` … `>>>>>>>` triple (or
  the diff3 form with `|||||||`) in content pubrepo writes blocks. A lone
  `=======` line, such as a reStructuredText underline, does not. Allow a
  path that legitimately contains them with `allow_markers`.

```toml
[publish.scrub]
forbidden     = ["acme-internal", "10.1.2."]
allow_opaque  = ["docs/*.gif"]          # known-safe binaries (globs)
allow_markers = ["docs/merge-howto.md"] # files that legitimately show markers
```

**Content pubrepo didn't write** (carried public files) is scanned the same
way and reported as already public. It never blocks a publish; it blocks a
release until resolved. A forbidden string in carried content has no
allowlist: clear it with `pubrepo pull --redecide <path> --remove <path>`,
with `repair`, or by changing your own forbidden list.

No regex, deliberately: a forbidden list is enumerable, regex in config is a
footgun, and user-supplied patterns are a ReDoS surface. For *class*
patterns ("any email", "any private IP", entropy detection), pair pubrepo
with [gitleaks](https://github.com/gitleaks/gitleaks) — run it over
`.publish/` after `pubrepo --stage`.

## Transforms

Per-file rewrites applied to what pubrepo publishes, before scrub:

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
| `src/gen/*.py` | contains `/` → anchored to the directory holding `.publish.toml` | `src/gen/foo.py`, not `other/gen/foo.py` |
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

## Collaborators: `pull`

People can commit to your public repo — a merged pull request, a web-UI
edit, a collaborator pushing directly. pubrepo never overwrites their work.
When the public branch has commits you haven't taken in, publish refuses
(exit 5) and says to run `pubrepo pull`.

`pubrepo pull` brings those changes into your **source** repo, under
guardrails:

- It is explicit — nothing pulls implicitly — and refuses on a dirty tree
  (staged changes, edited tracked files, or anything at a path it would
  write).
- It shows a **plan** first: the public commit range, every changed path
  with its class and diff, and anything it needs you to decide. Deletions,
  binaries, build and CI files, transformed paths and other flagged items
  are never decided for you.
- It writes the accepted files into your working tree and makes **one
  source commit**, authored by you, with `Co-authored-by:` lines for the
  collaborators whose changes you took.
- Text conflicts are written with markers and the pull stays in progress:
  resolve them, then `pubrepo pull --continue`, or `pubrepo pull --abort`
  to restore exactly the paths pull wrote.
- It never writes to the public repo.

```bash
pubrepo pull --plan                      # show the plan and its digest; write nothing
pubrepo pull                             # on a terminal: plan, then "Commit this pull? [y/N]"
pubrepo pull --theirs data/table.xlsx    # decide an item: one --<decision> PATH per path
pubrepo publish                          # then publish as usual (a fast-forward on their commits)
```

Off a terminal, a pull commits only a plan bound by digest: run
`pubrepo pull --plan [decisions]`, then
`pubrepo pull --yes --expect-tip <sha> --expect-plan <digest> [the same decisions]`.
`pubrepo integrate` is now an alias for `pull --plan`.

Pull requests must be merged on the host before `pull` can take them in;
pubrepo does not read unmerged pull requests. Host merges ignore git merge
drivers. Full workflow, decisions and checks:
[docs/integrating-changes.md](https://github.com/brenton-keller/pubrepo/blob/v1.1.0/docs/integrating-changes.md).

## Re-publishing: `--amend`

By default every publish appends a new snapshot commit. `--amend` instead
replaces **your own last snapshot** — an explicit, confirmed rewrite:

```bash
pubrepo --amend               # rebuild and replace your last snapshot; confirm on a terminal
pubrepo --amend -m "fix typo" # same, with a new commit title
pubrepo --amend --yes         # off a terminal
```

Without `-m` the existing title is reused. The author is kept. `--amend`
refuses unless:

- the public tip is a pubrepo snapshot of this source, not a merge, and its
  changes verify against your source (otherwise it asks you to run
  `pubrepo pull --baseline`, exit 8);
- no tag points at the tip (exit 2, naming the tag);
- there is nothing to pull or decide (otherwise it refuses with publish's
  code: 5 or 8).

The push is `--force-with-lease` pinned to the tip you were shown; if the
tip moved, exit 5. An amend is non-fast-forward by construction, so it needs
**force-push permission** on the published branch. If branch protection
refuses it, the amend fails (exit 2). Anyone who fetched the old commit must
realign, and pubrepo prints the recovery steps to give them.

`--no-amend` is a no-op. `on_republish = "amend"` was retired in 1.1.0 and is
a config error.

## Repair: removing content from public history

`pubrepo repair` is the only general history rewrite. Use it when something
that should never have been public is in the public history.

```bash
pubrepo repair remove-path secret.txt --from <commit> --plan   # what would change; prints a digest
pubrepo repair remove-path secret.txt --from <commit> --apply  # on a terminal: type the confirmation
pubrepo repair rewind <commit> --plan                          # move the branch back
pubrepo repair restore-ancestry <tag> --plan                   # make a dropped release reachable again
```

The plan names every commit that is rewritten or destroyed, every changed
path, every public ref (other branches, tags, pull-request refs) that still
reaches the content, and the authors whose clones will diverge. Choose what
happens to each extra ref with `--also-rewrite-ref`, `--also-delete-ref`
or `--leave-reachable`. On a terminal you confirm by typing the branch name
and the number of destroyed commits; off a terminal it needs
`--apply --yes --expect-tip <sha> --expect-plan <digest>`.

| Exit | Meaning |
|---|---|
| 0 | applied; no scanned public ref reaches the content |
| 4 | applied, but the content is still reachable through the named refs ("APPLIED, INCOMPLETE") |
| 2 | not applied (for example, host protection refused the push) |
| 1 | missing confirmation, or the plan digest doesn't match |
| 5 | a public tip moved since the plan |
| 8 | a pull is in progress |

If `.publish/` held commits that never reached the public repo, repair
saves them and warns `WARNING: N local commit(s) in .publish/ were never
published; saved to refs/pubrepo/local/<timestamp>:`, followed by the list.
Commits the repair itself rewrites are named in its plan and backed up
under `refs/pubrepo/repair/<timestamp>`; they are not reported here.

Repair never touches released ancestry: a leak at or before your latest
release cannot be repaired in-tool. In 1.1.0, ranges that contain merge
commits are refused. Repair cannot purge host caches, forks or open pull
requests — **rotate any exposed secret.**

## Release management

Release management is opt-in per publish target. When
`[publish.release] enabled = true`, every real publish requires one explicit
outcome: `--release VERSION`, `--bump patch|minor|major`, or
`--no-release`. There is deliberately no configured default. On a
terminal without a flag, pubrepo shows the publish preview and asks for the
choice — a numbered list with no preselected answer. That choice is the
confirmation, and an empty answer cancels. Off a terminal, a publish without
a choice refuses before changing anything.

```bash
pubrepo --release 1.1.0   # exact strict SemVer already committed in source
pubrepo --bump minor      # next version after the highest stable managed tag
pubrepo --no-release      # publish an explicitly unreleased snapshot
```

A release is a normal snapshot on the public tip, tagged. It needs a clean
state: otherwise it refuses with publish's exit code (5 when there is
something to pull). Tags go only on pubrepo snapshots. A release given by
flag confirms once on a terminal (`[y/N]`). A release that uploads to PyPI,
which cannot be undone, asks you to type the exact version instead. Off a
terminal it needs `--yes`.

Only **managed** tags count as releases: tags pubrepo created (they carry
`Pubrepo-Release:` and `Pubrepo-Source:` trailers) and tags you list in
`[publish.release] adopt_tags` — for example tags made before 1.1.0. Other
tags are reported, never used as the version base, and never block a
publish.

Incomplete releases are journaled; rerun the identical command to resume.
See [the release workflow](https://github.com/brenton-keller/pubrepo/blob/v1.1.0/docs/releasing.md) and
[release configuration](https://github.com/brenton-keller/pubrepo/blob/v1.1.0/docs/config.md).

## Previews and staging

Previews are **offline by default**: they use the last fetched state of the
public branch and say when it was fetched. Add `--remote` to fetch first.
One exception: after `init` and before the first publish, previews
(including the off-terminal publish preview) run a read-only `git ls-remote`
to the `[publish] remote` to check the host's default branch.

| Command | What it does | Exit |
|---|---|---|
| `pubrepo --dry-run` | lists what publish would do, including the commit message | the code publish would return (0, 4, 5 or 8) |
| `pubrepo --diff` | unified diff of what the public repo would receive | 0; 4 if the gate blocks or the `-m` text would be refused; 8 while a pull is in progress |
| `pubrepo --stage` | builds the snapshot into `.publish/` on disk | 0; 4 if the gate blocks; 8 while a pull is in progress |
| `pubrepo status` | publish state, unpulled commits, pending changes | see `status --check` below |

`--dry-run --remote` exits 2 if the remote is unreachable; `--diff --remote`
and `--stage --remote` fall back to the last fetched state, labelled.

`--stage` writes exactly the tree publish would commit into `.publish/` —
no commit, no push, no publish log entry, no tags, no records. The publish
clone's real git index is untouched (a temporary index is used). If the gate
blocks (exit 4), the files stay on disk so you can inspect what triggered it.

```bash
pubrepo --stage                            # build and report: A added, M modified, D deleted
pubrepo --stage --json                     # machine-readable change report
git -C .publish status --short             # inspect the built tree yourself
```

Previews never publish, so they reject `--amend`/`--no-amend`, the
release-choice flags, `--accept-source-divergence` and `--recreate-branch`
(exit 1); `--stage` also rejects `-m`. `--stage` cannot be combined with
`--dry-run` or `--diff`, and `--diff` implies `--dry-run`. Previews work
with `--json` (`--dry-run`, `--stage`), `--quiet`, `--verbose`, `--remote`
and `--config`.

## Confirmation and scripts

pubrepo will not publish by accident:

- **Bare `pubrepo` runs `status`** and prints "To publish: `pubrepo publish`".
  Arguments that start with a flag keep their publish meaning
  (`pubrepo --dry-run`, `pubrepo --amend`, …).
- **Commands that push confirm.** That covers `publish` and every flag form
  that publishes, `--amend`, `--release`/`--bump`, `repair --apply` and
  `repair restore-ancestry`. On a terminal they show the preview and ask one
  `[y/N]` question (repair: its typed confirmation; with release management
  enabled and no release flag, the release choice is that question).
  Anything but `y`/`yes` exits 1 with nothing written. `--quiet` never hides
  the prompt.
- **Off a terminal they need `--yes`** (repair: `--yes --expect-tip
  --expect-plan`). Without it they exit 1 — "publishing needs confirmation:
  run on a terminal, or pass --yes" — before fetching, pushing or changing
  anything.
- **Off a terminal, a publish shows what it would do first.** On stdout it
  prints a preview (offline once you have published; it writes only
  unreferenced git objects in `.publish/`), then one last line: the exact
  command to run when nothing blocks, such as
  `pubrepo publish --yes --expect-tip <sha>`, or why none is offered.
  `--quiet` keeps only that line. **A release is never offered as a
  `--yes` command:** for `--release` or `--bump` the line gives the
  command without `--yes`, to run on a terminal, where you confirm it (a
  PyPI release asks you to type the version).
- `--expect-tip <sha>` binds a run to the public tip you previewed; a
  mismatch exits 5 before anything is written.
- Previews, `status`, `validate` and `init` never push. Only `init` can ask
  anything: on a terminal, one `[y/N]` (default No) on adding the records
  remote's URLs to `private_remotes`, the only edit it makes to your config.

This protects against accidents — a stray invocation, a script, a test that
reaches a real repo. It does not stop a determined process running as you:
anything that passes `--yes`, edits the config, or runs `git push` itself
can still push. Optional hardening, outside pubrepo: keep the publishing
credential behind something that requires your presence.

## CI integration

| Exit | Meaning |
|---|---|
| 0 | success / up to date |
| 1 | config or usage error; a missing confirmation or explicit choice |
| 2 | git, network or host error (including "host refused the push: <reason>") |
| 3 | another pubrepo command holds the lock |
| 4 | the scrub gate blocks (forbidden string, unreadable file, conflict markers); for `repair`, applied but still public |
| 5 | the public repo has commits to pull, or your source checkout needs updating |
| 6 | your changes are pending (`status --check`) |
| 7 | a release failed or is incomplete (release commands only) |
| 8 | a decision is required (pull items, a pull in progress, a diverged source snapshot) |
| 130 | interrupted |

`status --check` precedence: **4 > 8 > 5 > 6 > 0**. Codes 1, 2 and 3 abort
before any state is evaluated (includes are validated first). Offline,
`status --check` and previews use the last fetched state, so **CI should use
`status --check --remote`**. While another pubrepo command is running,
`status` skips the lock and the fetch and reports the last known state,
labelled.

CI needs a full clone of the source. A shallow checkout exits 1 with
"full source history required" (GitHub Actions: `fetch-depth: 0`). The
first run in a fresh clone fetches pubrepo's records from the records
remote.

GitHub Actions gate — fail on a leak, unpulled public commits, or an open
decision; your own pending changes are fine:

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }
- name: pubrepo gate
  run: |
    pip install pubrepo
    pubrepo init
    pubrepo status --check --remote || [ $? -eq 6 ]
```

Publishing from CI (with push access to the public remote configured):

```yaml
- name: publish
  run: pubrepo publish --yes   # with release management enabled, add --no-release or --bump …
```

A publish that finds commits to pull exits 5 and waits for a human to run
`pubrepo pull`.

Pre-push hook in the source repo — block pushes while a leak exists, but
not because the public repo has unpulled commits (offline, last fetched
state):

```bash
#!/bin/sh
pubrepo --dry-run --quiet
case $? in 0|5|8) exit 0 ;; *) exit 1 ;; esac
```

`--json` gives machine-readable output on `status`, `validate`,
`--dry-run`, and `--stage`; with it, stdout carries only the JSON document.

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

Every field, type, default, and validation rule: [docs/config.md](https://github.com/brenton-keller/pubrepo/blob/v1.1.0/docs/config.md).
`pubrepo validate` reports every config and environment finding at once.

## FAQ

**Multiple public repos from one source?** One config per target:
`.publish-sdk.toml` with `dir = ".publish-sdk"`, then
`pubrepo publish --config .publish-sdk.toml`. Targets are isolated by
construction — neither can ship the other's workdir or config.

**Non-GitHub remotes?** Any git URL — GitLab, Bitbucket, self-hosted.

**Rollback?** Commit the reverting change in your source and publish it.
To fix a just-published mistake without a new commit, use
`pubrepo --amend`. To remove content from public history, use
`pubrepo repair`.

**Someone pushed commits to my public repo — now what?** `pubrepo pull`.
See [Collaborators](#collaborators-pull) and
[docs/integrating-changes.md](https://github.com/brenton-keller/pubrepo/blob/v1.1.0/docs/integrating-changes.md).

**What happened to `--force-overwrite`?** Removed in 1.1.0: publish never
rewrites public history. It exits 1 and points to `pull` and `repair`.

**Why was my publish blocked?** Run `pubrepo --dry-run` — it shows every
finding and the state publish would refuse; `pubrepo validate` checks the
config itself.

**Does `--dry-run` check for divergence?** Offline, it uses the last
fetched state and says so. Add `--remote` to fetch first.

**I published from another branch (or rebased) and now it asks about a
"diverged" snapshot.** pubrepo names the snapshot and the managed paths
that would change. Confirm on a terminal, or pass the exact
`--accept-source-divergence=<sha>` flag the message prints.

**What do `.gitattributes` line-ending and filter settings do?** Files are
published as git stores them: with `* text eol=crlf`, the public repo gets
LF blobs. Paths with a `filter` attribute (git-lfs, git-crypt) are published
as their raw working-tree bytes, as in 1.0.

**I committed or edited files by hand in `.publish/`.** Publish builds
on the public tip, so commits that exist only in `.publish/` are never
published: pubrepo saves them under `refs/pubrepo/local/<timestamp>` in
`.publish/` and lists them in a warning (`repair` does the same). Hand
edits to files in `.publish/` are discarded when pubrepo refreshes it,
except `keep` files.

**Flags before the command?** Put them after: `pubrepo status -v`, not
`pubrepo -v status` (a leading flag means `publish`).

**What's the difference between `--stage` and `--dry-run`?** `--dry-run`
reports what would be published without touching `.publish/`. `--stage`
builds the snapshot on disk in `.publish/` for hands-on inspection, but
stops before commit and push.

**Windows?** Best effort: the locks are no-ops on Windows and CI doesn't
test it. Confirmation works the same (`[y/N]` on a terminal, `--yes`
otherwise).

## Meta

This repository is published by pubrepo itself — the manifest and forbidden
list live in the private source repo, where you can't see them. That's the
point. The first publish used the installed tool; every publish after that
uses the version being published.

License: MIT
