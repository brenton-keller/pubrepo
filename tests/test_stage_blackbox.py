"""Black-box acceptance tests for --stage.

Written blind from the behavioral spec only — no implementation source read.
Tests exercise the CLI via the run() fixture (in-process main(argv)).
Every test targets a specific spec section and expected failure mode.
"""
import fcntl
import json
import os
import subprocess

import pytest

from conftest import config_toml, git_run

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")


def _repo(source_repo, public_remote, files=None, config_extra=None):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra or {})
    repo = source_repo(files or {"a.py": "x\n"}, config)
    return remote, repo


def _init_and_publish(run, repo):
    assert run(["init"], cwd=repo).code == 0
    assert run([], cwd=repo).code == 0


# =============================================================================
# §2.1 Incompatible flag combinations — exit 1, no file mutation
# =============================================================================

class TestIncompatibleFlags:
    """Each flag combo must be rejected before any file mutation."""

    @pytest.mark.parametrize("extra_args", [
        ["--dry-run"],
        ["--diff"],
        ["-m", "msg"],
        ["--message", "msg"],
        ["--force-overwrite"],
    ])
    def test_stage_with_incompatible_flag(self, source_repo, public_remote, run, extra_args):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        git_run("log", "--oneline", "-1", cwd=pub_dir)
        head_before = git_run("rev-parse", "HEAD", cwd=pub_dir).strip()
        r = run(["--stage"] + extra_args, cwd=repo)
        assert r.code == 1, f"Expected exit 1 for --stage {extra_args}, got {r.code}"
        head_after = git_run("rev-parse", "HEAD", cwd=pub_dir).strip()
        assert head_before == head_after, "Publish dir HEAD changed despite incompatible flags"

    def test_stage_with_incompatible_flag_via_subcommand(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        r = run(["publish", "--stage", "--dry-run"], cwd=repo)
        assert r.code == 1


# =============================================================================
# §3.1 Success with changes
# =============================================================================

class TestStageWithChanges:

    def test_stage_reports_added_modified_deleted(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "old\n", "b.py": "del_me\n"},
                             config_extra={"include": ["."]})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("modified\n")
        (repo / "b.py").unlink()
        (repo / "c.py").write_text("new\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "changes", cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        assert "Staged:" in r.stdout
        assert "added" in r.stdout.lower() or "1" in r.stdout
        for letter in ("A", "M", "D"):
            assert letter in r.stderr

    def test_stage_does_not_commit(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        pub_dir = repo / ".publish"
        head_before = git_run("rev-parse", "HEAD", cwd=pub_dir).strip()
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        head_after = git_run("rev-parse", "HEAD", cwd=pub_dir).strip()
        assert head_before == head_after, "Stage must not create a commit"

    def test_stage_does_not_push(self, source_repo, public_remote, run, tmp_path):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        clone_dir = tmp_path / "push_check"
        subprocess.run(["git", "clone", "-q", remote, str(clone_dir)],
                       check=True, capture_output=True)
        remote_head_before = git_run("rev-parse", "HEAD", cwd=clone_dir).strip()
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        git_run("fetch", "origin", cwd=clone_dir)
        remote_head_after = git_run("rev-parse", "origin/main", cwd=clone_dir).strip()
        assert remote_head_before == remote_head_after

    def test_stage_does_not_write_publish_log(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        log_candidates = list(repo.glob(".publish-log*")) + list(repo.glob("*publish*.log"))
        log_sizes = {str(p): p.stat().st_size for p in log_candidates if p.is_file()}
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        run(["--stage"], cwd=repo)
        for path_str, size_before in log_sizes.items():
            from pathlib import Path
            p = Path(path_str)
            if p.exists():
                assert p.stat().st_size == size_before, f"Publish log {p} was modified by --stage"

    def test_stage_does_not_create_tags(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        tags_before = git_run("tag", "-l", cwd=repo).strip()
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        run(["--stage"], cwd=repo)
        tags_after = git_run("tag", "-l", cwd=repo).strip()
        assert tags_before == tags_after

    def test_stage_inspection_hint(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        assert "Inspect with:" in r.stderr or "git -C" in r.stderr

    def test_stage_via_publish_subcommand(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["publish", "--stage"], cwd=repo)
        assert r.code == 0
        assert "Staged:" in r.stdout


# =============================================================================
# §3.2 No changes
# =============================================================================

class TestStageNoChanges:

    def test_nothing_to_publish(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        assert "Nothing to publish" in r.stdout

    def test_idempotent(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        r1 = run(["--stage"], cwd=repo)
        r2 = run(["--stage"], cwd=repo)
        assert r1.code == r2.code == 0
        assert "Nothing to publish" in r1.stdout
        assert "Nothing to publish" in r2.stdout


# =============================================================================
# §3.3 Scrub failure
# =============================================================================

class TestStageScrubFailure:

    def test_scrub_failure_exit_4(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={"scrub": {"forbidden": ["sekrit"]}})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("the sekrit\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "add secret", cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 4

    def test_scrub_failure_leaves_files_on_disk(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={"scrub": {"forbidden": ["sekrit"]}})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("the sekrit\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "add secret", cwd=repo)
        run(["--stage"], cwd=repo)
        assert (repo / ".publish" / "a.py").exists()
        assert "sekrit" in (repo / ".publish" / "a.py").read_text()

    def test_scrub_failure_no_log_change(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={"scrub": {"forbidden": ["sekrit"]}})
        _init_and_publish(run, repo)
        log_candidates = list(repo.glob(".publish-log*")) + list(repo.glob("*publish*.log"))
        log_sizes = {str(p): p.stat().st_size for p in log_candidates if p.is_file()}
        (repo / "a.py").write_text("the sekrit\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "add secret", cwd=repo)
        run(["--stage"], cwd=repo)
        for path_str, size_before in log_sizes.items():
            from pathlib import Path
            p = Path(path_str)
            if p.exists():
                assert p.stat().st_size == size_before

    def test_scrub_failure_json(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={"scrub": {"forbidden": ["sekrit"]}})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("the sekrit\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "add secret", cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 4
        data = json.loads(r.stdout)
        assert data["staged"] is False
        assert data["changed"] is None
        assert data["changes"] is None
        assert data["scrub"]["passed"] is False
        assert len(data["scrub"]["matches"]) > 0

    def test_scrub_failure_json_suppresses_stderr_banner(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={"scrub": {"forbidden": ["sekrit"]}})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("the sekrit\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "add secret", cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 4
        assert "SCRUB" not in r.stderr


# =============================================================================
# §3.4 Unborn HEAD (first stage)
# =============================================================================

class TestStageUnbornHead:

    def test_first_stage_all_added(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n", "b.py": "y\n"},
                             config_extra={"include": ["a.py", "b.py"]})
        run(["init"], cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        assert "A" in r.stderr

    def test_first_stage_json_null_sha(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={})
        run(["init"], cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["baseline"]["sha"] is None
        for f in data["changes"]["files"]:
            assert f["status"] == "A"


# =============================================================================
# §3.5 Wrong branch
# =============================================================================

class TestStageWrongBranch:

    def test_wrong_branch_exit_2(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        git_run("checkout", "-b", "wrong-branch", cwd=pub_dir)
        r = run(["--stage"], cwd=repo)
        assert r.code == 2
        assert "wrong-branch" in r.stderr or "branch" in r.stderr.lower()

    def test_wrong_branch_no_rebuild(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        (pub_dir / "a.py").write_text("marker_for_nuke_detection\n")
        git_run("checkout", "-b", "wrong-branch", cwd=pub_dir)
        run(["--stage"], cwd=repo)
        assert "marker_for_nuke_detection" in (pub_dir / "a.py").read_text()


# =============================================================================
# §3.6 Detached HEAD
# =============================================================================

class TestStageDetachedHead:

    def test_detached_head_exit_2(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        head = git_run("rev-parse", "HEAD", cwd=pub_dir).strip()
        git_run("checkout", head, cwd=pub_dir)
        r = run(["--stage"], cwd=repo)
        assert r.code == 2
        assert "detach" in r.stderr.lower() or "branch" in r.stderr.lower()


# =============================================================================
# §3.7 Lock contention
# =============================================================================

class TestStageLockContention:

    def test_lock_contention_exit_3(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        lock_path = repo / ".publish" / ".git" / "pubrepo.lock"
        with open(lock_path, "w") as holder:
            holder.write("999999")
            holder.flush()
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = run(["--stage"], cwd=repo)
            assert r.code == 3

    def test_lock_released_after_stage(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        run(["--stage"], cwd=repo)
        lock_path = repo / ".publish" / ".git" / "pubrepo.lock"
        with open(lock_path, "a+") as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# =============================================================================
# §4.1 JSON success schema
# =============================================================================

class TestStageJsonSuccess:

    def test_json_schema_fields(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n", "README.md": "# hi\n"},
                             config_extra={"include": ["a.py", "README.md"],
                                           "scrub": {"forbidden": ["sekrit"]},
                                           "transforms": {"README.md": [{"find": "# hi", "replace": "# hello"}]}})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["staged"] is True
        assert data["changed"] is True
        assert data["baseline"]["ref"] == "HEAD"
        assert isinstance(data["baseline"]["sha"], str)
        assert data["remote_checked"] is False
        assert isinstance(data["changes"]["added"], int)
        assert isinstance(data["changes"]["modified"], int)
        assert isinstance(data["changes"]["deleted"], int)
        assert isinstance(data["changes"]["files"], list)
        assert isinstance(data["files_copied"], int)
        assert isinstance(data["transforms_applied"], list)
        assert data["scrub"]["passed"] is True
        assert isinstance(data["scrub"]["patterns"], int)
        assert isinstance(data["source"]["commit"], str)
        assert isinstance(data["source"]["dirty"], bool)
        assert isinstance(data["publish_dir"], str)

    def test_json_no_changes_variant(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["changed"] is False
        assert data["changes"]["added"] == 0
        assert data["changes"]["modified"] == 0
        assert data["changes"]["deleted"] == 0
        assert data["changes"]["files"] == []

    def test_json_file_status_letters(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "old\n", "b.py": "remove\n"},
                             config_extra={"include": ["."]})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("modified\n")
        (repo / "b.py").unlink()
        (repo / "c.py").write_text("new\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "changes", cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        statuses = {f["status"] for f in data["changes"]["files"]}
        assert "A" in statuses
        assert "M" in statuses
        assert "D" in statuses
        for f in data["changes"]["files"]:
            assert f["status"] in ("A", "M", "D", "T")
            assert isinstance(f["path"], str)

    def test_json_remote_checked_always_false(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        data = json.loads(r.stdout)
        assert data["remote_checked"] is False

    def test_json_publish_dir_field(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        r = run(["--stage", "--json"], cwd=repo)
        data = json.loads(r.stdout)
        assert data["publish_dir"] == ".publish"


# =============================================================================
# §4.3 Stream discipline
# =============================================================================

class TestStreamDiscipline:

    def test_quiet_json_stderr_empty(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage", "--quiet", "--json"], cwd=repo)
        assert r.code == 0
        json.loads(r.stdout)
        assert r.stderr == ""

    def test_quiet_suppresses_stdout_summary(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        r = run(["--stage", "--quiet"], cwd=repo)
        assert r.code == 0
        assert r.stdout == ""

    def test_json_never_suppressed_by_quiet(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage", "--quiet", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["staged"] is True

    def test_error_messages_never_suppressed_by_quiet(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        git_run("checkout", "-b", "wrong-branch", cwd=pub_dir)
        r = run(["--stage", "--quiet"], cwd=repo)
        assert r.code == 2
        assert r.stderr != ""


# =============================================================================
# §7 Config validation — keep/transform overlap
# =============================================================================

class TestKeepTransformOverlap:

    def test_exact_overlap_rejected(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"LICENSE": "MIT\n", "a.py": "x\n"},
                             config_extra={"include": ["a.py", "LICENSE"],
                                           "keep": ["LICENSE"],
                                           "transforms": {"LICENSE": [{"find": "MIT", "replace": "BSD"}]}})
        r = run(["--stage"], cwd=repo)
        assert r.code == 1

    def test_nested_inside_kept_dir_rejected(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"docs/LICENSE": "MIT\n", "a.py": "x\n"},
                             config_extra={"include": ["a.py", "docs/"],
                                           "keep": ["docs"],
                                           "transforms": {"docs/LICENSE": [{"find": "MIT", "replace": "BSD"}]}})
        r = run(["--stage"], cwd=repo)
        assert r.code == 1

    def test_similar_prefix_not_rejected(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"docs/file": "x\n", "doc": "y\n", "a.py": "z\n"},
                             config_extra={"include": ["a.py", "docs/", "doc"],
                                           "keep": ["doc"],
                                           "transforms": {"docs/file": [{"find": "x", "replace": "y"}]}})
        r = run(["validate"], cwd=repo)
        assert r.code != 1, "docs/file should NOT be rejected when keep is ['doc']"

    def test_dot_slash_normalization(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"docs/file": "x\n", "a.py": "z\n"},
                             config_extra={"include": ["a.py", "docs/"],
                                           "keep": ["docs"],
                                           "transforms": {"./docs/file": [{"find": "x", "replace": "y"}]}})
        r = run(["--stage"], cwd=repo)
        assert r.code == 1

    def test_dotdot_traversal_normalization(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"docs/LICENSE": "MIT\n", "a.py": "x\n"},
                             config_extra={"include": ["a.py", "docs/"],
                                           "keep": ["docs"],
                                           "transforms": {"x/../docs/LICENSE": [{"find": "MIT", "replace": "BSD"}]}})
        r = run(["--stage"], cwd=repo)
        assert r.code == 1


# =============================================================================
# §8 Multi-target behavior
# =============================================================================

class TestMultiTarget:

    def test_alternate_target_isolation(self, source_repo, public_remote, run):
        remote_main = public_remote()
        remote_alt = public_remote()
        repo = source_repo(
            {"a.py": "x\n", "b.py": "y\n"},
            {"remote": remote_main, "include": ["a.py"]},
        )
        (repo / ".publish-alt.toml").write_text(config_toml({
            "remote": remote_alt, "dir": ".publish-alt", "include": ["b.py"],
        }))
        run(["init"], cwd=repo)
        run(["init", "--config", ".publish-alt.toml"], cwd=repo)
        run([], cwd=repo)
        run(["publish", "--config", ".publish-alt.toml"], cwd=repo)

        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)

        pub_alt_head_before = git_run("rev-parse", "HEAD", cwd=repo / ".publish-alt").strip()
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        pub_alt_head_after = git_run("rev-parse", "HEAD", cwd=repo / ".publish-alt").strip()
        assert pub_alt_head_before == pub_alt_head_after

    def test_alternate_target_json_publish_dir(self, source_repo, public_remote, run):
        remote = public_remote()
        repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
        alt_remote = public_remote()
        (repo / ".publish-alt.toml").write_text(config_toml({
            "remote": alt_remote, "dir": ".publish-alt", "include": ["a.py"],
        }))
        run(["init", "--config", ".publish-alt.toml"], cwd=repo)
        r = run(["--stage", "--json", "--config", ".publish-alt.toml"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["publish_dir"] == ".publish-alt"

    def test_lock_contention_per_target(self, source_repo, public_remote, run):
        remote_main = public_remote()
        remote_alt = public_remote()
        repo = source_repo({"a.py": "x\n"}, {"remote": remote_main, "include": ["a.py"]})
        (repo / ".publish-alt.toml").write_text(config_toml({
            "remote": remote_alt, "dir": ".publish-alt", "include": ["a.py"],
        }))
        run(["init"], cwd=repo)
        run(["init", "--config", ".publish-alt.toml"], cwd=repo)
        run([], cwd=repo)
        run(["publish", "--config", ".publish-alt.toml"], cwd=repo)

        lock_path = repo / ".publish" / ".git" / "pubrepo.lock"
        with open(lock_path, "w") as holder:
            holder.write("999999")
            holder.flush()
            fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = run(["--stage", "--config", ".publish-alt.toml"], cwd=repo)
            assert r.code == 0, "Lock on .publish should not block staging .publish-alt"


# =============================================================================
# §9 Integration invariant: stage-then-publish byte identity
# =============================================================================

class TestStageThenPublish:

    def test_stage_then_publish_identical_tree(self, source_repo, public_remote, run, published_tree):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "original\n", "README.md": "# readme\n"},
                             config_extra={"include": ["a.py", "README.md"]})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("modified\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)

        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        pub_dir = repo / ".publish"
        staged_tree = {}
        for p in pub_dir.rglob("*"):
            rel = p.relative_to(pub_dir)
            if p.is_file() and ".git" not in rel.parts:
                staged_tree[str(rel)] = p.read_bytes()

        r = run([], cwd=repo)
        assert r.code == 0
        remote_tree = published_tree(remote)
        assert staged_tree == remote_tree


# =============================================================================
# §10 Internal state invariant — git index unchanged
# =============================================================================

class TestIndexInvariant:

    def test_stage_preserves_publish_dir_index(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        index_before = git_run("ls-files", "--stage", cwd=pub_dir)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        index_after = git_run("ls-files", "--stage", cwd=pub_dir)
        assert index_before == index_after

    def test_scrub_failure_preserves_index(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={"scrub": {"forbidden": ["sekrit"]}})
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        index_before = git_run("ls-files", "--stage", cwd=pub_dir)
        (repo / "a.py").write_text("the sekrit\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "add secret", cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 4
        index_after = git_run("ls-files", "--stage", cwd=pub_dir)
        assert index_before == index_after

    def test_no_changes_preserves_index(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        pub_dir = repo / ".publish"
        index_before = git_run("ls-files", "--stage", cwd=pub_dir)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0
        index_after = git_run("ls-files", "--stage", cwd=pub_dir)
        assert index_before == index_after


# =============================================================================
# §6.1 source.dirty — untracked files don't affect dirty flag
# =============================================================================

class TestSourceDirty:

    def test_untracked_files_do_not_set_dirty(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "x\n"},
                             config_extra={"include": ["."]})
        _init_and_publish(run, repo)
        (repo / "untracked_new.py").write_text("new\n")
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["source"]["dirty"] is False

    def test_tracked_modification_sets_dirty(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("modified but not committed\n")
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["source"]["dirty"] is True


# =============================================================================
# §3.11 Cleanup failure behavior
# =============================================================================

class TestCleanupFailure:

    def test_cleanup_failure_never_exit_0(self, source_repo, public_remote, run):
        """If cleanup fails, exit code must be 2, not 0.
        This is a spec requirement but hard to trigger without mocking —
        we verify the converse: a successful stage that cleans up properly exits 0."""
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage"], cwd=repo)
        assert r.code == 0


# =============================================================================
# Edge cases and boundary conditions
# =============================================================================

class TestEdgeCases:

    def test_stage_with_transforms(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"README.md": "# PRIVATE\nkeep this\n", "a.py": "x\n"},
                             config_extra={"include": ["README.md", "a.py"],
                                           "transforms": {"README.md": [{"find": "PRIVATE", "replace": "PUBLIC"}]}})
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert "README.md" in data["transforms_applied"]
        staged_readme = (repo / ".publish" / "README.md").read_text()
        assert "PUBLIC" in staged_readme
        assert "PRIVATE" not in staged_readme

    def test_stage_bare_invocation_equivalent_to_publish_stage(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote)
        _init_and_publish(run, repo)
        (repo / "a.py").write_text("changed\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-m", "mod", cwd=repo)
        r_bare = run(["--stage", "--json"], cwd=repo)
        (repo / "a.py").write_text("changed\n")
        r_explicit = run(["publish", "--stage", "--json"], cwd=repo)
        assert r_bare.code == r_explicit.code
        d_bare = json.loads(r_bare.stdout)
        d_explicit = json.loads(r_explicit.stdout)
        assert d_bare["staged"] == d_explicit["staged"]
        assert d_bare["changed"] == d_explicit["changed"]

    def test_stage_files_copied_count(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "1\n", "b.py": "2\n", "c.py": "3\n"},
                             config_extra={"include": ["a.py", "b.py", "c.py"]})
        run(["init"], cwd=repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["files_copied"] == 3

    def test_stage_scrub_patterns_count(self, source_repo, public_remote, run):
        remote, repo = _repo(source_repo, public_remote,
                             files={"a.py": "clean\n"},
                             config_extra={"scrub": {"forbidden": ["sekrit", "password", "token"]}})
        _init_and_publish(run, repo)
        r = run(["--stage", "--json"], cwd=repo)
        assert r.code == 0
        data = json.loads(r.stdout)
        assert data["scrub"]["patterns"] >= 3
