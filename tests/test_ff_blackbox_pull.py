"""Blind ff suite: pull.

B3–B10, B19, B23 and ACs 26, 27, 28, 30, 35, 36, 37, 43, 48, 49, 83, 94, 95.
"""
import json
import re

import pytest

from ff_blackbox_lib import World, digest_of, opaque_bytes, tree_snapshot

README_F = "# readme\n\nline two\n\nline three\ncollab line\n"


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


@pytest.fixture
def b2(w):
    t1 = w.solo()
    f = w.collab_push({"README.md": README_F}, msg="collab readme")
    return t1, f


# ---------------------------------------------------------------- B3 / AC2 / AC28 / AC48
class TestB3PullThenPublish:
    def test_plan_exits_5_with_digest_and_writes_nothing(self, w, b2):
        head, files = w.head(), tree_snapshot(w.src)
        r = w.cli("pull", "--plan")
        assert r.code == 5, r
        digest_of(r)
        assert w.head() == head and tree_snapshot(w.src) == files

    def test_plan_digest_is_deterministic(self, w, b2):
        assert digest_of(w.cli("pull", "--plan")) == digest_of(w.cli("pull", "--plan"))

    def test_apply_commits_pull_record(self, w, b2):
        t1, f = b2
        pre = w.head()
        _, r = w.pull_apply()
        assert r.code == 0, r
        msg = w.git("log", "-1", "--format=%B")
        assert f"Pubrepo-Pulled-Through: {f}" in msg
        assert f"Pubrepo-Pulled-Range: {t1}..{f}" in msg
        assert "Co-authored-by: Collab Orator <collab@example.invalid>" in msg
        dest = [l for l in msg.splitlines() if l.startswith("Pubrepo-Destination: ")]
        assert len(dest) == 1, msg
        assert "public.git" in dest[0] and dest[0].rstrip().endswith(" main"), dest
        assert w.git("log", "-1", "--format=%an <%ae>") == "Owner <owner@example.invalid>"
        assert w.git("rev-list", "--parents", "-n1", "HEAD").split()[1:] == [pre]
        assert (w.src / "README.md").read_text() == README_F

    def test_pull_record_json_body(self, w, b2):
        _, r = w.pull_apply()
        assert r.code == 0, r
        body = w.git("log", "-1", "--format=%B")
        m = re.search(r"```(?:json)?\n(.*?)\n```", body, re.S)
        assert m, f"no fenced JSON in pull record:\n{body}"
        entries = json.loads(m.group(1))
        e = [x for x in entries if x["path"] == "README.md"]
        assert e and e[0]["decision"] == "accept"
        assert {"base", "ours", "theirs"} <= set(e[0])

    def test_no_public_commit_reachable_from_source(self, w, b2):
        t1, f = b2
        _, r = w.pull_apply()
        assert r.code == 0, r
        reachable = set(w.git("rev-list", "--all").split())
        assert f not in reachable and t1 not in reachable

    def test_publish_after_pull_parents_on_f(self, w, b2):
        _, f = b2
        _, r = w.pull_apply()
        assert r.code == 0, r
        w.write("src/app.py", "print('after pull')\n")
        w.commit()
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        tip = w.tip()
        assert w.public_git("rev-list", "--parents", "-n1", tip).split()[1:] == [f]
        assert w.public_tree()["README.md"] == README_F.encode()

    def test_publish_after_pull_with_no_own_change_is_noop(self, w, b2):
        _, f = b2
        _, r = w.pull_apply()
        assert r.code == 0, r
        assert w.cli("publish", "--yes").code == 0
        assert w.tip() == f, "tree equals tip: no empty commit"


