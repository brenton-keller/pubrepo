"""Identity, write sets, integration point, divergence (design §3.1,
INV-3/4/5/11, §4.7, §4.8, §6.9).

Module-level tests. Snapshots are built here the way pubrepo has always
built them (frozen message, tree = Π(σ)), and pull records are built by a
helper ("pull records constructed by a test helper"), so the identity
layer is tested on its own. The exit-code halves (5, 8, 0) are
core's state → exit mapping and are tested with publish and pull.
"""
import json
import subprocess
from pathlib import Path

import pytest

import pubrepo
from conftest import git_run
from ff_fixtures import CONFIG_REL, _git



# The `world` fixture lives in tests/ff_fixtures.py (sync fixtures).


# --- solo states ---

def test_empty_remote_is_s6(world):
    w = world()
    integ = w.integ()
    assert integ.state == "S6" and integ.tip is None and integ.ip is None


def test_solo_up_to_date_then_pending(world):
    w = world()
    snap = w.snapshot()
    integ = w.integ()
    assert (integ.state, integ.ip, integ.tip) == ("S0", snap, snap)
    assert integ.is_ours(snap) and not integ.items and not integ.unpulled
    w.commit_source({"a.py": "a = 2\n"})
    integ = w.integ()
    assert integ.state == "S1" and integ.ip == snap
    assert integ.accounted("a.py", w.blob(snap, "a.py"))           # (i) LW


def test_solo_reinit_deletion_is_verified(world):
    """AC11: no records; our clean delta verifies, so a source deletion
    publishes with no item."""
    w = world()
    snap = w.snapshot()
    w.commit_source({"docs/guide.md": None})
    integ = w.integ()
    assert integ.state == "S1" and not integ.items
    assert integ.lw("docs/guide.md") == w.blob(snap, "docs/guide.md")
    assert integ.accounted("docs/guide.md", w.blob(snap, "docs/guide.md"))


# --- foreign commits ---

def test_foreign_commit_is_unpulled_range(world):
    w = world()
    snap = w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# Guide\nedited by collab\n"})
    integ = w.integ()
    assert integ.state == "S2" and integ.ip == snap and integ.tip == f
    assert [c.sha for c in integ.unpulled] == [f]
    assert integ.unpulled[0].author == "Collab Orator <collab@example.invalid>"
    assert integ.unpulled[0].paths == ("docs/guide.md",)
    assert not integ.is_ours(f)
    w.commit_source({"a.py": "a = 3\n"})
    assert w.integ().state == "S3"


def test_reinit_over_foreign_history_never_ours(world):
    """AC3: a public history with no snapshot of ours → everything is
    unpulled (S2); nothing is ours, so nothing can be deleted as ours."""
    w = world()
    f1 = w.collab_commit({"a.py": "theirs = 1\n"})
    f2 = w.collab_commit({"notes.txt": "n\n"})
    integ = w.integ()
    assert integ.state == "S3" and integ.ip is None       # our a.py differs: changes pending
    assert [c.sha for c in integ.unpulled] == [f2, f1]
    assert not integ.written_ever("a.py") and integ.lw("a.py") is None


# --- §3.1 trailer verification and baseline items ---

def test_legacy_dirty_snapshot_untracked_file_is_baseline_never_foreign(world):
    """AC13: a legacy ` (dirty)` snapshot carrying an untracked included file.
    The snapshot is ours; the unverifiable path is a baseline item once the
    owner's copy differs from public."""
    w = world()
    (w.src / "docs" / "draft.md").write_text("untracked draft\n")
    snap = w.snapshot(extra={"docs/draft.md": "untracked draft\n"}, dirty=True)
    integ = w.integ()
    assert integ.is_ours(snap)
    assert integ.baseline_items_for(snap) == ["docs/draft.md"]
    assert integ.state == "S0" and not integ.items          # exempt: tip == Π (o)
    (w.src / "docs" / "draft.md").write_text("changed draft\n")
    integ = w.integ()
    assert integ.state == "S9"
    assert [(i.path, i.cls, i.origin, i.snapshot) for i in integ.items] == [
        ("docs/draft.md", "baseline_managed", "baseline", snap)]


def test_dirty_tracked_publish_is_baseline_not_written(world):
    """§3.1 table row 1: a legacy snapshot that published a dirty edit of a
    tracked managed file does not verify (S.tree[P] ≠ Π(σ)[P]); the path is
    a baseline item once the owner's content differs from public, and LW is
    unknown rather than the dirty blob."""
    w = world()
    snap = w.snapshot(extra={"a.py": "a = 'dirty, never committed'\n"}, dirty=True)
    integ = w.integ()
    assert integ.baseline_items_for(snap) == ["a.py"]
    assert integ.lw("a.py") is None and not integ.written_ever("a.py")
    assert integ.state == "S9"
    assert [(i.path, i.cls) for i in integ.items] == [("a.py", "baseline_managed")]


