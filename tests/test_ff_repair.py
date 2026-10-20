"""Repair (Phase 1) and release-retire (design §6).

Every test here rewrites public history in tmp bare remotes over file://;
they are marked `rewrite` (AC62's shim exempts them). The managed release
ledger comes from real managed release tags (adopt_tags).
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

import pubrepo
from conftest import git_run
from ff_fixtures import _git, push_argv

pytestmark = pytest.mark.rewrite

LEAK = "leaked material, long enough to be content-matched " * 3 + "\n"


# --- helpers ---

def _repair(run, w, *args):
    return run(["repair", *args], cwd=w.src)


def _digest(r):
    last = r.stdout.strip().splitlines()[-1]
    assert last.startswith("Plan digest: "), (r.code, r.stdout, r.stderr)
    return last.split(": ", 1)[1]


def _remote(w):
    return Path(w.url[len("file://"):])


def _remote_refs(w):
    out = git_run("ls-remote", w.url, cwd=w.src)
    return {n: s for s, n in (l.split("\t") for l in out.splitlines()) if n != "HEAD"}


def _apply(run, w, *args):
    r = _repair(run, w, *args, "--plan")
    assert r.code == 0, r.stderr
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    return _repair(run, w, *args, "--apply", "--yes", "--expect-tip", tip, "--expect-plan", d)


def _leaky_world(world, **extra):
    """A public snapshot with secret.txt, then the owner drops it from
    source (so the projection no longer contains it)."""
    w = world(include=["a.py", "docs", "secret.txt"],
              files={"a.py": "a\n", "docs/guide.md": "# Guide\n", "secret.txt": LEAK}, **extra)
    first = w.snapshot(record=True)
    _drop_secret(w)
    return w, first


def _drop_secret(w):
    """The owner removes the leak from source and from the manifest (an
    explicit include of a missing file is a config error)."""
    (w.src / "secret.txt").unlink()
    cfg = w.src / ".publish.toml"
    cfg.write_text(cfg.read_text().replace(', "secret.txt"', ""))
    git_run("commit", "-qam", "remove secret", cwd=w.src)


def _adopt(w, tag, sha):
    """A real managed release tag (design §8.5): the tag on public at
    `sha`, listed in [publish.release] adopt_tags (committed)."""
    _tag(w, tag, sha)
    cfg = w.src / ".publish.toml"
    cfg.write_text(cfg.read_text() + f'\n[publish.release]\nadopt_tags = ["{tag}"]\n')
    git_run("commit", "-qam", f"adopt {tag}", cwd=w.src)


def _tag(w, name, sha):
    git_run("tag", name, sha, cwd=w.pub)
    git_run("push", "-q", "origin", f"refs/tags/{name}", cwd=w.pub)


def _records(w, prefix="refs/pubrepo/"):
    return git_run("for-each-ref", "--format=%(refname)", prefix, cwd=w.src).split()


def _reachable_blobs(w, ref="refs/heads/main"):
    clone = w.src.parent / f"check-{os.getpid()}-{len(list(w.src.parent.iterdir()))}"
    git_run("clone", "-q", "--mirror", w.url, str(clone))
    out = git_run("rev-list", "--objects", "--all", cwd=clone)
    return {line.split(" ", 1)[0] for line in out.splitlines()}


# --- plan (AC52) ---

def test_plan_writes_nothing_and_names_every_item(world, run):
    """AC52: --plan writes nothing and prints all 8 §6.3 items."""
    w, first = _leaky_world(world)
    _tag(w, "draft-x", first)
    before_refs, before_records = _remote_refs(w), _records(w)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    assert r.code == 0
    for item in ("1. remote branch main", "2. commits rewritten", "3. changed and removed paths",
                 "4. public refs reaching", "5. treatments", "6. authors whose clones",
                 "7. Host protection: unknown until attempted.", "8. Plan digest"):
        assert item in r.stdout
    assert "refs/tags/draft-x" in r.stdout and "UNHANDLED" in r.stdout
    assert _remote_refs(w) == before_refs and _records(w) == before_records
    assert len(_digest(r)) == 64


def test_digest_stable_and_bound_to_treatments(world, run):
    w, first = _leaky_world(world)
    _tag(w, "draft-x", first)
    d1 = _digest(_repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan"))
    d2 = _digest(_repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan"))
    d3 = _digest(_repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan",
                         "--leave-reachable", "refs/tags/draft-x"))
    assert d1 == d2 != d3


# --- confirmation and bindings (AC53) ---

def test_apply_off_tty_needs_expect_flags(world, run):
    w, first = _leaky_world(world)
    assert _repair(run, w, "remove-path", "secret.txt", "--from", first, "--apply").code == 1
    assert _repair(run, w, "remove-path", "secret.txt", "--from", first, "--apply",
                   "--yes", "--expect-tip", "x").code == 1


def test_stale_tip_is_5(world, run):
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    w.collab_commit({"notes.txt": "moved\n"})
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--apply", "--yes",
                "--expect-tip", tip, "--expect-plan", d)
    assert r.code == 5
    assert not any(x.endswith("/intent") for x in _records(w))


def test_unhandled_reaching_ref_refused_before_push(world, run):
    w, first = _leaky_world(world)
    _tag(w, "draft-x", first)
    before = _remote_refs(w)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first)
    assert r.code == 1 and "refs/tags/draft-x" in r.stderr
    assert _remote_refs(w) == before


def test_lease_stale_between_plan_and_push_is_5(world, run, monkeypatch):
    """A collaborator push lands after the post-lock recompute and
    before the push → the lease rejects → 5, nothing landed."""
    w, first = _leaky_world(world)
    real = pubrepo.push_porcelain
    pushed = {}

    def racing(publish_dir, refspecs, **kw):
        if not pushed:
            pushed["x"] = w.collab_commit({"race.txt": "r\n"})
        return real(publish_dir, refspecs, **kw)

    monkeypatch.setattr(pubrepo, "push_porcelain", racing)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first)
    assert r.code == 5
    assert _remote_refs(w)["refs/heads/main"] == pushed["x"]


def test_atomic_collateral_reported(world, run, monkeypatch):
    """An atomic repair push where the branch's lease
    is stale → 5, and the other ref, which git dropped as `atomic push
    failed`, is reported per ref as atomic_collateral: neither landed nor
    refused (no `applied` or `refused` record)."""
    w, first = _leaky_world(world)
    _tag(w, "t1", first)
    real = pubrepo.push_porcelain
    pushed = {}

    def racing(publish_dir, refspecs, **kw):
        if not pushed:
            pushed["x"] = w.collab_commit({"race.txt": "r\n"})
        return real(publish_dir, refspecs, **kw)

    monkeypatch.setattr(pubrepo, "push_porcelain", racing)
    tag_before = _remote_refs(w)["refs/tags/t1"]
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--also-delete-ref", "refs/tags/t1")
    assert r.code == 5, r.stderr
    assert "refs/tags/t1: atomic_collateral (not pushed; the host did not refuse it)" in r.stderr
    assert "refs/heads/main: lease_stale" in r.stderr
    refs = _remote_refs(w)
    assert refs["refs/tags/t1"] == tag_before and refs["refs/heads/main"] == pushed["x"]
    recs = _records(w, "refs/pubrepo/repairs/")
    assert not any(x.endswith(("/applied", "/refused")) for x in recs)


# --- preconditions (AC54, AC55) ---

def test_released_ancestry_refused(world, run):
    """AC54, on a real managed tag (adopt_tags)."""
    w, first = _leaky_world(world)
    _adopt(w, "v1.0.0", first)
    before = _records(w)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    # The hint points at docs/releasing.md, not release-retire: retiring a
    # tag doesn't unblock repair (tests/test_u_repair_released.py).
    assert r.code == 1 and "released ancestry" in r.stderr and "docs/releasing.md" in r.stderr
    assert _records(w) == before


def test_projection_still_contains_target_refused(world, run):
    w = world(include=["a.py", "secret.txt"], files={"a.py": "a\n", "secret.txt": LEAK})
    first = w.snapshot()
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    assert r.code == 1 and "projection still contains" in r.stderr


def test_merge_range_refused_phase_2(world, run):
    w, first = _leaky_world(world)
    clone = w.pub.parent / "merger"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    git_run("checkout", "-q", "-b", "side", cwd=clone)
    (clone / "side.txt").write_text("s\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "side", cwd=clone)
    git_run("checkout", "-q", "main", cwd=clone)
    (clone / "main.txt").write_text("m\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "main", cwd=clone)
    git_run("merge", "-q", "--no-ff", "-m", "merge", "side", cwd=clone)
    git_run("push", "-q", "origin", "main", cwd=clone)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    assert r.code == 1 and "Phase 2" in r.stderr


# --- apply (AC55, AC57, AC98) ---

def test_linear_remove_path_preserves_authors_and_rekeys(world, run):
    """AC55 + AC98: content removed from every commit; authors and messages
    kept; recorded snapshots re-keyed by addition; the old record untouched."""
    w, first = _leaky_world(world)
    f = w.collab_commit({"notes.txt": "collab\n"})
    leaked = w.blob(first, "secret.txt")
    old_records = _records(w, "refs/pubrepo/snap/")
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first)
    assert r.code == 0, r.stderr + r.stdout
    assert "APPLIED" in r.stderr + r.stdout
    assert leaked not in _reachable_blobs(w)
    git_run("fetch", "-q", "origin", cwd=w.pub)
    log = git_run("log", "--format=%an|%s", "origin/main", cwd=w.pub).splitlines()
    assert log[0] == "Collab Orator|collaborator change"
    recs = _records(w)
    applied = [x for x in recs if x.endswith("/applied")]
    assert len(applied) == 1
    new_first = git_run("rev-list", "--max-parents=0", "origin/main", cwd=w.pub).strip()
    assert f"refs/pubrepo/snap/{new_first}" in recs
    assert f"refs/pubrepo/status/{first}/rewritten" in recs
    assert set(old_records) <= set(recs)                        # never moved or deleted
    assert w.records_synced()                        # arrival: repair (§18.11)
    integ = w.integ()
    assert integ.state in ("S0", "S1", "S2") and not integ.items
    assert integ.state != "S7"


def test_leave_reachable_is_4_and_rerun_still_4(world, run):
    """AC57: draft-x left reachable → 4 APPLIED, INCOMPLETE; the
    identical rerun recomputes and is still 4."""
    w, first = _leaky_world(world)
    _tag(w, "draft-x", first)
    args = ("remove-path", "secret.txt", "--from", first, "--leave-reachable",
            "refs/tags/draft-x", "--json")
    r = _repair(run, w, *args[:-1], "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    cmd = [*args, "--apply", "--yes", "--expect-tip", tip, "--expect-plan", d]
    r = _repair(run, w, *cmd)
    assert r.code == 4 and "APPLIED, INCOMPLETE" in r.stderr
    out = json.loads(r.stdout)                                  # stdout: one JSON document
    assert out["result"] == "applied_incomplete" and out["still_reachable"] == ["refs/tags/draft-x"]
    r = _repair(run, w, *cmd)
    assert r.code == 4 and "APPLIED, INCOMPLETE" in r.stderr


def test_rerun_pre_push_tip_rebuilds_same_digest(world, run, monkeypatch):
    """Ruling (b): intent written, push never landed → rerun plans again
    with the identical digest and proceeds."""
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    real = pubrepo.push_porcelain
    monkeypatch.setattr(pubrepo, "push_porcelain",
                        lambda *a, **k: (_ for _ in ()).throw(pubrepo.PublishError("network down")))
    cmd = ["remove-path", "secret.txt", "--from", first, "--apply", "--yes",
           "--expect-tip", tip, "--expect-plan", d]
    assert _repair(run, w, *cmd).code == 2
    assert any(x.endswith(f"{d}/intent") for x in _records(w))
    monkeypatch.setattr(pubrepo, "push_porcelain", real)
    assert _repair(run, w, *cmd).code == 0


def test_rerun_post_push_tip_writes_applied(world, run, monkeypatch):
    """AC59: the push landed but the run died before `applied`;
    the identical rerun writes it and reports per §6.7."""
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    cmd = ["remove-path", "secret.txt", "--from", first, "--apply", "--yes",
           "--expect-tip", tip, "--expect-plan", d]
    finish = pubrepo._finish_repair
    monkeypatch.setattr(pubrepo, "_finish_repair",
                        lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert _repair(run, w, *cmd).code == 130          # died after the push landed
    monkeypatch.setattr(pubrepo, "_finish_repair", finish)   # not undo(): keeps the sandbox env
    assert not any(x.endswith("/applied") for x in _records(w))
    r = _repair(run, w, *cmd)
    assert r.code == 0, r.stderr
    assert any(x.endswith(f"{d}/applied") for x in _records(w))
    assert "already pushed" in r.stderr


def test_rerun_other_tip_exit5(world, run):
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    cmd = ["remove-path", "secret.txt", "--from", first, "--apply", "--yes",
           "--expect-tip", tip, "--expect-plan", d]
    assert _repair(run, w, *cmd).code == 0
    w.collab_commit({"after.txt": "a\n"})
    assert _repair(run, w, *cmd).code == 5


def test_from_root_commit(world, run):
    """--from <root> rewrites from the root with no base^."""
    w, first = _leaky_world(world)
    assert git_run("rev-list", "--max-parents=0", "main", cwd=w.pub).strip() == first
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 0


def test_cleanup_commands_target_publish_only(world, run):
    """Every printed cleanup command is `git -C <.publish>`."""
    w, first = _leaky_world(world)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first)
    block = r.stdout[r.stdout.index("Local cleanup"):].splitlines()[1:]
    assert block and all(line.strip().startswith(f"git -C {w.pub}") for line in block)
    assert all("| git -C " not in line or line.count(f"-C {w.pub}") == 2 for line in block)


# --- per-ref treatments (AC84, refs/pull) ---

def _feature_x(w, first, n=10):
    clone = w.pub.parent / "fx"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    git_run("checkout", "-q", "-b", "feature-x", first, cwd=clone)
    for i in range(n):
        (clone / f"fx{i}.txt").write_text(f"{i}\n")
        git_run("add", "-A", cwd=clone)
        git_run("commit", "-qm", f"fx {i}", cwd=clone)
    git_run("push", "-q", "origin", "feature-x", cwd=clone)
    return clone


def test_delete_ref_names_every_destroyed_commit(world, run):
    """AC84: deleting feature-x (10 non-leak commits on the leaky root)
    lists all 11 commits that become unreachable... the 10 plus none of
    main's; with main's root rewritten, feature-x's base is destroyed too."""
    w, first = _leaky_world(world)
    w.commit_source({"a.py": "a2\n"})
    w.snapshot(carry=True)
    _feature_x(w, first)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan",
                "--also-delete-ref", "refs/heads/feature-x")
    destroyed = [l for l in r.stdout.splitlines() if l.strip().startswith("destroyed ")]
    assert len(destroyed) == 11, r.stdout


