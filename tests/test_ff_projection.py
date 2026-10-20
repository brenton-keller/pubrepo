"""The projection engine — Π live, Π(σ), project_blob (design §2,
§3.1, §4.2 item 2)."""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

import pubrepo
from conftest import git_run


def _blob_id(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _setup(source_repo, public_remote, run, files, **config_extra):
    config = {"remote": public_remote(), "include": ["."]}
    config.update(config_extra)
    repo = source_repo(files, config)
    assert run(["init"], cwd=repo).code == 0
    return repo, repo / ".publish", pubrepo.load_publish_config(repo / ".publish.toml")


def _has_object(repo: Path, sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", sha], cwd=repo,
                          capture_output=True).returncode == 0


def _worktree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(root).parts}


def _commit_all(repo: Path, message: str = "commit") -> None:
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-qm", message, cwd=repo)


# --- Π live: hashing ------------------------------------------------

def test_eol_crlf_source_gives_lf_blob(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        ".gitattributes": "*.txt text eol=crlf\n", "a.txt": "one\r\ntwo\r\n"})
    proj = pubrepo.build_projection(repo, pub, config)
    assert proj.entries["a.txt"].blob == _blob_id(b"one\ntwo\n")
    assert not proj.entries["a.txt"].nonderivable


def test_eol_attribute_only_in_source_gives_lf_blob_in_publish_odb(
        source_repo, public_remote, run):
    """.gitattributes is not in include, yet the source's attributes
    drive conversion, and the object lands only in .publish's ODB."""
    repo, pub, config = _setup(source_repo, public_remote, run,
                               {"a.txt": "x\n", ".gitattributes": "* text eol=crlf\n"},
                               include=["notes"])
    (repo / "notes").mkdir()
    (repo / "notes" / "new.txt").write_bytes(b"fresh\r\nline\r\n")  # never committed
    proj = pubrepo.build_projection(repo, pub, config)
    expected = _blob_id(b"fresh\nline\n")
    assert proj.entries["notes/new.txt"].blob == expected
    assert _has_object(pub, expected)
    assert not _has_object(repo, expected)


def test_custom_clean_filter_gives_raw_bytes_and_nonderivable(source_repo, public_remote, run):
    """AC96: a path with a filter attribute publishes its raw worktree bytes."""
    repo, pub, config = _setup(source_repo, public_remote, run, {
        ".gitattributes": "*.dat filter=upper\n", "x.dat": "lower case\n"})
    git_run("config", "filter.upper.clean", "tr a-z A-Z", cwd=repo)
    proj = pubrepo.build_projection(repo, pub, config)
    assert proj.entries["x.dat"].blob == _blob_id(b"lower case\n")
    assert proj.entries["x.dat"].nonderivable


def test_lfs_shaped_attribute_gives_raw_bytes(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        ".gitattributes": "*.bin filter=lfs diff=lfs merge=lfs -text\n",
        "model.bin": "real model bytes\n"})
    git_run("config", "filter.lfs.clean", "printf 'version https://git-lfs.github.com/spec/v1\\n'",
            cwd=repo)
    proj = pubrepo.build_projection(repo, pub, config)
    assert proj.entries["model.bin"].blob == _blob_id(b"real model bytes\n")
    assert proj.entries["model.bin"].nonderivable


def test_transforms_applied_in_memory(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        "conf.py": "name = 'private-name'\n"},
        transforms={"conf.py": [{"find": "private-name", "replace": "public-name"}]})
    before = _worktree_bytes(repo)
    proj = pubrepo.build_projection(repo, pub, config)
    entry = proj.entries["conf.py"]
    assert entry.transformed
    assert entry.blob == _blob_id(b"name = 'public-name'\n")
    assert _has_object(pub, entry.blob)
    assert _worktree_bytes(repo) == before


def test_binary_transform_target_is_skipped_with_warning(source_repo, public_remote, run, capsys):
    repo, pub, config = _setup(source_repo, public_remote, run, {"a.txt": "x\n"},
                               transforms={"data.bin": [{"find": "a", "replace": "b"}]})
    (repo / "data.bin").write_bytes(b"a\x00a")
    proj = pubrepo.build_projection(repo, pub, config)
    assert proj.entries["data.bin"].blob == _blob_id(b"a\x00a")
    assert not proj.entries["data.bin"].transformed
    assert "skipping binary file for transform: data.bin" in capsys.readouterr().err


