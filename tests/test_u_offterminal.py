"""Off a terminal, a covered publish without --yes prints an
offline preview and the exact next command on stdout, then exits 1 with the
unchanged §17.3 error: releases are never offered a --yes command; the
preview takes the shared source lock without creating it (contention
→ Preview unavailable); and the --amend preview's reused-title edges.
Every run here is off a terminal: pytest's stdin is not a tty.
"""
import fcntl
import hashlib
import os
import shlex
import subprocess
import sys
import time

import pytest

import pubrepo
from conftest import SCRIPT_PATH, git_run
from test_divergence import _external_commit

HIT = "acme-internal"
REFUSED = "ERROR: publishing needs confirmation: run on a terminal, or pass --yes"
RUN = "Not published: no terminal to confirm. Review the above, then run: "
TERMINAL = "Not published: releases must be confirmed on a terminal. Run this command in a terminal: "
RESOLVE = "Not published: no terminal to confirm; resolve the above first."
RELEASE = {"enabled": True, "version_file": "version.py",
           "version_pattern": r'^VERSION = "(?P<version>[0-9A-Za-z.+-]+)"$',
           "tag_format": "v{version}", "github": {"enabled": True, "repository": "owner/repo"}}


def _repo(source_repo, public_remote, run, *, publish=True, release=False, forbidden=None,
          files=None):
    url = public_remote()
    files = dict(files or {"a.py": "v1\n"})
    config = {"remote": url, "include": sorted(files)}
    if release:
        files["version.py"] = 'VERSION = "1.0.0"\n'
        config["include"] = sorted(files)
        config["release"] = RELEASE
    if forbidden:
        config["scrub"] = {"forbidden": list(forbidden)}
    repo = source_repo(files, config)
    assert run(["init"], cwd=repo).code == 0
    if publish:
        argv = ["publish", "--yes", *(["--no-release"] if release else [])]
        r = run(argv, cwd=repo)
        assert r.code == 0, r.stderr
    return url, repo


def _change(repo, text="v2\n", rel="a.py"):
    (repo / rel).write_text(text)
    git_run("add", rel, cwd=repo)
    git_run("commit", "-qm", f"edit {rel}", cwd=repo)


def _tip(url):
    out = git_run("ls-remote", url, "refs/heads/main").split()
    return out[0] if out else None


def _last(r):
    return r.stdout.rstrip("\n").split("\n")[-1]


def _command(line, prefix=RUN):
    assert line.startswith(prefix), line
    argv = shlex.split(line[len(prefix):])
    assert argv[0] == "pubrepo"
    return argv[1:]


def _no_network(git_policy):
    calls = git_policy.runs[-1]
    assert not [a for a in calls if a[:1] in (["fetch"], ["ls-remote"], ["push"])], calls


def _no_yes_with_release(r):
    """No stdout line that names --release or --bump offers --yes."""
    for line in r.stdout.split("\n"):
        if "--release" in line or "--bump" in line:
            assert "--yes" not in line, line


def _files(root, skip=()):
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and not any(rel == s or rel.startswith(s + "/") for s in skip):
            out[rel] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def _snapshot(repo):
    """Source .git/ and both worktrees byte-equal;
    .publish/.git refs, index, log and journals byte-equal (object files
    excluded: the preview's unreferenced blobs and trees are allowed)."""
    pd = repo / ".publish"
    return (_files(repo, skip=(".git", ".publish")), _files(repo / ".git"),
            _files(pd, skip=(".git",)), _files(pd / ".git", skip=("objects",)),
            git_run("for-each-ref", cwd=repo), git_run("for-each-ref", cwd=pd),
            git_run("status", "--porcelain", cwd=repo), git_run("status", "--porcelain", cwd=pd))


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


# --- the routine command ----------------------------------------------------------------

def test_t2_1_preview_and_next_command(source_repo, public_remote, run, git_policy):
    """Exit 1; stdout = preview + the last line with --yes
    --expect-tip <40-hex>; the §17.3 error on stderr; no fetch, ls-remote
    or push; nothing outside the object store changes."""
    url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    before = _snapshot(repo)
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    assert r.stderr.strip() == REFUSED
    assert r.stdout.startswith("Preview, offline (remote not checked (last fetched ")
    assert "Commit message:" in r.stdout and "  a.py" in r.stdout
    assert _command(_last(r)) == ["publish", "--yes", "--expect-tip", _tip(url)]
    _no_network(git_policy)
    assert _snapshot(repo) == before