def test_rewrite_ref_preserves_its_commits(world, run):
    """AC84 second half: --also-rewrite-ref keeps the 10 commits, leak-free."""
    w, first = _leaky_world(world)
    _feature_x(w, first)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--also-rewrite-ref", "refs/heads/feature-x")
    assert r.code == 0, r.stderr + r.stdout
    git_run("fetch", "-q", "origin", "+refs/heads/feature-x:refs/remotes/origin/feature-x",
            cwd=w.pub)
    assert git_run("rev-list", "--count", "origin/feature-x", cwd=w.pub).strip() == "11"
    assert "secret.txt" not in git_run("log", "--name-only", "--format=", "origin/feature-x",
                                       cwd=w.pub)


def test_refs_pull_only_leave_reachable(world, run):
    """refs/pull/* with --also-delete-ref is refused before the push."""
    w, first = _leaky_world(world)
    git_run("update-ref", "refs/pull/1/head", first, cwd=_remote(w))
    git_run("update-ref", "refs/pull/1/merge", first, cwd=_remote(w))
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan",
                "--also-delete-ref", "refs/pull/1/head")
    assert r.code == 1 and "leave-reachable" in r.stderr
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--leave-reachable", "refs/pull/1/head", "--leave-reachable", "refs/pull/1/merge")
    assert r.code == 4