# ---------------------------------------------------------------- AC35 digest binding
class TestDigestBinding:
    def test_yes_without_expect_plan_is_1(self, w, b2):
        head = w.head()
        r = w.cli("pull", "--yes", "--expect-tip", w.tip())
        assert r.code == 1, r
        assert w.head() == head

    def test_flags_not_in_plan_mismatch_is_1(self, w, b2):
        head = w.head()
        d = digest_of(w.cli("pull", "--plan"))
        r = w.cli("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", d,
                  "--decline", "README.md")
        assert r.code == 1, r
        assert w.head() == head

    def test_decision_flag_changes_digest(self, w, b2):
        a = digest_of(w.cli("pull", "--plan"))
        b = digest_of(w.cli("pull", "--plan", "--decline", "README.md"))
        assert a != b

    def test_tip_moved_between_plan_and_apply_writes_nothing(self, w, b2):
        _, f = b2
        head = w.head()
        d = digest_of(w.cli("pull", "--plan"))
        w.collab_push({"src/extra.py": "x = 1\n"}, msg="second")
        r = w.cli("pull", "--yes", "--expect-tip", f, "--expect-plan", d)
        assert r.code == 5, r  # §9 pull: 5 = the tip moved
        assert w.head() == head

    def test_output_flags_not_in_digest(self, w, b2):
        # --json/--quiet are outside the digest.
        d = digest_of(w.cli("pull", "--plan"))
        r = w.cli("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", d, "--quiet")
        assert r.code == 0, r

    def test_bogus_digest_is_1(self, w, b2):
        r = w.cli("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", "0" * 64)
        assert r.code == 1, r

    def test_integrate_alias_matches_plan(self, w, b2):
        a, b = w.cli("integrate"), w.cli("pull", "--plan")
        assert a.code == b.code == 5
        assert digest_of(a) == digest_of(b)


# ---------------------------------------------------------------- AC26 / AC27 dirty tree
class TestDirtyTree:
    def test_staged_change_is_1(self, w, b2):
        w.write("src/app.py", "print('staged')\n")
        w.git("add", "src/app.py")
        _, r = w.pull_apply()
        assert r.code == 1, r

    def test_tracked_modification_is_1(self, w, b2):
        w.write("src/app.py", "print('modified')\n")
        _, r = w.pull_apply()
        assert r.code == 1, r

    def test_untracked_at_written_path_is_1(self, w):
        w.solo()
        w.collab_push({"src/new.py": "new = 1\n"})
        w.write("src/new.py", "mine, untracked\n")
        plan = w.cli("pull", "--plan")
        r = w.cli("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", digest_of(plan))
        assert r.code == 1, r
        assert (w.src / "src/new.py").read_text() == "mine, untracked\n"

    def test_untracked_elsewhere_proceeds_and_stays_untracked(self, w, b2):
        w.write("wip.py", "wip\n")
        w.write(".env", "TOKEN=synthetic\n")
        _, r = w.pull_apply()
        assert r.code == 0, r
        assert (w.src / "wip.py").read_text() == "wip\n"
        assert (w.src / ".env").read_text() == "TOKEN=synthetic\n"
        status = w.git("status", "--porcelain")
        assert "?? wip.py" in status and "?? .env" in status


# ---------------------------------------------------------------- B4 / AC36
class TestB4UnmanagedAdd:
    def test_notes_carried_byte_identical(self, w):
        w.solo()
        notes = b"collab notes\r\nwith CRLF\r\n"
        w.collab_push({"notes.txt": notes})
        _, r = w.pull_apply()
        assert r.code == 0, r
        assert not (w.src / "notes.txt").exists(), "leave must not write into source"
        w.write("src/app.py", "print('ours')\n")
        w.commit()
        assert w.cli("publish", "--yes").code == 0
        assert w.public_tree()["notes.txt"] == notes
        w.write("src/app.py", "print('ours again')\n")
        w.commit()
        assert w.cli("publish", "--yes").code == 0
        assert w.public_tree()["notes.txt"] == notes

    def test_unmanaged_only_range_records_empty(self, w):
        w.solo()
        w.collab_push({"notes.txt": "n\n"})
        _, r = w.pull_apply()
        assert r.code == 0, r
        assert w.git("diff", "--name-only", "HEAD^", "HEAD") == "", "empty record: no file changes"

    def test_adoption_makes_plan_exit_5(self, w):
        w.solo()
        w.collab_push({"notes.txt": "n\n"})
        _, r = w.pull_apply()
        assert r.code == 0, r
        w.config(include=["src/", "README.md", "notes.txt"])
        w.commit("adopt notes")
        r = w.cli("pull", "--plan")
        assert r.code == 5, r
        assert "notes.txt" in r.out