def test_t2_2_printed_command_publishes(source_repo, public_remote, run):
    """Running the printed command publishes with exit 0."""
    url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    before = _tip(url)
    r = run(_command(_last(run(["publish"], cwd=repo))), cwd=repo)
    assert r.code == 0, r.stderr
    assert _tip(url) != before


def test_t2_3_printed_command_refuses_a_moved_tip(source_repo, public_remote, run, tmp_path):
    """An outside push after the preview → the printed command
    exits 5 (the public tip moved)."""
    url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    argv = _command(_last(run(["publish"], cwd=repo)))
    _external_commit(url, tmp_path)
    moved = _tip(url)
    r = run(argv, cwd=repo)
    assert r.code == 5
    assert "the public tip moved" in r.stderr
    assert _tip(url) == moved


def test_t2_4_first_publish_has_no_expect_tip(source_repo, public_remote, run):
    """Never published, never fetched → no --expect-tip; the
    command works."""
    url, repo = _repo(source_repo, public_remote, run, publish=False)
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    argv = _command(_last(r))
    assert argv == ["publish", "--yes"]
    assert run(argv, cwd=repo).code == 0
    assert _tip(url)


def test_t2_5_quiet_prints_only_the_command(source_repo, public_remote, run):
    """--quiet leaves exactly the last line on stdout."""
    url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    r = run(["publish", "--quiet"], cwd=repo)
    assert r.code == 1
    assert r.stdout.count("\n") == 1
    assert _command(r.stdout.rstrip("\n")) == ["publish", "--quiet", "--yes", "--expect-tip",
                                               _tip(url)]


# --- releases -----------------------------------------------------------------

def test_t2_6_release_on_without_a_release_flag(source_repo, public_remote, run, git_policy,
                                                provider_fakes):
    """Release management on, no release flag → the routine
    command with --no-release, and the release line is terminal-only."""
    url, repo = _repo(source_repo, public_remote, run, release=True)
    _change(repo)
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    lines = r.stdout.rstrip("\n").split("\n")
    assert _command(lines[-2]) == ["publish", "--yes", "--expect-tip", _tip(url),
                                   "--no-release"]
    assert lines[-1] == ("The release part is not shown. To release instead, run in a "
                         "terminal: pubrepo publish --release <version>")
    _no_yes_with_release(r)
    _no_network(git_policy)
    assert not provider_fakes.exists()


@pytest.mark.parametrize("flag", [["--release", "1.1.0"], ["--bump", "minor"]])
def test_t2_6_release_flag_gets_a_terminal_only_command(source_repo, public_remote, run,
                                                        git_policy, provider_fakes, flag):
    """--release/--bump → the argv bound to the previewed
    tip, with no --yes, to run on a terminal; the release part is named
    as not shown."""
    url, repo = _repo(source_repo, public_remote, run, release=True)
    _change(repo)
    r = run(["publish", *flag], cwd=repo)
    assert r.code == 1
    assert r.stderr.strip() == REFUSED
    lines = r.stdout.rstrip("\n").split("\n")
    assert _command(lines[-2], TERMINAL) == ["publish", *flag, "--expect-tip", _tip(url)]
    assert lines[-1] == "The release part (build, tag, uploads) is not shown."
    _no_yes_with_release(r)
    _no_network(git_policy)
    assert not provider_fakes.exists()


def test_t2_6b_release_off_a_terminal(source_repo, public_remote, run, git_policy,
                                      provider_fakes):
    """`--release 1.1.0` off a terminal → exit 1, the
    terminal line, no provider call, no journal, nothing changed."""
    url, repo = _repo(source_repo, public_remote, run, release=True)
    (repo / "version.py").write_text('VERSION = "1.1.0"\n')
    git_run("commit", "-qam", "1.1.0", cwd=repo)
    before = _snapshot(repo)
    r = run(["--release", "1.1.0"], cwd=repo)
    assert r.code == 1
    assert TERMINAL in r.stdout and "--yes" not in r.stdout
    _no_yes_with_release(r)
    assert not provider_fakes.exists()
    assert pubrepo.load_journal(repo / ".publish", "1.1.0") is None
    _no_network(git_policy)
    assert _snapshot(repo) == before


# --- nothing to offer -------------------------------------------------------------------

def test_t2_7_gate_failure_offers_no_command(source_repo, public_remote, run):
    """A blocked preview prints SCRUB WOULD FAIL and no
    command."""
    _url, repo = _repo(source_repo, public_remote, run, forbidden=[HIT])
    _change(repo, f"x = '{HIT}'\n")
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    assert "SCRUB WOULD FAIL" in r.stdout
    assert _last(r) == RESOLVE
    assert "run:" not in r.stdout


