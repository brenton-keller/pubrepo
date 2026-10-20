"""Sources whose .publish.toml sits in a subdirectory of the
git repo: the path-base rule for every git call that takes a source
path, with the required regression tests (a)-(e).

Every fixture here is a repo `top/` whose source root is `top/sub/dir/`.
Paths outside the prefix carry the same names as files inside it, with
other content, so a path-base mistake reads or writes the wrong file
instead of only failing. Every run is in-process through main(), with
file:// remotes; pytest's stdin is not a tty.
"""
import json
import shlex

import pytest

import pubrepo
from conftest import git_run

INSIDE = "inside = 1\n"
OUTSIDE = "OUTSIDE the source root\n"
GUIDE = "# Guide\n"
CONFIG_REL = ".publish.toml"
PREFIX = "sub/dir/"
VERSION_RE = r'^VERSION = "(?P<version>[0-9A-Za-z.+-]+)"$'


def _toml(config: dict) -> str:
    """[publish] keys as JSON-quoted values, plus optional sub-tables."""
    tables = {k: config.pop(k) for k in ("release", "scrub") if k in config}
    lines = ["[publish]"] + [f"{k} = {json.dumps(v)}" for k, v in config.items()]
    for name, table in tables.items():
        lines += ["", f"[publish.{name}]"]
        for k, v in table.items():
            if isinstance(v, dict):
                lines += ["", f"[publish.{name}.{k}]"]
                lines += [f"{kk} = {json.dumps(vv)}" for kk, vv in v.items()]
            else:
                lines.append(f"{k} = {json.dumps(v)}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def subworld(tmp_path, public_remote, run):
    """Factory: `top/` with the source root at `top/sub/dir/`, an origin for
    records, an initialized `.publish` and (by default) one publish done.

    Outside the prefix, `top/src/a.py` always exists with OUTSIDE content;
    `shadow=True` also mirrors every other inside file there, so git finds
    a file at every wrong path."""
    counter = {"n": 0}

    def _make(*, files=None, include=("src", "docs"), shadow=False, release=None,
              publish=True, extra_config=None):
        counter["n"] += 1
        top = tmp_path / f"top{counter['n']}"
        src = top / "sub" / "dir"
        src.mkdir(parents=True)
        git_run("init", "-q", "-b", "main", cwd=top)
        git_run("config", "user.email", "owner@example.invalid", cwd=top)
        git_run("config", "user.name", "Owner", cwd=top)
        inside = dict(files or {"src/a.py": INSIDE, "docs/guide.md": GUIDE})
        outside = {"src/a.py": OUTSIDE, "README-top.md": "top level\n"}
        if shadow:
            outside.update({rel: OUTSIDE for rel in inside})
        for base, tree in ((src, inside), (top, outside)):
            for rel, text in tree.items():
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_text(text)
        origin = tmp_path / f"top-origin{counter['n']}.git"
        git_run("init", "--bare", "-q", "-b", "main", str(origin))
        url = public_remote()
        config = {"remote": url, "include": list(include), "private_remotes": [str(origin)],
                  **(extra_config or {})}
        if release:
            config["release"] = release
        (src / CONFIG_REL).write_text(_toml(config))
        (src / ".gitignore").write_text(".publish/\n")
        git_run("add", "-A", cwd=top)
        git_run("commit", "-q", "-m", "init", cwd=top)
        git_run("remote", "add", "origin", str(origin), cwd=top)
        git_run("push", "-q", "-u", "origin", "main", cwd=top)
        ns = _ns(top, src, url, run, tmp_path)
        assert run(["init"], cwd=src).code == 0
        if publish:
            r = ns.publish(*(["--no-release"] if release else []))
            assert r.code == 0, r.stderr
        return ns

    return _make


def _ns(top, src, url, run, tmp_path):
    class NS:
        pass

    ns = NS()
    ns.top, ns.src, ns.pub, ns.url = top, src, src / ".publish", url
    ns.collab_dir = None

    def commit(files, message="source change", *, base=src):
        for rel, text in files.items():
            p = base / rel
            if text is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
        git_run("add", "-A", cwd=top)
        git_run("commit", "-q", "-m", message, cwd=top)
        return git_run("rev-parse", "HEAD", cwd=top).strip()

    def collab(files, message="collaborator change"):
        if ns.collab_dir is None:
            ns.collab_dir = tmp_path / f"collab-{top.name}"
            git_run("clone", "-q", url, str(ns.collab_dir))
            git_run("config", "user.name", "Collab Orator", cwd=ns.collab_dir)
            git_run("config", "user.email", "collab@example.invalid", cwd=ns.collab_dir)
        git_run("pull", "-q", "--ff-only", "origin", "main", cwd=ns.collab_dir)
        for rel, text in files.items():
            p = ns.collab_dir / rel
            if text is None:
                p.unlink()
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
        git_run("add", "-A", cwd=ns.collab_dir)
        git_run("commit", "-q", "-m", message, cwd=ns.collab_dir)
        git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=ns.collab_dir)
        return git_run("rev-parse", "HEAD", cwd=ns.collab_dir).strip()

    ns.commit, ns.collab = commit, collab
    ns.run = lambda *args: run(list(args), cwd=src)
    ns.publish = lambda *args: run(["publish", "--yes", *args], cwd=src)
    ns.tip = lambda: (git_run("ls-remote", url, "refs/heads/main").split() or [None])[0]
    ns.head = lambda: git_run("rev-parse", "HEAD", cwd=top).strip()
    ns.config = lambda: pubrepo.load_publish_config(src / CONFIG_REL)
    ns.status = lambda: git_run("status", "--porcelain", cwd=top)
    return ns


