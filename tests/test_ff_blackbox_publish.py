"""Blind ff suite: publish, previews, status, identity, divergence, branch lifecycle.

B1, B2, B11, B12, B13, B14, B24 and ACs 4, 7, 9, 10, 11, 15, 17, 19, 64, 76.
"""
import json
import subprocess

import pytest

from ff_blackbox_lib import PULL_MSG, World, digest_of, q


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


# ---------------------------------------------------------------- B1 / AC10
class TestB1StatusCheck:
    def test_solo_republish_then_status_zero(self, w):
        t1 = w.solo()
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        assert w.tip() == t1, "a no-change publish must not create a commit"
        assert w.cli("status", "--check").code == 0

    def test_committed_source_edit_gives_6(self, w):
        w.solo()
        w.write("src/app.py", "print('changed')\n")
        w.commit()
        r = w.cli("status", "--check")
        assert r.code == 6, r

    def test_uncommitted_edit_gives_6(self, w):
        # Π (live) hashes worktree bytes: a dirty edit is a pending change.
        w.solo()
        w.write("src/app.py", "print('dirty')\n")
        assert w.cli("status", "--check").code == 6

    def test_excluded_edit_still_0(self, w):
        w.solo()
        w.write("unrelated.txt", "not managed\n")
        w.commit()
        assert w.cli("status", "--check").code == 0

    def test_offline_status_uses_last_known_state(self, w):
        # §9: offline status --check uses the last-known remote state, so an
        # unseen collaborator push is invisible until --remote fetches.
        w.solo()
        w.collab_push({"README.md": "# readme\ncollab\n"})
        assert w.cli("status", "--check").code == 0
        assert w.cli("status", "--check", "--remote").code == 5
        # ...and the fetch is now last-known state.
        assert w.cli("status", "--check").code == 5

    def test_precedence_5_over_6(self, w):
        w.solo()
        w.collab_push({"README.md": "# readme\ncollab\n"})
        w.write("src/app.py", "print('ours')\n")
        w.commit()
        assert w.cli("status", "--check", "--remote").code == 5

    def test_precedence_4_over_5(self, w):
        canary = "zz-canary-b1prec"
        w.solo(forbidden=[canary])
        w.collab_push({"README.md": "# readme\ncollab\n"})
        w.write("src/app.py", f"x = '{canary}'\n")
        w.commit()
        assert w.cli("status", "--check", "--remote").code == 4


# ---------------------------------------------------------------- B2 / AC1 / AC9
class TestB2ForeignCommit:
    @pytest.fixture
    def b2(self, w):
        t1 = w.solo()
        f = w.collab_push({"README.md": "# readme\n\nline two\n\nline three\ncollab line\n"})
        return t1, f

    def test_publish_exits_5_and_changes_nothing(self, w, b2):
        _, f = b2
        w.write("src/app.py", "print('ours pending')\n")
        w.commit()
        refs = w.public_refs()
        r = w.cli("publish", "--yes")
        assert r.code == 5, r
        assert PULL_MSG.search(r.out), r
        assert w.public_refs() == refs
        assert w.tip() == f

    def test_publish_names_foreign_commit(self, w, b2):
        _, f = b2
        r = w.cli("publish", "--yes")
        assert r.code == 5, r
        assert f[:7] in r.out, "AC1: publish must name the foreign commit"

    def test_dry_run_remote_exits_5(self, w, b2):
        r = w.cli("--dry-run", "--remote")
        assert r.code == 5, r

    def test_offline_dry_run_uses_last_known_then_5(self, w, b2):
        # Never fetched since the collaborator push: last-known is S0.
        assert w.cli("--dry-run").code == 0
        assert w.cli("--dry-run", "--remote").code == 5
        assert w.cli("--dry-run").code == 5, "AC9: offline dry-run must use the fetched range"

    def test_dry_run_json_contract(self, w, b2):
        _, f = b2
        r = w.cli("--dry-run", "--remote", "--json")
        assert r.code == 5, r
        j = json.loads(r.stdout)
        assert j["remote_checked"] is True
        assert j["predicted_exit"] == 5
        assert [u["sha"][:7] for u in j["unpulled"]] == [f[:7]]
        u = j["unpulled"][0]
        assert "Collab Orator" in u["author"]
        assert "README.md" in u["paths"]
        assert isinstance(j["items"], list)

    def test_offline_stage_json_exit_0_remote_unchecked(self, w, b2):
        r = w.cli("--stage", "--json")
        assert r.code == 0, r
        assert json.loads(r.stdout)["remote_checked"] is False

    def test_stage_with_unpulled_range_is_0(self, w, b2):
        # §9: --stage is 0 for S2/S3 even with --remote.
        r = w.cli("--stage", "--remote")
        assert r.code == 0, r


