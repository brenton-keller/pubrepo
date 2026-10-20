"""Blind ff suite: outbound gate. B15, B16, B17 and ACs 20–23."""
import pytest

from ff_blackbox_lib import MARKERS, World, png_bytes, xlsx_bytes


@pytest.fixture
def w(tmp_path):
    return World(tmp_path)


def _publish_with(w, rel, data, **cfg):
    t1 = w.solo(**cfg)
    w.write(rel, data)
    w.commit()
    refs = w.public_refs()
    return t1, refs, w.cli("publish", "--yes")


# ---------------------------------------------------------------- B15 / AC23
class TestB15Markers:
    def test_marker_triple_blocks_4(self, w):
        t1, refs, r = _publish_with(w, "src/conflicted.py", MARKERS)
        assert r.code == 4, r
        assert w.public_refs() == refs

    def test_diff3_triple_blocks_4(self, w):
        diff3 = "<<<<<<< ours\na\n||||||| base\nb\n=======\nc\n>>>>>>> theirs\n"
        _, _, r = _publish_with(w, "src/d3.py", diff3)
        assert r.code == 4, r

    def test_rst_underline_publishes(self, w):
        _, _, r = _publish_with(w, "README.md", "Title\n=======\n\nbody\n")
        assert r.code == 0, r

    def test_out_of_order_markers_publish(self, w):
        _, _, r = _publish_with(w, "src/odd.py", ">>>>>>> x\n=======\n<<<<<<< y\n")
        assert r.code == 0, r

    @pytest.mark.parametrize("args", [["--dry-run"], ["--stage"], ["status", "--check"]])
    def test_gate_parity_in_previews(self, w, args):
        w.solo()
        w.write("src/conflicted.py", MARKERS)
        w.commit()
        assert w.cli(*args).code == 4


# ---------------------------------------------------------------- B16 / AC22
class TestB16Opaque:
    def test_png_blocks_then_allowlisted_publishes(self, w):
        _, refs, r = _publish_with(w, "src/demo.png", png_bytes())
        assert r.code == 4, r
        assert "src/demo.png" in r.out
        assert w.public_refs() == refs
        w.config(allow_opaque=["src/demo.png"])
        w.commit("allow png")
        r = w.cli("publish", "--yes")
        assert r.code == 0, r
        assert "not scanned (allowlisted)" in r.out
        assert "src/demo.png" in w.public_tree()

    def test_allowlisted_reported_every_run(self, w):
        w.solo(allow_opaque=["src/*.png"])
        w.write("src/demo.png", png_bytes())
        w.commit()
        assert w.cli("publish", "--yes").code == 0
        w.write("src/app.py", "print('next')\n")
        w.commit()
        for args in (["--dry-run"], ["--stage"], ["publish", "--yes"]):
            r = w.cli(*args)
            assert "not scanned (allowlisted)" in r.out, (args, r)

    def test_glob_does_not_overmatch(self, w):
        _, _, r = _publish_with(w, "src/demo.png", png_bytes(), allow_opaque=["assets/*.png"])
        assert r.code == 4, r


# ---------------------------------------------------------------- B17 / AC20 / AC21
class TestB17Office:
    def test_corrupt_xlsx_blocks_4(self, w):
        _, refs, r = _publish_with(w, "src/book.xlsx", b"PK\x03\x04" + b"\x00" * 40 + b"truncated")
        assert r.code == 4, r
        assert w.public_refs() == refs

    def test_forbidden_inside_xlsx_blocks_naming_part(self, w):
        canary = "zz-canary-ooxml7"
        _, _, r = _publish_with(w, "src/book.xlsx", xlsx_bytes(canary), forbidden=[canary])
        assert r.code == 4, r
        assert "sharedStrings.xml" in r.out

    def test_clean_xlsx_publishes(self, w):
        _, _, r = _publish_with(w, "src/book.xlsx", xlsx_bytes("harmless"))
        assert r.code == 0, r

    def test_entity_split_canary_blocks(self, w):
        canary = "zzcanaryent"
        split = "zzcan&#97;ryent"
        _, _, r = _publish_with(w, "src/book.xlsx", xlsx_bytes(split), forbidden=[canary])
        assert r.code == 4, r

    def test_cfb_labelled_and_blocks(self, w):
        cfb = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 504
        _, _, r = _publish_with(w, "src/old.doc", cfb)
        assert r.code == 4, r
        assert "encrypted or legacy Office container" in r.out
