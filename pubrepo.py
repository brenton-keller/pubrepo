#!/usr/bin/env python3
"""pubrepo — Publish a curated subset of files to a clean git repo.

Reads .publish.toml from the current directory to determine which files to
include, then nukes and rebuilds a .publish/ directory that mirrors the
public-facing repo. Commits and pushes the result.

Supports [publish.scrub] for forbidden string detection and
[publish.transforms] for find/replace on specific files after copy.

Usage:
    pubrepo                  # Full publish cycle
    pubrepo init             # First-time setup (clone or init .publish/)
    pubrepo status           # Show publish state and pending changes
    pubrepo --dry-run        # Show what would be published without touching .publish/
    pubrepo -m "message"     # Publish with a custom commit message
    pubrepo status --json    # Machine-readable output
    pubrepo --force-overwrite  # Re-assert source state over a diverged public repo
    pubrepo validate         # Check config and environment
    pubrepo integrate        # Print a recipe to pull public-repo commits back into source
    pubrepo publish --config .publish-sdk.toml  # Multi-target publish

(Deployed internally under the compatibility alias git-sync-publish —
same file, two names; hints follow the invoked name.)
"""
import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Callable

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

__version__ = "1.0.0"

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
        super().__init__(f"git {' '.join(git_args)} failed: {stderr.strip()}")


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


@contextmanager
def publish_lock(publish_dir: Path):
    """Exclusive, non-blocking lock around the publish mutation span (M3).

    The lock file lives inside .git/ so it survives the nuke and can never
    be published; the holder's PID is written for the contention message.
    status/validate/--dry-run stay lock-free by design — they are read-only,
    and momentary inconsistency during a concurrent publish is acceptable.
    """
    if fcntl is None:
        yield
        return
    lock_path = publish_dir / ".git" / "pubrepo.lock"
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


def git(*args, cwd=None, check=True):
    """Run a git command, returning the CompletedProcess.

    With check=True, a nonzero exit raises GitError.
    """
    result = subprocess.run(
        ["git"] + list(args),
        cwd=cwd, capture_output=True, text=True,
    )
    if check and result.returncode != 0:
        raise GitError(list(args), result.stderr)
    return result


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
                      "scrub", "transforms"}
KNOWN_SCRUB_KEYS = {"forbidden"}
KNOWN_RULE_KEYS = {"find", "replace", "strip_between"}


def validate_config(config: dict) -> list[dict]:
    """Centralized config validation (T15): every rule in one place.

    Returns findings [{level: error|warning, field, message}]. Pure config
    checks only — validate_environment covers filesystem/remote checks.
    Unknown keys warn, never error (forward compatibility).
    """
    f: list[dict] = []

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
                              f"top-level-only in v1 (the nuke preserves direct children of the "
                              f"publish dir only; nested keeps are planned for v1.1)"))

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

    for k in config:
        if k not in KNOWN_PUBLISH_KEYS:
            f.append(_warning("publish", f"unknown key '{k}' in [publish] (ignored)"))

    return f


def validate_environment(config: dict, source_root: Path) -> list[dict]:
    """Environment checks (T15): include existence/escape, exclude collisions,
    transform reachability, remote URL shape. Guards against invalid config
    shapes so the validate command can report everything in one pass."""
    f: list[dict] = []
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
            f.append(_err("include", f"include path escapes repo: {inc} — "
                          f"includes must stay inside the repository"))
            continue
        if resolved == source_root.resolve():
            f.append(_warning("include", f"include '{inc}' is the repository root — the manifest just "
                              f"became \"everything minus excludes\". Consider listing directories "
                              f"explicitly."))
        if _include_matches_excludes(inc, target.is_dir(), user_matcher):
            f.append(_warning("include", f"include '{inc}' is also matched by exclude — exclude wins, "
                              f"not published"))
    if missing:
        f.append(_err("include", "include paths not found in working tree:\n"
                      + "\n".join(f"  {m}" for m in missing)
                      + "\n(was it renamed? update " + CONFIG_FILE_NAME + ")"))

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

    if strict:
        findings = validate_config(config)
        for fi in findings:
            if fi["level"] == "warning":
                ui.warn(f"WARNING: {fi['message']}")
        errors = [fi for fi in findings if fi["level"] == "error"]
        if errors:
            raise ConfigError("\n".join(f"ERROR: {fi['message']}" for fi in errors))
    return config


def validate_includes(source_root: Path, config: dict) -> None:
    """Raising wrapper over the environment findings, for the publish and
    dry-run hot paths: warnings print, the errors abort."""
    findings = validate_environment(config, source_root)
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


def nuke_publish_dir(publish_dir: Path, keep: list[str]) -> None:
    """Remove everything from .publish/ except .git/ and kept files."""
    protected = {".git"} | set(keep)
    for entry in publish_dir.iterdir():
        if entry.name in protected:
            continue
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()


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
            target.write_text(new_content, errors="surrogateescape")
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


