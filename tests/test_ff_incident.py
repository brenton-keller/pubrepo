"""The 2026-10-14 incident, replayed (design AC106):
`git show HEAD:pubrepo.py | python3 -c "import sys; exec(sys.stdin.read())"`
run at a source root. Every remote is inside the sandbox, or refused by it.
"""
import subprocess
import sys

import pytest

import pubrepo
from conftest import SCRIPT_PATH, config_toml, git_run
from ff_fixtures import push_argv

HTTPS = "https://github.com/example-invalid/pubrepo.git"
GUARD = "test sandbox: refusing"


def _incident(repo, log, patch: str = ""):
    """The exact incident pipeline at `repo`, stderr captured (never
    suppressed). `patch` is test-side source inserted before the source's
    `if __name__ == "__main__":` line. Returns (CompletedProcess, git argvs)."""
    source = git_run("show", "HEAD:pubrepo.py", cwd=repo)
    if patch:
        marker = 'if __name__ == "__main__":'
        assert source.count(marker) == 1
        source = source.replace(marker, patch + "\n" + marker)
    r = subprocess.run([sys.executable, "-c", "import sys; exec(sys.stdin.read())"],
                       input=source, cwd=repo, capture_output=True, text=True,
                       env={**__import__("os").environ, "PUBREPO_AC62_LOG": str(log)})
    records = log.read_bytes().split(b"\036") if log.exists() else []
    calls = [rec.decode(errors="replace").split("\0")[:-1] for rec in records if rec]
    return r, calls


def _refs(*repos):
    return [git_run("for-each-ref", cwd=r) for r in repos]


@pytest.fixture
def shaped_like_the_repo(source_repo, public_remote, run):
    """pubrepo.py committed at a source root, a .publish.toml whose remote is
    a local bare repo inside the base, one publish done, changes pending."""
    url = public_remote()
    repo = source_repo({"pubrepo.py": SCRIPT_PATH.read_text(), "README.md": "r\n"},
                       {"remote": url, "include": ["README.md"]})
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    (repo / "README.md").write_text("pending\n")
    git_run("commit", "-qam", "pending", cwd=repo)
    return type("Repo", (), dict(repo=repo, url=url))


def test_exact_command_runs_status(shaped_like_the_repo, tmp_path):
    """incident::exact_command_runs_status (AC106, first half): status and
    the hint; no push; every ref unchanged."""
    s = shaped_like_the_repo
    before = _refs(s.repo, s.repo / ".publish") + [git_run("ls-remote", s.url)]
    r, calls = _incident(s.repo, tmp_path / "git.log")
    assert r.returncode == 0, r.stderr
    assert "To publish: `" in r.stderr and "publish`" in r.stderr
    assert "README.md" in r.stdout                      # the pending change, as status shows it
    assert not [argv for argv in calls if push_argv(argv)]
    assert _refs(s.repo, s.repo / ".publish") + [git_run("ls-remote", s.url)] == before


_CONFIRM_OFF = '''default_command = lambda argv: ["publish", *argv]
confirmation_mode = lambda *, yes, tty: "yes"'''


@pytest.mark.parametrize("patch, expect", [
    (_CONFIRM_OFF, GUARD),                                                   # CLI-entry check
    (_CONFIRM_OFF + "\ncheck_test_sandbox = lambda *a, **k: None", GUARD),   # fetch_tip's check
    (_CONFIRM_OFF + "\ncheck_test_sandbox = lambda *a, **k: None"
                    "\ncheck_test_sandbox_url = lambda url: None",
     "transport 'https' not allowed"),                                       # git itself
], ids=["cli_entry", "network_wrapper", "git_allow_protocol"])
def test_confirm_disabled_sandbox_refuses(source_repo, public_remote, run, tmp_path,
                                          patch, expect):
    """incident::confirm_disabled_sandbox_refuses (AC106, second half):
    §17.3 patched out test-side, the config remote an https URL. The
    CLI-entry check refuses (2); with it patched out, fetch_tip's network
    check refuses (2); with that patched out too, git refuses the https
    transport (GIT_ALLOW_PROTOCOL=file, set by the suite), so the fetch
    fails (2). No push is attempted and no ref moves in any case.

    pubrepo fetches by remote name (`origin`), so the PATH shim sees no URL
    argument: the last layer is git's protocol allowlist, not the shim."""
    local = public_remote()
    repo = source_repo({"pubrepo.py": SCRIPT_PATH.read_text(), "README.md": "r\n"},
                       {"remote": local, "include": ["README.md"]})
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    (repo / ".publish.toml").write_text(config_toml({"remote": HTTPS, "include": ["README.md"]}))
    (repo / "README.md").write_text("pending\n")
    git_run("commit", "-qam", "pending", cwd=repo)
    before = _refs(repo, repo / ".publish") + [git_run("ls-remote", local)]
    # The clone's origin becomes the https URL only for the incident run: the
    # suite's spawn audit refuses any test-side git child in a repo whose
    # config names a network URL (§17.4 b), so it is written as text and
    # restored before the refs are compared.
    cfg = repo / ".publish" / ".git" / "config"
    text = cfg.read_text()
    assert text.count(f"url = {local}") == 1
    cfg.write_text(text.replace(f"url = {local}", f"url = {HTTPS}"))
    try:
        r, calls = _incident(repo, tmp_path / "git.log", patch)
    finally:
        cfg.write_text(text)
    assert r.returncode == 2, r.stdout + r.stderr
    assert expect in r.stderr, r.stderr
    assert not [argv for argv in calls if push_argv(argv)]
    assert not [argv for argv in calls if any(HTTPS in a for a in argv)]
    assert _refs(repo, repo / ".publish") + [git_run("ls-remote", local)] == before


def test_no_product_switch_disables_confirmation():
    """Pubrepo ships no switch that turns §17.3 off; the patch above
    is test-side only."""
    source = SCRIPT_PATH.read_text()
    assert "PUBREPO_SKIP_CONFIRM" not in source and "PUBREPO_NO_CONFIRM" not in source
    assert pubrepo.confirmation_mode(yes=True, tty=False) == "yes"
    with pytest.raises(pubrepo.ConfigError):
        pubrepo.confirmation_mode(yes=False, tty=False)
