import json
import subprocess
from pathlib import Path

import pytest

import pubrepo
from conftest import git_run
from ff_fixtures import push_argv


def _release(version="1.0.0", **extra):
    value = {"enabled": True, "version_file": "version.py",
             "version_pattern": r'^VERSION = "(?P<version>[0-9A-Za-z.+-]+)"$',
             "tag_format": "v{version}", "source_tag": False}
    value.update(extra)
    return value


def _is_branch_push(args) -> bool:
    """The release branch push: `push --porcelain origin
    <candidate sha>:refs/heads/main`."""
    return bool(push_argv(args)) and args[-1].endswith(":refs/heads/main") \
        and not args[-1].startswith("refs/")


def _pull_decide(run, repo, remote, decisions: dict[str, str]):
    """`pull --plan`, then `pull --yes` bound to it, with an explicit
    decision per path (`--<decision> <path>`; design §5). A release needs
    tip == IP first (§8.1)."""
    flags = [arg for path, decision in decisions.items() for arg in (f"--{decision}", path)]
    plan = run(["pull", "--plan", *flags], repo)
    last = plan.stdout.strip().splitlines()[-1] if plan.stdout.strip() else ""
    assert last.startswith("Plan digest: "), (plan.code, plan.stdout, plan.stderr)
    digest = last.split(": ", 1)[1]
    tip = git_run("ls-remote", remote, "refs/heads/main").split()[0]
    r = run(["pull", "--yes", "--expect-tip", tip, "--expect-plan", digest, *flags], repo)
    assert r.code == 0, r.stdout + r.stderr


def _setup(source_repo, public_remote, run, *, version="1.0.0", release_extra=None,
           config_extra=None, files_extra=None):
    remote = public_remote()
    config = {"remote": remote, "include": ["version.py", "a.txt", *(files_extra or {})],
              "release": _release(version, **(release_extra or {}))}
    config.update(config_extra or {})
    repo = source_repo({"version.py": f'VERSION = "{version}"\n', "a.txt": "hello\n",
                        **(files_extra or {})}, config)
    if config["release"].get("source_tag") and "private_remotes" not in config:
        # AC75 re-target (§10 item 20, §18.11): the source_tag
        # remote must be listed in private_remotes, every value byte-exact
        source_remote = config["release"].get("source_remote", "origin")
        url = git_run("remote", "get-url", source_remote, cwd=repo).strip()
        cfg = repo / ".publish.toml"
        cfg.write_text(cfg.read_text().replace(
            "[publish]\n", f'[publish]\nprivate_remotes = ["{url}"]\n', 1))
        git_run("commit", "-qam", "list the source_tag remote", cwd=repo)
        git_run("push", "-q", source_remote, "HEAD", cwd=repo)
    assert run(["init"], repo).code == 0
    return repo, remote


def _remote_tag(remote, tag):
    output = git_run("ls-remote", "--tags", remote, f"refs/tags/{tag}^{{}}")
    return output.split()[0] if output.strip() else None


def test_git_only_release_tags_candidate_and_completes_journal(source_repo, public_remote, run):
    repo, remote = _setup(source_repo, public_remote, run)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    candidate = git_run("rev-parse", "HEAD", cwd=repo / ".publish").strip()
    assert _remote_tag(remote, "v1.0.0") == candidate
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["state"] == "complete"
    assert journal.data["completed"][:3] == ["candidate", "export", "artifacts"]
    entries = [json.loads(x) for x in (repo / ".publish" / ".git" / pubrepo.PUBLISH_LOG_NAME).read_text().splitlines()]
    assert entries[-1]["release"]["version"] == "1.0.0"


def test_identical_candidate_releases_existing_head_without_empty_commit(source_repo, public_remote, run):
    repo, remote = _setup(source_repo, public_remote, run)
    assert run(["--no-release", "--yes"], repo).code == 0
    before = git_run("rev-list", "--count", "HEAD", cwd=repo / ".publish").strip()
    head = git_run("rev-parse", "HEAD", cwd=repo / ".publish").strip()
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    assert git_run("rev-list", "--count", "HEAD", cwd=repo / ".publish").strip() == before
    assert _remote_tag(remote, "v1.0.0") == head


def test_transform_candidate_version_mismatch_refuses_without_remote_write(source_repo, public_remote, run):
    repo, remote = _setup(source_repo, public_remote, run, config_extra={
        "transforms": {"version.py": [{"find": "1.0.0", "replace": "2.0.0"}]},
    })
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "resolved=1.0.0" in result.stderr and "candidate=2.0.0" in result.stderr
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""


def test_dirty_included_file_refuses_before_rebuild(source_repo, public_remote, run):
    repo, _remote = _setup(source_repo, public_remote, run)
    marker = repo / ".publish" / "sentinel"
    marker.write_text("keep")
    (repo / "a.txt").write_text("dirty")
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "a.txt" in result.stderr
    assert marker.read_text() == "keep"


def test_ignored_included_file_counts_as_dirty(source_repo, public_remote, run):
    repo, _remote = _setup(source_repo, public_remote, run, config_extra={"include": ["version.py", "a.txt", "generated.txt"]})
    (repo / ".gitignore").write_text(".publish/\ngenerated.txt\n")
    git_run("add", ".gitignore", cwd=repo)
    git_run("commit", "-qm", "ignore generated", cwd=repo)
    git_run("push", "-q", cwd=repo)
    (repo / "generated.txt").write_text("ignored but publishable")
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "generated.txt" in result.stderr


