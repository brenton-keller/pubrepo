"""Shared fixtures for the pubrepo (git-sync-publish) test suite.

The tool is a single extensionless script; this conftest loads it once as
the module 'pubrepo' so tests can call main(argv) in-process. Git stays a
real subprocess against local bare remotes (file:// URLs) — no network,
no mocking of git.
"""
import ast
import importlib.machinery
import inspect
import importlib.util
import ipaddress
import json
import os
import pty
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import socket
import unicodedata
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

# --- Test sandbox (design §17.4) ---
# Mechanical, not inspected: no test reaches the network, the real
# repositories, the user's git config, or a controlling terminal.
#
# Before any fixture runs, git speaks only file:// and never prompts, for this
# process and every child.
os.environ["GIT_ALLOW_PROTOCOL"] = "file"
os.environ["GIT_TERMINAL_PROMPT"] = "0"

SANDBOX_VAR = "PUBREPO_TEST_SANDBOX"
_URL_TOKEN = re.compile(r"^(?:[a-z][a-z0-9+.-]*://|[\w.-]+@[\w.-]+:|ext::)", re.I)
_HOST_PATH = re.compile(r"^[\w-]+(?:\.[\w-]+)+:(?!refs/)"     # example.com:org/repo
                        r"|^[\w-]+:/(?!/)")                        # ssh-alias host:/path (CPA-04)
_CONFIG_URL = re.compile(r"^\s*(?:push)?url\s*=\s*(.+?)\s*$|^\s*\[url\s+\"(.+?)\"\]", re.M)
_CURRENT = {"pty": False}       # whether the running test is marked @pytest.mark.pty
# Exact URL strings the running test registered with
# sandbox_unreachable, and every git network command that ran with one of
# them in its argv or its repo's config (asserted empty at teardown).
_UNREACHABLE: set[str] = set()
_UNREACHABLE_HITS: list[list[str]] = []
_GIT_NETWORK_COMMANDS = {"fetch", "pull", "push", "clone", "ls-remote", "archive",
                         "fetch-pack", "send-pack"}
# The git_oracle fixture's subprocesses carry this marker in their
# environment; the audit lets exactly those through (they run the real git
# with logging stubs and never connect).
_ORACLE_ENV = "PUBREPO_TEST_GIT_ORACLE"


def _inside(path, base: Path) -> bool:
    p = Path(path).resolve()
    return p == base or base in p.parents


def _repo_root_of(path: Path) -> Path | None:
    """The git repository `path` belongs to (work tree or bare), or None."""
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists() or ((candidate / "HEAD").is_file()
                                             and (candidate / "objects").is_dir()):
            return candidate
    return None


def _network_url(value: str) -> bool:
    """A single-token, URL-shaped value naming a network location:
    any scheme but file://, user@host:, ext::, a bare host:path, or a value
    naming github.com. Multi-word text (a commit message, a shell
    snippet) is never a URL."""
    if not value or any(c.isspace() for c in value):
        return False
    if value.startswith("file://"):
        return False
    return bool(_URL_TOKEN.match(value) or _HOST_PATH.match(value)
                or "github.com" in value)


def _git_config_files(cwd: Path, env) -> list[Path]:
    files = [Path(env["GIT_CONFIG_GLOBAL"])] if env.get("GIT_CONFIG_GLOBAL") else []
    root = Path(cwd, env["GIT_DIR"]) if env.get("GIT_DIR") else _repo_root_of(cwd)
    if root is not None:
        git_dir = root / ".git" if (root / ".git").exists() else root
        if git_dir.is_file():                       # worktree: "gitdir: <path>"
            git_dir = Path(git_dir.read_text().split(":", 1)[1].strip())
        common = git_dir / "commondir"
        if common.is_file():
            git_dir = (git_dir / common.read_text().strip()).resolve()
        files.append(git_dir / "config")
    return [f for f in files if f.is_file()]


def _git_subcommand(argv: list[str]) -> str | None:
    """The git subcommand in `argv` (after -c/-C and other options)."""
    i = 1
    while i < len(argv):
        if argv[i] in ("-c", "-C"):
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return argv[i]
    return None