def test_dogfood_shaped_legacy_history_has_no_baseline_items(world):
    """The 7-snapshot dogfood shape (validation, synthetic): clean
    legacy snapshots, some marked dirty for tracked-but-clean files, all
    verify with zero baseline items."""
    w = world()
    snaps = []
    for i in range(7):
        w.commit_source({"a.py": f"a = {i}\n", f"docs/n{i}.md": f"note {i}\n"})
        snaps.append(w.snapshot(dirty=(i % 3 == 0)))
    w.commit_source({"docs/n0.md": None})
    snaps.append(w.snapshot())
    integ = w.integ()
    assert integ.state == "S0"
    assert all(integ.is_ours(s) and integ.baseline_items_for(s) == [] for s in snaps)


@pytest.mark.parametrize("variant", ["a_adds_unmanaged", "b_keep_edit", "c_deletes_managed"])
def test_collab_amend_variants(world, variant):
    """identity::collab_amend_variants (AC14 (a), (b), (c)): a collaborator amends our recorded tip, keeping the trailer
    → S7 (our recorded snapshot is unreachable) and a baseline item names
    the path; nothing is treated as ours silently. (b): the amend edits a
    keep file; the amended blob differs from the owner's `.publish/<P>`, so
    it does not verify (§3.1 keep row)."""
    keep = variant == "b_keep_edit"
    w = world(keep=["LICENSE"]) if keep else world()
    extra = {"LICENSE": "owner licence text\n"} if keep else None
    w.snapshot(record=True, extra=extra)
    w.commit_source({"a.py": "a = 11\n"})
    snap = w.snapshot(record=True, extra=extra)
    clone = Path(str(w.pub) + "-collab")
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    if variant == "a_adds_unmanaged":
        (clone / "extra.bin").write_text("collab extra\n")
        path = "extra.bin"
    elif keep:
        (clone / "LICENSE").write_text("collab licence edit\n")
        path = "LICENSE"
    else:
        (clone / "docs" / "guide.md").unlink()
        path = "docs/guide.md"
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-q", "--amend", "--no-edit", cwd=clone)
    amended = git_run("rev-parse", "HEAD", cwd=clone).strip()
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=clone)
    integ = w.integ()
    assert integ.state == "S7" and integ.missing_snapshots == [snap]
    assert path in integ.baseline_items_for(amended)
    if variant == "a_adds_unmanaged":
        assert [i.path for i in integ.baseline_items] == ["extra.bin"]
        assert integ.baseline_items[0].options == ("leave", "remove", "restore")
    if keep:
        assert (w.pub / "LICENSE").read_text() == "owner licence text\n"
        assert integ.lw("LICENSE") is None


def test_unverified_deletion_of_carried_file_always_surfaces(world):
    """AC14b2 (design §3.1 step 3, S3 half): a collaborator amends our
    recorded tip, keeping the trailer, and deletes a carried public-only
    file. tip and Π are both absent for it, which must not exempt it: an
    unmanaged baseline item surfaces with leave/remove/restore, and its
    base is the deleted blob (what `restore` re-adds)."""
    w = world()
    w.snapshot(record=True)
    f = w.collab_commit({"notes.txt": "carried public-only\n"})
    w.pull_record([], through=f)
    w.commit_source({"a.py": "a = 12\n"})
    snap = w.snapshot(carry=True, record=True)
    carried_blob = w.blob(snap, "notes.txt")
    clone = w.collab.path
    git_run("fetch", "-q", "origin", cwd=clone)
    git_run("reset", "-q", "--hard", "origin/main", cwd=clone)
    git_run("rm", "-q", "notes.txt", cwd=clone)
    git_run("commit", "-q", "--amend", "--no-edit", cwd=clone)
    amended = git_run("rev-parse", "HEAD", cwd=clone).strip()
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=clone)
    integ = w.integ()
    assert "notes.txt" in integ.baseline_items_for(amended)
    [item] = [i for i in integ.baseline_items if i.path == "notes.txt"]
    assert (item.cls, item.theirs, item.base, item.options) == (
        "baseline_unmanaged", None, carried_blob, ("leave", "remove", "restore"))
    assert "notes.txt" in [i.path for i in integ.items]
    # A recorded decision for (path, absent) settles it.
    w.pull_record([{"path": "notes.txt", "decision": "leave", "theirs": "absent",
                    "managed_at_decision": False}], through=amended)
    integ = w.integ()
    assert "notes.txt" not in [i.path for i in integ.items]
    assert [i.path for i in integ.baseline_items] == ["notes.txt"]


