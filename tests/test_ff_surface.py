"""Surface, exit codes, retirements, --amend (design §7, §9, §10).
 Every remote is a local bare
repo reached through file:// URLs.
"""
import os
import subprocess

import pytest

import pubrepo
from conftest import git_run
from ff_fixtures import forbidden_git_argv
from test_ff_publish import _bare, _blob_of, _commit, _pull_record, _tip, solo  # noqa: F401


# --- AC62 shim ----------------------------------------------------------

def test_shim_selftest_intercepts_real_pubrepo_git_calls(solo, run, git_policy):
    """surface::shim_selftest: a real publish's git calls pass through the
    shim, the push among them, so the routine policy is not vacuous."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    calls = git_policy.runs[-1]
    assert ["push", "--porcelain", "origin", f"{_tip(s.url)}:refs/heads/main"] in calls
    assert any(argv[:1] == ["fetch"] for argv in calls)


@pytest.mark.parametrize("argv", [
    ["push", "origin", ":refs/heads/x"],
    ["push", "--force", "origin", "a:refs/heads/main"],
    ["push", "--force-with-lease=refs/heads/main:abc", "origin", "a:refs/heads/main"],
    ["push", "origin", "+a:refs/heads/main"],
    ["push", "--delete", "origin", "x"],
    ["push", "--mirror", "origin"],
    ["fetch", "--prune", "origin"],
    ["fetch", "-f", "origin", "main"],
    ["tag", "-d", "v1.0.0"],
    ["tag", "-f", "v1.0.0"],
    ["update-ref", "-d", "refs/tags/v1.0.0"],
    ["-c", "core.hooksPath=/x/hooks", "push", "--force", "/x.git", "a:refs/heads/main"],
    ["-c", "core.hooksPath=/x/hooks", "push", "--no-verify", "/x.git", "a:refs/heads/main"],
])
def test_shim_negative_routine_forbidden_forms(argv):
    """surface::shim negative: every form is flagged for a routine run."""
    assert forbidden_git_argv(argv, set()) is not None


def test_shim_allows_rolling_back_a_tag_created_in_the_same_run():
    """INV-1 exception: deleting a tag the same run created."""
    assert forbidden_git_argv(["tag", "-d", "published/x"], {"published/x"}) is None
    assert forbidden_git_argv(["push", "origin", ":refs/tags/v1"], {"v1"}) is None
    assert forbidden_git_argv(["tag", "-d", "v0"], {"v1"}) is not None


def test_shim_sees_an_annotated_tag_creation():
    """`git tag -a <name> <sha> -m <msg>` creates <name>, so
    the same run may roll it back; -d/-f/listing calls create nothing."""
    from ff_fixtures import _tags_created
    assert _tags_created(["tag", "-a", "v1.0.0", "abc", "-m", "Release 1.0.0"]) == {"v1.0.0"}
    assert _tags_created(["tag", "published/x"]) == {"published/x"}
    assert _tags_created(["tag", "-d", "v1.0.0"]) == set()
    assert _tags_created(["tag", "-l"]) == set()


def test_shim_fails_a_routine_test_that_force_pushes(solo, run, git_policy, monkeypatch):
    """surface::shim negative, end to end: a synthetic `git push :refs/heads/x`
    from inside a routine CLI run is recorded and flagged."""
    s = solo()
    real_main = pubrepo.main

    def main_with_a_delete(argv):
        subprocess.run(["git", "push", "-q", "origin", ":refs/heads/nothing"],
                       cwd=s.repo / ".publish", capture_output=True)
        return real_main(argv)

    monkeypatch.setattr(pubrepo, "main", main_with_a_delete)
    run(["status"], cwd=s.repo)
    recorded = git_policy.runs.pop()        # keep this test's own teardown green
    flagged = [argv for argv in recorded if forbidden_git_argv(argv, set())]
    assert flagged == [["push", "-q", "origin", ":refs/heads/nothing"]]


# --- AC63: retirements -----------------------------------------------------------------

@pytest.mark.no_git_calls
@pytest.mark.parametrize("args", [["--force-overwrite"], ["publish", "--force-overwrite"],
                                  ["--force-overwrite", "--no-release"]])
def test_force_overwrite_is_1_with_zero_git_calls(source_repo, public_remote, run, args):
    """AC63: exit 1 before anything runs; the message points to pull
    and repair."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    r = run(args, cwd=repo)
    assert r.code == 1
    assert "`pubrepo pull`" in r.stderr and "`pubrepo repair`" in r.stderr


def test_on_republish_amend_is_a_config_error(solo, run):
    """AC63: on_republish = "amend" → 1 for publish, previews and validate."""
    s = solo()
    (s.repo / ".publish.toml").write_text((s.repo / ".publish.toml").read_text()
                                          + 'on_republish = "amend"\n')
    _commit(s.repo)
    for args in (["publish", "--yes"], ["--stage"], ["--dry-run"], ["validate"]):
        r = run(args, cwd=s.repo)
        assert r.code == 1, args
        assert "retired in 1.1.0" in r.stderr, args
    assert _tip(s.url) == s.first


