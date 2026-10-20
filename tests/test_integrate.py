"""integrate subcommand: print-only recipe for pulling foreign commits back.

The recipe must be EXACT — the roundtrip test executes the printed commands
verbatim and proves the foreign change lands in source and republishes.
"""
import subprocess
import pytest
from pathlib import Path

import pubrepo
from conftest import git_run


def _foreign_commit(remote_url: str, workdir: Path, files: dict,
                    message: str = "foreign work", marker: str = "collab") -> None:
    """Simulate a collaborator committing directly to the public repo."""
    work = workdir / f"collab-{marker}"
    if not work.exists():
        git_run("clone", "-q", remote_url, str(work))
        git_run("config", "user.email", "collab@example.com", cwd=work)
        git_run("config", "user.name", "Collab Orator", cwd=work)
    for rel, content in files.items():
        p = work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            p.unlink()
        else:
            p.write_text(content)
    git_run("add", "-A", cwd=work)
    git_run("commit", "-qm", message, cwd=work)
    git_run("push", "-q", "origin", "main", cwd=work)


def _setup_published(source_repo, public_remote, run, files=None, config_extra=None):
    remote = public_remote()
    files = files or {"a.py": "v1\n", "src/lib.py": "lib v1\n"}
    config = {"remote": remote, "include": ["a.py", "src/"]}
    config.update(config_extra or {})
    repo = source_repo(files, config)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    return remote, repo


# Rewrites for the integrate alias (the sync side owns pull;
# design §7 "`integrate`: an alias for `pull --plan` for one release", §10
# item 5, AC65). The recipe is retired with --force-overwrite.

def _digest(r):
    return r.stdout.strip().splitlines()[-1].split(": ", 1)[1]


def _integrate(run, repo):
    """integrate == pull --plan + a notice (AC65): same exit, same digest."""
    r = run(["integrate"], cwd=repo)
    p = run(["pull", "--plan"], cwd=repo)
    assert r.code == p.code
    if p.stdout.strip():
        assert _digest(r) == _digest(p)
    return r


def test_nothing_to_integrate(source_repo, public_remote, run):
    """§10 item 5: nothing foreign → the plan has no range and no items, 0."""
    _, repo = _setup_published(source_repo, public_remote, run)
    r = _integrate(run, repo)
    assert r.code == 0
    assert "unpulled commits" not in r.stdout and "items (" not in r.stdout


@pytest.mark.no_git_calls  # AC63: refused before any git call
def test_not_initialized_errors(source_repo, public_remote, run):
    """§10 item 5: no `.publish/` → 1, "not initialized", before any git call."""
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    r = run(["integrate"], cwd=repo)
    assert r.code == 1
    assert "not initialized" in r.stderr


def test_recipe_clean_surface(source_repo, public_remote, run, tmp_path):
    """The alias output is the plan, with no recipe (§7, §10 item 5)."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"src/lib.py": "lib v2 from collab\n"})
    r = _integrate(run, repo)
    assert r.code == 5                                       # items exist (§5.4)
    assert "unpulled commits (1)" in r.stdout
    assert "Collab Orator <collab@example.com>" in r.stdout
    assert "src/lib.py  [theirs_changed]" in r.stdout and "→ accept (default)" in r.stdout
    for retired in ("git apply", "--force-overwrite", "git -C .publish diff"):
        assert retired not in r.stdout
    assert "pull --plan" in r.stderr                          # the alias notice


def test_junk_is_filtered_and_reported(source_repo, public_remote, run, tmp_path):
    """§10 item 5, INV-3 (c): outside-manifest and excluded files are reported as
    "carried, stays public-only", not as files a republish deletes."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {
        "src/lib.py": "real change\n",
        "src/__pycache__/lib.cpython-313.pyc": "junk\n",   # default exclude
        "scratch.md": "outside the manifest\n",            # not in includes
    })
    r = _integrate(run, repo)
    assert r.code == 5
    assert "src/lib.py  [theirs_changed]" in r.stdout
    assert "scratch.md  [carried_add]" in r.stdout
    assert "src/__pycache__/lib.cpython-313.pyc  [carried_add]" in r.stdout
    assert "carried, stays public-only: 2 path(s)" in r.stdout