def test_t2_8_nothing_to_publish(source_repo, public_remote, run):
    """A shallow-clone-free preview exits 1 with the routine last line."""
    _url, repo = _repo(source_repo, public_remote, run)
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    assert _last(r) == "Not published: nothing to publish as of the last fetch."


def test_t2_9_shallow_publish_clone_is_not_previewed(source_repo, public_remote, run,
                                                     git_policy):
    """A shallow .publish/ → Preview unavailable, zero fetch
    (check_shallow would fetch --unshallow)."""
    url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    (repo / ".publish" / ".git" / "shallow").write_text(_tip(url) + "\n")
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    assert r.stdout.startswith("Preview unavailable: .publish/ is a shallow clone")
    assert _last(r) == "Not published: no terminal to confirm."
    _no_network(git_policy)


# --- --amend and the reused-title edges -----------------------------------------

@pytest.mark.rewrite
def test_t2_10_amend_clean_title_command_amends(source_repo, public_remote, run):
    """The routine preview with the reused title, the Action
    line, and a printed `--amend --yes --expect-tip` command that amends."""
    url, repo = _repo(source_repo, public_remote, run)
    _change(repo, "v3\n")
    tip = _tip(url)
    r = run(["publish", "--amend"], cwd=repo)
    assert r.code == 1
    assert (f"Action: --amend replaces public commit {tip[:12]}; its checks (tagged, merge, "
            f"ours) run when you publish.") in r.stdout
    argv = _command(_last(r))
    assert argv == ["publish", "--amend", "--yes", "--expect-tip", tip]
    done = run(argv, cwd=repo)
    assert done.code == 0, done.stderr
    parents = [git_run("rev-list", "--parents", "-n", "1", sha, cwd=repo / ".publish").split()[1:]
               for sha in (_tip(url), tip)]
    assert _tip(url) != tip and parents[0] == parents[1]


def _tainted(source_repo, public_remote, run, title):
    """A public tip titled `title`, then `acme-internal` forbidden and the
    content changed."""
    url, repo = _repo(source_repo, public_remote, run, publish=False)
    assert run(["publish", "--yes", "-m", title], cwd=repo).code == 0
    toml = (repo / ".publish.toml").read_text()
    (repo / ".publish.toml").write_text(toml + f'\n[publish.scrub]\nforbidden = ["{HIT}"]\n')
    git_run("commit", "-qam", "forbid", cwd=repo)
    _change(repo)
    return url, repo


def test_t2_10_amend_tainted_title_offers_no_command(source_repo, public_remote, run):
    """A tainted reused title → SCRUB WOULD FAIL and no command."""
    url, repo = _tainted(source_repo, public_remote, run, f"v1 {HIT}")
    r = run(["publish", "--amend"], cwd=repo)
    assert r.code == 1
    assert (f"SCRUB WOULD FAIL: the title --amend would reuse from {_tip(url)[:12]} contains "
            f"1 forbidden string(s) on line(s) 1.") in r.stdout
    assert "Commit message:" not in r.stdout
    assert _last(r) == RESOLVE
    assert HIT not in r.stdout + r.stderr


def test_f103r1_n6_clean_m_over_a_tainted_title(source_repo, public_remote, run):
    """With -m, the reused title is not scanned, so the
    amend preview offers its command."""
    url, repo = _tainted(source_repo, public_remote, run, f"v1 {HIT}")
    r = run(["publish", "--amend", "-m", "v2 clean"], cwd=repo)
    assert r.code == 1
    assert "SCRUB WOULD FAIL" not in r.stdout
    assert _command(_last(r)) == ["publish", "--amend", "-m", "v2 clean", "--yes",
                                  "--expect-tip", _tip(url)]
    assert HIT not in r.stdout + r.stderr


@pytest.mark.parametrize("argv", [["publish"], ["--dry-run"]])
def test_f103r1_n7_no_amend_does_not_scan_the_tip_title(source_repo, public_remote, run, argv):
    """Without --amend the tip's title is not reused, so
    it is not scanned; the rendered title is the default."""
    _url, repo = _tainted(source_repo, public_remote, run, f"v1 {HIT}")
    r = run(argv, cwd=repo)
    assert r.code == (1 if argv == ["publish"] else 0)
    assert "SCRUB WOULD FAIL" not in r.stdout
    rendered = r.stdout.split("Commit message:\n", 1)[1].split("\n")[0]
    assert rendered.startswith("  publish: ")
    assert HIT not in r.stdout + r.stderr


