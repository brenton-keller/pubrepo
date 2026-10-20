"""Blind ff suite: surface, release ledger, host refusal, no-force shim.

B18, B25, B26, B27, B28 and ACs 62, 63, 67.
"""
import pytest

from ff_blackbox_lib import World, digest_of, make_git_shim, read_shim_log


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


# ---------------------------------------------------------------- B18 / AC63
class TestB18Retired:
    @pytest.mark.parametrize("args", [["--force-overwrite"], ["publish", "--yes", "--force-overwrite"]])
    def test_force_overwrite_is_1_with_zero_git_calls(self, w, args):
        w.solo()
        refs = w.public_refs()
        log = make_git_shim(w)
        r = w.cli(*args)
        assert r.code == 1, r
        assert "pull" in r.out and "repair" in r.out
        assert read_shim_log(log) == [], "a refusal exits 1 with zero git calls"
        assert w.public_refs() == refs

    def test_on_republish_amend_is_1(self, w):
        t1 = w.solo()
        w.config(publish_extra='on_republish = "amend"')
        w.commit("retired key")
        r = w.cli("publish", "--yes")
        assert r.code == 1, r
        assert w.tip() == t1


# ---------------------------------------------------------------- B25 / B26 / AC67
class TestReleaseLedger:
    @pytest.fixture
    def rel(self, w):
        w.base_files()
        w.write("version.txt", "1.0.0\n")
        w.config(include=["src/", "README.md", "version.txt"], release=True)
        w.commit("init")
        assert w.cli("init").code == 0
        r = w.cli("publish", "--yes", "--no-release")
        assert r.code == 0, r
        d = w.collab_clone()
        w.git("tag", "v1.0.1", cwd=d)  # lightweight, foreign
        w.git("push", "-q", "origin", "v1.0.1", cwd=d)
        return d

    def test_b25_foreign_lightweight_tag_does_not_block(self, w, rel):
        w.write("src/app.py", "print('next')\n")
        w.commit()
        before = w.public_git("rev-parse", "refs/tags/v1.0.1")
        r = w.cli("publish", "--yes", "--no-release")
        assert r.code == 0, r
        assert w.public_git("rev-parse", "refs/tags/v1.0.1") == before

    def test_b26_squatted_version_is_7(self, w, rel):
        w.git("tag", "v1.1.0", cwd=rel)
        w.git("push", "-q", "origin", "v1.1.0", cwd=rel)
        squat = w.public_git("rev-parse", "refs/tags/v1.1.0")
        w.write("version.txt", "1.1.0\n")
        w.commit("bump")
        tags = w.public_git("tag", "--list")
        r = w.cli("publish", "--yes", "--release", "1.1.0")
        assert r.code == 7, r
        assert w.public_git("rev-parse", "refs/tags/v1.1.0") == squat
        assert w.public_git("tag", "--list") == tags

    def test_foreign_tag_never_sets_bump_base(self, w, rel):
        # Only foreign v1.0.1 exists, no managed stable tag: --bump cannot use it.
        w.write("src/app.py", "print('bump')\n")
        w.commit()
        r = w.cli("publish", "--yes", "--bump", "patch")
        assert r.code == 7, r
        assert "v1.0.2" not in w.public_git("tag", "--list")


# ---------------------------------------------------------------- B27
class TestB27HostRefusal:
    def test_pre_receive_gh006_is_2(self, w):
        t1 = w.solo()
        hook = w.public / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\necho 'GH006: Protected branch update failed' >&2\nexit 1\n")
        hook.chmod(0o755)
        w.write("src/app.py", "print('refused')\n")
        w.commit()
        recs = w.records()
        r = w.cli("publish", "--yes")
        assert r.code == 2, r
        assert "host refused the push:" in r.out
        assert w.tip() == t1
        assert w.records() == recs, "no snapshot record for a refused push"


# ---------------------------------------------------------------- B28 / AC62 / AC93
def _forbidden_git_calls(calls):
    bad = []
    for argv in calls:
        if not argv:
            continue
        i = 0
        while i < len(argv) and argv[i].startswith("-"):
            i += 2 if argv[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
        sub, args = (argv[i], argv[i + 1:]) if i < len(argv) else ("", [])
        if sub in ("push", "fetch"):
            if any(a in ("--force", "-f", "--prune", "--mirror", "--delete", "-d")
                   or a.startswith("--force-with-lease") for a in args):
                bad.append(argv)
            if any(a.startswith("+") or (sub == "push" and a.startswith(":")) for a in args):
                bad.append(argv)
        if sub == "tag" and any(a in ("-d", "--delete", "-f", "--force") for a in args):
            bad.append(argv)
        if sub == "update-ref" and "-d" in args and any(a.startswith("refs/tags/") for a in args):
            bad.append(argv)
    return bad


def test_b28_routine_commands_never_force(w):
    origin = w.bare("srcorigin.git")
    log = make_git_shim(w)
    w.base_files()
    w.config()
    w.commit("init")
    w.git("remote", "add", "origin", str(origin))
    w.git("push", "-q", "-u", "origin", "main")
    assert w.cli("init").code == 0
    assert w.cli("publish", "--yes").code == 0
    w.collab_push({"README.md": "# collab\n"})
    for args in (["status", "--remote"], ["--dry-run", "--remote"], ["--stage"], ["validate"]):
        w.cli(*args)
    plan = w.cli("pull", "--plan")
    w.cli("pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", digest_of(plan))
    w.write("src/app.py", "print('b28')\n")
    w.commit()
    assert w.cli("publish", "--yes").code == 0
    calls = read_shim_log(log)
    pubrepo_pushes = [c for c in calls if "push" in c and "-q" not in c]
    assert pubrepo_pushes, "positive control: the shim must see pubrepo's pushes"
    assert _forbidden_git_calls(calls) == []


# ---------------------------------------------------------------- 0.7.2 §17 accident prevention
def test_r3_bare_invocation_is_status_with_hint_and_no_push(w):
    t1 = w.solo()
    w.write("src/app.py", "print('pending')\n")
    w.commit()
    refs, recs = w.public_refs(), w.records()
    r = w.cli()
    assert "To publish:" in r.out, r
    assert w.public_refs() == refs and w.tip() == t1, "bare pubrepo must never push"
    assert w.records() == recs


def test_publish_off_tty_without_yes_is_1(w):
    t1 = w.solo()
    w.write("src/app.py", "print('pending')\n")
    w.commit()
    r = w.cli("publish")
    assert r.code == 1, r
    assert "publishing needs confirmation" in r.out
    assert w.tip() == t1


def test_flag_first_argv_without_yes_is_refused(w):
    # A flag-first argv means publish; off a TTY it still needs --yes.
    t1 = w.solo()
    w.write("src/app.py", "print('pending')\n")
    w.commit()
    r = w.cli("-m", "msg")
    assert r.code == 1, r
    assert "publishing needs confirmation" in r.out
    assert w.tip() == t1
