"""T9: publish locking (M3) + keep validation (M2) + keep behavior."""
import fcntl

from conftest import git_run


def _setup(source_repo, public_remote, run, config_extra=None):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra or {})
    repo = source_repo({"a.py": "x\n"}, config)
    run(["init"], cwd=repo)
    return remote, repo


# --- locking (M3) --------------------------------------------------------------

def test_lock_contention_exit_3_with_pid(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    lock_path = repo / ".publish" / ".git" / "pubrepo.lock"

    with open(lock_path, "w") as holder:
        holder.write("424242")
        holder.flush()
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        r = run([], cwd=repo)
        assert r.code == 3
        assert "already running" in r.stderr
        assert "424242" in r.stderr

    # Holder released the lock => publish proceeds.
    r = run([], cwd=repo)
    assert r.code == 0


def test_lock_released_after_failure(source_repo, public_remote, run):
    """The context manager must release the lock when the publish raises."""
    remote, repo = _setup(source_repo, public_remote, run,
                          {"scrub": {"forbidden": ["sekrit"]}})
    (repo / "a.py").write_text("the sekrit\n")

    r = run([], cwd=repo)
    assert r.code == 4  # scrub failure inside the lock

    lock_path = repo / ".publish" / ".git" / "pubrepo.lock"
    with open(lock_path, "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # acquirable => released


# --- keep validation (M2) -------------------------------------------------------

def test_nested_keep_rejected_at_load(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["a.py"], "keep": ["docs/LICENSE"]},
    )
    r = run(["status"], cwd=repo)
    assert r.code == 1
    assert "top-level-only" in r.stderr


# --- keep behavior --------------------------------------------------------------

def test_keep_survives_nuke_and_publishes(source_repo, public_remote, run, published_tree):
    remote, repo = _setup(source_repo, public_remote, run, {"keep": ["LICENSE"]})
    r = run([], cwd=repo)
    assert r.code == 0

    (repo / ".publish" / "LICENSE").write_text("MIT etc.\n")
    r = run([], cwd=repo)
    assert r.code == 0

    tree = published_tree(remote)
    assert tree.get("LICENSE") == b"MIT etc.\n"
    assert "a.py" in tree


def test_keep_file_is_scrubbed(source_repo, public_remote, run):
    """keep files get no scrub exemption — they publish, so they're checked."""
    remote, repo = _setup(source_repo, public_remote, run,
                          {"keep": ["LICENSE"], "scrub": {"forbidden": ["sekrit"]}})
    r = run([], cwd=repo)
    assert r.code == 0

    (repo / ".publish" / "LICENSE").write_text("contains sekrit\n")
    r = run([], cwd=repo)
    assert r.code == 4
    assert "LICENSE" in r.stderr
