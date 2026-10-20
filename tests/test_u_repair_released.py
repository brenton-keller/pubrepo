"""Repair's released-ancestry refusal names no internal ruling ID and no
flag or command that fails in that state. Neither `release-retire` nor
`--leave-reachable` gets repair past released history, so the message
points at the manual procedure in docs/releasing.md instead.
"""
import re

import pytest

from conftest import git_run
from test_ff_repair import _adopt, _leaky_world, _records, _repair

pytestmark = pytest.mark.rewrite

TAIL = ('repair never touches released history. To remove a leak from a released version, '
        'see "Leaks and released versions" in docs/releasing.md.')


def _assert_refusal(r, prefix):
    assert r.code == 1, (r.code, r.stdout, r.stderr)
    line = next(x for x in r.stderr.splitlines() if x.startswith("ERROR: "))
    assert line == f"ERROR: {prefix} (v1.0.0); {TAIL}"
    assert not re.search(r"\([A-Z][A-Z0-9]*-\d+\)", r.stderr)    # no internal ID tag
    assert "release-retire" not in line
    assert not re.search(r"(^|\s)--?[a-z]", line), line          # names no flag


def test_remove_path_over_a_release(world, run):
    w, first = _leaky_world(world)
    _adopt(w, "v1.0.0", first)
    before = _records(w)
    args = ("remove-path", "secret.txt", "--from", first, "--plan")
    _assert_refusal(_repair(run, w, *args), "the range rewrites released ancestry")
    assert _records(w) == before


def test_rewind_over_a_release(world, run):
    w = world()
    base = w.snapshot(record=True)
    w.commit_source({"docs/rel.md": "released\n"})
    rel = w.snapshot(record=True, carry=True)
    _adopt(w, "v1.0.0", rel)
    _assert_refusal(_repair(run, w, "rewind", base, "--plan"),
                    "rewinding would drop released ancestry")


def test_no_flag_or_retire_gets_past_it(world, run):
    """Why the message suggests no command: after `release-retire` and with
    `--leave-reachable` on the release tag, the refusal is unchanged."""
    w, first = _leaky_world(world)
    _adopt(w, "v1.0.0", first)
    assert run(["release-retire", "v1.0.0", "--yes"], cwd=w.src).code == 0
    args = ("remove-path", "secret.txt", "--from", first, "--plan")
    _assert_refusal(_repair(run, w, *args), "the range rewrites released ancestry")
    _assert_refusal(_repair(run, w, *args, "--leave-reachable", "refs/tags/v1.0.0"),
                    "the range rewrites released ancestry")
    assert git_run("ls-remote", w.url, "refs/tags/v1.0.0", cwd=w.src).strip()
