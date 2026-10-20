"""Release on fast-forward publishing (design §8, §16). Every remote is
a local bare repo inside the sandbox."""
import pytest

import pubrepo
from conftest import git_run
from test_release_transaction import _release, _remote_tag


def _released(source_repo, public_remote, run, version="1.0.0", **release_extra):
    """A source repo with one pubrepo release done; returns (repo, url)."""
    url = public_remote()
    repo = source_repo({"version.py": f'VERSION = "{version}"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"],
        "release": _release(version, **release_extra)})
    assert run(["init"], cwd=repo).code == 0
    r = run(["--release", version, "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    return repo, url


def _collab_commit(url, tmp_path, files):
    """A collaborator pushes a commit (outside any pubrepo run)."""
    c = tmp_path / f"collab-commit-{len(list(tmp_path.glob('collab-commit-*')))}"
    git_run("clone", "-q", url, str(c))
    for rel, text in files.items():
        (c / rel).parent.mkdir(parents=True, exist_ok=True)
        (c / rel).write_text(text)
    git_run("add", "-A", cwd=c)
    git_run("-c", "user.name=Collab", "-c", "user.email=collab@example.invalid",
            "commit", "-q", "-m", "collab", cwd=c)
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=c)


def _collab_tag(url, tmp_path, name, *, at="main"):
    """A collaborator pushes a lightweight tag (no pubrepo marker)."""
    c = tmp_path / f"collab-{name}"
    git_run("clone", "-q", url, str(c))
    git_run("tag", name, at, cwd=c)
    git_run("push", "-q", "origin", f"refs/tags/{name}", cwd=c)


def test_release_tag_carries_the_markers_and_is_managed(source_repo, public_remote, run):
    """ledger::release_tag_is_managed (design §8.5): pubrepo's annotated tag
    message carries Pubrepo-Release and Pubrepo-Source (the full source
    sha); read back, the tag is managed and sets the version base."""
    repo, url = _released(source_repo, public_remote, run)
    pub = repo / ".publish"
    message = git_run("cat-file", "tag", "v1.0.0", cwd=pub)
    source = git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert "Pubrepo-Release: 1.0.0\n" in message and f"Pubrepo-Source: {source}\n" in message
    ledger = pubrepo.read_remote_ledger(pub, "v{version}", source_root=repo)
    assert set(ledger.managed) == {"v1.0.0"} and ledger.latest == "1.0.0"
    assert ledger.foreign == {}


def test_foreign_tags_never_block_and_a_squat_is_7(source_repo, public_remote, run, tmp_path):
    """ledger::foreign_and_squat (AC67): a collaborator's v1.0.1 without the
    marker is foreign (reported, not the version base, never blocking); a
    foreign v1.1.0 squatting the release name → 7."""
    repo, url = _released(source_repo, public_remote, run)
    _collab_tag(url, tmp_path, "v1.0.1")
    pub = repo / ".publish"
    ledger = pubrepo.read_remote_ledger(pub, "v{version}", source_root=repo)
    assert set(ledger.managed) == {"v1.0.0"} and set(ledger.foreign) == {"v1.0.1"}
    assert ledger.latest == "1.0.0"
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    _collab_tag(url, tmp_path, "v1.1.0")
    squat = git_run("ls-remote", url, "refs/tags/v1.1.0").split()[0]
    (repo / "version.py").write_text('VERSION = "1.1.0"\n')
    git_run("commit", "-qam", "1.1.0", cwd=repo)
    r = run(["--release", "1.1.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "v1.1.0 already exists" in r.stderr
    assert git_run("ls-remote", url, "refs/tags/v1.1.0").split()[0] == squat


def test_ledger_reads_tag_objects_without_touching_refs_tags(source_repo, public_remote, run,
                                                             git_policy):
    """Design §8.5: tag messages come from tag objects fetched by id:
    no refspec destination, no `+`, no FETCH_HEAD, nothing under refs/tags."""
    repo, url = _released(source_repo, public_remote, run)
    fresh = repo.parent / "fresh-clone"
    git_run("clone", "-q", "--no-tags", url, str(fresh))
    before = git_run("for-each-ref", "refs/tags", cwd=fresh)
    ledger = pubrepo.read_remote_ledger(fresh, "v{version}", source_root=repo)
    assert set(ledger.managed) == {"v1.0.0"}
    assert git_run("for-each-ref", "refs/tags", cwd=fresh) == before
    assert not (fresh / ".git" / "FETCH_HEAD").exists()


def _published_release_repo(source_repo, public_remote, run):
    """Release-enabled, one --no-release publish done, a pending change."""
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"], "release": _release("1.0.0")})
    assert run(["init"], cwd=repo).code == 0
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    (repo / "a.txt").write_text("b\n")
    git_run("commit", "-qam", "b", cwd=repo)
    return repo, url


def _ref_state(repo, url):
    pub = repo / ".publish"
    return (git_run("for-each-ref", cwd=pub), git_run("rev-parse", "HEAD", cwd=pub),
            git_run("for-each-ref", "refs/tags", cwd=repo), git_run("ls-remote", url))


def test_failure_after_snapshot_commit_no_ref_moved(source_repo, public_remote, run,
                                                    monkeypatch):
    """release::failure_after_snapshot_commit_no_ref_moved: a
    failure after the candidate snapshot is made and before the push leaves
    refs/heads, HEAD, every tag and the remote as they were."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    before = _ref_state(repo, url)

    def broken_export(*_a, **_k):           # as export_projection fails (git archive)
        raise pubrepo.ReleaseError("ERROR: git archive failed: injected")

    monkeypatch.setattr(pubrepo, "export_projection", broken_export)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert _ref_state(repo, url) == before


def test_failure_after_tag_create_rolls_back_tag(source_repo, public_remote, run, monkeypatch):
    """release::failure_after_tag_create_rolls_back_tag: the run
    creates the local release tag, its push fails, and the tag is deleted
    again; the journal's rollback marker is cleared with it."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    real = pubrepo.push_porcelain

    def refuse_tag(publish_dir, refspecs, **kwargs):
        if any(r.startswith("refs/tags/v1.0.0") for r in refspecs):
            return pubrepo.PushOutcome("host_refused", "tag protected", [])
        return real(publish_dir, refspecs, **kwargs)

    monkeypatch.setattr(pubrepo, "push_porcelain", refuse_tag)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    pub = repo / ".publish"
    assert git_run("tag", "-l", "v1.0.0", cwd=pub).strip() == ""
    assert _remote_tag(url, "v1.0.0") is None
    journal = pubrepo.load_journal(pub, "1.0.0")
    assert journal.data.get("local_tags_created") == []


# --- the transaction (design §8.8) -----------------------------------------

class _CountingAdapter:
    """A provider that records its build and stage calls (no network)."""

    def __init__(self, name, events):
        self.name, self.events, self.pending, self.view = name, events, [], None

    def check_live(self, _ctx):
        return []

    def build(self, _ctx):
        self.events.append(f"{self.name}:build")

    def reconcile(self, _ctx):
        pass

    def execute(self, _ctx, stage):
        self.events.append(f"{self.name}:{stage}")


def test_push_during_build_supersedes_then_reuses_the_artifacts(source_repo, public_remote,
                                                                 run, monkeypatch, tmp_path):
    """release::push_during_build (AC70): a collaborator pushes while the
    candidate is being built → 5, the attempt superseded with its managed-
    tree identity, nothing pushed or moved; after the pull the identical
    command reuses the artifacts (no rebuild) and releases."""
    from test_release_transaction import _pull_decide
    repo, url = _published_release_repo(source_repo, public_remote, run)
    events = []
    monkeypatch.setattr(pubrepo, "enabled_adapters",
                        lambda _c: [_CountingAdapter("github", events)])
    local = git_run("rev-parse", "HEAD", cwd=repo / ".publish").strip()
    real_export = pubrepo.export_projection

    def collaborator_pushes_during_export(*a, **k):
        _collab_commit(url, tmp_path, {"collab.txt": "c\n"})
        return real_export(*a, **k)

    monkeypatch.setattr(pubrepo, "export_projection", collaborator_pushes_during_export)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 5, r.stderr
    assert "moved while release 1.0.0 was being built" in r.stderr
    journal = pubrepo.load_journal(repo / ".publish", "1.0.0")
    assert journal.data["state"] == "superseded" and journal.data["managed_tree_identity"]
    assert _remote_tag(url, "v1.0.0") is None
    assert git_run("rev-parse", "HEAD", cwd=repo / ".publish").strip() == local
    assert events.count("github:build") == 1
    monkeypatch.setattr(pubrepo, "export_projection", real_export)
    _pull_decide(run, repo, url, {})
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "Reusing the artifacts" in r.stderr
    assert events.count("github:build") == 1                     # not rebuilt
    assert _remote_tag(url, "v1.0.0") is not None


@pytest.mark.parametrize("retry, attempts", [(False, 1), (True, 3)])
def test_retry_stops_after_three_laps(source_repo, public_remote, run, monkeypatch,
                                      retry, attempts):
    """release::retry_bound (AC70): every push loses the race → 5; without
    --retry after one attempt, with --retry after three."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    pushes = []

    def always_rejected(publish_dir, sha, branch):
        pushes.append(sha)
        return pubrepo.PushOutcome("rejected_nonff", "fetch first", [])

    monkeypatch.setattr(pubrepo, "push_snapshot", always_rejected)
    argv = ["--release", "1.0.0", "--yes"] + (["--retry"] if retry else [])
    r = run(argv, cwd=repo)
    assert r.code == 5, r.stderr
    assert len(pushes) == attempts
    assert _remote_tag(url, "v1.0.0") is None


def _fail_tag_push_once(monkeypatch):
    real = pubrepo.push_porcelain
    state = {"failed": False}

    def flaky(publish_dir, refspecs, **kwargs):
        if not state["failed"] and any(r.startswith("refs/tags/v1.0.0") for r in refspecs):
            state["failed"] = True
            return pubrepo.PushOutcome("host_refused", "flaky host", [])
        return real(publish_dir, refspecs, **kwargs)

    monkeypatch.setattr(pubrepo, "push_porcelain", flaky)


def test_push_between_candidate_and_tag_resume_completes(source_repo, public_remote, run,
                                                         monkeypatch, tmp_path):
    """release::resume_after_push (AC69): the branch push lands, the tag push
    fails, a collaborator pushes on top; the rerun resumes (the candidate is
    still an ancestor, no pull gate) and completes with the tag on it."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    _fail_tag_push_once(monkeypatch)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7 and "release incomplete at public_tag (" in r.stderr, r.stderr
    candidate = pubrepo.load_journal(repo / ".publish", "1.0.0").data["public_candidate"]
    _collab_commit(url, tmp_path, {"collab.txt": "c\n"})
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert git_run("rev-parse", "refs/tags/v1.0.0^{}", cwd=repo / ".publish").strip() == candidate
    assert _remote_tag(url, "v1.0.0") is not None


def test_resume_refused_when_the_candidate_left_public(source_repo, public_remote, run,
                                                      monkeypatch):
    """§8.8: a resume needs the candidate to stay an ancestor of the tip;
    rewound off public → 7 naming restore-ancestry and release-abandon."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    before = git_run("ls-remote", url, "refs/heads/main").split()[0]
    _fail_tag_push_once(monkeypatch)
    assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 7
    bare = url.removeprefix("file://")
    git_run("update-ref", "refs/heads/main", before, cwd=bare)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "no longer on the public branch" in r.stderr
    assert "repair restore-ancestry v1.0.0 --plan" in r.stderr
    assert "release-abandon 1.0.0" in r.stderr


def test_foreign_github_release_object_is_a_named_refusal(source_repo, public_remote, run,
                                                          monkeypatch):
    """release::foreign_provider_object (§8.8): a GitHub release for the tag
    that this attempt did not create → 7 naming it; the journal is terminal
    and nothing is created on GitHub."""
    import json
    import subprocess
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n",
                        "CHANGELOG.md": "notes\n"}, {
        "remote": url, "include": ["version.py", "a.txt", "CHANGELOG.md"],
        "release": _release("1.0.0", github={"enabled": True, "repository": "owner/repo"})})
    assert run(["init"], cwd=repo).code == 0
    calls = []

    def gh(argv, **_kwargs):
        calls.append(argv)
        if argv[1:3] == ["release", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"tagName": "v1.0.0", "isDraft": False, "assets": []}), "")
        return subprocess.CompletedProcess(argv, 0, "gh ok\n", "")

    monkeypatch.setattr(pubrepo, "_run_tool", gh)
    monkeypatch.setattr(pubrepo.GitHubAdapter, "check_live", lambda self, ctx: [])
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "GitHub release v1.0.0 already exists and was not created by this release" in r.stderr
    assert pubrepo.load_journal(repo / ".publish", "1.0.0").data["state"] == "terminal"
    assert not [a for a in calls if a[1:3] in (["release", "create"], ["release", "upload"])]


# --- the one confirmation (design §8.3, §17.3; AC101 release row, AC103) -------

@pytest.mark.pty
@pytest.mark.parametrize("answer, code", [("n", 1), ("y", 0)])
def test_release_shows_one_combined_prompt_on_a_terminal(source_repo, public_remote, run,
                                                         tmp_path, answer, code):
    """release::one_prompt (AC101 release row): on a real terminal a
    --release run shows exactly one [y/N], carrying the release and the
    publish previews; n → 1 with nothing pushed, y → released."""
    from test_ff_auth import _cli_on_a_pty
    repo, url = _published_release_repo(source_repo, public_remote, run)
    before = git_run("ls-remote", url, "refs/heads/main").split()[0]
    exit_code, text, _calls = _cli_on_a_pty(repo, ["--release", "1.0.0"], answer,
                                            tmp_path / "pty-ac62.log")
    assert text.count("[y/N]") == 1, text
    assert "Release 1.0.0 (v1.0.0)" in text and "Publish to " in text
    assert exit_code == code, text
    if code:
        assert git_run("ls-remote", url, "refs/heads/main").split()[0] == before
        assert _remote_tag(url, "v1.0.0") is None
    else:
        assert _remote_tag(url, "v1.0.0") is not None


def test_release_prompt_is_bound_to_the_shown_tip(source_repo, public_remote, run,
                                                  monkeypatch, tmp_path):
    """§8.3: the confirmation binds the shown tip (and the managed-tree
    identity); a collaborator push while the human answers → 5, nothing
    released."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)

    def answer_while_the_tip_moves(summary):
        assert "Release 1.0.0 (v1.0.0)" in summary
        with monkeypatch.context() as m:
            m.delenv("PUBREPO_AC62_LOG", raising=False)
            _collab_commit(url, tmp_path, {"collab.txt": "c\n"})
        return True

    monkeypatch.setattr(pubrepo, "confirm_yes_no", answer_while_the_tip_moves)
    r = run(["--release", "1.0.0"], cwd=repo)
    assert r.code == 5, r.stderr
    assert _remote_tag(url, "v1.0.0") is None


@pytest.mark.parametrize("answer, code", [("n", 1), ("y", 0)])
def test_resume_confirms_again_on_a_terminal(source_repo, public_remote, run, monkeypatch,
                                             answer, code):
    """AC103 (first half, rerun): a resume is a covered run and asks again,
    once, naming the resume; n → 1 with nothing more done, y → completes."""
    import io
    repo, url = _published_release_repo(source_repo, public_remote, run)
    _fail_tag_push_once(monkeypatch)
    assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 7
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(answer + "\n"))
    r = run(["--release", "1.0.0"], cwd=repo)
    assert r.code == code, r.stderr
    assert r.stderr.count("[y/N]") == 1 and "Resume release 1.0.0 (v1.0.0)" in r.stderr
    assert (_remote_tag(url, "v1.0.0") is not None) == (code == 0)


def test_managed_release_targets_are_the_managed_ledger(source_repo, public_remote, run,
                                                        tmp_path):
    """§2.3 managed_release_targets (design §8.5): managed tag → peeled
    sha, the release's own tag included and a foreign tag excluded; with
    releases disabled the release table's tag_format and adopt_tags still
    apply."""
    repo, url = _released(source_repo, public_remote, run)
    _collab_tag(url, tmp_path, "v1.0.1")
    pub = repo / ".publish"
    config = pubrepo.load_publish_config(repo / ".publish.toml")
    targets = pubrepo.managed_release_targets(repo, pub, config)
    assert targets == {"v1.0.0": git_run("rev-parse", "refs/tags/v1.0.0^{}", cwd=pub).strip()}
    disabled = dict(config, release=dict(config["release"], enabled=False))
    assert pubrepo.managed_release_targets(repo, pub, disabled) == targets


def test_gh_create_has_verify_tag(monkeypatch, tmp_path):
    """release::gh_create_has_verify_tag (AC103 second half; design §17.3):
    the draft is created with --verify-tag, so gh never makes the tag."""
    import subprocess
    from test_release_github import _ctx
    ctx = _ctx(tmp_path)
    calls = []
    monkeypatch.setattr(pubrepo, "_run_tool", lambda argv, **_k: (calls.append(argv) or
                        subprocess.CompletedProcess(
                            argv, 1 if argv[1:3] == ["release", "view"] else 0, "",
                            "release not found" if argv[1:3] == ["release", "view"] else "")))
    adapter = pubrepo.GitHubAdapter(ctx.config)
    adapter.reconcile(ctx)
    adapter.execute(ctx, "draft")
    create = next(argv for argv in calls if argv[1:3] == ["release", "create"])
    assert "--verify-tag" in create


# --- C6b: the export, the build and the scrub ------------------------------------------

def test_export_is_projection_only_carried_gitattributes_ignored(source_repo, public_remote,
                                                                 run, tmp_path):
    """release::export_projection (design §8.6): a
    carried .gitattributes with export-ignore and export-subst over managed
    files changes nothing; the export holds exactly the managed (and keep)
    paths with the candidate's bytes; carried files are not packaged."""
    from test_release_transaction import _pull_decide
    repo, url = _published_release_repo(source_repo, public_remote, run)
    attributes = "a.txt export-ignore\nversion.py export-subst\n"
    _collab_commit(url, tmp_path, {".gitattributes": attributes, "notes.txt": "carried\n"})
    _pull_decide(run, repo, url, {".gitattributes": "leave"})         # flagged: carried as-is
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    pub = repo / ".publish"
    out_dir = pub / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "source"
    files = sorted(str(p.relative_to(out_dir)) for p in out_dir.rglob("*") if p.is_file())
    assert files == ["a.txt", "version.py"]
    for name in files:
        # refs/tags/…: a bare `v1.0.0:<path>` reads as a dotted host:path to the spawn audit
        blob = git_run("show", f"refs/tags/v1.0.0:{name}", cwd=pub).encode()
        assert (out_dir / name).read_bytes() == blob
    assert git_run("show", "refs/tags/v1.0.0:notes.txt", cwd=pub) == "carried\n"   # still public


STRIPPED = ("TWINE_PASSWORD", "TWINE_REPOSITORY_URL", "GH_TOKEN", "GITHUB_TOKEN",
            "SSH_AUTH_SOCK", "GPG_AGENT_INFO", "GIT_ASKPASS", "SSH_ASKPASS",
            "MYCORP_DEPLOY", "EXTRA_TOKEN")


def test_build_environment_has_no_credentials(source_repo, public_remote, run, monkeypatch):
    """release::build_env_stripped (AC72): the
    build backend sees none of TWINE_*, GH_TOKEN, GITHUB_TOKEN, the agent
    sockets and askpass helpers, or a strip_env match (exact or PREFIX*);
    other variables and SOURCE_DATE_EPOCH pass through."""
    import subprocess
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"],
        "release": _release("1.0.0", strip_env=["MYCORP_*", "EXTRA_TOKEN"],
                            pypi={"enabled": True, "package": "demo", "repository": "pypi"})})
    assert run(["init"], cwd=repo).code == 0
    for name in STRIPPED:
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    monkeypatch.setenv("KEEP_ME", "visible")
    seen = {}

    def tool(argv, env=None, cwd=None):
        if argv[1:3] == ["-m", "build"]:
            seen.update(env or {})
            return subprocess.CompletedProcess(argv, 1, "", "build stopped by the test")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "check_live", lambda self, ctx: [])
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert seen, "the build was not reached"
    assert [n for n in STRIPPED if n in seen] == []
    assert seen.get("KEEP_ME") == "visible" and seen.get("SOURCE_DATE_EPOCH")
    assert "secret-" not in r.stdout + r.stderr


