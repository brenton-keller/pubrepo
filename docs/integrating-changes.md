# Integrating changes from the public repo: `pubrepo pull`

Someone committed to your public repo — a merged pull request, a web-UI
edit, a collaborator pushing directly. pubrepo never overwrites their work:
the next publish refuses with exit 5 and says to run `pubrepo pull`.
`pubrepo status --remote` shows the unpulled commits too.

`pubrepo pull` brings that work into your **source** repo, where it belongs,
under guardrails. After it, publish is an ordinary fast-forward on top of
their commits: the public history keeps them.

## TL;DR

On a terminal:

```bash
pubrepo pull                      # show the plan; decide; "Commit this pull? [y/N]"
pubrepo publish                   # then publish as usual
```

If the plan has items only you can decide, pull stops (exit 8) and names
them. Pass one decision flag per path and run it again:

```bash
pubrepo pull --theirs assets/logo.png --delete old/notes.md
```

Off a terminal (scripts, CI), pull commits only a plan bound by its digest:

```bash
pubrepo pull --plan --theirs assets/logo.png          # prints the plan; last line: "Plan digest: <hex>"
pubrepo pull --yes --expect-tip <public sha> --expect-plan <digest> --theirs assets/logo.png
```

The second command must repeat the same decision flags. When pull refuses
off a terminal, it prints the exact command to run.

## What pull does, and won't do

- **Explicit only.** Nothing pulls implicitly; publish only tells you to.
- **Clean tree.** Pull refuses (exit 1) if anything is staged, any tracked
  file is modified, or any path it would write is untracked, ignored or
  modified. Commit or move your local work first.
- **Show before commit.** It computes a plan and shows it before writing
  anything. On a terminal you confirm the plan without pubrepo holding its
  lock; it then locks, recomputes, and writes only if the plan is unchanged.
- **One source commit.** Accepted files are written into your working tree
  and committed as one commit on your current branch, authored by you, with
  `Co-authored-by:` lines for the collaborators whose changes you took
  (including co-authors named in their commits).
- **Scoped writes.** It writes exactly the paths in the plan and verifies
  each written blob against the plan. It never runs `git add -A`.
- **No public writes.** Pull never pushes to the public repo.
- **Never silent.** Every item is shown. A decision you made in an earlier
  pull that applies again is shown as "previously decided in <pull record>"
  and still counts as a decision.

## The plan

The plan lists:

- the public commit range being pulled and the integration point it starts
  from;
- every item with its class and diff; for binaries, size, sha256 and author
  on both sides, plus an extracted-text diff for Office files;
- flags from the inbound checks (below);
- public files that are carried, not managed by your manifest;
- the digest (`Plan digest: <64 hex>`, the last line).

`pubrepo pull --plan` writes nothing. It exits 0 when there is nothing to
pull and 5 when there is.

## Items and decisions

"Ours changed" means your source now differs from what was last integrated;
"theirs changed" means the public tip differs from it.

| Situation | Decisions (default in **bold**) |
|---|---|
| Text changed only on public, managed by you | **`accept`** (write theirs into source) / `decline` |
| Binary changed only on public, managed by you | `accept` / `decline` — no default |
| Deleted on public, unchanged by you | `delete` / `keep` — no default |
| Text changed on both sides, no overlap | clean three-way merge, shown: **`accept`** / `ours` / `theirs` |
| Text changed on both sides, overlapping | conflict: markers written, pull in progress (below); then `resolved` / `ours` / `theirs` |
| Same change on both sides | not a conflict |
| Binary changed on both sides | `ours` / `theirs` — no default, no merge |
| You modified it, they deleted it | `keep` / `delete` |
| You deleted it from source, they modified it | `restore` (write theirs into source) / `delete` |
| You dropped it from `include`, they modified it | `leave` (carry it, unmanaged) / `delete` |
| Renamed by them | a delete of the old path plus an add of the new one |
| Added on both sides | text: merged as above, conflicts if they overlap; binary: `ours` / `theirs` |
| Added or changed on public outside your manifest | **`leave`** (carry it) / `remove` |
| Unverified change in a past snapshot (baseline), managed | `accept` / `ours` / `delete` — no default |
| Unverified change in a past snapshot (baseline), not managed | `leave` / `remove` / `restore` (put back the file as it was before that snapshot) — no default |
| A file with transform rules, changed by them | `resolved` / `decline` (see below) |
| A `keep` file changed by them | your copy unchanged: **`accept`** writes theirs into `.publish/<path>`; both changed: `ours` / `theirs` |

