# Contributing

Thanks for your interest! A few things to know up front.

## This repo is a one-way mirror

This public repo is published by pubrepo itself from a private source
repo. Commits here are snapshots — there is no shared history to merge
into.

**Pull requests are welcome**, but they can't be merged verbatim: a
maintainer hand-applies your change to the private source, credits you in
CHANGELOG.md, and republishes. Your commit hash won't survive; your name
will. If you'd rather not have your change re-authored this way, say so
in the PR and we'll work something out.

**Issues are the best way to contribute** — a bug report with a failing
config plus expected/actual behavior is gold.

## Running the tests

    python -m pytest tests/

stdlib only, Python 3.11+, nothing to install beyond pytest.

## What gets accepted

pubrepo has locked architecture decisions: zero dependencies, literal
scrub only, one-way mirror, nuke-and-rebuild. PRs that revisit those will
be declined with thanks — the README explains the reasoning.