def test_no_amend_is_a_noop_with_a_note(solo, run):
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["--no-amend", "--yes"], cwd=s.repo)
    assert r.code == 0
    assert "--no-amend has no effect" in r.stderr
    assert git_run("rev-parse", f"{_tip(s.url)}^", cwd=_bare(s.url)).strip() == s.first


# --- AC64: the --amend matrix (design §7) ------------------------------



@pytest.mark.rewrite
def test_amend_own_untagged_tip_writes_marker_and_new_record(solo, run):
    """AC64 / J1: the old record stays, status/<old>/rewritten and
    snap/<new> are written; the parent is unchanged."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    old = _tip(s.url)
    _commit(s.repo, {"src/app.py": "print(3)\n"})
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    new = _tip(s.url)
    assert git_run("rev-parse", f"{new}^", cwd=_bare(s.url)).strip() == s.first
    refs = git_run("for-each-ref", "--format=%(refname)", "refs/pubrepo/", cwd=s.repo).split()
    assert f"refs/pubrepo/snap/{old}" in refs and f"refs/pubrepo/snap/{new}" in refs
    assert f"refs/pubrepo/status/{old}/rewritten" in refs
    assert "git reset --keep origin/main" in r.stderr          # §6.6 block
    assert run(["publish", "--yes"], cwd=s.repo).code == 0                      # no false S7


@pytest.mark.rewrite
def test_amend_off_a_tty_without_yes_is_1_and_changes_nothing(solo, run):
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["--amend"], cwd=s.repo)
    # §17.3 (AC100): refused before anything runs.
    assert r.code == 1
    assert "publishing needs confirmation: run on a terminal, or pass --yes" in r.stderr
    assert _tip(s.url) == s.first


@pytest.mark.rewrite
def test_amend_tagged_tip_is_refused_naming_the_tag(solo, run):
    """A tag of any name on the tip blocks --amend (exit 2)."""
    s = solo()
    git_run("tag", "dependency-marker", s.first, cwd=_bare(s.url))
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 2
    assert f"the public tip {s.first[:12]} is tagged dependency-marker" in r.stderr
    assert _tip(s.url) == s.first


@pytest.mark.rewrite
def test_amend_on_a_pull_named_foreign_tip_is_refused(solo, run):
    """surface::amend_on_pull_named_foreign_tip_refused: tip == IP via
    a pull record, but not ours → exit 2."""
    s = solo()
    f = s.collab().commit({"notes.txt": "n\n"}, "collab")
    _pull_record(s.repo, s.config, f)
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 2
    assert f"the public tip {f[:12]} is not a pubrepo snapshot of this source" in r.stderr


@pytest.mark.rewrite
def test_amend_on_a_restore_ancestry_merge_snapshot_is_refused(solo, run):
    """surface::amend_on_restore_ancestry_merge_refused: our
    recorded snapshot with two parents → exit 2 naming the merge."""
    s = solo()
    pub = s.repo / ".publish"
    side = subprocess.run(["git", "commit-tree", f"{s.first}^{{tree}}", "-m", "released side"],
                          cwd=pub, capture_output=True, text=True, check=True).stdout.strip()
    sigma = git_run("rev-parse", "HEAD", cwd=s.repo).strip()
    merge = subprocess.run(["git", "commit-tree", f"{s.first}^{{tree}}", "-p", s.first,
                            "-p", side, "-m", f"restore ancestry\n\nSource: {sigma[:7]}"],
                           cwd=pub, capture_output=True, text=True, check=True).stdout.strip()
    git_run("push", "-q", "origin", f"{merge}:refs/heads/main", cwd=pub)
    pubrepo.write_snapshot_record(
        s.repo, snapshot_sha=merge, destination=pubrepo.destination_identity(s.config),
        sigma_full=sigma, written={}, nonderivable=[], keep=[], config_path=".publish.toml",
        fingerprint="test")
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 2
    assert f"the public tip {merge[:12]} is a merge commit" in r.stderr
    assert _tip(s.url) == merge


@pytest.mark.rewrite
def test_amend_tip_with_unverified_changes_is_8(solo, run):
    """A trailer-only tip whose delta is not verified (decided `leave`,
    so not S9) → exit 8 naming `pull --baseline`."""
    s = solo()
    sigma = git_run("rev-parse", "--short", "HEAD", cwd=s.repo).strip()
    t = s.collab().commit({"extra.txt": "added\n"}, f"fixup\n\nSource: {sigma}")
    _pull_record(s.repo, s.config, t, entries=[{
        "path": "extra.txt", "decision": "leave", "cls": "baseline_unmanaged", "flags": [],
        "theirs": _blob_of(s.url, t, "extra.txt"), "result": None}])
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 8, r.stderr
    assert "has unverified changes; run `pubrepo pull --baseline`" in r.stderr


@pytest.mark.rewrite
def test_amend_with_items_present_is_8(solo, run):
    """§9 `--amend` column: S9 → 8 before any amend check."""
    s = solo()
    f = s.collab().commit({"src/app.py": "print('collab')\n"}, "collab edit")
    _pull_record(s.repo, s.config, f)
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 8
    assert _tip(s.url) == f


@pytest.mark.rewrite
def test_amend_lease_race_is_5_and_changes_nothing(solo, run, monkeypatch):
    """§7: a lease rejected → 5; the racer survives; nothing local moves."""
    s = solo()
    collab = s.collab()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    real = pubrepo.push_porcelain

    def racing(*args, **kwargs):
        collab.commit({"racer.txt": "r\n"}, "racer")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "push_porcelain", racing)
    local = git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip()
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 5
    assert "racer.txt" in git_run("ls-tree", "--name-only", _tip(s.url), cwd=_bare(s.url))
    assert git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip() == local


@pytest.mark.rewrite
def test_amend_on_a_tty_confirms_through_confirm_then_lock(solo, run, monkeypatch):
    """§7: on a TTY the prompt names the commit and prints the §6.6
    block; "no" cancels with exit 1 and nothing written."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    import io
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))
    r = run(["--amend"], cwd=s.repo)
    assert r.code == 1
    # §17.3: the one [y/N] follows the commit, the consequences and the §6.6 block.
    assert r.stderr.count("[y/N]") == 1 and s.first[:12] in r.stderr
    assert "git reset --keep origin/main" in r.stderr
    assert _tip(s.url) == s.first