def test_source_tag_requires_source_head_at_remote(source_repo, public_remote, run):
    repo, _remote = _setup(source_repo, public_remote, run,
                           release_extra={"source_tag": True, "source_remote": "origin"})
    (repo / "a.txt").write_text("next\n")
    git_run("commit", "-qam", "local only", cwd=repo)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "push it first" in result.stderr


def test_source_tag_uses_remote_truth_not_stale_tracking_ref(source_repo, public_remote, run):
    repo, _remote = _setup(source_repo, public_remote, run,
                           release_extra={"source_tag": True, "source_remote": "origin"})
    (repo / "a.txt").write_text("local only\n")
    git_run("commit", "-qam", "local only", cwd=repo)
    local_head = git_run("rev-parse", "HEAD", cwd=repo).strip()
    git_run("update-ref", "refs/remotes/origin/main", local_head, cwd=repo)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "push it first" in result.stderr


# AC75: §16 row "§1 invariant: Any remotely tagged public HEAD is protected from amend" (Kept, reinforced → §7: the tag is named; exit 2)
def test_any_foreign_tag_on_public_head_blocks_amend(source_repo, public_remote, run):
    repo, _remote = _setup(source_repo, public_remote, run)
    assert run(["--no-release", "--yes"], repo).code == 0
    publish = repo / ".publish"
    git_run("tag", "dependency-marker", cwd=publish)
    git_run("push", "-q", "origin", "dependency-marker", cwd=publish)
    (repo / "version.py").write_text('VERSION = "1.1.0"\n')
    git_run("commit", "-qam", "bump", cwd=repo)
    git_run("push", "-q", cwd=repo)
    # --amend with a release is a flag conflict (1, test_ff_surface);
    # this test keeps amend's own refusal: a tagged tip, named.
    result = run(["--amend", "--no-release", "--yes"], repo)
    assert result.code == 2
    assert "is tagged dependency-marker" in result.stderr


# AC75: AC75 re-target: the tag push is `push --porcelain origin <ref>:<ref>` (_tag_push); assertions unchanged
def test_tag_push_failure_is_resumable_without_rebuild(source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run)
    real = pubrepo.git
    failed = {"done": False}

    def fail_once(*args, **kwargs):
        if _tag_push(args, "v1.0.0") and not failed["done"]:
            failed["done"] = True
            return subprocess.CompletedProcess(args, 1, "", "injected")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", fail_once)
    first = run(["--release", "1.0.0", "--yes"], repo)
    assert first.code == 7
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["in_flight"] == "public_tag"
    assert "branch_push" in journal.data["remote_writes"]
    export_mtime = (repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "source" / "a.txt").stat().st_mtime_ns
    second = run(["--release", "1.0.0", "--yes"], repo)
    assert second.code == 0, second.stderr
    assert _remote_tag(remote, "v1.0.0") is not None
    assert (repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "source" / "a.txt").stat().st_mtime_ns == export_mtime


# AC75: AC75 re-target: the tag push is `push --porcelain origin <ref>:<ref>` (_tag_push); assertions unchanged
def test_changed_retained_export_refuses_resume_without_rebuild(
        source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run)
    real = pubrepo.git
    failed = {"done": False}

    def fail_once(*args, **kwargs):
        if _tag_push(args, "v1.0.0") and not failed["done"]:
            failed["done"] = True
            return subprocess.CompletedProcess(args, 1, "", "injected")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", fail_once)
    assert run(["--release", "1.0.0", "--yes"], repo).code == 7
    export = (repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME
              / "1.0.0" / "source" / "a.txt")
    export.write_text("tampered")
    before_mtime = export.stat().st_mtime_ns

    retry = run(["--release", "1.0.0", "--yes"], repo)
    assert retry.code == 7
    assert "retained release export is missing or changed" in retry.stderr
    assert export.read_text() == "tampered"
    assert export.stat().st_mtime_ns == before_mtime
    assert _remote_tag(remote, "v1.0.0") is None


def test_pypi_unmappable_prerelease_refuses_before_candidate(source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run, version="1.2.0-pre.1",
                           release_extra={"pypi": {"enabled": True, "package": "x", "repository": "pypi"}})
    # Adapter construction is after the C2 preflight check; no provider call is needed.
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [])
    before_result = pubrepo.git("rev-parse", "--verify", "HEAD", cwd=repo / ".publish", check=False)
    before = before_result.stdout.strip() if before_result.returncode == 0 else ""
    result = run(["--release", "1.2.0-pre.1", "--yes"], repo)
    assert result.code == 7
    assert "-alpha.N, -beta.N, or -rc.N" in result.stderr
    after = git_run("rev-parse", "--verify", "HEAD", cwd=repo / ".publish") if before else ""
    assert after.strip() == before


def test_pypi_build_metadata_refuses_before_candidate(source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run, version="1.2.0+build.1",
                          release_extra={"pypi": {"enabled": True, "package": "x", "repository": "pypi"}})
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [])
    result = run(["--release", "1.2.0+build.1", "--yes"], repo)
    assert result.code == 7
    assert "build metadata" in result.stderr
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""


def test_git_only_accepts_strict_semver_prerelease(source_repo, public_remote, run):
    repo, remote = _setup(source_repo, public_remote, run, version="1.2.0-pre.1")
    result = run(["--release", "1.2.0-pre.1", "--yes"], repo)
    assert result.code == 0, result.stderr
    assert _remote_tag(remote, "v1.2.0-pre.1") is not None


