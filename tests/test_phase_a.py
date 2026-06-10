"""Backfilled automated tests for Phase A (T1-T4) and the T5 exit-code map.

These implement the per-task test specs from the release plan §5, which
shipped with manual verification while the scaffolding didn't yet exist.
"""
import re
from datetime import datetime
from pathlib import Path

import pytest

import pubrepo
from conftest import git_run


def _tags(repo):
    out = git_run("tag", "-l", "published/*", cwd=repo)
    return [line for line in out.splitlines() if line]


# --- T1: source tagging (H1) -------------------------------------------------

def test_clean_publish_tags_source_and_origin(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0

    tags = _tags(repo)
    assert len(tags) == 1
    assert re.fullmatch(r"published/\d{8}T\d{6}", tags[0])
    assert tags[0] in git_run("ls-remote", "--tags", "origin", cwd=repo)


def test_dirty_publish_does_not_tag(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    (repo / "a.py").write_text("y\n")  # tracked modification => dirty
    r = run([], cwd=repo)
    assert r.code == 0
    assert "(dirty)" in r.stdout
    assert _tags(repo) == []


def test_same_second_tag_collision_skips(source_repo, public_remote, run, monkeypatch):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)

    fixed = datetime.now().astimezone()

    class FixedDatetime:
        @staticmethod
        def now(tz=None):
            return fixed

    monkeypatch.setattr(pubrepo, "datetime", FixedDatetime)

    r = run([], cwd=repo)
    assert r.code == 0
    assert len(_tags(repo)) == 1

    (repo / "a.py").write_text("y\n")
    git_run("commit", "-aqm", "change", cwd=repo)  # stay clean => tag path runs
    r = run([], cwd=repo)
    assert r.code == 0
    assert "already exists, skipping" in r.stderr  # info => stderr since T13
    assert len(_tags(repo)) == 1


# --- T2: hard-excludes (H2) --------------------------------------------------

def test_root_include_ships_no_private_files(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "docs/d.md": "d\n"},
        {"remote": remote, "include": ["."]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "repository root" in r.stderr  # warn, don't block

    paths = set(published_tree(remote))
    assert "a.py" in paths and "docs/d.md" in paths
    assert ".publish.toml" not in paths
    assert not any(Path(p).parts[0] == ".git" or p.startswith(".publish") for p in paths)


def test_dry_run_listing_excludes_private(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["."]})
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 0
    lines = r.stdout.splitlines()
    assert "  a.py" in lines
    assert "  .publish.toml" not in lines
    assert not any(line == "  .git" or line.startswith("  .git/") for line in lines)
    assert not any(line.startswith("  .publish") for line in lines)


def test_nested_repo_warns_and_strips_git(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "vendored/lib/util.py": "u\n"},
        {"remote": remote, "include": ["."]},
    )
    git_run("init", "-q", "-b", "main", cwd=repo / "vendored/lib")  # nested repo
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "nested git repo at 'vendored/lib'" in r.stderr

    tree = published_tree(remote)
    assert "vendored/lib/util.py" in tree
    assert not any(".git" in Path(p).parts for p in tree)