def _published(w, ref="refs/heads/main"):
    """{path: text} of the public tree at `ref`, read from the bare remote."""
    remote = w.url[len("file://"):]
    names = git_run("ls-tree", "-r", "--name-only", ref, cwd=remote).splitlines()
    return {n: git_run("show", f"{ref}:{n}", cwd=remote) for n in names}


def _pull_yes(w, *flags):
    r = w.run("pull", "--plan", *flags)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    assert last.startswith("Plan digest: "), (r.code, r.stdout, r.stderr)
    return w.run("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan",
                 last.split(": ", 1)[1], *flags)


def _blob_text(repo, blob):
    return git_run("cat-file", "blob", blob, cwd=repo)


# --- guard's 8ccaa1d reproduction (named regression) ---

def test_guard_repro_dry_run_fatal_could_not_open(subworld):
    """guard's /tmp-clone probe at 8ccaa1d: from a subdirectory source,
    `hash-object --stdin-paths` resolved source-relative names against the
    repo top, so `--dry-run` failed with `fatal: could not open` (exit 2)."""
    w = subworld(publish=False)
    r = w.run("publish", "--dry-run")
    assert r.code == 0, r.stderr
    assert "could not open" not in r.stderr


def test_source_prefix_helper(subworld):
    w = subworld(publish=False)
    assert pubrepo._source_prefix(w.src) == PREFIX
    assert pubrepo._source_prefix(w.top) == ""
    assert pubrepo._top_to_source(PREFIX, "sub/dir/src/a.py") == "src/a.py"
    assert pubrepo._top_to_source(PREFIX, "src/a.py") is None
    assert pubrepo._top_to_source("", "src/a.py") == "src/a.py"


# --- (a) a same-named file outside the prefix is never read ---

def test_a_publish_hashes_the_inside_file_not_the_outside_one(subworld):
    """(a), 1350: with a same-named file at every wrong path, the published
    blobs are the prefix-internal files, byte for byte, at their
    source-relative names; nothing outside the prefix is published."""
    w = subworld(shadow=True)
    assert _published(w) == {"src/a.py": INSIDE, "docs/guide.md": GUIDE}


