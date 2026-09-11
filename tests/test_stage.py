"""Implementation-aware tests for the --stage feature.

Tests reference internal mechanics: ephemeral index, baseline_sha resolution,
_count_changes -z parsing, keep/transform validation, and JSON schema fields.
Complements test_stage_blackbox.py (blind contract tests).
"""
import fcntl
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pubrepo
from conftest import config_toml, git_run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _setup(source_repo, public_remote, run, files=None, config_extra=None):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra or {})
    repo = source_repo(files or {"a.py": "x\n"}, config)
    run(["init"], cwd=repo)
    run([], cwd=repo)  # initial publish
    return remote, repo


def _log_path(repo):
    return repo / ".publish" / ".git" / pubrepo.PUBLISH_LOG_NAME


def _log_bytes(repo):
    p = _log_path(repo)
    return p.read_bytes() if p.exists() else b""


def _tree_hash(root: Path) -> dict[str, str]:
    """SHA256 of every non-.git file under root."""
    hashes = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.relative_to(root).parts:
            hashes[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return hashes


# ===========================================================================
# Test 1: Builds real content
# ===========================================================================

def test_stage_builds_real_content(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "x\n", "secret.py": "PRIVATE\n"},
                          config_extra={"include": ["a.py"],
                                        "exclude": ["secret.py"]})
    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage"], cwd=repo)
    assert r.code == 0

    pub = repo / ".publish"
    assert (pub / "a.py").read_text() == "changed\n"
    assert not (pub / "secret.py").exists()


# ===========================================================================
# Test 2: No commit, no push, no log, no tag
# ===========================================================================

def test_no_commit_no_push_no_log_no_tag(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"

    head_before = git_run("rev-parse", "HEAD", cwd=pub).strip()
    log_before = git_run("log", "--oneline", cwd=pub).strip()
    log_bytes_before = _log_bytes(repo)
    tags_before = git_run("tag", "-l", cwd=repo).strip()

    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage"], cwd=repo)
    assert r.code == 0

    assert git_run("rev-parse", "HEAD", cwd=pub).strip() == head_before
    assert git_run("log", "--oneline", cwd=pub).strip() == log_before
    assert _log_bytes(repo) == log_bytes_before
    assert git_run("tag", "-l", cwd=repo).strip() == tags_before

    clone = tmp_path / "push_check"
    subprocess.run(["git", "clone", "-q", remote, str(clone)],
                   check=True, capture_output=True)
    assert git_run("rev-parse", "HEAD", cwd=clone).strip() == head_before


# ===========================================================================
# Test 3: Change report accuracy
# ===========================================================================

def test_change_report_accuracy(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "old\n", "b.py": "del_me\n"},
                          config_extra={"include": ["."]})
    (repo / "a.py").write_text("modified\n")
    (repo / "b.py").unlink()
    (repo / "c.py").write_text("new\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "changes", cwd=repo)

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)
    assert data["changes"]["added"] == 1
    assert data["changes"]["modified"] == 1
    assert data["changes"]["deleted"] == 1

    files_by_path = {f["path"]: f["status"] for f in data["changes"]["files"]}
    assert files_by_path["c.py"] == "A"
    assert files_by_path["a.py"] == "M"
    assert files_by_path["b.py"] == "D"


# ===========================================================================
# Test 4: Real index byte-identical
# ===========================================================================