# ---------------------------------------------------------------- AC7 records/message
class TestSnapshotRecordAndMessage:
    def test_message_has_source_no_branch_no_dirty(self, w):
        w.solo()
        w.write("src/app.py", "print('dirty publish')\n")  # a dirty working tree is allowed
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        msg = w.public_git("log", "-1", "--format=%B", "refs/heads/main")
        head = w.head()
        assert f"Source: {head[:7]}" in msg
        assert "Branch:" not in msg
        assert "(dirty)" not in msg

    def test_record_is_parentless_commit_with_keys(self, w):
        tip = w.solo()
        ref = f"refs/pubrepo/snap/{tip}"
        recs = w.records()
        assert ref in recs, f"no snapshot record for {tip}: {recs}"
        assert w.git("cat-file", "-t", ref) == "commit"
        assert w.git("rev-list", "--parents", "-n1", ref).split()[1:] == []
        j = w.record_json(ref)
        assert {"format", "destination", "source", "written", "nonderivable", "keep"} <= set(j)
        assert j["source"] == w.head()
        assert "README.md" in json.dumps(j["written"])

    def test_dirty_path_recorded_nonderivable(self, w):
        w.solo()
        w.write("src/app.py", "print('dirty two')\n")
        assert w.cli("publish", "--yes").code == 0
        j = w.record_json(f"refs/pubrepo/snap/{w.tip()}")
        assert "src/app.py" in json.dumps(j["nonderivable"])

    def test_record_not_on_any_branch_and_not_public(self, w):
        tip = w.solo()
        assert w.git("branch", "--contains", w.records()[f"refs/pubrepo/snap/{tip}"]) == ""
        assert "refs/pubrepo" not in w.public_refs(), "records must never reach the public remote"


# ---------------------------------------------------------------- AC4 / AC11
class TestManagedSetChanges:
    def test_dropped_from_include_is_deleted_and_named(self, w):
        w.solo()
        w.config(include=["src/"])
        w.commit()
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        assert "README.md" not in w.public_tree()
        assert "README.md" in r.out

    def test_reinit_without_records_publishes_deletion(self, w):
        w.solo()
        for ref in w.records():
            w.git("update-ref", "-d", ref)
        import shutil
        shutil.rmtree(w.src / ".publish")
        assert w.cli("init").code == 0
        w.write("src/other.py", "keeps src/ non-empty\n")
        w.git("rm", "-q", "src/app.py")
        w.commit()
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        assert "src/app.py" not in w.public_tree()


# ---------------------------------------------------------------- B11 / AC17
class TestB11MissingBranch:
    def test_deleted_branch_is_s11_then_recreate_root(self, w):
        w.solo()
        w.public_git("update-ref", "-d", "refs/heads/main")
        w.write("src/app.py", "print('after delete')\n")
        w.commit()
        r = w.cli("publish", "--yes")
        assert r.code == 5, r
        assert w.tip() is None, "S11 must not silently recreate the branch"
        r = w.cli("publish", "--yes", "--recreate-branch")
        assert r.code == 0, r
        tip = w.tip()
        assert w.public_git("rev-list", "--parents", "-n1", tip).split()[1:] == []
        assert w.public_tree()["src/app.py"] == b"print('after delete')\n"

    def test_first_publish_to_empty_remote_is_root(self, w):
        tip = w.solo()
        assert w.public_git("rev-list", "--count", tip) == "1"

    def test_branch_recreated_with_unrelated_history_is_s7(self, w):
        w.solo()
        w.public_git("update-ref", "-d", "refs/heads/main")
        d = w.collab_clone()
        w.git("checkout", "-q", "--orphan", "fresh", cwd=d)
        w.write("other.txt", "unrelated\n", root=d)
        w.commit("unrelated root", root=d)
        w.git("push", "-q", "origin", "fresh:main", cwd=d)
        r = w.cli("publish", "--yes")
        assert r.code == 5, r


