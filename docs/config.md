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
keep    = ["LICENSE"]                        # default []. Top-level names only (no "/"); preserved across
                                             # rebuilds; scrubbed like everything else.

[publish.scrub]
forbidden = ["acme-internal", "10.1.2."]     # default []. Literal, case-insensitive, substring.
                                             # Any hit BLOCKS the publish (exit 4).

[publish.transforms]
"pyproject.toml" = [
  { find = "private-name", replace = "public-name" },              # literal, all occurrences
  { strip_between = ["<!-- INTERNAL -->", "<!-- /INTERNAL -->"] }, # inclusive removal, all occurrences
]
```

## Field rules

| Field | Type | Validation |
|---|---|---|
| `remote` | str | required, non-empty; warning if it doesn't look like a git URL |
| `branch` | str | non-empty (default `main`) |
| `dir` | str | `.publish` or `.publish-<name>`; single path component, non-empty name |
| `include` | list[str] | required, non-empty; entries are literal paths that must exist; `.git`/`.publish*`/the config file are rejected; including the repo root warns |
| `exclude` | list[str] | three-rule patterns; empty strings and match-all `*` warn |
| `keep` | list[str] | no `/` (top-level only in v1) |
| `scrub.forbidden` | list[str] | literal strings; empty strings warn |
| `transforms` | table | keys are file paths in the publish set (else warning); each rule has exactly one of `find`+`replace` (both str, `find` non-empty) or `strip_between` (two non-empty strs) |

Unknown keys anywhere produce warnings, never errors (forward
compatibility).

## Exclude patterns: the three rules

1. **One trailing `/` is stripped first** and makes the pattern dirs-only.
   (Stripping happens *before* rule 2's test, so `internal/` is a bare
   name — gitignore's rule.)
2. **If the remainder contains `/`**, it matches anchored: fnmatch against
   the path relative to the repo root. `*` crosses `/`; there is no `**`.
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

## Binary files

A file with a null byte in its first 8 KiB is binary: it skips transforms
*and* scrub, by documented policy. The publish path notes each skip on
stderr.

## Multi-target

One config per target. In-tree configs must be named `.publish.toml` or
`.publish-<name>.toml` (anything else would escape the hard-exclude
namespace and leak its forbidden list to sibling targets — pubrepo refuses).
Configs outside the repo may be named anything.

```bash
pubrepo publish --config .publish-sdk.toml    # dir = ".publish-sdk" inside
```
