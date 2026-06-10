"""T8: fetch-first, branch verification, divergence, --force-overwrite (M4)."""
from pathlib import Path
from urllib.parse import urlparse

import pubrepo
from conftest import git_run


def _external_commit(remote_url: str, workdir: Path, marker: str = "external") -> None:
    """Simulate a PR merge / GitHub-UI edit landing directly on the remote."""
    work = workdir / f"ext-{marker}"
    git_run("clone", "-q", remote_url, str(work))
    git_run("config", "user.email", "ext@test", cwd=work)
    git_run("config", "user.name", "External", cwd=work)
    (work / f"{marker}.txt").write_text(f"{marker} change\n")
    git_run("add", "-A", cwd=work)
    git_run("commit", "-qm", f"{marker}: direct edit", cwd=work)
    git_run("push", "-q", "origin", "main", cwd=work)


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(root).parts
    }


def _setup_published(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "v1\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    return remote, repo


def test_diverged_refuses_and_leaves_publish_untouched(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup_published(source_repo, public_remote, run)
    _external_commit(remote, tmp_path)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    publish_dir = repo / ".publish"
    before_tree = _tree_bytes(publish_dir)
    before_head = git_run("rev-parse", "HEAD", cwd=publish_dir).strip()

    r = run([], cwd=repo)
    assert r.code == 5
    assert "not produced by this tool" in r.stderr
    assert "one-way mirror" in r.stderr
    assert "external: direct edit" in r.stderr  # the commit is listed
    assert "--force-overwrite" in r.stderr

    assert _tree_bytes(publish_dir) == before_tree
    assert git_run("rev-parse", "HEAD", cwd=publish_dir).strip() == before_head


def test_force_overwrite_reasserts_source_projection(source_repo, public_remote, run, published_tree, tmp_path):
    remote, repo = _setup_published(source_repo, public_remote, run)
    _external_commit(remote, tmp_path)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    r = run(["--force-overwrite"], cwd=repo)
    assert r.code == 0
    assert published_tree(remote) == {"a.py": b"v2\n"}  # external.txt gone


def test_lease_race_refuses_and_rolls_back(source_repo, public_remote, run, published_tree,
                                           tmp_path, monkeypatch):
    """A commit landing between fetch and push must be refused by the lease,
    not silently destroyed."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _external_commit(remote, tmp_path, marker="first")

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git
    state = {"raced": False}

    def racing_git(*args, cwd=None, check=True):
        if args and args[0] == "push" and any("force-with-lease" in a for a in args) \
                and not state["raced"]:
            state["raced"] = True
            _external_commit(remote, tmp_path, marker="second")  # between fetch and push
        return real_git(*args, cwd=cwd, check=check)

    monkeypatch.setattr(pubrepo, "git", racing_git)

    publish_dir = repo / ".publish"
    pre_head = git_run("rev-parse", "HEAD", cwd=publish_dir).strip()

    r = run(["--force-overwrite"], cwd=repo)
    assert r.code == 2
    assert "push failed" in r.stderr
    assert "rolled back" in r.stderr.lower()

    # The racing commit survived on the remote; the lease refused to destroy it.
    assert "second.txt" in published_tree(remote)
    # And the local publish commit was rolled back — HEAD did not advance.
    assert git_run("rev-parse", "HEAD", cwd=publish_dir).strip() == pre_head


def test_force_overwrite_without_divergence_pushes_normally(source_repo, public_remote, run,
                                                            published_tree, monkeypatch):
    """--force-overwrite on an up-to-date remote must NOT force-push (some
    server hooks reject any force-push); a normal push suffices."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git
    pushes = []

    def spying_git(*args, cwd=None, check=True):
        if args and args[0] == "push":
            pushes.append(args)
        return real_git(*args, cwd=cwd, check=check)

    monkeypatch.setattr(pubrepo, "git", spying_git)

    r = run(["--force-overwrite"], cwd=repo)
    assert r.code == 0
    assert published_tree(remote) == {"a.py": b"v2\n"}
    branch_pushes = [p for p in pushes if "origin" in p]
    assert branch_pushes
    assert not any("force-with-lease" in a for p in branch_pushes for a in p)


def test_wrong_branch_refused_with_fix_instruction(source_repo, public_remote, run):
    remote, repo = _setup_published(source_repo, public_remote, run)
    publish_dir = repo / ".publish"
    git_run("checkout", "-q", "-b", "scratch", cwd=publish_dir)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    r = run([], cwd=repo)
    assert r.code == 2
    assert "on branch 'scratch'" in r.stderr
    assert "git -C .publish checkout main" in r.stderr


def test_detached_head_refused(source_repo, public_remote, run):
    remote, repo = _setup_published(source_repo, public_remote, run)
    publish_dir = repo / ".publish"
    git_run("checkout", "-q", "--detach", cwd=publish_dir)

    r = run([], cwd=repo)
    assert r.code == 2
    assert "detached HEAD" in r.stderr


def test_unreachable_remote_fails_before_nuke(source_repo, public_remote, run):
    remote, repo = _setup_published(source_repo, public_remote, run)
    publish_dir = repo / ".publish"
    git_run("remote", "set-url", "origin", "/nonexistent/nowhere.git", cwd=publish_dir)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    before_tree = _tree_bytes(publish_dir)
    r = run([], cwd=repo)
    assert r.code == 2
    assert "cannot reach remote" in r.stderr
    assert _tree_bytes(publish_dir) == before_tree  # refused BEFORE the nuke


def test_first_publish_to_empty_remote_succeeds(source_repo, public_remote, run, published_tree):
    """Missing remote branch (couldn't find remote ref) is not an error."""
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert published_tree(remote) == {"a.py": b"x\n"}


def test_diverged_message_names_snapshot_and_integrate(source_repo, public_remote, run, tmp_path):
    """F1: the refusal points at the integration path, not just the flag."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _external_commit(remote, tmp_path)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    snap = git_run("rev-parse", "--short", "HEAD", cwd=repo / ".publish").strip()

    r = run([], cwd=repo)
    assert r.code == 5
    assert f"last snapshot {snap}" in r.stderr
    assert "Source:" in r.stderr
    assert "pubrepo integrate" in r.stderr        # _PROG under pytest
    assert "docs/integrating-changes.md" in r.stderr
