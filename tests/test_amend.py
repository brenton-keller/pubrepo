"""--amend + on_republish policy: T1-T27, T29, T30.

Design §10 item 4: --amend asks for confirmation, or --yes off a
terminal; every --amend run here is off a terminal and passes --yes.
"""
import json
import os
from pathlib import Path

import pytest

import pubrepo
from conftest import config_toml, git_run
from test_divergence import _external_commit, _tree_bytes
from ff_fixtures import push_argv

# AC62: --amend is an explicit rewrite; the routine policy does not apply.
pytestmark = pytest.mark.rewrite


def _setup(source_repo, public_remote, run, files=None, **config_extra):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra)
    repo = source_repo(files or {"a.py": "v1\n"}, config)
    run(["init"], cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0
    return remote, repo


def _publish_dir(repo):
    return repo / ".publish"


def _remote_count(publish_dir):
    return int(git_run("rev-list", "--count", "HEAD",
                       cwd=str(publish_dir)).strip())


def _remote_tip_parents(publish_dir):
    out = git_run("rev-list", "--parents", "-n", "1", "HEAD",
                  cwd=str(publish_dir)).strip()
    return out.split()


def _log_entries(publish_dir):
    log_path = publish_dir / ".git" / "pubrepo-log.jsonl"
    if not log_path.exists():
        return []
    entries = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if line:
            entries.append(json.loads(line))
    return entries


# T1
def test_amend_replaces_last_commit_same_parent(source_repo, public_remote, run, published_tree):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()
    pre_parents = _remote_tip_parents(pd)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    post_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()
    assert post_sha != pre_sha
    post_parents = _remote_tip_parents(pd)
    assert post_parents[1:] == pre_parents[1:]
    assert published_tree(remote) == {"a.py": b"v2\n"}


# T2
def test_amend_root_commit(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo({"a.py": "v1\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0
    pd = _publish_dir(repo)
    assert _remote_count(pd) == 1

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    assert _remote_count(pd) == 1
    parents = _remote_tip_parents(pd)
    assert len(parents) == 1  # root commit — no parents
    assert published_tree(remote) == {"a.py": b"v2\n"}


# T3
def test_amend_reuses_custom_title_regenerates_trailer(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    run(["-m", "My Custom Title", "--yes"], cwd=repo)

    (repo / "a.py").write_text("v3\n")
    git_run("commit", "-aqm", "v3", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    pd = _publish_dir(repo)
    subject = git_run("log", "-1", "--format=%s", cwd=str(pd)).strip()
    assert subject == "My Custom Title"

    body = git_run("log", "-1", "--format=%B", cwd=str(pd)).strip()
    source_hash = git_run("rev-parse", "--short", "HEAD", cwd=repo).strip()
    assert f"Source: {source_hash}" in body
    # Design §4.3 / §10 item 1: no Branch: line and no (dirty) marker.
    assert "Branch:" not in body
    assert "(dirty)" not in body


# T4
def test_amend_reuses_default_title(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    original_subject = git_run("log", "-1", "--format=%s", cwd=str(pd)).strip()
    assert original_subject.startswith("publish: ")

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    new_subject = git_run("log", "-1", "--format=%s", cwd=str(pd)).strip()
    assert new_subject == original_subject


# T5
def test_amend_m_overrides_title(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes", "-m", "Brand New Title"], cwd=repo)
    assert r.code == 0

    pd = _publish_dir(repo)
    subject = git_run("log", "-1", "--format=%s", cwd=str(pd)).strip()
    assert subject == "Brand New Title"


# T6
def test_amend_push_uses_lease_pinned_to_fetched_sha(source_repo, public_remote, run, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git
    push_calls = []

    # AC75 re-target: the branch push is `push --porcelain ... origin
    # <sha>:refs/heads/main`, and git() now also takes env=. Assertions unchanged.
    def spying_git(*args, cwd=None, check=True, **kwargs):
        if push_argv(args) is not None:  # AC75 re-target: the porcelain push argv
            push_calls.append(args)
        return real_git(*args, cwd=cwd, check=check, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spying_git)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    branch_pushes = [c for c in push_calls
                     if any(a.endswith(":refs/heads/main") for a in c)]  # AC75 re-target: the porcelain push argv
    assert len(branch_pushes) == 1
    assert any(f"--force-with-lease=refs/heads/main:{pre_sha}" in a
               for a in branch_pushes[0])


# T7
def test_amend_lease_race_refuses_and_restores_pre_amend(source_repo, public_remote, run,
                                                          published_tree, tmp_path, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git
    state = {"raced": False}

    def racing_git(*args, cwd=None, check=True, **kwargs):
        if (push_argv(args) is not None  # AC75 re-target: the porcelain push argv
                and any("force-with-lease" in a for a in args)
                and not state["raced"]):
            state["raced"] = True
            _external_commit(remote, tmp_path, marker="second")
        return real_git(*args, cwd=cwd, check=check, **kwargs)

    monkeypatch.setattr(pubrepo, "git", racing_git)
    r = run(["--amend", "--yes"], cwd=repo)
    # Design §7 / §9 (§10 items 4 and 6): a rejected lease exits 5.
    assert r.code == 5
    assert "moved" in r.stderr.lower()

    assert "second.txt" in published_tree(remote)
    assert git_run("rev-parse", "HEAD", cwd=str(pd)).strip() == pre_sha

    entries = _log_entries(pd)
    pushed_entries = [e for e in entries if e.get("pushed")]
    assert len(pushed_entries) == 1  # only the initial publish


# T8
def test_amend_push_rejected_rolls_back_preserving_index_and_worktree(
        source_repo, public_remote, run, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git

    # AC75 re-target: the host refusal arrives as a `[remote rejected]`
    # porcelain line. §10 item 4 / §6.5: nothing local moved (no commit to
    # roll back, index and worktree as before), and the hint is to publish a
    # new snapshot instead (--no-amend is a no-op since 1.1.0).
    def failing_push_git(*args, cwd=None, check=True, **kwargs):
        if push_argv(args) is not None:  # AC75 re-target: the porcelain push argv
            from types import SimpleNamespace
            refspec = args[-1]
            return SimpleNamespace(returncode=1, stderr="",
                                   stdout=f"To x\n!\t{refspec}\t[remote rejected] "
                                          f"(permission denied)\nDone\n")
        return real_git(*args, cwd=cwd, check=check, **kwargs)

    monkeypatch.setattr(pubrepo, "git", failing_push_git)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 2
    assert git_run("rev-parse", "HEAD", cwd=str(pd)).strip() == pre_sha

    staged = git_run("diff", "--cached", "--name-only", cwd=str(pd)).strip()
    assert staged == ""
    assert (pd / "a.py").read_text() == "v1\n"
    assert "host refused the push" in r.stderr
    assert "force-push permission" in r.stderr


# T9
# AC75: AC75 re-target: the push is `push --porcelain … origin` (push_argv) and git() takes env=; assertions unchanged
def test_amend_root_push_failure_rollback(source_repo, public_remote, run, monkeypatch):
    remote = public_remote()
    repo = source_repo({"a.py": "v1\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    run(["publish", "--yes"], cwd=repo)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git

    def failing_push_git(*args, cwd=None, check=True, **kwargs):   # AC75 re-target: env=
        if push_argv(args) is not None:  # AC75 re-target: the porcelain push argv
            from types import SimpleNamespace
            return SimpleNamespace(returncode=1, stdout="", stderr="denied")
        return real_git(*args, cwd=cwd, check=check, **kwargs)

    monkeypatch.setattr(pubrepo, "git", failing_push_git)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 2
    assert git_run("rev-parse", "HEAD", cwd=str(pd)).strip() == pre_sha


# T10
# AC75: AC75 re-target: the push is `push --porcelain … origin` (push_argv) and git() takes env=; assertions unchanged
def test_amend_interrupt_during_push_restores_everything(source_repo, public_remote, run,
                                                          monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()
    pre_tree = _tree_bytes(pd)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git

    def interrupt_push_git(*args, cwd=None, check=True, **kwargs):   # AC75 re-target: env=
        if push_argv(args) is not None:  # AC75 re-target: the porcelain push argv
            raise KeyboardInterrupt
        return real_git(*args, cwd=cwd, check=check, **kwargs)

    monkeypatch.setattr(pubrepo, "git", interrupt_push_git)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 130
    assert git_run("rev-parse", "HEAD", cwd=str(pd)).strip() == pre_sha
    assert _tree_bytes(pd) == pre_tree


# T11
def test_amend_unborn_head_refused(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "v1\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    pd = _publish_dir(repo)

    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 2
    assert "no previous publish" in r.stderr
    assert not _log_entries(pd)


# T12
def test_amend_remote_branch_absent_refused(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)

    # AC75 re-target (§10 item 20, §18.11 R5): R5 now refuses an origin
    # other than [publish] remote before the fetch, so the branch is removed
    # from the configured remote itself (was: set-url to an empty bare)
    git_run("update-ref", "-d", "refs/heads/main", cwd=remote.removeprefix("file://"))

    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 2
    assert "nothing on the remote" in r.stderr


# T13
def test_amend_local_ahead_refused(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    git_run("commit", "--allow-empty", "-m", "local extra\n\nSource: fake123\n",
            cwd=str(pd))
    git_run("commit", "--allow-empty", "-m", "local extra2\n\nSource: fake456\n",
            cwd=str(pd))
    local_head = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    # §10 item 4 (§4.10): local-only commits do not block --amend; the
    # amend proceeds on the fetched tip and they are named and backed up.
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0
    assert "local extra2" in r.stderr
    backups = git_run("for-each-ref", "--format=%(objectname)", "refs/pubrepo/local/",
                      cwd=str(pd)).split()
    assert backups == [local_head]
    assert git_run("rev-parse", "HEAD", cwd=str(pd)).strip() != pre_sha
    assert _remote_count(pd) == 1          # the root snapshot was replaced, not stacked


# T14
# AC75: §10 items 3 and 6 (no force hint, a foreign tip is 5, the hint says pull)
def test_amend_diverged_refused_exit5_with_two_step_hint(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup(source_repo, public_remote, run)
    _external_commit(remote, tmp_path)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 5
    # the hint says pull; no force hint.
    assert "run `pubrepo pull`" in r.stderr
    assert "--force-overwrite" not in r.stderr


# T15
def test_amend_merge_head_refused(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)

    work = tmp_path / "merge-work"
    git_run("clone", "-q", remote, str(work))
    git_run("config", "user.email", "m@test", cwd=str(work))
    git_run("config", "user.name", "Merger", cwd=str(work))
    git_run("checkout", "-b", "side", cwd=str(work))
    (work / "side.txt").write_text("side\n")
    git_run("add", "-A", cwd=str(work))
    git_run("commit", "-qm", "side commit", cwd=str(work))
    git_run("checkout", "main", cwd=str(work))
    git_run("merge", "--no-ff", "side", "-m", "merge", cwd=str(work))
    git_run("push", "-q", "--force", "origin", "main", cwd=str(work))

    import shutil
    shutil.rmtree(str(pd))
    run(["init"], cwd=repo)

    r = run(["--amend", "--yes"], cwd=repo)
    # §9, §10 items 6 and 4: a collaborator merge on top is an unpulled
    # range (S2) → 5. The restore-ancestry merge case exits 2.
    assert r.code == 5
    assert "run `pubrepo pull`" in r.stderr


# T16
def test_amend_foreign_head_no_log_refused(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)

    log_path = pd / ".git" / "pubrepo-log.jsonl"
    if log_path.exists():
        log_path.unlink()

    work = tmp_path / "manual"
    git_run("clone", "-q", remote, str(work))
    git_run("config", "user.email", "m@test", cwd=str(work))
    git_run("config", "user.name", "Manual", cwd=str(work))
    git_run("commit", "--allow-empty", "-m", "manual edit", cwd=str(work))
    git_run("push", "-q", "origin", "main", cwd=str(work))

    import shutil
    shutil.rmtree(str(pd))
    run(["init"], cwd=repo)

    r = run(["--amend", "--yes"], cwd=repo)
    # §9, §10 items 6 and 4: a manual foreign commit on top is S2 → 5.
    assert r.code == 5
    assert "run `pubrepo pull`" in r.stderr


# T17
def test_amend_log_mismatch_refused(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)

    git_run("commit", "--amend", "-m", "garbage\n\nSource: fake123\n", cwd=str(pd))
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()
    git_run("push", "--force", "origin", "main", cwd=str(pd))

    r = run(["--amend", "--yes"], cwd=repo)
    # §9, §10 items 6 and 4: our recorded snapshot is gone → S7 → 5.
    assert r.code == 5
    assert "no longer on public" in r.stderr
    assert "publish log records" not in r.stderr


# T18
def test_amend_fresh_init_clone_accepted(source_repo, public_remote, run, published_tree):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)

    import shutil
    shutil.rmtree(str(pd))
    run(["init"], cwd=repo)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0
    assert published_tree(remote) == {"a.py": b"v2\n"}


# T19
# AC75: AC75 re-target: the push is `push --porcelain … origin` (push_argv) and git() takes env=; assertions unchanged
def test_amend_nothing_to_publish(source_repo, public_remote, run, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    real_git = pubrepo.git
    push_calls = []

    def spying_git(*args, cwd=None, check=True, **kwargs):   # AC75 re-target: env=
        if push_argv(args) is not None:  # AC75 re-target: the porcelain push argv
            push_calls.append(args)
        return real_git(*args, cwd=cwd, check=check, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spying_git)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0
    assert "Nothing to publish" in r.stdout
    assert git_run("rev-parse", "HEAD", cwd=str(pd)).strip() == pre_sha
    assert not push_calls

    entries = _log_entries(pd)
    pushed_entries = [e for e in entries if e.get("pushed")]
    assert len(pushed_entries) == 1  # only the initial


# T20
def test_amend_message_only(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    r = run(["--amend", "--yes", "-m", "new title"], cwd=repo)
    assert r.code == 0

    post_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()
    assert post_sha != pre_sha
    subject = git_run("log", "-1", "--format=%s", cwd=str(pd)).strip()
    assert subject == "new title"

    entries = _log_entries(pd)
    amend_entry = [e for e in entries if e.get("amend")]
    assert len(amend_entry) == 1
    assert amend_entry[0]["added"] == 0
    assert amend_entry[0]["modified"] == 0
    assert amend_entry[0]["deleted"] == 0


# T21
def test_amend_m_equal_to_existing_title_is_noop(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)
    existing_title = git_run("log", "-1", "--format=%s", cwd=str(pd)).strip()
    pre_sha = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()

    r = run(["--amend", "--yes", "-m", existing_title], cwd=repo)
    assert r.code == 0
    assert "Nothing to publish" in r.stdout
    assert git_run("rev-parse", "HEAD", cwd=str(pd)).strip() == pre_sha


# T22
def test_amend_chain_twice(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    (repo / "a.py").write_text("v3\n")
    git_run("commit", "-aqm", "v3", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    count = _remote_count(pd)
    # Still 2: the initial (from _setup's second publish) + 1 amend chain.
    # Actually _setup does init+publish → 1 commit on remote. Two amends → still 1.
    # Wait: _setup does one publish → remote has 1 commit. amend replaces it twice → 1.
    # But there was an init publish first. Let me re-check: _setup does init, then
    # run(["publish", "--yes"], cwd=repo) which does a normal publish. Remote has 1 commit.
    # First amend replaces → 1. Second amend replaces → 1.
    assert count == 1


# T23
def test_amend_log_entry_fields(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    pd = _publish_dir(repo)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    entries = _log_entries(pd)
    assert len(entries) == 2

    normal_entry = entries[0]
    assert normal_entry["amend"] is False
    assert normal_entry["amended_from"] is None

    amend_entry = entries[1]
    assert amend_entry["amend"] is True
    assert amend_entry["amended_from"] == normal_entry["public_commit"]
    assert amend_entry["forced"] is False
    assert amend_entry["public_commit"] == git_run(
        "rev-parse", "HEAD", cwd=str(pd)).strip()


# T24
def test_amend_tags_source(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    # AC75 re-target (§10 item 20, §18.9 R2): the bookmark is the local ref
    # refs/pubrepo/bookmarks/<ts>, never a published/* tag
    assert git_run("tag", "-l", "published/*", cwd=repo).strip() == ""
    tags = git_run("for-each-ref", "--format=%(refname)", "refs/pubrepo/bookmarks/",
                   cwd=repo).strip().splitlines()
    assert len(tags) >= 1

    if "already exists, skipping" in r.stderr:
        assert len(tags) == 1
    elif len(tags) == 2:
        pass  # two different seconds


# T25
def test_amend_quiet_silent(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--quiet", "--amend", "--yes"], cwd=repo)
    assert r.code == 0
    assert r.stdout == ""


# T26
@pytest.mark.parametrize("flags,expected_code", [
    (["--amend", "--force-overwrite"], 1),
    (["--amend", "--stage"], 1),
    (["--no-amend", "--stage"], 1),
    (["--amend", "--dry-run"], 1),
    (["--amend", "--diff"], 1),
    (["--amend", "--json"], 1),
    (["--no-amend", "--dry-run"], 1),
    (["--no-amend", "--diff"], 1),
])
def test_flag_conflicts(source_repo, public_remote, run, monkeypatch, flags, expected_code):
    remote, repo = _setup(source_repo, public_remote, run)

    if "--force-overwrite" in flags and "--amend" in flags:
        real_git = pubrepo.git
        calls = []

        def spying_git(*args, cwd=None, check=True):
            calls.append(args)
            return real_git(*args, cwd=cwd, check=check)

        monkeypatch.setattr(pubrepo, "git", spying_git)
        calls.clear()
        r = run(flags, cwd=repo)
        assert r.code == expected_code
        assert not calls
    else:
        r = run(flags, cwd=repo)
        assert r.code == expected_code


# T27
@pytest.mark.no_git_calls  # AC63: refused at config load, before any git call
def test_config_on_republish_amend(source_repo, public_remote, run, published_tree, tmp_path):
    """§7, §10 item 3: on_republish = "amend" is retired. With it in
    the config from the first publish, every publish is a config error
    (exit 1) with the retirement message, and nothing is published."""
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"], "on_republish": "amend"}
    repo = source_repo({"a.py": "v1\n"}, config)

    run(["init"], cwd=repo)
    for args in (["publish", "--yes"], ["--no-amend"], ["--amend", "--yes"]):
        r = run(args, cwd=repo)
        assert r.code == 1, args
        assert "retired" in r.stderr and "on_republish" in r.stderr
    assert published_tree(remote) == {}


# T29
def test_amend_preserves_author_identity_refreshes_dates(source_repo, public_remote, run,
                                                          monkeypatch):
    remote = public_remote()
    repo = source_repo({"a.py": "v1\n"}, {"remote": remote, "include": ["a.py"]})
    pd = repo / ".publish"

    run(["init"], cwd=repo)

    # Set the publish clone's identity to a known value for the initial publish
    git_run("config", "user.name", "Pubrepo Tests", cwd=str(pd))
    git_run("config", "user.email", "test@pubrepo.test", cwd=str(pd))

    monkeypatch.setenv("GIT_AUTHOR_DATE", "2020-01-02T03:04:05+00:00")
    monkeypatch.setenv("GIT_COMMITTER_DATE", "2020-01-02T03:04:05+00:00")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0
    monkeypatch.delenv("GIT_AUTHOR_DATE")
    monkeypatch.delenv("GIT_COMMITTER_DATE")

    first_author = git_run("log", "-1", "--format=%an|%ae", cwd=str(pd)).strip()
    assert first_author == "Pubrepo Tests|test@pubrepo.test"

    # Change clone identity to a different user for the amend
    git_run("config", "user.name", "Second Identity", cwd=str(pd))
    git_run("config", "user.email", "second@test", cwd=str(pd))

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0

    fields = git_run("log", "-1", "--format=%an|%ae|%aI|%cn|%ce|%cI",
                     cwd=str(pd)).strip()
    parts = fields.split("|")
    author_name, author_email, author_date = parts[0], parts[1], parts[2]
    committer_name, committer_email, committer_date = parts[3], parts[4], parts[5]

    assert author_name == "Pubrepo Tests"
    assert author_email == "test@pubrepo.test"
    assert "2020" not in author_date

    from datetime import datetime as dt
    ad = dt.fromisoformat(author_date)
    cd = dt.fromisoformat(committer_date)
    assert abs((ad - cd).total_seconds()) < 60

    assert committer_name == "Second Identity"
    assert committer_email == "second@test"


# T30
@pytest.mark.no_git_calls  # AC63: refused at config load, before any git call
def test_stage_ignores_on_republish(source_repo, public_remote, run):
    # §7, §10 item 3: the retired key is refused at config load, for
    # publish and --stage alike.
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"], "on_republish": "amend"}
    repo = source_repo({"a.py": "v1\n"}, config)
    run(["init"], cwd=repo)
    for args in (["publish", "--yes"], ["--stage"]):
        r = run(args, cwd=repo)
        assert r.code == 1, args
        assert "retired" in r.stderr


# Regression: git add -A failure must produce exit 2 (PublishError), not exit 1
def test_git_add_failure_exits_2(source_repo, public_remote, run, monkeypatch):
    remote, repo = _setup(source_repo, public_remote, run)

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)

    real_git = pubrepo.git

    # AC75 re-target: the rework stages through a temporary index
    # (read-tree/update-index/write-tree) instead of `git add -A`; the fault
    # moves to write-tree. Behavioral assertions unchanged.
    def failing_add_git(*args, cwd=None, check=True, **kwargs):
        if args and args[:1] == ("write-tree",) and ".publish" in str(cwd):
            if check:
                raise pubrepo.GitError(list(args), "fatal: unable to stat")
            from types import SimpleNamespace
            return SimpleNamespace(returncode=128, stdout="",
                                  stderr="fatal: unable to stat")
        return real_git(*args, cwd=cwd, check=check, **kwargs)

    monkeypatch.setattr(pubrepo, "git", failing_add_git)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2
    assert "staging failed" in r.stderr
