"""The commit-message scrub, and previews that see the real -m, with the
1.0.3 port note that the 1.1 line stores messages verbatim (`commit-tree`), whatever
`commit.cleanup` says. Synthetic strings only.
"""
import io
import json
import os

import pytest

import pubrepo
from conftest import git_run
from test_divergence import _external_commit

HIT = "acme-internal"
HIT2 = "beta-host"
REFUSAL_M = "SCRUB FAILED — the commit message (-m) contains"
NOT_PUBLISHED_M = "Not published: the commit message (-m) contains"


def _repo(source_repo, public_remote, run, forbidden=(HIT,), init=True, **extra):
    url = public_remote()
    config = {"remote": url, "include": ["a.py"], **extra}
    if forbidden is not None:
        config["scrub"] = {"forbidden": list(forbidden)}
    repo = source_repo({"a.py": "v1\n"}, config)
    if init:
        assert run(["init"], cwd=repo).code == 0
    return url, repo


def _published(source_repo, public_remote, run, **kw):
    url, repo = _repo(source_repo, public_remote, run, **kw)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    _change(repo, "v2\n")
    return url, repo


def _change(repo, text):
    (repo / "a.py").write_text(text)
    git_run("commit", "-qam", f"edit {text.strip()}", cwd=repo)


def _tip(url):
    out = git_run("ls-remote", url, "refs/heads/main").split()
    return out[0] if out else None


def _public_message(url, tmp_path):
    """The raw message of the public tip, as stored (no %B reformatting)."""
    tip = _tip(url)
    bare = url.removeprefix("file://")
    raw = git_run("cat-file", "commit", tip, cwd=bare)
    return raw.split("\n\n", 1)[1]


def _state(repo, url):
    log = repo / ".publish" / ".git" / pubrepo.PUBLISH_LOG_NAME
    return (_tip(url), git_run("for-each-ref", cwd=repo),
            git_run("for-each-ref", cwd=repo / ".publish"),
            log.read_bytes() if log.exists() else None)


def _clean(r, *patterns):
    for p in (HIT, HIT2, *patterns):
        assert p.lower() not in (r.stdout + r.stderr).lower()


def _set_forbidden(repo, forbidden):
    text = (repo / ".publish.toml").read_text()
    head = text.split("\n[publish.scrub]")[0].rstrip("\n")
    (repo / ".publish.toml").write_text(
        head + "\n\n[publish.scrub]\nforbidden = " + json.dumps(list(forbidden)) + "\n")
    git_run("commit", "-qam", "forbid", cwd=repo)


# --- Step 2: the helper ----------------------------------------------------------------

def test_message_scrub_helper_post_conditions():
    cfg = {"scrub": {"forbidden": ["acme"]}}
    assert pubrepo.message_scrub("a\nx ACME y", cfg) == pubrepo.MessageScrub(1, (2,))
    assert pubrepo.message_scrub("a\nx ACME y", {"scrub": {"forbidden": ["", 5, "acme"]}}) \
        == pubrepo.MessageScrub(1, (2,))
    two = {"scrub": {"forbidden": ["acme", "beta"]}}
    assert pubrepo.message_scrub("acme\nb\nbeta", two) == pubrepo.MessageScrub(2, (1, 3))
    assert pubrepo.message_scrub(None, cfg).count == 0
    assert pubrepo.message_scrub("", cfg).count == 0
    assert pubrepo.message_scrub("acme", {}).count == 0
    assert pubrepo.message_scrub("acme", {"scrub": {"forbidden": []}}).count == 0


def test_message_scrub_counts_distinct_entries_and_every_line():
    """<N> is distinct entries (a repeated entry counts once); the lines are
    every occurrence's start line; an entry spanning lines reports its first."""
    cfg = {"scrub": {"forbidden": ["acme", "acme"]}}
    assert pubrepo.message_scrub("acme\nx\nACME", cfg) == pubrepo.MessageScrub(1, (1, 3))
    span = {"scrub": {"forbidden": ["alpha\nbeta"]}}
    assert pubrepo.message_scrub("t\nalpha\nbeta", span) == pubrepo.MessageScrub(1, (2,))


def test_message_scrub_refusal_texts_never_name_the_string():
    s = pubrepo.message_scrub("x acme", {"scrub": {"forbidden": ["acme"]}})
    m = pubrepo.message_scrub_refusal("the commit message (-m)", s, pubrepo.MESSAGE_REMEDY)
    assert str(m) == ("SCRUB FAILED — the commit message (-m) contains 1 forbidden string(s) "
                      "on line(s) 1.\nNothing was published or changed. "
                      "Edit the -m text and run again.")
    assert m.exit_code == 4
    assert m.results == [{"message": "the commit message (-m)", "count": 1, "lines": [1]}]
    t = pubrepo.message_scrub_refusal("the title --amend would reuse from 0123456789ab", s,
                                      pubrepo.REUSED_TITLE_REMEDY, after_fetch=True)
    assert str(t).endswith("\nNothing was rebuilt, committed or pushed. Pass -m with a new "
                           "title, or publish a new commit (omit --amend).")
    assert "acme" not in str(m) + str(t) + repr(m.results)


