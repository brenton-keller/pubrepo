"""T15 (centralized validate) + T16 (status dashboard, --check, --remote)."""
import json

import pytest

from conftest import git_run
from test_divergence import _external_commit


def _repo(source_repo, public_remote, files=None, **config_extra):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra)
    return source_repo(files or {"a.py": "x\n"}, config)


# --- T15: validate findings ------------------------------------------------------

@pytest.mark.parametrize("config_extra,level,phrase", [
    ({"include": []}, "error", "'include' must not be empty"),
    ({"branch": ""}, "error", "'branch' must be a non-empty string"),
    ({"dir": ".workdir"}, "error", "'dir' must be '.publish'"),
    ({"dir": ".publish-sdk"}, None, None),                       # valid namespace
    ({"exclude": [""]}, "warning", "empty string in exclude"),
    ({"exclude": ["*"]}, "warning", "matches everything"),
    ({"keep": ["docs/LICENSE"]}, "error", "top-level-only"),
    ({"bogus_key": "x"}, "warning", "unknown key 'bogus_key' in [publish]"),
    ({"scrub": {"command": "gitleaks"}}, "warning", "unknown key 'command' in [publish.scrub]"),
    ({"scrub": {"forbidden": [""]}}, "warning", "empty string in forbidden"),
    ({"transforms": {"a.py": [{"find": "a", "replace": "b", "extra": 1}]}},
     "warning", "unknown key 'extra'"),
    ({"transforms": {"ghost.py": [{"find": "a", "replace": "b"}]}},
     "warning", "will never run"),
])
def test_validate_findings(source_repo, public_remote, run, config_extra, level, phrase):
    repo = _repo(source_repo, public_remote, **config_extra)
    r = run(["validate"], cwd=repo)
    if level is None:
        assert r.code == 0
    elif level == "error":
        assert r.code == 1
        assert phrase in r.stderr
    else:
        assert r.code == 0  # warnings don't block
        assert phrase in r.stderr
        assert "Config OK" in r.stdout


def test_validate_reports_all_errors_at_once(source_repo, public_remote, run):
    """validate must not die at the first error like strict load does."""
    repo = _repo(source_repo, public_remote,
                 include=[], branch="", keep=["a/b"])
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "'include' must not be empty" in r.stderr
    assert "'branch' must be a non-empty string" in r.stderr
    assert "top-level-only" in r.stderr


def test_validate_json_schema(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote, exclude=[""])
    r = run(["validate", "--json"], cwd=repo)
    assert r.code == 0
    data = json.loads(r.stdout)
    assert data["valid"] is True
    assert data["errors"] == []
    assert len(data["warnings"]) == 1
    assert set(data["warnings"][0]) == {"level", "field", "message"}


def test_strict_load_warns_on_unknown_keys(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote, bogus_key="x")
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 0  # warning, not error
    assert "unknown key 'bogus_key'" in r.stderr


# --- T16: status dashboard --------------------------------------------------------

def test_status_sections_render(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    run(["init"], cwd=repo)
    run([], cwd=repo)
    r = run(["status"], cwd=repo)
    assert r.code == 0
    for section in ("Config:", "  Remote:", "  Rules:", "Source:", "  Branch:   main",
                    "Last published:", "No changes to publish"):
        assert section in r.stdout


def test_status_check_exit_codes_and_precedence(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote,
                 **{"scrub": {"forbidden": ["sekrit"]}})
    run(["init"], cwd=repo)
    run([], cwd=repo)

    assert run(["status", "--check"], cwd=repo).code == 0      # up to date

    (repo / "a.py").write_text("changed\n")
    assert run(["status", "--check"], cwd=repo).code == 6      # pending

    (repo / "a.py").write_text("changed sekrit\n")
    assert run(["status", "--check"], cwd=repo).code == 4      # scrub beats pending

    assert run(["status"], cwd=repo).code == 0                 # without --check: informational


def test_status_check_uninitialized_is_pending(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    assert run(["status", "--check"], cwd=repo).code == 6
    assert run(["status"], cwd=repo).code == 0


def test_status_json_schema(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    run(["init"], cwd=repo)
    run([], cwd=repo)
    r = run(["status", "--json"], cwd=repo)
    data = json.loads(r.stdout)
    assert set(data) == {"status", "config", "source", "last_publish",
                         "changes", "scrub", "remote"}
    assert data["status"] == "up_to_date"
    assert data["config"]["include"] == 1
    assert data["source"]["branch"] == "main"
    assert data["remote"] is None  # offline by default


def test_status_remote_up_to_date_and_diverged(source_repo, public_remote, run, tmp_path):
    repo = _repo(source_repo, public_remote)
    run(["init"], cwd=repo)
    run([], cwd=repo)

    r = run(["status", "--remote", "--json"], cwd=repo)
    data = json.loads(r.stdout)
    assert data["remote"] == {"checked": True, "diverged": False, "ahead_count": 0}

    remote_url = json.loads(run(["status", "--json"], cwd=repo).stdout)["config"]["remote"]
    _external_commit(remote_url, tmp_path)

    r = run(["status", "--remote"], cwd=repo)
    assert "Remote: DIVERGED" in r.stdout
    data = json.loads(run(["status", "--remote", "--json"], cwd=repo).stdout)
    assert data["remote"]["diverged"] is True
    assert data["remote"]["ahead_count"] == 1
