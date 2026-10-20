"""Blind ff suite: repair. B20, B21, B22 and ACs 52, 53, 57, 84, 98."""
import json

import pytest

from ff_blackbox_lib import World, digest_of


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def _leak(w, tag=True):
    """Publish a leak (secret.txt) in the latest snapshot; then drop it from source."""
    w.solo(include=["src/", "README.md"])
    w.write("src/secret.txt", "leaked synthetic token\n")
    w.commit("leak")
    assert w.cli("publish", "--yes").code == 0
    s = w.tip()
    if tag:
        w.public_git("tag", "draft-x", s)
    w.git("rm", "-q", "src/secret.txt")
    w.commit("drop leak")
    return s


def _plan(w, s, *extra):
    return w.cli("repair", "remove-path", "src/secret.txt", "--from", s, "--plan", *extra)


def _apply(w, s, digest, tip, *extra):
    return w.cli("repair", "remove-path", "src/secret.txt", "--from", s, "--apply", "--yes",
                 "--expect-tip", tip, "--expect-plan", digest, *extra)


def _history_has(w, path, ref="refs/heads/main"):
    return bool(w.public_git("log", "--format=%H", ref, "--", path))


# ---------------------------------------------------------------- B20 / AC52
class TestB20Plan:
    def test_plan_lists_tag_and_ends_with_digest(self, w):
        s = _leak(w)
        refs, recs = w.public_refs(), w.records()
        r = _plan(w, s)
        assert r.code == 0, r
        assert "refs/tags/draft-x" in r.stdout
        digest_of(r)
        assert w.public_refs() == refs
        assert not [k for k in w.records() if k not in recs and "/repairs/" in k], \
            "--plan must write no repair records"

    def test_plan_refused_while_projection_contains_target(self, w):
        w.solo(include=["src/", "README.md"])
        w.write("src/secret.txt", "still here\n")
        w.commit()
        assert w.cli("publish", "--yes").code == 0
        r = _plan(w, w.tip())
        assert r.code != 0, r


# ---------------------------------------------------------------- AC53
class TestApplyGuards:
    def test_apply_without_expect_flags_is_1(self, w):
        s = _leak(w, tag=False)
        refs = w.public_refs()
        r = w.cli("repair", "remove-path", "src/secret.txt", "--from", s, "--apply", "--yes")
        assert r.code == 1, r
        assert w.public_refs() == refs

    def test_stale_tip_is_5(self, w):
        s = _leak(w, tag=False)
        d = digest_of(_plan(w, s))
        old = w.tip()
        w.collab_push({"README.md": "# moved\n"})
        refs = w.public_refs()
        r = _apply(w, s, d, old)
        assert r.code == 5, r
        assert w.public_refs() == refs

    def test_unhandled_reaching_tag_refused_before_push(self, w):
        s = _leak(w)
        d = digest_of(_plan(w, s))
        refs = w.public_refs()
        r = _apply(w, s, d, w.tip())
        assert r.code != 0 and r.code != 4, r
        assert w.public_refs() == refs


# ---------------------------------------------------------------- B21 / AC57 / AC98
class TestB21LeaveReachable:
    @pytest.fixture
    def b21(self, w):
        s = _leak(w)
        flags = ("--leave-reachable", "refs/tags/draft-x")
        d = digest_of(_plan(w, s, *flags))
        tip = w.tip()
        r = _apply(w, s, d, tip, *flags)
        return s, d, tip, flags, r

    def test_apply_is_4_incomplete(self, w, b21):
        s, *_, r = b21
        assert r.code == 4, r
        assert "APPLIED, INCOMPLETE" in r.out
        assert not _history_has(w, "src/secret.txt"), "main history must be rewritten"
        assert w.public_git("rev-parse", "refs/tags/draft-x^{commit}") == s

    def test_applied_and_intent_records(self, w, b21):
        _, d, *_ = b21
        recs = w.records()
        assert f"refs/pubrepo/repairs/{d}/intent" in recs
        assert f"refs/pubrepo/repairs/{d}/applied" in recs

    def test_snapshot_rekeyed_by_addition(self, w, b21):
        s, *_ = b21
        recs = w.records()
        assert f"refs/pubrepo/snap/{s}" in recs, "old record never deleted"
        assert f"refs/pubrepo/status/{s}/rewritten" in recs

    def test_rerun_still_4_never_already_applied(self, w, b21):
        s, d, tip, flags, _ = b21
        r = _apply(w, s, d, tip, *flags)
        assert r.code == 4, r
        assert "already applied" not in r.out.lower()

    def test_rerun_accepts_post_push_tip(self, w, b21):
        # The post-push tip also satisfies --expect-tip on rerun.
        s, d, _, flags, _ = b21
        r = _apply(w, s, d, w.tip(), *flags)
        assert r.code == 4, r

    def test_rerun_with_other_tip_is_5(self, w, b21):
        s, d, _, flags, _ = b21
        refs = w.public_refs()
        r = _apply(w, s, d, "1" * 40, *flags)
        assert r.code == 5, r
        assert w.public_refs() == refs

    def test_rerun_json(self, w, b21):
        s, d, tip, flags, _ = b21
        r = _apply(w, s, d, tip, *flags, "--json")
        assert r.code == 4, r
        j = json.loads(r.stdout)
        assert j["result"] == "applied_incomplete"
        assert "refs/tags/draft-x" in j["still_reachable"]

    def test_next_publish_after_repair_is_0(self, w, b21):
        w.write("src/app.py", "print('post repair')\n")
        w.commit()
        r = w.cli("publish", "--yes")
        assert r.code == 0, r


# ---------------------------------------------------------------- B22 / AC84
class TestB22AlsoDeleteRef:
    def test_plan_lists_eleven_destroyed(self, w):
        s = _leak(w, tag=False)
        d = w.collab_clone()
        w.git("checkout", "-q", "-b", "feature-x", s, cwd=d)
        shas = []
        for i in range(10):
            w.write(f"feat/{i}.txt", f"{i}\n", root=d)
            shas.append(w.commit(f"feature {i}", root=d))
        w.git("push", "-q", "origin", "feature-x", cwd=d)
        r = _plan(w, s, "--also-delete-ref", "refs/heads/feature-x")
        assert r.code == 0, r
        for sha in shas + [s]:
            assert sha[:7] in r.stdout, f"destroyed commit {sha[:7]} not listed"
        d1 = digest_of(r)
        r = _apply(w, s, d1, w.tip(), "--also-delete-ref", "refs/heads/feature-x")
        assert r.code == 0, r
        assert "refs/heads/feature-x" not in w.public_refs()
        assert not _history_has(w, "src/secret.txt")


def test_repair_json_applied_clean(w):
    s = _leak(w, tag=False)
    d = digest_of(_plan(w, s))
    r = _apply(w, s, d, w.tip(), "--json")
    assert r.code == 0, r
    j = json.loads(r.stdout)
    assert j["result"] == "applied" and j["still_reachable"] == []
