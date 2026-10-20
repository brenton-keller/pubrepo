import io
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import pubrepo


def _config(repository="pypi", package="demo"):
    return {"scrub": {"forbidden": ["PRIVATE"]}, "release": {
        "enabled": True, "version_file": "version.py", "tag_format": "v{version}",
        "pypi": {"enabled": True, "package": package, "repository": repository},
    }}


def _ctx(tmp_path, version="1.2.0", package="demo"):
    publish = tmp_path / "publish"
    (publish / ".git").mkdir(parents=True)
    txn = publish / ".git" / pubrepo.RELEASE_DIR_NAME / version
    export = txn / "source"
    export.mkdir(parents=True)
    config = _config(package=package)
    journal = pubrepo.new_journal(publish, version, command=f"--release {version}",
                                   source_sha="s", public_baseline="b", tag=f"v{version}",
                                   strategy="new_commit", config=config)
    return SimpleNamespace(version=version, txn_dir=txn, export_dir=export,
                           publish_dir=publish, public_candidate="abc", config=config,
                           journal=journal, artifacts=[])


def _wheel(path: Path, name="demo", version="1.2.0"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
        archive.writestr(f"{name}/__init__.py", "ok = True\n")


def test_registry_names_map_only_to_approved_json_apis():
    pypi = pubrepo.PyPIAdapter(_config("pypi"))
    test = pubrepo.PyPIAdapter(_config("testpypi"))
    assert pypi._api_url("1.0.0") == "https://pypi.org/pypi/demo/1.0.0/json"
    assert test._api_url("1.0.0") == "https://test.pypi.org/pypi/demo/1.0.0/json"


# AC75: §16 row "§7 step 13 build from the export" (Amended → §8.6): the build runs in a
# disposable copy of the export, so the export stays immutable.
def test_build_uses_export_and_private_outdir_and_verifies_metadata(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    calls = []
    monkeypatch.setenv("TWINE_PASSWORD", "build-secret")
    monkeypatch.setenv("TWINE_USERNAME", "build-user")
    monkeypatch.setenv("GH_TOKEN", "github-secret")

    def run_tool(argv, env=None, cwd=None):
        calls.append((argv, env, cwd))
        if argv[2:4] == ["build", "--outdir"]:
            outdir = Path(argv[4])
            _wheel(outdir / "demo-1.2.0-py3-none-any.whl")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", run_tool)
    monkeypatch.setattr(pubrepo, "git", lambda *a, **k: subprocess.CompletedProcess(a, 0, "123\n", ""))
    adapter = pubrepo.PyPIAdapter(ctx.config)
    adapter.build(ctx)
    build_call = calls[0]
    assert build_call[2] == ctx.txn_dir / "build-src"           # a copy of the export
    assert build_call[0][-1] == str(ctx.txn_dir / "build-src")
    assert not (ctx.txn_dir / "build-src").exists()              # removed after the build
    assert Path(build_call[0][4]).is_relative_to(ctx.publish_dir / ".git" / pubrepo.RELEASE_DIR_NAME)
    assert build_call[1]["SOURCE_DATE_EPOCH"] == "123"
    assert not {"TWINE_PASSWORD", "TWINE_USERNAME", "GH_TOKEN", "GITHUB_TOKEN"} \
        & set(build_call[1])
    assert len(ctx.artifacts) == 1 and ctx.artifacts[0].kind == "distribution"


def test_distribution_metadata_mismatch_refuses_before_upload(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)

    def run_tool(argv, env=None, cwd=None):
        if argv[2:4] == ["build", "--outdir"]:
            _wheel(Path(argv[4]) / "wrong.whl", name="other")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", run_tool)
    monkeypatch.setattr(pubrepo, "git", lambda *a, **k: subprocess.CompletedProcess(a, 0, "123\n", ""))
    with pytest.raises(pubrepo.ReleaseError, match="identity"):
        pubrepo.PyPIAdapter(ctx.config).build(ctx)


def test_reconcile_uploads_only_absent_files_without_credentials_on_argv(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    one = ctx.txn_dir / "dist" / "one.whl"
    two = ctx.txn_dir / "dist" / "two.whl"
    one.parent.mkdir(parents=True)
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    ctx.artifacts = [
        pubrepo.Asset(one.name, one, pubrepo._sha256(one), "distribution"),
        pubrepo.Asset(two.name, two, pubrepo._sha256(two), "distribution"),
    ]
    states = iter([
        {"urls": [{"filename": one.name, "digests": {"sha256": pubrepo._sha256(one)}}]},
        {"urls": [
            {"filename": one.name, "digests": {"sha256": pubrepo._sha256(one)}},
            {"filename": two.name, "digests": {"sha256": pubrepo._sha256(two)}},
        ]},
    ])
    monkeypatch.setattr(pubrepo, "_http_json", lambda _url: next(states))
    calls = []
    monkeypatch.setattr(pubrepo, "_run_tool", lambda argv, **_k: (calls.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")))
    adapter = pubrepo.PyPIAdapter(ctx.config)
    adapter.reconcile(ctx)
    adapter.execute(ctx, "upload")
    argv = calls[0]
    assert str(two) in argv and str(one) not in argv
    assert "--non-interactive" in argv and "-p" not in argv and "-u" not in argv


def test_existing_registry_digest_mismatch_marks_terminal(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path)
    path = ctx.txn_dir / "dist" / "one.whl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"one")
    ctx.artifacts = [pubrepo.Asset(path.name, path, pubrepo._sha256(path), "distribution")]
    monkeypatch.setattr(pubrepo, "_http_json", lambda _url: {
        "urls": [{"filename": path.name, "digests": {"sha256": "different"}}],
    })
    with pytest.raises(pubrepo.ReleaseError, match="different SHA-256"):
        pubrepo.PyPIAdapter(ctx.config).reconcile(ctx)
    assert pubrepo.load_journal(ctx.publish_dir, ctx.version).data["state"] == "terminal"


def test_member_path_traversal_is_refused(tmp_path):
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("../escape", "bad")
        archive.writestr("x.dist-info/METADATA", "Name: demo\nVersion: 1.2.0\n")
    with pytest.raises(pubrepo.ReleaseError, match="unsafe member"):
        pubrepo.PyPIAdapter._metadata_from_archive(wheel)


def test_wheel_symlink_member_is_refused(tmp_path):
    wheel = tmp_path / "bad.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("x.dist-info/METADATA", "Name: demo\nVersion: 1.2.0\n")
        link = zipfile.ZipInfo("demo/link")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside")
    with pytest.raises(pubrepo.ReleaseError, match="unsafe symlink"):
        pubrepo.PyPIAdapter._metadata_from_archive(wheel)


def test_sdist_link_member_is_refused(tmp_path):
    sdist = tmp_path / "bad.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        metadata = b"Name: demo\nVersion: 1.2.0\n"
        info = tarfile.TarInfo("demo-1.2.0/PKG-INFO")
        info.size = len(metadata)
        archive.addfile(info, io.BytesIO(metadata))
        link = tarfile.TarInfo("demo-1.2.0/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../outside"
        archive.addfile(link)
    with pytest.raises(pubrepo.ReleaseError, match="unsafe link"):
        pubrepo.PyPIAdapter._metadata_from_archive(sdist)


def test_prerelease_metadata_uses_pep440_form(tmp_path, monkeypatch):
    ctx = _ctx(tmp_path, version="1.2.0-rc.1")

    def run_tool(argv, env=None, cwd=None):
        if argv[2:4] == ["build", "--outdir"]:
            _wheel(Path(argv[4]) / "demo.whl", version="1.2.0rc1")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", run_tool)
    monkeypatch.setattr(pubrepo, "git", lambda *a, **k: subprocess.CompletedProcess(a, 0, "123\n", ""))
    pubrepo.PyPIAdapter(ctx.config).build(ctx)
    assert ctx.artifacts


@pytest.mark.skipif(os.environ.get("PUBREPO_REAL_BUILD") != "1", reason="real build is opt-in")
def test_real_build_tool_is_available():
    assert subprocess.run([sys.executable, "-m", "build", "--version"], capture_output=True).returncode == 0
    assert subprocess.run([sys.executable, "-m", "twine", "--version"], capture_output=True).returncode == 0