# --- S10 (§4.8) ---

def test_owner_amends_source_after_publish_is_s10b_rewritten(world):
    """AC15 (rewritten): σ still resolves but diverged from HEAD."""
    w = world()
    snap = w.snapshot()
    git_run("commit", "-q", "--amend", "-m", "init (amended)", cwd=w.src)
    integ = w.integ()
    assert integ.state == "S10b" and integ.ip is None
    assert integ.divergence.kind == "diverged" and integ.divergence.snapshot == snap
    assert integ.divergence.accept_flag == f"--accept-source-divergence={snap}"
    accepted = w.integ(accept=snap)
    assert accepted.ip == snap and accepted.state in ("S0", "S1")
    sigma = integ.divergence.sigma
    assert w.integ(accept=sigma).ip == snap                         # σ form too


def test_publish_from_other_branch_is_s10b_naming_it(world):
    """AC15 (other branch)."""
    w = world()
    git_run("checkout", "-q", "-b", "feat", cwd=w.src)
    w.commit_source({"a.py": "feat = 1\n"})
    w.snapshot()
    git_run("checkout", "-q", "main", cwd=w.src)
    w.commit_source({"docs/guide.md": "# Guide\nmain work\n"})
    integ = w.integ()
    assert integ.state == "S10b"
    assert integ.divergence.branches == ("feat",)


def test_source_behind_upstream_is_s10a_update_it(world):
    """S10a, σ reachable from HEAD's upstream: no branches named ("update it")."""
    w = world()
    w.commit_source({"a.py": "a = 5\n"})
    w.snapshot()
    git_run("push", "-q", "origin", "main", cwd=w.src)
    git_run("reset", "-q", "--hard", "HEAD~1", cwd=w.src)
    integ = w.integ()
    assert integ.state == "S10a" and integ.divergence.kind == "behind"
    assert integ.divergence.branches == ()


def test_published_from_branch_ahead_is_s10a_naming_it(world):
    """AC15: HEAD is an ancestor of σ on another branch and σ is
    not reachable from HEAD's upstream → S10a naming that branch."""
    w = world()
    git_run("checkout", "-q", "-b", "feat", cwd=w.src)
    w.commit_source({"a.py": "feat = 1\n"})
    w.snapshot()
    git_run("checkout", "-q", "main", cwd=w.src)
    integ = w.integ()
    assert integ.state == "S10a" and integ.divergence.kind == "behind"
    assert integ.divergence.branches == ("feat",)


def test_gcd_sigma_with_record_is_s10b_gone_even_with_warm_cache(world):
    """AC76 plus the verdict cache: a warm verdict cache from before the
    gc never turns the snapshot back into "verified ours"."""
    w = world()
    w.commit_source({"a.py": "a = 7\n"})
    snap = w.snapshot(record=True)
    assert w.integ().state == "S0"                                  # a run before the gc
    git_run("commit", "-q", "--amend", "-m", "rewritten", cwd=w.src)
    git_run("reflog", "expire", "--expire=now", "--all", cwd=w.src)
    git_run("gc", "-q", "--prune=now", cwd=w.src)
    integ = w.integ()
    assert integ.state == "S10b" and integ.divergence.kind == "gone"
    assert integ.divergence.snapshot == snap and integ.divergence.branches == ()


def test_feat_snapshot_plus_foreign_is_composite_then_accept_lists_only_f(world):
    """AC77: snapshot from `feat`, collaborator F, owner on main → S10b+S2;
    with the flag the range is only F."""
    w = world()
    git_run("checkout", "-q", "-b", "feat", cwd=w.src)
    w.commit_source({"a.py": "feat = 2\n"})
    snap = w.snapshot()
    git_run("checkout", "-q", "main", cwd=w.src)
    w.commit_source({"docs/other.md": "main work\n"})
    f = w.collab_commit({"docs/guide.md": "# Guide\ncollab\n"})
    integ = w.integ()
    assert integ.state == "S10b+S2" and integ.divergence.snapshot == snap
    accepted = w.integ(accept=snap)
    assert accepted.ip == snap and [c.sha for c in accepted.unpulled] == [f]


def test_unresolvable_sigma_without_record_is_foreign(world):
    w = world()
    snap = w.snapshot()
    msg = git_run("log", "-1", "--format=%B", snap, cwd=w.pub)
    clone = Path(str(w.pub) + "-c")
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    (clone / "x.txt").write_text("x\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-q", "-m", msg.replace(f"Source: {w.head()[:7]}", "Source: abcdef0"),
            cwd=clone)
    git_run("push", "-q", "origin", "HEAD:main", cwd=clone)
    ident = pubrepo.classify_commit(w.src, w.pub, w.tip())
    assert ident.kind == "foreign" and ident.trailer == "abcdef0"