def test_deny_deletes_is_host_refusal_2(world, run):
    """receive.denyDeletes (it guards branches) with --also-delete-ref
    → host refusal, 2, `refused` record, nothing landed (atomic)."""
    w, first = _leaky_world(world)
    _feature_x(w, first, n=1)
    git_run("config", "receive.denyDeletes", "true", cwd=_remote(w))
    before = _remote_refs(w)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--also-delete-ref", "refs/heads/feature-x")
    assert r.code == 2 and "NOT APPLIED" in r.stderr
    assert _remote_refs(w) == before
    assert any(x.endswith("/refused") for x in _records(w))


def test_protected_branch_hook_is_2_and_next_state_not_s12(world, run):
    """AC56: a GH006-style pre-receive refusal → 2, `refused` written, and
    the next run is not S12 (only applied records count)."""
    w, first = _leaky_world(world)
    hook = _remote(w) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'remote: error: GH006: Protected branch update failed' >&2\n"
                    "exit 1\n")
    hook.chmod(0o755)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first)
    assert r.code == 2
    assert any(x.endswith("/refused") for x in _records(w))
    hook.unlink()
    assert w.integ().state != "S12"


@pytest.mark.parametrize("refuse,code", [("refs/tags/t2", 4), ("refs/tags/t1", 2)])
def test_nonatomic_partial_result(world, run, refuse, code):
    """AC86: no atomic push; an `update` hook refuses one ref. The
    fallback pushes ref by ref (branch last) and stops at the refusal:
    something landed → 4; nothing landed → 2."""
    w, first = _leaky_world(world)
    _tag(w, "t1", first)
    _tag(w, "t2", first)
    git_run("config", "receive.advertiseAtomic", "false", cwd=_remote(w))
    hook = _remote(w) / "hooks" / "update"
    hook.write_text(f'#!/bin/sh\n[ "$1" = "{refuse}" ] && exit 1\nexit 0\n')
    hook.chmod(0o755)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--also-delete-ref", "refs/tags/t1", "--also-delete-ref", "refs/tags/t2")
    assert r.code == code, r.stderr + r.stdout
    assert ("refs/tags/t1" in r.stderr) and ("refs/tags/t2" in r.stderr or code == 2)