def _git_network_command(argv: list[str]) -> bool:
    """git would open a transport (`ls-remote --get-url` and `remote
    get-url` are local reads)."""
    if not argv or Path(argv[0]).name != "git":
        return False
    sub = _git_subcommand(argv)
    if sub == "ls-remote":
        return "--get-url" not in argv
    if sub == "remote":
        rest = argv[argv.index("remote") + 1:]
        return bool(rest) and rest[0] in ("update", "prune", "show")
    return sub in _GIT_NETWORK_COMMANDS


def _positional_after_subcommand(argv: list[str]) -> list[str]:
    i = 1
    while i < len(argv) and (argv[i].startswith("-")):
        i += 2 if argv[i] in ("-c", "-C") else 1
    return [a for a in argv[i + 1:] if not a.startswith("-")]


def _targets_config_remote(argv: list[str], owners: tuple[str, ...],
                           remotes: tuple[str, ...]) -> bool:
    """A git network command reaches a registered URL configured
    on remote(s) `owners` (all remotes of the repo: `remotes`) when it
    names one of them, or names no remote at all (pushRemote, pushDefault
    or the upstream may be any remote), or runs `--all` / `remote update`.
    clone is judged by its argv only."""
    sub = _git_subcommand(argv)
    if sub == "clone" or not owners:
        return False
    rest = _positional_after_subcommand(argv)
    if sub == "remote":
        names = rest[1:]
        return not names or any(n in owners for n in names)
    if "--all" in argv or not rest:
        return True
    first = rest[0]
    if first in remotes:
        return first in owners
    return False                             # an explicit URL or path: the argv check


def _registered(value: str, argv: list[str], owners: tuple[str, ...] | None = None,
                remotes: tuple[str, ...] = ()) -> bool:
    """A sandbox_unreachable URL: allowed through, and recorded when a git
    network command targets it: the URL in argv (owners None), or a value
    from git config that _targets_config_remote reaches."""
    if value not in _UNREACHABLE:
        return False
    if _git_network_command(argv) and (owners is None
                                       or _targets_config_remote(argv, owners, remotes)):
        _UNREACHABLE_HITS.append(list(argv))
    return True


_REMOTE_SECTION = re.compile(r'^\s*\[remote\s+"(.+?)"\]', re.M)


def _remote_owners(text: str, url: str) -> tuple[str, ...]:
    """The [remote "<name>"] sections of a git config text whose url or
    pushurl is `url`."""
    owners, current = [], None
    for line in text.splitlines():
        m = _REMOTE_SECTION.match(line)
        if m:
            current = m.group(1)
        elif line.lstrip().startswith("["):
            current = None
        elif current and re.match(r"^\s*(?:push)?url\s*=\s*" + re.escape(url) + r"\s*$", line):
            owners.append(current)
    return tuple(owners)


def sandbox_violation(args, cwd, env) -> str | None:
    """Why a child process breaks §17.4 (b)/(d), or None."""
    base_text = env.get(SANDBOX_VAR)
    if not base_text:
        return f"the child environment lacks {SANDBOX_VAR} (§17.4 d)"
    base = Path(base_text).resolve()
    effective = Path(cwd if cwd is not None else os.getcwd())
    if not _inside(effective, base):
        return f"cwd {effective} is outside the sandbox {base}"
    if env.get("GIT_DIR") and not _inside(effective / env["GIT_DIR"], base):
        return f"GIT_DIR {env['GIT_DIR']} is outside the sandbox"
    if isinstance(args, (list, tuple)):
        argv = [os.fspath(a) for a in args]
    else:
        try:
            argv = shlex.split(os.fspath(args))
        except ValueError:
            argv = os.fspath(args).split()
    if argv and Path(argv[0]).name == "ssh":
        # `ssh -G` (print the resolved config, offline) is the one ssh
        # the suite may run; any other ssh would be a network transport.
        return None if "-G" in argv[1:] else f"ssh other than ssh -G: {argv}"
    if env.get(_ORACLE_ENV) == "1" and argv and Path(argv[0]).name == "git":
        # git_oracle's own subprocess, only with its logging stubs
        stubs = (env.get("GIT_SSH_COMMAND"), env.get("GIT_PROXY_COMMAND"))
        if env.get("GIT_CONFIG_NOSYSTEM") == "1" and all(
                stub and _inside(shlex.split(stub)[0], base) for stub in stubs):
            return None
        return "a git_oracle subprocess without its logging stubs"
    for arg in argv[1:]:
        value = arg.split("=", 1)[1] if arg.startswith(("--git-dir=", "--work-tree=")) else arg
        if _network_url(value) and not _registered(value, argv):
            return f"argv names a network URL: {value}"
        if not value or "\n" in value or len(value) > 4096 or "://" in value:
            continue
        candidate = effective / value
        try:
            if not candidate.exists() or candidate.resolve() == SCRIPT_PATH:
                continue
        except OSError:
            continue
        root = _repo_root_of(candidate.resolve())
        if root is not None and not _inside(root, base):
            return f"argument {value} resolves into the repository {root}, outside the sandbox"
    if argv and Path(argv[0]).name == "git":
        for config in _git_config_files(effective, env):
            text = config.read_text(errors="replace")
            remotes = tuple(_REMOTE_SECTION.findall(text))
            for url, base_url in _CONFIG_URL.findall(text):
                if _network_url(url or base_url) and not _registered(
                        url or base_url, argv, _remote_owners(text, url) if url else None,
                        remotes):
                    return f"git config {config} names a network URL: {url or base_url}"
    return None


