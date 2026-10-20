"""Release contract tests through the CLI and repository boundaries only."""
import json
import pytest

from conftest import git_run


def _release():
    return {"enabled": True, "version_file": "version.py",
            "version_pattern": r'^VERSION = "(?P<version>[0-9.]+)"$',
            "tag_format": "v{version}"}


def _repo(source_repo, public_remote):
    remote = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "public.txt": "ok\n"}, {
        "remote": remote, "include": ["version.py", "public.txt"], "release": _release(),
    })
    return repo, remote


def test_ci_ambiguity_refuses_without_public_branch(source_repo, public_remote, run):
    repo, remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    result = run(["publish", "--yes"], repo)
    assert result.code == 1
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""


def test_explicit_no_release_advances_without_formal_tag(source_repo, public_remote, run):
    repo, remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    assert run(["--no-release", "--yes"], repo).code == 0
    assert git_run("ls-remote", remote, "refs/heads/main").strip()
    assert git_run("ls-remote", "--tags", remote, "refs/tags/v1.0.0").strip() == ""


def test_formal_release_produces_annotated_tag_on_public_head(source_repo, public_remote, run):
    repo, remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    head = git_run("ls-remote", remote, "refs/heads/main").split()[0]
    peeled = git_run("ls-remote", "--tags", remote, "refs/tags/v1.0.0^{}").split()[0]
    assert peeled == head


def test_stage_is_inert_and_reports_future_decision(source_repo, public_remote, run):
    repo, remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    result = run(["--stage", "--json"], repo)
    assert result.code == 0
    assert json.loads(result.stdout)["release_decision_required"] is True
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""


def test_unpublished_private_dirt_does_not_block_release(source_repo, public_remote, run):
    repo, remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    private = repo / "notes.private"
    private.write_text("untracked private work")
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    clone = repo.parent / "world"
    git_run("clone", "-q", remote, str(clone))
    assert not (clone / "notes.private").exists()


def test_source_head_and_files_are_never_mutated(source_repo, public_remote, run):
    repo, _remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    before_head = git_run("rev-parse", "HEAD", cwd=repo)
    before = (repo / "version.py").read_bytes()
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    assert git_run("rev-parse", "HEAD", cwd=repo) == before_head
    assert (repo / "version.py").read_bytes() == before


def test_repeated_complete_release_is_idempotent(source_repo, public_remote, run):
    repo, remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    before = git_run("rev-list", "--count", "HEAD", cwd=repo / ".publish")
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    assert git_run("rev-list", "--count", "HEAD", cwd=repo / ".publish") == before
    assert len(git_run("ls-remote", "--tags", remote, "refs/tags/v1.0.0").splitlines()) == 1


def test_release_abandon_changes_no_remote_ref(source_repo, public_remote, run):
    repo, remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    # Missing-journal abandonment is itself a release failure, and must remain
    # a local-only operation.
    before = git_run("status", "--porcelain=v1", cwd=repo / ".publish")
    result = run(["release-abandon", "9.9.9", "--yes"], repo)
    assert result.code == 7
    assert git_run("status", "--porcelain=v1", cwd=repo / ".publish") == before
    assert git_run("ls-remote", remote).strip() == ""


def test_release_failure_uses_exit_seven_not_existing_codes(source_repo, public_remote, run):
    repo, _remote = _repo(source_repo, public_remote)
    assert run(["init"], repo).code == 0
    (repo / "version.py").write_text('VERSION = "2.0.0"\n')
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7


@pytest.mark.no_git_calls  # AC63: refused before any git call
def test_release_flags_never_degrade_on_unmanaged_project(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a": "a"}, {"remote": remote, "include": ["a"]})
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 1
    assert not (repo / ".publish").exists()


def test_no_release_permits_non_semver_source_state(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"version.py": 'VERSION = "development"\n'}, {
        "remote": remote, "include": ["version.py"],
        "release": {"enabled": True, "version_file": "version.py",
                    "version_pattern": r'^VERSION = "(?P<version>[A-Za-z]+)"$',
                    "tag_format": "v{version}"},
    })
    assert run(["init"], repo).code == 0
    result = run(["--no-release", "--yes"], repo)
    assert result.code == 0, result.stderr
    assert git_run("ls-remote", remote, "refs/heads/main").strip()


def test_bump_without_stable_remote_baseline_refuses_with_exact_alternative(
        source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n'}, {
        "remote": remote,
        "include": ["version.py"],
        "release": {
            "enabled": True,
            "version_file": "version.py",
            "version_pattern": r'^VERSION = "(?P<version>[0-9.]+)"$',
            "tag_format": "v{version}",
        },
    })
    assert run(["init"], repo).code == 0
    result = run(["--bump", "minor", "--yes"], repo)
    assert result.code == 7
    assert "--release INITIAL_VERSION" in result.stderr
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""