def test_identity_rederived_after_records_arrive(world, tmp_path):
    """A run before a records sync classifies a snapshot from another
    clone as foreign (σ unknown here); after the sync it is ours. Nothing
    from the first run is reused."""
    w = world()
    w.snapshot(record=False)
    other = tmp_path / "other-src"
    git_run("clone", "-q", git_run("remote", "get-url", "origin", cwd=w.src).strip(), str(other))
    git_run("config", "user.name", "Owner", cwd=other)
    git_run("config", "user.email", "owner@example.invalid", cwd=other)
    (other / "a.py").write_text("from other clone\n")
    git_run("commit", "-qam", "unpushed source", cwd=other)
    sigma = git_run("rev-parse", "HEAD", cwd=other).strip()
    # A snapshot naming σ from the other clone, which this source lacks.
    (w.pub / "a.py").write_text("from other clone\n")
    git_run("commit", "-qam", f"publish: x\n\nSource: {sigma[:7]}\n", cwd=w.pub)
    snap = git_run("rev-parse", "HEAD", cwd=w.pub).strip()
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=w.pub)
    assert w.integ().is_ours(snap) is False
    pubrepo.write_snapshot_record(
        other, snapshot_sha=snap, destination=pubrepo.destination_identity(w.config()),
        sigma_full=sigma, written={"a.py": w.blob(snap, "a.py")}, nonderivable=[],
        keep=[], config_path=CONFIG_REL, fingerprint="f" * 64)
    assert pubrepo.records_push_pending(other, w.config()).ok
    assert pubrepo.records_fetch(w.src, w.config()).ok
    ident = pubrepo.classify_commit(w.src, w.pub, snap)
    assert ident.kind == "ours_unresolved" and ident.record_ref == f"refs/pubrepo/snap/{snap}"
    git_run("push", "-q", "origin", "HEAD:refs/heads/side", cwd=other)
    integ = w.integ(fetch_sigma=True)                       # §4.8 fetch of the full σ
    assert integ.is_ours(snap)
    assert pubrepo.classify_commit(w.src, w.pub, snap).kind == "ours_record"


# --- accounted-for (ii), decision_for ---

def test_accounted_ii_after_later_source_edit(world):
    """AC87: accept F's change to P via a pull record, publish (no delta on
    P), then edit P in source: P stays accounted for by (ii)."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# Guide\nfrom collab\n"})
    theirs = w.blob(f, "docs/guide.md")
    w.pull_record([{"path": "docs/guide.md", "decision": "accept", "theirs": theirs}],
                  through=f, files={"docs/guide.md": "# Guide\nfrom collab\n"})
    snap2 = w.snapshot(carry=True)
    assert w.integ().state == "S0"
    w.commit_source({"docs/guide.md": "# Guide\nfrom collab\nand owner\n"})
    integ = w.integ()
    assert integ.state == "S1" and integ.ip == snap2
    assert integ.accounted("docs/guide.md", theirs)
    assert integ.decision_for("docs/guide.md", theirs).decision == "accept"


def test_newest_deciding_record_wins(world):
    """AC90: pull 1 decides P; pull 2 decides only Q; P stays accounted by
    pull 1, the newest record that decided P."""
    w = world()
    w.snapshot()
    f1 = w.collab_commit({"docs/guide.md": "# Guide\nv2\n"})
    p_blob = w.blob(f1, "docs/guide.md")
    w.pull_record([{"path": "docs/guide.md", "decision": "accept", "theirs": p_blob}],
                  through=f1, files={"docs/guide.md": "# Guide\nv2\n"})
    w.snapshot(carry=True)
    f2 = w.collab_commit({"docs/q.md": "q\n"})
    w.pull_record([{"path": "docs/q.md", "decision": "accept",
                    "theirs": w.blob(f2, "docs/q.md")}], through=f2,
                  files={"docs/q.md": "q\n"})
    w.commit_source({"docs/guide.md": "# Guide\nv3 owner\n"})
    integ = w.integ()
    assert integ.ip == f2 and integ.state == "S1"
    assert integ.accounted("docs/guide.md", p_blob)
    assert integ.decision_for("docs/guide.md", p_blob).record != w.head()


def test_decision_for_is_keyed_on_theirs_absent_equals_absent(world):
    """decision_for keys on (path, blob): an older blob's decision does not apply."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"notes.txt": "n\n"})
    nb = w.blob(f, "notes.txt")
    rec = w.pull_record([{"path": "notes.txt", "decision": "leave", "theirs": nb,
                          "managed_at_decision": False},
                         {"path": "gone.txt", "decision": "remove", "theirs": "absent",
                          "managed_at_decision": False}], through=f)
    integ = w.integ()
    d = integ.decision_for("notes.txt", nb)
    assert (d.decision, d.theirs, d.record, d.cls) == ("leave", nb, rec, "C1")
    assert integ.decision_for("notes.txt", "0" * 40) is None
    assert integ.decision_for("gone.txt", None).decision == "remove"
    assert integ.decision_for("gone.txt", nb) is None


