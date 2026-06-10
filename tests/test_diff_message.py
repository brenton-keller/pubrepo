"""T17 (--diff preview) + T18 (commit message enrichment)."""
from conftest import git_run


def _setup(source_repo, public_remote, run, files=None, **config_extra):
    remote = public_remote()
    config = {"remote": remote, "include": ["a.py"]}
    config.update(config_extra)
    repo = source_repo(files or {"a.py": "line1\nline2\n"}, config)
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    return remote, repo


# --- T17: --diff -----------------------------------------------------------------

def test_modified_file_shows_hunks(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("line1\nCHANGED\n")
    r = run(["status", "--diff"], cwd=repo)
    assert r.code == 0
    assert "--- a/a.py" in r.stdout
    assert "+++ b/a.py" in r.stdout
    assert "-line2" in r.stdout
    assert "+CHANGED" in r.stdout
    assert "@@" in r.stdout


def test_transform_only_change_shows_transformed_diff(source_repo, public_remote, run):
    """The selling point: the diff previews POST-TRANSFORM content."""
    remote, repo = _setup(
        source_repo, public_remote, run,
        files={"a.py": "name = private-name\n"},
        transforms={"a.py": [{"find": "private-name", "replace": "public-name"}]},
    )
    # Source unchanged; the transform RULE changes => post-transform bytes change.
    config = (repo / ".publish.toml").read_text()
    (repo / ".publish.toml").write_text(config.replace("public-name", "brand-new-name"))
    r = run(["status", "--diff"], cwd=repo)
    assert r.code == 0
    assert "+name = brand-new-name" in r.stdout
    assert "-name = public-name" in r.stdout


def test_publish_diff_implies_dry_run(source_repo, public_remote, run, published_tree):
    remote, repo = _setup(source_repo, public_remote, run)
    before = published_tree(remote)
    (repo / "a.py").write_text("line1\nCHANGED\n")

    r = run(["publish", "--diff"], cwd=repo)
    assert r.code == 0
    assert "+CHANGED" in r.stdout
    assert "Published" not in r.stdout
    assert published_tree(remote) == before  # nothing pushed


def test_status_and_publish_diff_identical(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("line1\nCHANGED\n")
    a = run(["status", "--diff"], cwd=repo)
    b = run(["publish", "--diff"], cwd=repo)
    assert a.stdout == b.stdout


def test_added_deleted_binary_large(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run,
                          files={"a.py": "x\n", "gone.txt": "bye\n",
                                 "blob.bin": "ok\n", "big.txt": "small\n"},
                          include=["."])
    (repo / "new.txt").write_text("hello\n")          # added
    (repo / "gone.txt").unlink()                       # deleted
    (repo / "blob.bin").write_bytes(b"\x00binary")     # becomes binary
    (repo / "big.txt").write_text("y" * (1024 * 1024 + 10))  # large

    r = run(["status", "--diff"], cwd=repo)
    assert r.code == 0
    assert "/dev/null" in r.stdout and "+++ b/new.txt" in r.stdout and "+hello" in r.stdout
    assert "Deleted: gone.txt" in r.stdout
    assert "Binary files differ: blob.bin" in r.stdout
    assert "big.txt: diff suppressed (large file)" in r.stdout


def test_diff_no_changes(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    r = run(["status", "--diff"], cwd=repo)
    assert r.code == 0
    assert "No changes to publish" in r.stdout


# --- T18: commit message enrichment ------------------------------------------------

def _last_message(repo):
    return git_run("log", "-1", "--format=%B", cwd=repo / ".publish")


def test_commit_message_structure(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo(
        {"a.py": "private-name\n", "b.py": "y\n"},
        {"remote": remote, "include": ["a.py", "b.py"],
         "scrub": {"forbidden": ["sekrit", "hush"]},
         "transforms": {"a.py": [{"find": "private-name", "replace": "public-name"}]}},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0

    body = _last_message(repo)
    src_hash = git_run("rev-parse", "--short", "HEAD", cwd=repo).strip()
    assert body.startswith("publish: ")
    assert f"Source: {src_hash}" in body
    assert "Branch: main" in body
    assert "Files: 2 (2 added, 0 modified, 0 deleted)" in body
    assert "Transforms: 1 file(s)" in body
    assert "Scrub: 2 pattern(s) passed" in body


def test_modified_count_on_second_publish(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("line1\nCHANGED\n")
    git_run("commit", "-aqm", "change", cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "Files: 1 (0 added, 1 modified, 0 deleted)" in _last_message(repo)


def test_custom_message_with_source_line_does_not_confuse_status(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-aqm", "v2", cwd=repo)
    real_hash = git_run("rev-parse", "--short", "HEAD", cwd=repo).strip()

    r = run(["publish", "-m", "tricky title\nSource: bogus123"], cwd=repo)
    assert r.code == 0

    r = run(["status"], cwd=repo)
    assert f"Source:       {real_hash}" in r.stdout
    assert "bogus123" not in r.stdout


def test_dry_run_previews_enriched_message(source_repo, public_remote, run):
    remote, repo = _setup(source_repo, public_remote, run)
    (repo / "a.py").write_text("line1\nCHANGED\n")
    r = run(["--dry-run"], cwd=repo)
    assert "Branch: main" in r.stdout
    assert "Files: 1 (0 added, 1 modified, 0 deleted)" in r.stdout