def _pub_calls(w, recorder, start):
    """The recorded git calls pubrepo made in the publish clone (not the
    collaborator's or the test's own)."""
    pub = w.pub.resolve()
    return [(a, cwd) for a, cwd in recorder.calls()[start:] if Path(cwd).resolve() == pub]


def _no_atomic_world(world):
    w, first = _leaky_world(world)
    _tag(w, "t1", first)
    git_run("config", "receive.advertiseAtomic", "false", cwd=_remote(w))
    return w, first


def test_atomic_unsupported_falls_back_after_lsremote_check(world, run, git_recorder):
    """Design §6.4: when the host lacks atomic push, an `ls-remote`
    confirms no public ref moved before the first per-ref push; the
    fallback then lands every update (branch last)."""
    w, first = _no_atomic_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first,
                "--also-delete-ref", "refs/tags/t1", "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    start = len(git_recorder.calls())
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first,
                "--also-delete-ref", "refs/tags/t1", "--apply", "--yes",
                "--expect-tip", tip, "--expect-plan", d)
    assert r.code == 0, r.stderr + r.stdout
    net = [a for a, cwd in _pub_calls(w, git_recorder, start)
           if push_argv(a) or (a[:1] == ["ls-remote"])]
    pushes = [i for i, a in enumerate(net) if push_argv(a)]
    atomic = [i for i in pushes if "--atomic" in push_argv(net[i])]
    per_ref = [i for i in pushes if "--atomic" not in push_argv(net[i])]
    assert len(atomic) == 1 and [push_argv(net[i])[-1] for i in per_ref] == [
        ":refs/tags/t1", f"{_remote_refs(w)['refs/heads/main']}:refs/heads/main"]
    between = net[atomic[0] + 1:per_ref[0]]
    assert ["ls-remote", "origin"] in between, net    # the full listing, before any per-ref push
    assert "refs/tags/t1" not in _remote_refs(w)


def test_atomic_unsupported_refs_moved_before_fallback_is_5(world, run, git_recorder,
                                                            monkeypatch):
    """A public ref moved between the plan and the fallback's
    `ls-remote` check → 5, and no per-ref push runs."""
    w, first = _no_atomic_world(world)
    real = pubrepo.push_porcelain
    pushed = {}

    def then_collaborator_pushes(publish_dir, refspecs, **kw):
        out = real(publish_dir, refspecs, **kw)
        if kw.get("atomic") and not pushed:
            pushed["x"] = w.collab_commit({"race.txt": "r\n"})
        return out

    monkeypatch.setattr(pubrepo, "push_porcelain", then_collaborator_pushes)
    tag_before = _remote_refs(w)["refs/tags/t1"]
    start = len(git_recorder.calls())
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--also-delete-ref", "refs/tags/t1")
    assert r.code == 5, r.stderr
    assert "public refs moved since the plan: refs/heads/main; nothing was pushed" in r.stderr
    pushes = [push_argv(a) for a, _ in _pub_calls(w, git_recorder, start) if push_argv(a)]
    assert pushes and all("--atomic" in p for p in pushes)       # no per-ref push
    refs = _remote_refs(w)
    assert refs["refs/tags/t1"] == tag_before and refs["refs/heads/main"] == pushed["x"]


def test_atomic_refusal_nothing_lands_result_from_ls_remote(world, run):
    """An atomic push where the host refuses one ref. The server reports
    the others `[remote rejected] (atomic transaction failed)`, which the
    status-field rule classifies as host_refused; client-side
    `[rejected] (atomic push failed)` is the only atomic_collateral. The
    result comes from the post-push ls-remote: nothing landed → 2."""
    w, first = _leaky_world(world)
    _tag(w, "t1", first)
    hook = _remote(w) / "hooks" / "update"
    hook.write_text('#!/bin/sh\n[ "$1" = "refs/tags/t1" ] && exit 1\nexit 0\n')
    hook.chmod(0o755)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--also-delete-ref", "refs/tags/t1")
    before_main = w.blob(first, "secret.txt")
    assert r.code == 2 and "refs/tags/t1: host_refused" in r.stderr
    assert before_main in _reachable_blobs(w)


# --- rewind ---

def test_rewind_drops_commits_and_marks_snapshots(world, run):
    w = world()
    first = w.snapshot(record=True)
    w.commit_source({"a.py": "a2\n"})
    second = w.snapshot(record=True)
    r = _apply(run, w, "rewind", first)
    assert r.code == 0, r.stderr + r.stdout
    assert _remote_refs(w)["refs/heads/main"] == first
    assert f"refs/pubrepo/status/{second}/dropped" in _records(w)


# --- reintroduction (AC58, AC91) ---

def test_reintroduced_on_second_clone_is_s12_sanitized_readd_is_not(world, run, tmp_path):
    """AC58 + AC91: after the repair syncs, a collaborator re-uploads the
    removed blob at a new path → S12 on a second clone with synced records;
    a sanitized file at the removed path → not S12."""
    w, first = _leaky_world(world)
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 0
    w.collab = None
    w.collab_commit({"copy/again.txt": LEAK})
    second = tmp_path / "second-src"
    origin = git_run("remote", "get-url", "origin", cwd=w.src).strip()
    git_run("clone", "-q", origin, str(second))
    assert pubrepo.records_fetch(second, w.config()).ok
    integ = w.integ(src_dir=second)
    assert integ.state == "S12"
    assert [h.path for h in integ.reintroduced] == ["copy/again.txt"]


