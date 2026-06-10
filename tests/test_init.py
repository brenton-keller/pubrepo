"""T10: init redesign (M5) — no silent fallback, guided first run."""
from conftest import git_run


def test_clone_failure_surfaces_stderr_exit_2(source_repo, public_remote, run):
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": "/nonexistent/nowhere.git", "include": ["a.py"]},
    )
    r = run(["init"], cwd=repo)
    assert r.code == 2
    assert "clone failed" in r.stderr.lower()
    assert "Create the remote repo first" in r.stderr
    # The old silent fallback is gone:
    assert "initializing fresh" not in r.stdout
    assert not (repo / ".publish").exists()


def test_empty_remote_lands_on_configured_branch(source_repo, public_remote, run):
    remote = public_remote()  # bare, -b main, zero commits
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["a.py"], "branch": "release"},
    )
    r = run(["init"], cwd=repo)
    assert r.code == 0
    head = git_run("symbolic-ref", "--short", "HEAD", cwd=repo / ".publish").strip()
    assert head == "release"

    r = run([], cwd=repo)  # full publish lands on that branch
    assert r.code == 0
    ls = git_run("ls-remote", remote)
    assert "refs/heads/release" in ls


def test_identity_warning_when_unresolvable(source_repo, public_remote, run, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    r = run(["init"], cwd=repo)
    assert r.code == 0
    assert "no git identity resolvable" in r.stderr
    assert "git -C .publish config user.name" in r.stderr


def test_gitignore_suggestion_when_entry_missing(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", ".gitignore": "*.pyc\n"},  # overrides the fixture default
        {"remote": remote, "include": ["a.py"]},
    )
    r = run(["init"], cwd=repo)
    assert r.code == 0
    assert "add '.publish/' to your .gitignore" in r.stderr


def test_no_gitignore_suggestion_when_entry_present(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    r = run(["init"], cwd=repo)  # fixture .gitignore already has .publish/
    assert r.code == 0
    assert "add '.publish/'" not in r.stderr


def test_init_idempotent(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    assert run(["init"], cwd=repo).code == 0
    r = run(["init"], cwd=repo)
    assert r.code == 0
    assert "Already initialized" in r.stdout


# --- P3: gitignore note covers the no-.gitignore case; publish warns if tracked ---

def test_gitignore_suggestion_when_file_absent(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    gi = repo / ".gitignore"
    if gi.exists():
        gi.unlink()
    r = run(["init"], cwd=repo)
    assert r.code == 0
    assert "create a .gitignore and add '.publish/'" in r.stderr


def test_publish_warns_when_publish_dir_tracked(source_repo, public_remote, run):
    import subprocess
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    gi = repo / ".gitignore"
    if gi.exists():
        gi.unlink()
    assert run(["init"], cwd=repo).code == 0
    # First publish gives .publish/.git a commit; only then can the classic
    # mistake (git add . sweeping in the embedded repo as a gitlink) happen.
    assert run(["publish"], cwd=repo).code == 0
    subprocess.run(["git", "add", "-f", ".publish"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "oops"], cwd=repo, check=True,
                   capture_output=True)
    (repo / "a.py").write_text("y\n")  # make a change so publish proceeds
    r = run(["publish"], cwd=repo)
    assert r.code == 0  # warning, not a failure
    assert ".publish/ is tracked by the source repo" in r.stderr
    assert "git rm -r --cached .publish" in r.stderr


def test_publish_no_tracked_warning_normally(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    assert run(["init"], cwd=repo).code == 0
    r = run(["publish"], cwd=repo)
    assert r.code == 0
    assert "is tracked by the source repo" not in r.stderr