def test_git_include_rejected(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": [".git"]})
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 1
    assert "can never be published" in r.stderr


def test_config_include_rejected(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": [".publish.toml"]})
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 1
    assert "can never be published" in r.stderr


def test_publish_namespace_is_not_greedy(source_repo, public_remote, run, published_tree):
    """'.publish' exact and '.publish-*' are hard-excluded; unrelated names
    that merely share the prefix ('.published-artifacts') must publish."""
    remote = public_remote()
    repo = source_repo(
        {
            ".published-artifacts/out.txt": "artifact\n",
            ".publishing-notes.md": "notes\n",
            ".publish-sdk/leak.txt": "sibling target workdir\n",
            "a.py": "x\n",
        },
        {"remote": remote, "include": ["."]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    tree = published_tree(remote)
    assert ".published-artifacts/out.txt" in tree
    assert ".publishing-notes.md" in tree
    assert not any(p.startswith(".publish-sdk") for p in tree)
    assert not any(p.startswith(".publish/") for p in tree)


# --- T3: .env rules (M1) ------------------------------------------------------

@pytest.mark.parametrize("name,published", [
    (".env", False),
    (".envrc", False),
    (".env.local", False),
    (".env.production", False),
    (".env.example", True),
    (".env.sample", True),
    (".env.template", True),
])
def test_env_rules(source_repo, public_remote, run, published_tree, name, published):
    remote = public_remote()
    repo = source_repo(
        {name: "X=1\n", "a.py": "x\n"},
        {"remote": remote, "include": ["."]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert (name in published_tree(remote)) is published


def test_user_exclude_beats_env_allowlist(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {".env.example": "X=1\n", ".env.sample": "Y=1\n", "a.py": "x\n"},
        {"remote": remote, "include": ["."], "exclude": [".env.example"]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    tree = published_tree(remote)
    assert ".env.example" not in tree
    assert ".env.sample" in tree


# --- T4: transform rule validation (L4) ---------------------------------------

@pytest.mark.parametrize("rule,phrase", [
    ({"find": 5, "replace": "x"}, "'find' must be a non-empty string"),
    ({"find": "a", "replace": 5}, "'replace' must be a string"),
    ({"find": "", "replace": "x"}, "'find' must be a non-empty string"),
    ({"find": "a"}, "needs both 'find' and 'replace'"),
    ({"replace": "a"}, "needs both 'find' and 'replace'"),
    ({"strip_between": ["a"]}, "two non-empty strings"),
    ({"strip_between": ["", "b"]}, "two non-empty strings"),
    ({"strip_between": "ab"}, "two non-empty strings"),
    ({"find": "a", "replace": "b", "strip_between": ["x", "y"]}, "use one per rule"),
    ({"other": "x"}, "must have 'find'+'replace' or 'strip_between'"),
])
def test_transform_validation(source_repo, public_remote, run, rule, phrase):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["a.py"], "transforms": {"a.py": [rule]}},
    )
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 1
    assert "transforms['a.py'][0]" in r.stderr
    assert phrase in r.stderr


def test_valid_transforms_load(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "private-name and <!-- A -->gone<!-- B --> rest\n"},
        {
            "remote": remote,
            "include": ["a.py"],
            "transforms": {"a.py": [
                {"find": "private-name", "replace": "public-name"},
                {"strip_between": ["<!-- A -->", "<!-- B -->"]},
            ]},
        },
    )
    r = run(["--dry-run"], cwd=repo)
    assert r.code == 0


# --- T5: exception exit-code contract ------------------------------------------

def test_exception_exit_codes():
    assert pubrepo.ConfigError("x").exit_code == 1
    assert pubrepo.PublishError("x").exit_code == 2
    assert pubrepo.LockContention("x").exit_code == 3
    assert pubrepo.ScrubFailure([], "x").exit_code == 4
    assert pubrepo.Diverged([], "x").exit_code == 5
    # GitError makes no claim about WHICH failure kind it is: direct child of
    # PubrepoError (base default code 1 = pre-T5 behavior), NOT a PublishError.
    # T8/T10 re-raise with context where §8 needs other codes.
    assert issubclass(pubrepo.GitError, pubrepo.PubrepoError)
    assert not issubclass(pubrepo.GitError, pubrepo.PublishError)
    assert pubrepo.GitError(["status"], "boom").exit_code == 1


def test_scrub_blocks_publish_exit_4(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "token sekrit here\n"},
        {"remote": remote, "include": ["a.py"], "scrub": {"forbidden": ["sekrit"]}},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 4
    assert "SCRUB FAILED" in r.stderr
    assert published_tree(remote) == {}  # nothing reached the world
