"""The test sandbox (design §17.4 (a)-(e); AC109, AC110). The layers
are tested from the inside out:
the product check, the network wrapper, the spawn audit, the PATH git shim.
"""
import errno
import os
import pty
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

import pubrepo
from conftest import SANDBOX_VAR, _network_url, git_run

NETWORK_REMOTES = ["https://github.com/example-invalid/x.git",
                   "ssh://git@example.invalid/x.git", "git@github.com:example-invalid/x.git"]


def _base() -> Path:
    return Path(os.environ[SANDBOX_VAR]).resolve()


# --- (c) product check at CLI entry ------------------------------------------------------

@pytest.mark.no_git_calls      # refused at CLI entry: zero git calls (no fetch, ls-remote, push)
@pytest.mark.parametrize("key", ["remote", "records_remote"])
@pytest.mark.parametrize("url", NETWORK_REMOTES)
def test_product_check_refuses_network_remotes(source_repo, public_remote, run, key, url):
    """sandbox::product_check_refuses_network_remotes (AC109)."""
    if key == "records_remote":
        # AC75 re-target (§10 item 20, R1): records_remote names a git remote;
        # that remote's network URL is what the product check refuses
        config = {"remote": public_remote(), "include": ["a.py"], key: "rec"}
        repo = source_repo({"a.py": "x\n"}, config, origin=False)
        with open(repo / ".git" / "config", "a") as f:
            f.write(f'[remote "rec"]\n\turl = {url}\n')
    else:
        config = {"remote": public_remote(), "include": ["a.py"], key: url}
        repo = source_repo({"a.py": "x\n"}, config, origin=False)
    for args in (["init"], [], ["status"], ["--dry-run"]):
        r = run(args, cwd=repo)
        assert r.code == 2, args
        assert "test sandbox: refusing" in r.stderr, args


def test_sentinel_and_lock_values_not_refused(source_repo, public_remote, run):
    """sandbox::sentinel_and_lock_values_not_refused: the
    sentinel pushurl and the lock value are relative local paths."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    assert run(["init"], cwd=repo).code == 0
    config = pubrepo.load_publish_config(repo / ".publish.toml")
    for value in ("pubrepo-push-only-via-pubrepo",
                  "DISABLED-pubrepo-publish-locked-during-1.1.0-build"):
        git_run("config", "remote.origin.pushurl", value, cwd=repo / ".publish")
        pubrepo.check_test_sandbox(repo, config)       # does not raise


def test_insteadof_rewrite_to_a_network_url_is_refused(source_repo, public_remote, run):
    """A url.<base>.insteadOf whose target is not local."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    with open(repo / ".git" / "config", "a") as f:
        f.write('[url "/elsewhere/outside/"]\n\tinsteadOf = ' + str(_base()) + "/\n")
    with pytest.raises(pubrepo.PublishError, match="test sandbox: refusing"):
        pubrepo.check_test_sandbox(repo, pubrepo.load_publish_config(repo / ".publish.toml"))


# --- (c) the network wrapper ----------------------------------------------------

def test_network_wrapper_check(source_repo, public_remote, run, monkeypatch):
    """sandbox::network_wrapper_check: with the CLI-entry check patched out,
    the guard inside fetch_tip still refuses a remote outside the base."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    assert run(["init"], cwd=repo).code == 0
    git_run("remote", "set-url", "origin", "/outside-the-sandbox/x.git", cwd=repo / ".publish")
    monkeypatch.setattr(pubrepo, "check_test_sandbox", lambda *a, **k: None)
    with pytest.raises(pubrepo.PublishError) as exc:
        pubrepo.fetch_tip(repo / ".publish", "main")
    assert exc.value.exit_code == 2 and "test sandbox: refusing" in str(exc.value)
    with pytest.raises(pubrepo.PublishError, match="test sandbox: refusing"):
        pubrepo.check_test_sandbox_url("https://github.com/example-invalid/x.git")


# --- (b) the spawn audit -----------------------------------------------------------------

def test_spawn_audit_fires(monkeypatch, tmp_path):
    """sandbox::spawn_audit_fires (AC110): Popen, os.system and pty.spawn
    with a cwd outside the base, and a github URL argument, each fail."""
    with pytest.raises(RuntimeError, match="outside the sandbox"):
        subprocess.run(["true"], cwd="/")
    with pytest.raises(RuntimeError, match="network URL"):
        subprocess.run(["git", "ls-remote", "https://github.com/example-invalid/x.git"])
    with pytest.raises(RuntimeError, match="lacks PUBREPO_TEST_SANDBOX"):
        subprocess.run(["true"], env={"PATH": os.environ["PATH"]})
    monkeypatch.chdir("/")
    with pytest.raises(RuntimeError, match="outside the sandbox"):
        os.system("true")
    with pytest.raises(RuntimeError, match="outside the sandbox"):
        pty.spawn(["true"])
    monkeypatch.chdir(tmp_path)
    assert subprocess.run(["true"]).returncode == 0          # a compliant child passes


def test_dotless_host_path_shorthand_is_a_network_url():
    """The spawn audit flags an ssh-alias `host:/path` with a dotless
    host (here a dummy host); revision paths and file:// URLs are not network."""
    assert _network_url("gitserver:/srv/git/x.git")
    for value in ("HEAD:pubrepo.py", "3af256f:tests/conftest.py", "cp/a:tests/x",
                  "HEAD:refs/x", "file:///tmp/x.git"):
        assert not _network_url(value), value


