"""Real PyPI builds: `python -m build
--no-isolation` with setuptools, through pubrepo's release path. Skipped only
when `build`, `setuptools` or `twine` cannot be imported; CI installs them.
Uploads and the registry are faked; every git remote is a local bare repo."""
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import pubrepo
from conftest import git_run
from test_release_transaction import _release

pytest.importorskip("build")
pytest.importorskip("setuptools")
pytest.importorskip("twine")

PYPROJECT = """[build-system]
requires = ["setuptools>=68"]
build-backend = "{backend}"
backend-path = ["_build"]

[project]
name = "demo"
version = "1.0.0"
description = "demo"

[tool.setuptools]
packages = ["demo"]
"""

# A backend that, like a version or build-info hook, writes generated content
# into its source dir and ships it. The canary is assembled at build time, so
# no source file contains it.
CANARY_BACKEND = """from setuptools import build_meta as _meta
from setuptools.build_meta import *  # noqa: F401,F403


def _generate():
    import pathlib
    pathlib.Path("demo/generated.py").write_text("BUILT = %r\\n" % ("zz-" + "canary-zz",))


def build_sdist(sdist_directory, config_settings=None):
    _generate()
    return _meta.build_sdist(sdist_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _generate()
    return _meta.build_wheel(wheel_directory, config_settings, metadata_directory)
"""

PLAIN_BACKEND = "from setuptools.build_meta import *  # noqa: F401,F403\n"


@pytest.fixture
def real_build(monkeypatch):
    """pubrepo's own build argv plus `--no-isolation` (no network), run for
    real; counts the builds. PyPI's live check, reconcile and upload are
    faked; `fail_upload_once` makes the first upload stage fail."""
    state = SimpleNamespace(builds=0, uploads=[], fail_upload_once=False)
    real = pubrepo._run_tool

    def tool(argv, env=None, cwd=None):
        if argv[1:3] == ["-m", "build"]:
            state.builds += 1
            argv = [*argv[:3], "--no-isolation", *argv[3:]]
        return real(argv, env=env, cwd=cwd)

    def execute(self, ctx, stage):
        if stage == "upload" and state.fail_upload_once:
            state.fail_upload_once = False
            raise pubrepo.ReleaseError("ERROR: PyPI upload failed: injected")
        state.uploads.append(stage)

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "check_live", lambda self, ctx: [])
    monkeypatch.setattr(pubrepo.PyPIAdapter, "reconcile", lambda self, ctx: None)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "execute", execute)
    return state


def _project(source_repo, public_remote, run, backend_source):
    url = public_remote()
    repo = source_repo({
        "version.py": 'VERSION = "1.0.0"\n',
        "pyproject.toml": PYPROJECT.format(backend="backend"),
        "_build/backend.py": backend_source,
        "demo/__init__.py": "VALUE = 1\n",
        "MANIFEST.in": "graft _build\nglobal-exclude *.pyc\n",   # the wheel builds from the sdist
    }, {"remote": url, "include": ["version.py", "pyproject.toml", "MANIFEST.in", "_build",
                                   "demo"],
        "scrub": {"forbidden": ["zz-canary-zz"]},
        "release": _release("1.0.0", pypi={"enabled": True, "package": "demo",
                                           "repository": "pypi"})})
    assert run(["init"], cwd=repo).code == 0
    return repo, url


def _release_cmd():
    return ["--release", "1.0.0", "--yes", "--accept-build-changes"]


def test_generated_canary_in_a_real_build_is_refused(source_repo, public_remote, run,
                                                     real_build):
    """A backend that writes generated content into its source dir
    and ships it is scrubbed: the canary → 4, nothing pushed or uploaded.
    (Before: the generated file landed in the export, its digest joined the
    skip set, and the release exited 0.)"""
    repo, url = _project(source_repo, public_remote, run, CANARY_BACKEND)
    r = run(_release_cmd(), cwd=repo)
    assert r.code == 4, r.stderr
    assert "release scrub failed in the distributions" in r.stderr
    assert "demo/generated.py" in r.stderr and "zz-canary-zz" in r.stderr
    assert git_run("ls-remote", url).strip() == "" and real_build.uploads == []


