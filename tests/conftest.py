"""Shared fixtures for the pubrepo (git-sync-publish) test suite.

The tool is a single extensionless script; this conftest loads it once as
the module 'pubrepo' so tests can call main(argv) in-process. Git stays a
real subprocess against local bare remotes (file:// URLs) — no network,
no mocking of git.
"""
import importlib.machinery
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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
def run(monkeypatch, capsys):
    """Invoke main(argv) in-process under chdir; capture stdout/stderr/code."""

    def _run(args: list[str], cwd: Path) -> SimpleNamespace:
        monkeypatch.chdir(cwd)
        try:
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
