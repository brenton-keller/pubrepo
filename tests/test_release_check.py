import json
import subprocess

import pytest

import pubrepo


def _repo(source_repo, public_remote):
    return source_repo({"version.py": 'VERSION = "1.0.0"\n'}, {
        "remote": public_remote(), "include": ["version.py"],
        "release": {"enabled": True, "version_file": "version.py",
                    "version_pattern": r'^VERSION = "(?P<version>[0-9.]+)"$',
                    "tag_format": "v{version}"},
    })


def test_release_check_is_read_only_on_bare_remote(source_repo, public_remote, run, monkeypatch):
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    publish = repo / ".publish"
    before_files = {str(path.relative_to(publish)): path.read_bytes()
                    for path in publish.rglob("*") if path.is_file()}
    before_refs = pubrepo.git("show-ref", cwd=str(publish), check=False).stdout
    calls = []
    real = pubrepo.git

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spy)
    result = run(["validate", "--release-check"], repo)
    assert result.code == 0, result.stderr
    assert "Release check OK" in result.stdout
    assert not any(call and call[0] in ("push", "tag") for call in calls)
    assert not any(call[:2] == ("fetch", "--tags") for call in calls)
    assert not any("build" in call or "twine" in call for call in calls)
    after_files = {str(path.relative_to(publish)): path.read_bytes()
                   for path in publish.rglob("*") if path.is_file()}
    assert after_files == before_files
    assert pubrepo.git("show-ref", cwd=str(publish), check=False).stdout == before_refs


def test_release_check_json_schema(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    result = run(["validate", "--release-check", "--json"], repo)
    assert result.code == 0
    assert json.loads(result.stdout) == {"valid": True, "errors": [], "warnings": []}


@pytest.mark.no_git_calls  # AC63: refused before any git call
def test_release_check_requires_enabled_config(source_repo, public_remote, run):
    repo = source_repo({"a": "a"}, {"remote": public_remote(), "include": ["a"]})
    result = run(["validate", "--release-check"], repo)
    assert result.code == 1
    assert "requires [publish.release]" in result.stderr


def test_sanitizer_redacts_url_userinfo_and_all_secret_occurrences(monkeypatch):
    monkeypatch.setenv("TWINE_PASSWORD", "s3cret")
    monkeypatch.setenv("GH_TOKEN", "token-value")
    text = "https://alice:pw@example.com/a s3cret s3cret token-value"
    sanitized = pubrepo.sanitize_provider_output(text)
    assert "alice" not in sanitized and "pw" not in sanitized
    assert "s3cret" not in sanitized and "token-value" not in sanitized
    assert sanitized.count("[redacted]") >= 4


def test_release_git_failure_is_sanitized_before_error_output(tmp_path, monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "unique-release-canary")
    monkeypatch.setattr(pubrepo, "git", lambda *_a, **_k: subprocess.CompletedProcess(
        [], 1, "", "https://alice:pw@example.test unique-release-canary"))
    with pytest.raises(pubrepo.ReleaseError) as exc:
        pubrepo.read_remote_ledger(tmp_path, "v{version}")
    message = str(exc.value)
    assert "alice" not in message and "pw" not in message
    assert "unique-release-canary" not in message


def test_no_adapters_means_no_tool_probe(monkeypatch):
    monkeypatch.setattr(pubrepo, "_run_tool", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("tool probed")))
    config = {"release": {"enabled": True}}
    assert pubrepo.enabled_adapters(config) == []