def test_a_dry_run_projection_and_pi_sigma_use_inside_blobs(subworld):
    """(a): --dry-run's hashing (build_projection), and Π(σ) from git
    objects (1538 _config_at, 1571 projection_at), both give the inside
    blobs; Π(σ) is not None, and lists no path from outside the prefix."""
    w = subworld(shadow=True, publish=False)
    live = pubrepo.build_projection(w.src, w.pub, w.config())
    assert {p: _blob_text(w.pub, e.blob) for p, e in live.entries.items()} == {
        "src/a.py": INSIDE, "docs/guide.md": GUIDE}
    at = pubrepo.projection_at(w.src, w.pub, w.head(), CONFIG_REL)
    assert at is not None
    assert {p: e.blob for p, e in at.entries.items()} == {
        p: e.blob for p, e in live.entries.items()}


def test_a_follow_up_publish_and_outside_edits(subworld):
    """A follow-up publish carries an inside change; an edit outside the
    prefix (even to the same-named file) changes nothing public."""
    w = subworld(shadow=True)
    w.commit({"src/a.py": "inside = 2\n"})
    assert w.publish().code == 0
    assert _published(w)["src/a.py"] == "inside = 2\n"
    tip = w.tip()
    w.commit({"src/a.py": "OUTSIDE 2\n"}, base=w.top)
    r = w.publish()
    assert r.code == 0, r.stderr
    assert w.tip() == tip
    assert w.run("status", "--check").code == 0


def test_dirty_paths_are_inside_the_prefix_only(subworld):
    """1455 _source_dirty_paths: a modified inside file is dirty; a modified
    same-named file outside the prefix is not."""
    w = subworld(shadow=True, publish=False)
    (w.top / "src" / "a.py").write_text("outside edit\n")
    assert pubrepo._source_dirty_paths(w.src, {"src/a.py", "docs/guide.md"}) == []
    (w.src / "src" / "a.py").write_text("inside edit\n")
    assert pubrepo._source_dirty_paths(w.src, {"src/a.py", "docs/guide.md"}) == ["src/a.py"]


# --- the core flows from sub/dir ---

def test_previews_status_and_validate(subworld):
    w = subworld()
    w.commit({"src/a.py": "inside = 2\n"})
    assert w.run("validate").code == 0
    dry = w.run("publish", "--dry-run", "--json")
    assert dry.code == 0, dry.stderr
    assert OUTSIDE.strip() not in dry.stdout
    diff = w.run("publish", "--diff")
    assert diff.code == 0, diff.stderr
    assert "+inside = 2" in diff.stdout and OUTSIDE.strip() not in diff.stdout
    assert w.run("status").code == 0
    assert w.run("status", "--check").code != 0           # a change is pending
    assert w.publish().code == 0
    assert w.run("status", "--check").code == 0


def test_stage_materializes_inside_content(subworld):
    w = subworld()
    w.commit({"src/a.py": "inside = 3\n"})
    r = w.run("publish", "--stage")
    assert r.code == 0, r.stderr
    assert (w.pub / "src" / "a.py").read_text() == "inside = 3\n"
    assert not (w.pub / "README-top.md").exists() and not (w.pub / "sub").exists()


def test_offterminal_preview_then_printed_command(subworld):
    """#2 from a subdirectory source: the preview and the next command;
    running that command publishes the inside change."""
    w = subworld()
    w.commit({"src/a.py": "inside = 4\n"})
    r = w.run("publish")
    assert r.code == 1
    last = r.stdout.rstrip("\n").split("\n")[-1]
    marker = "Not published: no terminal to confirm. Review the above, then run: "
    assert last.startswith(marker), r.stdout
    argv = shlex.split(last[len(marker):])[1:]
    assert w.run(*argv).code == 0
    assert _published(w)["src/a.py"] == "inside = 4\n"


