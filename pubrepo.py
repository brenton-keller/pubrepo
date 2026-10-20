#!/usr/bin/env python3
"""pubrepo — Publish a curated subset of files to a clean git repo.

Reads .publish.toml from the current directory to determine which files to
include, builds the next snapshot on the current public tip (public files it
does not manage are carried as they are), commits it in .publish/ and pushes
it as a fast-forward. It never rewrites public history, except for an
explicit `--amend` of its own last snapshot or a planned, confirmed `repair`.

Supports [publish.scrub] for forbidden string detection and
[publish.transforms] for find/replace on specific files after copy.

Usage:
    pubrepo                  # Show publish state and pending changes (as `status`)
    pubrepo publish          # Publish; confirms on a terminal ([y/N]), --yes otherwise
    pubrepo init             # First-time setup (clone or init .publish/)
    pubrepo status           # Show publish state and pending changes
    pubrepo --dry-run        # Show what would be published without touching .publish/
    pubrepo publish -m "message"  # Publish with a custom commit message
    pubrepo status --json    # Machine-readable output
    pubrepo pull             # Bring commits made on the public repo into source
    pubrepo repair rewind COMMIT --plan  # Plan a public-history repair (also
                             # remove-path, restore-ancestry); --apply when confirmed
    pubrepo validate         # Check config and environment
    pubrepo integrate        # Alias of `pubrepo pull --plan`
    pubrepo publish --config .publish-sdk.toml  # Multi-target publish

(Deployed internally under the compatibility alias git-sync-publish —
same file, two names; hints follow the invoked name.)
"""
import sys

if sys.version_info < (3, 11):   # before tomllib and the other 3.11-only imports below
    sys.exit(f"pubrepo requires Python 3.11+; this is "
             f"{sys.version_info[0]}.{sys.version_info[1]} at {sys.executable}")

import argparse
import configparser
import difflib
import fnmatch
import hashlib
import io
import ipaddress
import json
import os
import posixpath
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.parsers.expat
import zipfile
from collections.abc import Iterable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Protocol

try:
    import fcntl
except ImportError:  # Windows — locking becomes a no-op (best-effort tier)
    fcntl = None

# Three-rule patterns (see compile_patterns); all are rule-3 basename matches.
DEFAULT_EXCLUDES = {
    "__pycache__", ".DS_Store", ".venv",
    ".claude", ".ruff_cache", ".pytest_cache", ".mypy_cache",
    "*.pyc", "*.egg-info",
}
# .env.* variants that are templates by convention, safe to publish.
ENV_ALLOWLIST = {".env.example", ".env.sample", ".env.template"}

PUBLISH_DIR_NAME = ".publish"
CONFIG_FILE_NAME = ".publish.toml"

__version__ = "1.1.0"

# CLI name as invoked: 'pubrepo' via the console script / renamed single
# file, 'git-sync-publish' via the internal deploy name (same file, two
# names). main() overwrites this from sys.argv[0] before any dispatch, so
# every hint message tells the user a command that exists on THEIR machine.
_PROG = "pubrepo"


def _derive_prog(argv0: str) -> str:
    name = Path(argv0).name
    return name if name in ("pubrepo", "pubrepo.py", "git-sync-publish") else "pubrepo"


EXIT_SUCCESS = 0
EXIT_CONFIG_ERROR = 1
EXIT_PUBLISH_FAILED = 2
EXIT_LOCK_CONTENTION = 3
EXIT_SCRUB_FAILED = 4
EXIT_DIVERGED = 5
EXIT_PENDING_CHANGES = 6
EXIT_RELEASE_FAILED = 7
EXIT_DECISION_REQUIRED = 8


class PubrepoError(Exception):
    """Base for all tool failures. main() prints str(e) to stderr verbatim
    and returns exit_code; nothing below main() calls sys.exit."""
    exit_code = EXIT_CONFIG_ERROR


class ConfigError(PubrepoError):
    exit_code = EXIT_CONFIG_ERROR


class PublishError(PubrepoError):
    exit_code = EXIT_PUBLISH_FAILED


class GitError(PubrepoError):
    """A check=True git command failed, with no claim about WHICH kind of
    failure it is — that's the caller's context. Inherits the base default
    exit code (config error: the pre-T5 behavior); T8/T10 catch and
    re-raise as context-specific exceptions where §8 needs other codes."""

    def __init__(self, git_args: list[str], stderr: str):
        self.git_args = git_args
        self.stderr = stderr
        super().__init__(sanitize_provider_output(f"git {' '.join(git_args)} failed: {stderr.strip()}"))


class LockContention(PubrepoError):
    exit_code = EXIT_LOCK_CONTENTION


class ScrubFailure(PubrepoError):
    exit_code = EXIT_SCRUB_FAILED

    def __init__(self, results: list[dict], message: str):
        self.results = results
        super().__init__(message)


class Diverged(PubrepoError):
    exit_code = EXIT_DIVERGED

    def __init__(self, commits: list[str], message: str):
        self.commits = commits
        super().__init__(message)


class Interrupted(PubrepoError):
    exit_code = 130  # 128 + SIGINT


class ReleaseError(PubrepoError):
    exit_code = EXIT_RELEASE_FAILED

    def __init__(self, message: str):
        sanitizer = globals().get("sanitize_provider_output")
        super().__init__(sanitizer(message) if sanitizer else message)


class DecisionRequired(PubrepoError):
    """A recorded owner decision is needed before this can proceed (S4, S9,
    S10b). Every "foreign state" refusal stays Diverged (exit 5)."""
    exit_code = EXIT_DECISION_REQUIRED


class RecordConflict(PubrepoError):
    """A create-only refs/pubrepo/* record ref already exists with a
    different value. Callers warn and keep the local value."""


class _UI:
    """Output discipline (§4): results → stdout; info/warnings/errors →
    stderr. --quiet: errors only (success is silent; the exit code is the
    answer). --verbose: adds detail(). ANSI color only when the stream is a
    tty, and never when NO_COLOR or --no-color is set."""

    RED, GREEN, YELLOW = "31", "32", "33"

    def __init__(self):
        self.quiet = False
        self.verbose = False
        self.no_color = bool(os.environ.get("NO_COLOR"))

    def configure(self, quiet: bool = False, verbose: bool = False,
                  no_color: bool = False) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.no_color = no_color or bool(os.environ.get("NO_COLOR"))

    def _paint(self, text: str, code: str, stream) -> str:
        if self.no_color or not (hasattr(stream, "isatty") and stream.isatty()):
            return text
        return f"\033[{code}m{text}\033[0m"

    def error(self, msg: str) -> None:
        print(self._paint(msg, self.RED, sys.stderr), file=sys.stderr)

    def warn(self, msg: str) -> None:
        if not self.quiet:
            print(self._paint(msg, self.YELLOW, sys.stderr), file=sys.stderr)

    def info(self, msg: str) -> None:
        if not self.quiet:
            print(msg, file=sys.stderr)

    def success(self, msg: str) -> None:
        if not self.quiet:
            print(self._paint(msg, self.GREEN, sys.stdout))

    def detail(self, msg: str) -> None:
        if self.verbose and not self.quiet:
            print(msg, file=sys.stderr)


ui = _UI()


def is_hard_excluded(name: str) -> bool:
    """Names that can NEVER be published, at any depth, regardless of config.

    .git objects are binary, so the scrub gate cannot catch leaked history.
    '.publish' exact plus the '.publish-' prefix cover the workdir namespace
    (multi-target dirs like .publish-sdk; T23 validates dir names into this
    namespace) without swallowing unrelated user names like
    '.published-artifacts'. The config file enumerates the forbidden strings
    and is itself a secret.
    """
    return (name == ".git"
            or name == PUBLISH_DIR_NAME or name.startswith(PUBLISH_DIR_NAME + "-")
            or name == CONFIG_FILE_NAME)


def _include_is_hard_excluded(inc: str) -> bool:
    """True if any path component of an include entry is hard-excluded."""
    return any(is_hard_excluded(part) for part in Path(inc).parts)


def is_env_excluded(name: str) -> bool:
    """.env and .envrc routinely hold secrets (direnv exports); .env.*
    likewise, except the documented template allowlist."""
    return (name in (".env", ".envrc")
            or (name.startswith(".env.") and name not in ENV_ALLOWLIST))


def compile_patterns(patterns: list[str]) -> Callable[[str, bool], bool]:
    """Compile exclude patterns into matcher(rel_path, is_dir) -> bool.

    Three rules — this is the documented contract:
      1. One trailing '/' is stripped first and makes the pattern dirs-only.
         (Stripping happens BEFORE rule 2's test, so 'internal/' is a bare
         name — gitignore's rule.)
      2. If the remainder contains '/', it matches anchored: fnmatch against
         the path relative to the source root. '*' crosses '/' (fnmatch
         behavior); there is no '**'.
      3. Otherwise it matches the basename, at any depth.

    Matching is case-sensitive, like git. fnmatch metacharacters * ? [ ]
    are live; a literal '[' is written '[[]'.
    """
    compiled = []
    for pat in patterns:
        original = pat
        dirs_only = pat.endswith("/")
        if dirs_only:
            pat = pat[:-1]
        compiled.append((pat, dirs_only, "/" in pat, original))

    def matcher(rel_path: str, is_dir: bool) -> str:
        # Returns the matching pattern (truthy) or "" — callers use it as a
        # bool; --verbose uses the pattern text to explain each exclusion.
        basename = rel_path.rsplit("/", 1)[-1]
        for pat, dirs_only, anchored, original in compiled:
            if dirs_only and not is_dir:
                continue
            if fnmatch.fnmatchcase(rel_path if anchored else basename, pat):
                return original
        return ""

    return matcher


def _include_matches_excludes(inc: str, target_is_dir: bool, matcher: Callable) -> bool:
    """True if an include entry, or any of its ancestors, is excluded.

    Explicit exclude beats explicit include; an excluded directory also
    dominates direct includes that jump below it.
    """
    parts = Path(inc).parts
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        is_dir = True if i < len(parts) else target_is_dir
        if matcher(prefix, is_dir):
            return True
    return False


# Destination locks held by this process: (pid, resolved lock path).
_HELD_PUBLISH_LOCKS: set[tuple[int, str]] = set()


@contextmanager
def publish_lock(publish_dir: Path):
    """Exclusive, non-blocking lock around the publish mutation span (M3).

    The lock file lives inside .git/ so it survives the nuke and can never
    be published; the holder's PID is written for the contention message.
    status/validate/--dry-run stay lock-free by design — they are read-only,
    and momentary inconsistency during a concurrent publish is acceptable.

    A second acquisition of the same lock in the same process is a defect
    flock is per open file description, so it would
    otherwise self-contend and report our own PID as the holder.
    """
    lock_path = publish_dir / ".git" / "pubrepo.lock"
    held = (os.getpid(), str(lock_path.resolve()))
    if held in _HELD_PUBLISH_LOCKS:
        raise AssertionError(f"publish_lock re-acquired in the same process: {lock_path}")
    _HELD_PUBLISH_LOCKS.add(held)
    try:
        with _flock_publish(lock_path):
            yield
    finally:
        _HELD_PUBLISH_LOCKS.discard(held)


@contextmanager
def _flock_publish(lock_path: Path):
    if fcntl is None:
        yield
        return
    f = open(lock_path, "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip() or "unknown"
        f.close()
        raise LockContention(
            f"ERROR: another publish is already running (PID {holder}).\n"
            f"If that process is gone, the lock is stale — remove {lock_path}.")
    try:
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        yield
    finally:
        with suppress(OSError):
            f.close()  # closing the fd releases the flock


def _in_publish_clone(cwd) -> bool:
    """`cwd` is a publish clone (.publish or .publish-<name>) or inside one."""
    return cwd is not None and any(
        part == PUBLISH_DIR_NAME or part.startswith(PUBLISH_DIR_NAME + "-")
        for part in Path(cwd).parts)


def _clone_env(cwd, env: dict | None = None) -> dict | None:
    """§18.13: every git command in a publish clone ignores replace
    objects, so history checks see the parents a push sends."""
    if not _in_publish_clone(cwd):
        return env
    return {**(os.environ if env is None else env), "GIT_NO_REPLACE_OBJECTS": "1"}


def _run_git_process(argv: list[str], *, cwd=None, env: dict | None = None,
                     **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run for every git process pubrepo starts, so a git command
    in a publish clone always ignores replace objects (§18.13)."""
    return subprocess.run(argv, cwd=cwd, env=_clone_env(cwd, env), **kwargs)


def git(*args, cwd=None, check=True, env=None):
    """Run a git command, returning the CompletedProcess.

    With check=True, a nonzero exit raises GitError.
    """
    result = _run_git_process(["git"] + list(args), cwd=cwd, capture_output=True, text=True,
                              env=env)
    if check and result.returncode != 0:
        raise GitError(list(args), result.stderr)
    return result


# Test-only fault seam (design §11). Points are named at the call
# site; `pull.after_write:<n>` carries the path ordinal.
_FAULT_POINTS = frozenset({
    "pull.after_write", "publish.after_push", "repair.after_push",
    "push.ambiguous", "push.ambiguous_absent", "confirm.before_lock",
})


def _test_fault(point: str) -> None:
    """Fire the fault configured in PUBREPO_TEST_FAULT=<point>:<action>.

    Pre: none.
    Post: with PUBREPO_TEST_FAULT unset, or naming another point, this is a
    no-op. When it names `point`, the action runs:
      sigterm    — os.kill(os.getpid(), SIGTERM)
      exit128    — os._exit(128)
      pause-file — block until $PUBREPO_TEST_FAULT_FILE exists
    An unknown point or action raises ValueError, so a typo in a test
    cannot leave the seam silently inert.
    """
    action = _armed_fault(point)
    if action is None:
        return
    if action == "sigterm":
        os.kill(os.getpid(), signal.SIGTERM)
    elif action == "exit128":
        os._exit(128)
    elif action == "pause-file":
        flag = os.environ.get("PUBREPO_TEST_FAULT_FILE")
        if not flag:
            raise ValueError("PUBREPO_TEST_FAULT pause-file needs PUBREPO_TEST_FAULT_FILE")
        while not os.path.exists(flag):
            time.sleep(0.05)
    else:
        raise ValueError(f"PUBREPO_TEST_FAULT: unknown action {action!r}")


def _armed_fault(point: str) -> str | None:
    """The action PUBREPO_TEST_FAULT configures for `point`, or None.

    The query half of the seam: the push wrapper asks
    whether `push.ambiguous[_absent]` is armed with `exit128`, which there
    means "report git exit 128", not process death. Validates like
    _test_fault.
    """
    spec = os.environ.get("PUBREPO_TEST_FAULT")
    if not spec:
        return None
    configured, sep, action = spec.rpartition(":")
    if not sep or configured.split(":", 1)[0] not in _FAULT_POINTS:
        raise ValueError(f"PUBREPO_TEST_FAULT: unknown point in {spec!r}")
    if point.split(":", 1)[0] not in _FAULT_POINTS:
        raise ValueError(f"_test_fault: unknown point {point!r}")
    return action if configured == point else None


def is_binary_file(path: Path, check_bytes: int = 8192) -> bool:
    """Check if file appears to be binary (has null bytes in first 8KB)."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(check_bytes)
            return b"\x00" in chunk
    except (IOError, OSError):
        return False


def _err(field: str, message: str) -> dict:
    return {"level": "error", "field": field, "message": message}


def _warning(field: str, message: str) -> dict:
    return {"level": "warning", "field": field, "message": message}


KNOWN_PUBLISH_KEYS = {"remote", "branch", "dir", "include", "exclude", "keep",
                      "scrub", "transforms", "on_republish", "release",
                      "records_remote", "peers", "private",
                      "tag_remote", "private_remotes", "public_hosts"}     # §18 (1.0.2)
KNOWN_SCRUB_KEYS = {"forbidden", "allow_opaque", "allow_markers"}
KNOWN_RULE_KEYS = {"find", "replace", "strip_between"}
KNOWN_RELEASE_KEYS = {"enabled", "version_file", "version_pattern", "tag_format",
                      "source_tag", "source_remote", "github", "pypi",
                      "adopt_tags", "strip_env"}
# [publish.release] strip_env: an exact variable name, or a prefix with one
# trailing '*' (FOO_*). A bare '*' would strip everything and is refused.
_STRIP_ENV_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\*?")


# Design §7 / §10 item 3: retired in 1.1.0.
ON_REPUBLISH_RETIRED = (
    'on_republish = "amend" was retired in 1.1.0: publish always adds a new snapshot '
    'on top of the public branch. Remove the key; use `--amend --yes` explicitly to '
    'rewrite your own last snapshot.')
FORCE_OVERWRITE_RETIRED = (
    "ERROR: --force-overwrite was removed in 1.1.0: publish never rewrites public "
    "history. To take in commits made on the public repo, run `{prog} pull`; to remove "
    "content from public history, use `{prog} repair`.")


def _string_list_error(field: str, value) -> dict | None:
    """The finding for a key that must be a list of non-empty strings."""
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        return _err(field, f"'{field}' must be a list of non-empty strings")
    return None
KNOWN_RELEASE_GITHUB_KEYS = {"enabled", "repository", "assets", "checksum_file",
                             "notes_file", "include_distributions"}
KNOWN_RELEASE_PYPI_KEYS = {"enabled", "package", "repository"}
DEFAULT_VERSION_PATTERN = r'^__version__\s*=\s*"(?P<version>[0-9A-Za-z.+-]+)"$'
PYPI_API_BASES = {
    "pypi": "https://pypi.org/pypi",
    "testpypi": "https://test.pypi.org/pypi",
}


def release_enabled(config: dict) -> bool:
    release = config.get("release")
    return isinstance(release, dict) and release.get("enabled") is True


def _validate_release_adapter(name: str, value, known: set[str], findings: list[dict]) -> None:
    field = f"release.{name}"
    if value is None:
        return
    if not isinstance(value, dict):
        findings.append(_err(field, f"'{field}' must be a table"))
        return
    enabled = value.get("enabled", False)
    if not isinstance(enabled, bool):
        findings.append(_err(f"{field}.enabled", "'enabled' must be true or false"))
        return
    if not enabled:
        return
    for key in value:
        if key not in known:
            findings.append(_warning(field, f"unknown key '{key}' in [publish.{field}] (ignored)"))
    required = "repository" if name == "github" else "package"
    if not isinstance(value.get(required), str) or not value.get(required):
        findings.append(_err(f"{field}.{required}", f"'{field}.{required}' must be a non-empty string"))
    if name == "github":
        for key in ("assets",):
            if key in value and (not isinstance(value[key], list)
                                 or not all(isinstance(x, str) and x for x in value[key])):
                findings.append(_err(f"{field}.{key}", f"'{field}.{key}' must be a list of non-empty strings"))
        for key in ("checksum_file", "notes_file"):
            if key in value and (not isinstance(value[key], str) or not value[key]):
                findings.append(_err(f"{field}.{key}", f"'{field}.{key}' must be a non-empty string"))
        for key in ("assets",):
            for item in value.get(key, []) if isinstance(value.get(key, []), list) else []:
                normalized = posixpath.normpath(item.replace("\\", "/")) if isinstance(item, str) else ""
                if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
                    findings.append(_err(f"{field}.{key}", f"'{item}' must stay inside the candidate export"))
        for key in ("checksum_file", "notes_file"):
            item = value.get(key)
            if isinstance(item, str):
                normalized = posixpath.normpath(item.replace("\\", "/"))
                if normalized.startswith("/") or normalized == ".." or normalized.startswith("../"):
                    findings.append(_err(f"{field}.{key}", f"'{item}' must be a relative path that does not escape"))
        if "include_distributions" in value and not isinstance(value["include_distributions"], bool):
            findings.append(_err(f"{field}.include_distributions", "'include_distributions' must be true or false"))
    else:
        repository = value.get("repository", "pypi")
        if repository not in PYPI_API_BASES:
            findings.append(_err(
                f"{field}.repository",
                "v1 supports only repository = \"pypi\" or \"testpypi\"; custom registries cannot yet be reconciled safely"))


RELEASE_KEYS_USED_WHEN_DISABLED = ("enabled", "tag_format", "adopt_tags")
RELEASE_DISABLED_WARNING = ("release management is disabled; release settings other than "
                            "tag_format and adopt_tags are ignored")


def validate_release_config(config: dict) -> list[dict]:
    """Pure release-table validation. Dormant settings never break old projects."""
    findings: list[dict] = []
    if "release" not in config:
        return findings
    release = config.get("release")
    if not isinstance(release, dict):
        return [_err("release", "'[publish.release]' must be a table")]
    enabled = release.get("enabled", False)
    if not isinstance(enabled, bool):
        return [_err("release.enabled", "'release.enabled' must be true or false")]
    if not enabled:
        # tag_format and adopt_tags still identify earlier managed releases
        # (managed_release_targets: repair's released-ancestry check).
        if any(k not in RELEASE_KEYS_USED_WHEN_DISABLED for k in release):
            findings.append(_warning("release", RELEASE_DISABLED_WARNING))
        return findings

    for key in release:
        if key not in KNOWN_RELEASE_KEYS:
            findings.append(_warning("release", f"unknown key '{key}' in [publish.release] (ignored)"))
    version_file = release.get("version_file")
    if not isinstance(version_file, str) or not version_file:
        findings.append(_err("release.version_file", "'release.version_file' must be a non-empty source-relative path"))
    pattern = release.get("version_pattern", DEFAULT_VERSION_PATTERN)
    if not isinstance(pattern, str) or not pattern:
        findings.append(_err("release.version_pattern", "'release.version_pattern' must be a non-empty string"))
    else:
        try:
            compiled = re.compile(pattern, re.MULTILINE)
            if compiled.groupindex != {"version": 1} or compiled.groups != 1:
                findings.append(_err("release.version_pattern", "version_pattern must contain exactly one named group 'version' and no other capture groups"))
        except re.error as exc:
            findings.append(_err("release.version_pattern", f"invalid regular expression: {exc}"))
    tag_format = release.get("tag_format", "v{version}")
    if not isinstance(tag_format, str) or tag_format.count("{version}") != 1:
        findings.append(_err("release.tag_format", "tag_format must contain exactly one '{version}'"))
    else:
        rendered = tag_format.replace("{version}", "0.0.0")
        checked = git("check-ref-format", "--allow-onelevel", f"refs/tags/{rendered}", check=False)
        if checked.returncode != 0:
            findings.append(_err("release.tag_format", f"tag_format renders invalid git tag '{rendered}'"))
    source_tag = release.get("source_tag", False)
    if not isinstance(source_tag, bool):
        findings.append(_err("release.source_tag", "'release.source_tag' must be true or false"))
    if source_tag and (not isinstance(release.get("source_remote", "origin"), str)
                       or not release.get("source_remote", "origin")):
        findings.append(_err("release.source_remote", "'release.source_remote' must be a non-empty string when source_tag is true"))
    elif source_tag and remote_name_problem(release.get("source_remote", "origin")):
        # §18.9 R1, §18.12: a git remote NAME of the source repo
        value = release.get("source_remote", "origin")
        findings.append(_err("release.source_remote",
                             f"'release.source_remote' must name a git remote of the source "
                             f"repo ({remote_name_problem(value)}; got {value!r})"))
    if "adopt_tags" in release:
        error = _string_list_error("release.adopt_tags", release["adopt_tags"])
        if error:
            findings.append(error)
    if "strip_env" in release:
        error = _string_list_error("release.strip_env", release["strip_env"])
        if error:
            findings.append(error)
        else:
            for entry in release["strip_env"]:
                if not _STRIP_ENV_RE.fullmatch(entry):
                    findings.append(_err(
                        "release.strip_env",
                        f"release.strip_env entry {entry!r} must be an environment variable "
                        f"name, optionally ending in '*' as a prefix wildcard (FOO_*)"))
    _validate_release_adapter("github", release.get("github"), KNOWN_RELEASE_GITHUB_KEYS, findings)
    _validate_release_adapter("pypi", release.get("pypi"), KNOWN_RELEASE_PYPI_KEYS, findings)
    return findings


def validate_release_environment(config: dict, source_root: Path) -> list[dict]:
    if not release_enabled(config):
        return []
    release = config["release"]
    value = release.get("version_file")
    if not isinstance(value, str) or not value:
        return []
    path = source_root / value
    try:
        if not path.resolve().is_relative_to(source_root.resolve()):
            return [_err("release.version_file", f"version_file escapes source root: {value}")]
    except OSError as exc:
        return [_err("release.version_file", f"cannot resolve version_file '{value}': {exc}")]
    findings = []
    if not path.is_file():
        findings.append(_err("release.version_file", f"version_file not found: {value}"))
    if not _path_would_publish(value.replace(os.sep, "/"), config):
        findings.append(_err("release.version_file", f"version_file '{value}' is not in the resolved publish set"))
    return findings


def validate_config(config: dict) -> list[dict]:
    """Centralized config validation (T15): every rule in one place.

    Returns findings [{level: error|warning, field, message}]. Pure config
    checks only — validate_environment covers filesystem/remote checks.
    Unknown keys warn, never error (forward compatibility).
    """
    f: list[dict] = []
    f.extend(validate_release_config(config))

    if "remote" not in config:
        f.append(_err("remote", f"{CONFIG_FILE_NAME} missing 'remote'"))
    elif not isinstance(config["remote"], str) or not config["remote"]:
        f.append(_err("remote", "'remote' must be a non-empty string"))

    branch = config.get("branch")
    if not isinstance(branch, str) or not branch:
        f.append(_err("branch", "'branch' must be a non-empty string"))

    dir_val = config.get("dir", PUBLISH_DIR_NAME)
    if not isinstance(dir_val, str) or "/" in dir_val or not (
            dir_val == PUBLISH_DIR_NAME
            or (dir_val.startswith(PUBLISH_DIR_NAME + "-")
                and len(dir_val) > len(PUBLISH_DIR_NAME) + 1)):
        f.append(_err("dir", f"'dir' must be '{PUBLISH_DIR_NAME}' or "
                             f"'{PUBLISH_DIR_NAME}-<name>' (single path component, "
                             f"non-empty name)"))

    include = config.get("include")
    if "include" not in config:
        f.append(_err("include", f"{CONFIG_FILE_NAME} missing 'include'"))
    elif not isinstance(include, list):
        f.append(_err("include", f"{CONFIG_FILE_NAME} 'include' must be a list"))
    elif not include:
        f.append(_err("include", "'include' must not be empty — the manifest is an "
                                 "explicit allowlist"))
    elif not all(isinstance(i, str) and i for i in include):
        f.append(_err("include", "include entries must be non-empty strings"))
    else:
        for inc in include:
            if _include_is_hard_excluded(inc):
                f.append(_err("include", f"'{inc}' can never be published — .git, "
                              f"{PUBLISH_DIR_NAME}*, and {CONFIG_FILE_NAME} stay private"))

    excludes = config.get("exclude", [])
    if not isinstance(excludes, list):
        f.append(_err("exclude", f"{CONFIG_FILE_NAME} 'exclude' must be a list"))
    elif not all(isinstance(e, str) for e in excludes):
        f.append(_err("exclude", "exclude entries must be strings"))
    else:
        for e in excludes:
            if e == "":
                f.append(_warning("exclude", "empty string in exclude has no effect"))
            elif e in ("*", "*/"):
                f.append(_warning("exclude", f"exclude pattern '{e}' matches everything — "
                                  f"the publish set will be empty"))

    keep = config.get("keep", [])
    if not isinstance(keep, list):
        f.append(_err("keep", f"{CONFIG_FILE_NAME} 'keep' must be a list"))
    else:
        for k in keep:
            if not isinstance(k, str):
                f.append(_err("keep", f"{CONFIG_FILE_NAME} keep entries must be strings"))
            elif "/" in k:
                f.append(_err("keep", f"{CONFIG_FILE_NAME} keep entry '{k}' contains '/' — keep is "
                              f"top-level-only: entries name files directly in the publish "
                              f"dir, not in subdirectories"))

    scrub = config.get("scrub", {})
    if scrub and not isinstance(scrub, dict):
        f.append(_err("scrub", "'scrub' must be a table"))
    elif isinstance(scrub, dict):
        forbidden = scrub.get("forbidden")
        if forbidden is not None:
            if not isinstance(forbidden, list):
                f.append(_err("scrub.forbidden", f"{CONFIG_FILE_NAME} 'scrub.forbidden' must be a list"))
            else:
                for s in forbidden:
                    if not isinstance(s, str):
                        f.append(_err("scrub.forbidden", "forbidden entries must be strings"))
                    elif not s:
                        f.append(_warning("scrub.forbidden", "empty string in forbidden matches nothing"))
        for key in ("allow_opaque", "allow_markers"):
            if key in scrub:
                error = _string_list_error(f"scrub.{key}", scrub[key])
                if error:
                    f.append(error)
        for k in scrub:
            if k not in KNOWN_SCRUB_KEYS:
                f.append(_warning("scrub", f"unknown key '{k}' in [publish.scrub] (ignored)"))

    transforms = config.get("transforms", {})
    if transforms and not isinstance(transforms, dict):
        f.append(_err("transforms", "'transforms' must be a table"))
    elif isinstance(transforms, dict):
        for filename, rules in transforms.items():
            if not isinstance(rules, list):
                f.append(_err("transforms", f"{CONFIG_FILE_NAME} transforms for '{filename}' must be a list"))
                continue
            for i, rule in enumerate(rules):
                prefix = f"{CONFIG_FILE_NAME} transforms['{filename}'][{i}]"
                if not isinstance(rule, dict):
                    f.append(_err("transforms", f"{prefix} must be a table"))
                    continue
                has_find = "find" in rule
                has_replace = "replace" in rule
                has_strip = "strip_between" in rule
                if has_strip and (has_find or has_replace):
                    f.append(_err("transforms", f"{prefix} has both find/replace and strip_between — use one per rule"))
                elif has_strip:
                    sb = rule["strip_between"]
                    if (not isinstance(sb, list) or len(sb) != 2
                            or not all(isinstance(m, str) and m for m in sb)):
                        f.append(_err("transforms", f"{prefix} 'strip_between' must be a list of two non-empty strings [start, end]"))
                elif has_find or has_replace:
                    if not (has_find and has_replace):
                        f.append(_err("transforms", f"{prefix} needs both 'find' and 'replace'"))
                    else:
                        if not isinstance(rule["find"], str) or not rule["find"]:
                            f.append(_err("transforms", f"{prefix} 'find' must be a non-empty string"))
                        if not isinstance(rule["replace"], str):
                            f.append(_err("transforms", f"{prefix} 'replace' must be a string"))
                else:
                    f.append(_err("transforms", f"{prefix} must have 'find'+'replace' or 'strip_between'"))
                for k in rule:
                    if k not in KNOWN_RULE_KEYS:
                        f.append(_warning("transforms", f"{prefix} unknown key '{k}' (ignored)"))

    if isinstance(keep, list) and isinstance(transforms, dict):
        keep_set = {k for k in keep if isinstance(k, str)}
        for target in transforms:
            normalized = posixpath.normpath(target)
            if os.path.isabs(normalized):
                f.append(_err("transforms",
                    f"transform target '{target}' is an absolute path — "
                    f"transform paths must be relative to the publish directory"))
                continue
            if normalized == ".." or normalized.startswith("../"):
                f.append(_err("transforms",
                    f"transform target '{target}' escapes the publish directory"))
                continue
            parts = PurePosixPath(normalized).parts
            if not parts:
                continue
            if parts[0] in keep_set:
                f.append(_err("keep",
                    f"transform target '{target}' is inside kept entry "
                    f"'{parts[0]}' — transforms on kept content compound "
                    f"across rebuilds; move the content into an included source "
                    f"file or remove from keep"))

    on_republish = config.get("on_republish")
    if on_republish == "amend":
        f.append(_err("on_republish", ON_REPUBLISH_RETIRED))
    elif on_republish is not None and on_republish != "new_commit":
        f.append(_err("on_republish",
            f'on_republish must be "new_commit" (got {on_republish!r})'))

    for key in ("tag_remote", "records_remote"):
        # §18.9 R1, §18.12: a git remote NAME of the source repo, never a URL
        value = config.get(key)
        if value is not None and remote_name_problem(value) is not None:
            f.append(_err(key, f"'{key}' must name a git remote of the source repo "
                               f"({remote_name_problem(value)}; got {value!r})"))
    for key in ("private_remotes", "public_hosts"):
        if key in config:
            error = _string_list_error(key, config[key])
            if error:
                f.append(error)
    for entry in config.get("private_remotes") or []:
        if isinstance(entry, str) and entry and entry != entry.strip():
            # §18.12: compared byte-exactly; surrounding whitespace is refused
            f.append(_err("private_remotes", f"private_remotes entry {entry!r} has leading or "
                                             f"trailing whitespace"))
    for key in ("peers", "private"):
        if key in config:
            error = _string_list_error(key, config[key])
            if error:
                f.append(error)

    for k in config:
        if k not in KNOWN_PUBLISH_KEYS:
            f.append(_warning("publish", f"unknown key '{k}' in [publish] (ignored)"))

    return f


def _include_removed_by_pull(source_root: Path, config: dict, path: str) -> str | None:
    """The sha of the pull record that removed `path` from source, or None.
    Read locally and before any fetch: the newest pull record on
    HEAD's first-parent chain that decided `path` (pull_records), when that
    decision removes it: `delete`, or `accept`/`theirs` of an absent theirs
    (the same rule as pull's "remove it from include" notice). Unreadable state → None."""
    try:
        records = pull_records(source_root, destination=destination_identity(config))
    except (PubrepoError, GitError, KeyError, TypeError, OSError):
        return None
    for rec in records:                                   # newest first
        for entry in rec.entries:
            if entry.get("path") != path:
                continue
            decision = entry.get("decision")
            removed = decision == "delete" or (decision in ("accept", "theirs")
                                               and entry.get("theirs") is None)
            return rec.sha if removed else None
    return None


def _remote_name_findings(config: dict, source_root: Path) -> list[dict]:
    """§18.9 R1, §18.12: tag_remote, records_remote and (with
    source_tag) release.source_remote must name an existing remote of the
    source repo (`git remote get-url <name>` succeeds; a local read)."""
    release = config.get("release") if isinstance(config.get("release"), dict) else {}
    keys = [("tag_remote", config.get("tag_remote")),
            ("records_remote", config.get("records_remote"))]
    if release.get("source_tag") is True:
        keys.append(("release.source_remote", release.get("source_remote", "origin")))
    findings = []
    for key, value in keys:
        if value is None or remote_name_problem(value) is not None:
            continue                       # absent, or already reported by validate_config
        if git("remote", "get-url", value, cwd=str(source_root), check=False).returncode != 0:
            findings.append(_err(key, f"'{key}' must name a git remote of the source repo "
                                      f"(no remote {value!r})"))
    return findings


def validate_environment(config: dict, source_root: Path, *,
                         config_label: str = CONFIG_FILE_NAME) -> list[dict]:
    """Environment checks (T15): include existence/escape, exclude collisions,
    transform reachability, remote URL shape. Guards against invalid config
    shapes so the validate command can report everything in one pass."""
    f: list[dict] = []
    f.extend(validate_release_environment(config, source_root))
    f.extend(_remote_name_findings(config, source_root))
    include = config.get("include")
    if not (isinstance(include, list) and include
            and all(isinstance(i, str) and i for i in include)):
        return f
    excludes = config.get("exclude", [])
    if not (isinstance(excludes, list) and all(isinstance(e, str) for e in excludes)):
        excludes = []
    user_matcher = compile_patterns(excludes)

    missing = []
    for inc in include:
        target = source_root / inc
        if not target.exists():
            missing.append(inc)
            continue
        resolved = target.resolve()
        if not resolved.is_relative_to(source_root.resolve()):
            f.append(_err("include", f"include path escapes the source root: {inc} — "
                          f"includes must stay inside the directory that holds the config"))
            continue
        if resolved == source_root.resolve():
            f.append(_warning("include", f"include '{inc}' is the source root — the manifest just "
                              f"became \"everything minus excludes\". Consider listing directories "
                              f"explicitly."))
        if _include_matches_excludes(inc, target.is_dir(), user_matcher):
            f.append(_warning("include", f"include '{inc}' is also matched by exclude — exclude wins, "
                              f"not published"))
    if missing:
        # a path a pull removed names that pull; any other
        # missing path gets the generic hint. Exit 1 either way.
        by_pull = {m: _include_removed_by_pull(source_root, config, m) for m in missing}
        for m, sha in by_pull.items():
            if sha:
                f.append(_err("include", f"include '{m}' names a path removed by pull "
                              f"{sha[:12]}; remove it from include in {config_label}"))
        rest = [m for m in missing if not by_pull[m]]
        if rest:
            f.append(_err("include", "include paths not found in working tree:\n"
                          + "\n".join(f"  {m}" for m in rest)
                          + f"\nremove them from include in {config_label}, or restore them "
                          f"(renamed? update include)"))

    transforms = config.get("transforms", {})
    if isinstance(transforms, dict) and transforms and not missing:
        try:
            wanted = _build_wanted_files(source_root, config)
        except Exception:
            wanted = None
        if wanted is not None:
            for t in transforms:
                if isinstance(t, str) and t not in wanted:
                    f.append(_warning("transforms", f"transform for '{t}' will never run "
                                      f"(not in the publish set)"))

    remote = config.get("remote")
    if isinstance(remote, str) and remote and not remote.startswith(
            ("https://", "http://", "ssh://", "git@", "file://", "/", ".")):
        f.append(_warning("remote", f"remote '{remote}' doesn't look like a git URL "
                          f"(https://, git@..., ssh://)"))
    return f


def load_publish_config(path: Path, strict: bool = True) -> dict:
    """Load .publish.toml; validation is centralized in validate_config.

    strict (the default): raise ConfigError listing every error finding;
    warnings print. strict=False (the validate command): return the config
    regardless, so cmd_validate can report ALL findings itself.
    """
    if not path.exists():
        raise ConfigError(
            f"ERROR: {CONFIG_FILE_NAME} not found in {path.parent}\n"
            f"Create one — minimal example:\n"
            f"  [publish]\n"
            f"  remote  = \"https://github.com/you/repo.git\"\n"
            f"  include = [\"src/\", \"README.md\"]\n"
            f"Then: {_PROG} init")
    try:
        with open(path, "rb") as fobj:
            data = tomllib.load(fobj)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"ERROR: {CONFIG_FILE_NAME} has invalid TOML: {e}")
    config = data.get("publish", {})
    config.setdefault("branch", "main")
    config.setdefault("dir", PUBLISH_DIR_NAME)
    config.setdefault("exclude", [])
    config.setdefault("keep", [])
    config.setdefault("on_republish", "new_commit")
    release = config.get("release")
    if isinstance(release, dict) and release.get("enabled") is True:
        release.setdefault("version_pattern", DEFAULT_VERSION_PATTERN)
        release.setdefault("tag_format", "v{version}")
        release.setdefault("source_tag", False)
        release.setdefault("source_remote", "origin")
        for adapter in ("github", "pypi"):
            if isinstance(release.get(adapter), dict):
                release[adapter].setdefault("enabled", False)
        if isinstance(release.get("pypi"), dict):
            release["pypi"].setdefault("repository", "pypi")

    if strict:
        findings = validate_config(config)
        for fi in findings:
            if fi["level"] == "warning":
                ui.warn(f"WARNING: {fi['message']}")
        errors = [fi for fi in findings if fi["level"] == "error"]
        if errors:
            raise ConfigError("\n".join(f"ERROR: {fi['message']}" for fi in errors))
    return config


def validate_includes(source_root: Path, config: dict, *,
                      config_label: str = CONFIG_FILE_NAME) -> None:
    """Raising wrapper over the environment findings, for the publish,
    preview and status paths: warnings print, the errors abort (exit 1).
    `config_label` names the loaded config file in hints."""
    findings = validate_environment(config, source_root, config_label=config_label)
    for fi in findings:
        if fi["level"] == "warning":
            ui.warn(f"WARNING: {fi['message']}")
    errors = [fi for fi in findings if fi["level"] == "error"]
    if errors:
        raise ConfigError("\n".join(f"ERROR: {fi['message']}" for fi in errors))


def make_ignore_fn(source_root: Path, excludes: list[str],
                   on_nested_git: Callable | None = None) -> Callable:
    """Build an ignore function for shutil.copytree.

    Hard-excludes (is_hard_excluded) run before user patterns and cannot be
    overridden; then env-file rules; then default + user exclude patterns
    via the three-rule matcher (compile_patterns). When a .git entry is
    skipped below the source root, on_nested_git (if given) is called with
    that directory's relative path.
    """
    matcher = compile_patterns(sorted(DEFAULT_EXCLUDES) + list(excludes))

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        ignored = set()
        rel_dir = os.path.relpath(directory, start=str(source_root))
        for name in contents:
            rel_path = name if rel_dir == "." else f"{rel_dir}/{name}"
            if is_hard_excluded(name):
                ignored.add(name)
                ui.detail(f"exclude: {rel_path} (hard-excluded)")
                if name == ".git" and on_nested_git is not None and rel_dir != ".":
                    on_nested_git(rel_dir)
                continue
            if is_env_excluded(name):
                ignored.add(name)
                ui.detail(f"exclude: {rel_path} (.env rule)")
                continue
            why = matcher(rel_path, os.path.isdir(os.path.join(directory, name)))
            if why:
                ignored.add(name)
                ui.detail(f"exclude: {rel_path} (pattern: {why})")
        return ignored

    return _ignore


def copy_includes(source_root: Path, publish_dir: Path, includes: list[str], excludes: list[str]) -> list[str]:
    """Copy included files/dirs into .publish/, returning list of copied paths."""
    warned_nested: set[str] = set()

    def _warn_nested_git(rel_dir: str) -> None:
        if rel_dir not in warned_nested:
            warned_nested.add(rel_dir)
            ui.warn(f"WARNING: nested git repo at '{rel_dir}': its files WILL be published "
                    f"but its history will not; add '{rel_dir}/' to exclude if unintended")

    base_ignore_fn = make_ignore_fn(source_root, excludes, on_nested_git=_warn_nested_git)
    user_matcher = compile_patterns(excludes)
    copied = []

    def _ignore_with_symlinks(directory: str, contents: list[str]) -> set[str]:
        """Wrap the base ignore function to also skip symlinks."""
        ignored = base_ignore_fn(directory, contents)
        for name in contents:
            full = os.path.join(directory, name)
            if os.path.islink(full):
                ignored.add(name)
        return ignored

    for inc in includes:
        # Defense in depth: validate_includes already rejects these.
        if _include_is_hard_excluded(inc):
            ui.warn(f"WARNING: refusing to publish '{inc}': hard-excluded")
            continue
        src = source_root / inc
        # Explicit USER exclude beats explicit include (warned in
        # validate_includes). Deliberately user patterns only: an explicit
        # include beats DEFAULT excludes at the entry level, though defaults
        # still filter the contents of walked directories (§6 precedence).
        if _include_matches_excludes(inc, src.is_dir(), user_matcher):
            continue
        dst = publish_dir / inc

        if src.is_symlink():
            continue
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except OSError as e:
                # Fail closed, cleanly: a manifest file we cannot copy must
                # block the publish, not traceback past it.
                raise PublishError(f"ERROR: cannot copy '{inc}': {e}") from e
            copied.append(inc)
        elif src.is_dir():
            try:
                shutil.copytree(
                    src, dst, ignore=_ignore_with_symlinks, dirs_exist_ok=True,
                )
            except (OSError, shutil.Error) as e:
                raise PublishError(f"ERROR: cannot copy '{inc}': {e}") from e
            for root, dirs, files in os.walk(dst):
                dirs[:] = [d for d in dirs if not is_hard_excluded(d)]
                for f in files:
                    if is_hard_excluded(f):
                        continue
                    rel = os.path.relpath(os.path.join(root, f), start=str(publish_dir))
                    copied.append(rel)
        else:
            ui.warn(f"WARNING: {inc} is neither file nor directory, skipping")

    return sorted(set(copied))  # L8: overlapping includes must not double-count


def apply_transform_rules(content: str, rules: list[dict],
                          warn: Callable | None = None) -> str:
    """Apply transform rules to content — the single implementation (L1)
    behind the real apply path and both simulations (scrub preview,
    wanted-files hashing), which previously had three drifting copies.

    Each rule is either:
      {find: str, replace: str}          — literal replacement, all occurrences
      {strip_between: [start, end]}      — remove start marker through end
                                           marker inclusive, all occurrences

    warn(msg), when supplied, receives no-match and orphaned-marker
    diagnostics; the real apply path passes it, simulations stay quiet.
    An orphaned start marker (no end marker) leaves content in place —
    acceptable only because the scrub gate backstops it (pinned by test).
    """
    for i, rule in enumerate(rules):
        if "strip_between" in rule:
            start_marker, end_marker = rule["strip_between"]
            count = 0
            orphaned = False
            while True:
                start_idx = content.find(start_marker)
                if start_idx == -1:
                    break
                end_idx = content.find(end_marker, start_idx + len(start_marker))
                if end_idx == -1:
                    orphaned = True
                    if warn:
                        warn(f"rule {i+1}: end marker not found for occurrence {count+1}: {end_marker!r}")
                    break
                content = content[:start_idx] + content[end_idx + len(end_marker):]
                count += 1
            if count == 0 and not orphaned and warn:
                warn(f"rule {i+1}: start marker not found: {start_marker!r}")
        else:
            find_str = rule["find"]
            replace_str = rule["replace"]
            if find_str in content:
                content = content.replace(find_str, replace_str)
            elif warn:
                warn(f"rule {i+1}: no match for: {find_str!r}")
    return content


def apply_transforms(publish_dir: Path, transforms: dict) -> list[str]:
    """Apply transforms to specific files after copy.

    Returns list of filenames that were modified.
    """
    transformed = []
    for filename, rules in transforms.items():
        target = publish_dir / filename
        if not target.resolve().is_relative_to(publish_dir.resolve()):
            raise ConfigError(f"ERROR: transform path escapes publish dir: {filename}")
        if not target.exists():
            ui.warn(f"WARNING: transform target not found: {filename}")
            continue
        if is_binary_file(target):
            ui.warn(f"WARNING: skipping binary file for transform: {filename}")
            continue
        try:
            content = target.read_text(errors="surrogateescape")
        except (IOError, OSError) as e:
            ui.warn(f"WARNING: cannot read {filename} for transform: {e}")
            continue

        def _warn(msg: str, filename: str = filename) -> None:
            ui.warn(f"WARNING: {filename} {msg}")

        new_content = apply_transform_rules(content, rules, warn=_warn)
        if new_content != content:
            try:
                target.write_text(new_content, errors="surrogateescape")
            except OSError as e:
                raise PublishError(
                    f"ERROR: cannot write transformed file '{filename}': {e}") from e
            transformed.append(filename)
    return transformed


def scrub_check(publish_dir: Path, forbidden: list[str]) -> list[dict]:
    """Check ALL text files in publish_dir for forbidden strings.

    Returns list of {"file": rel_path, "matches": [matched_strings]} or
    {"file": rel_path, "matches": [], "error": ...} for files that could
    not be verified — fail closed: an unverifiable file blocks the publish
    exactly like a forbidden-string hit (M7). Case-insensitive matching.
    No exceptions — keep files are checked too.
    """
    if not forbidden:
        return []
    results = []
    forbidden_lower = [s.lower() for s in forbidden]
    for root, dirs, files in os.walk(publish_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            full = Path(root) / f
            rel = str(full.relative_to(publish_dir))
            if is_binary_file(full):
                ui.info(f"  scrub: skipped binary file: {rel}")
                continue
            try:
                content = full.read_text(errors="surrogateescape").lower()
            except (IOError, OSError) as e:
                results.append({"file": rel, "matches": [],
                                "error": f"unreadable — could not verify ({e.__class__.__name__})"})
                continue
            ui.detail(f"scrub: {rel}: {len(forbidden)} patterns")
            matches = [
                orig for orig, low in zip(forbidden, forbidden_lower)
                if low in content
            ]
            if matches:
                results.append({"file": rel, "matches": matches})
    return sorted(results, key=lambda r: r["file"])


# === core: projection ===
# Frozen interface (§2.3, core → sync).

@dataclass(frozen=True)
class ProjEntry:
    blob: str
    mode: str
    nonderivable: bool
    keep: bool
    transformed: bool


@dataclass
class Projection:
    entries: dict[str, ProjEntry]
    dirty_paths: list[str]
    config: dict


def managed_now(rel: str, config: dict) -> bool:
    """A path the current manifest predicate matches, or a keep entry
    (design §2 "Managed path (now)").

    The regular-file half of the definition is a property of a tree entry,
    which this signature cannot see: callers pass only regular-file paths
    (100644/100755 entries, or regular worktree files).
    """
    return rel in config.get("keep", []) or _path_would_publish(rel, config)


_SOURCE_PREFIXES: dict[str, str] = {}


def _source_prefix(source_root: Path) -> str:
    """The source root's place below its repo's top level, as
    `git rev-parse --show-prefix` prints it: "" at the top level, else
    "sub/dir/" (usability #5). Asked once per source root.

    pubrepo names source paths relative to the source root. git takes
    `<rev>:<path>`, `--stdin-paths`, `--cacheinfo` and `cat-file --path=`
    relative to the top level, and prints `status` and `ls-tree
    --full-tree` paths from there: those sites send `prefix + path` and
    read results back through _top_to_source. Pathspecs, `check-attr
    --stdin`, `ls-files` and `hash-object --path=` already resolve against
    the cwd (the source root) and need neither. With "" every argv is
    unchanged."""
    key = str(Path(source_root).resolve())
    if key not in _SOURCE_PREFIXES:
        _SOURCE_PREFIXES[key] = git("rev-parse", "--show-prefix",
                                    cwd=str(source_root)).stdout.rstrip("\n")
    return _SOURCE_PREFIXES[key]


def _top_to_source(prefix: str, path: str) -> str | None:
    """A top-level-relative path git printed, made relative to the source
    root; None when it lies outside the source root (never published)."""
    return path[len(prefix):] if path.startswith(prefix) else None


def _odb_dir(repo: Path) -> Path:
    out = git("rev-parse", "--git-path", "objects", cwd=str(repo)).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else (repo / path).resolve()


def _projection_env(source_root: Path, publish_dir: Path) -> dict:
    """Git runs in the source repo, so the source's attributes and
    config drive conversion; objects are written only to publish_dir's ODB,
    and source objects stay readable through alternates."""
    env = dict(os.environ)
    env["GIT_OBJECT_DIRECTORY"] = str(_odb_dir(publish_dir))
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(_odb_dir(source_root))
    return env


def _import_blobs(source_root: Path, publish_dir: Path, blobs: set[str]) -> None:
    """Copy into publish_dir's own ODB every blob it lacks. git skips writing
    an object that an alternate already has, so under _projection_env a blob
    equal to committed source content lives only in the source ODB; commits
    and pushes in publish_dir must not depend on that alternate."""
    if not blobs:
        return
    env = {k: v for k, v in os.environ.items()
           if k not in ("GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES")}
    check = _run_git_process(["git", "cat-file", "--batch-check=%(objectname)"],
                           cwd=str(publish_dir), env=_clone_env(publish_dir, env),
                           capture_output=True, text=True,
                           input="".join(f"{b}\n" for b in sorted(blobs)))
    missing = [line.split()[0] for line in check.stdout.splitlines()
               if line.endswith(" missing")]
    if not missing:
        return
    pack = _run_git_process(["git", "pack-objects", "--stdout", "-q"], cwd=str(source_root),
                          env=env, capture_output=True,
                          input="".join(f"{b}\n" for b in missing).encode())
    if pack.returncode != 0:
        raise PublishError(f"ERROR: cannot copy projection objects into {publish_dir.name}/: "
                           f"{sanitize_provider_output(pack.stderr.decode(errors='replace').strip())}")
    unpack = _run_git_process(["git", "unpack-objects", "-q"], cwd=str(publish_dir),
                            env=_clone_env(publish_dir, env),
                            input=pack.stdout, capture_output=True)
    if unpack.returncode != 0:
        raise PublishError(f"ERROR: cannot copy projection objects into {publish_dir.name}/: "
                           f"{sanitize_provider_output(unpack.stderr.decode(errors='replace').strip())}")


def _filter_attributes(source_root: Path, paths: list[str]) -> set[str]:
    """Paths with a `filter` attribute set (git-lfs, git-crypt, custom
    clean filters). `unspecified` and `unset` mean no filter."""
    if not paths:
        return set()
    result = _run_git_process(
        ["git", "check-attr", "-z", "--stdin", "filter"],
        cwd=str(source_root), input="\0".join(paths).encode(),
        capture_output=True)
    if result.returncode != 0:
        raise PublishError("ERROR: git check-attr failed: "
                           + sanitize_provider_output(result.stderr.decode(errors="replace").strip()))
    fields = result.stdout.decode(errors="surrogateescape").split("\0")
    filtered = set()
    for i in range(0, len(fields) - 2, 3):
        path, _attr, value = fields[i:i + 3]
        if value not in ("unspecified", "unset"):
            filtered.add(path)
    return filtered


def _hash_paths(source_root: Path, env: dict, paths: list[str], *,
                no_filters: bool) -> dict[str, str]:
    """`hash-object -w` for worktree files named relative to the source
    root, batched through --stdin-paths (a name containing a newline is
    hashed on its own). Conversion follows each path's attributes unless
    no_filters is set."""
    if not paths:
        return {}
    base = ["git", "hash-object", "-w"] + (["--no-filters"] if no_filters else [])
    batch = [p for p in paths if "\n" not in p]
    blobs: dict[str, str] = {}
    if batch:
        prefix = _source_prefix(source_root)    # --stdin-paths is top-level-relative (#5)
        result = _run_git_process(base + ["--stdin-paths"], cwd=str(source_root), env=env,
                                input="\n".join(prefix + p for p in batch) + "\n",
                                capture_output=True, text=True, errors="surrogateescape")
        if result.returncode != 0:
            unreadable = [p for p in batch if not os.access(source_root / p, os.R_OK)]
            what = repr(unreadable[0]) if unreadable else "included files"
            raise PublishError(f"ERROR: cannot read {what} for hashing: "
                               f"{sanitize_provider_output(result.stderr.strip())}")
        blobs.update(zip(batch, result.stdout.split()))
    for path in paths:
        if "\n" in path:
            result = _run_git_process(base + ["--", path], cwd=str(source_root), env=env,
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise PublishError(f"ERROR: cannot read {path!r} for hashing: "
                                   f"{sanitize_provider_output(result.stderr.strip())}")
            blobs[path] = result.stdout.strip()
    return blobs


def _cat_blob(repo: Path, env: dict | None, blob: str) -> bytes:
    result = _run_git_process(["git", "cat-file", "blob", blob], cwd=str(repo), env=env,
                            capture_output=True)
    if result.returncode != 0:
        raise PublishError(f"ERROR: cannot read blob {blob}: "
                           + sanitize_provider_output(result.stderr.decode(errors="replace").strip()))
    return result.stdout


def _hash_bytes(repo: Path, env: dict, data: bytes, *, write: bool,
                path: str | None = None) -> str:
    """hash-object --stdin. With `path`, that path's clean conversion
    applies; without it, the bytes are hashed as given (--no-filters)."""
    argv = ["git", "hash-object", "--stdin"]
    if write:
        argv.append("-w")
    argv += [f"--path={path}"] if path is not None else ["--no-filters"]
    result = _run_git_process(argv, cwd=str(repo), env=env, input=data, capture_output=True)
    if result.returncode != 0:
        raise PublishError("ERROR: git hash-object failed: "
                           + sanitize_provider_output(result.stderr.decode(errors="replace").strip()))
    return result.stdout.decode().strip()


def _transform_bytes(path: str, data: bytes, rules: list[dict]) -> bytes | None:
    """Transformed bytes, or None when the target is binary (it is then
    published untransformed, with a warning, as apply_transforms does)."""
    if b"\x00" in data[:8192]:
        ui.warn(f"WARNING: skipping binary file for transform: {path}")
        return None

    def _warn(msg: str) -> None:
        ui.warn(f"WARNING: {path} {msg}")

    text = data.decode(errors="surrogateescape")
    return apply_transform_rules(text, rules, warn=_warn).encode(errors="surrogateescape")


def _file_mode(path: Path) -> str:
    return "100755" if path.stat().st_mode & 0o111 else "100644"


def _walk_includes(source_root: Path, config: dict) -> list[str]:
    """The included regular files, relative to the source root: the same
    selection copy_includes makes (hard excludes, user excludes beating
    includes, default excludes below directory entries, .env rules), and
    no symlinks."""
    excludes = config.get("exclude", [])
    warned_nested: set[str] = set()

    def _warn_nested_git(rel_dir: str) -> None:
        if rel_dir not in warned_nested:
            warned_nested.add(rel_dir)
            ui.warn(f"WARNING: nested git repo at '{rel_dir}': its files WILL be published "
                    f"but its history will not; add '{rel_dir}/' to exclude if unintended")

    ignore_fn = make_ignore_fn(source_root, excludes, on_nested_git=_warn_nested_git)
    user_matcher = compile_patterns(excludes)
    found: set[str] = set()
    for inc in config["include"]:
        if _include_is_hard_excluded(inc):
            continue
        src = source_root / inc
        if src.is_symlink() or _include_matches_excludes(inc, src.is_dir(), user_matcher):
            continue
        if src.is_file():
            found.add(posixpath.normpath(inc))
        elif src.is_dir():
            for root, dirs, files in os.walk(src):
                ignored = ignore_fn(root, dirs + files)
                dirs[:] = [d for d in dirs
                           if d not in ignored and not os.path.islink(os.path.join(root, d))]
                for name in files:
                    full = os.path.join(root, name)
                    if name in ignored or os.path.islink(full) or not os.path.isfile(full):
                        continue
                    found.add(Path(os.path.relpath(full, start=str(source_root))).as_posix())
    return sorted(found)


def _source_dirty_paths(source_root: Path, paths: set[str]) -> list[str]:
    """Projected paths whose published bytes cannot come from HEAD: tracked
    paths with staged or unstaged changes, and paths git does not track
    (untracked or ignored files inside include directories)."""
    tracked = git("ls-files", "-z", cwd=str(source_root)).stdout.split("\0")
    changed = git("status", "--porcelain=v1", "-z", "--untracked-files=no",
                  cwd=str(source_root)).stdout.split("\0")
    prefix = _source_prefix(source_root)    # status prints top-level-relative paths (#5)
    dirty = set(paths) - set(tracked)
    records = iter(changed)
    for record in records:
        if len(record) < 4:
            continue
        status, path = record[:2], _top_to_source(prefix, record[3:])
        if status[0] in "RC":
            next(records, None)  # the rename source follows
        if path in paths:
            dirty.add(path)
    return sorted(dirty)


def build_projection(source_root: Path, publish_dir: Path, config: dict) -> Projection:
    """Π live (design §2).

    Pre: config is valid; publish_dir is a git repo.
    Post: every managed path hashed in the source repo with objects written
    to publish_dir's ODB: clean-convert (`--path`, or `--no-filters` plus
    nonderivable for a filter attribute), then transforms, then
    `hash-object -w --stdin --no-filters`. Keep paths are hashed from
    `.publish/<P>`. No worktree writes. An unreadable included file raises
    PublishError (exit 2); an unreadable keep file raises ScrubFailure (4).
    """
    return _build_projection(source_root, publish_dir, config)


def _build_projection(source_root: Path, publish_dir: Path, config: dict, *,
                      unreadable: list[tuple[str, str]] | None = None) -> Projection:
    """build_projection. With `unreadable` (previews), a path that
    cannot be read is skipped and appended as (path, reason) instead of
    raising, so the preview can list it as "could not verify"."""
    env = _projection_env(source_root, publish_dir)
    keep = [k for k in config.get("keep", []) if isinstance(k, str) and not is_hard_excluded(k)]
    included = [p for p in _walk_includes(source_root, config) if p not in keep]
    if unreadable is not None:
        for path in [p for p in included if not os.access(source_root / p, os.R_OK)]:
            ui.warn(f"WARNING: cannot read '{path}' for hashing (PermissionError)")
            unreadable.append((path, "unreadable — could not verify (PermissionError)"))
            included.remove(path)
    filtered = _filter_attributes(source_root, included)
    blobs = _hash_paths(source_root, env, [p for p in included if p not in filtered],
                        no_filters=False)
    blobs.update(_hash_paths(source_root, env, sorted(filtered), no_filters=True))
    transforms = config.get("transforms", {})
    entries: dict[str, ProjEntry] = {}
    for path in included:
        blob, transformed = blobs[path], False
        if path in transforms:
            new = _transform_bytes(path, _cat_blob(source_root, env, blob), transforms[path])
            if new is not None:
                try:
                    blob, transformed = _hash_bytes(source_root, env, new, write=True), True
                except (OSError, PublishError) as e:
                    raise PublishError(f"ERROR: cannot write transformed file {path!r}: {e}") from e
        entries[path] = ProjEntry(blob=blob, mode=_file_mode(source_root / path),
                                  nonderivable=path in filtered, keep=False,
                                  transformed=transformed)
    for name in keep:
        kept = publish_dir / name
        if kept.is_symlink() or not kept.is_file():
            continue
        try:
            data = kept.read_bytes()
        except OSError as e:     # fail closed: an unreadable keep file is never published
            why = f"unreadable — could not verify ({e.__class__.__name__})"
            if unreadable is not None:
                unreadable.append((name, why))
                continue
            raise ScrubFailure([{"file": name, "matches": [], "error": why}],
                               f"\nSCRUB FAILED — keep file {name}: {why}") from e
        blob = _hash_bytes(source_root, env, data, write=True, path=name)
        entries[name] = ProjEntry(blob=blob, mode=_file_mode(kept), nonderivable=False,
                                  keep=True, transformed=False)
    _import_blobs(source_root, publish_dir, {e.blob for e in entries.values()})
    return Projection(entries=entries,
                      dirty_paths=_source_dirty_paths(source_root, set(included)),
                      config=config)


def _config_at(source_root: Path, sigma: str, config_relpath: str) -> dict | None:
    shown = git("show", f"{sigma}:{_source_prefix(source_root)}{config_relpath}",
                cwd=str(source_root), check=False)
    if shown.returncode != 0:
        return None
    try:
        config = tomllib.loads(shown.stdout).get("publish", {})
    except tomllib.TOMLDecodeError:
        return None
    if not isinstance(config, dict):
        return None
    config.setdefault("branch", "main")
    config.setdefault("dir", PUBLISH_DIR_NAME)
    config.setdefault("exclude", [])
    config.setdefault("keep", [])
    if [f for f in validate_config(config) if f["level"] == "error"]:
        return None
    return config


def projection_at(source_root: Path, publish_dir: Path, sigma: str,
                  config_relpath: str) -> Projection | None:
    """Π(σ) from git objects at σ, config read at σ.

    Post: None if `σ:<config_relpath>` is missing, does not parse, or is not
    a valid config. Otherwise every 100644/100755 entry of σ the config at σ
    would publish. Its blob is `σ:P`, which is already normalized; a
    transformed path's result is written to publish_dir's ODB with
    `hash-object -w --stdin --no-filters`. Keep paths are excluded, because
    they are verified separately (design §3.1).
    """
    config = _config_at(source_root, sigma, config_relpath)
    if config is None:
        return None
    env = _projection_env(source_root, publish_dir)
    listing = git("ls-tree", "-r", "-z", "--full-tree", sigma, cwd=str(source_root))
    prefix = _source_prefix(source_root)    # --full-tree paths are top-level-relative (#5)
    keep = set(config.get("keep", []))
    transforms = config.get("transforms", {})
    entries: dict[str, ProjEntry] = {}
    for record in listing.stdout.split("\0"):
        if not record:
            continue
        meta, top_path = record.split("\t", 1)
        mode, _kind, blob = meta.split()
        path = _top_to_source(prefix, top_path)
        if (path is None or mode not in ("100644", "100755") or path in keep
                or not _path_would_publish(path, config)):
            continue
        transformed = False
        if path in transforms:
            new = _transform_bytes(path, _cat_blob(source_root, None, blob), transforms[path])
            if new is not None:
                blob, transformed = _hash_bytes(source_root, env, new, write=True), True
        entries[path] = ProjEntry(blob=blob, mode=mode, nonderivable=False, keep=False,
                                  transformed=transformed)
    return Projection(entries=entries, dirty_paths=[], config=config)


def nonderivable_paths(live: Projection, at_sigma: Projection | None) -> list[str]:
    """Paths whose live blob is not Π(σ)'s, or absent from Π(σ), plus every
    path with a filter attribute. Keep paths are excluded: Π(σ) never
    contains them, and they are verified against `.publish/` instead."""
    sigma_entries = at_sigma.entries if at_sigma is not None else {}
    return sorted(
        path for path, entry in live.entries.items()
        if not entry.keep and (entry.nonderivable or path not in sigma_entries
                               or sigma_entries[path].blob != entry.blob))


def project_blob(source_root: Path, publish_dir: Path, path: str, blob: str,
                 config: dict) -> ProjEntry | None:
    """Project one blob for `path` under `config`.

    `blob` is read from publish_dir's ODB, or from the source's through
    alternates (the projection environment). Nothing is written: the result is
    compared only, so `pull --plan` leaves no loose objects in `.publish`.

    Post: None when `path` would not be projected. Otherwise the blob as
    checkout-index would smudge it into the source worktree, re-hashed per
    §2 Π; a transformed path instead hashes the transform of the blob's
    normalized bytes (the bytes Π's clean step yields for a file
    without a filter attribute). Raises AssertionError for a path with a
    `filter` attribute: pull refuses those first (design §5.1 item 5), so
    reaching here is a defect.
    """
    if is_hard_excluded(posixpath.basename(path)) or not managed_now(path, config):
        return None
    if _filter_attributes(source_root, [path]):
        raise AssertionError(f"project_blob: {path!r} has a filter attribute")
    env = _projection_env(source_root, publish_dir)
    keep = path in config.get("keep", [])
    rules = config.get("transforms", {}).get(path)
    if rules and not keep:
        new = _transform_bytes(path, _cat_blob(source_root, env, blob), rules)
        if new is not None:
            return ProjEntry(blob=_hash_bytes(source_root, env, new, write=False),
                             mode="100644", nonderivable=False, keep=False, transformed=True)
    # cat-file --path= is top-level-relative, unlike hash-object's (#5).
    smudged = _run_git_process(["git", "cat-file", "--filters",
                                f"--path={_source_prefix(source_root)}{path}", blob],
                             cwd=str(source_root), env=env, capture_output=True)
    if smudged.returncode != 0:
        raise PublishError(f"ERROR: cannot read blob {blob} for {path}: "
                           + sanitize_provider_output(smudged.stderr.decode(errors="replace").strip()))
    return ProjEntry(blob=_hash_bytes(source_root, env, smudged.stdout, write=False, path=path),
                     mode="100644", nonderivable=False, keep=keep, transformed=False)


# === core: gate ===
# Frozen interface (§2.3, core → sync).

@dataclass
class GateFinding:
    path: str
    part: str | None
    kind: str           # forbidden | marker | unscannable | corrupt | limit | cfb
    detail: str
    allowlisted: bool


@dataclass
class GateResult:
    findings: list[GateFinding]

    def blocking(self) -> list[GateFinding]:
        """Non-allowlisted findings; empty for a carried scan (design §4.6.6)."""
        return [f for f in self.findings if not f.allowlisted]

    def release_blocking(self, accepted_unscanned: set[str]) -> list[GateFinding]:
        """Non-allowlisted findings including carried ones.
        `accepted_unscanned` clears only unscannable/cfb findings at those
        paths and never clears `forbidden` (design §4.6 item 6)."""
        return [f for f in self.findings
                if not f.allowlisted
                and not (f.kind in ("unscannable", "cfb") and f.path in accepted_unscanned)]


class _CarriedGateResult(GateResult):
    """A scan of carried content (design §4.6 item 6): reported, never
    blocking publish; release_blocking still sees every finding."""

    def blocking(self) -> list[GateFinding]:
        return []


# Zip-bomb and nesting limits for the OOXML walk. Exceeding one
# is a `limit` finding, which blocks (exit 4).
OOXML_MAX_DEPTH = 3
OOXML_MAX_MEMBERS = 10_000
OOXML_MAX_EXPANDED = 512 * 1024 * 1024

_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06")
_CFB_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_XML_PART_SUFFIXES = (".xml", ".rels")
_CFB_DETAIL = "encrypted or legacy Office container"


class _GateLimit(Exception):
    pass


def _finding(name: str, kind: str, detail: str, allowlisted: bool = False) -> GateFinding:
    path, _sep, part = name.partition("!")
    return GateFinding(path=path, part=part or None, kind=kind, detail=detail,
                       allowlisted=allowlisted)


def _glob_any(patterns: list[str], name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _opaque_finding(name: str, kind: str, detail: str, config: dict) -> GateFinding:
    """Item 3: an unscannable binary blocks unless `allow_opaque` lists it
    (globs match archive members as `<container>!<part>`)."""
    allowed = _glob_any((config.get("scrub") or {}).get("allow_opaque", []), name)
    return _finding(name, kind, "not scanned (allowlisted)" if allowed else detail, allowed)


def _forbidden_hits(name: str, text: str, forbidden: list[str]) -> list[GateFinding]:
    lowered = text.lower()
    return [_finding(name, "forbidden", f"forbidden string {s!r}")
            for s in forbidden if s and s.lower() in lowered]


def _decode_xml(data: bytes) -> str:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="surrogateescape")
    return data.decode("utf-8", errors="surrogateescape")


def _parse_xml_text(data: bytes) -> str:
    """Character data and attribute values of an XML part, with internal
    entities resolved. External entities and DTDs are never resolved. An
    entity declaration whose expansion could exceed OOXML_MAX_EXPANDED, or
    any external entity declaration, raises _GateLimit before anything
    expands; ExpatError propagates for a part that is not well-formed."""
    parser = xml.parsers.expat.ParserCreate()
    parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
    sizes: dict[str, int] = {}
    chunks: list[str] = []
    attrs: list[str] = []
    total = [0]

    def entity_decl(name, _is_param, value, _base, system_id, _public_id, _notation):
        if value is None:
            raise _GateLimit(f"declares external entity {name!r} ({system_id or 'no system id'})")
        size = len(value) + sum(sizes.get(ref, 0) for ref in re.findall(r"&([\w.:-]+);", value))
        sizes[name] = size
        if size > OOXML_MAX_EXPANDED:
            raise _GateLimit(f"entity {name!r} expands beyond {OOXML_MAX_EXPANDED} bytes")

    def character_data(text):
        total[0] += len(text)
        if total[0] > OOXML_MAX_EXPANDED:
            raise _GateLimit(f"text expands beyond {OOXML_MAX_EXPANDED} bytes")
        chunks.append(text)

    def start_element(_tag, attributes):
        attrs.extend(attributes.values())

    parser.EntityDeclHandler = entity_decl
    parser.CharacterDataHandler = character_data
    parser.StartElementHandler = start_element
    parser.Parse(data, True)
    return "".join(chunks) + "\n" + "\n".join(attrs)


def _scan_xml_part(name: str, data: bytes, forbidden: list[str]) -> list[GateFinding]:
    """XML parts are scanned twice (design §4.6 item 2): the full decoded
    text, which sees comments and CDATA, and the parsed nodes and
    attributes, which see entity-split strings."""
    findings = _forbidden_hits(name, _decode_xml(data), forbidden)
    try:
        parsed = _parse_xml_text(data)
    except _GateLimit as exc:
        return findings + [_finding(name, "limit", str(exc))]
    except xml.parsers.expat.ExpatError as exc:
        return findings + [_finding(name, "corrupt", f"part is not well-formed XML: {exc}")]
    seen = {f.detail for f in findings}
    return findings + [f for f in _forbidden_hits(name, parsed, forbidden) if f.detail not in seen]


def _scan_container(name: str, data: bytes, forbidden: list[str], config: dict,
                    depth: int, budget: list[int]) -> list[GateFinding]:
    """Walk a zip in memory: never extracted to disk, member names never
    used as paths. Top-level zips that are not OOXML are opaque (item 3);
    zips nested in a container are recursed into (item 2)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, ValueError) as exc:
        return [_finding(name, "corrupt", f"corrupt container: {exc}")]
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        names = {info.filename for info in infos}
        if depth == 0 and "[Content_Types].xml" not in names:
            return [_opaque_finding(name, "unscannable", "binary content cannot be scanned",
                                    config)]
        if depth >= OOXML_MAX_DEPTH:
            return [_finding(name, "limit", f"containers nested deeper than {OOXML_MAX_DEPTH}")]
        if len(infos) > OOXML_MAX_MEMBERS:
            return [_finding(name, "limit", f"more than {OOXML_MAX_MEMBERS} members")]
        findings: list[GateFinding] = []
        for info in sorted(infos, key=lambda i: i.filename):
            member = f"{name}!{info.filename}"
            if info.flag_bits & 0x1:
                findings.append(_opaque_finding(member, "unscannable", "encrypted member",
                                                config))
                continue
            if info.file_size > budget[0]:
                return findings + [_finding(member, "limit",
                                            f"expands beyond {OOXML_MAX_EXPANDED} bytes")]
            try:
                with archive.open(info) as handle:
                    content = handle.read(budget[0] + 1)
            except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
                findings.append(_finding(member, "corrupt", f"cannot read member: {exc}"))
                continue
            budget[0] -= len(content)
            if budget[0] < 0:
                return findings + [_finding(member, "limit",
                                            f"expands beyond {OOXML_MAX_EXPANDED} bytes")]
            if info.filename.lower().endswith(_XML_PART_SUFFIXES):
                findings += _scan_xml_part(member, content, forbidden)
            else:
                findings += _scan(member, content, forbidden, config, depth + 1, budget,
                                  markers=False)
        return findings


def _scan(name: str, data: bytes, forbidden: list[str], config: dict, depth: int,
          budget: list[int], *, markers: bool) -> list[GateFinding]:
    if data[:4] in _ZIP_MAGICS:
        return _scan_container(name, data, forbidden, config, depth, budget)
    if data[:8] == _CFB_MAGIC:
        return [_opaque_finding(name, "cfb", _CFB_DETAIL, config)]
    if b"\x00" in data[:8192]:
        return [_opaque_finding(name, "unscannable", "binary content cannot be scanned", config)]
    text = data.decode(errors="surrogateescape")
    findings = _forbidden_hits(name, text, forbidden)
    lines = has_marker_triple(text) if markers else []
    if lines:
        path = name.partition("!")[0]
        allowed = _glob_any((config.get("scrub") or {}).get("allow_markers", []), path)
        findings.append(_finding(name, "marker",
                                 "conflict markers at line(s) " + ", ".join(map(str, lines)),
                                 allowed))
    return findings


def scan_bytes(name: str, data: bytes, forbidden: list[str], config: dict,
               depth: int = 0) -> list[GateFinding]:
    """The gate's pure recursion core (design §4.6 items 1–5): text, OOXML
    and nested containers, CFB, other binaries, conflict markers. `name` is
    the path, or `<container>!<part>` for an archive member."""
    return _scan(name, data, forbidden, config, depth, [OOXML_MAX_EXPANDED], markers=True)


def _read_blobs(publish_dir: Path, blobs: list[str]) -> dict[str, bytes]:
    """Blob contents from publish_dir's ODB in one `cat-file --batch`."""
    if not blobs:
        return {}
    result = _run_git_process(["git", "cat-file", "--batch"], cwd=str(publish_dir),
                            env=_clone_env(publish_dir),
                            input="".join(f"{b}\n" for b in blobs).encode(), capture_output=True)
    if result.returncode != 0:
        raise PublishError("ERROR: git cat-file --batch failed: "
                           + sanitize_provider_output(result.stderr.decode(errors="replace").strip()))
    out, pos, contents = result.stdout, 0, {}
    for _ in blobs:
        end = out.index(b"\n", pos)
        header = out[pos:end].decode().split()
        if header[-1] == "missing":
            raise PublishError(f"ERROR: blob {header[0]} is missing from {publish_dir}")
        size = int(header[2])
        contents[header[0]] = out[end + 1:end + 1 + size]
        pos = end + 1 + size + 1
    return contents


def gate_scan(publish_dir: Path, entries: Iterable[tuple[str, str]], config: dict,
              *, carried: bool = False) -> GateResult:
    """Outbound gate over (path, blob) pairs (design §4.6).

    Pre: the blobs exist in publish_dir.
    Post: one GateFinding per hit. With `carried`, blocking() is empty:
    carried content never blocks publish, and release_blocking() still
    reports it.
    """
    pairs = list(entries)
    forbidden = [s for s in (config.get("scrub") or {}).get("forbidden") or []
                 if isinstance(s, str) and s]
    contents = _read_blobs(publish_dir, sorted({blob for _path, blob in pairs}))
    findings: list[GateFinding] = []
    for path, blob in sorted(pairs):
        findings += scan_bytes(path, contents[blob], forbidden, config)
    return (_CarriedGateResult if carried else GateResult)(findings=findings)


def scan_shipped_files(root: Path, config: dict, *, prefix: str = "",
                       skip_digests: frozenset = frozenset()) -> list[GateFinding]:
    """The release scrub over files on disk (design §8.6 "every shipped
    byte", §4.6 items 1-5; AC71): each regular file under `root`, named
    `<prefix><relpath>`. A distribution's members use the prefix
    "<dist>!", so allow_opaque globs match them as archive members; a
    member whose sha256 is in `skip_digests` is a byte-identical copy of
    an export file, already scanned under its repo path."""
    forbidden = [x for x in (config.get("scrub") or {}).get("forbidden") or []
                 if isinstance(x, str) and x]
    findings: list[GateFinding] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        data = path.read_bytes()
        if skip_digests and hashlib.sha256(data).hexdigest() in skip_digests:
            continue
        findings += scan_bytes(prefix + path.relative_to(root).as_posix(), data, forbidden,
                               config)
    return findings


def release_scrub_refusal(where: str, findings: list[GateFinding]) -> None:
    """Exit 4 (AC71) when the release scrub finds anything not allowlisted;
    nothing has been pushed, tagged or uploaded at this point."""
    blocking = [f for f in findings if not f.allowlisted]
    if blocking:
        detail = "\n".join(f"  {_finding_label(f)}: {f.detail}" for f in blocking)
        raise ScrubFailure(
            [{"file": f.path, "part": f.part, "kind": f.kind, "detail": f.detail}
             for f in blocking],
            f"ERROR: release scrub failed in {where}:\n{detail}\n"
            f"Nothing was pushed, tagged or uploaded.")


@dataclass(frozen=True)
class MessageScrub:
    """The forbidden strings in a user-originated commit
    message, as a count and line numbers only."""
    count: int              # distinct forbidden entries matched
    lines: tuple[int, ...]  # sorted 1-based start lines of every match


def message_scrub(text: str | None, config: dict) -> MessageScrub:
    """`_forbidden_hits` matching (case-insensitive substring;
    non-str and empty entries skipped) over `text` as given; 1.1 commits
    with `commit-tree -m`, which stores it verbatim. MessageScrub(0, ())
    when clean, when text is None/"" or when there are no usable entries."""
    if not text:
        return MessageScrub(0, ())
    lowered = text.lower()
    hits: list[str] = []
    lines: set[int] = set()
    for entry in (config.get("scrub") or {}).get("forbidden") or []:
        if not isinstance(entry, str) or not entry or entry in hits:
            continue
        needle = entry.lower()
        start = lowered.find(needle)
        if start >= 0:
            hits.append(entry)
        while start >= 0:
            lines.add(lowered.count("\n", 0, start) + 1)
            start = lowered.find(needle, start + 1)
    return MessageScrub(len(hits), tuple(sorted(lines)))


def message_scrub_text(what: str, scrub: MessageScrub) -> str:
    """`<what> contains <N> forbidden string(s) on line(s) <L…>.`"""
    where = ", ".join(str(n) for n in scrub.lines)
    return f"{what} contains {scrub.count} forbidden string(s) on line(s) {where}."


def message_scrub_refusal(what: str, scrub: MessageScrub, remedy: str, *,
                          after_fetch: bool = False) -> ScrubFailure:
    """Exit 4 for a commit message that holds a forbidden string. The text
    names the count and the lines, never the strings. `after_fetch`: the
    refusal follows a fetch, which moved the remote-tracking refs,
    so it claims only that nothing was rebuilt, committed or
    pushed."""
    nothing = ("Nothing was rebuilt, committed or pushed." if after_fetch
               else "Nothing was published or changed.")
    return ScrubFailure([{"message": what, "count": scrub.count, "lines": list(scrub.lines)}],
                        "SCRUB FAILED — " + message_scrub_text(what, scrub)
                        + f"\n{nothing} " + remedy)


MESSAGE_REMEDY = "Edit the -m text and run again."
REUSED_TITLE_REMEDY = "Pass -m with a new title, or publish a new commit (omit --amend)."


def has_marker_triple(text: str) -> list[int]:
    """1-based line numbers where an ordered conflict starts: `<<<<<<< `,
    an optional diff3 `||||||| `, then a line that is exactly `=======`, then
    `>>>>>>> `. A bare `=======` (an RST underline) is not a conflict."""

    def marker(line: str, char: str) -> bool:
        return line == char * 7 or line.startswith(char * 7 + " ")

    starts: list[int] = []
    state, start = 0, 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip("\r")
        if marker(line, "<"):
            state, start = 1, number
        elif state == 1 and marker(line, "|"):
            state = 2
        elif state in (1, 2) and line == "=" * 7:
            state = 3
        elif state == 3 and marker(line, ">"):
            starts.append(start)
            state = 0
    return starts


def extract_text(name: str, data: bytes) -> str | None:
    """OOXML full-part text for pull's extracted-text diff: every
    XML part's parsed text, in part order. None for non-OOXML, or when the
    container cannot be read safely."""
    if data[:4] not in _ZIP_MAGICS:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = [i for i in archive.infolist() if not i.is_dir()]
            if "[Content_Types].xml" not in {i.filename for i in infos}:
                return None
            if len(infos) > OOXML_MAX_MEMBERS or \
                    sum(i.file_size for i in infos) > OOXML_MAX_EXPANDED:
                return None
            parts = []
            for info in sorted(infos, key=lambda i: i.filename):
                if info.filename.lower().endswith(_XML_PART_SUFFIXES):
                    parts.append(f"--- {info.filename}\n"
                                 + _parse_xml_text(archive.read(info)).strip())
            return "\n".join(parts) + "\n"
    except (zipfile.BadZipFile, ValueError, OSError, _GateLimit, xml.parsers.expat.ExpatError):
        return None


def get_source_info(source_root: Path) -> tuple[str, bool]:
    """Return (short_hash, is_dirty) for the source repo."""
    result = git("rev-parse", "--short", "HEAD", cwd=str(source_root), check=False)
    if result.returncode != 0:
        return ("unknown", True)
    short_hash = result.stdout.strip()

    result = git("status", "--porcelain", "--untracked-files=no", cwd=str(source_root), check=False)
    is_dirty = bool(result.stdout.strip())

    return (short_hash, is_dirty)


def build_commit_message(source_hash: str, *, custom_message: str | None = None,
                         added: int = 0, modified: int = 0, deleted: int = 0,
                         transform_count: int = 0, scrub_patterns: int = 0) -> str:
    """Build the commit message (T18; §2.3, exported to sync). These
    messages are the public repo's visible history — keep them clean. The
    'Source:' line format is FROZEN (old-repo fallback parsing depends on
    it); parsing takes the LAST Source: line, so custom titles containing
    'Source:' cannot confuse it. Design §4.3 dropped `Branch:` and
    ` (dirty)`; a dirty publish is named in the record instead.
    """
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    if custom_message:
        title = custom_message
    else:
        title = f"publish: {now}"

    return (f"{title}\n"
            f"\n"
            f"Source: {source_hash}\n"
            f"Files: {added + modified + deleted} ({added} added, {modified} modified, "
            f"{deleted} deleted)\n"
            f"Transforms: {transform_count} file(s)\n"
            f"Scrub: {scrub_patterns} pattern(s) passed\n")


def _split_tool_message(body: str) -> tuple[str, str] | None:
    """Split a commit message at the LAST 'Source:' line; None if absent.

    Returns (title_block, trailer) where title_block is everything before
    the last 'Source:' line, rstrip'd.  The same 'last Source: line' rule
    the fallback parser at get_last_publish_info uses.
    """
    lines = body.split("\n")
    last_idx = None
    for i, line in enumerate(lines):
        if line.startswith("Source:"):
            last_idx = i
    if last_idx is None:
        return None
    title_block = "\n".join(lines[:last_idx]).rstrip("\n")
    trailer = "\n".join(lines[last_idx:])
    return (title_block, trailer)


# --- release: versions ---

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\."
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def parse_semver(text: str) -> tuple[int, int, int, tuple, str | None]:
    if text.startswith("v"):
        raise ValueError(f"invalid release version '{text}': pass {text[1:]}; the tag format adds v")
    match = _SEMVER_RE.fullmatch(text)
    if not match:
        raise ValueError(f"invalid strict SemVer 2.0 version: {text!r}")
    prerelease = tuple(match.group(4).split(".")) if match.group(4) else ()
    return int(match.group(1)), int(match.group(2)), int(match.group(3)), prerelease, match.group(5)


def semver_key(value) -> tuple:
    parsed = parse_semver(value) if isinstance(value, str) else value
    major, minor, patch, prerelease, _build = parsed
    if not prerelease:
        pre_key = ((2, ""),)
    else:
        pre_key = tuple((0, int(item)) if item.isdigit() else (1, item)
                        for item in prerelease)
    return major, minor, patch, 1 if not prerelease else 0, pre_key


def is_stable(value) -> bool:
    parsed = parse_semver(value) if isinstance(value, str) else value
    return not parsed[3]


def bump_version(base: str, part: str) -> str:
    major, minor, patch, prerelease, _build = parse_semver(base)
    if prerelease:
        raise ValueError("bump baseline must be a stable managed version")
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ValueError("bump must be patch, minor, or major")


def read_version_from_text(text: str, pattern: str) -> str:
    compiled = re.compile(pattern, re.MULTILINE)
    matches = list(compiled.finditer(text))
    if len(matches) != 1:
        raise ValueError(f"version_pattern matched {len(matches)} times; expected exactly one")
    try:
        value = matches[0].group("version")
    except (IndexError, KeyError) as exc:
        raise ValueError("version_pattern must contain exactly one named group 'version'") from exc
    parse_semver(value)
    return value


def render_tag(tag_format: str, version: str) -> str:
    return tag_format.replace("{version}", version)


def tag_version(tag_format: str, tag_name: str) -> str | None:
    if tag_format.count("{version}") != 1:
        return None
    before, after = tag_format.split("{version}")
    match = re.fullmatch(re.escape(before) + r"(.+)" + re.escape(after), tag_name)
    if not match:
        return None
    try:
        parse_semver(match.group(1))
    except ValueError:
        return None
    return match.group(1)


def pep440_prerelease(version: str) -> str:
    major, minor, patch, prerelease, build = parse_semver(version)
    if build:
        raise ValueError(f"SemVer {version} contains build metadata, which does not map safely to one PyPI identity")
    if not prerelease:
        return version
    if len(prerelease) == 2 and prerelease[0] in ("alpha", "beta", "rc") \
            and prerelease[1].isdigit():
        marker = {"alpha": "a", "beta": "b", "rc": "rc"}[prerelease[0]]
        return f"{major}.{minor}.{patch}{marker}{int(prerelease[1])}"
    raise ValueError(
        f"SemVer {version} is not PyPI-mappable; expected -alpha.N, -beta.N, or -rc.N")


# --- release: ledger ---

@dataclass
class Ledger:
    """The public release ledger (design §8.5). `managed`: release
    tags pubrepo owns, name → peeled target; only these set the version base,
    are compared with local tags and must stay ancestors of the branch.
    `foreign`: version-shaped tags that are not managed (reported only; a
    squatted release name still refuses with 7). `all_tags`: every remote
    tag, name → peeled target (any-tag amend protection, squats)."""
    managed: dict[str, str]
    all_tags: dict[str, str]
    latest: str | None
    latest_stable: str | None
    _tag_format: str = "v{version}"
    foreign: dict[str, str] = field(default_factory=dict)
    _adopt: frozenset[str] = frozenset()
    _source_root: Path | None = None


RELEASE_TRAILER = "Pubrepo-Release"
RELEASE_SOURCE_TRAILER = "Pubrepo-Source"


def release_tag_message(version: str, sigma_full: str) -> str:
    """An annotated release tag's message: the marker trailers make it
    managed (design §8.5). `sigma_full` is the full source commit sha."""
    return (f"Release {version}\n\n{RELEASE_TRAILER}: {version}\n"
            f"{RELEASE_SOURCE_TRAILER}: {sigma_full}\n")


def _release_trailers(message: str) -> tuple[str | None, str | None]:
    rel = re.findall(rf"^{RELEASE_TRAILER}: (\S+)\s*$", message, re.M)
    src = re.findall(rf"^{RELEASE_SOURCE_TRAILER}: ([0-9a-f]{{40}})\s*$", message, re.M)
    return (rel[-1] if rel else None, src[-1] if src else None)


def _parse_ls_remote_tag_objects(text: str) -> dict[str, tuple[str, str]]:
    """name → (direct object, peeled target); the two are equal for a
    lightweight tag."""
    direct: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split("\t", 1)
        if len(fields) != 2 or not fields[1].startswith("refs/tags/"):
            continue
        sha, ref = fields
        name = ref[len("refs/tags/"):]
        if name.endswith("^{}"):
            peeled[name[:-3]] = sha
        else:
            direct[name] = sha
    return {name: (sha, peeled.get(name, sha)) for name, sha in direct.items()}


def _parse_ls_remote_tags(text: str) -> dict[str, str]:
    return {name: target for name, (_obj, target) in _parse_ls_remote_tag_objects(text).items()}


def _fetch_tag_objects(publish_dir: Path, wanted: dict[str, str]) -> None:
    """Make the annotated tag objects in `wanted` (name → object id) present
    locally without creating or moving any ref under refs/tags (design §8.5):
    fetch by id, no `+`, no FETCH_HEAD. A host that refuses fetch by
    id gets the fallback: a fetch into a fresh per-run namespace under
    refs/pubrepo/ledger/, deleted again afterwards. Both failing → 7."""
    missing = {n: o for n, o in wanted.items()
               if git("cat-file", "-e", o, cwd=str(publish_dir), check=False).returncode != 0}
    if not missing:
        return
    _guard_remote(publish_dir)
    by_id = _git_c("fetch", "--no-tags", "--no-write-fetch-head", "origin",
                   *sorted(set(missing.values())), cwd=publish_dir)
    if by_id.returncode == 0:
        return
    run_ns = f"refs/pubrepo/ledger/{_ledger_run_id()}"
    specs = [f"refs/tags/{n}:{run_ns}/{n}" for n in sorted(missing)]
    fallback = _git_c("fetch", "--no-tags", "--no-write-fetch-head", "origin", *specs,
                      cwd=publish_dir)
    for n in sorted(missing):
        git("update-ref", "-d", f"{run_ns}/{n}", cwd=str(publish_dir), check=False)
    if fallback.returncode != 0:
        raise ReleaseError(
            f"ERROR: release ledger unavailable: cannot read tag messages: "
            f"{sanitize_provider_output((fallback.stderr or by_id.stderr).strip())}")


def _ledger_run_id() -> str:
    """A fresh per-run namespace id for the ledger fallback fetch."""
    return f"{os.getpid()}-{os.urandom(6).hex()}"


def _is_managed_tag(publish_dir: Path, source_root: Path | None, name: str, version: str,
                    obj: str, target: str, adopt: frozenset[str]) -> bool:
    """§8.5: listed in adopt_tags, or an annotated tag whose message carries
    `Pubrepo-Release: <version>` and `Pubrepo-Source: <full sha>` and whose
    target is an ours-snapshot (classify_commit). Lightweight → foreign
    unless adopted."""
    if name in adopt:
        return True
    if obj == target or source_root is None:
        return False
    body = git("cat-file", "tag", obj, cwd=str(publish_dir), check=False)
    if body.returncode != 0:
        return False
    release, sigma = _release_trailers(body.stdout)
    if release != version or sigma is None:
        return False
    try:
        return classify_commit(source_root, publish_dir, target).kind != "foreign"
    except (PubrepoError, GitError):
        return False


def read_remote_ledger(publish_dir: Path, tag_format: str, *,
                       source_root: Path | None = None,
                       adopt_tags: Sequence[str] = ()) -> Ledger:
    """Design §8.5: the ledger from `ls-remote --tags`, with tag
    messages read from the tag objects fetched by id. Without `source_root`
    only adopted tags can be managed."""
    result = git("ls-remote", "--tags", "origin", cwd=str(publish_dir), check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"ERROR: release ledger unavailable: "
            f"{sanitize_provider_output(result.stderr.strip())}")
    objects = _parse_ls_remote_tag_objects(result.stdout)
    return _ledger_from_objects(publish_dir, tag_format, objects, source_root=source_root,
                                adopt_tags=adopt_tags, fetch=True)


def read_local_ledger(publish_dir: Path, tag_format: str, *,
                      source_root: Path | None = None,
                      adopt_tags: Sequence[str] = ()) -> Ledger:
    """The ledger as of the last fetch (offline status; design §4.5, INV-8):
    the tags and tag objects already in `publish_dir`, classified
    exactly as read_remote_ledger does. No network call."""
    result = git("for-each-ref", "--format=%(refname:short)%00%(objectname)%00%(*objectname)",
                 "refs/tags", cwd=str(publish_dir), check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"ERROR: cannot inspect local release tags: "
            f"{sanitize_provider_output(result.stderr.strip())}")
    objects = {}
    for line in result.stdout.splitlines():
        parts = line.split("\0")
        if len(parts) >= 3:
            objects[parts[0]] = (parts[1], parts[2] or parts[1])
    return _ledger_from_objects(publish_dir, tag_format, objects, source_root=source_root,
                                adopt_tags=adopt_tags, fetch=False)


def _ledger_from_objects(publish_dir: Path, tag_format: str,
                         objects: dict[str, tuple[str, str]], *, source_root: Path | None,
                         adopt_tags: Sequence[str], fetch: bool) -> Ledger:
    """Classify tags (name → (tag object, peeled target)) into the §8.5
    ledger. With `fetch`, annotated tag objects are fetched by id first."""
    all_tags = {name: target for name, (_obj, target) in objects.items()}
    adopt = frozenset(adopt_tags)
    shaped = {name: tag_version(tag_format, name) for name in objects}
    shaped = {name: v for name, v in shaped.items() if v is not None}
    annotated = {name: objects[name][0] for name in shaped
                 if objects[name][0] != objects[name][1] and name not in adopt}
    if fetch and annotated and source_root is not None:
        _fetch_tag_objects(publish_dir, annotated)
    managed: dict[str, str] = {}
    foreign: dict[str, str] = {}
    for name, version in shaped.items():
        obj, target = objects[name]
        if _is_managed_tag(publish_dir, source_root, name, version, obj, target, adopt):
            managed[name] = target
        else:
            foreign[name] = target
    versions = [shaped[name] for name in managed]
    latest = max(versions, key=semver_key) if versions else None
    stable = [v for v in versions if is_stable(v)]
    latest_stable = max(stable, key=semver_key) if stable else None
    return Ledger(managed, all_tags, latest, latest_stable, tag_format, foreign,
                  adopt, source_root)


def local_tag_mismatches(publish_dir: Path, ledger: Ledger) -> list[str]:
    result = git("for-each-ref", "--format=%(refname:short)%00%(objectname)%00%(*objectname)",
                 "refs/tags", cwd=str(publish_dir), check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"ERROR: cannot inspect local release tags: "
            f"{sanitize_provider_output(result.stderr.strip())}")
    local: dict[str, tuple[str, str]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\0")
        if len(parts) >= 3:
            local[parts[0]] = (parts[1], parts[2] or parts[1])
    retired = (lambda name: is_retired(ledger._source_root, name)) if ledger._source_root \
        else (lambda name: False)
    mismatches = []
    for name in sorted(set(ledger.managed) | set(local)):
        # §8.5: managed tags only; foreign and audit tags are not ledger entries.
        if name not in ledger.managed:
            version = tag_version_from_ledger(ledger, name)
            if version is None or not _is_managed_tag(
                    publish_dir, ledger._source_root, name, version, *local[name],
                    ledger._adopt):
                continue
        if retired(name):
            continue
        if name not in local:
            mismatches.append(f"managed tag {name} exists remotely but not locally")
        elif name not in ledger.managed:
            mismatches.append(f"managed tag {name} exists locally but not remotely")
        elif local[name][1] != ledger.managed[name]:
            mismatches.append(f"managed tag {name} differs locally and remotely")
    return mismatches


def tag_version_from_ledger(ledger: Ledger, name: str) -> str | None:
    """Infer the configured managed shape from an existing managed tag set.

    local_tag_mismatches normally sees a remote peer. A local-only tag cannot
    be classified without the format, so callers attach `_tag_format` below.
    """
    tag_format = getattr(ledger, "_tag_format", None)
    return tag_version(tag_format, name) if tag_format else None


def ancestry_violations(publish_dir: Path, ledger: Ledger, tip: str) -> list[str]:
    """Managed release tags whose target is not an ancestor of `tip`
    (design §8.5: exit 7 on release paths "unless retired"; a tag recorded
    by release-retire is not a violation)."""
    violations = []
    for name, sha in sorted(ledger.managed.items()):
        if ledger._source_root is not None and is_retired(ledger._source_root, name):
            continue
        result = git("merge-base", "--is-ancestor", sha, tip,
                     cwd=str(publish_dir), check=False)
        if result.returncode != 0:
            violations.append(name)
    return violations


def ahead_of_latest(publish_dir: Path, ledger: Ledger, tip: str) -> int | None:
    if ledger.latest is None:
        return None
    target = ledger.managed[render_tag(getattr(ledger, "_tag_format", "v{version}"), ledger.latest)]
    result = git("rev-list", "--count", f"{target}..{tip}", cwd=str(publish_dir), check=False)
    return int(result.stdout.strip()) if result.returncode == 0 else None


def ahead_counts(publish_dir: Path, ledger: Ledger, tip: str,
                 source_root: Path) -> tuple[int, int] | None:
    """Design §8.3: the public commits after the latest
    managed release, as (our snapshots, collaborator commits), counted
    separately (classify_commit). None before the first managed release."""
    if ledger.latest is None:
        return None
    target = ledger.managed[render_tag(getattr(ledger, "_tag_format", "v{version}"), ledger.latest)]
    result = git("rev-list", f"{target}..{tip}", cwd=str(publish_dir), check=False)
    if result.returncode != 0:
        return None
    revs = result.stdout.split()

    def ours(sha: str) -> bool:
        try:
            return classify_commit(source_root, publish_dir, sha).kind != "foreign"
        except (PubrepoError, GitError):
            return False

    snapshots = sum(1 for sha in revs if ours(sha))
    return snapshots, len(revs) - snapshots


def tags_peeling_to(ledger: Ledger, sha: str) -> list[str]:
    return sorted(name for name, target in ledger.all_tags.items() if target == sha)


# --- release: journal ---

RELEASE_DIR_NAME = "pubrepo-releases"
JOURNAL_SCHEMA_VERSION = 1


def is_interactive() -> bool:
    return bool(getattr(sys.stdin, "isatty", lambda: False)()
                and getattr(sys.stderr, "isatty", lambda: False)())


@dataclass
class Journal:
    version: str
    path: Path
    data: dict


def journal_path(publish_dir: Path, version: str) -> Path:
    parse_semver(version)
    return publish_dir / ".git" / RELEASE_DIR_NAME / f"{version}.json"


def load_journal(publish_dir: Path, version: str) -> Journal | None:
    path = journal_path(publish_dir, version)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"ERROR: cannot read release journal {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != JOURNAL_SCHEMA_VERSION:
        raise ReleaseError(f"ERROR: unsupported or corrupt release journal: {path}")
    return Journal(version, path, data)


def _all_journals(publish_dir: Path) -> list[Journal]:
    directory = publish_dir / ".git" / RELEASE_DIR_NAME
    if not directory.exists():
        return []
    journals = []
    for path in sorted(directory.glob("*.json")):
        try:
            version = path.stem
            journals.append(load_journal(publish_dir, version))
        except ValueError as exc:
            raise ReleaseError(f"ERROR: invalid release journal filename {path}: {exc}") from exc
    return [j for j in journals if j is not None]


def journal_write(journal: Journal) -> None:
    journal.path.parent.mkdir(parents=True, exist_ok=True)
    journal.data["updated"] = datetime.now().astimezone().isoformat()
    fd, tmp_name = tempfile.mkstemp(prefix=journal.path.name + ".",
                                    dir=str(journal.path.parent))
    try:
        with os.fdopen(fd, "w") as file_obj:
            json.dump(journal.data, file_obj, indent=2, sort_keys=True)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(tmp_name, journal.path)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


def journal_record(journal: Journal, step: str, *, remote: bool = False,
                   **observed) -> None:
    journal.data["in_flight"] = step
    completed = journal.data.setdefault("completed", [])
    if step not in completed:
        completed.append(step)
    if remote:
        writes = journal.data.setdefault("remote_writes", [])
        if step not in writes:
            writes.append(step)
    if observed:
        journal.data.setdefault("observed_remote", {}).update(observed)
    journal_write(journal)


def blocking_journals(publish_dir: Path) -> list[Journal]:
    return [j for j in _all_journals(publish_dir)
            if j.data.get("state", "active") in ("active", "terminal")
            and j.data.get("remote_writes")]


def _journal_identity(journal: Journal) -> tuple:
    data = journal.data
    return (journal.version, data.get("source_sha"), data.get("config_fingerprint"),
            data.get("public_candidate"), data.get("command"))


def resolve_supersession(publish_dir: Path, new_attempt: Journal) -> list[Journal]:
    """Supersede every target-local unpushed attempt; block foreign written ones."""
    unresolved = sorted(
        (j for j in _all_journals(publish_dir)
         if j.data.get("attempt_id") != new_attempt.data.get("attempt_id")
         and j.data.get("state", "active") in ("active", "terminal")),
        key=lambda j: (j.data.get("created", ""), j.version),
    )
    superseded = []
    blockers = []
    for journal in unresolved:
        if journal.data.get("remote_writes"):
            if _journal_identity(journal) != _journal_identity(new_attempt):
                blockers.append(journal)
            continue
        journal.data["state"] = "superseded"
        journal.data["superseded_by"] = new_attempt.data.get("attempt_id")
        journal_write(journal)
        superseded.append(journal)
        _append_publish_log(publish_dir, {
            "ts": datetime.now().astimezone().isoformat(),
            "event": "release_superseded", "version": journal.version,
            "attempt_id": journal.data.get("attempt_id"),
            "superseded_by": new_attempt.data.get("attempt_id"), "pushed": False,
        })
    if superseded:
        new_attempt.data.setdefault("supersedes", []).extend(
            j.data.get("attempt_id") for j in superseded)
    if blockers:
        details = ", ".join(f"{j.version} ({j.data.get('in_flight') or 'active'})"
                            for j in blockers)
        raise ReleaseError(
            f"ERROR: incomplete release journal(s) have remote writes: {details}. "
            f"Resume the identical release or run '{_PROG} release-abandon VERSION'.")
    return superseded


def config_fingerprint(config: dict) -> str:
    payload = {key: config.get(key) for key in ("remote", "branch", "dir", "release")}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def new_journal(publish_dir: Path, version: str, *, command: str,
                source_sha: str, public_baseline: str | None, tag: str,
                strategy: str, config: dict) -> Journal:
    now = datetime.now().astimezone().isoformat()
    path = journal_path(publish_dir, version)
    return Journal(version, path, {
        "schema_version": JOURNAL_SCHEMA_VERSION,
        "attempt_id": uuid.uuid4().hex,
        "config_fingerprint": config_fingerprint(config),
        "command": command,
        "source_sha": source_sha,
        "public_baseline": public_baseline,
        "public_candidate": None,
        "tag": tag,
        "strategy": strategy,
        "artifacts": [], "providers": {}, "in_flight": None,
        "completed": [], "remote_writes": [], "observed_remote": {},
        "state": "active", "terminal_reason": None,
        "created": now, "updated": now,
    })


def _abandon_local_tags(journal: Journal) -> None:
    """A tag listed in the journal's local_tags_created
    that is still local (at the recorded commit, when one is recorded) and
    absent on its remote was made by the abandoned attempt and never
    published: delete it. A tag the remote has, or whose remote cannot be
    read, is left and named."""
    kept = []
    for entry in journal.data.get("local_tags_created", []):
        repo, tag = Path(entry.get("repo", "")), entry.get("tag", "")
        remote = entry.get("remote", "origin")
        local = git("rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{}}",
                    cwd=str(repo), check=False) if tag and repo.is_dir() else None
        if local is None or local.returncode != 0:
            continue                                     # already gone
        if entry.get("sha") and local.stdout.strip() != entry["sha"]:
            ui.warn(f"WARNING: local tag {tag} in {repo.name}/ no longer points at this "
                    f"attempt's commit; left in place")
            kept.append(entry)
            continue
        listed = git("ls-remote", "--tags", remote, f"refs/tags/{tag}", cwd=str(repo),
                     check=False)
        if listed.returncode != 0:
            ui.warn(f"WARNING: cannot read {remote}'s tags; local tag {tag} in {repo.name}/ "
                    f"left in place")
            ui.detail(f"  {sanitize_provider_output(listed.stderr.strip())}")
            kept.append(entry)
            continue
        if listed.stdout.strip():
            kept.append(entry)                           # published: never touched
            continue
        git("tag", "-d", tag, cwd=str(repo), check=False)
        print(f"Removed local tag {tag} in {repo.name}/ (made by this attempt, never pushed)")
    journal.data["local_tags_created"] = kept


def cmd_release_abandon(source_root: Path, config: dict, version: str,
                        yes: bool) -> int:
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    try:
        journal = load_journal(publish_dir, version)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if journal is None:
        raise ReleaseError(f"ERROR: no local release journal for {version}")
    state = journal.data.get("state", "active")
    if state not in ("active", "terminal"):
        raise ReleaseError(f"ERROR: release journal {version} is {state}, not unresolved")
    print(f"Release {version}: {state}")
    print(f"Journal: {journal.path}")
    print(f"Command: {journal.data.get('command') or '(unknown)'}")
    print(f"Source/candidate: {journal.data.get('source_sha') or '?'} / "
          f"{journal.data.get('public_candidate') or '(not created)'}")
    print(f"Last step: {journal.data.get('in_flight') or '(none)'}")
    print("Completed: " + (", ".join(journal.data.get("completed", [])) or "(none)"))
    if journal.data.get("terminal_reason"):
        print(f"Terminal reason: {journal.data['terminal_reason']}")
    if journal.data.get("artifacts"):
        print("Artifacts: " + ", ".join(item.get("name", "?")
                                          for item in journal.data["artifacts"]))
    if journal.data.get("remote_writes"):
        print("Remote objects may remain: " + ", ".join(journal.data["remote_writes"]))
        print("Abandonment will not delete, move, or overwrite any of them.")
    if not yes:
        if not is_interactive():
            raise ConfigError("release-abandon is non-interactive; pass --yes after reviewing the journal")
        print(f"Type {version} to abandon this journal: ", end="",
              file=sys.stderr, flush=True)
        answer = input()
        if answer != version:
            raise ConfigError("release abandonment cancelled; version did not match")
    _abandon_local_tags(journal)
    journal.data["state"] = "abandoned"
    journal.data["terminal_reason"] = "abandoned by operator; remote objects were not changed"
    journal_write(journal)
    _append_publish_log(publish_dir, {
        "ts": datetime.now().astimezone().isoformat(), "event": "release_abandon",
        "version": version, "completed": journal.data.get("completed", []),
        "remote_writes": journal.data.get("remote_writes", []), "pushed": False,
    })
    ui.success(f"Release journal {version} abandoned; no remote objects were changed")
    return EXIT_SUCCESS


# --- release: choice ---

@dataclass
class ReleaseChoice:
    # "ask": no release flag on a terminal; the choice is asked with the
    # publish preview as the one confirmation (_choice_routine).
    kind: Literal["release", "bump", "none", "ask"]
    version: str | None = None
    bump: str | None = None
    from_prompt: bool = False
    # --accept-unscanned PATH (repeatable; design §4.6 item 6): carried
    # unscannable content at these paths does not block this release.
    accept_unscanned: tuple = ()
    # --accept-build-changes (design §8.3, AC72): an unattended release
    # acknowledges the build files changed since the latest managed release.
    accept_build_changes: bool = False


@dataclass
class Discovery:
    ledger: Ledger
    branch_sha: str | None
    source_version: str
    ahead: int | None
    journals: list[Journal]
    ahead_split: tuple | None = None   # (our snapshots, collaborator commits), §8.3


def _source_release_version(source_root: Path, config: dict) -> str:
    release = config["release"]
    path = source_root / release["version_file"]
    try:
        compiled = re.compile(release["version_pattern"], re.MULTILINE)
        matches = list(compiled.finditer(path.read_text()))
        if len(matches) != 1:
            raise ValueError(f"version_pattern matched {len(matches)} times; expected exactly one")
        return matches[0].group("version")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ReleaseError(f"ERROR: cannot resolve source release version from {path}: {exc}") from exc


def _remote_branch_sha(publish_dir: Path, branch: str) -> str | None:
    result = git("ls-remote", "origin", f"refs/heads/{branch}",
                 cwd=str(publish_dir), check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"ERROR: cannot discover public branch: "
            f"{sanitize_provider_output(result.stderr.strip())}")
    line = result.stdout.strip().splitlines()
    return line[0].split()[0] if line else None


def discover_release_state(publish_dir: Path, source_root: Path,
                           config: dict) -> Discovery:
    release = config["release"]
    ledger = read_remote_ledger(publish_dir, release["tag_format"], source_root=source_root,
                                adopt_tags=release.get("adopt_tags", []))
    branch_sha = _remote_branch_sha(publish_dir, config["branch"])
    ahead = ahead_of_latest(publish_dir, ledger, branch_sha) if branch_sha else None
    split = ahead_counts(publish_dir, ledger, branch_sha, source_root) if branch_sha else None
    return Discovery(ledger, branch_sha, _source_release_version(source_root, config),
                     ahead, _all_journals(publish_dir), split)


def _choice_command(choice: ReleaseChoice) -> str:
    if choice.kind == "release":
        return f"--release {choice.version}"
    if choice.kind == "bump":
        return f"--bump {choice.bump}"
    return "--no-release"


def _is_version_downgrade(discovery: Discovery) -> bool:
    """The source's version_file version is below the latest managed
    release (AC73). A version that does not parse as semver is not judged
    here; the release path reports it."""
    if discovery.ledger.latest is None:
        return False
    try:
        return semver_key(discovery.source_version) < semver_key(discovery.ledger.latest)
    except ValueError:
        return False


def warn_version_downgrade(discovery: Discovery, config: dict) -> None:
    """AC73 (design §8 item 7): on every release-enabled publish,
    --no-release included, a source version below the latest managed
    release warns, naming both versions. The exit is unchanged; a release
    of that version is still refused (7) by the release path."""
    if _is_version_downgrade(discovery):
        ui.warn(f"WARNING: version downgrade: {config['release']['version_file']} has "
                f"{discovery.source_version}, below the latest managed release "
                f"{discovery.ledger.latest}")


def prompt_release_choice(discovery: Discovery, config: dict) -> ReleaseChoice | None:
    latest = (render_tag(config["release"]["tag_format"], discovery.ledger.latest)
              if discovery.ledger.latest else "(none)")
    print("Release management is enabled.", file=sys.stderr)
    print(f"Latest managed release: {latest}", file=sys.stderr)
    snapshots, collaborators = discovery.ahead_split or (discovery.ahead or 0, 0)
    print(f"Public {config['branch']}: {snapshots} snapshot(s) of ours and {collaborators} "
          f"collaborator commit(s) since the latest managed release", file=sys.stderr)
    print(f"Source version: {discovery.source_version}\n", file=sys.stderr)
    if (discovery.ledger.latest and discovery.source_version != discovery.ledger.latest
            and not _is_version_downgrade(discovery)):
        print(f"WARNING: publishing without release will carry source version "
              f"{discovery.source_version} while latest managed release is "
              f"{discovery.ledger.latest}.\n", file=sys.stderr)
    release = config["release"]
    pypi_enabled = isinstance(release.get("pypi"), dict) and release["pypi"].get("enabled")

    def compatible(version: str) -> bool:
        try:
            parse_semver(version)
            if pypi_enabled:
                pep440_prerelease(version)
        except ValueError:
            return False
        journal_versions = {journal.version for journal in discovery.journals
                            if journal.data.get("state") in ("active", "complete", "terminal")}
        if version in journal_versions:
            return True
        if render_tag(release["tag_format"], version) in discovery.ledger.all_tags:
            return False
        return (discovery.ledger.latest is None
                or semver_key(version) > semver_key(discovery.ledger.latest))

    choices: list[tuple[str, ReleaseChoice]] = []
    if compatible(discovery.source_version):
        choices.append(
            (f"release exact {discovery.source_version}",
             ReleaseChoice("release", version=discovery.source_version, from_prompt=True)))
    if discovery.ledger.latest_stable:
        for part in ("patch", "minor", "major"):
            derived = bump_version(discovery.ledger.latest_stable, part)
            if derived == discovery.source_version and compatible(derived):
                choices.append((f"bump {part} -> {derived}",
                                ReleaseChoice("bump", version=derived, bump=part,
                                              from_prompt=True)))
    choices.append(("publish without release", ReleaseChoice("none", from_prompt=True)))
    print("Choose one (no default):", file=sys.stderr)
    for number, (label, _choice) in enumerate(choices, 1):
        print(f"  {number}. {label}", file=sys.stderr)
    cancel_number = len(choices) + 1
    print(f"  {cancel_number}. cancel", file=sys.stderr)
    while True:
        try:
            print("> ", end="", file=sys.stderr, flush=True)
            answer = input().strip()
        except EOFError:
            return None
        if not answer:
            return None                  # no default: empty cancels
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1][1]
        if answer == str(cancel_number):
            return None
        print("Choose a listed number; there is no default.", file=sys.stderr)


def resolve_release_choice(config: dict, args, publish_dir: Path, source_root: Path, *,
                           defer: bool = False,
                           cannot_release: str | None = None
                           ) -> tuple[ReleaseChoice | None, Discovery | None]:
    """The release choice for a publish (design §8.2). `defer` (prompt
    mode): with no release flag, return kind "ask"; the routine
    asks with the preview. `cannot_release` names a flag that cannot be
    combined with a release (--amend, --accept-source-divergence,
    --recreate-branch): with no release flag, the publish is a --no-release
    one, and a one-line note says so."""
    explicit = bool(args.release_version or args.bump or args.no_release)
    if not release_enabled(config):
        if explicit:
            raise ConfigError(
                "release flags require [publish.release] enabled = true; configure release management or omit the flag")
        return None, None
    if not explicit and not is_interactive():
        raise ConfigError(
            "release management requires an explicit choice in non-interactive mode:\n"
            f"  {_PROG} --bump minor\n  {_PROG} --release VERSION\n  {_PROG} --no-release")
    if args.release_version:
        try:
            parse_semver(args.release_version)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    discovery = discover_release_state(publish_dir, source_root, config)
    warn_version_downgrade(discovery, config)
    if args.release_version:
        return ReleaseChoice("release", version=args.release_version), discovery
    if args.bump:
        if discovery.ledger.latest_stable is None:
            raise ReleaseError("ERROR: no stable managed release exists; pass --release INITIAL_VERSION")
        return ReleaseChoice("bump", version=bump_version(discovery.ledger.latest_stable, args.bump),
                             bump=args.bump), discovery
    if args.no_release:
        return ReleaseChoice("none"), discovery
    if cannot_release:
        ui.info(f"Note: no release this run: {cannot_release} cannot be combined with a release")
        return ReleaseChoice("none"), discovery
    if defer:
        return ReleaseChoice("ask", from_prompt=True), discovery
    return prompt_release_choice(discovery, config), discovery


def _same_discovery(left: Discovery, right: Discovery) -> bool:
    return (left.branch_sha == right.branch_sha
            and left.ledger.managed == right.ledger.managed
            and left.ledger.all_tags == right.ledger.all_tags)


RELEASE_UNKNOWN_OFFLINE = "unknown (run with --remote)"


def _release_status_offline(source_root: Path, publish_dir: Path, config: dict,
                            tip: str | None) -> dict:
    """Offline status's release section (design §4.5, INV-8):
    no network call. The ledger is the local tags and tag objects, the tip
    the last-fetched refs/remotes/origin/<branch> (`tip`); what cannot be
    computed from them (tag truth versus the remote, anything needing a
    missing object) is None, shown as RELEASE_UNKNOWN_OFFLINE, never BROKEN."""
    release = config["release"]
    ledger = read_local_ledger(publish_dir, release["tag_format"], source_root=source_root,
                               adopt_tags=release.get("adopt_tags", []))
    journals = _all_journals(publish_dir)
    violations: list[str] | None = None
    ahead = split = None
    if tip is not None:
        violations = []
        for name, sha in sorted(ledger.managed.items()):
            if is_retired(source_root, name):
                continue
            check = git("merge-base", "--is-ancestor", sha, tip, cwd=str(publish_dir),
                        check=False)
            if check.returncode == 1:
                violations.append(name)
            elif check.returncode != 0:          # a missing object: not computable offline
                violations = None
                break
        ahead = ahead_of_latest(publish_dir, ledger, tip)
        split = ahead_counts(publish_dir, ledger, tip, source_root)
    when = _last_fetched(publish_dir)
    return _release_status_dict(config, ledger, journals, ahead=ahead, split=split,
                                violations=violations, mismatches=None,
                                as_of=(f"release state as of last fetch ({when})" if when
                                       else "release state as of last fetch (never fetched)"))


def _release_status_dict(config: dict, ledger: Ledger, journals: list, *, ahead, split,
                         violations, mismatches, as_of: str | None) -> dict:
    active = [j for j in journals
              if j.data.get("state", "active") not in ("complete", "abandoned", "superseded")]
    completed_versions = {j.version for j in journals if j.data.get("state") == "complete"}
    return {
        "as_of": as_of,                  # None: read from the remote now (--remote)
        "latest_version": ledger.latest,
        "latest_tag": (render_tag(config["release"]["tag_format"], ledger.latest)
                       if ledger.latest else None),
        "ahead_count": ahead,
        "ahead_snapshots": split[0] if split else None,
        "ahead_collaborator": split[1] if split else None,
        "ancestry_ok": None if violations is None else not violations,
        "ancestry_violations": violations,
        "tag_mismatches": mismatches,
        "attestation": ("locally attested" if ledger.latest in completed_versions
                        else ("remote tag; completion not locally attested"
                              if ledger.latest else None)),
        "journals": [{"version": j.version, "state": j.data.get("state", "active"),
                      "in_flight": j.data.get("in_flight")} for j in active],
    }


def release_status(source_root: Path, config: dict, *, remote: bool = True,
                   tip: str | None = None) -> dict:
    """The status release section. `remote`: read the ledger and branch from
    the remote now (status --remote); otherwise offline, from the last fetch
    (`tip` is the last-fetched branch tip)."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    if not remote:
        try:
            return _release_status_offline(source_root, publish_dir, config, tip)
        except ReleaseError as exc:
            return {"error": str(exc)}
    try:
        discovery = discover_release_state(publish_dir, source_root, config)
        mismatches = local_tag_mismatches(publish_dir, discovery.ledger)
        violations = (ancestry_violations(publish_dir, discovery.ledger, discovery.branch_sha)
                      if discovery.branch_sha else [])
        return _release_status_dict(config, discovery.ledger, discovery.journals,
                                    ahead=discovery.ahead, split=discovery.ahead_split,
                                    violations=violations, mismatches=mismatches, as_of=None)
    except ReleaseError as exc:
        return {"error": str(exc)}


# --- release: adapters (protocol) ---

class ReleaseAdapter(Protocol):
    name: str

    def check_live(self, ctx) -> list[dict]: ...
    def build(self, ctx) -> None: ...
    def reconcile(self, ctx) -> None: ...
    def execute(self, ctx, stage: str) -> None: ...


# what a release build never sees. Exact names,
# or a prefix ending in "*". [publish.release] strip_env adds to it.
BUILD_ENV_STRIP = ("TWINE_*", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "GPG_AGENT_INFO",
                   "GIT_ASKPASS", "SSH_ASKPASS")


def _env_name_matches(name: str, patterns: Iterable[str]) -> bool:
    return any(name.startswith(p[:-1]) if p.endswith("*") else name == p for p in patterns)


def build_environment(config: dict) -> dict[str, str]:
    """The environment a release build runs in (design §8.6 "the
    build runs without credentials"): os.environ minus BUILD_ENV_STRIP and
    every [publish.release] strip_env entry. Doc states the residual: this
    does not stop a build backend reading files by absolute path."""
    release = config.get("release") if isinstance(config.get("release"), dict) else {}
    patterns = (*BUILD_ENV_STRIP, *(release.get("strip_env") or []))
    return {k: v for k, v in os.environ.items() if not _env_name_matches(k, patterns)}


# [publish.release] strip_env of the loaded config (configure_redaction):
# those variables' values are redacted too.
_REDACT_EXTRA: tuple[str, ...] = ()


def configure_redaction(config: dict) -> None:
    """Called once the config is loaded: sanitize_provider_output also
    redacts the values of the config's strip_env variables."""
    global _REDACT_EXTRA
    release = config.get("release") if isinstance(config.get("release"), dict) else {}
    extra = release.get("strip_env") or []
    _REDACT_EXTRA = tuple(p for p in extra if isinstance(p, str) and p)


def sanitize_provider_output(text: str) -> str:
    """Credentials in URLs (user:pass@) and the values of the stripped
    build-environment variables (BUILD_ENV_STRIP and the config's
    strip_env) are redacted."""
    sanitized = re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@", r"\1[redacted]@", text)
    patterns = (*BUILD_ENV_STRIP, *_REDACT_EXTRA)
    for name, value in os.environ.items():
        if value and len(value) >= 4 and _env_name_matches(name, patterns):
            sanitized = sanitized.replace(value, "[redacted]")
    return sanitized


def _run_tool(argv: list[str], env: dict | None = None,
              cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=str(cwd) if cwd is not None else None,
                          env=env, capture_output=True, text=True)


def _http_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise ReleaseError(f"ERROR: provider query failed ({exc.code}) for {url}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"ERROR: provider query failed for {url}: {sanitize_provider_output(str(exc))}") from exc


def enabled_adapters(config: dict) -> list[ReleaseAdapter]:
    if not release_enabled(config):
        return []
    release = config["release"]
    adapters: list[ReleaseAdapter] = []
    github = release.get("github", {})
    pypi = release.get("pypi", {})
    if isinstance(github, dict) and github.get("enabled"):
        adapters.append(GitHubAdapter(config))
    if isinstance(pypi, dict) and pypi.get("enabled"):
        adapters.append(PyPIAdapter(config))
    return adapters


@dataclass
class ReleaseCheckContext:
    source_root: Path
    publish_dir: Path
    config: dict
    version: str
    tag: str


def cmd_validate_release_check(source_root: Path, config: dict,
                               as_json: bool = False) -> int:
    if not release_enabled(config):
        raise ConfigError("--release-check requires [publish.release] enabled = true")
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    if not (publish_dir / ".git").exists():
        raise ReleaseError(f"ERROR: {publish_dir.name}/ not initialized; run {_PROG} init")
    findings = validate_release_environment(config, source_root)
    try:
        discovery = discover_release_state(publish_dir, source_root, config)
        version_valid = True
        try:
            parse_semver(discovery.source_version)
        except ValueError as exc:
            version_valid = False
            findings.append(_err("release.version_file", str(exc)))
        for message in local_tag_mismatches(publish_dir, discovery.ledger):
            findings.append(_err("release.tags", message))
        if discovery.branch_sha:
            for tag in ancestry_violations(publish_dir, discovery.ledger, discovery.branch_sha):
                findings.append(_err("release.ancestry", f"{tag} is not an ancestor of public main"))
        for journal in blocking_journals(publish_dir):
            findings.append(_err("release.journal", f"{journal.version} is incomplete at {journal.data.get('in_flight')}"))
        ctx = ReleaseCheckContext(
            source_root, publish_dir, config, discovery.source_version,
            render_tag(config["release"]["tag_format"], discovery.source_version))
        source_refusal = (private_target_refusal(
            source_root, config, config["release"].get("source_remote", "origin"))
            if version_valid and config["release"].get("source_tag") else None)
        if source_refusal is not None:
            # §18.2, R1: no network call to a refused source_remote
            findings.append(_err("release.source_remote",
                                 f"refusing to push private ref refs/tags/{ctx.tag} to "
                                 f"{config['release'].get('source_remote', 'origin')}: "
                                 f"{source_refusal}"))
        elif version_valid and config["release"].get("source_tag"):
            source_branch = _source_branch(source_root)
            source_remote = config["release"].get("source_remote", "origin")
            if not source_branch or not source_upstream_matches(source_root, source_remote, source_branch):
                findings.append(_err(
                    "release.source_remote",
                    f"source HEAD does not match {source_remote}/{source_branch or '(detached)'}"))
            source_tags = git(
                "ls-remote", "--tags", source_remote, f"refs/tags/{ctx.tag}",
                f"refs/tags/{ctx.tag}^{{}}", cwd=str(source_root), check=False)
            if source_tags.returncode != 0:
                findings.append(_err("release.source_remote",
                                     f"cannot query source tags: "
                                     f"{sanitize_provider_output(source_tags.stderr.strip())}"))
            else:
                remote_source_tags = _parse_ls_remote_tags(source_tags.stdout)
                source_head = git("rev-parse", "HEAD", cwd=str(source_root), check=False)
                if (ctx.tag in remote_source_tags and source_head.returncode == 0
                        and remote_source_tags[ctx.tag] != source_head.stdout.strip()):
                    findings.append(_err(
                        "release.source_tag",
                        f"source tag {ctx.tag} points to a different source commit"))
        if version_valid:
            for adapter in enabled_adapters(config):
                findings.extend(adapter.check_live(ctx))
    except ReleaseError as exc:
        findings.append(_err("release", str(exc)))
    errors = [item for item in findings if item["level"] == "error"]
    warnings = [item for item in findings if item["level"] == "warning"]
    if as_json:
        print(json.dumps({"valid": not errors, "errors": errors, "warnings": warnings}, indent=2))
    else:
        for item in errors:
            ui.error(f"ERROR [{item['field']}]: {item['message']}")
        for item in warnings:
            ui.warn(f"WARNING [{item['field']}]: {item['message']}")
        if not errors:
            ui.success("Release check OK")
    return EXIT_RELEASE_FAILED if errors else EXIT_SUCCESS


# --- release adapter: pypi ---


class PyPIAdapter:
    name = "pypi"

    def __init__(self, config: dict):
        self.config = config
        self.settings = config["release"]["pypi"]
        self.repository = self.settings.get("repository", "pypi")
        self.pending: list[Path] = []

    def _api_url(self, version: str) -> str:
        base = PYPI_API_BASES[self.repository]
        package = urllib.parse.quote(self.settings["package"], safe="")
        version = urllib.parse.quote(pep440_prerelease(version), safe="")
        return f"{base}/{package}/{version}/json"

    def check_live(self, ctx) -> list[dict]:
        findings = []
        for module in ("build", "twine"):
            result = _run_tool([sys.executable, "-m", module, "--version"])
            if result.returncode != 0:
                findings.append(_err(
                    f"release.pypi.{module}",
                    f"{module} is unavailable; install it before releasing: "
                    f"{sanitize_provider_output(result.stderr.strip())}"))
        credentials = "TWINE_PASSWORD" in os.environ
        if not credentials:
            parser = configparser.RawConfigParser()
            with suppress(OSError, configparser.Error):
                parser.read(Path.home() / ".pypirc")
                credentials = parser.has_section(self.repository)
        if not credentials:
            findings.append(_warning(
                "release.pypi.credentials",
                "twine credentials not detected; twine may still succeed via keyring"))
        try:
            _http_json(self._api_url(ctx.version))
        except ReleaseError as exc:
            findings.append(_err("release.pypi.registry", str(exc)))
        return findings

    @staticmethod
    def _safe_members(names: list[str], archive_name: str) -> None:
        for name in names:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ReleaseError(f"ERROR: unsafe member path in {archive_name}: {name}")

    @staticmethod
    def _safe_zip_members(archive: zipfile.ZipFile, archive_name: str) -> None:
        PyPIAdapter._safe_members(archive.namelist(), archive_name)
        for info in archive.infolist():
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ReleaseError(
                    f"ERROR: unsafe symlink member in {archive_name}: {info.filename}")

    @staticmethod
    def _safe_tar_members(archive, archive_name: str) -> None:
        PyPIAdapter._safe_members(archive.getnames(), archive_name)
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise ReleaseError(
                    f"ERROR: unsafe link member in {archive_name}: {member.name}")

    @staticmethod
    def _metadata_from_archive(path: Path) -> tuple[str, str]:
        metadata = None
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                PyPIAdapter._safe_zip_members(archive, path.name)
                names = [n for n in archive.namelist() if n.endswith(".dist-info/METADATA")]
                if len(names) == 1:
                    metadata = archive.read(names[0]).decode(errors="replace")
        else:
            import tarfile
            with tarfile.open(path, "r:*") as archive:
                PyPIAdapter._safe_tar_members(archive, path.name)
                # only the top-level <name>-<ver>/PKG-INFO; setuptools also
                # ships <name>.egg-info/PKG-INFO, which is not the sdist's metadata.
                candidates = [m for m in archive.getmembers()
                              if len(PurePosixPath(m.name).parts) == 2
                              and PurePosixPath(m.name).name == "PKG-INFO"]
                if len(candidates) == 1:
                    file_obj = archive.extractfile(candidates[0])
                    metadata = file_obj.read().decode(errors="replace") if file_obj else None
        if metadata is None:
            raise ReleaseError(f"ERROR: distribution {path.name} has no unique package metadata")
        fields = {}
        for line in metadata.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                if key in ("Name", "Version"):
                    fields[key] = value.strip()
        if not fields.get("Name") or not fields.get("Version"):
            raise ReleaseError(f"ERROR: distribution {path.name} lacks Name/Version metadata")
        return fields["Name"], fields["Version"]

    @staticmethod
    def _extract_for_scrub(path: Path, destination: Path) -> None:
        safe_extract_distribution(path, destination)

    def build(self, ctx) -> None:
        dist_dir = ctx.txn_dir / "dist"
        dist_dir.mkdir(parents=True, exist_ok=True)
        timestamp = git("show", "-s", "--format=%ct", ctx.public_candidate,
                        cwd=str(ctx.publish_dir), check=False)
        if timestamp.returncode != 0:
            raise ReleaseError("ERROR: cannot determine SOURCE_DATE_EPOCH for candidate")
        env = build_environment(ctx.config)           # no credentials, no agents
        env["SOURCE_DATE_EPOCH"] = timestamp.stdout.strip()
        # the backend runs in a disposable copy, so whatever it writes
        # into its source dir (egg-info, generated files) never enters the
        # retained export, which stays equal to its journaled manifest (resume
        # and artifact reuse) and to what the export scrub saw.
        build_src = ctx.txn_dir / "build-src"
        if build_src.exists():
            shutil.rmtree(build_src)
        shutil.copytree(ctx.export_dir, build_src, symlinks=True)
        try:
            result = _run_tool(
                [sys.executable, "-m", "build", "--outdir", str(dist_dir), str(build_src)],
                env=env, cwd=build_src)
        finally:
            shutil.rmtree(build_src, ignore_errors=True)
        if result.returncode != 0:
            raise ReleaseError(f"ERROR: package build failed: {sanitize_provider_output(result.stderr.strip())}")
        files = sorted(path for path in dist_dir.iterdir() if path.is_file())
        if not files:
            raise ReleaseError("ERROR: package build produced no distributions")
        # twine check runs in the same credential-stripped environment.
        check = _run_tool([sys.executable, "-m", "twine", "check", *map(str, files)],
                          env=build_environment(ctx.config))
        if check.returncode != 0:
            raise ReleaseError(f"ERROR: twine check failed: {sanitize_provider_output(check.stderr.strip())}")
        expected_name = re.sub(r"[-_.]+", "-", self.settings["package"]).lower()
        expected_version = pep440_prerelease(ctx.version)
        extracted = ctx.txn_dir / "extracted"
        if extracted.exists():
            shutil.rmtree(extracted)
        for path in files:
            name, version = self._metadata_from_archive(path)
            if re.sub(r"[-_.]+", "-", name).lower() != expected_name or version != expected_version:
                raise ReleaseError(
                    f"ERROR: distribution {path.name} identity is {name} {version}; "
                    f"expected {self.settings['package']} {expected_version}")
            self._extract_for_scrub(path, extracted / path.name)
        # skip only members byte-identical to files the export scan saw
        # (the pre-build manifest); anything the build generated is scanned.
        export_digests = frozenset(item["sha256"] for item in
                                   ctx.journal.data.get("export_manifest") or [])
        release_scrub_refusal("the distributions", [
            f for path in files
            for f in scan_shipped_files(extracted / path.name, ctx.config,
                                        prefix=f"{path.name}!", skip_digests=export_digests)])
        ctx.artifacts.extend(Asset(path.name, path, _sha256(path), "distribution")
                             for path in files)

    def _remote_files(self, ctx) -> dict[str, str]:
        data = _http_json(self._api_url(ctx.version))
        if data is None:
            return {}
        return {item["filename"]: item.get("digests", {}).get("sha256")
                for item in data.get("urls", [])}

    def _terminal(self, ctx, reason: str) -> None:
        reason = sanitize_provider_output(reason)
        ctx.journal.data["state"] = "terminal"
        ctx.journal.data["terminal_reason"] = reason
        journal_write(ctx.journal)
        raise ReleaseError(f"ERROR: {reason}")

    def reconcile(self, ctx) -> None:
        remote = self._remote_files(ctx)
        distributions = [a for a in ctx.artifacts if a.kind == "distribution"]
        self.pending = []
        for artifact in distributions:
            if artifact.name in remote:
                if remote[artifact.name] != artifact.sha256:
                    self._terminal(ctx, f"PyPI file {artifact.name} exists with a different SHA-256")
            else:
                self.pending.append(artifact.path)

    def execute(self, ctx, stage: str) -> None:
        if stage != "upload" or not self.pending:
            return
        result = _run_tool([
            sys.executable, "-m", "twine", "upload", "--non-interactive",
            "--repository", self.repository, *map(str, self.pending),
        ])
        if result.returncode != 0:
            raise ReleaseError(f"ERROR: PyPI upload failed: {sanitize_provider_output(result.stderr.strip())}")
        remote = self._remote_files(ctx)
        for artifact in (a for a in ctx.artifacts if a.kind == "distribution"):
            if remote.get(artifact.name) != artifact.sha256:
                self._terminal(ctx, f"PyPI post-upload identity mismatch for {artifact.name}")


# --- release adapter: github ---


def _check_archive_member(name: str, root: Path, archive_name: str) -> None:
    """A member is refused (nothing written) when its path is
    absolute, has a `..` component or a backslash, or resolves outside the
    extraction root."""
    path = PurePosixPath(name)
    if (path.is_absolute() or ".." in path.parts or "\\" in name
            or not (root / name).resolve().is_relative_to(root)):
        raise ReleaseError(f"ERROR: unsafe member path in {archive_name}: {name}")


def safe_extract_distribution(path: Path, destination: Path) -> None:
    """Extract a built sdist or wheel for the release scrub (design §8.6).
    Every member is inspected here, before anything is written:
    paths per _check_archive_member; for tar, only regular files and
    directories (no symlink, hardlink, device or FIFO), extracted with
    filter="data"; for zip, no symlink members. At most OOXML_MAX_MEMBERS
    members and OOXML_MAX_EXPANDED bytes (§4.6 limits). Refusal → 7."""
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > OOXML_MAX_MEMBERS:
                raise ReleaseError(f"ERROR: {path.name} has more than {OOXML_MAX_MEMBERS} members")
            total = 0
            for info in infos:
                _check_archive_member(info.filename, root, path.name)
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ReleaseError(
                        f"ERROR: unsafe symlink member in {path.name}: {info.filename}")
                total += info.file_size
                if total > OOXML_MAX_EXPANDED:
                    raise ReleaseError(f"ERROR: {path.name} expands beyond "
                                       f"{OOXML_MAX_EXPANDED} bytes")
            archive.extractall(destination)
        return
    import tarfile
    with tarfile.open(path, "r:*") as archive:
        members = archive.getmembers()
        if len(members) > OOXML_MAX_MEMBERS:
            raise ReleaseError(f"ERROR: {path.name} has more than {OOXML_MAX_MEMBERS} members")
        total = 0
        for member in members:
            _check_archive_member(member.name, root, path.name)
            if not (member.isfile() or member.isdir()):
                raise ReleaseError(f"ERROR: unsafe link or special member in {path.name}: "
                                   f"{member.name}")
            total += member.size
            if total > OOXML_MAX_EXPANDED:
                raise ReleaseError(f"ERROR: {path.name} expands beyond {OOXML_MAX_EXPANDED} bytes")
        archive.extractall(destination, filter="data")


def normalize_git_remote(url: str) -> tuple[str, str, str]:
    value = url.strip()
    if re.match(r"^[^/@:]+@[^:]+:", value):
        _user, remainder = value.split("@", 1)
        host, path = remainder.split(":", 1)
    elif "://" in value:
        parsed = urllib.parse.urlparse(value)
        host = (parsed.hostname or "").lower()
        path = parsed.path.lstrip("/")
    else:
        parts = value.strip("/").split("/")
        if len(parts) == 2:
            host, path = "github.com", value
        else:
            raise ValueError(f"unsupported git remote identity: {url}")
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = path.split("/")
    if not host or len(parts) != 2 or not all(parts):
        raise ValueError(f"remote must identify one host/owner/repository: {url}")
    return host.lower(), parts[0].lower(), parts[1].lower()


class GitHubAdapter:
    name = "github"

    def __init__(self, config: dict):
        self.config = config
        self.settings = config["release"]["github"]
        self.repository = self.settings["repository"]
        try:
            self.host, self.owner, self.repo = normalize_git_remote(self.repository)
        except ValueError:
            # owner/name is the documented short form.
            self.host, self.owner, self.repo = normalize_git_remote(
                f"https://github.com/{self.repository}")
        self.repo_arg = f"{self.owner}/{self.repo}"
        self.view: dict | None = None
        self.pending: list[Asset] = []

    def _run(self, *args: str):
        return _run_tool(["gh", *args, "--repo", self.repo_arg])

    def _release_view(self, tag: str) -> dict | None:
        result = self._run("release", "view", tag, "--json",
                           "tagName,isDraft,isPrerelease,assets,url")
        if result.returncode != 0:
            text = (result.stderr + result.stdout).lower()
            if "not found" in text or "release not found" in text:
                return None
            raise ReleaseError(
                f"ERROR: GitHub release query failed: {sanitize_provider_output(result.stderr.strip())}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ReleaseError("ERROR: GitHub release query returned invalid JSON") from exc

    def check_live(self, ctx) -> list[dict]:
        findings = []
        version = _run_tool(["gh", "--version"])
        if version.returncode != 0:
            findings.append(_err("release.github.gh", "gh CLI is unavailable; install it before releasing"))
            return findings
        auth = _run_tool(["gh", "auth", "status", "--hostname", self.host])
        if auth.returncode != 0:
            findings.append(_err(
                "release.github.auth",
                f"gh authentication failed for {self.host}: {sanitize_provider_output(auth.stderr.strip())}; run gh auth login --hostname {self.host}"))
        try:
            remote_identity = normalize_git_remote(self.config["remote"])
            if remote_identity != (self.host, self.owner, self.repo):
                findings.append(_err(
                    "release.github.repository",
                    f"configured GitHub repository {self.host}/{self.owner}/{self.repo} does not match publish remote {self.config['remote']}"))
        except ValueError as exc:
            findings.append(_err("release.github.repository", str(exc)))
        try:
            self._release_view(ctx.tag)
        except ReleaseError as exc:
            findings.append(_err("release.github.query", str(exc)))
        return findings

    def build(self, ctx) -> None:
        return None

    def _terminal(self, ctx, reason: str) -> None:
        reason = sanitize_provider_output(reason)
        ctx.journal.data["state"] = "terminal"
        ctx.journal.data["terminal_reason"] = reason
        journal_write(ctx.journal)
        raise ReleaseError(f"ERROR: {reason}")

    def _download_digest(self, ctx, asset_name: str) -> str:
        directory = ctx.txn_dir / "github-observed" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        result = self._run("release", "download", ctx.tag, "--pattern", asset_name,
                           "--dir", str(directory))
        if result.returncode != 0:
            raise ReleaseError(
                f"ERROR: cannot download GitHub asset {asset_name} for verification: "
                f"{sanitize_provider_output(result.stderr.strip())}")
        path = directory / asset_name
        if not path.is_file():
            raise ReleaseError(f"ERROR: GitHub did not return expected asset {asset_name}")
        return _sha256(path)

    def reconcile(self, ctx) -> None:
        self.view = self._release_view(ctx.tag)
        self.pending = list(ctx.artifacts)
        if self.view is None:
            return
        remote_assets = {item["name"]: item for item in self.view.get("assets", [])}
        pending = []
        for artifact in ctx.artifacts:
            remote = remote_assets.get(artifact.name)
            if remote is None:
                pending.append(artifact)
                continue
            digest = remote.get("digest")
            if isinstance(digest, str) and digest.startswith("sha256:"):
                digest = digest.split(":", 1)[1]
            else:
                digest = self._download_digest(ctx, artifact.name)
            if digest != artifact.sha256:
                self._terminal(ctx, f"GitHub asset {artifact.name} exists with a different SHA-256")
        self.pending = pending

    def execute(self, ctx, stage: str) -> None:
        if stage == "draft":
            if self.view is not None:
                return
            notes = (ctx.export_dir / self.settings.get("notes_file", "CHANGELOG.md")).resolve()
            if not notes.is_relative_to(ctx.export_dir.resolve()) or not notes.is_file():
                raise ReleaseError(f"ERROR: GitHub notes file is missing from candidate export: {notes.name}")
            # §17.3 / AC103: --verify-tag, so gh never creates the tag server-side
            # (the release tag is pushed by the transaction before this step).
            args = ["release", "create", ctx.tag, "--verify-tag", "--draft", "--title", ctx.tag,
                    "--notes-file", str(notes)]
            if not is_stable(ctx.version):
                args.append("--prerelease")
            result = self._run(*args)
            if result.returncode != 0:
                raise ReleaseError(f"ERROR: GitHub draft creation failed: {sanitize_provider_output(result.stderr.strip())}")
            self.view = {"isDraft": True, "assets": []}
        elif stage == "upload":
            if not self.pending:
                return
            result = self._run("release", "upload", ctx.tag,
                               *[str(asset.path) for asset in self.pending])
            if result.returncode != 0:
                raise ReleaseError(f"ERROR: GitHub asset upload failed: {sanitize_provider_output(result.stderr.strip())}")
            self.reconcile(ctx)
            if self.pending:
                self._terminal(ctx, "GitHub did not expose every uploaded asset with its expected digest")
        elif stage == "publish":
            if self.view is not None and self.view.get("isDraft") is False:
                return
            result = self._run("release", "edit", ctx.tag, "--draft=false")
            if result.returncode != 0:
                raise ReleaseError(f"ERROR: GitHub release publication failed: {sanitize_provider_output(result.stderr.strip())}")


PUBLISH_LOG_NAME = "pubrepo-log.jsonl"


def _append_publish_log(publish_dir: Path, entry: dict) -> None:
    """Append a JSONL audit entry (T19). Lives inside .git/ so it survives
    the nuke, is never published, and travels with the clone. A log failure
    must never block a publish."""
    try:
        with open(publish_dir / ".git" / PUBLISH_LOG_NAME, "a") as fobj:
            fobj.write(json.dumps(entry) + "\n")
    except OSError as e:
        ui.warn(f"WARNING: could not write publish log: {e}")


def _read_last_log_publish(publish_dir: Path) -> dict | None:
    """Last successful publish from the T19 log; corrupt lines tolerated."""
    log_path = publish_dir / ".git" / PUBLISH_LOG_NAME
    if not log_path.exists():
        return None
    try:
        lines = log_path.read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # corrupt line: degrade gracefully, never crash status
        if not entry.get("pushed"):
            continue  # blocked attempts are audit data, not "last publish"
        source = str(entry.get("source_commit", "?"))
        if entry.get("source_dirty"):
            source += " (dirty)"
        public_commit_raw = str(entry.get("public_commit") or "?")
        return {
            "commit": public_commit_raw[:12],
            "date": entry.get("ts", "?"),
            "source": source,
            "public_commit_full": public_commit_raw if public_commit_raw != "?" else None,
        }
    return None


def get_last_publish_info(publish_dir: Path) -> dict | None:
    """Last publish info: the T19 log is authoritative; fall back to
    commit-message parsing for repos that predate the log."""
    from_log = _read_last_log_publish(publish_dir)
    if from_log is not None:
        return from_log
    result = git(
        "log", "-1", "--format=%H%n%ai%n%B",
        cwd=str(publish_dir), check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None

    lines = result.stdout.strip().split("\n")
    commit_hash = lines[0] if lines else "?"
    commit_date = lines[1] if len(lines) > 1 else "?"
    body = "\n".join(lines[2:]) if len(lines) > 2 else ""

    source_hash = None
    for line in body.split("\n"):
        if line.startswith("Source:"):
            # No break: the LAST Source: line is always the tool's own
            # footer; custom titles containing 'Source:' come earlier.
            source_hash = line.split(":", 1)[1].strip()

    return {
        "commit": commit_hash[:12],
        "date": commit_date,
        "source": source_hash,
    }


def _build_wanted_files(source_root: Path, config: dict, with_content: bool = False):
    """Build a dict of {relative_path: content_hash} for files that would be published.

    Hashes reflect post-transform content so comparisons against .publish/
    correctly show 'no changes' when only transforms differ. With
    with_content, returns (hashes, {relative_path: post_transform_bytes})
    for the --diff renderer (T17).
    """
    contents: dict[str, bytes] = {}
    ignore_fn = make_ignore_fn(source_root, config["exclude"])
    user_matcher = compile_patterns(config["exclude"])
    transforms = config.get("transforms", {})
    wanted = {}
    for inc in config["include"]:
        if _include_is_hard_excluded(inc):
            continue
        src = source_root / inc
        if _include_matches_excludes(inc, src.is_dir(), user_matcher):
            continue
        if src.is_file():
            if src.is_symlink():
                continue
            try:
                content = src.read_bytes()
            except OSError as e:
                ui.warn(f"WARNING: cannot read '{inc}' for hashing "
                        f"({e.__class__.__name__}) — scrub will flag it")
                continue
            if inc in transforms and not is_binary_file(src):
                text = content.decode(errors="surrogateescape")
                text = apply_transform_rules(text, transforms[inc])
                content = text.encode(errors="surrogateescape")
            wanted[inc] = hashlib.sha256(content).hexdigest()
            if with_content:
                contents[inc] = content
        elif src.is_dir():
            for root, dirs, filenames in os.walk(src):
                ignored = ignore_fn(root, dirs + filenames)
                dirs[:] = [d for d in dirs if d not in ignored]
                for f in filenames:
                    if f not in ignored:
                        full = os.path.join(root, f)
                        full_path = Path(full)
                        if full_path.is_symlink():
                            continue
                        rel = os.path.relpath(full, start=str(source_root))
                        try:
                            content = full_path.read_bytes()
                        except OSError as e:
                            ui.warn(f"WARNING: cannot read '{rel}' for hashing "
                                    f"({e.__class__.__name__}) — scrub will flag it")
                            continue
                        if rel in transforms and not is_binary_file(full_path):
                            text = content.decode(errors="surrogateescape")
                            text = apply_transform_rules(text, transforms[rel])
                            content = text.encode(errors="surrogateescape")
                        wanted[rel] = hashlib.sha256(content).hexdigest()
                        if with_content:
                            contents[rel] = content
    return (wanted, contents) if with_content else wanted


def _path_would_publish(rel_path: str, config: dict) -> bool:
    """Single-path version of the copy_includes filter (§6 precedence), for
    paths that may not exist locally — integrate filters the FOREIGN tree's
    changed files, so it can never consult the filesystem. Delegates to the
    same engine (compile_patterns, is_hard_excluded, is_env_excluded,
    _include_matches_excludes); the verdicts cannot drift from publish:
    hard-excludes → user excludes (beat includes, at every ancestor) →
    include entry level (beats default excludes) → default+user patterns and
    env rules filtering every component BELOW a directory entry."""
    matcher = compile_patterns(sorted(DEFAULT_EXCLUDES) + list(config["exclude"]))
    user_matcher = compile_patterns(config["exclude"])
    if any(is_hard_excluded(part) for part in rel_path.split("/")):
        return False
    for inc in config["include"]:
        # copy_includes resolves entries through the filesystem, so "." (the
        # repository root) and "./src" name the same trees as "" and "src".
        entry = posixpath.normpath(inc)
        if entry == ".":
            below = rel_path.split("/")
            prefix = ""
        else:
            if rel_path == entry:
                # Explicit file include: beats default excludes at entry level.
                if not _include_matches_excludes(entry, False, user_matcher):
                    return True
                continue
            if not rel_path.startswith(entry + "/"):
                continue
            if _include_matches_excludes(entry, True, user_matcher):
                continue
            prefix = entry
            below = rel_path[len(entry) + 1:].split("/")
        for i, name in enumerate(below):
            prefix = f"{prefix}/{name}" if prefix else name
            is_dir = i < len(below) - 1
            if is_env_excluded(name) or matcher(prefix, is_dir):
                break
        else:
            return True
    return False


def cmd_init(source_root: Path, config: dict, *, config_file: Path | None = None) -> None:
    """First-time setup: clone the remote into .publish/ and report problems.

    Reports only, with one exception (§18.11): on a
    terminal it offers to add the records remote's values to
    [publish] private_remotes, default No, and edits `config_file` only on
    an explicit yes. Off a terminal it prints the line and edits nothing.
    .gitignore advice is printed, not applied.
    """
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    remote = config["remote"]
    branch = config["branch"]

    if (publish_dir / ".git").exists():
        unresolved = [j for j in _all_journals(publish_dir)
                      if j.data.get("state", "active") not in
                      ("complete", "abandoned", "superseded")]
        if unresolved:
            names = ", ".join(j.version for j in unresolved)
            raise ReleaseError(
                f"ERROR: init cannot replace a publish clone with unresolved release journal(s): {names}. "
                f"Complete or release-abandon them first.")
        ui.success(f"Already initialized: {publish_dir}")
        return

    ui.info(f"Remote: {remote}")
    ui.info(f"Branch: {branch}")

    # Modern git clones empty repos successfully (unborn HEAD), so a clone
    # failure is a REAL error — auth, URL, network — not "empty repo?".
    result = git("clone", remote, str(publish_dir), cwd=str(source_root),
                 check=False)
    if result.returncode != 0:
        raise PublishError(
            f"ERROR: clone failed:\n{sanitize_provider_output(result.stderr.strip())}\n"
            f"Create the remote repo first (it can be empty), check the URL, "
            f"verify auth (SSH key / token).")

    try:
        head = git("symbolic-ref", "--short", "HEAD", cwd=str(publish_dir), check=False)
        current = head.stdout.strip() if head.returncode == 0 else None
        if current != branch:
            # Tracking checkout if the branch exists on the remote, else create.
            result = git("checkout", branch, cwd=str(publish_dir), check=False)
            if result.returncode != 0:
                git("checkout", "-b", branch, cwd=str(publish_dir))
    except GitError as e:
        raise PublishError(str(e)) from e

    # Post-clone checks: report only.
    email = git("config", "user.email", cwd=str(publish_dir), check=False)
    name = git("config", "user.name", cwd=str(publish_dir), check=False)
    if email.returncode != 0 or name.returncode != 0 \
            or not email.stdout.strip() or not name.stdout.strip():
        ui.warn("WARNING: no git identity resolvable — commits will fail until this is set:")
        ui.warn(f"  git -C {publish_dir.name} config user.name 'Your Name'")
        ui.warn(f"  git -C {publish_dir.name} config user.email 'you@example.com'")
    else:
        ui.info(f"Git identity: {name.stdout.strip()} <{email.stdout.strip()}>")

    if not remote.startswith(("https://", "http://", "ssh://", "git@", "file://", "/", ".")):
        ui.warn(f"WARNING: remote '{remote}' doesn't look like a git URL "
                f"(https://, git@..., ssh://)")

    # Ask git whether the publish dir is ignored (covers nested/global
    # ignores, not just a literal root .gitignore line). 0 = ignored,
    # 1 = not ignored, 128 = source isn't a git repo (concern is moot).
    check = git("check-ignore", "-q", publish_dir.name,
                cwd=str(source_root), check=False)
    if check.returncode == 1:
        verb = ("add" if (source_root / ".gitignore").exists()
                else "create a .gitignore and add")
        ui.warn(f"NOTE: {verb} '{publish_dir.name}/' to your .gitignore — the "
                f"publish dir should not be tracked by the source repo")
    _offer_private_remotes(source_root, config, config_file)

    ui.success(f"Initialized: {publish_dir}")


def _offer_private_remotes(source_root: Path, config: dict, config_file: Path | None) -> None:
    """The records remote (sync's silent
    records_remote_candidate; records_remote_name returns None for exactly
    the refused remote this offers to fix) gets its private_remotes line
    when the private-target check refuses it and the line has something to
    add.
    - On a terminal: name the remote and the values, then ask `Add these to
      [publish] private_remotes in <config>? [y/N]`, default No. On `y`,
      insert the line directly after the [publish] header when the config
      has no private_remotes; when it has one, print the full replacement
      line instead of editing. Anything else prints the line. --quiet never
      hides the question.
    - Off a terminal: print the line only. No new flag."""
    name = records_remote_candidate(source_root, config)
    if not name or private_target_refusal(source_root, config, name) is None:
        return
    line = private_remotes_line(source_root, config, name)
    if line is None:
        return
    shown = str(config_file.name if config_file is not None else CONFIG_FILE_NAME)
    listed = config.get("private_remotes") or []
    new, cannot = [], []
    for value in remote_url_values(source_root, name):
        if value in listed:
            continue
        # the same filter as private_remotes_line, with the reason
        why = (f"{value!r} has leading or trailing whitespace" if value != value.strip()
               else _publish_repo_refusal(source_root, config, value))
        (cannot if why else new).append((value, why))
    advice = (f"Records sync only to a listed private remote. To list '{name}', add this line "
              f"to [publish] in {shown}:\n  {line}")
    if not is_interactive():
        ui.info(advice)
        return
    sys.stderr.write(f"Records remote '{name}' has these unlisted values:\n"
                     + "".join(f"  {v}\n" for v, _why in new)
                     + "".join(f"  cannot be listed: {why}\n" for _v, why in cannot)
                     + f"Add these to [publish] private_remotes in {shown}? Only if these "
                       f"repositories are private. [y/N] ")
    sys.stderr.flush()
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes") or config_file is None:
        ui.info(advice)
        return
    if "private_remotes" in config:
        ui.info(f"{shown} already sets private_remotes; replace that line with:\n  {line}")
        return
    text = config_file.read_text(encoding="utf-8")
    header = re.search(r"(?m)^\[publish\][ \t]*\r?\n", text)
    if header is None:
        ui.info(advice)
        return
    config_file.write_text(text[:header.end()] + line + "\n" + text[header.end():],
                           encoding="utf-8")
    ui.info(f"Added to [publish] in {shown}: {line}")


def cmd_integrate(source_root: Path, config: dict, plan_args: argparse.Namespace) -> int:
    """`integrate` (design §7, §10 item 3; AC65): an alias of `pull --plan`
    plus a notice. Same plan, digest and exit code; the 1.0 print-only recipe
    (which ended in the retired --force-overwrite) is gone. `plan_args` is
    what `pull --plan` parses to."""
    ui.info(f"Note: `{_PROG} integrate` is now `{_PROG} pull --plan`: it shows the pull plan "
            f"and its digest; `{_PROG} pull` applies it.")
    return cmd_pull_plan(source_root, config, plan_args)


def _source_branch(source_root: Path) -> str | None:
    result = git("symbolic-ref", "--short", "HEAD", cwd=str(source_root), check=False)
    return result.stdout.strip() if result.returncode == 0 else None


# === core: previews ===
# Design §4.5, INV-8, §9 preview columns. Every preview is
# computed on the git-tree baseline (the tip, or the IP when tip != IP) plus
# the projection, never on the .publish/ worktree (bug #7).

@dataclass
class Baseline:
    """The public tip a preview compares against (design §4.5)."""
    tip: str | None
    remote_checked: bool
    last_fetched: str | None      # when .publish last fetched (FETCH_HEAD), ISO 8601
    label: str


def _last_fetched(publish_dir: Path) -> str | None:
    fetch_head = _git_dir(publish_dir) / "FETCH_HEAD"
    if not fetch_head.exists():
        return None
    return datetime.fromtimestamp(fetch_head.stat().st_mtime).astimezone() \
        .isoformat(timespec="seconds")


def preview_baseline(publish_dir: Path, branch: str, *, remote: bool) -> Baseline:
    """With `remote`, fetch_tip (a transport failure raises PublishError, 2).
    Offline, the last-known refs/remotes/origin/<branch>, labelled."""
    if remote:
        tip = fetch_tip(publish_dir, branch)
        return Baseline(tip, True, _last_fetched(publish_dir),
                        f"remote checked (origin/{branch})")
    ref = git("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}^{{commit}}",
              cwd=str(publish_dir), check=False)
    tip = ref.stdout.strip() if ref.returncode == 0 else None
    when = _last_fetched(publish_dir)
    label = (f"remote not checked (last fetched {when})" if when
             else "remote not checked (never fetched)")
    return Baseline(tip, False, when, label)


# Design §9 `--dry-run` column (publish's code, predicted from the state).
_PREDICTED_EXIT = {"S0": 0, "S1": 0, "S6": 0,
                   "S2": 5, "S3": 5, "S7": 5, "S10a": 5, "S11": 5, "S12": 5,
                   "S9": 8, "S10b": 8, "S10b+S2": 8}


# Defined before their first use in an annotation: Python < 3.14 evaluates annotations eagerly.
@dataclass
class SnapshotPlan:
    """build_snapshot_tree's result. adds/mods/dels are
    (path, reason) against the base, reason "" for an ordinary projection
    change. `written` is this snapshot's write set, path → blob or
    "deleted": projection writes and INV-3 (b) deletions only. `decided`
    holds the decision-authorized changes of unmanaged paths (restore,
    remove), path → {"decision", "blob" (None when deleted), "record"}; they
    are not pubrepo writes and never enter `written` (design §3.1).
    `items` (preview only) are paths left at base content
    because a pull must decide them."""
    tree: str
    adds: list[tuple[str, str]]
    mods: list[tuple[str, str]]
    dels: list[tuple[str, str]]
    carried_count: int
    written: dict[str, str]
    nonderivable: list[str]
    keep: list[str]
    items: list[str]
    decided: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True)
class CommitInfo:
    sha: str
    author: str                # "Name <email>"
    subject: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class PullItem:
    path: str
    cls: str                   # design §5.3 class name
    base: str | None
    ours: str | None
    theirs: str | None
    origin: Literal["range", "inv3", "baseline", "redecide"]
    snapshot: str | None       # baseline items: the newest ours-snapshot touching path
    flags: tuple[str, ...]     # §5.2 inbound flag names; empty at publish time
    options: tuple[str, ...]   # decisions offered
    default: str | None
    prefilled_from: str | None # pull-record sha when a §5.7 prefill applies


@dataclass(frozen=True)
class DivergenceInfo:
    snapshot: str
    sigma: str | None
    kind: Literal["behind", "diverged", "gone"]   # S10a | S10b | S10b, σ gone
    branches: tuple[str, ...]  # `git branch --contains σ`; empty when gone. For
                               # "behind", only when σ is not reachable from
                               # HEAD's upstream
    accept_flag: str           # exact text to paste: --accept-source-divergence=<sha>


@dataclass(frozen=True)
class Hit:                     # S12
    path: str
    blob: str
    repair: str                # plan digest of the applied repair record
    commit: str | None         # where it is reachable from the tip; None = the tip tree


@dataclass(frozen=True)
class Decision:
    path: str
    decision: str
    cls: str
    flags: tuple[str, ...]
    theirs: str | None
    result: str | None         # lets `restore` re-add the blob
    record: str


@dataclass
class IntegrationState:
    state: str                 # "S0".."S12", plus "S10b+S2"
    tip: str | None
    ip: str | None
    unpulled: list[CommitInfo]
    items: list[PullItem]
    missing_snapshots: list[str]       # S7 by record
    divergence: DivergenceInfo | None
    reintroduced: list[Hit]            # S12
    copied_trailer: list[str]
    baseline_items: list[PullItem]     # unmanaged baseline items

    # The methods read a private view built by compute_integration
    # (_IdentityView). It is not a dataclass field, so the frozen field list
    # is unchanged; a fake built from the fields alone has no view.

    def is_ours(self, sha: str) -> bool:
        """A qualified ours-snapshot (§3.1 step 1, INV-4 chain
        qualification). Copied-trailer commits are not ours."""
        return sha in self._view.ours_qualified

    def accounted(self, path: str, tip_blob: str | None) -> bool:
        """Design §2 accounted-for (o), (i), (ii) for P at the tip."""
        view = self._view
        if tip_blob == view.pi_blob(path):                       # (o), absent == absent
            return True
        lw = view.lw.get(path)
        if lw is not None and tip_blob == (None if lw == "deleted" else lw):   # (i)
            return True
        if tip_blob is None and path not in view.touched:
            return True   # absent publicly and never written: adding destroys nothing
        entry = view.newest_decision(path)                       # (ii)
        return (entry is not None and entry["decision"] in AUTHORIZING_DECISIONS
                and entry["theirs"] == tip_blob
                and (entry.get("managed_at_decision", True) or entry["decision"] == "remove"))

    def lw(self, path: str) -> str | None:
        """Blob, "deleted", or None."""
        return self._view.lw.get(path)

    def written_ever(self, path: str) -> bool:
        return path in self._view.written_ever

    def decision_for(self, path: str, tip_blob: str | None) -> Decision | None:
        """The decision of the newest pull record on HEAD's
        first-parent chain that decided `path`, iff that record's `theirs`
        == tip_blob (absent == absent); else None."""
        entry = self._view.newest_decision(path)
        if entry is None or entry["theirs"] != tip_blob:
            return None
        return _decision_from_entry(entry)

    def baseline_items_for(self, sha: str) -> list[str]:
        return sorted(self._view.baseline_by_snapshot.get(sha, ()))


@dataclass
class PreviewReport:
    """Everything a preview reports. `gate` holds the managed
    scan's findings plus unreadable paths; `carried` the carried scan's
    non-allowlisted findings."""
    baseline: Baseline
    state: str
    integ: IntegrationState
    proj: Projection
    plan: SnapshotPlan
    base: str | None
    gate: list[GateFinding]
    carried: list[GateFinding]
    predicted_exit: int

    def blocking(self) -> list[GateFinding]:
        return [f for f in self.gate if not f.allowlisted]


def preview(source_root: Path, publish_dir: Path, config: dict, *, remote: bool,
            config_relpath: str | None = None, accept_divergence: str | None = None,
            recreate: bool = False) -> PreviewReport:
    """Design §4.5. Pre: the source lock is held. Builds the
    snapshot tree in preview mode — no commit, no push, no ref — and never
    asserts: pull items stay at base content and are listed.
    `accept_divergence` and `recreate` model publish's
    --accept-source-divergence and --recreate-branch (the off-terminal
    preview passes them); the defaults are the plain preview."""
    branch = config["branch"]
    check_shallow(source_root, publish_dir)
    baseline = preview_baseline(publish_dir, branch, remote=remote)
    if recreate:
        # --recreate-branch says the public branch was deleted; the
        # last-fetched tip can predate that, so the preview starts from none.
        baseline = Baseline(None, baseline.remote_checked, baseline.last_fetched,
                            baseline.label)
    if remote:
        records_fetch(source_root, config)
    lifecycle = branch_lifecycle(publish_dir, source_root, config, baseline.tip,
                                 recreate=recreate)
    unreadable: list[tuple[str, str]] = []
    proj = _build_projection(source_root, publish_dir, config, unreadable=unreadable)
    scanned = [(p, e.blob) for p, e in proj.entries.items()]
    if unreadable and baseline.tip:
        # INV-8: an unreadable managed path stays at base
        # content in a preview. The gate lists it as unscannable; it is never
        # shown as a deletion that publish (which stops on it) would not make.
        at_base = _tree_entries(publish_dir, baseline.tip)
        for path, _why in unreadable:
            old = at_base.get(path)
            if old is not None and old[1] == "blob":
                proj.entries[path] = ProjEntry(blob=old[2], mode=old[0], nonderivable=False,
                                               keep=False, transformed=False)
    integ = compute_integration(source_root, publish_dir, config, baseline.tip, proj,
                                accept_divergence=accept_divergence, fetch_sigma=remote,
                                config_relpath=config_relpath)
    state = "S11" if lifecycle.kind == "missing" else integ.state
    plan = build_snapshot_tree(publish_dir, integ.tip, proj, integ, config, mode="preview")
    gate = gate_scan(publish_dir, scanned, config).findings
    gate += [GateFinding(path=p, part=None, kind="unscannable", detail=why, allowlisted=False)
             for p, why in unreadable]
    base = integ.ip if integ.ip is not None and integ.tip != integ.ip else integ.tip
    predicted = _PREDICTED_EXIT[state]
    if predicted == 0 and any(not f.allowlisted for f in gate):
        predicted = EXIT_SCRUB_FAILED
    return PreviewReport(baseline=baseline, state=state, integ=integ, proj=proj, plan=plan,
                         base=base, gate=gate,
                         carried=_carried_findings(publish_dir, plan.tree, proj, config),
                         predicted_exit=predicted)


def _preview_json(report: PreviewReport) -> dict:
    """The additive preview keys in the preview JSON."""
    return {
        "remote_checked": report.baseline.remote_checked,
        "remote_last_fetched": report.baseline.last_fetched,
        "unpulled": [{"sha": c.sha, "author": c.author, "subject": c.subject,
                      "paths": list(c.paths)} for c in report.integ.unpulled],
        "items": [{"path": i.path, "class": i.cls} for i in report.integ.items],
        "carried_count": report.plan.carried_count,
        "predicted_exit": report.predicted_exit,
    }


def _state_note(report: PreviewReport) -> str | None:
    """The §9 refusal text for a non-publishable state, without "ERROR: "."""
    if report.state == "S11":
        return (f"the public branch is missing but was published before; publish needs "
                f"--recreate-branch")
    refusal = _state_refusal(report.integ)
    return str(refusal).replace("ERROR: ", "", 1) if refusal is not None else None


def _preview_lines(report: PreviewReport, branch: str) -> list[str]:
    """INV-8: the baseline and whether the remote was checked, the unpulled
    range, every item, the carried count and every named deletion."""
    base = report.base[:12] if report.base else "(empty)"
    where = "origin/" + branch if report.base == report.integ.tip else "the integration point"
    lines = [f"Baseline: {base} ({where}; {report.baseline.label})"]
    if report.integ.unpulled:
        lines.append(f"Unpulled: {len(report.integ.unpulled)} commit(s) on origin/{branch}:")
        lines.append(_commit_lines(report.integ.unpulled))
    if report.integ.items:
        lines.append(f"Pull items ({len(report.integ.items)}), left at public content:")
        lines += [f"  {i.path} ({i.cls})" for i in report.integ.items]
    if report.plan.carried_count:
        lines.append(f"Carried: {report.plan.carried_count} public path(s) not managed here")
    for path, reason in report.plan.dels:
        lines.append(f"  deleted: {path}" + (f" ({reason})" if reason else ""))
    note = _state_note(report)
    if note:
        lines.append(f"Publish would exit {report.predicted_exit}: {note}")
    return lines


def _gate_lines(report: PreviewReport, heading: str) -> list[str]:
    blocking = report.blocking()
    lines = [f"  {_finding_label(f)}: {f.detail}" for f in report.gate if f.allowlisted]
    if blocking:
        paths = sorted({f.path for f in blocking})
        lines.append(f"{heading}: {len(paths)} file(s) blocked by the outbound gate:")
        lines += [f"  {_finding_label(f)}: {f.detail}" for f in blocking]
    return lines


def _finding_json(f: GateFinding) -> dict:
    return {"file": f.path, "part": f.part, "kind": f.kind, "detail": f.detail}


def _preview_locked(source_root: Path, config: dict, *, remote: bool,
                    config_relpath: str | None, command: str) -> PreviewReport:
    """A read-only preview under the shared source lock (§5.13)."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    with source_lock(source_root, exclusive=False):
        assert_no_pull_in_progress(source_root, command=command)
        return preview(source_root, publish_dir, config, remote=remote,
                       config_relpath=config_relpath)


def _dry_run_uninitialized(source_root: Path, config: dict, as_json: bool, *,
                           custom_message: str | None = None) -> int:
    """`--dry-run` before init: no public state exists; the projection and
    the gate still run, over a throwaway object store. A forbidden string in
    `custom_message` is exit 4, as publish would refuse it (usability #3)."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    source_hash, is_dirty = get_source_info(source_root)
    with tempfile.TemporaryDirectory(prefix="pubrepo-dry-run-") as tmp:
        scratch = Path(tmp)
        git("init", "-q", "--bare", str(scratch / "odb.git"))
        unreadable: list[tuple[str, str]] = []
        proj = _build_projection(source_root, scratch / "odb.git", config, unreadable=unreadable)
        findings = gate_scan(scratch / "odb.git",
                             [(p, e.blob) for p, e in proj.entries.items()], config).findings
    findings += [GateFinding(path=p, part=None, kind="unscannable", detail=why,
                             allowlisted=False) for p, why in unreadable]
    blocking = [f for f in findings if not f.allowlisted]
    files = sorted(p for p, e in proj.entries.items() if not e.keep)
    scan = _message_scan(config, custom_message, None, None)
    hit = scan if scan is not None and scan[1].count else None
    code = EXIT_SCRUB_FAILED if blocking or hit else EXIT_SUCCESS
    if as_json:
        scrub_json = {"passed": not blocking and not hit,
                      "matches": [_finding_json(f) for f in blocking]}
        if scan is not None:
            scrub_json["message"] = {"count": scan[1].count, "lines": list(scan[1].lines)}
        print(json.dumps({"files": files, "file_count": len(files),
                          "source": {"commit": source_hash, "dirty": is_dirty},
                          "transforms": sorted(p for p, e in proj.entries.items()
                                               if e.transformed),
                          "scrub": scrub_json,
                          "initialized": False}, indent=2))
        return code
    if ui.quiet:
        for f in blocking:
            ui.error(f"SCRUB WOULD FAIL: {_finding_label(f)}: {f.detail}")
        if hit:
            ui.error("SCRUB WOULD FAIL: " + message_scrub_text(*hit))
        return code
    print(f"Files to publish ({len(files)}):")
    for f in files:
        print(f"  {f}")
    lines = [f"  {_finding_label(f)}: {f.detail}" for f in findings if f.allowlisted]
    if blocking:
        lines.append(f"SCRUB WOULD FAIL: {len({f.path for f in blocking})} file(s) blocked by "
                     f"the outbound gate:")
        lines += [f"  {_finding_label(f)}: {f.detail}" for f in blocking]
    if hit:
        lines.append("SCRUB WOULD FAIL: " + message_scrub_text(*hit))
    if blocking or hit:
        lines.append("\nPublish will be blocked until these are resolved.")
    elif config.get("scrub", {}).get("forbidden"):
        lines.append(f"Scrub: passed ({len(config['scrub']['forbidden'])} patterns checked)")
    if lines:
        print()
        for line in lines:
            print(line)
    print()
    print(f"NOTE: {publish_dir.name}/ not initialized. Run: {_PROG} init")
    return code


def cmd_dry_run(source_root: Path, config: dict, as_json: bool = False, *,
                remote: bool = False, config_relpath: str | None = None,
                config_label: str = CONFIG_FILE_NAME, custom_message: str | None = None) -> int:
    """Design §4.5 / §9 `--dry-run`: shows what publish would do and exits
    with the code publish would exit with, from the state it can see
    (offline: the last-known remote state, labelled). The
    message shown is the one publish would commit with `custom_message`."""
    validate_includes(source_root, config, config_label=config_label)
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    if not (publish_dir / ".git").exists():
        return _dry_run_uninitialized(source_root, config, as_json,
                                      custom_message=custom_message)
    report = _preview_locked(source_root, config, remote=remote,
                             config_relpath=config_relpath, command="--dry-run")
    return _render_dry_run(source_root, config, report, custom_message=custom_message,
                           as_json=as_json)


def _message_scan(config: dict, custom_message: str | None, title: str | None,
                  tip: str | None) -> tuple[str, MessageScrub] | None:
    """The user-originated message a preview checks, as
    (what, scrub): `-m` when given, else the title --amend would reuse;
    None when there is neither."""
    if custom_message is not None:
        return "the commit message (-m)", message_scrub(custom_message, config)
    if title is not None:
        return (f"the title --amend would reuse from {(tip or '')[:12]}",
                message_scrub(title, config))
    return None


def _render_dry_run(source_root: Path, config: dict, report: PreviewReport, *,
                    custom_message: str | None, as_json: bool,
                    title: str | None = None) -> int:
    """The human/JSON/quiet `--dry-run` output for `report`; returns the
    exit code publish would exit with. `title` (only #2's --amend preview)
    is the reused title, rendered and scanned when there is no -m. A message
    hit is exit 4 whatever the state predicts: a real publish refuses -m
    before it fetches."""
    branch = config["branch"]
    source_hash, is_dirty = get_source_info(source_root)
    proj, plan = report.proj, report.plan
    files = sorted(p for p, e in proj.entries.items() if not e.keep)
    transformed = sorted(p for p, e in proj.entries.items() if e.transformed)
    forbidden = config.get("scrub", {}).get("forbidden", [])
    blocking = report.blocking()
    scan = _message_scan(config, custom_message, title, report.baseline.tip)
    hit = scan if scan is not None and scan[1].count else None
    code = EXIT_SCRUB_FAILED if hit else report.predicted_exit
    message = None
    if not blocking and not hit:
        message = build_commit_message(
            source_hash, custom_message=custom_message if custom_message is not None else title,
            added=len(plan.adds), modified=len(plan.mods), deleted=len(plan.dels),
            transform_count=len(transformed), scrub_patterns=len(forbidden))

    if as_json:
        scrub_json = {"passed": not blocking and not hit,
                      "matches": [_finding_json(f) for f in blocking]}
        if scan is not None:
            scrub_json["message"] = {"count": scan[1].count, "lines": list(scan[1].lines)}
        print(json.dumps({
            "files": files,
            "file_count": len(files),
            "source": {"commit": source_hash, "dirty": is_dirty},
            "transforms": transformed,
            "scrub": scrub_json,
            "state": report.state,
            "baseline": {"sha": report.base, "label": report.baseline.label},
            **_preview_json(report),
            "commit_message": message,
        }, indent=2))
        return code

    if ui.quiet:
        # §4: success is silent; failures are errors on stderr, so
        # `--dry-run --quiet` stays usable as a pre-push gate.
        for f in blocking:
            ui.error(f"SCRUB WOULD FAIL: {_finding_label(f)}: {f.detail}")
        if hit:
            ui.error("SCRUB WOULD FAIL: " + message_scrub_text(*hit))
        note = _state_note(report)
        if note:
            ui.error(f"publish would exit {report.predicted_exit}: {note}")
        return code

    print(f"Files to publish ({len(files)}):")
    for f in files:
        print(f"  {f}")
    transforms = config.get("transforms", {})
    if transformed:
        print(f"\nTransforms ({len(transformed)}):")
        for f in transformed:
            n_rules = len(transforms[f])
            print(f"  {f} ({n_rules} rule{'s' if n_rules != 1 else ''})")
    print()
    for line in _preview_lines(report, branch):
        print(line)
    if message is not None:
        # A blocked publish commits nothing, so there is no message to show
        # (its "Scrub: … passed" line would contradict SCRUB WOULD FAIL).
        print()
        print("Commit message:")
        for line in message.rstrip().split("\n"):
            print(f"  {line}")
    gate = _gate_lines(report, "SCRUB WOULD FAIL")
    if hit:
        gate.append("SCRUB WOULD FAIL: " + message_scrub_text(*hit))
    if gate or forbidden:
        print()
        for line in gate:
            print(line)
        if blocking or hit:
            print("\nPublish will be blocked until these are resolved.")
        elif forbidden:
            print(f"Scrub: passed ({len(forbidden)} patterns checked)")
    _report_carried(report.carried)
    return code


MAX_DIFF_BYTES = 1024 * 1024


def _is_binary_bytes(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def cmd_diff(source_root: Path, config: dict, *, remote: bool = False,
             config_relpath: str | None = None, config_label: str = CONFIG_FILE_NAME,
             custom_message: str | None = None) -> int:
    """Render what the WORLD would see change (T17): a unified diff from the
    git-tree baseline to the snapshot publish would build (design §4.5; bug
    #7). Never publishes. Exit per §9 `--diff`: 0, or 4 when the gate blocks
    or publish would refuse `custom_message` (usability #3; stdout stays a
    pure diff, the message line goes to stderr)."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    if not (publish_dir / ".git").exists():
        raise ConfigError(f"ERROR: {publish_dir.name}/ not initialized\n"
                          f"Run: {_PROG} init")
    validate_includes(source_root, config, config_label=config_label)
    try:
        report = _preview_locked(source_root, config, remote=remote,
                                 config_relpath=config_relpath, command="--diff")
    except PublishError as e:
        # §9 S5: `--diff` → 0, labelled, as `--stage`.
        if not remote:
            raise
        ui.warn(f"WARNING: {str(e).splitlines()[0].removeprefix('ERROR: ')}; "
                f"diffing against the last-known remote state")
        report = _preview_locked(source_root, config, remote=False,
                                 config_relpath=config_relpath, command="--diff")
    plan = report.plan
    code = EXIT_SCRUB_FAILED if report.blocking() else EXIT_SUCCESS
    scan = _message_scan(config, custom_message, None, None)
    hit = scan if scan is not None and scan[1].count else None
    if not (plan.adds or plan.mods or plan.dels):
        print("No changes to publish")
        return _diff_message_refusal(hit) or code
    before = _tree_entries(publish_dir, report.base)
    after = _tree_entries(publish_dir, plan.tree)
    changed = {p for p, _ in plan.adds} | {p for p, _ in plan.mods}
    contents = _read_blobs(publish_dir, sorted(
        {after[p][2] for p in changed} | {before[p][2] for p in changed if p in before}))
    chunks = []
    for f in sorted(changed):
        new_bytes = contents[after[f][2]]
        old_bytes = contents[before[f][2]] if f in before else b""
        if _is_binary_bytes(new_bytes) or _is_binary_bytes(old_bytes):
            chunks.append(f"Binary files differ: {f}")
            continue
        if len(new_bytes) > MAX_DIFF_BYTES or len(old_bytes) > MAX_DIFF_BYTES:
            chunks.append(f"{f}: diff suppressed (large file)")
            continue
        diff = difflib.unified_diff(
            old_bytes.decode(errors="surrogateescape").splitlines(keepends=True),
            new_bytes.decode(errors="surrogateescape").splitlines(keepends=True),
            fromfile=f"a/{f}" if f in before else "/dev/null",
            tofile=f"b/{f}",
        )
        chunks.append("".join(diff).rstrip("\n"))
    for f, _reason in sorted(plan.dels):
        chunks.append(f"Deleted: {f}")
    print("\n".join(chunks))
    return _diff_message_refusal(hit) or code


def _diff_message_refusal(hit: tuple[str, MessageScrub] | None) -> int | None:
    """`--diff -m` with a forbidden string: 4, with the line on stderr after
    the diff on stdout; None when the message is clean."""
    if hit is None:
        return None
    sys.stdout.flush()
    ui.error("SCRUB WOULD FAIL: " + message_scrub_text(*hit))
    return EXIT_SCRUB_FAILED


def _materialize(publish_dir: Path, tree: str, keep: list[str]) -> None:
    """Write `tree` into the `.publish/` worktree through a temporary
    index (`read-tree <tree>`, `checkout-index -a -f`), then remove worktree
    files the tree lacks. `.git` and top-level keep files are preserved; the
    real index is never touched."""
    kept = {}
    for name in keep:
        path = publish_dir / name
        if path.is_file() and not path.is_symlink():
            kept[name] = (path.read_bytes(), path.stat().st_mode)
    with _temp_index(publish_dir) as env:
        git("read-tree", tree, cwd=str(publish_dir), env=env)
        git("checkout-index", "-a", "-f", cwd=str(publish_dir), env=env)
        listed = set(git("ls-files", "-z", cwd=str(publish_dir), env=env).stdout.split("\0"))
    for root, dirs, files in os.walk(publish_dir, topdown=False):
        rel_root = os.path.relpath(root, publish_dir)
        if rel_root == ".git" or rel_root.startswith(".git" + os.sep):
            continue
        for name in files + [d for d in dirs if os.path.islink(os.path.join(root, d))]:
            rel = name if rel_root == "." else posixpath.join(*rel_root.split(os.sep), name)
            if rel not in listed and rel.split("/", 1)[0] not in keep:
                os.unlink(os.path.join(root, name))
        if rel_root != "." and not os.listdir(root):
            os.rmdir(root)
    for name, (data, mode) in kept.items():
        path = publish_dir / name
        path.write_bytes(data)
        os.chmod(path, stat.S_IMODE(mode))


def cmd_stage(source_root: Path, publish_dir: Path, config: dict, *, as_json: bool,
              remote: bool, config_relpath: str | None) -> int:
    """`--stage` (design §4.5): materialize exactly
    the tree publish would build — on the IP tree when tip != IP — into the
    `.publish/` worktree, and report. No commit, push, log, tag or record;
    the real index is unchanged. Exit per §9 `--stage`: 0, or 4 when the
    gate blocks (the content is still materialized for inspection)."""
    branch = config["branch"]
    last = time.monotonic()

    def mark(phase: str) -> None:
        nonlocal last
        now = time.monotonic()
        ui.detail(f"timing: {phase} {now - last:.2f}s")
        last = now

    with source_lock(source_root, exclusive=True), publish_lock(publish_dir):
        _check_publish_branch(publish_dir, branch)
        assert_no_pull_in_progress(source_root, command="--stage")
        try:
            report = preview(source_root, publish_dir, config, remote=remote,
                             config_relpath=config_relpath)
        except PublishError as e:
            if not remote:
                raise
            ui.warn(f"WARNING: {str(e).splitlines()[0].removeprefix('ERROR: ')}; "
                    f"staging against the last-known remote state")
            report = preview(source_root, publish_dir, config, remote=False,
                             config_relpath=config_relpath)
        proj, plan = report.proj, report.plan
        files = sum(1 for e in proj.entries.values() if not e.keep)
        ui.info(f"Rebuilding {publish_dir.name}/ ({files} files)... ok")
        transformed = sorted(p for p, e in proj.entries.items() if e.transformed)
        if transformed:
            ui.info(f"Transforming ({len(transformed)})... ok")
        mark("rebuild")
        _materialize(publish_dir, plan.tree, config.get("keep", []))
        mark("stage")

    source_hash, is_dirty = get_source_info(source_root)
    forbidden = config.get("scrub", {}).get("forbidden", [])
    blocking = report.blocking()
    statuses = sorted([("A", p) for p, _ in plan.adds] + [("M", p) for p, _ in plan.mods]
                      + [("D", p) for p, _ in plan.dels], key=lambda x: x[1])
    ref = None if report.base is None else ("tip" if report.base == report.integ.tip else "ip")
    common = {
        "baseline": {"ref": ref, "sha": report.base},
        "files_copied": files,
        "transforms_applied": transformed,
        "source": {"commit": source_hash, "dirty": is_dirty},
        "publish_dir": publish_dir.name,
        **({"release_decision_required": True} if release_enabled(config) else {}),
        **_preview_json(report),
    }
    if blocking:
        if as_json:
            print(json.dumps({"staged": False, "changed": None, "changes": None,
                              "scrub": {"passed": False, "patterns": len(forbidden),
                                        "matches": [_finding_json(f) for f in blocking]},
                              **common}))
            return EXIT_SCRUB_FAILED
        for line in _gate_lines(report, "SCRUB FAILED"):
            ui.error(line)
        return EXIT_SCRUB_FAILED
    if as_json:
        print(json.dumps({
            "staged": True,
            "changed": bool(statuses),
            "changes": {"added": len(plan.adds), "modified": len(plan.mods),
                        "deleted": len(plan.dels),
                        "files": [{"status": s, "path": p} for s, p in statuses]},
            "scrub": {"passed": True, "patterns": len(forbidden)},
            **common,
        }))
        return EXIT_SUCCESS
    for line in _preview_lines(report, branch):
        ui.info(line)
    for line in _gate_lines(report, "SCRUB FAILED"):
        ui.info(line)
    _report_carried(report.carried)
    if statuses:
        parts = [f"{n} {word}" for n, word in ((len(plan.adds), "added"),
                                              (len(plan.mods), "modified"),
                                              (len(plan.dels), "deleted")) if n]
        ui.success(f"Staged: {', '.join(parts)}")
        for status, path in statuses:
            ui.info(f"  {status}  {path}")
        ui.info(f"Inspect with: git -C {shlex.quote(publish_dir.name)} status --short")
    else:
        ui.success("Nothing to publish")
    if release_enabled(config):
        ui.info("Release management: the next real publish requires --release, --bump, "
                "or --no-release")
    return EXIT_SUCCESS


STATUS_CONTENDED_NOTE = "another pubrepo command is running; showing last known state"


def cmd_status(source_root: Path, config: dict, as_json: bool = False,
               check: bool = False, remote: bool = False,
               config_relpath: str | None = None,
               config_label: str = CONFIG_FILE_NAME) -> int:
    """Dashboard (T16) on the git-tree baseline: config → source →
    last publish → pending → gate → public state. Offline by default, using
    the last-known remote state (labelled); --remote fetches first.
    --check precedence (design §9): 4 > 8 > 5 > 6 > 0. A config error, such
    as a missing include path, is 1 before any state is evaluated, as in
    publish and --dry-run."""
    validate_includes(source_root, config, config_label=config_label)
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    branch = config["branch"]

    if not (publish_dir / ".git").exists():
        if as_json:
            print(json.dumps({"status": "not_initialized"}, indent=2))
        else:
            print(f"Not initialized. Run: {_PROG} init")
        return EXIT_PENDING_CHANGES if check else EXIT_SUCCESS

    info = get_last_publish_info(publish_dir)
    source_hash, is_dirty = get_source_info(source_root)
    source_branch = _source_branch(source_root)
    in_progress = pull_in_progress(source_root)
    # Design §9 S4 is this worktree's pull; another worktree's gets the
    # §5.12 notice.
    pull_here = (in_progress is not None
                 and in_progress.worktree == _worktree_toplevel(source_root))
    remote_error = None
    contended = False
    try:
        with source_lock(source_root, exclusive=False):
            try:
                report = preview(source_root, publish_dir, config, remote=remote,
                                 config_relpath=config_relpath)
            except PublishError as e:
                if not remote:
                    raise
                remote_error = str(e).splitlines()[0].removeprefix("ERROR: ")
                report = preview(source_root, publish_dir, config, remote=False,
                                 config_relpath=config_relpath)
    except LockContention:
        # Design §5.13, AC42: status never waits and never fails on
        # contention. Without the lock it reads the last-known state only: no
        # fetch, even with --remote. Exit codes follow that state.
        contended = True
        report = preview(source_root, publish_dir, config, remote=False,
                         config_relpath=config_relpath)
    plan = report.plan
    added = sorted(p for p, _ in plan.adds)
    modified = sorted(p for p, _ in plan.mods)
    deleted = sorted(p for p, _ in plan.dels)
    pending = bool(added or modified or deleted)
    porcelain = git("status", "--porcelain", cwd=str(publish_dir), check=False)
    publish_dir_dirty = bool(porcelain.stdout.strip())
    blocking = report.blocking()
    forbidden = config.get("scrub", {}).get("forbidden", [])
    unsynced = unsynced_count(source_root, config) if remote and not contended else None
    remote_info = None
    if remote and contended:
        remote_info = {"checked": False, "tip": report.baseline.tip, "unpulled": None,
                       "note": STATUS_CONTENDED_NOTE}
    elif remote and remote_error is None:
        remote_info = {"checked": True, "tip": report.baseline.tip,
                       "unpulled": len(report.integ.unpulled)}
    elif remote:
        remote_info = {"checked": True, "tip": None, "unpulled": None, "error": remote_error}
    # §4.5, INV-8: only --remote (with the lock) reads the remote.
    release_info = (release_status(source_root, config,
                                   remote=remote and not contended and remote_error is None,
                                   tip=report.baseline.tip)
                    if release_enabled(config) else None)

    if as_json:
        output = {
            "status": "changes_pending" if (pending or publish_dir_dirty) else "up_to_date",
            "config": {
                "remote": config.get("remote"),
                "branch": branch,
                "dir": config.get("dir", PUBLISH_DIR_NAME),
                "include": len(config.get("include", [])),
                "exclude": len(config.get("exclude", [])),
                "keep": len(config.get("keep", [])),
                "forbidden": len(forbidden),
                "transforms": len(config.get("transforms", {})),
            },
            "source": {"commit": source_hash, "branch": source_branch, "dirty": is_dirty},
            "last_publish": info,
            "changes": {
                "added": added,
                "modified": modified,
                "deleted": deleted,
                "publish_dir_dirty": publish_dir_dirty,
            },
            "scrub": {
                "passed": not blocking,
                "matches": [_finding_json(f) for f in blocking],
            },
            "remote": remote_info,
            **_preview_json(report),
        }
        if release_info is not None:
            output["release"] = release_info
        if contended:
            output["contended"] = True
            output["note"] = STATUS_CONTENDED_NOTE
        print(json.dumps(output, indent=2))
    else:
        print("Config:")
        print(f"  Remote:   {config.get('remote')}")
        print(f"  Branch:   {branch}")
        print(f"  Dir:      {config.get('dir', PUBLISH_DIR_NAME)}")
        print(f"  Rules:    {len(config.get('include', []))} include, "
              f"{len(config.get('exclude', []))} exclude, {len(config.get('keep', []))} keep, "
              f"{len(forbidden)} forbidden, {len(config.get('transforms', {}))} transform file(s)")
        print()
        print("Source:")
        print(f"  Commit:   {source_hash}{'  (dirty)' if is_dirty else ''}")
        print(f"  Branch:   {source_branch or '(detached)'}")
        print()
        if info:
            print(f"Last published: {info['date']}")
            print(f"  Commit:       {info['commit']}")
            print(f"  Source:       {info['source']}")
        else:
            print("Last published: (no commits yet)")
            print(f"  Run `{_PROG} publish` to create the initial publish.")

        print()
        if not pending:
            print("No changes to publish")
        else:
            print("Pending changes:")
            for f in added:
                print(f"  + {f}")
            for f in modified:
                print(f"  M {f}")
            for f in deleted:
                print(f"  - {f}")
            print(f"  ({len(added)} added, {len(modified)} modified, {len(deleted)} deleted)")

        if publish_dir_dirty:
            print()
            print(f"Uncommitted changes in {publish_dir.name}/ (keep edits, a --stage, or "
                  f"a blocked publish) — the next publish will commit the keep edits")

        gate = _gate_lines(report, "SCRUB WARNING")
        if gate or forbidden:
            print()
            for line in gate:
                print(line)
            if not blocking and forbidden:
                print(f"Scrub: passed ({len(forbidden)} patterns checked)")
        _report_carried(report.carried)

        print()
        if contended:
            print(f"Note: {STATUS_CONTENDED_NOTE}")
        if remote_error:
            print(f"Remote: check failed — {remote_error}; showing the last-known state")
        unpulled = len(report.integ.unpulled)
        tip = report.baseline.tip[:12] if report.baseline.tip else "(none)"
        if unpulled:
            print(f"Remote: {unpulled} unpulled commit(s) on origin/{branch} — run "
                  f"`{_PROG} pull` ({report.baseline.label})")
        else:
            print(f"Remote: up to date (origin/{branch} {tip}; {report.baseline.label})")
        if unsynced:
            print(f"Records: {unsynced} not yet synced to the records remote")
        if pull_here:
            print(f"Pull in progress (started in {in_progress.worktree}); finish it with "
                  f"`{_PROG} pull --continue` or `{_PROG} pull --abort`")
        elif in_progress is not None:
            print(f"Note: a pull is in progress in another worktree: "
                  f"{in_progress.worktree}{_owner_status(source_root, in_progress)}")
        note = _state_note(report)
        if note:
            print(f"State: {note}")

        if release_info is not None:
            print()
            print("Release:")
            if release_info.get("error"):
                print(f"  WARNING: {release_info['error']}")
            else:
                offline = release_info.get("as_of") is not None
                if offline:
                    print(f"  ({release_info['as_of']})")
                print(f"  Latest:   {release_info['latest_tag'] or '(none)'}")
                if release_info.get("ahead_snapshots") is not None:
                    print(f"  Ahead:    {release_info['ahead_snapshots']} snapshot(s) of ours, "
                          f"{release_info['ahead_collaborator']} collaborator commit(s)")
                elif release_info["ahead_count"] is not None:
                    print(f"  Ahead:    {release_info['ahead_count']}")
                else:
                    print("  Ahead:    " + (RELEASE_UNKNOWN_OFFLINE
                                            if offline and release_info["latest_tag"]
                                            else "(no baseline)"))
                ancestry = release_info["ancestry_ok"]
                print("  Ancestry: " + ("ok" if ancestry else RELEASE_UNKNOWN_OFFLINE
                                         if ancestry is None else "BROKEN"))
                if release_info["tag_mismatches"] is None:
                    print(f"  Tags:     {RELEASE_UNKNOWN_OFFLINE}")
                if release_info.get("attestation"):
                    print(f"  Record:   {release_info['attestation']}")
                if release_info["tag_mismatches"]:
                    for message in release_info["tag_mismatches"]:
                        print(f"  WARNING: {message}")
                if release_info["journals"]:
                    for journal in release_info["journals"]:
                        print(f"  Journal:  {journal['version']} {journal['state']} ({journal['in_flight'] or 'not started'})")

    if check:
        if blocking:
            return EXIT_SCRUB_FAILED
        if pull_here or report.state in ("S9", "S10b", "S10b+S2"):
            return EXIT_DECISION_REQUIRED
        if _PREDICTED_EXIT.get(report.state) == 5:
            return EXIT_DIVERGED
        if pending or publish_dir_dirty:
            return EXIT_PENDING_CHANGES
    return EXIT_SUCCESS


def cmd_validate(source_root: Path, config: dict, as_json: bool = False,
                 release_check: bool = False, *, config_label: str = CONFIG_FILE_NAME,
                 remote: bool = False) -> int:
    """Report ALL config + environment findings (T15); exit 0 unless errors."""
    if release_check:
        static = validate_config(config) + validate_environment(config, source_root,
                                                                config_label=config_label)
        static_errors = [item for item in static if item["level"] == "error"]
        if static_errors:
            warnings = [item for item in static if item["level"] == "warning"]
            if as_json:
                print(json.dumps({"valid": False, "errors": static_errors,
                                  "warnings": warnings}, indent=2))
            else:
                for item in static_errors:
                    ui.error(f"ERROR [{item['field']}]: {item['message']}")
                for item in warnings:
                    ui.warn(f"WARNING [{item['field']}]: {item['message']}")
            return EXIT_CONFIG_ERROR
        return cmd_validate_release_check(source_root, config, as_json=as_json)
    findings = validate_config(config) + validate_environment(config, source_root,
                                                              config_label=config_label)
    if not [x for x in findings if x["level"] == "error"]:
        findings += private_ref_findings(source_root, config)       # §18.5, offline
        if remote:
            findings += leaked_ref_findings(source_root, config)    # R7, validate --remote
            findings += records_default_findings(source_root, config)   # R6 default
    errors = [x for x in findings if x["level"] == "error"]
    warnings = [x for x in findings if x["level"] == "warning"]
    if as_json:
        print(json.dumps({"valid": not errors, "errors": errors,
                          "warnings": warnings}, indent=2))
    else:
        for x in errors:
            ui.error(f"ERROR [{x['field']}]: {x['message']}")
        for x in warnings:
            ui.warn(f"WARNING [{x['field']}]: {x['message']}")
        if not errors:
            suffix = f" ({len(warnings)} warning(s))" if warnings else ""
            ui.success(f"Config OK: {CONFIG_FILE_NAME}{suffix}")
    return EXIT_CONFIG_ERROR if errors else EXIT_SUCCESS


def _warn_if_publish_dir_tracked(source_root: Path, publish_dir: Path) -> None:
    """The publish dir is tool output; if the SOURCE repo tracks it, every
    source commit snapshots a stale public clone. Warn on every publish
    until fixed — init's one-time NOTE is easy to miss."""
    tracked = git("ls-files", "--", publish_dir.name,
                  cwd=str(source_root), check=False)
    if tracked.returncode == 0 and tracked.stdout.strip():
        n = len(tracked.stdout.splitlines())
        ui.warn(f"WARNING: {publish_dir.name}/ is tracked by the source repo "
                f"({n} files). Untrack it:\n"
                f"  git rm -r --cached {publish_dir.name}\n"
                f"  echo '{publish_dir.name}/' >> .gitignore")


def resolve_republish_mode(cli_amend: bool | None) -> str | None:
    """Design §7: "cli" for an explicit --amend, else None (a new snapshot).
    --no-amend is a no-op with a note; on_republish is retired (config error)."""
    if cli_amend is False:
        ui.info("Note: --no-amend has no effect: publish always adds a new snapshot")
    return "cli" if cli_amend else None


# --- release: transaction ---

@dataclass
class Asset:
    name: str
    path: Path
    sha256: str
    kind: str


@dataclass
class ReleaseContext:
    choice: ReleaseChoice
    version: str
    tag: str
    discovery_snapshot: Discovery
    journal: Journal
    txn_dir: Path
    export_dir: Path
    artifacts: list[Asset]
    adapters: list
    source_root: Path
    publish_dir: Path
    config: dict
    tip: str | None                    # the fetched public tip (== IP) the candidate builds on
    source_sha: str
    public_candidate: str | None = None
    resume: bool = False
    # The candidate's SnapshotPlan (None: tag the tip). Typed `object`: a quoted
    # forward reference in a dataclass breaks loaders that skip sys.modules.
    plan: object = None
    config_relpath: str | None = None
    config_file: Path | None = None
    reuse: dict | None = None          # a superseded attempt's journal data (§8.8 reuse)
    managed_paths: list = field(default_factory=list)   # managed + keep (the export)
    accepted_unscanned: frozenset = frozenset()          # --accept-unscanned paths
    summary_shown: bool = False        # the §8.3 summary was in the confirmation


def publish_set_dirty_paths(source_root: Path, config: dict) -> list[str]:
    entries = [entry.rstrip("/") or "." for entry in config.get("include", [])]
    result = git("status", "--porcelain=v1", "-z", "--ignored",
                 "--untracked-files=all", "--", *entries,
                 cwd=str(source_root), check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"ERROR: cannot check release source cleanliness: "
            f"{sanitize_provider_output(result.stderr.strip())}")
    prefix = _source_prefix(source_root)    # status prints top-level-relative paths (#5)
    dirty = []
    fields = result.stdout.split("\0")
    i = 0
    while i < len(fields):
        record = fields[i]
        if not record:
            i += 1
            continue
        status = record[:2]
        path = _top_to_source(prefix, record[3:])
        if status[0] in "RC" and i + 1 < len(fields):
            i += 1
        if path is not None:
            path = path.replace(os.sep, "/")
            if _path_would_publish(path, config):
                dirty.append(path)
        i += 1
    return sorted(set(dirty))


def source_upstream_matches(source_root: Path, remote: str, branch: str) -> bool:
    local = git("rev-parse", "HEAD", cwd=str(source_root), check=False)
    upstream = git("ls-remote", remote, f"refs/heads/{branch}",
                   cwd=str(source_root), check=False)
    if local.returncode != 0 or upstream.returncode != 0:
        return False
    lines = upstream.stdout.strip().splitlines()
    return bool(lines and lines[0].split()[0] == local.stdout.strip())


def _release_identity_matches(journal: Journal, choice: ReleaseChoice,
                              source_sha: str, config: dict) -> bool:
    data = journal.data
    return (data.get("command") == _choice_command(choice)
            and data.get("source_sha") == source_sha
            and data.get("config_fingerprint") == config_fingerprint(config))


def _dropped_release_refusal(tags: list[str]) -> ReleaseError:
    """Design §8.5: a dropped released target refuses the release (7) and
    offers restore-ancestry or release-retire; --no-release never gets here."""
    first = tags[0]
    return ReleaseError(
        f"ERROR: released tag target(s) are no longer ancestors of public main: "
        f"{', '.join(tags)}\n"
        f"Make it reachable again: `{_PROG} repair restore-ancestry {first} --plan`, "
        f"or record it as retired: `{_PROG} release-retire {first}`.")


def release_preflight(ctx: ReleaseContext, publish_dir: Path, source_root: Path,
                      config: dict, integ: "IntegrationState") -> None:
    """All discoverable release refusals; never mutates a ref (design §8.1,
    §8.5). The candidate needs the fast-forward states S0, S1 or S6 (first
    release to an empty remote) with tip == IP and no items:
    other states refuse with their §9 code ("pull needed" is 5, never 7)."""
    refusal = _state_refusal(integ)
    if refusal is not None:
        raise refusal
    if integ.state not in ("S0", "S1", "S6") or integ.tip != integ.ip or integ.items:
        raise ReleaseError(f"ERROR: a release needs the public tip to be integrated "
                           f"(state {integ.state}); run `{_PROG} pull`, then release")
    current = discover_release_state(publish_dir, source_root, config)
    if not _same_discovery(ctx.discovery_snapshot, current):
        raise ReleaseError("ERROR: public state changed while you were choosing; rerun")
    mismatches = local_tag_mismatches(publish_dir, current.ledger)
    if ctx.resume and ctx.journal.data.get("public_candidate"):
        local_target = git("rev-parse", f"refs/tags/{ctx.tag}^{{}}",
                           cwd=str(publish_dir), check=False)
        if (local_target.returncode == 0
                and local_target.stdout.strip() == ctx.journal.data["public_candidate"]):
            mismatches = [m for m in mismatches if ctx.tag not in m]
    if mismatches:
        raise ReleaseError("ERROR: managed release tag truth differs locally/remotely:\n  "
                           + "\n  ".join(mismatches))
    if current.branch_sha:
        # The candidate descends from the tip, so released targets that are
        # ancestors of the tip stay ancestors (§8.1: no force).
        violations = ancestry_violations(publish_dir, current.ledger, current.branch_sha)
        if violations:
            raise _dropped_release_refusal(violations)

    if ctx.version != current.source_version:
        raise ReleaseError(
            f"ERROR: resolved release {ctx.version} does not match source version "
            f"{current.source_version}; edit and commit {config['release']['version_file']}, then rerun")
    parse_semver(ctx.version)
    pypi = config["release"].get("pypi", {})
    if isinstance(pypi, dict) and pypi.get("enabled"):
        try:
            pep440_prerelease(ctx.version)
        except ValueError as exc:
            raise ReleaseError(f"ERROR: {exc}") from exc
    if ctx.tag in current.ledger.all_tags and not ctx.resume:
        raise ReleaseError(
            f"ERROR: remote tag {ctx.tag} already exists without this clone's incomplete journal; "
            "refusing provider adoption for an immutable version; inspect the remote release and adopt it manually")
    if current.ledger.latest and semver_key(ctx.version) <= semver_key(current.ledger.latest) \
            and not ctx.resume:
        raise ReleaseError(
            f"ERROR: release {ctx.version} is not strictly greater than latest managed version "
            f"{current.ledger.latest}; version identities are immutable")
    dirty = publish_set_dirty_paths(source_root, config)
    if dirty:
        raise ReleaseError("ERROR: release publish set is not committed:\n  " + "\n  ".join(dirty))
    if config["release"].get("source_tag"):
        source_branch = _source_branch(source_root)
        source_remote = config["release"].get("source_remote", "origin")
        # §18.2, R1: source_remote must pass the private-target
        # check before its first network call (the ls-remote below)
        why = private_target_refusal(source_root, config, source_remote)
        if why is not None:
            raise ReleaseError(f"ERROR: refusing to push private ref refs/tags/{ctx.tag} "
                               f"to {source_remote}: {why}")
        if not source_branch or not source_upstream_matches(source_root, source_remote, source_branch):
            raise ReleaseError(
                f"ERROR: source HEAD is not exactly present at {source_remote}/{source_branch or '(detached)'}; push it first")
        source_remote_tag = git(
            "ls-remote", "--tags", source_remote, f"refs/tags/{ctx.tag}",
            f"refs/tags/{ctx.tag}^{{}}", cwd=str(source_root), check=False)
        if source_remote_tag.returncode != 0:
            raise ReleaseError(
                f"ERROR: cannot inspect source tag truth: "
                f"{sanitize_provider_output(source_remote_tag.stderr.strip())}")
        remote_tags = _parse_ls_remote_tags(source_remote_tag.stdout)
        if ctx.tag in remote_tags:
            if remote_tags[ctx.tag] != ctx.source_sha:
                raise ReleaseError(f"ERROR: immutable source tag {ctx.tag} points to a different commit")
            if not ctx.resume:
                raise ReleaseError(f"ERROR: source tag {ctx.tag} already exists without this transaction journal")
        local_tag = git("rev-parse", f"refs/tags/{ctx.tag}^{{}}",
                        cwd=str(source_root), check=False)
        if local_tag.returncode == 0:
            if local_tag.stdout.strip() != ctx.source_sha:
                raise ReleaseError(f"ERROR: local source tag {ctx.tag} points to a different commit")
            if ctx.tag not in remote_tags and not ctx.resume:
                raise ReleaseError(f"ERROR: source tag {ctx.tag} exists only locally; investigate before release")
    for adapter in ctx.adapters:
        findings = adapter.check_live(ctx)
        errors = [f for f in findings if f.get("level") == "error"]
        for finding in findings:
            if finding.get("level") == "warning":
                ui.warn(f"WARNING: {finding['message']}")
        if errors:
            raise ReleaseError("\n".join(f"ERROR: {f['message']}" for f in errors))


def prepare_release_context(choice: ReleaseChoice, snapshot: Discovery,
                            publish_dir: Path, source_root: Path, config: dict,
                            integ: "IntegrationState") -> ReleaseContext:
    version = choice.version
    if not version:
        raise ReleaseError("ERROR: release version could not be resolved")
    tag = render_tag(config["release"]["tag_format"], version)
    source_sha_result = git("rev-parse", "HEAD", cwd=str(source_root), check=False)
    if source_sha_result.returncode != 0:
        raise ReleaseError("ERROR: source has no commit to release")
    source_sha = source_sha_result.stdout.strip()
    existing = load_journal(publish_dir, version)
    # §8.8: reuse is bound to the managed-tree identity (checked at build), not
    # to the source sha: a pull between the attempts adds a source commit.
    reuse = (existing.data if existing and existing.data.get("state") == "superseded"
             and existing.data.get("command") == _choice_command(choice) else None)
    resume = False
    if existing and existing.data.get("state") == "complete":
        if _release_identity_matches(existing, choice, source_sha, config):
            resume = True
        else:
            raise ReleaseError(f"ERROR: release {version} is already complete with a different identity")
    elif existing and existing.data.get("state") in ("active", "terminal") \
            and existing.data.get("remote_writes"):
        if not _release_identity_matches(existing, choice, source_sha, config):
            raise ReleaseError(
                f"ERROR: release {version} is incomplete with a different identity; resume it or release-abandon {version}")
        resume = True
    journal = existing if resume else new_journal(
        publish_dir, version, command=_choice_command(choice), source_sha=source_sha,
        public_baseline=integ.tip, tag=tag, strategy="new_commit", config=config)
    txn_dir = publish_dir / ".git" / RELEASE_DIR_NAME / version
    ctx = ReleaseContext(
        choice, version, tag, snapshot, journal, txn_dir, txn_dir / "source", [],
        [], source_root, publish_dir, config, integ.tip, source_sha, resume=resume,
        reuse=reuse, accepted_unscanned=frozenset(choice.accept_unscanned),
    )
    if resume:
        ctx.public_candidate = journal.data.get("public_candidate")
        return ctx                       # §8.8: a resume needs only the ancestry check
    ctx.adapters = enabled_adapters(config)
    release_preflight(ctx, publish_dir, source_root, config, integ)
    return ctx


def _projection_archive(publish_dir: Path, treeish: str, managed_paths: Sequence[str]) -> bytes:
    """Steps 1-2 of export_projection: the tar `git archive` writes for the
    projection-only tree of `treeish` (a candidate commit or a snapshot
    tree). git's stderr is sanitized."""
    wanted = set(managed_paths)
    updates = [f"{mode} {oid}\t{path}\0"
               for path, (mode, kind, oid) in sorted(_tree_entries(publish_dir, treeish).items())
               if path in wanted and kind == "blob"]
    tree = _write_tree(publish_dir, None, updates)
    result = _run_git_process(["git", "archive", "--format=tar", tree],
                            cwd=str(publish_dir), env=_clone_env(publish_dir),
                            capture_output=True)
    if result.returncode != 0:
        raise ReleaseError("ERROR: git archive failed: " + sanitize_provider_output(
            result.stderr.decode(errors="replace").strip()))
    return result.stdout


def exported_file_text(publish_dir: Path, treeish: str, managed_paths: Sequence[str],
                       path: str) -> str | None:
    """`path` as the release export will hold it (attributes applied by
    git archive, e.g. ident or eol), or None when the export lacks it
    (not managed or keep, or export-ignore). For the release summary
    the human confirms what ships."""
    import tarfile
    with tarfile.open(fileobj=io.BytesIO(_projection_archive(publish_dir, treeish,
                                                             managed_paths)),
                      mode="r:") as archive:
        for member in archive.getmembers():
            if member.isfile() and posixpath.normpath(member.name) == posixpath.normpath(path):
                handle = archive.extractfile(member)
                return handle.read().decode(errors="replace") if handle else None
    return None


def export_projection(publish_dir: Path, candidate: str, managed_paths: Sequence[str],
                      export_dir: Path) -> None:
    """The release export (design §8.6): only the
    candidate's managed and keep paths, never carried ones.

    1. A projection-only tree: the candidate's entries for `managed_paths`
       in a temporary index (read-tree --empty, then --index-info), then
       write-tree. A carried .gitattributes is absent, so it cannot drop or
       substitute anything; a managed one is in the tree and honored.
    2. `git archive` of that tree into `export_dir`. Every member is checked
       first (absolute paths, `..`, links: refused, nothing written), and
       the extraction uses filter="data".
    git's stderr is sanitized before it is surfaced."""
    if export_dir.exists():
        shutil.rmtree(export_dir)
    export_dir.mkdir(parents=True)
    import tarfile
    root = export_dir.resolve()
    data = _projection_archive(publish_dir, candidate, managed_paths)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        for member in archive.getmembers():
            _check_archive_member(member.name, root, "the release export")
            if not (member.isfile() or member.isdir()):
                raise ReleaseError(f"ERROR: unsafe release archive member: {member.name}")
        archive.extractall(export_dir, filter="data")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_obj:
        for chunk in iter(lambda: file_obj.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def export_manifest(export_dir: Path) -> list[dict[str, str]]:
    """Return the exact regular-file identity of a retained candidate export."""
    manifest = []
    for path in sorted(export_dir.rglob("*")):
        if path.is_symlink():
            raise ReleaseError(f"ERROR: retained release export contains a symlink: {path}")
        if path.is_file():
            manifest.append({
                "path": path.relative_to(export_dir).as_posix(),
                "sha256": _sha256(path),
            })
    return manifest


def compute_assets(ctx: ReleaseContext, config: dict) -> list[Asset]:
    github = config["release"].get("github", {})
    if not isinstance(github, dict) or not github.get("enabled"):
        return list(ctx.artifacts)
    # the notes ship from the export; checked here, before the
    # first remote write, not at draft time after the tag is public.
    notes_file = github.get("notes_file", "CHANGELOG.md")
    notes = (ctx.export_dir / notes_file).resolve()
    if not notes.is_relative_to(ctx.export_dir.resolve()) or not notes.is_file():
        raise ReleaseError(f"ERROR: release notes file {notes_file} is not in the release export")
    assets = []
    for relative in github.get("assets", []):
        path = (ctx.export_dir / relative).resolve()
        if not path.is_relative_to(ctx.export_dir.resolve()) or not path.is_file():
            raise ReleaseError(f"ERROR: GitHub release asset missing from candidate export: {relative}")
        assets.append(Asset(Path(relative).name, path, _sha256(path), "candidate"))
    if github.get("include_distributions", False):
        assets.extend(a for a in ctx.artifacts if a.kind == "distribution")
    checksum_name = github.get("checksum_file")
    if checksum_name:
        checksum_path = ctx.txn_dir / checksum_name
        checksum_path.parent.mkdir(parents=True, exist_ok=True)
        checksum_path.write_text("".join(f"{asset.sha256}  {asset.name}\n" for asset in assets))
        assets.append(Asset(Path(checksum_name).name, checksum_path,
                            _sha256(checksum_path), "checksum"))
    names = [asset.name for asset in assets]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ReleaseError("ERROR: GitHub asset names collide: " + ", ".join(duplicates))
    return assets


def render_release_summary(ctx: ReleaseContext) -> str:
    providers = ", ".join(adapter.name for adapter in ctx.adapters) or "git only"
    assets = "\n".join(f"  {a.name}  {a.sha256}" for a in ctx.artifacts) or "  (none)"
    return (f"Release summary\n  Source: {ctx.source_sha}\n"
            f"  Public: {ctx.tip or '(unborn)'} -> {ctx.public_candidate}\n"
            f"  Version/tag: {ctx.version} / {ctx.tag}\n  Providers: {providers}\n"
            f"  Assets:\n{assets}")


def _push_candidate(ctx: ReleaseContext) -> None:
    """The release branch push (design §8.1, §8.8): the candidate sha
    as a plain fast-forward (push_snapshot), confirmed by ancestry, and only
    then the local branch moves and the snapshot record is written. A
    rejection leaves every ref where it was."""
    branch, candidate = ctx.config["branch"], ctx.public_candidate
    if candidate != ctx.tip:
        outcome = push_snapshot(ctx.publish_dir, candidate, branch)
        if outcome.status == "rejected_nonff":
            raise _CandidateRejected(branch, outcome.reason)    # §8.8: superseded
        refusal = _push_refusal(outcome, candidate, branch)
        if refusal is not None:
            raise refusal
        if outcome.status == "ok":
            _confirm_pushed(ctx.publish_dir, candidate, branch)
        advance_local(ctx.publish_dir, branch, candidate, ctx.config.get("keep", []))
        if ctx.plan is not None:
            destination = destination_identity(ctx.config)
            try:
                write_snapshot_record(
                    ctx.source_root, snapshot_sha=candidate, destination=destination,
                    sigma_full=_source_full_head(ctx.source_root) or ctx.source_sha,
                    written=ctx.plan.written, nonderivable=ctx.plan.nonderivable,
                    keep=ctx.plan.keep, config_path=ctx.config_relpath,
                    fingerprint=config_fingerprint(ctx.config), decided=ctx.plan.decided)
                register_target(ctx.source_root,
                                str(ctx.config_file or ctx.source_root / CONFIG_FILE_NAME),
                                destination)
            except (PubrepoError, OSError) as e:
                ui.warn(f"WARNING: released {candidate[:12]}, but its record could not be "
                        f"written ({e}); the next run identifies it by its Source: trailer")
            records_push_pending(ctx.source_root, ctx.config)
    journal_record(ctx.journal, "branch_push", remote=True, public_branch=candidate)


def _rollback_created_tag(repo: Path, tag: str, journal: Journal | None) -> None:
    """Delete a tag this run created, and drop its journal marker."""
    git("tag", "-d", tag, cwd=str(repo), check=False)
    if journal is not None:
        journal.data["local_tags_created"] = [
            t for t in journal.data.get("local_tags_created", [])
            if (t.get("repo"), t.get("tag")) != (str(repo), tag)]
        journal_write(journal)


def _foreign_local_release_tag(ctx: "ReleaseContext") -> str | None:
    """A local refs/tags/<tag> in
    .publish is reused only when this release's journal records creating
    that exact tag object; otherwise its tagger and message would be
    published as they are. The reason, or None."""
    local = git("rev-parse", "--verify", "-q", f"refs/tags/{ctx.tag}",
                cwd=str(ctx.publish_dir), check=False)
    if local.returncode != 0:
        return None
    recorded = {t.get("object") for t in ctx.journal.data.get("local_tags_created", [])
                if (t.get("repo"), t.get("tag")) == (str(ctx.publish_dir), ctx.tag)}
    if local.stdout.strip() in recorded:
        return None
    return (f"local tag {ctx.tag} in {ctx.publish_dir.name}/ was not created by this release "
            f"(its tag object is not the one the journal records), so it is not published; "
            f"inspect it, then delete it (git -C {ctx.publish_dir.name} tag -d {ctx.tag}) "
            f"and rerun")


def _push_annotated_tag(repo: Path, remote: str, tag: str, sha: str,
                        message: str, publish_clone: bool = False,
                        journal: Journal | None = None) -> None:
    """Create (if absent) and push an annotated release tag. A tag
    this call creates is recorded in the journal before the push, and is
    deleted again if the push fails or raises (lock, outcome unknown,
    interrupt), so a failed run leaves tags as they were."""
    remote_result = git("ls-remote", "--tags", remote, f"refs/tags/{tag}",
                        f"refs/tags/{tag}^{{}}", cwd=str(repo), check=False)
    if remote_result.returncode != 0:
        raise ReleaseError(
            f"ERROR: cannot reconcile tag {tag}: "
            f"{sanitize_provider_output(remote_result.stderr.strip())}")
    remote_tags = _parse_ls_remote_tags(remote_result.stdout)
    if tag in remote_tags:
        if remote_tags[tag] != sha:
            raise ReleaseError(f"ERROR: immutable tag {tag} points to a different commit")
        return
    local = git("rev-parse", f"refs/tags/{tag}^{{}}", cwd=str(repo), check=False)
    if local.returncode == 0 and local.stdout.strip() != sha:
        raise ReleaseError(f"ERROR: local tag {tag} points to a different commit")
    created = False
    if local.returncode != 0:
        result = git("tag", "-a", tag, sha, "-m", message, cwd=str(repo), check=False)
        if result.returncode != 0:
            raise ReleaseError(
                f"ERROR: cannot create annotated tag {tag}: "
                f"{sanitize_provider_output(result.stderr.strip())}")
        created = True
        if journal is not None:          # the marker precedes the next failing call
            # the tag OBJECT this release made, so a resume reuses
            # only that object, never a look-alike made by hand
            obj = git("rev-parse", f"refs/tags/{tag}", cwd=str(repo)).stdout.strip()
            journal.data.setdefault("local_tags_created", []).append(
                {"repo": str(repo), "tag": tag, "remote": remote, "sha": sha, "object": obj})
            journal_write(journal)
    try:
        if publish_clone:          # through push_porcelain
            outcome = push_porcelain(repo, [f"refs/tags/{tag}:refs/tags/{tag}"])
            ok, why = outcome.status in ("ok", "ambiguous_landed"), outcome.reason
        else:
            result = git("push", "--recurse-submodules=no", remote,     # (S)
                         f"refs/tags/{tag}", cwd=str(repo), check=False)
            ok, why = result.returncode == 0, result.stderr.strip()
    except BaseException:
        if created:                      # a raising push rolls back too
            _rollback_created_tag(repo, tag, journal)
        raise
    if not ok:
        if created:                      # roll back the tag this call made
            _rollback_created_tag(repo, tag, journal)
        raise ReleaseError(
            f"ERROR: cannot push immutable tag {tag}: {sanitize_provider_output(why)}")


def _release_audit_tag(ctx: ReleaseContext) -> None:
    """The release journal's audit_tag stage (§18.3, R2, R6): the bookmark
    of the released source, pushed only to a checked tag_remote. The
    journal keys stay: audit_tag = the bookmark ref, audit_tag_pushed."""
    if "audit_tag" in ctx.journal.data.get("completed", []):
        return
    ref = write_bookmark(ctx.source_root)
    pushed = ref is not None and push_bookmark(ctx.source_root, ctx.config, ref)
    journal_record(ctx.journal, "audit_tag", remote=pushed, audit_tag=ref,
                   audit_tag_pushed=pushed)


def _release_log_entry(ctx: ReleaseContext) -> dict:
    return {
        "ts": datetime.now().astimezone().isoformat(),
        "source_commit": ctx.source_sha,
        "source_branch": _source_branch(ctx.source_root),
        "source_dirty": False,
        "pushed": True,
        "public_commit": ctx.public_candidate,
        "forced": False,
        "release": {
            "attempt_id": ctx.journal.data.get("attempt_id"),
            "version": ctx.version,
            "tag": ctx.tag,
            "public_tag_sha": ctx.public_candidate,
            "source_tag": (ctx.source_sha
                           if ctx.config["release"].get("source_tag") else None),
            "providers": [adapter.name for adapter in ctx.adapters],
        },
    }


def _append_release_log_once(ctx: ReleaseContext) -> None:
    """Append the release audit record idempotently across completion retries."""
    attempt_id = ctx.journal.data.get("attempt_id")
    path = ctx.publish_dir / ".git" / PUBLISH_LOG_NAME
    if path.exists() and attempt_id:
        try:
            for line in path.read_text().splitlines():
                entry = json.loads(line)
                if entry.get("release", {}).get("attempt_id") == attempt_id:
                    return
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError(
                f"ERROR: cannot reconcile release audit log {path}: {exc}") from exc
    try:
        _append_publish_log(ctx.publish_dir, _release_log_entry(ctx))
    except OSError as exc:
        raise ReleaseError(f"ERROR: cannot append release audit log: {exc}") from exc


def run_release_transaction(ctx: ReleaseContext) -> int:
    journal = ctx.journal
    completed = journal.data.get("completed", [])
    # §8.8: a provider release object is ours only if this attempt's journal,
    # as it stood when the run began, shows the draft step started or done.
    draft_ours = ("github:draft" in completed
                  or journal.data.get("in_flight") == "github:draft")
    if journal.data.get("state") == "complete":
        ui.success(f"Release {ctx.version} already complete")
        return EXIT_SUCCESS
    release = ctx.config["release"]
    plan = build_push_plan(ctx.source_root, ctx.publish_dir, ctx.config, kind="release", refs={
        "public": [f"refs/heads/{ctx.config['branch']}", f"refs/tags/{ctx.tag}"],
        "bookmark": "audit_tag" not in completed,
        "source_tag": ctx.tag if release.get("source_tag") else None,
        "records": records_remote_name(ctx.source_root, ctx.config)})
    refused = [e for e in plan if e.ref == f"refs/tags/{ctx.tag}" and e.cls == "S"
               and e.refusal and "source_tag" not in completed]
    if refused:                                 # §18.2: checked again at the push site
        raise ReleaseError(f"ERROR: refusing to push private ref {refused[0].ref} to "
                           f"{refused[0].remote}: {refused[0].refusal}")
    foreign = None if "public_tag" in completed else _foreign_local_release_tag(ctx)
    if foreign is not None:                     # before any push
        raise ReleaseError(f"ERROR: {foreign}")
    try:
        if "branch_push" not in completed:
            journal.data["in_flight"] = "branch_push"
            journal_write(journal)
            _push_candidate(ctx)
        if "public_tag" not in completed:
            journal.data["in_flight"] = "public_tag"
            journal_write(journal)
            _push_annotated_tag(ctx.publish_dir, "origin", ctx.tag,
                                ctx.public_candidate,
                                release_tag_message(ctx.version, ctx.source_sha),
                                publish_clone=True, journal=ctx.journal)
            journal_record(journal, "public_tag", remote=True,
                           public_tag_sha=ctx.public_candidate)
        release = ctx.config["release"]
        if release.get("source_tag") and "source_tag" not in completed:
            journal.data["in_flight"] = "source_tag"
            journal_write(journal)
            remote = release.get("source_remote", "origin")
            _push_annotated_tag(ctx.source_root, remote, ctx.tag, ctx.source_sha,
                                release_tag_message(ctx.version, ctx.source_sha),
                                journal=ctx.journal)
            journal_record(journal, "source_tag", remote=True, source_tag=ctx.source_sha)
        if "audit_tag" not in completed:
            _release_audit_tag(ctx)
        github = next((a for a in ctx.adapters if a.name == "github"), None)
        pypi = next((a for a in ctx.adapters if a.name == "pypi"), None)
        for adapter, stage in ((github, "draft"), (github, "upload"),
                               (pypi, "upload"), (github, "publish")):
            step = f"{adapter.name}:{stage}" if adapter else None
            if adapter and step not in journal.data.get("completed", []):
                journal.data["in_flight"] = step
                journal_write(journal)
                adapter.reconcile(ctx)
                if step == "github:draft" and getattr(adapter, "view", None) is not None \
                        and not draft_ours:
                    reason = (f"GitHub release {ctx.tag} already exists and was not created "
                              f"by this release; remove it on GitHub, or release a new version")
                    journal.data["state"] = "terminal"
                    journal.data["terminal_reason"] = reason
                    journal_write(journal)
                    raise ReleaseError(f"ERROR: {reason}")
                adapter.execute(ctx, stage)
                journal_record(journal, step, remote=True)
        if "audit_log" not in journal.data.get("completed", []):
            journal.data["in_flight"] = "complete"
            journal_write(journal)
            _append_release_log_once(ctx)
            journal_record(journal, "audit_log")
        journal.data["state"] = "complete"
        journal.data["in_flight"] = "complete"
        journal_write(journal)
    except (PublishError, Diverged):
        raise                            # §8.8: "pull needed" is 5, transport 2, never 7
    except (Exception, KeyboardInterrupt) as exc:
        if not journal.data.get("remote_writes"):
            # no ref moved before the confirmed push, so nothing to undo.
            if isinstance(exc, KeyboardInterrupt):
                raise
            raise ReleaseError(str(exc)) from exc
        # A failed final atomic write must leave a resumable journal on disk,
        # not persist the in-memory "complete" state during error handling.
        if journal.data.get("in_flight") == "complete":
            journal.data["state"] = "active"
        with suppress(Exception):
            journal_write(journal)
        cause = (str(exc).splitlines()[0].removeprefix("ERROR: ") if str(exc).strip()
                 else type(exc).__name__)
        if journal.data.get("state") == "terminal":
            raise ReleaseError(
                f"ERROR: release {ctx.version} stopped at {journal.data.get('in_flight')}: "
                f"{cause}\nResolve it, then `{_PROG} release-abandon {ctx.version}` before "
                f"releasing again") from exc
        raise ReleaseError(
            f"ERROR: release incomplete at {journal.data.get('in_flight')} ({cause}); "
            f"rerun the identical command '{_choice_command(ctx.choice)}'") from exc
    ui.success(f"Released {ctx.version} ({ctx.tag})")
    return EXIT_SUCCESS


def finish_release_build(ctx: ReleaseContext, candidate: str, identity: str) -> int:
    """Export, build and journal the candidate (or reuse a superseded
    attempt's artifacts, §8.8), then run the transaction. Nothing
    here moves a ref; a failure leaves refs/heads, HEAD and every tag as
    they were before the run."""
    ctx.public_candidate = candidate
    ctx.journal.data["public_candidate"] = candidate
    ctx.journal.data["managed_tree_identity"] = identity
    journal_record(ctx.journal, "candidate")
    if not _reuse_artifacts(ctx, identity):
        ctx.journal.data["in_flight"] = "export"
        journal_write(ctx.journal)
        export_projection(ctx.publish_dir, candidate, ctx.managed_paths, ctx.export_dir)
        release_scrub_refusal("the release export", scan_shipped_files(ctx.export_dir,
                                                                       ctx.config))
        ctx.journal.data["export_manifest"] = export_manifest(ctx.export_dir)
        journal_record(ctx.journal, "export")
        for adapter in ctx.adapters:
            ctx.journal.data["in_flight"] = f"{adapter.name}:build"
            journal_write(ctx.journal)
            adapter.build(ctx)
            journal_record(ctx.journal, f"{adapter.name}:build")
        ctx.artifacts = compute_assets(ctx, ctx.config)
        # Export files and distributions are scrubbed above; the rest
        # (the generated checksum file) here.
        forbidden = [x for x in (ctx.config.get("scrub") or {}).get("forbidden") or []
                     if isinstance(x, str) and x]
        release_scrub_refusal("the release assets", [
            f for asset in ctx.artifacts if asset.kind not in ("candidate", "distribution")
            for f in scan_bytes(asset.name, asset.path.read_bytes(), forbidden, ctx.config)])
        ctx.journal.data["artifacts"] = [
            {"name": asset.name, "path": str(asset.path), "sha256": asset.sha256,
             "kind": asset.kind} for asset in ctx.artifacts]
        journal_record(ctx.journal, "artifacts")
    print(render_release_summary(ctx), file=sys.stderr)
    return run_release_transaction(ctx)


RELEASE_RETRY_LAPS = 3


def managed_tree_identity(entries: dict[str, tuple[str, str]]) -> str:
    """sha256 over the sorted `mode SP path NUL blob LF` records of the
    managed and keep paths (path → (mode, blob); the mode counts,
    because the exec bit changes the artifacts). It binds the confirmation
    and artifact reuse (design §8.3, §8.8)."""
    h = hashlib.sha256()
    for path in sorted(entries):
        mode, blob = entries[path]
        h.update(f"{mode} {path}\0{blob}\n".encode())
    return h.hexdigest()


class _CandidateRejected(Diverged):
    """The public branch moved between the evaluation and the push (a
    non-fast-forward rejection, §8.8): the attempt is superseded."""

    def __init__(self, branch: str, reason: str):
        super().__init__([branch], reason)
        self.reason = reason


def _superseded_refusal(version: str, reason: str) -> Diverged:
    return Diverged([], f"ERROR: the public branch moved while release {version} was being "
                        f"built ({reason}); nothing was pushed.\n"
                        f"Run `{_PROG} pull`, then rerun the identical command: the artifacts "
                        f"are reused while the managed tree is unchanged.")


def _reuse_artifacts(ctx: ReleaseContext, identity: str) -> bool:
    """§8.8: a superseded attempt's export and artifacts are reused when
    its managed-tree identity equals this one and every retained file is
    unchanged. True when reused (export and build are then skipped)."""
    old = ctx.reuse or {}
    if old.get("managed_tree_identity") != identity or "artifacts" not in old:
        return False
    if not ctx.export_dir.is_dir() or old.get("export_manifest") != export_manifest(ctx.export_dir):
        return False
    artifacts = []
    for item in old["artifacts"]:
        path = Path(item["path"])
        if not path.is_file() or _sha256(path) != item["sha256"]:
            return False
        artifacts.append(Asset(item["name"], path, item["sha256"], item["kind"]))
    ctx.artifacts = artifacts
    ctx.journal.data["export_manifest"] = old["export_manifest"]
    ctx.journal.data["artifacts"] = old["artifacts"]
    journal_record(ctx.journal, "artifacts_reused",
                   reused_from=old.get("attempt_id"))
    ui.info("Reusing the artifacts of the superseded attempt (managed tree unchanged)")
    return True


def _resume_release(ctx: ReleaseContext, tip: str | None) -> int:
    """§8.8 resume: no pull gate; once the branch push is recorded, the
    candidate must still be an ancestor of the public tip. The retained
    artifacts and export must be unchanged (never rebuilt); then the
    transaction continues from its journal."""
    ctx.public_candidate = ctx.journal.data.get("public_candidate")
    if not ctx.public_candidate:
        raise ReleaseError("ERROR: incomplete release journal has no candidate SHA")
    if "branch_push" in ctx.journal.data.get("completed", []) and (
            tip is None or git("merge-base", "--is-ancestor", ctx.public_candidate, tip,
                               cwd=str(ctx.publish_dir), check=False).returncode != 0):
        raise ReleaseError(
            f"ERROR: release {ctx.version}'s candidate {ctx.public_candidate[:12]} is no longer "
            f"on the public branch; make it reachable with `{_PROG} repair restore-ancestry "
            f"{ctx.tag} --plan`, or abandon the attempt: `{_PROG} release-abandon {ctx.version}`")
    is_complete = ctx.journal.data.get("state") == "complete"
    artifacts = []
    for item in ctx.journal.data.get("artifacts", []):
        path = Path(item["path"])
        if not is_complete and (not path.is_file() or _sha256(path) != item["sha256"]):
            raise ReleaseError(
                f"ERROR: retained release artifact missing or changed: {item['name']}; "
                "refusing to rebuild")
        artifacts.append(Asset(item["name"], path, item["sha256"], item["kind"]))
    ctx.artifacts = artifacts
    ctx.adapters = enabled_adapters(ctx.config)
    if not is_complete:
        if not ctx.export_dir.is_dir():
            raise ReleaseError(
                f"ERROR: retained release export is missing: {ctx.export_dir}; "
                "artifacts are immutable and will not be rebuilt")
        expected_manifest = ctx.journal.data.get("export_manifest")
        if not isinstance(expected_manifest, list):
            raise ReleaseError(
                "ERROR: incomplete release journal has no retained export manifest; "
                "refusing to rebuild")
        if export_manifest(ctx.export_dir) != expected_manifest:
            raise ReleaseError(
                f"ERROR: retained release export is missing or changed: "
                f"{ctx.export_dir}; refusing to rebuild")
    else:
        for adapter in ctx.adapters:
            adapter.reconcile(ctx)
            if getattr(adapter, "pending", []):
                raise ReleaseError(
                    f"ERROR: complete journal disagrees with {adapter.name} remote objects")
    return run_release_transaction(ctx)


def _build_release(ctx: ReleaseContext, ev: "_PublishEval", custom_message: str | None) -> int:
    """§8.1-§8.4: the candidate is a snapshot of the projection on the
    public tip (tip == IP). With no change and our own snapshot at the tip,
    the tag goes on the tip itself; otherwise a new snapshot (tree-identical
    when only the tip is foreign) is made. No ref moves here."""
    publish_dir, config = ctx.publish_dir, ctx.config
    integ, proj = ev.integ, ev.proj
    tip = integ.tip
    if not ctx.summary_shown and not ctx.choice.accept_build_changes:
        # §8.3 (AC72): unattended, a build-file change needs the flag (1),
        # checked before the attempt's first write.
        build = release_build_changes(publish_dir, config, ctx.discovery_snapshot.ledger, ev)
        if build:
            raise ConfigError(
                "ERROR: build files changed since the latest managed release (the build "
                "runs them):\n" + "\n".join(_listed(build, indent="  "))
                + "\nReview them, then pass --accept-build-changes, or release on a "
                  "terminal; nothing was written")
    resolve_supersession(publish_dir, ctx.journal)   # the first write of a new attempt
    ctx.managed_paths = sorted(proj.entries)          # managed + keep: what the export holds
    source_hash, is_dirty = get_source_info(ctx.source_root)
    files = sum(1 for e in proj.entries.values() if not e.keep)
    ui.info(f"Rebuilding {publish_dir.name}/ ({files} files)... ok")
    forbidden = config.get("scrub", {}).get("forbidden", [])
    transformed = sorted(p for p, e in proj.entries.items() if e.transformed)
    log_entry = {
        "ts": datetime.now().astimezone().isoformat(), "source_commit": source_hash,
        "source_branch": _source_branch(ctx.source_root), "source_dirty": is_dirty,
        "files": files, "transforms_applied": len(transformed),
        "scrub_patterns": len(forbidden), "forced": False,
        "title": custom_message or None, "amend": False, "amended_from": None,
    }
    plan = build_snapshot_tree(publish_dir, tip, proj, integ, config)
    _gate_publish(publish_dir, ev, plan, config, log_entry)
    _gate_release_carried(publish_dir, plan.tree, proj, config, ctx.accepted_unscanned)
    if not ctx.summary_shown:            # §8.3: before the build
        for line in release_summary_lines(ctx.source_root, publish_dir, config,
                                          ctx.discovery_snapshot.ledger, ev, plan.tree):
            ui.info(line)
    version_file = config["release"]["version_file"]
    shown = git("show", f"{plan.tree}:{version_file}", cwd=str(publish_dir), check=False)
    try:
        if shown.returncode != 0:
            raise ValueError("not in the candidate")
        candidate_version = read_version_from_text(shown.stdout,
                                                   config["release"]["version_pattern"])
    except ValueError as exc:
        raise ReleaseError(f"ERROR: cannot resolve the candidate version from "
                           f"{version_file}: {exc}") from exc
    if candidate_version != ctx.version:
        raise ReleaseError(
            f"ERROR: version mismatch: resolved={ctx.version}, "
            f"source={ctx.discovery_snapshot.source_version}, "
            f"candidate={candidate_version}; edit and commit source, then rerun")
    if plan.adds or plan.mods or plan.dels or tip is None or not integ.is_ours(tip):
        for path, reason in plan.dels:
            ui.info(f"  deleted: {path}" + (f" ({reason})" if reason else ""))
        for path, reason in plan.adds + plan.mods:
            if reason:
                ui.info(f"  {path} ({reason})")
        message = build_commit_message(
            source_hash, custom_message=custom_message, added=len(plan.adds),
            modified=len(plan.mods), deleted=len(plan.dels),
            transform_count=len(transformed), scrub_patterns=len(forbidden))
        assert_parent_provenance(publish_dir, [tip] if tip else [], tip=tip, fetched=set(),
                                 created=set())                   # §18.13
        candidate = snapshot_commit(publish_dir, plan.tree, [tip] if tip else [], message)
        ctx.plan = plan
        ui.info("Committing... ok")
    else:
        candidate = tip                  # §8.4: the tag goes on our existing snapshot
    return finish_release_build(ctx, candidate, _release_identity(ev))


def _gate_release_carried(publish_dir: Path, tree: str, proj: Projection, config: dict,
                          accepted_unscanned: frozenset) -> None:
    """Design §4.6 item 6 (AC24 release half): carried content that publish
    only reports blocks a release (4) unless allowlisted, or, for
    unscannable content, accepted with --accept-unscanned PATH. A forbidden
    string has no accept flag: pull --redecide PATH → remove."""
    carried = GateResult(findings=_carried_findings(publish_dir, tree, proj, config))
    blocking = carried.release_blocking(set(accepted_unscanned))
    if not blocking:
        return
    lines = []
    for f in blocking:
        route = f"{_PROG} pull --redecide {f.path} → remove"
        if f.kind in ("unscannable", "cfb"):
            route += f", or accept it for this release: --accept-unscanned {f.path}"
        lines.append(f"  {_finding_label(f)}: {f.detail} — already public; {route}")
    raise ScrubFailure(
        [{"file": f.path, "part": f.part, "kind": f.kind, "detail": f.detail}
         for f in blocking],
        "ERROR: release blocked by carried content (publish only reports it):\n"
        + "\n".join(lines))


RELEASE_SUMMARY_LIST_MAX = 20          # longer lists end with "… and N more"


def _listed(items: list[str], indent: str = "    ") -> list[str]:
    shown = [indent + item for item in items[:RELEASE_SUMMARY_LIST_MAX]]
    if len(items) > RELEASE_SUMMARY_LIST_MAX:
        shown.append(f"{indent}… and {len(items) - RELEASE_SUMMARY_LIST_MAX} more")
    return shown


def _decisions_since_release(source_root: Path, publish_dir: Path, config: dict,
                             base: str | None) -> "list[Decision]":
    """Pull decisions recorded after the pull that brought the public branch
    up to the last release `base` (None: all)."""
    destination = destination_identity(config)
    if base is not None:
        for rec in pull_records(source_root, destination=destination, first_parent=True):
            if rec.through and git("merge-base", "--is-ancestor", rec.through, base,
                                   cwd=str(publish_dir), check=False).returncode == 0:
                return pull_decisions_since(source_root, rec.through, destination=destination)
    return pull_decisions_since(source_root, None, destination=destination)


def _latest_release_target(config: dict, ledger: Ledger) -> str | None:
    if ledger.latest is None:
        return None
    return ledger.managed.get(render_tag(config["release"]["tag_format"], ledger.latest))


def release_build_changes(publish_dir: Path, config: dict, ledger: Ledger,
                          ev: "_PublishEval") -> list[str]:
    """Design §8.3 (AC72): every export path (managed
    or keep) matching the §5.2 item 3 predicate, except version_file (it
    changes every release and is flagged at pull), whose blob or mode
    differs from the latest managed release's tree. Before the first
    release every such path counts. No pull-decision exemption."""
    version_file = posixpath.normpath(config["release"]["version_file"])
    base = _tree_entries(publish_dir, _latest_release_target(config, ledger))
    return sorted(path for path, entry in ev.proj.entries.items()
                  if path != version_file and is_exec_or_build(path, entry.mode, config)
                  and base.get(path, (None, None, None))[::2] != (entry.mode, entry.blob))


def release_summary_lines(source_root: Path, publish_dir: Path, config: dict, ledger: Ledger,
                          ev: "_PublishEval", tree: str) -> list[str]:
    """Design §8.3, shown before the build (in the confirmation on a
    terminal): since the latest managed release, collaborator commits
    counted separately from our snapshots; the carried paths (public, not
    packaged, §8.6); the release notes; and the pull decisions on flagged
    items (accepted flags)."""
    release = config["release"]
    base = (ledger.managed.get(render_tag(release["tag_format"], ledger.latest))
            if ledger.latest else None)
    since = (f"since {render_tag(release['tag_format'], ledger.latest)}" if base
             else "in the public history (no managed release yet)")
    lines: list[str] = []
    tip = ev.integ.tip
    if tip:
        log = git("log", "-z", "--format=%H%x1f%an%x1f%s", tip, *([f"^{base}"] if base else []),
                  cwd=str(publish_dir)).stdout
        commits = [entry.split("\x1f") for entry in log.split("\0") if entry]
        ours = [sha for sha, _author, _subject in commits if ev.integ.is_ours(sha)]
        theirs = [f"{sha[:12]} {author}: {subject}" for sha, author, subject in commits
                  if not ev.integ.is_ours(sha)]
        lines.append(f"Public commits {since}: {len(ours)} snapshot(s) of ours, "
                     f"{len(theirs)} collaborator commit(s)")
        lines += _listed(theirs)
    carried = sorted(p for p in _tree_entries(publish_dir, tree) if p not in ev.proj.entries)
    lines.append(f"Carried paths (public, not packaged): {len(carried)}")
    lines += _listed(carried)
    flagged = [d for d in _decisions_since_release(source_root, publish_dir, config, base)
               if d.flags]
    lines.append(f"Accepted flags {since}: {len(flagged)}")
    lines += _listed([f"{d.path}: {d.decision} ({', '.join(d.flags)})" for d in flagged])
    build = release_build_changes(publish_dir, config, ledger, ev)
    lines.append(f"Build files changed {since}: {len(build)}"
                 + (" (the build runs them; off a terminal: --accept-build-changes)"
                    if build else ""))
    lines += _listed(build)
    github = release.get("github")
    if isinstance(github, dict) and github.get("enabled"):
        notes_file = github.get("notes_file", "CHANGELOG.md")
        notes = exported_file_text(publish_dir, tree, sorted(ev.proj.entries), notes_file)
        if notes is not None:
            lines.append(f"Release notes ({notes_file}):")
            lines += _listed(notes.splitlines(), indent="  | ")
        else:
            lines.append(f"Release notes: {notes_file} is not in the release export")
    return lines


def _release_identity(ev: "_PublishEval") -> str:
    """The managed-tree identity of an evaluated projection (managed + keep)."""
    return managed_tree_identity({p: (e.mode, e.blob) for p, e in ev.proj.entries.items()})


def _release_preview(ctx: ReleaseContext) -> str:
    """The release half of the one prompt (design §8.3, §17.3), with
    collaborator commits, carried paths, notes and accepted flags."""
    providers = ", ".join(a.name for a in ctx.adapters) or "git only"
    lines = [f"Release {ctx.version} ({ctx.tag}); providers: {providers}"]
    if any(a.name == "pypi" for a in ctx.adapters):
        lines.append("  The PyPI upload is immutable.")
    return "\n".join(lines)


class _ReleaseAlreadyComplete(Exception):
    """Raised in a prompt-mode compute: nothing to confirm or do."""


def _release_attempts(source_root: Path, publish_dir: Path, config: dict, *,
                      choice: ReleaseChoice, discovery: Discovery,
                      custom_message: str | None, config_relpath: str | None,
                      config_file: Path, expect_tip: str | None, retry: bool,
                      confirmed: str | None = None,
                      first: "tuple[_PublishEval, ReleaseContext] | None" = None) -> int:
    """Under the locks: a resume, or a new candidate. A non-fast-forward
    rejection supersedes the attempt (journal kept for artifact reuse) and
    exits 5; with --retry the run re-fetches and tries again, at most
    RELEASE_RETRY_LAPS attempts, and (in prompt mode) only while the
    managed tree is the one confirmed. A build-changes list shown in
    the confirmation is acknowledged for that managed-tree identity, so a
    later lap with the same identity does not ask for --accept-build-changes."""
    lap = 0
    acknowledged: str | None = None
    while True:
        lap += 1
        if first is not None and lap == 1:
            ev, ctx = first
        else:
            ev = _evaluate_publish(source_root, publish_dir, config,
                                   config_relpath=config_relpath, accept=None, recreate=False,
                                   expect_tip=expect_tip if lap == 1 else None)
            if lap > 1:                    # the ledger may have moved with the tip
                discovery = discover_release_state(publish_dir, source_root, config)
            ctx = prepare_release_context(choice, discovery, publish_dir, source_root,
                                          config, ev.integ)
            ctx.config_relpath, ctx.config_file = config_relpath, config_file
        if ctx.resume:
            return _resume_release(ctx, ev.integ.tip)
        identity = _release_identity(ev)
        if confirmed is not None and identity != confirmed:
            raise _superseded_refusal(ctx.version, "the managed tree changed since it was "
                                                   "confirmed")
        if ctx.summary_shown:
            acknowledged = identity
        elif acknowledged is not None and identity == acknowledged:
            ctx.summary_shown = True       # lap 1's confirmation covers this tree
        try:
            return _build_release(ctx, ev, custom_message)
        except _CandidateRejected as rejected:
            ctx.journal.data["state"] = "superseded"
            ctx.journal.data["superseded_reason"] = rejected.reason
            journal_write(ctx.journal)
            if not retry or lap >= RELEASE_RETRY_LAPS:
                raise _superseded_refusal(ctx.version, rejected.reason) from rejected
            ui.warn(f"WARNING: the public branch moved ({rejected.reason}); "
                    f"retrying ({lap + 1}/{RELEASE_RETRY_LAPS})")


def _release_routine(source_root: Path, publish_dir: Path, config: dict, *,
                     choice: ReleaseChoice, discovery: Discovery,
                     custom_message: str | None,
                     config_relpath: str | None, config_file: Path,
                     expect_tip: str | None = None, retry: bool = False,
                     mode: str = "yes") -> int:
    """Design §8 on the fast-forward path, under §17.3.
    "yes": source (exclusive) then destination locks, held to the end.
    "prompt": the one [y/N] (the release preview plus the publish preview,
    or the resume summary: a resume confirms again) is shown before the
    build and without the lock (§8.3), through confirm_then_lock bound to
    the shown tip and the managed-tree identity; then the locked attempt."""
    branch = config["branch"]
    kwargs = dict(choice=choice, discovery=discovery, custom_message=custom_message,
                  config_relpath=config_relpath, config_file=config_file,
                  expect_tip=expect_tip, retry=retry)
    if mode != "prompt":
        with source_lock(source_root, exclusive=True), publish_lock(publish_dir):
            _check_publish_branch(publish_dir, branch)
            return _release_attempts(source_root, publish_dir, config, **kwargs)
    _check_publish_branch(publish_dir, branch)
    url = sanitize_provider_output(config["remote"])
    latest: dict = {}

    def compute() -> Binding:
        ev = _evaluate_publish(source_root, publish_dir, config, config_relpath=config_relpath,
                               accept=None, recreate=False, expect_tip=expect_tip)
        ctx = prepare_release_context(choice, discovery, publish_dir, source_root, config,
                                      ev.integ)
        ctx.config_relpath, ctx.config_file = config_relpath, config_file
        tip = ev.integ.tip
        uploads_pypi = (any(a.name == "pypi" for a in enabled_adapters(config))
                        and "pypi:upload" not in ctx.journal.data.get("completed", []))
        if ctx.resume:
            if ctx.journal.data.get("state") == "complete":
                raise _ReleaseAlreadyComplete()
            identity = ctx.journal.data.get("managed_tree_identity")
            summary = (f"Resume release {ctx.version} ({ctx.tag}) at "
                       f"{ctx.journal.data.get('in_flight') or 'start'}: candidate "
                       f"{(ctx.journal.data.get('public_candidate') or '?')[:12]}")
        else:
            ctx.adapters = ctx.adapters or enabled_adapters(config)
            plan = build_snapshot_tree(publish_dir, tip, ev.proj, ev.integ, config)
            identity = _release_identity(ev)
            summary = "\n".join([_release_preview(ctx), *release_summary_lines(
                source_root, publish_dir, config, discovery.ledger, ev, plan.tree),
                _publish_summary(url, branch, plan, "")])
            ctx.summary_shown = True
        latest.update(ev=ev, ctx=ctx, summary=summary, identity=identity,
                      typed=ctx.version if uploads_pypi else None)
        return Binding(tips=((f"refs/heads/{branch}", tip),) if tip else (), digest=None,
                       managed_tree=identity, accepted_snapshot=None, changed_paths=())

    def show_and_confirm(_binding: Binding) -> bool:
        # §17.3: an irreversible PyPI upload asks for the exact
        # version in the one prompt; every other release asks [y/N].
        if latest["typed"]:
            return confirm_typed_version(latest["summary"], latest["typed"])
        return confirm_yes_no(latest["summary"])

    try:
        with confirm_then_lock(source_root, publish_dir, compute, show_and_confirm):
            return _release_attempts(source_root, publish_dir, config, **kwargs,
                                     confirmed=None if latest["ctx"].resume
                                     else latest["identity"],
                                     first=(latest["ev"], latest["ctx"]))
    except _ReleaseAlreadyComplete:
        ui.success(f"Release {choice.version} already complete")
        return EXIT_SUCCESS


def _choice_routine(source_root: Path, publish_dir: Path, config: dict, *,
                    discovery: Discovery, custom_message: str | None,
                    config_relpath: str | None, config_file: Path,
                    expect_tip: str | None = None, retry: bool = False) -> int:
    """Release enabled, no release flag, on a terminal (design §17.3):
    the release-choice question, shown with
    the full publish preview, is the one confirmation. Choosing release,
    bump or no-release proceeds with no [y/N] after it; cancel or an empty
    answer → 1. A release that uploads to PyPI asks for the exact version
    instead of proceeding on the choice. The choice is bound
    like any prompt (confirm_then_lock: the shown tip and snapshot tree)."""
    branch = config["branch"]
    url = sanitize_provider_output(config["remote"])
    _check_publish_branch(publish_dir, branch)
    latest: dict = {}

    def compute() -> Binding:
        ev = _evaluate_publish(source_root, publish_dir, config, config_relpath=config_relpath,
                               accept=None, recreate=False, expect_tip=expect_tip)
        refusal = _state_refusal(ev.integ)
        if refusal is not None:
            raise refusal
        plan = build_snapshot_tree(publish_dir, ev.integ.tip, ev.proj, ev.integ, config)
        changed = bool(plan.adds or plan.mods or plan.dels)
        publish_part = (_publish_summary(url, branch, plan, "") if changed
                        else "Nothing to publish: the public tree is current.")
        latest.update(ev=ev, changed=changed, summary="\n".join([
            publish_part, "If released:", *("  " + line for line in release_summary_lines(
                source_root, publish_dir, config, discovery.ledger, ev, plan.tree))]))
        tip = ev.integ.tip
        return Binding(tips=((f"refs/heads/{branch}", tip),) if tip else (), digest=None,
                       managed_tree=plan.tree, accepted_snapshot=None,
                       changed_paths=tuple(sorted({*plan.written, *plan.decided})))

    def show_and_choose(_binding: Binding) -> bool:
        sys.stderr.write(latest["summary"].rstrip("\n") + "\n\n")
        sys.stderr.flush()
        choice = prompt_release_choice(discovery, config)
        latest["choice"] = choice
        if choice is None:
            return False
        if choice.kind != "none" and any(a.name == "pypi" for a in enabled_adapters(config)):
            ctx = prepare_release_context(choice, discovery, publish_dir, source_root, config,
                                          latest["ev"].integ)       # writes nothing
            ctx.adapters = ctx.adapters or enabled_adapters(config)
            preview = ("" if ctx.resume else _release_preview(ctx))
            return confirm_typed_version(preview, choice.version)
        return True

    with confirm_then_lock(source_root, publish_dir, compute, show_and_choose):
        choice, ev = latest["choice"], latest["ev"]
        if choice.kind == "none":
            if not latest["changed"]:
                ui.success("Nothing to publish")
                return EXIT_SUCCESS
            if ev.branch_state.default_branch:
                ui.info(f"Note: the remote's default branch is "
                        f"'{ev.branch_state.default_branch}'; this publish creates '{branch}'")
            return _publish_snapshot(source_root, publish_dir, config, ev,
                                     custom_message=custom_message,
                                     config_relpath=config_relpath, config_file=config_file,
                                     accepted=None, mark=lambda _phase: None)
        ctx = prepare_release_context(choice, discovery, publish_dir, source_root, config,
                                      ev.integ)
        ctx.config_relpath, ctx.config_file = config_relpath, config_file
        ctx.summary_shown = True         # in the choice's preview
        if ctx.resume:
            return _resume_release(ctx, ev.integ.tip)
        return _release_attempts(source_root, publish_dir, config, choice=choice,
                                 discovery=discovery, custom_message=custom_message,
                                 config_relpath=config_relpath, config_file=config_file,
                                 expect_tip=expect_tip, retry=retry,
                                 confirmed=_release_identity(ev), first=(ev, ctx))


def managed_release_targets(source_root: Path, publish_dir: Path,
                            config: dict) -> dict[str, str]:
    """Managed release tag → peeled sha (§2.3, design §8.5): the
    ledger's managed tags (marker trailers on an ours-snapshot, or
    adopt_tags). Repair refuses released ancestry through this. The
    release table's tag_format and adopt_tags apply even when releases are
    now disabled: earlier managed tags still need their ancestry."""
    release = config.get("release") if isinstance(config.get("release"), dict) else {}
    return dict(read_remote_ledger(
        publish_dir, release.get("tag_format", "v{version}"),   # load_publish_config's default
        source_root=source_root, adopt_tags=release.get("adopt_tags", [])).managed)


# === core: push targets ===
# Design §18. (P) public refs go only to the
# publish clone's origin, which must equal [publish] remote (R5).
# (S) private refs (the bookmark, the release source_tag, the records) go
# only to a NAMED source remote every value of which is listed byte-for-byte
# in [publish] private_remotes (§18.11), and never to a publish repo
# (known_publish_remotes is sync's). The §18.10 parser only feeds the
# publish-repo refusal, the insteadOf exemption (§18.13) and the refusal
# wording. Public commits take their parents only from this run's fetch
# (§18.13).

PUBLIC_HOSTS = ("github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "sr.ht",
                "gitee.com")
_DEFAULT_PORTS = {"ssh": 22, "git+ssh": 22, "ssh+git": 22, "https": 443, "http": 80,
                  "git": 9418}
# §18.10: git's transport-helper syntax `<helper>::<address>`, an anchored
# prefix (a bracketed IPv6 host such as [::1] is not one; git also takes a
# leading digit: `1foo::x` → "transport '1foo' not allowed").
_HELPER_RE = re.compile(r"^(?P<helper>[A-Za-z0-9+.-]+)::(?P<address>.*)$", re.S)
# git's is_url(): a scheme of URL-scheme characters, then "://".
_IS_URL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.-]*://")
_SSH_SCHEMES = ("ssh", "git+ssh", "ssh+git")
_CURL_SCHEMES = ("http", "https")        # any other scheme is unknown
_NEVER_CHECKABLE_HELPERS = ("ext", "fd")      # the address is a command or a descriptor
_LDH_HOST_RE = re.compile(r"[A-Za-z0-9.-]+")
# Under the test sandbox, `ssh -G` reads only this config (or none).
TEST_SSH_CONFIG_VAR = "PUBREPO_TEST_SSH_CONFIG"
# §18.12: remote.<name>.* keys that can change the program or helper that
# receives a push, or what it pushes. The per-key review against `git help
# config` (git 2.53) is in tests/test_ff_push_targets.py REMOTE_KEYS.
TRANSPORT_ALTERING_KEYS = ("receivepack", "uploadpack", "vcs", "mirror")


@dataclass(frozen=True)
class RemoteId:
    """One repository as git would reach it (§18.10): `host` is "" for a
    local repo, whose `path` is then the resolved absolute path; `port` is
    None for the scheme's default."""
    host: str
    port: int | None
    path: str


class _UnknownHost(Exception):
    """A value whose repository pubrepo cannot identify the way git would
    (§18.10): refused unless listed in private_remotes."""


def _plain_host(host: str) -> bool:
    """§18.10: plain ASCII letters, digits, dots and hyphens, or an IP
    literal. IDNA, fullwidth, ideographic-dot and %-encoded hosts are not
    plain: curl or ssh may rewrite them, so they are unknown."""
    if _LDH_HOST_RE.fullmatch(host):
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _ssh_resolve(host: str, port: int | None, user: str | None = None) -> tuple[str, int]:
    """(hostname, port) from `ssh -G` on the host part only, with an
    explicit port passed as -p (§18.10) and the URL's user as -l
    (so `Match user` blocks resolve as they do for git's ssh). Offline. A
    hostname that is empty, not plain, dotless, or contains a colon
    without being an IP literal (ssh echoes unknown names back) is unknown.
    Under the test sandbox ssh reads only PUBREPO_TEST_SSH_CONFIG
    (or /dev/null)."""
    config = ([] if not os.environ.get(TEST_SANDBOX_VAR)
              else ["-F", os.environ.get(TEST_SSH_CONFIG_VAR) or os.devnull])
    argv = ["ssh", *config, "-G", *(["-p", str(port)] if port is not None else []),
            *(["-l", user] if user else []), host]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=10,
                                stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _UnknownHost(host) from exc
    if result.returncode != 0:
        raise _UnknownHost(host)
    values = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    name = values.get("hostname", "").strip()
    if not _plain_host(name):
        raise _UnknownHost(host)
    if ":" in name:
        try:
            ipaddress.ip_address(name)
        except ValueError:
            raise _UnknownHost(host) from None
    elif not name or "." not in name.rstrip("."):
        raise _UnknownHost(host)                   # a dotless alias echo
    try:
        resolved_port = int(values.get("port", str(port or 22)))
    except ValueError:
        resolved_port = port or 22
    return name, resolved_port


def _norm_host(host: str) -> str:
    return host.strip().rstrip(".").lower()


def _host_end(host: str) -> int:
    """git connect.c host_end(host, 0): the index where the separator
    search starts: the "]" of a bracketed part ("[" at the start or right
    after "@"), else 0."""
    at = host.find("@[")
    start = at + 1 if at >= 0 else 0
    if host[start:start + 1] == "[":
        close = host.find("]", start + 1)
        if close >= 0:
            return close
    return 0


def _get_port(host: str) -> tuple[str, int | None]:
    """git connect.c get_port(): a port after the FIRST colon, digits only."""
    colon = host.find(":")
    if colon >= 0:
        digits = host[colon + 1:]
        if digits.isdigit() and digits.isascii() and int(digits) < 65536:
            return host[:colon], int(digits)
    return host, None


def _get_host_and_port(hostandport: str) -> tuple[str, int | None]:
    """git connect.c get_host_and_port(), then get_port() when no port was
    found. A bracketed part ("[...]" at the start or after "@") is the host
    with the brackets removed; git then looks for ":port" only in what
    follows the "]", and anything else there is not part of the host."""
    at = hostandport.find("@[")
    start = at + 1 if at >= 0 else 0
    close = hostandport.find("]", start + 1) if hostandport[start:start + 1] == "[" else -1
    if close >= 0:
        host, rest = hostandport[:start] + hostandport[start + 1:close], hostandport[close + 1:]
    else:
        host, rest = hostandport, hostandport
    colon = rest.find(":")
    if colon >= 0:
        digits = rest[colon + 1:]
        if digits.isdigit() and digits.isascii() and int(digits) < 65536:
            return (host if close >= 0 else rest[:colon]), int(digits)
        if not digits and close < 0:
            host = rest[:colon]
    return _get_port(host)


def _git_remote_target(url: str) -> tuple[str, str | None, str, int | None, str]:
    """How git reads a remote value (§18.10; connect.c parse_connect_url
    + get_host_and_port, transport.c helper detection): (kind, user, host,
    port, path), kind one of "ssh", "git", "curl:<scheme>", "local". `host`
    is without the user part and with brackets removed; `port` is None when
    the value has none. A host that is not plain ASCII LDH or an IP literal
    is unknown; a file:// URL's host is dropped, as git's PROTO_FILE
    does. Anything else raises _UnknownHost."""
    value = url
    if _HELPER_RE.match(value):
        raise _UnknownHost(value)
    if _IS_URL_RE.match(value):
        scheme = value.split("://", 1)[0]
        if scheme in _CURL_SCHEMES:
            parsed = urllib.parse.urlsplit(value)
            try:
                port = parsed.port
            except ValueError:
                raise _UnknownHost(value) from None
            if not parsed.hostname or not _plain_host(parsed.hostname):
                raise _UnknownHost(value)            # curl may rewrite the rest
            return ("curl:" + scheme, parsed.username, parsed.hostname, port,
                    urllib.parse.unquote(parsed.path))
        if scheme not in (*_SSH_SCHEMES, "git", "file"):
            raise _UnknownHost(value)              # git hands it to git-remote-<scheme>
        decoded = urllib.parse.unquote(value)      # is_url() → url_decode()
        rest = decoded.split("://", 1)[1]
        if scheme == "file":
            # git's PROTO_FILE ignores the host: file://<host>/<path> is /<path>
            slash = rest.find("/")
            return "local", None, "", None, rest[slash:] if slash >= 0 else ""
        end = _host_end(rest)
        slash = rest.find("/", end)
        if slash <= 0:
            raise _UnknownHost(value)              # "no path specified"
        hostandport, path = rest[:slash], rest[slash:]
        kind = "git" if scheme == "git" else "ssh"
    else:
        colon, slash = value.find(":"), value.find("/")
        if colon < 0 or (0 <= slash < colon):
            return "local", None, "", None, value  # url_is_local_not_ssh()
        end = _host_end(value)
        sep = value.find(":", end)
        if sep < 0 or sep + 1 >= len(value):
            raise _UnknownHost(value)              # "no path specified"
        hostandport, path = value[:sep], value[sep + 1:]
        kind = "ssh"
    host, port = _get_host_and_port(hostandport)
    user, _at, host = host.rpartition("@")         # ssh takes the user from user@host
    if not host or not _plain_host(host):
        raise _UnknownHost(value)
    return kind, user or None, host, port, path


def _remote_identity(url: str, *, resolve: bool = True) -> tuple[str, int | None, str]:
    """(host, port, path) naming one repository, from git's own reading of
    the value (§18.10). With `resolve`, ssh hosts are resolved
    with `ssh -G` on the host part. Hosts are case-folded with a
    trailing dot dropped, default ports dropped, and paths compared
    without slashes or a `.git` suffix (any case). Local repos: ("", None,
    resolved path). Raises _UnknownHost for anything git would not read
    as one of these forms."""
    kind, user, host, port, path = _git_remote_target(url.strip())
    if kind == "local":
        return "", None, re.sub(r"\.git$", "", str(Path(path).expanduser().resolve()),
                                flags=re.I)
    if kind == "ssh" and resolve:
        host, port = _ssh_resolve(host, port, user)
    scheme = kind.removeprefix("curl:")
    if port == _DEFAULT_PORTS.get(scheme if kind.startswith("curl:") else kind):
        port = None
    path = path.strip("/")
    path = re.sub(r"\.git$", "", path, flags=re.I).rstrip("/")
    return _norm_host(host), port, path.lower()


def _innermost_address(url: str) -> tuple[str, list[str]]:
    """Strip every `<helper>::` prefix (§18.10): (address, helpers)."""
    helpers = []
    value = url.strip()
    while (m := _HELPER_RE.match(value)) is not None:
        helpers.append(m.group("helper").lower())
        value = m.group("address")
    return value, helpers


def _identity_or_unknown(url: str) -> tuple[str, int | None, str] | None:
    try:
        return _remote_identity(url)
    except (_UnknownHost, ValueError):
        return None


def _raw_identity(url: str) -> tuple[str, int | None, str] | None:
    """The identity as written (no ssh alias resolution), or None."""
    try:
        return _remote_identity(url, resolve=False)
    except (_UnknownHost, ValueError):
        return None


def remote_identity(url: str) -> RemoteId | None:
    """§18.10 (R3): the repository `url` names, or None (unknown:
    a helper, a form git does not read as a host, ssh -G absent/non-zero or
    a dotless alias)."""
    identity = _identity_or_unknown(url)
    return None if identity is None else RemoteId(*identity)


def remote_name_problem(name) -> str | None:
    """§18.12 (R1): why `name` is not a usable git remote name, or
    None. git would read such a value as a URL, a path or an option."""
    if not isinstance(name, str):
        return "not a string"
    if not name:
        return "empty"
    if name != name.strip():
        return "leading or trailing whitespace"
    if "/" in name or "\\" in name:
        return "contains / or \\"
    if ":" in name:
        return "contains :, so it reads as a URL"
    if name in (".", ".."):
        return "is . or .."
    if name.startswith("-"):
        return "starts with -"
    return None


def remote_url_values(repo: Path, name: str) -> list[str]:
    """§18.9 R1 / §18.11: every value git may use for remote `name`:
    remote.<name>.url and .pushurl as written, then `git remote get-url
    --all` and `--push --all` (insteadOf/pushInsteadOf expanded), in order,
    without duplicates, byte-exact. Local reads only; [] when `name`
    is not a remote."""
    urls: list[str] = []
    for args in (("config", "--get-all", f"remote.{name}.url"),
                 ("config", "--get-all", f"remote.{name}.pushurl"),
                 ("remote", "get-url", "--all", name),
                 ("remote", "get-url", "--push", "--all", name)):
        result = git(*args, cwd=str(repo), check=False)
        if result.returncode == 0:
            urls += [line for line in result.stdout.split("\n") if line != ""]
    return list(dict.fromkeys(urls))


def is_public_host(host: str, config: dict) -> bool:
    """§18.9 R4: `host` equals or is a subdomain of PUBLIC_HOSTS or
    [publish] public_hosts (port and a trailing dot ignored). Wording only
    under §18.11."""
    host = _norm_host(_get_port(host)[0] if host.count(":") == 1 else host)
    extra = [h for h in config.get("public_hosts") or [] if isinstance(h, str)]
    return any(host == h or host.endswith("." + h)
               for h in {_norm_host(h) for h in (*PUBLIC_HOSTS, *extra)} if h)


def _expanded(source_root: Path, url: str) -> list[str]:
    """`url` as written and with insteadOf/pushInsteadOf applied."""
    forms = [url]
    result = git("ls-remote", "--get-url", url, cwd=str(source_root), check=False)
    if result.returncode == 0 and result.stdout.strip():
        forms.append(result.stdout.strip())
    push = git("config", "--get-regexp", r"^url\..*\.pushinsteadof$", cwd=str(source_root),
               check=False)
    for line in push.stdout.splitlines() if push.returncode == 0 else []:
        key, _, prefix = line.partition(" ")
        if prefix and url.startswith(prefix):
            forms.append(key[len("url."):-len(".pushinsteadof")] + url[len(prefix):])
    return list(dict.fromkeys(forms))


def _publish_repo_refusal(source_root: Path, config: dict, url: str) -> str | None:
    """§18.11: refusals that hold even for a listed value. An ext:: or
    fd:: anywhere in a helper chain (the address is a command or a
    descriptor, so it can never be checked); or rules 1-2, by the §18.10
    parser: the value, or its innermost helper address, is a known publish
    remote (known_publish_remotes, sync's) or the same repository; or
    also after percent-decoding (can only refuse). A value the parser
    cannot identify is not refused here (the listing is the owner's
    assertion; a stated residual)."""
    address, helpers = _innermost_address(url)
    blocked = [h for h in helpers if h in _NEVER_CHECKABLE_HELPERS]
    if blocked:
        return (f"{url}: a {blocked[0]}:: address is a command, not a repository, so it "
                f"can never be checked")
    candidates = {url, address, urllib.parse.unquote(url), urllib.parse.unquote(address)}
    mine = ({_identity_or_unknown(c) for c in candidates}
            | {_raw_identity(c) for c in candidates}) - {None}
    for publish in known_publish_remotes(source_root, config):
        forms = _expanded(source_root, publish) + [_innermost_address(publish)[0]]
        forms += [urllib.parse.unquote(f) for f in forms]
        theirs = ({_identity_or_unknown(u) for u in forms}
                  | {_raw_identity(u) for u in forms}) - {None}
        if mine & theirs or {c.strip() for c in candidates} & {f.strip() for f in forms}:
            return f"{url} is the publish remote {publish} (the same repository)"
    return None


def _unlisted_wording(config: dict, url: str) -> str:
    """§18.11: the §18.10 classification only chooses the wording."""
    identity = _identity_or_unknown(url)
    if identity is None:
        return ("its repository cannot be identified: a transport helper, a form git "
                "does not read as a host, or an ssh host ssh -G cannot resolve")
    if identity[0] and is_public_host(identity[0], config):
        return "it is on a public host"
    return "only listed remotes may receive private refs"


def private_target_refusal(source_root: Path, config: dict, name: str) -> str | None:
    """§18.11/§18.12: why source remote `name` may NOT receive private refs,
    or None. No network call.
    - `name` is a usable remote name and `git remote get-url` succeeds.
    - remote.<name>.receivepack, .uploadpack, .vcs or .mirror refused.
    - V = remote_url_values(name) is non-empty and every value is
      byte-equal to a [publish] private_remotes entry (no trimming;
      a value with surrounding whitespace is refused, naming it). No
      normalization, no ssh -G, no host parsing in this decision.
    - A listed value is still refused by _publish_repo_refusal.
    - The refusal names each unlisted value exactly as it must be
      added; the parser's classification only picks the wording."""
    problem = remote_name_problem(name)
    if problem is not None:
        return f"{name!r} is not a usable remote name ({problem})"
    exists = git("remote", "get-url", name, cwd=str(source_root), check=False)
    if exists.returncode != 0:
        return f"'{name}' is not a remote of the source repository"
    for key in TRANSPORT_ALTERING_KEYS:
        got = git("config", "--get-all", f"remote.{name}.{key}", cwd=str(source_root),
                  check=False)
        if got.returncode == 0:
            return (f"remote.{name}.{key} is set, which can change what receives the push; "
                    f"private refs are never pushed through it")
    urls = remote_url_values(source_root, name)
    if not urls:
        return f"'{name}' is not a remote of the source repository"
    for url in urls:
        if url != url.strip():
            return f"{url!r} has leading or trailing whitespace"
        why = _publish_repo_refusal(source_root, config, url)
        if why is not None:
            return why
    listed = {p for p in config.get("private_remotes") or [] if isinstance(p, str)}
    unlisted = [u for u in urls if u not in listed]           # byte-exact
    if unlisted:
        return "; ".join(f"{u!r} is not listed in [publish] private_remotes "
                         f"({_unlisted_wording(config, u)})" for u in unlisted) + \
            ". List each exact value in private_remotes only if that repository is private"
    return None


@dataclass(frozen=True)
class PushPlanEntry:
    """One push a covered run will make (§18.4): `cls` "P" or "S"; `remote`
    a remote name (origin of .publish/ for P, a source remote for S);
    `refusal` from private_target_refusal for S entries."""
    ref: str
    remote: str
    cls: str
    refusal: str | None


PUSH_PLAN_KINDS = ("publish", "amend", "release", "repair", "restore_ancestry")
PUSH_PLAN_FILE = "push-plan.json"


def build_push_plan(source_root: Path, publish_dir: Path, config: dict, *, kind: str,
                    refs: dict) -> list[PushPlanEntry]:
    """§18.4: every (ref, remote, class) a covered run pushes, built after
    confirmation and the lock, before the first network write. `kind` is
    publish, amend, release, repair or restore_ancestry. `refs`:
    - "public": the (P) refs, each a ref or (ref, remote); every one must
      go to the publish clone's origin (anything else is an internal
      error, exit 1, before any write);
    - "bookmark": True when this run writes a bookmark → (S) to
      tag_remote, if one is configured;
    - "source_tag": the release tag pushed to release.source_remote, or
      None;
    - "records": the records remote NAME (sync's records_remote_name), or
      None.
    (S) entries carry private_target_refusal's reason; their push sites
    skip a refused entry. --verbose prints the plan; under the test
    sandbox it is also written to <git common dir>/pubrepo/push-plan.json
    replaced on each run."""
    if kind not in PUSH_PLAN_KINDS:
        raise AssertionError(f"unknown push plan kind: {kind}")
    plan: list[PushPlanEntry] = []
    for item in refs.get("public") or []:
        ref, remote = (item, "origin") if isinstance(item, str) else item
        if remote != "origin":
            raise PubrepoError(f"ERROR: internal: public ref {ref} planned for {remote}; "
                               f"nothing written")                  # exit 1
        plan.append(PushPlanEntry(ref, "origin", "P", None))
    private = []
    if refs.get("bookmark") and config.get("tag_remote"):
        private.append(("refs/tags/published/*", config["tag_remote"]))
    if refs.get("source_tag"):
        private.append((f"refs/tags/{refs['source_tag']}",
                        config.get("release", {}).get("source_remote", "origin")))
    if refs.get("records"):
        private.append(("refs/pubrepo/*", refs["records"]))
    for ref, remote in private:
        plan.append(PushPlanEntry(ref, remote, "S",
                                  private_target_refusal(source_root, config, remote)))
    for entry in plan:
        ui.detail(f"push plan ({kind}): {entry.cls} {entry.ref} → {entry.remote}"
                  + (f" (refused: {entry.refusal})" if entry.refusal else ""))
    if os.environ.get(TEST_SANDBOX_VAR):
        common = git("rev-parse", "--git-common-dir", cwd=str(source_root),
                     check=False).stdout.strip()
        if common:
            target = (Path(source_root) / common).resolve() / "pubrepo" / PUSH_PLAN_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"kind": kind, "entries": [
                {"ref": e.ref, "remote": e.remote, "cls": e.cls, "refusal": e.refusal}
                for e in plan]}, indent=2) + "\n")
    return plan


BOOKMARK_PREFIX = "refs/pubrepo/bookmarks/"


def write_bookmark(source_root: Path) -> str | None:
    """§18.9 R2: record a clean publish or release as the local ref
    refs/pubrepo/bookmarks/<YYYYmmddTHHMMSS> at HEAD (create-only). Never a
    tag, so `git push --tags` and followTags cannot send it. The ref, or
    None (info) when that second's bookmark already exists. It keeps σ
    reachable, as the old published/<ts> tag did."""
    ts = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    ref = f"{BOOKMARK_PREFIX}{ts}"
    made = git("update-ref", ref, "HEAD", "", cwd=str(source_root), check=False)
    if made.returncode != 0:
        ui.info(f"Bookmark {ts} already exists, skipping")
        return None
    return ref


def push_bookmark(source_root: Path, config: dict, ref: str) -> bool:
    """§18.3 with R1: push `ref` as refs/tags/published/<ts> only to
    [publish] tag_remote, by name, when private_target_refusal passes, with
    --recurse-submodules=no. A refusal or a failed push is a
    one-line warning (git's reason, sanitized, under --verbose); the run's
    exit is unchanged. True when pushed (the release journal records it)."""
    ts = ref.removeprefix(BOOKMARK_PREFIX)
    name = config.get("tag_remote")
    if not name:
        ui.detail(f"Bookmark {ts} kept locally (no tag_remote)")
        return False
    why = private_target_refusal(source_root, config, name)
    if why is not None:
        ui.warn(f"WARNING: refusing to push private ref {ref} to {name}: {why}; kept locally")
        return False
    _guard_remote(source_root, name)
    result = git("push", "--recurse-submodules=no", name, f"{ref}:refs/tags/published/{ts}",
                 cwd=str(source_root), check=False)
    if result.returncode == 0:
        ui.info(f"Bookmark {ts} pushed to {name}")
        return True
    ui.warn(f"WARNING: bookmark {ts} kept locally; push to {name} failed")
    ui.detail(f"  {name}: {sanitize_provider_output(result.stderr.strip())}")
    return False


def check_publish_clone_origin(publish_dir: Path, config: dict) -> None:
    """R5, before the first fetch of a covered run (publish, --amend,
    release; repair and restore-ancestry are sync's call sites), so no
    fetch result can become a parent. Exit 2 (PublishError) unless:
    - check_push_lock passes first (a DISABLED- pushurl keeps its own
      message);
    - origin has no receivepack, uploadpack, vcs or mirror (the
      transport-altering keys), naming the key;
    - no refs/replace/* ref and no info/grafts file;
    - every value of origin (remote_url_values: url and pushurl as
      written, get-url --all and --push --all) is byte-equal to [publish]
      remote, or to its `ls-remote --get-url` (insteadOf) rewrite; the
      rewrite only when origin's fetch set equals its push set. A
      relative local remote matches only os.path.join(<source
      root>, <remote>), never the relative string.
    - a rewrite byte-equal to, or with the same raw
      identity as, any value of any source remote is refused, unless
      exemption (i) holds (the rewrite is [publish] remote's repository).
      There is no other exemption.
    The mismatch message gives the set-url fix and the re-create line."""
    check_push_lock(publish_dir)
    for key in TRANSPORT_ALTERING_KEYS:
        got = git("config", "--get-all", f"remote.origin.{key}", cwd=str(publish_dir),
                  check=False)
        if got.returncode == 0:
            raise PublishError(
                f"ERROR: remote.origin.{key} is set in {publish_dir.name}/, which can change "
                f"what serves the fetch or receives the push; nothing was pushed\n"
                f"Fix: git -C {publish_dir.name} config --unset-all remote.origin.{key}")
    _refuse_replaced_history(publish_dir)
    urls = remote_url_values(publish_dir, "origin")
    remote = config["remote"]
    stored = remote
    if _is_relative_local_remote(remote):
        stored = os.path.join(str(publish_dir.parent), remote)
    allowed = {stored}
    fetch_set = _remote_urls(publish_dir, "origin", push=False)
    push_set = _remote_urls(publish_dir, "origin", push=True)
    recreate = (f"If {publish_dir.name}/ was cloned from a different repository, re-create "
                f"it with `{_PROG} init` (move {publish_dir.name}/ aside first) instead of "
                f"set-url")
    rewritten = git("ls-remote", "--get-url", remote, cwd=str(publish_dir), check=False)
    rewrite = rewritten.stdout.rstrip("\n") if rewritten.returncode == 0 else ""
    if rewrite != remote and rewrite.strip() and fetch_set == push_set:
        owner = (None if rewrite == stored
                 else _rewrite_owner(publish_dir.parent, stored, rewrite))
        if owner is None:
            allowed.add(rewrite)
        elif rewrite in urls:
            raise PublishError(
                f"ERROR: [publish] remote is rewritten (insteadOf) to {rewrite}, which is "
                f"a value of the source repository's remote '{owner}'; publishing there "
                f"would push the snapshot to that remote. Nothing was pushed.\n"
                f"Fix: remove the url.<base>.insteadOf / pushInsteadOf rule that rewrites "
                f"{remote}\n{recreate}")
    for url in urls or ["(none)"]:
        if url not in allowed:
            pushurl = git("config", "--get-all", "remote.origin.pushurl", cwd=str(publish_dir),
                          check=False).stdout.strip()
            fix = f"git -C {publish_dir.name} remote set-url origin {shlex.quote(stored)}"
            if pushurl or url not in fetch_set or fetch_set != push_set:
                fix += (f"\n  git -C {publish_dir.name} remote set-url --push origin "
                        f"{shlex.quote(stored)}")
            raise PublishError(f"ERROR: publish clone's origin does not match [publish] "
                               f"remote: {url}\nFix:\n  {fix}\n{recreate}")


def _remote_urls(repo: Path, name: str, *, push: bool) -> list[str]:
    """`git remote get-url [--push] --all <name>` (insteadOf/pushInsteadOf
    expanded), byte-exact."""
    args = ["remote", "get-url", *(["--push"] if push else []), "--all", name]
    got = git(*args, cwd=str(repo), check=False)
    return [u for u in got.stdout.split("\n") if u != ""] if got.returncode == 0 else []


def _is_relative_local_remote(remote: str) -> bool:
    """§18.12: a value git treats as a local path (no `://`, no colon
    before the first `/`, not a `<helper>::` form) that is relative."""
    if "://" in remote or _HELPER_RE.match(remote):
        return False
    colon, slash = remote.find(":"), remote.find("/")
    if colon >= 0 and (slash < 0 or colon < slash):
        return False
    return not os.path.isabs(remote)


def _rewrite_owner(source_root: Path, publish_remote: str, rewrite: str) -> str | None:
    """§18.13: the first source remote one of whose values is byte-equal
    to the insteadOf `rewrite` of [publish] remote or has the same
    §18.10 raw identity, or None. Exempt only by (i): the parser (no ssh -G)
    gives the rewrite the identity of [publish] remote."""
    publish = _raw_identity(publish_remote)
    mine = _raw_identity(rewrite)
    if publish is not None and mine == publish:                       # (i)
        return None
    names = git("remote", cwd=str(source_root), check=False)
    for name in names.stdout.split() if names.returncode == 0 else []:
        for value in remote_url_values(source_root, name):
            if value == rewrite or (mine is not None and _raw_identity(value) == mine):
                return name
    return None


def _refuse_replaced_history(publish_dir: Path) -> None:
    """§18.13: a refs/replace/* ref or an info/grafts file changes
    the history git reports (and the provenance checks see) but not what a
    push sends. Exit 2, naming it."""
    refs = git("for-each-ref", "--format=%(refname)", "refs/replace/", cwd=str(publish_dir),
               check=False)
    replaced = refs.stdout.split() if refs.returncode == 0 else []
    if replaced:
        raise PublishError(
            f"ERROR: {publish_dir.name}/ has replace ref(s) {', '.join(replaced)}, which change "
            f"the history git reports but not what a push sends; nothing was pushed\n"
            f"Fix: git -C {publish_dir.name} replace -d <object>, or re-create "
            f"{publish_dir.name}/ with `{_PROG} init` (move it aside first)")
    grafts = git("rev-parse", "--git-path", "info/grafts", cwd=str(publish_dir), check=False)
    path = grafts.stdout.strip() if grafts.returncode == 0 else ""
    if path and (publish_dir / path).exists():
        raise PublishError(
            f"ERROR: {publish_dir.name}/ has a grafts file ({path}), which changes the history "
            f"git reports but not what a push sends; nothing was pushed\n"
            f"Fix: remove {publish_dir.name}/{path}, or re-create {publish_dir.name}/ with "
            f"`{_PROG} init` (move it aside first)")


def assert_parent_provenance(publish_dir: Path, parents: Sequence[str], *, tip: str | None,
                             fetched: set[str], created: set[str]) -> None:
    """§18.13 (parent provenance): immediately
    before every commit-tree that creates a public commit, each parent must
    be `tip` (this run's fetch result, never origin/<b>), a commit verified
    this run (`fetched`), one this run created (`created`), or an ancestor
    of `tip`. Otherwise PublishError (2) with the provenance wording; nothing is
    committed or pushed."""
    allowed = {tip, *fetched, *created} - {None}
    foreign = [p for p in parents if p not in allowed and not (
        tip and git("merge-base", "--is-ancestor", p, tip, cwd=str(publish_dir),
                    check=False).returncode == 0)]
    if foreign:
        raise PublishError(
            f"ERROR: {publish_dir.name}/ holds {len(foreign)} commit(s) that did not come "
            f"from [publish] remote (e.g. {foreign[0][:12]}); it may have been cloned from "
            f"another repository. Nothing was pushed.\n"
            f"Re-create it: move {publish_dir.name}/ aside and run `{_PROG} init`")


_LEAK_REFS = ("refs/tags/published/*", "refs/pubrepo/*")


def leaked_ref_findings(source_root: Path, config: dict) -> list[dict]:
    """§18.5 with R7, `validate --remote` only: published/* tags and
    refs/pubrepo/* on .publish/'s origin, on every known publish remote, and
    on every source remote that fails private_target_refusal. Read-only
    ls-remote calls with the records timeout; an unreachable remote is a
    warning. Each listing carries the manual cleanup command; for an
    unlisted source remote the wording names the unlisted value. Warnings only; pubrepo
    never deletes a remote ref."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    targets: list[tuple[str, str, Path, bool]] = []       # (label, remote, cwd, publish?)
    if (publish_dir / ".git").exists():
        targets.append((f"{publish_dir.name}/'s origin", "origin", publish_dir, True))
    for url in known_publish_remotes(source_root, config):
        targets.append((url, url, source_root, True))
    names = git("remote", cwd=str(source_root), check=False)
    for name in (names.stdout.split() if names.returncode == 0 else []):
        if private_target_refusal(source_root, config, name) is not None:
            targets.append((name, name, source_root, False))
    findings: list[dict] = []
    seen: set[tuple[str, str]] = set()
    origin_values = (remote_url_values(publish_dir, "origin")
                     if (publish_dir / ".git").exists() else [])
    for label, remote, cwd, is_publish in targets:
        key = (str(cwd), remote)
        if key in seen or (cwd == source_root and remote in origin_values):
            continue                       # the same publish remote as .publish/'s origin
        seen.add(key)
        altering = _transport_altering_key(cwd, remote)
        if altering is not None:
            # such a key chooses the program git runs; never run it
            findings.append(_warning("leaked_refs",
                f"'{label}' not contacted: remote.{remote}.{altering} is set; check it by hand"))
            continue
        listed = _git_net(["ls-remote", remote, *_LEAK_REFS], cwd, url=remote)
        if listed is None or listed.returncode != 0:
            findings.append(_warning("leaked_refs", f"could not list the refs on {label}"))
            continue
        refs = sorted({line.split("\t")[1].removesuffix("^{}") for line in
                       listed.stdout.splitlines() if "\t" in line})
        if not refs:
            continue
        where = (f"git -C {publish_dir.name} push origin" if cwd == publish_dir
                 else f"git push {remote}")
        if is_publish:
            findings.append(_warning("leaked_refs",
                f"{len(refs)} private ref(s) on {label}, which must not hold private history: "
                f"{', '.join(refs)}. pubrepo never deletes remote refs; to remove them: "
                f"{where} --delete {' '.join(refs)}"))
        else:
            findings.append(_warning("leaked_refs",                      # §18.12
                f"'{label}' is not listed in private_remotes; if this remote is public, "
                f"these refs expose private history: {', '.join(refs)}. If it is public: "
                f"{where} --delete {' '.join(refs)}"))
    return findings


def _may_hold_published_tags(source_root: Path) -> bool:
    """A filesystem pre-check, so plain validate runs no git process when the
    source repo cannot hold a published/* tag (no loose ref directory, no
    packed-refs line). True whenever it cannot tell (reftable, a git dir it
    cannot find, an unreadable file); `git tag -l` then decides."""
    config = _git_config_path(source_root)
    if config is None:
        return True
    common = config.parent
    if (common / "reftable").exists() or (common / "refs" / "tags" / "published").exists():
        return True
    packed = common / "packed-refs"
    try:
        return packed.is_file() and b" refs/tags/published/" in packed.read_bytes()
    except OSError:
        return True


def _transport_altering_key(repo: Path, remote: str) -> str | None:
    """The first key set on remote `remote` of `repo`, or None (a URL
    or path is not a remote name and has no such keys)."""
    if remote_name_problem(remote) is not None:
        return None
    for key in TRANSPORT_ALTERING_KEYS:
        got = git("config", "--get-all", f"remote.{remote}.{key}", cwd=str(repo), check=False)
        if got.returncode == 0:
            return key
    return None


def private_ref_findings(source_root: Path, config: dict) -> list[dict]:
    """§18.5, R7 (plain `validate`, offline): local published/* tags
    (pubrepo 1.0.0/1.0.1 bookmarks) with the local cleanup command, and
    every (S) remote that private_target_refusal refuses (tag_remote,
    records_remote, release.source_remote with source_tag), naming each
    value to add. Warnings only; no network call."""
    findings: list[dict] = []
    local = (git("tag", "-l", "published/*", cwd=str(source_root), check=False)
             if _may_hold_published_tags(source_root) else None)
    tags = local.stdout.split() if local is not None and local.returncode == 0 else []
    if tags:
        findings.append(_warning("leaked_refs",
            f"{len(tags)} local published/* tag(s) point at private source commits, and "
            f"`git push --tags` would publish them; to remove them locally: "
            f"git tag -d {' '.join(tags)}"))
    release = config.get("release") if isinstance(config.get("release"), dict) else {}
    checks = [("tag_remote", config.get("tag_remote")),
              ("records_remote", config.get("records_remote"))]
    if release.get("source_tag") is True:
        checks.append(("release.source_remote", release.get("source_remote", "origin")))
    for key, name in checks:
        if not name:
            continue
        why = private_target_refusal(source_root, config, name)
        if why is not None:
            findings.append(_warning(key, f"private refs will not be pushed to '{name}': {why}"))
    return findings


def private_remotes_line(source_root: Path, config: dict, name: str) -> str | None:
    """§18.11: for source remote `name`, the exact TOML
    line `private_remotes = [...]` that lists its unlisted
    remote_url_values (existing entries kept, new ones appended in V's
    order), or None when nothing can be added. A value that is always
    refused (the publish repo, ext::/fd::) or one with surrounding whitespace is
    never listed; private_target_refusal reports it. The caller picks the
    remote (init's offer uses sync's records_remote_name)."""
    existing = [p for p in config.get("private_remotes") or [] if isinstance(p, str)]
    new = [v for v in remote_url_values(source_root, name)
           if v not in existing and v == v.strip()
           and _publish_repo_refusal(source_root, config, v) is None]
    if not new:
        return None
    return "private_remotes = [" + ", ".join(json.dumps(v) for v in existing + new) + "]"


# === core: confirmation ===
# Design §17.3: accident prevention. A covered
# command asks one [y/N] on a terminal, or needs --yes off one.

def confirmation_mode(*, yes: bool, tty: bool) -> str:
    """Pre: runs in main for a covered command, before any fetch, lock or
    write. Post: "yes" when `yes`; else "prompt" when `tty` (stdin and
    stderr are terminals); else ConfigError (1)."""
    if yes:
        return "yes"
    if tty:
        return "prompt"
    raise ConfigError("ERROR: publishing needs confirmation: run on a terminal, or pass --yes")


def confirm_typed_version(summary: str, version: str) -> bool:
    """The one prompt of a covered run that will upload to PyPI (design
    §17.3): prints `summary`, then `Type <version> to
    release: `, and reads one line; only the exact version confirms.
    --quiet does not suppress it."""
    sys.stderr.write(summary.rstrip("\n") + f"\nType {version} to release: ")
    sys.stderr.flush()
    return sys.stdin.readline().strip() == version


def confirm_yes_no(summary: str) -> bool:
    """The one show_and_confirm of a covered run in "prompt" mode: prints
    `summary` (the preview; with S10b, its acceptance as well) to stderr,
    then `Publish? [y/N] `, and reads one line. --quiet does not suppress it."""
    sys.stderr.write(summary.rstrip("\n") + "\nPublish? [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _without_expect_tip(argv: list[str]) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False
        elif a == "--expect-tip":
            skip = True
        elif not a.startswith("--expect-tip="):
            out.append(a)
    return out


@contextmanager
def _offterminal_source_lock(source_root: Path) -> Iterator[None]:
    """The off-terminal preview's shared source lock, without
    creating anything. The lock file is opened read-only, never with
    O_CREAT; when it is absent the preview runs without a lock. When
    present, LOCK_SH|LOCK_NB; contention raises LockContention at once."""
    fd = None
    if fcntl is not None:
        with suppress(FileNotFoundError):
            fd = os.open(source_common_dir(source_root) / SOURCE_LOCK_NAME, os.O_RDONLY)
    try:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LockContention("ERROR: another pubrepo command is using this source "
                                     "repository") from None
        yield
    finally:
        if fd is not None:
            os.close(fd)             # closing the fd releases the flock


def _offterminal_preview(source_root: Path, config: dict, *, config_relpath: str | None,
                         accept_divergence: str | None, recreate: bool) -> PreviewReport:
    """_preview_locked for the off-terminal path: offline, under
    _offterminal_source_lock instead of source_lock, modelling the
    publish's --accept-source-divergence and --recreate-branch."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    with _offterminal_source_lock(source_root):
        assert_no_pull_in_progress(source_root, command="publish")
        return preview(source_root, publish_dir, config, remote=False,
                       config_relpath=config_relpath, accept_divergence=accept_divergence,
                       recreate=recreate)


def _release_conflict(args) -> str | None:
    """The publish flag in `args` that cannot be combined with a release,
    or None. main refuses each of them with --release/--bump."""
    return ("--amend" if args.amend is True
            else "--accept-source-divergence" if args.accept_source_divergence
            else "--recreate-branch" if args.recreate_branch else None)


def offterminal_refusal(source_root: Path, config: dict, args, argv: list[str], *,
                        config_relpath: str | None, config_label: str) -> ConfigError:
    """Off a terminal, a covered publish without --yes shows
    an offline preview and the next command on stdout, then is refused.

    Pre: a covered real publish, not --yes, not is_interactive(); the -m
    scrub has passed. Offline only: no fetch, ls-remote, records fetch or
    provider call; a shallow .publish/ is not previewed (check_shallow would
    fetch to unshallow it). Prints to stdout (the last line even with
    --quiet), flushes, and RETURNS the unchanged §17.3 ConfigError for main
    to raise (exit 1). A release (--release/--bump) is never offered a --yes
    command: --yes skips its typed confirmation."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    releasing = bool(args.release_version or args.bump)
    report, title, unavailable = None, None, None
    if (publish_dir / ".git").exists():
        try:
            if git("rev-parse", "--is-shallow-repository",
                   cwd=publish_dir).stdout.strip() == "true":
                unavailable = (f"{publish_dir.name}/ is a shallow clone; previewing it would "
                               f"fetch its full history")
            else:
                validate_includes(source_root, config, config_label=config_label)
                report = _offterminal_preview(
                    source_root, config, config_relpath=config_relpath,
                    accept_divergence=args.accept_source_divergence,
                    recreate=args.recreate_branch)
                if args.amend and args.message is None and report.baseline.tip:
                    title = _reused_title(publish_dir, report.baseline.tip)
        except PubrepoError as e:
            report, unavailable = None, str(e).splitlines()[0]
        if not ui.quiet:
            if report is not None:
                print(f"Preview, offline ({report.baseline.label}):")
                _render_dry_run(source_root, config, report, custom_message=args.message,
                                as_json=False, title=title)
                if args.amend and report.baseline.tip:
                    print(f"Action: --amend replaces public commit {report.baseline.tip[:12]}; "
                          f"its checks (tagged, merge, ours) run when you publish.")
                if args.recreate_branch:
                    print("Action: --recreate-branch: previewed as a new branch from this "
                          "source; if the branch still exists, publish fast-forwards instead.")
            else:
                print(f"Preview unavailable: {unavailable}")
    elif not ui.quiet:
        print("Preview, offline (not initialized):")
        try:
            _dry_run_uninitialized(source_root, config, False, custom_message=args.message)
        except PubrepoError as e:
            print(f"Preview unavailable: {str(e).splitlines()[0]}")

    notes: list[str] = []
    if report is None:
        last = "Not published: no terminal to confirm."
    else:
        tip = report.baseline.tip
        scan = _message_scan(config, args.message, title, tip)
        hit = scan is not None and scan[1].count > 0
        plan = report.plan
        # --recreate-branch: the last-fetched tip may be of a deleted branch,
        # so the command is not bound to it.
        bind = ["--expect-tip", tip] if tip and not args.recreate_branch else []
        if hit:
            last = "Not published: no terminal to confirm; resolve the above first."
        elif args.amend and not tip:
            last = "Not published: --amend: no previous publish to amend."
        elif report.predicted_exit != 0 or report.blocking():
            last = "Not published: no terminal to confirm; resolve the above first."
        elif releasing:
            # bound to the previewed tip, typed confirmation kept.
            last = ("Not published: releases must be confirmed on a terminal. "
                    "Run this command in a terminal: "
                    + shlex.join([_PROG, *_without_expect_tip(argv), *bind]))
            notes.append("The release part (build, tag, uploads) is not shown.")
        elif not (args.amend or plan.adds or plan.mods or plan.dels):
            last = "Not published: nothing to publish as of the last fetch."
        else:
            cmd = [_PROG, *_without_expect_tip(argv), "--yes", *bind]
            if release_enabled(config) and not args.no_release:
                cmd.append("--no-release")
                if _release_conflict(args) is None:
                    notes.append("The release part is not shown. To release instead, run in "
                                 "a terminal: " + shlex.join([_PROG, *argv])
                                 + " --release <version>")
            last = "Not published: no terminal to confirm. Review the above, then run: " \
                + shlex.join(cmd)
    print(last)
    if not ui.quiet:
        for note in notes:
            print(note)
    sys.stdout.flush()
    return ConfigError("ERROR: publishing needs confirmation: run on a terminal, or pass --yes")


# === core: snapshot and push ===
# Frozen interface (§2.3, core → sync).

PushStatus = Literal["ok", "rejected_nonff", "lease_stale", "host_refused",
                     "atomic_unsupported", "ambiguous_landed", "ambiguous_absent"]
PerRefStatus = Literal["ok", "rejected_nonff", "lease_stale", "host_refused",
                       "atomic_collateral"]


@dataclass
class PushOutcome:
    """Classification, with git run under LC_ALL=C:
      "[remote rejected]" → host_refused (the server's reason is never parsed);
      "[rejected]" → git's own reason: non-fast-forward | fetch first →
        rejected_nonff; stale info → lease_stale; atomic push failed →
        atomic_collateral; anything else → host_refused (reason shown);
      " ", "+", "*", "=" flags → ok.
    atomic_unsupported: no porcelain line and "does not support --atomic"
    on stderr. Any other no-porcelain failure → ambiguous_* (fetch and
    ancestry decide)."""
    status: PushStatus
    reason: str
    per_ref: list[tuple[str, PerRefStatus]]


_REGULAR_MODES = ("100644", "100755")
_NULL_OID = "0" * 40


def _tree_entries(publish_dir: Path, rev: str | None) -> dict[str, tuple[str, str, str]]:
    """path → (mode, type, oid) for every entry under `rev`, recursively."""
    if rev is None:
        return {}
    out = git("ls-tree", "-r", "-z", "--full-tree", rev, cwd=str(publish_dir)).stdout
    entries = {}
    for record in filter(None, out.split("\0")):
        meta, path = record.split("\t", 1)
        mode, kind, oid = meta.split()
        entries[path] = (mode, kind, oid)
    return entries


def _parent_mode(publish_dir: Path, snapshot: str | None, path: str) -> str:
    """The mode `path` had in `snapshot`'s first parent, for `restore`."""
    if snapshot:
        listed = git("ls-tree", "-z", f"{snapshot}^", "--", path,
                     cwd=str(publish_dir), check=False).stdout.split("\t", 1)[0].split()
        if listed and listed[0] in _REGULAR_MODES:
            return listed[0]
    return "100644"


TEMP_INDEX_PREFIX = "pubrepo-idx-"


@contextmanager
def _temp_index(publish_dir: Path) -> Iterator[dict]:
    """A temporary index under `.publish/.git` (pubrepo-idx-*); the real
    index is never touched. Yields the environment that selects it.
    It is removed on exit; a removal that fails is an error (exit 2) unless
    another exception is already propagating."""
    fd, index = tempfile.mkstemp(dir=str(_git_dir(publish_dir)), prefix=TEMP_INDEX_PREFIX)
    os.close(fd)
    failed: list[str] = []
    try:
        yield {**os.environ, "GIT_INDEX_FILE": index}
    finally:
        for leftover in (index, index + ".lock"):
            try:
                os.unlink(leftover)
            except FileNotFoundError:
                pass
            except OSError as e:
                failed.append(f"{leftover}: {e}")
    if failed:
        raise PublishError(f"ERROR: could not remove the temporary index "
                           f"({'; '.join(failed)}); remove it by hand")


def _write_tree(publish_dir: Path, base: str | None, updates: list[str]) -> str:
    """read-tree base into a temporary index, apply `--index-info` lines,
    write-tree. The real index is never touched."""
    with _temp_index(publish_dir) as env:
        try:
            git("read-tree", *([base] if base else ["--empty"]), cwd=str(publish_dir), env=env)
            if updates:
                result = _run_git_process(["git", "update-index", "-z", "--index-info"],
                                        cwd=str(publish_dir), env=env, capture_output=True,
                                        input="".join(updates).encode())
                if result.returncode != 0:
                    raise GitError(["update-index", "--index-info"],
                                   result.stderr.decode(errors="replace"))
            return git("write-tree", cwd=str(publish_dir), env=env).stdout.strip()
        except GitError as e:
            raise PublishError(f"ERROR: staging failed: {sanitize_provider_output(e.stderr.strip())}") from e


def _git_dir(repo: Path) -> Path:
    out = git("rev-parse", "--git-dir", cwd=str(repo)).stdout.strip()
    path = Path(out)
    return path if path.is_absolute() else (repo / path).resolve()


def build_snapshot_tree(publish_dir: Path, tip: str | None, proj: Projection,
                        integ: IntegrationState, config: dict, *,
                        mode: Literal["publish", "preview"] = "publish") -> SnapshotPlan:
    """The carry-forward tree (design §4.2, INV-3 (a)-(e)).

    Pre (publish): tip == integ.ip, integ.items is empty, and the caller has
    refused S7, S10 and S12.
    Post: the base tree (the tip, or empty for a first publish; in preview
    the IP tree when tip != IP) with, per path:
      (a) managed now: set to Π when integ.accounted, else a pull item;
      (b) unmanaged, written before: deleted when tip == LW ("removed from
          the managed set") or by a delete/remove decision; carried after
          `leave`; else a pull item;
      (c) unmanaged, never written: carried, deleted only by `remove`;
          an unmanaged baseline item follows its decision (leave carries,
          remove deletes, restore re-adds Decision.result). These decided
          changes go to `plan.decided`, never `plan.written`, so the
          path stays class (c) on later publishes;
      (d) Π entries absent from the base are added under (a).
    Public symlinks and gitlinks are carried. A pull item in publish mode
    raises AssertionError (S9 must already have refused it); in preview it
    stays at base content and is listed in `items`. No ref, worktree or
    real-index change.
    """
    publish = mode == "publish"
    if publish:
        assert tip == integ.ip, f"publish needs tip == IP ({tip} != {integ.ip})"
        assert not integ.items, "publish with undecided pull items (S9 not refused)"
        base = tip
    else:
        base = integ.ip if integ.ip is not None and tip != integ.ip else tip

    entries = _tree_entries(publish_dir, base)
    item_paths = {item.path for item in integ.items}
    baseline = {item.path: item for item in integ.baseline_items}
    plan = SnapshotPlan(tree="", adds=[], mods=[], dels=[], carried_count=0, written={},
                        nonderivable=[], keep=[], items=[])
    updates: list[str] = []

    def note(path: str, value: str, decided_by: Decision | None) -> None:
        """A pubrepo write enters `written`; a change an owner's decision made
        to an unmanaged path enters `decided` instead (§3.1)."""
        if decided_by is None:
            plan.written[path] = value
        else:
            plan.decided[path] = {"decision": decided_by.decision,
                                  "blob": None if value == "deleted" else value,
                                  "record": decided_by.record}

    def put(path: str, file_mode: str, blob: str, reason: str = "",
            decided_by: Decision | None = None) -> None:
        old = entries.get(path)
        if old and (old[0], old[2]) == (file_mode, blob):
            return
        (plan.mods if old else plan.adds).append((path, reason))
        note(path, blob, decided_by)
        updates.append(f"{file_mode} {blob}\t{path}\0")

    def drop(path: str, reason: str = "", decided_by: Decision | None = None) -> None:
        if path not in entries:
            return
        plan.dels.append((path, reason))
        note(path, "deleted", decided_by)
        updates.append(f"0 {_NULL_OID}\t{path}\0")

    def pull_item(path: str, why: str) -> None:
        if publish:
            raise AssertionError(f"{path}: {why} at publish time; S9 must already have refused")
        plan.items.append(path)

    def carry(path: str) -> None:
        if path in entries:
            plan.carried_count += 1

    for path in sorted(set(entries) | set(proj.entries) | set(baseline)):
        old = entries.get(path)
        base_blob = old[2] if old and old[1] == "blob" else None
        regular = old is None or (old[1] == "blob" and old[0] in _REGULAR_MODES)
        if path in item_paths:
            pull_item(path, "undecided pull item")
            continue
        pi = proj.entries.get(path)
        decision = integ.decision_for(path, base_blob)
        verdict = decision.decision if decision else None
        named = f"by decision {decision.record[:12]}" if decision else ""
        if pi is not None or (regular and managed_now(path, config)):       # (a), (d)
            # A change the newest applicable decision authorizes is named
            # with it.
            if not integ.accounted(path, base_blob):
                pull_item(path, "unaccounted managed path")
            elif pi is None:
                drop(path, named)
            else:
                put(path, pi.mode, pi.blob, named)
            continue
        if not regular:                                                       # §4.6 item 7
            carry(path)
        elif path in baseline:
            if verdict == "remove":
                drop(path, f"removed {named}", decided_by=decision)
            elif verdict == "restore" and decision.result:
                put(path, _parent_mode(publish_dir, baseline[path].snapshot, path),
                    decision.result, f"restored {named}", decided_by=decision)
            elif verdict in (None, "leave"):      # undecided ones are in integ.items
                carry(path)
            else:
                pull_item(path, f"baseline item with decision {verdict!r}")
        elif integ.written_ever(path):                                        # (b)
            last = integ.lw(path)
            if old is None:
                pass
            elif last not in (None, "deleted") and base_blob == last:
                drop(path, "removed from the managed set")
            elif verdict in ("delete", "remove"):
                drop(path, f"removed {named}")
            elif verdict == "leave":
                carry(path)
            else:
                pull_item(path, "unmanaged path changed since pubrepo wrote it (C8b)")
        elif verdict == "remove":                                             # (c)
            drop(path, f"removed {named}", decided_by=decision)
        else:
            carry(path)

    plan.tree = _write_tree(publish_dir, base, updates)
    plan.nonderivable = sorted(p for p in plan.written if p in proj.entries
                               and (proj.entries[p].nonderivable or p in proj.dirty_paths))
    plan.keep = sorted(p for p, e in proj.entries.items() if e.keep)
    return plan


def _git_c(*args, cwd: Path, check: bool = False) -> subprocess.CompletedProcess:
    """git under LC_ALL=C, for output this module classifies."""
    return git(*args, cwd=str(cwd), check=check, env={**os.environ, "LC_ALL": "C"})


# Porcelain flags that mean the ref update happened or was a no-op:
# " " fast-forward, "+" forced, "*" new ref, "=" up to date, "-" deleted.
_PUSH_OK_FLAGS = frozenset(" +*=-")
_PUSH_LINE_RE = re.compile(r"^(?P<flag>.)\t(?P<src>[^:\t]*):(?P<dst>[^\t]+)\t"
                           r"(?P<summary>\[[^\]]*\]|\S+)(?: \((?P<reason>.*)\))?$")
# When refs fail for different reasons, the outcome reports the one that
# decides the next action: a moved tip (rerun/pull) before a host refusal.
_PUSH_STATUS_ORDER = ("lease_stale", "rejected_nonff", "host_refused", "atomic_collateral")


def _refspec_parts(refspec: str) -> tuple[str, str]:
    """(src, dst) of an explicit refspec; src is "" for a deletion. dst must
    be a full ref name: porcelain lines are matched to refspecs by it."""
    src, sep, dst = refspec.lstrip("+").partition(":")
    if not sep or not dst.startswith("refs/"):
        raise ValueError(f"push_porcelain needs explicit src:refs/... refspecs, got {refspec!r}")
    return src, dst


def _classify_push_line(flag: str, summary: str, reason: str) -> PerRefStatus:
    if flag in _PUSH_OK_FLAGS:
        return "ok"
    if summary == "[remote rejected]":
        return "host_refused"            # the server's reason is never parsed
    if summary == "[rejected]":
        if reason in ("non-fast-forward", "fetch first"):
            return "rejected_nonff"
        if reason == "stale info":
            return "lease_stale"
        if reason == "atomic push failed":
            return "atomic_collateral"
    return "host_refused"


def _ref_landed(publish_dir: Path, src: str, dst: str) -> bool | None:
    """Whether `src:dst` is in effect at origin: True, False, or None when the
    check itself cannot reach it. A branch landed when its tip is `src` or
    descends from it (§4.4 confirmation); other refs must match exactly."""
    _guard_remote(publish_dir)
    listed = _git_c("ls-remote", "origin", dst, cwd=publish_dir)
    if listed.returncode != 0:
        return None
    remote = next((line.split("\t")[0] for line in listed.stdout.splitlines()
                   if line.split("\t")[-1] == dst), None)
    if not src:
        return remote is None
    want = git("rev-parse", "--verify", src, cwd=str(publish_dir)).stdout.strip()
    if remote is None or remote == want or not dst.startswith("refs/heads/"):
        return remote == want
    fetched = _git_c("fetch", "--no-tags", "origin", dst, cwd=publish_dir)
    if fetched.returncode != 0:
        return None
    return git("merge-base", "--is-ancestor", want, "FETCH_HEAD",
               cwd=str(publish_dir), check=False).returncode == 0


def _outcome_unknown(publish_dir: Path, unverified: list[tuple[str, str]]) -> PublishError:
    """When verification fails on transport the outcome is
    unknown, never "did not land". If it landed, the next run identifies the
    snapshot by its trailer (§4.3 crash window)."""
    remote = sanitize_provider_output(git("config", "--get", "remote.origin.url",
                                          cwd=str(publish_dir), check=False).stdout.strip())
    what = ", ".join(git("rev-parse", "--verify", src, cwd=str(publish_dir)).stdout.strip()
                     if src else f"the deletion of {dst}" for src, dst in unverified)
    return PublishError(f"ERROR: push outcome unknown: could not verify whether {what} reached "
                        f"{remote}; nothing was changed locally; rerun to reconcile")


def _run_push(publish_dir: Path, push) -> subprocess.CompletedProcess:
    """Run `push()` (the `git push` call) with the push.ambiguous seams.
    With `exit128` armed, the wrapper reports git exit 128
    and no porcelain: push.ambiguous_absent before anything is sent,
    push.ambiguous after the ref has landed. Other actions fire through
    _test_fault at those points."""
    fake = subprocess.CompletedProcess(["git", "push"], 128, "",
                                       "fatal: the remote end hung up unexpectedly\n")
    if _armed_fault("push.ambiguous_absent") == "exit128":
        return fake
    _test_fault("push.ambiguous_absent")
    result = push()
    if _armed_fault("push.ambiguous") == "exit128":
        return fake
    _test_fault("push.ambiguous")
    return result


PUSH_LOCK_PREFIX = "DISABLED-"


def check_push_lock(publish_dir: Path) -> None:
    """Design §17.1: a `remote.origin.pushurl`
    starting with DISABLED- locks the clone. Raises PublishError (2) naming
    the value; called first in push_porcelain, before any git push runs."""
    values = git("config", "--get-all", "remote.origin.pushurl", cwd=str(publish_dir),
                 check=False).stdout.split()
    locked = next((v for v in values if v.startswith(PUSH_LOCK_PREFIX)), None)
    if locked is not None:
        raise PublishError(f"ERROR: publishing is locked for this clone "
                           f"(remote.origin.pushurl is {locked}); nothing was pushed")


def push_porcelain(publish_dir: Path, refspecs: list[str], *,
                   leases: dict[str, str] | None = None, atomic: bool = False) -> PushOutcome:
    """`git push --porcelain [--atomic] [--force-with-lease=…] origin
    <refspecs>` under LC_ALL=C, classified per PushOutcome (§2.3).
    Pushing to the remote name makes git update
    refs/remotes/origin/* itself.

    Pre: every refspec is explicit `src:refs/...` (`:refs/...` deletes).
    Post: check_push_lock runs first (a locked clone → 2, nothing pushed).
    `status` is "ok" only when every ref reports an ok flag. When refs
    fail differently, `status` is the first of lease_stale, rejected_nonff,
    host_refused present, and `reason` is that ref's. With no porcelain line
    for some ref: atomic_unsupported if git says so, otherwise each ref is
    checked at origin (ambiguous_landed if all landed, else
    ambiguous_absent). On the ambiguous paths `per_ref` lists only the refs
    that landed. If that check cannot reach origin, raises PublishError
    (exit 2, "push outcome unknown"); nothing local has moved.
    """
    parts = [_refspec_parts(r) for r in refspecs]
    check_push_lock(publish_dir)
    _guard_remote(publish_dir)
    argv = ["push", "--porcelain"] + (["--atomic"] if atomic else [])
    argv += [f"--force-with-lease={ref}:{expect}" for ref, expect in (leases or {}).items()]
    result = _run_push(publish_dir, lambda: _git_c(*argv, "origin", *refspecs, cwd=publish_dir))

    lines: dict[str, tuple[PerRefStatus, str]] = {}
    for line in result.stdout.splitlines():
        m = _PUSH_LINE_RE.match(line)
        if m:
            reason = m["reason"] or ""
            lines[m["dst"]] = (_classify_push_line(m["flag"], m["summary"], reason),
                               sanitize_provider_output(reason))

    if all(dst in lines for _, dst in parts):
        per_ref = [(dst, lines[dst][0]) for _, dst in parts]
        failed = {status for _, status in per_ref if status != "ok"}
        if not failed:
            return PushOutcome("ok", "", per_ref)
        status = next(s for s in _PUSH_STATUS_ORDER if s in failed)
        if status == "atomic_collateral":   # git always names the cause; be safe
            status = "host_refused"
        reason = next((lines[dst][1] for dst, st in per_ref if st == status), "")
        return PushOutcome(status, reason, per_ref)

    stderr = sanitize_provider_output(result.stderr.strip())
    if "does not support --atomic" in stderr:
        return PushOutcome("atomic_unsupported", stderr, [])
    checked = [(src, dst, _ref_landed(publish_dir, src, dst)) for src, dst in parts]
    unverified = [(src, dst) for src, dst, landed in checked if landed is None]
    if unverified:
        raise _outcome_unknown(publish_dir, unverified)
    landed = [dst for _, dst, ok in checked if ok]
    status = "ambiguous_landed" if len(landed) == len(parts) else "ambiguous_absent"
    return PushOutcome(status, stderr or f"git push exited {result.returncode}",
                       [(dst, "ok") for dst in landed])


def fetch_tip(publish_dir: Path, branch: str) -> str | None:
    """Fetch origin/<branch> and return its sha.

    Post: the tip as this fetch saw it (FETCH_HEAD). The argv has no
    refspec destination and no `+` (AC62): the clone's configured
    remote.origin.fetch updates refs/remotes/origin/<branch>
    opportunistically, so a rewritten remote is still seen (S7). None only
    when the branch ref is absent (S6 vs S11 is branch_lifecycle's call); an
    existing tracking ref is then left as evidence. A transport failure
    raises PublishError (exit 2, S5).
    """
    ref = f"refs/heads/{branch}"
    _guard_remote(publish_dir)
    # §18.13 condition 1: T is what this fetch wrote; --write-fetch-head
    # overrides a fetch.writeFetchHEAD=false that would leave a stale one.
    fetched = _git_c("fetch", "--no-tags", "--write-fetch-head", "origin", ref, cwd=publish_dir)
    if fetched.returncode != 0:
        if f"couldn't find remote ref {ref}" in fetched.stderr:
            return None
        raise PublishError(f"ERROR: cannot reach remote; publish requires push access.\n"
                           f"{sanitize_provider_output(fetched.stderr.strip())}")
    return git("rev-parse", "--verify", "FETCH_HEAD^{commit}", cwd=str(publish_dir)).stdout.strip()


def confirm_by_ancestry(publish_dir: Path, sha: str, branch: str) -> bool:
    """Design §4.4 confirmation: fetch origin/<branch>, then
    `merge-base --is-ancestor sha origin/<branch>`. A transport failure
    raises PublishError (fetch_tip)."""
    tip = fetch_tip(publish_dir, branch)
    if tip is None:
        return False
    return git("merge-base", "--is-ancestor", sha, tip,
               cwd=str(publish_dir), check=False).returncode == 0


def push_snapshot(publish_dir: Path, sha: str, branch: str) -> PushOutcome:
    """Design §4.4: push the snapshot as a plain fast-forward (no lease, no
    --atomic) to origin's refs/heads/<branch>. git moves the tracking ref
    itself; nothing else local moves here."""
    outcome = push_porcelain(publish_dir, [f"{sha}:refs/heads/{branch}"])
    _test_fault("publish.after_push")
    return outcome


def _identity_hint(publish_dir: Path, stderr: str) -> str:
    if "who you are" in stderr or "Author identity unknown" in stderr:
        return (f"\nNo git identity in {publish_dir.name}/ — set one:\n"
                f"  git -C {publish_dir.name} config user.name 'Your Name'\n"
                f"  git -C {publish_dir.name} config user.email 'you@example.com'")
    return ""


def snapshot_commit(publish_dir: Path, tree: str, parents: Sequence[str],
                    message: str) -> str:
    """`commit-tree` with `-p` for each parent; empty `parents` is a root
    commit. Leaves no ref and no worktree change."""
    return _commit_tree(publish_dir, tree, parents, message)


def _commit_tree(publish_dir: Path, tree: str, parents: Sequence[str], message: str,
                 author: tuple[str, str] | None = None) -> str:
    """snapshot_commit, optionally keeping an existing (name, email) author
    (--amend keeps the replaced commit's author, as `commit --amend` did)."""
    args = ["commit-tree", tree, "-m", message]
    for parent in parents:
        args += ["-p", parent]
    env = None
    if author is not None:
        env = {**os.environ, "GIT_AUTHOR_NAME": author[0], "GIT_AUTHOR_EMAIL": author[1]}
        env.pop("GIT_AUTHOR_DATE", None)
    try:
        return git(*args, cwd=str(publish_dir), env=env).stdout.strip()
    except GitError as e:
        raise PublishError(f"{e}{_identity_hint(publish_dir, e.stderr)}") from e


def _backup_ref_name(publish_dir: Path) -> str:
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S")
    name, n = f"refs/pubrepo/local/{stamp}", 1
    while git("rev-parse", "--verify", "-q", name, cwd=str(publish_dir), check=False).returncode == 0:
        n += 1
        name = f"refs/pubrepo/local/{stamp}-{n}"
    return name


def advance_local(publish_dir: Path, branch: str, sha: str, keep: list[str], *,
                  replaced: Sequence[str] = ()) -> list[str]:
    """Pre: the snapshot is confirmed (design §4.4); HEAD is
    refs/heads/<branch> (the caller's branch check).

    Post: local-only commits (on refs/heads/<branch>, not in the history of
    sha or of any `replaced` public commit) are named and backed up to
    refs/pubrepo/local/<ts> in `.publish` (§4.10); `update-ref
    refs/heads/<branch> sha`; the index and worktree are refreshed to sha
    with `read-tree -u --reset`, keeping the bytes of top-level keep files.
    Returns the backup refs created. `replaced`: the public tip an
    --amend replaced; it and its ancestors are public, never local-only.
    """
    ref = f"refs/heads/{branch}"
    cwd = str(publish_dir)
    current = git("rev-parse", "--verify", "-q", ref, cwd=cwd, check=False).stdout.strip()
    backups = []
    if current:
        local_only = git("rev-list", current, "--not", sha, *replaced, cwd=cwd).stdout.split()
        if local_only:
            backup = _backup_ref_name(publish_dir)
            git("update-ref", backup, current, "", cwd=cwd)
            backups.append(backup)
            listing = git("log", "--format=  %h %s", current, "--not", sha, *replaced,
                          cwd=cwd).stdout.rstrip()
            ui.warn(f"WARNING: {len(local_only)} local commit(s) in {publish_dir.name}/ were "
                    f"never published; saved to {backup}:\n{listing}")
    git("update-ref", ref, sha, current, cwd=cwd)

    kept = {}
    for name in keep:
        path = publish_dir / name
        if path.is_file() and not path.is_symlink():
            kept[name] = (path.read_bytes(), path.stat().st_mode)
    git("read-tree", "-u", "--reset", sha, cwd=cwd)
    for name, (data, mode) in kept.items():
        path = publish_dir / name
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        path.write_bytes(data)
        os.chmod(path, stat.S_IMODE(mode))
    return backups


# === core: publish ===
# The routine publish path (design §4). --stage is
# cmd_stage, --amend is amend_snapshot and --release/--bump is
# _release_routine, all on the same evaluation and snapshot builder.

@dataclass
class BranchState:
    """branch_lifecycle's verdict (design §4.9). kind: "exists" (the tip was
    fetched), "first" (absent, no evidence: root commit), "missing" (absent
    with evidence: S11), "recreate" (absent, --recreate-branch: root commit)."""
    kind: Literal["exists", "first", "missing", "recreate"]
    evidence: list[str]
    default_branch: str | None = None


def _log_has_pushed(publish_dir: Path) -> bool:
    return _read_last_log_publish(publish_dir) is not None


def _local_snapshot(source_root: Path, publish_dir: Path) -> str | None:
    """The newest commit in `.publish`'s local history that is ours."""
    head = git("rev-parse", "--verify", "-q", "HEAD", cwd=str(publish_dir), check=False)
    if head.returncode != 0:
        return None
    for sha in git("rev-list", "HEAD", cwd=str(publish_dir)).stdout.split():
        if classify_commit(source_root, publish_dir, sha).kind != "foreign":
            return sha
    return None


def _remote_default_branch(publish_dir: Path, branch: str) -> str | None:
    """The host's default branch when the remote has branches other than
    `branch` (design §4.9 item 1, AC74); None otherwise."""
    _guard_remote(publish_dir)
    heads = _git_c("ls-remote", "--heads", "origin", cwd=publish_dir).stdout.split("\n")
    if not any(line.strip() for line in heads):
        return None
    for line in _git_c("ls-remote", "--symref", "origin", "HEAD",
                       cwd=publish_dir).stdout.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            default = line[len("ref: "):-len("\tHEAD")].removeprefix("refs/heads/")
            return default if default != branch else None
    return None


def branch_lifecycle(publish_dir: Path, source_root: Path, config: dict,
                     tip: str | None, *, recreate: bool) -> BranchState:
    """Design §4.9. With the branch present: "exists". Absent: the evidence
    that we published to it is a pushed log entry, a remote-tracking ref,
    an ours-snapshot in local history (classify_commit), or a source
    record for this destination; any of them makes it S11 unless
    --recreate-branch."""
    if tip is not None:
        return BranchState("exists", [])
    branch = config["branch"]
    evidence = []
    if _log_has_pushed(publish_dir):
        evidence.append("the publish log records a pushed snapshot")
    if git("rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}",
           cwd=str(publish_dir), check=False).returncode == 0:
        evidence.append(f"{publish_dir.name}/ still has refs/remotes/origin/{branch}")
    local = _local_snapshot(source_root, publish_dir)
    if local:
        evidence.append(f"{publish_dir.name}/ history holds snapshot {local[:12]}")
    if records_has_destination(source_root, destination_identity(config)):
        evidence.append("a snapshot record names this destination")
    if evidence:
        return BranchState("recreate" if recreate else "missing", evidence)
    return BranchState("first", [], _remote_default_branch(publish_dir, branch))


@dataclass
class _PublishEval:
    """Everything the routine publish decides on, computed under the locks."""
    branch_state: BranchState
    proj: Projection
    integ: IntegrationState


def _evaluate_publish(source_root: Path, publish_dir: Path, config: dict, *,
                      config_relpath: str | None, accept: str | None,
                      recreate: bool, amend: bool = False,
                      expect_tip: str | None = None) -> _PublishEval:
    """The evaluation, from the pull-in-progress check to compute_integration.
    With `amend`, a missing branch is refused (exit 2). A
    fetched tip other than `expect_tip` → 5 right after the fetch, before any
    other check (§17.3: one message, and in tty mode before the
    prompt and again after the lock)."""
    branch = config["branch"]
    assert_no_pull_in_progress(source_root, command="publish")
    check_shallow(source_root, publish_dir)
    check_publish_clone_origin(publish_dir, config)     # R5: before the first fetch
    tip = fetch_tip(publish_dir, branch)
    ui.info(f"Fetching origin/{branch}... ok")
    if expect_tip is not None and tip != expect_tip:
        raise _expect_tip_refusal(expect_tip, tip)
    records_fetch(source_root, config)
    branch_state = branch_lifecycle(publish_dir, source_root, config, tip, recreate=recreate)
    if amend and branch_state.kind != "exists":
        if branch_state.kind == "first":
            raise PublishError("ERROR: --amend: no previous publish to amend")
        raise PublishError("ERROR: --amend: nothing on the remote to amend")
    if branch_state.kind == "missing":
        raise Diverged(branch_state.evidence,
                       f"ERROR: the public branch '{branch}' is missing, but it was published "
                       f"before ({'; '.join(branch_state.evidence)}).\n"
                       f"If it was deleted on purpose, recreate it from this source with:\n"
                       f"  {_PROG} publish --recreate-branch")
    if recreate and branch_state.kind == "exists":
        ui.info(f"Note: origin/{branch} exists; --recreate-branch has no effect")
    proj = build_projection(source_root, publish_dir, config)
    integ = compute_integration(source_root, publish_dir, config, tip, proj,
                                accept_divergence=accept, fetch_sigma=True,
                                config_relpath=config_relpath)
    return _PublishEval(branch_state, proj, integ)


def _commit_lines(commits: list[CommitInfo], limit: int = 20) -> str:
    lines = [f"  {c.sha[:7]} {c.subject}  ({c.author})" for c in commits[:limit]]
    if len(commits) > limit:
        lines.append(f"  … and {len(commits) - limit} more")
    return "\n".join(lines)


def _s10b_text(div: DivergenceInfo) -> str:
    if div.kind == "gone":
        head = (f"ERROR: the source commit for snapshot {div.snapshot[:12]} no longer exists "
                f"here: amended or rebased and pruned, filtered, or never pushed.")
    else:
        where = (f"on {', '.join(div.branches)}" if div.branches
                 else "on no local branch (rewritten)")
        head = (f"ERROR: snapshot {div.snapshot[:12]} was published from source commit "
                f"{(div.sigma or '?')[:12]} {where}, which has diverged from your checkout.")
    return (f"{head}\nIf this checkout is what you want public, publish over it with:\n"
            f"  {_PROG} publish {div.accept_flag}")


def _state_refusal(integ: IntegrationState) -> PubrepoError | None:
    """Design §9 publish column; None for the publishable states S0, S1, S6."""
    state = integ.state
    pull = f"run `{_PROG} pull`"
    if state in ("S0", "S1", "S6"):
        return None
    if state == "S7":
        lost = ", ".join(s[:12] for s in integ.missing_snapshots)
        what = (f"our snapshot(s) {lost} are no longer on public" if lost else
                "the commit our last pull integrated is no longer on public")
        return Diverged(integ.missing_snapshots,
                        f"ERROR: public history was rewritten: {what}.\n"
                        f"{pull} to review the new public state before publishing.")
    if state in ("S10b", "S10b+S2"):
        return DecisionRequired(_s10b_text(integ.divergence))
    if state == "S10a":
        div = integ.divergence
        if div.branches:     # σ is not reachable from HEAD's upstream
            return Diverged([div.snapshot],
                            f"ERROR: snapshot {div.snapshot[:12]} was published from "
                            f"{', '.join(div.branches)}, which is ahead of this checkout; "
                            f"check out or merge it, then publish.")
        return Diverged([div.snapshot],
                        f"ERROR: your source checkout is behind: snapshot {div.snapshot[:12]} was "
                        f"published from {(div.sigma or '?')[:12]}, which your HEAD does not "
                        f"contain yet; update it (git pull / git checkout) and rerun.")
    if state == "S12":
        hits = "\n".join(f"  {h.path} (removed by repair {h.repair[:12]})"
                         for h in integ.reintroduced)
        return Diverged([h.path for h in integ.reintroduced],
                        f"ERROR: content removed by a repair is on public again:\n{hits}\n"
                        f"{pull} to review it before publishing.")
    if state in ("S2", "S3"):
        copied = ("\nNote: some of these carry a copied `Source:` trailer; they are "
                  "treated as collaborator commits.") if integ.copied_trailer else ""
        return Diverged([c.sha for c in integ.unpulled],
                        f"ERROR: the public branch has {len(integ.unpulled)} commit(s) you have "
                        f"not pulled:\n{_commit_lines(integ.unpulled)}\n"
                        f"{pull} to integrate them, then publish.{copied}")
    if state == "S9":
        command = f"{_PROG} pull --baseline" if any(i.origin == "baseline" for i in integ.items) \
            else f"{_PROG} pull"
        items = "\n".join(f"  {i.path} ({i.cls})" for i in integ.items)
        return DecisionRequired(f"ERROR: {len(integ.items)} public path(s) need a decision before "
                                f"publishing:\n{items}\nRun `{command}` to decide them.")
    raise AssertionError(f"unhandled integration state {state!r}")


def _gate_publish(publish_dir: Path, ev: _PublishEval, plan: SnapshotPlan, config: dict,
                  log_entry: dict) -> None:
    """Design §4.6 at publish: every projection entry is scanned and blocks
    (S8, exit 4); allowlisted content is reported every run; carried content
    is scanned, reported with the --redecide route, and never blocks."""
    forbidden = config.get("scrub", {}).get("forbidden", [])
    managed = gate_scan(publish_dir, [(p, e.blob) for p, e in ev.proj.entries.items()], config)
    for f in managed.findings:
        if f.allowlisted:
            ui.info(f"  {_finding_label(f)}: {f.detail}")
    blocking = managed.blocking()
    if blocking:
        _append_publish_log(publish_dir, {**log_entry, "added": None, "modified": None,
                                          "deleted": None, "scrub_result": "failed",
                                          "pushed": False, "public_commit": None})
        detail = "\n".join(f"  {_finding_label(f)}: {f.detail}" for f in blocking)
        paths = sorted({f.path for f in blocking})
        raise ScrubFailure(
            [{"file": f.path, "part": f.part, "kind": f.kind, "detail": f.detail}
             for f in blocking],
            f"\nSCRUB FAILED — {len(paths)} file(s) blocked by the outbound gate:\n{detail}\n"
            f"\nPublish aborted. Fix the source files or update [publish.scrub] / "
            f"[publish.transforms].")
    if forbidden:
        n = sum(1 for e in ev.proj.entries.values() if not e.keep)
        ui.info(f"Scrubbing ({n} files, {len(forbidden)} patterns)... ok")
    _report_carried(_carried_findings(publish_dir, plan.tree, ev.proj, config))


def _carried_findings(publish_dir: Path, tree: str, proj: Projection,
                      config: dict) -> list[GateFinding]:
    """Design §4.6 item 6: the gate over carried content (regular files in
    the snapshot tree that the projection does not manage)."""
    entries = _tree_entries(publish_dir, tree)
    carried = [(p, oid) for p, (mode, kind, oid) in entries.items()
               if kind == "blob" and mode in _REGULAR_MODES and p not in proj.entries]
    return [f for f in gate_scan(publish_dir, carried, config, carried=True).findings
            if not f.allowlisted]


def _report_carried(findings: list[GateFinding]) -> None:
    for f in findings:
        ui.warn(f"WARNING: {_finding_label(f)}: {f.detail} — already public, not written by "
                f"pubrepo. To remove it: {_PROG} pull --redecide {f.path} → remove")


def _finding_label(f: GateFinding) -> str:
    return f"{f.path}!{f.part}" if f.part else f.path


def _source_full_head(source_root: Path) -> str | None:
    head = git("rev-parse", "--verify", "-q", "HEAD", cwd=str(source_root), check=False)
    return head.stdout.strip() if head.returncode == 0 else None


def _bookmark_publish(source_root: Path, config: dict) -> None:
    """A clean publish's bookmark (§18.3, R2): local always, pushed only to
    a checked tag_remote."""
    ref = write_bookmark(source_root)
    if ref is not None:
        push_bookmark(source_root, config, ref)


def _push_refusal(outcome: PushOutcome, sha: str, branch: str) -> PubrepoError | None:
    """Design §4.4 outcome → exit (ok and ambiguous_landed are confirmed)."""
    if outcome.status in ("ok", "ambiguous_landed"):
        return None
    if outcome.status == "rejected_nonff":
        return Diverged([branch], f"ERROR: the public branch moved during this run "
                        f"({outcome.reason}); nothing was changed locally.\n"
                        f"run `{_PROG} pull`, then publish again.")
    if outcome.status == "host_refused":
        return PublishError(f"ERROR: host refused the push: {outcome.reason}\n"
                            f"Nothing was changed locally.")
    return PublishError(f"ERROR: push failed: {sanitize_provider_output(outcome.reason)}\n"
                        f"{sha[:12]} is not on the remote; nothing was changed locally.")


def _confirm_pushed(publish_dir: Path, sha: str, branch: str) -> None:
    """Design §4.4: after a porcelain `ok`, the
    local ref advances only once confirm_by_ancestry finds `sha` on the
    public branch. Not contained → Diverged (5); the check cannot reach the
    remote → PublishError (2). Nothing local has moved in either case."""
    try:
        landed = confirm_by_ancestry(publish_dir, sha, branch)
    except PublishError as e:
        raise PublishError(f"ERROR: pushed {sha[:12]}, but could not confirm it on the public "
                           f"branch ({e}); nothing was changed locally; rerun to reconcile") from e
    if not landed:
        raise Diverged([branch], f"ERROR: {sha[:12]} was pushed, but the public branch no "
                       f"longer contains it; nothing was changed locally.\n"
                       f"run `{_PROG} pull`, then publish again.")


def _drop_accepted(source_root: Path, publish_dir: Path, accepted: str | None,
                   tip: str | None) -> None:
    """Design §4.8: an accepted snapshot that `tip` does not contain
    gets its `dropped` status marker, so the next publish is not S7 again.
    Called after a confirmed push (tip = the new snapshot) and on "Nothing to
    publish" (tip = the public tip; acceptance itself confirms).
    A record failure warns; the next run sees S7 and asks again."""
    if not accepted or (tip and git("merge-base", "--is-ancestor", accepted, tip,
                                    cwd=str(publish_dir), check=False).returncode == 0):
        return
    try:
        write_status_marker(source_root, accepted, "dropped",
                            reason="accepted source divergence; no longer on public")
    except (PubrepoError, OSError) as e:
        ui.warn(f"WARNING: could not record {accepted[:12]} as dropped ({e})")


def _publish_snapshot(source_root: Path, publish_dir: Path, config: dict, ev: _PublishEval,
                      *, custom_message: str | None, config_relpath: str | None,
                      config_file: Path, accepted: str | None, mark) -> int:
    """From the gate to the audit tag. No ref moves before
    the push is confirmed."""
    branch = config["branch"]
    proj, integ = ev.proj, ev.integ
    tip = integ.tip
    source_hash, is_dirty = get_source_info(source_root)
    files = sum(1 for e in proj.entries.values() if not e.keep)
    ui.info(f"Rebuilding {publish_dir.name}/ ({files} files)... ok")
    mark("rebuild")
    transformed = sorted(p for p, e in proj.entries.items() if e.transformed)
    if transformed:
        ui.info(f"Transforming ({len(transformed)})... ok")
        ui.detail(f"transformed: {', '.join(transformed)}")
    if proj.dirty_paths:
        ui.warn(f"WARNING: publishing uncommitted changes ({len(proj.dirty_paths)} path(s): "
                f"{', '.join(proj.dirty_paths[:5])}{' …' if len(proj.dirty_paths) > 5 else ''}); "
                f"the snapshot record lists them as nonderivable")
    forbidden = config.get("scrub", {}).get("forbidden", [])
    log_entry = {
        "ts": datetime.now().astimezone().isoformat(),
        "source_commit": source_hash,
        "source_branch": _source_branch(source_root),
        "source_dirty": is_dirty,
        "files": files,
        "transforms_applied": len(transformed),
        "scrub_patterns": len(forbidden),
        "forced": False,
        "title": custom_message or None,
        "amend": False,
        "amended_from": None,
    }

    plan = build_snapshot_tree(publish_dir, tip, proj, integ, config)
    _gate_publish(publish_dir, ev, plan, config, log_entry)
    mark("scrub")
    changed = bool(plan.adds or plan.mods or plan.dels)
    build_push_plan(source_root, publish_dir, config, kind="publish", refs={
        "public": [f"refs/heads/{branch}"] if changed else [],
        "bookmark": changed and not is_dirty,
        "records": records_remote_name(source_root, config)})
    if not changed:
        _drop_accepted(source_root, publish_dir, accepted, tip)
        records_push_pending(source_root, config)
        ui.success("Nothing to publish")
        return EXIT_SUCCESS
    for path, reason in plan.dels:
        ui.info(f"  deleted: {path}" + (f" ({reason})" if reason else ""))
    for path, reason in plan.adds + plan.mods:
        if reason:
            ui.info(f"  {path} ({reason})")

    message = build_commit_message(
        source_hash, custom_message=custom_message, added=len(plan.adds),
        modified=len(plan.mods), deleted=len(plan.dels), transform_count=len(transformed),
        scrub_patterns=len(forbidden))
    assert_parent_provenance(publish_dir, [tip] if tip else [], tip=tip, fetched=set(),
                             created=set())                       # §18.13
    sha = snapshot_commit(publish_dir, plan.tree, [tip] if tip else [], message)
    ui.info("Committing... ok")
    mark("commit")

    outcome = push_snapshot(publish_dir, sha, branch)
    refusal = _push_refusal(outcome, sha, branch)
    if refusal is not None:
        raise refusal
    if outcome.status == "ok":              # ambiguous_landed is confirmed by ancestry already
        _confirm_pushed(publish_dir, sha, branch)
    ui.info("Pushing... ok")
    mark("push")

    advance_local(publish_dir, branch, sha, config.get("keep", []))
    destination = destination_identity(config)
    _drop_accepted(source_root, publish_dir, accepted, sha)
    try:
        sigma_full = _source_full_head(source_root) or source_hash
        write_snapshot_record(source_root, snapshot_sha=sha, destination=destination,
                              sigma_full=sigma_full, written=plan.written,
                              nonderivable=plan.nonderivable, keep=plan.keep,
                              config_path=config_relpath,
                              fingerprint=config_fingerprint(config), decided=plan.decided)
        register_target(source_root, str(config_file), destination)
    except (PubrepoError, OSError) as e:
        ui.warn(f"WARNING: published {sha[:12]}, but its record could not be written ({e}); "
                f"the next run identifies it by its Source: trailer")
    records_push_pending(source_root, config)

    _append_publish_log(publish_dir, {
        **log_entry, "added": len(plan.adds), "modified": len(plan.mods),
        "deleted": len(plan.dels), "scrub_result": "passed" if forbidden else "skipped",
        "pushed": True, "public_commit": sha})
    if not is_dirty:
        _bookmark_publish(source_root, config)
    ui.success(f"Published {source_hash}{'  (dirty)' if is_dirty else ''}")
    return EXIT_SUCCESS


class _NothingToPublish(Exception):
    """Raised inside a tty-mode compute when the snapshot equals the tip:
    nothing is asked and nothing is written."""


def _expect_tip_refusal(expect_tip: str, tip: str | None) -> Diverged:
    return Diverged([expect_tip], f"ERROR: the public tip moved: expected {expect_tip[:12]}, "
                    f"found {(tip or 'no branch')[:12]}; nothing was written.")


def _publish_summary(url: str, branch: str, plan: SnapshotPlan, note: str) -> str:
    """The §4.5 preview a prompt-mode publish shows before its one [y/N]."""
    lines = [note] if note else []
    lines.append(f"Publish to {url} ({branch}): {len(plan.adds)} added, {len(plan.mods)} "
                 f"modified, {len(plan.dels)} deleted")
    lines += [f"  A  {p}" + (f" ({r})" if r else "") for p, r in plan.adds]
    lines += [f"  M  {p}" + (f" ({r})" if r else "") for p, r in plan.mods]
    lines += [f"  D  {p}" + (f" ({r})" if r else "") for p, r in plan.dels]
    return "\n".join(lines)


def _publish_routine(source_root: Path, publish_dir: Path, config: dict, *,
                     custom_message: str | None, config_relpath: str | None,
                     config_file: Path, accept_divergence: str | None,
                     recreate_branch: bool,
                     mode: str = "yes", expect_tip: str | None = None) -> int:
    """Design §4 publish under §17.3. `mode` comes from confirmation_mode.
    "yes": source (exclusive) then destination locks at the start, held to
    the end. "prompt": confirm_then_lock with the one [y/N] (S10b acceptance
    folded in), bound to the tip and the snapshot tree. --expect-tip is
    compared right after every fetch (_evaluate_publish), so in prompt mode
    before the prompt and again after the lock, with one message (5)."""
    branch = config["branch"]
    url = sanitize_provider_output(config["remote"])           # shown in the preview only
    last = time.monotonic()

    def mark(phase: str) -> None:
        nonlocal last
        now = time.monotonic()
        ui.detail(f"timing: {phase} {now - last:.2f}s")
        last = now

    def evaluate(accept: str | None) -> _PublishEval:
        return _evaluate_publish(source_root, publish_dir, config,
                                 config_relpath=config_relpath, accept=accept,
                                 recreate=recreate_branch, expect_tip=expect_tip)

    def finish(ev: _PublishEval, accepted: str | None) -> int:
        refusal = _state_refusal(ev.integ)
        if refusal is not None:
            raise refusal
        if ev.branch_state.default_branch:
            ui.info(f"Note: the remote's default branch is '{ev.branch_state.default_branch}'; "
                    f"this publish creates '{branch}'")
        return _publish_snapshot(source_root, publish_dir, config, ev,
                                 custom_message=custom_message, config_relpath=config_relpath,
                                 config_file=config_file, accepted=accepted, mark=mark)

    if mode != "prompt":
        with source_lock(source_root, exclusive=True), publish_lock(publish_dir):
            _check_publish_branch(publish_dir, branch)
            ev = evaluate(accept_divergence)
            mark("remote checks")
            return finish(ev, _accepted_snapshot(source_root, accept_divergence))

    _check_publish_branch(publish_dir, branch)
    latest: dict = {}

    def compute() -> Binding:
        ev = evaluate(accept_divergence)       # --expect-tip: refused right after the fetch
        accepted, note = _accepted_snapshot(source_root, accept_divergence), ""
        if ev.integ.state in ("S10b", "S10b+S2") and accept_divergence is None:
            snapshot = ev.integ.divergence.snapshot
            note = _s10b_text(ev.integ.divergence).replace("ERROR: ", "", 1)
            ev, accepted = evaluate(snapshot), snapshot
        refusal = _state_refusal(ev.integ)
        if refusal is not None:
            raise refusal
        plan = build_snapshot_tree(publish_dir, ev.integ.tip, ev.proj, ev.integ, config)
        if not (plan.adds or plan.mods or plan.dels):
            latest.update(nothing_tip=ev.integ.tip)
            raise _NothingToPublish()
        latest.update(ev=ev, accepted=accepted, summary=_publish_summary(url, branch, plan, note))
        tip = ev.integ.tip
        return Binding(tips=((f"refs/heads/{branch}", tip),) if tip else (), digest=None,
                       managed_tree=plan.tree, accepted_snapshot=accepted,
                       changed_paths=tuple(sorted({*plan.written, *plan.decided})))

    try:
        with confirm_then_lock(source_root, publish_dir, compute,
                               lambda _binding: confirm_yes_no(latest["summary"])):
            mark("remote checks")
            return finish(latest["ev"], latest["accepted"])
    except _NothingToPublish:
        if accept_divergence is not None:       # the flag is the acceptance
            with source_lock(source_root, exclusive=True):
                _drop_accepted(source_root, publish_dir,
                               _accepted_snapshot(source_root, accept_divergence),
                               latest.get("nothing_tip"))
            records_push_pending(source_root, config)
        ui.success("Nothing to publish")
        return EXIT_SUCCESS


# --- core: --amend (design §7) ---

def _amend_refusal(publish_dir: Path, integ: IntegrationState) -> PubrepoError | None:
    """Check order after the missing-branch refusals: the §9 `--amend`
    column, then a merge, not ours, an unverified delta, tagged."""
    refusal = _state_refusal(integ)
    if refusal is not None:
        return refusal
    tip = integ.tip
    parents = git("rev-list", "--parents", "-n", "1", tip, cwd=str(publish_dir)).stdout.split()[1:]
    if not integ.is_ours(tip):
        return PublishError(f"ERROR: --amend: the public tip {tip[:12]} is not a pubrepo "
                            f"snapshot of this source")
    if len(parents) > 1:
        return PublishError(f"ERROR: --amend: the public tip {tip[:12]} is a merge commit; "
                            f"amending it would drop a parent (publish a new snapshot instead)")
    if integ.baseline_items_for(tip):
        return DecisionRequired(f"ERROR: --amend: the public tip {tip[:12]} has unverified "
                                f"changes; run `{_PROG} pull --baseline`")
    _guard_remote(publish_dir)
    tags = [line.split("\t")[1].removeprefix("refs/tags/").removesuffix("^{}")
            for line in _git_c("ls-remote", "--tags", "origin", cwd=publish_dir).stdout.splitlines()
            if line.split("\t")[0] == tip]
    if tags:
        return PublishError(f"ERROR: --amend: the public tip {tip[:12]} is tagged "
                            f"{', '.join(sorted(set(tags)))}")
    return None


def _rewrite_notice(old: str, branch: str) -> str:
    """Design §7 J3/J4 and the §6.6 collaborator recovery block."""
    return (f"--amend replaces public commit {old[:12]} on '{branch}'. Anyone who fetched it\n"
            f"keeps the old commit; their next pull sees a rewritten branch. Tell them to:\n"
            f"  1. back up: git branch pre-amend-backup origin/{branch}\n"
            f"  2. git fetch && git reset --keep origin/{branch}\n"
            f"  3. re-apply unpushed work with git rebase --onto; never re-push the old commit\n"
            f"  4. delete the backup afterwards. Forks and open PRs still hold the old commit;\n"
            f"     no host purge is promised. Rotate any secret it exposed.")


@dataclass
class _AmendPlan:
    ev: _PublishEval
    tip: str
    parents: list[str]
    tree: str
    written: dict[str, str]
    counts: tuple[int, int, int]
    nonderivable: list[str]
    keep: list[str]
    title: str
    decided: dict[str, dict]


def _reused_title(publish_dir: Path, tip: str) -> str:
    """The title --amend without -m keeps: `tip`'s title block (everything
    before the last `Source:` line), else its subject."""
    body = git("log", "-1", "--format=%B", tip, cwd=str(publish_dir)).stdout.strip()
    split = _split_tool_message(body)
    return split[0] if split else git("log", "-1", "--format=%s", tip,
                                      cwd=str(publish_dir)).stdout.strip()


def _plan_amend(source_root: Path, publish_dir: Path, config: dict, ev: _PublishEval,
                custom_message: str | None) -> _AmendPlan:
    """The amended snapshot: the projection over the tip's tree (tip == IP
    and its delta is verified ours, so this equals building on its parent),
    committed on the tip's parents."""
    integ, proj = ev.integ, ev.proj
    tip = integ.tip
    parents = git("rev-list", "--parents", "-n", "1", tip, cwd=str(publish_dir)).stdout.split()[1:]
    plan = build_snapshot_tree(publish_dir, tip, proj, integ, config)
    before = _tree_entries(publish_dir, parents[0] if parents else None)
    after = _tree_entries(publish_dir, plan.tree)
    delta = {p: after[p][2] for p in after if before.get(p) != after[p]}
    delta.update({p: "deleted" for p in before if p not in after})
    # A decision-authorized change of an unmanaged path is not a pubrepo write
    # (design §3.1), whether this build made it or the replaced
    # tip did; the amended record carries it under `decided`.
    ref = f"refs/pubrepo/snap/{tip}"
    replaced = read_records(source_root, ref).get(ref, {}).get("decided") or {}
    decided = {p: v for p, v in replaced.items()
               if p in delta and p not in plan.written and isinstance(v, dict)}
    decided.update(plan.decided)
    written = {p: v for p, v in delta.items() if p not in decided}
    # The message and log count what this amend changes relative to the tip it
    # replaces (as `commit --amend` did); the record's write set is the rest of
    # the delta against the parent (§3.1).
    counts = (len(plan.adds), len(plan.mods), len(plan.dels))
    title = custom_message if custom_message is not None else _reused_title(publish_dir, tip)
    nonderivable = sorted(p for p in written if p in proj.entries
                          and (proj.entries[p].nonderivable or p in proj.dirty_paths))
    return _AmendPlan(ev, tip, parents, plan.tree, written, counts, nonderivable, plan.keep,
                      title, decided)


def amend_snapshot(source_root: Path, publish_dir: Path, config: dict, *, yes: bool,
                   custom_message: str | None = None, config_relpath: str | None = None,
                   config_file: Path | None = None, mode: str = "yes",
                   expect_tip: str | None = None) -> int:
    """Design §7 `--amend`: rewrite our own last snapshot.

    Pre: an explicit --amend; `mode` is this run's confirmation_mode
    (§17.3). Runs the amend checks in order. In "prompt" mode the one
    [y/N] follows the commit, the consequences and the §6.6 block, through
    confirm_then_lock bound to the shown tip. Push: --force-with-lease=refs/heads/<b>:<shown
    tip>; lease rejected → 5; host refusal → 2. On success the old record
    stays, a status/<old>/rewritten marker and a snap/<new> record are
    written (AC64); local-only .publish commits are backed up (§4.10).
    """
    branch = config["branch"]
    source_hash, is_dirty = get_source_info(source_root)
    forbidden = config.get("scrub", {}).get("forbidden", [])
    last = time.monotonic()

    def mark(phase: str) -> None:
        nonlocal last
        now = time.monotonic()
        ui.detail(f"timing: {phase} {now - last:.2f}s")
        last = now

    def evaluate() -> _AmendPlan:
        ev = _evaluate_publish(source_root, publish_dir, config, config_relpath=config_relpath,
                               accept=None, recreate=False, amend=True, expect_tip=expect_tip)
        refusal = _amend_refusal(publish_dir, ev.integ)
        if refusal is not None:
            raise refusal
        plan = _plan_amend(source_root, publish_dir, config, ev, custom_message)
        if custom_message is None:
            # the reused title is refused before the prompt and
            # before anything is written (main has scanned -m already).
            scrub = message_scrub(plan.title, config)
            if scrub.count:
                raise message_scrub_refusal(
                    f"the title --amend would reuse from {plan.tip[:12]}", scrub,
                    REUSED_TITLE_REMEDY, after_fetch=True)
        return plan

    def binding(plan: _AmendPlan) -> Binding:
        return Binding(tips=((f"refs/heads/{branch}", plan.tip),), digest=None,
                       managed_tree=plan.tree, accepted_snapshot=None,
                       changed_paths=tuple(sorted({*plan.written, *plan.decided})))

    def finish(plan: _AmendPlan) -> int:
        proj = plan.ev.proj
        tip_tree = git("rev-parse", f"{plan.tip}^{{tree}}", cwd=str(publish_dir)).stdout.strip()
        tip_body = git("log", "-1", "--format=%B", plan.tip, cwd=str(publish_dir)).stdout.strip()
        tip_split = _split_tool_message(tip_body)
        if plan.tree == tip_tree and (custom_message is None
                                      or (tip_split and tip_split[0] == custom_message)):
            ui.success("Nothing to publish")
            return EXIT_SUCCESS
        ui.info(f"Amending {plan.tip[:12]} "
                f"({'title from -m' if custom_message else 'title reused'})")
        build_push_plan(source_root, publish_dir, config, kind="amend", refs={
            "public": [f"refs/heads/{branch}"], "bookmark": not is_dirty,
            "records": records_remote_name(source_root, config)})
        log_entry = {"ts": datetime.now().astimezone().isoformat(),
                     "source_commit": source_hash, "source_branch": _source_branch(source_root),
                     "source_dirty": is_dirty,
                     "files": sum(1 for e in proj.entries.values() if not e.keep),
                     "transforms_applied": sum(1 for e in proj.entries.values() if e.transformed),
                     "scrub_patterns": len(forbidden), "forced": False,
                     "title": custom_message or None, "amend": True,
                     "amended_from": plan.tip}
        gate_plan = SnapshotPlan(tree=plan.tree, adds=[], mods=[], dels=[], carried_count=0,
                                 written=plan.written, nonderivable=plan.nonderivable,
                                 keep=plan.keep, items=[])
        _gate_publish(publish_dir, plan.ev, gate_plan, config, log_entry)
        added, modified, deleted = plan.counts
        message = build_commit_message(
            source_hash, custom_message=plan.title, added=added, modified=modified,
            deleted=deleted, transform_count=log_entry["transforms_applied"],
            scrub_patterns=len(forbidden))
        author = git("log", "-1", "--format=%an%x00%ae", plan.tip,
                     cwd=str(publish_dir)).stdout.strip().split("\0")
        assert_parent_provenance(publish_dir, plan.parents, tip=plan.tip, fetched=set(),
                                 created=set())                   # §18.13
        new = _commit_tree(publish_dir, plan.tree, plan.parents, message,
                           author=(author[0], author[1]))
        ui.info("Committing (amend)... ok")
        mark("commit")
        outcome = push_porcelain(publish_dir, [f"{new}:refs/heads/{branch}"],
                                 leases={f"refs/heads/{branch}": plan.tip})
        _test_fault("publish.after_push")
        if outcome.status in ("lease_stale", "rejected_nonff"):
            raise Diverged([branch], f"ERROR: --amend: the public tip moved since it was shown "
                           f"({outcome.reason}); nothing was changed.\n"
                           f"run `{_PROG} pull`, then decide again.")
        if outcome.status == "host_refused":
            raise PublishError(f"ERROR: host refused the push: {outcome.reason}\n"
                               f"--amend needs force-push permission on '{branch}' (branch "
                               f"protection or hooks). Nothing was changed; publish a new "
                               f"snapshot instead (omit --amend).")
        if outcome.status == "ambiguous_absent":
            raise PublishError(f"ERROR: push failed: {sanitize_provider_output(outcome.reason)}\n"
                               f"{new[:12]} is not on the remote; nothing was changed locally.")
        if outcome.status == "ok":
            _confirm_pushed(publish_dir, new, branch)
        ui.info("Pushing (force-with-lease)... ok")
        mark("push")
        advance_local(publish_dir, branch, new, config.get("keep", []), replaced=(plan.tip,))
        destination = destination_identity(config)
        try:
            write_status_marker(source_root, plan.tip, "rewritten",
                                reason="replaced by --amend", new_sha=new)
            write_snapshot_record(source_root, snapshot_sha=new, destination=destination,
                                  sigma_full=_source_full_head(source_root) or source_hash,
                                  written=plan.written, nonderivable=plan.nonderivable,
                                  keep=plan.keep, config_path=config_relpath,
                                  fingerprint=config_fingerprint(config), decided=plan.decided)
            register_target(source_root, str(config_file or source_root / CONFIG_FILE_NAME),
                            destination)
        except (PubrepoError, OSError) as e:
            ui.warn(f"WARNING: amended to {new[:12]}, but its records could not be written "
                    f"({e}); the next run identifies it by its Source: trailer")
        records_push_pending(source_root, config)
        _append_publish_log(publish_dir, {**log_entry, "added": added, "modified": modified,
                                          "deleted": deleted,
                                          "scrub_result": "passed" if forbidden else "skipped",
                                          "pushed": True, "public_commit": new})
        if mode != "tty":
            ui.warn(_rewrite_notice(plan.tip, branch))
        if not is_dirty:
            _bookmark_publish(source_root, config)
        ui.success(f"Published {source_hash}{'  (dirty)' if is_dirty else ''} "
                   f"(amended {plan.tip[:12]})")
        return EXIT_SUCCESS

    if mode != "prompt":
        with source_lock(source_root, exclusive=True), publish_lock(publish_dir):
            _check_publish_branch(publish_dir, branch)
            plan = evaluate()
            mark("remote checks")
            return finish(plan)

    _check_publish_branch(publish_dir, branch)
    latest: dict[str, _AmendPlan] = {}

    def compute() -> Binding:
        latest["plan"] = evaluate()
        return binding(latest["plan"])

    def show_and_confirm(b: Binding) -> bool:
        """§17.3: the one [y/N], after the commit, the consequences and the
        §6.6 block (design §7)."""
        tip = latest["plan"].tip
        summary = _rewrite_notice(tip, branch)
        if b.changed_paths:
            summary += "\nPaths the amended snapshot writes:\n" + "\n".join(
                f"  {p}" for p in b.changed_paths)
        return confirm_yes_no(summary + f"\nRewrite public commit {tip[:12]}.")

    with confirm_then_lock(source_root, publish_dir, compute, show_and_confirm):
        return finish(latest["plan"])


def _accepted_snapshot(source_root: Path, accept: str | None) -> str | None:
    """The snapshot sha an --accept-source-divergence value names: the sha
    itself, or the snapshot whose record carries that full σ."""
    if not accept:
        return None
    for name, record in read_records(source_root, "refs/pubrepo/snap/").items():
        sha = name.rsplit("/", 1)[1]
        if accept in (sha, record.get("source")):
            return sha
    return accept


def _check_publish_branch(publish_dir: Path, branch: str) -> None:
    """`.publish/` must be on the configured branch (advance_local moves it)."""
    head_ref = git("symbolic-ref", "--short", "HEAD", cwd=str(publish_dir), check=False)
    current = head_ref.stdout.strip() if head_ref.returncode == 0 else None
    if current != branch:
        state = f"on branch '{current}'" if current else "in detached HEAD state"
        raise PublishError(
            f"ERROR: {publish_dir.name}/ is {state}, but the configured "
            f"branch is '{branch}'.\n"
            f"Fix: git -C {publish_dir.name} checkout {branch}")


def cmd_publish(source_root: Path, config: dict, custom_message: str | None = None,
                force_overwrite: bool = False, stage_only: bool = False,
                as_json: bool = False, amend_mode: str | None = None,
                release_choice: ReleaseChoice | None = None,
                release_discovery: Discovery | None = None, *,
                config_relpath: str | None = None, config_file: Path | None = None,
                accept_divergence: str | None = None, recreate_branch: bool = False,
                remote: bool = False, yes: bool = False, confirmation: str = "yes",
                expect_tip: str | None = None, config_label: str = CONFIG_FILE_NAME,
                retry: bool = False) -> int:
    """Full publish cycle (or stage-only preview when stage_only=True).

    The routine publish runs _publish_routine (design §4), --stage runs
    cmd_stage, --amend runs amend_snapshot and --release/
    --bump run _release_routine (design §8).
    """
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    branch = config["branch"]

    validate_includes(source_root, config, config_label=config_label)

    if not (publish_dir / ".git").exists():
        raise ConfigError(f"ERROR: {publish_dir.name}/ not initialized\n"
                          f"Run: {_PROG} init")

    _warn_if_publish_dir_tracked(source_root, publish_dir)

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    try:
        old_handler = signal.signal(signal.SIGTERM, _on_sigterm)
    except ValueError:  # not the main thread; best-effort
        old_handler = None
    try:
        if stage_only:
            return cmd_stage(source_root, publish_dir, config, as_json=as_json,
                             remote=remote, config_relpath=config_relpath)
        if release_choice is not None and release_choice.kind == "ask":
            return _choice_routine(
                source_root, publish_dir, config, discovery=release_discovery,
                custom_message=custom_message, config_relpath=config_relpath,
                config_file=config_file or source_root / CONFIG_FILE_NAME,
                expect_tip=expect_tip, retry=retry)
        releasing = release_choice is not None and release_choice.kind != "none"
        if not releasing and amend_mode:
            return amend_snapshot(source_root, publish_dir, config, yes=yes,
                                  custom_message=custom_message, config_relpath=config_relpath,
                                  config_file=config_file or source_root / CONFIG_FILE_NAME,
                                  mode=confirmation, expect_tip=expect_tip)
        if releasing:
            return _release_routine(
                source_root, publish_dir, config, choice=release_choice,
                discovery=release_discovery, custom_message=custom_message,
                config_relpath=config_relpath,
                config_file=config_file or source_root / CONFIG_FILE_NAME,
                expect_tip=expect_tip, retry=retry, mode=confirmation)
        return _publish_routine(
            source_root, publish_dir, config, custom_message=custom_message,
            config_relpath=config_relpath,
            config_file=config_file or source_root / CONFIG_FILE_NAME,
            accept_divergence=accept_divergence, recreate_branch=recreate_branch,
            mode=confirmation, expect_tip=expect_tip)
    except KeyboardInterrupt:
        # House git-safety rules treat reset --hard and clean -fd as
        # destructive. They are correct HERE, and only here: every byte
        # under .publish/ except .git/ is regenerable tool output, and keep
        # files are tracked in prior commits — restoring to HEAD is provably
        # lossless in this one directory.
        head = git("rev-parse", "--verify", "HEAD",
                   cwd=str(publish_dir), check=False)
        if head.returncode == 0:
            git("reset", "--hard", "HEAD", cwd=str(publish_dir), check=False)
        else:
            # `reset --hard HEAD` cannot restore an unborn branch. Clear the
            # index first so the rebuilt files become cleanable untracked data.
            git("read-tree", "--empty", cwd=str(publish_dir), check=False)
        git("clean", "-fd", cwd=str(publish_dir), check=False)
        raise Interrupted("publish interrupted; .publish/ restored to last "
                          "published state")
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)


# ===== sync: BEGIN (the sync section runs to "sync: END") =====
# Frozen interface (§2.3, sync → core). Blob and sha fields are full
# 40-hex; None means absent.

# --- sync: locks ---

@dataclass(frozen=True)
class Binding:
    """What a confirmation is bound to; equality is the post-lock check."""
    tips: tuple[tuple[str, str], ...]   # (ref, sha) shown; a mismatch → Diverged (5)
    digest: str | None                  # plan digest
    managed_tree: str | None            # managed_tree_identity (release)
    accepted_snapshot: str | None       # S10b
    changed_paths: tuple[str, ...]      # S10b; any non-tip mismatch → ConfigError (1)
    required: frozenset[str] = field(default=frozenset(), compare=False)


SOURCE_LOCK_NAME = "pubrepo-source.lock"
IN_PROGRESS_FORMAT = 1

# Source locks held by this process: (pid, resolved lock path). Same rule as
# _HELD_PUBLISH_LOCKS: re-acquiring while held is a defect.
_HELD_SOURCE_LOCKS: set[tuple[int, str]] = set()

# assert_no_pull_in_progress `command` values with special handling (§5.12).
_S4_EXEMPT_COMMANDS = frozenset({"status", "pull --plan"})
_S4_OWNER_ONLY_COMMANDS = frozenset({"pull --continue", "pull --abort"})


def source_common_dir(source_root: Path) -> Path:
    """`git rev-parse --git-common-dir` for source, as an absolute path.
    Shared by every worktree of the source repo (design §2 source-side state)."""
    out = git("rev-parse", "--git-common-dir", cwd=source_root).stdout.strip()
    path = Path(out)
    return (path if path.is_absolute() else Path(source_root) / path).resolve()


def _worktree_toplevel(source_root: Path) -> str:
    out = git("rev-parse", "--show-toplevel", cwd=source_root).stdout.strip()
    return str(Path(out).resolve())


@contextmanager
def source_lock(source_root: Path, *, exclusive: bool) -> Iterator[None]:
    """flock on <git-common-dir>/pubrepo-source.lock, LOCK_EX or LOCK_SH,
    non-blocking; contention raises LockContention (design §5.13).

    Mutators take it exclusive, read-only previews and `status` shared. An
    exclusive holder writes its PID into the file and clears it on release,
    so a contention message never names a stale holder. A second
    acquisition in the same process raises AssertionError; an
    acquisition after release is fine.
    """
    lock_path = source_common_dir(source_root) / SOURCE_LOCK_NAME
    held = (os.getpid(), str(lock_path))
    if held in _HELD_SOURCE_LOCKS:
        raise AssertionError(f"source_lock re-acquired in the same process: {lock_path}")
    _HELD_SOURCE_LOCKS.add(held)
    try:
        with _flock_source(lock_path, exclusive):
            yield
    finally:
        _HELD_SOURCE_LOCKS.discard(held)


@contextmanager
def _flock_source(lock_path: Path, exclusive: bool):
    if fcntl is None:  # Windows: no-op, as publish_lock (design §5.13)
        yield
        return
    f = open(lock_path, "a+")
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    try:
        fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        holder = f.read().strip()
        f.close()
        who = (f"another pubrepo command (PID {holder})" if holder
               else "a read-only pubrepo command (status or a preview)")
        raise LockContention(
            f"ERROR: {who} is using this source repository.\n"
            f"Wait for it to finish. If that process is gone, the lock is stale — "
            f"remove {lock_path}.")
    try:
        if exclusive:
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()))
            f.flush()
        yield
    finally:
        with suppress(OSError):
            if exclusive:
                f.seek(0)
                f.truncate()
                f.flush()
        with suppress(OSError):
            f.close()  # closing the fd releases the flock


@dataclass
class InProgress:
    """Source-side record of a pull that has written to a worktree (§5.12)."""
    worktree: str                       # `rev-parse --show-toplevel`, resolved
    branch: str
    git_dir: str
    config_path: str
    digest: str                         # plan digest
    written: dict[str, str | None]      # path → pre-pull blob (None = absent)
    # Keep writes into the publish clone: path → the pre-pull
    # bytes of <publish_dir>/<path>, stored as a blob there (None = absent).
    kept: dict[str, str | None] = field(default_factory=dict)
    publish_dir: str = ""


def _in_progress_path(source_root: Path) -> Path:
    return source_common_dir(source_root) / "pubrepo" / "pull-inprogress.json"


def pull_in_progress(source_root: Path) -> InProgress | None:
    """Read the in-progress record, or None when no pull is in progress.

    An unreadable record raises ConfigError naming the file: an in-progress
    pull is never silently ignored (INV-7).
    """
    path = _in_progress_path(source_root)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        if data.get("format") != IN_PROGRESS_FORMAT:
            raise ValueError(f"unknown format {data.get('format')!r}")
        return InProgress(
            worktree=data["worktree"], branch=data["branch"], git_dir=data["git_dir"],
            config_path=data["config_path"], digest=data["digest"],
            written=dict(data["written"]), kept=dict(data.get("kept", {})),
            publish_dir=data.get("publish_dir", ""))
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise ConfigError(
            f"ERROR: the in-progress pull record is unreadable ({e}): {path}\n"
            f"A pull may be half-applied. Inspect the worktrees before removing it.")


def write_in_progress(source_root: Path, rec: InProgress) -> None:
    """Write the record atomically (tempfile + fsync + os.replace) into
    source's common dir. `source_root` is explicit so the location never
    depends on `rec.worktree` still being a live repo."""
    path = _in_progress_path(source_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"format": IN_PROGRESS_FORMAT, "worktree": rec.worktree,
               "branch": rec.branch, "git_dir": rec.git_dir,
               "config_path": rec.config_path, "digest": rec.digest,
               "written": rec.written, "kept": rec.kept, "publish_dir": rec.publish_dir}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pull-inprogress.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def clear_in_progress(source_root: Path) -> None:
    with suppress(FileNotFoundError):
        _in_progress_path(source_root).unlink()


def _registered_worktrees(source_root: Path) -> set[str]:
    out = git("worktree", "list", "--porcelain", cwd=source_root).stdout
    return {str(Path(line[len("worktree "):]).resolve())
            for line in out.splitlines() if line.startswith("worktree ")}


def in_progress_orphaned(source_root: Path, rec: InProgress) -> bool:
    """The owning worktree is gone: absent from `git worktree list
    --porcelain` **and** absent on disk (design §5.12, AC42b)."""
    return (rec.worktree not in _registered_worktrees(source_root)
            and not Path(rec.worktree).exists())


def _owner_status(source_root: Path, rec: InProgress) -> str:
    if in_progress_orphaned(source_root, rec):
        return " (that worktree no longer exists)"
    if not Path(rec.worktree).exists():
        return " (missing on disk but still registered in `git worktree list`)"
    return ""


def assert_no_pull_in_progress(source_root: Path, *, command: str) -> None:
    """S4 in the owning worktree raises DecisionRequired (except `pull
    --plan` and `status`); other worktrees get a notice (design §5.12).

    `command` is the user-facing command name. "pull --continue" and
    "pull --abort" pass in the owning worktree and raise ConfigError (1)
    naming the owner anywhere else. Exception (AC42b): "pull --abort"
    passes from another worktree when the owner is orphaned
    (`in_progress_orphaned`); the caller then runs `abort_orphaned_pull`.
    """
    rec = pull_in_progress(source_root)
    if rec is None:
        if command in _S4_OWNER_ONLY_COMMANDS:
            raise ConfigError(f"ERROR: no pull is in progress; nothing to {command.split()[-1][2:]}.")
        return
    here = _worktree_toplevel(source_root)
    owner = rec.worktree
    if here == owner:
        if command in _S4_EXEMPT_COMMANDS or command in _S4_OWNER_ONLY_COMMANDS:
            return
        raise DecisionRequired(
            f"ERROR: a pull is in progress in this worktree (branch {rec.branch}).\n"
            f"Finish it with `{_PROG} pull --continue`, or undo it with `{_PROG} pull --abort`.")
    if command == "pull --abort" and in_progress_orphaned(source_root, rec):
        return
    if command in _S4_OWNER_ONLY_COMMANDS:
        raise ConfigError(
            f"ERROR: `{_PROG} {command}` must run in the worktree that started the pull: "
            f"{owner}{_owner_status(source_root, rec)}")
    ui.info(f"Note: a pull is in progress in another worktree: "
            f"{owner}{_owner_status(source_root, rec)}")


def abort_orphaned_pull(source_root: Path, *, yes: bool) -> int:
    """`pull --abort` for a record whose worktree is gone (design §5.12,
    AC42b). Restores nothing — the written files went with the worktree —
    and clears the record after a TTY confirmation or `--yes`.

    Pre: a record exists and `in_progress_orphaned` holds; anything else
    is refused with exit 1 and nothing changes.
    """
    rec = pull_in_progress(source_root)
    if rec is None:
        raise ConfigError("ERROR: no pull is in progress; nothing to abort.")
    if not in_progress_orphaned(source_root, rec):
        raise ConfigError(
            f"ERROR: `{_PROG} pull --abort` must run in the worktree that started the pull: "
            f"{rec.worktree}{_owner_status(source_root, rec)}")
    summary = (f"The pull in progress belongs to a worktree that no longer exists:\n"
               f"  worktree: {rec.worktree}\n"
               f"  branch:   {rec.branch}\n"
               f"  plan:     {rec.digest}\n"
               f"Nothing can be restored (its files went with the worktree). "
               f"Clearing the record ends the pull.")
    if not yes:
        if not is_interactive():
            raise ConfigError(summary + "\nERROR: pass --yes to clear it off a terminal.")
        print(summary + "\nClear the record? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            answer = input()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            raise ConfigError("Cancelled; nothing was written.")
    else:
        ui.info(summary)
    with source_lock(source_root, exclusive=True):   # confirm, then lock (§5.13)
        now = pull_in_progress(source_root)
        if now != rec or not in_progress_orphaned(source_root, rec):
            raise ConfigError("ERROR: the in-progress record changed since it was shown; "
                              "nothing was written. Run the command again.")
        clear_in_progress(source_root)
    ui.success(f"Cleared the in-progress pull of {rec.worktree} "
               f"(branch {rec.branch}, plan {rec.digest}).")
    return EXIT_SUCCESS


def _moved_tips(shown: dict[str, str], now: dict[str, str]) -> list[str]:
    """Refs in `shown` whose sha differs in `now` (absent counts as moved)."""
    return [f"{ref}: {shown[ref]} → {now.get(ref) or '(absent)'}"
            for ref in sorted(shown) if now.get(ref) != shown[ref]]


def _check_binding(before: Binding, after: Binding, *, what: str) -> None:
    """Tips first (a moved tip → Diverged, 5), then every other field (→ 1)."""
    before_tips, after_tips = dict(before.tips), dict(after.tips)
    moved = _moved_tips(before_tips, after_tips) + [
        f"{ref}: (absent) → {sha}" for ref, sha in sorted(after_tips.items())
        if ref not in before_tips]
    if moved:
        raise Diverged(moved, "ERROR: the public tip moved since "
                       f"{what}; nothing was written.\n  " + "\n  ".join(moved) +
                       "\nRun the command again to see the new state.")
    if before != after:
        changed = [name for name in ("digest", "managed_tree", "accepted_snapshot",
                                     "changed_paths")
                   if getattr(before, name) != getattr(after, name)]
        raise ConfigError(f"ERROR: the plan changed since {what} "
                          f"({', '.join(changed)}); nothing was written.\n"
                          f"Run the command again and review the new plan.")


def _check_expected(expected: Binding, now: Binding) -> None:
    """Compare only the fields `expected` sets; tips are per named ref."""
    moved = _moved_tips(dict(expected.tips), dict(now.tips))
    if moved:
        raise Diverged(moved, "ERROR: --expect-tip does not match the public tip; "
                       "nothing was written.\n  " + "\n  ".join(moved))
    changed = [name for name in ("digest", "managed_tree", "accepted_snapshot",
                                 "changed_paths")
               if getattr(expected, name) not in (None, ())
               and getattr(expected, name) != getattr(now, name)]
    if changed:
        flag = "--expect-plan" if changed == ["digest"] else "the expected values"
        raise ConfigError(f"ERROR: {flag} does not match the current plan "
                          f"({', '.join(changed)}); nothing was written.\n"
                          f"Run `--plan` again and pass the digest it prints.")


def confirm_then_lock(source_root: Path, publish_dir: Path | None,
                      compute: Callable[[], Binding],
                      show_and_confirm: Callable[[Binding], bool] | None, *,
                      expected: Binding | None = None) -> AbstractContextManager[Binding]:
    """The only lock path for sync mutators and prompting publish paths
    (design §5.13). Computes under the shared lock, releases it,
    confirms (no prompt when show_and_confirm is None), fires
    `confirm.before_lock`, takes source (exclusive) then destination,
    re-fetches and recomputes, and yields holding both. A tip mismatch →
    Diverged (5); any other mismatch, or a field named in
    `expected.required` being None → ConfigError (1). Nothing is written.

    `compute` owns the re-fetch: it must fetch the tip itself, because it
    knows the branch. `show_and_confirm` returning False raises ConfigError
    (1, "cancelled"); a caller that needs another code raises its own
    exception from inside the callback. Callers never take `publish_lock`
    themselves.
    """
    return _confirm_then_lock(source_root, publish_dir, compute, show_and_confirm, expected)


@contextmanager
def _confirm_then_lock(source_root, publish_dir, compute, show_and_confirm, expected):
    if expected is not None:
        missing = sorted(name for name in expected.required
                         if getattr(expected, name) in (None, ()))
        if missing:
            raise ConfigError(f"ERROR: missing required expectation(s): {', '.join(missing)}; "
                              f"nothing was computed or written.")
    with source_lock(source_root, exclusive=False):
        shown = compute()
    if show_and_confirm is not None and not show_and_confirm(shown):
        raise ConfigError("Cancelled; nothing was written.")
    _test_fault("confirm.before_lock")
    what = "it was shown" if show_and_confirm else "it was computed"
    with source_lock(source_root, exclusive=True):
        if publish_dir is None:
            yield _recompute_and_check(compute, shown, expected, what)
        else:
            with publish_lock(publish_dir):
                yield _recompute_and_check(compute, shown, expected, what)


def _recompute_and_check(compute, shown: Binding, expected: Binding | None,
                         what: str) -> Binding:
    now = compute()
    _check_binding(shown, now, what=what)
    if expected is not None:
        _check_expected(expected, now)
    return now


# --- sync: records ---

@dataclass(frozen=True)
class SyncReport:
    remote: str | None         # records remote name used; None when sync was skipped
    skipped: str | None        # why sync was skipped, else None
    ok: bool                   # False on total failure
    created: tuple[str, ...]   # refs created locally (fetch) or remotely (push)
    rejected: tuple[tuple[str, str], ...]   # (ref, reason): warn, keep local
    unsynced: int | None       # local refs the remote lacks; None if unknown


RECORDS_FORMAT = 1
RECORDS_TIMEOUT = 20   # seconds per network git call (design §4.3)
RECORD_NAMESPACES = ("snap", "status", "repairs", "retired")   # allowlist
# Fixed identity and epoch-0 dates: an identical payload gives an identical
# record sha on every machine and on reruns.
_RECORD_ENV = {
    "GIT_AUTHOR_NAME": "pubrepo", "GIT_COMMITTER_NAME": "pubrepo",
    "GIT_AUTHOR_EMAIL": "records@pubrepo.invalid",
    "GIT_COMMITTER_EMAIL": "records@pubrepo.invalid",
    "GIT_AUTHOR_DATE": "@0 +0000", "GIT_COMMITTER_DATE": "@0 +0000",
}


def _canonical_json(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def write_record(source_root: Path, ref: str, payload: dict) -> str:
    """Create the append-only record `ref` (design §2) and return its sha.

    Pre: `ref` is under refs/pubrepo/<allowlisted namespace>/.
    Post: a parentless commit whose tree holds `record.json` (canonical
    JSON, `format` 1) is created at `ref` with a create-only update-ref.
    Rewriting an identical payload is a no-op returning the same sha; a ref
    that already holds a different record raises RecordConflict.
    """
    namespace = ref.split("/")[2] if ref.startswith("refs/pubrepo/") else None
    if namespace not in RECORD_NAMESPACES or ref.count("/") < 3:
        raise AssertionError(f"record ref outside refs/pubrepo/{{{','.join(RECORD_NAMESPACES)}}}: {ref}")
    body = _canonical_json({**payload, "format": RECORDS_FORMAT})
    env = {**os.environ, **_RECORD_ENV}
    blob = _run_git_process(["git", "hash-object", "-w", "--no-filters", "--stdin"], cwd=source_root,
                          input=body.encode("utf-8"), capture_output=True, check=True,
                          env=env).stdout.decode().strip()
    tree = _run_git_process(["git", "mktree"], cwd=source_root,
                          input=f"100644 blob {blob}\trecord.json\n", capture_output=True,
                          text=True, check=True, env=env).stdout.strip()
    commit = git("commit-tree", tree, "-m", f"pubrepo record {ref}", cwd=source_root,
                 env=env).stdout.strip()
    created = git("update-ref", ref, commit, "", cwd=source_root, check=False)
    if created.returncode != 0:
        existing = git("rev-parse", "--verify", "-q", ref, cwd=source_root,
                       check=False).stdout.strip()
        if existing == commit:
            return commit
        if existing:
            raise RecordConflict(f"record {ref} already exists with a different value "
                                 f"({existing}); keeping it")
        raise GitError(["update-ref", ref, commit, ""], created.stderr)
    return commit


def _local_record_refs(source_root: Path, prefix: str = "refs/pubrepo/") -> dict[str, str]:
    out = git("for-each-ref", "--format=%(refname) %(objectname)", prefix,
              cwd=source_root).stdout
    refs = {}
    for line in out.splitlines():
        name, sha = line.split(" ", 1)
        if name.split("/")[2:3] and name.split("/")[2] in RECORD_NAMESPACES:
            refs[name] = sha
    return refs


def read_records(source_root: Path, prefix: str) -> dict[str, dict]:
    """{ref: payload} for every record under `prefix` (a refs/pubrepo/...
    prefix). An unknown format or unparsable JSON warns and is skipped
    (design §4.3)."""
    refs = _local_record_refs(source_root, prefix)
    if not refs:
        return {}
    names = sorted(refs)
    batch = _run_git_process(["git", "cat-file", "--batch"], cwd=source_root,
                           input="".join(f"{refs[n]}:record.json\n" for n in names).encode(),
                           capture_output=True, check=True).stdout
    records, pos = {}, 0
    for name in names:
        header_end = batch.index(b"\n", pos)
        header = batch[pos:header_end].decode()
        pos = header_end + 1
        if header.endswith(" missing"):
            ui.warn(f"WARNING: record {name} has no record.json; ignored")
            continue
        size = int(header.rsplit(" ", 1)[1])
        data = batch[pos:pos + size]
        pos += size + 1
        try:
            payload = json.loads(data.decode("utf-8"))
            if not isinstance(payload, dict) or payload.get("format") != RECORDS_FORMAT:
                raise ValueError(f"unknown format {payload.get('format') if isinstance(payload, dict) else None!r}")
        except (ValueError, UnicodeDecodeError) as e:
            ui.warn(f"WARNING: record {name} is unreadable ({e}); ignored")
            continue
        records[name] = payload
    return records


def destination_identity(config: dict) -> tuple[str, str]:
    """(public remote identity, branch). The identity is "host/owner/name"
    when the remote normalizes, else the literal URL (design §2)."""
    remote = config["remote"]
    try:
        identity = "/".join(normalize_git_remote(remote))
    except ValueError:
        identity = remote
    return identity, config.get("branch", "main")


def _destination_json(destination: tuple[str, str]) -> dict:
    return {"remote": destination[0], "branch": destination[1]}


def write_snapshot_record(source_root: Path, *, snapshot_sha: str,
                          destination: tuple[str, str], sigma_full: str,
                          written: dict[str, str], nonderivable: list[str],
                          keep: list[str], config_path: str | None, fingerprint: str,
                          decided: dict[str, dict] | None = None) -> str:
    """refs/pubrepo/snap/<snapshot sha> (design §4.3). `written` maps path
    → blob or "deleted": projection writes only (design §3.1). config_path
    (relative posix path; None → JSON null for a config outside the source
    tree) and fingerprint are informational; the record is always written.
    `decided` holds decision-authorized
    changes of unmanaged paths, {path: {"decision": "restore"|"remove",
    "blob": sha|None, "record": pull-record sha}}; it is informational,
    written only when non-empty, and never read as W or LW."""
    payload = {
        "destination": _destination_json(destination), "source": sigma_full,
        "written": dict(written), "nonderivable": sorted(nonderivable),
        "keep": sorted(keep), "config_path": config_path, "fingerprint": fingerprint,
    }
    if decided:
        payload["decided"] = {p: dict(v) for p, v in decided.items()}
    return write_record(source_root, f"refs/pubrepo/snap/{snapshot_sha}", payload)


def write_status_marker(source_root: Path, snapshot_sha: str, kind: str, *,
                        reason: str, new_sha: str | None = None) -> str:
    """refs/pubrepo/status/<snapshot sha>/<kind>, kind ∈ {rewritten,
    dropped}. `reason` must be deterministic text (no timestamps), so the
    same marker written on two machines has one sha."""
    if kind not in ("rewritten", "dropped"):
        raise AssertionError(f"unknown status kind: {kind}")
    if (kind == "rewritten") != (new_sha is not None):
        raise AssertionError("new_sha is required for, and only for, 'rewritten'")
    return write_record(source_root, f"refs/pubrepo/status/{snapshot_sha}/{kind}", {
        "snapshot": snapshot_sha, "kind": kind, "reason": reason, "new_sha": new_sha})


def active_snapshot_records(source_root: Path,
                            destination: tuple[str, str]) -> dict[str, dict]:
    """{snapshot sha: record} for snap records of `destination` with no
    status/<sha>/* marker (design §2 "active"; §3.1 step 4)."""
    snaps = read_records(source_root, "refs/pubrepo/snap/")
    marked = {name.split("/")[3] for name in _local_record_refs(source_root,
                                                                 "refs/pubrepo/status/")}
    want = _destination_json(destination)
    return {name.rsplit("/", 1)[1]: rec for name, rec in snaps.items()
            if rec.get("destination") == want and name.rsplit("/", 1)[1] not in marked}


def records_has_destination(source_root: Path, destination: tuple[str, str]) -> bool:
    """Evidence for §4.9: any snapshot record (active or not) for destination."""
    want = _destination_json(destination)
    return any(rec.get("destination") == want
               for rec in read_records(source_root, "refs/pubrepo/snap/").values())


def write_retired(source_root: Path, tag: str, reason: str) -> str:
    return write_record(source_root, f"refs/pubrepo/retired/{tag}",
                        {"tag": tag, "reason": reason})


def is_retired(source_root: Path, tag: str) -> bool:
    return f"refs/pubrepo/retired/{tag}" in _local_record_refs(source_root,
                                                               "refs/pubrepo/retired/")


def _targets_path(source_root: Path) -> Path:
    return source_common_dir(source_root) / "pubrepo" / "targets.json"


def registered_targets(source_root: Path) -> list[dict]:
    """[{config_path, destination: {remote, branch}}] from targets.json."""
    try:
        data = json.loads(_targets_path(source_root).read_text(encoding="utf-8"))
        return [t for t in data.get("targets", []) if isinstance(t, dict)]
    except FileNotFoundError:
        return []
    except (ValueError, AttributeError) as e:
        ui.warn(f"WARNING: {_targets_path(source_root)} is unreadable ({e}); ignored")
        return []


def register_target(source_root: Path, config_path: str,
                    destination: tuple[str, str]) -> None:
    """Record that `config_path` (absolute) publishes to `destination`
    (design §5.2 item 2). Atomic; one entry per config path."""
    path = _targets_path(source_root)
    entry = {"config_path": str(Path(config_path).resolve()),
             "destination": _destination_json(destination)}
    targets = [t for t in registered_targets(source_root)
               if t.get("config_path") != entry["config_path"]] + [entry]
    targets.sort(key=lambda t: t["config_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".targets.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"format": RECORDS_FORMAT, "targets": targets}, f, sort_keys=True,
                      indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def known_publish_remotes(source_root: Path, config: dict) -> list[str]:
    """Every configured publish remote, as written: this config, root-level
    .publish*.toml, registered targets (targets.json) and peers (§18.2
    rules 1-2; §2.3, used by core's private-target check)."""
    remotes = [config["remote"]] if config.get("remote") else []
    paths = sorted(Path(source_root).glob(".publish*.toml"))
    paths += [Path(t["config_path"]) for t in registered_targets(source_root)
              if t.get("config_path")]
    paths += [Path(source_root) / p for p in config.get("peers", [])]
    for p in paths:
        try:
            remote = tomllib.loads(p.read_text(encoding="utf-8")).get("publish", {}).get("remote")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(remote, str) and remote:
            remotes.append(remote)
    return remotes


_NO_RECORDS_REMOTE = ("WARNING: no records remote; records not synced; identity by "
                      "verification only")
# One line per (source root, text) per process: a run resolves the records
# remote for its fetch and again for its push (§4.3, §18.11).
_RECORDS_REMOTE_WARNED: set[tuple[str, str]] = set()


def _warn_once(source_root: Path, text: str) -> None:
    key = (str(Path(source_root).resolve()), text)
    if key not in _RECORDS_REMOTE_WARNED:
        _RECORDS_REMOTE_WARNED.add(key)
        ui.warn(text)


def _notices_path(source_root: Path) -> Path:
    return source_common_dir(source_root) / "pubrepo" / "notices.json"


def _first_notice(source_root: Path, key: str) -> bool:
    """True the first time `key` is asked for in this clone, which marks it
    shown (§18.11, one-time notice). Atomic; an unreadable file counts
    as empty, so the notice can repeat but never blocks a run."""
    path = _notices_path(source_root)
    try:
        shown = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        shown = {}
    if not isinstance(shown, dict):
        shown = {}
    if shown.get(key):
        return False
    shown[key] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".notices.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(shown, f, sort_keys=True, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return True


def _push_resolution_remote(source_root: Path) -> str:
    """R6: branch.<b>.pushRemote, then remote.pushDefault, then the upstream
    remote branch.<b>.remote, for the current branch <b> (a detached HEAD
    skips the branch keys). "" when none is set; there is no `origin`
    fallback. An upstream of "." (a local branch) is not a remote."""
    branch = git("symbolic-ref", "-q", "--short", "HEAD", cwd=source_root,
                 check=False).stdout.strip()

    def value(key: str) -> str:
        return git("config", "--get", key, cwd=source_root, check=False).stdout.strip()

    for name in ((value(f"branch.{branch}.pushRemote") if branch else ""),
                 value("remote.pushDefault"),
                 (value(f"branch.{branch}.remote") if branch else "")):
        if name and name != ".":
            return name
    return ""


def _config_shown(source_root: Path, config: dict) -> str:
    """The config file to name in a message: the registered target with
    this config's destination (targets.json), else the default name."""
    dest = _destination_json((config.get("remote", ""), config.get("branch", "main")))
    for t in registered_targets(source_root):
        if t.get("destination") == dest and t.get("config_path"):
            path = Path(t["config_path"])
            with suppress(ValueError):
                return str(path.relative_to(Path(source_root).resolve()))
            return str(path)
    return CONFIG_FILE_NAME


def records_remote_candidate(source_root: Path, config: dict) -> str:
    """The records remote name before the private-target check: [publish]
    records_remote, else the R6 push resolution; "" when there is none.
    Silent, for checks that report a refusal themselves."""
    return config.get("records_remote") or _push_resolution_remote(source_root)


def records_default_findings(source_root: Path, config: dict) -> list[dict]:
    """`validate --remote` only: when no records_remote is
    configured, the R6 default records remote, if the private-target check
    refuses it, as one warning with the check's reason. Plain validate
    checks only configured remotes and makes no git call (AC63)."""
    if config.get("records_remote"):
        return []                                   # private_ref_findings reports it
    name = records_remote_candidate(source_root, config)
    if not name:
        return []
    why = private_target_refusal(source_root, config, name)
    if why is None:
        return []
    return [_warning("records_remote",
                     f"records would sync to '{name}' (the push remote; no records_remote "
                     f"is set), which is refused, so records are not synced: {why}")]


def _resolve_records_remote(source_root: Path, config: dict, *, announce: bool) -> str | None:
    name = records_remote_candidate(source_root, config)
    if not name:
        if announce:
            _warn_once(source_root, _NO_RECORDS_REMOTE)
        return None
    reason = private_target_refusal(source_root, config, name)
    if reason is None:
        return name
    if not announce:
        return None
    line = private_remotes_line(source_root, config, name)
    if line is not None:
        digest = hashlib.sha256(line.encode("utf-8")).hexdigest()
        if _first_notice(source_root, f"private_remotes_line:{name}:{digest}"):
            ui.warn("NOTICE: records are not synced until the records remote is listed; "
                    f"add this line to [publish] in {_config_shown(source_root, config)}: "
                    f"{line}")
    _warn_once(source_root, f"WARNING: refusing to push private refs refs/pubrepo/* to "
                            f"{name}: {reason}; records not synced")
    return None


def records_remote_name(source_root: Path, config: dict) -> str | None:
    """The records remote, as a source remote NAME (R6, §18.11).

    Post: `[publish] records_remote` (a name, validated by core), else git's
    push resolution (pushRemote → pushDefault → upstream; no `origin`
    fallback). None, with one warning per run, when there is none or
    when private_target_refusal refuses it (unlisted, a publish repo, or a
    transport-altering key). The first refusal of an
    unlisted remote in this clone also prints the one-time notice with the
    exact `private_remotes` line. Every records fetch and push goes to
    this name, so git uses exactly the values that were checked (R1)."""
    return _resolve_records_remote(source_root, config, announce=True)


def _git_net(args: list[str], cwd: Path, *, url: str) -> subprocess.CompletedProcess | None:
    """A network git call with a timeout, no prompts and C-locale output.
    None on timeout.

    `url` is what the command contacts: a remote name of `cwd`'s repo, or a
    URL or path. Under the test sandbox it is checked immediately before git
    runs (design §17.4 (c)) and refuses with exit 2;
    outside the sandbox the check is a no-op."""
    names = git("remote", cwd=str(cwd), check=False).stdout.split()
    if url in names:
        _guard_remote(Path(cwd), url)
    elif "://" in url or Path(url).is_absolute() or re.match(r"^[^/]+:", url):
        check_test_sandbox_url(url)
    else:
        check_test_sandbox_url(str((Path(cwd) / url).resolve()))
    env = {**os.environ, "LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0"}
    try:
        return _run_git_process(["git", *args], cwd=cwd, capture_output=True, text=True,
                              env=_clone_env(cwd, env), timeout=RECORDS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None


def _remote_record_refs(source_root: Path, url: str) -> tuple[dict[str, str] | None, str]:
    """(allowlisted refs/pubrepo/* refs on `url`, failure reason). The refs
    are None when the remote is unreachable; the reason is git's stderr."""
    r = _git_net(["ls-remote", url, "refs/pubrepo/*"], source_root, url=url)
    if r is None:
        return None, "timed out"
    if r.returncode != 0:
        return None, r.stderr
    refs = {}
    for line in r.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name.startswith("refs/pubrepo/") and name.split("/")[2] in RECORD_NAMESPACES:
            refs[name] = sha
    return refs, ""


_NOT_SYNCED = "records not synced; identity by verification only"
# (source root, records remote) pairs already warned in this process: a run's
# fetch and its later push report one line between them (§4.3).
_NOT_SYNCED_WARNED: set[tuple[str, str]] = set()


def _warn_not_synced(source_root: Path, url: str, reason: str) -> None:
    """Design §4.3: a best-effort records sync failure prints only "records
    not synced; identity by verification only" (once per source and remote
    in a run); git's own text is shown, sanitized, only under --verbose."""
    key = (str(Path(source_root).resolve()), url)
    if key not in _NOT_SYNCED_WARNED:
        _NOT_SYNCED_WARNED.add(key)
        ui.warn(f"WARNING: {_NOT_SYNCED}")
    if reason.strip():
        ui.detail(f"  records remote: {sanitize_provider_output(reason.strip())}")


def records_fetch(source_root: Path, config: dict) -> SyncReport:
    """Fetch new records from the records remote (design §4.3; build note 1).

    Post: every allowlisted remote record missing locally is created; no
    `+`, so an existing local ref with a different value is rejected and
    kept (warned as corruption) while the other refs still apply. Total
    failure warns "records not synced; identity by verification only".
    """
    url = records_remote_name(source_root, config)
    if url is None:
        return SyncReport(None, "no usable records remote", False, (), (), None)
    remote, reason = _remote_record_refs(source_root, url)
    if remote is None:
        _warn_not_synced(source_root, url, reason)
        return SyncReport(url, None, False, (), (), None)
    before = _local_record_refs(source_root)
    r = None
    if any(name not in before for name in remote):
        r = _git_net(["fetch", "--no-tags", "--no-write-fetch-head", url,
                      *(f"refs/pubrepo/{ns}/*:refs/pubrepo/{ns}/*" for ns in RECORD_NAMESPACES)],
                     source_root, url=url)
        if r is None:
            _warn_not_synced(source_root, url, "fetch timed out")
            return SyncReport(url, None, False, (), (), None)
    after = _local_record_refs(source_root)
    created = tuple(sorted(n for n in after if n not in before))
    rejected = tuple(sorted((n, f"local {after[n]} differs from remote {sha}")
                            for n, sha in remote.items() if n in after and after[n] != sha))
    missing_here = [n for n in remote if n not in after]
    for name, why in rejected:
        ui.warn(f"WARNING: record {name} differs on {url} ({why}); records are immutable, "
                f"so one side is corrupt. Keeping the local value.")
    if missing_here:
        _warn_not_synced(source_root, url, f"{len(missing_here)} record(s) could not be "
                         f"fetched: {r.stderr if r is not None else ''}")
    unsynced = sum(1 for n in after if n not in remote)
    return SyncReport(url, None, not missing_here, created, rejected, unsynced)


def records_push_pending(source_root: Path, config: dict) -> SyncReport:
    """Push every local allowlisted record the remote lacks (build note 2):
    explicit `ref:ref` refspecs, no `+`, no `--prune`. Best-effort."""
    url = records_remote_name(source_root, config)
    if url is None:
        return SyncReport(None, "no usable records remote", False, (), (), None)
    remote, reason = _remote_record_refs(source_root, url)
    if remote is None:
        _warn_not_synced(source_root, url, reason)      # they stay local; the next sync retries
        return SyncReport(url, None, False, (), (), None)
    local = _local_record_refs(source_root)
    pending = sorted(n for n in local if n not in remote)
    rejected = tuple(sorted((n, f"remote {remote[n]} differs from local {local[n]}")
                            for n in local if n in remote and remote[n] != local[n]))
    created: list[str] = []
    if pending:
        r = _git_net(["push", "--porcelain", "--recurse-submodules=no", url,
                      *(f"{n}:{n}" for n in pending)],
                     source_root, url=url)
        if r is None:
            _warn_not_synced(source_root, url, "push timed out")
            return SyncReport(url, None, False, (), rejected, len(pending))
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and ":" in parts[1]:
                ref = parts[1].split(":", 1)[1]
                if parts[0] in ("*", " ", "+", "="):
                    created.append(ref)
                elif parts[0] == "!":
                    rejected += ((ref, parts[2]),)
    for name, why in rejected:
        ui.warn(f"WARNING: record {name} was not pushed to {url} ({why}); keeping local")
    unsynced = len([n for n in pending if n not in created])
    return SyncReport(url, None, unsynced == 0 and not rejected, tuple(sorted(created)),
                      rejected, unsynced)


def unsynced_count(source_root: Path, config: dict) -> int | None:
    """Local allowlisted records the records remote lacks (for `status`);
    None when it cannot be determined."""
    url = _resolve_records_remote(source_root, config, announce=False)   # status is silent
    if url is None:
        return None
    remote, _reason = _remote_record_refs(source_root, url)
    if remote is None:
        return None
    return sum(1 for n in _local_record_refs(source_root) if n not in remote)


@contextmanager
def _quiet_ui():
    saved = ui.quiet
    ui.quiet = True
    try:
        yield
    finally:
        ui.quiet = saved


# --- sync: identity ---

IdentityKind = Literal["ours_record", "ours_trailer", "ours_unresolved", "foreign"]


@dataclass(frozen=True)
class Identity:
    sha: str
    kind: IdentityKind
    sigma: str | None          # full σ (resolved, or from the record when unresolved)
    trailer: str | None        # raw `Source:` value as written (abbrev), if any
    record_ref: str | None     # refs/pubrepo/snap/<sha> that matched, if any
    via_repair: str | None     # old snapshot sha matched through an applied repair map


ENGINE_VERSION = "1"            # bump when W verification semantics change (cache key)
EMPTY_BLOB = "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
S12_MIN_BLOB_SIZE = 64          # design §5.2 item 7, §6.9
# Design §2 (`keep` acts like `ours`).
AUTHORIZING_DECISIONS = frozenset({"accept", "theirs", "resolved", "decline", "ours", "keep",
                                   "delete", "remove", "restore"})
_SOURCE_TRAILER_RE = re.compile(r"^Source: ([0-9a-f]{7,40})(?: \(dirty\))?[ \t]*$", re.M)
_PULLED_THROUGH_RE = re.compile(r"^Pubrepo-Pulled-Through: ([0-9a-f]{40})[ \t]*$", re.M)
_PULLED_RANGE_RE = re.compile(r"^Pubrepo-Pulled-Range: (\S+)[ \t]*$", re.M)
_ACCEPTED_DIV_RE = re.compile(r"^Pubrepo-Accepted-Divergence: ([0-9a-f]{40})[ \t]*$", re.M)
_DESTINATION_RE = re.compile(r"^Pubrepo-Destination: (\S+) (\S+)[ \t]*$", re.M)
_FENCED_JSON_RE = re.compile(r"^```json[ \t]*\n(.*?)\n```[ \t]*$", re.M | re.S)


@dataclass(frozen=True)
class PullRecord:
    """A pull commit in source history (design §5.7)."""
    sha: str
    through: str
    range: str | None
    accepted_divergence: str | None
    destination: tuple[str, str] | None     # None = applies to every destination
    entries: tuple[dict, ...]               # blob fields normalized: "absent" → None


def _decision_from_entry(entry: dict) -> Decision:
    return Decision(path=entry["path"], decision=entry["decision"],
                    cls=entry.get("class", ""), flags=tuple(entry.get("flags", ())),
                    theirs=entry["theirs"], result=entry.get("result"),
                    record=entry["_record"])


def _absent(value):
    return None if value in (None, "absent") else value


def _source_head(source_root: Path) -> str | None:
    head = git("rev-parse", "--verify", "-q", "HEAD^{commit}", cwd=source_root, check=False)
    return head.stdout.strip() or None


def pull_records(source_root: Path, *, destination: tuple[str, str],
                 first_parent: bool = True) -> list[PullRecord]:
    """Pull records in source HEAD's history for `destination`, newest
    first (design §5.7). The ONLY reader of pull records:
    a record whose `Pubrepo-Destination` names another destination is
    skipped; a record without the trailer applies to every destination.

    With first_parent, only HEAD's first-parent chain (decision precedence,
    accounted-for (ii)); otherwise the whole history (INV-4 (c)). A record
    whose JSON body does not parse warns and keeps its trailers only.
    """
    if _source_head(source_root) is None:
        return []
    argv = ["log", "-z", "--topo-order", "--format=%H%n%B",
            "--grep=^Pubrepo-Pulled-Through: "]
    if first_parent:
        argv.append("--first-parent")
    out = git(*argv, "HEAD", cwd=source_root).stdout
    records = []
    for chunk in out.split("\0"):
        if not chunk.strip():
            continue
        sha, _, body = chunk.partition("\n")
        through = _PULLED_THROUGH_RE.findall(body)
        if not through:
            continue
        dest = _DESTINATION_RE.findall(body)
        rng = _PULLED_RANGE_RE.findall(body)
        acc = _ACCEPTED_DIV_RE.findall(body)
        entries: list[dict] = []
        fenced = _FENCED_JSON_RE.findall(body)
        if fenced:
            try:
                parsed = json.loads(fenced[-1])
                if not isinstance(parsed, list):
                    raise ValueError("not a JSON array")
                for raw in parsed:
                    if not isinstance(raw, dict) or "path" not in raw or "decision" not in raw:
                        raise ValueError("entry without path/decision")
                    entry = dict(raw)
                    for key in ("base", "ours", "theirs", "result"):
                        entry[key] = _absent(entry.get(key))
                    entry["_record"] = sha
                    entries.append(entry)
            except ValueError as e:
                ui.warn(f"WARNING: pull record {sha[:12]} has an unreadable decision body ({e}); "
                        f"its decisions are ignored")
                entries = []
        record_dest = tuple(dest[-1]) if dest else None
        if record_dest is not None and record_dest != tuple(destination):
            continue
        records.append(PullRecord(sha=sha, through=through[-1], range=(rng or [None])[-1],
                                  accepted_divergence=(acc or [None])[-1],
                                  destination=tuple(dest[-1]) if dest else None,
                                  entries=tuple(entries)))
    return records


def resolve_sigma(source_root: Path, abbrev: str) -> list[str]:
    """Full source commits an abbreviated `Source:` value can name:
    one when unique, every candidate when ambiguous, none when absent."""
    one = git("rev-parse", "--verify", "-q", f"{abbrev}^{{commit}}", cwd=source_root,
              check=False)
    if one.returncode == 0:
        return [one.stdout.strip()]
    probe = git("cat-file", "-e", f"{abbrev}^{{commit}}", cwd=source_root, check=False)
    if "ambiguous" not in probe.stderr:
        return []
    out = git("rev-list", "--all", "--reflog", cwd=source_root, check=False).stdout
    return sorted({sha for sha in out.split() if sha.startswith(abbrev)})


def _is_ancestor(repo: Path, a: str, b: str) -> bool:
    return git("merge-base", "--is-ancestor", a, b, cwd=repo, check=False).returncode == 0


def _applied_repairs(source_root: Path) -> list[dict]:
    """Applied repair records (design §6.4): those whose `applied` ref
    exists. Payload keys used here: `map` (old → new), `removed`
    ([[path, blob], …]) and `removed_blobs`."""
    out = []
    for name, rec in read_records(source_root, "refs/pubrepo/repairs/").items():
        if name.endswith("/applied"):
            out.append({**rec, "_digest": name.split("/")[3]})
    return out


def _tree_map(repo: Path, rev: str | None) -> dict[str, tuple[str, str]]:
    """{path: (mode, blob)} for every blob entry (regular, exec, symlink)
    of `rev`; gitlinks are listed with their commit id."""
    if rev is None:
        return {}
    out = git("ls-tree", "-r", "-z", "--full-tree", rev, cwd=repo).stdout
    entries = {}
    for record in out.split("\0"):
        if record:
            meta, path = record.split("\t", 1)
            mode, _kind, obj = meta.split()
            entries[path] = (mode, obj)
    return entries


def _regular(entry: tuple[str, str] | None) -> str | None:
    return entry[1] if entry and entry[0] in ("100644", "100755") else None


def _delta(repo: Path, sha: str, first_parent: str | None) -> dict[str, tuple[str | None, str | None, str]]:
    """Delta(S) against its first parent: {path: (old blob, new blob, status)}
    with None for absent; renames are split (`--no-renames`)."""
    argv = (["diff-tree", "-r", "-z", "--no-renames", first_parent, sha] if first_parent
            else ["diff-tree", "-r", "-z", "--no-renames", "--root", sha])
    fields = git(*argv, cwd=repo).stdout.split("\0")
    if not first_parent and fields and fields[0] == sha:
        fields = fields[1:]
    delta = {}
    i = 0
    while i + 1 < len(fields):
        meta, path = fields[i], fields[i + 1]
        i += 2
        if not meta.startswith(":"):
            continue
        _om, nm, old, new, status = meta[1:].split()
        old = None if set(old) == {"0"} else old
        new = None if set(new) == {"0"} else new
        if nm == "160000":
            new = new and f"gitlink:{new}"
        delta[path] = (old, new, status)
    return delta


@dataclass
class _Commit:
    sha: str
    parents: tuple[str, ...]
    author: str
    subject: str
    trailer: str | None          # last `Source:` value, if any


def _tip_history(publish_dir: Path, tip: str) -> list[_Commit]:
    """Every commit reachable from tip, children before parents."""
    out = git("log", "-z", "--topo-order", "--format=%H %P%n%an <%ae>%n%s%n%B", tip,
              cwd=publish_dir).stdout
    commits = []
    for chunk in out.split("\0"):
        if not chunk.strip():
            continue
        head, author, subject, body = (chunk.split("\n", 3) + ["", "", ""])[:4]
        ids = head.split()
        found = _SOURCE_TRAILER_RE.findall(body)
        commits.append(_Commit(sha=ids[0], parents=tuple(ids[1:]), author=author,
                               subject=subject, trailer=found[-1] if found else None))
    return commits


class _VerdictCache:
    """Clone-local, advisory memo of Π(σ)-derived W verdicts:
    `.publish/.git/pubrepo/verdicts.json`, keyed (snapshot, full σ, config
    blob at σ, ENGINE_VERSION). Identity and keep verdicts are never stored."""

    def __init__(self, publish_dir: Path):
        self.path = publish_dir / ".git" / "pubrepo" / "verdicts.json"
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.data = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.data = {}
        self.dirty = False

    def get(self, key: str):
        return self.data.get(key)

    def put(self, key: str, value) -> None:
        self.data[key] = value
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        with suppress(OSError):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".verdicts.")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.data, f, sort_keys=True)
            os.replace(tmp, self.path)


class _IdentityView:
    """Everything §3.1 derives for one destination, from records, pull
    records and the tip's history. Built per run; identity is never cached."""

    def __init__(self, source_root: Path, publish_dir: Path, config: dict,
                 proj: Projection | None, *, config_relpath: str | None,
                 fetch_sigma: bool):
        self.source_root, self.publish_dir, self.config = source_root, publish_dir, config
        self.config_relpath = config_relpath    # None: config outside the source tree
        self.proj = proj
        self.fetch_sigma = fetch_sigma
        self.destination = destination_identity(config)
        self.head = _source_head(source_root)
        snaps = read_records(source_root, "refs/pubrepo/snap/")
        self.snap_records = {name.rsplit("/", 1)[1]: rec for name, rec in snaps.items()}
        self.repair_new_to_old: dict[str, str] = {}
        self.repairs = _applied_repairs(source_root)
        for rep in self.repairs:
            for old, new in (rep.get("map") or {}).items():
                if new:
                    self.repair_new_to_old[new] = old
        self.pulls_fp = pull_records(source_root, destination=self.destination,
                                     first_parent=True)
        self.pulls_all = pull_records(source_root, destination=self.destination,
                                      first_parent=False)
        self.through = {self._map_repaired(r.through) for r in self.pulls_all}
        self.sigma_cache: dict[str, list[str]] = {}
        self.fetched_sigmas: set[str] = set()
        # filled by analyse()
        self.ours_qualified: set[str] = set()
        self.lw: dict[str, str] = {}
        self.touched: set[str] = set()
        self.written_ever: set[str] = set()
        self.baseline_by_snapshot: dict[str, set[str]] = {}
        self.newest_baseline: dict[str, str] = {}
        self._decisions: dict[str, dict] | None = None

    # --- helpers ---

    def _map_repaired(self, sha: str) -> str:
        """Follow applied repair maps old → new (INV-4 (c))."""
        seen = set()
        new_by_old = {old: new for new, old in self.repair_new_to_old.items()}
        while sha in new_by_old and sha not in seen:
            seen.add(sha)
            sha = new_by_old[sha]
        return sha

    def pi_blob(self, path: str) -> str | None:
        entry = self.proj.entries.get(path) if self.proj else None
        return entry.blob if entry else None

    def merged_decision(self, path: str) -> dict | None:
        """The newest decision for `path` in a pull record of this
        destination that is in HEAD's history but not on its first-parent
        chain (a --no-ff-merged topic). Display only (design §5.7):
        never a decision, never applicable, never prefilled."""
        fp = {r.sha for r in self.pulls_fp}
        for rec in self.pulls_all:                       # newest first
            if rec.sha in fp:
                continue
            for entry in rec.entries:
                if entry["path"] == path:
                    return entry
        return None

    def newest_decision(self, path: str) -> dict | None:
        if self._decisions is None:
            self._decisions = {}
            for rec in self.pulls_fp:                    # newest first
                for entry in rec.entries:
                    self._decisions.setdefault(entry["path"], entry)
        return self._decisions.get(path)

    def remove_decided(self, path: str, blob: str | None) -> bool:
        return any(e["path"] == path and e["decision"] == "remove" and e["theirs"] == blob
                   for rec in self.pulls_all for e in rec.entries)

    def sigmas(self, abbrev: str) -> list[str]:
        if abbrev not in self.sigma_cache:
            self.sigma_cache[abbrev] = resolve_sigma(self.source_root, abbrev)
        return self.sigma_cache[abbrev]

    def _try_fetch_sigma(self, sigma: str) -> bool:
        """§4.8: fetch a record's full σ from records_remote when it does not
        resolve here. Objects only; no ref, no FETCH_HEAD."""
        if not self.fetch_sigma or sigma in self.fetched_sigmas:
            return False
        self.fetched_sigmas.add(sigma)
        # announce=False: the run's own records fetch already warned, and a
        # quiet resolve must never mark the one-time notice as shown.
        url = _resolve_records_remote(self.source_root, self.config, announce=False)
        if url is None:
            return False
        r = _git_net(["fetch", "--no-tags", "--no-write-fetch-head", url, sigma],
                     self.source_root, url=url)
        if r is None or r.returncode != 0:
            return False
        self.sigma_cache.pop(sigma, None)
        return bool(self.sigmas(sigma))

    # --- §3.1 step 1 ---

    def identify(self, commit: _Commit) -> Identity:
        sha = commit.sha
        record, record_sha, via = self.snap_records.get(sha), sha, None
        if record is None and sha in self.repair_new_to_old:
            via = self.repair_new_to_old[sha]
            record, record_sha = self.snap_records.get(via), via
        if record is not None:
            sigma = record.get("source")
            if sigma and (self.sigmas(sigma) or self._try_fetch_sigma(sigma)):
                kind = "ours_record"
            else:
                kind = "ours_unresolved"
            return Identity(sha=sha, kind=kind, sigma=sigma, trailer=commit.trailer,
                            record_ref=f"refs/pubrepo/snap/{record_sha}", via_repair=via)
        if commit.trailer:
            candidates = self.sigmas(commit.trailer)
            if candidates:
                return Identity(sha=sha, kind="ours_trailer", sigma=candidates[0],
                                trailer=commit.trailer, record_ref=None, via_repair=via)
        return Identity(sha=sha, kind="foreign", sigma=None, trailer=commit.trailer,
                        record_ref=None, via_repair=via)

    # --- §3.1 step 2 ---

    def write_set(self, commit: _Commit, ident: Identity, cache: _VerdictCache,
                  earlier_written: set[str]) -> tuple[dict[str, str], set[str], set[str]]:
        """(W, baseline paths, Delta paths) of an ours-snapshot."""
        fp = commit.parents[0] if commit.parents else None
        delta = _delta(self.publish_dir, commit.sha, fp)
        record = (self.snap_records.get(commit.sha) if ident.record_ref else None)
        if record is not None and isinstance(record.get("written"), dict):
            written = {p: v for p, v in record["written"].items() if isinstance(v, str)}
            return written, set(), set(delta) | set(written)
        if ident.record_ref and ident.kind == "ours_unresolved":
            return {}, set(delta), set(delta)   # nothing verifiable without σ or a record
        candidates = self.sigmas(ident.trailer) if ident.trailer else []
        if ident.sigma and ident.sigma not in candidates:
            candidates = [ident.sigma] + candidates
        best: tuple[dict, set] | None = None
        for sigma in candidates or [None]:
            result = self._verify_trailer(commit.sha, sigma, delta, cache, earlier_written)
            if best is None or len(result[1]) < len(best[1]):
                best = result
            if not result[1]:
                break
        return best[0], best[1], set(delta)

    def _pi_sigma_for(self, sha: str, sigma: str | None, delta_paths: list[str],
                      cache: _VerdictCache) -> dict | None:
        """{path: Π(σ) blob or None} for the Delta paths; None when Π(σ)
        cannot be computed (config untracked, renamed or invalid at σ)."""
        if sigma is None or self.config_relpath is None:
            return None    # Π(σ) unknown: every Delta path becomes a baseline item
        rel = self.config_relpath
        cfg_blob = git("rev-parse", "-q", "--verify",
                       f"{sigma}:{_source_prefix(self.source_root)}{rel}",
                       cwd=self.source_root, check=False).stdout.strip() or "none"
        key = f"{sha}:{sigma}:{cfg_blob}:{ENGINE_VERSION}"
        cached = cache.get(key)
        if isinstance(cached, dict) and set(delta_paths) <= set(cached.get("pi", {})) | (
                {"__none__"} if cached.get("none") else set()):
            return None if cached.get("none") else cached["pi"]
        at = projection_at(self.source_root, self.publish_dir, sigma, rel)
        if at is None:
            cache.put(key, {"none": True})
            return None
        pi = {p: (at.entries[p].blob if p in at.entries else None) for p in delta_paths}
        cache.put(key, {"pi": pi})
        return pi

    def _verify_trailer(self, sha: str, sigma: str | None, delta: dict, cache: _VerdictCache,
                        earlier_written: set[str]) -> tuple[dict[str, str], set[str]]:
        """The §3.1 trailer-only table."""
        pi = self._pi_sigma_for(sha, sigma, sorted(delta), cache)
        keep = set(self.config.get("keep", []))
        written: dict[str, str] = {}
        baseline: set[str] = set()
        for path, (old, new, _status) in delta.items():
            in_pi = pi is not None and pi.get(path) is not None
            if new is not None:
                if new.startswith("gitlink:"):
                    ok = False
                elif in_pi:
                    ok = new == pi[path]
                elif path in keep:
                    ok = self.proj is not None and path in self.proj.entries \
                        and self.proj.entries[path].keep and self.proj.entries[path].blob == new
                else:
                    ok = False
                if ok:
                    written[path] = new
                else:
                    baseline.add(path)
            else:
                if pi is None or in_pi:
                    baseline.add(path)
                elif path in earlier_written or self.remove_decided(path, old):
                    written[path] = "deleted"
                else:
                    baseline.add(path)
        return written, baseline

    # --- the whole history ---

    def analyse(self, history: list[_Commit], cache: _VerdictCache):
        """Identity, qualification and W for every commit in the tip's
        history. Returns ({sha: Identity}, {sha: qualified})."""
        by_sha = {c.sha: c for c in history}
        idents = {c.sha: self.identify(c) for c in history}
        has_ours: dict[str, bool] = {}
        qualified: dict[str, bool] = {}
        for commit in reversed(history):                 # parents first
            ours = idents[commit.sha].kind != "foreign"
            fp = commit.parents[0] if commit.parents else None
            if ours:
                qualified[commit.sha] = (
                    fp is None or fp not in by_sha or fp in self.through
                    or (idents[fp].kind != "foreign" and qualified.get(fp, False))
                    or not has_ours.get(fp, False))
            has_ours[commit.sha] = ours or any(has_ours.get(p, False) for p in commit.parents)
        self.ours_qualified = {s for s, q in qualified.items() if q}
        earlier: set[str] = set()
        per_snapshot: list[tuple[str, dict, set, set]] = []
        for commit in reversed(history):                 # oldest first: "earlier W"
            if commit.sha not in self.ours_qualified:
                continue
            written, baseline, touched = self.write_set(commit, idents[commit.sha], cache,
                                                        earlier)
            earlier |= {p for p, v in written.items() if v != "deleted"}
            self.written_ever |= {p for p, v in written.items() if v != "deleted"}
            self.baseline_by_snapshot[commit.sha] = baseline
            per_snapshot.append((commit.sha, written, baseline, touched))
        for sha, written, baseline, touched in reversed(per_snapshot):   # newest first
            for path in touched:
                if path in self.touched:
                    continue
                self.touched.add(path)
                if path in written:
                    self.lw[path] = written[path]
                elif path in baseline:
                    self.newest_baseline[path] = sha
        return idents, qualified


def classify_commit(source_root: Path, publish_dir: Path, sha: str) -> Identity:
    """Identity alone (§3.1 step 1), for evidence checks that run
    before compute_integration. Records and σ are local only (no fetch)."""
    out = git("log", "-1", "--format=%H %P%n%an <%ae>%n%s%n%B", sha, cwd=publish_dir).stdout
    head, author, subject, body = (out.split("\n", 3) + ["", "", ""])[:4]
    ids = head.split()
    found = _SOURCE_TRAILER_RE.findall(body)
    view = _IdentityView.__new__(_IdentityView)
    view.source_root, view.publish_dir, view.config = source_root, publish_dir, {}
    view.config_relpath = None
    view.fetch_sigma, view.sigma_cache, view.fetched_sigmas = False, {}, set()
    view.snap_records = {name.rsplit("/", 1)[1]: rec for name, rec in
                         read_records(source_root, "refs/pubrepo/snap/").items()}
    view.repair_new_to_old = {}
    for rep in _applied_repairs(source_root):
        for old, new in (rep.get("map") or {}).items():
            if new:
                view.repair_new_to_old[new] = old
    return view.identify(_Commit(sha=ids[0], parents=tuple(ids[1:]), author=author,
                                 subject=subject, trailer=found[-1] if found else None))


def _commit_info(publish_dir: Path, commit: _Commit) -> CommitInfo:
    fp = commit.parents[0] if commit.parents else None
    return CommitInfo(sha=commit.sha, author=commit.author, subject=commit.subject,
                      paths=tuple(sorted(_delta(publish_dir, commit.sha, fp))))


def _reintroduced(view: _IdentityView, publish_dir: Path, tip: str,
                  tip_tree: dict[str, tuple[str, str]]) -> list[Hit]:
    """§6.9 / S12: removed content from an applied repair at the tip or
    reachable from it. Blob ids under 64 bytes and the empty blob match
    only as exact (path, blob) pairs; a path alone never matches."""
    if not view.repairs:
        return []
    pairs: dict[tuple[str, str], str] = {}
    blobs: dict[str, str] = {}
    for rep in view.repairs:
        for item in rep.get("removed") or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                pairs[(item[0], item[1])] = rep["_digest"]
        for blob in rep.get("removed_blobs") or []:
            blobs.setdefault(blob, rep["_digest"])
    if blobs:
        sizes = _run_git_process(["git", "cat-file", "--batch-check=%(objectname) %(objectsize)"],
                               cwd=publish_dir, input="".join(f"{b}\n" for b in blobs),
                               capture_output=True, text=True).stdout.split("\n")
        big = {line.split()[0] for line in sizes
               if len(line.split()) == 2 and line.split()[1].isdigit()
               and int(line.split()[1]) >= S12_MIN_BLOB_SIZE}
        blobs = {b: d for b, d in blobs.items() if b in big and b != EMPTY_BLOB}
    hits: dict[tuple[str, str], Hit] = {}
    for path, (_mode, blob) in tip_tree.items():
        digest = pairs.get((path, blob)) or blobs.get(blob)
        if digest:
            hits[(path, blob)] = Hit(path=path, blob=blob, repair=digest, commit=None)
    out = git("rev-list", "--objects", tip, cwd=publish_dir).stdout
    for line in out.splitlines():
        obj, _, path = line.partition(" ")
        digest = pairs.get((path, obj)) or blobs.get(obj)
        if digest and (path, obj) not in hits:
            where = git("log", "-1", "--format=%H", f"--find-object={obj}", tip,
                        cwd=publish_dir).stdout.strip() or None
            hits[(path, obj)] = Hit(path=path, blob=obj, repair=digest, commit=where)
    return [hits[k] for k in sorted(hits)]


def compute_integration(source_root: Path, publish_dir: Path, config: dict,
                        tip: str | None, proj: Projection, *,
                        accept_divergence: str | None,
                        fetch_sigma: bool, config_relpath: str | None) -> IntegrationState:
    """§3.1, INV-3/4/5, §4.7, §4.8, §6.9 detection for one destination.

    `config_relpath` is the config file relative to source_root (posix),
    used to read the config at σ for Π(σ). It is None only for a config
    outside the source tree; Π(σ) is then None and trailer-only deltas are
    baseline items. An in-tree config that σ lacks (untracked, renamed)
    passes its path, and projection_at returns None by itself.

    Pre: the source lock is held (shared or exclusive); records were
    fetched by the caller when online; `tip` is the fetched public tip
    (None: the branch is absent). Post: an IntegrationState. It never writes
    refs, the index or a worktree; it may fetch a record's σ objects (§4.8)
    and update the clone-local verdict cache.
    """
    view = _IdentityView(source_root, publish_dir, config, proj,
                         config_relpath=config_relpath, fetch_sigma=fetch_sigma)
    if tip is None:
        state = IntegrationState(state="S6", tip=None, ip=None, unpulled=[], items=[],
                                 missing_snapshots=[], divergence=None, reintroduced=[],
                                 copied_trailer=[], baseline_items=[])
        state._view = view
        return state

    cache = _VerdictCache(publish_dir)
    history = _tip_history(publish_dir, tip)
    by_sha = {c.sha: c for c in history}
    idents, qualified = view.analyse(history, cache)
    cache.save()

    # INV-4: the integration point, walking first-parent from the tip.
    ip, divergence = None, None
    accepted = accept_divergence
    sha = tip
    while sha is not None and sha in by_sha:
        commit = by_sha[sha]
        ident = idents[sha]
        if sha in view.through:
            ip = sha
            break
        if sha in view.ours_qualified:
            sigma = ident.sigma
            if accepted and accepted in (sha, sigma):
                ip = sha
                break
            if ident.kind != "ours_unresolved" and view.head and sigma and \
                    _is_ancestor(source_root, sigma, view.head):
                ip = sha
                break
            if ident.kind == "ours_unresolved" or not sigma:
                kind, branches = "gone", ()
            elif view.head and _is_ancestor(source_root, view.head, sigma):
                kind, branches = "behind", ()
                upstream = git("rev-parse", "-q", "--verify", "@{upstream}",
                               cwd=source_root, check=False).stdout.strip()
                if not upstream or not _is_ancestor(source_root, sigma, upstream):
                    # name the local branches the snapshot was
                    # published from, so the message can say which to check out.
                    listed = git("branch", "--format=%(refname:short)", "--contains", sigma,
                                 cwd=source_root, check=False).stdout.split()
                    branches = tuple(sorted(listed))
            else:
                kind = "diverged"
                listed = git("branch", "--format=%(refname:short)", "--contains", sigma,
                             cwd=source_root, check=False).stdout.split()
                branches = tuple(sorted(listed))
            divergence = DivergenceInfo(snapshot=sha, sigma=sigma, kind=kind,
                                        branches=branches,
                                        accept_flag=f"--accept-source-divergence={sha}")
            break
        sha = commit.parents[0] if commit.parents else None

    # §3.1 step 4 and the pull-record half of S7.
    reachable = set(by_sha)
    missing = sorted(s for s in active_snapshot_records(source_root, view.destination)
                     if s not in reachable)
    if accepted and accepted in missing:
        missing.remove(accepted)          # acceptance writes `dropped` (§4.8)
    newest_pull = view.pulls_fp[0] if view.pulls_fp else None
    pull_lost = (newest_pull is not None
                 and view._map_repaired(newest_pull.through) not in reachable)

    # The unpulled range and copied trailers.
    if ip is not None:
        range_shas = git("rev-list", "--topo-order", tip, f"^{ip}", cwd=publish_dir).stdout.split()
    elif divergence is not None:
        range_shas = git("rev-list", "--topo-order", tip, f"^{divergence.snapshot}",
                         cwd=publish_dir).stdout.split()
    else:
        range_shas = [c.sha for c in history]
    unpulled = [_commit_info(publish_dir, by_sha[s]) for s in range_shas]
    copied = [s for s in range_shas
              if idents[s].kind == "ours_trailer" and not qualified.get(s, True)]

    # INV-3 items at the tip.
    tip_tree = _tip_tree = _tree_map(publish_dir, tip)
    state_obj = IntegrationState(state="", tip=tip, ip=ip, unpulled=unpulled, items=[],
                                 missing_snapshots=missing, divergence=divergence,
                                 reintroduced=[], copied_trailer=copied, baseline_items=[])
    state_obj._view = view
    items: list[PullItem] = []
    baseline_unmanaged: list[PullItem] = []
    paths = set(proj.entries) | {p for p, e in tip_tree.items() if _regular(e)} \
        | set(view.newest_baseline)
    for path in sorted(paths):
        tip_blob = _regular(tip_tree.get(path))
        pi = view.pi_blob(path)
        managed = path in proj.entries or (tip_blob is not None and managed_now(path, config))
        # §3.1 step 3 (AC14b2): the tip == Π exemption applies only
        # to a path managed now. For an unmanaged path it is vacuous (both
        # absent) and would hide an unverified deletion of a carried file.
        if path in view.newest_baseline and (not managed or tip_blob != pi):
            snap = view.newest_baseline[path]
            if managed:
                if not state_obj.accounted(path, tip_blob):
                    items.append(PullItem(path=path, cls="baseline_managed", base=None, ours=pi,
                                          theirs=tip_blob, origin="baseline", snapshot=snap,
                                          flags=(), options=("accept", "ours", "delete"),
                                          default=None, prefilled_from=None))
            else:
                parent = by_sha[snap].parents[0] if by_sha[snap].parents else None
                base = _delta(publish_dir, snap, parent).get(path, (None,))[0]
                item = PullItem(path=path, cls="baseline_unmanaged", base=base, ours=None,
                                theirs=tip_blob, origin="baseline", snapshot=snap, flags=(),
                                options=("leave", "remove", "restore"), default=None,
                                prefilled_from=None)
                baseline_unmanaged.append(item)
                if state_obj.decision_for(path, tip_blob) is None:
                    items.append(item)
            continue
        if managed:
            if state_obj.accounted(path, tip_blob):
                continue
            add_add = tip_blob is not None and pi is not None and path not in view.touched
            items.append(PullItem(path=path, cls="add_add" if add_add else "unaccounted",
                                  base=None, ours=pi, theirs=tip_blob, origin="inv3",
                                  snapshot=None, flags=(), options=("accept", "ours", "delete"),
                                  default=None, prefilled_from=None))
        elif path in view.written_ever and tip_blob is not None:
            lw = view.lw.get(path)
            if lw is not None and lw != "deleted" and tip_blob == lw:
                continue                                     # (b) delete, named by publish
            decision = state_obj.decision_for(path, tip_blob)
            if decision is not None and decision.decision in ("delete", "remove", "leave"):
                continue
            items.append(PullItem(path=path, cls="C8b", base=lw if lw != "deleted" else None,
                                  ours=None, theirs=tip_blob, origin="inv3", snapshot=None,
                                  flags=(), options=("leave", "delete"), default=None,
                                  prefilled_from=None))
    state_obj.items = items
    state_obj.baseline_items = baseline_unmanaged
    state_obj.reintroduced = _reintroduced(view, publish_dir, tip, tip_tree)

    # Our changes pending, against the tip (tip == IP) or the IP.
    base_tree = tip_tree if ip in (None, tip) else _tree_map(publish_dir, ip)
    pending = any(_regular(base_tree.get(p)) != e.blob for p, e in proj.entries.items()) or any(
        _regular(e) is not None and managed_now(p, config) and p not in proj.entries
        and view.lw.get(p) == _regular(e) for p, e in base_tree.items())

    if missing or pull_lost:
        state = "S7"
    elif divergence is not None:
        state = {"behind": "S10a"}.get(divergence.kind, "S10b")
        if state == "S10b" and unpulled:
            state = "S10b+S2"
    elif state_obj.reintroduced:
        state = "S12"
    elif ip != tip:
        state = "S3" if pending else "S2"
    elif items:
        state = "S9"
    else:
        state = "S1" if pending else "S0"
    state_obj.state = state
    return state_obj


def check_shallow(source_root: Path, publish_dir: Path) -> None:
    """AC16: a shallow source is refused; a shallow `.publish/` is
    unshallowed (design §3.2 INV-4)."""
    if git("rev-parse", "--is-shallow-repository", cwd=source_root).stdout.strip() == "true":
        raise ConfigError("ERROR: full source history required (CI: fetch-depth: 0)")
    if (publish_dir / ".git").exists() and git(
            "rev-parse", "--is-shallow-repository", cwd=publish_dir).stdout.strip() == "true":
        ui.info("Unshallowing .publish/ (full public history is needed)…")
        r = _git_net(["fetch", "--unshallow", "--no-tags", "origin"], publish_dir, url="origin")
        if r is None or r.returncode != 0:
            raise PublishError("ERROR: cannot unshallow .publish/: "
                               + (sanitize_provider_output(r.stderr.strip()) if r else "timed out"))


# --- sync: pull ---
# Design §5 (Phase 1: no §5.11 check). pull never runs implicitly, never
# merges public commits into source refs, and writes source only after the
# plan was shown (TTY) or bound by digest (--yes --expect-tip --expect-plan).

PULL_BLOB_FIELDS = ("base", "ours", "theirs", "result")
_NEVER_DEFAULT_CLASSES = frozenset({"transformed", "baseline_managed", "baseline_unmanaged"})
# Decisions that write the incoming blob into the source worktree and index.
_WRITES_THEIRS = frozenset({"accept", "theirs", "restore", "accept-excluded"})


@dataclass
class PlanItem:
    """One pull item (design §5.3) with its decision state."""
    path: str
    cls: str
    base: str | None
    ours: str | None                 # Π blob for managed paths
    theirs: str | None
    options: tuple[str, ...]
    default: str | None = None
    decision: str | None = None      # explicit (flag or --decisions)
    prefilled: str | None = None     # §5.7 prefill, shown; needs its flag off a TTY
    prefilled_from: str | None = None
    result: str | None = None        # C1 merge result blob
    flags: tuple = ()                # inbound Flag objects
    managed: bool = False
    mode: str | None = None          # incoming tree mode
    base_mode: str | None = None
    origin: str = "range"
    note: str | None = None
    merged_from: str | None = None   # display only: "<decision> <record sha>" (§5.7)

    @property
    def chosen(self) -> str | None:
        return self.decision or self.prefilled or self.default

    def needs_explicit(self) -> bool:
        """Never defaulted off a TTY (§5.1 item 3)."""
        return (self.default is None or bool(self.flags) or self.prefilled is not None
                or self.origin in ("baseline", "redecide") or self.cls in _NEVER_DEFAULT_CLASSES)


@dataclass
class PullPlan:
    destination: tuple[str, str]
    head: str
    tip: str | None
    ip: str | None
    base_ref: str | None             # tree the range is diffed from (IP, a lost snapshot, or None)
    state: str
    unpulled: list                   # CommitInfo
    items: list[PlanItem]
    carried: list[str]               # unmanaged paths carried byte for byte
    copied_trailer: list[str]
    missing_snapshots: list[str]
    accepted_divergence: str | None
    accept_rewritten: bool
    config_fingerprint: str
    targets_fingerprint: str
    possible_targets: list[str]
    digest: str = ""
    # Explicit `include` entries and the config's name, for
    # the "remove it from include" notice. Not part of the digest.
    explicit_includes: list[str] = field(default_factory=list)
    config_label: str = CONFIG_FILE_NAME

    def undecided(self) -> list[PlanItem]:
        return [i for i in self.items if i.chosen is None]


def plan_digest(plan: PullPlan) -> str:
    """sha256 of canonical JSON (§5.1 item 3). It covers
    the range, source HEAD, the config and target-set fingerprints, the
    pubrepo version, the acceptance flags and, per item, class, decision,
    origin (range / inv3 / baseline / redecide), blobs and prefill. It never covers --yes, --expect-*, --json or --quiet."""
    payload = {
        "version": __version__, "destination": list(plan.destination), "head": plan.head,
        "range": [plan.ip, plan.tip], "base": plan.base_ref,
        "config": plan.config_fingerprint, "targets": plan.targets_fingerprint,
        "accept_rewritten_history": plan.accept_rewritten,
        "accept_source_divergence": plan.accepted_divergence,
        "items": [{"path": i.path, "class": i.cls, "decision": i.chosen, "origin": i.origin,
                   "base": i.base, "ours": i.ours, "theirs": i.theirs, "result": i.result,
                   "prefilled_from": i.prefilled_from} for i in plan.items],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _targets_fingerprint(targets: list) -> str:
    rows = sorted([t.config_path, *t.destination] for t in targets)
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def _is_binary_blob(publish_dir: Path, blob: str | None) -> bool:
    if blob is None:
        return False
    data = _run_git_process(["git", "cat-file", "blob", blob], cwd=publish_dir,
                          capture_output=True).stdout
    return b"\x00" in data[:8192]


def _pull_env(source_root: Path, publish_dir: Path) -> dict:
    """Run git in source with .publish's objects readable as an alternate;
    new objects land in the source ODB (§C)."""
    env = dict(os.environ)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(_odb_dir(publish_dir))
    return env


def _copy_blob_to_source(source_root: Path, publish_dir: Path, blob: str) -> str:
    """Write `blob` (readable from .publish or source) into the source ODB."""
    data = _cat_blob(source_root, _pull_env(source_root, publish_dir), blob)
    stored = _run_git_process(["git", "hash-object", "-w", "--stdin", "--no-filters"],
                            cwd=source_root, input=data, capture_output=True, check=True)
    sha = stored.stdout.decode().strip()
    if sha != blob:
        raise AssertionError(f"blob copy changed id: {blob} → {sha}")
    return sha


def merge_text(base_blob: str | None, ours_blob: str, theirs_blob: str, *,
               source_root: Path, publish_dir: Path) -> tuple[str, int]:
    """Three-way text merge of normalized blobs (§5.6): `git
    merge-file -p --diff3` on cat-file bytes (an absent base is empty, C7).
    The result is stored in the source ODB with --no-filters. Returns
    (result blob, conflict count). `apply --3way` and union are never used."""
    env = _pull_env(source_root, publish_dir)
    with tempfile.TemporaryDirectory(prefix="pubrepo-merge-") as tmp:
        names = []
        for label, blob in (("ours", ours_blob), ("base", base_blob), ("theirs", theirs_blob)):
            p = Path(tmp) / label
            p.write_bytes(_cat_blob(source_root, env, blob) if blob else b"")
            names.append(str(p))
        merged = _run_git_process(["git", "merge-file", "-p", "--diff3", "-L", "ours", "-L", "base",
                                 "-L", "theirs", *names], cwd=source_root, capture_output=True)
    if merged.returncode < 0 or merged.returncode > 127:
        raise PublishError("ERROR: git merge-file failed: "
                           + sanitize_provider_output(merged.stderr.decode(errors="replace").strip()))
    stored = _run_git_process(["git", "hash-object", "-w", "--stdin", "--no-filters"],
                            cwd=source_root, input=merged.stdout, capture_output=True, check=True)
    return stored.stdout.decode().strip(), merged.returncode


def _range_delta(publish_dir: Path, base: str | None, tip: str) -> dict[str, tuple]:
    """{path: (base mode, base blob, tip mode, tip blob)} for every path that
    differs between `base` (a commit or None = empty) and `tip`, no renames."""
    base_tree = _tree_map(publish_dir, base)
    tip_tree = _tree_map(publish_dir, tip)
    out = {}
    for path in set(base_tree) | set(tip_tree):
        b, t = base_tree.get(path), tip_tree.get(path)
        if b != t:
            out[path] = (b[0] if b else None, b[1] if b else None,
                         t[0] if t else None, t[1] if t else None)
    return out


def _prefill(integ: IntegrationState, item: PlanItem) -> dict | None:
    """§5.7 applicability, for pull-time prefill only: overwriting decisions
    need path, class, base, ours and theirs to all match; decline, leave and
    remove need (path, theirs)."""
    entry = integ._view.newest_decision(item.path)
    if entry is None:
        return None
    decision = entry["decision"]
    if decision in ("decline", "leave", "remove"):
        ok = entry["theirs"] == item.theirs
    else:
        ok = (entry.get("class") == item.cls and entry["base"] == item.base
              and entry["ours"] == item.ours and entry["theirs"] == item.theirs)
    return entry if ok and decision in item.options else None


def _classify(path: str, rng: tuple, *, pi: ProjEntry | None, config: dict, managed: bool,
              keep: bool, transformed: bool, binary: bool) -> tuple[str, tuple, str | None]:
    """(class, options, default) for one range path (design §5.3, §5.4)."""
    base_mode, base, mode, theirs = rng
    ours = pi.blob if pi else None
    regular = mode in (None, "100644", "100755")
    if not managed:
        if not regular or (base_mode and base_mode not in ("100644", "100755")):
            return "type_unprojected", ("leave", "remove"), None
        if theirs is None:
            return "carried_delete", ("leave",), None           # deletions never defaulted
        if base is None:
            return "carried_add", ("leave", "remove"), "leave"
        return "carried", ("leave", "remove"), "leave"
    if not regular:
        return "type_projected", ("ours",), None
    if keep:
        if ours == base:
            return "keep", ("accept", "ours"), ("accept" if theirs is not None else None)
        return "keep_both", ("ours", "theirs"), None
    if transformed:
        return "transformed", ("resolved", "decline"), None
    ours_changed = ours != base
    if ours == theirs:
        return "C10", ("accept",), "accept"
    if not ours_changed:
        if theirs is None:
            return "theirs_deleted", ("delete", "keep"), None
        if binary:
            return "theirs_binary", ("accept", "decline"), None
        return "theirs_changed", ("accept", "decline"), "accept"
    if base is None:                                           # add/add (C7)
        return ("C7_binary", ("ours", "theirs"), None) if binary else \
            ("C7", ("accept", "ours", "theirs", "resolved"), None)
    if theirs is None:
        return "C4", ("keep", "delete"), None
    if ours is None:
        return "C5", ("restore", "delete"), None
    if binary:
        return "C3", ("ours", "theirs"), None
    return "C1", ("accept", "ours", "theirs"), None             # refined by the merge


def compute_pull_plan(source_root: Path, publish_dir: Path, config: dict, *,
                      config_relpath: str | None, tip: str | None, proj: Projection,
                      integ: IntegrationState, redecide: Iterable[str] = (),
                      baseline: bool = False, accept_rewritten: bool = False,
                      accepted_divergence: str | None = None) -> PullPlan:
    """The plan (design §5.3–§5.10) for one destination. Read-only for
    source refs, index and worktree; merge results are stored as loose
    objects in the source ODB."""
    destination = destination_identity(config)
    head = _source_head(source_root) or ""
    targets, possible = known_targets(source_root, config)
    base_ref = integ.ip
    if integ.state == "S7" and integ.missing_snapshots:
        lost = integ.missing_snapshots[-1]
        if git("cat-file", "-e", f"{lost}^{{commit}}", cwd=publish_dir, check=False).returncode == 0:
            base_ref = lost                                    # §5.9: diff against it
    plan = PullPlan(destination=destination, head=head, tip=integ.tip, ip=integ.ip,
                    base_ref=base_ref, state=integ.state, unpulled=list(integ.unpulled),
                    items=[], carried=[], copied_trailer=list(integ.copied_trailer),
                    missing_snapshots=list(integ.missing_snapshots),
                    accepted_divergence=accepted_divergence, accept_rewritten=accept_rewritten,
                    config_fingerprint=config_fingerprint(config),
                    targets_fingerprint=_targets_fingerprint(targets),
                    possible_targets=possible,
                    explicit_includes=sorted({posixpath.normpath(e) for e in config.get("include", [])
                                              if isinstance(e, str)}),
                    config_label=config_relpath or CONFIG_FILE_NAME)
    if integ.tip is None:
        plan.digest = plan_digest(plan)
        return plan
    rng = {} if base_ref == integ.tip else _range_delta(publish_dir, base_ref, integ.tip)
    tip_tree = _tree_map(publish_dir, integ.tip)
    base_tree = _tree_map(publish_dir, base_ref) if base_ref else {}
    keep = set(config.get("keep", []))
    transforms = set(config.get("transforms", {}))
    items: dict[str, PlanItem] = {}

    def managed_path(path: str) -> bool:
        if is_hard_excluded(posixpath.basename(path)):
            return False
        return path in proj.entries or managed_now(path, config)

    for path in sorted(rng):
        base_mode, base, mode, theirs = rng[path]
        if mode == "160000":
            theirs = f"gitlink:{theirs}"
        pi = proj.entries.get(path)
        managed = managed_path(path)
        binary = _is_binary_blob(publish_dir, theirs if mode not in ("120000", "160000") else None) \
            or _is_binary_blob(publish_dir, pi.blob if pi else None)
        cls, options, default = _classify(path, (base_mode, base, mode, theirs), pi=pi,
                                          config=config, managed=managed, keep=path in keep,
                                          transformed=path in transforms, binary=binary)
        item = PlanItem(path=path, cls=cls, base=base, ours=pi.blob if pi else None,
                        theirs=theirs, options=options, default=default, managed=managed,
                        mode=mode, base_mode=base_mode)
        if cls in ("carried", "carried_add") and item.default == "leave":
            plan.carried.append(path)
        items[path] = item

    for pitem in integ.items + (integ.baseline_items if baseline else []):
        # INV-3 classes the range cannot see (C8b, baseline) take precedence;
        # for other range paths the range classification is the richer one.
        if pitem.path in items and pitem.cls not in ("C8b", "baseline_managed",
                                                     "baseline_unmanaged"):
            continue
        entry = tip_tree.get(pitem.path)
        items[pitem.path] = PlanItem(
            path=pitem.path, cls=pitem.cls, base=pitem.base, ours=pitem.ours,
            theirs=pitem.theirs, options=pitem.options, managed=pitem.cls != "baseline_unmanaged"
            and pitem.cls != "C8b", mode=entry[0] if entry else None,
            base_mode=None, origin=pitem.origin, note=(f"unverified change in our snapshot "
                                                      f"{pitem.snapshot[:12]}" if pitem.snapshot
                                                      else None))
    for path in redecide:
        if path in items:
            items[path].origin = "redecide"
            continue
        entry = tip_tree.get(path)
        theirs = entry[1] if entry else None
        base = base_tree.get(path)
        pi = proj.entries.get(path)
        managed = managed_path(path)
        options = ("accept", "ours", "theirs", "decline", "delete", "leave", "remove",
                   "restore") if managed else ("leave", "remove")
        items[path] = PlanItem(path=path, cls="redecide", base=base[1] if base else None,
                               ours=pi.blob if pi else None, theirs=theirs, options=options,
                               managed=managed, mode=entry[0] if entry else None,
                               origin="redecide")

    # C1/C7 text merges and accept reachability.
    for item in items.values():
        if item.cls in ("C1", "C7") and item.ours and item.theirs:
            result, conflicts = merge_text(item.base, item.ours, item.theirs,
                                           source_root=source_root, publish_dir=publish_dir)
            item.result = result
            if conflicts:
                item.cls = "C2" if item.cls == "C1" else "C7"
                item.options = ("resolved", "ours", "theirs")
                item.default = None
            else:
                item.default = "accept" if item.cls == "C1" else None
        if ("accept" in item.options and item.theirs and not item.theirs.startswith("gitlink:")
                and item.managed and item.cls not in ("keep", "C10", "C1")
                and not _filter_attributes(source_root, [item.path])):
            projected = project_blob(source_root, publish_dir, item.path, item.theirs, config)
            if projected is None or projected.blob != item.theirs:
                item.options = tuple(o for o in item.options if o != "accept")
                if item.default == "accept":
                    item.default = None
                item.note = "accept is not offered: publishing it back would change its bytes"

    # Inbound flags, prefill (§5.7), copied trailers.
    ctx = make_inbound_ctx(source_root, publish_dir, config,
                           config_path=str(Path(source_root) / config_relpath) if config_relpath
                           else None, proj=proj, integ=integ, plan_paths=items,
                           incoming_blobs=[i.theirs for i in items.values()
                                           if i.theirs and not i.theirs.startswith("gitlink:")])
    for item in items.values():
        if item.theirs is None or not item.theirs.startswith("gitlink:"):
            pull_item = PullItem(path=item.path, cls=item.cls, base=item.base, ours=item.ours,
                                 theirs=item.theirs, origin="range", snapshot=None, flags=(),
                                 options=item.options, default=item.default, prefilled_from=None)
            flags = inbound_flags(pull_item, ctx, mode=item.mode, old_mode=item.base_mode)
        else:
            flags = [Flag("type_change", "gitlink", ("leave", "remove"))]
        if flags:
            item.flags = tuple(flags)
            options = list(item.options)
            for flag in flags:
                if flag.allowed is not None:
                    # A flag restricts the class's options; when none survive
                    # (e.g. a private path offered accept/decline), the flag's
                    # own options are the only ones left.
                    options = [o for o in options if o in flag.allowed] or list(flag.allowed)
            item.options = tuple(options)
            item.default = None
            if item.path in plan.carried:
                plan.carried.remove(item.path)
        entry = _prefill(integ, item)
        if entry is not None:
            item.prefilled, item.prefilled_from = entry["decision"], entry["_record"]
            item.default = None
        else:
            merged = integ._view.merged_decision(item.path)
            if merged is not None:
                item.merged_from = f"{merged['decision']} {merged['_record']}"
    plan.items = [items[p] for p in sorted(items)]
    plan.digest = plan_digest(plan)
    return plan


# --- decisions ---

_asdict = __import__("dataclasses").asdict   # the import block is core's

def read_decisions_file(path: str) -> list[tuple[str, str]]:
    """`--decisions FILE`: one `path<TAB>decision` per line, paths literal.
    Blank lines are skipped; anything else malformed → exit 1."""
    try:
        raw = Path(path).read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"ERROR: cannot read --decisions file {path}: {e}")
    pairs = []
    for number, line in enumerate(raw.split("\n"), 1):
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            continue
        fields = line.split("\t")
        if len(fields) != 2 or not fields[0] or fields[1] not in PULL_DECISIONS:
            raise ConfigError(f"ERROR: --decisions {path}:{number}: expected "
                              f"`path<TAB>decision` with one of {', '.join(PULL_DECISIONS)}")
        pairs.append((fields[0], fields[1]))
    return pairs


def _decision_flags(args) -> list[tuple[str, str]]:
    pairs = []
    for decision in PULL_DECISIONS:
        for path in getattr(args, f"decide_{decision.replace('-', '_')}", None) or []:
            pairs.append((path, decision))
    if getattr(args, "decisions", None):
        pairs += read_decisions_file(args.decisions)
    return pairs


def apply_decisions(plan: PullPlan, pairs: list[tuple[str, str]]) -> PullPlan:
    """Merge explicit decisions into the plan (§5.4). Unknown paths, a
    decision the item's class or flags do not offer, and two different
    decisions for one path are refused (exit 1)."""
    by_path = {i.path: i for i in plan.items}
    seen: dict[str, str] = {}
    for path, decision in pairs:
        if path in seen and seen[path] != decision:
            raise ConfigError(f"ERROR: two decisions for {path}: {seen[path]} and {decision}")
        seen[path] = decision
        item = by_path.get(path)
        if item is None:
            raise ConfigError(f"ERROR: --{decision} {path}: not an item in this pull plan")
        if decision not in item.options:
            raise ConfigError(f"ERROR: --{decision} {path}: not offered for this item "
                              f"(offered: {', '.join(item.options) or 'none'})")
        if decision == "resolved" and item.cls in ("C2", "C7"):
            # #7: a conflict is resolved by editing the file, then --continue;
            # up front, `resolved` would write nothing yet record a merge.
            others = ", ".join(f"--{o}" for o in item.options if o != "resolved")
            raise ConfigError(f"ERROR: --resolved {path}: resolved is recorded by pull "
                              f"--continue after you edit the file; for an up-front decision "
                              f"use one of: {others}")
        item.decision = decision
    plan.digest = plan_digest(plan)
    return plan


# --- rendering ---

def _short(blob: str | None) -> str:
    return "absent" if blob is None else blob[:12]


def _explicit_include_removals(plan: PullPlan) -> list[str]:
    """Paths a decision removes from source while the
    config's `include` names them explicitly. The next publish refuses the
    missing include (as in 1.0), so pull says so at decision time; it never
    edits the config itself."""
    out = []
    for i in plan.items:
        removes = (i.chosen == "delete" and i.managed) or (
            i.chosen in ("accept", "theirs") and i.theirs is None and i.managed)
        if removes and i.path in plan.explicit_includes:
            out.append(i.path)
    return out


def render_plan(plan: PullPlan, *, json_out: bool = False) -> str:
    """The plan as shown before any write (§5.4). The last stdout line is
    always `Plan digest: <64 hex>`."""
    if json_out:
        body = json.dumps({
            "destination": list(plan.destination), "tip": plan.tip, "ip": plan.ip,
            "state": plan.state, "unpulled": [_asdict(c) for c in plan.unpulled],
            "items": [{"path": i.path, "class": i.cls, "decision": i.chosen,
                       "explicit": i.decision is not None, "options": list(i.options),
                       "flags": [f.kind for f in i.flags], "prefilled_from": i.prefilled_from,
                       "base": i.base, "ours": i.ours, "theirs": i.theirs, "result": i.result}
                      for i in plan.items],
            "carried": plan.carried, "possible_targets": plan.possible_targets,
        }, indent=2, sort_keys=True)
        return f"{body}\nPlan digest: {plan.digest}"
    lines = [f"Pull plan for {plan.destination[0]} ({plan.destination[1]})",
             f"  integration point: {plan.ip or '(none: first pull)'}",
             f"  public tip:        {plan.tip or '(absent)'}"]
    if plan.accepted_divergence:
        lines.append(f"  accepted divergence: {plan.accepted_divergence}")
    if plan.missing_snapshots:
        lines.append("  public history was rewritten; no longer on public:")
        lines += [f"    {s}" for s in plan.missing_snapshots]
        lines.append("  content pulled from those commits may differ from public now")
    if plan.unpulled:
        lines.append(f"  unpulled commits ({len(plan.unpulled)}):")
        for c in plan.unpulled:
            note = ("  [carries a copied `Source:` trailer]"
                    if c.sha in plan.copied_trailer else "")
            lines.append(f"    {c.sha[:12]} {c.author}: {c.subject}{note}")
    if plan.items:
        lines.append(f"  items ({len(plan.items)}):")
    for i in plan.items:
        state = (f"→ {i.decision}" if i.decision else
                 f"→ {i.prefilled} (previously decided in {i.prefilled_from[:12]}; pass "
                 f"--{i.prefilled} to confirm)" if i.prefilled else
                 f"→ {i.default} (default)" if i.default else "→ decision required")
        lines.append(f"    {i.path}  [{i.cls}] base {_short(i.base)} ours {_short(i.ours)} "
                     f"theirs {_short(i.theirs)}  {state}")
        lines.append(f"      options: {', '.join('--' + o for o in i.options) or '(none)'}")
        if i.result and i.cls == "C1":
            lines.append(f"      clean merge → {_short(i.result)}")
        for f in i.flags:
            lines.append(f"      FLAG {f.kind}: {f.detail}")
        if i.note:
            lines.append(f"      note: {i.note}")
        if i.merged_from:
            decision, record = i.merged_from.split(" ", 1)
            lines.append(f"      previously decided `{decision}` in {record[:12]} on a merged branch")
        if i.path in _explicit_include_removals(plan):
            lines.append(f"      note: include entry '{i.path}' will name a missing path; "
                         f"remove it from include")
    if plan.carried:
        lines.append(f"  carried, stays public-only: {len(plan.carried)} path(s)")
    lines.append("  coverage: known targets checked"
                 + (f"; possible targets (unregistered): {', '.join(plan.possible_targets)}"
                    if plan.possible_targets else ""))
    lines.append(f"Plan digest: {plan.digest}")
    return "\n".join(lines)


def _plans_dir(source_root: Path) -> Path:
    return source_common_dir(source_root) / "pubrepo" / "plans"


def _plan_to_json(plan: PullPlan) -> dict:
    data = _asdict(plan)
    data["unpulled"] = [_asdict(c) for c in plan.unpulled]
    for item, raw in zip(plan.items, data["items"]):
        raw["flags"] = [_asdict(f) for f in item.flags]
    return data


def _plan_from_json(data: dict) -> PullPlan:
    items = []
    for raw in data["items"]:
        raw = dict(raw)
        raw["flags"] = tuple(Flag(**f) for f in raw.get("flags", []))
        raw["options"] = tuple(raw["options"])
        items.append(PlanItem(**raw))
    data = dict(data, items=items,
                unpulled=[CommitInfo(**{**c, "paths": tuple(c["paths"])}) for c in data["unpulled"]],
                destination=tuple(data["destination"]))
    return PullPlan(**data)


def save_plan_file(source_root: Path, plan: PullPlan) -> Path:
    """Every plan is kept at <common-dir>/pubrepo/plans/<digest>.json (§5.1
    item 3); the pull record names it."""
    directory = _plans_dir(source_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{plan.digest}.json"
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".plan.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(_plan_to_json(plan), f, sort_keys=True, indent=1)
    os.replace(tmp, path)
    return path


# --- writing ---

def _clean_tree_check(source_root: Path, paths: list[str]) -> None:
    """§5.1 item 2: staged changes, a modified tracked file, or a
    path pull would write that is untracked, ignored or modified → exit 1."""
    if git("diff", "--cached", "--quiet", cwd=source_root, check=False).returncode != 0:
        raise ConfigError("ERROR: pull refuses while changes are staged; commit or unstage them")
    if git("diff", "--quiet", cwd=source_root, check=False).returncode != 0:
        raise ConfigError("ERROR: pull refuses while tracked files are modified; commit them first")
    if paths:
        out = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored", "--",
                  *paths, cwd=source_root).stdout
        blocked = sorted({r[3:] for r in out.split("\0") if len(r) > 3})
        if blocked:
            raise ConfigError("ERROR: pull would write paths that exist untracked, ignored or "
                              "modified in source: " + ", ".join(blocked)
                              + "\nCommit, move or delete them first.")


def _head_blob(source_root: Path, path: str) -> str | None:
    r = git("rev-parse", "-q", "--verify", f"HEAD:{_source_prefix(source_root)}{path}",
            cwd=source_root, check=False)
    return r.stdout.strip() or None


def _publish_file_blob(publish_dir: Path, path: str) -> str | None:
    """The current bytes of <publish_dir>/<path> as a blob in the publish
    clone's ODB (so --abort can restore them), or None when absent."""
    target = Path(publish_dir) / path
    if not target.is_file():
        return None
    return _run_git_process(["git", "hash-object", "-w", "--no-filters", "--", str(target)],
                          cwd=publish_dir, capture_output=True, text=True,
                          check=True).stdout.strip()


def _stage_blob(source_root: Path, publish_dir: Path, path: str, blob: str, mode: str) -> None:
    """Scoped staging (§5.1 item 4): the blob enters the source ODB, the
    index, and then the worktree through checkout-index (eol smudge)."""
    _copy_blob_to_source(source_root, publish_dir, blob)
    git("update-index", "--add", "--cacheinfo",       # --cacheinfo is top-level-relative (#5)
        f"{mode},{blob},{_source_prefix(source_root)}{path}", cwd=source_root)
    git("checkout-index", "-f", "--", path, cwd=source_root)


def _stage_delete(source_root: Path, path: str) -> None:
    git("update-index", "--force-remove", "--", path, cwd=source_root)
    with suppress(FileNotFoundError):
        (Path(source_root) / path).unlink()


def write_pull(plan: PullPlan, source_root: Path, publish_dir: Path, config: dict, *,
               config_path: str) -> list[str]:
    """Write the decided items into source (§5.1 items 4–5). Returns the
    conflicted paths (markers in the worktree only; the pull stays in
    progress). Pre: the exclusive source lock is held and the plan was
    re-verified under it."""
    actions: list[tuple[PlanItem, str]] = []
    for item in plan.items:
        d = item.chosen
        if item.cls in ("keep", "keep_both") and d in ("accept", "theirs"):
            actions.append((item, "keep"))
        elif item.cls in ("C2", "C7") and d is None:
            actions.append((item, "conflict"))
        elif item.cls == "C1" and d == "accept":
            actions.append((item, "merge"))
        elif d in _WRITES_THEIRS and item.theirs is not None:
            actions.append((item, "theirs"))
        elif d in ("accept", "theirs", "delete") and item.theirs is None and item.managed:
            actions.append((item, "delete"))
        elif d == "delete" and item.managed and item.ours is not None:
            actions.append((item, "delete"))
    source_paths = [i.path for i, kind in actions if kind != "keep"]
    _clean_tree_check(source_root, source_paths)
    filtered = _filter_attributes(source_root, source_paths)
    if filtered:
        raise ConfigError("ERROR: pull refuses paths with a `filter` attribute (LFS, clean/"
                          "smudge): " + ", ".join(sorted(filtered)))
    here = _worktree_toplevel(source_root)
    write_in_progress(source_root, InProgress(
        worktree=here, branch=git("symbolic-ref", "-q", "--short", "HEAD", cwd=source_root,
                                  check=False).stdout.strip() or "(detached)",
        git_dir=str(Path(git("rev-parse", "--absolute-git-dir", cwd=source_root).stdout.strip())),
        config_path=config_path, digest=plan.digest,
        written={p: _head_blob(source_root, p) for p in source_paths},
        kept={i.path: _publish_file_blob(publish_dir, i.path)
              for i, kind in actions if kind == "keep"},
        publish_dir=str(Path(publish_dir).resolve())))
    conflicts = []
    for n, (item, kind) in enumerate(actions, 1):
        mode = item.mode if item.mode in ("100644", "100755") else "100644"
        if kind == "keep":
            data = _cat_blob(publish_dir, None, item.theirs)
            (Path(publish_dir) / item.path).write_bytes(data)
        elif kind == "theirs":
            _stage_blob(source_root, publish_dir, item.path, item.theirs, mode)
        elif kind == "merge":
            _stage_blob(source_root, publish_dir, item.path, item.result, mode)
        elif kind == "delete":
            _stage_delete(source_root, item.path)
        elif kind == "conflict":
            data = _cat_blob(source_root, _pull_env(source_root, publish_dir), item.result)
            target = Path(source_root) / item.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            conflicts.append(item.path)
        _test_fault(f"pull.after_write:{n}")
    for item, kind in actions:
        expect = {"theirs": item.theirs, "merge": item.result, "delete": None}.get(kind, "skip")
        if expect == "skip":
            continue
        staged = git("ls-files", "-s", "--", item.path, cwd=source_root).stdout.split()
        got = staged[1] if staged else None
        if got != expect:
            raise PublishError(f"ERROR: staged blob for {item.path} is {got}, the plan says "
                               f"{expect}; the pull is in progress — run `{_PROG} pull --abort`")
    return conflicts


def _co_authors(publish_dir: Path, plan: PullPlan, source_ident: str) -> list[str]:
    """§5.7: authors whose changes were accepted, resolved or taken as
    theirs, plus co-authors carried in their commits' trailers."""
    paths = {i.path for i in plan.items
             if i.chosen in ("accept", "theirs", "resolved", "restore", "accept-excluded")}
    people: list[str] = []
    for c in plan.unpulled:
        if not paths & set(c.paths):
            continue
        body = git("log", "-1", "--format=%B", c.sha, cwd=publish_dir).stdout
        found = [c.author] + re.findall(r"^Co-authored-by: (.+)$", body, re.M | re.I)
        for person in found:
            person = person.strip()
            if person and person != source_ident and person not in people:
                people.append(person)
    return people


def _entries_for_record(plan: PullPlan, config_path: str | None) -> list[dict]:
    return [{"path": i.path, "class": i.cls, "decision": i.chosen,
             "base": i.base or "absent", "ours": i.ours or "absent",
             "theirs": i.theirs or "absent",
             "result": (i.result if i.cls in ("C1", "C2", "C7") and i.chosen in ("accept", "resolved")
                        # an unmanaged baseline `restore` re-adds the parent blob (§5.3)
                        else i.base if i.cls == "baseline_unmanaged" and i.chosen == "restore"
                        else i.theirs if i.chosen in _WRITES_THEIRS else i.ours) or "absent",
             "managed_at_decision": i.managed, "config": config_path,
             "prefilled_from": i.prefilled_from, "flags": [f.kind for f in i.flags]}
            for i in plan.items]


def commit_pull_record(plan: PullPlan, source_root: Path, publish_dir: Path, *,
                       config_path: str | None, plan_file: Path,
                       resolved: dict[str, str] | None = None) -> str:
    """The pull record (§5.7): write-tree, commit-tree -p HEAD as the source
    identity, `dropped` markers for S7 acceptance (AC94), then a
    compare-and-swap of HEAD."""
    for path, blob in (resolved or {}).items():
        for item in plan.items:
            if item.path == path:
                item.decision, item.result = "resolved", blob
    head = _source_head(source_root)
    tree = git("write-tree", cwd=source_root).stdout.strip()
    ident = git("var", "GIT_AUTHOR_IDENT", cwd=source_root).stdout.strip()
    source_person = re.sub(r"\s+\d+\s+[+-]\d{4}$", "", ident)
    entries = _entries_for_record(plan, config_path)
    taken = sum(1 for i in plan.items if i.chosen in _WRITES_THEIRS | {"resolved"})
    title = (f"pull: {len(plan.unpulled)} public commit(s) through {plan.tip[:12]}"
             if plan.tip else "pull: nothing public")
    trailers = [f"Pubrepo-Pulled-Through: {plan.tip}",
                f"Pubrepo-Pulled-Range: {plan.ip or 'root'}..{plan.tip}",
                f"Pubrepo-Destination: {plan.destination[0]} {plan.destination[1]}",
                f"Pubrepo-Plan-File: pubrepo/plans/{plan_file.name}"]
    if plan.accepted_divergence:
        trailers.append(f"Pubrepo-Accepted-Divergence: {plan.accepted_divergence}")
    trailers += [f"Co-authored-by: {p}" for p in _co_authors(publish_dir, plan, source_person)]
    message = (f"{title}\n\n{len(plan.items)} item(s), {taken} written from public.\n\n"
               f"```json\n{json.dumps(entries, indent=1, sort_keys=True)}\n```\n\n"
               + "\n".join(trailers) + "\n")
    commit = git("commit-tree", tree, *(["-p", head] if head else []), "-m", message,
                 cwd=source_root).stdout.strip()
    if plan.accept_rewritten or plan.accepted_divergence:
        reachable = set(git("rev-list", plan.tip, cwd=publish_dir).stdout.split()) if plan.tip else set()
        for snap in sorted(active_snapshot_records(source_root, plan.destination)):
            if snap not in reachable and (plan.accept_rewritten or snap in (
                    plan.accepted_divergence,)):
                try:
                    write_status_marker(source_root, snap, "dropped",
                                        reason=f"accepted by pull through {plan.tip}")
                except RecordConflict as e:
                    ui.warn(f"WARNING: {e}")
    git("update-ref", "-m", "pubrepo pull", "HEAD", commit, head or "", cwd=source_root)
    return commit


# --- the command ---

def _pull_config_relpath(source_root: Path, args) -> tuple[str | None, str]:
    """(config path relative to source_root or None if outside the tree,
    absolute config path). Mirrors main()'s resolution."""
    path = Path(args.config) if getattr(args, "config", None) else Path(source_root) / CONFIG_FILE_NAME
    if not path.is_absolute():
        path = Path(source_root) / path
    resolved, root = path.resolve(), Path(source_root).resolve()
    rel = resolved.relative_to(root).as_posix() if resolved.is_relative_to(root) else None
    return rel, str(resolved)


def _staleness_warning(source_root: Path) -> None:
    """§5.1 item 7: compare with the last-known upstream; never fetch."""
    behind = git("rev-list", "--count", "HEAD..@{upstream}", cwd=source_root, check=False)
    if behind.returncode == 0 and behind.stdout.strip() not in ("", "0"):
        ui.warn(f"WARNING: source branch is behind its upstream by {behind.stdout.strip()} "
                f"commit(s) (last fetched state); consider updating it first")


def _require_initialized(source_root: Path, config: dict) -> None:
    """Before any git call: pull needs the publish clone (`init`)."""
    publish_dir = Path(source_root) / config.get("dir", PUBLISH_DIR_NAME)
    if not (publish_dir / ".git").exists():
        raise ConfigError(f"ERROR: {publish_dir.name}/ not initialized\nRun: {_PROG} init")


class _PullRun:
    """One pull invocation: the state needed by compute() under the locks."""

    def __init__(self, source_root: Path, config: dict, args):
        self.source_root, self.config, self.args = source_root, config, args
        self.publish_dir = Path(source_root) / config.get("dir", PUBLISH_DIR_NAME)
        self.config_relpath, self.config_path = _pull_config_relpath(source_root, args)
        self.pairs = _decision_flags(args)
        self.redecide = list(getattr(args, "redecide", None) or [])
        self.plan: PullPlan | None = None
        self.integ: IntegrationState | None = None

    def compute(self) -> Binding:
        branch = self.config.get("branch", "main")
        tip = fetch_tip(self.publish_dir, branch)
        with _quiet_ui() if self.plan is not None else nullcontext_():
            records_fetch(self.source_root, self.config)
        proj = build_projection(self.source_root, self.publish_dir, self.config)
        integ = compute_integration(
            self.source_root, self.publish_dir, self.config, tip, proj,
            accept_divergence=self.args.accept_source_divergence, fetch_sigma=True,
            config_relpath=self.config_relpath)
        plan = compute_pull_plan(
            self.source_root, self.publish_dir, self.config, config_relpath=self.config_relpath,
            tip=tip, proj=proj, integ=integ, redecide=self.redecide,
            baseline=self.args.baseline, accept_rewritten=self.args.accept_rewritten_history,
            accepted_divergence=self.args.accept_source_divergence)
        apply_decisions(plan, self.pairs)
        self.plan, self.integ = plan, integ
        return Binding(tips=((f"refs/heads/{branch}", tip or ""),), digest=plan.digest,
                       managed_tree=None, accepted_snapshot=self.args.accept_source_divergence,
                       changed_paths=())


@contextmanager
def nullcontext_():
    yield


def _pull_state_refusal(plan: PullPlan, *, planning: bool) -> int | None:
    """§9 pull columns for states pull cannot integrate as asked."""
    if plan.state == "S10a":
        raise Diverged([], "ERROR: your source checkout is behind the snapshot on public; "
                       "update it, then pull")
    if plan.state.startswith("S10b") and not plan.accepted_divergence:
        raise DecisionRequired(
            "ERROR: a snapshot on public came from a source commit that diverged from this "
            "checkout.\nIf it is yours, pass "
            f"--accept-source-divergence={plan.ip or '<snapshot sha>'} (see `{_PROG} status`).")
    if plan.state == "S7" and not plan.accept_rewritten:
        message = ("ERROR: public history was rewritten; these were on public and are not now:\n  "
                   + "\n  ".join(plan.missing_snapshots or ["(a previously pulled commit)"])
                   + "\nReview the plan, then pass --accept-rewritten-history.")
        if planning:
            return EXIT_DIVERGED
        raise DecisionRequired(message)
    return None


def cmd_pull_plan(source_root: Path, config: dict, args) -> int:
    """`pull --plan` (and the `integrate` alias): show the plan and its
    digest, write nothing but the plan file. Exit 0 with nothing to decide,
    5 with items or a range (§5.4), 8 in S4 (shows the in-progress plan)."""
    _require_initialized(source_root, config)
    rec = pull_in_progress(source_root)
    if rec is not None and rec.worktree == _worktree_toplevel(source_root):
        path = _plans_dir(source_root) / f"{rec.digest}.json"
        if path.exists():
            print(render_plan(_plan_from_json(json.loads(path.read_text()))))
        ui.error(f"A pull is in progress in this worktree: `{_PROG} pull --continue` or "
                 f"`{_PROG} pull --abort`.")
        return EXIT_DECISION_REQUIRED
    assert_no_pull_in_progress(source_root, command="pull --plan")
    run = _PullRun(source_root, config, args)
    check_shallow(source_root, run.publish_dir)
    _staleness_warning(source_root)
    with source_lock(source_root, exclusive=False):
        run.compute()
    plan = run.plan
    save_plan_file(source_root, plan)
    print(render_plan(plan, json_out=bool(getattr(args, "json_output", False))))
    refusal = _pull_state_refusal(plan, planning=True) if plan.state != "S7" else None
    if plan.state == "S7" and not plan.accept_rewritten:
        return EXIT_DIVERGED
    if refusal is not None:
        return refusal
    if plan.state == "S12" or plan.items or plan.unpulled:
        return EXIT_DIVERGED
    return EXIT_SUCCESS


def _restore_paths(source_root: Path, rec: InProgress) -> None:
    for path, blob in sorted(rec.kept.items()):          # keep writes in the publish clone
        target = Path(rec.publish_dir) / path
        if blob is None:
            with suppress(FileNotFoundError):
                target.unlink()
        else:
            target.write_bytes(_cat_blob(Path(rec.publish_dir), None, blob))
    for path, blob in sorted(rec.written.items()):
        if blob is None:
            _stage_delete(source_root, path)
        else:
            mode = (git("ls-tree", "HEAD", "--", path, cwd=source_root).stdout.split() or
                    ["100644"])[0]
            git("update-index", "--add", "--cacheinfo",
                f"{mode},{blob},{_source_prefix(source_root)}{path}", cwd=source_root)
            git("checkout-index", "-f", "--", path, cwd=source_root)


def _cmd_pull_abort(source_root: Path, args) -> int:
    rec = pull_in_progress(source_root)
    assert_no_pull_in_progress(source_root, command="pull --abort")
    if rec is not None and rec.worktree != _worktree_toplevel(source_root):
        return abort_orphaned_pull(source_root, yes=bool(args.yes))
    with source_lock(source_root, exclusive=True):
        rec = pull_in_progress(source_root)
        _restore_paths(source_root, rec)
        clear_in_progress(source_root)
    ui.success(f"Pull aborted; restored {len(rec.written) + len(rec.kept)} path(s).")
    return EXIT_SUCCESS


def _cmd_pull_continue(source_root: Path, config: dict, args) -> int:
    assert_no_pull_in_progress(source_root, command="pull --continue")
    rec = pull_in_progress(source_root)
    plan_file = _plans_dir(source_root) / f"{rec.digest}.json"
    plan = _plan_from_json(json.loads(plan_file.read_text(encoding="utf-8")))
    remaining = []
    resolved: dict[str, str] = {}
    for path in sorted(rec.written):
        target = Path(source_root) / path
        if not target.exists():
            continue
        text = target.read_bytes().decode("utf-8", errors="replace")
        if has_marker_triple(text):
            remaining.append(path)
    if remaining:
        raise DecisionRequired("ERROR: conflict markers remain in: " + ", ".join(remaining)
                               + f"\nResolve them, then run `{_PROG} pull --continue`.")
    publish_dir = Path(source_root) / config.get("dir", PUBLISH_DIR_NAME)
    for item in plan.items:
        if item.path not in rec.written:
            continue
        target = Path(source_root) / item.path
        if not target.exists():
            continue
        blob = git("hash-object", "-w", f"--path={item.path}", str(target),
                   cwd=source_root).stdout.strip()
        expected = item.result if item.cls in ("C1", "C2", "C7") else item.theirs
        if item.cls in ("C2", "C7") or blob != expected:
            resolved[item.path] = blob     # edited during the pull → `resolved` (§5.1 item 5)
    if not args.yes:
        if not is_interactive():
            raise ConfigError("ERROR: `pull --continue` off a terminal needs --yes")
        for path, blob in resolved.items():
            print(f"  resolved {path} → {blob[:12]}", file=sys.stderr)
        print("Commit this pull? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            answer = input()
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            raise ConfigError("Cancelled; nothing was written.")
    with source_lock(source_root, exclusive=True):
        for path, blob in resolved.items():
            mode = (git("ls-files", "-s", "--", path, cwd=source_root).stdout.split() or
                    ["100644"])[0]
            git("update-index", "--add", "--cacheinfo",
                f"{mode},{blob},{_source_prefix(source_root)}{path}", cwd=source_root)
        # The record names the config relative to the source root, or null
        # when it is outside the tree, as the direct path does.
        cfg = Path(rec.config_path).resolve()
        root = Path(source_root).resolve()
        rel_config = cfg.relative_to(root).as_posix() if cfg.is_relative_to(root) else None
        commit = commit_pull_record(plan, source_root, publish_dir, config_path=rel_config,
                                    plan_file=plan_file, resolved=resolved)
        clear_in_progress(source_root)
    ui.success(f"Pulled through {plan.tip[:12]} as {commit[:12]}.")
    return EXIT_SUCCESS


def cmd_pull(source_root: Path, config: dict, args) -> int:
    """`pubrepo pull` (design §5). Exit codes follow §9: --plan 0/5; off a
    TTY without --yes 8 when a decision is missing, else 1; --yes needs --expect-tip and --expect-plan (1), a moved
    tip is 5, a changed digest 1, any undecided item 1; text conflicts 8."""
    if args.abort:                       # restores source only; needs no publish clone
        return _cmd_pull_abort(source_root, args)
    _require_initialized(source_root, config)
    if args.continue_:
        return _cmd_pull_continue(source_root, config, args)
    if args.plan:
        return cmd_pull_plan(source_root, config, args)
    if args.yes and not (args.expect_tip and args.expect_plan):
        raise ConfigError("ERROR: `pull --yes` needs --expect-tip and --expect-plan from "
                          f"`{_PROG} pull --plan`")
    assert_no_pull_in_progress(source_root, command="pull")
    run = _PullRun(source_root, config, args)
    check_shallow(source_root, run.publish_dir)
    _staleness_warning(source_root)
    interactive = is_interactive() and not args.yes

    if not args.yes:
        with source_lock(source_root, exclusive=False):
            run.compute()
        plan = run.plan
        _pull_state_refusal(plan, planning=False)
        if not plan.unpulled and not plan.items:
            ui.success("Nothing to pull.")
            return EXIT_SUCCESS
        print(render_plan(plan))
        missing = [i for i in plan.items if i.chosen is None]
        if missing or not interactive:
            needs = [i for i in plan.items if i.chosen is None or i.needs_explicit()]
            if missing:
                # The example is an option the named item offers;
                # `resolved` is for edited conflict markers (pull --continue).
                first = missing[0]
                offered = [o for o in first.options if o != "resolved"]
                example = (f" (e.g. --{offered[0]} {shlex.quote(first.path)})"
                           if offered else "")
                listing = f"{_PROG} pull --plan" + (
                    " --baseline" if any(i.origin == "baseline" for i in missing) else "")
                raise DecisionRequired(
                    "ERROR: decisions required for: " + ", ".join(i.path for i in missing)
                    + f"\nPass one decision flag per path{example} and run again."
                    + f"\nEach item's options are listed by: {listing}")
            raise ConfigError(
                "ERROR: off a terminal, pull commits only a plan bound by digest:\n"
                f"  {_PROG} pull --yes --expect-tip {plan.tip} --expect-plan {plan.digest}"
                + "".join(f" --{i.chosen} {shlex.quote(i.path)}" for i in needs))

    def show_and_confirm(binding: Binding) -> bool:
        print("Commit this pull? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            return input().strip().lower() in ("y", "yes")
        except EOFError:
            return False

    expected = None
    if args.yes:
        expected = Binding(tips=((f"refs/heads/{config.get('branch', 'main')}", args.expect_tip),),
                           digest=args.expect_plan, managed_tree=None, accepted_snapshot=None,
                           changed_paths=(), required=frozenset({"tips", "digest"}))
    with confirm_then_lock(source_root, run.publish_dir, run.compute,
                           show_and_confirm if interactive else None, expected=expected):
        plan = run.plan
        _pull_state_refusal(plan, planning=False)
        if not plan.unpulled and not plan.items:
            ui.success("Nothing to pull.")
            return EXIT_SUCCESS
        undecided = [i for i in plan.items
                     if i.chosen is None or (args.yes and i.needs_explicit() and not i.decision)]
        blocking = [i for i in undecided if i.cls not in ("C2", "C7")]
        if blocking:
            raise ConfigError("ERROR: missing explicit choice for: "
                              + ", ".join(i.path for i in blocking) + "; nothing was written")
        plan_file = save_plan_file(source_root, plan)
        conflicts = write_pull(plan, source_root, run.publish_dir, config,
                               config_path=run.config_path)
        if conflicts:
            raise DecisionRequired(
                "Conflicts written with markers (nothing committed): " + ", ".join(conflicts)
                + f"\nResolve them, then `{_PROG} pull --continue` (or `--abort`).")
        commit = commit_pull_record(plan, source_root, run.publish_dir,
                                    config_path=run.config_relpath, plan_file=plan_file)
        clear_in_progress(source_root)
    for path in _explicit_include_removals(plan):
        ui.warn(f"NOTE: {path} is listed explicitly in include; remove it from "
                f"{plan.config_label} before the next publish")
    ui.success(f"Pulled {len(plan.unpulled)} public commit(s) through {plan.tip[:12]} "
               f"as {commit[:12]}.")
    return EXIT_SUCCESS


def pull_decisions_since(source_root: Path, public_sha: str | None, *,
                         destination: tuple[str, str]) -> list[Decision]:
    """Decisions recorded by `destination`'s pull records newer than the one
    that pulled through `public_sha` (for the release summary, §8.3). None:
    all (§2.3)."""
    out: list[Decision] = []
    for rec in pull_records(source_root, destination=destination, first_parent=True):
        if public_sha is not None and rec.through == public_sha:
            break
        out += [_decision_from_entry(e) for e in rec.entries]
    return out


# --- sync: inbound ---
# Design §5.2: checks on every incoming public blob before pull may
# write it into source. Each hit is a Flag on the pull item; a flagged item
# is never defaulted, and `allowed` (when set) restricts its decisions.

# §5.2 item 3, verbatim; the list is extensible and cannot be shrunk. Name
# patterns match the basename at any depth; directory patterns match a path
# component at any depth (over-matching only makes a decision explicit).
EXEC_BUILD_NAMES = (
    ".gitlab-ci*", "pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "Makefile",
    "GNUmakefile", "*.mk", "Justfile", "package.json", "Cargo.toml", "build.rs", "Dockerfile",
    "Containerfile", "*.sh",
    "*.ps1", "conftest.py", "*.pth", "tox.ini", "noxfile.py", ".pre-commit-config.yaml",
    ".envrc", "requirements*.txt", ".gitattributes", ".gitmodules", ".publish*.toml",
)
EXEC_BUILD_DIRS = (".github", "hooks", ".githooks")
EXEC_BUILD_PATHS = (".vscode/tasks.json",)

_SCRUB_KINDS = frozenset({"forbidden", "marker"})
_UNREADABLE_KINDS = frozenset({"unscannable", "corrupt", "limit", "cfb"})


@dataclass(frozen=True)
class Flag:
    """One inbound check hit (design §5.2). `allowed` restricts the item's
    decisions when not None (e.g. never `accept` for scrub hits)."""
    kind: str        # scrub, unreadable, cross_destination, exec_build, private,
                     # unsafe, type_change, reintroduced
    detail: str
    allowed: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Target:
    """A publish config known to this source (design §5.2 item 2)."""
    config_path: str                 # absolute
    config: dict
    destination: tuple[str, str]


@dataclass
class InboundCtx:
    """What inbound_flags needs; built once per plan by make_inbound_ctx."""
    source_root: Path
    publish_dir: Path
    config: dict
    config_path: str | None          # this config, absolute; None if unknown
    proj: Projection
    written_ever: Callable[[str], bool]
    targets: list[Target]
    source_paths: set[str]           # every path present in the source worktree/index
    plan_paths: set[str]             # every path in this pull plan
    repairs: list[dict]
    blob_sizes: dict[str, int]


def _load_target(path: Path) -> dict | None:
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8")).get("publish")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("remote"), str):
        return None
    data.setdefault("branch", "main")
    data.setdefault("exclude", [])
    data.setdefault("keep", [])
    data.setdefault("include", [])
    return data


def known_targets(source_root: Path, config: dict | None = None
                  ) -> tuple[list[Target], list[str]]:
    """(known targets, possible targets). Known: root-level
    `.publish.toml` / `.publish-*.toml`, configs registered in targets.json,
    and `[publish] peers`. Possible: every other in-tree `.publish*.toml`,
    listed as "possible targets (unregistered)" and never flagged."""
    root = Path(source_root).resolve()
    paths: list[Path] = sorted(root.glob(".publish*.toml"))
    paths += [Path(t["config_path"]) for t in registered_targets(source_root)
              if t.get("config_path")]
    paths += [root / p for p in (config or {}).get("peers", []) if isinstance(p, str)]
    targets, seen = [], set()
    for p in paths:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        cfg = _load_target(p)
        if cfg is not None:
            targets.append(Target(config_path=key, config=cfg,
                                  destination=destination_identity(cfg)))
    listed = git("ls-files", "-z", "--cached", "--others", "--exclude-standard", "--",
                 ":(glob)**/.publish*.toml", cwd=source_root).stdout.split("\0")
    possible = sorted({rel for rel in listed if rel and "/" in rel
                       and str((root / rel).resolve()) not in seen})
    return targets, possible


def is_exec_or_build(path: str, mode: str | None, config: dict) -> bool:
    """§5.2 item 3: executable bits, build or auto-execution content, and
    the release `version_file`. Names match case-insensitively (design
    §5.2 item 3): `makefile`, `DOCKERFILE` and `.GitHub/`
    run the same tools on case-insensitive filesystems."""
    if mode == "100755":
        return True
    folded = path.casefold()
    parts = folded.split("/")
    if any(fnmatch.fnmatchcase(parts[-1], pat.casefold()) for pat in EXEC_BUILD_NAMES):
        return True
    if any(part in {d.casefold() for d in EXEC_BUILD_DIRS} for part in parts[:-1]):
        return True
    paths = [p.casefold() for p in EXEC_BUILD_PATHS]
    if folded in paths or any(folded.endswith("/" + p) for p in paths):
        return True
    release = config.get("release")
    version_file = release.get("version_file") if isinstance(release, dict) else None
    return isinstance(version_file, str) and posixpath.normpath(version_file) == path


def is_private(path: str, ctx: InboundCtx) -> bool:
    """§5.2 item 4. Private paths are never read: existence checks only."""
    if ctx.written_ever(path):
        return False                                     # INV-3 (b) governs it
    if any(fnmatch.fnmatchcase(path, pat) or fnmatch.fnmatchcase(path, pat.rstrip("/") + "/*")
           for pat in ctx.config.get("private", []) or [] if isinstance(pat, str)):
        return True                                      # (c)
    src = Path(ctx.source_root) / path
    if os.path.lexists(src) and path not in ctx.proj.entries:
        return True                                      # (a)
    projected = list(ctx.proj.entries)
    parts = path.split("/")
    for i in range(1, len(parts)):
        directory = "/".join(parts[:i])
        if (Path(ctx.source_root) / directory).is_dir() and not any(
                p.startswith(directory + "/") for p in projected):
            return True                                  # (b)
    return False


def _unsafe(path: str, ctx: InboundCtx) -> str | None:
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return "absolute path"
    parts = path.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return "path escapes the repository"
    if any(p.casefold() == ".git" for p in parts):
        return "path inside .git"
    folded = path.casefold()
    others = (ctx.source_paths | ctx.plan_paths) - {path}
    clash = sorted(p for p in others if p.casefold() == folded)
    if clash:
        return f"case collision with {clash[0]}"
    return None


def _reintroduced_flag(path: str, blob: str, ctx: InboundCtx) -> str | None:
    """§5.2 item 7: a removed blob id (≥ 64 bytes, not the empty blob) at
    any path, or an exact (path, blob) pair. A path alone never matches."""
    for rep in ctx.repairs:
        pairs = {tuple(x) for x in rep.get("removed") or [] if isinstance(x, (list, tuple))}
        if (path, blob) in pairs:
            return f"removed by repair {rep['_digest'][:12]}"
        if (blob in (rep.get("removed_blobs") or []) and blob != EMPTY_BLOB
                and ctx.blob_sizes.get(blob, 0) >= S12_MIN_BLOB_SIZE):
            return f"content removed by repair {rep['_digest'][:12]}, re-added at a new path"
    return None


def make_inbound_ctx(source_root: Path, publish_dir: Path, config: dict, *,
                     config_path: str | None, proj: Projection,
                     integ: IntegrationState, plan_paths: Iterable[str],
                     incoming_blobs: Iterable[str]) -> InboundCtx:
    targets, _possible = known_targets(source_root, config)
    listed = git("ls-files", "-z", "--cached", "--others", cwd=source_root).stdout.split("\0")
    blobs = sorted({b for b in incoming_blobs if b})
    sizes: dict[str, int] = {}
    if blobs:
        out = _run_git_process(["git", "cat-file", "--batch-check=%(objectname) %(objectsize)"],
                             cwd=publish_dir, input="".join(f"{b}\n" for b in blobs),
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[1].isdigit():
                sizes[fields[0]] = int(fields[1])
    return InboundCtx(
        source_root=source_root, publish_dir=publish_dir, config=config,
        config_path=str(Path(config_path).resolve()) if config_path else None, proj=proj,
        written_ever=integ.written_ever, targets=targets,
        source_paths={p for p in listed if p}, plan_paths=set(plan_paths),
        repairs=_applied_repairs(source_root), blob_sizes=sizes)


def inbound_flags(item: PullItem, ctx: InboundCtx, *, mode: str | None = None,
                  old_mode: str | None = None) -> list[Flag]:
    """Every §5.2 check on the incoming side of `item` (its `theirs` blob,
    or the path itself for a deletion). `mode` is the incoming tree mode,
    `old_mode` the base mode (a type change is flagged)."""
    path, blob = item.path, item.theirs
    flags: list[Flag] = []
    unsafe = _unsafe(path, ctx)
    if unsafe:                                                       # item 5
        flags.append(Flag("unsafe", f"refused: {unsafe}", ("leave", "remove")))
    if mode and old_mode and mode != old_mode and {mode, old_mode} - {"100644", "100755"}:
        flags.append(Flag("type_change", f"type change {old_mode} → {mode}"))
    if mode in ("120000", "160000"):
        # On a path we project, the class offers `ours`; elsewhere
        # a symlink or gitlink is unmanaged by type: leave or remove.
        flags.append(Flag("type_change", "symlink" if mode == "120000" else "gitlink",
                          None if path in ctx.proj.entries else ("leave", "remove")))
    if blob is not None and mode not in ("120000", "160000"):
        result = gate_scan(ctx.publish_dir, [(path, blob)], ctx.config)
        for finding in result.blocking():
            if finding.kind in _SCRUB_KINDS:                         # item 1
                flags.append(Flag("scrub", f"would fail our outbound scrub "
                                  f"({finding.kind}{': ' + finding.part if finding.part else ''}); "
                                  f"the content is already public",
                                  ("decline", "leave", "remove")))
            elif finding.kind in _UNREADABLE_KINDS:                  # item 6
                flags.append(Flag("unreadable", f"{finding.detail}; to publish it anyway, add "
                                  f"`allow_opaque = [\"{path}\"]` to [publish.scrub]"))
        reason = _reintroduced_flag(path, blob, ctx)                 # item 7
        if reason:
            flags.append(Flag("reintroduced", reason, ("decline", "leave", "remove")))
    others = [t for t in ctx.targets if t.config_path != ctx.config_path
              and t.destination != destination_identity(ctx.config)]
    hit = [t for t in others if _path_would_publish(path, t.config)]  # item 2
    if hit:
        names = ", ".join(sorted(Path(t.config_path).name for t in hit))
        flags.append(Flag("cross_destination", f"also published by {names}",
                          ("accept", "accept-excluded", "decline", "leave")))
    if is_exec_or_build(path, mode, ctx.config):                     # item 3
        flags.append(Flag("exec_build", "executable, build or auto-execution content"))
    if is_private(path, ctx):                                        # item 4
        flags.append(Flag("private", "private source path; never written to source",
                          ("leave", "remove")))
    return flags


# --- sync: repair ---
# Design §6 (Phase 1): rewind, linear remove-path, restore-ancestry, and the
# release-retire record. Repair is the only general history rewrite: a
# named plan, a confirmation bound to it, leases pinned to the shown shas,
# two-phase records, and a result recomputed from the remote every run.

_REPAIR_RESULT_PREFIX = {0: "APPLIED", 4: "APPLIED, INCOMPLETE", 2: "NOT APPLIED"}


@dataclass
class RefScan:
    ref: str
    sha: str
    scanned: bool
    reaches: bool                   # reaches the target content before the repair
    treatment: str | None = None    # rewrite | delete | leave
    new_sha: str | None = None      # after the repair ("" = deleted)
    destroyed: list = field(default_factory=list)   # CommitInfo, for delete


@dataclass
class RepairPlan:
    op: str                          # rewind | remove-path | restore-ancestry
    branch: str
    tip: str
    new_tip: str
    target: str                      # commit, --from commit, or tag
    paths: list[str]
    rewritten: list                  # CommitInfo of commits rewritten or dropped
    snapshots: dict[str, bool]       # sha → ours-snapshot? for every listed commit
    signed: dict[str, bool]          # sha → signature lost by the rewrite?
    mapping: dict[str, str]          # old → new ("" = dropped)
    removed: list[tuple[str, str]]   # (path, blob) removed
    removed_blobs: list[str]
    refs: list[RefScan]
    authors: list[str]
    unscanned: list[str]
    digest: str = ""


def applied_repair_records(source_root: Path) -> list[dict]:
    return _applied_repairs(source_root)


def _commit_infos(publish_dir: Path, shas: list[str]) -> list:
    return [_commit_info(publish_dir, c) for c in
            (_tip_history_one(publish_dir, s) for s in shas)]


def _tip_history_one(publish_dir: Path, sha: str) -> _Commit:
    out = git("log", "-1", "--format=%H %P%n%an <%ae>%n%s%n%B", sha, cwd=publish_dir).stdout
    head, author, subject, body = (out.split("\n", 3) + ["", "", ""])[:4]
    ids = head.split()
    found = _SOURCE_TRAILER_RE.findall(body)
    return _Commit(sha=ids[0], parents=tuple(ids[1:]), author=author, subject=subject,
                   trailer=found[-1] if found else None)


def _signed(publish_dir: Path, sha: str) -> bool:
    raw = git("cat-file", "commit", sha, cwd=publish_dir).stdout
    return "\ngpgsig " in raw or raw.startswith("gpgsig ")


def _path_matches(path: str, targets: list[str]) -> bool:
    return any(path == t or path.startswith(t.rstrip("/") + "/") for t in targets)


def rewrite_linear(publish_dir: Path, base: str | None, tip: str, remove: list[str],
                   mapping: dict[str, str] | None = None, *, fetched_tip: str | None,
                   fetched: set[str]) -> dict[str, str]:
    """old → new for every commit from `base` (inclusive; None = the root)
    to `tip`, with `remove` paths dropped (design §6.1).
    Authors, committers, dates and messages are preserved. A merge in the
    range is a precondition refusal (exit 1). `mapping` carries shared
    commits across refs (--also-rewrite-ref). Before each commit-tree, its
    parents pass assert_parent_provenance (§18.13): `fetched_tip` is
    this run's fetch_tip result, `fetched` the shas this run fetched (the
    scanned public refs), and the commits rewritten so far are `created`."""
    mapping = dict(mapping or {})
    rng = f"{base}^..{tip}" if base and git("rev-parse", "-q", "--verify", f"{base}^",
                                             cwd=publish_dir, check=False).returncode == 0 \
        else tip
    lines = git("rev-list", "--reverse", "--parents", rng, cwd=publish_dir).stdout.splitlines()
    for line in lines:
        ids = line.split()
        sha, parents = ids[0], ids[1:]
        if sha in mapping:
            continue
        if len(parents) > 1:
            raise ConfigError(f"ERROR: merge-containing range ({sha[:12]} is a merge): "
                              f"Phase 2. Nothing was written.")
        fields = git("log", "-1", "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd",
                     "--date=raw", sha, cwd=publish_dir).stdout.rstrip("\n").split("\0")
        message = git("log", "-1", "--format=%B", sha, cwd=publish_dir).stdout
        index = Path(tempfile.mkstemp(dir=Path(publish_dir) / ".git", prefix="repair-idx-")[1])
        try:
            index.unlink()
            env = {**os.environ, "GIT_INDEX_FILE": str(index)}
            git("read-tree", sha, cwd=publish_dir, env=env)
            listed = git("ls-files", "-z", cwd=publish_dir, env=env).stdout.split("\0")
            gone = [p for p in listed if p and _path_matches(p, remove)]
            for p in gone:
                git("update-index", "--force-remove", "--", p, cwd=publish_dir, env=env)
            tree = git("write-tree", cwd=publish_dir, env=env).stdout.strip()
        finally:
            with suppress(FileNotFoundError):
                index.unlink()
            with suppress(FileNotFoundError):
                Path(str(index) + ".lock").unlink()
        new_parents = [mapping.get(p, p) for p in parents]
        cenv = {**os.environ, "GIT_AUTHOR_NAME": fields[0], "GIT_AUTHOR_EMAIL": fields[1],
                "GIT_AUTHOR_DATE": fields[2], "GIT_COMMITTER_NAME": fields[3],
                "GIT_COMMITTER_EMAIL": fields[4], "GIT_COMMITTER_DATE": fields[5]}
        assert_parent_provenance(publish_dir, new_parents, tip=fetched_tip, fetched=fetched,
                                 created={n for n in mapping.values() if n})
        args = ["commit-tree", tree]
        for p in new_parents:
            args += ["-p", p]
        new = _run_git_process(["git", *args], cwd=publish_dir, env=cenv, input=message,
                             capture_output=True, text=True, check=True).stdout.strip()
        mapping[sha] = new
    return mapping


def _scan_refs(publish_dir: Path, branch: str, globs: list[str] | None) -> tuple[dict[str, str], list[str]]:
    """{ref: sha} of every public ref (ls-remote; peeled tags excluded), and
    the refs left unscanned by --scan-refs."""
    r = _git_net(["ls-remote", "origin"], publish_dir, url="origin")
    if r is None or r.returncode != 0:
        raise PublishError("ERROR: cannot list the public refs (ls-remote origin): "
                           + (sanitize_provider_output(r.stderr.strip()) if r else "timed out"))
    refs = {}
    for line in r.stdout.splitlines():
        sha, _, name = line.partition("\t")
        if name == "HEAD" or name.endswith("^{}"):
            continue
        refs[name] = sha
    branch_ref = f"refs/heads/{branch}"
    unscanned = []
    if globs:
        for name in sorted(refs):
            if name != branch_ref and not any(fnmatch.fnmatchcase(name, g) for g in globs):
                unscanned.append(name)
    return refs, unscanned


def _fetch_scan(publish_dir: Path, refs: dict[str, str]) -> None:
    """Fetch every scanned ref into refs/pubrepo/scan/<n> in .publish
    (§6.3 item 4). `+` is allowed: repair is a marked rewrite."""
    specs = [f"+{name}:refs/pubrepo/scan/{n}" for n, name in enumerate(sorted(refs))]
    if not specs:
        return
    r = _git_net(["fetch", "--no-tags", "--no-write-fetch-head", "origin", *specs], publish_dir,
                 url="origin")
    if r is None or r.returncode != 0:
        raise PublishError("ERROR: cannot fetch public refs for the scan: "
                           + (sanitize_provider_output(r.stderr.strip()) if r else "timed out"))


def _reaching(publish_dir: Path, sha: str, *, commits: set[str], blobs: set[str]) -> bool:
    """Does history at `sha` reach any of `commits`, or any of `blobs`?"""
    if not sha:
        return False
    if commits:
        listed = set(git("rev-list", sha, cwd=publish_dir).stdout.split())
        if listed & commits:
            return True
    if blobs:
        for line in git("rev-list", "--objects", sha, cwd=publish_dir).stdout.splitlines():
            if line.split(" ", 1)[0] in blobs:
                return True
    return False


def _repair_digest(plan: RepairPlan) -> str:
    payload = {"version": __version__, "op": plan.op, "branch": plan.branch, "tip": plan.tip,
               "new_tip": plan.new_tip, "target": plan.target, "paths": plan.paths,
               "mapping": plan.mapping, "removed": plan.removed,
               "refs": [[r.ref, r.sha, r.scanned, r.reaches, r.treatment, r.new_sha]
                        for r in plan.refs], "unscanned": plan.unscanned}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# No flag gets repair past released history (--leave-reachable treats only
# other refs, and release-retire doesn't change the managed set), so the
# refusal points at the manual procedure instead of a command.
RELEASED_HISTORY_REFUSAL = ("repair never touches released history. To remove a leak from a "
                            "released version, see \"Leaks and released versions\" in "
                            "docs/releasing.md.")


def repair_plan(source_root: Path, publish_dir: Path, config: dict, args, *,
                config_relpath: str | None) -> RepairPlan:
    """The read-only plan (design §6.3). Precondition refusals exit 1 and
    write nothing."""
    branch = config.get("branch", "main")
    branch_ref = f"refs/heads/{branch}"
    tip = fetch_tip(publish_dir, branch)
    if tip is None:
        raise ConfigError(f"ERROR: the public branch {branch} does not exist; nothing to repair")
    op = args.repair_op
    released = managed_release_targets(source_root, publish_dir, config)
    refs, unscanned = _scan_refs(publish_dir, branch, getattr(args, "scan_refs", None))
    scanned = {n: s for n, s in refs.items() if n not in unscanned}
    _fetch_scan(publish_dir, scanned)
    paths: list[str] = []
    removed: list[tuple[str, str]] = []
    if op == "rewind":
        target = git("rev-parse", "--verify", f"{args.commit}^{{commit}}", cwd=publish_dir,
                     check=False).stdout.strip()
        if not target or not _is_ancestor(publish_dir, target, tip):
            raise ConfigError(f"ERROR: {args.commit} is not on public {branch}; nothing written")
        dropped = git("rev-list", tip, f"^{target}", cwd=publish_dir).stdout.split()
        mapping = {s: "" for s in dropped}
        new_tip = target
        for tag, peeled in released.items():
            if peeled in dropped:
                raise ConfigError(f"ERROR: rewinding would drop released ancestry ({tag}); "
                                  + RELEASED_HISTORY_REFUSAL)
        content_commits = set(dropped)
        content_blobs: set[str] = set()
    elif op == "remove-path":
        paths = [posixpath.normpath(p) for p in args.paths]
        start = git("rev-parse", "--verify", f"{args.from_commit}^{{commit}}", cwd=publish_dir,
                    check=False).stdout.strip()
        if not start or not _is_ancestor(publish_dir, start, tip):
            raise ConfigError(f"ERROR: --from {args.from_commit} is not on public {branch}")
        proj = build_projection(source_root, publish_dir, config)
        still = sorted(p for p in proj.entries if _path_matches(p, paths))
        if still:
            raise ConfigError("ERROR: the current projection still contains "
                              + ", ".join(still) + "; remove or exclude it in source first. "
                              "Nothing was written.")
        rng_shas = set(git("rev-list", tip, *([f"^{start}^"] if git(
            "rev-parse", "-q", "--verify", f"{start}^", cwd=publish_dir,
            check=False).returncode == 0 else []), cwd=publish_dir).stdout.split())
        for tag, peeled in released.items():
            if peeled in rng_shas:
                raise ConfigError(f"ERROR: the range rewrites released ancestry ({tag}); "
                                  + RELEASED_HISTORY_REFUSAL)
        for sha in sorted(rng_shas):
            for rel, (mode, blob) in _tree_map(publish_dir, sha).items():
                if _path_matches(rel, paths) and (rel, blob) not in removed:
                    removed.append((rel, blob))
        removed.sort()
        mapping = rewrite_linear(publish_dir, start, tip, paths, fetched_tip=tip,
                                 fetched=set(scanned.values()))
        mapping = {o: n for o, n in mapping.items() if o in rng_shas}
        new_tip = mapping.get(tip, tip)
        target = start
        content_commits = set()
        content_blobs = {b for _p, b in removed}
    else:
        raise AssertionError(op)

    plan_refs: list[RefScan] = []
    rewrite = set(getattr(args, "also_rewrite_ref", None) or [])
    delete = set(getattr(args, "also_delete_ref", None) or [])
    leave = set(getattr(args, "leave_reachable", None) or [])
    for name in sorted(refs):
        sha = refs[name]
        if name == branch_ref:
            continue
        is_scanned = name in scanned
        reaches = is_scanned and _reaching(publish_dir, sha, commits=content_commits,
                                          blobs=content_blobs)
        treatment = ("rewrite" if name in rewrite else "delete" if name in delete
                     else "leave" if name in leave else None)
        if name.startswith("refs/pull/") and treatment not in (None, "leave"):
            raise ConfigError(f"ERROR: {name} can only be --leave-reachable "
                              f"(the host owns pull refs); nothing was written")
        plan_refs.append(RefScan(ref=name, sha=sha, scanned=is_scanned, reaches=reaches,
                                 treatment=treatment))
    for name in rewrite | delete | leave:
        if name not in refs:
            raise ConfigError(f"ERROR: {name} is not a public ref; nothing was written")
    for scan in plan_refs:
        if scan.treatment == "rewrite":
            if op != "remove-path":
                raise ConfigError("ERROR: --also-rewrite-ref applies to remove-path only")
            mapping = rewrite_linear(publish_dir, target, scan.sha, paths, mapping,
                                     fetched_tip=tip, fetched=set(scanned.values()))
            scan.new_sha = mapping.get(scan.sha, scan.sha)
        elif scan.treatment == "delete":
            scan.new_sha = ""
    remaining = [new_tip] + [s.new_sha if s.treatment in ("rewrite",) else s.sha
                             for s in plan_refs if s.treatment != "delete"]
    for scan in plan_refs:
        if scan.treatment == "delete":
            destroyed = git("rev-list", scan.sha, "--not", *[r for r in remaining if r],
                            cwd=publish_dir).stdout.split()
            scan.destroyed = _commit_infos(publish_dir, destroyed)
    listed = [s for s in mapping if s]
    infos = _commit_infos(publish_dir, git("rev-list", "--no-walk=sorted", *listed,
                                           cwd=publish_dir).stdout.split()) if listed else []
    view = _IdentityView(source_root, publish_dir, config, None,
                         config_relpath=config_relpath, fetch_sigma=False)
    snapshots = {c.sha: view.identify(_tip_history_one(publish_dir, c.sha)).kind != "foreign"
                 for c in infos}
    authors = sorted({c.author for c in infos} | {c.author for s in plan_refs
                                                  for c in s.destroyed})
    signed = {c.sha: op != "rewind" and _signed(publish_dir, c.sha) for c in infos}
    plan = RepairPlan(op=op, branch=branch, tip=tip, new_tip=new_tip, target=target,
                      paths=paths, rewritten=infos, snapshots=snapshots, signed=signed,
                      mapping=mapping,
                      removed=removed, removed_blobs=sorted({b for _p, b in removed}),
                      refs=plan_refs, authors=authors, unscanned=unscanned)
    plan.digest = _repair_digest(plan)
    return plan


def render_repair_plan(plan: RepairPlan) -> str:
    """The eight §6.3 items, then the digest line."""
    lines = [f"Repair plan: {plan.op} on {plan.branch}",
             f"1. remote branch {plan.branch}, tip {plan.tip}",
             f"2. commits {'dropped' if plan.op == 'rewind' else 'rewritten'} "
             f"({len(plan.rewritten)}):"]
    for c in plan.rewritten:
        kind = "snapshot" if plan.snapshots.get(c.sha) else "foreign"
        sig = ", signature will be lost" if plan.signed.get(c.sha) else ""
        lines.append(f"   {c.sha[:12]} {c.author}: {c.subject} [{kind}{sig}]")
    lines.append("3. changed and removed paths: "
                 + (", ".join(sorted({p for p, _b in plan.removed})) or "(none; whole commits)"))
    lines.append("4. public refs reaching the target content:")
    for s in plan.refs:
        state = ("UNSCANNED" if not s.scanned else "reaches" if s.reaches else "clean")
        lines.append(f"   {s.ref} {s.sha[:12]} {state}")
    for name in plan.unscanned:
        lines.append(f"   {name} UNSCANNED (--scan-refs)")
    lines.append("5. treatments:")
    for s in plan.refs:
        if s.treatment or s.reaches:
            lines.append(f"   {s.ref}: {s.treatment or 'UNHANDLED (needs a treatment)'}")
            for c in s.destroyed:
                lines.append(f"      destroyed {c.sha[:12]} {c.author}: {c.subject}")
    lines.append("6. authors whose clones will diverge: " + (", ".join(plan.authors) or "(none)"))
    lines.append("7. Host protection: unknown until attempted.")
    lines.append(f"8. Plan digest: {plan.digest}")
    lines.append(f"Plan digest: {plan.digest}")
    return "\n".join(lines)


def _remote_refs(publish_dir: Path) -> dict[str, str] | None:
    r = _git_net(["ls-remote", "origin"], publish_dir, url="origin")
    if r is None or r.returncode != 0:
        return None
    return {name: sha for sha, _, name in (line.partition("\t") for line in r.stdout.splitlines())
            if name != "HEAD" and not name.endswith("^{}")}


def _updates(plan: RepairPlan) -> list[tuple[str, str, str]]:
    """(ref, shown sha, new sha or "" for a deletion); the branch last."""
    ups = [(s.ref, s.sha, s.new_sha) for s in plan.refs
           if s.treatment in ("rewrite", "delete") and s.new_sha != s.sha]
    return ups + [(f"refs/heads/{plan.branch}", plan.tip, plan.new_tip)]


def _push_updates(publish_dir: Path, plan: RepairPlan) -> list[tuple[str, str]]:
    """Push every update with leases pinned to the shown shas: atomic, or
    ref by ref (branch last, stop at the first refusal) when the host lacks
    atomic push (§6.4). Returns per-ref statuses; a stale lease is
    Diverged (5)."""
    ups = _updates(plan)
    def spec(new, ref):
        return f"{new}:{ref}" if new else f":{ref}"
    outcome = push_porcelain(publish_dir, [spec(n, r) for r, _o, n in ups],
                             leases={r: o for r, o, _n in ups}, atomic=True)
    if outcome.status == "atomic_unsupported":
        now = _remote_refs(publish_dir)
        if now is None:
            raise PublishError("ERROR: cannot re-check the public refs before the per-ref "
                               "fallback; nothing was pushed")
        moved = [r for r, o, _n in ups if now.get(r) != o]
        if moved:
            raise Diverged(moved, "ERROR: public refs moved since the plan: "
                           + ", ".join(moved) + "; nothing was pushed")
        per_ref: list[tuple[str, str]] = []
        for ref, old, new in ups:
            one = push_porcelain(publish_dir, [spec(new, ref)], leases={ref: old})
            per_ref += one.per_ref or [(ref, one.status)]
            if one.status != "ok":
                if one.status == "lease_stale" and not any(s == "ok" for _r, s in per_ref):
                    raise Diverged([ref], f"ERROR: {ref} moved since the plan; nothing landed")
                break
        return per_ref
    if outcome.status == "lease_stale":
        # Every ref is named. A collateral ref is neither landed nor
        # refused: git dropped it because another ref's lease failed.
        lines = "".join(f"\n  {r}: {s}" + (" (not pushed; the host did not refuse it)"
                                           if s == "atomic_collateral" else "")
                        for r, s in outcome.per_ref)
        raise Diverged([r for r, s in outcome.per_ref if s == "lease_stale"],
                       "ERROR: a public ref moved since the plan was shown; nothing was "
                       f"pushed:{lines}\nPlan again.")
    return outcome.per_ref or [(r, outcome.status) for r, _o, _n in ups]


def _landed(plan: RepairPlan, now: dict[str, str]) -> list[str]:
    return [ref for ref, _old, new in _updates(plan)
            if (now.get(ref) == new if new else ref not in now)]


def _still_reachable(publish_dir: Path, plan: RepairPlan, now: dict[str, str]) -> list[str]:
    """§6.7: a fresh scan of every public ref against the target content."""
    _fetch_scan(publish_dir, now)
    commits = {s for s, n in plan.mapping.items() if plan.op == "rewind"}
    blobs = set(plan.removed_blobs)
    return sorted(name for name, sha in now.items()
                  if _reaching(publish_dir, sha, commits=commits, blobs=blobs))


def _rekey_snapshots(source_root: Path, plan: RepairPlan) -> None:
    """§6.4: re-key recorded snapshots by addition; `dropped` for rewind."""
    records = {n.rsplit("/", 1)[1]: r for n, r in read_records(source_root,
                                                               "refs/pubrepo/snap/").items()}
    removed_paths = {p for p, _b in plan.removed}
    for old, new in sorted(plan.mapping.items()):
        rec = records.get(old)
        if rec is None:
            continue
        try:
            if not new:
                write_status_marker(source_root, old, "dropped",
                                    reason=f"repair {plan.digest} (rewind)")
                continue
            written = {p: v for p, v in (rec.get("written") or {}).items()
                       if not _path_matches(p, list(removed_paths))}
            dest = rec.get("destination") or {}
            write_snapshot_record(source_root, snapshot_sha=new,
                                  destination=(dest.get("remote"), dest.get("branch")),
                                  sigma_full=rec.get("source"), written=written,
                                  nonderivable=list(rec.get("nonderivable") or []),
                                  keep=list(rec.get("keep") or []),
                                  config_path=rec.get("config_path"),
                                  fingerprint=rec.get("fingerprint") or "")
            write_status_marker(source_root, old, "rewritten",
                                reason=f"repair {plan.digest}", new_sha=new)
        except RecordConflict as e:
            ui.warn(f"WARNING: {e}")


def _repair_reset_local(publish_dir: Path, branch: str, shown_tip: str, new_tip: str) -> None:
    """Move the publish clone's branch to the repaired tip before
    advance_local. The commits this repair
    rewrote or dropped are named in its plan and backed up under
    refs/pubrepo/repair/<ts>, so they are not "never published". Commits
    that genuinely never reached public (local-only, beyond the shown tip)
    are still named and saved to refs/pubrepo/local/<ts> (§4.10), as
    advance_local would. advance_local then finds the branch at the new tip
    and only refreshes the worktree, keeping keep files."""
    ref = f"refs/heads/{branch}"
    cwd = str(publish_dir)
    current = git("rev-parse", "--verify", "-q", ref, cwd=cwd, check=False).stdout.strip()
    if not current or current == new_tip:
        return
    local_only = git("rev-list", current, "--not", shown_tip, cwd=cwd).stdout.split()
    if local_only:
        backup = _backup_ref_name(publish_dir)
        git("update-ref", backup, current, "", cwd=cwd)
        listing = git("log", "--format=  %h %s", current, "--not", shown_tip,
                      cwd=cwd).stdout.rstrip()
        ui.warn(f"WARNING: {len(local_only)} local commit(s) in {Path(publish_dir).name}/ were "
                f"never published; saved to {backup}:\n{listing}")
    git("update-ref", ref, new_tip, current, cwd=cwd)


def _repair_payload(plan: RepairPlan) -> dict:
    return {"op": plan.op, "branch": plan.branch, "tip": plan.tip, "new_tip": plan.new_tip,
            "map": plan.mapping, "removed": [list(x) for x in plan.removed],
            "removed_blobs": plan.removed_blobs,
            "refs": [[r, o, n] for r, o, n in _updates(plan)]}


def _report_repair(publish_dir: Path, plan: RepairPlan, now: dict[str, str],
                   per_ref: list[tuple[str, str]], *, json_out: bool,
                   left: list[str]) -> int:
    landed = _landed(plan, now)
    still = _still_reachable(publish_dir, plan, now) if landed else []
    code = 2 if not landed else (4 if still else 0)
    result = {0: "applied", 4: "applied_incomplete", 2: "not_applied"}[code]
    for ref, status in per_ref:
        ui.info(f"  {ref}: {status}")
    if json_out:
        print(json.dumps({"result": result, "still_reachable": still,
                          "per_ref": [list(x) for x in per_ref], "digest": plan.digest},
                         sort_keys=True))
    prefix = _REPAIR_RESULT_PREFIX[code]
    if code == 4:
        ui.error(f"{prefix}: the content is still public via " + ", ".join(still)
                 + "; rotate any exposed secret.")
    elif code == 2:
        ui.error(f"{prefix}: content still public; rotate.")
    elif json_out:
        ui.info(f"{prefix}: no scanned public ref reaches the content.")   # stdout is JSON only
    else:
        ui.success(f"{prefix}: no scanned public ref reaches the content.")
    if landed:
        # With --json, stdout carries the JSON object only; the human blocks
        # go to stderr (the blind suite parses stdout as one JSON document).
        print(_recovery_block(plan), file=sys.stderr if json_out else sys.stdout)
    print(_cleanup_block(publish_dir), file=sys.stderr if json_out else sys.stdout)
    return code


def _recovery_block(plan: RepairPlan) -> str:
    """§6.6, for collaborators."""
    b = plan.branch
    return "\n".join([
        "Collaborator recovery:",
        f"  1. {b} moved {plan.tip[:12]} → {plan.new_tip[:12]}; affected commits: "
        + (", ".join(c.sha[:12] for c in plan.rewritten) or "(none)"),
        f"  2. back up first: git branch pre-repair-backup origin/{b}",
        f"  3. git fetch && git reset --keep origin/{b}",
        "  4. re-apply unpushed work with git rebase --onto; never re-push the old "
        "commits or merge the old history",
        "  5. delete the backup after re-applying",
        "  6. open PRs and forks still contain the old history; merging them reintroduces it",
        "  7. no host purge is promised; rotate any exposed secret"])


def _cleanup_block(publish_dir: Path) -> str:
    """§6.7: exact commands, each scoped to the publish clone."""
    d = shlex.quote(str(publish_dir))
    return "\n".join([
        "Local cleanup (removes the old content from this clone):",
        f"  git -C {d} for-each-ref --format='delete %(refname)' refs/pubrepo/repair/ "
        f"| git -C {d} update-ref --stdin",
        f"  git -C {d} for-each-ref --format='delete %(refname)' refs/pubrepo/scan/ "
        f"| git -C {d} update-ref --stdin",
        f"  git -C {d} reflog expire --expire=now --all",
        f"  git -C {d} gc --prune=now"])


def _apply_repair(source_root: Path, publish_dir: Path, config: dict, plan: RepairPlan, *,
                  json_out: bool) -> int:
    """Pre: locks held, plan re-verified. Two-phase records around the push."""
    unhandled = [s.ref for s in plan.refs if s.reaches and s.treatment is None]
    if unhandled:
        raise ConfigError("ERROR: public refs still reach the content and have no treatment: "
                          + ", ".join(unhandled) + "\nPass --also-rewrite-ref, --also-delete-ref "
                          "or --leave-reachable for each. Nothing was written.")
    backup = f"refs/pubrepo/repair/{time.strftime('%Y%m%dT%H%M%S')}"
    git("update-ref", backup, plan.tip, cwd=publish_dir)
    plans = Path(publish_dir) / ".git" / "pubrepo" / "repairs"
    plans.mkdir(parents=True, exist_ok=True)
    (plans / f"{plan.digest}.json").write_text(json.dumps(_repair_payload(plan), sort_keys=True))
    payload = _repair_payload(plan)
    try:
        write_record(source_root, f"refs/pubrepo/repairs/{plan.digest}/intent", payload)
    except RecordConflict as e:
        ui.warn(f"WARNING: {e}")
    if plan.op == "rewind":
        # §18.13: the rewind moves the branch to `new_tip`, which must be on
        # this run's fetched history (it is an ancestor of plan.tip).
        assert_parent_provenance(publish_dir, [plan.new_tip], tip=plan.tip, fetched=set(),
                                 created=set())
    build_push_plan(source_root, publish_dir, config, kind="repair", refs={
        "public": [ref for ref, _old, _new in _updates(plan)], "bookmark": False,
        "source_tag": None, "records": records_remote_name(source_root, config)})
    try:
        per_ref = _push_updates(publish_dir, plan)
    except Diverged:
        raise
    _test_fault("repair.after_push")
    now = _remote_refs(publish_dir)
    if now is None:
        raise PublishError("ERROR: the repair push outcome is unknown (cannot list public refs); "
                           "rerun the identical command to recompute it")
    return _finish_repair(source_root, publish_dir, config, plan, now, per_ref,
                          json_out=json_out)


def _finish_repair(source_root: Path, publish_dir: Path, config: dict, plan: RepairPlan,
                   now: dict[str, str], per_ref: list, *, json_out: bool) -> int:
    landed = _landed(plan, now)
    left = [s.ref for s in plan.refs if s.treatment == "leave"]
    if landed:
        try:
            write_record(source_root, f"refs/pubrepo/repairs/{plan.digest}/applied",
                         {**_repair_payload(plan), "left_reachable": left})
        except RecordConflict as e:
            ui.warn(f"WARNING: {e}")
        _rekey_snapshots(source_root, plan)
        if f"refs/heads/{plan.branch}" in landed:
            _repair_reset_local(publish_dir, plan.branch, plan.tip, plan.new_tip)
            advance_local(publish_dir, plan.branch, plan.new_tip, config.get("keep", []))
    else:
        reason = "; ".join(f"{r}: {s}" for r, s in per_ref) or "refused"
        try:
            write_record(source_root, f"refs/pubrepo/repairs/{plan.digest}/refused",
                         {**_repair_payload(plan), "reason": reason})
        except RecordConflict as e:
            ui.warn(f"WARNING: {e}")
    records_push_pending(source_root, config)
    return _report_repair(publish_dir, plan, now, per_ref, json_out=json_out, left=left)


def _repair_rerun(source_root: Path, publish_dir: Path, config: dict, args, *,
                  config_relpath: str | None) -> int | None:
    """With an intent for --expect-plan, the post-push
    tip means "reached": write `applied` if missing and report. The
    pre-push tip falls through to a normal (identical) plan. None = no
    intent, or not reached."""
    records = read_records(source_root, f"refs/pubrepo/repairs/{args.expect_plan}/")
    intent = records.get(f"refs/pubrepo/repairs/{args.expect_plan}/intent")
    if intent is None:
        return None
    branch = config.get("branch", "main")
    tip = fetch_tip(publish_dir, branch)
    if args.expect_tip not in (intent.get("tip"), intent.get("new_tip")):
        raise Diverged([], "ERROR: --expect-tip matches neither side of the recorded repair")
    if tip == intent.get("tip"):
        return None                                   # not reached: plan again
    if tip != intent.get("new_tip"):
        raise Diverged([tip or ""], "ERROR: the public tip moved since the repair was planned")
    now = _remote_refs(publish_dir)
    if now is None:
        raise PublishError("ERROR: cannot list public refs to recompute the repair result")
    plan = RepairPlan(op=intent["op"], branch=intent["branch"], tip=intent["tip"],
                      new_tip=intent["new_tip"], target="", paths=[], rewritten=[],
                      snapshots={}, signed={}, mapping=intent.get("map") or {},
                      removed=[tuple(x) for x in intent.get("removed") or []],
                      removed_blobs=intent.get("removed_blobs") or [],
                      refs=[RefScan(ref=r, sha=o, scanned=True, reaches=True,
                                    treatment="delete" if not n else "rewrite", new_sha=n)
                            for r, o, n in intent.get("refs") or []
                            if r != f"refs/heads/{intent['branch']}"],
                      authors=[], unscanned=[], digest=args.expect_plan)
    ui.info("Repair already pushed; recomputing the result.")
    return _finish_repair(source_root, publish_dir, config, plan, now, [],
                          json_out=bool(getattr(args, "json_output", False)))


def _typed_confirmation(prompt: str, expected: str) -> bool:
    print(f"{prompt}: ", end="", file=sys.stderr, flush=True)
    try:
        return input().strip() == expected
    except EOFError:
        return False


def _restore_preconditions(source_root: Path, publish_dir: Path, config: dict,
                           tip: str | None, *, config_relpath: str | None) -> None:
    """Restore-ancestry takes publish's §4.1 preconditions
    3–8 (tip == IP, no S7/S10/S11/S12, no items) and refuses with publish's
    §9 code, writing nothing: 5 "pull first", 8 for S9/S10b. (S4 is
    refused earlier by assert_no_pull_in_progress.)"""
    check_shallow(source_root, publish_dir)
    if tip is None:
        raise Diverged([], "ERROR: the public branch is missing; nothing to restore onto")
    proj = build_projection(source_root, publish_dir, config)
    integ = compute_integration(source_root, publish_dir, config, tip, proj,
                                accept_divergence=None, fetch_sigma=True,
                                config_relpath=config_relpath)
    if integ.state in ("S0", "S1"):
        return
    if integ.state in ("S9", "S10b", "S10b+S2"):
        raise DecisionRequired(f"ERROR: restore-ancestry needs an integrated public branch "
                               f"(state {integ.state}); run `{_PROG} pull` first. Nothing "
                               f"was written.")
    raise Diverged([c.sha for c in integ.unpulled],
                   f"ERROR: restore-ancestry needs an integrated public branch (state "
                   f"{integ.state}); run `{_PROG} pull` first. Nothing was written.")


def _restore_ancestry(source_root: Path, publish_dir: Path, config: dict, args, *,
                      config_relpath: str | None) -> int:
    """§6.1 restore-ancestry: a fast-forward merge snapshot (ours: frozen
    message, record with empty `written`), planned, gated, confirmed."""
    branch = config.get("branch", "main")
    tag = args.tag
    # AC100 (design §17.3): the confirming-flag refusals come before
    # any fetch, lock or write (managed_release_targets reads the remote).
    if args.apply and not args.yes and not is_interactive():
        raise ConfigError("ERROR: off a terminal, restore-ancestry --apply needs --yes "
                          "--expect-tip --expect-plan")
    if args.apply and args.yes and not (args.expect_plan and args.expect_tip):
        raise ConfigError("ERROR: --expect-tip and --expect-plan are required "
                          f"(from `{_PROG} repair restore-ancestry … --plan`)")
    check_publish_clone_origin(publish_dir, config)      # R5, before the first remote read
    targets = managed_release_targets(source_root, publish_dir, config)
    if tag not in targets:
        raise ConfigError(f"ERROR: {tag} is not a managed release tag")
    target = targets[tag]

    def compute() -> Binding:
        tip = fetch_tip(publish_dir, branch)
        if git("cat-file", "-e", f"{target}^{{commit}}", cwd=publish_dir, check=False).returncode:
            r = _git_net(["fetch", "--no-tags", "--no-write-fetch-head", "origin", target],
                         publish_dir, url="origin")
            if r is None or r.returncode != 0:
                raise PublishError(f"ERROR: the release target {target} of {tag} is missing "
                                   f"and cannot be fetched")
        commits = git("rev-list", target, *([f"^{tip}"] if tip else []),
                      cwd=publish_dir).stdout.split()
        entries = set()
        for sha in commits:
            fp = git("rev-parse", "-q", "--verify", f"{sha}^", cwd=publish_dir,
                     check=False).stdout.strip() or None
            for path, (_o, new, _s) in _delta(publish_dir, sha, fp).items():
                if new and not new.startswith("gitlink:"):
                    entries.add((path, new))
        state["tip"], state["commits"], state["entries"] = tip, commits, sorted(entries)
        payload = {"op": "restore-ancestry", "tag": tag, "target": target, "tip": tip,
                   "commits": commits}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        state["digest"] = digest
        return Binding(tips=((f"refs/heads/{branch}", tip or ""),), digest=digest,
                       managed_tree=None, accepted_snapshot=None, changed_paths=())

    state: dict = {}
    with source_lock(source_root, exclusive=False):
        compute()
        _restore_preconditions(source_root, publish_dir, config, state["tip"],
                               config_relpath=config_relpath)
    if state["tip"] and _is_ancestor(publish_dir, target, state["tip"]):
        ui.success(f"{tag} is already in public history; nothing to restore.")
        return EXIT_SUCCESS
    print(f"restore-ancestry {tag} → {target[:12]}")
    print(f"These commits were removed from public by someone else; restoring "
          f"re-publishes them ({len(state['commits'])}):")
    for c in _commit_infos(publish_dir, state["commits"]):
        print(f"  {c.sha[:12]} {c.author}: {c.subject}")
    hits = gate_scan(publish_dir, state["entries"], config).blocking()
    repaired = {b for rep in _applied_repairs(source_root) for b in rep.get("removed_blobs") or []}
    reintro = [p for p, b in state["entries"] if b in repaired]
    print(f"Plan digest: {state['digest']}")
    if hits or reintro:
        for h in hits:
            ui.error(f"  gate: {h.path}{'!' + h.part if h.part else ''}: {h.kind}")
        for p in reintro:
            ui.error(f"  repaired content: {p}")
        ui.error(f"Refused: restoring would re-publish blocked content. Consider "
                 f"`{_PROG} release-retire {tag}`.")
        return EXIT_SCRUB_FAILED
    if not args.apply:
        return EXIT_SUCCESS
    expected = None
    show = None
    if args.yes:
        expected = Binding(tips=((f"refs/heads/{branch}", args.expect_tip),),
                           digest=args.expect_plan, managed_tree=None, accepted_snapshot=None,
                           changed_paths=(), required=frozenset({"tips", "digest"}))
    else:
        show = lambda b: _typed_confirmation(f"Type {tag} to restore its ancestry", tag)
    with confirm_then_lock(source_root, publish_dir, compute, show, expected=expected):
        tip = state["tip"]
        head = _source_head(source_root)
        message = build_commit_message(head[:7], custom_message=f"restore ancestry of {tag}")
        tree = git("rev-parse", f"{tip}^{{tree}}", cwd=publish_dir).stdout.strip()
        # §18.13: R (`target`) comes from this run's ledger (ls-remote) and,
        # when missing, a fetch by id, never a local tag.
        assert_parent_provenance(publish_dir, [tip, target], tip=tip, fetched={target},
                                 created=set())
        merge = snapshot_commit(publish_dir, tree, [tip, target], message)
        build_push_plan(source_root, publish_dir, config, kind="restore_ancestry", refs={
            "public": [f"refs/heads/{branch}"], "bookmark": False, "source_tag": None,
            "records": records_remote_name(source_root, config)})
        outcome = push_porcelain(publish_dir, [f"{merge}:refs/heads/{branch}"])
        if outcome.status in ("rejected_nonff", "lease_stale"):
            raise Diverged([tip], "ERROR: public branch moved; nothing restored. Plan again.")
        if outcome.status in ("host_refused", "ambiguous_absent"):
            raise PublishError(f"ERROR: host refused the push: {outcome.reason}")
        write_snapshot_record(source_root, snapshot_sha=merge,
                              destination=destination_identity(config), sigma_full=head,
                              written={}, nonderivable=[], keep=list(config.get("keep", [])),
                              config_path=config_relpath, fingerprint=config_fingerprint(config))
        advance_local(publish_dir, branch, merge, config.get("keep", []))
    records_push_pending(source_root, config)
    ui.success(f"Restored the ancestry of {tag} with {merge[:12]}.")
    return EXIT_SUCCESS


def cmd_repair(source_root: Path, config: dict, args) -> int:
    """`pubrepo repair` (design §6). Exit: 0 applied and unreachable; 4
    applied but still reachable (or restore-ancestry gate hit); 2 not
    applied; 1 missing confirmation, digest mismatch or a precondition; 5
    a tip moved; 8 pull in progress."""
    assert_no_pull_in_progress(source_root, command="repair")
    publish_dir = Path(source_root) / config.get("dir", PUBLISH_DIR_NAME)
    config_relpath, _abs = _pull_config_relpath(source_root, args)
    if args.repair_op == "restore-ancestry":
        return _restore_ancestry(source_root, publish_dir, config, args,
                                 config_relpath=config_relpath)
    if args.apply and not args.yes and not is_interactive():
        raise ConfigError("ERROR: off a terminal, repair --apply needs --yes --expect-tip "
                          "--expect-plan")
    if args.apply and args.yes and not (args.expect_tip and args.expect_plan):
        raise ConfigError("ERROR: --expect-tip and --expect-plan are required "
                          f"(from `{_PROG} repair … --plan`)")
    json_out = bool(getattr(args, "json_output", False))
    # R5 (§18.11): before the first fetch (the rerun check and
    # repair_plan both fetch); after the AC100 refusals, which fetch nothing.
    check_publish_clone_origin(publish_dir, config)
    if args.apply and args.yes:
        rerun = _repair_rerun(source_root, publish_dir, config, args,
                              config_relpath=config_relpath)
        if rerun is not None:
            return rerun
    holder: dict = {}

    def compute() -> Binding:
        plan = repair_plan(source_root, publish_dir, config, args, config_relpath=config_relpath)
        holder["plan"] = plan
        tips = ((f"refs/heads/{plan.branch}", plan.tip),) + tuple(
            (s.ref, s.sha) for s in plan.refs)
        return Binding(tips=tips, digest=plan.digest, managed_tree=None,
                       accepted_snapshot=None, changed_paths=())

    if not args.apply:
        with source_lock(source_root, exclusive=False):
            compute()
        print(render_repair_plan(holder["plan"]))
        return EXIT_SUCCESS
    expected = None
    show = None
    if args.yes:
        expected = Binding(tips=((f"refs/heads/{config.get('branch', 'main')}", args.expect_tip),),
                           digest=args.expect_plan, managed_tree=None, accepted_snapshot=None,
                           changed_paths=(), required=frozenset({"tips", "digest"}))
    else:
        def show(binding: Binding) -> bool:
            # The typed branch-and-count confirmation is repair's one prompt
            # (design §17.3); off a TTY, --yes --expect-tip --expect-plan (§6).
            plan = holder["plan"]
            print(render_repair_plan(plan))
            count = len(plan.rewritten) + sum(len(s.destroyed) for s in plan.refs)
            return (_typed_confirmation(f"Type the branch name ({plan.branch})", plan.branch)
                    and _typed_confirmation("Type the number of rewritten, dropped or "
                                            "destroyed commits", str(count)))
    with confirm_then_lock(source_root, publish_dir, compute, show, expected=expected):
        return _apply_repair(source_root, publish_dir, config, holder["plan"], json_out=json_out)


def cmd_release_retire(source_root: Path, config: dict, tag: str, *, yes: bool) -> int:
    """`pubrepo release-retire TAG`: a confirmed source-side record (§6.1).
    Exit 0; 1 without confirmation; 7 when TAG is not a managed release
    tag."""
    assert_no_pull_in_progress(source_root, command="release-retire")
    publish_dir = Path(source_root) / config.get("dir", PUBLISH_DIR_NAME)
    if tag not in managed_release_targets(source_root, publish_dir, config):
        raise ReleaseError(f"ERROR: {tag} is not a managed release tag; nothing to retire")
    if is_retired(source_root, tag):
        ui.success(f"{tag} is already retired.")
        return EXIT_SUCCESS
    if not yes:
        if not is_interactive():
            raise ConfigError("ERROR: off a terminal, release-retire needs --yes")
        if not _typed_confirmation(f"Type {tag} to retire it", tag):
            raise ConfigError("Cancelled; nothing was written.")
    with source_lock(source_root, exclusive=True):
        write_retired(source_root, tag, f"retired by the owner")
    records_push_pending(source_root, config)
    ui.success(f"Retired {tag}: releases no longer require it.")
    return EXIT_SUCCESS


# ===== sync: END =====


KNOWN_COMMANDS = ("publish", "init", "status", "validate", "integrate", "release-abandon",
                  "pull", "repair", "release-retire")

# One repeatable `--<decision> PATH` flag per pull decision (design §5.3, §5.4).
PULL_DECISIONS = ("accept", "decline", "ours", "theirs", "keep", "delete", "restore",
                  "resolved", "remove", "leave", "accept-excluded")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", "-q", action="store_true",
                        help="Errors only; success is silent (the exit code is the answer)")
    common.add_argument("--verbose", "-v", action="store_true",
                        help="Per-file decisions and per-phase timing")
    common.add_argument("--no-color", action="store_true",
                        help="Disable ANSI color (NO_COLOR is also honored)")
    common.add_argument("--config", metavar="PATH",
                        help=f"Config file (default ./{CONFIG_FILE_NAME}); in-tree "
                             f"configs must be named {CONFIG_FILE_NAME} or "
                             f"{PUBLISH_DIR_NAME}-<name>.toml")

    # Identify as invoked (see _PROG): set before any dispatch so every
    # hint message downstream names the command the user actually has.
    global _PROG
    _PROG = _derive_prog(sys.argv[0])
    prog = _PROG
    parser = argparse.ArgumentParser(
        prog=prog,
        description="Publish a curated subset of files to a clean git repo",
        parents=[common],
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_publish = sub.add_parser("publish", parents=[common],
                               help="Full publish cycle (confirms on a terminal; --yes otherwise)")
    p_publish.add_argument("-m", "--message", help="Custom commit message")
    p_publish.add_argument("--dry-run", "-n", action="store_true",
                           help="Show what would be published without touching "
                                ".publish/; exits with the code publish would "
                                "(0, 4, 5 or 8; offline: from the last-known remote state)")
    p_publish.add_argument("--force-overwrite", action="store_true",
                           help="Retired in 1.1.0: exits 1. Use pull for public "
                                "changes, or repair")
    p_publish.add_argument("--json", action="store_true", dest="json_output",
                           help="Machine-readable output (with --dry-run or --stage)")
    p_publish.add_argument("--diff", action="store_true",
                           help="Show a unified diff of what would change on the "
                                "public repo (implies --dry-run; never publishes)")
    p_publish.add_argument("--stage", action="store_true",
                           help="Build the snapshot publish would commit and write it to "
                                ".publish/, report changes, then stop — no commit, no push. "
                                "Built files stay for inspection")
    p_publish.add_argument("--amend", action=argparse.BooleanOptionalAction, default=None,
                           help="Rewrite your own last snapshot instead of adding a new one "
                                "(asks for confirmation; --yes off a terminal; pushes with "
                                "--force-with-lease). --no-amend is a no-op")
    p_publish.add_argument("--yes", action="store_true",
                           help="Publish without the confirmation prompt (required off a terminal)")
    p_publish.add_argument("--expect-tip", metavar="SHA",
                           help="Refuse unless the public tip is SHA when fetched")
    release_group = p_publish.add_mutually_exclusive_group()
    release_group.add_argument("--release", dest="release_version", metavar="VERSION",
                               help="Formally release this exact strict SemVer version")
    release_group.add_argument("--bump", choices=("patch", "minor", "major"),
                               help="Formally release the next semantic version")
    release_group.add_argument("--no-release", action="store_true",
                               help="Explicitly publish without a formal release")
    p_publish.add_argument("--remote", action="store_true",
                           help="Previews: fetch the public tip first (default: last fetched)")
    p_publish.add_argument("--accept-source-divergence", metavar="SHA",
                           help="Accept one diverged snapshot, named by its full sha "
                                "or its full source commit sha")
    p_publish.add_argument("--recreate-branch", action="store_true",
                           help="Recreate a deleted public branch as a root commit")
    p_publish.add_argument("--accept-build-changes", action="store_true",
                           help="Release: accept build files changed without a pull flag")
    p_publish.add_argument("--accept-unscanned", action="append", metavar="PATH",
                           help="Release: accept unscannable carried content at PATH (repeatable)")
    p_publish.add_argument("--retry", action="store_true",
                           help="Release: retry a superseded attempt (at most 3 laps)")

    sub.add_parser("init", parents=[common],
                   help="First-time setup: clone the remote into .publish/")

    p_status = sub.add_parser("status", parents=[common],
                              help="Show publish state and pending changes")
    p_status.add_argument("--json", action="store_true", dest="json_output",
                          help="Machine-readable output")
    p_status.add_argument("--check", action="store_true",
                          help="Exit 0 up to date / 4 outbound gate would block / "
                               "5 foreign state on public, pull needed / 6 our changes "
                               "pending / 8 decision required (precedence 4 > 8 > 5 > 6)")
    p_status.add_argument("--remote", action="store_true",
                          help="Also fetch and report unpulled public commits (network)")
    p_status.add_argument("--diff", action="store_true",
                          help="Show a unified diff of what would change on the "
                               "public repo")

    sub.add_parser("integrate", parents=[common],
                   help="Alias of `pull --plan`: show the pull plan and its digest")

    p_pull = sub.add_parser("pull", parents=[common],
                            help="Bring public-repo changes into source under guardrails")
    p_pull.add_argument("--plan", action="store_true",
                        help="Show the plan and its digest; write nothing")
    p_pull.add_argument("--yes", action="store_true",
                        help="Commit off a TTY (needs --expect-tip and --expect-plan)")
    p_pull.add_argument("--expect-tip", metavar="SHA", help="The public tip the plan covers")
    p_pull.add_argument("--expect-plan", metavar="DIGEST", help="The plan digest to bind to")
    p_pull.add_argument("--continue", action="store_true", dest="continue_",
                        help="Finish an in-progress pull")
    p_pull.add_argument("--abort", action="store_true",
                        help="Abandon an in-progress pull, restoring the paths it wrote")
    p_pull.add_argument("--baseline", action="store_true", help="Present the baseline items")
    p_pull.add_argument("--redecide", action="append", metavar="PATH",
                        help="Decide PATH again (repeatable)")
    p_pull.add_argument("--accept-rewritten-history", action="store_true",
                        help="Accept that public history was rewritten")
    p_pull.add_argument("--accept-source-divergence", metavar="SHA",
                        help="Accept one diverged snapshot, named by its full sha "
                             "or its full source commit sha")
    p_pull.add_argument("--decisions", metavar="FILE",
                        help="Decisions file: path<TAB>decision per line")
    for decision in PULL_DECISIONS:
        only = " (transformed files only)" if decision == "resolved" else ""
        p_pull.add_argument(f"--{decision}", action="append", metavar="PATH",
                            dest=f"decide_{decision.replace('-', '_')}",
                            help=f"Decide '{decision}' for PATH{only} (repeatable)")

    repair_opts = argparse.ArgumentParser(add_help=False)
    repair_opts.add_argument("--plan", action="store_true",
                             help="Show the plan and its digest; write nothing")
    repair_opts.add_argument("--apply", action="store_true", help="Apply the confirmed plan")
    repair_opts.add_argument("--yes", action="store_true", help="Confirm off a TTY")
    repair_opts.add_argument("--expect-tip", metavar="SHA", help="The public tip the plan covers")
    repair_opts.add_argument("--expect-plan", metavar="DIGEST", help="The plan digest to bind to")
    repair_opts.add_argument("--also-rewrite-ref", action="append", metavar="REF",
                             help="Apply the same rewrite to REF (linear ranges only)")
    repair_opts.add_argument("--also-delete-ref", action="append", metavar="REF",
                             help="Delete REF; the plan names every commit destroyed")
    repair_opts.add_argument("--leave-reachable", action="append", metavar="REF",
                             help="Leave REF reaching the content (result: exit 4)")
    repair_opts.add_argument("--scan-refs", action="append", metavar="GLOB",
                             help="Narrow the scanned public refs; the rest are listed unscanned")
    repair_opts.add_argument("--json", action="store_true", dest="json_output",
                             help="Machine-readable result")
    p_repair = sub.add_parser("repair", parents=[common],
                              help="Planned, confirmed rewrite of public history")
    repair_ops = p_repair.add_subparsers(dest="repair_op", required=True)
    p_rewind = repair_ops.add_parser("rewind", parents=[common, repair_opts],
                                     help="Move the branch back to a public commit")
    p_rewind.add_argument("commit", metavar="COMMIT")
    p_remove = repair_ops.add_parser("remove-path", parents=[common, repair_opts],
                                     help="Remove paths from every commit from COMMIT on")
    p_remove.add_argument("paths", metavar="PATH", nargs="+")
    p_remove.add_argument("--from", dest="from_commit", metavar="COMMIT", required=True)
    p_restore = repair_ops.add_parser("restore-ancestry", parents=[common, repair_opts],
                                      help="Make a dropped release target reachable again")
    p_restore.add_argument("tag", metavar="TAG")

    p_retire = sub.add_parser("release-retire", parents=[common],
                              help="Record a managed release tag as retired")
    p_retire.add_argument("tag", metavar="TAG")
    p_retire.add_argument("--yes", action="store_true", help="Confirm non-interactively")

    p_abandon = sub.add_parser(
        "release-abandon", parents=[common],
        help="Mark a local incomplete release journal abandoned (never mutates remotes)")
    p_abandon.add_argument("version", metavar="VERSION")
    p_abandon.add_argument("--yes", action="store_true",
                           help="Confirm non-interactively after reviewing consequences")

    p_validate = sub.add_parser("validate", parents=[common],
                                help="Validate .publish.toml and the environment")
    p_validate.add_argument("--json", action="store_true", dest="json_output",
                            help="Machine-readable output")
    p_validate.add_argument("--release-check", action="store_true",
                            help="Run read-only live release capability and identity checks")
    p_validate.add_argument("--remote", action="store_true",
                            help="Also list private refs (published/*, refs/pubrepo/*) on the "
                                 "publish remotes and on unlisted source remotes (read-only)")
    return parser


# --- Test sandbox, product side (design §17.4 c) ---
# Active only when PUBREPO_TEST_SANDBOX is set (the test suite sets it). It can
# only refuse; with the variable unset nothing changes.
TEST_SANDBOX_VAR = "PUBREPO_TEST_SANDBOX"
# Exact URL strings a test registered with the
# sandbox_unreachable fixture, which only sets this for the test's duration.
# They pass check_test_sandbox's CLI-entry scan only; the network wrapper
# (check_test_sandbox_url, _guard_remote) still refuses them. Always empty
# outside the test suite.
_TEST_SANDBOX_UNREACHABLE: frozenset[str] = frozenset()
_CONFIG_REMOTE_URL = re.compile(r"^\s*(?:push)?url\s*=\s*(.+?)\s*$", re.M)
_CONFIG_URL_REWRITE = re.compile(r"^\s*\[url\s+\"(.+?)\"\]", re.M)   # insteadOf targets


def _git_config_path(repo: Path) -> Path | None:
    """The config file of the repository at `repo`, found without running git."""
    dot_git = repo / ".git"
    if dot_git.is_file():
        dot_git = (repo / dot_git.read_text().split(":", 1)[1].strip()).resolve()
    if not dot_git.is_dir():
        return None
    common = dot_git / "commondir"
    if common.is_file():
        dot_git = (dot_git / common.read_text().strip()).resolve()
    config = dot_git / "config"
    return config if config.is_file() else None


def _local_remote_path(value: str, relative_to: Path) -> Path | None:
    """The local path a remote value names, or None for a network remote
    (a URL with a scheme other than file://, user@host:path, or host:path).
    A bare word such as a DISABLED-… lock value is a relative local path."""
    if value.startswith("file://"):
        return Path(urllib.parse.unquote(urllib.parse.urlparse(value).path))
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I) or re.match(r"^[\w.-]+@[\w.-]+:", value):
        return None
    if re.match(r"^[\w.-]+:", value) and not value.startswith((".", "/")):
        return None
    return (relative_to / value).resolve()


def check_test_sandbox_url(url: str) -> None:
    """Design §17.4 (c) in the network wrapper.
    A no-op unless PUBREPO_TEST_SANDBOX is set; then a URL that is not a
    local path inside the sandbox base refuses with exit 2. Relative paths
    resolve against the current directory; callers pass absolute ones."""
    base_text = os.environ.get(TEST_SANDBOX_VAR)
    if not base_text:
        return
    base = Path(base_text).resolve()
    local = _local_remote_path(url, Path.cwd())
    if local is None or not (local.resolve() == base or base in local.resolve().parents):
        raise PublishError(f"ERROR: test sandbox: refusing remote {url} (outside {base})")


def _guard_remote(repo: Path, name: str = "origin") -> None:
    """check_test_sandbox_url for every URL git would use for remote `name`
    of `repo` (url and pushurl, relative paths made absolute). Runs
    immediately before a network git call; a no-op outside the sandbox."""
    if not os.environ.get(TEST_SANDBOX_VAR):
        return
    for key in ("url", "pushurl"):
        got = git("config", "--get-all", f"remote.{name}.{key}", cwd=str(repo), check=False)
        for value in got.stdout.split("\n"):
            value = value.strip()
            if value:
                local = _local_remote_path(value, repo)
                check_test_sandbox_url(str(local) if local is not None else value)


def check_test_sandbox(source_root: Path, config: dict) -> None:
    """Design §17.4 (c). Under PUBREPO_TEST_SANDBOX, refuse (exit 2) before
    any git call when the source root or publish dir lies outside the
    sandbox base, or when any configured or resolved remote is not a file://
    URL or local path inside it: `remote`, `records_remote`, the source
    repo's remotes and the publish clone's remotes."""
    base_text = os.environ.get(TEST_SANDBOX_VAR)
    if not base_text:
        return
    base = Path(base_text).resolve()

    def inside(path: Path) -> bool:
        path = path.resolve()
        return path == base or base in path.parents

    def refuse(what: str) -> None:
        raise PublishError(f"ERROR: test sandbox: refusing {what} (outside {base})")

    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    for label, path in (("source root", source_root), ("publish dir", publish_dir)):
        if not inside(path):
            refuse(f"{label} {path}")
    remotes = [("remote", config.get("remote"), source_root),
               ("records_remote", config.get("records_remote"), source_root)]
    for repo in (source_root, publish_dir):
        config_file = _git_config_path(repo)
        if config_file is not None:
            text = config_file.read_text(errors="replace")
            remotes += [(f"a remote of {repo}", url, repo)
                        for url in _CONFIG_REMOTE_URL.findall(text)]
            remotes += [(f"a url.<base>.insteadOf rewrite in {repo}", url, repo)
                        for url in _CONFIG_URL_REWRITE.findall(text)]
    for label, value, relative_to in remotes:
        if not isinstance(value, str) or not value or value in _TEST_SANDBOX_UNREACHABLE:
            continue
        local = _local_remote_path(value, relative_to)
        if local is None or not inside(local):
            refuse(f"{label} {value}")


def default_command(argv: list[str]) -> list[str]:
    """Design §17.3 (§10 item 17): no arguments runs `status`; arguments
    that start with a flag keep their publish meaning (`pubrepo --dry-run`);
    a known command is unchanged."""
    if not argv:
        return ["status"]
    if argv[0].startswith("-") and argv[0] not in ("-h", "--help", "--version"):
        return ["publish", *argv]
    return list(argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    bare = not argv
    argv = default_command(argv)

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on usage errors, but §8 reserves 2 for publish
        # failures — usage errors are config errors (1). 0 = --help/--version.
        return EXIT_SUCCESS if e.code == 0 else EXIT_CONFIG_ERROR

    ui.configure(quiet=args.quiet, verbose=args.verbose, no_color=args.no_color)
    if bare:
        ui.info(f"To publish: `{_PROG} publish`")

    source_root = Path.cwd()
    config_path = Path(args.config) if args.config else source_root / CONFIG_FILE_NAME
    if not config_path.is_absolute():
        config_path = source_root / config_path

    try:
        if args.command == "publish" and args.force_overwrite:
            # refused before anything else runs, so zero git calls (AC63).
            raise ConfigError(FORCE_OVERWRITE_RETIRED.format(prog=_PROG))
        resolved_cfg = config_path.resolve()
        if resolved_cfg.is_relative_to(source_root.resolve()):
            name = resolved_cfg.name
            min_len = len(PUBLISH_DIR_NAME) + 1 + len(".toml")
            if not (name == CONFIG_FILE_NAME
                    or (name.startswith(PUBLISH_DIR_NAME + "-")
                        and name.endswith(".toml") and len(name) > min_len)):
                raise ConfigError(
                    f"ERROR: in-tree config '{name}' must be named {CONFIG_FILE_NAME} "
                    f"or {PUBLISH_DIR_NAME}-<name>.toml — other names are not "
                    f"hard-excluded and would publish the forbidden list to "
                    f"sibling targets")
        config = load_publish_config(config_path,
                                     strict=(args.command != "validate"))
        check_test_sandbox(source_root, config)        # §17.4 (c): before any git call
        configure_redaction(config)                    # strip_env values too
        config_relpath = (resolved_cfg.relative_to(source_root.resolve()).as_posix()
                          if resolved_cfg.is_relative_to(source_root.resolve()) else None)
        config_label = config_relpath or str(resolved_cfg)   # names the config in hints

        if args.command == "publish":
            # Skeleton: the 1.1.0 publish flags parse; their behavior
            # lands in the owning WP. Without them, publish is unchanged.
            for flag in ("accept_unscanned", "accept_build_changes"):
                if getattr(args, flag) and not (args.release_version or args.bump):
                    raise ConfigError(f"--{flag.replace('_', '-')} applies to a release "
                                      f"(--release/--bump)")
            if args.remote and not (args.dry_run or args.diff or args.stage):
                raise ConfigError("--remote applies to previews (--dry-run, --diff, --stage); "
                                  "publish always fetches first")
            if args.accept_source_divergence or args.recreate_branch:
                if args.stage or args.dry_run or args.diff:
                    raise ConfigError("--accept-source-divergence/--recreate-branch apply only "
                                      "to a real publish; previews do not publish")
                if args.amend:
                    raise ConfigError("--accept-source-divergence/--recreate-branch do not "
                                      "apply to --amend")
                if args.release_version or args.bump:
                    # Design §8.1: a release needs S0/S1/S6; accepting a source
                    # divergence or recreating the branch is a publish first.
                    raise ConfigError("--accept-source-divergence/--recreate-branch apply to a "
                                      "publish: publish with it first, then release")
            if args.retry and not (args.release_version or args.bump):
                raise ConfigError("--retry applies to a release (--release/--bump)")
            if args.amend and (args.release_version or args.bump):
                # a flag conflict (1), not a release outcome (7).
                raise ConfigError("--amend cannot be combined with a release; "
                                  "publish the amend first, then release")
            release_flag = bool(args.release_version or args.bump or args.no_release)
            if release_flag and (args.stage or args.dry_run or args.diff):
                raise ConfigError(
                    "--release/--bump/--no-release apply only to a real publish, not --stage/--dry-run/--diff")
            if args.stage:
                if args.dry_run:
                    raise ConfigError(
                        "--stage and --dry-run are incompatible "
                        "(--stage builds real files; --dry-run is virtual)")
                if args.diff:
                    raise ConfigError("--stage and --diff are incompatible")
                if args.message:
                    raise ConfigError(
                        "--stage does not commit; -m/--message has no effect")
                if args.force_overwrite:
                    raise ConfigError(
                        "--stage does not push; --force-overwrite has no effect")
                if args.amend is not None:
                    raise ConfigError(
                        "--stage does not commit; --amend/--no-amend has no effect")
            if args.amend is not None and (args.dry_run or args.diff):
                raise ConfigError(
                    "--amend/--no-amend only applies to a real publish "
                    "(not --dry-run/--diff)")
            if args.diff:
                # --diff implies --dry-run: previews never publish.
                return cmd_diff(source_root, config, remote=args.remote,
                                config_relpath=config_relpath, config_label=config_label,
                                custom_message=args.message)
            if args.json_output and not (args.dry_run or args.stage):
                raise ConfigError("ERROR: --json only applies to status, --dry-run, and --stage")
            if args.dry_run:
                return cmd_dry_run(source_root, config, as_json=args.json_output,
                                   remote=args.remote, config_relpath=config_relpath,
                                   config_label=config_label, custom_message=args.message)
            if args.stage:
                return cmd_publish(source_root, config, stage_only=True,
                                   as_json=args.json_output, remote=args.remote,
                                   config_relpath=config_relpath, config_label=config_label)
            if args.message is not None:
                # the -m text is refused before any git
                # call. Off a terminal the outcome is also one stdout line
                # flushed before the stderr error.
                scrub = message_scrub(args.message, config)
                if scrub.count:
                    if not args.yes and not is_interactive():
                        print("Not published: "
                              + message_scrub_text("the commit message (-m)", scrub)
                              + " " + MESSAGE_REMEDY, flush=True)
                    raise message_scrub_refusal("the commit message (-m)", scrub, MESSAGE_REMEDY)
            # §17.3: a covered publish confirms before anything is fetched,
            # locked or written.
            try:
                confirmation = confirmation_mode(yes=args.yes, tty=is_interactive())
            except ConfigError:
                # off a terminal without --yes, the offline
                # preview and the next command on stdout replace the bare
                # error; confirmation_mode stays the only decision point.
                raise offterminal_refusal(source_root, config, args, argv,
                                          config_relpath=config_relpath,
                                          config_label=config_label) from None
            publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
            if not (publish_dir / ".git").exists() and release_enabled(config):
                raise ConfigError(f"ERROR: {publish_dir.name}/ not initialized\nRun: {_PROG} init")
            if release_enabled(config):
                validate_includes(source_root, config, config_label=config_label)
            release_choice, release_discovery = resolve_release_choice(
                config, args, publish_dir, source_root, defer=confirmation == "prompt",
                cannot_release=_release_conflict(args))
            if release_enabled(config) and release_choice is None:
                # --yes on a terminal still asks the choice; cancel or an empty
                # answer is 1, as in prompt mode.
                raise ConfigError("Release cancelled; nothing changed")
            if release_choice is not None and args.accept_unscanned:
                release_choice.accept_unscanned = tuple(
                    posixpath.normpath(p) for p in args.accept_unscanned)
            if release_choice is not None and args.accept_build_changes:
                release_choice.accept_build_changes = True
            amend_mode = resolve_republish_mode(args.amend)
            return cmd_publish(source_root, config, args.message,
                               force_overwrite=args.force_overwrite,
                               stage_only=args.stage,
                               as_json=args.json_output and args.stage,
                               amend_mode=amend_mode,
                               release_choice=release_choice,
                               release_discovery=release_discovery,
                               config_relpath=config_relpath, config_file=resolved_cfg,
                               config_label=config_label,
                               accept_divergence=args.accept_source_divergence,
                               recreate_branch=args.recreate_branch, yes=args.yes,
                               confirmation=confirmation, expect_tip=args.expect_tip,
                               retry=args.retry)
        elif args.command == "init":
            cmd_init(source_root, config, config_file=resolved_cfg)
        elif args.command == "status":
            if args.diff:
                return cmd_diff(source_root, config, remote=args.remote,
                                config_relpath=config_relpath, config_label=config_label)
            return cmd_status(source_root, config, as_json=args.json_output,
                              check=args.check, remote=args.remote,
                              config_relpath=config_relpath, config_label=config_label)
        elif args.command == "integrate":
            return cmd_integrate(source_root, config, parser.parse_args(["pull", "--plan"]))
        elif args.command == "release-abandon":
            return cmd_release_abandon(source_root, config, args.version, args.yes)
        elif args.command == "pull":
            return cmd_pull(source_root, config, args)
        elif args.command == "repair":
            return cmd_repair(source_root, config, args)
        elif args.command == "release-retire":
            return cmd_release_retire(source_root, config, args.tag, yes=args.yes)
        elif args.command == "validate":
            return cmd_validate(source_root, config, as_json=args.json_output,
                                release_check=args.release_check, config_label=config_label,
                                remote=args.remote)
    except PubrepoError as e:
        ui.error(str(e))
        return e.exit_code
    except KeyboardInterrupt:
        ui.error("interrupted")
        return 130
    except Exception as e:
        # Last resort (T22): a clean line instead of a raw traceback —
        # except under --verbose, where the traceback must stay reachable.
        if args.verbose:
            raise
        ui.error(f"ERROR: unexpected failure: {e!r}\n"
                 f"rerun with --verbose for the full traceback")
        return EXIT_CONFIG_ERROR
    return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(main())
