# Changelog

## 1.1.0

Publishing becomes a fast-forward on the public branch, and collaborators'
commits become something you pull, not something you overwrite. Read the
breaking changes before upgrading scripts or CI.

### Security
- **Commit messages are scrubbed**, as in 1.0.3. The text you give with
  `-m` (title and body), and the title `--amend` reuses when you give no
  `-m`, is checked against `[publish.scrub] forbidden` with the gate's
  case-insensitive matching. A hit exits 4. `-m` is refused before any git
  call; the reused title is refused after the fetch, before anything is
  rebuilt, committed or pushed. No log entry, record or bookmark is
  written. The error gives the number of forbidden strings and the lines
  they are on, never the strings. This covers publish, `--amend`,
  `--release` and `--bump`. 1.1.0 commits the message as given, whatever
  `commit.cleanup` says, so the checked text is the stored text.

### Changed (breaking)
- **Publish builds on the public branch instead of nuking and rebuilding
  it.** Each snapshot is committed on top of the current public tip and
  pushed as a plain fast-forward. Public files your manifest doesn't cover
  are carried forward byte for byte; pubrepo changes or removes a public file
  only when it verifies it wrote the current content, or when you recorded a
  decision with `pull`.
- **Commits made on the public repo are pulled, not overwritten.** Publish
  refuses with exit 5 ("run `pubrepo pull`") while the public branch has
  commits you haven't pulled.
- **`--force-overwrite` is removed.** It exits 1 before doing anything and
  points to `pull` and `repair`.
- **`on_republish = "amend"` is retired** and is a config error; only
  `"new_commit"` is accepted. `--no-amend` is a no-op with a note.
- **`--amend` is an explicit, confirmed rewrite of your own last snapshot.**
  It refuses a tip that isn't a verified pubrepo snapshot, a merge, a tagged
  tip, or a state with anything to pull or decide. On a terminal it shows
  the consequences and the recovery steps for collaborators; off a terminal
  it needs `--yes`.
- **Bare `pubrepo` shows `status`, and commands that push confirm** — the
  same change as 1.0.1, now covering every command that pushes (`publish`
  and its flag forms, `--amend`, `--release`/`--bump`, `repair --apply`,
  `repair restore-ancestry`). Off a terminal they need `--yes` (repair:
  `--yes --expect-tip --expect-plan`).
- **Binary files no longer skip the scrub gate.** Files pubrepo cannot scan
  block the publish (exit 4) unless listed in `[publish.scrub]
  allow_opaque`. Office files (OOXML) are opened and scanned, part by part.
  Conflict-marker triples in published content block unless listed in
  `allow_markers`.
- **Exit codes.** 5 now also covers a non-fast-forward publish and every
  "pull first" state. New 8: a decision is required. 7 is release-only.
  `repair` exits 4 when applied but still reachable.
- **`--dry-run` predicts publish's exit code** (0, 4, 5 or 8), from the last
  fetched state unless `--remote` is given. The pre-push hook and GitHub
  Action recipes in the README are updated: a plain `pubrepo --dry-run`
  gate now also fails while the public repo has unpulled commits.
- **`status`** reports a foreign range as "unpulled", not "DIVERGED", and
  its JSON gains `remote_checked`, `remote_last_fetched`, `unpulled`,
  `items` and `carried_count`. `status --check` precedence is
  4 > 8 > 5 > 6 > 0. `status` and `status --check` validate `include` first
  (exit 1). While another pubrepo command runs, `status` reports the last
  known state, labelled, instead of waiting.
- **`integrate` is an alias for `pull --plan`**; the copy-paste recipe is
  gone.
- **Public commit messages** no longer carry the `Branch:` line or the
  ` (dirty)` marker. The `Source:` trailer is unchanged.
- **Line endings.** Files are published as git stores them: with
  `* text eol=crlf`, publish now stores LF blobs where it published CRLF
  bytes before. Paths with a `filter` attribute keep the raw-bytes behavior.