@pytest.mark.rewrite
def test_amend(subworld):
    w = subworld()
    first = w.tip()
    w.commit({"src/a.py": "inside = 5\n"})
    r = w.publish("--amend")
    assert r.code == 0, r.stderr
    assert w.tip() != first
    assert git_run("rev-list", "--count", "refs/heads/main",
                   cwd=w.url[len("file://"):]).strip() == "1"
    assert _published(w) == {"src/a.py": "inside = 5\n", "docs/guide.md": GUIDE}


# --- pull ---

def test_pull_writes_inside_at_the_prefixed_index_path(subworld):
    """(c), 10420 _stage_blob: a collaborator's change lands in
    sub/dir/docs/guide.md, staged at that index path and committed; the
    top-level tree outside the prefix is untouched; then nothing is left."""
    w = subworld(shadow=True)
    w.collab({"docs/guide.md": "# Guide\nfrom collab\n"})
    r = _pull_yes(w)
    assert r.code == 0, r.stderr
    assert (w.src / "docs" / "guide.md").read_text() == "# Guide\nfrom collab\n"
    assert (w.top / "docs" / "guide.md").read_text() == OUTSIDE
    assert w.status() == ""
    assert git_run("show", "HEAD:sub/dir/docs/guide.md", cwd=w.top) == "# Guide\nfrom collab\n"
    assert w.run("pull", "--plan").code == 0
    assert w.publish().code == 0


def test_b_pull_abort_restores_and_never_deletes(subworld):
    """(b), 10401 _head_blob + 10706 _restore_paths: a conflicted pull is
    aborted; the existing tracked file is restored to its bytes, not
    deleted, and the index is clean."""
    w = subworld()
    w.collab({"docs/guide.md": "# Guide\nT\n", "docs/added.md": "x\n"})
    w.commit({"docs/guide.md": "# Guide\nO\n"})
    r = _pull_yes(w, "--accept", "docs/added.md")
    assert r.code == 8, r.stderr
    assert "<<<<<<< ours" in (w.src / "docs" / "guide.md").read_text()
    assert w.run("pull", "--abort").code == 0
    assert (w.src / "docs" / "guide.md").read_text() == "# Guide\nO\n"
    assert not (w.src / "docs" / "added.md").exists()
    assert w.status() == ""


def test_c_pull_continue_stages_the_prefixed_path(subworld):
    """(c), 10768 _cmd_pull_continue: the resolved file is staged at
    sub/dir/…, committed, and the tree is clean afterwards."""
    w = subworld()
    w.collab({"docs/guide.md": "# Guide\nT\n"})
    w.commit({"docs/guide.md": "# Guide\nO\n"})
    assert _pull_yes(w).code == 8
    (w.src / "docs" / "guide.md").write_text("# Guide\nO and T\n")
    r = w.run("pull", "--continue", "--yes")
    assert r.code == 0, r.stderr
    assert git_run("show", "HEAD:sub/dir/docs/guide.md", cwd=w.top) == "# Guide\nO and T\n"
    assert w.status() == ""
    assert pubrepo.pull_in_progress(w.src) is None


def test_legacy_snapshot_gives_no_baseline_items(subworld):
    """A 1.0.x-shaped snapshot (only the `Source:` trailer) of a clean
    publish, then an inside change: the snapshot is verified against Π(σ),
    read at the prefixed config path (1538, 1571), so pull finds 0 baseline
    items (guard saw every changed path as one), and the verdict cache key
    carries the config blob at σ (9486)."""
    w = subworld(publish=False)
    sigma = w.head()
    for rel in ("src/a.py", "docs/guide.md"):
        (w.pub / rel).parent.mkdir(parents=True, exist_ok=True)
        (w.pub / rel).write_text((w.src / rel).read_text())
    git_run("add", "-A", cwd=w.pub)
    git_run("-c", "user.name=Owner", "-c", "user.email=owner@example.invalid", "commit", "-q",
            "-m", f"publish: 2026-10-01 12:00\n\nSource: {sigma[:7]}\nBranch: main\n", cwd=w.pub)
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=w.pub)
    w.commit({"src/a.py": "inside = 7\n"})
    for flags in ((), ("--baseline",)):
        r = w.run("pull", "--plan", *flags)
        assert r.code == 0, r.stdout + r.stderr
        assert "baseline" not in r.stdout.lower()
    config_blob = git_run("rev-parse", f"{sigma}:sub/dir/{CONFIG_REL}", cwd=w.top).strip()
    keys = json.loads((w.pub / ".git" / "pubrepo" / "verdicts.json").read_text())
    assert keys and {k.split(":")[2] for k in keys} == {config_blob}
    assert w.publish().code == 0
    assert _published(w)["src/a.py"] == "inside = 7\n"