def test_sanitized_readd_at_removed_path_is_not_s12(world, run):
    """AC58 bullet 3: a path alone never matches."""
    w, first = _leaky_world(world)
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 0
    w.collab = None
    w.collab_commit({"secret.txt": "sanitized replacement\n"})
    assert w.integ().state != "S12"


# --- restore-ancestry (AC60) and release-retire ---

VERSION_RE = r'^VERSION = "(?P<version>[0-9A-Za-z.+-]+)"$'


def _dropped_release(world, content="clean\n", *, releasable=False):
    """v1.0.0 is a real managed tag (adopt_tags) on a release snapshot that
    a collaborator then force-drops from main. `releasable` adds a version
    file and an enabled [publish.release] so a later release can run."""
    if releasable:
        w = world(files={"a.py": "a = 1\n", "docs/guide.md": "# Guide\n",
                         "version.py": 'VERSION = "1.0.0"\n'},
                  include=["a.py", "docs", "version.py"],
                  release={"enabled": True, "version_file": "version.py",
                           "version_pattern": VERSION_RE, "tag_format": "v{version}",
                           "source_tag": False, "adopt_tags": ["v1.0.0"]})
    else:
        w = world()
    w.snapshot(record=True)
    w.commit_source({"docs/rel.md": content})
    rel = w.snapshot(record=True, carry=True)
    if releasable:
        _tag(w, "v1.0.0", rel)                       # adopt_tags is already committed
    else:
        _adopt(w, "v1.0.0", rel)
    clone = w.pub.parent / "dropper"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    git_run("reset", "-q", "--hard", "HEAD~1", cwd=clone)
    (clone / "other.txt").write_text("o\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "drop the release", cwd=clone)
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=clone)
    git_run("fetch", "-q", "origin", cwd=w.pub)
    return w, rel


def _integrate_rewrite(run, w):
    """The owner accepts the collaborator's rewrite (S7 → pull), keeping
    the release's file in source."""
    flags = ["--accept-rewritten-history", "--keep", "docs/rel.md"]
    r = run(["pull", "--plan", *flags], cwd=w.src)
    pd = r.stdout.strip().splitlines()[-1].split(": ", 1)[1]
    r = run(["pull", "--yes", *flags, "--expect-tip", _remote_refs(w)["refs/heads/main"],
             "--expect-plan", pd], cwd=w.src)
    assert r.code == 0, r.stderr


def test_restore_ancestry_clean_is_ours_snapshot(world, run):
    """AC60: plan, confirm, fast-forward merge snapshot that is
    ours (frozen message, record with empty `written`)."""
    w, rel = _dropped_release(world)
    # Over the unpulled rewrite (S7) restore-ancestry refuses
    # with 5 and writes nothing; the owner pulls first.
    before_refs, before_records = _remote_refs(w), _records(w)
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--plan")
    assert r.code == 5 and "pull" in r.stderr
    assert _remote_refs(w) == before_refs and _records(w) == before_records
    _integrate_rewrite(run, w)
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--plan")
    assert r.code == 0 and "removed from public by someone else" in r.stdout
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--apply", "--yes",
                "--expect-tip", tip, "--expect-plan", d)
    assert r.code == 0, r.stderr
    new = _remote_refs(w)["refs/heads/main"]
    parents = git_run("rev-list", "--parents", "-n1", new, cwd=w.pub).split()[1:]
    assert parents == [tip, rel]
    assert "Source: " in git_run("log", "-1", "--format=%B", new, cwd=w.pub)
    snap = json.loads(git_run("cat-file", "blob", f"refs/pubrepo/snap/{new}:record.json",
                              cwd=w.src))
    assert snap["written"] == {}
    integ = w.integ()
    assert integ.is_ours(new) and integ.ip == new and integ.state in ("S0", "S1")
    assert w.records_synced()                        # arrival: restore-ancestry (§18.11)


def test_restore_ancestry_gate_hit_refused(world, run):
    w, rel = _dropped_release(world, content="zz-restore-canary-zz\n")
    (w.src / ".publish.toml").write_text((w.src / ".publish.toml").read_text()
                                         + '\n[publish.scrub]\nforbidden = ["zz-restore-canary-zz"]\n')
    git_run("commit", "-qam", "forbid", cwd=w.src)
    _integrate_rewrite(run, w)
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--plan")
    assert r.code == 4 and "release-retire" in r.stderr


def test_release_retire(world, run):
    w = world()
    rel = w.snapshot()
    assert run(["release-retire", "v9.9.9", "--yes"], cwd=w.src).code == 7
    _adopt(w, "v1.0.0", rel)
    assert run(["release-retire", "v1.0.0"], cwd=w.src).code == 1          # off a TTY, no --yes
    assert run(["release-retire", "v1.0.0", "--yes"], cwd=w.src).code == 0
    assert pubrepo.is_retired(w.src, "v1.0.0")
    assert w.records_synced()                        # arrival: release-retire (§18.11)


# --- repair's confirmation (design §17.3; §6.4) ---

def test_typed_confirmation_is_the_one_prompt(world, run, monkeypatch):
    """On a TTY without --yes, the typed branch-and-count confirmation is
    repair's one prompt: a wrong count writes nothing; the right one
    applies. Off a TTY without --yes → 1 (§6)."""
    w, first = _leaky_world(world)
    before = _remote_refs(w)
    assert _repair(run, w, "remove-path", "secret.txt", "--from", first, "--apply").code == 1
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    answers = iter(["main", "999"])
    monkeypatch.setattr("builtins.input", lambda: next(answers))
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--apply")
    assert r.code == 1 and _remote_refs(w) == before
    count = str(len(git_run("rev-list", "main", cwd=w.pub).split()))
    answers = iter(["main", count])
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--apply")
    assert r.code == 0, r.stderr + r.stdout