def test_unreadable_pull_record_body_warns_and_keeps_trailers(world, capsys):
    w = world()
    w.snapshot()
    f = w.collab_commit({"notes.txt": "n\n"})
    git_run("commit", "-q", "--allow-empty", "-m",
            f"pull\n\n```json\n{{broken\n```\n\nPubrepo-Pulled-Through: {f}", cwd=w.src)
    integ = w.integ()
    assert integ.ip == f
    assert "unreadable decision body" in capsys.readouterr().err


# --- INV-4 (c) across --no-ff merges ---

def _topic_pull_merged(w, decision):
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# Guide\ncollab topic\n"})
    theirs = w.blob(f, "docs/guide.md")
    git_run("checkout", "-q", "-b", "topic", cwd=w.src)
    files = {"docs/guide.md": "# Guide\ncollab topic\n"} if decision == "accept" else None
    w.pull_record([{"path": "docs/guide.md", "decision": decision, "theirs": theirs}],
                  through=f, files=files)
    git_run("checkout", "-q", "main", cwd=w.src)
    git_run("merge", "-q", "--no-ff", "-m", "merge topic", "topic", cwd=w.src)
    return f, theirs


def test_pull_on_topic_merge_no_ff_accept_only(world):
    """The IP is found through a pull record on the merged topic."""
    w = world()
    f, _ = _topic_pull_merged(w, "accept")
    integ = w.integ()
    assert integ.ip == f and integ.tip == f and integ.state == "S0" and not integ.items


def test_merge_no_ff_decline_reasks(world):
    """A `decline` on the topic is not a first-parent decision,
    so the path is unaccounted (S9), explicitly."""
    w = world()
    f, theirs = _topic_pull_merged(w, "decline")
    integ = w.integ()
    assert integ.ip == f and integ.state == "S9"
    assert [(i.path, i.cls, i.theirs) for i in integ.items] == [
        ("docs/guide.md", "unaccounted", theirs)]
    assert integ.decision_for("docs/guide.md", theirs) is None


# --- destinations (§5.14) ---

def test_two_configs_gate_independently(world, public_remote):
    """Config A has an unpulled range (S2); config B publishes."""
    w = world()
    url_b = public_remote()
    (w.src / ".publish-b.toml").write_text(
        f'[publish]\nremote = "{url_b}"\ninclude = ["a.py"]\ndir = ".publish-b"\n')
    git_run("add", ".publish-b.toml", cwd=w.src)
    git_run("commit", "-qm", "second target", cwd=w.src)
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\ncollab\n"})
    assert w.integ().state == "S2"
    pub_b = w.src / ".publish-b"
    git_run("clone", "-q", url_b, str(pub_b))
    cfg_b = pubrepo.load_publish_config(w.src / ".publish-b.toml")
    proj_b = pubrepo.build_projection(w.src, pub_b, cfg_b)
    integ_b = pubrepo.compute_integration(w.src, pub_b, cfg_b, None, proj_b,
                                          accept_divergence=None, fetch_sigma=False,
                                          config_relpath=".publish-b.toml")
    assert integ_b.state == "S6"