# ---------------------------------------------------------------- B5 / AC49
# The deleted path is inside an included directory: deleting an explicitly listed
# include entry is a config error (exit 1) before any pull logic.
def _solo_with_util(w):
    w.solo()
    w.write("src/util.py", "util = 1\n")
    w.commit("util")
    assert w.cli("publish", "--yes").code == 0


class TestB5PublicDeletion:
    def test_delete_without_flag_is_1(self, w):
        _solo_with_util(w)
        w.collab_push(deletes=["src/util.py"])
        head = w.head()
        _, r = w.pull_apply()
        assert r.code == 1, r
        assert w.head() == head
        assert (w.src / "src/util.py").exists()

    def test_delete_with_flag_commits(self, w):
        _solo_with_util(w)
        w.collab_push(deletes=["src/util.py"])
        _, r = w.pull_apply("--delete", "src/util.py")
        assert r.code == 0, r
        assert not (w.src / "src/util.py").exists()
        assert w.cli("publish", "--yes").code == 0
        assert "src/util.py" not in w.public_tree()


# ---------------------------------------------------------------- B6 / AC29
class TestB6BinaryConflict:
    @pytest.fixture
    def b6(self, w):
        w.base_files()
        w.write("assets/logo.bin", opaque_bytes())
        w.config(include=["src/", "README.md", "assets/"], allow_opaque=["assets/*.bin"])
        w.commit("init")
        assert w.cli("init").code == 0
        assert w.cli("publish", "--yes").code == 0
        theirs = opaque_bytes()
        w.collab_push({"assets/logo.bin": theirs})
        ours = opaque_bytes()
        w.write("assets/logo.bin", ours)
        w.commit("ours")
        return ours, theirs

    def test_undecided_binary_plain_pull_is_8(self, w, b6):
        # Plain pull off a TTY with an undecided item -> 8, nothing written.
        head, files = w.head(), tree_snapshot(w.src)
        r = w.cli("pull")
        assert r.code == 8, r
        assert w.head() == head and tree_snapshot(w.src) == files

    def test_undecided_binary_with_yes_is_1(self, w, b6):
        # --yes with any item undecided -> 1, nothing written.
        head, files = w.head(), tree_snapshot(w.src)
        _, r = w.pull_apply()
        assert r.code == 1, r
        assert w.head() == head and tree_snapshot(w.src) == files

    def test_theirs_stages_public_blob(self, w, b6):
        _, theirs = b6
        _, r = w.pull_apply("--theirs", "assets/logo.bin")
        assert r.code == 0, r
        assert (w.src / "assets/logo.bin").read_bytes() == theirs
        pub_blob = w.public_git("rev-parse", "refs/heads/main:assets/logo.bin")
        assert w.git("rev-parse", "HEAD:assets/logo.bin") == pub_blob
        tip = w.tip()
        assert w.cli("publish", "--yes").code == 0
        assert w.tip() == tip

    def test_ours_publishes_ours(self, w, b6):
        ours, _ = b6
        _, r = w.pull_apply("--ours", "assets/logo.bin")
        assert r.code == 0, r
        assert w.cli("publish", "--yes").code == 0
        assert w.public_tree()["assets/logo.bin"] == ours


