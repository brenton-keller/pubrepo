"""Previews and status on the git-tree baseline (design §4.5, §9,
INV-8). Every remote is a local bare
repo reached through file:// URLs.
"""
import json
import subprocess

import pytest

import pubrepo
from conftest import git_run
from test_ff_publish import _bare, _blob_of, _commit, _pull_record, _tip, solo  # noqa: F401


def _json(run, args, repo):
    r = run(args + ["--json"], cwd=repo)
    return r.code, json.loads(r.stdout)


def _worktree(root) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*"))
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def _changes(run, repo) -> tuple:
    code, data = _json(run, ["status"], repo)
    c = data["changes"]
    return tuple(sorted(c["added"])), tuple(sorted(c["modified"])), tuple(sorted(c["deleted"]))


def test_stage_does_not_change_what_previews_report(solo, run):
    """AC8 / bug #7: previews read the git-tree baseline, not the worktree,
    so --stage leaves every change set as it was."""
    s = solo(files={"src/app.py": "print(1)\n", "src/old.py": "o\n", "README.md": "readme\n"})
    _commit(s.repo, {"src/app.py": "print(2)\n", "src/new.py": "n\n"}, deletes=["src/old.py"])
    before = _changes(run, s.repo)
    dry_before = _json(run, ["--dry-run"], s.repo)[1]["files"]
    assert run(["--stage"], cwd=s.repo).code == 0
    assert _changes(run, s.repo) == before == (("src/new.py",), ("src/app.py",), ("src/old.py",))
    assert _json(run, ["--dry-run"], s.repo)[1]["files"] == dry_before
    diff = run(["--diff"], cwd=s.repo).stdout
    assert "+++ b/src/new.py" in diff and "Deleted: src/old.py" in diff


def test_stage_materializes_through_a_temporary_index(solo, run):
    """The worktree gets the snapshot tree, stale files go, keep files
    and the real index stay."""
    s = solo(keep=["LICENSE"])
    pub = s.repo / ".publish"
    (pub / "LICENSE").write_text("owner terms\n")
    (pub / "stale.txt").write_text("left over\n")
    index = (pub / ".git" / "index").read_bytes()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    assert run(["--stage"], cwd=s.repo).code == 0
    assert _worktree(pub) == {"LICENSE": b"owner terms\n", "README.md": b"readme\n",
                              "src/app.py": b"print(2)\n"}
    assert (pub / ".git" / "index").read_bytes() == index
    assert not list((pub / ".git").glob(f"{pubrepo.TEMP_INDEX_PREFIX}*"))


def test_offline_range_is_last_known_and_labelled(solo, run):
    """AC9: offline previews use the last fetched state and say so; --remote
    fetches and names the range; --dry-run then exits 5 offline too."""
    s = solo()
    f = s.collab().commit({"README.md": "collab\n"}, "collab readme")
    code, data = _json(run, ["--dry-run"], s.repo)
    assert code == 0 and data["remote_checked"] is False and data["unpulled"] == []
    assert "remote not checked" in data["baseline"]["label"]
    r = run(["--dry-run", "--remote"], cwd=s.repo)
    assert r.code == 5
    assert f[:7] in r.stdout and "remote checked" in r.stdout
    code, data = _json(run, ["--dry-run"], s.repo)
    assert code == 5 and data["remote_checked"] is False
    assert [u["sha"] for u in data["unpulled"]] == [f]
    assert data["remote_last_fetched"]


def test_stage_offline_json_and_remote_flag(solo, run):
    """AC9: offline --stage exits 0 with remote_checked false; --stage
    --remote checks the remote."""
    s = solo()
    s.collab().commit({"README.md": "collab\n"}, "collab readme")
    code, data = _json(run, ["--stage"], s.repo)
    assert code == 0 and data["remote_checked"] is False
    code, data = _json(run, ["--stage", "--remote"], s.repo)
    assert code == 0 and data["remote_checked"] is True and len(data["unpulled"]) == 1


def test_status_check_precedence(solo, run):
    """AC10: 4 > 8 > 5 > 6 > 0."""
    s = solo(scrub={"forbidden": ["sekrit"]})
    assert run(["status", "--check"], cwd=s.repo).code == 0
    (s.repo / "src" / "app.py").write_text("print('pending')\n")
    assert run(["status", "--check"], cwd=s.repo).code == 6
    s.collab().commit({"notes.txt": "n\n"}, "collab")
    assert run(["status", "--check", "--remote"], cwd=s.repo).code == 5
    f = s.collab().commit({"src/app.py": "print('collab')\n"}, "collab edit")
    _pull_record(s.repo, s.config, f)                 # pulled through, undecided → S9
    (s.repo / "src" / "app.py").write_text("print('pending')\n")
    assert run(["status", "--check", "--remote"], cwd=s.repo).code == 8
    (s.repo / "src" / "app.py").write_text("print('sekrit')\n")
    assert run(["status", "--check"], cwd=s.repo).code == 4