def test_sanitizer_redacts_stripped_variable_values(monkeypatch):
    """Values of the stripped credential variables are
    redacted from provider and git output, as are URL credentials."""
    monkeypatch.setenv("TWINE_PASSWORD", "pypi-s3cret-token")
    monkeypatch.setenv("GH_TOKEN", "ghp_abcdef123456")
    text = ("push https://alice:pw@example.invalid/x failed; token pypi-s3cret-token "
            "and ghp_abcdef123456")
    out = pubrepo.sanitize_provider_output(text)
    assert "alice:pw" not in out and "pypi-s3cret-token" not in out
    assert "ghp_abcdef123456" not in out and out.count("[redacted]") == 3


def _bad_archive(path, kind, member):
    """A distribution holding one unsafe member (and one ordinary file)."""
    import io
    import tarfile
    import zipfile
    if kind == "zip":
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("demo/ok.py", "ok")
            z.writestr(zipfile.ZipInfo(member), "escaped")
        return
    with tarfile.open(path, "w:gz") as t:
        ok = tarfile.TarInfo("demo-1.0.0/ok.py")
        ok.size = 2
        t.addfile(ok, io.BytesIO(b"ok"))
        info = tarfile.TarInfo(member if member != "link" else "demo-1.0.0/link")
        if member == "link":
            info.type, info.linkname = tarfile.SYMTYPE, "/etc/hostname"
            t.addfile(info)
        else:
            info.size = 7
            t.addfile(info, io.BytesIO(b"escaped"))