def test_pypi_approved_rc_prerelease_reaches_transaction(source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run, version="1.2.0-rc.1",
                          release_extra={"pypi": {"enabled": True, "package": "x", "repository": "pypi"}})

    class FakePyPI:
        name = "pypi"
        def check_live(self, _ctx): return []
        def build(self, _ctx): return None
        def reconcile(self, _ctx): return None
        def execute(self, _ctx, _stage): return None

    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [FakePyPI()])
    result = run(["--release", "1.2.0-rc.1", "--yes"], repo)
    assert result.code == 0, result.stderr
    assert _remote_tag(remote, "v1.2.0-rc.1") is not None


def _seed_public(remote, tmp_path, version="1.0.0", *, detached_tag=False):
    work = tmp_path / f"seed-{version.replace('.', '-')}"
    work.mkdir()
    git_run("init", "-q", "-b", "main", cwd=work)
    git_run("config", "user.name", "Tests", cwd=work)
    git_run("config", "user.email", "t@example.test", cwd=work)
    (work / "version.py").write_text(f'VERSION = "{version}"\n')
    (work / "a.txt").write_text("legacy\n")
    git_run("add", "-A", cwd=work)
    git_run("commit", "-qm", "legacy", cwd=work)
    git_run("remote", "add", "origin", remote, cwd=work)
    git_run("push", "-q", "origin", "main", cwd=work)
    if detached_tag:
        git_run("checkout", "--orphan", "released-old", cwd=work)
        git_run("rm", "-qrf", ".", cwd=work)
        (work / "old").write_text("old")
        git_run("add", "old", cwd=work)
        git_run("commit", "-qm", "detached release", cwd=work)
    git_run("tag", "-a", f"v{version}", "-m", "legacy", cwd=work)
    git_run("push", "-q", "origin", f"refs/tags/v{version}", cwd=work)
    return work


def test_bump_uses_legacy_remote_tag_without_local_journal(source_repo, public_remote, run, tmp_path):
    remote = public_remote()
    _seed_public(remote, tmp_path)
    repo = source_repo({"version.py": 'VERSION = "1.1.0"\n', "a.txt": "new\n"}, {
        "remote": remote, "include": ["version.py", "a.txt"],
        "release": _release("1.1.0", adopt_tags=["v1.0.0"]),      # requires adopt_tags
    })
    assert run(["init"], repo).code == 0
    # §16 (release candidate after pull; AC68)
    _pull_decide(run, repo, remote, {"a.txt": "ours", "version.py": "ours"})      # §8.1: tip == IP first
    result = run(["--bump", "minor", "--yes"], repo)
    assert result.code == 0, result.stderr
    assert _remote_tag(remote, "v1.1.0") is not None


def test_dogfood_shape_legacy_tag_two_ahead_private_dirty_bumps_minor(source_repo, public_remote, run, tmp_path):
    remote = public_remote()
    public_work = _seed_public(remote, tmp_path)
    for number in (1, 2):
        (public_work / f"ahead-{number}.txt").write_text(str(number))
        git_run("add", "-A", cwd=public_work)
        git_run("commit", "-qm", f"ahead {number}", cwd=public_work)
    git_run("push", "-q", "origin", "main", cwd=public_work)
    repo = source_repo({"version.py": 'VERSION = "1.1.0"\n', "a.txt": "release\n"}, {
        "remote": remote, "include": ["version.py", "a.txt"],
        "release": _release("1.1.0", adopt_tags=["v1.0.0"]),      # AC68: adopt_tags
    })
    (repo / ".claude").mkdir()
    (repo / ".claude" / "session.txt").write_text("private and untracked")
    assert run(["init"], repo).code == 0
    # §16 (release candidate after pull; AC68)
    _pull_decide(run, repo, remote, {"a.txt": "ours", "version.py": "ours"})      # pull first
    result = run(["--bump", "minor", "--yes"], repo)
    assert result.code == 0, result.stderr
    assert _remote_tag(remote, "v1.1.0") is not None
    files = git_run("ls-tree", "-r", "--name-only", "v1.1.0", cwd=repo / ".publish").split()
    assert {"ahead-1.txt", "ahead-2.txt"} <= set(files)       # collaborator files kept


def test_released_target_outside_public_main_refuses_before_rebuild(source_repo, public_remote, run, tmp_path):
    remote = public_remote()
    _seed_public(remote, tmp_path, detached_tag=True)
    repo = source_repo({"version.py": 'VERSION = "1.1.0"\n', "a.txt": "new\n"}, {
        "remote": remote, "include": ["version.py", "a.txt"],
        "release": _release("1.1.0", adopt_tags=["v1.0.0"]),      # §8.5: a managed tag
    })
    assert run(["init"], repo).code == 0
    # §16 (release candidate after pull; AC68)
    _pull_decide(run, repo, remote, {"a.txt": "ours", "version.py": "ours"})      # §8.1: tip == IP first
    marker = repo / ".publish" / "marker"
    marker.write_text("untouched")
    result = run(["--release", "1.1.0", "--yes"], repo)
    assert result.code == 7
    assert "no longer ancestors" in result.stderr
    # §8.5: the release-only refusal offers both routes.
    assert "repair restore-ancestry v1.0.0 --plan" in result.stderr
    assert "release-retire v1.0.0" in result.stderr
    assert marker.read_text() == "untouched"
    assert run(["--no-release", "--yes"], repo).code == 0      # --no-release is never blocked


def test_complete_matching_journal_is_idempotent_success(source_repo, public_remote, run):
    repo, _remote = _setup(source_repo, public_remote, run)
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    before = git_run("rev-parse", "HEAD", cwd=repo / ".publish")
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0
    assert "already complete" in result.stdout
    assert git_run("rev-parse", "HEAD", cwd=repo / ".publish") == before