# ---------------------------------------------------------------- B7 / B8 / AC30 / AC83
class TestB7B8TextConflict:
    @pytest.fixture
    def b7(self, w):
        w.solo()
        w.collab_push({"README.md": "# readme\n\nTHEIRS two\n\nline three\n"})
        w.write("README.md", "# readme\n\nOURS two\n\nline three\n")
        w.commit("ours")
        before = (w.src / "README.md").read_bytes()
        _, r = w.pull_apply()
        assert r.code == 8, r
        return before

    def test_markers_written_and_nothing_committed(self, w, b7):
        text = (w.src / "README.md").read_text()
        assert "<<<<<<< " in text and ">>>>>>> " in text
        assert "Pubrepo-Pulled-Through" not in w.git("log", "-1", "--format=%B")

    @pytest.mark.parametrize("args", [["publish", "--yes"], ["--dry-run"], ["--stage"]])
    def test_in_progress_blocks_with_8(self, w, b7, args):
        refs = w.public_refs()
        r = w.cli(*args)
        assert r.code == 8, r
        assert w.public_refs() == refs

    def test_status_check_during_progress_gate_wins(self, w, b7):
        # §9 status --check precedence 4 > 8: the markers in the worktree fail the gate.
        r = w.cli("status", "--check")
        assert r.code == 4, r

    def test_plan_during_progress_is_8(self, w, b7):
        assert w.cli("pull", "--plan").code == 8

    def test_continue_with_markers_is_8(self, w, b7):
        r = w.cli("pull", "--continue", "--yes")
        assert r.code == 8, r

    def test_abort_restores_bytes(self, w, b7):
        head = w.head()
        r = w.cli("pull", "--abort")
        assert r.code == 0, r
        assert (w.src / "README.md").read_bytes() == b7
        assert w.head() == head
        assert w.git("status", "--porcelain", "--untracked-files=no") == ""
        assert w.cli("status", "--check").code != 8

    def test_continue_off_tty_without_yes_is_1(self, w, b7):
        w.write("README.md", "# readme\n\nMERGED two\n\nline three\n")
        r = w.cli("pull", "--continue")
        assert r.code == 1, r

    def test_b8_resolve_and_continue(self, w, b7):
        resolved = "# readme\n\nMERGED two\n\nline three\n"
        w.write("README.md", resolved)
        r = w.cli("pull", "--continue", "--yes")
        assert r.code == 0, r
        assert "Pubrepo-Pulled-Through" in w.git("log", "-1", "--format=%B")
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        assert w.public_tree()["README.md"] == resolved.encode()


# ---------------------------------------------------------------- B9 / AC34 private paths
class TestB9PrivatePath:
    @pytest.fixture
    def b9(self, w):
        w.base_files()
        w.write("secret/own.txt", "owner only\n")
        w.config(include=["src/", "README.md", "secret/"], exclude=["secret/"])
        w.commit("init")
        assert w.cli("init").code == 0
        assert w.cli("publish", "--yes").code == 0
        assert "secret/own.txt" not in w.public_tree()
        w.collab_push({"secret/x": "planted\n"})
        return tree_snapshot(w.src / "secret")

    def test_pull_refuses_8_and_secret_untouched(self, w, b9):
        r = w.cli("pull")
        assert r.code == 8, r
        assert tree_snapshot(w.src / "secret") == b9

    def test_pull_yes_undecided_private_is_1(self, w, b9):
        _, r = w.pull_apply()
        assert r.code == 1, r
        assert tree_snapshot(w.src / "secret") == b9

    def test_accept_on_private_path_never_writes(self, w, b9):
        plan = w.cli("pull", "--plan", "--accept", "secret/x")
        m = re.search(r"Plan digest: ([0-9a-f]{64})", plan.stdout)
        r = w.cli("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan",
                  m.group(1) if m else "0" * 64, "--accept", "secret/x")
        assert r.code != 0, r
        assert tree_snapshot(w.src / "secret") == b9

    def test_leave_carries_and_never_writes(self, w, b9):
        _, r = w.pull_apply("--leave", "secret/x")
        assert r.code == 0, r
        assert tree_snapshot(w.src / "secret") == b9
        assert w.cli("publish", "--yes").code == 0
        assert w.public_tree()["secret/x"] == b"planted\n"


