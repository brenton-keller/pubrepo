"""`pubrepo pull` (design §5).

Pull runs in-process through main() in tmp repos with file:// remotes.
"Then publish exits 0" halves are asserted through compute_integration
(state S0/S1 with tip == IP, no items); publish-level round trips are in
tests/test_ff_publish.py (core).
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

import pubrepo
from conftest import git_run
from ff_fixtures import CONFIG_REL, _git

# --- helpers ---

def _pull(run, w, *args):
    return run(["pull", *args], cwd=w.src)


def _plan(run, w, *flags):
    r = _pull(run, w, "--plan", *flags)
    last = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    assert last.startswith("Plan digest: "), (r.code, r.stdout, r.stderr)
    return r, last.split(": ", 1)[1], w.tip()


def _pull_yes(run, w, *flags):
    _r, digest, tip = _plan(run, w, *flags)
    return _pull(run, w, "--yes", "--expect-tip", tip, "--expect-plan", digest, *flags)


def _head_msg(w):
    return git_run("log", "-1", "--format=%B", cwd=w.src)


def _entries(w):
    body = _head_msg(w)
    start = body.index("```json\n") + len("```json\n")
    return json.loads(body[start:body.index("\n```", start)])


def _trailer(w, key):
    return git_run("log", "-1", f"--format=%(trailers:key={key},valueonly)", cwd=w.src).strip()


def _source_bytes(w):
    return {p.relative_to(w.src).as_posix(): p.read_bytes() for p in w.src.rglob("*")
            if p.is_file() and ".git" not in p.relative_to(w.src).parts
            and ".publish" not in p.relative_to(w.src).parts}


# --- the round trip (AC2, AC36, AC40, AC48) ---

def test_roundtrip_text_change(world, run):
    """AC2 (pull side) and AC48: accept a collaborator's text change; the
    record is one source commit on HEAD; no public commit is reachable from
    source refs; then the state is integrated (publish would be a no-op)."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# Guide\nfrom collab\n"})
    old_head = w.head()
    r = _pull_yes(run, w)
    assert r.code == 0, r.stderr
    assert (w.src / "docs" / "guide.md").read_text() == "# Guide\nfrom collab\n"
    assert git_run("rev-parse", "HEAD^", cwd=w.src).strip() == old_head
    assert _trailer(w, "Pubrepo-Pulled-Through") == f
    reachable = git_run("rev-list", "--all", cwd=w.src).split()
    assert f not in reachable and git_run("status", "--porcelain", cwd=w.src) == ""
    integ = w.integ()
    assert integ.ip == f and integ.tip == f and integ.state == "S0" and not integ.items


def test_empty_pull_records_unmanaged_only_range(world, run):
    """AC36: an unmanaged-only range records an (empty-decision) pull; the
    file stays public-only; adoption later makes --plan exit 5."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"notes.txt": "public only\n"})
    assert _pull_yes(run, w).code == 0
    assert not (w.src / "notes.txt").exists()
    assert [(e["path"], e["decision"], e["class"]) for e in _entries(w)] == [
        ("notes.txt", "leave", "carried_add")]
    assert w.integ().ip == f
    (w.src / CONFIG_REL).write_text((w.src / CONFIG_REL).read_text().replace(
        '"a.py", "docs"', '"a.py", "docs", "notes.txt"'))
    git_run("commit", "-qam", "adopt notes", cwd=w.src)
    r, _d, _t = _plan(run, w)
    assert r.code == 5


def test_nothing_to_pull_is_zero_and_writes_nothing(world, run):
    w = world()
    w.snapshot()
    head = w.head()
    for args in ([], ["--plan"]):
        assert _pull(run, w, *args).code == 0
    assert w.head() == head


def test_s9_without_foreign_commit_then_integrated(world, run):
    """AC40: items with tip == IP (S9) are decided by pull."""
    w = world()
    (w.src / "docs" / "draft.md").write_text("untracked draft\n")
    snap = w.snapshot(extra={"docs/draft.md": "untracked draft\n"}, dirty=True)
    (w.src / "docs" / "draft.md").write_text("changed\n")
    git_run("add", "docs/draft.md", cwd=w.src)
    git_run("commit", "-qm", "track draft", cwd=w.src)
    assert w.integ().state == "S9"
    r = _pull_yes(run, w, "--ours", "docs/draft.md")
    assert r.code == 0, r.stderr
    assert w.integ().state in ("S0", "S1") and not w.integ().items
    assert _trailer(w, "Pubrepo-Pulled-Through") == snap


# --- §5.7 record shape (AC28; Pubrepo-Destination; plan file) ---

def test_record_shape_author_coauthors_trailers_body(world, run):
    """AC28: author is the source identity; co-authors are accepted
    authors only; trailers; a JSON array body with base/ours/theirs."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\naccepted\n"})
    w.collab_commit({"notes.txt": "left public\n"})
    assert _pull_yes(run, w).code == 0
    who = git_run("log", "-1", "--format=%an <%ae>", cwd=w.src).strip()
    assert who == "Pubrepo Tests <test@pubrepo.test>"
    assert _trailer(w, "Co-authored-by") == "Collab Orator <collab@example.invalid>"
    entries = _entries(w)
    assert isinstance(entries, list)
    by = {e["path"]: e for e in entries}
    assert {"path", "class", "decision", "base", "ours", "theirs", "result",
            "managed_at_decision", "config", "prefilled_from"} <= set(by["docs/guide.md"])
    assert by["docs/guide.md"]["decision"] == "accept"
    assert _trailer(w, "Pubrepo-Pulled-Range").endswith(f"..{w.tip()}")


def test_record_carries_destination_trailer_including_empty(world, run):
    """pull::record_carries_destination_trailer_including_empty."""
    w = world()
    w.snapshot()
    dest = " ".join(pubrepo.destination_identity(w.config()))
    w.collab_commit({"docs/guide.md": "# Guide\nx\n"})
    assert _pull_yes(run, w).code == 0
    assert _trailer(w, "Pubrepo-Destination") == dest
    w.collab_commit({"notes.txt": "n\n"})
    assert _pull_yes(run, w).code == 0
    assert _trailer(w, "Pubrepo-Destination") == dest
    assert isinstance(_entries(w), list)