def test_every_preview_with_a_foreign_range(solo, run):
    """§9: with an unpulled range --dry-run exits 5, --stage and --diff
    exit 0, status --check exits 5; the stage shows our change on the IP tree."""
    s = solo()
    s.collab().commit({"README.md": "collab\n"}, "collab readme")
    assert run(["status", "--remote"], cwd=s.repo).code == 0     # fetch: last-known
    _commit(s.repo, {"src/app.py": "print('ours')\n"})
    assert run(["--dry-run"], cwd=s.repo).code == 5
    assert run(["--diff"], cwd=s.repo).code == 0
    assert run(["status", "--check"], cwd=s.repo).code == 5
    code, data = _json(run, ["--stage"], s.repo)
    assert code == 0 and data["baseline"]["ref"] == "ip"
    assert data["baseline"]["sha"] == s.first
    pub = s.repo / ".publish"
    assert (pub / "README.md").read_text() == "readme\n"           # IP content, not theirs
    assert (pub / "src" / "app.py").read_text() == "print('ours')\n"


def test_stage_in_s9_lists_items_at_base_bytes(solo, run):
    """--stage in S9 exits 0, lists the items, and leaves item paths
    byte-identical to the base."""
    s = solo()
    f = s.collab().commit({"src/app.py": "print('collab')\n"}, "collab edit")
    _pull_record(s.repo, s.config, f)
    _commit(s.repo, {"README.md": "ours\n"})
    run(["status", "--remote"], cwd=s.repo)
    code, data = _json(run, ["--stage"], s.repo)
    assert code == 0
    assert data["items"] == [{"path": "src/app.py", "class": "unaccounted"}]
    assert data["predicted_exit"] == 8
    pub = s.repo / ".publish"
    assert (pub / "src" / "app.py").read_text() == "print('collab')\n"
    assert (pub / "README.md").read_text() == "ours\n"
    assert run(["--dry-run"], cwd=s.repo).code == 8


def test_stage_equals_publish_with_a_foreign_range_pulled(solo, run, published_tree):
    """After a pull, --stage materializes byte for byte what publish then
    pushes, collaborator content included."""
    s = solo()
    f = s.collab().commit({"notes/collab.txt": "theirs\n"}, "collab file")
    _pull_record(s.repo, s.config, f)
    _commit(s.repo, {"src/app.py": "print(3)\n"})
    run(["status", "--remote"], cwd=s.repo)
    assert run(["--stage"], cwd=s.repo).code == 0
    staged = _worktree(s.repo / ".publish")
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    assert published_tree(s.url) == staged


def test_remote_unreachable_columns(solo, run, tmp_path):
    """§9 S5: --dry-run --remote → 2; --stage stays 0 on the last-known
    state, labelled; status reports the failure and uses last-known."""
    s = solo()
    git_run("remote", "set-url", "origin", (tmp_path / "gone.git").as_uri(),
            cwd=s.repo / ".publish")
    assert run(["--dry-run", "--remote"], cwd=s.repo).code == 2
    r = run(["--stage", "--remote"], cwd=s.repo)
    assert r.code == 0 and "last-known" in r.stderr
    r = run(["status", "--remote"], cwd=s.repo)
    assert r.code == 0 and "check failed" in r.stdout


def test_previews_in_s4_exit_8(solo, run):
    """§9 S4: a pull in progress in this worktree → 8 for --dry-run, --diff,
    --stage and status --check."""
    s = solo()
    git_dir = git_run("rev-parse", "--absolute-git-dir", cwd=s.repo).strip()
    pubrepo.write_in_progress(s.repo, pubrepo.InProgress(
        worktree=str(s.repo.resolve()), branch="main", git_dir=git_dir,
        config_path=str(s.repo / ".publish.toml"), digest="f" * 64, written={}))
    for args in (["--dry-run"], ["--diff"], ["--stage"], ["status", "--check"]):
        assert run(args, cwd=s.repo).code == 8, args