# ---------------------------------------------------------------- B10 / AC37 / AC94
class TestB10ForcePushOverSnapshot:
    @pytest.fixture
    def b10(self, w):
        p1 = w.solo()
        w.write("src/app.py", "print('p2')\n")
        w.commit()
        assert w.cli("publish", "--yes").code == 0
        p2 = w.tip()
        d = w.collab_clone()
        w.git("reset", "-q", "--hard", p1, cwd=d)
        w.write("README.md", "# rewritten by collab\n", root=d)
        f = w.commit("collab rewrite", root=d)
        w.git("push", "-q", "--force", "origin", "HEAD:main", cwd=d)
        return p2, f

    def test_publish_is_5_no_longer_on_public(self, w, b10):
        p2, f = b10
        r = w.cli("publish", "--yes")
        assert r.code == 5, r
        assert "no longer on public" in r.out
        assert p2[:7] in r.out
        assert w.tip() == f

    def test_pull_8_until_accepted_then_two_publishes(self, w, b10):
        p2, f = b10
        assert w.cli("pull").code == 8
        assert w.head() == w.git("rev-parse", "HEAD")
        _, r = w.pull_apply("--accept-rewritten-history")
        assert r.code == 0, r
        assert f"refs/pubrepo/status/{p2}/dropped" in w.records()
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        tip = w.tip()
        assert w.is_ancestor(f, tip), "publish after accepting must fast-forward from F"
        w.write("src/app.py", "print('p4')\n")
        w.commit()
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        assert "no longer on public" not in r.out
        assert w.public_git("rev-parse", f"{w.tip()}^") == tip


# ---------------------------------------------------------------- B19 / AC42 worktrees
class TestB19Worktrees:
    @pytest.fixture
    def b19(self, w):
        pub2 = w.bare("public2.git")
        w.base_files()
        w.config()
        w.config(remote=pub2, name=".publish-d2.toml", dir=".publish-d2")
        w.commit("init")
        assert w.cli("init").code == 0
        assert w.cli("init", "--config", ".publish-d2.toml").code == 0
        assert w.cli("publish", "--yes").code == 0
        assert w.cli("publish", "--yes", "--config", ".publish-d2.toml").code == 0
        wt_b = w.tmp / "wtB"
        w.git("worktree", "add", "-q", "-b", "wtb", str(wt_b))
        assert w.cli("init", cwd=wt_b).code == 0
        assert w.cli("init", "--config", ".publish-d2.toml", cwd=wt_b).code == 0
        # conflicting pull for D1 in worktree A
        w.collab_push({"README.md": "# readme\n\nTHEIRS\n\nline three\n"})
        w.write("README.md", "# readme\n\nOURS\n\nline three\n")
        w.commit("ours")
        _, r = w.pull_apply()
        assert r.code == 8, r
        return wt_b, pub2

    def test_abort_in_other_worktree_is_1_naming_a(self, w, b19):
        wt_b, _ = b19
        before = tree_snapshot(wt_b)
        r = w.cli("pull", "--abort", cwd=wt_b)
        assert r.code == 1, r
        assert str(w.src) in r.out
        assert tree_snapshot(wt_b) == before
        assert "<<<<<<< " in (w.src / "README.md").read_text(), "A's pull must survive"

    def test_publish_d2_in_b_proceeds_with_notice(self, w, b19):
        wt_b, pub2 = b19
        w.write("src/app.py", "print('from B')\n", root=wt_b)
        w.commit("b change", root=wt_b)
        r = w.cli("publish", "--yes", "--config", ".publish-d2.toml", cwd=wt_b)
        assert r.code == 0, r
        assert str(w.src) in r.stderr
        assert w.public_tree(remote=pub2)["src/app.py"] == b"print('from B')\n"

    def test_publish_d2_in_a_is_8(self, w, b19):
        _, pub2 = b19
        refs = w.public_refs(pub2)
        r = w.cli("publish", "--yes", "--config", ".publish-d2.toml")
        assert r.code == 8, r
        assert w.public_refs(pub2) == refs


# ---------------------------------------------------------------- B23 / AC95
class TestB23CopiedTrailer:
    @pytest.fixture
    def b23(self, w):
        w.solo()
        w.write("docs/x.md", "x doc\n")
        w.config(include=["src/", "README.md", "docs/"])
        w.commit("add docs")
        assert w.cli("publish", "--yes").code == 0
        p1 = w.tip()
        d = w.collab_clone()
        w.write("README.md", "# readme by collab\n", root=d)
        w.git("rm", "-q", "docs/x.md", cwd=d)
        w.commit("collab edits readme, drops doc", root=d)
        f1 = w.head(d)
        w.git("cherry-pick", p1, cwd=d)
        c = w.head(d)
        assert "Source: " in w.git("log", "-1", "--format=%B", cwd=d)
        w.git("push", "-q", "origin", "HEAD:main", cwd=d)
        return f1, c

    def test_publish_is_5(self, w, b23):
        r = w.cli("publish", "--yes")
        assert r.code == 5, r

    def test_plan_lists_both_and_flags_copied_trailer(self, w, b23):
        f1, c = b23
        r = w.cli("pull", "--plan")
        assert r.code == 5, r
        assert "carries a copied `Source:` trailer" in r.out
        assert f1[:7] in r.out and c[:7] in r.out
        assert (w.src / "docs/x.md").read_text() == "x doc\n"

    def test_cherry_pick_not_treated_as_ip(self, w, b23):
        r = w.cli("--dry-run", "--remote", "--json")
        j = json.loads(r.stdout)
        assert len(j["unpulled"]) == 2, j["unpulled"]