def test_plan_file_recorded(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\ny\n"})
    _r, digest, _tip = _plan(run, w)
    assert (w.src / ".git" / "pubrepo" / "plans" / f"{digest}.json").exists()
    assert _pull_yes(run, w).code == 0
    assert _trailer(w, "Pubrepo-Plan-File") == f"pubrepo/plans/{digest}.json"


def test_staged_blobs_exist_in_source_odb_without_alternate(world, run):
    """§C: every staged blob is in the source ODB on its own."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# Guide\nodb\n", "docs/new.md": "new\n"})
    assert _pull_yes(run, w).code == 0
    env = {k: v for k, v in os.environ.items() if "OBJECT" not in k}
    for path in ("docs/guide.md", "docs/new.md"):
        blob = w.blob(f, path)
        assert subprocess.run(["git", "cat-file", "-e", blob], cwd=w.src, env=env).returncode == 0


# --- exit by invocation ---

def test_plan_with_items_is_5(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nz\n"})
    r, _d, _t = _plan(run, w)
    assert r.code == 5


def test_plain_pull_off_tty_undecided_is_8_nothing_written(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": None})
    head, before = w.head(), _source_bytes(w)
    r = _pull(run, w)
    assert r.code == 8 and w.head() == head and _source_bytes(w) == before


def test_plain_pull_off_tty_decision_free_is_1_with_command(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\ndecision free\n"})
    head = w.head()
    r = _pull(run, w)
    assert r.code == 1 and "--expect-plan" in r.stderr and w.head() == head


@pytest.mark.no_git_calls
def test_yes_without_expect_flags_is_1_nothing_computed(world, run, git_recorder):
    """Missing --expect-plan → 1 before any compute."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nq\n"})
    calls = len(git_recorder.calls())
    r = _pull(run, w, "--yes", "--expect-tip", w.tip())
    assert r.code == 1
    assert not [a for a, _ in git_recorder.calls()[calls + 1:] if a and a[0] in ("fetch",)]
    assert not (w.src / ".git" / "pubrepo" / "plans").exists()


def test_moved_tip_is_5_same_tip_changed_plan_is_1(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nfirst\n"})
    _r, digest, tip = _plan(run, w)
    w.commit_source({"a.py": "a = 'owner edit after plan'\n"})   # changes the digest
    r = _pull(run, w, "--yes", "--expect-tip", tip, "--expect-plan", digest)
    assert r.code == 1
    _r, digest, tip = _plan(run, w)
    w.collab_commit({"docs/more.md": "more\n"})                    # the tip moves
    r = _pull(run, w, "--yes", "--expect-tip", tip, "--expect-plan", digest)
    assert r.code == 5


@pytest.mark.parametrize("change,flag", [
    ("delete", None), ("binary", None), ("forbidden", None), ("baseline", None)])
def test_yes_with_undecided_never_default_class_is_1(world, run, change, flag):
    """Ruling (a): `--yes` with any undecided item → 1, nothing written."""
    w = world(scrub={"forbidden": ["zz-pull-canary-zz"]})
    if change == "baseline":
        (w.src / "docs" / "draft.md").write_text("d\n")
        w.snapshot(extra={"docs/draft.md": "d\n"}, dirty=True)
        (w.src / "docs" / "draft.md").write_text("d2\n")
        git_run("add", "docs/draft.md", cwd=w.src)
        git_run("commit", "-qm", "track", cwd=w.src)
    else:
        w.snapshot()
        files = {"delete": {"docs/guide.md": None},
                 "binary": {"docs/pic.bin": b"\x00\x01binary\x00" * 20},
                 "forbidden": {"docs/leak.md": "zz-pull-canary-zz\n"}}[change]
        w.collab_commit(files)
    head, before = w.head(), _source_bytes(w)
    r = _pull_yes(run, w)
    assert r.code == 1 and "missing explicit choice" in r.stderr
    assert w.head() == head and _source_bytes(w) == before


def test_text_conflict_is_8_with_yes_and_in_progress(world, run):
    """AC30 / C2: markers written, nothing committed, in progress (8)."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\ntheirs line\n"})
    w.commit_source({"docs/guide.md": "# Guide\nours line\n"})
    head = w.head()
    r = _pull_yes(run, w)
    assert r.code == 8 and w.head() == head
    assert "<<<<<<< ours" in (w.src / "docs" / "guide.md").read_text()
    assert pubrepo.pull_in_progress(w.src) is not None


# --- digest coverage (AC35) ---

def test_digest_stable_and_changes_with_flags(world, run, tmp_path):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": None, "docs/b.md": "b\n"})
    _r, d1, _ = _plan(run, w)
    _r, d2, _ = _plan(run, w)
    _r, d3, _ = _plan(run, w, "--delete", "docs/guide.md")
    f1, f2 = tmp_path / "one.tsv", tmp_path / "two.tsv"
    f1.write_text("docs/guide.md\tdelete\n")
    f2.write_text("docs/guide.md\tdelete\n")
    _r, d4, _ = _plan(run, w, "--decisions", str(f1))
    _r, d5, _ = _plan(run, w, "--decisions", str(f2))
    _r, d6, _ = _plan(run, w, "--quiet")
    assert d1 == d2 == d6 and d1 != d3 and d3 == d4 == d5


def test_flags_given_to_yes_but_not_plan_mismatch_is_1(world, run):
    """AC35: --yes with flags absent from --plan → digest mismatch → 1."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": None})
    _r, digest, tip = _plan(run, w)
    r = _pull(run, w, "--yes", "--expect-tip", tip, "--expect-plan", digest,
              "--delete", "docs/guide.md")
    assert r.code == 1
    r = _pull_yes(run, w, "--delete", "docs/guide.md")
    assert r.code == 0 and not (w.src / "docs" / "guide.md").exists()


def test_deletion_without_flag_is_1(world, run):
    """AC49: a public deletion is never defaulted."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": None})
    assert _pull_yes(run, w).code == 1
    assert (w.src / "docs" / "guide.md").exists()


@pytest.mark.parametrize("content", ["a\tb.md\tdelete\n", "only-a-path\n", "x\tunknown\n"])
def test_decisions_file_malformed_refused(world, run, tmp_path, content):
    """A path with TAB, a line without one, an unknown decision → 1."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": None})
    f = tmp_path / "d.tsv"
    f.write_text(content)
    assert _pull(run, w, "--plan", "--decisions", str(f)).code == 1


def test_unknown_path_or_invalid_decision_refused(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nv\n"})
    assert _pull(run, w, "--plan", "--accept", "nope.md").code == 1
    assert _pull(run, w, "--plan", "--remove", "docs/guide.md").code == 1


# --- clean tree (AC26, AC27) ---

def test_staged_or_tracked_modification_refused(world, run):
    """AC26."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nw\n"})
    (w.src / "a.py").write_text("dirty\n")
    assert _pull_yes(run, w).code == 1
    git_run("add", "a.py", cwd=w.src)
    assert _pull_yes(run, w).code == 1


def test_untracked_elsewhere_proceeds_and_stays_untracked(world, run):
    """AC27 (B12b): untracked wip.py and .env stay untracked."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nu\n"})
    (w.src / "wip.py").write_text("wip\n")
    (w.src / ".env").write_text("SECRET_SHAPED=1\n")
    assert _pull_yes(run, w).code == 0
    status = git_run("status", "--porcelain", "--untracked-files=all", cwd=w.src)
    assert "?? wip.py" in status and "?? .env" in status
    assert ".env" not in git_run("ls-tree", "-r", "--name-only", "HEAD", cwd=w.src)


