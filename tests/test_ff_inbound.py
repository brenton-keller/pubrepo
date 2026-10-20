"""Inbound checks (design §5.2,
AC33 (a)–(g), AC34 module halves).

Each test builds the incoming side (a collaborator commit), runs the real
S3 integration and the real gate, and asserts the flags pull will attach.
"Exit 8 and nothing written" is pull's behaviour on a flagged item and is
asserted in tests/test_ff_pull.py.
"""
import os

import pytest

import pubrepo
from conftest import git_run

FORBIDDEN = "zz-inbound-canary-zz"


def _item(w, path, tip=None):
    tip = tip or w.tip()
    entry = git_run("ls-tree", tip, "--", path, cwd=w.pub).split()
    mode, blob = (entry[0], entry[2]) if entry else (None, None)
    item = pubrepo.PullItem(path=path, cls="C1", base=None, ours=None, theirs=blob,
                            origin="range", snapshot=None, flags=(), options=("accept",),
                            default=None, prefilled_from=None)
    return item, mode


def _flags(w, path, *, plan_paths=None, config=None):
    config = config or w.config()
    integ = w.integ(config=config)
    item, mode = _item(w, path)
    proj = pubrepo.build_projection(w.src, w.pub, config)
    ctx = pubrepo.make_inbound_ctx(w.src, w.pub, config, config_path=str(w.src / ".publish.toml"),
                                   proj=proj, integ=integ,
                                   plan_paths=plan_paths or [path], incoming_blobs=[item.theirs])
    return {f.kind: f for f in pubrepo.inbound_flags(item, ctx, mode=mode)}


# --- (a) forbidden content: never accept ---

def test_forbidden_content_flagged_never_accept(world):
    w = world(scrub={"forbidden": [FORBIDDEN]})
    w.snapshot()
    w.collab_commit({"docs/note.md": f"text {FORBIDDEN} text\n"})
    flags = _flags(w, "docs/note.md")
    assert "scrub" in flags
    assert "accept" not in flags["scrub"].allowed
    assert "already public" in flags["scrub"].detail


def test_marker_triple_incoming_is_scrub_flag(world):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/c.md": "<<<<<<< ours\na\n=======\nb\n>>>>>>> theirs\n"})
    assert "scrub" in _flags(w, "docs/c.md")


def test_clean_text_has_no_flags(world):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/plain.md": "plain\n"})
    assert _flags(w, "docs/plain.md") == {}


# --- (b) cross-destination ---

def test_second_root_target_is_flagged(world, public_remote):
    w = world()
    (w.src / ".publish-b.toml").write_text(
        f'[publish]\nremote = "{public_remote()}"\ninclude = ["docs"]\n')
    w.snapshot()
    w.collab_commit({"docs/shared.md": "s\n"})
    flags = _flags(w, "docs/shared.md")
    assert ".publish-b.toml" in flags["cross_destination"].detail
    assert set(flags["cross_destination"].allowed) == {"accept", "accept-excluded",
                                                        "decline", "leave"}


def test_registered_and_peer_targets_are_known(world, public_remote, tmp_path):
    w = world()
    reg = tmp_path / "elsewhere.toml"
    reg.write_text(f'[publish]\nremote = "{public_remote()}"\ninclude = ["docs"]\n')
    pubrepo.register_target(w.src, str(reg), ("github.com/acme/reg", "main"))
    (w.src / "cfg").mkdir()
    (w.src / "cfg" / "peer.toml").write_text(
        f'[publish]\nremote = "{public_remote()}"\ninclude = ["a.py"]\n')
    cfg = dict(w.config(), peers=["cfg/peer.toml"])
    targets, possible = pubrepo.known_targets(w.src, cfg)
    names = {os.path.basename(t.config_path) for t in targets}
    assert {"elsewhere.toml", "peer.toml", ".publish.toml"} <= names
    assert possible == []


def test_unregistered_nested_config_is_listed_not_flagged(world, public_remote):
    """AC33 (b): docs/example/.publish.toml is a possible target."""
    w = world()
    (w.src / "docs" / "example").mkdir()
    (w.src / "docs" / "example" / ".publish.toml").write_text(
        f'[publish]\nremote = "{public_remote()}"\ninclude = ["docs"]\n')
    _targets, possible = pubrepo.known_targets(w.src, w.config())
    assert possible == ["docs/example/.publish.toml"]
    w.snapshot()
    w.collab_commit({"docs/shared.md": "s\n"})
    assert "cross_destination" not in _flags(w, "docs/shared.md")