# ---------------------------------------------------------------- AC43 fault seam
def test_sigterm_after_first_write_leaves_s4_and_abort_restores(w):
    w.solo()
    w.collab_push({"README.md": README_F, "src/app.py": "print('c')\n"})
    before = tree_snapshot(w.src)
    head = w.head()
    plan = w.cli("pull", "--plan")
    r = w.cli("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", digest_of(plan),
              env_extra={"PUBREPO_TEST_FAULT": "pull.after_write:1:sigterm"})
    assert r.code != 0, r
    assert w.cli("status", "--check").code == 8
    assert w.cli("pull", "--abort").code == 0
    assert tree_snapshot(w.src) == before
    assert w.head() == head


# ---------------------------------------------------------------- off-TTY pull exit codes
def test_plain_pull_decision_free_off_tty_is_1(w, b2):
    # B2's README change is a default-accept item: decision-free, but off a TTY
    # pull commits only with --yes.
    head, files = w.head(), tree_snapshot(w.src)
    r = w.cli("pull")
    assert r.code == 1, r
    assert w.head() == head and tree_snapshot(w.src) == files


def test_plain_pull_nothing_to_record_is_0(w):
    w.solo()
    head, files = w.head(), tree_snapshot(w.src)
    r = w.cli("pull")
    assert r.code == 0, r
    assert w.head() == head, "no range and no items: no pull record"
    assert tree_snapshot(w.src) == files


# ---------------------------------------------------------------- Pubrepo-Destination (§5.1)
def test_empty_pull_record_carries_destination(w):
    w.solo()
    w.collab_push({"notes.txt": "n\n"})  # unmanaged-only range -> empty record
    _, r = w.pull_apply()
    assert r.code == 0, r
    msg = w.git("log", "-1", "--format=%B")
    assert any(l.startswith("Pubrepo-Destination: ") and "public.git" in l for l in msg.splitlines()), msg


def test_pull_for_d1_never_accounts_d2(w):
    pub2 = w.bare("public2.git")
    w.base_files()
    w.config()
    w.config(remote=pub2, name=".publish-d2.toml", dir=".publish-d2")
    w.commit("init")
    for pre in ([], ["--config", ".publish-d2.toml"]):
        assert w.cli("init", *pre).code == 0
        assert w.cli("publish", "--yes", *pre).code == 0
    same = {"README.md": README_F}
    w.collab_push(same)                   # D1
    f2 = w.collab_push(same, remote=pub2)  # D2, identical content
    # README.md is also published by D2: §5.2 item-2 cross-destination flag, never defaulted
    # so the D1 pull needs an explicit decision.
    _, r = w.pull_apply("--accept", "README.md")  # pull for D1 only
    assert r.code == 0, r
    msg = w.git("log", "-1", "--format=%B")
    dest = [l for l in msg.splitlines() if l.startswith("Pubrepo-Destination: ")]
    assert len(dest) == 1 and "public2.git" not in dest[0] and "public.git" in dest[0], msg
    refs2 = w.public_refs(pub2)
    r = w.cli("publish", "--yes", "--config", ".publish-d2.toml")
    assert r.code == 5, r  # D2's range is still unpulled
    assert w.public_refs(pub2) == refs2
    plan = w.cli("pull", "--config", ".publish-d2.toml", "--plan")
    assert plan.code == 5 and f2[:7] in plan.out, plan