# --- Step 3: the call sites -------------------------------------------------------------

@pytest.mark.no_git_calls
def test_t1_1_m_hit_refused_before_any_git_call(source_repo, public_remote, run):
    """4, line 1, zero git calls, stdout empty under --yes."""
    _url, repo = _repo(source_repo, public_remote, run, init=False)
    r = run(["publish", "--yes", "-m", f"x {HIT}"], cwd=repo)
    assert r.code == 4
    assert "contains 1 forbidden string(s) on line(s) 1." in r.stderr
    assert REFUSAL_M in r.stderr and "Nothing was published or changed." in r.stderr
    assert r.stdout == ""
    _clean(r)


@pytest.mark.no_git_calls
@pytest.mark.parametrize("message, entries, expect", [
    (f"Release v2\n\nnotes\nmoved to {HIT}", (HIT,), "1 forbidden string(s) on line(s) 4."),
    ("x ACME-Internal y", (HIT,), "1 forbidden string(s) on line(s) 1."),
    (f"{HIT}\n\n{HIT2}", (HIT, HIT2), "2 forbidden string(s) on line(s) 1, 3."),
], ids=["t1_2_body_only", "t1_3_case_insensitive", "t1_4_two_entries"])
def test_t1_2_to_1_4_report_count_and_lines(source_repo, public_remote, run, message, entries,
                                            expect):
    _url, repo = _repo(source_repo, public_remote, run, forbidden=entries, init=False)
    r = run(["publish", "--yes", "-m", message], cwd=repo)
    assert r.code == 4
    assert expect in r.stderr
    _clean(r)


@pytest.mark.parametrize("forbidden", [None, [], [""]], ids=["no_table", "empty", "empty_entry"])
def test_t1_5_no_patterns_nothing_scanned(source_repo, public_remote, run, tmp_path, forbidden):
    """No usable entries → the -m publishes; the public title is -m."""
    url, repo = _repo(source_repo, public_remote, run, forbidden=forbidden)
    r = run(["publish", "--yes", "-m", f"x {HIT}"], cwd=repo)
    assert r.code == 0, r.stderr
    assert _public_message(url, tmp_path).split("\n")[0] == f"x {HIT}"


def _tainted_tip(source_repo, public_remote, run):
    """Published with `-m "v1 acme-internal"`, then the entry was forbidden
    and the content changed."""
    url, repo = _repo(source_repo, public_remote, run, forbidden=None)
    assert run(["publish", "--yes", "-m", f"v1 {HIT}"], cwd=repo).code == 0
    _set_forbidden(repo, [HIT])
    return url, repo


@pytest.mark.rewrite
@pytest.mark.parametrize("change", [True, False], ids=["t1_6_changed", "t1_7_no_change"])
def test_t1_6_t1_7_reused_title_refused(source_repo, public_remote, run, change):
    """--amend without -m over a tainted title → 4, nothing
    rebuilt, committed or pushed; with no content change still 4."""
    url, repo = _tainted_tip(source_repo, public_remote, run)
    if change:
        _change(repo, "v2\n")
    tip = _tip(url)
    before = _state(repo, url)
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 4
    assert (f"SCRUB FAILED — the title --amend would reuse from {tip[:12]} contains 1 "
            f"forbidden string(s) on line(s) 1.") in r.stderr
    assert "Nothing was rebuilt, committed or pushed." in r.stderr
    assert "Pass -m with a new title, or publish a new commit (omit --amend)." in r.stderr
    assert _state(repo, url) == before
    _clean(r)


@pytest.mark.rewrite
def test_t1_8_clean_m_over_tainted_title_amends(source_repo, public_remote, run, tmp_path):
    """-m replaces the reused title, so the amend lands."""
    url, repo = _tainted_tip(source_repo, public_remote, run)
    _change(repo, "v2\n")
    r = run(["--amend", "--yes", "-m", "v1.0.1"], cwd=repo)
    assert r.code == 0, r.stderr
    assert _public_message(url, tmp_path).startswith("v1.0.1\n\nSource: ")


