"""Shared harness for the blind ff black-box suite (tests/test_ff_blackbox_*.py).

Derived only from the public CLI contract and the design spec. Drives the
CLI as a subprocess; imports no project code; synthetic repos built with plain git.
"""
from __future__ import annotations

import io
import json
import os
import re
import secrets
import shutil
import struct
import subprocess
import sys
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN = Path(os.environ.get("PUBREPO_BIN", REPO_ROOT / "pubrepo.py"))

OWNER = ("Owner", "owner@example.invalid")
COLLAB = ("Collab Orator", "collab@example.invalid")
DIGEST_LINE = re.compile(r"^Plan digest: ([0-9a-f]{64})$")
PULL_MSG = re.compile(r"run `pubrepo(\.py)? pull`")


@dataclass(frozen=True)
class Result:
    code: int
    stdout: str
    stderr: str

    @property
    def out(self) -> str:
        return self.stdout + "\n" + self.stderr

    def __str__(self) -> str:  # shown by pytest on assertion failure
        return f"<exit {self.code}>\nSTDOUT:\n{self.stdout}\nSTDERR:\n{self.stderr}"


def digest_of(r: Result) -> str:
    lines = [ln for ln in r.stdout.splitlines() if ln.strip()]
    assert lines, f"no stdout, expected a Plan digest line\n{r}"
    m = DIGEST_LINE.match(lines[-1].strip())
    assert m, f"last stdout line is not 'Plan digest: <64 hex>'\n{r}"
    return m.group(1)


def q(s: str) -> str:
    return json.dumps(s)


def arr(xs) -> str:
    return "[" + ", ".join(q(x) for x in xs) + "]"


def png_bytes() -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00" + secrets.token_bytes(3)))
            + chunk(b"IEND", b""))


def opaque_bytes(n: int = 256) -> bytes:
    return b"\x00\x01BIN\x00" + secrets.token_bytes(n)