- **`--stage`** writes exactly the tree publish would commit (on the last
  integrated state when there are unpulled commits). Its JSON `baseline.ref`
  names that git tree.
- **Publishing from another source branch** (or after a rebase that dropped
  the published source commit) asks for confirmation, or
  `--accept-source-divergence=<sha>`.
- **The first 1.1.0 publish** after earlier publishes with uncommitted or
  untracked files may ask a one-time set of questions about changes pubrepo
  cannot verify (`pubrepo pull --baseline`).
- **pubrepo now writes to your source repo:** record refs under
  `refs/pubrepo/*`, a lock file in the git directory, and (from `pull`) pull
  commits. Records sync only to a listed private remote (next entry).
- **Private refs go only to listed private remotes.** The bookmark, the
  release source tag and pubrepo's records point at private source commits.
  pubrepo pushes them only by remote name — `tag_remote`,
  `release.source_remote`, and `records_remote` or else git's push remote
  for the branch (`pushRemote`, `pushDefault`, upstream; no `origin`
  fallback) — and only when every URL git may use for that remote is
  exactly an entry of `[publish] private_remotes`. Even a listed remote is
  refused if it sets `receivepack`, `uploadpack`, `vcs` or `mirror`
  (`mirror` is new since 1.0.2), if a value is an `ext::` or `fd::` remote,
  or if it is a publish remote, however written. A refused bookmark or
  records sync is a warning naming each value to add, and the first refused
  records sync in a clone also prints the exact `private_remotes = […]`
  line; the exit code is unchanged. On a terminal, `pubrepo init` offers to
  add that line for the records remote (default No; off a terminal it
  prints it). A refused source tag stops the release
  (exit 7) before it contacts that remote. `tag_remote`, `records_remote`
  and (with `source_tag`) `release.source_remote` must be remote names, not
  URLs (exit 1).
- **No `published/*` tags.** As in 1.0.2, a clean publish or a release
  records its source commit as the local ref
  `refs/pubrepo/bookmarks/<timestamp>`, pushed only to `tag_remote` (as
  `refs/tags/published/<timestamp>`). 1.0.0 and 1.0.1 created a
  `published/<timestamp>` tag and pushed it to your source repo's `origin`;
  `pubrepo validate` lists leftover local tags, and `validate --remote`
  lists them on remotes, with the cleanup commands.