@pytest.mark.rewrite
def test_t1_9_reused_title_refused_before_the_prompt(source_repo, public_remote, run,
                                                     monkeypatch):
    """Prompt mode refuses in compute, before `Publish? [y/N]`."""
    url, repo = _tainted_tip(source_repo, public_remote, run)
    _change(repo, "v2\n")
    before = _state(repo, url)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    r = run(["--amend"], cwd=repo)
    assert r.code == 4
    assert "[y/N]" not in r.stderr
    assert _state(repo, url) == before
    _clean(r)


@pytest.fixture
def provider_fakes(tmp_path, monkeypatch):
    """`gh` and `twine` first on PATH; each call is logged."""
    bin_dir = tmp_path / "provider-bin"
    bin_dir.mkdir()
    log = tmp_path / "provider-calls.log"
    for tool in ("gh", "twine"):
        fake = bin_dir / tool
        fake.write_text(f'#!/bin/sh\necho "{tool} $*" >> {log}\nexit 0\n')
        fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


RELEASE = {"enabled": True, "version_file": "version.py",
           "version_pattern": r'^VERSION = "(?P<version>[0-9A-Za-z.+-]+)"$',
           "tag_format": "v{version}", "github": {"enabled": True, "repository": "owner/repo"}}


def test_t1_10_release_m_hit_refused(source_repo, public_remote, run, git_policy,
                                     provider_fakes):
    """`--release --yes -m <hit>` → 4, no provider call, no journal.
    The only git call is release-config validation's check-ref-format."""
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"], "release": RELEASE,
        "scrub": {"forbidden": [HIT]}})
    r = run(["--release", "1.0.0", "--yes", "-m", f"1.0.0 {HIT}"], cwd=repo)
    assert r.code == 4
    assert REFUSAL_M in r.stderr
    assert not provider_fakes.exists()
    assert not (repo / ".publish").exists()
    assert {argv[0] for argv in git_policy.runs[-1]} == {"check-ref-format"}
    _clean(r)


@pytest.mark.no_git_calls
@pytest.mark.parametrize("quiet", [False, True], ids=["plain", "quiet"])
def test_t1_11_offterminal_m_hit_one_stdout_line(source_repo, public_remote, run, quiet):
    """Off a terminal without --yes, 4 with no preview and
    exactly one stdout line, under --quiet too; the error stays on stderr."""
    _url, repo = _repo(source_repo, public_remote, run, init=False)
    r = run(["publish", "-m", f"t\n\nx {HIT}", *(["--quiet"] if quiet else [])], cwd=repo)
    assert r.code == 4
    assert r.stdout == (f"{NOT_PUBLISHED_M} 1 forbidden string(s) on line(s) 3. "
                        f"Edit the -m text and run again.\n")
    assert REFUSAL_M in r.stderr
    _clean(r)


def test_t1_12_clean_multiline_m_stored_verbatim(source_repo, public_remote, run, tmp_path):
    """A clean multi-line -m with trailing spaces is the public
    message verbatim, then a blank line and the footer."""
    url, repo = _repo(source_repo, public_remote, run)
    message = "Release v2  \n\nbody line\t\n\n\nlast  "
    assert run(["publish", "--yes", "-m", message], cwd=repo).code == 0
    assert _public_message(url, tmp_path).startswith(message + "\n\nSource: ")


def test_commit_cleanup_strip_does_not_apply_on_the_1_1_line(source_repo, public_remote, run,
                                                             tmp_path):
    """1.0.3 port: 1.1 commits with commit-tree, which stores the
    message as given whatever commit.cleanup says, so the scanned text is
    the stored text."""
    url, repo = _repo(source_repo, public_remote, run, forbidden=None, init=False)
    _set_forbidden(repo, ["alpha\nbeta"])
    assert run(["init"], cwd=repo).code == 0
    git_run("config", "commit.cleanup", "strip", cwd=repo / ".publish")
    message = "T\n\nalpha\n# note\nbeta"
    r = run(["publish", "--yes", "-m", message], cwd=repo)
    assert r.code == 0, r.stderr
    stored = _public_message(url, tmp_path)
    assert stored.startswith(message + "\n\nSource: ")
    assert "alpha\nbeta" not in stored


# --- Step 4: previews -------------------------------------------------------------------

def _rendered_message(stdout):
    lines = stdout.split("\n")
    start = lines.index("Commit message:") + 1
    out = []
    for line in lines[start:]:
        if not line.startswith("  "):
            break
        out.append(line[2:])
    return out