def test_multiword_url_text_allowed(tmp_path):
    """sandbox::multiword_url_text_allowed."""
    git_run("init", "-q", str(tmp_path / "r"))
    git_run("commit", "-q", "--allow-empty", "-m", "see https://github.com/x", cwd=tmp_path / "r")


def test_children_have_no_ctty(tmp_path):
    """sandbox::children_have_no_ctty (AC110): /dev/tty cannot be opened,
    directly and through the git shim's `sh -c`."""
    probe = "import os; os.open('/dev/tty', os.O_RDWR)"
    direct = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert direct.returncode != 0
    assert f"[Errno {errno.ENXIO}]" in direct.stderr    # the text differs by OS
    git_run("init", "-q", str(tmp_path / "r"))
    shell = subprocess.run(["git", "-c", f"alias.probe=!{sys.executable} -c \"{probe}\"", "probe"],
                           cwd=tmp_path / "r", capture_output=True, text=True)
    assert shell.returncode != 0
    assert f"[Errno {errno.ENXIO}]" in shell.stderr    # the text differs by OS


# --- (b) the PATH git shim ---------------------------------------------------------------

def test_git_shim_refuses_transport_and_cwd(tmp_path):
    """sandbox::git_shim_refuses_transport_and_cwd: reached through `sh -c`
    (a multi-word value the spawn audit allows), the shim itself refuses."""
    https = subprocess.run(["sh", "-c", "git ls-remote https://example.invalid/x.git"],
                           capture_output=True, text=True)
    assert https.returncode == 128
    assert "test sandbox (git shim): non-file transport" in https.stderr
    gitdir = subprocess.run(["sh", "-c", "GIT_DIR=/ git rev-parse --git-dir"],
                            capture_output=True, text=True)
    assert gitdir.returncode == 128
    assert "test sandbox (git shim): GIT_DIR" in gitdir.stderr


# --- (a) TMPDIR ------------------------------------------------------------

def test_tmpdir_inside_base(source_repo, public_remote, run):
    """sandbox::tmpdir_inside_base: tempfile and a child's $TMPDIR are inside
    the base, and an uninitialized --dry-run's scratch store stays there."""
    base = _base()
    assert base in Path(tempfile.gettempdir()).resolve().parents
    child = subprocess.run(["sh", "-c", 'printf %s "$TMPDIR"'], capture_output=True, text=True)
    assert base in Path(child.stdout).resolve().parents
    system_tmp = Path("/tmp")
    before = {p.name for p in system_tmp.iterdir() if p.name.startswith("pubrepo-")}
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    assert run(["--dry-run"], cwd=repo).code == 0
    after = {p.name for p in system_tmp.iterdir() if p.name.startswith("pubrepo-")}
    assert after == before



# --- sandbox_unreachable, and ssh -G only ----------------------

def _write_remote(repo, name, url):
    with open(repo / ".git" / "config", "a") as f:
        f.write(f'[remote "{name}"]\n\turl = {url}\n')


def test_unregistered_url_still_refused(source_repo, public_remote, sandbox_unreachable):
    """sandbox::unregistered_url_still_refused: in the same test, a
    registered URL passes the CLI-entry scan and the audit's git-config
    check, while an unregistered one is still refused by both, and the
    network wrapper refuses even the registered one."""
    config = {"remote": public_remote(), "include": ["a.py"]}
    repo = source_repo({"a.py": "x\n"}, config, origin=False)
    _write_remote(repo, "reg", sandbox_unreachable("https://reg.example.invalid/x.git"))
    pubrepo.check_test_sandbox(repo, config)                  # registered: not refused
    assert git_run("remote", "get-url", "reg", cwd=repo).strip() == \
        "https://reg.example.invalid/x.git"
    with pytest.raises(pubrepo.PublishError, match="test sandbox: refusing"):
        pubrepo.check_test_sandbox_url("https://reg.example.invalid/x.git")
    _write_remote(repo, "unreg", "https://unreg.example.invalid/y.git")
    with pytest.raises(pubrepo.PublishError, match="test sandbox: refusing"):
        pubrepo.check_test_sandbox(repo, config)
    with pytest.raises(RuntimeError, match="names a network URL"):
        git_run("remote", cwd=repo)