def scrub_check_source(source_root: Path, config: dict) -> list[dict]:
    """Run scrub check against source files (for dry-run, without touching .publish/).

    Builds the wanted file list and checks each source file for forbidden strings.
    """
    forbidden = config.get("scrub", {}).get("forbidden", [])
    if not forbidden:
        return []

    transforms = config.get("transforms", {})
    ignore_fn = make_ignore_fn(source_root, config["exclude"])
    user_matcher = compile_patterns(config["exclude"])
    results = []
    forbidden_lower = [s.lower() for s in forbidden]

    for inc in config["include"]:
        if _include_is_hard_excluded(inc):
            continue
        src = source_root / inc
        if _include_matches_excludes(inc, src.is_dir(), user_matcher):
            continue
        if src.is_file():
            files_to_check = [(src, inc)]
        elif src.is_dir():
            files_to_check = []
            for root, dirs, filenames in os.walk(src):
                ignored = ignore_fn(root, dirs + filenames)
                dirs[:] = [d for d in dirs if d not in ignored]
                for f in filenames:
                    if f not in ignored:
                        full = Path(root) / f
                        rel = os.path.relpath(str(full), start=str(source_root))
                        files_to_check.append((full, rel))
        else:
            continue

        for full_path, rel_path in files_to_check:
            if full_path.is_symlink():
                continue
            if is_binary_file(full_path):
                continue
            try:
                content = full_path.read_text(errors="surrogateescape")
            except (IOError, OSError) as e:
                results.append({"file": rel_path, "matches": [],
                                "error": f"unreadable — could not verify ({e.__class__.__name__})"})
                continue

            if rel_path in transforms:
                content = apply_transform_rules(content, transforms[rel_path])

            content_lower = content.lower()
            matches = [
                orig for orig, low in zip(forbidden, forbidden_lower)
                if low in content_lower
            ]
            if matches:
                results.append({"file": rel_path, "matches": matches})

    # Keep files exist only in the publish dir — scan them too, so dry-run
    # can never say "passed" where publish would fail (M7 parity).
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    for k in config["keep"]:
        kp = publish_dir / k
        if not kp.exists() or kp.is_symlink() or is_binary_file(kp):
            continue
        try:
            content = kp.read_text(errors="surrogateescape").lower()
        except (IOError, OSError) as e:
            results.append({"file": k, "matches": [],
                            "error": f"unreadable — could not verify ({e.__class__.__name__})"})
            continue
        matches = [orig for orig, low in zip(forbidden, forbidden_lower) if low in content]
        if matches:
            results.append({"file": k, "matches": matches})

    return sorted(results, key=lambda r: r["file"])


def get_source_info(source_root: Path) -> tuple[str, bool]:
    """Return (short_hash, is_dirty) for the source repo."""
    result = git("rev-parse", "--short", "HEAD", cwd=str(source_root), check=False)
    if result.returncode != 0:
        return ("unknown", True)
    short_hash = result.stdout.strip()

    result = git("status", "--porcelain", "--untracked-files=no", cwd=str(source_root), check=False)
    is_dirty = bool(result.stdout.strip())

    return (short_hash, is_dirty)


