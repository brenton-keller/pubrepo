# Integrating changes from the public repo

Someone pushed commits to your public repo — a merged PR, a GitHub-UI
edit, a collaborator working directly on the mirror. The next publish
refuses with exit 5, listing the foreign commits. This page is the
workflow for getting that work back into your source repo and
republishing cleanly.

## TL;DR

```bash
pubrepo integrate
```

prints a copy-paste recipe with everything filled in: the foreign commit
range, the exact files to diff (filtered through your manifest, junk
excluded), the apply commands, the `--author` credit, and the republish.
Paste it at your source-repo root, review, run.

## Why print-only?

pubrepo never modifies your source repo — that invariant is what makes
the tool safe to run from hooks and CI. And integration genuinely needs
your judgment: collaborators commit build artifacts, scratch files, and
editor droppings to mirrors (we've seen `__pycache__/`, `.egg-info/`,
and stray notes land on a real public repo). `integrate` filters those
out of the recipe and lists them, but *importing third-party content
into your source tree is a decision, not a side effect*.

## How the recipe works

Every snapshot commit pubrepo creates carries a `Source: <hash>` trailer
— the source commit it was built from. That trailer is the bridge
between the two unrelated histories:

1. **Last snapshot** = the HEAD of your `.publish/` clone. Foreign work
   is everything on `origin/<branch>` beyond it.
2. **Conflict surface** = files changed in the foreign range ∩ files
   changed in your source since the `Source:` base. Empty means the
   patch applies cleanly; non-empty means real (resolvable) conflicts,
   and the recipe tells you which files.
3. **The diff** is taken inside `.publish/` (which already has both
   sides fetched) and applied at your source root. Published files are
   byte-identical to source files, so paths and contexts line up — `git
   apply --3way` gives you proper conflict markers if anything overlaps.
4. **Credit**: the commit is authored to the contributor with
   `--author`, so their name survives in your source history even though
   the public repo only ever sees snapshot commits.
5. **Republish** with `--force-overwrite`. The push uses
   `--force-with-lease` pinned to the sha you saw — a commit landing in
   between is refused, never destroyed.

## Transforms change the rules

If a foreign change touches a file that has transform rules, its public
content is **not** your source content — the diff has no well-defined
preimage in your repo. `integrate` detects this and prints the manual
variant instead: read the diff, hand-apply the intent to your source,
republish. Files without transform rules are unaffected; the recipe
only goes manual for the files that need it.

## After you republish

- The foreign commit hashes vanish from the public branch (it now
  carries your new snapshot). **Tell your collaborators** — they reset
  with `git fetch && git reset --hard origin/<branch>`.
- Consider backing up their work first if the integration was big:
  `git -C .publish push origin origin/<branch>:refs/heads/backup/<date>`
- Their *branches* are untouched: pubrepo only ever force-pushes the
  configured branch.

## The sustainable collaborator workflow

If someone works on the mirror regularly, direct pushes to the published
branch will diverge it weekly. The pattern that works:

1. **They work on their own branch** (`alice/dev`). pubrepo never
   touches branches, so a republish can't clobber their work — and tools
   that sync a working copy to git (Databricks Repos, IDEs) can point at
   the branch.
2. **They PR to the published branch** when ready. The PR is an isolated
   diff — easy to review, easy to integrate.
3. **You integrate and republish.** Their PR closes unmerged (its
   commits can't land on a snapshot branch) — that's the documented
   cost; their name lands in your source history via `--author` and in
   the public CHANGELOG if you keep one.
4. **Protect the published branch** so this is the only path: no direct
   pushes, PRs only. One critical nuance — pubrepo needs force-push on
   that branch, so use a ruleset that lets *the publishing identity*
   bypass (GitHub: branch ruleset → restrict updates → add yourself as
   bypass actor, or classic protection with "allow force pushes —
   specify who"). Protection that blocks your own publish breaks the
   mirror.