def test_remote_tag_without_journal_cannot_be_adopted(source_repo, public_remote, run, tmp_path):
    """§8.5 (the squat test): a foreign v1.0.0 without
    adoption is not managed; releasing that exact name → 7."""
    remote = public_remote()
    _seed_public(remote, tmp_path)
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "legacy\n"}, {
        "remote": remote, "include": ["version.py", "a.txt"], "release": _release(),
    })
    assert run(["init"], repo).code == 0
    # §16 (release candidate after pull; AC68)
    _pull_decide(run, repo, remote, {"version.py": "accept"})  # §8.1: tip == IP first (flagged)
    ledger = pubrepo.read_remote_ledger(repo / ".publish", "v{version}", source_root=repo)
    assert "v1.0.0" in ledger.foreign and "v1.0.0" not in ledger.managed
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "immutable" in result.stderr


# AC75: AC75 re-target: pushes are `push --porcelain … origin` (push_argv); assertions unchanged
def test_formal_source_tag_precedes_best_effort_audit_tag(source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run,
                           release_extra={"source_tag": True, "source_remote": "origin"})
    # AC75 re-target (§10 item 20, §18.9 R1/R2/R6): the audit bookmark goes to
    # a listed tag_remote as refs/pubrepo/bookmarks/<ts>:refs/tags/published/<ts>
    cfg = repo / ".publish.toml"                       # origin is listed by _setup
    cfg.write_text(cfg.read_text().replace("[publish]\n", '[publish]\ntag_remote = "origin"\n', 1))
    git_run("commit", "-qam", "tag_remote = origin", cwd=repo)
    git_run("push", "-q", "origin", "main", cwd=repo)
    calls = []
    real = pubrepo.git

    def spy(*args, **kwargs):
        calls.append((args, kwargs.get("cwd")))
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spy)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    formal = next(i for i, (call, cwd) in enumerate(calls)
                  if call[:4] == ("push", "--recurse-submodules=no", "origin", "refs/tags/v1.0.0")
                  and cwd == str(repo))
    audit = next(i for i, (call, cwd) in enumerate(calls)
                 if call[:3] == ("push", "--recurse-submodules=no", "origin")
                 and call[3].startswith("refs/pubrepo/bookmarks/")
                 and ":refs/tags/published/" in call[3] and cwd == str(repo))
    assert formal < audit
    tag_pushes = [call for call, _cwd in calls
                  if push_argv(call)  # AC75 re-target: the porcelain push argv
                  and any("tags/" in str(arg) or str(arg).startswith("published/")
                          for arg in call)]
    assert tag_pushes
    assert all("-f" not in call and "--force" not in call
               and not any(str(arg).startswith("+refs/tags/") for arg in call)
               for call in tag_pushes)


def test_release_does_not_mutate_source_tree_or_commit(source_repo, public_remote, run):
    repo, _remote = _setup(source_repo, public_remote, run)
    source_head = git_run("rev-parse", "HEAD", cwd=repo)
    before = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*")
              if path.is_file() and ".git" not in path.parts and ".publish" not in path.parts}
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    after = {str(path.relative_to(repo)): path.read_bytes() for path in repo.rglob("*")
             if path.is_file() and ".git" not in path.parts and ".publish" not in path.parts}
    assert after == before
    assert git_run("rev-parse", "HEAD", cwd=repo) == source_head


def _advance_source(repo, version):
    (repo / "version.py").write_text(f'VERSION = "{version}"\n')
    (repo / "a.txt").write_text(f"source {version}\n")
    git_run("commit", "-qam", f"bump {version}", cwd=repo)
    git_run("push", "-q", cwd=repo)