- **The publish clone is checked before anything is pushed.** Publish,
  `--amend`, releases, `repair` and `repair restore-ancestry` exit 2 before
  fetching unless every URL of `.publish/`'s `origin` is exactly
  `[publish] remote` (or its `insteadOf` rewrite, when the fetch and push
  URLs agree). They also refuse `remote.origin.receivepack`, `uploadpack`,
  `vcs` or `mirror`, replace refs or a grafts file in `.publish/`, and a
  rewrite of the remote to one of the source repo's remotes. Each new public
  commit's parents must be this run's fetched tip, an ancestor of it, or a
  commit the run verified or created (exit 2 otherwise). See
  [docs/config.md](docs/config.md#publish-clone-origin).
- **Missing `include` paths:** the error ends "remove them from include in
  <config file>, or restore them (renamed? update include)", and names the
  pull when a pull deleted the path.
- An unreadable source file is reported as "cannot read '<path>'" (was
  "cannot copy"); the exit code is unchanged.

### Changed
- **Off a terminal, a publish without `--yes` shows what it would do.** It
  still exits 1 with the same error on stderr, and it still fetches,
  pushes and changes nothing: the preview is offline (before the first
  publish it runs one read-only check; see Limits) and writes only
  unreferenced git objects in `.publish/`. Stdout now carries that preview,
  then one last line: the exact command to run when nothing blocks
  (`pubrepo publish … --yes --expect-tip <sha>`, bound to the previewed
  tip; with release management on, `--no-release` is added), or why no
  command is offered. `--quiet` leaves only the last line. **A release is
  never offered as a `--yes` command:** for `--release` or `--bump` the
  line gives the command without `--yes`, to run on a terminal, where you
  confirm it (a PyPI release asks you to type the version). A refused
  `-m` exits 4 instead, with one `Not published: …` line on stdout.
  Scripts that expected an empty stdout here now get text; the exit code
  and stderr are unchanged.
- **On Python 3.8 to 3.10**, pubrepo prints `pubrepo requires Python
  3.11+; this is X.Y at <interpreter>` on stderr and exits 1, instead of a
  traceback.

### Added
- `pubrepo pull`: bring public commits into source under guardrails — a
  plan with a digest, explicit decisions (`--accept`, `--theirs`, `--ours`,
  `--keep`, `--delete`, `--restore`, `--resolved` for transformed files,
  `--decline`, `--leave`, `--remove`, `--accept-excluded`, or
  `--decisions FILE`), inbound checks, conflict markers with
  `--continue`/`--abort` (a conflict is recorded as resolved at
  `--continue`), one source commit authored by you with `Co-authored-by:`
  lines. Off a terminal: `pull --plan`, then
  `pull --yes --expect-tip <sha> --expect-plan <digest>`.
- `pubrepo repair` (`rewind`, `remove-path --from`, `restore-ancestry`): a
  planned, confirmed rewrite of public history that names every destroyed
  commit and every public ref still reaching the content.
  `pubrepo release-retire <tag>` records a release as retired.
- Publish flags: `--yes`, `--expect-tip SHA`, `--remote` (previews),
  `--accept-source-divergence SHA`, `--recreate-branch`.
- Config keys: `records_remote`, `peers`, `private`, `scrub.allow_opaque`,
  `scrub.allow_markers`, `release.adopt_tags`, `release.strip_env`; and, as
  in 1.0.2, `tag_remote`, `private_remotes`, `public_hosts`.
- `validate` reports private refs: offline, local `published/*` tags and
  each refused `tag_remote`, `records_remote` or `release.source_remote`;
  with the new `validate --remote`, also `published/*` tags and
  `refs/pubrepo/*` refs on the publish remotes and on source remotes that
  fail the private-refs check, with the delete commands. A remote with
  `receivepack`, `uploadpack`, `vcs` or `mirror` set is reported, never
  contacted. Warnings only; pubrepo never deletes a remote ref.
- `--verbose` prints the push plan of every command that pushes, before its
  first push: `push plan (<kind>): <P|S> <ref> → <remote>`.
- Release-aware publishing (opt-in, `[publish.release]`): `--release
  VERSION`, `--bump patch|minor|major` or `--no-release` on every real
  publish; managed annotated tags (`Pubrepo-Release:`/`Pubrepo-Source:`
  trailers, or `adopt_tags`); resumable journals; `release-abandon`;
  read-only `validate --release-check`; release flags `--retry` (at most
  3 attempts after the public branch moved), `--accept-unscanned PATH` and
  `--accept-build-changes`; optional GitHub (`gh`) and PyPI
  (`python -m build`/`twine`) adapters; exit 7. A release is a snapshot on
  the public tip, confirmed once before the build (a PyPI release asks you to
  type the version).
- **`--dry-run -m` shows the real commit message.** "Commit message:" is
  the text publish would commit for the same tree. `--dry-run --json` adds
  `commit_message` (null when something blocks) and, when `-m` is given,
  `scrub.message` (`count`, `lines`). With a forbidden string in `-m`,
  `--dry-run` exits 4, even where it would otherwise predict 5 or 8,
  because publish refuses `-m` first. `--diff -m` exits 4 too, with the
  error on stderr; its stdout stays a plain diff.

### Fixed
- Previews compare against git trees, not the `.publish/` working tree.
- `--amend` no longer warns that the public commit it replaces was "never
  published" and no longer saves it under `refs/pubrepo/local/`. Commits
  that exist only in `.publish/` are still saved and listed.
- Sources whose `.publish.toml` sits in a subdirectory of their git
  repository work as in 1.0.x: paths are resolved against that directory,
  files outside it are never published, and `pull` works there too.
- When `pull` stops because items need a decision (exit 8), the example
  flag it prints is one that the named item offers, and it says to run
  `pull --plan` (with `--baseline` for baseline items) to see each item's
  options.
- An up-front `--resolved` for a text conflict, given as a flag, a
  decisions-file line or with `pull --plan`, exits 1 before anything is
  written and names the item's other options. A conflict is resolved by
  editing the file, then `pull --continue`. Transformed files still take
  `--resolved` up front.

### Limits (1.1.0)
The private-refs check and the publish-clone checks compare URL strings and
check commit parents. None of the cases below arises from normal
configuration: each needs transport, object, hook or environment
configuration set on purpose, or is an assertion you make in
`private_remotes`. "Not tested" marks a trigger the 1.1.0 review did not
try. A private push below means the bookmark, records or the release source
tag going to a listed remote.

- **Custom ssh command.** Trigger: `core.sshCommand` (in the source repo or
  `.publish/` config), or `GIT_SSH_COMMAND` / `GIT_SSH` in the environment,
  naming a command or `-F` config whose `HostName` or `ProxyCommand`
  connects somewhere other than the URL and the default ssh config say.
  Effect: a private push to a listed value, or the public push, reaches a
  different host.
- **ssh config `Host` override.** Trigger: a `Host` block in
  `~/.ssh/config` for a listed or publish host with a different
  `HostName`, `ProxyJump` or `ProxyCommand`. Effect: the same; the
  allowlist and the publish-clone origin check compare URL strings, without
  `ssh -G`. One integrity case: `Host github.com` pointing at a private
  host, plus a global `insteadOf`, can send public content to a
  source-side repository.
- **git:// proxy command.** Trigger: `core.gitProxy` or
  `GIT_PROXY_COMMAND`, for a `git://` value. Effect: the same, for
  `git://`.
- **DNS or curl overrides.** Trigger: `/etc/hosts`, `http.curloptResolve`
  or `http.<url>.curloptResolve` (not tested), or `http.<url>.proxy` to a
  different host, together with `http.sslVerify=false`. Effect: a listed
  private hostname resolves to a public forge.
- **Hooks.** Trigger: a `pre-push` hook, or `core.hooksPath`, in the source
  repo or `.publish/` (pubrepo's pushes don't pass `--no-verify`; not
  tested). Effect: the hook runs on every push and can itself push
  anywhere.
- **Exported git environment.** Trigger: `GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY` or `GIT_NAMESPACE` exported when
  running pubrepo (not tested). Effect: git ignores the working directory,
  so checks and pushes act on another repository. This applies to every
  pubrepo version.
- **Hand-made annotated tags.** Trigger: `push.followTags=true` and an
  annotated tag created by hand in `.publish/` on a commit the pushed
  snapshot reaches, and that the remote doesn't have (not tested; a tag on
  a private commit was tested and isn't pushed). Effect: the tag object,
  including its message, is pushed with the snapshot.
- **Grafts outside `.publish/`.** Trigger: `GIT_GRAFT_FILE` in the
  environment, pointing outside `.publish/.git` (not tested; a grafts file
  inside it is refused). Effect: may change the parents git reports.
- **Shallow file or commit-graph.** Trigger: a hand-written
  `.publish/.git/shallow`, or a forged commit-graph file (not tested,
  except one shallow shape, which is refused). Effect: changes the parents
  git reports, and a push won't send past a shallow boundary.
- **Alternates.** Trigger: `.publish/.git/objects/info/alternates` or
  `GIT_ALTERNATE_OBJECT_DIRECTORIES` pointing at a private object store.
  Effect: private objects become available; the parent check and the push
  still follow commits reachable from the snapshot, and no leak was shown.
- **Listed spellings of the publish repo.** Trigger: you list the publish
  repository in `private_remotes` under a fullwidth or IDNA spelling, or an
  ssh alias `ssh -G` can't resolve. Effect: pubrepo can't tell it is the
  publish repository, and the bookmark or records go there.
- **A listed repo made public.** Trigger: a repository in
  `private_remotes` is later made public on its host. Effect: later
  bookmarks and records are public.
- **Out-of-tree configs.** Trigger: a target whose `.publish.toml` lives
  outside the repo and has never run on this clone. Effect: its remote
  isn't in the set of publish remotes the publish-repo check compares
  against; the allowlist still gates every private push.
- **Replace refs or grafts in the source repo.** Trigger: `refs/replace/*`
  refs or an `info/grafts` file in your source repository (not tested).
  Effect: integrity only. pubrepo's reads of source history, such as pull's
  and the release export's, follow the replacement; the export and the
  scrub see the same view, and public commits are built in `.publish/`,
  which ignores replace objects.
- **Read-only commands and `.publish/`'s origin.** Trigger:
  `remote.origin.uploadpack` or `remote.origin.vcs` (`vcs` not tested) set
  by hand in `.publish/.git/config`. Effect: `status --remote`,
  `pull --plan`, `pull`, `validate --release-check`, and, with release
  management enabled, every publish's discovery (`--release`, `--bump`,
  `--no-release`, `--amend`) contact `origin` with that program before
  anything refuses it, so the program runs; the publish or release then
  exits 2 before its first write. Nothing is pushed. With an `uploadpack`
  that redirects to another repository, `pull` can bring that repository's
  history into your source (integrity only).