def _refuse(why: str, what) -> None:
    raise RuntimeError(f"test sandbox (§17.4): {why}: {what!r}")


# (§17.4 b) Every way a test or pubrepo starts a process is audited.
_popen_init = subprocess.Popen.__init__


def _guarded_popen_init(self, args, *rest, **kwargs):
    cwd = kwargs.get("cwd", rest[8] if len(rest) > 8 else None)
    env = kwargs.get("env", rest[9] if len(rest) > 9 else None)
    why = sandbox_violation(args, cwd, os.environ if env is None else env)
    if why is not None:
        _refuse(why, args)
    if not _CURRENT["pty"] and len(rest) <= 9:
        kwargs["start_new_session"] = True      # (§17.4 a) no controlling terminal
    _popen_init(self, args, *rest, **kwargs)


subprocess.Popen.__init__ = _guarded_popen_init


def _audit(name: str, original, argv_of, env_of=lambda *a, **k: None):
    def guarded(*args, **kwargs):
        env = env_of(*args, **kwargs)
        why = sandbox_violation(argv_of(*args, **kwargs), None,
                                os.environ if env is None else env)
        if why is not None:
            _refuse(why, args)
        return original(*args, **kwargs)
    guarded.__name__ = name
    return guarded


os.system = _audit("system", os.system, lambda cmd: cmd)
for _name in ("execv", "execvp"):
    setattr(os, _name, _audit(_name, getattr(os, _name), lambda path, argv: argv))
for _name in ("execve", "execvpe"):
    setattr(os, _name, _audit(_name, getattr(os, _name), lambda path, argv, env: argv,
                              lambda path, argv, env: env))
for _name in ("execl", "execlp"):
    setattr(os, _name, _audit(_name, getattr(os, _name), lambda path, *argv: list(argv)))
for _name in ("execle", "execlpe"):
    setattr(os, _name, _audit(_name, getattr(os, _name), lambda path, *rest: list(rest[:-1]),
                              lambda path, *rest: rest[-1]))
for _name in ("posix_spawn", "posix_spawnp"):
    if hasattr(os, _name):
        setattr(os, _name, _audit(_name, getattr(os, _name), lambda path, argv, env, **k: argv,
                                  lambda path, argv, env, **k: env))
pty.fork = _audit("fork", pty.fork, lambda: ["pty.fork"])
pty.spawn = _audit("spawn", pty.spawn,
                   lambda argv, *a, **k: [argv] if isinstance(argv, str) else list(argv))