def test_write_target_untracked_refused(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/new.md": "theirs\n"})
    (w.src / "docs" / "new.md").write_text("local untracked\n")
    r = _pull_yes(run, w)
    assert r.code == 1
    assert (w.src / "docs" / "new.md").read_text() == "local untracked\n"


# --- classes (AC29, AC31, AC32, AC39, AC49–AC51, AC82, AC88, AC89) ---

def test_binary_both_sides_ours_or_theirs(world, run):
    """AC29 (C3, N4): exit 8 until a choice; staged blob == chosen."""
    w = world(include=["a.py", "docs", "data.bin"],
              files={"a.py": "a\n", "docs/guide.md": "g\n", "data.bin": "\x00base\x00"})
    w.snapshot()
    f = w.collab_commit({"data.bin": b"\x00theirs\x00" * 3})
    w.commit_source({"data.bin": "\x00ours\x00"})
    assert _pull(run, w).code == 8
    assert _pull_yes(run, w, "--theirs", "data.bin").code == 0
    staged = git_run("ls-files", "-s", "data.bin", cwd=w.src).split()[1]
    assert staged == w.blob(f, "data.bin")


def test_p3_cases_never_union(world, run):
    """AC31: overlapping edits never produce a union; merge-file only."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\n## [1.1.0]\n- theirs\n"})
    w.commit_source({"docs/guide.md": "# Guide\n## [1.1.0]\n- ours\n"})
    r = _pull_yes(run, w)
    assert r.code == 8
    text = (w.src / "docs" / "guide.md").read_text()
    assert "<<<<<<< ours" in text and "=======" in text and ">>>>>>> theirs" in text


def test_disjoint_changelog_appends_merge_clean(world, run):
    """AC88 (C1): disjoint hunks merge cleanly; accept by default."""
    base = "# Changelog\n\n" + "".join(f"line {i}\n" for i in range(20))
    w = world(files={"a.py": "a\n", "docs/guide.md": base})
    w.snapshot()
    w.collab_commit({"docs/guide.md": base + "theirs appended\n"})
    w.commit_source({"docs/guide.md": "# Changelog\nours at top\n\n"
                     + "".join(f"line {i}\n" for i in range(20))})
    assert _pull_yes(run, w).code == 0
    text = (w.src / "docs" / "guide.md").read_text()
    assert "ours at top" in text and "theirs appended" in text
    integ = w.integ()
    assert integ.ip == integ.tip and not integ.items


def test_overlapping_changelog_resolve_continue(world, run):
    """AC89 / AC30: conflict, publish refused meanwhile, resolve, --continue."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nT\n"})
    w.commit_source({"docs/guide.md": "# Guide\nO\n"})
    assert _pull_yes(run, w).code == 8
    with pytest.raises(pubrepo.DecisionRequired):
        pubrepo.assert_no_pull_in_progress(w.src, command="publish")
    assert _pull(run, w, "--continue", "--yes").code == 8           # markers remain
    (w.src / "docs" / "guide.md").write_text("# Guide\nO and T\n")
    assert _pull(run, w, "--continue").code == 1                    # AC83: off a TTY needs --yes
    r = _pull(run, w, "--continue", "--yes")
    assert r.code == 0, r.stderr
    assert pubrepo.pull_in_progress(w.src) is None
    by = {e["path"]: e for e in _entries(w)}
    assert by["docs/guide.md"]["decision"] == "resolved"
    integ = w.integ()
    assert integ.ip == integ.tip and not integ.items


def test_abort_restores_exactly_written_paths(world, run):
    """AC30 / AC43 shape: --abort restores the bytes pull wrote."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nT\n", "docs/added.md": "x\n"})
    w.commit_source({"docs/guide.md": "# Guide\nO\n"})
    before = _source_bytes(w)
    assert _pull_yes(run, w, "--accept", "docs/added.md").code == 8
    assert (w.src / "docs" / "added.md").exists()
    assert _pull(run, w, "--abort").code == 0
    assert _source_bytes(w) == before
    assert git_run("status", "--porcelain", cwd=w.src) == ""


def test_sigterm_after_first_write_leaves_s4_then_abort(world, run, monkeypatch, tmp_path):
    """AC43 (L1): pull.after_write:1 + sigterm → in progress; abort restores."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nnew\n", "docs/x.md": "x\n"})
    _r, digest, tip = _plan(run, w)
    before = _source_bytes(w)
    env = {**os.environ, "PUBREPO_TEST_FAULT": "pull.after_write:1:sigterm",
           "GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}
    from conftest import SCRIPT_PATH
    child = subprocess.run(
        [os.sys.executable, "-c",
         "import importlib.machinery, importlib.util, sys\n"
         f"l = importlib.machinery.SourceFileLoader('pubrepo', {str(SCRIPT_PATH)!r})\n"
         "s = importlib.util.spec_from_loader('pubrepo', l); m = importlib.util.module_from_spec(s)\n"
         "sys.modules['pubrepo'] = m; l.exec_module(m)\n"
         f"sys.exit(m.main(['pull', '--yes', '--expect-tip', {tip!r}, '--expect-plan', {digest!r}]))\n"],
        cwd=w.src, env=env, capture_output=True, text=True)
    assert child.returncode != 0
    assert pubrepo.pull_in_progress(w.src) is not None
    assert _pull(run, w).code == 8                                 # S4 in this worktree
    assert _pull(run, w, "--abort").code == 0
    assert _source_bytes(w) == before


def test_transformed_path_never_written(world, run):
    """AC32 (§5.8): resolved/decline only; never written automatically."""
    w = world(transforms={"docs/guide.md": [{"find": "PRIVATE", "replace": "public"}]},
              files={"a.py": "a\n", "docs/guide.md": "# Guide PRIVATE\n"})
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide public edited\n"})
    r = _pull(run, w)
    assert r.code == 8
    r = _pull_yes(run, w, "--decline", "docs/guide.md")
    assert r.code == 0
    assert (w.src / "docs" / "guide.md").read_text() == "# Guide PRIVATE\n"


def test_no_accept_for_symlink_or_hard_excluded(world, run):
    """AC39: no accept for a symlink or a hard-excluded path."""
    w = world()
    w.snapshot()
    clone = w.pub.parent / "c39"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    os.symlink("guide.md", clone / "docs" / "link.md")
    (clone / "docs" / ".env").write_text("X=1\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "c39", cwd=clone)
    git_run("push", "-q", "origin", "HEAD:main", cwd=clone)
    r, _d, _t = _plan(run, w)
    assert r.code == 5
    assert _pull(run, w, "--plan", "--accept", "docs/link.md").code == 1
    assert _pull(run, w, "--plan", "--accept", "docs/.env").code == 1


def test_rename_by_them_is_delete_plus_add(world, run):
    """AC50: a rename is delete + add; no resurrection."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/renamed.md": "# Guide\n", "docs/guide.md": None})
    r = _pull_yes(run, w, "--delete", "docs/guide.md")
    assert r.code == 0, r.stderr
    assert not (w.src / "docs" / "guide.md").exists()
    assert (w.src / "docs" / "renamed.md").read_text() == "# Guide\n"


@pytest.mark.parametrize("case", ["C4", "C5", "C6", "C7"])
def test_both_sides_cases_stop_at_8(world, run, case):
    """AC51 (C4–C7)."""
    w = world()
    w.snapshot()
    if case == "C4":
        w.collab_commit({"docs/guide.md": None})
        w.commit_source({"docs/guide.md": "# Guide\nours\n"})
    elif case == "C5":
        w.collab_commit({"docs/guide.md": "# Guide\ntheirs\n"})
        w.commit_source({"docs/guide.md": None})
    elif case == "C6":
        w.collab_commit({"docs/moved.md": "# Guide\n", "docs/guide.md": None})
        w.commit_source({"docs/guide.md": "# Guide\nours\n"})
    else:
        w.collab_commit({"docs/new.md": "theirs\n"})
        w.commit_source({"docs/new.md": "ours\n"})
    assert _pull(run, w).code == 8


def test_their_symlink_replacing_our_file_offers_ours(world, run):
    """AC82: T change on a projected path → `ours` restores ours."""
    w = world()
    w.snapshot()
    clone = w.pub.parent / "c82"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    (clone / "docs" / "guide.md").unlink()
    os.symlink("../a.py", clone / "docs" / "guide.md")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "c82", cwd=clone)
    git_run("push", "-q", "origin", "HEAD:main", cwd=clone)
    r, _d, _t = _plan(run, w)
    assert "--ours" in r.stdout
    assert _pull_yes(run, w, "--ours", "docs/guide.md").code == 0
    assert (w.src / "docs" / "guide.md").read_text() == "# Guide\n"


def test_keep_is_authorizing_no_resurface(world, run):
    """pull::keep_is_authorizing_no_resurface: C4 decided
    `--keep` is authorizing like `ours`: publish restores our file (exit
    0), and the next pull has no item for it."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": None})
    w.commit_source({"docs/guide.md": "# Guide\nours, kept\n"})
    assert _pull_yes(run, w, "--keep", "docs/guide.md").code == 0
    integ = w.integ()
    assert integ.accounted("docs/guide.md", None) and not integ.items
    r = run(["publish", "--yes"], cwd=w.src)
    assert r.code == 0, r.stderr
    git_run("fetch", "-q", "origin", cwd=w.pub)
    assert git_run("show", "origin/main:docs/guide.md", cwd=w.pub) == "# Guide\nours, kept\n"
    r, _d, _t = _plan(run, w)
    assert r.code == 0 and "docs/guide.md" not in r.stdout