- **Other transport keys.** Trigger: none known. `receivepack`,
  `uploadpack`, `vcs` and `mirror` are refused on the remotes that receive
  private refs, and on `.publish/`'s `origin` before any push: with
  release management disabled, publish and `--amend` refuse them before
  they contact `origin`; with it enabled, every publish's discovery has
  already contacted `origin` (entry above), and the refusal comes before
  the first write. The `remote.<name>.*` keys were checked against
  `git help --config`.

The commit-message check (Security, above) has two limits, both in normal
use:

- **The generated part of the message is not scanned:** the
  `publish: <time>` default title and the `Source:`, `Files:`,
  `Transforms:` and `Scrub:` lines.
- **Rewritten and restored history.** `repair` carries the messages of the
  commits it rewrites over unchanged, and `repair restore-ancestry` writes
  a message that names the public tag; neither is scanned.

Previews and the off-terminal publish have two limits, both in normal use:

- **Previews before the first publish.** Trigger: after `pubrepo init` and
  before the first publish, `--dry-run`, `--diff`, `--stage`, `status` and
  the off-terminal publish preview. Effect: each runs
  `git ls-remote --heads origin` in `.publish/`, and
  `git ls-remote --symref origin HEAD` when the remote has branches, to
  check the host's default branch. Both calls are read-only and go to the
  `[publish] remote`. As in the "Read-only commands" entry above, a
  `remote.origin.uploadpack` or `vcs` set by hand in `.publish/` would run
  here too (not tested on this path).