# --- repair through the CLI and the real publish ---

def _publish(run, cwd):
    return run(["publish", "--yes"], cwd=cwd)


def test_ac100_offtty_refusals_before_any_fetch_or_push(world, run, git_recorder):
    """AC100 (repair rows, design §17.3): off a TTY without the
    confirming flags, `repair --apply` and `restore-ancestry --apply` exit
    1 with no fetch, ls-remote or push; with the expect flags they proceed
    (the other repair tests)."""
    w, first = _leaky_world(world)
    _adopt(w, "v1.0.0", first)
    calls = len(git_recorder.calls())
    for argv in (["repair", "remove-path", "secret.txt", "--from", first, "--apply"],
                 ["repair", "remove-path", "secret.txt", "--from", first, "--apply", "--yes"],
                 ["repair", "rewind", first, "--apply"],
                 ["repair", "restore-ancestry", "v1.0.0", "--apply"],
                 ["repair", "restore-ancestry", "v1.0.0", "--apply", "--yes"]):
        r = run(argv, cwd=w.src)
        assert r.code == 1, (argv, r.stderr)
    net = [a for a, _ in git_recorder.calls()[calls:] if a and a[0] in ("fetch", "ls-remote", "push")]
    assert net == []


def test_ac59_repair_after_push_sigterm_rerun_writes_applied(world, run):
    """AC59 (L4/O5) through the real seam: repair.after_push + sigterm in a
    child; the identical rerun writes `applied` and reports per §6.7."""
    import sys
    from conftest import SCRIPT_PATH
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    cmd = ["repair", "remove-path", "secret.txt", "--from", first, "--apply", "--yes",
           "--expect-tip", tip, "--expect-plan", d]
    env = {**os.environ, "PUBREPO_TEST_FAULT": "repair.after_push:sigterm"}
    child = subprocess.run(
        [sys.executable, "-c",
         "import importlib.machinery, importlib.util, sys\n"
         f"l = importlib.machinery.SourceFileLoader('pubrepo', {str(SCRIPT_PATH)!r})\n"
         "s = importlib.util.spec_from_loader('pubrepo', l); m = importlib.util.module_from_spec(s)\n"
         "sys.modules['pubrepo'] = m; l.exec_module(m)\n"
         f"sys.exit(m.main({cmd!r}))\n"],
        cwd=w.src, env=env, capture_output=True, text=True)
    assert child.returncode != 0
    assert _remote_refs(w)["refs/heads/main"] != tip, child.stderr    # the push landed
    assert not any(x.endswith("/applied") for x in _records(w))
    r = _repair(run, w, *cmd[1:])
    assert r.code == 0, r.stderr + r.stdout
    assert any(x.endswith(f"{d}/applied") for x in _records(w))


def test_ac41_repaired_commits_not_reoffered_by_pull(world, run):
    """AC41 bullet 3: after a repair re-SHAs a pulled collaborator commit,
    pull resolves it through the repair map and offers nothing again."""
    w, first = _leaky_world(world)
    f = w.collab_commit({"notes.txt": "collab\n"})
    r = run(["pull", "--plan"], cwd=w.src)
    pd = _digest(r)
    assert run(["pull", "--yes", "--expect-tip", f, "--expect-plan", pd], cwd=w.src).code == 0
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 0
    assert _remote_refs(w)["refs/heads/main"] != f                    # F was re-SHA'd
    r = run(["pull", "--plan"], cwd=w.src)
    assert r.code == 0, r.stdout + r.stderr
    assert "unpulled commits" not in r.stdout and "items (" not in r.stdout


def test_ac98_remove_path_across_dirty_published_snapshot_then_publish(world, run):
    """AC98: remove-path across a dirty-published, recorded snapshot →
    snap/<new> plus status/<old>/rewritten, the old record untouched; the
    next publish exits 0 with no items."""
    w = world(include=["a.py", "docs", "secret.txt"],
              files={"a.py": "a\n", "docs/guide.md": "# Guide\n", "secret.txt": LEAK})
    (w.src / "docs" / "draft.md").write_text("untracked, published dirty\n")
    first = w.snapshot(record=True, dirty=True, extra={"docs/draft.md": "untracked, published dirty\n"})
    old_rec = git_run("rev-parse", f"refs/pubrepo/snap/{first}", cwd=w.src).strip()
    _drop_secret(w)
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 0
    new = _remote_refs(w)["refs/heads/main"]
    recs = _records(w)
    assert f"refs/pubrepo/snap/{new}" in recs and f"refs/pubrepo/status/{first}/rewritten" in recs
    assert git_run("rev-parse", f"refs/pubrepo/snap/{first}", cwd=w.src).strip() == old_rec
    r = _publish(run, w.src)
    assert r.code == 0, r.stderr
    assert not w.integ().items


def test_ac58_reintroduction_on_second_clone_publish_is_5(world, run, tmp_path):
    """AC58 bullet 1 (publish level): on a second clone with synced
    records, publish exits 5 (S12) when the removed blob is re-uploaded
    under a new path."""
    w, first = _leaky_world(world)
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 0
    git_run("push", "-q", "origin", "main", cwd=w.src)
    w.collab = None
    w.collab_commit({"copy/again.txt": LEAK})
    second = tmp_path / "second-src"
    git_run("clone", "-q", git_run("remote", "get-url", "origin", cwd=w.src).strip(), str(second))
    git_run("config", "user.name", "Owner", cwd=second)
    git_run("config", "user.email", "owner@example.invalid", cwd=second)
    assert run(["init"], cwd=second).code == 0
    r = _publish(run, second)
    assert r.code == 5, r.stderr


def test_ac56_refused_repair_next_publish_not_s12(world, run):
    """AC56 (publish level): a host-refused repair writes `refused`, and
    the next publish is not S12."""
    w, first = _leaky_world(world)
    hook = _remote(w) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'GH006: Protected branch update failed' >&2\nexit 1\n")
    hook.chmod(0o755)
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 2
    hook.unlink()
    w.commit_source({"a.py": "a2\n"})
    r = _publish(run, w.src)
    assert r.code == 0, r.stderr