def test_pull_record_for_destination_a_accounts_nothing_for_b(world, public_remote):
    """`Pubrepo-Destination` filters (ii), decision_for
    and INV-4 (c); a record without the trailer applies to every destination."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# Guide\nX\n"})
    theirs = w.blob(f, "docs/guide.md")
    other = ("github.com/acme/elsewhere", "main")
    w.pull_record([{"path": "docs/guide.md", "decision": "decline", "theirs": theirs}],
                  through=f, destination=other)
    integ = w.integ()
    assert integ.ip != f and integ.state == "S2"
    assert integ.decision_for("docs/guide.md", theirs) is None
    w.pull_record([{"path": "docs/guide.md", "decision": "decline", "theirs": theirs}],
                  through=f, destination=None)
    integ = w.integ()
    assert integ.ip == f
    assert integ.decision_for("docs/guide.md", theirs).decision == "decline"


def test_decided_key_is_never_w_or_lw(world):
    """Sync half (design §3.1): a decision-authorized
    restore of an unmanaged path recorded under `decided` is not a write.
    With the record's `written` empty for it, LW stays unknown, the path is
    never "written ever", and it is not deleted as "removed from the managed
    set" on a later publish (helper record; the real pull is in test_ff_pull)."""
    w = world()
    w.snapshot(record=True)
    f = w.collab_commit({"notes.txt": "carried\n"})
    w.pull_record([], through=f)
    w.commit_source({"a.py": "a = 3\n"})
    snap = w.snapshot(carry=True)
    blob = w.blob(snap, "notes.txt")
    pubrepo.write_snapshot_record(
        w.src, snapshot_sha=snap, destination=pubrepo.destination_identity(w.config()),
        sigma_full=w.head(), written={"a.py": w.blob(snap, "a.py")}, nonderivable=[], keep=[],
        config_path=CONFIG_REL, fingerprint="f" * 64,
        decided={"notes.txt": {"decision": "restore", "blob": blob, "record": "c" * 40}})
    rec = json.loads(git_run("cat-file", "blob", f"refs/pubrepo/snap/{snap}:record.json",
                             cwd=w.src))
    assert rec["decided"]["notes.txt"]["decision"] == "restore"
    integ = w.integ()
    assert integ.lw("notes.txt") is None and not integ.written_ever("notes.txt")
    assert integ.state == "S0" and not integ.items                 # carried, not deleted
    plain = pubrepo.write_snapshot_record(
        w.src, snapshot_sha="9" * 40, destination=("x", "main"), sigma_full=w.head(),
        written={}, nonderivable=[], keep=[], config_path=None, fingerprint="f" * 64)
    assert "decided" not in json.loads(git_run("cat-file", "blob", f"{plain}:record.json",
                                               cwd=w.src))