def test_keep_is_authorizing_no_resurface_helper_record(world):
    """The helper-record variant of
    pull::keep_is_authorizing_no_resurface. A pull record deciding C4
    `keep` (theirs absent) accounts for the path by (ii), like `ours`; no
    item resurfaces."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": None})
    w.commit_source({"docs/guide.md": "# Guide\nours, kept\n"})
    w.pull_record([{"path": "docs/guide.md", "class": "C4", "decision": "keep",
                    "theirs": "absent"}], through=f)
    integ = w.integ()
    assert integ.ip == f and integ.accounted("docs/guide.md", None)
    assert "docs/guide.md" not in [i.path for i in integ.items]
    assert "keep" in pubrepo.AUTHORIZING_DECISIONS


# --- baseline, adoption, decisions over time (AC12, AC41, AC80, AC81) ---

def test_baseline_accept_writes_tip_blob(world, run):
    """AC81: baseline `accept` writes the tip blob; next publish no-op."""
    w = world()
    (w.src / "docs" / "draft.md").write_text("published draft\n")
    w.snapshot(extra={"docs/draft.md": "published draft\n"}, dirty=True)
    (w.src / "docs" / "draft.md").unlink()
    w.commit_source({"a.py": "a = 2\n"})
    assert _pull_yes(run, w, "--accept", "docs/draft.md").code == 0
    assert (w.src / "docs" / "draft.md").read_text() == "published draft\n"
    assert not w.integ().items


def test_baseline_accept_refused_when_untracked_in_source(world, run):
    """AC81 second half: commit-first refusal."""
    w = world()
    (w.src / "docs" / "draft.md").write_text("published\n")
    w.snapshot(extra={"docs/draft.md": "published\n"}, dirty=True)
    (w.src / "docs" / "draft.md").write_text("local untracked\n")
    r = _pull_yes(run, w, "--accept", "docs/draft.md")
    assert r.code == 1 and "untracked" in r.stderr


def test_leave_then_adopt_is_add_add(world, run):
    """AC12: leave, then adopt → S9 add/add, decide, integrated."""
    w = world()
    w.snapshot()
    w.collab_commit({"extra/n.md": "theirs\n"})
    assert _pull_yes(run, w).code == 0
    (w.src / CONFIG_REL).write_text((w.src / CONFIG_REL).read_text().replace(
        '"a.py", "docs"', '"a.py", "docs", "extra"'))
    (w.src / "extra").mkdir()
    (w.src / "extra" / "n.md").write_text("ours\n")
    git_run("add", "-A", cwd=w.src)
    git_run("commit", "-qm", "adopt extra", cwd=w.src)
    assert w.integ().state == "S9"
    assert _pull_yes(run, w, "--ours", "extra/n.md").code == 0
    assert not w.integ().items


def test_drop_from_include_then_leave_or_delete(world, run):
    """AC80: leave → carried, no item; delete → integrated."""
    w = world()
    w.snapshot()
    (w.src / CONFIG_REL).write_text((w.src / CONFIG_REL).read_text().replace(
        '"a.py", "docs"', '"a.py"'))
    git_run("commit", "-qam", "drop docs", cwd=w.src)
    w.collab_commit({"docs/guide.md": "# Guide\ncollab\n"})
    assert _pull(run, w).code == 8
    assert _pull_yes(run, w, "--leave", "docs/guide.md").code == 0
    assert not w.integ().items


def test_accept_then_local_edit_then_repush_is_fresh_conflict(world, run):
    """AC41 bullet 1: nothing is reused when ours changed."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nb\n"})
    assert _pull_yes(run, w).code == 0
    w.commit_source({"docs/guide.md": "# Guide\nlocal\n"})
    w.collab_commit({"docs/guide.md": "# Guide\nb2\n"})
    r, _d, _t = _plan(run, w)
    assert "previously decided" not in r.stdout
    assert _pull(run, w).code == 8