- **`--recreate-branch` on a branch that still exists.** Trigger: off a
  terminal, `publish --recreate-branch` while the public branch still
  exists and publish would refuse: for example, the last fetch saw public
  commits you haven't pulled, the source no longer holds the published
  source commit (another branch, or a rebase), or there are baseline items
  to decide. Effect: the preview shows a new branch (`Baseline: (empty)`),
  and its `Action:` line says "if the branch still exists, publish
  fast-forwards instead", which doesn't hold here. The last line offers
  `pubrepo publish --recreate-branch --yes` (ending in `--no-release` with
  release management on). That command exits 5 or 8 without writing
  anything, and its error names the next step (`pubrepo pull`,
  `--accept-source-divergence=<sha>`, or `pubrepo pull --baseline`).

## 1.0.3

Security fix: commit messages are scrubbed.

### Security
- **Commit messages were never scrubbed.** 1.0.0–1.0.2 checked every
  published file against `[publish.scrub] forbidden`, but not the commit
  message: a forbidden string in `-m` (title or body), in the title an
  `--amend` (or `on_republish = "amend"`) reuses, or in the source branch
  name (the `Branch:` line) was published.
- **Now:** that text is checked with the same case-insensitive matching,
  both as given and as git stores it after whitespace cleanup. A hit exits
  4 before anything is rebuilt, committed or pushed, and no log entry or
  bookmark is written. `-m` is checked before anything is fetched. The
  error gives the number of forbidden strings and the lines they are on,
  not the strings. `--dry-run` checks `-m` and the
  branch name too, and exits 4 where publish would.
