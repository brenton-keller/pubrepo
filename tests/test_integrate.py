"""integrate subcommand: print-only recipe for pulling foreign commits back.

The recipe must be EXACT — the roundtrip test executes the printed commands
verbatim and proves the foreign change lands in source and republishes.
"""
import subprocess
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
    assert run([], cwd=repo).code == 0
    return remote, repo


def test_nothing_to_integrate(source_repo, public_remote, run):
    _, repo = _setup_published(source_repo, public_remote, run)
    r = run(["integrate"], cwd=repo)
    assert r.code == 0
    assert "Nothing to integrate" in r.stdout


def test_not_initialized_errors(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    r = run(["integrate"], cwd=repo)
    assert r.code == 1
    assert "not initialized" in r.stderr


def test_recipe_clean_surface(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"src/lib.py": "lib v2 from collab\n"})

    r = run(["integrate"], cwd=repo)
    assert r.code == 0
    assert "Foreign commits: 1" in r.stdout
    assert "Conflict surface: empty" in r.stdout
    assert "Collab Orator <collab@example.com> (1)" in r.stdout
    # The diff command names the exact file and ends in the apply chain.
    assert "git -C .publish diff HEAD origin/main" in r.stdout
    assert "src/lib.py" in r.stdout
    assert "git apply --3way" in r.stdout
    assert "--author='Collab Orator <collab@example.com>'" in r.stdout
    assert "--force-overwrite" in r.stdout


def test_junk_is_filtered_and_reported(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {
        "src/lib.py": "real change\n",
        "src/__pycache__/lib.cpython-313.pyc": "junk\n",   # default exclude
        "scratch.md": "outside the manifest\n",            # not in includes
    })

    r = run(["integrate"], cwd=repo)
    assert r.code == 0
    assert "Changed files in the publish set: 1" in r.stdout
    assert "Ignored — outside the manifest or excluded (2" in r.stdout
    # Junk appears in the report but NEVER in the diff command.
    recipe = "\n".join(l for l in r.stdout.splitlines() if not l.startswith("#"))
    assert "src/lib.py" in recipe
    assert "__pycache__" not in recipe
    assert "scratch.md" not in recipe


def test_all_junk_means_nothing_to_integrate(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"scratch.md": "outside the manifest\n"})

    r = run(["integrate"], cwd=repo)
    assert r.code == 0
    assert "outside the publish manifest" in r.stdout
    assert "git apply" not in r.stdout


def test_conflict_surface_lists_overlap(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"src/lib.py": "collab version\n"})
    # Source ALSO changes the same file after the snapshot.
    (repo / "src/lib.py").write_text("local version\n")
    git_run("commit", "-aqm", "local change", cwd=repo)

    r = run(["integrate"], cwd=repo)
    assert r.code == 0
    assert "changed in BOTH source and" in r.stdout
    assert "src/lib.py" in r.stdout


def test_transforms_force_manual_variant(source_repo, public_remote, run, tmp_path):
    remote, repo = _setup_published(
        source_repo, public_remote, run,
        config_extra={"transforms": {
            "src/lib.py": [{"find": "internal-name", "replace": "public-name"}],
        }})
    _foreign_commit(remote, tmp_path, {"src/lib.py": "edited public-name\n"})

    r = run(["integrate"], cwd=repo)
    assert r.code == 0
    assert "MANUAL INTEGRATION REQUIRED" in r.stdout
    assert "src/lib.py" in r.stdout
    # Plain read-the-diff command, no apply chain.
    assert "git -C .publish diff HEAD origin/main" in r.stdout
    assert "git apply" not in r.stdout


def test_transforms_elsewhere_keep_the_recipe(source_repo, public_remote, run, tmp_path):
    # Transforms on an UNCHANGED file don't poison the recipe: the changed
    # files are still byte-identical between source and public.
    remote, repo = _setup_published(
        source_repo, public_remote, run,
        config_extra={"transforms": {
            "a.py": [{"find": "internal-name", "replace": "public-name"}],
        }})
    _foreign_commit(remote, tmp_path, {"src/lib.py": "clean change\n"})

    r = run(["integrate"], cwd=repo)
    assert r.code == 0
    assert "MANUAL INTEGRATION REQUIRED" not in r.stdout
    assert "git apply --3way" in r.stdout


def test_roundtrip_recipe_actually_works(source_repo, public_remote, run,
                                          published_tree, tmp_path):
    """Execute the printed recipe verbatim; the foreign change must land in
    source and the republish must reconcile the public repo."""
    remote, repo = _setup_published(source_repo, public_remote, run)
    _foreign_commit(remote, tmp_path, {"src/lib.py": "collab improvement\n"})

    r = run(["integrate"], cwd=repo)
    assert r.code == 0

    # Everything except the final publish line (run in-process below so the
    # test exercises main() rather than needing a console script on PATH).
    script = "\n".join(
        line for line in r.stdout.splitlines()
        if not line.startswith("#") and "force-overwrite" not in line)
    proc = subprocess.run(["bash", "-e"], input="set -u\n" + script,
                          cwd=repo, capture_output=True, text=True)
    assert proc.returncode == 0, f"recipe failed:\n{proc.stderr}\n---\n{script}"

    assert (repo / "src/lib.py").read_text() == "collab improvement\n"
    author = git_run("log", "-1", "--format=%an <%ae>", cwd=repo).strip()
    assert author == "Collab Orator <collab@example.com>"

    r = run(["publish", "--force-overwrite"], cwd=repo)
    assert r.code == 0
    tree = published_tree(remote)
    assert tree["src/lib.py"] == b"collab improvement\n"

    # And now the mirror is reconciled: nothing foreign remains.
    r = run(["integrate"], cwd=repo)
    assert "Nothing to integrate" in r.stdout