def test_force_overwrite_can_drop_foreign_unreleased_commit_after_release(source_repo, public_remote, run, tmp_path):
    """§16 / §8.1 with §10 item 3: --force-overwrite no longer exists,
    so a foreign commit can never be dropped: exit 1, the commit stays, no
    tag. Then the replacement route: pull, then
    release; F stays an ancestor of the release and its file is carried."""
    repo, remote = _setup(source_repo, public_remote, run)
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0
    foreign = tmp_path / "foreign"
    git_run("clone", "-q", remote, str(foreign))
    git_run("config", "user.name", "Foreign", cwd=foreign)
    git_run("config", "user.email", "f@example.test", cwd=foreign)
    (foreign / "foreign.txt").write_text("unreleased")
    git_run("add", "foreign.txt", cwd=foreign)
    git_run("commit", "-qm", "foreign", cwd=foreign)
    git_run("push", "-q", cwd=foreign)
    f = git_run("rev-parse", "HEAD", cwd=foreign).strip()
    _advance_source(repo, "1.1.0")
    result = run(["--force-overwrite", "--release", "1.1.0"], repo)
    assert result.code == 1
    assert "removed in 1.1.0" in result.stderr
    assert git_run("ls-remote", remote, "refs/heads/main").split()[0] == f
    assert _remote_tag(remote, "v1.1.0") is None
    assert run(["--release", "1.1.0", "--yes"], repo).code == 5     # pull needed (§8.8)
    _pull_decide(run, repo, remote, {"foreign.txt": "leave"})
    result = run(["--release", "1.1.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    tagged = git_run("rev-parse", "refs/tags/v1.1.0^{commit}", cwd=repo / ".publish").strip()
    assert git_run("merge-base", "--is-ancestor", f, tagged, cwd=repo / ".publish") == ""
    assert git_run("show", f"{tagged}:foreign.txt", cwd=repo / ".publish") == "unreleased"


# test_force_overwrite_refuses_when_local_base_lacks_released_ancestry:
# deleted (§10 item 3: force is removed).


def test_transaction_provider_order_is_branch_tags_github_pypi_publish(source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run)
    events = []
    real_git = pubrepo.git

    def spy_git(*args, **kwargs):
        if push_argv(args) and kwargs.get("cwd") == str(repo / ".publish"):
            if _is_branch_push(args):                          # re-target
                events.append("branch")
            elif any(str(arg).startswith("refs/tags/v1.0.0") for arg in args):
                events.append("public_tag")
        return real_git(*args, **kwargs)

    class Fake:
        def __init__(self, name): self.name = name
        def check_live(self, ctx): events.append(f"{self.name}:check"); return []
        def build(self, ctx):
            if self.name == "pypi": events.append("pypi:build")
        def reconcile(self, ctx): events.append(f"{self.name}:reconcile")
        def execute(self, ctx, stage): events.append(f"{self.name}:{stage}")

    monkeypatch.setattr(pubrepo, "git", spy_git)
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _c: [Fake("github"), Fake("pypi")])
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    ordered = ["branch", "public_tag", "github:draft", "github:upload", "pypi:upload", "github:publish"]
    indices = [events.index(item) for item in ordered]
    assert indices == sorted(indices)


# AC75: AC75 re-target: the branch push is `push --porcelain origin <sha>:refs/heads/main` (_is_branch_push); assertions unchanged
def test_branch_push_failure_rolls_back_and_keeps_unpushed_journal(source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run)
    real = pubrepo.git

    def fail_push(*args, **kwargs):
        if _is_branch_push(args) and kwargs.get("cwd") == str(repo / ".publish"):  # re-target
            return subprocess.CompletedProcess(args, 1, "", "denied")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", fail_push)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 2
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""
    assert pubrepo.git("rev-parse", "HEAD", cwd=repo / ".publish", check=False).returncode != 0
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["remote_writes"] == []


def test_ambiguous_branch_push_is_reconciled_before_retrying(
        source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run)
    real = pubrepo.git
    pushes = {"count": 0}

    def ambiguous_push(*args, **kwargs):
        if (_is_branch_push(args)                              # re-target
                and kwargs.get("cwd") == str(repo / ".publish")):
            pushes["count"] += 1
            real(*args, **kwargs)
            return subprocess.CompletedProcess(args, 1, "", "ambiguous response")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", ambiguous_push)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    assert pushes["count"] == 1
    assert git_run("ls-remote", remote, "refs/heads/main").strip()


def test_public_snapshot_drift_refuses_before_rebuild(source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run)
    marker = repo / ".publish" / "marker"
    marker.write_text("untouched")
    real = pubrepo.discover_release_state
    count = {"n": 0}

    def drift(*args, **kwargs):
        discovery = real(*args, **kwargs)
        count["n"] += 1
        if count["n"] == 2:
            discovery.branch_sha = "changed"
        return discovery

    monkeypatch.setattr(pubrepo, "discover_release_state", drift)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "state changed" in result.stderr
    assert marker.read_text() == "untouched"


# AC75: §10 item 18 (commands that push confirm; §17.3 one prompt: the typed PyPI version)
def test_prompt_pypi_cancellation_rolls_back_before_remote_write(source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run, release_extra={
        "pypi": {"enabled": True, "package": "demo", "repository": "pypi"},
    })

    class FakePyPI:
        name = "pypi"
        def check_live(self, _ctx): return []
        def build(self, _ctx): return None
        def reconcile(self, _ctx): return None
        def execute(self, _ctx, _stage): return None

    # Design §17.3: a run that will upload to PyPI asks for the
    # exact version in its one prompt (no [y/N]); a wrong version cancels (1)
    # with nothing written; the immutability note is shown.
    import io
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": "1")           # the release choice
    monkeypatch.setattr("sys.stdin", io.StringIO("1.0.1\n"))           # a wrong version
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _c: [FakePyPI()])
    result = run(["publish"], repo)
    assert result.code == 1
    assert "The PyPI upload is immutable." in result.stderr
    assert result.stderr.count("Type 1.0.0 to release:") == 1 and "[y/N]" not in result.stderr
    assert "Cancelled; nothing was written." in result.stderr
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""
    assert pubrepo.git("rev-parse", "HEAD", cwd=repo / ".publish", check=False).returncode != 0


def test_prompt_pypi_typed_version_proceeds(source_repo, public_remote, run, monkeypatch):
    """§17.3: typing the exact version in the one prompt releases."""
    import io
    repo, remote = _setup(source_repo, public_remote, run, release_extra={
        "pypi": {"enabled": True, "package": "demo", "repository": "pypi"},
    })

    class FakePyPI:
        name = "pypi"
        def check_live(self, _ctx): return []
        def build(self, _ctx): return None
        def reconcile(self, _ctx): return None
        def execute(self, _ctx, _stage): return None

    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO("1.0.0\n"))
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _c: [FakePyPI()])
    result = run(["--release", "1.0.0"], repo)
    assert result.code == 0, result.stderr
    assert result.stderr.count("Type 1.0.0 to release:") == 1 and "[y/N]" not in result.stderr
    assert _remote_tag(remote, "v1.0.0") is not None