@pytest.mark.rewrite
def test_f103r1_n10_multiline_reused_title_hit_in_the_body(source_repo, public_remote, run):
    """The whole reused title block is scanned; a hit
    on line 4 is refused in the preview and by `--amend --yes`."""
    url, repo = _tainted(source_repo, public_remote, run, f"v1\n\nbody\nmoved to {HIT}")
    tip = _tip(url)
    r = run(["publish", "--amend"], cwd=repo)
    assert r.code == 1
    assert (f"SCRUB WOULD FAIL: the title --amend would reuse from {tip[:12]} contains 1 "
            f"forbidden string(s) on line(s) 4.") in r.stdout
    assert _last(r) == RESOLVE
    yes = run(["--amend", "--yes"], cwd=repo)
    assert yes.code == 4
    assert "contains 1 forbidden string(s) on line(s) 4." in yes.stderr
    assert _tip(url) == tip
    assert HIT not in r.stdout + r.stderr + yes.stdout + yes.stderr


# --- ordering, argv round trip, locks ---------------------------------------------------

def test_t2_11_stdout_precedes_stderr(source_repo, public_remote, run):
    """With both streams on one pipe, the stdout block comes
    before the ERROR line."""
    _url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    p = subprocess.run([sys.executable, str(SCRIPT_PATH), "publish"], cwd=repo,
                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True, timeout=120)
    assert p.returncode == 1
    out = p.stdout
    assert out.index("Preview, offline") < out.index(RUN) < out.index(REFUSED)
    assert out.rstrip("\n").endswith(REFUSED)


def test_t2_12_multiline_m_round_trips(source_repo, public_remote, run, tmp_path):
    """A multi-line -m survives shlex in the printed command,
    and the publish it runs carries that message."""
    url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    message = "Release v2\n\nIt's \"quoted\" & $dollar\n  indented"
    r = run(["publish", "-m", message], cwd=repo)
    assert r.code == 1
    # The quoted message spans lines, so the command is everything after
    # the last "run: " marker, not the last physical line.
    argv = _command(r.stdout[r.stdout.rindex(RUN):].rstrip("\n"))
    assert argv == ["publish", "-m", message, "--yes", "--expect-tip", _tip(url)]
    assert run(argv, cwd=repo).code == 0
    bare = url.removeprefix("file://")
    raw = git_run("cat-file", "commit", _tip(url), cwd=bare)
    assert raw.split("\n\n", 1)[1].startswith(message + "\n\nSource: ")


