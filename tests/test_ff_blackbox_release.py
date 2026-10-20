"""Blind ff suite: release content gate and version_file (design §8.6–8.7; AC24 release half, AC73).

AC66 (sdist contents) and AC71 (archive-member canaries) are not covered: the blind spec
names no artifact location or provider config, so the export cannot be observed.
"""
import pytest

from ff_blackbox_lib import World, png_bytes

CANARY = "zz-canary-rel7q"


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


@pytest.fixture
def rel(w):
    w.base_files()
    w.write("version.txt", "1.0.0\n")
    w.config(include=["src/", "README.md", "version.txt"], release=True, forbidden=[CANARY])
    w.commit("init")
    assert w.cli("init").code == 0
    r = w.cli("publish", "--yes", "--no-release")
    assert r.code == 0, r
    return w


def _bump_source(w, version="1.1.0"):
    w.write("version.txt", f"{version}\n")
    w.commit(f"version {version}")


def _pull_all(w, *flags):
    _, r = w.pull_apply(*flags)
    assert r.code == 0, r


# ---------------------------------------------------------------- positive control
def test_release_succeeds_and_tags_snapshot(rel):
    w = rel
    _bump_source(w)
    r = w.cli("publish", "--yes", "--release", "1.1.0")
    assert r.code == 0, r
    assert w.public_git("rev-parse", "refs/tags/v1.1.0^{commit}") == w.tip()


# ---------------------------------------------------------------- §8.6 carried content blocks release
class TestCarriedFindingsBlockRelease:
    def test_carried_forbidden_publishes_but_blocks_release(self, rel):
        w = rel
        w.collab_push({"notes.txt": f"collab wrote {CANARY}\n"})
        _pull_all(w, "--leave", "notes.txt")
        w.write("src/app.py", "print('n')\n")
        w.commit()
        r = w.cli("publish", "--yes", "--no-release")
        assert r.code == 0, r  # carried findings never block publish
        assert "notes.txt" in r.out, "carried finding must be reported"
        _bump_source(w)
        tags = w.public_git("tag", "--list")
        tip = w.tip()
        r = w.cli("publish", "--yes", "--release", "1.1.0")
        assert r.code == 4, r
        assert "notes.txt" in r.out
        assert w.public_git("tag", "--list") == tags
        assert w.tip() == tip, "a refused release must not push the candidate"

    def test_forbidden_carried_has_no_accept_flag(self, rel):
        w = rel
        w.collab_push({"notes.txt": f"{CANARY}\n"})
        _pull_all(w, "--leave", "notes.txt")
        _bump_source(w)
        r = w.cli("publish", "--yes", "--release", "1.1.0", "--accept-unscanned", "notes.txt")
        assert r.code != 0, r
        assert "v1.1.0" not in w.public_git("tag", "--list")

    def test_redecide_remove_clears_release(self, rel):
        w = rel
        w.collab_push({"notes.txt": f"{CANARY}\n"})
        _pull_all(w, "--leave", "notes.txt")
        _pull_all(w, "--redecide", "notes.txt", "--remove", "notes.txt")
        _bump_source(w)
        r = w.cli("publish", "--yes", "--release", "1.1.0")
        assert r.code == 0, r
        assert "notes.txt" not in w.public_tree()

    def test_carried_unscannable_needs_accept_unscanned(self, rel):
        w = rel
        w.collab_push({"art.png": png_bytes()})
        _pull_all(w, "--leave", "art.png")
        _bump_source(w)
        r = w.cli("publish", "--yes", "--release", "1.1.0")
        assert r.code == 4, r
        r = w.cli("publish", "--yes", "--release", "1.1.0", "--accept-unscanned", "art.png")
        assert r.code == 0, r
        assert w.public_tree()["art.png"]  # carried, still public

    def test_carried_marker_triple_blocks_release(self, rel):
        w = rel
        w.collab_push({"notes.md": "<<<<<<< a\nx\n=======\ny\n>>>>>>> b\n"})
        _pull_all(w, "--leave", "notes.md")
        _bump_source(w)
        r = w.cli("publish", "--yes", "--release", "1.1.0")
        assert r.code == 4, r


# ---------------------------------------------------------------- §8.7 / AC73 version_file
class TestVersionFile:
    @pytest.fixture
    def theirs_version(self, rel):
        w = rel
        w.collab_push({"version.txt": "9.9.9\n"})
        return w

    def test_public_version_change_is_flagged_plain_pull_8(self, theirs_version):
        w = theirs_version
        head = w.head()
        r = w.cli("pull")
        assert r.code == 8, r
        assert w.head() == head
        assert (w.src / "version.txt").read_text() == "1.0.0\n"

    def test_flagged_version_not_defaulted_under_yes(self, theirs_version):
        w = theirs_version
        _, r = w.pull_apply()
        assert r.code == 1, r
        assert (w.src / "version.txt").read_text() == "1.0.0\n"

    def test_explicit_accept_writes_version(self, theirs_version):
        w = theirs_version
        _, r = w.pull_apply("--accept", "version.txt")
        assert r.code == 0, r
        assert (w.src / "version.txt").read_text() == "9.9.9\n"

    def test_release_version_comes_from_source(self, theirs_version):
        # Declined public bump: the release version is the source's, not public's.
        w = theirs_version
        _, r = w.pull_apply("--decline", "version.txt")
        assert r.code == 0, r
        _bump_source(w, "1.1.0")
        r = w.cli("publish", "--yes", "--release", "9.9.9")
        assert r.code == 7, r
        assert "v9.9.9" not in w.public_git("tag", "--list")

    def test_release_not_greater_is_7(self, rel):
        # A requested version not strictly greater stays 7.
        w = rel
        _bump_source(w, "1.1.0")
        assert w.cli("publish", "--yes", "--release", "1.1.0").code == 0
        _bump_source(w, "1.0.5")
        tags = w.public_git("tag", "--list")
        r = w.cli("publish", "--yes", "--release", "1.0.5")
        assert r.code == 7, r
        assert w.public_git("tag", "--list") == tags

    def test_pulled_version_decrease_warns_on_no_release(self, rel):
        # A source version_file below the latest managed version
        # (here via a pulled decrease) -> "downgrade" warning, exit unchanged.
        w = rel
        _bump_source(w, "1.1.0")
        assert w.cli("publish", "--yes", "--release", "1.1.0").code == 0
        w.collab_push({"version.txt": "1.0.4\n"})
        _pull_all(w, "--accept", "version.txt")
        assert (w.src / "version.txt").read_text() == "1.0.4\n"
        w.write("src/app.py", "print('after decrease')\n")
        w.commit()
        r = w.cli("publish", "--yes", "--no-release")
        assert r.code == 0, r
        assert "downgrade" in r.out.lower(), r