def test_all_junk_means_nothing_to_integrate(source_repo, public_remote, run, tmp_path):
    """§10 item 5, AC36: an outside-manifest-only range → pull records it (the
    file stays public-only) and publish proceeds."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"scratch.md": "outside the manifest\n"})
    r = _integrate(run, repo)
    assert r.code == 5 and "scratch.md  [carried_add]" in r.stdout
    tip = git_run("ls-remote", remote, "refs/heads/main").split()[0]
    r = run(["pull", "--yes", "--expect-tip", tip, "--expect-plan", _digest(r)], cwd=repo)
    assert r.code == 0, r.stderr
    assert not (repo / "scratch.md").exists()
    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-qam", "owner change", cwd=repo)
    assert run(["publish", "--yes"], cwd=repo).code == 0


def test_conflict_surface_lists_overlap(source_repo, public_remote, run, tmp_path):
    """§10 item 5: a path changed on both sides is listed as a conflict."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"src/lib.py": "collab version\n"})
    (repo / "src/lib.py").write_text("local version\n")
    git_run("commit", "-aqm", "local change", cwd=repo)
    r = _integrate(run, repo)
    assert r.code == 5
    assert "src/lib.py  [C2]" in r.stdout and "decision required" in r.stdout


def test_transforms_force_manual_variant(source_repo, public_remote, run, tmp_path):
    """§10 item 5, §5.8: a transformed path changed on public is `resolved` or
    `decline` only; it is never written automatically."""
    remote, repo = _setup_published(
        source_repo, public_remote, run,
        config_extra={"transforms": {
            "src/lib.py": [{"find": "internal-name", "replace": "public-name"}],
        }})
    _foreign_commit(remote, tmp_path, {"src/lib.py": "edited public-name\n"})
    r = _integrate(run, repo)
    assert r.code == 5
    assert "src/lib.py  [transformed]" in r.stdout
    assert "options: --resolved, --decline" in r.stdout and "decision required" in r.stdout


def test_transforms_elsewhere_keep_the_recipe(source_repo, public_remote, run, tmp_path):
    """§10 item 5, §5.8: transforms on an unchanged file don't affect the changed
    one, which is an ordinary item with the default accept."""
    remote, repo = _setup_published(
        source_repo, public_remote, run,
        config_extra={"transforms": {
            "a.py": [{"find": "internal-name", "replace": "public-name"}],
        }})
    _foreign_commit(remote, tmp_path, {"src/lib.py": "clean change\n"})
    r = _integrate(run, repo)
    assert r.code == 5
    assert "src/lib.py  [theirs_changed]" in r.stdout and "→ accept (default)" in r.stdout
    assert "[transformed]" not in r.stdout


def test_roundtrip_recipe_actually_works(source_repo, public_remote, run,
                                          published_tree, tmp_path):
    """Design §10 item 3, §10 item 5, AC2: the
    recipe and --force-overwrite are retired. The round trip is now
    `pull` (plan, then the digest-bound --yes), then an ordinary publish
    that fast-forwards: the collaborator's change lands in source, public
    keeps the foreign commit, and nothing foreign remains to pull."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"src/lib.py": "collab improvement\n"})
    foreign = git_run("ls-remote", remote, "refs/heads/main").split()[0]

    r = run(["pull", "--plan"], cwd=repo)
    assert r.code == 5
    digest = r.stdout.strip().splitlines()[-1].split(": ", 1)[1]
    r = run(["pull", "--yes", "--expect-tip", foreign, "--expect-plan", digest], cwd=repo)
    assert r.code == 0, r.stderr

    assert (repo / "src/lib.py").read_text() == "collab improvement\n"
    author = git_run("log", "-1", "--format=%an <%ae>", cwd=repo).strip()
    assert author == "Pubrepo Tests <test@pubrepo.test>"          # source identity
    trailers = git_run("log", "-1", "--format=%(trailers:key=Co-authored-by,valueonly)",
                       cwd=repo).strip()
    assert trailers == "Collab Orator <collab@example.com>"

    (repo / "a.py").write_text("v2\n")
    git_run("commit", "-qam", "owner change", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    tree = published_tree(remote)
    assert tree["src/lib.py"] == b"collab improvement\n" and tree["a.py"] == b"v2\n"
    tip = git_run("ls-remote", remote, "refs/heads/main").split()[0]
    assert subprocess.run(["git", "merge-base", "--is-ancestor", foreign, tip],
                          cwd=repo / ".publish").returncode == 0     # fast-forward kept it

    r = run(["pull", "--plan"], cwd=repo)
    assert r.code == 0                                            # nothing left to pull


def test_every_integrate_test_cites_section_10_item_5():
    """Every test in this file changed from
    5a6fe29 for the integrate alias, so each docstring cites §10 item 5."""
    import ast
    tree = ast.parse(Path(__file__).read_text())
    missing = [f.name for f in tree.body
               if isinstance(f, ast.FunctionDef) and f.name.startswith("test_")
               and f.name != "test_every_integrate_test_cites_section_10_item_5"
               and "§10 item 5" not in (ast.get_docstring(f) or "")]
    assert missing == []
