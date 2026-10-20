"""Accident prevention (design §17.1, §17.3): bare argv is status, a
covered command confirms with one [y/N]
on a terminal or needs --yes off one, and --expect-tip binds the tip. Every
remote is a local bare repo inside the sandbox.
"""
import contextlib
import io
import os
import pty
import re
import select
import sys
import time

import pytest

import pubrepo
from conftest import SCRIPT_PATH, git_run
from ff_fixtures import push_argv

REFUSED = "publishing needs confirmation: run on a terminal, or pass --yes"


@pytest.fixture
def pub_repo(source_repo, public_remote, run):
    """An initialized, published source repo with one pending change."""
    url = public_remote()
    repo = source_repo({"a.py": "v1\n"}, {"remote": url, "include": ["a.py"]})
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-qam", "v2", cwd=repo)
    return type("P", (), dict(url=url, repo=repo, publish=repo / ".publish"))


def _tip(url):
    out = git_run("ls-remote", url, "refs/heads/main").split()
    return out[0] if out else None


def _pushes(calls, public=False):
    """The push argvs among `calls`; with `public`, only the public branch
    pushes (the source repo's records and audit-tag pushes are not)."""
    return [argv for argv in calls if push_argv(argv)
            and (not public or any(a.endswith(":refs/heads/main") for a in argv))]


def _state(repo, url):
    """What a covered run could change: the public tip, the source records,
    the publish clone's refs and its log."""
    log = repo / ".publish" / ".git" / pubrepo.PUBLISH_LOG_NAME
    return (_tip(url), git_run("for-each-ref", cwd=repo),
            git_run("for-each-ref", cwd=repo / ".publish"),
            log.read_bytes() if log.exists() else None)


# --- the push path (design §17.1) ---------------------------

def test_pubrepo_pushes_only_to_origin_and_never_skips_hooks():
    """pubrepo.py has no --no-verify and no hooksPath override,
    and push_porcelain's push names origin."""
    source = SCRIPT_PATH.read_text()
    assert "--no-verify" not in source and "hooksPath" not in source
    assert '_git_c(*argv, "origin", *refspecs, cwd=publish_dir)' in source


# --- AC99: bare argv is status; flags-only previews --------------------------------------

def test_bare_argv_is_status(pub_repo, run, git_policy):
    """auth::bare_argv_is_status (AC99): with pending changes, `[]` prints
    status plus the hint and pushes nothing; refs, records and the log are
    unchanged."""
    before = _state(pub_repo.repo, pub_repo.url)
    status = run(["status"], cwd=pub_repo.repo)
    bare = run([], cwd=pub_repo.repo)
    assert bare.code == status.code
    assert bare.stdout == status.stdout
    assert bare.stderr == "To publish: `pubrepo publish`\n" + status.stderr
    assert not _pushes(git_policy.runs[-1])
    assert _state(pub_repo.repo, pub_repo.url) == before


def test_flags_only_dry_run_previews(pub_repo, run, git_policy):
    """auth::flags_only_dry_run_previews (AC99): `--dry-run` keeps its
    publish-family meaning and is a preview: no prompt, no push."""
    before = _state(pub_repo.repo, pub_repo.url)
    r = run(["--dry-run"], cwd=pub_repo.repo)
    assert r.code == 0, r.stderr
    assert "a.py" in r.stdout + r.stderr and "[y/N]" not in r.stderr
    assert not _pushes(git_policy.runs[-1])
    assert _state(pub_repo.repo, pub_repo.url) == before


# --- AC100: off a terminal a covered command needs --yes --------------------------------

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


@pytest.mark.parametrize("argv", [["publish"], ["-m", "x"], ["--release", "1.2.0"],
                                  ["--amend"]])
def test_offtty_refusals_zero_push(source_repo, public_remote, run, git_policy,
                                   provider_fakes, argv):
    """auth::offtty_refusals_zero_push (AC100): off a terminal, each covered
    form without --yes exits 1 with the §17.3 message, before any fetch,
    lock or write, with zero pushes and zero provider calls."""
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.1.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"],
        "release": {"enabled": True, "version_file": "version.py",
                    "version_pattern": r'^VERSION = "(?P<version>[0-9A-Za-z.+-]+)"$',
                    "tag_format": "v{version}",
                    "github": {"enabled": True, "repository": "owner/repo"}}})
    assert run(["init"], cwd=repo).code == 0
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    (repo / "a.txt").write_text("b\n")
    git_run("commit", "-qam", "b", cwd=repo)
    before = _state(repo, url)
    r = run(argv, cwd=repo)
    calls = git_policy.runs[-1]
    assert r.code == 1
    assert REFUSED in r.stderr
    assert not _pushes(calls) and not [a for a in calls if a[:1] == ["fetch"]]
    assert not provider_fakes.exists()
    assert _state(repo, url) == before


