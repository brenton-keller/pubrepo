# Releasing with pubrepo

Release management adds a deliberate decision gate to a publish target. A
release is an ordinary pubrepo snapshot on the public branch, tagged and
optionally uploaded to GitHub Releases and PyPI. pubrepo never edits,
versions or commits your source for a release: the version must already be
committed there.

## Configure and validate

Enable `[publish.release]` and commit the exact version in its
`version_file`. See [the config reference](config.md) for Git-only, GitHub,
and PyPI settings. If you already have release tags from before pubrepo
managed releases, list them in `adopt_tags` (see below). Then run:

```sh
pubrepo validate
pubrepo validate --release-check
pubrepo --stage
```

The live check is read-only. `--stage` builds the candidate for inspection
but does not consume a release decision or contact providers; its output
notes that the next real publish still needs a choice.

With `source_tag = true`, the source tag is pushed to `source_remote` by
name, and only if every URL of that remote is listed exactly in
`[publish] private_remotes` and none of them is a publish remote (see
[private refs](config.md#private-refs-and-private_remotes)). Otherwise the
release stops before it contacts that remote, with exit 7: "refusing to
push private ref refs/tags/<tag> to <remote>: <reason>", naming each value
to add. `validate --release-check` reports the same refusal and skips its
queries to that remote; plain `validate` warns about it.

## Choose exactly one outcome

```sh
pubrepo --release 1.2.0
pubrepo --bump minor
pubrepo --no-release
```

On a terminal without a flag, pubrepo shows the publish preview, then asks
you to choose from the compatible options — a numbered list with no default.
That choice is the confirmation: no separate `[y/N]` follows, and an empty
answer cancels (exit 1, nothing written). Non-interactive runs must pass a
flag. `--bump` uses the highest stable
managed release tag, and its result must exceed every managed version. The
source, scrubbed candidate, and distribution versions must agree exactly.

The release summary covers everything since the latest managed
release (or the whole public history before the first): public commits,
with your snapshots counted separately from collaborator commits and each
collaborator commit named; carried paths, which are public but not
packaged; decisions on flagged pull items; build files changed since that
release; and, with GitHub enabled, the release notes exactly as they will
ship (read from the export). Long lists end with "… and N more". With `--yes` it is
printed before the build.

With a release flag, a release confirms once: on a terminal it shows the
release summary and the publish preview together and asks `[y/N]`. A release
that will upload to PyPI, which cannot be undone, asks you to type the exact
version instead (`Type 1.2.0 to release:`), whether you chose it by flag or
from the list. Off a terminal, pass `--yes`. A wrong or empty answer exits 1
with nothing written.

With release management enabled, `--amend`, `--accept-source-divergence`
and `--recreate-branch` without a release flag publish as `--no-release`
and print "Note: no release this run: <flag> cannot be combined with a
release"; they can't be combined with `--release`/`--bump`.

## What a release needs

A release is built on the current public tip, like any publish, and needs the
same clean state: nothing to pull, no decisions open, no rewritten history.
Otherwise it refuses with publish's exit code — 5 when there is something to
pull, 8 when a decision is required — never 7. Pull first, then release.

The candidate is a normal snapshot on the public tip, with collaborators'
pulled and carried content included. If the public tip is already your own
snapshot and nothing changed, the tip itself is tagged. Tags go only on
pubrepo snapshots.

If the version in `version_file` is below the latest managed
release — for example after you accepted a collaborator's change to it —
every publish with release management enabled warns "version downgrade",
naming both versions; the exit code is unchanged. Releasing that lower
version is refused (exit 7).

These combinations are refused before anything runs (exit 1):

- `--amend` with `--release`/`--bump` — publish the amend first, then
  release;
- `--accept-source-divergence` or `--recreate-branch` with a release —
  publish with it first, then release;
- `--retry` without `--release`/`--bump`.

## Managed tags

Public release tags are the release ledger. A tag is **managed** if:

- pubrepo created it: an annotated tag whose message carries
  `Pubrepo-Release: <version>` and `Pubrepo-Source: <source sha>` and that
  points at a pubrepo snapshot; or
- it is listed in `[publish.release] adopt_tags`.

Every other tag — lightweight tags, tags a collaborator made — is
**foreign**: reported, never used as the version base, never blocking. A
foreign tag already holding the name of the version you release refuses
(exit 7).

Tags made before 1.1.0 carry no trailers, so list them in `adopt_tags`;
otherwise `--bump` finds no stable base and asks for `--release VERSION`.

Every managed release target must stay in the public branch's history. If
one is dropped, a release refuses (exit 7) and offers
`pubrepo repair restore-ancestry <tag> --plan` (make it reachable again) or
`pubrepo release-retire <tag>` (record it as retired; later releases then
proceed). `--no-release`
publishing is never blocked by the ledger.

## Packaging

- Release artifacts are built from an export of the candidate that contains
  only the files pubrepo manages plus `keep` files. Carried public files are
  listed, not packaged. A carried `.gitattributes` cannot exclude or rewrite
  a managed file; a managed `.gitattributes` is honored. GitHub assets and
  the release notes are read from this export; a configured asset or notes
  file missing from it stops the release before anything is pushed
  (exit 7).
- The build and its `twine check` run without credentials in their
  environment: `TWINE_*`, `GH_TOKEN`, `GITHUB_TOKEN`, `SSH_AUTH_SOCK`,
  `GPG_AGENT_INFO`, `GIT_ASKPASS`, `SSH_ASKPASS` and every `strip_env` match
  are removed.
  This covers the environment only: a build backend can still read files
  by path. Git and tool error output that pubrepo shows or journals has
  credentials in URLs, and the values of all those variables (the built-in
  list and your `strip_env` entries), replaced by `[redacted]`.
- Every archive is checked before extraction: absolute paths, `..`, links,
  and non-file members are refused (exit 7).
- The build runs in a disposable copy of the export. Files the build
  backend writes into its source directory (for example setuptools'
  `<name>.egg-info`) never change the retained export, so a resume or a
  rerun reuses it, and they are scanned like everything else they ship in.
- The release scrub covers every shipped byte: the export, every sdist and
  wheel member (named `<dist>!<member>`, so `allow_opaque` globs can match
  members), and other assets such as the checksum file. Anything not
  allowlisted blocks the release with exit 4 before anything is pushed,
  tagged or uploaded.
- Findings in carried content, which publish only reports, block a release
  (exit 4) unless allowlisted or — for content pubrepo cannot scan —
  accepted with `--accept-unscanned PATH` (release commands only;
  elsewhere exit 1). A forbidden string has no accept flag: clear it with
  `pubrepo pull --redecide <path> --remove <path>`, which the message
  names.
- Build files in the export — the same executable, build and CI list that
  `pull` flags (see [integrating changes](integrating-changes.md#inbound-checks)),
  except `version_file` — whose content or mode changed since the latest
  managed release (every one, before the first release) are listed in the
  summary, because the build runs them. On a terminal the confirmation
  covers them; with `--yes`, the release also needs `--accept-build-changes`
  or it stops with exit 1 before its first write, naming the files. Like
  `--accept-unscanned`, the flag applies to release commands only.
- If your README is also the PyPI description (`readme` in
  `pyproject.toml`), relative links and images break on PyPI. Use absolute
  URLs pinned to the release tag, and move them to the new tag in the commit
  that sets the version; pubrepo's own README does this.

## Remote order

Before the first remote write, pubrepo has built, scrubbed and checked
everything locally. Writes then occur in this order:

1. public branch (a plain fast-forward), confirmed by ancestry;
2. annotated public release tag;
3. optional annotated source tag, then the best-effort
   [bookmark](config.md#bookmark), pushed only to a checked `tag_remote`;
4. GitHub draft (created with `gh release create --verify-tag`, so the tag
   is never created by GitHub) and digest-checked assets;
5. absent PyPI files after immutable digest reconciliation;
6. publish the GitHub draft;
7. mark the local journal complete and append the audit record.

No ref moves before the branch push is confirmed. A tag the run creates is
recorded before its push, with its tag object, and removed if that push
fails. A release tag already in `.publish/` is reused only if it is that
recorded object; otherwise the release stops before any push (exit 7),
naming the tag and `git -C .publish tag -d <tag>`, so a tag made by hand
never goes public with its message and tagger. No credential is
accepted on pubrepo's command line or written to its journal. With
`--verbose`, the release first prints every push it will make
(`push plan (release): …`).

## Failure and resume

Release state lives at `.publish/.git/pubrepo-releases/<version>.json`;
retained exports and artifacts live beside it.

- **The public branch moved during the build.** The push is rejected as
  non-fast-forward, the attempt is set aside, and pubrepo exits 5: run
  `pubrepo pull`, then rerun the identical command. The built artifacts are
  reused while the managed files are unchanged. `--retry` instead re-fetches
  and tries again, at most 3 attempts; a state that needs a pull still
  refuses at once (5).
- **A later step failed.** Exit 7: "release incomplete at <step> (<cause>)".
  Fix the reported problem and rerun the **identical command**. A resume
  confirms again (on a terminal, or `--yes`). Once the branch push is
  recorded, resuming only needs the candidate to still be in the public
  branch's history; otherwise it refuses (7) and names `repair
  restore-ancestry` and `release-abandon`.
- **Immutable conflicts.** Tag or provider digest mismatches, and a GitHub
  release this attempt didn't create, are terminal (7: "stopped at
  <step>: <cause>"). Investigate the named remote objects manually.

If a transaction cannot be resumed, acknowledge it without changing any
remote:

```sh
pubrepo release-abandon VERSION       # type VERSION at a TTY
pubrepo release-abandon VERSION --yes # non-interactive confirmation
```

Abandonment is audited and leaves every remote branch, tag, release, and
package in place. Local release tags that the abandoned run
created but never pushed are removed and named; a tag the remote has, whose
remote can't be read, or that no longer points at the attempt's commit is
left and named. A later release may then proceed; the same version is
still available if the abandoned run never pushed its tag. `pubrepo init`
refuses to erase unresolved journal evidence.

## Leaks and released versions

`repair` never rewrites released history: a leak at or before your latest
release cannot be repaired in-tool until that release is removed. Rotate
the secret and yank the version on PyPI. Then run
`pubrepo release-retire <tag>` while the tag still exists, so pubrepo
records the release as retired; once the tag is gone, `release-retire`
exits 7. Then delete the release and tag by hand. `repair` can then rewrite
that history.
