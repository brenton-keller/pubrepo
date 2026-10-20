"""Pull's decision-required hint (exit 8) names the
first undecided item's path and an option that item offers, and says how
to list every item's options.

At 8ccaa1d the hint was always `e.g. --theirs PATH`; baseline items never
offer `theirs`, so following it gave exit 1 (`not offered for this item`).
"""
import re
import shlex

from conftest import git_run

HINT = re.compile(r"^Pass one decision flag per path \(e\.g\. --(?P<option>[a-z-]+) "
                  r"(?P<path>.+)\) and run again\.$", re.M)
LISTING = "Each item's options are listed by: pubrepo pull --plan"


def _offered(stdout, path):
    """The options the rendered plan lists for `path`."""
    lines = stdout.splitlines()
    for n, line in enumerate(lines):
        if line.strip().startswith(f"{path}  ["):
            options = lines[n + 1].strip()
            assert options.startswith("options: "), options
            return [o.lstrip("-") for o in options[len("options: "):].split(", ")]
    raise AssertionError(f"{path} not in the plan:\n{stdout}")


def _hint(r):
    """(option, path) from the exit-8 hint; the listing line follows it."""
    assert r.code == 8, (r.code, r.stdout, r.stderr)
    m = HINT.search(r.stderr)
    assert m, r.stderr
    path = shlex.split(m["path"])[0]
    return m["option"], path


def _assert_followable(run, w, r, *, baseline):
    option, path = _hint(r)
    assert option in _offered(r.stdout, path)
    listing = LISTING + (" --baseline" if baseline else "")
    assert r.stderr.rstrip("\n").splitlines()[-1] == listing
    plan = run(["pull", "--plan", *(["--baseline"] if baseline else []), f"--{option}", path],
               cwd=w.src)
    assert plan.code in (0, 5), plan.stderr
    assert "not offered" not in plan.stderr
    return option, path


def test_baseline_unmanaged_items_with_a_partial_decisions_file(world, run, tmp_path):
    """Unmanaged baseline items (leave/remove/restore) with one decided in
    a --decisions file: exit 8, the hint names the undecided item and one
    of its options, the listing command carries --baseline, and the named
    flag is accepted by --plan."""
    w = world()
    w.snapshot(extra={"notes.txt": "n\n", "zz-other.txt": "o\n"})
    decisions = tmp_path / "decisions.tsv"
    decisions.write_text("zz-other.txt\tleave\n")
    r = run(["pull", "--baseline", "--decisions", str(decisions)], cwd=w.src)
    option, path = _assert_followable(run, w, r, baseline=True)
    assert path == "notes.txt" and option in ("leave", "remove", "restore")
    assert "--theirs" not in r.stderr


def test_managed_baseline_item(world, run):
    """A managed baseline item: the hint's flag is one of accept/ours/delete."""
    w = world()
    w.snapshot(extra={"a.py": "a = 'dirty, never committed'\n"}, dirty=True)
    r = run(["pull"], cwd=w.src)
    option, path = _assert_followable(run, w, r, baseline=True)
    assert path == "a.py" and option in ("accept", "ours", "delete")


def test_range_item_names_its_own_option(world, run):
    """An ordinary range item (a text conflict, C2: resolved/ours/theirs)
    gets an option it offers, never `resolved`, which is for edited markers
    at --continue; no --baseline in the listing command."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\ntheirs line\n"})
    w.commit_source({"docs/guide.md": "# Guide\nours line\n"})
    r = run(["pull"], cwd=w.src)
    option, path = _assert_followable(run, w, r, baseline=False)
    assert path == "docs/guide.md"
    assert "[C2]" in r.stdout and "resolved" in _offered(r.stdout, path)
    assert option != "resolved"
    assert git_run("status", "--porcelain", cwd=w.src) == ""