@pytest.fixture(scope="session")
def sandbox_git_shim_dir(tmp_path_factory):
    """(§17.4 b) The one PATH `git` for the whole suite,
    POSIX sh for speed. It refuses (exit 128) a non-file transport argument
    and a $PWD or GIT_DIR outside the sandbox, logs argv for AC62 when
    PUBREPO_AC62_LOG is set, and execs the real git in a new session where
    setsid(1) exists (Linux); on macOS the Popen wrapper's new session is
    inherited."""
    real_git = shutil.which("git")
    assert real_git, "git not on PATH"
    shim_dir = tmp_path_factory.mktemp("sandbox-git-shim")
    shim = shim_dir / "git"
    shim.write_text(f"""#!/bin/sh
refuse() {{ echo "test sandbox (git shim): $1" >&2; exit 128; }}
[ -n "$PUBREPO_TEST_SANDBOX" ] || refuse "PUBREPO_TEST_SANDBOX is not set"
case "$PWD/" in "$PUBREPO_TEST_SANDBOX"/*) ;; *) refuse "cwd $PWD is outside $PUBREPO_TEST_SANDBOX" ;; esac
if [ -n "$GIT_DIR" ]; then
  case "$GIT_DIR" in /*) gd="$GIT_DIR" ;; *) gd="$PWD/$GIT_DIR" ;; esac
  case "$gd/" in "$PUBREPO_TEST_SANDBOX"/*) ;; *) refuse "GIT_DIR $GIT_DIR is outside $PUBREPO_TEST_SANDBOX" ;; esac
fi
for arg in "$@"; do
  case "$arg" in
    *[[:space:]]*) ;;
    file://*) ;;
    ext::*|*://*) refuse "non-file transport: $arg" ;;
    [A-Za-z0-9._-]*@[A-Za-z0-9._-]*:*) refuse "non-file transport: $arg" ;;
  esac
done
if [ -n "$PUBREPO_AC62_LOG" ]; then
  {{ printf '%s\\0' "$@"; printf '\\036'; }} >> "$PUBREPO_AC62_LOG"
fi
if command -v setsid >/dev/null 2>&1; then exec setsid -w {shlex.quote(real_git)} "$@"; fi
exec {shlex.quote(real_git)} "$@"
""")
    shim.chmod(0o755)
    return shim_dir


@pytest.fixture(scope="session")
def _sandbox_git_config(tmp_path_factory):
    """A private global git config: a synthetic identity, never the user's."""
    path = tmp_path_factory.mktemp("sandbox-gitconfig") / "gitconfig"
    path.write_text("[user]\n\tname = Pubrepo Tests\n\temail = tests@example.invalid\n"
                    "[init]\n\tdefaultBranch = main\n"
                    "[protocol \"file\"]\n\tallow = always\n"
                    "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n")
    return path


