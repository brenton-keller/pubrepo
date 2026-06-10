"""T19 (publish log) + T20 (signal handling) + the keep-dirty status fix."""
import json
from pathlib import Path

import pubrepo
from conftest import git_run


def _setup(source_repo, public_remote, run, **config_extra):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra)
    repo = source_repo({"a.py": "v1\n"}, config)
    run(["init"], cwd=repo)
    return remote, repo


def _log_entries(repo):
    log = repo / ".publish" / ".git" / "pubrepo-log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


# --- T19: publish log ------------------------------------------------------------

def test_success_entry(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          **{"scrub": {"forbidden": ["sekrit"]}})
    r = run([], cwd=repo)
    assert r.code == 0

    entries = _log_entries(repo)
    assert len(entries) == 1
    e = entries[0]
    assert e["pushed"] is True
    assert e["scrub_result"] == "passed"
    assert e["files"] == 1 and e["added"] == 1
    assert e["source_branch"] == "main"
    assert e["forced"] is False
    assert e["public_commit"] == git_run("rev-parse", "HEAD", cwd=repo / ".publish").strip()
    assert "T" in e["ts"] and ("+" in e["ts"] or "-" in e["ts"][10:])  # tz-aware iso8601


def test_blocked_entry(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          **{"scrub": {"forbidden": ["sekrit"]}})
    (repo / "a.py").write_text("the sekrit\n")
    r = run([], cwd=repo)
    assert r.code == 4

    entries = _log_entries(repo)
    assert len(entries) == 1
    e = entries[0]
    assert e["pushed"] is False
    assert e["scrub_result"] == "failed"
    assert e["added"] is None  # blocked before staging
    assert e["public_commit"] is None


def test_corrupt_line_tolerated_and_log_authoritative(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    assert run([], cwd=repo).code == 0

    log = repo / ".publish" / ".git" / "pubrepo-log.jsonl"
    with open(log, "a") as f:
        f.write("{this is not json\n")

    r = run(["status"], cwd=repo)
    assert r.code == 0  # never crashes status
    # The corrupt tail is skipped; the valid entry still provides last-publish.
    src_hash = git_run("rev-parse", "--short", "HEAD", cwd=repo).strip()
    assert f"Source:       {src_hash}" in r.stdout


def test_status_reads_log_not_commit_message(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    assert run([], cwd=repo).code == 0
    # Rewrite the publish commit message to garbage; the log still answers.
    git_run("commit", "--amend", "-qm", "garbage message no source line",
            cwd=repo / ".publish")
    r = run(["status"], cwd=repo)
    src_hash = git_run("rev-parse", "--short", "HEAD", cwd=repo).strip()
    assert f"Source:       {src_hash}" in r.stdout


# --- T20: signal handling ----------------------------------------------------------

def test_interrupt_mid_copy_restores_tree_exit_130(source_repo, public_remote, run, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    assert run([], cwd=repo).code == 0

    publish_dir = repo / ".publish"
    before = _tree_bytes(publish_dir)
    before_head = git_run("rev-parse", "HEAD", cwd=publish_dir).strip()

    (repo / "a.py").write_text("v2\n")
    real_copy = pubrepo.copy_includes

    def interrupting_copy(*args, **kwargs):
        real_copy(*args, **kwargs)  # half-done state: copy happened...
        raise KeyboardInterrupt    # ...then ^C lands

    monkeypatch.setattr(pubrepo, "copy_includes", interrupting_copy)
    r = run([], cwd=repo)
    assert r.code == 130
    assert "restored to last published state" in r.stderr

    assert _tree_bytes(publish_dir) == before
    assert git_run("rev-parse", "HEAD", cwd=publish_dir).strip() == before_head


def test_interrupted_exit_code():
    assert pubrepo.Interrupted("x").exit_code == 130


# --- keep-dirty status fix (T17 review follow-up) -----------------------------------

def test_keep_edit_visible_in_status(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run, keep=["LICENSE"])
    assert run([], cwd=repo).code == 0
    assert run(["status", "--check"], cwd=repo).code == 0

    (repo / ".publish" / "LICENSE").write_text("MIT\n")
    r = run(["status"], cwd=repo)
    assert "Uncommitted changes in .publish/" in r.stdout
    assert run(["status", "--check"], cwd=repo).code == 6

    data = json.loads(run(["status", "--json"], cwd=repo).stdout)
    assert data["status"] == "changes_pending"
    assert data["changes"]["publish_dir_dirty"] is True

    # Publishing resolves it.
    assert run([], cwd=repo).code == 0
    assert run(["status", "--check"], cwd=repo).code == 0
