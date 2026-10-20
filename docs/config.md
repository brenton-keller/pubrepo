# .publish.toml reference

pubrepo reads its manifest from `./.publish.toml` (override with
`--config PATH`). `pubrepo validate` checks everything below and reports all
findings at once; warnings never block, errors exit 1.

```toml
[publish]
remote  = "https://github.com/you/repo.git"  # REQUIRED. Any git URL (GitHub/GitLab/Bitbucket/self-hosted).
branch  = "main"                             # default "main"
dir     = ".publish"                         # default ".publish"; must be ".publish" or ".publish-<name>"
include = ["src/", "pyproject.toml"]         # REQUIRED, non-empty. Literal paths only — never patterns.
exclude = ["src/secret.py", "*.snap"]        # default []. Three-rule patterns (below).
keep    = ["LICENSE"]                        # default []. Top-level names only (no "/"); owner-managed
                                             # in .publish/; scrubbed like everything else.
records_remote = "origin"                    # a remote NAME; default: the branch's push remote, no "origin" fallback.
tag_remote = "origin"                        # a remote NAME; no default: without it the bookmark stays local.
private_remotes = ["git@nas.example.com:you/private-src.git"]  # default []. The allowlist: exact URLs (here, origin's).
public_hosts = ["git.example.com"]           # default []. Only changes how refusals are worded.
peers   = ["../sdk/.publish-sdk.toml"]       # default []. Other publish configs pull should know about.
private = ["clients/", "notes/*.md"]         # default []. Paths pull must never write into source.

[publish.scrub]
forbidden     = ["acme-internal", "10.1.2."] # default []. Literal, case-insensitive, substring.
                                             # Any hit BLOCKS the publish (exit 4).
allow_opaque  = ["docs/*.gif"]               # default []. Binaries that may ship unscanned (globs).
allow_markers = ["docs/merge-howto.md"]      # default []. Paths allowed to contain conflict markers.

[publish.transforms]
"pyproject.toml" = [
  { find = "private-name", replace = "public-name" },              # literal, all occurrences
  { strip_between = ["<!-- INTERNAL -->", "<!-- /INTERNAL -->"] }, # inclusive removal, all occurrences
]

[publish.release]
enabled = true
version_file = "src/package/__init__.py"
version_pattern = '^__version__\s*=\s*"(?P<version>[0-9A-Za-z.+-]+)"$'
tag_format = "v{version}"
source_tag = true
source_remote = "origin"
adopt_tags = ["v1.0.0"]                      # tags made before pubrepo managed releases
strip_env = ["MYCORP_*", "EXTRA_TOKEN"]      # extra variables to remove from the build environment

[publish.release.github]
enabled = true
repository = "owner/repo"
assets = ["bin/tool"]
checksum_file = "SHA256SUMS"
notes_file = "CHANGELOG.md"
include_distributions = true

[publish.release.pypi]
enabled = true
package = "package-name"
repository = "pypi" # v1: "pypi" or "testpypi" only
```

## Field rules