# --- exit codes (design §9) ------------------------------------------------------------

def test_exit_code_constants_table():
    assert (pubrepo.EXIT_SUCCESS, pubrepo.EXIT_CONFIG_ERROR, pubrepo.EXIT_PUBLISH_FAILED,
            pubrepo.EXIT_LOCK_CONTENTION, pubrepo.EXIT_SCRUB_FAILED, pubrepo.EXIT_DIVERGED,
            pubrepo.EXIT_PENDING_CHANGES, pubrepo.EXIT_RELEASE_FAILED,
            pubrepo.EXIT_DECISION_REQUIRED) == (0, 1, 2, 3, 4, 5, 6, 7, 8)
    assert pubrepo.DecisionRequired("x").exit_code == 8
    assert pubrepo.Diverged([], "x").exit_code == 5
    assert pubrepo.ConfigError("x").exit_code == 1


def test_integrate_is_an_alias_of_pull_plan(solo, run):
    """surface::integrate_alias (AC65; design §7, §10 item 3): `integrate`
    prints exactly what `pull --plan` prints, with the same exit code, plus a
    notice naming `pull --plan`; the 1.0 recipe and --force-overwrite are gone."""
    s = solo()
    s.collab().commit({"notes.txt": "n\n"}, "collab")
    plan = run(["pull", "--plan"], cwd=s.repo)
    alias = run(["integrate"], cwd=s.repo)
    assert plan.code == alias.code == 5, (plan.stderr, alias.stderr)
    assert alias.stdout == plan.stdout and "notes.txt" in alias.stdout
    assert "`pubrepo integrate` is now `pubrepo pull --plan`" in alias.stderr
    assert "--force-overwrite" not in alias.stdout + alias.stderr


@pytest.mark.no_git_calls  # a flag conflict, refused before any git call
@pytest.mark.parametrize("release", [["--release", "1.1.0"], ["--bump", "minor"]])
def test_amend_with_a_release_is_a_flag_conflict(source_repo, public_remote, run, release):
    """--amend with --release/--bump → 1 before any git call."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    r = run(["--amend", *release, "--yes"], cwd=repo)
    assert r.code == 1
    assert "--amend cannot be combined with a release" in r.stderr


def _subcommand_help(name: str) -> str:
    import argparse
    parser = pubrepo.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    return " ".join(sub.choices[name].format_help().split())        # argparse wraps


def test_help_text_matches_1_1_0_behavior():
    """Docs match (design): the help names
    1.1.0 behavior, not 1.0.0's."""
    publish, status = _subcommand_help("publish"), _subcommand_help("status")
    assert "Retired in 1.1.0: exits 1" in publish                       # AC63
    assert "--force-with-lease pinned" not in publish
    assert "Nuke" not in publish and "Build the snapshot publish would commit" in publish
    assert "exits with the code publish would (0, 4, 5 or 8" in publish  # §9
    assert "§" not in publish and "design" not in publish
    assert "5 foreign state on public" in status and "8 decision required" in status
    assert "precedence 4 > 8 > 5 > 6" in status                          # §9, AC10
    assert "unpulled public commits" in status and "divergence" not in status