def test_ac91_two_clones_s12_on_both_applied_never_reverted(world, run, tmp_path):
    """AC91: two clones share records_remote. Clone A repairs to completion;
    a fetch on B; a collaborator's reintroduction → S12 on both A and B;
    A's `applied` ref is never reverted by a fetch."""
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d = _digest(r)
    assert _apply(run, w, "remove-path", "secret.txt", "--from", first).code == 0
    applied = git_run("rev-parse", f"refs/pubrepo/repairs/{d}/applied", cwd=w.src).strip()
    git_run("push", "-q", "origin", "main", cwd=w.src)
    b = tmp_path / "clone-b-src"
    git_run("clone", "-q", git_run("remote", "get-url", "origin", cwd=w.src).strip(), str(b))
    assert pubrepo.records_fetch(b, w.config()).ok
    w.collab = None
    w.collab_commit({"copy/leak2.txt": LEAK})
    assert pubrepo.records_fetch(w.src, w.config()).ok
    assert w.integ().state == "S12"
    assert w.integ(src_dir=b).state == "S12"
    assert git_run("rev-parse", f"refs/pubrepo/repairs/{d}/applied", cwd=w.src).strip() == applied


def test_ac60_restore_then_publish_nothing_then_release(world, run):
    """AC60: restore-ancestry, then publish publishes nothing, then a release:
    after restore-ancestry the next publish exits 0 with nothing new (the
    merge snapshot is the IP), and a real release then runs on top: v1.1.0
    is tagged and managed, with v1.0.0's ancestry still in main."""
    w, rel = _dropped_release(world, releasable=True)
    _integrate_rewrite(run, w)
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    assert _repair(run, w, "restore-ancestry", "v1.0.0", "--apply", "--yes",
                   "--expect-tip", tip, "--expect-plan", d).code == 0
    merged = _remote_refs(w)["refs/heads/main"]
    integ = w.integ()
    assert integ.ip == merged and not integ.items                  # the merge is the IP
    r = run(["publish", "--no-release", "--yes"], cwd=w.src)      # release is enabled here
    assert r.code == 0, r.stderr
    after = _remote_refs(w)["refs/heads/main"]
    # The owner kept docs/rel.md (--keep), which the tip's tree lacks, so the
    # publish fast-forwards on top of the merge snapshot.
    assert after == merged or git_run("rev-parse", f"{after}^", cwd=w.pub).strip() == merged
    (w.src / "version.py").write_text('VERSION = "1.1.0"\n')
    git_run("commit", "-qam", "version 1.1.0", cwd=w.src)
    r = run(["--release", "1.1.0", "--yes"], cwd=w.src)
    assert r.code == 0, r.stderr + r.stdout
    refs = _remote_refs(w)
    tagged = git_run("ls-remote", w.url, "refs/tags/v1.1.0^{}").split()[0]
    assert tagged == refs["refs/heads/main"]
    git_run("fetch", "-q", "--tags", "origin", cwd=w.pub)
    for older in (rel, merged):
        git_run("merge-base", "--is-ancestor", older, tagged, cwd=w.pub)   # raises if not
    ledger = pubrepo.read_remote_ledger(w.pub, "v{version}", source_root=w.src,
                                        adopt_tags=["v1.0.0"])
    assert set(ledger.managed) == {"v1.0.0", "v1.1.0"} and ledger.latest == "1.1.0"
    assert w.records_synced()                        # arrival: release (§18.11)


def test_repair_json_stdout_is_one_json_document(world, run):
    """--json: stdout is exactly one JSON object (result, still_reachable);
    the recovery and cleanup blocks go to stderr."""
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--apply", "--yes",
                "--expect-tip", tip, "--expect-plan", d, "--json")
    assert r.code == 0
    out = json.loads(r.stdout)
    assert out["result"] == "applied" and out["still_reachable"] == []
    assert "Local cleanup" in r.stderr and "Collaborator recovery" in r.stderr


def test_repair_local_advance_does_not_call_rewritten_commits_unpublished(world, run):
    """After a successful remove-path the
    commits the repair rewrote are not reported as "never published"; the
    repair's own backup ref still holds the pre-repair tip."""
    w, first = _leaky_world(world)
    old_tip = _remote_refs(w)["refs/heads/main"]
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first)
    assert r.code == 0, r.stderr
    assert "never published" not in r.stderr
    backups = git_run("for-each-ref", "--format=%(refname) %(objectname)",
                      "refs/pubrepo/repair/", cwd=w.pub).split("\n")
    assert any(line.endswith(old_tip) for line in backups if line)
    assert git_run("rev-parse", "HEAD", cwd=w.pub).strip() == _remote_refs(w)["refs/heads/main"]


def test_repair_still_names_genuinely_local_commits(world, run):
    """§4.10 still holds for commits that were never on public: a local-only
    .publish commit on top of the shown tip is named and saved, alone."""
    w, first = _leaky_world(world)
    (w.pub / "local-only.txt").write_text("never pushed\n")
    git_run("add", "local-only.txt", cwd=w.pub)
    git_run("commit", "-qm", "local experiment", cwd=w.pub)
    local = git_run("rev-parse", "HEAD", cwd=w.pub).strip()
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first)
    assert r.code == 0, r.stderr
    assert "1 local commit(s)" in r.stderr and local[:7] in r.stderr
    assert "local experiment" in r.stderr
    saved = git_run("for-each-ref", "--format=%(objectname)", "refs/pubrepo/local/", cwd=w.pub).split()
    assert local in saved


# --- R5 (§18.11), the push plan (§18.4) and parent provenance
# (§18.13) at repair's and restore-ancestry's call sites ---

def _push_plan(w):
    common = git_run("rev-parse", "--git-common-dir", cwd=w.src).strip()
    return json.loads(((w.src / common).resolve() / "pubrepo" / "push-plan.json").read_text())