def test_same_destination_other_config_is_not_cross(world):
    """A second config file for the same destination is not a second target."""
    w = world()
    (w.src / ".publish-copy.toml").write_text((w.src / ".publish.toml").read_text())
    w.snapshot()
    w.collab_commit({"docs/shared.md": "s\n"})
    assert "cross_destination" not in _flags(w, "docs/shared.md")


# --- (c) and (g): build, auto-execution, version_file ---

DESIGN_LIST_SAMPLES = [
    ".github/workflows/ci.yml", ".gitlab-ci.yml", "hooks/pre-commit", ".githooks/x",
    "pyproject.toml", "setup.py", "setup.cfg", "MANIFEST.in", "Makefile", "rules.mk",
    "Justfile", "package.json", "Cargo.toml", "build.rs", "Dockerfile", "run.sh", "run.ps1",
    "tests/conftest.py", "site/x.pth", "tox.ini", "noxfile.py", ".pre-commit-config.yaml",
    ".envrc", ".vscode/tasks.json", "requirements-dev.txt", ".gitattributes", ".gitmodules",
    ".publish.toml", ".publish-prod.toml", "GNUmakefile", "Containerfile",
]


@pytest.mark.parametrize("path", DESIGN_LIST_SAMPLES)
def test_exec_list_verbatim(path):
    """inbound::exec_list_verbatim — every design §5.2 item 3 entry."""
    assert pubrepo.is_exec_or_build(path, "100644", {})


@pytest.mark.parametrize("path", ["makefile", "DOCKERFILE", "PyProject.TOML", "sub/ContainerFile",
                                  ".GitHub/workflows/ci.yml", "Hooks/pre-push", "RUN.SH",
                                  ".VSCode/Tasks.json", "Requirements-Dev.TXT"])
def test_exec_list_case_insensitive(path):
    """Names match case-insensitively (design §5.2 item 3)."""
    assert pubrepo.is_exec_or_build(path, "100644", {})


@pytest.mark.parametrize("path", ["README.md", "docs/guide.md", "src/app.py", "githooks.md",
                                  "my-hooks/readme.txt"])
def test_ordinary_paths_not_exec(path):
    assert not pubrepo.is_exec_or_build(path, "100644", {})


def test_exec_bit_and_version_file():
    assert pubrepo.is_exec_or_build("tools/thing", "100755", {})
    cfg = {"release": {"version_file": "pkg/version.py"}}
    assert pubrepo.is_exec_or_build("pkg/version.py", "100644", cfg)
    assert not pubrepo.is_exec_or_build("pkg/other.py", "100644", cfg)


def test_build_file_from_collaborator_is_flagged(world):
    w = world(include=["a.py", "docs", "pyproject.toml"])
    w.snapshot()
    w.collab_commit({"pyproject.toml": "[project]\nname='x'\n"})
    flags = _flags(w, "pyproject.toml")
    assert "exec_build" in flags and flags["exec_build"].allowed is None


# --- (d) symlinks and type changes ---

def test_symlink_offers_only_leave_remove(world):
    w = world()
    w.snapshot()
    clone = w.pub.parent / "sym-collab"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    os.symlink("../a.py", clone / "docs" / "link.md")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "symlink", cwd=clone)
    git_run("push", "-q", "origin", "HEAD:main", cwd=clone)
    flags = _flags(w, "docs/link.md")
    assert flags["type_change"].allowed == ("leave", "remove")
    assert "scrub" not in flags and "unreadable" not in flags   # never scanned as content


# --- (e) unreadable content ---

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4


def test_unreadable_binary_flagged_with_allowlist_line(world):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/pic.png": PNG})
    flags = _flags(w, "docs/pic.png")
    assert "allow_opaque" in flags["unreadable"].detail


def test_allowlisted_binary_not_flagged(world):
    w = world(scrub={"allow_opaque": ["docs/*.png"]})
    w.snapshot()
    w.collab_commit({"docs/pic.png": PNG})
    assert "unreadable" not in _flags(w, "docs/pic.png")


# --- (f) reintroduced repaired content ---

def _applied(w, removed=(), blobs=()):
    pubrepo.write_record(w.src, "refs/pubrepo/repairs/" + "c" * 64 + "/applied",
                         {"removed": [list(x) for x in removed], "removed_blobs": list(blobs),
                          "map": {}, "left_reachable": []})


def test_repaired_blob_readded_at_new_path_is_flagged(world):
    w = world()
    w.snapshot()
    big = "leaked material " * 8 + "\n"
    f = w.collab_commit({"docs/again.md": big})
    blob = w.blob(f, "docs/again.md")
    _applied(w, removed=[("old/leak.md", blob)], blobs=[blob])
    flags = _flags(w, "docs/again.md")
    assert flags["reintroduced"].allowed == ("decline", "leave", "remove")


