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
    r = run(["publish", "--yes"], cwd=repo)
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

    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 5
    # §10 items 3 and 6 (the message changed with the fast-forward model): exit 5
    # kept; the refusal says to pull, with no mirror or force wording.
    assert "run `pubrepo pull`" in r.stderr
    assert "one-way mirror" not in r.stderr
    assert "external: direct edit" in r.stderr  # the commit is listed
    assert "--force-overwrite" not in r.stderr

    assert _tree_bytes(publish_dir) == before_tree
    assert git_run("rev-parse", "HEAD", cwd=publish_dir).strip() == before_head


def _assert_force_overwrite_retired(run, repo, remote, published_tree, git_recorder):
    before = published_tree(remote)
    calls = len(git_recorder.calls())
    r = run(["--force-overwrite"], cwd=repo)
    assert r.code == 1
    assert "pull" in r.stderr and "repair" in r.stderr
    assert len(git_recorder.calls()) == calls, "exit 1 with zero git calls"
    assert published_tree(remote) == before


# AC75: §10 item 3 (--force-overwrite is removed, exit 1)
def test_force_overwrite_reasserts_source_projection(source_repo, public_remote, run,
                                                     published_tree, tmp_path, git_recorder):
    """Replaced by the AC63 refusal: --force-overwrite
    exits 1 with zero git calls and points to pull and repair; the foreign
    commit stays."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _external_commit(remote, tmp_path)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    _assert_force_overwrite_retired(run, repo, remote, published_tree, git_recorder)
    assert "external.txt" in published_tree(remote)


# test_lease_race_refuses_and_rolls_back: deleted (§10 items 3
# and 6). No lease in routine publish; the race is AC5
# (test_ff_publish::test_concurrent_push_exit5_local_ref_unchanged).


def test_force_overwrite_without_divergence_pushes_normally(source_repo, public_remote, run,
                                                            published_tree, git_recorder):
    """§10 item 3: --force-overwrite exits 1 with zero git calls
    even when nothing diverged."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    _assert_force_overwrite_retired(run, repo, remote, published_tree, git_recorder)


def test_wrong_branch_refused_with_fix_instruction(source_repo, public_remote, run):
    remote, repo = _setup_published(source_repo, public_remote, run)
    publish_dir = repo / ".publish"
    git_run("checkout", "-q", "-b", "scratch", cwd=publish_dir)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2
    assert "on branch 'scratch'" in r.stderr
    assert "git -C .publish checkout main" in r.stderr


def test_detached_head_refused(source_repo, public_remote, run):
    remote, repo = _setup_published(source_repo, public_remote, run)
    publish_dir = repo / ".publish"
    git_run("checkout", "-q", "--detach", cwd=publish_dir)

    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2
    assert "detached HEAD" in r.stderr


def test_unreachable_remote_fails_before_nuke(source_repo, public_remote, run):
    remote, repo = _setup_published(source_repo, public_remote, run)
    publish_dir = repo / ".publish"
    # §17.4 sandbox: the unreachable remote is a missing path inside the base.
    # AC75 re-target (§10 item 20, §18.11 R5): the configured remote's
    # bare is moved away, so origin still equals [publish] remote for R5
    # (was: set-url origin to a missing path, which R5 now refuses first)
    bare = Path(remote.removeprefix("file://"))
    bare.rename(bare.with_name("moved-away.git"))

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    before_tree = _tree_bytes(publish_dir)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2
    assert "cannot reach remote" in r.stderr
    assert _tree_bytes(publish_dir) == before_tree  # refused BEFORE the nuke


def test_first_publish_to_empty_remote_succeeds(source_repo, public_remote, run, published_tree):
    """Missing remote branch (couldn't find remote ref) is not an error."""
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0
    assert published_tree(remote) == {"a.py": b"x\n"}


def test_diverged_message_names_snapshot_and_integrate(source_repo, public_remote, run, tmp_path):
    """F1: the refusal points at the integration path, not just the flag."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _external_commit(remote, tmp_path)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    foreign = git_run("ls-remote", remote, "refs/heads/main").split()[0]

    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 5
    # §10 items 3 and 6 (the message changed with the fast-forward model): the
    # refusal names the foreign commit (AC1) and points at `pubrepo pull`.
    assert foreign[:7] in r.stderr
    assert "run `pubrepo pull`" in r.stderr       # _PROG under pytest
    assert "one-way mirror" not in r.stderr
    assert "--force-overwrite" not in r.stderr