def xlsx_bytes(shared_string: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>')
        z.writestr("xl/sharedStrings.xml",
                   '<?xml version="1.0" encoding="UTF-8"?><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                   f"<si><t>{shared_string}</t></si></sst>")
    return buf.getvalue()


MARKERS = "<<<<<<< ours\nmine\n=======\ntheirs\n>>>>>>> theirs\n"


class World:
    """Source repo + bare public remote + isolated git config, all under tmp."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        (self.home / ".gitconfig").write_text(
            "[user]\n\tname = Owner\n\temail = owner@example.invalid\n"
            "[init]\n\tdefaultBranch = main\n"
            "[protocol \"file\"]\n\tallow = always\n"
            "[commit]\n\tgpgsign = false\n[tag]\n\tgpgsign = false\n"
            "[advice]\n\tdetachedHead = false\n")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("GIT_", "PUBREPO_"))}
        env.update(HOME=str(self.home), GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=str(self.home / ".gitconfig"),
                   LC_ALL="C", NO_COLOR="1",
                   GIT_ALLOW_PROTOCOL="file", GIT_TERMINAL_PROMPT="0")
        # design rev 0.7 §17.4 (d): the conftest sandbox audit requires this on every child.
        if "PUBREPO_TEST_SANDBOX" in os.environ:
            env["PUBREPO_TEST_SANDBOX"] = os.environ["PUBREPO_TEST_SANDBOX"]
        self.env = env
        self.public = self.bare("public.git")
        self.src = tmp / "src"
        self.src.mkdir()
        self.git("init", "-q", cwd=self.src)
        self._collab_n = 0

    # ---- git plumbing -------------------------------------------------
    def git(self, *args, cwd=None, check=True, env=None) -> str:
        p = subprocess.run(["git", *args], cwd=cwd or self.src, env=env or self.env,
                           capture_output=True, text=True)
        if check and p.returncode:
            raise AssertionError(f"git {' '.join(args)} failed ({p.returncode}):\n{p.stderr}")
        return p.stdout.strip()

    def git_bytes(self, *args, cwd) -> bytes:
        return subprocess.run(["git", *args], cwd=cwd, env=self.env,
                              capture_output=True, check=True).stdout

    def bare(self, name: str) -> Path:
        p = self.tmp / name
        subprocess.run(["git", "init", "-q", "--bare", str(p)], cwd=self.tmp, env=self.env, check=True)
        return p

    # ---- CLI ----------------------------------------------------------
    def cli(self, *args, cwd=None, env_extra=None, timeout=120) -> Result:
        env = dict(self.env)
        if env_extra:
            env.update(env_extra)
        p = subprocess.run([sys.executable, str(BIN), *args], cwd=cwd or self.src,
                           env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout)
        return Result(p.returncode, p.stdout, p.stderr)

    # ---- source helpers -----------------------------------------------
    def write(self, rel: str, data, root: Path | None = None) -> None:
        p = (root or self.src) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.write_bytes if isinstance(data, bytes) else p.write_text)(data)

    def commit(self, msg="change", root: Path | None = None) -> str:
        root = root or self.src
        self.git("add", "-A", cwd=root)
        self.git("commit", "-q", "--allow-empty", "-m", msg, cwd=root)
        return self.git("rev-parse", "HEAD", cwd=root)

    def head(self, root: Path | None = None) -> str:
        return self.git("rev-parse", "HEAD", cwd=root or self.src)

    def config(self, *, remote: Path | None = None, include=("src/", "README.md"),
               exclude=(), keep=(), forbidden=(), allow_opaque=(), name=".publish.toml",
               dir: str | None = None, publish_extra: str = "", release: bool = False,
               root: Path | None = None) -> None:
        lines = ["[publish]", f"remote = {q('file://' + str(remote or self.public))}",
                 f"include = {arr(include)}"]
        if exclude:
            lines.append(f"exclude = {arr(exclude)}")
        if keep:
            lines.append(f"keep = {arr(keep)}")
        if dir:
            lines.append(f"dir = {q(dir)}")
        if publish_extra:
            lines.append(publish_extra)
        lines += ["", "[publish.scrub]", f"forbidden = {arr(forbidden or ['zz-canary-' + secrets.token_hex(6)])}"]
        if allow_opaque:
            lines.append(f"allow_opaque = {arr(allow_opaque)}")
        if release:
            lines += ["", "[publish.release]", "enabled = true", 'version_file = "version.txt"',
                      "version_pattern = '^(?P<version>[0-9A-Za-z.+-]+)$'", 'tag_format = "v{version}"']
        self.write(name, "\n".join(lines) + "\n", root=root)

    def base_files(self, root: Path | None = None) -> None:
        self.write(".gitignore", ".publish*/\n", root=root)
        self.write("src/app.py", "print('hi')\n", root=root)
        self.write("README.md", "# readme\n\nline two\n\nline three\n", root=root)

    # ---- public helpers -----------------------------------------------
    def tip(self, remote: Path | None = None, branch="main") -> str | None:
        out = self.git("ls-remote", str(remote or self.public), f"refs/heads/{branch}")
        return out.split()[0] if out else None

    def public_refs(self, remote: Path | None = None) -> str:
        return self.git("ls-remote", str(remote or self.public))

    def public_tree(self, ref="refs/heads/main", remote: Path | None = None) -> dict[str, bytes]:
        r = remote or self.public
        out = self.git_bytes("ls-tree", "-r", "-z", ref, cwd=r)
        tree = {}
        for ent in filter(None, out.split(b"\0")):
            meta, path = ent.split(b"\t", 1)
            tree[path.decode()] = self.git_bytes("cat-file", "blob", meta.split()[2].decode(), cwd=r)
        return tree

    def is_ancestor(self, a: str, b: str, remote: Path | None = None) -> bool:
        return subprocess.run(["git", "merge-base", "--is-ancestor", a, b], cwd=remote or self.public,
                              env=self.env).returncode == 0

    def public_git(self, *args, remote: Path | None = None, check=True) -> str:
        return self.git(*args, cwd=remote or self.public, check=check)

    def records(self, root: Path | None = None) -> dict[str, str]:
        out = self.git("for-each-ref", "--format=%(refname) %(objectname)", "refs/pubrepo/",
                       cwd=root or self.src)
        return dict(line.split() for line in out.splitlines() if line)

    def record_json(self, ref: str, root: Path | None = None) -> dict:
        return json.loads(self.git("show", f"{ref}:record.json", cwd=root or self.src))

    # ---- collaborator -------------------------------------------------
    def collab_clone(self, remote: Path | None = None) -> Path:
        self._collab_n += 1
        d = self.tmp / f"collab{self._collab_n}"
        self.git("clone", "-q", str(remote or self.public), str(d), cwd=self.tmp)
        self.git("config", "user.name", COLLAB[0], cwd=d)
        self.git("config", "user.email", COLLAB[1], cwd=d)
        return d

    def collab_push(self, writes=None, deletes=(), msg="collab change",
                    remote: Path | None = None) -> str:
        d = self.collab_clone(remote)
        for rel, data in (writes or {}).items():
            self.write(rel, data, root=d)
        for rel in deletes:
            self.git("rm", "-q", "-r", rel, cwd=d)
        sha = self.commit(msg, root=d)
        self.git("push", "-q", "origin", "HEAD:main", cwd=d)
        return sha

    # ---- composite flows ----------------------------------------------
    def solo(self, **cfg) -> str:
        """Base files + config, committed, init, first publish. Returns the public tip."""
        self.base_files()
        self.config(**cfg)
        self.commit("initial")
        r = self.cli("init")
        assert r.code == 0, r
        r = self.cli("publish", "--yes")
        assert r.code == 0, r
        return self.tip()

    def pull_apply(self, *flags, expect_plan_code=None, cwd=None) -> tuple[Result, Result]:
        """`pull --plan FLAGS` then `pull --yes --expect-tip --expect-plan FLAGS`."""
        plan = self.cli("pull", "--plan", *flags, cwd=cwd)
        if expect_plan_code is not None:
            assert plan.code == expect_plan_code, plan
        d = digest_of(plan)
        tip = self.tip()
        apply = self.cli("pull", "--yes", "--expect-tip", tip, "--expect-plan", d, *flags, cwd=cwd)
        return plan, apply


def tree_snapshot(root: Path) -> dict[str, bytes]:
    out = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        dirs = rel.parts[:-1]
        if p.is_file() and ".git" not in rel.parts and not any(d.startswith(".publish") for d in dirs):
            out[str(rel)] = p.read_bytes()
    return out


def make_git_shim(w: World) -> Path:
    """A `git` on PATH that logs argv as JSON lines then execs the real git."""
    real = shutil.which("git", path=w.env["PATH"])
    shim_dir = w.tmp / "shim"
    shim_dir.mkdir(exist_ok=True)
    log = w.tmp / "git-argv.log"
    shim = shim_dir / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"os.execv({real!r}, [{real!r}] + sys.argv[1:])\n")
    shim.chmod(0o755)
    w.env["PATH"] = f"{shim_dir}{os.pathsep}{w.env['PATH']}"
    log.write_text("")
    return log


def read_shim_log(log: Path) -> list[list[str]]:
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