Where `accept` would publish different bytes back (for example because of
line-ending settings), it is not offered, and the plan says why.

Deletions and binaries are never defaulted. Off a terminal, nothing that is
a deletion, a binary, a flagged item, a transformed path, a baseline item,
a re-decision or a previously recorded decision is taken without its flag.

**Giving decisions.** Repeat `--<decision> <path>` for each path
(`--accept`, `--decline`, `--ours`, `--theirs`, `--keep`, `--delete`,
`--restore`, `--resolved`, `--remove`, `--leave`, `--accept-excluded`),
or put them in a file with `--decisions FILE`: one `path<TAB>decision` per
line. Paths are literal. A decisions file must decide every item, each
with one of the options that item offers. An up-front `--resolved` is for
[transformed files](#transformed-files) only: a conflict is resolved by
editing the file, then `pubrepo pull --continue`, and an up-front
`--resolved` for it exits 1.

## Inbound checks

Every incoming file is checked before pull may write it into your source.
A hit makes the item flagged: it is never defaulted, and some decisions are
withheld.

- **Would fail your scrub gate** (a forbidden string, conflict markers):
  `accept` is never offered. Choose `decline`, `leave` or `remove` — the
  content is already public; `repair` removes it from history.
- **Executable, build or CI files** — executable bits, `.github/`,
  `hooks/`, `.githooks/`, `.gitlab-ci*`, `pyproject.toml`, `setup.py`,
  `setup.cfg`, `MANIFEST.in`, makefiles (`Makefile`, `GNUmakefile`, `*.mk`),
  `Justfile`, `package.json`, `Cargo.toml`, `build.rs`, `Dockerfile`,
  `Containerfile`, `*.sh`, `*.ps1`, `conftest.py`, `*.pth`, `tox.ini`,
  `noxfile.py`, `.pre-commit-config.yaml`, `.envrc`, `.vscode/tasks.json`,
  `requirements*.txt`, `.gitattributes`, `.gitmodules`, `.publish*.toml`
  and your release `version_file`. Names match case-insensitively. These
  can run code on your machine, so you decide each one.
- **Private paths** — a path that exists in your source but isn't
  published, a path under a source directory none of whose files are
  published, or a path matching `[publish] private`. Pull never writes it
  into source; the only options are `leave` and `remove`.
- **Other publish targets** — a path another of your publish configs owns
  (root `.publish.toml` / `.publish-*.toml`, configs you have published
  from, and `[publish] peers`). Options: `accept`, `accept-excluded`,
  `decline`, `leave`. Other `.publish*.toml` files deeper in the tree are
  listed as possible targets, not flagged.
- **Unsafe paths** — absolute paths, `..`, `.git`, and case collisions are
  refused. Type changes are flagged; symlinks and submodules are never
  managed.
- **Unreadable content** — flagged; the plan prints the `allow_opaque` line
  that would allowlist it.
- **Content a repair removed** — flagged when an incoming blob matches one
  a repair removed.

## Conflicts

**Text.** Overlapping edits are merged with `git merge-file --diff3`.
Conflicting files get markers in your working tree, nothing is committed,
and the pull is **in progress** (exit 8). Edit the files, then:

```bash
pubrepo pull --continue           # refuses while any marker remains; off a terminal add --yes
pubrepo pull --abort              # restore exactly the paths pull wrote
```

Paths you edit while the pull is in progress are recorded as `resolved`.

**Binary.** No merge and no default: choose `ours` or `theirs`. The version
you don't take stays recoverable from git history.

## Transformed files

A public change to a file that has transform rules has no well-defined
source equivalent, so pull never writes it automatically. Read the diff,
apply the intent to your source by hand, and decide `resolved` — or
`decline`. The pull stays open until you decide.

## Special situations

- **Rewritten public history.** If public commits pubrepo published are no
  longer on the public branch (someone force-pushed), publish exits 5
  ("our snapshot(s) <sha> are no longer on public") and pull names every
  commit that is gone, warns about content that came from
  them, and requires `--accept-rewritten-history` (or confirmation on a
  terminal).
- **Your source is behind.** If the last snapshot was published from a
  source commit your checkout doesn't have yet, pubrepo exits 5 and asks you
  to update your checkout, naming any local branch that contains it.
- **A diverged source snapshot.** If the last snapshot came from another
  branch, or from a commit you have since rebased or amended away, pubrepo
  asks you to confirm (exit 8 off a terminal). When that source commit is
  gone, the message says the snapshot's source commit
  "no longer exists here". Pass the exact
  `--accept-source-divergence=<sha>` flag the message prints, to `publish`
  or to `pull`.
- **Copied `Source:` trailers.** A collaborator commit that copies one of
  pubrepo's snapshot messages is treated as theirs: the plan notes
  "carries a copied `Source:` trailer", and its changes are ordinary items.
- **Unverified past snapshots** (baseline items). Changes in pubrepo
  snapshots that pubrepo cannot verify against your source — for example
  from a publish with uncommitted edits, on a clone without synced records —
  are asked once: `pubrepo pull --baseline`.
- **Changing an earlier decision.** `pubrepo pull --redecide <path>` asks
  again, whether or not a decision exists.
- **Adopting public files.** Widen `include`, then pull: newly managed
  paths whose public content isn't yours become items.
- **First pull from a repo pubrepo never published to.** The whole public
  tree counts as added on public; identical files are reported, not
  conflicted.
- **Pull records reached through a merge.** If a pull commit reaches your
  main branch through a `--no-ff` merge, paths decided `decline`, `ours`,
  `resolved` or `remove` are asked once more. The earlier decision is shown
  for reference ("previously decided `<decision>` in <record> on a merged
  branch") but not prefilled: you decide it again.

## Worktrees

A pull in progress belongs to the worktree that started it. There,
`--continue` and `--abort` work, and every other pubrepo command except
`pull --plan` and `status` exits 8. In other worktrees, `--continue` and
`--abort` exit 1 and name the owning worktree; other commands proceed with
a notice. If the owning worktree was removed, `pull --abort` from any
worktree clears the record (confirm on a terminal, or `--yes`).

## The pull record

The pull commit carries trailers that pubrepo reads later:
`Pubrepo-Pulled-Through: <public sha>`, `Pubrepo-Pulled-Range: <from>..<to>`,
`Pubrepo-Destination: <remote> <branch>`, and
`Pubrepo-Accepted-Divergence` when used. The body holds a JSON list of every
decision. Even a pull with nothing to write records one.

If a pull deletes a file your `include` lists by name, pull says so; remove
it from `include` before the next publish, which otherwise exits 1 naming
that pull.

## Exit codes

| Exit | Meaning |
|---|---|
| 0 | committed (or nothing to pull) |
| 1 | dirty tree; missing or invalid flags; digest mismatch; `--continue`/`--abort` from another worktree |
| 2 | network |
| 3 | lock |
| 5 | the public tip moved, or your source checkout needs updating |
| 8 | stopped: a decision is required, or conflicts are in progress |

## The collaborator workflow

1. **Collaborators work on their own branches** and open pull requests to
   the published branch.
2. **Merge the pull request on the host.** pubrepo does not read unmerged
   pull requests. Host merges ignore git merge drivers.
3. **Run `pubrepo pull`**, decide, then `pubrepo publish`. Their commits stay
   in the public history; your next snapshot sits on top.
4. **Protect the published branch** if you like: routine publishing is a
   plain fast-forward and needs no force-push permission. Only `--amend` and
   `repair` rewrite, and those need force-push permission when you use them.