@pytest.fixture(autouse=True)
def _test_sandbox(request, tmp_path, tmp_path_factory, monkeypatch, _sandbox_git_config,
                  sandbox_git_shim_dir):
    """(§17.4 a) Every test runs in its own tmp_path with HOME, TMPDIR and the
    global git config inside the sandbox, the PATH git shim first, and
    PUBREPO_TEST_SANDBOX naming the pytest base temp, so pubrepo's own
    sandbox check (§17.4 c) is on. Children start in a new session unless
    the test is marked @pytest.mark.pty."""
    home = tmp_path_factory.mktemp("home")
    scratch = tmp_path_factory.mktemp("tmpdir")
    monkeypatch.setenv(SANDBOX_VAR, str(tmp_path_factory.getbasetemp().resolve()))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(_sandbox_git_config))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.setenv("PATH", f"{sandbox_git_shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.chdir(tmp_path)
    _CURRENT["pty"] = request.node.get_closest_marker("pty") is not None
    _UNREACHABLE.clear()                   # no registration crosses tests
    _UNREACHABLE_HITS.clear()
    yield
    _CURRENT["pty"] = False
    _UNREACHABLE.clear()


# Registered URLs name only a public host
# (PUBLIC_HOSTS or a subdomain), a reserved name (.invalid, .example, .test,
# example.com/.net/.org) or a documentation address, never a reachable
# private host.
_DOC_NETWORKS = [ipaddress.ip_network(n) for n in
                 ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24", "2001:db8::/32")]


def unreachable_host_ok(url: str) -> bool:
    value = url
    while (m := re.match(r"^[A-Za-z0-9+.-]+::(.*)$", value, re.S)) is not None:
        value = m.group(1)                                 # innermost helper address
    value = urllib.parse.unquote(unicodedata.normalize("NFKC", value)).replace("\u3002", ".")
    m = re.match(r"^(?:[A-Za-z0-9+.-]+://)?(?:[^@/\[]*@)?(\[[^\]]*\]|[^:/]*)", value)
    if m is None:
        return False
    host = m.group(1).strip("[]")
    if not host:
        return True                        # no host at all: nothing to reach
    host = host.rsplit("@", 1)[-1]
    try:
        return any(ipaddress.ip_address(host) in net for net in _DOC_NETWORKS)
    except ValueError:
        host = host.split(":", 1)[0].rstrip(".").lower()
    names = (*pubrepo.PUBLIC_HOSTS, "example.com", "example.net", "example.org")
    return (host.endswith((".invalid", ".example", ".test"))
            or any(host == h or host.endswith("." + h) for h in names))


_LITERALS: dict[str, frozenset[str]] = {}


def _module_string_literals(filename: str) -> frozenset[str]:
    """Every whole string constant in a test module (condition 1: a
    registered URL is a literal, never computed; f-strings do not count)."""
    if filename not in _LITERALS:
        tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
        joined = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                  for v in n.values}
        _LITERALS[filename] = frozenset(
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in joined)
    return _LITERALS[filename]


@pytest.fixture
def sandbox_unreachable(monkeypatch):
    """`sandbox_unreachable(*urls)` registers those
    exact strings, for this test only, with pubrepo's CLI-entry sandbox scan
    and with the spawn audit's argv and git-config checks, so a test can put
    a public or .invalid URL into git config.
    - Call sites pass string literals (test_ff_sandbox checks every call
      site); hosts must pass unreachable_host_ok.
    - The network wrapper (check_test_sandbox_url, _guard_remote), the git
      shim, GIT_ALLOW_PROTOCOL=file and every unregistered URL still refuse.
    - Teardown clears the registration and asserts that no git network
      command ran with a registered URL in its argv or repo config."""
    def register(*urls):
        literals = _module_string_literals(inspect.stack()[1].filename)
        for url in urls:
            assert isinstance(url, str) and unreachable_host_ok(url), url
            assert url in literals, f"not a string literal in the calling test module: {url!r}"
            _UNREACHABLE.add(url)
        monkeypatch.setattr(pubrepo, "_TEST_SANDBOX_UNREACHABLE", frozenset(_UNREACHABLE))
        return urls[0] if len(urls) == 1 else urls

    yield register
    hits = list(_UNREACHABLE_HITS)
    _UNREACHABLE.clear()
    _UNREACHABLE_HITS.clear()
    assert hits == [], f"git network command(s) ran with a registered URL: {hits}"


def _real_git() -> str:
    """The git binary behind the sandbox shim."""
    path = os.pathsep.join(p for p in os.environ.get("PATH", "").split(os.pathsep)
                           if "sandbox-git-shim" not in p)
    found = shutil.which("git", path=path)
    assert found, "git not on PATH"
    return found


@pytest.fixture
def git_oracle(tmp_path):
    """§18.10: where the REAL git (not the PATH shim) would
    connect, without connecting.
    - ssh: GIT_SSH_COMMAND is a logging stub (git's OpenSSH variant, so it
      passes -p), which exits 1;
    - git://: GIT_PROXY_COMMAND is a logging stub (git passes host and port);
    - http(s): `-c http.proxy=http://127.0.0.1:<port>` to a loopback
      listener that records the request line and Host:, then closes.
    GIT_ALLOW_PROTOCOL is widened for that one subprocess only, with
    GIT_CONFIG_NOSYSTEM=1; the spawn audit lets it through only with both
    stubs set. `.remote(cwd, *args)` runs a push or fetch by remote name
    the same way and returns the connections git attempted."""
    stub_dir = tmp_path / "git-oracle"
    stub_dir.mkdir()
    log = stub_dir / "log.jsonl"
    for name in ("ssh", "proxy"):
        (stub_dir / name).write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(log)!r}, 'a').write(json.dumps([{name!r}, sys.argv[1:]]) + '\\n')\n"
            "sys.exit(1)\n")
        (stub_dir / name).chmod(0o755)
    real_git = _real_git()

    def env(protocols: str) -> dict:
        e = {**os.environ, _ORACLE_ENV: "1", "GIT_SSH_COMMAND": shlex.quote(str(stub_dir / "ssh")),
             "GIT_SSH_VARIANT": "ssh", "GIT_PROXY_COMMAND": str(stub_dir / "proxy"),
             "GIT_CONFIG_NOSYSTEM": "1", "GIT_ALLOW_PROTOCOL": protocols,
             "GIT_TERMINAL_PROMPT": "0"}
        for key in ("GIT_SSH", "NO_PROXY", "no_proxy", "HTTP_PROXY", "http_proxy",
                    "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            e.pop(key, None)
        return e

    def calls() -> list:
        if not log.exists():
            return []
        return [tuple(json.loads(line)) for line in log.read_text().splitlines()]

    def _ssh_target(argv: list[str]) -> tuple[str, int | None]:
        port, i = None, 0
        while i < len(argv):
            if argv[i] == "-p":
                port, i = int(argv[i + 1]), i + 2
            elif argv[i] in ("-o", "-l", "-i", "-F"):
                i += 2
            elif argv[i].startswith("-"):
                i += 1
            else:
                return argv[i].rsplit("@", 1)[-1], port
        raise AssertionError(f"no host in ssh argv {argv}")

    def run(args: list[str], cwd, protocols: str) -> list:
        log.write_text("")
        subprocess.run([real_git, *args], cwd=cwd, env=env(protocols), capture_output=True,
                       text=True, timeout=30, stdin=subprocess.DEVNULL)
        return calls()

    def ssh(url: str) -> tuple[str, int | None]:
        found = run(["ls-remote", url], tmp_path, "ssh")
        assert found and found[0][0] == "ssh", f"git made no ssh attempt for {url}: {found}"
        return _ssh_target(found[0][1])

    def git_proto(url: str) -> tuple[str, int]:
        found = run(["ls-remote", url], tmp_path, "git")
        assert found and found[0][0] == "proxy", f"git made no git:// attempt for {url}"
        host, port = found[0][1][:2]
        return host, int(port)

    def http(url: str) -> list[str]:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        server.settimeout(20)
        requests: list[list[str]] = []

        def serve():
            try:
                while True:
                    conn, _ = server.accept()
                    with conn:
                        conn.settimeout(5)
                        data = b""
                        while b"\r\n\r\n" not in data:
                            chunk = conn.recv(4096)
                            if not chunk:
                                break
                            data += chunk
                        requests.append(data.decode("latin-1").split("\r\n"))
            except OSError:
                return

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        port = server.getsockname()[1]
        try:
            run(["-c", f"http.proxy=http://127.0.0.1:{port}", "-c", "http.lowSpeedTime=5",
                 "ls-remote", url], tmp_path, "http:https")
        finally:
            server.close()
            thread.join(timeout=5)
        assert len(requests) >= 1, f"git sent {url} without the proxy"
        return requests[0]

    def remote(cwd, *args: str) -> list:
        return run(list(args), cwd, "ssh:git:file")

    return SimpleNamespace(ssh=ssh, git=git_proto, http=http, remote=remote, calls=calls)


def pytest_configure(config):
    """The suite's one pytest_configure hook (conftest and ff_fixtures share
    a namespace through `from ff_fixtures import *`)."""
    config.addinivalue_line("markers", "pty: the test drives a real pseudo-terminal; its "
                            "children keep the caller's session (§17.4 a)")
    config.addinivalue_line("markers", "git_oracle: the test runs the real git through the "
                            "git_oracle fixture's logging stubs")
    register_ac62_markers(config)       # ff_fixtures (imported at the end of this file)


# The same test suite runs in the public repo (pubrepo.py) and the internal
# platform repo (git-sync-publish) — same file, two names (§11).
SCRIPT_PATH = next(
    p for name in ("pubrepo.py", "git-sync-publish")
    if (p := Path(__file__).resolve().parent.parent / name).exists()
)

_loader = importlib.machinery.SourceFileLoader("pubrepo", str(SCRIPT_PATH))
_spec = importlib.util.spec_from_loader("pubrepo", _loader)
pubrepo = importlib.util.module_from_spec(_spec)
sys.modules["pubrepo"] = pubrepo
_loader.exec_module(pubrepo)


def git_run(*args, cwd=None) -> str:
    """Run git, raising on failure; returns stdout."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
    )
    return result.stdout


def _to_toml_value(v) -> str:
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_to_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_to_toml_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"unsupported TOML value: {v!r}")


def config_toml(config: dict) -> str:
    """Serialize a .publish.toml from a dict shaped like the parsed config.

    'scrub' and 'transforms' become their own tables; everything else goes
    under [publish]. Intentionally permissive — invalid-shape tests rely on
    being able to serialize bad values (e.g. find = 5).
    """
    lines = ["[publish]"]
    for k, v in config.items():
        if k in ("scrub", "transforms"):
            continue
        lines.append(f"{k} = {_to_toml_value(v)}")
    if "scrub" in config:
        lines += ["", "[publish.scrub]"]
        for k, v in config["scrub"].items():
            lines.append(f"{k} = {_to_toml_value(v)}")
    if "transforms" in config:
        lines += ["", "[publish.transforms]"]
        for fname, rules in config["transforms"].items():
            lines.append(f'"{fname}" = {_to_toml_value(rules)}')
    return "\n".join(lines) + "\n"


@pytest.fixture
def source_repo(tmp_path):
    """Factory: build a committed source repo with files, a .publish.toml,
    and (by default) a bare source-origin wired as 'origin'."""
    counter = {"n": 0}

    def _make(files: dict[str, str], config: dict | str, origin: bool = True) -> Path:
        counter["n"] += 1
        repo = tmp_path / f"source{counter['n']}"
        repo.mkdir()
        git_run("init", "-q", "-b", "main", cwd=repo)
        git_run("config", "user.email", "test@pubrepo.test", cwd=repo)
        git_run("config", "user.name", "Pubrepo Tests", cwd=repo)
        for rel, content in files.items():
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
        toml_text = config if isinstance(config, str) else config_toml(config)
        (repo / ".publish.toml").write_text(toml_text)
        if ".gitignore" not in files:
            (repo / ".gitignore").write_text(".publish/\n")
        git_run("add", "-A", cwd=repo)
        git_run("commit", "-q", "-m", "init", cwd=repo)
        if origin:
            origin_dir = tmp_path / f"source-origin{counter['n']}.git"
            git_run("init", "--bare", "-q", "-b", "main", str(origin_dir))
            git_run("remote", "add", "origin", str(origin_dir), cwd=repo)
            git_run("push", "-q", "-u", "origin", "main", cwd=repo)
        return repo

    return _make


@pytest.fixture
def public_remote(tmp_path):
    """Factory: a bare repo standing in for the public remote; returns its
    file:// URL. Real push/fetch, no network."""
    counter = {"n": 0}

    def _make() -> str:
        counter["n"] += 1
        bare = tmp_path / f"public{counter['n']}.git"
        git_run("init", "--bare", "-q", "-b", "main", str(bare))
        return bare.as_uri()

    return _make


@pytest.fixture
def run(monkeypatch, capsys, git_policy):
    """Invoke main(argv) in-process under chdir; capture stdout/stderr/code.
    pubrepo's git calls go through the AC62 shim (ff_fixtures.git_policy)."""

    def _run(args: list[str], cwd: Path) -> SimpleNamespace:
        base = Path(os.environ[SANDBOX_VAR])
        if not _inside(cwd, base):
            raise RuntimeError(f"test sandbox (§17.4): the CLI would run outside {base}: {cwd}")
        monkeypatch.chdir(cwd)
        try:
            with git_policy.active():
                code = pubrepo.main(args)
        except SystemExit as e:  # argparse usage errors, until T14
            code = e.code if isinstance(e.code, int) else 1
        captured = capsys.readouterr()
        return SimpleNamespace(code=code, stdout=captured.out, stderr=captured.err)

    return _run


@pytest.fixture
def published_tree(tmp_path):
    """Clone a remote and return {relpath: bytes} — what the world actually
    received. The highest-value assertion in the suite."""
    counter = {"n": 0}

    def _tree(remote_url: str) -> dict[str, bytes]:
        counter["n"] += 1
        dest = tmp_path / f"clone{counter['n']}"
        subprocess.run(
            ["git", "clone", "-q", remote_url, str(dest)],
            check=True, capture_output=True,
        )
        tree = {}
        for p in dest.rglob("*"):
            rel = p.relative_to(dest)
            if p.is_file() and ".git" not in rel.parts:
                tree[str(rel)] = p.read_bytes()
        return tree

    return _tree


# 1.1.0 shared fixtures. Imported by module name:
# tests/ is not a package, and `from tests.ff_fixtures import *` fails when
# pytest runs from another directory.
from ff_fixtures import *  # noqa: E402,F401,F403