| Field | Type | Validation |
|---|---|---|
| `remote` | str | required, non-empty; warning if it doesn't look like a git URL |
| `branch` | str | non-empty (default `main`) |
| `dir` | str | `.publish` or `.publish-<name>`; single path component, non-empty name |
| `include` | list[str] | required, non-empty; entries are literal paths that must exist; `.git`/`.publish*`/the config file are rejected; including the source root (`.`) warns |
| `exclude` | list[str] | three-rule patterns; empty strings and match-all `*` warn |
| `keep` | list[str] | no `/` (top-level only) |
| `records_remote` | str | the name of a git remote of your source repo, never a URL; default: git's push remote for the branch (see [Records](#records)). It must be a plain, existing remote name — non-empty, no `/`, `\` or `:`, not `.` or `..`, no leading `-`, no surrounding whitespace — or validation fails (exit 1): "'records_remote' must name a git remote of the source repo (…)" |
| `tag_remote` | str | the name of a git remote of your source repo, with the `records_remote` rules; no default, never inferred from `origin`. Whether it may receive the bookmark is decided at publish time (see [Bookmark](#bookmark)) |
| `private_remotes` | list[str] | non-empty strings, no leading or trailing whitespace (an error); the exact URLs that may receive private refs (see [Private refs](#private-refs-and-private_remotes)). Listing a publish repo, or an `ext::`/`fd::` remote, never permits a push. Making a listed repo public later exposes what was pushed to it |
| `public_hosts` | list[str] | non-empty strings; hosts added to the built-in public-host list, which only changes how a refusal is worded |
| `peers` | list[str] | non-empty strings; paths (relative to the source root) of other publish configs |
| `private` | list[str] | non-empty strings; glob patterns on source-relative paths; a pattern also covers everything below it |
| `on_republish` | str | only `"new_commit"` is accepted; `"amend"` was retired in 1.1.0 and is an error |
| `scrub.forbidden` | list[str] | literal strings; empty strings warn |
| `scrub.allow_opaque` | list[str] | non-empty strings; globs on the published path; parts inside a scanned archive match as `<container>!<part>`, and release distribution members as `<dist>!<member>` |
| `scrub.allow_markers` | list[str] | non-empty strings; globs on the published path |
| `transforms` | table | keys are file paths in the publish set (else warning); each rule has exactly one of `find`+`replace` (both str, `find` non-empty) or `strip_between` (two non-empty strs); see path validation below |
| `release.enabled` | bool | default false; see [Release settings](#release-settings) |
| `release.version_file` | str | required when enabled; source-relative, inside the publish set, exactly one pattern match |
| `release.version_pattern` | str | Python regex with exactly one named `version` group; applied with `re.MULTILINE` |
| `release.tag_format` | str | default `v{version}`; exactly one `{version}` and a valid git tag |
| `release.source_tag` | bool | default false; create the same annotated formal tag on the exact clean source commit |
| `release.source_remote` | str | default `origin`; with `source_tag`, the name of an existing git remote of your source repo (the `records_remote` rules); a release stops (exit 7) unless every URL of it is listed in `private_remotes` (see [Private refs](#private-refs-and-private_remotes)) |
| `release.adopt_tags` | list[str] | non-empty strings; existing tags to treat as managed releases |
| `release.strip_env` | list[str] | each an environment variable name, optionally ending in one `*` as a prefix wildcard (`FOO_*`); a bare `*` is refused |
| `release.github` | table | optional `gh` adapter: repository identity must match the publish remote |
| `release.pypi` | table | optional build/twine adapter; package required; repository is only `"pypi"` or `"testpypi"` in v1 |

Unknown keys anywhere produce warnings, never errors (forward
compatibility).

Globs (`allow_opaque`, `allow_markers`, `private`) use shell-style
matching on the whole path, case-sensitive; `*` also matches `/`.

## Missing include paths

Every `include` entry must exist. Publish, the previews, `status` and
`status --check` all check this first and exit 1:

- if a `pubrepo pull` deleted the path, the error names that pull:
  "include '<path>' names a path removed by pull <sha>; remove it from
  include in <config file>";
- otherwise it lists the missing paths and ends "remove them from include in
  <config file>, or restore them (renamed? update include)".

`<config file>` is the config you loaded, so a `.publish-<name>.toml` target
names itself. Pull never edits your config.

## Scrub settings

`forbidden` strings are matched literally and case-insensitively in every
file pubrepo publishes (after transforms) and in `keep` files. They are
also matched in the commit message you give with `-m`, and in the title
`--amend` reuses without `-m` (exit 4; the error names the count and the
line numbers, never the strings). The generated parts of the message are
not scanned.

Binary files pubrepo cannot scan — images, PDFs, non-Office archives,
encrypted or legacy Office files — block the publish (exit 4) unless an
`allow_opaque` glob matches them. Allowlisted files are reported as
"not scanned (allowlisted)" on every run. Office files (OOXML) are opened
and scanned instead; a corrupt one blocks and cannot be allowlisted.

A conflict-marker triple (`<<<<<<< ` … `=======` … `>>>>>>> `, or the diff3
form with `|||||||`) blocks unless an `allow_markers` glob matches the path.

Public files pubrepo didn't write (carried files) are scanned too. Findings
in them are reported and never block a publish; they block a release unless
allowlisted. A forbidden string in carried content has no allowlist.

## Private refs and `private_remotes`

Three kinds of ref point at private source commits: the
[bookmark](#bookmark), the release source tag (`release.source_tag`), and
pubrepo's [records](#records) (`refs/pubrepo/*`). Anyone who can fetch one of
them can fetch your private history reachable from it, including
`.publish.toml`. pubrepo pushes them only by the **name** of a remote of your
source repo — `tag_remote`, `release.source_remote` or the records remote —
and only if that remote passes this check, which makes no network call:

- `private_remotes` is an **allowlist**. Every URL git may use for the
  remote — each `url` and `pushurl` as written, and each value of
  `git remote get-url --all` and `git remote get-url --push --all` (with
  `insteadOf` and `pushInsteadOf` applied) — must be **exactly** an entry of
  `private_remotes`, byte for byte. There is no normalization and no host
  parsing in this decision. A value with leading or trailing whitespace is
  refused.
- Even when listed, the remote is refused if:
  - it has `remote.<name>.receivepack`, `uploadpack`, `vcs` or `mirror`
    set, which can change what receives the push;
  - a value is an `ext::` or `fd::` remote, anywhere in a transport-helper
    chain: its address is a command or a file descriptor, not a repository;
  - a value, or the address inside a transport helper, is a publish remote
    — of this config, of any root-level `.publish.toml` /
    `.publish-*.toml`, of a target you have published from, or of a `peers`
    config — or the same repository written another way (pubrepo reads the
    URL as git does, resolves ssh aliases with `ssh -G`, and also compares
    the %-decoded value). If pubrepo can't identify a listed value, this
    check can't refuse it: listing it is your assertion that it is private.
- A refusal names each unlisted value exactly as it must be added to
  `private_remotes`. The wording says whether the value is on a public host
  (the built-in list — `github.com`, `gitlab.com`, `bitbucket.org`,
  `codeberg.org`, `sr.ht`, `gitee.com` and their subdomains — plus
  `public_hosts`) or can't be identified, but that only explains the
  refusal. List a value only if that repository is private.

A refused bookmark stays local and refused records aren't synced, each with
one warning; the exit code is unchanged. A release with `source_tag` stops
with exit 7 before it contacts a refused `source_remote` (see
[Releasing](releasing.md#configure-and-validate)).

With `--verbose`, every command that pushes — publish, `--amend`, a release,
`repair` and `repair restore-ancestry` — prints its pushes before the first
one, as `push plan (<kind>): <P|S> <ref> → <remote>`. `P` marks public refs,
which only ever go to `.publish/`'s `origin`; `S` marks private refs, with
`(refused: <reason>)` when this check refuses one.

## Bookmark

After a publish or `--amend` that pushes a snapshot from a clean source,
and after a release, pubrepo records the source commit as the local ref
`refs/pubrepo/bookmarks/<timestamp>` in your **source** repo. It is a ref,
not a tag, so `git push --tags`, `push.followTags` and default refspecs
never send it. If that second's bookmark already exists, pubrepo says
"Bookmark <timestamp> already exists, skipping".

- With no `tag_remote`, the bookmark stays local.
- With `tag_remote`, it is pushed there as `refs/tags/published/<timestamp>`
  when the remote passes the [check above](#private-refs-and-private_remotes)
  ("Bookmark <timestamp> pushed to <name>"). Otherwise pubrepo warns
  "refusing to push private ref <ref> to <name>: <reason>; kept locally". A
  failed push warns "bookmark <timestamp> kept locally; push to <name>
  failed", with git's reason under `--verbose`. Neither changes the exit
  code.

pubrepo 1.0.0 and 1.0.1 instead created a `published/<timestamp>` tag and
pushed it to your source repo's `origin`. See
[Checking for leaked refs](#checking-for-leaked-refs).

## Records

pubrepo writes small private records under `refs/pubrepo/*` in your source
repo: what each snapshot contained, which snapshots were rewritten or
dropped, what each repair removed, and which release tags were retired.
They are commit objects on no branch, never changed or deleted. (Pull
decisions live in the pull commits on your branch.)
pubrepo pushes new ones to the records remote and fetches others' from it,
best-effort, so another machine or a CI clone recognizes your snapshots.

- The records remote is `records_remote`, else git's push remote for the
  current branch: `branch.<branch>.pushRemote`, then `remote.pushDefault`,
  then the branch's upstream remote. There is no fallback to `origin`. With
  none, pubrepo warns "no records remote; records not synced; identity by
  verification only".
- pubrepo syncs records only by that remote's name, and only if it passes
  the [private-refs check](#private-refs-and-private_remotes): every one of
  its URLs listed in `private_remotes`, and none of them a publish remote
  (records name paths and the config path). Otherwise it warns once per run
  — "refusing to push private refs refs/pubrepo/* to <name>: <reason>;
  records not synced" — naming each value to add.
- The first time that happens in a clone, pubrepo also prints the exact line
  to add: "NOTICE: records are not synced until the records remote is
  listed; add this line to [publish] in <config file>: private_remotes =
  […]". It is shown once per clone (again only if the line would change),
  and `status` never shows it.
- `pubrepo init` checks the records remote the same way. Off a terminal it
  prints the line to add (not under `--quiet`). On a terminal it lists the
  remote's unlisted values that can be listed, shows each that can't (a
  publish repository, or a value with surrounding whitespace) as "cannot be
  listed: <reason>", and asks "Add these to [publish] private_remotes in
  <config>? Only if these repositories are private. [y/N]", default No.
  Only `y` edits the config: pubrepo inserts the line directly after the
  `[publish]` header, or, if the config already sets `private_remotes`,
  prints the full replacement line instead. Publish and pull never edit
  your config.
- If sync fails, pubrepo warns "records not synced; identity by verification
  only" and continues; it then verifies snapshots against your source
  history, which may ask one-time questions about changes it can't verify.
- A plain `git clone` of your source doesn't copy records; the first pubrepo
  run there fetches them.

## Publish clone origin

Before they fetch, publish, `--amend`, a release, `repair` (including
`--plan`) and `repair restore-ancestry` check `.publish/`'s `origin`. Each
failure exits 2, prints the fix, and pushes nothing:

- **Transport keys.** `remote.origin.receivepack`, `uploadpack`, `vcs` or
  `mirror` set in `.publish/` is refused: it can change what serves the
  fetch or receives the push. The fix is
  `git -C .publish config --unset-all remote.origin.<key>`. With release
  management enabled, every publish's discovery (`--release`, `--bump`,
  `--no-release`, `--amend`) has already read `origin`'s tags when this
  check runs, and `status --remote`, `pull --plan`, `pull` and
  `validate --release-check` contact `origin` without it (see
  [Limits (1.1.0)](../CHANGELOG.md#limits-110)).
- **Replace refs and grafts.** `refs/replace/*` refs or a grafts file in
  `.publish/` are refused: they change the history git reports but not what
  a push sends. The fix is `git -C .publish replace -d <object>` (or
  removing the grafts file), or re-creating `.publish/` with `pubrepo init`.
  pubrepo also runs every git command in `.publish/` with replace objects
  ignored.
- **URLs.** Every URL of `origin` — each `url` and `pushurl` as written, and
  each value of `git remote get-url --all` and `--push --all` — must equal
  `[publish] remote` as an exact string, or the `insteadOf` rewrite of
  `remote`, and that only when `origin`'s fetch and push URLs are the same
  (so a snapshot can't be fetched from one repository and pushed to
  another). For a relative local `remote` such as `../public.git`, only the
  absolute path `init` stored (the source root joined with `remote`) is
  accepted; `../public.git` itself is refused, because git would resolve it
  from inside `.publish/`. Otherwise the message is "publish clone's
  origin does not match [publish] remote: <url>", with the fix, for example
  `git -C .publish remote set-url origin <remote>` (the absolute path for a
  relative `remote`; `--push` too when needed). If `.publish/` was cloned
  from a different repository, re-create it instead: move `.publish/` aside
  and run `pubrepo init`.
- **Rewrites to a source remote.** An `insteadOf` rewrite of `remote` is
  refused if the rewritten URL names a repository that one of your source
  repo's remotes points at, however either is spelled (a path or `file://`
  URL, https or scp form, with or without `.git`), unless the rewritten URL
  is the publish repository itself. Public content is never pushed to a
  source-side remote. The message names the remote and says to remove the
  `url.<base>.insteadOf` / `pushInsteadOf` rule.

## Publish clone history

Immediately before it creates a public commit — a snapshot, an amend, a
release candidate, a repair rewrite or a restore-ancestry merge — pubrepo
checks the new commit's parents. Each must be the public tip fetched in
this run, an ancestor of it, or a commit this run verified or created.
Otherwise it exits 2 and pushes nothing: "<dir>/ holds <n> commit(s) that
did not come from [publish] remote (e.g. <sha>); it may have been cloned
from another repository. Nothing was pushed.", followed by "Re-create it:
move <dir>/ aside and run `pubrepo init`".

If `.publish/` was cloned from your private repository by mistake and then
pointed at a new, empty public repository, the first publish refuses
instead, with exit 5 and nothing pushed: "the public branch '<branch>' is
missing, but it was published before (…)", because `.publish/` still holds
the private repository's `refs/remotes/origin/<branch>`. Re-create
`.publish/` (move it aside, then `pubrepo init`), or run
`pubrepo publish --recreate-branch`: it publishes a root snapshot, so no
private commit reaches the public repository, and saves the commits that
existed only in `.publish/` under `refs/pubrepo/local/<timestamp>` there,
which is never pushed.

## Limits of these checks

The private-refs and publish-clone checks compare URL strings and check
commit parents. Configuration that changes where git connects, or what
history it reports, without changing those can get past them. The full
list, with the exact triggers, is in the 1.1.0 CHANGELOG entry under
[Limits (1.1.0)](../CHANGELOG.md#limits-110). The most likely ones: a
custom `core.sshCommand`, `GIT_SSH_COMMAND` or `GIT_SSH`; a `Host` block in
`~/.ssh/config` that sends a listed or publish host elsewhere; and
`core.gitProxy` or `GIT_PROXY_COMMAND` for `git://`.

## Checking for leaked refs

- `pubrepo validate` (offline) lists local `published/*` tags left by 1.0.0
  and 1.0.1, with the `git tag -d …` command to delete them:
  `git push --tags` would still publish them. It also runs `tag_remote`,
  `records_remote` and (with `source_tag`) `release.source_remote` through
  the [private-refs check](#private-refs-and-private_remotes) and warns
  about each refusal, naming the values to add.
- `pubrepo validate --remote` also lists `published/*` tags and
  `refs/pubrepo/*` refs on `.publish/`'s `origin` and the other publish
  remotes, with the `git push … --delete …` command, and on any source
  remote that fails the private-refs check. For those it says "'<remote>'
  is not listed in private_remotes; if this remote is public, these refs
  expose private history", with the delete command to run if it is public.
  With no `records_remote` set, it also warns when the push remote that
  records would use is refused. An unreachable remote is a warning. A
  remote with `receivepack`, `uploadpack`, `vcs` or `mirror` set is never
  contacted, since that key chooses the program git runs: "'<remote>' not
  contacted: remote.<name>.<key> is set; check it by hand".

These checks run only when the config has no errors. They report warnings
and only read; pubrepo never deletes refs itself.

## Pull settings

`private` marks source paths that `pubrepo pull` must never write. Besides
these globs, a path is private if it exists in your source but isn't
published, or if it lies under a source directory none of whose files are
published. A public change to a private path can only be left on public or
removed from it.

`peers` names other publish configs (by path) so pull can tell when an
incoming file belongs to another target. Root-level `.publish.toml` and
`.publish-*.toml` files, and configs you have published from, count
automatically.

## Release settings

Missing `publish.release` is completely inert. Release flags are errors
unless the table is enabled. Enabled projects have no automatic action:
every real publish chooses `--release`, `--bump`, or `--no-release`.

When `enabled = false`, `tag_format` and `adopt_tags` still identify the
tags that earlier releases created, so their released history stays
protected from `repair`. Any other release key present draws the warning
"release management is disabled; release settings other than tag_format
and adopt_tags are ignored".

A tag counts as a **managed** release only if pubrepo created it (its
annotated message carries `Pubrepo-Release:` and `Pubrepo-Source:` trailers
and it points at a pubrepo snapshot) or it is listed in `adopt_tags`. List
tags made before 1.1.0 there; otherwise `--bump` has no stable base. Other
tags are reported and never block anything.

The release build runs without credentials: `TWINE_*`, `GH_TOKEN`,
`GITHUB_TOKEN`, `SSH_AUTH_SOCK`, `GPG_AGENT_INFO`, `GIT_ASKPASS`,
`SSH_ASKPASS` and every `strip_env` match are removed from its environment.

Release artifacts are built only from the files pubrepo manages plus `keep`
files. Carried public files are listed, not packaged; GitHub assets and the
release notes are read from those files, and a configured asset or notes
file that isn't among them stops the release before anything is pushed
(exit 7). Credentials are removed from the
build's environment only; a build backend can still read files by path.

Exact versions use strict SemVer 2.0. Git-only releases accept any strict
SemVer prerelease. With PyPI enabled, prereleases are intentionally narrower:
only `-alpha.N`, `-beta.N`, and `-rc.N` are accepted, mapping to PEP 440
`aN`, `bN`, and `rcN`. Build metadata is refused with PyPI. Custom package
registries are not supported in v1 because pubrepo cannot reconcile their
immutable filename/digest identity safely.

The regex example uses a TOML literal single-quoted string, so backslashes
are not doubled. `version_file`, GitHub assets, notes, and checksum paths may
not be absolute or escape their respective roots.

## Transform path validation

Transform target paths are validated at config load (errors exit 1):

- **Absolute paths** (e.g., `/etc/passwd`) are rejected.
- **Paths that escape the publish directory** via `..` (e.g.,
  `../../secret`, `x/../../../etc/passwd`) are rejected after
  normalization.
- **Overlap with `keep` entries** is rejected: a transform target that
  matches or nests inside a kept path would be re-transformed on every
  publish. `keep = ["docs"]` with a transform on `"docs/README.md"` is
  an error; `keep = ["doc"]` with a transform on `"docs/README.md"` is
  fine.

## Exclude patterns: the three rules

1. **One trailing `/` is stripped first** and makes the pattern dirs-only.
   (Stripping happens *before* rule 2's test, so `internal/` is a bare
   name — gitignore's rule.)
2. **If the remainder contains `/`**, it matches anchored: fnmatch against
   the path relative to the source root (the directory that holds the
   config). `*` crosses `/`; there is no `**`.
3. **Otherwise it matches the basename, at any depth.**

Matching is case-sensitive, like git. fnmatch metacharacters `* ? [ ]` are
live; a literal `[` is written `[[]`.

## Precedence

1. **Hard-excludes** — `.git`, `.publish`/`.publish-*`, and the config file:
   at every depth, in every code path, cannot be overridden.
2. **Your excludes** beat your includes (with a warning on the collision);
   an excluded directory also dominates includes below it.
3. **Your includes** beat the default excludes at the entry level — if you
   explicitly include `__pycache__/`, you get the directory…
4. **Default excludes** always filter the *contents* of walked directories:
   `__pycache__`, `.DS_Store`, `.venv`, `.claude`, `.ruff_cache`,
   `.pytest_cache`, `.mypy_cache`, `*.pyc`, `*.egg-info`, plus `.env`,
   `.envrc`, and `.env.*` **except** `.env.example`, `.env.sample`,
   `.env.template`. (…so `*.pyc` files inside that included `__pycache__/`
   still drop. There are no negation patterns.)

## Line endings and filters

pubrepo publishes files as git would store them from your working tree:
text/eol conversion applies, so with `* text eol=crlf` the public repo gets
LF blobs. Paths with a `filter` attribute (git-lfs, git-crypt, custom clean
filters) are published as their raw working-tree bytes.

## Multi-target

One config per target. In-tree configs must be named `.publish.toml` or
`.publish-<name>.toml` (anything else would escape the hard-exclude
namespace and leak its forbidden list to sibling targets — pubrepo refuses).
Configs outside the repo may be named anything.

```bash
pubrepo publish --config .publish-sdk.toml    # dir = ".publish-sdk" inside
```