def test_gate_parity_and_allowlisted_in_every_preview(solo, run):
    """§4.5 gate parity: a blocking finding gives 4 in --dry-run, --stage,
    --diff and status --check; allowlisted content is reported in each."""
    s = solo(scrub={"allow_opaque": ["src/*.bin"]})
    (s.repo / "src" / "blob.bin").write_bytes(b"\x00binary")
    _commit(s.repo)
    for args in (["--dry-run"], ["--stage"], ["status"]):
        r = run(args, cwd=s.repo)
        assert "src/blob.bin: not scanned (allowlisted)" in r.stdout + r.stderr, args
    (s.repo / "src" / "conflict.py").write_text("<<<<<<< a\nx\n=======\ny\n>>>>>>> b\n")
    for args in (["--dry-run"], ["--stage"], ["--diff"], ["status", "--check"]):
        assert run(args, cwd=s.repo).code == 4, args


def test_dry_run_json_contract(solo, run):
    s = solo()
    f = s.collab().commit({"README.md": "collab\n"}, "collab readme")
    code, data = _json(run, ["--dry-run", "--remote"], s.repo)
    assert code == 5 == data["predicted_exit"]
    assert data["state"] == "S2" and data["remote_checked"] is True
    u = data["unpulled"][0]
    assert u["sha"] == f and "Collab Orator" in u["author"] and u["paths"] == ["README.md"]
    # The range changes a managed path: listed as an item, left at base bytes.
    assert data["items"] == [{"path": "README.md", "class": "unaccounted"}]
    assert data["carried_count"] == 0