def test_r_ac100a_t2_12_lock_contention_is_preview_unavailable(source_repo, public_remote,
                                                               run):
    """The source lock held exclusively → Preview
    unavailable and exit 1 at once, without waiting."""
    _url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    lock = pubrepo.source_common_dir(repo) / pubrepo.SOURCE_LOCK_NAME
    with open(lock, "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.monotonic()
        r = run(["publish"], cwd=repo)
        elapsed = time.monotonic() - start
    assert r.code == 1
    assert r.stdout.startswith("Preview unavailable: ERROR: another pubrepo command is using "
                               "this source repository")
    assert _last(r) == "Not published: no terminal to confirm."
    assert r.stderr.strip() == REFUSED
    assert elapsed < 30


def test_r_ac100a_t2_13_no_lock_file_is_created(source_repo, public_remote, run):
    """With no lock file beforehand, the source's git
    common dir is byte-equal after the refusal (no O_CREAT)."""
    _url, repo = _repo(source_repo, public_remote, run)
    _change(repo)
    common = pubrepo.source_common_dir(repo)
    (common / pubrepo.SOURCE_LOCK_NAME).unlink(missing_ok=True)
    before = _files(common)
    r = run(["publish"], cwd=repo)
    assert r.code == 1
    assert _last(r).startswith(RUN)
    assert _files(common) == before
    assert not (common / pubrepo.SOURCE_LOCK_NAME).exists()


# --- every printed command runs ---------------------------------------------------------
# For each state that prints `then run: <cmd>`, the printed command publishes,
# and a printed "To release instead" command reaches the confirmation, never a
# flag conflict. The offline preview models --accept-source-divergence and
# --recreate-branch, so their command is offered instead of a loop.

NOTE = "The release part is not shown. To release instead, run in a terminal: "


def _bare(url):
    return url.removeprefix("file://")


def _diverged(repo, run):
    """Rewrite the source commit the public snapshot came from, then commit
    a change; returns the snapshot sha the dry run says to accept."""
    git_run("commit", "-q", "--amend", "-m", "init rewritten", cwd=repo)
    _change(repo, "v3\n")
    dry = run(["--dry-run"], cwd=repo)
    assert dry.code == 8, dry.stdout + dry.stderr
    flag = next(w for w in (dry.stdout + dry.stderr).split()
                if w.startswith("--accept-source-divergence="))
    return flag.split("=", 1)[1]


def _deleted_branch(repo, url, run):
    git_run("update-ref", "-d", "refs/heads/main", cwd=_bare(url))
    _change(repo, "v3\n")


STATES = ["change", "amend", "accept", "recreate", "recreate-refreshed", "recreate-exists"]
RECREATE = ("Action: --recreate-branch: previewed as a new branch from this source; if the "
            "branch still exists, publish fast-forwards instead.")


def _state(state, repo, url, run):
    """The publish argv for `state`, with the source set up for it."""
    if state == "change":
        _change(repo)
        return ["publish"]
    if state == "amend":
        _change(repo)
        return ["publish", "--amend"]
    if state == "accept":
        return ["publish", f"--accept-source-divergence={_diverged(repo, run)}"]
    if state == "recreate":                    # deleted; the last fetch predates it
        _deleted_branch(repo, url, run)
    elif state == "recreate-refreshed":        # deleted, and the last fetch saw it gone
        git_run("update-ref", "-d", "refs/heads/main", cwd=_bare(url))
        assert run(["--dry-run", "--remote"], cwd=repo).code == 5
    else:                                      # the branch still exists
        _change(repo)
    return ["publish", "--recreate-branch"]


@pytest.mark.rewrite
@pytest.mark.parametrize("release", [False, True], ids=["release-off", "release-on"])
@pytest.mark.parametrize("state", STATES)
def test_f1_run_printed_command_publishes(source_repo, public_remote, run, git_policy,
                                          state, release):
    """T-F1-run: the printed command exits 0 and moves the public tip."""
    url, repo = _repo(source_repo, public_remote, run, release=release)
    argv = _state(state, repo, url, run)
    before = _tip(url)
    r = run(argv, cwd=repo)
    assert r.code == 1 and r.stderr.strip() == REFUSED
    _no_network(git_policy)
    lines = r.stdout.rstrip("\n").split("\n")
    offered = [x for x in lines if x.startswith(RUN)]
    assert offered, r.stdout                   # a command is printed
    cmd = _command(offered[-1])
    assert cmd[:len(argv)] == argv and "--yes" in cmd
    assert ("--no-release" in cmd) == release
    if state.startswith("recreate"):
        assert "--expect-tip" not in cmd       # the last-fetched tip may be of a deleted branch
        assert RECREATE in lines
    done = run(cmd, cwd=repo)
    assert done.code == 0, done.stderr
    assert _tip(url) not in (None, before)
    parents = git_run("rev-list", "--parents", "-n", "1", _tip(url), cwd=_bare(url)).split()[1:]
    if state in ("recreate", "recreate-refreshed"):
        assert parents == []                   # the branch was created as a root commit
    elif state == "recreate-exists":
        assert parents == [before]             # fast-forward


@pytest.mark.parametrize("state", STATES)
def test_f1_note_only_where_a_release_can_follow(source_repo, public_remote, run, state):
    """T-F1-note: with release management on, a printed "To release
    instead" command reaches the confirmation refusal off a terminal, not
    a flag conflict; --amend, --accept-source-divergence and
    --recreate-branch print no such note."""
    url, repo = _repo(source_repo, public_remote, run, release=True)
    argv = _state(state, repo, url, run)
    r = run(argv, cwd=repo)
    assert r.code == 1
    notes = [x[len(NOTE):] for x in r.stdout.split("\n") if x.startswith(NOTE)]
    if state != "change":
        assert notes == [] and "--release" not in r.stdout
        return
    [note] = notes
    cmd = shlex.split(note.replace("<version>", "1.1.0"))
    assert cmd == ["pubrepo", "publish", "--release", "1.1.0"]
    released = run(cmd[1:], cwd=repo)
    assert released.code == 1
    assert released.stderr.strip() == REFUSED
    assert TERMINAL in released.stdout
    _no_yes_with_release(released)


def test_f1_b_amend_with_nothing_published(source_repo, public_remote, run):
    """T-F1-b: init, never publish, `publish --amend` off a terminal → the
    last line names the reason, and no command is offered."""
    url, repo = _repo(source_repo, public_remote, run, publish=False)
    r = run(["publish", "--amend"], cwd=repo)
    assert r.code == 1
    assert r.stderr.strip() == REFUSED
    assert _last(r) == "Not published: --amend: no previous publish to amend."
    assert "--yes" not in r.stdout
    assert _tip(url) is None