def test_pull_records_is_the_destination_filter(world):
    """pull_records(..., destination=) is the only reader;
    records for another destination are skipped, trailer-less records
    apply to every destination."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"notes.txt": "n\n"})
    mine = w.pull_record([], through=f)
    theirs = w.pull_record([], through=f, destination=("github.com/acme/elsewhere", "main"))
    legacy = w.pull_record([], through=f, destination=None)
    dest = pubrepo.destination_identity(w.config())
    got = [r.sha for r in pubrepo.pull_records(w.src, destination=dest, first_parent=False)]
    assert got == [legacy, mine]
    other = [r.sha for r in pubrepo.pull_records(w.src, destination=("github.com/acme/elsewhere",
                                                                     "main"))]
    assert other == [legacy, theirs]
    import inspect
    p = inspect.signature(pubrepo.pull_records).parameters["destination"]
    assert p.kind is p.KEYWORD_ONLY and p.default is p.empty
    assert not hasattr(pubrepo._IdentityView, "_for_us")


def test_config_relpath_none_makes_trailer_deltas_baseline(world):
    """A config outside the source tree gives no Π(σ)."""
    w = world()
    snap = w.snapshot()
    integ = w.integ(config_relpath=None)
    assert integ.is_ours(snap)
    assert set(integ.baseline_items_for(snap)) == {"a.py", "docs/guide.md"}


def test_config_relpath_is_required_keyword():
    import inspect
    p = inspect.signature(pubrepo.compute_integration).parameters["config_relpath"]
    assert p.kind is p.KEYWORD_ONLY and p.default is p.empty


# --- copied trailers (AC95) and the legacy base case ---

def test_cherry_picked_trailer_is_not_an_ip(world):
    """AC95: P1 → F1 → C (cherry-pick of our snapshot, trailer kept, re-adds
    docs/x.md) → S2, C listed as a copied trailer, docs/x.md is not ours."""
    w = world()
    w.commit_source({"docs/x.md": "x\n"})
    p0 = w.snapshot()
    w.commit_source({"docs/x.md": None})
    p1 = w.snapshot()
    f1 = w.collab_commit({"README.md": "readme by collab\n"})
    clone = w.collab.path
    git_run("cherry-pick", "--allow-empty", "-x", p0, cwd=clone)
    git_run("push", "-q", "origin", "HEAD:main", cwd=clone)
    c = git_run("rev-parse", "HEAD", cwd=clone).strip()
    integ = w.integ()
    assert integ.ip == p1 and integ.state == "S2"
    assert integ.copied_trailer == [c] and not integ.is_ours(c)
    assert {u.sha for u in integ.unpulled} == {f1, c}
    assert integ.lw("docs/x.md") == "deleted"


def test_legacy_snapshot_on_host_initial_commit_still_finds_ip(world):
    """AC95 legacy variant: the first snapshot is parented on a
    host-created initial commit with no ours-snapshot in its history."""
    w = world()
    host = w.collab_commit({"README.md": "created by host\n"}, "Initial commit")
    git_run("fetch", "-q", "origin", cwd=w.pub)
    git_run("checkout", "-q", "-B", "main", "origin/main", cwd=w.pub)
    snap = w.snapshot(carry=True)
    integ = w.integ()
    assert integ.ip == snap and integ.is_ours(snap)
    assert git_run("rev-parse", f"{snap}^", cwd=w.pub).strip() == host


def test_our_snapshot_as_second_parent(world):
    """M = merge(F, P2). The first-parent walk finds P1;
    P2's changes are accounted for by (o), and P2's writes still count."""
    w = world()
    p1 = w.snapshot()
    f = w.collab_commit({"notes.txt": "collab\n"})
    clone = w.collab.path
    git_run("reset", "-q", "--hard", p1, cwd=w.pub)
    git_run("push", "-q", "-f", "origin", f"{p1}:refs/heads/main", cwd=w.pub)  # rewind for setup
    w.commit_source({"a.py": "a = 42\n"})
    p2 = w.snapshot(carry=True)
    git_run("fetch", "-q", "origin", cwd=clone)
    git_run("checkout", "-q", "-B", "main", f, cwd=clone)
    git_run("merge", "-q", "--no-ff", "-m", "merge", "origin/main", cwd=clone)
    git_run("push", "-q", "origin", "HEAD:main", cwd=clone)
    m = git_run("rev-parse", "HEAD", cwd=clone).strip()
    integ = w.integ()
    assert integ.tip == m and integ.ip == p1
    assert {u.sha for u in integ.unpulled} == {m, f, p2}
    assert integ.lw("a.py") == w.blob(p2, "a.py")
    assert integ.accounted("a.py", w.blob(m, "a.py"))
    assert not integ.items


# --- INV-3 items: C8b, add/add, unmanaged baseline decisions ---

def test_dropped_from_include_then_collab_edit_is_c8b_until_decided(world):
    """INV-3 (b) / AC80 detection: written by us, dropped from include,
    modified by them → C8b; a `leave` decision carries it."""
    w = world()
    w.snapshot()
    (w.src / CONFIG_REL).write_text((w.src / CONFIG_REL).read_text().replace(
        '"a.py", "docs"', '"a.py"'))
    git_run("commit", "-qam", "drop docs", cwd=w.src)
    f = w.collab_commit({"docs/guide.md": "# Guide\ncollab\n"})
    theirs = w.blob(f, "docs/guide.md")
    w.pull_record([], through=f)
    integ = w.integ()
    assert integ.ip == f and integ.state == "S9"
    assert [(i.path, i.cls) for i in integ.items] == [("docs/guide.md", "C8b")]
    w.pull_record([{"path": "docs/guide.md", "decision": "leave", "theirs": theirs,
                    "managed_at_decision": False}], through=f)
    integ = w.integ()
    assert integ.state == "S0" and not integ.items       # carried; nothing of ours pending


def test_dropped_from_include_unchanged_is_deletion_not_item(world):
    """INV-3 (b): tip == LW → publish deletes it (named); no item."""
    w = world()
    snap = w.snapshot()
    (w.src / CONFIG_REL).write_text((w.src / CONFIG_REL).read_text().replace(
        '"a.py", "docs"', '"a.py"'))
    git_run("commit", "-qam", "drop docs", cwd=w.src)
    integ = w.integ()
    assert not integ.items and integ.lw("docs/guide.md") == w.blob(snap, "docs/guide.md")
    assert integ.written_ever("docs/guide.md")


def test_add_add_after_leave_then_adopt(world):
    """AC12 detection: a public-only file, then we include and add our own
    version → add/add item."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/new.md": "theirs\n"})
    w.pull_record([], through=f)
    w.commit_source({"docs/new.md": "ours\n"})
    integ = w.integ()
    assert integ.ip == f and integ.state == "S9"
    assert [(i.path, i.cls) for i in integ.items] == [("docs/new.md", "add_add")]


def test_new_file_absent_publicly_is_accounted(world):
    w = world()
    w.snapshot()
    w.commit_source({"docs/fresh.md": "new\n"})
    integ = w.integ()
    assert integ.state == "S1" and not integ.items
    assert integ.accounted("docs/fresh.md", None)