def test_t3_1_dry_run_renders_the_message_publish_commits(source_repo, public_remote, run,
                                                          tmp_path):
    """The "Commit message:" lines equal the public message lines."""
    url, repo = _published(source_repo, public_remote, run)
    message = "Release v2\n\nbody"
    d = run(["--dry-run", "-m", message], cwd=repo)
    assert d.code == 0, d.stderr
    assert run(["publish", "--yes", "-m", message], cwd=repo).code == 0
    assert _rendered_message(d.stdout) == _public_message(url, tmp_path).rstrip().split("\n")


def test_t3_1_default_title_unchanged(source_repo, public_remote, run):
    url, repo = _published(source_repo, public_remote, run)
    d = run(["--dry-run"], cwd=repo)
    assert d.code == 0
    assert _rendered_message(d.stdout)[0].startswith("publish: ")


def test_t3_2_dry_run_m_hit(source_repo, public_remote, run):
    """4 with the line on stdout; on stderr with --quiet; JSON
    `scrub.passed` false, `commit_message` null, `scrub.message.lines`."""
    _url, repo = _published(source_repo, public_remote, run)
    message = f"t\n\nx {HIT}"
    text = run(["--dry-run", "-m", message], cwd=repo)
    assert text.code == 4
    assert ("SCRUB WOULD FAIL: the commit message (-m) contains 1 forbidden string(s) on "
            "line(s) 3.") in text.stdout
    assert "Publish will be blocked until these are resolved." in text.stdout
    assert "Commit message:" not in text.stdout
    _clean(text)
    quiet = run(["--dry-run", "--quiet", "-m", message], cwd=repo)
    assert quiet.code == 4 and quiet.stdout == ""
    assert "SCRUB WOULD FAIL: the commit message (-m) contains 1" in quiet.stderr
    _clean(quiet)
    js = run(["--dry-run", "--json", "-m", message], cwd=repo)
    assert js.code == 4
    data = json.loads(js.stdout)
    assert data["scrub"]["passed"] is False
    assert data["commit_message"] is None
    assert data["scrub"]["message"] == {"count": 1, "lines": [3]}
    _clean(js)


def test_t3_2_json_clean_has_the_commit_message(source_repo, public_remote, run):
    _url, repo = _published(source_repo, public_remote, run)
    data = json.loads(run(["--dry-run", "--json", "-m", "Release v2"], cwd=repo).stdout)
    assert data["scrub"]["passed"] is True
    assert data["scrub"]["message"] == {"count": 0, "lines": []}
    assert data["commit_message"].startswith("Release v2\n\nSource: ")


def test_t3_3_message_hit_overrides_a_predicted_state_exit(source_repo, public_remote, run,
                                                           tmp_path):
    """In a diverged state (predicted 5), a -m hit is 4."""
    url, repo = _published(source_repo, public_remote, run)
    _external_commit(url, tmp_path)
    assert run(["--dry-run", "--remote"], cwd=repo).code == 5
    r = run(["--dry-run", "-m", f"x {HIT}"], cwd=repo)
    assert r.code == 4
    _clean(r)


def test_t3_4_dry_run_m_hit_before_init(source_repo, public_remote, run):
    """Uninitialized --dry-run, text/quiet/JSON → 4."""
    _url, repo = _repo(source_repo, public_remote, run, init=False)
    text = run(["--dry-run", "-m", f"x {HIT}"], cwd=repo)
    assert text.code == 4
    assert "SCRUB WOULD FAIL: the commit message (-m) contains 1" in text.stdout
    quiet = run(["--dry-run", "--quiet", "-m", f"x {HIT}"], cwd=repo)
    assert quiet.code == 4 and "SCRUB WOULD FAIL" in quiet.stderr
    data = json.loads(run(["--dry-run", "--json", "-m", f"x {HIT}"], cwd=repo).stdout)
    assert data["scrub"]["passed"] is False and data["scrub"]["message"]["count"] == 1
    for r in (text, quiet):
        _clean(r)
    assert run(["--dry-run", "-m", "clean"], cwd=repo).code == 0


def test_t3_5_diff_m(source_repo, public_remote, run):
    """`--diff -m <hit>` → 4, the diff unchanged on stdout and the
    line on stderr; a clean -m → 0 with the same diff."""
    _url, repo = _published(source_repo, public_remote, run)
    plain = run(["--diff"], cwd=repo)
    assert plain.code == 0 and "+v2" in plain.stdout
    hit = run(["--diff", "-m", f"x {HIT}"], cwd=repo)
    assert hit.code == 4
    assert hit.stdout == plain.stdout
    assert ("SCRUB WOULD FAIL: the commit message (-m) contains 1 forbidden string(s) on "
            "line(s) 1.") in hit.stderr
    _clean(hit)
    clean = run(["--diff", "-m", "Release v2"], cwd=repo)
    assert clean.code == 0 and clean.stdout == plain.stdout