def _publish_net(recorder, start, w):
    """Network git calls made in .publish since `start` (`ls-remote
    --get-url` is a local lookup)."""
    pub = w.pub.resolve()
    return [a for a, c in recorder.calls()[start:]
            if Path(c).resolve() == pub and a and a[0] in ("fetch", "push", "ls-remote")
            and "--get-url" not in a]


def test_repair_r5_mismatched_publish_clone_origin_is_2(world, run, tmp_path, git_recorder):
    """R5 before repair's and restore-ancestry's first fetch,
    including the --expect-plan rerun check: .publish's origin pointing
    elsewhere → 2 with the R5 message, no fetch or listing, nothing pushed."""
    w, first = _leaky_world(world)
    _adopt(w, "v1.0.0", first)
    before = _remote_refs(w)
    elsewhere = tmp_path / "elsewhere.git"
    git_run("init", "--bare", "-q", str(elsewhere))
    git_run("remote", "set-url", "origin", str(elsewhere), cwd=w.pub)
    start = len(git_recorder.calls())
    for argv in (["repair", "remove-path", "secret.txt", "--from", first, "--plan"],
                 ["repair", "remove-path", "secret.txt", "--from", first, "--apply", "--yes",
                  "--expect-tip", before["refs/heads/main"], "--expect-plan", "0" * 64],
                 ["repair", "restore-ancestry", "v1.0.0", "--plan"]):
        r = run(argv, cwd=w.src)
        assert r.code == 2, (argv, r.stderr)
        assert "publish clone's origin does not match [publish] remote" in r.stderr, argv
    assert _publish_net(git_recorder, start, w) == []
    assert _remote_refs(w) == before


def test_repair_push_plan_lists_public_refs_and_records(world, run):
    """§18.4 (kind=repair): the plan names every (P) update, branch last,
    to origin, and the records as (S) to the records remote, unrefused."""
    w, first = _leaky_world(world)
    _tag(w, "t1", first)
    r = _apply(run, w, "remove-path", "secret.txt", "--from", first,
               "--also-delete-ref", "refs/tags/t1")
    assert r.code == 0, r.stderr
    plan = _push_plan(w)
    assert plan["kind"] == "repair"
    assert [(e["ref"], e["remote"], e["cls"], e["refusal"]) for e in plan["entries"]] == [
        ("refs/tags/t1", "origin", "P", None), ("refs/heads/main", "origin", "P", None),
        ("refs/pubrepo/*", "origin", "S", None)]


def test_restore_ancestry_push_plan(world, run):
    """§18.4 (kind=restore_ancestry): the branch to origin, records (S)."""
    w, rel = _dropped_release(world)
    _integrate_rewrite(run, w)
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    assert _repair(run, w, "restore-ancestry", "v1.0.0", "--apply", "--yes",
                   "--expect-tip", tip, "--expect-plan", d).code == 0
    plan = _push_plan(w)
    assert plan["kind"] == "restore_ancestry"
    assert [(e["ref"], e["remote"], e["cls"]) for e in plan["entries"]] == [
        ("refs/heads/main", "origin", "P"), ("refs/pubrepo/*", "origin", "S")]


def _inject_foreign_parent(monkeypatch, w):
    """The fault seam of push_targets::provenance_assertion_refuses_foreign_parent:
    every provenance check also sees a parentless commit no fetch produced."""
    tip = _remote_refs(w)["refs/heads/main"]
    git_run("fetch", "-q", "origin", cwd=w.pub)
    foreign = git_run("commit-tree", f"{tip}^{{tree}}", "-m", "foreign", cwd=w.pub).strip()
    real = pubrepo.assert_parent_provenance
    monkeypatch.setattr(pubrepo, "assert_parent_provenance",
                        lambda d, parents, **kw: real(d, [*parents, foreign], **kw))
    return foreign


def test_provenance_refuses_foreign_parent_in_repair(world, run, monkeypatch):
    """push_targets::provenance_assertion_refuses_foreign_parent, sync half
    (§18.13): a parent outside this run's fetched history at repair's
    rewrite commit-tree, at a rewind's ref move, and at restore-ancestry's
    merge → 2 with the provenance wording; nothing pushed, no repair intent."""
    w, first = _leaky_world(world)
    r = _repair(run, w, "remove-path", "secret.txt", "--from", first, "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    second = w.snapshot(record=True)
    r = _repair(run, w, "rewind", first, "--plan")
    rd, rtip = _digest(r), _remote_refs(w)["refs/heads/main"]
    before = _remote_refs(w)
    _inject_foreign_parent(monkeypatch, w)
    for argv in (["remove-path", "secret.txt", "--from", first, "--apply", "--yes",
                  "--expect-tip", tip, "--expect-plan", d],
                 ["rewind", first, "--apply", "--yes", "--expect-tip", rtip,
                  "--expect-plan", rd]):
        r = _repair(run, w, *argv)
        assert r.code == 2, (argv, r.stderr + r.stdout)
        assert "may have been cloned from another repository" in r.stderr, argv
    assert _remote_refs(w) == before and second == before["refs/heads/main"]
    assert not [x for x in _records(w, "refs/pubrepo/repairs/") if x.endswith("/applied")]


def test_provenance_refuses_foreign_parent_in_restore_ancestry(world, run, monkeypatch):
    """The same at restore-ancestry's merge snapshot_commit (R from this
    run's ledger): 2, the public branch unchanged."""
    w, rel = _dropped_release(world)
    _integrate_rewrite(run, w)
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--plan")
    d, tip = _digest(r), _remote_refs(w)["refs/heads/main"]
    _inject_foreign_parent(monkeypatch, w)
    r = _repair(run, w, "restore-ancestry", "v1.0.0", "--apply", "--yes",
                "--expect-tip", tip, "--expect-plan", d)
    assert r.code == 2 and "may have been cloned from another repository" in r.stderr
    assert _remote_refs(w)["refs/heads/main"] == tip