# --- repair and restore-ancestry ---

LEAK = "leaked material, long enough to be content-matched " * 3 + "\n"


def _remote_refs(w):
    out = git_run("ls-remote", w.url, cwd=w.src)
    return {n: s for s, n in (line.split("\t") for line in out.splitlines()) if n != "HEAD"}


def _digest(r):
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith("Plan digest: "), (r.code, r.stdout, r.stderr)
    return last.split(": ", 1)[1]


@pytest.mark.rewrite
def test_repair_remove_path(subworld):
    w = subworld(files={"src/a.py": INSIDE, "docs/guide.md": GUIDE, "secret.txt": LEAK},
                 include=("src", "docs", "secret.txt"), shadow=True)
    first = w.tip()
    cfg = w.src / CONFIG_REL
    cfg.write_text(cfg.read_text().replace(', "secret.txt"', ""))
    w.commit({"secret.txt": None}, "remove secret")
    args = ("repair", "remove-path", "secret.txt", "--from", first)
    r = w.run(*args, "--plan")
    assert r.code == 0, r.stderr
    r = w.run(*args, "--apply", "--yes", "--expect-tip", _remote_refs(w)["refs/heads/main"],
              "--expect-plan", _digest(r))
    assert r.code == 0, r.stderr
    assert _published(w) == {"src/a.py": INSIDE, "docs/guide.md": GUIDE}


@pytest.mark.rewrite
def test_restore_ancestry(subworld, tmp_path):
    w = subworld(release={"enabled": True, "version_file": "version.py",
                          "version_pattern": VERSION_RE, "tag_format": "v{version}",
                          "source_tag": False, "adopt_tags": ["v1.0.0"]},
                 files={"src/a.py": INSIDE, "docs/guide.md": GUIDE,
                        "version.py": 'VERSION = "1.0.0"\n'},
                 include=("src", "docs", "version.py"))
    w.commit({"docs/rel.md": "released\n"})
    assert w.publish("--no-release").code == 0
    rel = w.tip()
    git_run("tag", "v1.0.0", rel, cwd=w.pub)
    git_run("push", "-q", "origin", "refs/tags/v1.0.0", cwd=w.pub)
    dropper = tmp_path / "dropper"
    git_run("clone", "-q", w.url, str(dropper))
    git_run("reset", "-q", "--hard", "HEAD~1", cwd=dropper)
    (dropper / "other.txt").write_text("o\n")
    git_run("add", "-A", cwd=dropper)
    git_run("-c", "user.name=Collab Orator", "-c", "user.email=collab@example.invalid",
            "commit", "-qm", "drop the release", cwd=dropper)
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=dropper)
    flags = ["--accept-rewritten-history", "--keep", "docs/rel.md"]
    r = _pull_yes(w, *flags)
    assert r.code == 0, r.stderr
    r = w.run("repair", "restore-ancestry", "v1.0.0", "--plan")
    assert r.code == 0, r.stdout + r.stderr
    tip = _remote_refs(w)["refs/heads/main"]
    r = w.run("repair", "restore-ancestry", "v1.0.0", "--apply", "--yes",
              "--expect-tip", tip, "--expect-plan", _digest(r))
    assert r.code == 0, r.stderr
    new = _remote_refs(w)["refs/heads/main"]
    assert git_run("rev-list", "--parents", "-n1", new, cwd=w.pub).split()[1:] == [tip, rel]