@pytest.mark.parametrize("kind, member", [("tar", "../escape"), ("tar", "/abs-x"),
                                          ("tar", "link"), ("zip", "../escape"),
                                          ("zip", "/abs-x")])
def test_unsafe_distribution_members_are_refused(tmp_path, kind, member):
    """release::extract_members_validated: a release sdist or
    wheel with `../escape`, an absolute member or (tar) a symlink is refused
    with a release-class error, and nothing appears outside the destination
    (the tmp tree is listed before and after)."""
    work = tmp_path / "work"
    work.mkdir()
    archive = work / ("demo-1.0.0-py3-none-any.whl" if kind == "zip" else "demo-1.0.0.tar.gz")
    _bad_archive(archive, kind, member)
    destination = work / "extracted"
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    with pytest.raises(pubrepo.ReleaseError) as exc:
        pubrepo.safe_extract_distribution(archive, destination)
    assert exc.value.exit_code == 7
    after = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*"))
    assert [p for p in after if p not in before] == [destination.relative_to(tmp_path)]
    assert list(destination.iterdir()) == []
    assert not (tmp_path / "escape").exists() and not pubrepo.Path("/abs-x").exists()


def test_export_failure_stderr_sanitized(source_repo, public_remote, run, monkeypatch, tmp_path):
    """release::export_failure_stderr_sanitized: a git
    shim makes `git archive` fail with a credential-shaped URL and a stripped
    variable's value on stderr; the message shows both redacted and the
    release journal holds neither."""
    import os
    import shutil
    repo, url = _published_release_repo(source_repo, public_remote, run)
    monkeypatch.setenv("GH_TOKEN", "ghp_injected0secret")
    shim_dir = tmp_path / "archive-shim"
    shim_dir.mkdir()
    shim = shim_dir / "git"
    shim.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = archive ]; then\n'
        "  echo \"fatal: unable to access 'https://alice:pa55word@example.invalid/x.git': "
        "token ghp_injected0secret refused\" >&2\n"
        "  exit 128\n"
        "fi\n"
        f'exec {shutil.which("git")} "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "git archive failed" in r.stderr and "[redacted]" in r.stderr
    assert "pa55word" not in r.stdout + r.stderr
    assert "ghp_injected0secret" not in r.stdout + r.stderr
    journals = list((repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME).rglob("*.json"))
    assert journals, "no release journal was written"
    for journal in journals:
        text = journal.read_text()
        assert "pa55word" not in text and "ghp_injected0secret" not in text


