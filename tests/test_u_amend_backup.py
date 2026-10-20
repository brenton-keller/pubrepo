"""--amend backs up
only genuinely local `.publish` commits. The public tip it replaces, and its
ancestors, are public, so they are neither named as "never published" nor
saved to refs/pubrepo/local/.
"""
import pytest

from conftest import git_run

# AC62: --amend is an explicit rewrite; the routine policy does not apply.
pytestmark = pytest.mark.rewrite

NEVER = "never published"


def _published(source_repo, public_remote, run, files, title):
    url = public_remote()
    repo = source_repo(files, {"remote": url, "include": sorted(files)})
    assert run(["init"], cwd=repo).code == 0
    r = run(["publish", "--yes", "-m", title], cwd=repo)
    assert r.code == 0, r.stderr
    return url, repo, repo / ".publish"


def _change(repo, rel, text):
    (repo / rel).write_text(text)
    git_run("commit", "-qam", f"edit {rel}", cwd=repo)


def _backups(pd):
    return git_run("for-each-ref", "--format=%(objectname)", "refs/pubrepo/local/",
                   cwd=str(pd)).split()


def test_t4_1_plain_amend_backs_up_nothing(source_repo, public_remote, run):
    """A normal --amend names no commit as never published and
    creates no refs/pubrepo/local/* ref."""
    _url, repo, pd = _published(source_repo, public_remote, run, {"a.py": "v1\n"}, "v1 title")
    _change(repo, "a.py", "v2\n")
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert NEVER not in r.stderr
    assert _backups(pd) == []


def test_t4_2_two_amends_in_a_row(source_repo, public_remote, run):
    """A second --amend over the first one's result backs up nothing
    either."""
    _url, repo, pd = _published(source_repo, public_remote, run, {"a.py": "v1\n"}, "v1 title")
    for text in ("v2\n", "v3\n"):
        _change(repo, "a.py", text)
        r = run(["--amend", "--yes"], cwd=repo)
        assert r.code == 0, r.stderr
        assert NEVER not in r.stderr
    assert _backups(pd) == []
    assert int(git_run("rev-list", "--count", "HEAD", cwd=str(pd))) == 1


def test_t4_3_amend_files_line_counts_against_the_replaced_tip(source_repo, public_remote,
                                                               run):
    """Unchanged: the amended message's Files: line still
    counts the change against the replaced tip, not against its parent."""
    _url, repo, pd = _published(source_repo, public_remote, run,
                                {"a.py": "a1\n", "b.py": "b1\n"}, "v1 title")
    _change(repo, "a.py", "a2\n")
    assert run(["publish", "--yes", "-m", "v2 title"], cwd=repo).code == 0
    _change(repo, "b.py", "b2\n")
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    body = git_run("log", "-1", "--format=%B", "HEAD", cwd=str(pd))
    assert body.startswith("v2 title\n\n")
    assert "Files: 1 (0 added, 1 modified, 0 deleted)\n" in body


def test_t4_4_local_commit_on_the_tip_is_still_backed_up(source_repo, public_remote, run):
    """A genuinely local commit on top of the public tip is backed up
    (the backup is the local head) and named; the replaced tip is not."""
    _url, repo, pd = _published(source_repo, public_remote, run, {"a.py": "v1\n"},
                                "replaced tip title")
    git_run("commit", "--allow-empty", "-m", "local only work\n\nSource: fake123\n",
            cwd=str(pd))
    local_head = git_run("rev-parse", "HEAD", cwd=str(pd)).strip()
    _change(repo, "a.py", "v2\n")
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert _backups(pd) == [local_head]
    warning = r.stderr[r.stderr.index("WARNING: "):]
    assert "1 local commit(s)" in warning and NEVER in warning
    assert "local only work" in warning
    assert "replaced tip title" not in warning
