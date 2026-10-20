# Contributing

Thanks for your interest! A few things to know up front.

## How this repo is published

This public repo is published by pubrepo itself from a private source
repo. The maintainer's commits arrive here as snapshot commits.

**Pull requests are welcome.** Once a pull request is merged here, the
maintainer runs `pubrepo pull` to bring it into the private source; your
commits stay in this repo's history, and the source commit credits you with
a `Co-authored-by:` line for the changes taken in. Files with transform
rules are re-applied by hand, so a change to one of those may be adapted.

**Issues are a great way to contribute** — a bug report with a failing
config plus expected/actual behavior is gold.

## Running the tests

    python -m pytest tests/

pubrepo itself is stdlib only (Python 3.11+). The tests need pytest; the
real-build release tests also need `build`, `setuptools` and `twine`, and
are skipped without them. CI installs all four:

    python -m pip install pytest build setuptools twine

The suite runs inside a sandbox (`tests/conftest.py`): every test gets its
own temporary directory, `HOME` and git config; git is limited to local
`file` transports; a git wrapper on `PATH` refuses any repository outside
the test run's temporary directory; and a test that starts a process
outside it fails.
Tests that need a real terminal are marked `pty`.

## Running pubrepo while developing

Don't run `pubrepo` (or `python pubrepo.py`) with your working directory
inside a checkout of this repo, and don't `exec` or import-and-call
`main()` from its source. Use a scratch repo under `/tmp` whose remotes are
local bare repos. The same rule applies to coding agents working on pubrepo.

## What gets accepted

pubrepo has deliberate design decisions: zero dependencies (stdlib plus
git), literal forbidden-string matching (no regex), and routine commands
that never rewrite public history. PRs that revisit those will be declined
with thanks — the README explains the reasoning.