# ---------------------------------------------------------------- B12 / AC15
class TestB12BranchDivergence:
    @pytest.fixture
    def b12(self, w):
        w.solo()
        w.git("checkout", "-q", "-b", "feat")
        w.write("src/feature.py", "feature = 1\n")
        w.commit("feature")
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        snap = w.tip()
        w.git("checkout", "-q", "main")
        w.write("src/mainonly.py", "main = 1\n")  # truly diverged from feat
        w.commit("main only")
        return snap

    def test_publish_from_other_branch_is_8_naming_branch(self, w, b12):
        refs = w.public_refs()
        r = w.cli("publish", "--yes")
        assert r.code == 8, r
        assert "feat" in r.out
        assert w.public_refs() == refs

    def test_accept_snapshot_sha_publishes(self, w, b12):
        r = w.cli("publish", "--yes", f"--accept-source-divergence={b12}")
        assert r.code == 0, r
        new = w.tip()
        assert w.public_git("rev-parse", f"{new}^") == b12
        assert "src/feature.py" not in w.public_tree()
        assert w.cli("publish", "--yes").code == 0, "next run must be integrated"

    def test_accept_full_sigma_publishes(self, w, b12):
        sigma = w.git("rev-parse", "feat")
        r = w.cli("publish", "--yes", f"--accept-source-divergence={sigma}")
        assert r.code == 0, r

    def test_bare_accept_flag_is_1(self, w, b12):
        refs = w.public_refs()
        r = w.cli("publish", "--yes", "--accept-source-divergence")
        assert r.code == 1, r
        assert w.public_refs() == refs

    def test_accept_wrong_sha_does_not_publish(self, w, b12):
        refs = w.public_refs()
        r = w.cli("publish", "--yes", f"--accept-source-divergence={w.head()}")
        assert r.code != 0, r
        assert w.public_refs() == refs

    def test_stage_is_0_dry_run_is_8(self, w, b12):
        assert w.cli("--stage").code == 0
        assert w.cli("--dry-run").code == 8
        assert w.cli("status", "--check").code == 8


class TestB12bStrictlyBehind:
    """A checkout strictly behind σ on another branch is S10a (5), not S10b."""

    @pytest.fixture
    def b12b(self, w):
        w.solo()
        w.git("checkout", "-q", "-b", "feat")
        w.write("src/feature.py", "feature = 1\n")
        w.commit("feature")
        assert w.cli("publish", "--yes").code == 0
        w.git("checkout", "-q", "main")
        return w.tip()

    def test_publish_is_5_naming_feat(self, w, b12b):
        refs = w.public_refs()
        r = w.cli("publish", "--yes")
        assert r.code == 5, r
        assert "feat" in r.stderr
        assert w.public_refs() == refs
        assert "src/feature.py" in w.public_tree(), "published content must not be reverted"

    def test_dry_run_is_5(self, w, b12b):
        assert w.cli("--dry-run").code == 5


# ---------------------------------------------------------------- B13 / AC76
class TestB13SigmaGone:
    @pytest.fixture
    def b13(self, w):
        w.solo()
        w.write("src/app.py", "print('v2')\n")
        w.commit("v2")
        assert w.cli("publish", "--yes").code == 0
        snap = w.tip()
        sigma = w.head()
        # Publish keeps a local bookmark on σ: legacy `published/<ts>` tags, or (design §18.9 R2)
        # `refs/pubrepo/bookmarks/<ts>`. Both are dropped after the amend so σ is truly gone
        # (AC76 recipe; otherwise the test depends on timing).
        w.git("commit", "-q", "--amend", "-m", "v2 amended")
        for t in w.git("tag", "-l", "published/*").split():
            w.git("tag", "-d", t)
        for ref in w.git("for-each-ref", "--format=%(refname)", "refs/pubrepo/bookmarks/").split():
            w.git("update-ref", "-d", ref)
        w.git("reflog", "expire", "--expire=now", "--all")
        w.git("gc", "-q", "--prune=now")
        assert subprocess.run(["git", "cat-file", "-e", sigma], cwd=w.src,
                              env=w.env).returncode != 0, "fixture: σ must be unresolvable"
        return snap

    def test_publish_is_8_no_longer_exists(self, w, b13):
        r = w.cli("publish", "--yes")
        assert r.code == 8, r
        assert "no longer exists" in r.out
        assert "behind" not in r.out.lower(), "must be S10b, not S10a"

    def test_message_prints_pasteable_flag_and_it_works(self, w, b13):
        r = w.cli("publish", "--yes")
        assert f"--accept-source-divergence={b13}" in r.out
        r = w.cli("publish", "--yes", f"--accept-source-divergence={b13}")
        assert r.code == 0, r


