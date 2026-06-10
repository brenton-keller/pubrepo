"""T13 (ui: --quiet/--verbose/color) + T14 (subparsers, --version, exit codes)."""
import json

import pubrepo


def _repo(source_repo, public_remote, **config_extra):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra)
    return source_repo({"a.py": "x\n"}, config)


# --- T13: output discipline ----------------------------------------------------

def test_quiet_success_produces_empty_stdout(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    assert run(["init", "-q"], cwd=repo).stdout == ""
    r = run(["publish", "-q"], cwd=repo)
    assert r.code == 0
    assert r.stdout == ""
    assert "Published" not in r.stderr  # success is silent, not rerouted


def test_json_with_warning_still_parses(source_repo, public_remote, run):
    """Warnings go to stderr; --json stdout stays pristine."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "b.py": "y\n"},
        {"remote": remote, "include": ["a.py", "b.py"], "exclude": ["b.py"]},  # collision warning
    )
    r = run(["publish", "--dry-run", "--json"], cwd=repo)
    assert "exclude wins" in r.stderr
    data = json.loads(r.stdout)  # must not raise
    assert data["files"] == ["a.py"]


def test_verbose_emits_exclude_details_and_timing(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "junk.pyc": "c\n"},
        {"remote": remote, "include": ["."], "scrub": {"forbidden": ["sekrit"]}},
    )
    run(["init"], cwd=repo)
    r = run(["publish", "-v"], cwd=repo)
    assert r.code == 0
    assert "exclude: junk.pyc (pattern: *.pyc)" in r.stderr
    assert "timing: rebuild" in r.stderr
    assert "timing: push" in r.stderr


def test_no_color_and_tty_painting(monkeypatch):
    class FakeTty:
        def isatty(self):
            return True

    u = pubrepo._UI()
    u.configure()  # no NO_COLOR in env by default
    monkeypatch.delenv("NO_COLOR", raising=False)
    u.configure()
    assert "\033[31m" in u._paint("boom", u.RED, FakeTty())

    monkeypatch.setenv("NO_COLOR", "1")
    u.configure()
    assert u._paint("boom", u.RED, FakeTty()) == "boom"

    monkeypatch.delenv("NO_COLOR", raising=False)
    u.configure(no_color=True)
    assert u._paint("boom", u.RED, FakeTty()) == "boom"


# --- T14: CLI surface -----------------------------------------------------------

def test_default_command_shim_equivalence(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    bare = run(["--dry-run"], cwd=repo)
    explicit = run(["publish", "--dry-run"], cwd=repo)
    assert bare.code == explicit.code == 0
    assert bare.stdout == explicit.stdout


def test_version_exits_zero(source_repo, public_remote, run, tmp_path, capsys):
    # --version short-circuits before config load; any cwd works.
    r = run(["--version"], cwd=tmp_path)
    assert r.code == 0


def test_unknown_command_exits_one(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    r = run(["bogus"], cwd=repo)
    assert r.code == 1


def test_dry_run_flag_on_wrong_command_exits_one(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    r = run(["init", "--dry-run"], cwd=repo)  # argparse: unrecognized argument
    assert r.code == 1


def test_dry_run_exits_4_on_scrub_failure(source_repo, public_remote, run):
    """§8 behavior change: --dry-run exits 4 when scrub would fail."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "the sekrit\n"},
        {"remote": remote, "include": ["a.py"], "scrub": {"forbidden": ["sekrit"]}},
    )
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 4
    assert "SCRUB WOULD FAIL" in r.stdout

    r = run(["--dry-run", "--json"], cwd=repo)
    assert r.code == 4
    assert json.loads(r.stdout)["scrub"]["passed"] is False


def test_publish_json_without_dry_run_exits_one(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    r = run(["publish", "--json"], cwd=repo)
    assert r.code == 1
    assert "--json only applies" in r.stderr


def test_validate_happy_and_missing_include(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    r = run(["validate"], cwd=repo)
    assert r.code == 0
    assert "Config OK" in r.stdout

    (repo / "a.py").unlink()
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "not found in working tree" in r.stderr


# --- P0: prog-name in hint messages (same file, two names) ---------------------

def test_hints_name_pubrepo_by_default(run, monkeypatch, tmp_path):
    # Console-script users must never be told to run the internal alias.
    monkeypatch.setattr(pubrepo.sys, "argv", ["pubrepo", "status"])
    r = run(["status"], cwd=tmp_path)  # no .publish.toml here
    assert r.code == 1
    assert "Then: pubrepo init" in r.stderr
    assert "git-sync-publish" not in r.stdout + r.stderr


def test_hints_follow_internal_alias(run, monkeypatch, tmp_path):
    monkeypatch.setattr(pubrepo.sys, "argv",
                        ["/home/x/.local/bin/git-sync-publish", "status"])
    r = run(["status"], cwd=tmp_path)
    assert r.code == 1
    assert "Then: git-sync-publish init" in r.stderr


def test_unknown_argv0_falls_back_to_pubrepo(run, monkeypatch, tmp_path):
    monkeypatch.setattr(pubrepo.sys, "argv", ["/usr/bin/pytest", "status"])
    r = run(["status"], cwd=tmp_path)
    assert r.code == 1
    assert "Then: pubrepo init" in r.stderr


def test_not_initialized_hint_uses_prog(source_repo, public_remote, run, monkeypatch):
    repo = _repo(source_repo, public_remote)  # no init
    monkeypatch.setattr(pubrepo.sys, "argv", ["pubrepo", "status"])
    r = run(["status"], cwd=repo)
    assert "Run: pubrepo init" in r.stdout


# --- dry-run honors --quiet (§4) — the README pre-push hook depends on it ------

def test_dry_run_quiet_success_is_silent(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    r = run(["--dry-run", "-q"], cwd=repo)
    assert r.code == 0
    assert r.stdout == ""


def test_dry_run_quiet_scrub_failure_errors_to_stderr(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "uses acme-internal\n"},
                       {"remote": remote, "include": ["a.py"],
                        "scrub": {"forbidden": ["acme-internal"]}})
    r = run(["--dry-run", "-q"], cwd=repo)
    assert r.code == 4
    assert r.stdout == ""
    assert "SCRUB WOULD FAIL" in r.stderr
    assert "a.py" in r.stderr


def test_dry_run_quiet_json_still_emits_json(source_repo, public_remote, run):
    import json as _json
    repo = _repo(source_repo, public_remote)
    r = run(["--dry-run", "-q", "--json"], cwd=repo)
    assert r.code == 0
    assert _json.loads(r.stdout)["scrub"]["passed"] is True