def test_decline_is_prefilled_and_counts_as_decision(world, run):
    """AC41 bullet 2: a prior decline is prefilled and shown; off a TTY
    it needs its flag (not decision-free)."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nunwanted\n"})
    assert _pull_yes(run, w, "--decline", "docs/guide.md").code == 0
    # The same public state is presented again (e.g. after a --redecide):
    r, _d, _t = _plan(run, w, "--redecide", "docs/guide.md")
    assert "previously decided" in r.stdout
    assert _pull_yes(run, w, "--redecide", "docs/guide.md").code == 1
    assert _pull_yes(run, w, "--redecide", "docs/guide.md", "--decline", "docs/guide.md").code == 0


# --- S7 acceptance (AC37, AC94) ---

def test_rewritten_history_accept_writes_dropped(world, run):
    """AC37/AC94: S7 → pull 8 until accepted; acceptance writes `dropped`
    for our unreachable recorded snapshot; integrated afterwards."""
    w = world()
    w.snapshot(record=True)
    w.commit_source({"a.py": "a = 2\n"})
    snap = w.snapshot(record=True)
    clone = w.pub.parent / "c37"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    git_run("reset", "-q", "--hard", "HEAD~1", cwd=clone)
    (clone / "notes.txt").write_text("rewrote\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "force", cwd=clone)
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=clone)
    assert w.integ().state == "S7"
    assert _pull(run, w).code == 8
    r, _d, _t = _plan(run, w)
    assert r.code == 5 and snap in r.stdout
    assert _pull_yes(run, w, "--accept-rewritten-history").code == 0
    refs = git_run("for-each-ref", "--format=%(refname)", "refs/pubrepo/status/", cwd=w.src)
    assert f"refs/pubrepo/status/{snap}/dropped" in refs
    assert w.integ().state != "S7"


# --- first pull on a never-integrated remote (AC38) ---

def test_first_pull_never_integrated(world, run):
    """AC38: carried, add/add, identical."""
    w = world()
    w.collab_commit({"a.py": "a = 1\n", "docs/other.md": "theirs only\n", "README.md": "r\n"})
    r, _d, _t = _plan(run, w)
    out = r.stdout
    assert "a.py  [C10]" in out
    assert "docs/other.md" in out and "README.md  [carried_add]" in out


# --- CRLF (AC44 with AC88) and zero transforms (AC45) ---

def test_crlf_disjoint_c1_clean_lf_blob(world, run):
    body = "".join(f"line {i}\n" for i in range(12))
    w = world(files={"a.py": "a\n", "docs/guide.md": body, ".gitattributes": "*.md text eol=crlf\n"})
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": body + "theirs end\n"})
    (w.src / "docs" / "guide.md").write_bytes(("ours top\n" + body).replace("\n", "\r\n").encode())
    git_run("commit", "-qam", "ours", cwd=w.src)
    assert _pull_yes(run, w).code == 0
    staged = git_run("ls-files", "-s", "docs/guide.md", cwd=w.src).split()[1]
    expected = subprocess.run(["git", "hash-object", "--stdin"], cwd=w.src, capture_output=True,
                              input=("ours top\n" + body + "theirs end\n").encode()).stdout.decode().strip()
    assert staged == expected
    assert b"\r\n" in (w.src / "docs" / "guide.md").read_bytes()


def test_zero_transforms_written_one_to_one(world, run):
    """AC45 (GF-4)."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# exact bytes\n"})
    assert _pull_yes(run, w).code == 0
    assert git_run("rev-parse", "HEAD:docs/guide.md", cwd=w.src).strip() == w.blob(f, "docs/guide.md")


# --- worktrees (AC42) ---

def test_abort_from_other_worktree_refused_naming_owner(world, run):
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nT\n"})
    w.commit_source({"docs/guide.md": "# Guide\nO\n"})
    assert _pull_yes(run, w).code == 8
    b = w.src.parent / "wt-b"
    git_run("worktree", "add", "-q", "-b", "wt-b", str(b), cwd=w.src)
    r = run(["pull", "--abort"], cwd=b)
    assert r.code == 1 and str(w.src.resolve()) in r.stderr
    assert "<<<<<<<" in (w.src / "docs" / "guide.md").read_text()


# --- staleness (§5.1 item 7) ---

def test_staleness_warning(world, run):
    w = world()
    w.snapshot()
    origin = git_run("remote", "get-url", "origin", cwd=w.src).strip()
    other = w.src.parent / "src-other"
    git_run("clone", "-q", origin, str(other))
    git_run("config", "user.name", "O", cwd=other)
    git_run("config", "user.email", "o@example.invalid", cwd=other)
    (other / "a.py").write_text("upstream moved\n")
    git_run("commit", "-qam", "up", cwd=other)
    git_run("push", "-q", "origin", "main", cwd=other)
    git_run("fetch", "-q", "origin", cwd=w.src)
    w.collab_commit({"docs/guide.md": "# Guide\ns\n"})
    r = _pull(run, w, "--plan")
    assert "behind its upstream" in r.stderr


# --- integrate alias entry point and release summary feed ---

def test_pull_decisions_since(world, run):
    w = world()
    w.snapshot()
    f1 = w.collab_commit({"docs/guide.md": "# Guide\n1\n"})
    assert _pull_yes(run, w).code == 0
    w.collab_commit({"docs/b.md": "b\n"})
    assert _pull_yes(run, w).code == 0
    dest = pubrepo.destination_identity(w.config())
    allx = pubrepo.pull_decisions_since(w.src, None, destination=dest)
    since = pubrepo.pull_decisions_since(w.src, f1, destination=dest)
    assert {d.path for d in allx} == {"docs/guide.md", "docs/b.md"}
    assert [d.path for d in since] == ["docs/b.md"]
    assert all(isinstance(d.flags, tuple) for d in allx)