# ---------------------------------------------------------------- B14 / AC16
def test_b14_shallow_source_refused(w):
    w.base_files()
    w.config()
    w.commit("one")
    w.write("src/app.py", "print(2)\n")
    w.commit("two")
    origin = w.bare("srcorigin.git")
    w.git("push", "-q", str(origin), "main")
    shallow = w.tmp / "shallow"
    w.git("clone", "-q", "--depth", "1", f"file://{origin}", str(shallow), cwd=w.tmp)
    assert w.git("rev-parse", "--is-shallow-repository", cwd=shallow) == "true"
    w.cli("init", cwd=shallow)
    r = w.cli("publish", "--yes", cwd=shallow)
    assert r.code == 1, r
    assert "full source history required" in r.out
    assert w.tip() is None


# ---------------------------------------------------------------- AC19 fault seam
def test_push_ambiguous_recorded_as_pushed(w):
    w.solo()
    w.write("src/app.py", "print('amb')\n")
    w.commit()
    r = w.cli("publish", "--yes", env_extra={"PUBREPO_TEST_FAULT": "push.ambiguous:exit128"})
    assert r.code == 0, r
    assert f"refs/pubrepo/snap/{w.tip()}" in w.records()


def test_push_ambiguous_absent_is_2_no_record(w):
    t1 = w.solo()
    w.write("src/app.py", "print('absent')\n")
    w.commit()
    before = w.records()
    r = w.cli("publish", "--yes", env_extra={"PUBREPO_TEST_FAULT": "push.ambiguous_absent:exit128"})
    assert r.code == 2, r
    assert w.tip() == t1
    assert w.records() == before


# ---------------------------------------------------------------- B24 / AC64 / AC92
class TestB24AmendAcrossClones:
    @pytest.fixture
    def b24(self, w):
        origin = w.bare("srcorigin.git")
        # Design §18.11: records sync only to remotes listed exactly in private_remotes.
        # Both clones see origin as this exact path (A adds it; B clones from it).
        w.solo(publish_extra=f"private_remotes = [{q(str(origin))}]")
        w.git("remote", "add", "origin", str(origin))
        w.git("push", "-q", "-u", "origin", "main")
        w.write("src/app.py", "print('records sync')\n")
        w.commit()
        assert w.cli("publish", "--yes").code == 0  # records now pushed to origin
        old = w.tip()
        b = w.tmp / "cloneB"
        w.git("clone", "-q", str(origin), str(b), cwd=w.tmp)
        assert w.cli("init", cwd=b).code == 0
        assert w.cli("status", "--remote", cwd=b).code in (0, 6)  # B now holds old records
        assert f"refs/pubrepo/snap/{old}" in w.records(b)
        w.write("src/app.py", "print('amended')\n")
        w.commit("fix")
        r = w.cli("publish", "--amend", "--yes")
        assert r.code == 0, r
        w.git("push", "-q", "origin", "main")
        return old, b

    def test_a_writes_rewritten_marker_and_keeps_old_record(self, w, b24):
        old, _ = b24
        recs = w.records()
        new = w.tip()
        assert new != old
        assert w.public_git("rev-parse", f"{new}^") == w.public_git("rev-parse", f"{old}^"), \
            "--amend must replace the old tip, not stack on it"
        assert f"refs/pubrepo/status/{old}/rewritten" in recs
        assert f"refs/pubrepo/snap/{new}" in recs
        assert f"refs/pubrepo/snap/{old}" in recs, "old record must never be deleted"

    def test_b_publishes_without_s7(self, w, b24):
        _, b = b24
        w.git("pull", "-q", "--ff-only", cwd=b)
        r = w.cli("publish", "--yes", cwd=b)
        assert r.code == 0, r
        assert "no longer on public" not in r.out

    def test_amend_without_yes_off_tty_is_refused(self, w):
        t1 = w.solo()
        w.write("src/app.py", "print('x')\n")
        w.commit()
        r = w.cli("publish", "--amend")
        assert r.code == 1, r
        assert w.tip() == t1