# --- release ---

RELEASE = {"enabled": True, "version_file": "version.py", "version_pattern": VERSION_RE,
           "tag_format": "v{version}", "source_tag": False,
           "github": {"enabled": True, "repository": "owner/repo"}}


def _release_world(subworld, *, shadow=True):
    return subworld(release=RELEASE, shadow=shadow,
                    files={"src/a.py": INSIDE, "docs/guide.md": GUIDE,
                           "version.py": 'VERSION = "1.0.0"\n', "CHANGELOG.md": "# 1.0.0\n"},
                    include=("src", "docs", "version.py", "CHANGELOG.md"))


def test_a_release_export_and_resume(subworld, monkeypatch):
    """(a) in the release export, and a provider failure resumed: the export
    the provider sees and the public tag hold the inside bytes only."""
    w = _release_world(subworld)
    w.commit({"src/a.py": "inside = 6\n"})
    seen, failed = [], {"done": False}

    class Fake:
        name = "github"

        def check_live(self, _ctx):
            return []

        def build(self, ctx):
            seen.append({p.relative_to(ctx.export_dir).as_posix(): p.read_text()
                         for p in ctx.export_dir.rglob("*") if p.is_file()})

        def reconcile(self, _ctx):
            return None

        def execute(self, _ctx, stage):
            if stage == "publish" and not failed["done"]:
                failed["done"] = True
                raise pubrepo.ReleaseError("injected provider failure")

    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [Fake()])
    first = w.run("--release", "1.0.0", "--yes")
    assert first.code == 7, first.stderr
    second = w.run("--release", "1.0.0", "--yes")
    assert second.code == 0, second.stderr
    assert seen and seen[0] == {"src/a.py": "inside = 6\n", "docs/guide.md": GUIDE,
                                "version.py": 'VERSION = "1.0.0"\n', "CHANGELOG.md": "# 1.0.0\n"}
    assert _published(w, "refs/tags/v1.0.0")["src/a.py"] == "inside = 6\n"


def test_d_release_clean_check_sees_inside_dirty_files(subworld):
    """(d), 4685 publish_set_dirty_paths: an uncommitted inside change is
    reported and blocks the release; the same-named file outside the prefix
    is never reported."""
    w = _release_world(subworld)
    (w.top / "src" / "a.py").write_text("outside edit\n")
    assert pubrepo.publish_set_dirty_paths(w.src, w.config()) == []
    (w.src / "src" / "a.py").write_text("inside edit\n")
    assert pubrepo.publish_set_dirty_paths(w.src, w.config()) == ["src/a.py"]
    r = w.run("--release", "1.0.0", "--yes")
    assert r.code != 0 and "src/a.py" in r.stderr
    assert "refs/tags/v1.0.0" not in _remote_refs(w)


# --- (e) attributes inside the prefix ---

def test_e_prefix_gitattributes_apply_in_project_blob(subworld, monkeypatch):
    """(e), 1631: `sub/dir/.gitattributes` (eol=crlf) is honored by
    project_blob's smudge (`cat-file --filters --path=`), as checkout
    would apply it in the source worktree."""
    w = subworld(publish=False)
    (w.src / ".gitattributes").write_text("*.md text eol=crlf\n")
    w.commit({}, "attributes")
    blob = git_run("rev-parse", "HEAD:sub/dir/docs/guide.md", cwd=w.top).strip()
    smudged = []
    real = pubrepo._hash_bytes

    def spy(repo, env, data, *, write, path=None):
        smudged.append((path, data))
        return real(repo, env, data, write=write, path=path)

    monkeypatch.setattr(pubrepo, "_hash_bytes", spy)
    entry = pubrepo.project_blob(w.src, w.pub, "docs/guide.md", blob, w.config())
    assert entry is not None and entry.blob == blob
    assert smudged == [("docs/guide.md", b"# Guide\r\n")]