def test_explicit_pypi_release_never_prompts(source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run, release_extra={
        "pypi": {"enabled": True, "package": "demo", "repository": "pypi"},
    })

    class FakePyPI:
        name = "pypi"
        def check_live(self, _ctx): return []
        def build(self, _ctx): return None
        def reconcile(self, _ctx): return None
        def execute(self, _ctx, _stage): return None

    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _c: [FakePyPI()])
    monkeypatch.setattr("builtins.input", lambda _="": (_ for _ in ()).throw(AssertionError("prompted")))
    assert run(["--release", "1.0.0", "--yes"], repo).code == 0


def _tag_push(args, version_tag: str) -> bool:
    """A push of refs/tags/<tag>, from the source repo (`push <remote> refs/tags/v…`)
    or the publish clone (`-c … push <url> refs/tags/v…:refs/tags/v…`). R5."""
    push = push_argv(args)
    return bool(push) and push[-1].startswith(f"refs/tags/{version_tag}")


def test_release_absent_nonrelease_mutating_git_argv_stays_legacy(source_repo, public_remote, run, monkeypatch):
    remote = public_remote()
    repo = source_repo({"a.txt": "a\n"}, {"remote": remote, "include": ["a.txt"]})
    assert run(["init"], repo).code == 0
    calls = []
    real = pubrepo.git

    # Design §16 row "R1 compatibility gate" (Replaced → §10)
    # and "§7 step 10 create the normal local public snapshot commit"
    # (Replaced → §4.2): the new argv is write-tree / commit-tree / push
    # --porcelain <sha>:refs/heads/main, then update-ref; audit tag unchanged.
    def spy(*args, **kwargs):
        if args and (args[0] in ("add", "commit", "write-tree", "commit-tree", "push",
                                 "update-ref", "tag") or push_argv(args)):  # AC75 re-target: the porcelain push argv
            calls.append((args, kwargs.get("cwd")))
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spy)
    assert run(["publish", "--yes"], repo).code == 0
    publish = str(repo / ".publish")
    assert not any(call[0] in ("add", "commit") for call, _cwd in calls)
    pub_calls = [call for call, cwd in calls if cwd == publish]
    assert [call[0] for call in pub_calls] == ["write-tree", "commit-tree", "push", "update-ref"]
    sha = pub_calls[2][-1].split(":")[0]
    assert pub_calls[2] == ("push", "--porcelain", "origin", f"{sha}:refs/heads/main")
    assert pub_calls[3][:3] == ("update-ref", "refs/heads/main", sha)
    source_calls = [call for call, cwd in calls if cwd == str(repo)]
    # AC75 re-target (§10 item 20, §18.9 R2): the audit bookmark is one
    # create-only update-ref and, with no tag_remote, no push and no tag
    assert not any(call[0] == "tag" for call in source_calls)
    bookmarks = [call for call in source_calls if call[0] == "update-ref"
                 and call[1].startswith("refs/pubrepo/bookmarks/")]
    assert len(bookmarks) == 1 and bookmarks[0][2:] == ("HEAD", "")
    assert not any(call[0] == "push" or push_argv(call) for call in source_calls)
    assert all("refs/tags/v" not in " ".join(call) for call, _cwd in calls)


# AC75: §16 row "§7 step 13 build from the export" (Amended → §8.3 --accept-build-changes)
def test_candidate_export_is_exact_git_archive_and_honors_export_ignore(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({
        "version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n",
        "generated.txt": "candidate-only\n",
        ".gitattributes": "generated.txt export-ignore\n",
    }, {
        "remote": remote,
        "include": ["version.py", "a.txt", "generated.txt", ".gitattributes"],
        "release": _release(),
    })
    assert run(["init"], repo).code == 0
    # .gitattributes is an item-3 build file; a first release off a terminal
    # acknowledges it (design §8.3, AC72).
    assert run(["--release", "1.0.0", "--yes", "--accept-build-changes"], repo).code == 0
    publish = repo / ".publish"
    assert "generated.txt" in git_run("ls-tree", "-r", "--name-only", "HEAD", cwd=publish).splitlines()
    export = publish / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "source"
    assert not (export / "generated.txt").exists()
    assert (export / "a.txt").read_text() == "a\n"


# AC75: §16 row "§7 step 13 build from the export" (Amended → §8.3 --accept-build-changes)
def test_configured_asset_omitted_by_export_ignore_refuses_prewrite(source_repo, public_remote, run, monkeypatch):
    remote = public_remote()
    repo = source_repo({
        "version.py": 'VERSION = "1.0.0"\n', "asset.bin": "asset",
        "CHANGELOG.md": "notes", ".gitattributes": "asset.bin export-ignore\n",
    }, {
        "remote": remote,
        "include": ["version.py", "asset.bin", "CHANGELOG.md", ".gitattributes"],
        "release": _release(github={"enabled": True, "repository": "owner/repo",
                                    "assets": ["asset.bin"], "notes_file": "CHANGELOG.md"}),
    })

    class FakeGitHub:
        name = "github"
        def check_live(self, _ctx): return []
        def build(self, _ctx): return None
        def reconcile(self, _ctx): return None
        def execute(self, _ctx, _stage): return None

    assert run(["init"], repo).code == 0
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [FakeGitHub()])
    # .gitattributes is an item-3 build file (design §8.3, AC72).
    result = run(["--release", "1.0.0", "--yes", "--accept-build-changes"], repo)
    assert result.code == 7
    assert "missing from candidate export" in result.stderr
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""


