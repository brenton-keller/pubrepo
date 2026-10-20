"""Snapshot commit, push classification, fetch, confirmation,
local advance (design §4.2–§4.4, §4.10; §2.3).

Every remote is a local bare repo reached through file:// URLs.
"""
import os
import re
import subprocess

import pytest

import pubrepo
from conftest import git_run
from ff_fixtures import push_argv

PUB_NAME = "Pub Lisher"
PUB_EMAIL = "publisher@example.invalid"


@pytest.fixture
def pub(tmp_path, bare_remote, collab_clone):
    """A remote seeded with one collaborator commit, plus a `.publish`-like
    clone of it (identity set, on branch main)."""
    remote = bare_remote()
    collab = collab_clone(remote.url)
    seed = collab.commit({"README.md": "seed\n"}, "seed")
    work = tmp_path / "publish-clone"
    git_run("clone", "-q", remote.url, str(work))
    git_run("config", "user.name", PUB_NAME, cwd=work)
    git_run("config", "user.email", PUB_EMAIL, cwd=work)

    def push(refspecs, **kwargs):
        return pubrepo.push_porcelain(work, refspecs, **kwargs)

    def push_snapshot(sha, branch="main"):
        return pubrepo.push_snapshot(work, sha, branch)

    def tree(files: dict[str, str]) -> str:
        """A tree object holding `files` (path → text), written to work's ODB."""
        env = {**os.environ, "GIT_INDEX_FILE": str(tmp_path / "tree-index")}
        subprocess.run(["git", "read-tree", "--empty"], cwd=work, env=env, check=True)
        for path, text in files.items():
            blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=work, env=env,
                                  input=text, capture_output=True, text=True,
                                  check=True).stdout.strip()
            subprocess.run(["git", "update-index", "--add", "--cacheinfo",
                            f"100644,{blob},{path}"], cwd=work, env=env, check=True)
        return subprocess.run(["git", "write-tree"], cwd=work, env=env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def snapshot(files: dict[str, str], parents=None) -> str:
        parents = [seed] if parents is None else parents
        return pubrepo.snapshot_commit(work, tree(files), parents, "snap\n\nSource: abc1234\n")

    def remote_ref(ref: str = "refs/heads/main") -> str | None:
        out = git_run("ls-remote", remote.url, ref).split()
        return out[0] if out else None

    return type("Pub", (), dict(remote=remote, collab=collab, seed=seed, work=work, tree=tree,
                                push=staticmethod(push), push_snapshot=staticmethod(push_snapshot),
                                snapshot=snapshot, remote_ref=remote_ref))


def _refs(repo, *patterns) -> str:
    return git_run("for-each-ref", "--format=%(refname) %(objectname)", *patterns, cwd=repo)


# --- snapshot_commit -------------------------------------------------------------

def test_snapshot_commit_root_leaves_no_ref_and_no_worktree_change(pub):
    refs, status = _refs(pub.work), git_run("status", "--porcelain", cwd=pub.work)
    sha = pub.snapshot({"a.py": "x\n"}, parents=[])
    assert git_run("rev-list", "--parents", "-n1", sha, cwd=pub.work).split() == [sha]
    assert git_run("show", f"{sha}:a.py", cwd=pub.work) == "x\n"
    assert git_run("log", "-1", "--format=%B", sha, cwd=pub.work).strip() == \
        "snap\n\nSource: abc1234"
    assert _refs(pub.work) == refs
    assert git_run("status", "--porcelain", cwd=pub.work) == status


def test_snapshot_commit_records_every_parent_in_order(pub):
    other = pub.snapshot({"b.py": "y\n"}, parents=[])
    sha = pub.snapshot({"a.py": "x\n"}, parents=[pub.seed, other])
    assert git_run("rev-list", "--parents", "-n1", sha, cwd=pub.work).split() == \
        [sha, pub.seed, other]


def test_snapshot_commit_without_identity_names_the_fix(pub, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("EMAIL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    git_run("config", "--unset", "user.name", cwd=pub.work)
    git_run("config", "--unset", "user.email", cwd=pub.work)
    git_run("config", "user.useConfigOnly", "true", cwd=pub.work)
    with pytest.raises(pubrepo.PublishError) as exc:
        pubrepo.snapshot_commit(pub.work, pub.tree({"a.py": "x\n"}), [pub.seed], "m")
    assert exc.value.exit_code == 2
    assert "config user.email" in str(exc.value)


# --- fetch_tip / confirm_by_ancestry -----------------------------------------------

def test_fetch_tip_returns_the_remote_tip(pub):
    later = pub.collab.commit({"b.py": "later\n"})
    assert pubrepo.fetch_tip(pub.work, "main") == later
    assert git_run("rev-parse", "refs/remotes/origin/main", cwd=pub.work).strip() == later


def test_fetch_tip_follows_a_rewritten_remote(pub):
    """A force-pushed remote is seen (S7): the return value and, through the
    configured fetch refspec, refs/remotes/origin/<b>."""
    pubrepo.fetch_tip(pub.work, "main")
    root = pub.snapshot({"z.py": "z\n"}, parents=[])
    git_run("push", "-q", "origin", f"+{root}:refs/heads/main", cwd=pub.work)
    assert pubrepo.fetch_tip(pub.work, "main") == root
    assert git_run("rev-parse", "refs/remotes/origin/main", cwd=pub.work).strip() == root


def test_fetch_tip_argv_has_no_plus_refspec(pub, git_recorder):
    """AC62: routine fetches carry no `+` refspec and no force."""
    pubrepo.fetch_tip(pub.work, "main")
    fetches = [argv for argv, _ in git_recorder.calls() if argv[:1] == ["fetch"]]
    # AC75 re-target (§10 item 20, §18.13 condition 1): --write-fetch-head,
    # so T is always this fetch's result
    assert fetches == [["fetch", "--no-tags", "--write-fetch-head", "origin", "refs/heads/main"]]
    assert not any(a.startswith("+") or a in ("--force", "-f", "--prune")
                   for argv in fetches for a in argv)


def test_fetch_tip_absent_branch_is_none_and_keeps_tracking_ref(pub):
    """None only when the branch is absent; the stale tracking ref stays as
    S11 evidence (§4.9)."""
    pubrepo.fetch_tip(pub.work, "main")
    git_run("update-ref", "refs/remotes/origin/gone", pub.seed, cwd=pub.work)
    assert pubrepo.fetch_tip(pub.work, "gone") is None
    assert git_run("rev-parse", "refs/remotes/origin/gone", cwd=pub.work).strip() == pub.seed


def test_fetch_tip_transport_error_exit2(pub, tmp_path):
    """publish::fetch_tip_transport_error_exit2: PublishError, not
    GitError, so the exit code is 2."""
    git_run("remote", "set-url", "origin", (tmp_path / "missing.git").as_uri(), cwd=pub.work)
    with pytest.raises(pubrepo.PublishError) as exc:
        pubrepo.fetch_tip(pub.work, "main")
    assert exc.value.exit_code == 2
    assert "cannot reach remote" in str(exc.value)


def test_confirm_by_ancestry(pub):
    landed = pub.snapshot({"a.py": "x\n"})
    git_run("push", "-q", "origin", f"{landed}:refs/heads/main", cwd=pub.work)
    git_run("pull", "-q", "--ff-only", "origin", "main", cwd=pub.collab.path)
    pub.collab.commit({"b.py": "after\n"})              # landed is now an ancestor
    assert pubrepo.confirm_by_ancestry(pub.work, landed, "main") is True
    stray = pub.snapshot({"c.py": "never pushed\n"})
    assert pubrepo.confirm_by_ancestry(pub.work, stray, "main") is False
    assert pubrepo.confirm_by_ancestry(pub.work, landed, "absent") is False


# --- push_porcelain / push_snapshot ------------------------------------------------

def test_push_snapshot_fast_forward_ok_moves_no_local_ref(pub):
    """§4.4: the local branch advances only after confirmation (advance_local).
    git's own refs/remotes/origin/* update on a successful push is allowed."""
    refs = _refs(pub.work, "refs/heads", "refs/pubrepo", "refs/tags")
    sha = pub.snapshot({"a.py": "x\n"})
    out = pub.push_snapshot(sha)
    assert (out.status, out.per_ref) == ("ok", [("refs/heads/main", "ok")])
    assert pub.remote_ref() == sha
    assert _refs(pub.work, "refs/heads", "refs/pubrepo", "refs/tags") == refs


def test_push_snapshot_remote_moved_is_rejected_nonff(pub):
    """AC5 shape: a concurrent push between fetch and push → exit-5 class."""
    sha = pub.snapshot({"a.py": "x\n"})
    moved = pub.collab.commit({"b.py": "concurrent\n"})
    out = pub.push_snapshot(sha)
    assert out.status == "rejected_nonff"
    assert out.reason in ("fetch first", "non-fast-forward")
    assert pub.remote_ref() == moved


def test_push_snapshot_never_forces(pub, git_recorder):
    sha = pub.snapshot({"a.py": "x\n"})
    pub.push_snapshot(sha)
    pushes = [argv for argv, _ in git_recorder.calls() if argv[:1] == ["push"]]
    assert pushes == [["push", "--porcelain", "origin", f"{sha}:refs/heads/main"]]


def test_push_hook_refusal_is_host_refused_with_reason(pub):
    pub.remote.install_hook("pre-receive", "#!/bin/sh\necho no >&2\nexit 1\n")
    out = pub.push_snapshot(pub.snapshot({"a.py": "x\n"}))
    assert out.status == "host_refused"
    assert out.reason == "pre-receive hook declined"
    assert pub.remote_ref() == pub.seed


def test_deny_non_fast_forwards_is_host_refused_not_nonff(pub):
    """`[remote rejected] (non-fast-forward)` is the host's word: the status
    field decides, the reason text is never parsed."""
    git_run("config", "receive.denyNonFastForwards", "true", cwd=pub.remote.path)
    root = pub.snapshot({"a.py": "x\n"}, parents=[])
    out = pub.push([f"+{root}:refs/heads/main"])
    assert (out.status, out.reason) == ("host_refused", "non-fast-forward")
    assert pub.remote_ref() == pub.seed


def test_stale_lease_is_lease_stale(pub):
    sha = pub.snapshot({"a.py": "x\n"}, parents=[])
    out = pub.push([f"{sha}:refs/heads/main"],
                                 leases={"refs/heads/main": sha})
    assert out.status == "lease_stale"
    assert pub.remote_ref() == pub.seed


def test_lease_matching_allows_rewrite(pub):
    root = pub.snapshot({"a.py": "x\n"}, parents=[])
    out = pub.push([f"{root}:refs/heads/main"],
                                 leases={"refs/heads/main": pub.seed})
    assert out.status == "ok"
    assert pub.remote_ref() == root


def test_atomic_failure_names_cause_and_collateral(pub):
    sha = pub.snapshot({"a.py": "x\n"}, parents=[])     # non-ff on main
    out = pub.push([f"{sha}:refs/heads/side", f"{sha}:refs/heads/main"],
                                 atomic=True)
    assert out.status == "rejected_nonff"
    assert dict(out.per_ref) == {"refs/heads/side": "atomic_collateral",
                                 "refs/heads/main": "rejected_nonff"}
    assert pub.remote_ref("refs/heads/side") is None


def test_mixed_failures_report_the_moved_tip_first(pub):
    pub.remote.install_hook("update", '#!/bin/sh\n[ "$1" = refs/heads/side ] && exit 1\nexit 0\n')
    sha = pub.snapshot({"a.py": "x\n"}, parents=[])
    out = pub.push([f"{sha}:refs/heads/side", f"{sha}:refs/heads/main"])
    assert dict(out.per_ref) == {"refs/heads/side": "host_refused",
                                 "refs/heads/main": "rejected_nonff"}
    assert out.status == "rejected_nonff"


def test_new_ref_up_to_date_and_deletion_are_ok(pub):
    git_run("push", "-q", "origin", f"{pub.seed}:refs/tags/t1", cwd=pub.work)
    sha = pub.snapshot({"a.py": "x\n"})
    out = pub.push([f"{pub.seed}:refs/heads/main",
                                            f"{sha}:refs/heads/new", ":refs/tags/t1"])
    assert out.status == "ok"
    assert [s for _, s in out.per_ref] == ["ok", "ok", "ok"]
    assert pub.remote_ref("refs/tags/t1") is None


def test_unreachable_remote_outcome_unknown_exit2(pub, tmp_path):
    """When verification cannot reach origin the outcome is
    unknown: exit 2, never "did not land", nothing local moved."""
    missing = (tmp_path / "missing.git").as_uri()
    sha = pub.snapshot({"a.py": "x\n"})
    git_run("remote", "set-url", "origin", missing, cwd=pub.work)
    refs = _refs(pub.work)
    with pytest.raises(pubrepo.PublishError) as exc:
        pub.push_snapshot(sha)
    assert exc.value.exit_code == 2
    msg = str(exc.value)
    assert (f"push outcome unknown: could not verify whether {sha} reached "
            f"{(tmp_path / 'missing.git').as_uri()}; nothing was changed locally; "
            f"rerun to reconcile") in msg
    assert "did not land" not in msg
    assert _refs(pub.work) == refs


def test_outcome_unknown_redacts_credentials_in_the_remote(pub, monkeypatch, tmp_path):
    # §17.4 sandbox: the credential-shaped remote points inside the base.
    url = f"file://user:s3cret@localhost{tmp_path}/nowhere.git"
    sha = pub.snapshot({"a.py": "x\n"})
    git_run("remote", "set-url", "origin", url, cwd=pub.work)
    with pytest.raises(pubrepo.PublishError) as exc:
        pub.push_snapshot(sha)
    assert "s3cret" not in str(exc.value)
    assert "push outcome unknown" in str(exc.value)


def test_disabled_pushurl_exit2(solo, run, git_policy):
    """publish::disabled_pushurl_exit2 (design §17.1): a clone
    whose pushurl starts with DISABLED- refuses before any
    git push (2), naming the value; the remote is unchanged and the outcome
    is never "push outcome unknown"."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    git_run("config", "remote.origin.pushurl", "DISABLED-x", cwd=s.repo / ".publish")
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 2, r.stderr
    assert ("publishing is locked for this clone (remote.origin.pushurl is DISABLED-x); "
            "nothing was pushed") in r.stderr
    assert "push outcome unknown" not in r.stderr
    assert not [argv for argv in git_policy.runs[-1] if push_argv(argv)]
    assert _tip(s.url) == s.first


def test_refspec_must_be_explicit_full_ref(pub):
    with pytest.raises(ValueError):
        pub.push(["main"])
    with pytest.raises(ValueError):
        pub.push([f"{pub.seed}:main"])


# --- push seams (exit128 = the wrapper reports git exit 128) -----

def test_seam_push_ambiguous_landed_is_confirmed(pub, fault_seam):
    """AC19: the ref lands, the wrapper reports exit 128, ancestry confirms."""
    fault_seam("push.ambiguous", "exit128")
    sha = pub.snapshot({"a.py": "x\n"})
    out = pub.push_snapshot(sha)
    assert out.status == "ambiguous_landed"
    assert out.per_ref == [("refs/heads/main", "ok")]
    assert pub.remote_ref() == sha


def test_seam_push_ambiguous_absent_sends_nothing(pub, fault_seam):
    """Nothing landed → ambiguous_absent; the remote is unchanged."""
    fault_seam("push.ambiguous_absent", "exit128")
    out = pub.push_snapshot(pub.snapshot({"a.py": "x\n"}))
    assert out.status == "ambiguous_absent"
    assert pub.remote_ref() == pub.seed


def test_seam_ambiguous_applies_to_tag_refs(pub, fault_seam):
    """Repair pushes tags through the same wrapper: exact-match confirmation."""
    fault_seam("push.ambiguous", "exit128")
    out = pub.push([f"{pub.seed}:refs/tags/v9"])
    assert out.status == "ambiguous_landed"
    assert pub.remote_ref("refs/tags/v9") == pub.seed


def test_seam_publish_after_push_fires_after_the_ref_lands(pub, fault_seam):
    fault_seam("publish.after_push", "sigterm")
    sha = pub.snapshot({"a.py": "x\n"})
    got = []
    import signal
    old = signal.signal(signal.SIGTERM, lambda *a: got.append(pub.remote_ref()))
    try:
        pub.push_snapshot(sha)
    finally:
        signal.signal(signal.SIGTERM, old)
    assert got == [sha]


def test_armed_fault_query(monkeypatch):
    monkeypatch.delenv("PUBREPO_TEST_FAULT", raising=False)
    assert pubrepo._armed_fault("push.ambiguous") is None
    monkeypatch.setenv("PUBREPO_TEST_FAULT", "push.ambiguous:exit128")
    assert pubrepo._armed_fault("push.ambiguous") == "exit128"
    assert pubrepo._armed_fault("push.ambiguous_absent") is None
    monkeypatch.setenv("PUBREPO_TEST_FAULT", "push.ambiguos:exit128")
    with pytest.raises(ValueError):
        pubrepo._armed_fault("push.ambiguous")


# --- advance_local -----------------------------------------------------------------

def test_advance_local_moves_branch_and_refreshes_worktree(pub):
    sha = pub.snapshot({"a.py": "x\n", "dir/b.py": "y\n"})
    (pub.work / "stray.txt").write_text("untracked\n")
    assert pubrepo.advance_local(pub.work, "main", sha, []) == []
    assert git_run("rev-parse", "refs/heads/main", cwd=pub.work).strip() == sha
    assert git_run("rev-parse", "HEAD", cwd=pub.work).strip() == sha
    assert not (pub.work / "README.md").exists()        # not in the snapshot
    assert (pub.work / "dir" / "b.py").read_text() == "y\n"
    assert git_run("status", "--porcelain", "--untracked-files=no", cwd=pub.work) == ""


def test_advance_local_backs_up_and_names_local_only_commits(pub, capsys):
    """AC18 / §4.10: a local-only `.publish` commit is never pushed, and is
    saved to refs/pubrepo/local/<ts> and named."""
    (pub.work / "local.txt").write_text("mine\n")
    git_run("add", "local.txt", cwd=pub.work)
    git_run("commit", "-qm", "local only work", cwd=pub.work)
    local = git_run("rev-parse", "HEAD", cwd=pub.work).strip()
    sha = pub.snapshot({"a.py": "x\n"})                  # parent: the public tip
    backups = pubrepo.advance_local(pub.work, "main", sha, [])
    assert len(backups) == 1 and re.fullmatch(r"refs/pubrepo/local/\d{8}T\d{6}(-\d+)?",
                                              backups[0])
    assert git_run("rev-parse", backups[0], cwd=pub.work).strip() == local
    assert "local only work" in capsys.readouterr().err
    assert git_run("rev-parse", "refs/heads/main", cwd=pub.work).strip() == sha


def test_advance_local_backup_names_are_unique(pub):
    for n in range(2):
        (pub.work / f"l{n}.txt").write_text("mine\n")
        git_run("add", f"l{n}.txt", cwd=pub.work)
        git_run("commit", "-qm", f"local {n}", cwd=pub.work)
        git_run("update-ref", f"refs/pubrepo/local/x{n}", "HEAD", cwd=pub.work)
    first = pubrepo._backup_ref_name(pub.work)
    git_run("update-ref", first, "HEAD", cwd=pub.work)
    assert pubrepo._backup_ref_name(pub.work) != first


def test_advance_local_preserves_keep_bytes(pub):
    """AC25: keep files are owner-managed; the refresh keeps their bytes."""
    (pub.work / "LICENSE").write_text("owner edited, not yet published\n")
    os.chmod(pub.work / "LICENSE", 0o600)
    sha = pub.snapshot({"a.py": "x\n", "LICENSE": "committed licence\n"})
    pubrepo.advance_local(pub.work, "main", sha, ["LICENSE", "ABSENT"])
    assert (pub.work / "LICENSE").read_text() == "owner edited, not yet published\n"
    assert (pub.work / "LICENSE").stat().st_mode & 0o777 == 0o600
    assert not (pub.work / "ABSENT").exists()


def test_advance_local_first_publish_on_unborn_branch(tmp_path, bare_remote):
    remote = bare_remote()
    work = tmp_path / "fresh"
    git_run("init", "-q", "-b", "main", str(work))
    git_run("config", "user.name", PUB_NAME, cwd=work)
    git_run("config", "user.email", PUB_EMAIL, cwd=work)
    git_run("remote", "add", "origin", remote.url, cwd=work)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=work, input="x\n",
                          capture_output=True, text=True, check=True).stdout.strip()
    tree = subprocess.run(["git", "mktree"], cwd=work, input=f"100644 blob {blob}\ta.py\n",
                          capture_output=True, text=True, check=True).stdout.strip()
    sha = pubrepo.snapshot_commit(work, tree, [], "first")
    assert pubrepo.fetch_tip(work, "main") is None       # empty remote (S6)
    assert pubrepo.push_snapshot(work, sha, "main").status == "ok"
    assert pubrepo.advance_local(work, "main", sha, []) == []
    assert (work / "a.py").read_text() == "x\n"
    assert git_run("rev-parse", "HEAD", cwd=work).strip() == sha


# === step 2: the routine publish path, end to end (design §4) ===

import json  # noqa: E402

from conftest import config_toml  # noqa: E402


def _tip(url: str, branch: str = "main") -> str | None:
    out = git_run("ls-remote", url, f"refs/heads/{branch}").split()
    return out[0] if out else None


def _bare(url: str):
    from pathlib import Path
    return Path(url.removeprefix("file://"))


def _records(repo) -> dict[str, str]:
    out = git_run("for-each-ref", "--format=%(refname) %(objectname)", "refs/pubrepo/",
                  cwd=repo)
    return dict(line.split() for line in out.splitlines() if line)


def _record(repo, ref) -> dict:
    return json.loads(git_run("show", f"{ref}:record.json", cwd=repo))


def _commit(repo, files=None, deletes=(), message="change"):
    for rel, text in (files or {}).items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    for rel in deletes:
        git_run("rm", "-q", rel, cwd=repo)
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-q", "--allow-empty", "-m", message, cwd=repo)
    return git_run("rev-parse", "HEAD", cwd=repo).strip()


def _blob_of(url: str, rev: str, path: str) -> str:
    return git_run("rev-parse", f"{rev}:{path}", cwd=_bare(url)).strip()


def _pull_record(repo, config: dict, through: str, entries=(), files=None, message="pull"):
    """A source pull record (design §5.7), as pull writes it; built by a
    test helper ("constructed by a test helper")."""
    for rel, text in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    remote, branch = pubrepo.destination_identity(config)
    body = (f"{message}\n\nPubrepo-Pulled-Through: {through}\n"
            f"Pubrepo-Destination: {remote} {branch}\n")
    if entries:
        body += "\n```json\n" + json.dumps(list(entries)) + "\n```\n"
    git_run("add", "-A", cwd=repo)
    git_run("commit", "-q", "--allow-empty", "-m", body, cwd=repo)


@pytest.fixture
def solo(source_repo, public_remote, run, collab_clone):
    """A published solo repo: include src/ and README.md, one publish done."""
    def _make(files=None, **config_extra):
        url = public_remote()
        config = {"remote": url, "include": ["src", "README.md"], **config_extra}
        repo = source_repo(files or {"src/app.py": "print(1)\n", "README.md": "readme\n"},
                           config)
        assert run(["init"], cwd=repo).code == 0
        r = run(["publish", "--yes"], cwd=repo)
        assert r.code == 0, r.stderr
        full = {"branch": "main", **config}
        return type("Solo", (), dict(url=url, repo=repo, config=full, first=_tip(url),
                                     collab=lambda: collab_clone(url)))
    return _make


def test_first_publish_is_a_root_commit_with_a_record(solo):
    """AC7: root commit; the record is a parentless commit object with the
    design §4.3 keys; nothing under refs/pubrepo reaches the public remote."""
    s = solo()
    assert git_run("rev-list", "--parents", "-n1", s.first, cwd=_bare(s.url)).split() == [s.first]
    ref = f"refs/pubrepo/snap/{s.first}"
    assert ref in _records(s.repo)
    rec = _record(s.repo, ref)
    assert rec["source"] == git_run("rev-parse", "HEAD", cwd=s.repo).strip()
    assert set(rec["written"]) == {"src/app.py", "README.md"}
    assert rec["config_path"] == ".publish.toml"
    assert "refs/pubrepo" not in git_run("ls-remote", s.url)
    log = (s.repo / ".publish" / ".git" / "pubrepo-log.jsonl").read_text().splitlines()
    assert json.loads(log[-1])["public_commit"] == s.first


def test_republish_without_changes_is_nothing_to_publish(solo, run):
    s = solo()
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0 and "Nothing to publish" in r.stdout + r.stderr
    assert _tip(s.url) == s.first


def test_every_routine_publish_is_a_fast_forward(solo, run):
    """INV-1 / AC2 shape: a pre-receive hook refusing any non-fast-forward
    never trips across edits, deletions and a managed-set change."""
    s = solo()
    hook = _bare(s.url) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n"
                    "  [ \"$old\" = 0000000000000000000000000000000000000000 ] && continue\n"
                    "  git merge-base --is-ancestor \"$old\" \"$new\" || exit 1\n"
                    "done\n")
    hook.chmod(0o755)
    _commit(s.repo, {"src/app.py": "print(2)\n", "src/new.py": "n\n"})
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    _commit(s.repo, deletes=["src/new.py"])
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    (s.repo / ".publish.toml").write_text(config_toml({**s.config, "include": ["src"]}))
    _commit(s.repo)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "README.md (removed from the managed set)" in r.stdout + r.stderr   # AC4, named
    assert git_run("rev-list", "--count", _tip(s.url), cwd=_bare(s.url)).strip() == "4"


def test_concurrent_push_exit5_local_ref_unchanged(solo, run, monkeypatch):
    """AC5: a push lands between our fetch and our push → exit 5, nothing local moves."""
    s = solo()
    collab = s.collab()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    local = git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip()
    real = pubrepo.push_snapshot

    def racing(*args, **kwargs):
        collab.commit({"other.txt": "racer\n"}, "concurrent")
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "push_snapshot", racing)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert "run `pubrepo pull`" in r.stderr
    assert git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip() == local
    assert f"refs/pubrepo/snap/" not in "".join(set(_records(s.repo)) - {
        f"refs/pubrepo/snap/{s.first}"})


def test_collaborator_commit_right_after_push_is_recorded_as_pushed(solo, run):
    """AC6: a post-receive hook adds a commit on top of ours → exit 0, our
    snapshot is recorded; the next publish sees the range (S2 → 5)."""
    s = solo()
    hook = _bare(s.url) / "hooks" / "post-receive"
    hook.write_text("#!/bin/sh\nread old new ref\n"
                    "[ -f hook-ran ] && exit 0\n: > hook-ran\n"
                    "c=$(GIT_AUTHOR_NAME=C GIT_AUTHOR_EMAIL=c@example.invalid "
                    "GIT_COMMITTER_NAME=C GIT_COMMITTER_EMAIL=c@example.invalid "
                    "git commit-tree \"$new^{tree}\" -p \"$new\" -m after)\n"
                    "git update-ref \"$ref\" \"$c\" \"$new\"\n")
    hook.chmod(0o755)
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    ours = git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip()
    assert f"refs/pubrepo/snap/{ours}" in _records(s.repo)
    assert git_run("rev-parse", f"{_tip(s.url)}^", cwd=_bare(s.url)).strip() == ours
    assert run(["publish", "--yes"], cwd=s.repo).code == 5


def test_missing_branch_with_evidence_is_s11_then_recreate_is_root(solo, run):
    """AC17 / §4.9: S11 exit 5; --recreate-branch makes a root commit."""
    s = solo()
    git_run("update-ref", "-d", "refs/heads/main", cwd=_bare(s.url))
    _commit(s.repo, {"src/app.py": "print('after delete')\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert "--recreate-branch" in r.stderr
    assert _tip(s.url) is None
    r = run(["--recreate-branch", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    tip = _tip(s.url)
    assert git_run("rev-list", "--parents", "-n1", tip, cwd=_bare(s.url)).split() == [tip]


def test_s11_evidence_from_local_history_alone(solo, run):
    """Tracking ref pruned, log and records gone; the ours-snapshot in
    .publish's own history is still evidence."""
    s = solo()
    publish = s.repo / ".publish"
    git_run("update-ref", "-d", "refs/heads/main", cwd=_bare(s.url))
    git_run("update-ref", "-d", "refs/remotes/origin/main", cwd=publish)
    (publish / ".git" / "pubrepo-log.jsonl").unlink()
    for ref in _records(s.repo):
        git_run("update-ref", "-d", ref, cwd=s.repo)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert f"history holds snapshot {s.first[:12]}" in r.stderr


def test_local_only_publish_commit_is_never_pushed(solo, run):
    """AC18 / §4.10: commit-tree -p <tip> excludes it; it is backed up and named."""
    s = solo()
    publish = s.repo / ".publish"
    (publish / "local-note.txt").write_text("mine\n")
    git_run("add", "local-note.txt", cwd=publish)
    git_run("commit", "-qm", "local only", cwd=publish)
    local = git_run("rev-parse", "HEAD", cwd=publish).strip()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "local only" in r.stderr
    assert subprocess.run(["git", "merge-base", "--is-ancestor", local, _tip(s.url)],
                          cwd=publish).returncode != 0
    backups = git_run("for-each-ref", "--format=%(objectname)", "refs/pubrepo/local/",
                      cwd=publish).split()
    assert backups == [local]


def test_push_ambiguous_seams(solo, run, fault_seam):
    """AC19: landed → recorded as pushed; absent → exit 2, local ref unchanged."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    fault_seam("push.ambiguous_absent", "exit128")
    local = git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip()
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 2, r.stderr
    assert _tip(s.url) == s.first
    assert git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip() == local
    fault_seam("push.ambiguous", "exit128")
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert f"refs/pubrepo/snap/{_tip(s.url)}" in _records(s.repo)


def test_host_refusal_exit2_names_reason(solo, run):
    s = solo()
    hook = _bare(s.url) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'GH006: Protected branch update failed' >&2\nexit 1\n")
    hook.chmod(0o755)
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    records = _records(s.repo)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 2
    assert "host refused the push: pre-receive hook declined" in r.stderr
    assert _records(s.repo) == records


def test_first_publish_names_the_hosts_other_default_branch(source_repo, public_remote, run,
                                                           collab_clone):
    """AC74: the remote has 'trunk' as its default; publishing to main names it."""
    url = public_remote()
    git_run("symbolic-ref", "HEAD", "refs/heads/trunk", cwd=_bare(url))
    collab = collab_clone(url, branch="trunk")
    collab.commit({"host.txt": "initial\n"}, "host initial commit")
    repo = source_repo({"src/app.py": "x\n"}, {"remote": url, "include": ["src"]})
    assert run(["init"], cwd=repo).code == 0
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "default branch is 'trunk'" in r.stdout + r.stderr
    tip = _tip(url)
    assert git_run("rev-list", "--parents", "-n1", tip, cwd=_bare(url)).split() == [tip]


def test_keep_file_published_preserved_and_scanned(solo, run):
    """AC25: keep content is published, its bytes survive the refresh, and a
    forbidden string in it blocks (exit 4)."""
    s = solo(keep=["LICENSE"], scrub={"forbidden": ["sekrit"]})
    publish = s.repo / ".publish"
    (publish / "LICENSE").write_text("licence v1\n")
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert git_run("show", f"{_tip(s.url)}:LICENSE", cwd=_bare(s.url)) == "licence v1\n"
    assert (publish / "LICENSE").read_text() == "licence v1\n"
    (publish / "LICENSE").write_text("contains sekrit\n")
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 4
    assert "LICENSE" in r.stderr


def test_carried_forbidden_reported_then_removed_by_decision(solo, run):
    """AC24 publish half: carried content never blocks publish but is
    reported with the --redecide route; a `remove` decision deletes it, named."""
    s = solo(scrub={"forbidden": ["sekrit"]})
    f = s.collab().commit({"notes.txt": "has sekrit\n"}, "collab notes")
    _pull_record(s.repo, s.config, f)
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "already public, not written by pubrepo" in r.stderr
    assert "pubrepo pull --redecide notes.txt" in r.stderr
    tip = _tip(s.url)
    _pull_record(s.repo, s.config, tip, entries=[{
        "path": "notes.txt", "decision": "remove", "cls": "carried", "flags": [],
        "theirs": _blob_of(s.url, tip, "notes.txt"), "result": None}])
    record = git_run("rev-parse", "HEAD", cwd=s.repo).strip()
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "notes.txt (removed by decision" in r.stdout + r.stderr
    assert "notes.txt" not in git_run("ls-tree", "--name-only", _tip(s.url), cwd=_bare(s.url))
    # Design §3.1: the decided deletion of an unmanaged
    # path is not a pubrepo write; the record keeps it under `decided`.
    rec = _record(s.repo, f"refs/pubrepo/snap/{_tip(s.url)}")
    assert "notes.txt" not in rec["written"]
    assert rec["decided"] == {"notes.txt": {"decision": "remove", "blob": None, "record": record}}


def test_leave_decision_survives_an_unrelated_later_pull(solo, run):
    """`leave` in pull 1, an unrelated pull 2, publish → still carried, no S9."""
    s = solo(files={"src/app.py": "x\n", "README.md": "r\n", "docs/x.md": "ours\n"},
             include=["src", "README.md", "docs"])
    f1 = s.collab().commit({"docs/x.md": "collab edit\n"}, "collab edit")
    theirs = _blob_of(s.url, f1, "docs/x.md")
    (s.repo / ".publish.toml").write_text(config_toml({**s.config, "include": ["src", "README.md"]}))
    _pull_record(s.repo, s.config, f1, entries=[{
        "path": "docs/x.md", "decision": "leave", "cls": "C8b", "flags": [],
        "theirs": theirs, "result": None, "managed_at_decision": False}])
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    f2 = s.collab().commit({"unrelated.txt": "u\n"}, "unrelated")
    _pull_record(s.repo, s.config, f2)
    _commit(s.repo, {"src/app.py": "y\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert git_run("show", f"{_tip(s.url)}:docs/x.md", cwd=_bare(s.url)) == "collab edit\n"


def test_collaborator_edit_we_did_not_touch_survives(solo, run):
    """After a pull that took theirs, a later publish of other paths
    keeps the collaborator's in-manifest edit byte for byte."""
    s = solo()
    f = s.collab().commit({"README.md": "collab readme\n"}, "collab readme")
    _pull_record(s.repo, s.config, f, files={"README.md": "collab readme\n"}, entries=[{
        "path": "README.md", "decision": "accept", "cls": "theirs_changed", "flags": [],
        "theirs": _blob_of(s.url, f, "README.md"), "result": None}])
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    tip = _tip(s.url)
    assert git_run("rev-parse", f"{tip}^", cwd=_bare(s.url)).strip() == f
    assert git_run("show", f"{tip}:README.md", cwd=_bare(s.url)) == "collab readme\n"


def test_reinit_over_a_foreign_commit_is_5_and_deletes_nothing(solo, run):
    """A fresh .publish over a public range we never pulled."""
    import shutil
    s = solo()
    f = s.collab().commit({"collab.txt": "theirs\n"}, "collab file")
    shutil.rmtree(s.repo / ".publish")
    assert run(["init"], cwd=s.repo).code == 0
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert f[:7] in r.stderr
    assert _tip(s.url) == f


def test_dirty_publish_warns_and_records_nonderivable(solo, run):
    """The dirty-path record when the source has untracked included files."""
    s = solo()
    (s.repo / "src" / "app.py").write_text("print('dirty')\n")
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "publishing uncommitted changes" in r.stderr
    rec = _record(s.repo, f"refs/pubrepo/snap/{_tip(s.url)}")
    assert rec["nonderivable"] == ["src/app.py"]


def test_s9_is_8_and_names_the_pull_command(solo, run):
    """A managed path a collaborator changed, pulled through with no decision
    recorded for it → S9, exit 8."""
    s = solo()
    f = s.collab().commit({"src/app.py": "print('collab')\n"}, "collab edit")
    _pull_record(s.repo, s.config, f)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 8, r.stderr
    assert "src/app.py" in r.stderr and "pubrepo pull" in r.stderr
    assert _tip(s.url) == f


def test_s10a_source_behind_is_5(solo, run):
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    git_run("reset", "-q", "--hard", "HEAD~1", cwd=s.repo)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert "behind" in r.stderr


def test_s10a_published_from_a_branch_ahead_is_5(solo, run):
    """Main strictly behind feat's σ → S10a, exit 5, never
    an accept route. Naming the branch needs S3's DivergenceInfo.branches."""
    s = solo()
    git_run("checkout", "-q", "-b", "feat", cwd=s.repo)
    _commit(s.repo, {"src/feature.py": "f\n"}, message="feature")
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    snap = _tip(s.url)
    git_run("checkout", "-q", "main", cwd=s.repo)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert "--accept-source-divergence" not in r.stderr
    assert _tip(s.url) == snap


def _publish_from_feature(s, run):
    """A snapshot published from `feat`, then main diverges from it (S10b by
    the §4.8 table: σ resolves, neither side contains the other)."""
    git_run("checkout", "-q", "-b", "feat", cwd=s.repo)
    _commit(s.repo, {"src/feature.py": "f\n"}, message="feature")
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    git_run("checkout", "-q", "main", cwd=s.repo)
    _commit(s.repo, {"src/app.py": "print('main moved on')\n"}, message="main work")
    return _tip(s.url)


def test_s10b_off_a_tty_is_8_with_the_flag_to_paste(solo, run):
    s = solo()
    snap = _publish_from_feature(s, run)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 8, r.stderr
    assert "feat" in r.stderr
    assert f"--accept-source-divergence={snap}" in r.stderr
    r = run([f"--accept-source-divergence={snap}", "--yes"], cwd=s.repo)   # R2
    assert r.code == 0, r.stderr
    assert run(["publish", "--yes"], cwd=s.repo).code == 0


@pytest.mark.parametrize("answer, code", [("y", 0), ("n", 1)])
def test_s10b_on_a_tty_confirms_through_confirm_then_lock(solo, run, monkeypatch, answer, code):
    """§17.3: on a terminal the one [y/N] carries the S10b acceptance
    and names the changing paths; y publishes over the snapshot, anything
    else cancels with nothing written."""
    import io
    s = solo()
    snap = _publish_from_feature(s, run)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("sys.stdin", io.StringIO(answer + "\n"))
    locks = []
    real_ctl = pubrepo.confirm_then_lock
    monkeypatch.setattr(pubrepo, "confirm_then_lock",
                        lambda *a, **k: locks.append(1) or real_ctl(*a, **k))
    r = run(["publish"], cwd=s.repo)
    assert r.code == code, r.stderr
    assert locks == [1] and r.stderr.count("Publish? [y/N]") == 1
    assert snap[:12] in r.stderr and "src/feature.py" in r.stderr
    if code == 0:
        assert git_run("rev-parse", f"{_tip(s.url)}^", cwd=_bare(s.url)).strip() == snap
    else:
        assert _tip(s.url) == snap


def test_accepted_unreachable_snapshot_is_dropped_once(solo, run):
    """Our snapshot is rewound off public (S7); accepting it publishes
    and writes its `dropped` marker, so the next publish is 0, not S7."""
    s = solo()
    snap = _publish_from_feature(s, run)
    git_run("update-ref", "refs/heads/main", s.first, snap, cwd=_bare(s.url))
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5 and "no longer on public" in r.stderr
    _commit(s.repo, {"src/app.py": "print(3)\n"})
    r = run([f"--accept-source-divergence={snap}", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert f"refs/pubrepo/status/{snap}/dropped" in _records(s.repo)
    assert run(["publish", "--yes"], cwd=s.repo).code == 0


def test_accept_and_recreate_are_refused_on_previews(solo, run):
    s = solo()
    for preview in ("--stage", "--dry-run", "--diff"):
        r = run([preview, "--recreate-branch"], cwd=s.repo)
        assert r.code == 1 and "previews do not publish" in r.stderr



# --- build_snapshot_tree on the real integration state (§2.3) ---

def _real(s, *, accept=None, preview=False):
    """Fetch, project and compute_integration exactly as publish does, then
    hand back what build_snapshot_tree needs."""
    pub = s.repo / ".publish"
    cfg = pubrepo.load_publish_config(s.repo / ".publish.toml")
    tip = pubrepo.fetch_tip(pub, "main")
    proj = pubrepo.build_projection(s.repo, pub, cfg)
    with pubrepo.source_lock(s.repo, exclusive=False):
        integ = pubrepo.compute_integration(s.repo, pub, cfg, tip, proj,
                                            accept_divergence=accept, fetch_sigma=False,
                                            config_relpath=".publish.toml")
    return pub, cfg, tip, proj, integ


def _tree_files(pub, tree) -> dict[str, str]:
    names = git_run("ls-tree", "-r", "--name-only", tree, cwd=pub).split("\n")
    return {n: git_run("cat-file", "-p", f"{tree}:{n}", cwd=pub) for n in names if n}


def _push_commit(s, files=None, deletes=(), message="collab", parents=None, extra_index=()):
    """A commit built with plumbing in a collaborator clone and pushed as a
    fast-forward: plain files, deletions, and raw index entries (symlinks,
    gitlinks) on top of the current tip."""
    c = s.collab()
    work = c.path
    for rel, text in (files or {}).items():
        (work / rel).parent.mkdir(parents=True, exist_ok=True)
        (work / rel).write_text(text)
        git_run("add", rel, cwd=work)
    for rel in deletes:
        git_run("rm", "-q", rel, cwd=work)
    for mode, oid, rel in extra_index:
        git_run("update-index", "--add", "--cacheinfo", f"{mode},{oid},{rel}", cwd=work)
    git_run("commit", "-q", "-m", message, cwd=work)
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    return git_run("rev-parse", "HEAD", cwd=work).strip()


def test_tree_overlays_projection_and_carries_the_rest(solo, run):
    """INV-3 (a), (c), (d) on a real S1 state after a pull."""
    s = solo(files={"src/app.py": "v1\n", "src/old.py": "o\n", "README.md": "r\n"})
    f = _push_commit(s, {"collab.txt": "theirs\n"})
    _pull_record(s.repo, s.config, f)
    _commit(s.repo, {"src/app.py": "v2\n", "src/new.py": "n\n"}, deletes=["src/old.py"])
    pub, cfg, tip, proj, integ = _real(s)
    assert integ.state == "S1" and tip == integ.ip == f
    index = (pub / ".git" / "index").read_bytes()
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert _tree_files(pub, plan.tree) == {"README.md": "r\n", "collab.txt": "theirs\n",
                                           "src/app.py": "v2\n", "src/new.py": "n\n"}
    assert (plan.adds, plan.mods, plan.dels) == ([("src/new.py", "")], [("src/app.py", "")],
                                                [("src/old.py", "")])
    assert plan.written["src/old.py"] == "deleted" and plan.carried_count == 1
    assert (pub / ".git" / "index").read_bytes() == index
    assert not list((pub / ".git").glob(f"{pubrepo.TEMP_INDEX_PREFIX}*"))


def test_unchanged_projection_gives_the_tip_tree(solo):
    s = solo()
    pub, cfg, tip, proj, integ = _real(s)
    assert integ.state == "S0"
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert plan.tree == git_run("rev-parse", f"{tip}^{{tree}}", cwd=pub).strip()
    assert plan.written == {}


def test_first_publish_builds_from_the_empty_tree(source_repo, public_remote, run):
    url = public_remote()
    repo = source_repo({"src/a.py": "x\n"}, {"remote": url, "include": ["src"]})
    assert run(["init"], cwd=repo).code == 0
    s = type("S", (), dict(repo=repo))
    pub, cfg, tip, proj, integ = _real(s)
    assert tip is None and integ.state == "S6"
    plan = pubrepo.build_snapshot_tree(pub, None, proj, integ, cfg)
    assert _tree_files(pub, plan.tree) == {"src/a.py": "x\n"}
    assert plan.adds == [("src/a.py", "")]


def test_mode_change_is_a_modification(solo):
    s = solo()
    os.chmod(s.repo / "src" / "app.py", 0o755)
    _commit(s.repo)
    pub, cfg, tip, proj, integ = _real(s)
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert plan.mods == [("src/app.py", "")]
    assert git_run("ls-tree", plan.tree, "src/app.py", cwd=pub).startswith("100755")


def test_written_path_dropped_from_include_is_removed_and_named(solo):
    """INV-3 (b), C8: tip == LW → deleted, "removed from the managed set"."""
    s = solo()
    (s.repo / ".publish.toml").write_text(config_toml({**s.config, "include": ["src"]}))
    _commit(s.repo)
    pub, cfg, tip, proj, integ = _real(s)
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert plan.dels == [("README.md", "removed from the managed set")]


def test_changed_written_path_is_s9_asserted_in_publish_listed_in_preview(solo):
    """C8b: a collaborator changed a path we wrote, then it left the managed
    set with no decision → S9; publish mode asserts, preview lists it."""
    s = solo(files={"src/app.py": "x\n", "README.md": "r\n", "docs/x.md": "ours\n"},
             include=["src", "README.md", "docs"])
    f = _push_commit(s, {"docs/x.md": "collab\n"})
    (s.repo / ".publish.toml").write_text(config_toml({**s.config,
                                                       "include": ["src", "README.md"]}))
    _pull_record(s.repo, s.config, f)
    pub, cfg, tip, proj, integ = _real(s)
    assert integ.state == "S9" and [i.cls for i in integ.items] == ["C8b"]
    with pytest.raises(AssertionError, match="S9"):
        pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg, mode="preview")
    assert plan.items == ["docs/x.md"]
    assert _tree_files(pub, plan.tree)["docs/x.md"] == "collab\n"


def test_written_path_follows_delete_decision(solo):
    """INV-3 (b): an applicable `delete` removes it, named by the record."""
    s = solo(files={"src/app.py": "x\n", "README.md": "r\n", "docs/x.md": "ours\n"},
             include=["src", "README.md", "docs"])
    f = _push_commit(s, {"docs/x.md": "collab\n"})
    (s.repo / ".publish.toml").write_text(config_toml({**s.config,
                                                       "include": ["src", "README.md"]}))
    _pull_record(s.repo, s.config, f, entries=[{
        "path": "docs/x.md", "decision": "delete", "cls": "C8b", "flags": [],
        "theirs": _blob_of(s.url, f, "docs/x.md"), "result": None,
        "managed_at_decision": False}])
    record = git_run("rev-parse", "HEAD", cwd=s.repo).strip()
    pub, cfg, tip, proj, integ = _real(s)
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert plan.dels == [("docs/x.md", f"removed by decision {record[:12]}")]


def test_remove_applies_only_when_theirs_matches_the_tip(solo):
    """INV-3 (c), keying: a `remove` recorded for older content of a
    carried path does not apply once the path changed again."""
    s = solo()
    f1 = _push_commit(s, {"notes.txt": "v1\n"})
    _pull_record(s.repo, s.config, f1, entries=[{
        "path": "notes.txt", "decision": "remove", "cls": "carried", "flags": [],
        "theirs": _blob_of(s.url, f1, "notes.txt"), "result": None}])
    f2 = _push_commit(s, {"notes.txt": "v2\n"})
    _pull_record(s.repo, s.config, f2)
    pub, cfg, tip, proj, integ = _real(s)
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert plan.dels == [] and _tree_files(pub, plan.tree)["notes.txt"] == "v2\n"


def test_unmanaged_baseline_items_follow_leave_remove_restore(solo):
    """On a real trailer-only snapshot: its unverified delta adds
    extra.txt and kept.txt and rewrites the carried old.txt. Decided by
    pull: remove deletes, restore puts back the parent blob (named), leave
    carries. The decided changes are not pubrepo writes (design §3.1):
     they are in plan.decided, never plan.written. The
    trailer-only deletion case is the CLI test below (AC14b2)."""
    s = solo()
    f = _push_commit(s, {"old.txt": "carried\n"})
    _pull_record(s.repo, s.config, f)
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    sigma = git_run("rev-parse", "--short", "HEAD", cwd=s.repo).strip()
    t = _push_commit(s, {"extra.txt": "added\n", "kept.txt": "k\n", "old.txt": "rewritten\n"},
                     message=f"fixup\n\nSource: {sigma}")
    _, cfg, tip, proj, integ = _real(s)
    assert git_run("rev-parse", f"{t}^", cwd=s.repo / ".publish").strip() == f
    assert {i.path for i in integ.baseline_items} >= {"extra.txt", "kept.txt", "old.txt"}
    old_blob = _blob_of(s.url, f, "old.txt")
    _pull_record(s.repo, s.config, t, entries=[
        {"path": "extra.txt", "decision": "remove", "cls": "baseline_unmanaged", "flags": [],
         "theirs": _blob_of(s.url, t, "extra.txt"), "result": None},
        {"path": "kept.txt", "decision": "leave", "cls": "baseline_unmanaged", "flags": [],
         "theirs": _blob_of(s.url, t, "kept.txt"), "result": None},
        {"path": "old.txt", "decision": "restore", "cls": "baseline_unmanaged", "flags": [],
         "theirs": _blob_of(s.url, t, "old.txt"), "result": old_blob}])
    record = git_run("rev-parse", "HEAD", cwd=s.repo).strip()[:12]
    pub, cfg, tip, proj, integ = _real(s)
    assert integ.items == [] and tip == integ.ip == t
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert ("extra.txt", f"removed by decision {record}") in plan.dels
    assert ("old.txt", f"restored by decision {record}") in plan.mods
    files = _tree_files(pub, plan.tree)
    assert files["kept.txt"] == "k\n" and files["old.txt"] == "carried\n"
    assert "extra.txt" not in files
    full = git_run("rev-parse", "HEAD", cwd=s.repo).strip()
    assert plan.decided == {
        "extra.txt": {"decision": "remove", "blob": None, "record": full},
        "old.txt": {"decision": "restore", "blob": old_blob, "record": full}}
    assert not set(plan.decided) & set(plan.written)


def _delete_then_restore(s, run, published_tree):
    """AC14b2 up to the restore: a trailer-only snapshot deletes the carried,
    unmanaged notes.txt → publish 8; a `restore` decision → publish 0 with
    the file back, recorded under `decided`, not `written`. Returns
    (blob, pull record sha, restored tip)."""
    f = _push_commit(s, {"notes.txt": "theirs\n"})
    _pull_record(s.repo, s.config, f)
    sigma = git_run("rev-parse", "--short", "HEAD", cwd=s.repo).strip()
    t = _push_commit(s, deletes=["notes.txt"], message=f"tidy\n\nSource: {sigma}")
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 8, r.stderr
    assert "notes.txt" in r.stdout + r.stderr
    assert _tip(s.url) == t
    blob = _blob_of(s.url, f, "notes.txt")
    _pull_record(s.repo, s.config, t, entries=[{
        "path": "notes.txt", "decision": "restore", "cls": "baseline_unmanaged", "flags": [],
        "theirs": None, "result": blob}])
    record = git_run("rev-parse", "HEAD", cwd=s.repo).strip()
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert f"notes.txt (restored by decision {record[:12]})" in r.stdout + r.stderr
    restored = _tip(s.url)
    assert restored != t and published_tree(s.url)["notes.txt"] == b"theirs\n"
    rec = _record(s.repo, f"refs/pubrepo/snap/{restored}")
    assert "notes.txt" not in rec["written"]
    assert rec["decided"] == {"notes.txt": {"decision": "restore", "blob": blob,
                                            "record": record}}
    return blob, record, restored


def test_trailer_only_unmanaged_delete_restore_survives_the_next_publish(solo, run,
                                                                          published_tree):
    """publish::trailer_only_unmanaged_delete_restore (AC14b2;
    design §3.1). After the restore, an unrelated source edit
    publishes 0 and notes.txt is still there, byte for byte: the restore
    made it no pubrepo write, so it stays class (c)."""
    s = solo()
    _, _, restored = _delete_then_restore(s, run, published_tree)
    _commit(s.repo, {"src/app.py": "print(3)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "notes.txt" not in r.stdout + r.stderr
    assert _tip(s.url) != restored
    tree = published_tree(s.url)
    assert tree["notes.txt"] == b"theirs\n" and tree["src/app.py"] == b"print(3)\n"
    assert "decided" not in _record(s.repo, f"refs/pubrepo/snap/{_tip(s.url)}")


@pytest.mark.rewrite
def test_amending_a_restore_snapshot_keeps_the_restore_decided(solo, run, published_tree):
    """--amend half: the amended record's write set is the
    delta against the parent, so the restore the replaced tip made must be
    carried over under `decided`, not counted as written; the next publish
    then keeps notes.txt."""
    s = solo()
    blob, record, restored = _delete_then_restore(s, run, published_tree)
    _commit(s.repo, {"src/app.py": "print(3)\n"})
    r = run(["--amend", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    amended = _tip(s.url)
    assert amended != restored
    rec = _record(s.repo, f"refs/pubrepo/snap/{amended}")
    assert "notes.txt" not in rec["written"] and "src/app.py" in rec["written"]
    assert rec["decided"] == {"notes.txt": {"decision": "restore", "blob": blob,
                                            "record": record}}
    _commit(s.repo, {"README.md": "readme 2\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "notes.txt" not in r.stdout + r.stderr
    assert published_tree(s.url)["notes.txt"] == b"theirs\n"


def test_public_symlinks_and_gitlinks_are_carried(solo):
    """§4.6 item 7: never projected, carried byte for byte, even under an
    include that matches their names."""
    s = solo(include=["."])
    c = s.collab()
    target = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=c.path,
                            input="src/app.py", capture_output=True, text=True,
                            check=True).stdout.strip()
    git_run("update-index", "--add", "--cacheinfo", f"120000,{target},link", cwd=c.path)
    git_run("update-index", "--add", "--cacheinfo", f"160000,{s.first},vendor", cwd=c.path)
    git_run("commit", "-q", "-m", "links", cwd=c.path)
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=c.path)
    f = git_run("rev-parse", "HEAD", cwd=c.path).strip()
    _pull_record(s.repo, s.config, f)
    pub, cfg, tip, proj, integ = _real(s)
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert plan.tree == git_run("rev-parse", f"{f}^{{tree}}", cwd=pub).strip()
    assert plan.carried_count == 2


def test_preview_with_an_unpulled_range_builds_on_the_ip(solo):
    """Tip != IP → base is the IP tree; range items stay at base."""
    s = solo()
    _push_commit(s, {"README.md": "collab\n"})
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    pub, cfg, tip, proj, integ = _real(s)
    assert integ.state == "S3" and integ.ip == s.first
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg, mode="preview")
    assert plan.items == ["README.md"]
    files = _tree_files(pub, plan.tree)
    assert files["README.md"] == "readme\n" and files["src/app.py"] == "print(2)\n"
    with pytest.raises(AssertionError, match="tip == IP"):
        pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)


def test_nonderivable_and_keep_lists(solo):
    """Written dirty paths are nonderivable; keep listed."""
    s = solo(keep=["LICENSE"])
    (s.repo / ".publish" / "LICENSE").write_text("terms\n")
    (s.repo / "src" / "app.py").write_text("print('dirty')\n")
    pub, cfg, tip, proj, integ = _real(s)
    plan = pubrepo.build_snapshot_tree(pub, tip, proj, integ, cfg)
    assert plan.nonderivable == ["src/app.py"]
    assert plan.keep == ["LICENSE"]


@pytest.mark.parametrize("terminal", [False, True])
def test_accepted_unreachable_with_nothing_to_publish_is_dropped(solo, run, monkeypatch,
                                                                 terminal):
    """Design §4.8: accepting an unreachable snapshot
    when the source matches the public tip publishes nothing but still
    writes its `dropped` marker, so the next publish is not S7 again."""
    import io
    s = solo()
    snap = _publish_from_feature(s, run)
    git_run("update-ref", "refs/heads/main", s.first, snap, cwd=_bare(s.url))
    _commit(s.repo, {"src/app.py": "print(1)\n"})          # source back to the first snapshot
    argv = [f"--accept-source-divergence={snap}"]
    if terminal:
        monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
        monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    else:
        argv.append("--yes")
    r = run(argv, cwd=s.repo)
    assert r.code == 0, r.stderr
    assert "Nothing to publish" in r.stdout + r.stderr
    assert f"refs/pubrepo/status/{snap}/dropped" in _records(s.repo)
    assert _tip(s.url) == s.first
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr


def test_decided_changes_to_managed_paths_name_their_decision(solo, run):
    """Every applied decision appears in the
    M/A/D output with its reason): a declined collaborator edit is reverted
    and a declined collaborator add is deleted, each named "by decision
    <record sha12>"."""
    s = solo()
    f = s.collab().commit({"README.md": "collab readme\n", "src/extra.py": "x\n"}, "collab")
    _pull_record(s.repo, s.config, f, entries=[
        {"path": "README.md", "decision": "decline", "cls": "theirs_changed", "flags": [],
         "theirs": _blob_of(s.url, f, "README.md"), "result": None},
        {"path": "src/extra.py", "decision": "decline", "cls": "theirs_added", "flags": [],
         "theirs": _blob_of(s.url, f, "src/extra.py"), "result": None}])
    record = git_run("rev-parse", "HEAD", cwd=s.repo).strip()[:12]
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    out = r.stdout + r.stderr
    assert f"README.md (by decision {record})" in out
    assert f"deleted: src/extra.py (by decision {record})" in out
    tip = _tip(s.url)
    assert git_run("show", f"{tip}:README.md", cwd=_bare(s.url)) == "readme\n"
    assert "src/extra.py" not in git_run("ls-tree", "-r", "--name-only", tip, cwd=_bare(s.url))


def test_ok_push_is_confirmed_by_ancestry_before_the_local_ref_moves(solo, run, monkeypatch):
    """Design §4.4: after a porcelain ok, publish
    calls confirm_by_ancestry before advance_local."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    order = []
    real_confirm, real_advance = pubrepo.confirm_by_ancestry, pubrepo.advance_local
    monkeypatch.setattr(pubrepo, "confirm_by_ancestry",
                        lambda *a, **k: order.append("confirm") or real_confirm(*a, **k))
    monkeypatch.setattr(pubrepo, "advance_local",
                        lambda *a, **k: order.append("advance") or real_advance(*a, **k))
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    assert order == ["confirm", "advance"]


def test_ok_push_overwritten_before_confirmation_is_5(solo, run, monkeypatch):
    """The push lands, then the branch is rewound before confirmation:
    exit 5, and the local branch does not move."""
    s = solo()
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    local = git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip()
    real = pubrepo.push_snapshot

    def push_then_rewind(publish_dir, sha, branch):
        outcome = real(publish_dir, sha, branch)
        git_run("update-ref", "refs/heads/main", s.first, sha, cwd=_bare(s.url))
        return outcome

    monkeypatch.setattr(pubrepo, "push_snapshot", push_then_rewind)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert "was pushed, but the public branch no longer contains it" in r.stderr
    assert git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip() == local


# --- builder-level tests for the snapshot path ------------------------------------

_PNG = b"\x89PNG\r\n\x1a\n" + bytes(64)


@pytest.mark.parametrize("kind, files", [
    ("text_inside_include", {"src/app.py": "print('collab')\n"}),
    ("binary", {"src/logo.png": _PNG}),
    ("outside_include", {"notes.txt": "collab notes\n"}),
    ("new_path_inside_include", {"src/new.py": "x = 1\n"}),
])
def test_foreign_commit_of_each_kind_exits_5_and_changes_nothing(solo, run, kind, files):
    """publish::foreign_kinds_exit5 (AC1): a foreign commit F of each kind →
    exit 5 naming F; the remote and .publish/ HEAD are unchanged."""
    s = solo()
    c = s.collab()
    for rel, data in files.items():
        path = c.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        (path.write_bytes if isinstance(data, bytes) else path.write_text)(data)
        git_run("add", rel, cwd=c.path)
    git_run("commit", "-q", "-m", f"collab {kind}", cwd=c.path)
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=c.path)
    f = git_run("rev-parse", "HEAD", cwd=c.path).strip()
    local = git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip()
    _commit(s.repo, {"README.md": "ours\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 5, r.stderr
    assert f[:7] in r.stderr
    assert _tip(s.url) == f
    assert git_run("rev-parse", "HEAD", cwd=s.repo / ".publish").strip() == local


def test_keep_pull_item(solo, run):
    """publish::keep_pull_item (AC25): a collaborator's edit of a keep file
    is a pull item, never overwritten by the owner's copy: publish → 8."""
    s = solo(keep=["LICENSE"])
    (s.repo / ".publish" / "LICENSE").write_text("licence v1\n")
    assert run(["publish", "--yes"], cwd=s.repo).code == 0
    f = s.collab().commit({"LICENSE": "licence, collab edit\n"}, "collab licence")
    _pull_record(s.repo, s.config, f)                 # pulled through, LICENSE undecided
    _commit(s.repo, {"src/app.py": "print(2)\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 8, r.stderr
    assert "LICENSE" in r.stdout + r.stderr
    assert git_run("show", f"{_tip(s.url)}:LICENSE", cwd=_bare(s.url)) == "licence, collab edit\n"


def _xlsx(text: str) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        z.writestr("xl/sharedStrings.xml",
                   f'<?xml version="1.0"?><sst><si><t>{text}</t></si></sst>')
    return buf.getvalue()


@pytest.mark.parametrize("name, data, extra, code, shown", [
    ("src/merge.txt", b"a\n<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n", {}, 4,
     "src/merge.txt"),                                                         # AC23
    ("src/doc.rst", b"Title\n=======\n\nbody\n", {}, 0, None),                 # AC23
    ("src/logo.png", _PNG, {}, 4, "src/logo.png"),                             # AC22
    ("src/logo.png", _PNG, {"scrub": {"allow_opaque": ["src/logo.png"]}}, 0,
     "not scanned"),                                                           # AC22
    ("src/old.doc", bytes.fromhex("D0CF11E0A1B11AE1") + bytes(64), {}, 4,
     "encrypted or legacy Office container"),                                  # AC21 (CFB)
    ("src/book.xlsx", b"PK\x03\x04 not really a zip", {}, 4, "src/book.xlsx"),  # AC21
    ("src/book.xlsx", _xlsx("our sekrit"), {"scrub": {"forbidden": ["sekrit"]}}, 4,
     "xl/sharedStrings.xml"),                                                  # AC20
], ids=["marker_triple", "rst_underline", "binary", "binary_allowlisted", "cfb",
        "corrupt_zip", "xlsx_forbidden"])
def test_gate_through_publish(solo, run, name, data, extra, code, shown):
    """publish::gate_through_publish (AC20–AC23 through publish):
    each case blocks (4, naming the path or part) or publishes (0)."""
    s = solo(**extra)
    path = s.repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    git_run("add", name, cwd=s.repo)
    git_run("commit", "-q", "-m", "add", cwd=s.repo)
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == code, r.stdout + r.stderr
    if shown:
        assert shown in r.stdout + r.stderr
    if code:
        assert _tip(s.url) == s.first


def test_eol_crlf_and_clean_filter_through_publish(solo, run):
    """publish::eol_and_filter_through_publish (AC79, AC96 through publish):
    with `* text=auto eol=crlf` a pulled collaborator file republishes with
    no change, and a dirty edit stores clean (LF) bytes; a path with a clean
    filter publishes the raw worktree bytes and is recorded nonderivable."""
    s = solo()
    (s.repo / ".gitattributes").write_text("* text=auto eol=crlf\nsrc/data.txt filter=upper\n")
    git_run("config", "filter.upper.clean", "tr a-z A-Z", cwd=s.repo)
    git_run("config", "filter.upper.smudge", "cat", cwd=s.repo)
    f = s.collab().commit({"src/c.txt": "collab\nlines\n"}, "collab text")
    blob = _blob_of(s.url, f, "src/c.txt")
    _pull_record(s.repo, s.config, f, files={"src/c.txt": "collab\r\nlines\r\n"}, entries=[{
        "path": "src/c.txt", "decision": "accept", "cls": "theirs_added", "flags": [],
        "theirs": blob, "result": None}])
    _commit(s.repo, {"src/data.txt": "raw bytes\n"})
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    tip = _tip(s.url)
    assert _blob_of(s.url, tip, "src/c.txt") == blob                        # AC79: no M
    assert git_run("show", f"{tip}:src/data.txt", cwd=_bare(s.url)) == "raw bytes\n"   # AC96
    rec = _record(s.repo, f"refs/pubrepo/snap/{tip}")
    assert "src/data.txt" in rec["nonderivable"]
    (s.repo / "src" / "app.py").write_bytes(b"print(3)\r\n")                 # dirty, CRLF
    r = run(["publish", "--yes"], cwd=s.repo)
    assert r.code == 0, r.stderr
    assert git_run("show", f"{_tip(s.url)}:src/app.py", cwd=_bare(s.url)) == "print(3)\n"


def _with_tag_remote(repo, name, url):
    """§18.9 R1/R2: remote `name` → `url`, named as [publish]
    tag_remote and listed in private_remotes; committed, so the source is
    clean and the bookmark path runs."""
    git_run("remote", "add", name, url, cwd=repo)
    cfg = repo / ".publish.toml"
    cfg.write_text(cfg.read_text().replace(
        "[publish]\n", f'[publish]\ntag_remote = "{name}"\nprivate_remotes = ["{url}"]\n', 1))
    git_run("commit", "-qam", f"tag_remote = {name}", cwd=repo)


def test_audit_tag_push_failure_is_one_line_with_reason_under_verbose(solo, run, tmp_path):
    """With an unreachable tag_remote, the
    bookmark stays local and publish prints one line, "bookmark <ts> kept
    locally; push to <name> failed", and no raw git text; the sanitized
    reason appears only under --verbose. Exit 0. AC75 re-target (§10 item
    20, §18.9 R1/R2): the bookmark goes only to a listed tag_remote, never
    to origin."""
    import time
    s = solo()
    _with_tag_remote(s.repo, "bk", str(tmp_path / "nowhere.git"))
    pattern = re.compile(r"^WARNING: bookmark (\d{8}T\d{6}) kept locally; "
                         r"push to bk failed$")
    for verbose in (False, True):
        time.sleep(1.1)                  # published/<ts> has one-second resolution
        (s.repo / "src" / "app.py").write_text(f"print({verbose!r})\n")
        git_run("commit", "-qam", f"edit {verbose}", cwd=s.repo)
        r = run(["publish", "--yes", *(["--verbose"] if verbose else [])], cwd=s.repo)
        assert r.code == 0, r.stderr
        notices = [m for m in map(pattern.match, r.stderr.splitlines()) if m]
        assert len(notices) == 1, r.stderr
        assert f"refs/pubrepo/bookmarks/{notices[0][1]}" in git_run(
            "for-each-ref", "--format=%(refname)", "refs/pubrepo/bookmarks/", cwd=s.repo).split()
        assert ("does not appear to be a git repository" in r.stderr) == verbose, r.stderr
        if not verbose:
            assert "fatal:" not in r.stderr, r.stderr