def test_upload_failure_then_identical_rerun_reuses_the_build(source_repo, public_remote, run,
                                                              real_build):
    """§8.8, AC69: setuptools writes demo.egg-info into its source
    dir; the retained export stays as journaled, so after a failed upload
    the identical rerun resumes with the same artifacts (no second build)
    and completes. (Before: "retained release export is missing or
    changed … refusing to rebuild", 7, with v1.0.0 already public.)"""
    repo, url = _project(source_repo, public_remote, run, PLAIN_BACKEND)
    real_build.fail_upload_once = True
    r = run(_release_cmd(), cwd=repo)
    assert r.code == 7 and "pypi:upload" in r.stderr, r.stderr
    export = repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "source"
    assert not list(export.rglob("*.egg-info")), "the build wrote into the retained export"
    dist = repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "dist"
    before = {p.name: p.read_bytes() for p in dist.iterdir()}
    r = run(_release_cmd(), cwd=repo)
    assert r.code == 0, r.stderr
    assert real_build.builds == 1 and real_build.uploads.count("upload") == 1
    assert {p.name: p.read_bytes() for p in dist.iterdir()} == before
    assert git_run("ls-remote", url, "refs/tags/v1.0.0").strip()


def test_setuptools_sdist_metadata_is_the_top_level_pkg_info(source_repo, public_remote, run,
                                                            real_build):
    """A setuptools sdist holds <name>-<ver>/PKG-INFO and
    <name>.egg-info/PKG-INFO; only the top-level one is the metadata, so
    the identity check passes and the release completes. (Before: "has no
    unique package metadata", 7.)"""
    repo, url = _project(source_repo, public_remote, run, PLAIN_BACKEND)
    r = run(_release_cmd(), cwd=repo)
    assert r.code == 0, r.stderr
    sdist = next((repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "dist")
                 .glob("*.tar.gz"))
    import tarfile
    with tarfile.open(sdist) as t:
        names = [m.name for m in t.getmembers() if m.name.endswith("PKG-INFO")]
    assert len(names) == 2, names                     # the two-PKG-INFO shape


def test_pubrepos_own_pyproject_builds_and_is_accepted(tmp_path, real_build, monkeypatch):
    """Dogfood: pubrepo's own pyproject.toml (setuptools.build_meta)
    builds for real and its sdist and wheel pass the identity check."""
    root = Path(pubrepo.__file__).resolve().parent
    publish = tmp_path / "publish"
    (publish / ".git").mkdir(parents=True)
    txn = publish / ".git" / pubrepo.RELEASE_DIR_NAME / pubrepo.__version__
    export = txn / "source"
    export.mkdir(parents=True)
    for name in ("pyproject.toml", "pubrepo.py", "README.md", "LICENSE"):
        if (root / name).exists():
            shutil.copy2(root / name, export / name)
    config = {"release": {"enabled": True, "version_file": "pubrepo.py",
                          "tag_format": "v{version}",
                          "pypi": {"enabled": True, "package": "pubrepo",
                                   "repository": "pypi"}}}
    journal = pubrepo.new_journal(publish, pubrepo.__version__,
                                  command=f"--release {pubrepo.__version__}", source_sha="s",
                                  public_baseline="b", tag=f"v{pubrepo.__version__}",
                                  strategy="new_commit", config=config)
    journal.data["export_manifest"] = pubrepo.export_manifest(export)
    ctx = SimpleNamespace(version=pubrepo.__version__, txn_dir=txn, export_dir=export,
                          publish_dir=publish, public_candidate="abc", config=config,
                          journal=journal, artifacts=[])
    monkeypatch.setattr(pubrepo, "git",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "1700000000\n", ""))
    pubrepo.PyPIAdapter(config).build(ctx)
    kinds = sorted(a.name.rsplit(".", 2)[-1] if a.name.endswith(".gz") else a.name[-3:]
                   for a in ctx.artifacts)
    assert real_build.builds == 1 and ctx.artifacts, kinds
    assert any(a.name.endswith(".tar.gz") for a in ctx.artifacts)
    assert pubrepo.export_manifest(export) == journal.data["export_manifest"]   # untouched
    assert sys.executable                                                       # (real build)