def test_keep_comes_from_publish_dir(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run,
                               {"a.txt": "x\n", "LICENSE": "source copy\n"},
                               keep=["LICENSE"])
    (pub / "LICENSE").write_text("owner copy in .publish\n")
    proj = pubrepo.build_projection(repo, pub, config)
    assert proj.entries["LICENSE"].keep
    assert proj.entries["LICENSE"].blob == _blob_id(b"owner copy in .publish\n")


def test_blob_equal_to_committed_source_content_lands_in_publish_odb(
        source_repo, public_remote, run):
    """Regression: under the projection environment git skips writing
    an object the source alternate already has. build_projection must still
    leave every entry's blob in .publish's own ODB, readable with no
    alternates, or commit-tree / push in .publish fail."""
    repo, pub, config = _setup(source_repo, public_remote, run, {"a.txt": "committed\n"})
    blob = _blob_id(b"committed\n")
    assert _has_object(repo, blob) and not _has_object(pub, blob)
    proj = pubrepo.build_projection(repo, pub, config)
    assert proj.entries["a.txt"].blob == blob
    assert _has_object(pub, blob)
    assert not (pub / ".git" / "objects" / "info" / "alternates").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads anything")
def test_unreadable_keep_file_fails_closed_exit4(source_repo, public_remote, run):
    """Regression: an unreadable keep file can never be
    verified, so the projection refuses with exit 4 instead of crashing."""
    repo, pub, config = _setup(source_repo, public_remote, run, {"a.txt": "x\n"},
                               keep=["LICENSE"])
    (pub / "LICENSE").write_text("terms\n")
    (pub / "LICENSE").chmod(0o000)
    try:
        with pytest.raises(pubrepo.ScrubFailure) as exc:
            pubrepo.build_projection(repo, pub, config)
    finally:
        (pub / "LICENSE").chmod(0o644)
    assert exc.value.exit_code == 4
    assert "LICENSE" in str(exc.value) and "could not verify" in str(exc.value)


def test_missing_keep_file_is_absent(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {"a.txt": "x\n"},
                               keep=["LICENSE"])
    assert "LICENSE" not in pubrepo.build_projection(repo, pub, config).entries


def test_symlinks_are_absent(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run,
                               {"a.txt": "x\n", "dir/real.txt": "r\n"})
    os.symlink("a.txt", repo / "link.txt")
    os.symlink("dir", repo / "linkdir")
    entries = pubrepo.build_projection(repo, pub, config).entries
    assert "a.txt" in entries and "dir/real.txt" in entries
    assert not any(p.startswith(("link.txt", "linkdir")) for p in entries)


def test_exec_bit_gives_mode_100755(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {"run.sh": "echo hi\n"})
    (repo / "run.sh").chmod(0o755)
    assert pubrepo.build_projection(repo, pub, config).entries["run.sh"].mode == "100755"


def test_selection_matches_copy_includes(source_repo, public_remote, run, tmp_path):
    """Π selects exactly the files today's copy path publishes."""
    repo, pub, config = _setup(source_repo, public_remote, run, {
        "src/app.py": "a\n", "src/__pycache__/x.pyc": "c\n", "src/.env": "S=1\n",
        "src/.env.example": "S=\n", "docs/d.md": "d\n", "docs/internal/i.md": "i\n",
        "README.md": "r\n", "skip.log": "l\n"},
        include=["src/", "./docs", "README.md", "skip.log"], exclude=["internal/", "*.log"])
    copy_dir = tmp_path / "copy"
    copy_dir.mkdir()
    copied = pubrepo.copy_includes(repo, copy_dir, config["include"], config["exclude"])
    assert "src/.env.example" in copied and "src/.env" not in copied
    assert sorted(pubrepo.build_projection(repo, pub, config).entries) == copied


