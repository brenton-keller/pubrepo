"""`resolved` for a text conflict comes only from
`pull --continue`. Up front, a C2/C7 `--resolved` wrote nothing, recorded the merge
blob with its markers, and the next publish replaced the collaborator's
text without asking. Transformed items keep the documented up-front
`resolved` (docs/integrating-changes.md "Transformed files").
"""
import json
import re

import pytest

import pubrepo
from conftest import git_run

REFUSED = re.compile(r"^ERROR: --resolved (?P<path>\S+): resolved is recorded by pull --continue "
                     r"after you edit the file; for an up-front decision use one of: "
                     r"(?P<options>.+)$", re.M)
THEIRS = "# Guide\ntheirs line\n"
OURS = "# Guide\nours line\n"


def _offered(stdout, path):
    lines = stdout.splitlines()
    for n, line in enumerate(lines):
        if line.strip().startswith(f"{path}  ["):
            return [o.lstrip("-") for o in lines[n + 1].strip()[len("options: "):].split(", ")]
    raise AssertionError(f"{path} not in the plan:\n{stdout}")


def _state(w):
    return (git_run("rev-parse", "HEAD", cwd=w.src), git_run("status", "--porcelain", cwd=w.src),
            git_run("for-each-ref", cwd=w.src), (w.src / "docs" / "guide.md").read_bytes(),
            pubrepo.pull_in_progress(w.src))


def _conflict(world, case):
    w = world()
    w.snapshot()
    if case == "C2":
        w.collab_commit({"docs/guide.md": THEIRS})
        w.commit_source({"docs/guide.md": OURS})
        return w, "docs/guide.md"
    w.collab_commit({"docs/new.md": "theirs\n"})          # add/add
    w.commit_source({"docs/new.md": "ours\n"})
    return w, "docs/new.md"


def _assert_refused(r, path, plan_stdout):
    assert r.code == 1, (r.code, r.stdout, r.stderr)
    m = REFUSED.search(r.stderr)
    assert m and m["path"] == path, r.stderr
    named = [o.lstrip("-") for o in m["options"].split(", ")]
    offered = _offered(plan_stdout, path)
    assert named and "resolved" not in named and set(named) <= set(offered)     # (iv)


@pytest.mark.parametrize("case", ["C2", "C7"])
def test_i_up_front_resolved_refused_and_publish_still_asks(world, run, case):
    """(i) The flag, in --plan, plain pull and a digest-bound --yes: exit 1,
    nothing written; the next publish --dry-run still stops for the
    unpulled public change instead of predicting ours over it."""
    w, path = _conflict(world, case)
    plan = run(["pull", "--plan"], cwd=w.src)
    assert plan.code == 5 and f"{path}  [{case}]" in plan.stdout
    digest = plan.stdout.strip().splitlines()[-1].split(": ", 1)[1]
    before = _state(w)
    for argv in (["pull", "--plan", "--resolved", path],
                 ["pull", "--resolved", path],
                 ["pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", digest,
                  "--resolved", path]):
        _assert_refused(run(argv, cwd=w.src), path, plan.stdout)
        assert _state(w) == before
    dry = run(["publish", "--dry-run"], cwd=w.src)
    assert dry.code == 5, (dry.stdout, dry.stderr)
    assert f"Pull items (1), left at public content:\n  {path} " in dry.stdout
    assert "Publish would exit 5" in dry.stdout
    assert "Files: 0 (0 added, 0 modified, 0 deleted)" in dry.stdout


@pytest.mark.parametrize("case", ["C2", "C7"])
def test_ii_decisions_file_line_refused(world, run, case, tmp_path):
    w, path = _conflict(world, case)
    plan = run(["pull", "--plan"], cwd=w.src)
    decisions = tmp_path / "decisions.tsv"
    decisions.write_text(f"{path}\tresolved\n")
    before = _state(w)
    for argv in (["pull", "--plan", "--decisions", str(decisions)],
                 ["pull", "--decisions", str(decisions)]):
        _assert_refused(run(argv, cwd=w.src), path, plan.stdout)
        assert _state(w) == before


def test_iii_continue_still_records_resolved_and_publishes_the_edit(world, run):
    """(iii) AC30 unchanged: markers, edit, --continue records `resolved`,
    and publish carries the edited content."""
    w, path = _conflict(world, "C2")
    plan = run(["pull", "--plan"], cwd=w.src)
    digest = plan.stdout.strip().splitlines()[-1].split(": ", 1)[1]
    r = run(["pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", digest], cwd=w.src)
    assert r.code == 8 and "<<<<<<< ours" in (w.src / path).read_text()
    (w.src / path).write_text("# Guide\nours and theirs\n")
    r = run(["pull", "--continue", "--yes"], cwd=w.src)
    assert r.code == 0, r.stderr
    body = git_run("log", "-1", "--format=%B", cwd=w.src)
    start = body.index("```json\n") + len("```json\n")
    entries = {e["path"]: e for e in json.loads(body[start:body.index("\n```", start)])}
    assert entries[path]["decision"] == "resolved"
    assert run(["publish", "--yes"], cwd=w.src).code == 0
    assert git_run("show", f"refs/heads/main:{path}",
                   cwd=w.url[len("file://"):]) == "# Guide\nours and theirs\n"


def test_v_transformed_up_front_resolved_after_hand_edit(world, run):
    """(v) Transformed items keep the up-front `resolved`: after the owner
    applies the public intent by hand, the record's result is the source's
    blob as projected (its published form), and the next --dry-run
    publishes that content without asking again."""
    w = world(transforms={"docs/guide.md": [{"find": "PRIVATE", "replace": "public"}]},
              files={"a.py": "a\n", "docs/guide.md": "# Guide PRIVATE\n"})
    w.snapshot()
    w.collab_commit({"docs/guide.md": "# Guide public edited\n"})
    w.commit_source({"docs/guide.md": "# Guide PRIVATE edited\n"})
    plan = run(["pull", "--plan", "--resolved", "docs/guide.md"], cwd=w.src)
    assert plan.code == 5 and "docs/guide.md  [transformed]" in plan.stdout
    digest = plan.stdout.strip().splitlines()[-1].split(": ", 1)[1]
    r = run(["pull", "--yes", "--expect-tip", w.tip(), "--expect-plan", digest,
             "--resolved", "docs/guide.md"], cwd=w.src)
    assert r.code == 0, r.stderr
    body = git_run("log", "-1", "--format=%B", cwd=w.src)
    start = body.index("```json\n") + len("```json\n")
    [entry] = [e for e in json.loads(body[start:body.index("\n```", start)])
               if e["path"] == "docs/guide.md"]
    live = pubrepo.build_projection(w.src, w.pub, w.config()).entries["docs/guide.md"].blob
    assert entry["decision"] == "resolved" and entry["result"] == live
    assert git_run("cat-file", "blob", live, cwd=w.pub) == "# Guide public edited\n"
    dry = run(["publish", "--dry-run"], cwd=w.src)
    assert dry.code == 0, dry.stderr
    assert run(["pull", "--plan"], cwd=w.src).code == 0
    assert run(["publish", "--yes"], cwd=w.src).code == 0
    assert git_run("show", "refs/heads/main:docs/guide.md",
                   cwd=w.url[len("file://"):]) == "# Guide public edited\n"