def test_pull_decisions_since_filters_by_destination(world, run):
    """The release summary reads only its destination's records."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\n1\n"})
    assert _pull_yes(run, w).code == 0
    other = ("github.com/acme/elsewhere", "main")
    assert pubrepo.pull_decisions_since(w.src, None, destination=other) == []
    assert [d.path for d in pubrepo.pull_decisions_since(
        w.src, None, destination=pubrepo.destination_identity(w.config()))] == ["docs/guide.md"]


def test_delete_of_explicit_include_tells_owner_to_edit_config(world, run):
    """A `delete` of a path that `include` names
    explicitly is flagged on the plan item and, after the commit, pull names
    the edit and the config file. Pull never edits the config, and the next
    publish still refuses the missing include (exit 1, as in 1.0). A path
    that is not listed explicitly gets no note."""
    w = world(include=["a.py", "docs", "README.md"],
              files={"a.py": "a\n", "docs/guide.md": "# Guide\n", "README.md": "readme\n",
                     "docs/other.md": "o\n"})
    w.snapshot()
    w.collab_commit({"README.md": None, "docs/other.md": None})
    notice = ("README.md is listed explicitly in include; remove it from .publish.toml "
              "before the next publish")
    flags = ["--delete", "README.md", "--delete", "docs/other.md"]
    r, digest, tip = _plan(run, w, *flags)
    item_note = "include entry 'README.md' will name a missing path; remove it from include"
    assert item_note in r.stdout and "include entry 'docs/other.md'" not in r.stdout
    config_before = (w.src / ".publish.toml").read_bytes()
    r = _pull(run, w, "--yes", "--expect-tip", tip, "--expect-plan", digest, *flags)
    assert r.code == 0 and notice in r.stderr
    assert (w.src / ".publish.toml").read_bytes() == config_before          # never edited
    assert not (w.src / "README.md").exists()
    record = git_run("rev-parse", "HEAD", cwd=w.src).strip()[:12]
    r = run(["publish", "--yes"], cwd=w.src)
    assert r.code == 1
    assert (f"ERROR: include 'README.md' names a path removed by pull {record}; remove it "
            f"from include in .publish.toml") in r.stderr


def test_keep_of_explicit_include_has_no_notice(world, run):
    w = world(include=["a.py", "docs", "README.md"],
              files={"a.py": "a\n", "docs/guide.md": "# Guide\n", "README.md": "readme\n"})
    w.snapshot()
    w.collab_commit({"README.md": None})
    r, _digest, _tip = _plan(run, w, "--keep", "README.md")
    assert "will name a missing path" not in r.stdout


def test_ac42_worktree_matrix_command_level(world, run, public_remote):
    """AC42 (command-level half, shape, design §5.12): a pull in
    progress in worktree A → publish in A exits 8; in worktree B, `pull
    --abort` exits 1 naming A, and publishing a second destination from B
    proceeds (0) with a notice naming A."""
    w = world()
    url_b = public_remote()
    (w.src / ".publish-b.toml").write_text(
        f'[publish]\nremote = "{url_b}"\ninclude = ["a.py"]\ndir = ".publish-b"\n')
    (w.src / ".gitignore").write_text(".publish/\n.publish-b/\n")
    git_run("add", ".publish-b.toml", ".gitignore", cwd=w.src)
    git_run("commit", "-qm", "second target", cwd=w.src)
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nT\n"})
    w.commit_source({"docs/guide.md": "# Guide\nO\n"})
    assert _pull_yes(run, w).code == 8                         # conflict: in progress in A
    a_path = str(w.src.resolve())
    r = run(["publish", "--yes"], cwd=w.src)
    assert r.code == 8
    b = w.src.parent / "wt-b19"
    git_run("worktree", "add", "-q", "-b", "wt-b19", str(b), cwd=w.src)
    r = run(["pull", "--abort"], cwd=b)
    assert r.code == 1 and a_path in r.stderr
    assert run(["init", "--config", ".publish-b.toml"], cwd=b).code == 0
    r = run(["publish", "--yes", "--config", ".publish-b.toml"], cwd=b)
    assert r.code == 0, r.stderr
    assert a_path in r.stderr
    assert "<<<<<<<" in (w.src / "docs" / "guide.md").read_text()    # A untouched


def test_roundtrip_ff_hook(world, run, published_tree):
    """AC2 (pull::roundtrip_ff_hook): the remote rejects every non-fast-
    forward (pre-receive hook). After pull, publish exits 0 with F as the
    snapshot's parent; the collaborator's carried file is byte-identical."""
    w = world()
    w.snapshot()
    hook = Path(w.url[len("file://"):]) / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\nwhile read old new ref; do\n"
                    "  [ \"$old\" = 0000000000000000000000000000000000000000 ] && continue\n"
                    "  git merge-base --is-ancestor \"$old\" \"$new\" || "
                    "{ echo \"non-fast-forward refused\" >&2; exit 1; }\n"
                    "done\n")
    hook.chmod(0o755)
    f = w.collab_commit({"docs/guide.md": "# Guide\nfrom collab\n",
                         "notes/carried.txt": "public only, bytes kept \x01\n"})
    assert _pull_yes(run, w).code == 0
    w.commit_source({"a.py": "a = 'after pull'\n"})
    r = run(["publish", "--yes"], cwd=w.src)
    assert r.code == 0, r.stderr
    tip = git_run("ls-remote", w.url, "refs/heads/main").split()[0]
    git_run("fetch", "-q", "origin", cwd=w.pub)
    assert git_run("rev-parse", f"{tip}^", cwd=w.pub).strip() == f
    tree = published_tree(w.url)
    assert tree["notes/carried.txt"] == b"public only, bytes kept \x01\n"
    assert tree["docs/guide.md"] == b"# Guide\nfrom collab\n"
    assert tree["a.py"] == b"a = 'after pull'\n"


# --- round trips through the real publish (§3) ---

def _publish(run, w, *args, cwd=None):
    return run(["publish", "--yes", *args], cwd=cwd or w.src)


def _public_files(w):
    git_run("fetch", "-q", "origin", cwd=w.pub)
    return set(git_run("ls-tree", "-r", "--name-only", "origin/main", cwd=w.pub).split())


def test_ac13_full_baseline_ours_then_publish(world, run):
    """AC13 (full): legacy dirty snapshot with an untracked file → a later
    change → publish 8 (baseline item, never foreign) → pull --baseline
    --ours → publish 0."""
    w = world()
    (w.src / "docs" / "draft.md").write_text("untracked draft\n")
    w.snapshot(extra={"docs/draft.md": "untracked draft\n"}, dirty=True)
    (w.src / "docs" / "draft.md").write_text("owner's new draft\n")
    git_run("add", "docs/draft.md", cwd=w.src)
    git_run("commit", "-qm", "track the draft", cwd=w.src)
    assert _publish(run, w).code == 8
    assert _pull_yes(run, w, "--baseline", "--ours", "docs/draft.md").code == 0
    assert _publish(run, w).code == 0
    git_run("fetch", "-q", "origin", cwd=w.pub)
    assert git_run("show", "origin/main:docs/draft.md", cwd=w.pub) == "owner's new draft\n"


def test_ac14b2_full_pull_baseline_restore(world, run):
    """AC14b2 (full, real pull): a collaborator's amend deletes a carried
    file; publish 8; pull --baseline --restore → publish 0 with the file
    present; an unrelated edit and publish keep it."""
    w = world()
    w.snapshot(record=True)
    f = w.collab_commit({"notes.txt": "carried public-only\n"})
    assert _pull_yes(run, w).code == 0
    w.commit_source({"a.py": "a = 12\n"})
    assert _publish(run, w).code == 0
    clone = w.collab.path
    git_run("fetch", "-q", "origin", cwd=clone)
    git_run("reset", "-q", "--hard", "origin/main", cwd=clone)
    git_run("rm", "-q", "notes.txt", cwd=clone)
    git_run("commit", "-q", "--amend", "--no-edit", cwd=clone)
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=clone)
    assert _publish(run, w).code in (5, 8)
    r = _pull_yes(run, w, "--accept-rewritten-history", "--baseline", "--restore", "notes.txt")
    assert r.code == 0, r.stderr + r.stdout
    assert _publish(run, w).code == 0
    assert "notes.txt" in _public_files(w)
    w.commit_source({"a.py": "a = 13\n"})
    assert _publish(run, w).code == 0
    assert "notes.txt" in _public_files(w)


def test_ac24_redecide_remove_route(world, run):
    """AC24 (--redecide route): carried forbidden content is left public by
    pull; publish exits 0 and reports it; pull --redecide P --remove → the
    next publish deletes P, named."""
    w = world(scrub={"forbidden": ["zz-carried-canary-zz"]})
    w.snapshot()
    w.collab_commit({"notes/leak.txt": "zz-carried-canary-zz\n"})
    assert _pull_yes(run, w, "--leave", "notes/leak.txt").code == 0
    r = _publish(run, w)
    assert r.code == 0 and "notes/leak.txt" in r.stderr + r.stdout
    assert _pull_yes(run, w, "--redecide", "notes/leak.txt", "--remove", "notes/leak.txt").code == 0
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    assert "notes/leak.txt" not in _public_files(w)
    assert "notes/leak.txt" in r.stderr + r.stdout                 # the deletion is named


def test_ac77_full_feat_plus_foreign(world, run):
    """AC77: snapshot from `feat`, collaborator F, owner on main → publish 8;
    pull --accept-source-divergence=<snapshot> lists only F; publish 0."""
    w = world()
    git_run("checkout", "-q", "-b", "feat", cwd=w.src)
    w.commit_source({"a.py": "feat = 2\n"})
    snap = w.snapshot()
    git_run("checkout", "-q", "main", cwd=w.src)
    w.commit_source({"docs/other.md": "main work\n"})
    f = w.collab_commit({"docs/guide.md": "# Guide\ncollab\n"})
    assert _publish(run, w).code == 8
    flag = f"--accept-source-divergence={snap}"
    r, _d, _t = _plan(run, w, flag)
    assert f[:12] in r.stdout and snap[:12] not in r.stdout.split("unpulled commits")[1].split("items")[0]
    assert _pull_yes(run, w, flag).code == 0
    assert _publish(run, w).code == 0


def test_ac78_ci_clone_records_and_unreachable_records_remote(world, run, tmp_path):
    """AC78: a CI-shaped fresh clone of the source fetches records → no
    baseline items; with records_remote unreachable → a warning and publish
    succeeds on clean history."""
    w = world()
    (w.src / "docs" / "draft.md").write_text("untracked draft\n")
    snap = w.snapshot(extra={"docs/draft.md": "untracked draft\n"}, dirty=True, record=True)
    git_run("push", "-q", "origin", "main", cwd=w.src)
    assert pubrepo.records_push_pending(w.src, w.config()).ok
    ci = tmp_path / "ci-src"
    git_run("clone", "-q", git_run("remote", "get-url", "origin", cwd=w.src).strip(), str(ci))
    git_run("config", "user.name", "CI", cwd=ci)
    git_run("config", "user.email", "ci@example.invalid", cwd=ci)
    (ci / "docs" / "draft.md").write_text("untracked draft\n")       # the CI checkout builds it
    assert run(["init"], cwd=ci).code == 0
    r = run(["publish", "--yes"], cwd=ci)
    assert r.code == 0, r.stderr                                    # recorded W: no baseline item
    git_run("remote", "set-url", "origin", str(tmp_path / "nowhere.git"), cwd=ci)
    git_run("commit", "-q", "--allow-empty", "-m", "ci empty", cwd=ci)
    r = run(["publish", "--yes"], cwd=ci)
    assert r.code == 0 and "records not synced" in r.stderr


def test_ac87_full_accepted_then_edited(world, run):
    """AC87 (full): accept F's change to P by pull, publish (P untouched),
    edit P, publish → 0 and P is named M."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nfrom collab\n"})
    assert _pull_yes(run, w).code == 0
    w.commit_source({"a.py": "a = 'x'\n"})
    assert _publish(run, w).code == 0
    w.commit_source({"docs/guide.md": "# Guide\nfrom collab\nand owner\n"})
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    assert "docs/guide.md" in r.stderr + r.stdout