def test_no_worktree_writes(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {"a.txt": "x\n"},
                               keep=["LICENSE"])
    (pub / "LICENSE").write_text("kept\n")
    source_before, publish_before = _worktree_bytes(repo), _worktree_bytes(pub)
    index_before = (pub / ".git" / "index").read_bytes() if (pub / ".git" / "index").exists() else b""
    pubrepo.build_projection(repo, pub, config)
    assert _worktree_bytes(repo) == source_before
    assert _worktree_bytes(pub) == publish_before
    after = (pub / ".git" / "index").read_bytes() if (pub / ".git" / "index").exists() else b""
    assert after == index_before


# --- dirty and nonderivable paths -------------------------------------------------------

def test_untracked_file_in_include_dir_is_nonderivable(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {"src/a.py": "a\n"},
                               include=["src/"])
    (repo / "src" / "new.py").write_text("untracked\n")
    live = pubrepo.build_projection(repo, pub, config)
    sigma = git_run("rev-parse", "HEAD", cwd=repo).strip()
    at_sigma = pubrepo.projection_at(repo, pub, sigma, ".publish.toml")
    assert "src/new.py" in live.dirty_paths
    assert pubrepo.nonderivable_paths(live, at_sigma) == ["src/new.py"]


def test_dirty_paths_cover_modified_staged_and_ignored(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        "a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n",
        ".gitignore": ".publish/\ngen.txt\n"})
    (repo / "a.txt").write_text("edited\n")
    (repo / "b.txt").write_text("staged\n")
    git_run("add", "b.txt", cwd=repo)
    (repo / "gen.txt").write_text("ignored but included\n")
    assert pubrepo.build_projection(repo, pub, config).dirty_paths == ["a.txt", "b.txt", "gen.txt"]


def test_filter_paths_are_always_nonderivable(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        ".gitattributes": "*.dat filter=upper\n", "x.dat": "LOWER\n"})
    git_run("config", "filter.upper.clean", "tr a-z A-Z", cwd=repo)
    live = pubrepo.build_projection(repo, pub, config)
    sigma = git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert "x.dat" in pubrepo.nonderivable_paths(
        live, pubrepo.projection_at(repo, pub, sigma, ".publish.toml"))


# --- Π(σ) -----------------------------------------------------------------------------

def test_projection_at_equals_live_on_a_clean_tree(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        "a.txt": "a\n", "src/b.py": "b\n", "src/__pycache__/c.pyc": "c\n",
        "conf.py": "private-name\n", "LICENSE": "mit\n"},
        transforms={"conf.py": [{"find": "private-name", "replace": "public-name"}]},
        keep=["LICENSE"])
    sigma = git_run("rev-parse", "HEAD", cwd=repo).strip()
    live = pubrepo.build_projection(repo, pub, config)
    at_sigma = pubrepo.projection_at(repo, pub, sigma, ".publish.toml")
    non_keep = {p: e.blob for p, e in live.entries.items() if not e.keep}
    assert {p: e.blob for p, e in at_sigma.entries.items()} == non_keep
    assert "LICENSE" not in at_sigma.entries  # keep is verified separately
    assert pubrepo.nonderivable_paths(live, at_sigma) == []


def test_transformed_crlf_file_live_equals_sigma(source_repo, public_remote, run):
    """Hashing in source then transforming makes Π == Π(σ)."""
    repo, pub, config = _setup(source_repo, public_remote, run, {
        ".gitattributes": "*.txt text eol=crlf\n", "doc.txt": "private-name\r\nend\r\n"},
        transforms={"doc.txt": [{"find": "private-name", "replace": "public-name"}]})
    sigma = git_run("rev-parse", "HEAD", cwd=repo).strip()
    live = pubrepo.build_projection(repo, pub, config).entries["doc.txt"]
    at_sigma = pubrepo.projection_at(repo, pub, sigma, ".publish.toml").entries["doc.txt"]
    assert live.blob == at_sigma.blob == _blob_id(b"public-name\nend\n")


def test_projection_at_none_when_config_untracked(source_repo, public_remote, run):
    repo, pub, _config = _setup(source_repo, public_remote, run, {"a.txt": "a\n"})
    git_run("rm", "-q", "--cached", ".publish.toml", cwd=repo)
    git_run("commit", "-qm", "untrack config", cwd=repo)
    sigma = git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert pubrepo.projection_at(repo, pub, sigma, ".publish.toml") is None