def test_registration_does_not_leak_a(sandbox_unreachable):
    """sandbox::registration_does_not_leak, part 1: register a URL."""
    sandbox_unreachable("https://leak.example.invalid/x.git")
    assert "https://leak.example.invalid/x.git" in pubrepo._TEST_SANDBOX_UNREACHABLE


def test_registration_does_not_leak_b(tmp_path):
    """sandbox::registration_does_not_leak, part 2 (runs next): the URL
    part 1 registered is refused again."""
    import conftest
    assert conftest._UNREACHABLE == set()
    assert pubrepo._TEST_SANDBOX_UNREACHABLE == frozenset()
    why = conftest.sandbox_violation(["git", "ls-remote", "https://leak.example.invalid/x.git"],
                                     tmp_path, os.environ)
    assert why is not None and "network URL" in why


def test_registration_needs_a_literal_reserved_host(sandbox_unreachable):
    """A computed string or a reachable private host
    is not registered."""
    host = "computed"
    with pytest.raises(AssertionError, match="not a string literal"):
        sandbox_unreachable(f"https://{host}.example.invalid/x.git")
    with pytest.raises(AssertionError):
        sandbox_unreachable("https://10.0.0.5/x.git")
    with pytest.raises(AssertionError):
        sandbox_unreachable("git@gitserver.example.com.evil:o/r")


def test_registered_url_in_a_network_command_is_recorded(tmp_path):
    """A git network command with a registered URL is
    recorded for the teardown assertion; local reads are not."""
    import conftest
    conftest._UNREACHABLE.add("https://rec.example.invalid/x.git")
    try:
        assert conftest._registered("https://rec.example.invalid/x.git",
                                    ["git", "ls-remote", "--get-url", "x"])
        assert conftest._UNREACHABLE_HITS == []
        assert conftest._registered("https://rec.example.invalid/x.git",
                                    ["git", "-C", "r", "push", "origin", "main"])
        assert conftest._UNREACHABLE_HITS == [["git", "-C", "r", "push", "origin", "main"]]
    finally:
        conftest._UNREACHABLE.clear()
        conftest._UNREACHABLE_HITS.clear()


def test_only_ssh_dash_g_may_run(tmp_path):
    """The spawn audit lets exactly `ssh ... -G ...` through."""
    import conftest
    assert conftest.sandbox_violation(["ssh", "-F", "/dev/null", "-G", "github.com"],
                                      tmp_path, os.environ) is None
    why = conftest.sandbox_violation(["ssh", "github.com", "true"], tmp_path, os.environ)
    assert why is not None and "-G" in why



def test_push_naming_no_remote_to_a_registered_upstream_is_a_hit(tmp_path):
    """A git network command that names no remote goes to
    pushRemote/pushDefault/the upstream, which may be any remote; with a
    registered URL configured on any remote of the repo it counts as a hit.
    Naming another remote, or clone, does not."""
    import conftest
    repo = tmp_path / "r"
    git_run("init", "-q", str(repo))
    with open(repo / ".git" / "config", "a") as f:
        f.write('[remote "up"]\n\turl = https://up.example.invalid/x.git\n'
                '[remote "local"]\n\turl = /srv/local.git\n'
                '[branch "main"]\n\tremote = up\n')
    conftest._UNREACHABLE.add("https://up.example.invalid/x.git")
    try:
        for argv, hit in ((["git", "push"], True), (["git", "fetch"], True),
                          (["git", "ls-remote"], True), (["git", "push", "--all"], True),
                          (["git", "remote", "update"], True), (["git", "push", "up"], True),
                          (["git", "push", "local", "main"], False),
                          (["git", "clone", "file:///srv/x.git", "y"], False),
                          (["git", "config", "--get", "remote.up.url"], False)):
            conftest._UNREACHABLE_HITS.clear()
            assert conftest.sandbox_violation(argv, repo, os.environ) is None
            assert bool(conftest._UNREACHABLE_HITS) == hit, argv
    finally:
        conftest._UNREACHABLE.clear()
        conftest._UNREACHABLE_HITS.clear()