- An `--amend` whose reused title contains a forbidden string exits 4 even
  when nothing else changed: pass `-m` with a new title, or `--no-amend`.
- A publish from a branch whose name contains a forbidden string exits 4,
  even when nothing else changed: publish from another branch. The README's
  pre-push hook (`pubrepo --dry-run`) therefore also blocks pushes from such
  a branch.
- Commits are made with `--cleanup=whitespace` whatever `commit.cleanup`
  says: lines starting with `#` in `-m` are kept even when your config strips
  them, and trailing whitespace and extra blank lines are cleaned even when it
  says `verbatim`.

### Limits (1.0.3)
- The generated part of the message is not scanned: the `publish: <time>`
  default title and the `Source:`, `Files:`, `Transforms:` and `Scrub:`
  lines.
- `--diff` does not show or check the `-m` text; a real publish and
  `--dry-run -m` check it.

## 1.0.2

Security fix: pubrepo no longer pushes private source history.

### Security
- **The bookmark tag leaked private history.** After each clean publish,
  1.0.0 and 1.0.1 created the tag `published/<timestamp>` in your source repo
  and pushed it to the source repo's `origin`. That tag points at your
  private source commit, so pushing it published the whole private history
  reachable from that commit: every source commit, the `.publish.toml`
  config (with its forbidden-string list) and every committed file that was
  never published. If `origin` was the public repo itself, or any repo
  others can read, that history is public.
- **Now:** pubrepo no longer creates `published/*` tags. A clean publish
  records the bookmark as the local ref `refs/pubrepo/bookmarks/<timestamp>`,
  which `git push --tags`, `push.followTags` and default refspecs never
  send. It is pushed **only** to `[publish] tag_remote`, which names a
  remote of your source repo, and then as `refs/tags/published/<timestamp>`.
  `[publish] private_remotes` is an **allowlist**: the bookmark is pushed
  only if every URL git may use for that remote — each `url` and `pushurl`
  as written, and each value with `insteadOf`/`pushInsteadOf` applied — is
  exactly (byte for byte) an entry of `private_remotes`. There is no
  normalization or host parsing in that decision, and a value or entry with
  surrounding whitespace is refused. Even when listed, the push is refused
  if the remote sets `receivepack`, `uploadpack` or `vcs`, if a value is an
  `ext::` or `fd::` remote, or if a value (or the address inside a
  transport helper) is a publish remote of the repo or the same repository
  written another way. `tag_remote` must be a plain remote name. A refusal
  is a one-line warning that names each unlisted value to add, and a failed
  push is a one-line warning too; either way the publish still succeeds.
- **The publish clone pushes only to `[publish] remote`.** Before
  publishing, every URL of `.publish/`'s `origin` must equal
  `[publish] remote`, or its `insteadOf` rewrite when the fetch and push
  URLs are the same, as exact strings. For a relative local `remote`, only
  the absolute path `init` stored is accepted; the relative string itself
  is refused, because git resolves it from inside `.publish/`. Otherwise
  pubrepo exits 2 ("publish clone's origin does not match [publish]
  remote") before fetching, and prints the `git remote set-url` fix (with
  the absolute path for a relative `remote`), or, if `.publish/` was
  cloned from a different repository, says to re-create it with
  `pubrepo init`. Also refused with exit 2:
  - an `insteadOf` rewrite of `[publish] remote` to a repository one of the
    source repo's remotes points at, however either is spelled, unless the
    rewrite is the publish repository itself;
  - `remote.origin.receivepack`, `uploadpack` or `vcs` set in `.publish/`;
  - replace refs (`refs/replace/*`) or a grafts file in `.publish/`.
    pubrepo also ignores replace objects in every git command it runs
    there.