def test_collab_deletes_file_we_wrote_is_item(world):
    """A managed file we wrote, deleted publicly, still in our projection:
    unaccounted (never resurrected silently)."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": None})
    w.pull_record([], through=f)
    integ = w.integ()
    assert [(i.path, i.cls, i.theirs) for i in integ.items] == [
        ("docs/guide.md", "unaccounted", None)]


# --- S7 via pull records ---

def test_rewritten_history_after_pull_is_s7(world):
    """F1/F2: a pulled collaborator commit is rewritten away → S7."""
    w = world()
    snap = w.snapshot()
    f = w.collab_commit({"notes.txt": "n\n"})
    w.pull_record([], through=f)
    assert w.integ().ip == f
    clone = w.collab.path
    git_run("reset", "-q", "--hard", snap, cwd=clone)
    (clone / "other.txt").write_text("o\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "rewrite", cwd=clone)
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=clone)
    assert w.integ().state == "S7"


# --- S12 (§6.9) ---

def _applied_repair(w, removed=(), removed_blobs=(), mapping=None):
    pubrepo.write_record(w.src, "refs/pubrepo/repairs/" + "d" * 64 + "/applied",
                         {"removed": [list(x) for x in removed],
                          "removed_blobs": list(removed_blobs), "map": mapping or {},
                          "left_reachable": []})


def test_reintroduced_blob_at_new_path_is_s12(world):
    w = world()
    w.snapshot()
    secret = "s" * 80 + "\n"
    f = w.collab_commit({"copy/leak.txt": secret})
    leaked = w.blob(f, "copy/leak.txt")
    w.pull_record([], through=f)
    _applied_repair(w, removed=[("old/leak.txt", leaked)], removed_blobs=[leaked])
    integ = w.integ()
    assert integ.state == "S12"
    assert [(h.path, h.blob, h.commit) for h in integ.reintroduced] == [
        ("copy/leak.txt", leaked, None)]


def test_small_blob_matches_only_exact_path(world):
    w = world()
    w.snapshot()
    f = w.collab_commit({"copy/tiny.txt": "tiny\n", "orig/tiny.txt": "tiny\n"})
    tiny = w.blob(f, "copy/tiny.txt")
    w.pull_record([], through=f)
    _applied_repair(w, removed=[("orig/tiny.txt", tiny)], removed_blobs=[tiny])
    integ = w.integ()
    assert [h.path for h in integ.reintroduced] == ["orig/tiny.txt"]


def test_no_applied_repair_no_s12(world):
    w = world()
    w.snapshot()
    pubrepo.write_record(w.src, "refs/pubrepo/repairs/" + "e" * 64 + "/intent",
                         {"removed_blobs": [w.blob(w.tip(), "a.py")]})
    assert w.integ().state == "S0"


def test_repair_map_makes_new_sha_ours(world):
    """§6.4: a commit re-SHA'd by an applied repair is ours through the map."""
    w = world()
    snap = w.snapshot(record=True)
    git_run("commit", "-q", "--amend", "-m",
            git_run("log", "-1", "--format=%B", snap, cwd=w.pub) + "\nrepaired\n", cwd=w.pub)
    new = git_run("rev-parse", "HEAD", cwd=w.pub).strip()
    _applied_repair(w, mapping={snap: new})
    ident = pubrepo.classify_commit(w.src, w.pub, new)
    assert (ident.kind, ident.via_repair) == ("ours_record", snap)


# --- shallow (AC16) ---

def test_shallow_source_refused(world, tmp_path):
    w = world()
    w.commit_source({"a.py": "a = 9\n"})
    shallow = tmp_path / "shallow-src"
    git_run("clone", "-q", "--depth", "1", w.src.as_uri(), str(shallow))
    with pytest.raises(pubrepo.ConfigError, match="full source history required"):
        pubrepo.check_shallow(shallow, w.pub)


def test_shallow_publish_is_unshallowed(world, tmp_path):
    w = world()
    w.snapshot()
    w.commit_source({"a.py": "a = 10\n"})
    w.snapshot()
    shallow_pub = tmp_path / "shallow-pub"
    git_run("clone", "-q", "--depth", "1", w.url, str(shallow_pub))
    pubrepo.check_shallow(w.src, shallow_pub)
    assert git_run("rev-parse", "--is-shallow-repository", cwd=shallow_pub).strip() == "false"


# --- verdict cache ---

def test_verdict_cache_holds_only_pi_sigma_verdicts(world):
    w = world()
    snap = w.snapshot()
    w.integ()
    data = json.loads((w.pub / ".git" / "pubrepo" / "verdicts.json").read_text())
    assert data and all(k.startswith(snap) and k.endswith(f":{pubrepo.ENGINE_VERSION}")
                        for k in data)
    assert all(set(v) <= {"pi", "none"} for v in data.values())
    (w.pub / ".git" / "pubrepo" / "verdicts.json").write_text("{corrupt")
    assert w.integ().state == "S0"            # advisory: a bad cache is ignored