def test_real_index_unchanged_after_stage(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"
    index_before = (pub / ".git" / "index").read_bytes()

    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage"], cwd=repo)
    assert r.code == 0
    assert (pub / ".git" / "index").read_bytes() == index_before


def test_real_index_unchanged_no_changes(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"
    index_before = (pub / ".git" / "index").read_bytes()

    r = run(["--stage"], cwd=repo)
    assert r.code == 0
    assert (pub / ".git" / "index").read_bytes() == index_before


# ===========================================================================
# Test 5: No-change case
# ===========================================================================

def test_no_change_case(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)

    r = run(["--stage"], cwd=repo)
    assert r.code == 0
    assert "Nothing to publish" in r.stdout

    r2 = run(["--stage"], cwd=repo)
    assert r2.code == 0
    assert "Nothing to publish" in r2.stdout


# ===========================================================================
# Test 6: Scrub failure
# ===========================================================================

def test_scrub_failure(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          config_extra={"scrub": {"forbidden": ["sekrit"]}})
    pub = repo / ".publish"
    log_before = _log_bytes(repo)

    (repo / "a.py").write_text("the sekrit\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "add sekrit", cwd=repo)

    r = run(["--stage"], cwd=repo)
    assert r.code == 4
    assert (pub / "a.py").exists()
    assert _log_bytes(repo) == log_before


# ===========================================================================
# Test 7: JSON — all schemas
# ===========================================================================

def test_json_success_with_changes(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          config_extra={"scrub": {"forbidden": ["sekrit"]}})
    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)

    assert data["staged"] is True
    assert data["changed"] is True
    assert isinstance(data["baseline"]["sha"], str) and len(data["baseline"]["sha"]) > 0
    assert data["baseline"]["ref"] == "HEAD"
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
    assert data["publish_dir"] == ".publish"


def test_json_no_changes(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)

    assert data["staged"] is True
    assert data["changed"] is False
    assert data["changes"]["added"] == 0
    assert data["changes"]["modified"] == 0
    assert data["changes"]["deleted"] == 0
    assert data["changes"]["files"] == []


def test_json_scrub_failure(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          config_extra={"scrub": {"forbidden": ["sekrit"]}})
    (repo / "a.py").write_text("the sekrit\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "add sekrit", cwd=repo)

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 4
    data = json.loads(r.stdout)

    assert data["staged"] is False
    assert data["changed"] is None
    assert data["changes"] is None
    assert data["scrub"]["passed"] is False
    assert isinstance(data["scrub"]["matches"], list)
    assert len(data["scrub"]["matches"]) > 0
    assert "SCRUB FAILED" not in r.stderr


def test_json_unborn_head(source_repo, public_remote, run, tmp_path):
    """Unborn HEAD (first stage): baseline.sha is null, all files Added."""
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"},
                       {"remote": remote, "include": ["a.py"]})
    pub = repo / ".publish"
    pub.mkdir(exist_ok=True)
    git_run("init", "-q", "-b", "main", cwd=pub)
    git_run("config", "user.email", "test@pubrepo.test", cwd=pub)
    git_run("config", "user.name", "Pubrepo Tests", cwd=pub)
    git_run("remote", "add", "origin", remote, cwd=pub)

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)

    assert data["baseline"]["sha"] is None
    assert data["changed"] is True
    for f in data["changes"]["files"]:
        assert f["status"] == "A"


# ===========================================================================
# Test 8: CLI rejects
# ===========================================================================

def test_cli_rejects(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)

    r = run(["--stage", "--dry-run"], cwd=repo)
    assert r.code == 1
    assert "incompatible" in r.stderr

    r = run(["publish", "--stage", "--dry-run"], cwd=repo)
    assert r.code == 1

    r = run(["--stage", "--diff"], cwd=repo)
    assert r.code == 1

    r = run(["--stage", "-m", "msg"], cwd=repo)
    assert r.code == 1

    r = run(["--stage", "--force-overwrite"], cwd=repo)
    assert r.code == 1


# ===========================================================================
# Test 9: Lock contention
# ===========================================================================

def test_lock_contention(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    lock_path = repo / ".publish" / ".git" / "pubrepo.lock"

    with open(lock_path, "w") as holder:
        holder.write("999999")
        holder.flush()
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        r = run(["--stage"], cwd=repo)
        assert r.code == 3
        assert "already running" in r.stderr


# ===========================================================================
# Test 10: Interrupt mid-copy
# ===========================================================================

def test_interrupt_mid_copy(source_repo, public_remote, run, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"
    head_before = git_run("rev-parse", "HEAD", cwd=pub).strip()

    (repo / "a.py").write_text("v2\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    real_copy = pubrepo.copy_includes

    def interrupting_copy(*args, **kwargs):
        real_copy(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(pubrepo, "copy_includes", interrupting_copy)
    r = run(["--stage"], cwd=repo)
    assert r.code == 130

    assert git_run("rev-parse", "HEAD", cwd=pub).strip() == head_before

    lock_path = pub / ".git" / "pubrepo.lock"
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# ===========================================================================
# Test 11: Quiet/verbose/quiet-json
# ===========================================================================

def test_stage_quiet(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage", "--quiet"], cwd=repo)
    assert r.code == 0
    assert r.stdout == ""
    assert r.stderr == ""


def test_stage_verbose(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage", "--verbose"], cwd=repo)
    assert r.code == 0
    assert "timing:" in r.stderr


def test_stage_quiet_json(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage", "--quiet", "--json"], cwd=repo)
    assert r.code == 0
    json.loads(r.stdout)
    assert r.stderr == ""


# ===========================================================================
# Test 12: Multi-target
# ===========================================================================

def test_multi_target(source_repo, public_remote, run, tmp_path):
    remote1 = public_remote()
    remote2 = public_remote()
    repo = source_repo({"a.py": "x\n"},
                       {"remote": remote1, "include": ["a.py"]})
    run(["init"], cwd=repo)
    run([], cwd=repo)

    alt_config = config_toml({"remote": remote2, "include": ["a.py"],
                              "dir": ".publish-alt", "branch": "main"})
    (repo / ".publish-alt.toml").write_text(alt_config)
    (repo / ".gitignore").write_text(".publish/\n.publish-alt/\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "alt config", cwd=repo)
    run(["init", "--config", ".publish-alt.toml"], cwd=repo)

    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage", "--json", "--config", ".publish-alt.toml"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)
    assert data["publish_dir"] == ".publish-alt"

    pub_default = repo / ".publish"
    default_head = git_run("rev-parse", "HEAD", cwd=pub_default).strip()
    r2 = run(["--stage", "--json"], cwd=repo)
    data2 = json.loads(r2.stdout)
    assert data2["publish_dir"] == ".publish"


# ===========================================================================
# Test 13: Wrong branch / detached HEAD
# ===========================================================================

def test_wrong_branch(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"
    git_run("checkout", "-b", "other", cwd=pub)

    r = run(["--stage"], cwd=repo)
    assert r.code == 2
    assert "on branch 'other'" in r.stderr


def test_detached_head(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"
    head = git_run("rev-parse", "HEAD", cwd=pub).strip()
    git_run("checkout", head, cwd=pub)

    r = run(["--stage"], cwd=repo)
    assert r.code == 2
    assert "detached HEAD" in r.stderr


# ===========================================================================
# Test 14: Unborn HEAD (first stage)
# ===========================================================================

def test_unborn_head_first_stage(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"},
                       {"remote": remote, "include": ["a.py"]})
    pub = repo / ".publish"
    pub.mkdir(exist_ok=True)
    git_run("init", "-q", "-b", "main", cwd=pub)
    git_run("config", "user.email", "test@pubrepo.test", cwd=pub)
    git_run("config", "user.name", "Pubrepo Tests", cwd=pub)
    git_run("remote", "add", "origin", remote, cwd=pub)

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)
    assert data["baseline"]["sha"] is None
    assert data["changed"] is True
    assert data["changes"]["added"] > 0
    assert all(f["status"] == "A" for f in data["changes"]["files"])


# ===========================================================================
# Test 15: Keep/transform overlap validation
# ===========================================================================

def test_keep_transform_exact_overlap(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "LICENSE": "MIT\n"},
        {"remote": remote, "include": ["a.py", "LICENSE"],
         "keep": ["LICENSE"],
         "transforms": {"LICENSE": [{"find": "MIT", "replace": "BSD"}]}},
    )
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "compound" in r.stderr


def test_keep_transform_nested_overlap(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "docs/LICENSE": "MIT\n"},
        {"remote": remote, "include": ["a.py", "docs/"],
         "keep": ["docs"],
         "transforms": {"docs/LICENSE": [{"find": "MIT", "replace": "BSD"}]}},
    )
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "compound" in r.stderr


def test_keep_transform_normalized_overlap(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "docs/file": "x\n"},
        {"remote": remote, "include": ["a.py", "docs/"],
         "keep": ["docs"],
         "transforms": {"./docs/file": [{"find": "x", "replace": "y"}]}},
    )
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "compound" in r.stderr


def test_keep_transform_non_overlap_passes(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "docs/file": "x\n"},
        {"remote": remote, "include": ["a.py", "docs/"],
         "keep": ["doc"],
         "transforms": {"docs/file": [{"find": "x", "replace": "y"}]}},
    )
    r = run(["validate"], cwd=repo)
    assert r.code == 0


def test_keep_transform_dotdot_bypass_blocked(source_repo, public_remote, run):
    """Critic BLOCKER 1: x/../docs/LICENSE must be caught."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "docs/LICENSE": "MIT\n", "x/dummy": "d\n"},
        {"remote": remote, "include": ["a.py", "docs/", "x/"],
         "keep": ["docs"],
         "transforms": {"x/../docs/LICENSE": [{"find": "MIT", "replace": "BSD"}]}},
    )
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "compound" in r.stderr


def test_absolute_transform_path_rejected(source_repo, public_remote, run):
    """Audit F-02: absolute transform paths must be rejected at validation."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["a.py"],
         "transforms": {"/etc/passwd": [{"find": "x", "replace": "y"}]}},
    )
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "absolute path" in r.stderr


def test_dotdot_escape_transform_path_rejected(source_repo, public_remote, run):
    """Audit F-02: ../escaped transform paths must be rejected at validation."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["a.py"],
         "transforms": {"../../secret": [{"find": "x", "replace": "y"}]}},
    )
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "escapes" in r.stderr


# ===========================================================================
# Test 16: End-to-end stage→publish byte comparison
# ===========================================================================

def test_stage_then_publish_byte_identical(source_repo, public_remote, run, published_tree):
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "original\n", "README.md": "# readme\n"},
                          config_extra={"include": ["a.py", "README.md"]})
    (repo / "a.py").write_text("modified\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage"], cwd=repo)
    assert r.code == 0
    stage_hashes = _tree_hash(repo / ".publish")

    r = run([], cwd=repo)
    assert r.code == 0

    tree = published_tree(remote)
    for path, content_bytes in tree.items():
        assert hashlib.sha256(content_bytes).hexdigest() == stage_hashes[path], \
            f"Mismatch for {path}"


# ===========================================================================
# Test 17: Git fault injection (monkeypatch)
# ===========================================================================

def test_git_fault_injection(source_repo, public_remote, run, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"
    index_before = (pub / ".git" / "index").read_bytes()

    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    real_git = pubrepo.git

    def failing_git(*args, **kwargs):
        if kwargs.get("env") and "GIT_INDEX_FILE" in kwargs["env"]:
            if args and args[0] == "diff":
                result = type(real_git("--version"))
                result.returncode = 128
                result.stdout = ""
                result.stderr = "fatal: injected"
                raise pubrepo.GitError(list(args), "fatal: injected")
        return real_git(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", failing_git)
    r = run(["--stage"], cwd=repo)
    assert r.code == 2
    assert r.stdout == ""

    assert (pub / ".git" / "index").read_bytes() == index_before

    lock_path = pub / ".git" / "pubrepo.lock"
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# ===========================================================================
# Test 18: Filenames with spaces, Unicode, tabs
# ===========================================================================

def test_special_filenames(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["."]},
    )
    run(["init"], cwd=repo)
    run([], cwd=repo)

    (repo / "hello world.py").write_text("space\n")
    (repo / "café.txt").write_text("unicode\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "special files", cwd=repo)

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)
    paths = [f["path"] for f in data["changes"]["files"]]
    assert "hello world.py" in paths
    assert "café.txt" in paths
    assert data["changes"]["added"] >= 2


# ===========================================================================
# Test 19: Unreachable remote succeeds for stage
# ===========================================================================

def test_unreachable_remote_succeeds(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)

    pub = repo / ".publish"
    git_run("remote", "set-url", "origin", "ssh://nonexistent.invalid/repo.git", cwd=pub)

    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    r = run(["--stage"], cwd=repo)
    assert r.code == 0

    r2 = run([], cwd=repo)
    assert r2.code == 2


# ===========================================================================
# Test 20: Lock isolation (same vs different target)
# ===========================================================================

def test_lock_isolation(source_repo, public_remote, run):
    remote1 = public_remote()
    remote2 = public_remote()
    repo = source_repo({"a.py": "x\n"},
                       {"remote": remote1, "include": ["a.py"]})
    run(["init"], cwd=repo)
    run([], cwd=repo)

    alt_config = config_toml({"remote": remote2, "include": ["a.py"],
                              "dir": ".publish-alt", "branch": "main"})
    (repo / ".publish-alt.toml").write_text(alt_config)
    git_run("add", ".publish-alt.toml", cwd=repo)
    git_run("commit", "-m", "alt config", cwd=repo)
    run(["init", "--config", ".publish-alt.toml"], cwd=repo)
    run(["publish", "--config", ".publish-alt.toml"], cwd=repo)

    lock_path = repo / ".publish" / ".git" / "pubrepo.lock"
    with open(lock_path, "w") as holder:
        holder.write("999999")
        holder.flush()
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        r = run(["--stage", "--config", ".publish-alt.toml"], cwd=repo)
        assert r.code == 0

        lock_alt = repo / ".publish-alt" / ".git" / "pubrepo.lock"
        with open(lock_alt, "w") as holder_alt:
            holder_alt.write("888888")
            holder_alt.flush()
            fcntl.flock(holder_alt.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            r2 = run(["--stage", "--config", ".publish-alt.toml"], cwd=repo)
            assert r2.code == 3


# ===========================================================================
# Test 21: Interrupt during temp-index (Critic MINOR 5 correction)
# ===========================================================================

def test_interrupt_during_temp_index(source_repo, public_remote, run, monkeypatch):
    """Interrupting during the ephemeral-index block must clean up temp files
    and restore worktree to HEAD (not preserve byte-identical index)."""
    remote, repo = _setup(source_repo, public_remote, run)
    pub = repo / ".publish"
    head_before = git_run("rev-parse", "HEAD", cwd=pub).strip()

    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    real_git = pubrepo.git
    interrupted = False

    def interrupting_git(*args, **kwargs):
        nonlocal interrupted
        env = kwargs.get("env")
        if env and "GIT_INDEX_FILE" in env and args and args[0] == "add" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return real_git(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", interrupting_git)
    r = run(["--stage"], cwd=repo)
    assert r.code == 130

    import glob
    stage_files = glob.glob(str(pub / ".git" / "stage-idx-*"))
    assert stage_files == [], f"Temp index files left behind: {stage_files}"

    lock_path = pub / ".git" / "pubrepo.lock"
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# ===========================================================================
# Test 22: source.dirty with untracked included file
# ===========================================================================

def test_source_dirty_with_untracked_included_file(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          config_extra={"include": ["."]})
    (repo / "untracked_new.py").write_text("new\n")

    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)

    has_untracked = any(f["path"] == "untracked_new.py" for f in data["changes"]["files"])
    assert has_untracked
    assert data["source"]["dirty"] is False


# ===========================================================================
# Critic MAJOR 3: Cleanup failure raises PublishError
# ===========================================================================

def test_cleanup_failure_raises_error(source_repo, public_remote, run, monkeypatch):
    """If temp index cleanup fails, stage must exit 2 (not succeed)."""
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    real_unlink = os.unlink

    def failing_unlink(path):
        if "stage-idx-" in str(path) and not str(path).endswith(".lock"):
            raise PermissionError("injected")
        real_unlink(path)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    r = run(["--stage"], cwd=repo)
    assert r.code == 2
    assert "temporary index" in r.stderr


def test_cleanup_failure_no_json_success(source_repo, public_remote, run, monkeypatch):
    """JSON mode must not emit success payload when cleanup fails."""
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("changed\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    real_unlink = os.unlink

    def failing_unlink(path):
        if "stage-idx-" in str(path) and not str(path).endswith(".lock"):
            raise PermissionError("injected")
        real_unlink(path)

    monkeypatch.setattr(os, "unlink", failing_unlink)
    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 2
    assert r.stdout == "" or '"staged": true' not in r.stdout


# ===========================================================================
# Critic MAJOR 4: Transform write failure → PublishError (exit 2)
# ===========================================================================

def test_transform_write_failure_stage(source_repo, public_remote, run, monkeypatch):
    """Transform write errors must exit 2 (PublishError) during stage."""
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "original\n"},
                          config_extra={
                              "include": ["a.py"],
                              "transforms": {"a.py": [{"find": "original", "replace": "replaced"}]},
                          })
    (repo / "a.py").write_text("original content\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    real_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        pub = repo / ".publish"
        if pub in self.parents or self.parent == pub:
            if self.name == "a.py":
                raise OSError("injected write failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    r = run(["--stage"], cwd=repo)
    assert r.code == 2
    assert "cannot write transformed file" in r.stderr

    lock_path = repo / ".publish" / ".git" / "pubrepo.lock"
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_transform_write_failure_full_publish(source_repo, public_remote, run, monkeypatch):
    """Transform write errors must exit 2 (PublishError) during full publish too."""
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "original\n"},
                          config_extra={
                              "include": ["a.py"],
                              "transforms": {"a.py": [{"find": "original", "replace": "replaced"}]},
                          })
    (repo / "a.py").write_text("original content\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "mod", cwd=repo)

    real_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        pub = repo / ".publish"
        if pub in self.parents or self.parent == pub:
            if self.name == "a.py":
                raise OSError("injected write failure")
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", failing_write_text)

    r = run([], cwd=repo)
    assert r.code == 2
    assert "cannot write transformed file" in r.stderr

    lock_path = repo / ".publish" / ".git" / "pubrepo.lock"
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


# ===========================================================================
# Scrub failure log not appended during stage
# ===========================================================================

def test_scrub_failure_no_log_during_stage(source_repo, public_remote, run):
    """Stage must NOT append to publish log on scrub failure."""
    remote, repo = _setup(source_repo, public_remote, run,
                          config_extra={"scrub": {"forbidden": ["sekrit"]}})
    log_before = _log_bytes(repo)

    (repo / "a.py").write_text("the sekrit\n")
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-m", "add sekrit", cwd=repo)

    r = run(["--stage"], cwd=repo)
    assert r.code == 4
    assert _log_bytes(repo) == log_before

    r2 = run([], cwd=repo)
    assert r2.code == 4
    assert _log_bytes(repo) != log_before


# ===========================================================================
# Transforms applied on disk during stage
# ===========================================================================

def test_transforms_applied_on_disk(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "REPLACE_ME\n"},
                          config_extra={
                              "include": ["a.py"],
                              "transforms": {"a.py": [{"find": "REPLACE_ME", "replace": "DONE"}]},
                          })
    r = run(["--stage"], cwd=repo)
    assert r.code == 0
    assert (repo / ".publish" / "a.py").read_text() == "DONE\n"


def test_transforms_in_json(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "REPLACE_ME\n"},
                          config_extra={
                              "include": ["a.py"],
                              "transforms": {"a.py": [{"find": "REPLACE_ME", "replace": "DONE"}]},
                          })
    r = run(["--stage", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)
    assert "a.py" in data["transforms_applied"]
