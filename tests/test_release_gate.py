import builtins
import json
from types import SimpleNamespace

import pytest

import pubrepo
from conftest import git_run


def _release(enabled=True):
    return {"enabled": enabled, "version_file": "version.py",
            "version_pattern": r'^VERSION = "(?P<version>[0-9A-Za-z.+-]+)"$',
            "tag_format": "v{version}"}


def _repo(source_repo, public_remote, release=True):
    config = {"remote": public_remote(), "include": ["version.py", "a.txt"]}
    if release is not None:
        config["release"] = _release(release)
    return source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, config)


def test_noninteractive_enabled_publish_requires_explicit_choice_before_mutation(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    publish = repo / ".publish"
    before = git_run("status", "--porcelain=v1", cwd=publish)
    result = run(["publish", "--yes"], repo)
    assert result.code == 1
    assert "--bump minor" in result.stderr
    assert "--release VERSION" in result.stderr
    assert "--no-release" in result.stderr
    assert git_run("status", "--porcelain=v1", cwd=publish) == before


@pytest.mark.parametrize("strategy", [["--amend", "--yes"], ["--force-overwrite"]])  # R2
def test_gate_also_applies_to_amend_and_overwrite(source_repo, public_remote, run, strategy):
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    result = run(strategy, repo)
    assert result.code == 1
    if strategy == ["--force-overwrite"]:
        # §10 item 3: the refusal, not the release-gate text.
        assert "removed in 1.1.0" in result.stderr
    else:
        assert "explicit choice" in result.stderr


@pytest.mark.parametrize("flag", [
    ["--release", "1.0.0"], ["--bump", "patch"], ["--no-release"],
])
@pytest.mark.parametrize("preview", [["--stage"], ["--dry-run"], ["--diff"]])
def test_release_flags_rejected_by_previews(source_repo, public_remote, run, flag, preview):
    repo = _repo(source_repo, public_remote)
    result = run(preview + flag, repo)
    assert result.code == 1
    assert "real publish" in result.stderr
    assert not (repo / ".publish").exists()


@pytest.mark.no_git_calls  # AC63: refused before any git call
def test_release_flag_on_absent_config_is_opt_in_error(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote, release=None)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 1
    assert "enabled = true" in result.stderr


def test_no_release_runs_existing_publish_pipeline(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": remote, "include": ["version.py", "a.txt"], "release": _release(),
    })
    assert run(["init"], repo).code == 0
    result = run(["--no-release", "--yes"], repo)
    assert result.code == 0
    assert published_tree(remote)["a.txt"] == b"a\n"
    assert not (repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME).exists()


def test_stage_json_adds_decision_key_only_when_enabled(source_repo, public_remote, run):
    enabled = _repo(source_repo, public_remote)
    assert run(["init"], enabled).code == 0
    data = json.loads(run(["--stage", "--json"], enabled).stdout)
    assert data["release_decision_required"] is True

    absent = _repo(source_repo, public_remote, release=None)
    assert run(["init"], absent).code == 0
    data = json.loads(run(["--stage", "--json"], absent).stdout)
    assert "release_decision_required" not in data


def test_text_stage_notes_future_choice(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    result = run(["--stage"], repo)
    assert result.code == 0
    assert "next real publish requires" in result.stderr


def test_status_release_block_only_when_enabled(source_repo, public_remote, run):
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    assert "Release:" in run(["status"], repo).stdout
    absent = _repo(source_repo, public_remote, release=None)
    assert run(["init"], absent).code == 0
    assert "Release:" not in run(["status"], absent).stdout


# AC75: §16 row "§4 Ledger: a matching remote tag is presumed to be a completed legacy/external release" (Replaced → §8.5: managed = marker trailers or adopt_tags)
def test_status_labels_remote_tag_without_local_completion_attestation(source_repo, public_remote, run, tmp_path):
    """§8.5: the label applies to managed tags only; a
    higher foreign tag is neither the latest release nor labelled."""
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    (repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0.json").unlink()
    remote = git_run("remote", "get-url", "origin", cwd=repo / ".publish").strip()
    collab = tmp_path / "collab-tag"
    git_run("clone", "-q", remote, str(collab))
    git_run("tag", "v9.0.0", cwd=collab)                       # foreign: no marker, not adopted
    git_run("push", "-q", "origin", "refs/tags/v9.0.0", cwd=collab)
    result = run(["status"], repo)
    assert result.code == 0
    assert "Latest:   v1.0.0" in result.stdout and "v9.0.0" not in result.stdout
    assert "remote tag; completion not locally attested" in result.stdout


# AC75: §10 item 18 (commands that push confirm; §17.3 one prompt): replaces test_prompt_blank_reprompts_and_only_consistent_bump_is_offered, deleted: an empty answer now cancels
def test_prompt_blank_cancels_and_only_consistent_bump_is_offered(source_repo, public_remote, run, monkeypatch, capsys):
    """§17.3 (one prompt): the choice is the
    one confirmation, so there is no default and an empty answer cancels
    (was: re-prompt)."""
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    publish = repo / ".publish"
    config = pubrepo.load_publish_config(repo / ".publish.toml")
    discovery = pubrepo.discover_release_state(publish, repo, config)
    monkeypatch.setattr(builtins, "input", lambda _="": "")
    assert pubrepo.prompt_release_choice(discovery, config) is None
    monkeypatch.setattr(builtins, "input", lambda _="": "1")
    choice = pubrepo.prompt_release_choice(discovery, config)
    assert choice == pubrepo.ReleaseChoice("release", "1.0.0", None, True)
    output = capsys.readouterr().err
    assert "no default" in output
    assert "bump patch" not in output


def test_prompt_eof_cancels(source_repo, public_remote, run, monkeypatch):
    repo = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    config = pubrepo.load_publish_config(repo / ".publish.toml")
    discovery = pubrepo.discover_release_state(repo / ".publish", repo, config)
    monkeypatch.setattr(builtins, "input", lambda _="": (_ for _ in ()).throw(EOFError))
    assert pubrepo.prompt_release_choice(discovery, config) is None


def test_help_is_additive_and_existing_descriptions_remain(capsys):
    code = pubrepo.main(["publish", "--help"])
    assert code == 0
    text = capsys.readouterr().out
    for old in ("--dry-run", "--force-overwrite", "--stage", "--amend", "Custom commit message"):
        assert old in text
    for new in ("--release", "--bump", "--no-release"):
        assert new in text


@pytest.mark.parametrize("stdin_tty,stderr_tty,expected", [
    (False, False, False),
    (True, False, False),
    (False, True, False),
    (True, True, True),
])
def test_interactive_gate_requires_both_input_and_diagnostic_ttys(
        monkeypatch, stdin_tty, stderr_tty, expected):
    monkeypatch.setattr(pubrepo.sys, "stdin", SimpleNamespace(
        isatty=lambda: stdin_tty))
    monkeypatch.setattr(pubrepo.sys, "stderr", SimpleNamespace(
        isatty=lambda: stderr_tty))
    assert pubrepo.is_interactive() is expected