def test_remote_previews_with_an_unreachable_remote_follow_section_9(solo, run, tmp_path):
    """Design §9 S5: with --remote and an unreachable
    remote, --diff and --stage exit 0 labelled "last-known"; --dry-run
    --remote exits 2."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    git_run("remote", "set-url", "origin", str(tmp_path / "missing.git"), cwd=s.repo / ".publish")
    for argv in (["--diff", "--remote"], ["--stage", "--remote"]):
        r = run(argv, cwd=s.repo)
        assert r.code == 0, (argv, r.stderr)
        assert "last-known remote state" in r.stderr, argv
    assert run(["--diff", "--remote"], cwd=s.repo).stdout.count("src/app.py") >= 1
    assert run(["--dry-run", "--remote"], cwd=s.repo).code == 2


def test_unreadable_managed_file_stays_at_base_content_in_previews(solo, run):
    """INV-8: a managed file that cannot be read is listed
    as unscannable, never as a deletion: publish stops on it and deletes
    nothing, and the previews agree."""
    import os
    s = solo()
    app = s.repo / "src" / "app.py"
    app.write_text("print(2)\n")
    os.chmod(app, 0)
    try:
        dry = run(["--dry-run"], cwd=s.repo)
        assert "deleted: src/app.py" not in dry.stdout + dry.stderr
        assert "src/app.py" in dry.stdout + dry.stderr          # named as unscannable
        diff = run(["--diff"], cwd=s.repo)
        assert diff.code == 4 and "src/app.py" not in diff.stdout   # no change shown for it
        stage = run(["--stage"], cwd=s.repo)
        assert (s.repo / ".publish" / "src" / "app.py").read_text() == "print(1)\n"
        assert stage.code == dry.code == 4
        publish = run(["publish", "--yes"], cwd=s.repo)
        assert publish.code != 0 and _tip(s.url) == s.first
    finally:
        os.chmod(app, 0o644)


def test_status_check_is_8_only_for_this_worktrees_pull(solo, run, tmp_path):
    """Design §9 "S4 (this worktree)", §5.12: a pull in
    progress in another worktree gives a notice, not 8; in this worktree, 8."""
    s = solo()
    other = tmp_path / "other-worktree"
    git_run("worktree", "add", "-q", "-b", "side", str(other), cwd=s.repo)

    def record(worktree):
        git_dir = git_run("rev-parse", "--absolute-git-dir", cwd=worktree).strip()
        return pubrepo.InProgress(worktree=str(worktree.resolve()), branch="main",
                                  git_dir=git_dir, config_path=".publish.toml",
                                  digest="f" * 64, written={})

    pubrepo.write_in_progress(other, record(other))
    r = run(["status", "--check"], cwd=s.repo)
    assert r.code == 0, r.stdout + r.stderr
    assert f"a pull is in progress in another worktree: {other.resolve()}" in r.stdout
    pubrepo.write_in_progress(s.repo, record(s.repo))
    r = run(["status", "--check"], cwd=s.repo)
    assert r.code == 8 and "Pull in progress" in r.stdout


_COMMANDS = (["publish", "--yes"], ["--dry-run"], ["status"])


@pytest.mark.parametrize("decision", ["accept", "delete"])
def test_include_removed_by_pull_names_the_pull(solo, run, decision):
    """Publish side: an explicit include that a pull removed
    → 1 in publish, --dry-run and status, naming the pull record and the
    config file; nothing is published."""
    from test_ff_publish import _push_commit
    s = solo()
    f = _push_commit(s, deletes=["README.md"])
    (s.repo / "README.md").unlink()                   # what the pull wrote
    _pull_record(s.repo, s.config, f, entries=[{
        "path": "README.md", "decision": decision, "cls": "theirs_deleted", "flags": [],
        "theirs": None, "result": None}])
    record = git_run("rev-parse", "HEAD", cwd=s.repo).strip()
    for argv in _COMMANDS:
        r = run(argv, cwd=s.repo)
        assert r.code == 1, (argv, r.stdout + r.stderr)
        assert (f"include 'README.md' names a path removed by pull {record[:12]}; "
                f"remove it from include in .publish.toml") in r.stderr, argv
    assert _tip(s.url) == f


def test_include_typo_gets_the_generic_hint(solo, run):
    """A missing include path no pull removed → 1 with the generic
    hint, in publish, --dry-run and status."""
    s = solo()
    (s.repo / ".publish.toml").write_text(
        (s.repo / ".publish.toml").read_text().replace('"README.md"', '"READMEE.md"'))
    git_run("commit", "-qam", "typo", cwd=s.repo)
    for argv in _COMMANDS:
        r = run(argv, cwd=s.repo)
        assert r.code == 1, (argv, r.stdout + r.stderr)
        assert ("include paths not found in working tree:\n  READMEE.md\n"
                "remove them from include in .publish.toml, or restore them "
                "(renamed? update include)") in r.stderr, argv
        assert "removed by pull" not in r.stderr


def test_include_hint_names_the_loaded_config_file(solo, run):
    """A `.publish-<name>.toml` target names itself."""
    s = solo()
    alt = s.repo / ".publish-sdk.toml"
    alt.write_text((s.repo / ".publish.toml").read_text().replace('"README.md"', '"GONE.md"'))
    git_run("add", ".publish-sdk.toml", cwd=s.repo)
    git_run("commit", "-qm", "sdk target", cwd=s.repo)
    r = run(["status", "--config", ".publish-sdk.toml"], cwd=s.repo)
    assert r.code == 1, r.stdout + r.stderr
    assert "remove them from include in .publish-sdk.toml, or restore them" in r.stderr


def test_status_under_contention_shows_last_known_state(solo, run, git_policy):
    """Design §5.13, AC42, module half: with another command
    holding the source lock, status (even --remote) takes no lock and makes
    no fetch, labels the last-known state, and exits from it; other previews
    still exit 3. (The AC42 command-level test is in test_ff_pull.)"""
    import fcntl
    s = solo()
    (s.repo / "src" / "app.py").write_text("print('pending')\n")
    lock_path = pubrepo.source_common_dir(s.repo) / pubrepo.SOURCE_LOCK_NAME
    with open(lock_path, "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        try:
            r = run(["status", "--check", "--remote"], cwd=s.repo)
            assert r.code == 6, r.stdout + r.stderr          # pending, from the last-known state
            assert "Note: another pubrepo command is running; showing last known state" in r.stdout
            network = [a for a in git_policy.runs[-1] if a[:1] in (["fetch"], ["ls-remote"])]
            assert network == []
            r = run(["status", "--json"], cwd=s.repo)
            data = json.loads(r.stdout)
            assert data["contended"] is True and "last known state" in data["note"]
            assert run(["--dry-run"], cwd=s.repo).code == 3
        finally:
            fcntl.flock(held.fileno(), fcntl.LOCK_UN)
    r = run(["status", "--check"], cwd=s.repo)
    assert r.code == 6 and "another pubrepo command is running" not in r.stdout


def test_dry_run_blocked_by_the_gate_shows_no_passing_scrub_line(solo, run):
    """Docs match: a --dry-run that the gate blocks
    (exit 4) prints SCRUB WOULD FAIL and no commit-message preview, so no
    "Scrub: N pattern(s) passed" line contradicts it. Unblocked, the
    preview and its passing Scrub line are shown."""
    s = solo(scrub={"forbidden": ["sekrit"]})
    (s.repo / "src" / "app.py").write_text("print('pending')\n")
    r = run(["--dry-run"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "Commit message:" in r.stdout and "Scrub: 1 pattern(s) passed" in r.stdout
    (s.repo / "src" / "app.py").write_text("print('sekrit')\n")
    r = run(["--dry-run"], cwd=s.repo)
    assert r.code == 4, r.stdout + r.stderr
    assert "SCRUB WOULD FAIL" in r.stdout + r.stderr
    assert "pattern(s) passed" not in r.stdout + r.stderr
    assert "Commit message:" not in r.stdout