def test_ac90_full_newest_deciding_record(world, run):
    """AC90 (full): pull 1 accepts P; publish; C changes only Q; pull 2
    accepts Q; edit P; publish → 0."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nv2\n"})
    assert _pull_yes(run, w).code == 0
    assert _publish(run, w).code in (0,)
    w.collab_commit({"docs/q.md": "q\n"})
    assert _pull_yes(run, w).code == 0
    w.commit_source({"docs/guide.md": "# Guide\nv3 owner\n"})
    assert _publish(run, w).code == 0


def test_ac94_accept_rewritten_then_publish_twice(world, run):
    """AC94: a collaborator force-push drops our recorded snapshot →
    publish 5 → pull --accept-rewritten-history → publish 0 → publish 0."""
    w = world()
    w.snapshot(record=True)
    w.commit_source({"a.py": "a = 2\n"})
    assert _publish(run, w).code == 0
    clone = w.pub.parent / "c94"
    git_run("clone", "-q", w.url, str(clone))
    git_run("config", "user.name", "Collab Orator", cwd=clone)
    git_run("config", "user.email", "collab@example.invalid", cwd=clone)
    git_run("reset", "-q", "--hard", "HEAD~1", cwd=clone)
    (clone / "notes.txt").write_text("rewrote\n")
    git_run("add", "-A", cwd=clone)
    git_run("commit", "-qm", "force", cwd=clone)
    git_run("push", "-q", "-f", "origin", "HEAD:main", cwd=clone)
    assert _publish(run, w).code == 5
    assert _pull_yes(run, w, "--accept-rewritten-history").code == 0
    assert _publish(run, w).code == 0
    assert _publish(run, w).code == 0


def test_ac95_full_plan_names_copied_trailer(world, run):
    """AC95 (full): publish → 5; pull --plan lists F1 and C, with C noted
    "carries a copied `Source:` trailer"; docs/x.md is never deleted
    without a decision."""
    w = world()
    w.commit_source({"docs/x.md": "x\n"})
    p0 = w.snapshot()
    w.commit_source({"docs/x.md": None})
    w.snapshot()
    w.collab_commit({"README.md": "readme by collab\n"})
    clone = w.collab.path
    git_run("cherry-pick", "--allow-empty", "-x", p0, cwd=clone)
    git_run("push", "-q", "origin", "HEAD:main", cwd=clone)
    assert _publish(run, w).code == 5
    r, _d, _t = _plan(run, w)
    assert "carries a copied `Source:` trailer" in r.stdout
    line = next(l for l in r.stdout.splitlines() if l.strip().startswith("docs/x.md"))
    assert "delete" not in line.split("→", 1)[1]           # an ordinary item; never a silent deletion
    assert _pull_yes(run, w).code == 0 and (w.src / "docs" / "x.md").exists()
    assert _publish(run, w).code == 0 and "docs/x.md" in _public_files(w)


def test_ac97_records_remote_public_refused_publish_proceeds(world, run):
    """AC97: records_remote resolving to the public remote URL → sync
    refused with a warning, and publish proceeds. AC75 re-target (§10 item
    20, §18.11): the refusal now comes from the private-target
    check, which refuses the publish repo even when it is listed."""
    w = world()
    # AC75 re-target (§10 item 20, R1): records_remote names a remote whose
    # URL is the public remote; listing it does not help
    git_run("remote", "add", "pubrec", w.url, cwd=w.src)
    (w.src / ".publish.toml").write_text((w.src / ".publish.toml").read_text().replace(
        "private_remotes = [", f'records_remote = "pubrec"\nprivate_remotes = ["{w.url}", ', 1))
    git_run("commit", "-qam", "records to public (misconfigured)", cwd=w.src)
    assert run(["init"], cwd=w.src).code in (0, 1)
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    assert "refusing to push private refs refs/pubrepo/* to pubrec" in r.stderr
    assert f"is the publish remote {w.url}" in r.stderr
    assert r.stderr.count("refusing to push private refs") == 1
    assert "refs/pubrepo" not in git_run("ls-remote", w.url, cwd=w.src)


@pytest.mark.rewrite
def test_ac92_amend_sync_second_clone_no_s7(world, run, tmp_path):
    """AC92: --amend, sync, a fresh second clone fetching records, then
    publish on both → no S7 on either."""
    w = world()
    w.snapshot(record=True)
    w.commit_source({"a.py": "a = 2\n"})
    assert _publish(run, w).code == 0
    w.commit_source({"a.py": "a = 3\n"})
    r = run(["publish", "--yes", "--amend"], cwd=w.src)
    assert r.code == 0, r.stderr
    assert w.records_synced()                        # arrival: the --amend path (§18.11)
    git_run("push", "-q", "origin", "main", cwd=w.src)
    b = tmp_path / "second"
    git_run("clone", "-q", git_run("remote", "get-url", "origin", cwd=w.src).strip(), str(b))
    git_run("config", "user.name", "Owner", cwd=b)
    git_run("config", "user.email", "owner@example.invalid", cwd=b)
    assert run(["init"], cwd=b).code == 0
    assert run(["pull", "--plan"], cwd=b).code == 0
    ours = git_run("for-each-ref", "--format=%(refname) %(objectname)", "refs/pubrepo/snap/",
                   cwd=w.src)
    assert ours and git_run("for-each-ref", "--format=%(refname) %(objectname)",
                            "refs/pubrepo/snap/", cwd=b) == ours   # arrival: pull's fetch
    assert run(["publish", "--yes"], cwd=b).code == 0
    assert run(["publish", "--yes"], cwd=w.src).code == 0


def test_merge_no_ff_decline_shows_merged_decision_for_reference(world, run):
    """Design §5.7 (display only): a path decided on a
    --no-ff-merged topic is asked again, with "previously decided
    `<decision>` in <record> on a merged branch" shown. It is not a
    decision: the item still needs one, and the digest ignores the line."""
    w = world()
    w.snapshot()
    f = w.collab_commit({"docs/guide.md": "# Guide\ncollab topic\n"})
    theirs = w.blob(f, "docs/guide.md")
    git_run("checkout", "-q", "-b", "topic", cwd=w.src)
    rec = w.pull_record([{"path": "docs/guide.md", "decision": "decline", "theirs": theirs}],
                        through=f)
    git_run("checkout", "-q", "main", cwd=w.src)
    git_run("merge", "-q", "--no-ff", "-m", "merge topic", "topic", cwd=w.src)
    r, digest, _tip = _plan(run, w)
    assert r.code == 5
    assert f"previously decided `decline` in {rec[:12]} on a merged branch" in r.stdout
    item = next(l for l in r.stdout.splitlines() if l.strip().startswith("docs/guide.md"))
    assert "decision required" in item and "previously decided in" not in r.stdout
    assert _pull_yes(run, w).code == 1                         # still needs an explicit choice
    integ = w.integ()
    assert integ.decision_for("docs/guide.md", theirs) is None   # decision_for unchanged


def test_pull_hints_use_the_invoked_name(world, run, monkeypatch):
    """Pull's hints name the command as invoked
    (`_PROG`), e.g. `git-sync-publish pull --yes …`, never a literal
    `pubrepo`."""
    import sys
    monkeypatch.setattr(sys, "argv", ["/opt/bin/git-sync-publish"])
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nx\n"})
    r = _pull(run, w)                                   # off a TTY, decision-free → 1 + hint
    assert r.code == 1
    assert "git-sync-publish pull --yes --expect-tip" in r.stderr
    assert "pubrepo pull" not in r.stderr
    w.commit_source({"docs/guide.md": "# Guide\nO\n"})
    w.collab_commit({"docs/guide.md": "# Guide\nT\n"})
    assert _pull_yes(run, w).code == 8                  # conflict: in progress
    r = run(["publish", "--yes"], cwd=w.src)
    assert r.code == 8
    assert "`git-sync-publish pull --continue`" in r.stderr and "pubrepo pull" not in r.stderr


def test_redecide_changes_the_digest_even_on_an_existing_item(world, run):
    """--redecide on a path that is already a
    plan item changes the digest (each item's origin is hashed), so --yes
    bound to the --redecide digest without --redecide → 1."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nchanged\n"})
    _r, plain, tip = _plan(run, w)
    _r, redecided, _t = _plan(run, w, "--redecide", "docs/guide.md")
    assert plain != redecided
    r = _pull(run, w, "--yes", "--expect-tip", tip, "--expect-plan", redecided)
    assert r.code == 1
    assert (w.src / "docs" / "guide.md").read_text() == "# Guide\n"       # nothing written


