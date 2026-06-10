"""T11: fail-closed scrub + dry-run/publish scrub parity (M7)."""
import os

import pytest

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")


@not_root
def test_unreadable_keep_file_fails_publish(source_repo, public_remote, run):
    """A file the scrubber cannot read is a scrub FAILURE (exit 4), not a
    silent pass — the whole brand is fail-closed."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["a.py"], "keep": ["LICENSE"],
         "scrub": {"forbidden": ["sekrit"]}},
    )
    run(["init"], cwd=repo)
    assert run([], cwd=repo).code == 0

    keep_file = repo / ".publish" / "LICENSE"
    keep_file.write_text("whatever\n")
    keep_file.chmod(0o000)
    try:
        r = run([], cwd=repo)
        assert r.code == 4
        assert "LICENSE" in r.stderr
        assert "could not verify" in r.stderr
    finally:
        keep_file.chmod(0o644)


@not_root
def test_unreadable_source_file_listed_in_dry_run(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "locked.txt": "contents\n"},
        {"remote": remote, "include": ["a.py", "locked.txt"],
         "scrub": {"forbidden": ["sekrit"]}},
    )
    locked = repo / "locked.txt"
    locked.chmod(0o000)
    try:
        r = run(["--dry-run"], cwd=repo)
        assert "SCRUB WOULD FAIL" in r.stdout
        assert "locked.txt" in r.stdout
        assert "could not verify" in r.stdout
        assert "cannot read 'locked.txt' for hashing" in r.stderr
    finally:
        locked.chmod(0o644)


@not_root
def test_unreadable_source_file_fails_publish_cleanly(source_repo, public_remote, run):
    """The copy stage fails closed with a clean exit 2, not a traceback."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n", "locked.txt": "contents\n"},
        {"remote": remote, "include": ["a.py", "locked.txt"]},
    )
    run(["init"], cwd=repo)
    locked = repo / "locked.txt"
    locked.chmod(0o000)
    try:
        r = run([], cwd=repo)
        assert r.code == 2
        assert "cannot copy 'locked.txt'" in r.stderr
    finally:
        locked.chmod(0o644)


def test_keep_file_parity_dry_run_vs_publish(source_repo, public_remote, run):
    """M7's headline case: a keep file with a forbidden string must be caught
    identically by --dry-run and publish — dry-run can never say 'passed'
    where publish would fail."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote, "include": ["a.py"], "keep": ["LICENSE"],
         "scrub": {"forbidden": ["sekrit"]}},
    )
    run(["init"], cwd=repo)
    assert run([], cwd=repo).code == 0
    (repo / ".publish" / "LICENSE").write_text("contains sekrit\n")

    dry = run(["--dry-run"], cwd=repo)
    assert "SCRUB WOULD FAIL" in dry.stdout
    assert "LICENSE" in dry.stdout

    pub = run([], cwd=repo)
    assert pub.code == 4
    assert "LICENSE" in pub.stderr


def test_scrub_clean_pass_message(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "clean\n"},
        {"remote": remote, "include": ["a.py"], "scrub": {"forbidden": ["sekrit", "hush"]}},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "Scrubbing (1 files, 2 patterns)... ok" in r.stderr  # T21 progress line


def test_binary_files_skip_scrub_with_notice(source_repo, public_remote, run, published_tree):
    """Binary skip is the documented, principled policy (§7) — unchanged."""
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py", "blob.bin"],
                                         "scrub": {"forbidden": ["sekrit"]}})
    (repo / "blob.bin").write_bytes(b"sekrit\x00binary")  # forbidden string + null byte
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0  # binary content is not scrubbed, by policy
    assert "skipped binary file: blob.bin" in r.stderr
    assert "blob.bin" in published_tree(remote)