def test_release_audit_tag_failure_warns_but_never_blocks(source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run)
    # AC75 re-target (§10 item 20, §18.9 R1/R2): the audit bookmark's push to
    # a listed tag_remote fails; one warning, the release completes
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    cfg = repo / ".publish.toml"
    cfg.write_text(cfg.read_text().replace(
        "[publish]\n", f'[publish]\ntag_remote = "origin"\nprivate_remotes = ["{origin}"]\n', 1))
    git_run("commit", "-qam", "tag_remote = origin", cwd=repo)
    git_run("push", "-q", "origin", "main", cwd=repo)
    real = pubrepo.git

    def fail_audit(*args, **kwargs):
        if args[:2] == ("push", "--recurse-submodules=no") and kwargs.get("cwd") == str(repo):
            return subprocess.CompletedProcess(args, 1, "", "audit tag denied")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", fail_audit)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    assert "kept locally; push to origin failed" in result.stderr
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert "audit_tag" in journal.data["completed"]
    assert journal.data["state"] == "complete"


def test_release_choice_preserves_scrub_exit_four(source_repo, public_remote, run):
    repo, remote = _setup(source_repo, public_remote, run,
                          config_extra={"scrub": {"forbidden": ["hello"]}})
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 4
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""


def test_interrupt_before_first_remote_write_restores_baseline(source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run)
    publish = repo / ".publish"

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(pubrepo, "export_projection", interrupt)   # re-target
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 130
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""
    assert pubrepo.git("rev-parse", "HEAD", cwd=publish, check=False).returncode != 0
    assert git_run("status", "--porcelain", cwd=publish) == ""


# AC75: AC75 re-target: the tag push is `push --porcelain origin <ref>:<ref>` (_tag_push); assertions unchanged
def test_formal_source_tag_failure_resumes_without_recreating_public_tag(
        source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(
        source_repo, public_remote, run,
        release_extra={"source_tag": True, "source_remote": "origin"})
    real = pubrepo.git
    failed = {"done": False}
    public_tag_pushes = {"count": 0}

    def fail_source_once(*args, **kwargs):
        if _tag_push(args, "v1.0.0"):
            if kwargs.get("cwd") == str(repo / ".publish"):
                public_tag_pushes["count"] += 1
            elif kwargs.get("cwd") == str(repo) and not failed["done"]:
                failed["done"] = True
                return subprocess.CompletedProcess(args, 1, "", "injected source tag failure")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", fail_source_once)
    first = run(["--release", "1.0.0", "--yes"], repo)
    assert first.code == 7
    assert _remote_tag(remote, "v1.0.0") is not None
    assert git_run("ls-remote", "origin", "refs/tags/v1.0.0", cwd=repo).strip() == ""
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["in_flight"] == "source_tag"

    second = run(["--release", "1.0.0", "--yes"], repo)
    assert second.code == 0, second.stderr
    assert public_tag_pushes["count"] == 1
    assert git_run("ls-remote", "origin", "refs/tags/v1.0.0^{}", cwd=repo).strip()


# AC75: AC75 re-target: the tag push is `push --porcelain origin <ref>:<ref>` (_tag_push); assertions unchanged
def test_ambiguous_source_tag_push_reconciles_without_recreating_either_tag(
        source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(
        source_repo, public_remote, run,
        release_extra={"source_tag": True, "source_remote": "origin"})
    real = pubrepo.git
    injected = {"done": False}
    public_pushes = {"count": 0}
    source_pushes = {"count": 0}

    def ambiguous_once(*args, **kwargs):
        if _tag_push(args, "v1.0.0"):
            if kwargs.get("cwd") == str(repo / ".publish"):
                public_pushes["count"] += 1
            elif kwargs.get("cwd") == str(repo):
                source_pushes["count"] += 1
                if not injected["done"]:
                    injected["done"] = True
                    real(*args, **kwargs)
                    return subprocess.CompletedProcess(args, 1, "", "ambiguous response")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", ambiguous_once)
    first = run(["--release", "1.0.0", "--yes"], repo)
    assert first.code == 7
    assert public_pushes["count"] == 1
    assert source_pushes["count"] == 1

    second = run(["--release", "1.0.0", "--yes"], repo)
    assert second.code == 0, second.stderr
    assert public_pushes["count"] == 1
    assert source_pushes["count"] == 1


def test_audit_log_failure_is_resumable_and_append_is_idempotent(
        source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run)
    real = pubrepo._append_publish_log
    failed = {"done": False}

    def fail_once(publish_dir, entry):
        if entry.get("release") and not failed["done"]:
            failed["done"] = True
            raise OSError("injected audit failure")
        return real(publish_dir, entry)

    monkeypatch.setattr(pubrepo, "_append_publish_log", fail_once)
    first = run(["--release", "1.0.0", "--yes"], repo)
    assert first.code == 7
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["state"] == "active"
    assert journal.data["in_flight"] == "complete"

    second = run(["--release", "1.0.0", "--yes"], repo)
    assert second.code == 0, second.stderr
    entries = [json.loads(line) for line in (
        repo / ".publish" / ".git" / pubrepo.PUBLISH_LOG_NAME).read_text().splitlines()]
    assert len([entry for entry in entries if entry.get("release")]) == 1


def test_final_journal_write_failure_retries_without_duplicate_audit(
        source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run)
    real = pubrepo.journal_write
    failed = {"done": False}

    def fail_complete_once(journal):
        if journal.data.get("state") == "complete" and not failed["done"]:
            failed["done"] = True
            raise OSError("injected final journal failure")
        return real(journal)

    monkeypatch.setattr(pubrepo, "journal_write", fail_complete_once)
    first = run(["--release", "1.0.0", "--yes"], repo)
    assert first.code == 7
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["state"] == "active"
    assert journal.data["in_flight"] == "complete"

    second = run(["--release", "1.0.0", "--yes"], repo)
    assert second.code == 0, second.stderr
    entries = [json.loads(line) for line in (
        repo / ".publish" / ".git" / pubrepo.PUBLISH_LOG_NAME).read_text().splitlines()]
    assert len([entry for entry in entries if entry.get("release")]) == 1


# AC75: §16 row "§7 step 16 asset resolution from the candidate export" (Amended → §8.6: the notes file must be in the export)
@pytest.mark.parametrize("failing_name,failing_stage", [
    ("github", "draft"),
    ("github", "upload"),
    ("pypi", "upload"),
    ("github", "publish"),
])
def test_provider_failure_resumes_exact_stage_without_rebuilding(
        source_repo, public_remote, run, monkeypatch, failing_name, failing_stage):
    release_extra = {
        "github": {"enabled": True, "repository": "owner/repo"},
        "pypi": {"enabled": True, "package": "demo", "repository": "pypi"},
    }
    # With GitHub enabled the notes file must be in the export.
    repo, _remote = _setup(
        source_repo, public_remote, run, release_extra=release_extra,
        files_extra={"CHANGELOG.md": "# 1.0.0\n"})
    failed = {"done": False}
    events = []

    class FakeAdapter:
        def __init__(self, name):
            self.name = name

        def check_live(self, _ctx):
            return []

        def build(self, _ctx):
            events.append((self.name, "build"))

        def reconcile(self, _ctx):
            events.append((self.name, "reconcile"))

        def execute(self, _ctx, stage):
            events.append((self.name, stage))
            if ((self.name, stage) == (failing_name, failing_stage)
                    and not failed["done"]):
                failed["done"] = True
                raise pubrepo.ReleaseError("injected provider failure")

    adapters = [FakeAdapter("github"), FakeAdapter("pypi")]
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: adapters)
    first = run(["--release", "1.0.0", "--yes"], repo)
    assert first.code == 7
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["in_flight"] == f"{failing_name}:{failing_stage}"
    export = (repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME
              / "1.0.0" / "source" / "a.txt")
    export_mtime = export.stat().st_mtime_ns
    build_count = events.count(("pypi", "build"))

    second = run(["--release", "1.0.0", "--yes"], repo)
    assert second.code == 0, second.stderr
    assert export.stat().st_mtime_ns == export_mtime
    assert events.count(("pypi", "build")) == build_count
    assert events.count((failing_name, failing_stage)) == 2


def test_provider_capability_failure_leaves_publish_tree_untouched(
        source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run, release_extra={
        "github": {"enabled": True, "repository": "owner/repo"},
    })
    marker = repo / ".publish" / "preflight-marker"
    marker.write_text("untouched")

    class Unavailable:
        name = "github"

        def check_live(self, _ctx):
            return [{"level": "error", "field": "release.github.auth",
                     "message": "injected unavailable provider"}]

    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [Unavailable()])
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 7
    assert "injected unavailable provider" in result.stderr
    assert marker.read_text() == "untouched"
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""