@pytest.mark.parametrize("argv", [["publish"], ["-m", "x"],
                                  pytest.param(["--amend"], marks=pytest.mark.rewrite)])
def test_offtty_yes_proceeds(pub_repo, run, argv):
    """AC100: with --yes the covered command proceeds."""
    before = _tip(pub_repo.url)
    r = run([*argv, "--yes"], cwd=pub_repo.repo)
    assert r.code == 0, r.stderr
    assert _tip(pub_repo.url) != before


# --- AC101: one [y/N] on a terminal ---------------------------------------------------

def _cli_on_a_pty(repo, argv, answer: str, log, prompt: bytes = b"[y/N] "):
    """Run pubrepo's CLI as a child on a pseudo-terminal, cwd `repo`, and
    answer the first `prompt` (default the [y/N]) with `answer`. Returns
    (exit code, output, the child's git argvs)."""
    env = {**os.environ, "PUBREPO_AC62_LOG": str(log)}
    pid, fd = pty.fork()
    if pid == 0:                                       # child: the CLI, on the pty
        try:
            os.chdir(repo)
            os.execve(sys.executable, [sys.executable, str(SCRIPT_PATH), *argv], env)
        finally:
            os._exit(127)
    seen = b""
    deadline = time.monotonic() + 60
    while prompt not in seen and time.monotonic() < deadline:
        if select.select([fd], [], [], 1)[0]:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            seen += chunk
    if prompt in seen:
        os.write(fd, answer.encode() + b"\n")
    with contextlib.suppress(OSError):
        while select.select([fd], [], [], 30)[0]:
            chunk = os.read(fd, 4096)
            if not chunk:
                break
            seen += chunk
    _, status = os.waitpid(pid, 0)
    os.close(fd)
    records = log.read_bytes().split(b"\036") if log.exists() else []
    calls = [r.decode(errors="replace").split("\0")[:-1] for r in records if r]
    return os.waitstatus_to_exitcode(status), seen.decode(errors="replace"), calls


@pytest.mark.pty
@pytest.mark.rewrite
@pytest.mark.parametrize("argv", [["publish"], ["--amend"]])
@pytest.mark.parametrize("answer, code", [("n", 1), ("", 1), ("y", 0)])
def test_pty_one_prompt_per_run(pub_repo, tmp_path, argv, answer, code):
    """auth::pty_one_prompt_per_run (AC101): exactly one [y/N] per run; `n`
    or just Enter → 1 with nothing written; `y` → one public push."""
    before = _tip(pub_repo.url)
    exit_code, text, calls = _cli_on_a_pty(pub_repo.repo, argv, answer,
                                           tmp_path / "pty-ac62.log")
    pushes = _pushes(calls, public=True)
    assert text.count("[y/N]") == 1, text
    assert exit_code == code, text
    if code == 0:
        assert len(pushes) == 1 and _tip(pub_repo.url) != before
    else:
        assert pushes == [] and _tip(pub_repo.url) == before


# --- AC102: --expect-tip ------------------------------------------------------------------

@pytest.mark.parametrize("terminal", [False, True])
@pytest.mark.parametrize("matches", [False, True])
def test_expect_tip_mismatch_exit5(pub_repo, run, git_policy, monkeypatch, terminal, matches):
    """auth::expect_tip_mismatch_exit5 (AC102): a stale
    --expect-tip → 5 with no push and one message in every mode; on a
    terminal it is known before the prompt, so none is shown. The current
    tip → 0."""
    tip = _tip(pub_repo.url)
    expected = tip if matches else git_run("rev-parse", "HEAD", cwd=pub_repo.repo).strip()
    argv = ["publish", "--expect-tip", expected]
    if terminal:
        monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    else:
        argv.append("--yes")
    r = run(argv, cwd=pub_repo.repo)
    if matches:
        assert r.code == 0, r.stderr
        assert _tip(pub_repo.url) != tip
        return
    assert r.code == 5, r.stderr
    assert (f"the public tip moved: expected {expected[:12]}, found {tip[:12]}; "
            f"nothing was written") in r.stderr
    assert "[y/N]" not in r.stderr
    assert not _pushes(git_policy.runs[-1]) and _tip(pub_repo.url) == tip