def build_commit_message(source_hash: str, is_dirty: bool, custom_message: str | None = None,
                         source_branch: str | None = None, added: int = 0, modified: int = 0,
                         deleted: int = 0, transform_count: int = 0,
                         scrub_patterns: int = 0) -> str:
    """Build the commit message (T18). These messages are the public repo's
    visible history — keep them clean. The 'Source:' line format is FROZEN
    (old-repo fallback parsing depends on it); parsing takes the LAST
    Source: line, so custom titles containing 'Source:' cannot confuse it.
    """
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    dirty_tag = " (dirty)" if is_dirty else ""

    if custom_message:
        title = custom_message
    else:
        title = f"publish: {now}"

    return (f"{title}\n"
            f"\n"
            f"Source: {source_hash}{dirty_tag}\n"
            f"Branch: {source_branch or '(detached)'}\n"
            f"Files: {added + modified + deleted} ({added} added, {modified} modified, "
            f"{deleted} deleted)\n"
            f"Transforms: {transform_count} file(s)\n"
            f"Scrub: {scrub_patterns} pattern(s) passed\n")


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
        return {
            "commit": str(entry.get("public_commit") or "?")[:12],
            "date": entry.get("ts", "?"),
            "source": source,
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


def _build_published_files(publish_dir: Path) -> dict[str, str]:
    """Build a dict of {relative_path: content_hash} for files currently in .publish/."""
    published = {}
    for root, dirs, filenames in os.walk(publish_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in filenames:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, start=str(publish_dir))
            published[rel] = hashlib.sha256(Path(full).read_bytes()).hexdigest()
    return published


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
        entry = inc.rstrip("/")
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
            prefix = f"{prefix}/{name}"
            is_dir = i < len(below) - 1
            if is_env_excluded(name) or matcher(prefix, is_dir):
                break
        else:
            return True
    return False


def cmd_init(source_root: Path, config: dict) -> None:
    """First-time setup: clone the remote into .publish/ and report problems.

    Reports, never prompts (automation-safe) and never edits source files
    (§2 invariant — .gitignore advice is printed, not applied).
    """
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    remote = config["remote"]
    branch = config["branch"]

    if (publish_dir / ".git").exists():
        ui.success(f"Already initialized: {publish_dir}")
        return

    ui.info(f"Remote: {remote}")
    ui.info(f"Branch: {branch}")

    # Modern git clones empty repos successfully (unborn HEAD), so a clone
    # failure is a REAL error — auth, URL, network — not "empty repo?".
    result = git("clone", remote, str(publish_dir), check=False)
    if result.returncode != 0:
        raise PublishError(
            f"ERROR: clone failed:\n{result.stderr.strip()}\n"
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

    ui.success(f"Initialized: {publish_dir}")


def cmd_integrate(source_root: Path, config: dict) -> int:
    """Print (never run) a copy-paste recipe that brings foreign commits on
    the public repo back into the source repo.

    Print-only is the design: integration imports third-party content into
    source, which can carry junk (build artifacts, scratch files) and needs
    human judgment — and §2 forbids the tool from modifying source. But
    everything pubrepo knows — the last snapshot, its Source: base, the
    manifest filter — is baked into the printed commands, so the user never
    reconstructs a pathspec by hand.
    """
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    branch = config["branch"]
    pd = publish_dir.name

    if not (publish_dir / ".git").exists():
        raise ConfigError(f"ERROR: {pd}/ not initialized\n"
                          f"Run: {_PROG} init")

    fetch = git("fetch", "origin", branch, cwd=str(publish_dir), check=False)
    if fetch.returncode != 0:
        if "couldn't find remote ref" in fetch.stderr.lower():
            print(f"Nothing to integrate: remote branch '{branch}' does not exist yet.")
            return EXIT_SUCCESS
        raise PublishError(f"ERROR: cannot reach remote:\n{fetch.stderr.strip()}")

    remote_ref = f"origin/{branch}"
    head_valid = git("rev-parse", "--verify", "-q", "HEAD",
                     cwd=str(publish_dir), check=False).returncode == 0
    if not head_valid:
        print(f"Nothing to diff against: no publish has been made from here yet\n"
              f"({pd}/ has no commits — run {_PROG} first).")
        return EXIT_SUCCESS

    count = int(git("rev-list", "--count", f"HEAD..{remote_ref}",
                    cwd=str(publish_dir)).stdout.strip())
    if count == 0:
        print(f"Nothing to integrate: {remote_ref} has no commits beyond the last snapshot.")
        return EXIT_SUCCESS

    snap = git("rev-parse", "--short", "HEAD", cwd=str(publish_dir)).stdout.strip()
    tip = git("rev-parse", "--short", remote_ref, cwd=str(publish_dir)).stdout.strip()
    base = (get_last_publish_info(publish_dir) or {}).get("source")

    rng = f"HEAD..{remote_ref}"
    log_lines = git("log", "--oneline", rng,
                    cwd=str(publish_dir), check=False).stdout.rstrip().splitlines()
    author_counts: dict[str, int] = {}
    for a in git("log", "--format=%an <%ae>", rng,
                 cwd=str(publish_dir), check=False).stdout.splitlines():
        if a.strip():
            author_counts[a.strip()] = author_counts.get(a.strip(), 0) + 1
    top_author = (max(author_counts, key=author_counts.get)
                  if author_counts else "Their Name <them@example.com>")

    changed = [f for f in git("diff", "--name-only", "HEAD", remote_ref,
                              cwd=str(publish_dir), check=False).stdout.splitlines()
               if f.strip()]
    in_set = [f for f in changed if _path_would_publish(f, config)]
    junk = [f for f in changed if f not in in_set]

    # Conflict surface: files changed in BOTH the foreign range and the
    # source repo since the snapshot's Source: base.
    overlap = None
    if base and git("cat-file", "-e", f"{base}^{{commit}}",
                    cwd=str(source_root), check=False).returncode == 0:
        src_changed = set(git("diff", "--name-only", base, "HEAD",
                              cwd=str(source_root), check=False).stdout.splitlines())
        overlap = sorted(set(in_set) & src_changed)

    transformed_hits = sorted(f for f in in_set
                              if f in config.get("transforms", {}))

    out: list[str] = []

    def c(line: str = "") -> None:
        out.append(f"# {line}".rstrip())

    c(f"{_PROG} integrate — public-repo changes not in your source repo")
    c()
    c(f"Last snapshot:   {snap}" + (f"  (Source: {base})" if base else ""))
    c(f"Foreign commits: {count} ({snap}..{tip})")
    for line in log_lines[:15]:
        c(f"  {line}")
    if len(log_lines) > 15:
        c(f"  ... and {len(log_lines) - 15} more")
    c("Authors:         " + ", ".join(
        f"{a} ({n})" for a, n in
        sorted(author_counts.items(), key=lambda kv: -kv[1])))
    c()
    c(f"Changed files in the publish set: {len(in_set)}")
    if junk:
        c(f"Ignored — outside the manifest or excluded ({len(junk)}; a republish "
          f"deletes these from the public repo):")
        for f in junk[:15]:
            c(f"  {f}")
        if len(junk) > 15:
            c(f"  ... and {len(junk) - 15} more")
    if overlap is None:
        c("Conflict surface: unknown — source base commit "
          + (f"{base} not found in this repo" if base else "not recorded"))
    elif overlap:
        c(f"Conflict surface: {len(overlap)} file(s) changed in BOTH source and")
        c("the foreign range — expect merge conflicts there:")
        for f in overlap:
            c(f"  {f}")
    else:
        c("Conflict surface: empty — the patch should apply cleanly")
    c()

    if not in_set:
        c("Every foreign change is outside the publish manifest — nothing to")
        c(f"integrate. A republish ({_PROG} --force-overwrite) will remove the")
        c("files above from the public repo.")
        print("\n".join(out))
        return EXIT_SUCCESS

    if transformed_hits:
        c("MANUAL INTEGRATION REQUIRED — these changed files have transform")
        c("rules, so their public content is not source content and the diff")
        c("has no preimage in your repo. Read it, hand-apply to source, then")
        c(f"republish with {_PROG} --force-overwrite:")
        for f in transformed_hits:
            c(f"  {f}")
        c()
        out.append(f"git -C {shlex.quote(pd)} diff HEAD {remote_ref}")
        print("\n".join(out))
        return EXIT_SUCCESS

    c("Recipe — paste at the source-repo root. Review the patch before")
    c(f"applying; {_PROG} never modifies your source repo itself:")
    c()
    patch = "/tmp/pubrepo-integrate.patch"
    out.append(f"git -C {shlex.quote(pd)} diff HEAD {remote_ref} -- \\")
    for i, f in enumerate(in_set):
        tail = " \\" if i < len(in_set) - 1 else f" > {patch}"
        out.append(f"    {shlex.quote(f)}{tail}")
    out.append(f"git apply --3way --check {patch}   # dry check, applies nothing")
    out.append(f"git apply --3way {patch}")
    out.append("git add -A")
    out.append(f"git commit --author={shlex.quote(top_author)} \\")
    out.append(f"    -m {shlex.quote(f'Integrate public-repo work ({snap}..{tip})')}")
    out.append(f"{_PROG} --force-overwrite")
    print("\n".join(out))
    return EXIT_SUCCESS


def _source_branch(source_root: Path) -> str | None:
    result = git("symbolic-ref", "--short", "HEAD", cwd=str(source_root), check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def cmd_status(source_root: Path, config: dict, as_json: bool = False,
               check: bool = False, remote: bool = False) -> int:
    """Dashboard (T16): config → source → last publish → pending → scrub.

    --remote adds an opt-in fetch + divergence report (default stays
    offline — network in a status command is surprising). --check: exit 0
    up-to-date / 4 scrub would fail / 6 changes pending; scrub wins.
    """
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

    wanted = _build_wanted_files(source_root, config)
    published = _build_published_files(publish_dir)

    for k in config.get("keep", []):
        published.pop(k, None)

    added = sorted(set(wanted) - set(published))
    deleted = sorted(set(published) - set(wanted))
    modified = sorted(
        f for f in set(wanted) & set(published)
        if wanted[f] != published[f]
    )
    pending = bool(added or deleted or modified)

    # Keep files are user-managed inside the publish dir; the wanted-vs-
    # published comparison can't see their edits (both sides pop keep), but
    # the next publish WILL commit them — surface that honestly.
    porcelain = git("status", "--porcelain", cwd=str(publish_dir), check=False)
    publish_dir_dirty = bool(porcelain.stdout.strip())

    scrub_results = scrub_check_source(source_root, config)
    forbidden = config.get("scrub", {}).get("forbidden", [])

    remote_info = None
    if remote:
        fetch = git("fetch", "origin", branch, cwd=str(publish_dir), check=False)
        if fetch.returncode != 0:
            if "couldn't find remote ref" in fetch.stderr.lower():
                remote_info = {"checked": True, "diverged": False, "ahead_count": 0}
            else:
                remote_info = {"checked": True, "diverged": None, "ahead_count": None,
                               "error": fetch.stderr.strip()}
        else:
            rev = git("rev-parse", f"origin/{branch}", cwd=str(publish_dir), check=False)
            if rev.returncode != 0:
                remote_info = {"checked": True, "diverged": False, "ahead_count": 0}
            else:
                sha = rev.stdout.strip()
                head_valid = git("rev-parse", "--verify", "-q", "HEAD",
                                 cwd=str(publish_dir), check=False).returncode == 0
                rev_range = f"HEAD..{sha}" if head_valid else sha
                count_res = git("rev-list", "--count", rev_range,
                                cwd=str(publish_dir), check=False)
                count = int(count_res.stdout.strip()) if count_res.returncode == 0 else 0
                remote_info = {"checked": True, "diverged": count > 0, "ahead_count": count}

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
                "passed": len(scrub_results) == 0,
                "matches": scrub_results,
            },
            "remote": remote_info,
        }
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
            print(f"Last published: (no commits yet)")
            print(f"  Run {_PROG} to create initial publish.")

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
            print(f"Uncommitted changes in {publish_dir.name}/ (keep edits or a "
                  f"blocked publish) — the next publish will commit them")

        if forbidden:
            print()
            if scrub_results:
                print(f"SCRUB WARNING: {len(scrub_results)} file(s) contain forbidden strings:")
                for r in scrub_results:
                    print(f"  {r['file']}: {r.get('error') or ', '.join(r['matches'])}")
            else:
                print(f"Scrub: passed ({len(forbidden)} patterns checked)")

        if remote_info is not None:
            print()
            if remote_info.get("error"):
                print(f"Remote: check failed — {remote_info['error']}")
            elif remote_info["diverged"]:
                print(f"Remote: DIVERGED — origin/{branch} has {remote_info['ahead_count']} "
                      f"commit(s) not in local; pubrepo is a one-way mirror "
                      f"(publish --force-overwrite re-asserts source)")
            else:
                print(f"Remote: up to date (origin/{branch})")

    if check:
        if scrub_results:
            return EXIT_SCRUB_FAILED
        if pending or publish_dir_dirty:
            return EXIT_PENDING_CHANGES
    return EXIT_SUCCESS


def cmd_dry_run(source_root: Path, config: dict, as_json: bool = False) -> int:
    """Show what would be published without touching .publish/.

    Returns EXIT_SCRUB_FAILED when scrub would fail (§8 behavior change:
    previously printed the failure and exited 0 — that was a bug).
    """
    validate_includes(source_root, config)

    source_hash, is_dirty = get_source_info(source_root)

    wanted = _build_wanted_files(source_root, config)
    files = sorted(wanted.keys())

    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    d_added = d_modified = d_deleted = 0
    if (publish_dir / ".git").exists():
        published = _build_published_files(publish_dir)
        for k in config.get("keep", []):
            published.pop(k, None)
        d_added = len(set(wanted) - set(published))
        d_deleted = len(set(published) - set(wanted))
        d_modified = len([x for x in set(wanted) & set(published)
                          if wanted[x] != published[x]])

    forbidden_count = len(config.get("scrub", {}).get("forbidden", []))
    message = build_commit_message(
        source_hash, is_dirty,
        source_branch=_source_branch(source_root),
        added=d_added, modified=d_modified, deleted=d_deleted,
        transform_count=len([t for t in config.get("transforms", {}) if t in wanted]),
        scrub_patterns=forbidden_count)

    transforms = config.get("transforms", {})
    transform_targets = [f for f in transforms if f in wanted]

    scrub_results = scrub_check_source(source_root, config)

    if as_json:
        output = {
            "files": files,
            "file_count": len(files),
            "source": {"commit": source_hash, "dirty": is_dirty},
            "transforms": transform_targets,
            "scrub": {
                "passed": len(scrub_results) == 0,
                "matches": scrub_results,
            },
        }
        print(json.dumps(output, indent=2))
        return EXIT_SCRUB_FAILED if scrub_results else EXIT_SUCCESS

    if ui.quiet:
        # §4: success is silent (the exit code is the answer); failures are
        # errors on stderr. This is what makes `--dry-run --quiet` usable as
        # the README's pre-push leak gate.
        if scrub_results:
            ui.error(f"SCRUB WOULD FAIL: {len(scrub_results)} file(s) "
                     f"contain forbidden strings:")
            for r in scrub_results:
                ui.error(f"  {r['file']}: {r.get('error') or ', '.join(r['matches'])}")
        return EXIT_SCRUB_FAILED if scrub_results else EXIT_SUCCESS

    print(f"Files to publish ({len(files)}):")
    for f in files:
        print(f"  {f}")

    if transform_targets:
        print(f"\nTransforms ({len(transform_targets)}):")
        for f in transform_targets:
            n_rules = len(transforms[f])
            print(f"  {f} ({n_rules} rule{'s' if n_rules != 1 else ''})")

    print()
    print("Commit message:")
    for line in message.rstrip().split("\n"):
        print(f"  {line}")

    forbidden = config.get("scrub", {}).get("forbidden", [])
    if forbidden:
        print()
        if scrub_results:
            print(f"SCRUB WOULD FAIL: {len(scrub_results)} file(s) contain forbidden strings:")
            for r in scrub_results:
                print(f"  {r['file']}: {r.get('error') or ', '.join(r['matches'])}")
            print("\nPublish will be blocked until these are resolved.")
        else:
            print(f"Scrub: passed ({len(forbidden)} patterns checked)")

    if not (publish_dir / ".git").exists():
        print()
        print(f"NOTE: {publish_dir.name}/ not initialized. Run: {_PROG} init")

    return EXIT_SCRUB_FAILED if scrub_results else EXIT_SUCCESS


def cmd_validate(source_root: Path, config: dict, as_json: bool = False) -> int:
    """Report ALL config + environment findings (T15); exit 0 unless errors."""
    findings = validate_config(config) + validate_environment(config, source_root)
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


MAX_DIFF_BYTES = 1024 * 1024


def _is_binary_bytes(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def cmd_diff(source_root: Path, config: dict) -> int:
    """Render what the WORLD would see change (T17): unified diff between
    current .publish/ content and the would-be-published POST-TRANSFORM
    content. Replaces normal output; never publishes (implies --dry-run)."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    if not (publish_dir / ".git").exists():
        raise ConfigError(f"ERROR: {publish_dir.name}/ not initialized\n"
                          f"Run: {_PROG} init")

    validate_includes(source_root, config)
    wanted, contents = _build_wanted_files(source_root, config, with_content=True)
    published = _build_published_files(publish_dir)
    for k in config.get("keep", []):
        published.pop(k, None)

    added = set(wanted) - set(published)
    deleted = sorted(set(published) - set(wanted))
    modified = {f for f in set(wanted) & set(published) if wanted[f] != published[f]}

    if not (added or deleted or modified):
        print("No changes to publish")
        return EXIT_SUCCESS

    chunks = []
    for f in sorted(added | modified):
        new_bytes = contents[f]
        old_bytes = (publish_dir / f).read_bytes() if f in modified else b""
        if _is_binary_bytes(new_bytes) or _is_binary_bytes(old_bytes):
            chunks.append(f"Binary files differ: {f}")
            continue
        if len(new_bytes) > MAX_DIFF_BYTES or len(old_bytes) > MAX_DIFF_BYTES:
            chunks.append(f"{f}: diff suppressed (large file)")
            continue
        diff = difflib.unified_diff(
            old_bytes.decode(errors="surrogateescape").splitlines(keepends=True),
            new_bytes.decode(errors="surrogateescape").splitlines(keepends=True),
            fromfile=f"a/{f}" if f in modified else "/dev/null",
            tofile=f"b/{f}",
        )
        chunks.append("".join(diff).rstrip("\n"))
    for f in deleted:
        chunks.append(f"Deleted: {f}")
    print("\n".join(chunks))
    return EXIT_SUCCESS


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


def cmd_publish(source_root: Path, config: dict, custom_message: str | None = None,
                force_overwrite: bool = False) -> None:
    """Full publish cycle."""
    publish_dir = source_root / config.get("dir", PUBLISH_DIR_NAME)
    branch = config["branch"]

    validate_includes(source_root, config)

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
        with publish_lock(publish_dir):
            _publish_under_lock(source_root, publish_dir, branch, config,
                                custom_message, force_overwrite)
    except KeyboardInterrupt:
        # House git-safety rules treat reset --hard and clean -fd as
        # destructive. They are correct HERE, and only here: every byte
        # under .publish/ except .git/ is regenerable tool output, and keep
        # files are tracked in prior commits — restoring to HEAD is provably
        # lossless in this one directory.
        git("reset", "--hard", "HEAD", cwd=str(publish_dir), check=False)
        git("clean", "-fd", cwd=str(publish_dir), check=False)
        raise Interrupted("publish interrupted; .publish/ restored to last "
                          "published state")
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)


def _publish_under_lock(source_root: Path, publish_dir: Path, branch: str, config: dict,
                        custom_message: str | None, force_overwrite: bool) -> None:
    """cmd_publish's mutation span; the caller holds the publish lock."""
    _last_mark = time.monotonic()

    def _mark(phase: str) -> None:
        nonlocal _last_mark
        now = time.monotonic()
        ui.detail(f"timing: {phase} {now - _last_mark:.2f}s")
        _last_mark = now

    # --- Remote preamble: discover every remote problem BEFORE the nuke, ---
    # --- so a refused publish leaves .publish/ untouched (M4).           ---
    diverged_count = 0
    try:
        # 1. Fetch. A missing remote branch (first publish to an empty repo)
        #    is not an error; anything else is.
        fetch = git("fetch", "origin", branch, cwd=str(publish_dir), check=False)
        remote_branch_exists = fetch.returncode == 0
        if not remote_branch_exists and "couldn't find remote ref" not in fetch.stderr.lower():
            raise PublishError(f"ERROR: cannot reach remote; publish requires push access.\n"
                               f"{fetch.stderr.strip()}")

        # 2. The publish clone must sit on the configured branch.
        head = git("symbolic-ref", "--short", "HEAD", cwd=str(publish_dir), check=False)
        current_branch = head.stdout.strip() if head.returncode == 0 else None
        if current_branch != branch:
            state = f"on branch '{current_branch}'" if current_branch else "in detached HEAD state"
            raise PublishError(f"ERROR: {publish_dir.name}/ is {state}, but the configured "
                               f"branch is '{branch}'.\n"
                               f"Fix: git -C {publish_dir.name} checkout {branch}")

        # 3. Pin the sha the user is (possibly) overwriting — the lease target.
        fetched_sha = None
        if remote_branch_exists:
            rev = git("rev-parse", f"origin/{branch}", cwd=str(publish_dir), check=False)
            if rev.returncode == 0:
                fetched_sha = rev.stdout.strip()

        # 4. Divergence: remote commits not reachable from HEAD.
        if fetched_sha:
            head_valid = git("rev-parse", "--verify", "-q", "HEAD",
                             cwd=str(publish_dir), check=False).returncode == 0
            rev_range = f"HEAD..{fetched_sha}" if head_valid else fetched_sha
            diverged_count = int(git("rev-list", "--count", rev_range,
                                     cwd=str(publish_dir)).stdout.strip())
            if diverged_count > 0 and not force_overwrite:
                log_out = git("log", "--oneline", rev_range,
                              cwd=str(publish_dir), check=False).stdout.rstrip()
                snap = git("rev-parse", "--short", "HEAD",
                           cwd=str(publish_dir), check=False).stdout.strip()
                base = (get_last_publish_info(publish_dir) or {}).get("source")
                base_note = f" (Source: {base})" if base else ""
                raise Diverged(
                    log_out.splitlines(),
                    f"ERROR: the public repo has {diverged_count} commit(s) not produced by this tool:\n"
                    f"{log_out}\n\n"
                    f"They sit on top of your last snapshot {snap}{base_note}.\n"
                    f"pubrepo maintains a one-way mirror: apply these changes to the source "
                    f"repo, then republish with --force-overwrite.\n"
                    f"Run `{_PROG} integrate` for a copy-paste integration recipe "
                    f"(docs/integrating-changes.md explains the workflow).")
    except GitError as e:
        # Preamble git failures are publish failures too (§8 code 2).
        raise PublishError(f"Remote check failed: {e}") from e
    ui.info(f"Fetching origin/{branch}... ok")
    _mark("remote checks")

    source_hash, is_dirty = get_source_info(source_root)

    # Nuke and rebuild
    nuke_publish_dir(publish_dir, config["keep"])
    copied = copy_includes(source_root, publish_dir, config["include"], config["exclude"])
    ui.info(f"Rebuilding {PUBLISH_DIR_NAME}/ ({len(copied)} files)... ok")
    _mark("rebuild")

    # Apply transforms
    transforms = config.get("transforms", {})
    transformed = []
    if transforms:
        transformed = apply_transforms(publish_dir, transforms)
        if transformed:
            ui.info(f"Transforming ({len(transformed)})... ok")
            ui.detail(f"transformed: {', '.join(transformed)}")
        _mark("transforms")

    # Scrub check — BEFORE staging/committing
    forbidden = config.get("scrub", {}).get("forbidden", [])
    if forbidden:
        scrub_results = scrub_check(publish_dir, forbidden)
        if scrub_results:
            _append_publish_log(publish_dir, {
                "ts": datetime.now().astimezone().isoformat(),
                "source_commit": source_hash,
                "source_branch": _source_branch(source_root),
                "source_dirty": is_dirty,
                "files": len(copied),
                "added": None, "modified": None, "deleted": None,
                "transforms_applied": len(transformed),
                "scrub_patterns": len(forbidden),
                "scrub_result": "failed",
                "pushed": False,
                "public_commit": None,
                "forced": False,
                "title": custom_message or None,
            })
            detail = "\n".join(
                f"  {r['file']}: {r.get('error') or ', '.join(r['matches'])}"
                for r in scrub_results)
            raise ScrubFailure(
                scrub_results,
                f"\nSCRUB FAILED — {len(scrub_results)} file(s) contain forbidden strings:\n"
                f"{detail}\n"
                f"\nPublish aborted. Fix the source files or update [publish.scrub] / [publish.transforms].",
            )
        else:
            ui.info(f"Scrubbing ({len(copied)} files, {len(forbidden)} patterns)... ok")
        _mark("scrub")

    # Stage everything
    try:
        git("add", "-A", cwd=str(publish_dir))

        # Check for changes
        result = git("diff", "--cached", "--quiet", cwd=str(publish_dir), check=False)
        if result.returncode == 0:
            ui.success("Nothing to publish")
            return

        # Build and apply commit from the staged reality
        counts = git("diff", "--cached", "--no-renames", "--name-status",
                     cwd=str(publish_dir), check=False)
        n_added = n_modified = n_deleted = 0
        for line in counts.stdout.splitlines():
            code = line.split("\t", 1)[0].strip()
            if code.startswith("A"):
                n_added += 1
            elif code.startswith("M"):
                n_modified += 1
            elif code.startswith("D"):
                n_deleted += 1
        message = build_commit_message(
            source_hash, is_dirty, custom_message,
            source_branch=_source_branch(source_root),
            added=n_added, modified=n_modified, deleted=n_deleted,
            transform_count=len(transformed), scrub_patterns=len(forbidden))
        git("commit", "-m", message, cwd=str(publish_dir))
        ui.info("Committing... ok")
    except GitError as e:
        # Mid-publish git failures are publish failures (§8 code 2).
        msg = str(e)
        if "who you are" in e.stderr or "Author identity unknown" in e.stderr:
            msg += (f"\nNo git identity in {publish_dir.name}/ — set one:\n"
                    f"  git -C {publish_dir.name} config user.name 'Your Name'\n"
                    f"  git -C {publish_dir.name} config user.email 'you@example.com'")
        raise PublishError(msg) from e
    _mark("commit")

    # Push. With --force-overwrite the lease pins the exact sha the user was
    # shown at fetch time — anything landing in between gets refused, never
    # silently destroyed. Force only when actually diverged: a normal push
    # suffices otherwise, and some server hooks reject any force-push.
    push_args = ["push", "-u", "origin", branch]
    if force_overwrite and fetched_sha and diverged_count > 0:
        push_args = ["push", "-u", f"--force-with-lease=refs/heads/{branch}:{fetched_sha}",
                     "origin", branch]
    result = git(*push_args, cwd=str(publish_dir), check=False)
    if result.returncode != 0:
        ui.error(f"ERROR: push failed: {result.stderr.strip()}")
        ui.info("Rolling back local commit...")
        # Check if this is the root commit (HEAD~1 doesn't exist)
        has_parent = git("rev-parse", "--verify", "HEAD~1", cwd=str(publish_dir), check=False)
        if has_parent.returncode == 0:
            git("reset", "--soft", "HEAD~1", cwd=str(publish_dir))
        else:
            git("update-ref", "-d", "HEAD", cwd=str(publish_dir))
        raise PublishError("Local commit rolled back. Fix the issue and retry.")
    ui.info("Pushing... ok")
    _mark("push")

    public_commit = git("rev-parse", "HEAD", cwd=str(publish_dir),
                        check=False).stdout.strip()
    _append_publish_log(publish_dir, {
        "ts": datetime.now().astimezone().isoformat(),
        "source_commit": source_hash,
        "source_branch": _source_branch(source_root),
        "source_dirty": is_dirty,
        "files": len(copied),
        "added": n_added, "modified": n_modified, "deleted": n_deleted,
        "transforms_applied": len(transformed),
        "scrub_patterns": len(forbidden),
        "scrub_result": "passed" if forbidden else "skipped",
        "pushed": True,
        "public_commit": public_commit,
        "forced": bool(force_overwrite and fetched_sha and diverged_count > 0),
        "title": custom_message or None,
    })

    # Tag source if working tree is clean
    if not is_dirty:
        # Compact timestamp: colons are illegal in git ref names.
        now = datetime.now().astimezone()
        tag_name = f"published/{now.strftime('%Y%m%dT%H%M%S')}"
        exists = git("rev-parse", "--verify", "--quiet", f"refs/tags/{tag_name}",
                     cwd=str(source_root), check=False)
        if exists.returncode == 0:
            # Legitimate when two publishes land in the same second.
            ui.info(f"Tag {tag_name} already exists, skipping")
        else:
            result = git("tag", tag_name, cwd=str(source_root), check=False)
            if result.returncode != 0:
                # TOCTOU: the tag can appear between the rev-parse pre-check
                # and here (T9's publish lock closes the realistic window) —
                # report that case as the skip it is, not as a failure.
                if "already exists" in result.stderr:
                    ui.info(f"Tag {tag_name} already exists, skipping")
                else:
                    ui.warn(f"WARNING: failed to create tag {tag_name}: {result.stderr.strip()}")
            else:
                result = git("push", "origin", tag_name, cwd=str(source_root), check=False)
                if result.returncode == 0:
                    ui.info(f"Tagged source: {tag_name}")
                else:
                    ui.warn(f"Tag created locally ({tag_name}) but push failed: {result.stderr.strip()}")

    ui.success(f"Published {source_hash}{'  (dirty)' if is_dirty else ''}")


KNOWN_COMMANDS = ("publish", "init", "status", "validate", "integrate")


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
                               help="Full publish cycle (the default command)")
    p_publish.add_argument("-m", "--message", help="Custom commit message")
    p_publish.add_argument("--dry-run", "-n", action="store_true",
                           help="Show what would be published without touching "
                                ".publish/; exits 4 if scrub would fail")
    p_publish.add_argument("--force-overwrite", action="store_true",
                           help="Publish even if the public repo has diverged; pushes "
                                "with --force-with-lease pinned to the fetched sha")
    p_publish.add_argument("--json", action="store_true", dest="json_output",
                           help="Machine-readable output (with --dry-run)")
    p_publish.add_argument("--diff", action="store_true",
                           help="Show a unified diff of what would change on the "
                                "public repo (implies --dry-run; never publishes)")

    sub.add_parser("init", parents=[common],
                   help="First-time setup: clone the remote into .publish/")

    p_status = sub.add_parser("status", parents=[common],
                              help="Show publish state and pending changes")
    p_status.add_argument("--json", action="store_true", dest="json_output",
                          help="Machine-readable output")
    p_status.add_argument("--check", action="store_true",
                          help="Exit 0 up-to-date / 4 scrub would fail / "
                               "6 changes pending (scrub takes precedence)")
    p_status.add_argument("--remote", action="store_true",
                          help="Also fetch and report divergence (network)")
    p_status.add_argument("--diff", action="store_true",
                          help="Show a unified diff of what would change on the "
                               "public repo")

    sub.add_parser("integrate", parents=[common],
                   help="Print a copy-paste recipe for pulling public-repo "
                        "commits back into the source repo (never executes)")

    p_validate = sub.add_parser("validate", parents=[common],
                                help="Validate .publish.toml and the environment")
    p_validate.add_argument("--json", action="store_true", dest="json_output",
                            help="Machine-readable output")
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Default-command shim (§4): bare `git-sync-publish --dry-run` keeps
    # working — if the first arg isn't a known command, assume publish.
    if not argv or argv[0] not in KNOWN_COMMANDS + ("-h", "--help", "--version"):
        argv.insert(0, "publish")

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        # argparse exits 2 on usage errors, but §8 reserves 2 for publish
        # failures — usage errors are config errors (1). 0 = --help/--version.
        return EXIT_SUCCESS if e.code == 0 else EXIT_CONFIG_ERROR

    ui.configure(quiet=args.quiet, verbose=args.verbose, no_color=args.no_color)

    source_root = Path.cwd()
    config_path = Path(args.config) if args.config else source_root / CONFIG_FILE_NAME
    if not config_path.is_absolute():
        config_path = source_root / config_path

    try:
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

        if args.command == "publish":
            if args.diff:
                # --diff implies --dry-run: previews never publish.
                return cmd_diff(source_root, config)
            if args.json_output and not args.dry_run:
                raise ConfigError("ERROR: --json only applies to status and --dry-run")
            if args.dry_run:
                return cmd_dry_run(source_root, config, as_json=args.json_output)
            cmd_publish(source_root, config, args.message,
                        force_overwrite=args.force_overwrite)
        elif args.command == "init":
            cmd_init(source_root, config)
        elif args.command == "status":
            if args.diff:
                return cmd_diff(source_root, config)
            return cmd_status(source_root, config, as_json=args.json_output,
                              check=args.check, remote=args.remote)
        elif args.command == "integrate":
            return cmd_integrate(source_root, config)
        elif args.command == "validate":
            return cmd_validate(source_root, config, as_json=args.json_output)
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