def test_release_journal_and_artifacts_never_capture_environment_secrets(
        source_repo, public_remote, run, monkeypatch):
    repo, _remote = _setup(source_repo, public_remote, run)
    canaries = {
        "TWINE_PASSWORD": "canary-twine-password",
        "TWINE_USERNAME": "canary-twine-user",
        "GH_TOKEN": "canary-github-token",
        "GITHUB_TOKEN": "canary-github-alt-token",
    }
    for name, value in canaries.items():
        monkeypatch.setenv(name, value)
    result = run(["--release", "1.0.0", "--yes"], repo)
    assert result.code == 0, result.stderr
    release_dir = repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME
    payload = b"\n".join(path.read_bytes() for path in release_dir.rglob("*")
                          if path.is_file())
    for value in canaries.values():
        assert value.encode() not in payload


def test_build_failure_rolls_back_and_next_attempt_supersedes_without_remote_write(
        source_repo, public_remote, run, monkeypatch):
    repo, remote = _setup(source_repo, public_remote, run, release_extra={
        "pypi": {"enabled": True, "package": "demo", "repository": "pypi"},
    })

    class FakePyPI:
        name = "pypi"

        def __init__(self, fail):
            self.fail = fail

        def check_live(self, _ctx):
            return []

        def build(self, _ctx):
            if self.fail:
                raise pubrepo.ReleaseError("injected build failure")

        def reconcile(self, _ctx):
            return None

        def execute(self, _ctx, _stage):
            return None

    failing = FakePyPI(True)
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [failing])
    first = run(["--release", "1.0.0", "--yes"], repo)
    assert first.code == 7
    assert git_run("ls-remote", remote, "refs/heads/main").strip() == ""
    assert pubrepo.git("rev-parse", "HEAD", cwd=repo / ".publish", check=False).returncode != 0
    first_attempt = pubrepo.load_journal(repo / ".publish", "1.0.0").data["attempt_id"]

    succeeding = FakePyPI(False)
    monkeypatch.setattr(pubrepo, "enabled_adapters", lambda _config: [succeeding])
    second = run(["--release", "1.0.0", "--yes"], repo)
    assert second.code == 0, second.stderr
    second_journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert second_journal.data["attempt_id"] != first_attempt
    assert first_attempt in second_journal.data["supersedes"]
    entries = [json.loads(line) for line in (
        repo / ".publish" / ".git" / pubrepo.PUBLISH_LOG_NAME).read_text().splitlines()]
    assert any(entry.get("event") == "release_superseded"
               and entry.get("attempt_id") == first_attempt for entry in entries)