def test_projection_at_none_when_config_does_not_parse(source_repo, public_remote, run):
    repo, pub, _config = _setup(source_repo, public_remote, run, {"a.txt": "a\n"})
    (repo / ".publish.toml").write_text("[publish\n")
    _commit_all(repo, "broken config")
    sigma = git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert pubrepo.projection_at(repo, pub, sigma, ".publish.toml") is None


def test_projection_at_uses_config_at_sigma_and_skips_non_files(source_repo, public_remote, run):
    repo, pub, _config = _setup(source_repo, public_remote, run,
                                {"a.txt": "a\n", "b.txt": "b\n"}, include=["a.txt"])
    old = git_run("rev-parse", "HEAD", cwd=repo).strip()
    (repo / ".publish.toml").write_text((repo / ".publish.toml").read_text()
                                        .replace('include = ["a.txt"]', 'include = ["."]'))
    os.symlink("a.txt", repo / "link.txt")
    _commit_all(repo, "widen")
    new = git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert sorted(pubrepo.projection_at(repo, pub, old, ".publish.toml").entries) == ["a.txt"]
    assert "link.txt" not in pubrepo.projection_at(repo, pub, new, ".publish.toml").entries
    assert "b.txt" in pubrepo.projection_at(repo, pub, new, ".publish.toml").entries


# --- managed_now and the root-include predicate fix ------------------------------------

@pytest.mark.parametrize("include,path,expected", [
    (["."], "a.py", True),
    (["."], "docs/x.md", True),
    (["."], "src/__pycache__/x.pyc", False),
    (["."], ".env", False),
    (["./src"], "src/a.py", True),
    (["src/"], "src/a.py", True),
    (["src"], "srcx/a.py", False),
])
def test_managed_now_predicate(include, path, expected):
    config = {"include": include, "exclude": [], "keep": []}
    assert pubrepo.managed_now(path, config) is expected


def test_managed_now_counts_keep_paths():
    assert pubrepo.managed_now("LICENSE", {"include": ["src/"], "exclude": [], "keep": ["LICENSE"]})


# --- project_blob -----------------------------------------------------

def _loose_objects(repo: Path) -> set[str]:
    objects = repo / ".git" / "objects"
    return {str(p.relative_to(objects)) for p in objects.glob("??/*")}


def test_project_blob_reads_publish_odb_and_writes_nothing(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        ".gitattributes": "*.txt text eol=crlf\n", "a.txt": "x\r\n"})
    theirs = pubrepo._hash_bytes(pub, None, b"from public\n", write=True)  # only in .publish
    assert not _has_object(repo, theirs)
    pub_before, src_before = _loose_objects(pub), _loose_objects(repo)
    entry = pubrepo.project_blob(repo, pub, "a.txt", theirs, config)
    assert entry.blob == theirs  # smudge to CRLF, clean back to the same LF blob
    assert _loose_objects(pub) == pub_before and _loose_objects(repo) == src_before


def test_project_blob_applies_transforms(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {"conf.py": "x\n"},
                               transforms={"conf.py": [{"find": "private-name",
                                                        "replace": "public-name"}]})
    theirs = pubrepo._hash_bytes(pub, None, b"private-name here\n", write=True)
    entry = pubrepo.project_blob(repo, pub, "conf.py", theirs, config)
    assert entry.transformed and entry.blob == _blob_id(b"public-name here\n")


def test_project_blob_none_when_not_projected(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {"src/a.py": "a\n"},
                               include=["src/"])
    theirs = pubrepo._hash_bytes(pub, None, b"n\n", write=True)
    assert pubrepo.project_blob(repo, pub, "notes.txt", theirs, config) is None
    assert pubrepo.project_blob(repo, pub, ".publish.toml", theirs, config) is None


def test_project_blob_raises_on_filter_attribute(source_repo, public_remote, run):
    repo, pub, config = _setup(source_repo, public_remote, run, {
        ".gitattributes": "*.dat filter=upper\n", "x.dat": "X\n"})
    theirs = pubrepo._hash_bytes(pub, None, b"y\n", write=True)
    with pytest.raises(AssertionError, match="filter attribute"):
        pubrepo.project_blob(repo, pub, "x.dat", theirs, config)