@pytest.mark.parametrize("argv", [["publish"], ["--amend"]])
def test_expect_tip_rechecked_after_the_prompt(pub_repo, run, git_policy, monkeypatch, argv):
    """AC102: the post-lock check stays. --expect-tip matches
    when the prompt is shown; a collaborator moves the tip while the human
    answers → 5 with the same message, nothing pushed."""
    tip = _tip(pub_repo.url)
    other = pub_repo.repo.parent / "collab"
    git_run("clone", "-q", pub_repo.url, str(other))
    git_run("commit", "-q", "--allow-empty", "-m", "collab", cwd=other)

    def answer_while_the_tip_moves(summary):
        with monkeypatch.context() as m:       # the collaborator's push is not pubrepo's
            m.delenv("PUBREPO_AC62_LOG", raising=False)
            git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=other)
        return True

    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr(pubrepo, "confirm_yes_no", answer_while_the_tip_moves)
    r = run([*argv, "--expect-tip", tip], cwd=pub_repo.repo)
    moved = _tip(pub_repo.url)
    assert moved != tip
    assert r.code == 5, r.stderr
    assert (f"the public tip moved: expected {tip[:12]}, found {moved[:12]}; "
            f"nothing was written") in r.stderr
    assert not _pushes(git_policy.runs[-1], public=True)


@pytest.mark.pty
@pytest.mark.parametrize("argv", [["publish"], ["--amend"]])
def test_pty_stale_expect_tip_shows_no_prompt(pub_repo, tmp_path, argv):
    """AC102 on a real terminal: a stale --expect-tip is refused (5) before
    the prompt is shown; nothing is pushed."""
    tip = _tip(pub_repo.url)
    stale = git_run("rev-parse", "HEAD", cwd=pub_repo.repo).strip()
    exit_code, text, calls = _cli_on_a_pty(pub_repo.repo, [*argv, "--expect-tip", stale],
                                           "y", tmp_path / "pty-ac62.log")
    assert exit_code == 5, text
    assert "[y/N]" not in text
    assert f"the public tip moved: expected {stale[:12]}, found {tip[:12]}" in text
    assert _pushes(calls, public=True) == [] and _tip(pub_repo.url) == tip


def test_hints_name_publish_not_bare_pubrepo(source_repo, public_remote, run):
    """Carried from the 1.0.x hotfixes to 1.1.0: bare
    `pubrepo` is status, so the first-publish status hint and the publish
    help name `pubrepo publish`."""
    url = public_remote()
    repo = source_repo({"a.py": "v1\n"}, {"remote": url, "include": ["a.py"]})
    assert run(["init"], cwd=repo).code == 0
    status = run(["status"], cwd=repo)
    assert "Run `pubrepo publish` to create the initial publish." in status.stdout
    # (The integrate hint left with the 1.0 recipe: integrate is `pull --plan` now, AC65.)
    assert not re.search(r"[Rr]un `?pubrepo`? (to|first)", status.stdout + status.stderr)
    help_text = " ".join(pubrepo.build_parser().format_help().split())   # argparse wraps
    assert "the default command" not in help_text
    assert "Full publish cycle (confirms on a terminal; --yes otherwise)" in help_text


# --- AC103: provider steps only inside a confirmed run (re-scoped) -----

def test_release_resume_offtty_without_yes_refused(source_repo, public_remote, run,
                                                   git_policy, monkeypatch):
    """auth::release_resume_offtty_without_yes_refused (AC103, first half):
    a release fails at a provider step; the resume off a terminal without
    --yes → 1 before any provider step or push; with --yes it resumes the
    failed step. The --verify-tag half is in the release tests."""
    from test_release_transaction import _setup
    repo, _remote = _setup(source_repo, public_remote, run,
                           release_extra={"github": {"enabled": True, "repository": "owner/repo"}},
                           files_extra={"CHANGELOG.md": "# 1.0.0\n"})   # C1: notes exported
    events, failed = [], {"done": False}

    class FakeGitHub:
        name = "github"

        def check_live(self, _ctx):
            return []

        def build(self, _ctx):
            events.append("build")

        def reconcile(self, _ctx):
            events.append("reconcile")

        def execute(self, _ctx, stage):
            events.append(stage)
            if stage == "draft" and not failed["done"]:
                failed["done"] = True
                raise pubrepo.ReleaseError("injected provider failure")

    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [FakeGitHub()])
    assert run(["--release", "1.0.0", "--yes"], repo).code == 7
    assert pubrepo.load_journal(repo / ".publish", "1.0.0").data["in_flight"] == "github:draft"
    mark = len(events)
    resume = run(["--release", "1.0.0"], repo)
    assert resume.code == 1
    assert REFUSED in resume.stderr
    assert events[mark:] == [] and not _pushes(git_policy.runs[-1])
    assert pubrepo.load_journal(repo / ".publish", "1.0.0").data["in_flight"] == "github:draft"
    confirmed = run(["--release", "1.0.0", "--yes"], repo)
    assert confirmed.code == 0, confirmed.stderr
    assert "draft" in events[mark:]