def test_abort_restores_keep_writes_in_publish(world, run):
    """A pull that accepted a collaborator's keep edit wrote
    `.publish/<P>`; --abort restores the owner's bytes there too (the
    in-progress record lists keep writes)."""
    w = world(keep=["LICENSE"])
    owner = "owner licence text\n"
    w.snapshot(extra={"LICENSE": owner})
    w.collab_commit({"LICENSE": "collab licence edit\n", "docs/guide.md": "# Guide\nT\n"})
    w.commit_source({"docs/guide.md": "# Guide\nO\n"})
    r = _pull_yes(run, w)
    assert r.code == 8                                          # conflict: in progress
    assert (w.pub / "LICENSE").read_text() == "collab licence edit\n"
    assert _pull(run, w, "--abort").code == 0
    assert (w.pub / "LICENSE").read_text() == owner
    assert pubrepo.pull_in_progress(w.src) is None


def test_continue_records_relative_config_path(world, run):
    """The pull record written by --continue names the config
    relative to the source root (as the direct path does), never absolute."""
    w = world()
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide\nT\n"})
    w.commit_source({"docs/guide.md": "# Guide\nO\n"})
    assert _pull_yes(run, w).code == 8
    (w.src / "docs" / "guide.md").write_text("# Guide\nO and T\n")
    assert _pull(run, w, "--continue", "--yes").code == 0
    assert {e["config"] for e in _entries(w)} == {".publish.toml"}
