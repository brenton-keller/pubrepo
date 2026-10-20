import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import pubrepo


def _config(remote="git@github.com:Owner/Repo.git"):
    return {"remote": remote, "release": {"enabled": True,
        "github": {"enabled": True, "repository": "owner/repo",
                   "notes_file": "CHANGELOG.md", "assets": ["tool.py"]}}}


def _ctx(tmp_path, version="1.2.0"):
    publish = tmp_path / "publish"
    (publish / ".git").mkdir(parents=True)
    txn = publish / ".git" / pubrepo.RELEASE_DIR_NAME / version
    export = txn / "source"
    export.mkdir(parents=True)
    (export / "CHANGELOG.md").write_text("notes")
    asset_path = export / "tool.py"
    asset_path.write_text("tool")
    config = _config()
    journal = pubrepo.new_journal(publish, version, command=f"--release {version}",
                                   source_sha="s", public_baseline="b", tag=f"v{version}",
                                   strategy="new_commit", config=config)
    asset = pubrepo.Asset("tool.py", asset_path, pubrepo._sha256(asset_path), "candidate")
    return SimpleNamespace(version=version, tag=f"v{version}", txn_dir=txn,
                           export_dir=export, config=config, journal=journal,
                           artifacts=[asset])


def test_normalize_git_remote_ssh_https_and_short_forms():
    expected = ("github.com", "owner", "repo")
    assert pubrepo.normalize_git_remote("git@GitHub.com:Owner/Repo.git") == expected
    assert pubrepo.normalize_git_remote("https://github.com/owner/repo.git") == expected
    assert pubrepo.normalize_git_remote("ssh://git@github.com/owner/repo") == expected
    assert pubrepo.normalize_git_remote("owner/repo") == expected


def test_live_check_normalizes_repo_identity(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)

    def tool(argv, **_kwargs):
        if argv[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(argv, 1, "", "release not found")
        return subprocess.CompletedProcess(argv, 0, "gh 1\n", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    assert pubrepo.GitHubAdapter(ctx.config).check_live(ctx) == []


def test_live_check_refuses_repo_remote_mismatch(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    ctx.config["remote"] = "https://github.com/other/repo.git"
    monkeypatch.setattr(pubrepo, "_run_tool", lambda argv, **_k: subprocess.CompletedProcess(
        argv, 1 if argv[:3] == ["gh", "release", "view"] else 0,
        "", "not found" if argv[:3] == ["gh", "release", "view"] else ""))
    findings = pubrepo.GitHubAdapter(ctx.config).check_live(ctx)
    assert any(x["field"] == "release.github.repository" for x in findings)


def test_draft_upload_publish_order_and_no_clobber(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    calls = []
    state = {"draft": False, "uploaded": False, "published": False}

    def tool(argv, **_kwargs):
        calls.append(argv)
        command = argv[1:3]
        if command == ["release", "view"]:
            if not state["draft"]:
                return subprocess.CompletedProcess(argv, 1, "", "release not found")
            assets = ([{"name": "tool.py", "digest": f"sha256:{ctx.artifacts[0].sha256}"}]
                      if state["uploaded"] else [])
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "tagName": ctx.tag, "isDraft": not state["published"], "assets": assets,
            }), "")
        if command == ["release", "create"]:
            state["draft"] = True
        elif command == ["release", "upload"]:
            state["uploaded"] = True
        elif command == ["release", "edit"]:
            state["published"] = True
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    adapter = pubrepo.GitHubAdapter(ctx.config)
    adapter.reconcile(ctx); adapter.execute(ctx, "draft")
    adapter.reconcile(ctx); adapter.execute(ctx, "upload")
    adapter.reconcile(ctx); adapter.execute(ctx, "publish")
    mutation = [argv[2] for argv in calls if argv[1] == "release" and argv[2] in ("create", "upload", "edit")]
    assert mutation == ["create", "upload", "edit"]
    assert not any("--clobber" in argv for argv in calls)


def test_prerelease_draft_passes_prerelease_flag(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path, "1.2.0-rc.1")
    calls = []
    monkeypatch.setattr(pubrepo, "_run_tool", lambda argv, **_k: (calls.append(argv) or subprocess.CompletedProcess(
        argv, 1 if argv[:3] == ["gh", "release", "view"] else 0,
        "", "not found" if argv[:3] == ["gh", "release", "view"] else "")))
    adapter = pubrepo.GitHubAdapter(ctx.config)
    adapter.reconcile(ctx)
    adapter.execute(ctx, "draft")
    create = next(argv for argv in calls if argv[1:3] == ["release", "create"])
    assert "--prerelease" in create


def test_asset_digest_mismatch_is_terminal(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(pubrepo, "_run_tool", lambda argv, **_k: subprocess.CompletedProcess(
        argv, 0, json.dumps({"tagName": ctx.tag, "isDraft": True,
                            "assets": [{"name": "tool.py", "digest": "sha256:different"}]}), ""))
    with pytest.raises(pubrepo.ReleaseError, match="different SHA-256"):
        pubrepo.GitHubAdapter(ctx.config).reconcile(ctx)
    assert pubrepo.load_journal(ctx.journal.path.parents[2], ctx.version).data["state"] == "terminal"


def test_missing_api_digest_downloads_and_hashes_asset(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)

    def tool(argv, **_kwargs):
        if argv[1:3] == ["release", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "tagName": ctx.tag, "isDraft": True, "assets": [{"name": "tool.py"}],
            }), "")
        if argv[1:3] == ["release", "download"]:
            directory = Path(argv[argv.index("--dir") + 1])
            (directory / "tool.py").write_bytes(ctx.artifacts[0].path.read_bytes())
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    adapter = pubrepo.GitHubAdapter(ctx.config)
    adapter.reconcile(ctx)
    assert adapter.pending == []


def test_candidate_asset_symlink_escape_is_refused(tmp_path):
    ctx = _ctx(tmp_path)
    outside = tmp_path / "private.txt"
    outside.write_text("private")
    asset = ctx.export_dir / "escape.txt"
    asset.symlink_to(outside)
    ctx.config["release"]["github"]["assets"] = ["escape.txt"]
    with pytest.raises(pubrepo.ReleaseError, match="missing from candidate export"):
        pubrepo.compute_assets(ctx, ctx.config)


def test_ambiguous_draft_failure_reuses_matching_draft_on_retry(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    state = {"draft": False, "created": 0}

    def tool(argv, **_kwargs):
        if argv[1:3] == ["release", "view"]:
            if not state["draft"]:
                return subprocess.CompletedProcess(argv, 1, "", "release not found")
            return subprocess.CompletedProcess(argv, 0, json.dumps({
                "tagName": ctx.tag, "isDraft": True, "assets": [],
            }), "")
        if argv[1:3] == ["release", "create"]:
            state["draft"] = True
            state["created"] += 1
            return subprocess.CompletedProcess(argv, 1, "", "ambiguous response")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    first = pubrepo.GitHubAdapter(ctx.config)
    first.reconcile(ctx)
    with pytest.raises(pubrepo.ReleaseError, match="draft creation failed"):
        first.execute(ctx, "draft")

    retry = pubrepo.GitHubAdapter(ctx.config)
    retry.reconcile(ctx)
    retry.execute(ctx, "draft")
    assert state["created"] == 1