- **The publish clone pushes only public history.** Before committing a
  snapshot, every commit in `.publish/` that isn't on the public branch as
  fetched in that run must be a snapshot pubrepo made and logged. Otherwise
  pubrepo exits 2 and pushes nothing ("… did not come from [publish]
  remote …; Re-create it: move .publish/ aside and run `pubrepo init`").
  This catches a `.publish/` that was once cloned from the private repo.
- **Check for leaks:** `pubrepo validate` (offline) lists the local
  `published/*` tags that 1.0.0 and 1.0.1 created (with the `git tag -d …`
  command to delete them, since `git push --tags` would still send them).
  `pubrepo validate --remote` also lists any `published/*` tag or
  `refs/pubrepo/*` ref on the publish remotes, and on source remotes that
  can't receive private refs ("not listed in private_remotes; if this
  remote is public, these refs expose private history"), with the delete
  commands. They are reported as warnings; `validate` never deletes
  anything.
- **Manual cleanup** (pubrepo deletes nothing):
  - local tags: `git tag -d $(git tag -l 'published/*')`;
  - each remote `validate --remote` names: for example
    `git push origin --delete $(git ls-remote --tags origin 'refs/tags/published/*' | cut -f2)`.

  Deleting the tags does not erase objects a host has already served or
  cached: if private history was public, treat its contents (hosts, IPs,
  paths, credentials) as disclosed, and ask the host to purge cached
  objects where it offers that.

### Limits
These checks compare URL strings and read `.publish/`'s history. Each case
below needs transport, object, hook or environment configuration set on
purpose, or is an assertion you make in `private_remotes`. "Not tested"
marks a trigger the 1.0.2 review did not try.

- **Custom ssh command.** Trigger: `core.sshCommand` (in the source repo or
  `.publish/` config), or `GIT_SSH_COMMAND` / `GIT_SSH` in the environment,
  naming a command or `-F` config whose `HostName` or `ProxyCommand`
  connects somewhere other than the URL and the default ssh config say.
  Effect: a bookmark push to a listed value, or the public push, reaches a
  different host.
- **ssh config `Host` override.** Trigger: a `Host` block in
  `~/.ssh/config` for a listed or publish host with a different
  `HostName`, `ProxyJump` or `ProxyCommand`. Effect: the same; the
  allowlist and the publish-repo exemption compare URL strings, without
  `ssh -G`. One integrity case: `Host github.com` pointing at a private
  host, plus a global `insteadOf`, can send public content to a
  source-side repository.
- **git:// proxy command.** Trigger: `core.gitProxy` or
  `GIT_PROXY_COMMAND`, for a `git://` value. Effect: the same, for
  `git://`.
- **DNS or curl overrides.** Trigger: `/etc/hosts`, `http.curloptResolve`
  or `http.<url>.curloptResolve` (not tested), or `http.<url>.proxy` to a
  different host, together with `http.sslVerify=false`. Effect: a listed
  private hostname resolves to a public forge.
- **Hooks.** Trigger: a `pre-push` hook, or `core.hooksPath`, in the source
  repo or `.publish/` (not tested). Effect: the hook runs on every push and
  can itself push anywhere.
- **Exported git environment.** Trigger: `GIT_DIR`, `GIT_WORK_TREE`,
  `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY` or `GIT_NAMESPACE` exported when
  running pubrepo (ordinary hooks don't set them; not tested). Effect: git
  ignores the working directory, so checks and pushes act on another
  repository. This applies to every pubrepo version.
- **Grafts outside `.publish/`.** Trigger: `GIT_GRAFT_FILE` in the
  environment, pointing outside `.publish/.git` (not tested). Effect: may
  change the parents git reports.
- **Shallow file or commit-graph.** Trigger: a hand-written
  `.publish/.git/shallow`, or a forged commit-graph file (not tested,
  except one shallow shape, which is refused). Effect: changes the parents
  git reports, and a push won't send past a shallow boundary.
- **Alternates.** Trigger: `.publish/.git/objects/info/alternates` or
  `GIT_ALTERNATE_OBJECT_DIRECTORIES` pointing at a private object store.
  Effect: private objects become available; the history check and the
  push still follow commits reachable from the branch, and no leak was
  shown.
- **Publish-log edits.** Trigger: adding full shas of private commits as
  `snapshot_commit` / `public_commit` to `.publish/.git/pubrepo-log.jsonl`
  by hand. Effect: the history check treats them as pubrepo's own
  snapshots.
- **Hand-made annotated tags.** Trigger: `push.followTags=true` and an
  annotated tag created by hand in `.publish/` on a commit being pushed
  (not tested). Effect: the tag object, including its message, is pushed
  with the snapshot.
- **Listed spellings of the publish repo.** Trigger: you list the publish
  repository in `private_remotes` under a fullwidth or IDNA spelling, or an
  ssh alias `ssh -G` can't resolve. Effect: pubrepo can't tell it is the
  publish repository, and the bookmark goes there.
- **A listed repo made public.** Trigger: a repository in
  `private_remotes` is later made public on its host. Effect: later
  bookmarks are public.
- **Out-of-tree configs.** Trigger: a target whose `.publish.toml` lives
  outside the repo and has never run on this clone. Effect: its remote
  isn't in the set of publish remotes the listed-value check compares
  against; the allowlist still gates every bookmark push.
- **Other transport keys.** Trigger: none known. `receivepack`,
  `uploadpack` and `vcs` are refused on both the bookmark's remote and
  `.publish/`'s `origin`, and the `remote.<name>.*` keys were reviewed
  against git 2.53.

### Upgrade note
- A `.publish/` clone made by 1.0.0 or 1.0.1 that holds a local commit that
  was never pushed, or pubrepo snapshots that are older than its publish log
  and not on the public branch, now makes publish exit 2 (see above). Move
  `.publish/` aside and run `pubrepo init` to re-create it.

### Added
- `[publish] tag_remote`: the name of a source remote that receives the
  bookmark (no default; never inferred from `origin`).
- `[publish] private_remotes`: the exact URLs that may receive the bookmark.
  Making a listed repo public later exposes what was pushed to it.
- `[publish] public_hosts`: extra hosts named as public in refusal messages.

## 1.0.1

Safety fix: an accidental invocation can no longer publish.

### Changed (breaking)
- Bare `pubrepo` (no arguments) now shows `status` and the hint
  "To publish: `pubrepo publish`". It used to publish. Scripts that ran bare
  `pubrepo` to publish must run `pubrepo publish --yes`.
- Publishing asks for confirmation. This covers `pubrepo publish`, the
  flags-only forms that push (`-m`, `--amend`, `--no-amend`, `--quiet`,
  `--force-overwrite`, …) and `on_republish = "amend"`. On a terminal
  (stdin and stderr both a TTY), pubrepo prints the `--dry-run` preview and
  the action (new commit, amend, or force-overwrite), then asks
  `Publish? [y/N]`; any answer other than `y` or `yes` exits 1 ("publishing
  cancelled; nothing changed"). Off a terminal, publishing needs `--yes`;
  without it, pubrepo exits 1 ("publishing needs confirmation: run on a
  terminal, or pass --yes") before fetching or writing anything. Scripts
  and CI that publish off a terminal must add `--yes`. `--quiet` does not
  suppress the prompt.

### Added
- `--yes`: publish without the confirmation prompt.
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

### Fixed
- Transform target paths are validated at config load: absolute paths and
  paths that escape the publish directory (via `..`) are rejected; overlap
  with `keep` entries (exact or nested) is rejected.

### Unchanged
- `--dry-run`, `--diff`, `--stage`, `status`, `validate`, `init` and
  `integrate` never ask.

## 1.0.0

First public release as **pubrepo** (previously an internal publishing tool).

### Added
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
