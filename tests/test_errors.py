"""T22: golden-phrase tests for the top error paths (phrases, not whole
strings — refactor-tolerant) + the generic last-resort catch. T21's phase
progress lines are pinned here too."""
import subprocess

import pytest

import pubrepo


def _repo(source_repo, public_remote, **config_extra):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra)
    return source_repo({"a.py": "x\n"}, config)


def test_missing_config_suggests_starter(source_repo, public_remote, run, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    r = run(["--dry-run"], cwd=empty)
    assert r.code == 1
    assert "not found" in r.stderr
    assert "[publish]" in r.stderr           # starter snippet
    assert "pubrepo init" in r.stderr   # _PROG fallback under pytest


def test_bad_toml_keeps_line_info(source_repo, public_remote, run, tmp_path):
    repo = tmp_path / "badtoml"
    repo.mkdir()
    (repo / ".publish.toml").write_text("[publish\nremote = x\n")
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 1
    assert "invalid TOML" in r.stderr
    assert "line" in r.stderr                # tomllib's line/col preserved


def test_missing_include_path_renamed_hint(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote, include=["a.py", "ghost.py"])
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 1
    assert "not found in working tree" in r.stderr
    assert "was it renamed?" in r.stderr


def test_not_initialized_says_what_to_do(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    assert "not initialized" in r.stderr
    assert "init" in r.stderr


def test_identity_less_commit_gives_exact_commands(source_repo, public_remote, run, monkeypatch, tmp_path):
    repo = _repo(source_repo, public_remote)
    run(["init"], cwd=repo)
    publish_dir = repo / ".publish"
    # Erase identity everywhere git could find it: env, global, system,
    # local repo config, HOME, and XDG.  macOS CI runners inject identity
    # via actions/checkout's includeIf.gitdir — overriding HOME is not
    # enough; we must also blank the repo-local config.
    fake_home = tmp_path / "empty_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for v in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
              "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(v, raising=False)
    # Strip identity from .publish repo-local config while preserving
    # remote/branch — actions/checkout writes identity + includeIf here
    # and env overrides alone don't suppress includeIf-sourced identity.
    cfg = publish_dir / ".git" / "config"
    lines = cfg.read_text().splitlines(keepends=True)
    cleaned = [l for l in lines
               if not any(k in l.lower() for k in ("user.name", "user.email", "includeif"))]
    cfg.write_text("".join(cleaned))
    r = run([], cwd=repo)
    assert r.code == 2
    assert "git -C .publish config user.name" in r.stderr
    assert "git -C .publish config user.email" in r.stderr


def test_unexpected_failure_clean_line_and_verbose_reraise(source_repo, public_remote, run, monkeypatch):
    repo = _repo(source_repo, public_remote)
    run(["init"], cwd=repo)

    def exploding(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pubrepo, "copy_includes", exploding)
    r = run([], cwd=repo)
    assert r.code == 1
    assert "unexpected failure" in r.stderr
    assert "RuntimeError" in r.stderr
    assert "rerun with --verbose" in r.stderr
    assert "Traceback" not in r.stderr

    with pytest.raises(RuntimeError):       # --verbose re-raises
        run(["publish", "-v"], cwd=repo)


def test_phase_progress_lines(source_repo, public_remote, run):
    """T21: one line per phase at normal verbosity."""
    repo = _repo(source_repo, public_remote,
                 **{"scrub": {"forbidden": ["sekrit"]}})
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "Fetching origin/main... ok" in r.stderr
    assert "Rebuilding .publish/ (1 files)... ok" in r.stderr
    assert "Scrubbing (1 files, 1 patterns)... ok" in r.stderr
    assert "Committing... ok" in r.stderr
    assert "Pushing... ok" in r.stderr
    assert "Published" in r.stdout


def test_overlapping_includes_dedupe(source_repo, public_remote, run):
    """L8: overlapping includes must not double-count in the report."""
    remote = public_remote()
    repo = source_repo(
        {"src/app.py": "a\n", "src/util.py": "u\n"},
        {"remote": remote, "include": ["src/", "src/app.py"]},  # overlap
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "Rebuilding .publish/ (2 files)... ok" in r.stderr  # not 3