def test_small_repaired_blob_matches_only_exact_pair(world):
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/tiny.md": "tiny\n"})
    blob = w.blob(f, "docs/tiny.md")
    _applied(w, removed=[("old/tiny.md", blob)], blobs=[blob])
    assert "reintroduced" not in _flags(w, "docs/tiny.md")
    _applied_exact = [("docs/tiny.md", blob)]
    pubrepo.write_record(w.src, "refs/pubrepo/repairs/" + "b" * 64 + "/applied",
                         {"removed": [list(x) for x in _applied_exact], "removed_blobs": [],
                          "map": {}, "left_reachable": []})
    assert "reintroduced" in _flags(w, "docs/tiny.md")


def test_path_alone_never_matches(world):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/clean.md": "sanitized content, long enough to matter " * 3})
    _applied(w, removed=[("docs/clean.md", "1" * 40)], blobs=["1" * 40])
    assert "reintroduced" not in _flags(w, "docs/clean.md")


# --- private paths (§5.2 item 4, AC34) ---

def test_private_directory_with_no_projected_descendant(world):
    """Shape: source has secret/own.txt (excluded); a collaborator adds
    secret/x → private, leave/remove only; the source dir is untouched."""
    w = world(include=["a.py", "docs", "secret"], exclude=["secret/"])
    (w.src / "secret").mkdir()
    (w.src / "secret" / "own.txt").write_text("owner only\n")
    before = (w.src / "secret" / "own.txt").read_bytes()
    os.chmod(w.src / "secret" / "own.txt", 0)          # never read: lstat only
    try:
        w.snapshot()
        w.collab_commit({"secret/x": "from collab\n"})
        flags = _flags(w, "secret/x")
    finally:
        os.chmod(w.src / "secret" / "own.txt", 0o644)
    assert flags["private"].allowed == ("leave", "remove")
    assert (w.src / "secret" / "own.txt").read_bytes() == before
    assert sorted(p.name for p in (w.src / "secret").iterdir()) == ["own.txt"]


def test_existing_unprojected_source_path_is_private(world):
    """(a): the path exists in source and is not projected (B12a)."""
    w = world()
    (w.src / "notes.md").write_text("private notes\n")
    w.snapshot()
    w.collab_commit({"notes.md": "collab notes\n"})
    assert "private" in _flags(w, "notes.md")


def test_private_glob(world):
    w = world(private=["docs/internal/*"])
    w.snapshot()
    w.collab_commit({"docs/internal/plan.md": "p\n"})
    assert "private" in _flags(w, "docs/internal/plan.md", config=w.config())


def test_root_level_public_only_file_is_not_private(world):
    """AC34: a root-level public-only file is not private (it defaults to
    `leave` in pull)."""
    w = world()
    w.snapshot()
    w.collab_commit({"NEWS.md": "news\n"})
    assert "private" not in _flags(w, "NEWS.md")


def test_path_we_wrote_is_never_private(world):
    w = world()
    w.snapshot()
    (w.src / ".publish.toml").write_text((w.src / ".publish.toml").read_text().replace(
        '"a.py", "docs"', '"a.py"'))
    git_run("commit", "-qam", "drop docs", cwd=w.src)
    w.collab_commit({"docs/guide.md": "# Guide\nedit\n"})
    assert "private" not in _flags(w, "docs/guide.md")


# --- unsafe paths (§5.2 item 5) ---

def test_case_collision_is_unsafe(world):
    w = world(include=["a.py", "docs", "README.md"], files={"a.py": "a\n",
                                                             "docs/guide.md": "g\n",
                                                             "README.md": "r\n"})
    w.snapshot()
    w.collab_commit({"readme.md": "lowercase twin\n"})
    flags = _flags(w, "readme.md")
    assert "case collision with README.md" in flags["unsafe"].detail
    assert flags["unsafe"].allowed == ("leave", "remove")


@pytest.mark.parametrize("path,why", [
    ("/etc/passwd", "absolute"), ("C:/x", "absolute"), ("a/../b", "escapes"),
    ("./a", "escapes"), (".git/config", ".git"), ("x/.GIT/hooks/post", ".git")])
def test_unsafe_path_shapes(world, path, why):
    w = world()
    w.snapshot()
    ctx = pubrepo.make_inbound_ctx(
        w.src, w.pub, w.config(), config_path=None,
        proj=pubrepo.build_projection(w.src, w.pub, w.config()), integ=w.integ(),
        plan_paths=[path], incoming_blobs=[])
    assert why in pubrepo._unsafe(path, ctx)