def test_git_error_message_is_sanitized(monkeypatch):
    """A GitError's message (argv and stderr) is sanitized; its raw
    .stderr stays for callers that match on git's wording."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_rawvalue123")
    e = pubrepo.GitError(["fetch", "https://bob:hunter22@example.invalid/r"],
                         "fatal: auth failed for https://bob:hunter22@example.invalid/r "
                         "(ghs_rawvalue123)\n")
    assert "hunter22" not in str(e) and "ghs_rawvalue123" not in str(e)
    assert "hunter22" in e.stderr


def test_surfaced_subprocess_stderr_goes_through_the_sanitizer():
    """Source audit (parse only): every `.stderr` read inside a
    `raise` or a `ui.*` call is wrapped in sanitize_provider_output(), is
    handed to GitError (which sanitizes its message), or is only matched
    (`in` / `==`, or by _identity_hint, which returns fixed text)."""
    import ast
    from pathlib import Path
    tree = ast.parse(Path(pubrepo.__file__).read_text(encoding="utf-8"))
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def ancestors(node):
        while node in parent:
            node = parent[node]
            yield node

    def surfaced(node):
        for a in ancestors(node):
            if isinstance(a, ast.Raise):
                return True
            if (isinstance(a, ast.Call) and isinstance(a.func, ast.Attribute)
                    and isinstance(a.func.value, ast.Name) and a.func.value.id == "ui"):
                return True
            if isinstance(a, ast.stmt):
                return False
        return False

    def cleared(node):
        for a in ancestors(node):
            if isinstance(a, ast.Compare):
                return True
            if isinstance(a, ast.Call) and isinstance(a.func, ast.Name) and \
                    a.func.id in ("sanitize_provider_output", "GitError", "_identity_hint"):
                return True
            if isinstance(a, ast.stmt):
                return False
        return False

    bare = [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr == "stderr"
            and not (isinstance(node.value, ast.Name) and node.value.id == "sys")
            and surfaced(node) and not cleared(node)]
    assert bare == [], f"unsanitized stderr surfaced at pubrepo.py lines {bare}"


# --- AC73: version_file and the downgrade warning (design §8 item 7) ----------

def _commit_if_dirty(repo, message):
    if git_run("status", "--porcelain", "--untracked-files=no", cwd=repo):
        git_run("commit", "-qam", message, cwd=repo)


def test_pulled_version_decrease_is_flagged_then_warns_downgrade(source_repo, public_remote,
                                                                 run, tmp_path):
    """release::version_file_downgrade_warns (AC73): a public change to
    version_file is flagged by pull (§5.2 item 3; `pull --yes` alone does
    not write it); once accepted, a --no-release publish warns "downgrade"
    naming both versions and still exits 0; releasing the lower version is
    refused (7)."""
    from test_release_transaction import _pull_decide
    repo, url = _released(source_repo, public_remote, run, "1.1.0")
    _collab_commit(url, tmp_path, {"version.py": 'VERSION = "1.0.4"\n'})
    assert run(["pull", "--yes"], cwd=repo).code != 0
    assert (repo / "version.py").read_text() == 'VERSION = "1.1.0"\n'
    _pull_decide(run, repo, url, {"version.py": "accept"})
    assert (repo / "version.py").read_text() == 'VERSION = "1.0.4"\n'
    _commit_if_dirty(repo, "pulled version")
    (repo / "a.txt").write_text("after the decrease\n")
    git_run("commit", "-qam", "edit", cwd=repo)
    r = run(["--no-release", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    warning = [line for line in r.stderr.splitlines() if "downgrade" in line.lower()]
    assert len(warning) == 1 and "1.0.4" in warning[0] and "1.1.0" in warning[0], r.stderr
    r = run(["--release", "1.0.4", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr


def test_local_version_decrease_warns_on_any_release_enabled_publish(source_repo,
                                                                     public_remote, run):
    """AC73: the warning is not tied to pull; a source edit
    that lowers version_file below the latest managed release warns on the
    next release-enabled publish. Equal or higher: no warning."""
    repo, _url = _released(source_repo, public_remote, run, "1.1.0")
    (repo / "a.txt").write_text("same version\n")
    git_run("commit", "-qam", "same", cwd=repo)
    r = run(["--no-release", "--yes"], cwd=repo)
    assert r.code == 0 and "downgrade" not in r.stderr.lower(), r.stderr
    (repo / "version.py").write_text('VERSION = "1.0.9"\n')
    git_run("commit", "-qam", "lower", cwd=repo)
    r = run(["--no-release", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "version downgrade" in r.stderr and "1.0.9" in r.stderr and "1.1.0" in r.stderr
    (repo / "version.py").write_text('VERSION = "1.2.0"\n')
    git_run("commit", "-qam", "higher", cwd=repo)
    r = run(["--no-release", "--yes"], cwd=repo)
    assert r.code == 0 and "downgrade" not in r.stderr.lower(), r.stderr


def _raise_on_tag_push(monkeypatch, kind):
    """push_porcelain raises for the release tag refspec, as the real code
    does for a locked clone (check_push_lock) or an unverifiable outcome."""
    real = pubrepo.push_porcelain

    def push(publish_dir, refspecs, **kwargs):
        if any(r.startswith("refs/tags/v1.0.0") for r in refspecs):
            if kind == "lock":
                git_run("config", "remote.origin.pushurl",
                        f"{pubrepo.PUSH_LOCK_PREFIX}test-lock", cwd=publish_dir)
                return real(publish_dir, refspecs, **kwargs)
            raise pubrepo._outcome_unknown(publish_dir, [("refs/tags/v1.0.0",
                                                          "refs/tags/v1.0.0")])
        return real(publish_dir, refspecs, **kwargs)

    monkeypatch.setattr(pubrepo, "push_porcelain", push)


@pytest.mark.parametrize("kind", ["lock", "outcome_unknown"])
def test_raising_tag_push_rolls_back_the_tag(source_repo, public_remote, run, monkeypatch,
                                             kind):
    """release::failure_after_tag_create_rolls_back_tag, raise paths
    when the tag push raises (locked clone, outcome
    unknown) instead of returning a failure, the run-created local tag is
    deleted and the journal marker cleared; the error is unchanged (2)."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    _raise_on_tag_push(monkeypatch, kind)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert ("publishing is locked" if kind == "lock" else "push outcome unknown") in r.stderr
    pub = repo / ".publish"
    assert git_run("tag", "-l", "v1.0.0", cwd=pub).strip() == ""
    assert _remote_tag(url, "v1.0.0") is None
    assert pubrepo.load_journal(pub, "1.0.0").data.get("local_tags_created") == []


@pytest.mark.rewrite      # release-abandon is outside INV-1's routine list; it deletes a tag
@pytest.mark.parametrize("on_remote", [False, True])
def test_release_abandon_removes_unpushed_run_created_tags(source_repo, public_remote, run,
                                                           monkeypatch, on_remote, git_policy):
    """A run that dies between the tag marker and its rollback (the
    rollback is disabled here, as a killed process would skip it) leaves
    a local tag listed in local_tags_created. release-abandon deletes it
    when it is absent on the remote, and leaves it when the remote has it."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    _raise_on_tag_push(monkeypatch, "outcome_unknown")
    monkeypatch.setattr(pubrepo, "_rollback_created_tag", lambda *a, **k: None,
                        raising=False)
    assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 2
    pub = repo / ".publish"
    assert git_run("tag", "-l", "v1.0.0", cwd=pub).strip() == "v1.0.0"
    if on_remote:
        git_run("push", "-q", url, "refs/tags/v1.0.0", cwd=pub)
    r = run(["release-abandon", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    # abandon is a local recovery command; it never pushes (no
    # `push :refs/tags/…`), as the shim log of that run shows.
    from ff_fixtures import _strip_config_pairs
    abandon_calls = [_strip_config_pairs(list(argv)) for argv in git_policy.runs[-1]]
    assert abandon_calls and not [a for a in abandon_calls if a[:1] == ["push"]]
    if on_remote:
        assert git_run("tag", "-l", "v1.0.0", cwd=pub).strip() == "v1.0.0"
        assert _remote_tag(url, "v1.0.0") is not None
    else:
        assert git_run("tag", "-l", "v1.0.0", cwd=pub).strip() == ""
        assert "Removed local tag v1.0.0" in r.stdout
    assert pubrepo.load_journal(pub, "1.0.0").data["state"] == "abandoned"


@pytest.mark.pty
@pytest.mark.parametrize("answer, code, outcome", [("1", 0, "released"),
                                                   ("2", 0, "published"),
                                                   ("3", 1, "cancelled"),
                                                   ("", 1, "cancelled")])
def test_release_choice_is_the_one_prompt_on_a_terminal(source_repo, public_remote, run,
                                                        tmp_path, answer, code, outcome):
    """release::one_prompt, no release flag (§17.3): on a
    real terminal with release enabled and no release flag,
    the release-choice question, shown after the full publish preview, is
    the only prompt. Release and no-release proceed with no [y/N]; cancel
    or an empty answer → 1 with nothing pushed. (The extra input lines
    would answer a second prompt, and stop a re-ask loop, on older code.)"""
    from test_ff_auth import _cli_on_a_pty
    repo, url = _published_release_repo(source_repo, public_remote, run)
    before = git_run("ls-remote", url, "refs/heads/main").split()[0]
    exit_code, text, _calls = _cli_on_a_pty(repo, ["publish"], answer + "\nn\n3",
                                            tmp_path / "pty-ac62.log", prompt=b"\n> ")
    assert exit_code == code, text
    assert text.count("\n> ") == 1 and "[y/N]" not in text, text
    assert text.index("Publish to ") < text.index("Choose one"), text
    after = git_run("ls-remote", url, "refs/heads/main").split()[0]
    assert (_remote_tag(url, "v1.0.0") is not None) == (outcome == "released")
    assert (after != before) == (outcome != "cancelled")


@pytest.mark.rewrite                    # --amend rewrites our own tip (INV-2)
def test_amend_without_a_release_flag_notes_no_release(source_repo, public_remote, run,
                                                       monkeypatch):
    """On a terminal with release enabled and no release flag,
    --amend (which cannot be combined with a release) publishes as
    --no-release with a one-line note, not a release-choice question."""
    repo, _url = _published_release_repo(source_repo, public_remote, run)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": pytest.fail("a choice was asked"))
    r = run(["--amend", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    notes = [line for line in r.stderr.splitlines() if "no release this run" in line]
    assert notes == ["Note: no release this run: --amend cannot be combined with a release"]
    assert "Choose one" not in r.stderr


@pytest.mark.parametrize("flag", ["--accept-source-divergence", "--recreate-branch"])
def test_flags_that_cannot_release_imply_no_release_with_a_note(source_repo, public_remote,
                                                                run, monkeypatch, capsys,
                                                                flag):
    """The other two flags: resolve_release_choice gives
    --no-release and the note, with no question asked."""
    from types import SimpleNamespace
    repo, _url = _published_release_repo(source_repo, public_remote, run)
    capsys.readouterr()
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": pytest.fail("a choice was asked"))
    config = pubrepo.load_publish_config(repo / ".publish.toml")
    args = SimpleNamespace(release_version=None, bump=None, no_release=False)
    choice, _discovery = pubrepo.resolve_release_choice(
        config, args, repo / ".publish", repo, defer=True, cannot_release=flag)
    assert choice.kind == "none"
    assert f"no release this run: {flag} cannot be combined with a release" in \
        capsys.readouterr().err


def test_release_audit_tag_push_failure_is_one_line(source_repo, public_remote, run, tmp_path):
    """The release path: an unreachable tag_remote leaves the
    release's bookmark local with the one-line notice and no raw git text;
    the release itself completes (0). AC75 re-target (§10 item 20, §18.9
    R1/R2/R6): the release bookmark goes only to a listed tag_remote."""
    import re
    import time
    repo, url = _published_release_repo(source_repo, public_remote, run)
    nowhere = str(tmp_path / "nowhere.git")
    git_run("remote", "add", "bk", nowhere, cwd=repo)
    cfg = repo / ".publish.toml"
    cfg.write_text(cfg.read_text().replace(
        "[publish]\n", f'[publish]\ntag_remote = "bk"\nprivate_remotes = ["{nowhere}"]\n', 1))
    git_run("commit", "-qam", "tag_remote = bk", cwd=repo)
    time.sleep(1.1)              # bookmarks have one-second resolution (the setup published)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    notices = [line for line in r.stderr.splitlines()
               if re.match(r"^WARNING: bookmark \d{8}T\d{6} kept locally; "
                           r"push to bk failed$", line)]
    assert len(notices) == 1, r.stderr
    assert "fatal:" not in r.stderr and "does not appear" not in r.stderr, r.stderr
    assert _remote_tag(url, "v1.0.0") is not None


# --- the release scrub (design §4.6 item 6, §8.6; AC24 release half, AC71) ---------------

def _pull_flags(run, repo, url, flags):
    """`pull --plan FLAGS`, then `pull --yes` bound to its digest and tip."""
    plan = run(["pull", "--plan", *flags], repo)
    last = plan.stdout.strip().splitlines()[-1] if plan.stdout.strip() else ""
    assert last.startswith("Plan digest: "), (plan.code, plan.stdout, plan.stderr)
    tip = git_run("ls-remote", url, "refs/heads/main").split()[0]
    r = run(["pull", "--yes", "--expect-tip", tip, "--expect-plan", last.split(": ", 1)[1],
             *flags], repo)
    assert r.code == 0, r.stdout + r.stderr


def _scrubbed_release_repo(source_repo, public_remote, run, **scrub):
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"], "release": _release("1.0.0"),
        "scrub": {"forbidden": ["zz-canary-zz"], **scrub}})
    assert run(["init"], cwd=repo).code == 0
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    return repo, url


def test_carried_forbidden_blocks_release_until_redecide_remove(source_repo, public_remote,
                                                                 run, tmp_path):
    """release::carried_forbidden_redecide_remove_then_release (AC24 release
    half): carried forbidden content is reported by publish (0) and
    blocks a release (4, the --redecide route named, nothing pushed or
    tagged); --accept-unscanned does not clear a forbidden string; after
    pull --redecide P → remove, the release ships without P."""
    repo, url = _scrubbed_release_repo(source_repo, public_remote, run)
    _collab_commit(url, tmp_path, {"notes.txt": "zz-canary-zz\n"})
    _pull_flags(run, repo, url, ["--leave", "notes.txt"])
    before = git_run("ls-remote", url, "refs/heads/main").split()[0]
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 4, r.stderr
    assert "notes.txt" in r.stderr and "pull --redecide notes.txt → remove" in r.stderr
    r = run(["--release", "1.0.0", "--yes", "--accept-unscanned", "notes.txt"], cwd=repo)
    assert r.code == 4, r.stderr
    assert git_run("ls-remote", url, "refs/heads/main").split()[0] == before
    assert _remote_tag(url, "v1.0.0") is None
    _pull_flags(run, repo, url, ["--redecide", "notes.txt", "--remove", "notes.txt"])
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    tagged = git_run("ls-tree", "-r", "--name-only", "refs/tags/v1.0.0",
                     cwd=repo / ".publish").split()
    assert "notes.txt" not in tagged and "a.txt" in tagged


def test_carried_unscannable_blocks_release_unless_accepted(source_repo, public_remote, run,
                                                           tmp_path):
    """Design §4.6 item 6: carried unscannable content blocks a
    release (4) naming both routes; --accept-unscanned PATH lets this
    release through; the flag needs a release (1)."""
    repo, url = _scrubbed_release_repo(source_repo, public_remote, run)
    c = tmp_path / "collab-bin"
    git_run("clone", "-q", url, str(c))
    (c / "blob.bin").write_bytes(b"\x00\x01binary\x00")
    git_run("add", "blob.bin", cwd=c)
    git_run("-c", "user.name=Collab", "-c", "user.email=collab@example.invalid",
            "commit", "-qm", "bin", cwd=c)
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=c)
    _pull_flags(run, repo, url, ["--leave", "blob.bin"])
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 4, r.stderr
    assert "--accept-unscanned blob.bin" in r.stderr
    assert run(["--no-release", "--yes", "--accept-unscanned", "blob.bin"], cwd=repo).code == 1
    r = run(["--release", "1.0.0", "--yes", "--accept-unscanned", "blob.bin"], cwd=repo)
    assert r.code == 0, r.stderr
    assert _remote_tag(url, "v1.0.0") is not None


def _fake_pypi_build(monkeypatch, members: dict):
    """A build backend that writes one sdist holding `members` (plus
    PKG-INFO), and a passing twine check."""
    import io
    import subprocess
    import tarfile

    def tool(argv, env=None, cwd=None):
        if argv[1:3] == ["-m", "build"]:
            out = pubrepo.Path(argv[argv.index("--outdir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            with tarfile.open(out / "demo-1.0.0.tar.gz", "w:gz") as t:
                files = {"demo-1.0.0/PKG-INFO": b"Metadata-Version: 2.1\nName: demo\n"
                                                b"Version: 1.0.0\n", **members}
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    t.addfile(info, io.BytesIO(data))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "check_live", lambda self, ctx: [])


@pytest.mark.parametrize("member, data, allow, code", [
    ("demo-1.0.0/generated.txt", b"built with zz-canary-zz\n", [], 4),
    ("demo-1.0.0/blob.so", b"\x7fELF\x00\x00binary", [], 4),
    ("demo-1.0.0/blob.so", b"\x7fELF\x00\x00binary", ["demo-1.0.0.tar.gz!*.so"], None),
])
def test_distribution_members_are_scrubbed(source_repo, public_remote, run, monkeypatch,
                                           member, data, allow, code):
    """release::archive_member_canaries (AC71; design §8.6 "every shipped
    byte"): a distribution member the export does not hold — a generated
    file with a forbidden canary, or an opaque binary — blocks the release
    with 4 before anything is pushed, tagged or uploaded; allow_opaque
    globs match members as `<dist>!<member>`. (Was: forbidden strings
    only, exit 7.)"""
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"],
        "release": _release("1.0.0", pypi={"enabled": True, "package": "demo",
                                           "repository": "pypi"}),
        "scrub": {"forbidden": ["zz-canary-zz"], "allow_opaque": allow}})
    assert run(["init"], cwd=repo).code == 0
    _fake_pypi_build(monkeypatch, {member: data, "demo-1.0.0/a.txt": b"a\n"})
    uploads = []
    monkeypatch.setattr(pubrepo.PyPIAdapter, "execute",
                        lambda self, ctx, stage: uploads.append(stage))
    monkeypatch.setattr(pubrepo.PyPIAdapter, "reconcile", lambda self, ctx: None)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    if code is None:
        assert r.code == 0, r.stderr
        return
    assert r.code == code, r.stderr
    assert "release scrub failed in the distributions" in r.stderr
    assert f"demo-1.0.0.tar.gz!{member}" in r.stderr
    assert git_run("ls-remote", url).strip() == "" and uploads == []


# --- the §8.3 summary -------------------------------------------------------------------

def _summary_world(source_repo, public_remote, run, tmp_path, monkeypatch=None,
                   **release_extra):
    """Released 1.0.0 (providers off for that run); then a collaborator adds
    a flagged workflow file, left carried by pull; then our bump to 1.0.1."""
    url = public_remote()
    files = {"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n", "CHANGELOG.md": "# 1.0.1\n- fix\n"}
    repo = source_repo(files, {"remote": url, "include": ["version.py", "a.txt", "CHANGELOG.md"],
                               "release": _release("1.0.0", **release_extra)})
    assert run(["init"], cwd=repo).code == 0
    if monkeypatch is None:
        assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 0, "first release"
    else:
        with monkeypatch.context() as m:
            m.setattr(pubrepo, "enabled_adapters", lambda _config: [])
            assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 0, "first release"
    _collab_commit(url, tmp_path, {".github/workflows/ci.yml": "on: push\n"})
    _pull_flags(run, repo, url, ["--leave", ".github/workflows/ci.yml"])
    (repo / "version.py").write_text('VERSION = "1.0.1"\n')
    git_run("commit", "-qam", "1.0.1", cwd=repo)
    return repo, url


def test_release_summary_lists_collaborators_carried_and_flags(source_repo, public_remote,
                                                              run, tmp_path):
    """release::summary (design §8.3), --yes: before the build the run
    lists collaborator commits separately from our snapshots, the carried
    paths and the accepted flags since the latest managed release."""
    repo, _url = _summary_world(source_repo, public_remote, run, tmp_path)
    r = run(["--release", "1.0.1", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert ("Public commits since v1.0.0: 0 snapshot(s) of ours, 1 collaborator commit(s)"
            in r.stderr), r.stderr
    assert "Collab: collab" in r.stderr
    assert "Carried paths (public, not packaged): 1\n    .github/workflows/ci.yml" in r.stderr
    assert "Accepted flags since v1.0.0: 1" in r.stderr
    assert ".github/workflows/ci.yml: leave (exec_build" in r.stderr


def test_release_summary_is_in_the_one_prompt_with_the_notes(source_repo, public_remote, run,
                                                            tmp_path, monkeypatch):
    """§8.3 + §17.3: on a terminal the summary, with the release notes when
    GitHub is enabled, is part of the one confirmation (declined here: 1)."""
    repo, url = _summary_world(source_repo, public_remote, run, tmp_path, monkeypatch,
                               github={"enabled": True, "repository": "owner/repo"})
    shown = []
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr(pubrepo.GitHubAdapter, "check_live", lambda self, ctx: [])
    monkeypatch.setattr(pubrepo, "confirm_yes_no", lambda summary: shown.append(summary) or False)
    before = git_run("ls-remote", url, "refs/heads/main").split()[0]
    r = run(["--release", "1.0.1"], cwd=repo)
    assert r.code == 1, r.stderr
    [summary] = shown
    assert "1 collaborator commit(s)" in summary and ".github/workflows/ci.yml" in summary
    assert "Release notes (CHANGELOG.md):\n  | # 1.0.1\n  | - fix" in summary
    assert "Publish to " in summary
    assert git_run("ls-remote", url, "refs/heads/main").split()[0] == before


# --- AC72: build files changed since the latest managed release (§8.3) ------------

def _build_world(source_repo, public_remote, run, *, release_first=True):
    url = public_remote()
    files = {"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n",
             "pyproject.toml": "[project]\nname = 'demo'\n", "run.sh": "echo hi\n"}
    repo = source_repo(files, {"remote": url,
                               "include": ["version.py", "a.txt", "pyproject.toml", "run.sh"],
                               "release": _release("1.0.0")})
    assert run(["init"], cwd=repo).code == 0
    if release_first:
        r = run(["--release", "1.0.0", "--yes", "--accept-build-changes"], cwd=repo)
        assert r.code == 0, r.stderr
    return repo, url


def _bump(repo, version, extra=None):
    (repo / "version.py").write_text(f'VERSION = "{version}"\n')
    for rel, text in (extra or {}).items():
        (repo / rel).write_text(text)
    git_run("commit", "-qam", version, cwd=repo)


def test_first_release_lists_every_build_file_and_needs_the_flag(source_repo, public_remote,
                                                                run):
    """release::build_changes_first_release (AC72): before
    the first managed release every item-3 build file in the export counts
    (version_file excepted); off a terminal → 1 naming them, nothing written."""
    repo, url = _build_world(source_repo, public_remote, run, release_first=False)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 1, r.stderr
    assert "  pyproject.toml\n  run.sh\n" in r.stderr and "--accept-build-changes" in r.stderr
    assert "version.py" not in r.stderr.split("build files changed")[1].split("Review")[0]
    assert git_run("ls-remote", url).strip() == ""
    assert not (repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0.json").exists() \
        or pubrepo.load_journal(repo / ".publish", "1.0.0").data.get("public_candidate") is None


def test_changed_build_file_needs_the_flag_off_a_terminal(source_repo, public_remote, run):
    """release::build_changes_flag (AC72): a pyproject.toml change
    since v1.0.0 → 1 off a terminal without --accept-build-changes, nothing
    pushed; with the flag the release proceeds and the summary names it."""
    repo, url = _build_world(source_repo, public_remote, run)
    _bump(repo, "1.0.1", {"pyproject.toml": "[project]\nname = 'demo'\nversion = '1'\n"})
    before = git_run("ls-remote", url, "refs/heads/main").split()[0]
    r = run(["--release", "1.0.1", "--yes"], cwd=repo)
    assert r.code == 1, r.stderr
    assert "  pyproject.toml\n" in r.stderr and "run.sh" not in r.stderr
    assert git_run("ls-remote", url, "refs/heads/main").split()[0] == before
    r = run(["--release", "1.0.1", "--yes", "--accept-build-changes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "Build files changed since v1.0.0: 1" in r.stderr
    assert _remote_tag(url, "v1.0.1") is not None


def test_version_file_only_change_needs_no_flag(source_repo, public_remote, run):
    """AC72: version_file is not counted; a release whose
    only build-relevant change is the version needs no flag."""
    repo, url = _build_world(source_repo, public_remote, run)
    _bump(repo, "1.0.1", {"a.txt": "b\n"})
    r = run(["--release", "1.0.1", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "Build files changed since v1.0.0: 0" in r.stderr


def test_status_and_prompt_count_snapshots_and_collaborator_commits_separately(
        source_repo, public_remote, run, tmp_path):
    """§8.3: after a
    release, one snapshot of ours and one collaborator commit are counted
    separately in status (text and JSON)."""
    import json
    repo, url = _released(source_repo, public_remote, run)
    (repo / "a.txt").write_text("ours\n")
    git_run("commit", "-qam", "ours", cwd=repo)
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    _collab_commit(url, tmp_path, {"notes.txt": "theirs\n"})
    r = run(["status", "--remote"], cwd=repo)                  # fetches the collaborator commit
    assert "Ahead:    1 snapshot(s) of ours, 1 collaborator commit(s)" in r.stdout, r.stdout
    data = json.loads(run(["status", "--json"], cwd=repo).stdout)
    assert (data["release"]["ahead_snapshots"], data["release"]["ahead_collaborator"]) == (1, 1)
    assert data["release"]["ahead_count"] == 2


def test_sdist_holds_managed_and_keep_paths_only(source_repo, public_remote, run, monkeypatch,
                                                 tmp_path):
    """release::sdist_projection_only (AC66, design §8.6): the
    build sees the projection-only export, so the sdist holds the managed
    paths and the keep LICENSE, never the carried notes.txt nor a source
    file outside the publish set (an unincluded dotenv file)."""
    import io
    import subprocess
    import tarfile
    from test_release_transaction import _pull_decide
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n",
                        ".env": "SYNTHETIC=1\n"}, {
        "remote": url, "include": ["version.py", "a.txt"], "keep": ["LICENSE"],
        "release": _release("1.0.0", pypi={"enabled": True, "package": "demo",
                                           "repository": "pypi"})})
    assert run(["init"], cwd=repo).code == 0
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    (repo / ".publish" / "LICENSE").write_text("MIT synthetic\n")
    (repo / "a.txt").write_text("b\n")
    git_run("commit", "-qam", "b", cwd=repo)
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    _collab_commit(url, tmp_path, {"notes.txt": "carried\n"})
    _pull_decide(run, repo, url, {"notes.txt": "leave"})

    def tool(argv, env=None, cwd=None):          # a backend that packages its input tree
        if argv[1:3] == ["-m", "build"]:
            out = pubrepo.Path(argv[argv.index("--outdir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            with tarfile.open(out / "demo-1.0.0.tar.gz", "w:gz") as t:
                info = tarfile.TarInfo("demo-1.0.0/PKG-INFO")
                meta = b"Metadata-Version: 2.1\nName: demo\nVersion: 1.0.0\n"
                info.size = len(meta)
                t.addfile(info, io.BytesIO(meta))
                for path in sorted(p for p in pubrepo.Path(cwd).rglob("*") if p.is_file()):
                    t.add(path, f"demo-1.0.0/{path.relative_to(cwd).as_posix()}")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "check_live", lambda self, ctx: [])
    monkeypatch.setattr(pubrepo.PyPIAdapter, "reconcile", lambda self, ctx: None)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "execute", lambda self, ctx, stage: None)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    sdist = repo / ".publish" / ".git" / pubrepo.RELEASE_DIR_NAME / "1.0.0" / "dist" / \
        "demo-1.0.0.tar.gz"
    with tarfile.open(sdist) as t:
        members = sorted(m.name.removeprefix("demo-1.0.0/") for m in t.getmembers())
    assert members == ["LICENSE", "PKG-INFO", "a.txt", "version.py"]
    assert git_run("show", "refs/tags/v1.0.0:notes.txt", cwd=repo / ".publish") == "carried\n"


def test_release_retire_unblocks_a_release_after_a_dropped_release(source_repo, public_remote,
                                                                   run, tmp_path):
    """release::retire_unblocks (AC60, release part; design §8.5): a
    collaborator rewrite drops the released v1.0.0 from public. After the
    owner pulls the rewrite, a release exits 7 naming restore-ancestry and
    release-retire; --no-release is never blocked; after release-retire
    v1.0.0 the next release proceeds."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 0
    dropper = tmp_path / "dropper"
    git_run("clone", "-q", url, str(dropper))
    git_run("reset", "-q", "--hard", "HEAD~1", cwd=dropper)
    (dropper / "other.txt").write_text("o\n")
    git_run("add", "-A", cwd=dropper)
    git_run("-c", "user.name=Collab", "-c", "user.email=collab@example.invalid",
            "commit", "-qm", "drop the release", cwd=dropper)
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=dropper)
    flags = ["--accept-rewritten-history", "--decline", "a.txt", "--leave", "other.txt"]
    _pull_flags(run, repo, url, flags)
    (repo / "version.py").write_text('VERSION = "1.0.1"\n')
    git_run("commit", "-qam", "1.0.1", cwd=repo)
    r = run(["--release", "1.0.1", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "release-retire" in r.stderr and "restore-ancestry" in r.stderr
    assert run(["release-retire", "v1.0.0", "--yes"], cwd=repo).code == 0
    r = run(["--release", "1.0.1", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert _remote_tag(url, "v1.0.1") is not None


def test_offline_status_release_section_makes_no_network_call(source_repo, public_remote, run,
                                                              tmp_path, git_policy):
    """Design §4.5, INV-8 (previews are offline by default): after
    a collaborator push, offline status reads the release state from the
    last fetch (labelled), shows no BROKEN, and runs no network git command
    (shim log); status --remote fetches and gives the current state."""
    from ff_fixtures import _strip_config_pairs
    repo, url = _released(source_repo, public_remote, run)
    _collab_commit(url, tmp_path, {"notes.txt": "theirs\n"})
    r = run(["status"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "BROKEN" not in r.stdout, r.stdout
    assert "(release state as of last fetch (" in r.stdout
    assert "Tags:     unknown (run with --remote)" in r.stdout
    network = [argv for argv in map(_strip_config_pairs, map(list, git_policy.runs[-1]))
               if argv[:1] in (["ls-remote"], ["fetch"], ["push"], ["clone"])]
    assert network == [], network
    r = run(["status", "--remote"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "as of last fetch" not in r.stdout
    assert "Ancestry: ok" in r.stdout
    assert "Ahead:    0 snapshot(s) of ours, 1 collaborator commit(s)" in r.stdout


def _github_off_network(monkeypatch):
    monkeypatch.setattr(pubrepo.GitHubAdapter, "check_live", lambda self, ctx: [])
    monkeypatch.setattr(pubrepo.GitHubAdapter, "reconcile", lambda self, ctx: None)
    monkeypatch.setattr(pubrepo.GitHubAdapter, "execute", lambda self, ctx, stage: None)


def test_summary_notes_are_the_exported_bytes(source_repo, public_remote, run, monkeypatch):
    """The summary shows the notes as the export holds them
    (the source the GitHub release uses). With a managed `ident` attribute
    the candidate blob says `$Id$` and the export `$Id: <blob> $`; the
    summary shows the export's."""
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n",
                        "CHANGELOG.md": "# 1.0.0 $Id$\n", ".gitattributes": "CHANGELOG.md ident\n"},
                       {"remote": url, "include": ["version.py", "a.txt", "CHANGELOG.md",
                                                   ".gitattributes"],
                        "release": _release("1.0.0", github={"enabled": True,
                                                             "repository": "owner/repo"})})
    assert run(["init"], cwd=repo).code == 0
    _github_off_network(monkeypatch)
    shown = []
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr(pubrepo, "confirm_yes_no", lambda summary: shown.append(summary) or False)
    assert run(["--release", "1.0.0"], cwd=repo).code == 1
    blob = git_run("hash-object", "CHANGELOG.md", cwd=repo).strip()
    [summary] = shown
    assert f"  | # 1.0.0 $Id: {blob} $" in summary, summary
    assert "  | # 1.0.0 $Id$" not in summary


@pytest.mark.parametrize("how", ["missing", "carried"])
def test_notes_file_outside_the_export_is_refused_before_any_push(source_repo, public_remote,
                                                                 run, monkeypatch, tmp_path,
                                                                 git_policy, how):
    """With GitHub enabled, a notes file the export lacks
    (absent, or carried public content that is not packaged) → 7 "release
    notes file CHANGELOG.md is not in the release export" before the first
    remote write: no push in that run, the remote unchanged."""
    from ff_fixtures import _strip_config_pairs
    from test_release_transaction import _pull_decide
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"],
        "release": _release("1.0.0", github={"enabled": True, "repository": "owner/repo"})})
    assert run(["init"], cwd=repo).code == 0
    assert run(["--no-release", "--yes"], cwd=repo).code == 0
    if how == "carried":
        _collab_commit(url, tmp_path, {"CHANGELOG.md": "# theirs\n"})
        _pull_decide(run, repo, url, {"CHANGELOG.md": "leave"})
    _github_off_network(monkeypatch)
    before = git_run("ls-remote", url)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "release notes file CHANGELOG.md is not in the release export" in r.stderr
    pushes = [argv for argv in map(_strip_config_pairs, map(list, git_policy.runs[-1]))
              if argv[:1] == ["push"]]
    assert pushes == [] and git_run("ls-remote", url) == before


def test_sanitizer_redacts_strip_env_extras(monkeypatch):
    """The values of the config's [publish.release] strip_env
    variables (exact or PREFIX*) are redacted like the built-in list's."""
    monkeypatch.setattr(pubrepo, "_REDACT_EXTRA", ())
    monkeypatch.setenv("MYCORP_DEPLOY_KEY", "corp-deploy-s3cret")
    monkeypatch.setenv("EXTRA_TOKEN", "extra-t0ken-value")
    text = "failed with corp-deploy-s3cret and extra-t0ken-value"
    assert pubrepo.sanitize_provider_output(text) == text           # not configured yet
    pubrepo.configure_redaction({"release": {"strip_env": ["MYCORP_*", "EXTRA_TOKEN"]}})
    out = pubrepo.sanitize_provider_output(text)
    assert "corp-deploy-s3cret" not in out and "extra-t0ken-value" not in out
    assert out.count("[redacted]") == 2


def test_cli_redacts_strip_env_values_in_a_build_failure(source_repo, public_remote, run,
                                                         monkeypatch):
    """End to end: a build backend failure that echoes a
    strip_env variable's value shows it redacted."""
    import subprocess
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"],
        "release": _release("1.0.0", strip_env=["MYCORP_*"],
                            pypi={"enabled": True, "package": "demo", "repository": "pypi"})})
    assert run(["init"], cwd=repo).code == 0
    monkeypatch.setenv("MYCORP_DEPLOY_KEY", "corp-deploy-s3cret")

    def tool(argv, env=None, cwd=None):
        if argv[1:3] == ["-m", "build"]:
            return subprocess.CompletedProcess(argv, 1, "", "backend saw corp-deploy-s3cret")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "check_live", lambda self, ctx: [])
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "package build failed" in r.stderr and "[redacted]" in r.stderr
    assert "corp-deploy-s3cret" not in r.stdout + r.stderr


@pytest.mark.parametrize("answer", ["", "3"])
def test_yes_on_a_terminal_choice_cancel_is_1(source_repo, public_remote, run, monkeypatch,
                                              answer):
    """--yes on a terminal with release enabled and
    no release flag still asks the release choice; an empty answer or
    cancel exits 1 "Release cancelled; nothing changed" (was 0), nothing
    pushed."""
    repo, url = _published_release_repo(source_repo, public_remote, run)
    before = git_run("ls-remote", url)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": answer)
    r = run(["--yes"], cwd=repo)
    assert r.code == 1, r.stderr
    assert "Release cancelled; nothing changed" in r.stderr
    assert git_run("ls-remote", url) == before


def test_release_snapshot_is_recorded(source_repo, public_remote, run):
    """records::every_snapshot_recorded, release kind (AC47 record
    half; design §8.4): the release's candidate snapshot carries `Source:`
    and gets a refs/pubrepo/snap/<sha> record in source, like a publish."""
    import json
    repo, _url = _published_release_repo(source_repo, public_remote, run)
    assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 0
    pub = repo / ".publish"
    candidate = git_run("rev-parse", "refs/tags/v1.0.0^{commit}", cwd=pub).strip()
    assert "Source: " in git_run("log", "-1", "--format=%B", candidate, cwd=pub)
    record = json.loads(git_run("cat-file", "blob",
                                f"refs/pubrepo/snap/{candidate}:record.json", cwd=repo))
    assert record["source"] == git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert "a.txt" in record["written"]


def test_retry_lap_reuses_the_build_changes_acknowledgment(source_repo, public_remote, run,
                                                           monkeypatch):
    """On a terminal the one prompt lists
    the changed build files and the user confirms; the first push loses the
    race; the --retry lap has the same managed-tree identity, so it reuses
    that acknowledgment instead of refusing for --accept-build-changes
    ("…or release on a terminal", 1, before)."""
    repo, url = _build_world(source_repo, public_remote, run, release_first=False)
    shown = []
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr(pubrepo, "confirm_yes_no", lambda summary: shown.append(summary) or True)
    real, pushes = pubrepo.push_snapshot, []

    def lose_once(publish_dir, sha, branch):
        pushes.append(sha)
        if len(pushes) == 1:
            return pubrepo.PushOutcome("rejected_nonff", "fetch first", [])
        return real(publish_dir, sha, branch)

    monkeypatch.setattr(pubrepo, "push_snapshot", lose_once)
    r = run(["--release", "1.0.0", "--retry"], cwd=repo)
    assert r.code == 0, r.stderr
    assert len(shown) == 1 and "pyproject.toml" in shown[0] and len(pushes) == 2
    assert "--accept-build-changes" not in r.stderr.split("retrying")[-1]
    assert _remote_tag(url, "v1.0.0") is not None



def test_twine_check_runs_without_credentials(source_repo, public_remote, run, monkeypatch):
    """`twine check` runs with the same
    credential-stripped environment as the build (the stripped names and strip_env
    matches absent; others kept)."""
    import subprocess
    url = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, {
        "remote": url, "include": ["version.py", "a.txt"],
        "release": _release("1.0.0", strip_env=["MYCORP_*"],
                            pypi={"enabled": True, "package": "demo", "repository": "pypi"})})
    assert run(["init"], cwd=repo).code == 0
    for name in ("TWINE_PASSWORD", "GH_TOKEN", "SSH_AUTH_SOCK", "MYCORP_DEPLOY"):
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    monkeypatch.setenv("KEEP_ME", "visible")
    seen = {}

    def tool(argv, env=None, cwd=None):
        if argv[1:3] == ["-m", "build"]:
            out = pubrepo.Path(argv[argv.index("--outdir") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "demo-1.0.0.tar.gz").write_bytes(b"not checked here")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[1:4] == ["-m", "twine", "check"]:
            seen["env"] = env
            return subprocess.CompletedProcess(argv, 1, "", "check stopped by the test")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(pubrepo, "_run_tool", tool)
    monkeypatch.setattr(pubrepo.PyPIAdapter, "check_live", lambda self, ctx: [])
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7 and "twine check failed" in r.stderr, r.stderr
    env = seen["env"]
    assert env is not None, "twine check inherited the full environment"
    assert not {"TWINE_PASSWORD", "GH_TOKEN", "SSH_AUTH_SOCK", "MYCORP_DEPLOY"} & set(env)
    assert env.get("KEEP_ME") == "visible"
