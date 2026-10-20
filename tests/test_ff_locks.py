"""Source lock, in-progress record, confirm_then_lock (design §5.12,
§5.13; AC42).

These are unit-level tests against the module. The command-level halves of
AC42 (publish in worktree B while A has a pull in progress, `status` during
a publish, `pull --yes` without `--expect-plan`) are tested with publish
and pull.
"""
import dataclasses
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from conftest import git_run, pubrepo

TIP_A = "a" * 40
TIP_B = "b" * 40
MAIN = "refs/heads/main"


def _binding(tip=TIP_A, digest="d1", **kw) -> "pubrepo.Binding":
    return pubrepo.Binding(tips=((MAIN, tip),), digest=digest, managed_tree=None,
                           accepted_snapshot=None, changed_paths=(), **kw)


def _in_progress(worktree: Path, **kw) -> "pubrepo.InProgress":
    fields = dict(worktree=str(worktree.resolve()), branch="main",
                  git_dir=str((worktree / ".git").resolve()), config_path=".publish.toml",
                  digest="f" * 64, written={"a.py": "0" * 40, "new.txt": None})
    fields.update(kw)
    return pubrepo.InProgress(**fields)


def _publish_dir(repo: Path) -> Path:
    pub = repo / ".publish"
    git_run("init", "-q", "-b", "main", str(pub))
    return pub


# --- source_lock ---

def test_source_lock_lives_in_common_dir_shared_by_worktrees(source_worktrees, lock_holder):
    wt = source_worktrees()
    lock_holder.hold(wt.a, "source-ex")
    r = lock_holder.try_take(wt.b, "source-ex")
    lock_holder.release()
    assert r.returncode == 3
    assert (wt.a / ".git" / "pubrepo-source.lock").exists()
    assert not (wt.b / ".git").is_dir()  # linked worktree: .git is a file


def test_exclusive_contention_names_holder_pid(source_repo, lock_holder):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    holder = lock_holder.hold(repo, "source-ex")
    with pytest.raises(pubrepo.LockContention) as e:
        with pubrepo.source_lock(repo, exclusive=True):
            pass
    lock_holder.release()
    assert e.value.exit_code == 3
    assert f"PID {holder.pid}" in str(e.value)


def test_shared_holders_coexist_and_block_exclusive(source_repo, lock_holder):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    lock_holder.hold(repo, "source-sh")
    with pubrepo.source_lock(repo, exclusive=False):
        pass
    with pytest.raises(pubrepo.LockContention) as e:
        with pubrepo.source_lock(repo, exclusive=True):
            pass
    lock_holder.release()
    assert "read-only pubrepo command" in str(e.value)


def test_exclusive_release_clears_pid(source_repo):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    lock_file = repo / ".git" / "pubrepo-source.lock"
    with pubrepo.source_lock(repo, exclusive=True):
        assert lock_file.read_text().strip().isdigit()
    assert lock_file.read_text() == ""


@pytest.mark.parametrize("first,second", [(True, True), (True, False), (False, False),
                                          (False, True)])
def test_source_lock_reentry_is_assertion_not_contention(source_repo, first, second):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    with pubrepo.source_lock(repo, exclusive=first):
        with pytest.raises(AssertionError, match="re-acquired"):
            with pubrepo.source_lock(repo, exclusive=second):
                pass
    with pubrepo.source_lock(repo, exclusive=second):  # acquire after release is fine
        pass


def test_publish_lock_reentry_is_assertion_not_contention(source_repo):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    pub = _publish_dir(repo)
    with pubrepo.publish_lock(pub):
        with pytest.raises(AssertionError, match="re-acquired"):
            with pubrepo.publish_lock(pub):
                pass
    with pubrepo.publish_lock(pub):
        pass


def test_reentry_guard_releases_on_contention(source_repo, lock_holder):
    """A failed acquisition must not leave the per-process guard set."""
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    lock_holder.hold(repo, "source-ex")
    with pytest.raises(pubrepo.LockContention):
        with pubrepo.source_lock(repo, exclusive=True):
            pass
    lock_holder.release()
    with pubrepo.source_lock(repo, exclusive=True):
        pass


def test_lock_order_stress_never_deadlocks(source_repo, lock_holder, tmp_path):
    """Twenty rounds of two processes each taking source (exclusive) then
    destination: every child finishes, each acquiring (0) or contending (3)."""
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    _publish_dir(repo)
    script = tmp_path / "order_child.py"
    from conftest import SCRIPT_PATH
    script.write_text(
        "import importlib.machinery, importlib.util, sys, time\n"
        "from pathlib import Path\n"
        f"loader = importlib.machinery.SourceFileLoader('pubrepo', {str(SCRIPT_PATH)!r})\n"
        "spec = importlib.util.spec_from_loader('pubrepo', loader)\n"
        "m = importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
        "repo = Path(sys.argv[1])\n"
        "try:\n"
        "    with m.source_lock(repo, exclusive=True):\n"
        "        with m.publish_lock(repo / '.publish'):\n"
        "            time.sleep(0.01)\n"
        "except m.LockContention:\n"
        "    sys.exit(3)\n")
    import sys
    for _ in range(20):
        procs = [subprocess.Popen([sys.executable, str(script), str(repo)], cwd=tmp_path)
                 for _ in range(2)]
        codes = [p.wait(timeout=30) for p in procs]
        assert set(codes) <= {0, 3}, codes
        assert 0 in codes or codes == [3, 3]


# --- in-progress record ---

def test_in_progress_roundtrip_and_clear(source_repo):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    assert pubrepo.pull_in_progress(repo) is None
    rec = _in_progress(repo)
    pubrepo.write_in_progress(repo, rec)
    path = repo / ".git" / "pubrepo" / "pull-inprogress.json"
    assert json.loads(path.read_text())["format"] == 1
    assert pubrepo.pull_in_progress(repo) == rec
    assert sorted(p.name for p in path.parent.iterdir()) == ["pull-inprogress.json"]
    pubrepo.clear_in_progress(repo)
    assert pubrepo.pull_in_progress(repo) is None
    pubrepo.clear_in_progress(repo)  # idempotent


def test_in_progress_visible_from_other_worktree(source_worktrees):
    wt = source_worktrees()
    pubrepo.write_in_progress(wt.a, _in_progress(wt.a))
    assert pubrepo.pull_in_progress(wt.b).worktree == str(wt.a.resolve())


@pytest.mark.parametrize("content", ["{not json", '{"format": 99}', '{"format": 1}'])
def test_unreadable_in_progress_record_fails_closed(source_repo, content):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    path = repo / ".git" / "pubrepo" / "pull-inprogress.json"
    path.parent.mkdir()
    path.write_text(content)
    with pytest.raises(pubrepo.ConfigError, match="unreadable"):
        pubrepo.pull_in_progress(repo)
    with pytest.raises(pubrepo.ConfigError):
        pubrepo.assert_no_pull_in_progress(repo, command="publish")


# --- assert_no_pull_in_progress (AC42 module half; §5.12) ---

def test_owner_worktree_s4_matrix(source_worktrees):
    wt = source_worktrees()
    pubrepo.write_in_progress(wt.a, _in_progress(wt.a))
    for command in ("publish", "pull", "--stage", "repair", "--amend"):
        with pytest.raises(pubrepo.DecisionRequired) as e:
            pubrepo.assert_no_pull_in_progress(wt.a, command=command)
        assert e.value.exit_code == 8
        assert "pull --continue" in str(e.value) and "pull --abort" in str(e.value)
    for command in ("status", "pull --plan", "pull --continue", "pull --abort"):
        pubrepo.assert_no_pull_in_progress(wt.a, command=command)


def test_other_worktree_gets_notice_and_cannot_continue_or_abort(source_worktrees, capsys):
    wt = source_worktrees()
    pubrepo.write_in_progress(wt.a, _in_progress(wt.a))
    pubrepo.assert_no_pull_in_progress(wt.b, command="publish")
    assert str(wt.a.resolve()) in capsys.readouterr().err
    for command in ("pull --continue", "pull --abort"):
        with pytest.raises(pubrepo.ConfigError) as e:
            pubrepo.assert_no_pull_in_progress(wt.b, command=command)
        assert e.value.exit_code == 1
        assert str(wt.a.resolve()) in str(e.value)


def test_no_pull_in_progress(source_repo):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    pubrepo.assert_no_pull_in_progress(repo, command="publish")
    with pytest.raises(pubrepo.ConfigError, match="no pull is in progress"):
        pubrepo.assert_no_pull_in_progress(repo, command="pull --continue")


# --- confirm_then_lock ---

class Computer:
    """A compute() double: returns the queued bindings in order, and records
    which locks were held at each call (seen from another process)."""

    def __init__(self, *bindings, probe=None):
        self.queue = list(bindings)
        self.calls = 0
        self.probe = probe
        self.probes = []

    def __call__(self):
        self.calls += 1
        if self.probe:
            self.probes.append(self.probe())
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


@pytest.fixture
def repo_pub(source_repo):
    repo = source_repo({"a.py": "x\n"}, {"remote": "file:///unused", "include": ["a.py"]})
    return repo, _publish_dir(repo)


def test_yields_holding_source_exclusive_and_destination(repo_pub, lock_holder):
    repo, pub = repo_pub
    compute = Computer(_binding())
    with pubrepo.confirm_then_lock(repo, pub, compute, lambda b: True) as bound:
        assert bound == _binding()
        assert lock_holder.try_take(repo, "source-sh").returncode == 3
        assert lock_holder.try_take(pub, "publish").returncode == 3
    assert compute.calls == 2
    assert lock_holder.try_take(repo, "source-ex").returncode == 0
    assert lock_holder.try_take(pub, "publish").returncode == 0


def test_first_compute_shared_lock_released_before_prompt(repo_pub, lock_holder):
    repo, pub = repo_pub
    seen = {}

    def confirm(b):
        seen["ex_during_prompt"] = lock_holder.try_take(repo, "source-ex").returncode
        return True

    compute = Computer(_binding(),
                       probe=lambda: lock_holder.try_take(repo, "source-sh").returncode)
    with pubrepo.confirm_then_lock(repo, pub, compute, confirm):
        pass
    assert compute.probes[0] == 0      # first compute: shared lock, readers coexist
    assert compute.probes[1] == 3      # recompute: exclusive
    assert seen["ex_during_prompt"] == 0   # no lock across the prompt


def test_no_publish_dir_takes_source_only(repo_pub, lock_holder):
    repo, pub = repo_pub
    with pubrepo.confirm_then_lock(repo, None, Computer(_binding()), None):
        assert lock_holder.try_take(pub, "publish").returncode == 0


def test_tip_moved_after_confirmation_exit5_body_not_run(repo_pub):
    repo, pub = repo_pub
    ran = []
    with pytest.raises(pubrepo.Diverged) as e:
        with pubrepo.confirm_then_lock(repo, pub, Computer(_binding(TIP_A), _binding(TIP_B)),
                                       lambda b: True):
            ran.append(1)
    assert e.value.exit_code == 5 and not ran
    assert TIP_A in str(e.value) and TIP_B in str(e.value)


def test_tip_moved_and_plan_changed_reports_tip_first(repo_pub):
    repo, pub = repo_pub
    with pytest.raises(pubrepo.Diverged):
        with pubrepo.confirm_then_lock(repo, pub, Computer(_binding(TIP_A, "d1"),
                                                           _binding(TIP_B, "d2")), None):
            pass


@pytest.mark.parametrize("change", [
    {"digest": "d2"}, {"managed_tree": "m2"}, {"accepted_snapshot": TIP_B},
    {"changed_paths": ("a.py",)}])
def test_plan_changed_after_confirmation_exit1(repo_pub, change):
    repo, pub = repo_pub
    after = dataclasses.replace(_binding(), **change)
    with pytest.raises(pubrepo.ConfigError) as e:
        with pubrepo.confirm_then_lock(repo, pub, Computer(_binding(), after), lambda b: True):
            pass
    assert e.value.exit_code == 1
    assert next(iter(change)) in str(e.value)


def test_new_tip_ref_after_confirmation_is_a_move(repo_pub):
    repo, pub = repo_pub
    after = dataclasses.replace(_binding(), tips=((MAIN, TIP_A), ("refs/heads/x", TIP_B)))
    with pytest.raises(pubrepo.Diverged):
        with pubrepo.confirm_then_lock(repo, pub, Computer(_binding(), after), None):
            pass


def test_declined_confirmation_writes_nothing_and_takes_no_exclusive(repo_pub, monkeypatch):
    repo, pub = repo_pub
    faults = []
    monkeypatch.setattr(pubrepo, "_test_fault", faults.append)
    compute = Computer(_binding())
    with pytest.raises(pubrepo.ConfigError, match="Cancelled"):
        with pubrepo.confirm_then_lock(repo, pub, compute, lambda b: False):
            pass
    assert compute.calls == 1 and faults == []


def test_missing_expect_plan_exit1_nothing_computed(repo_pub):
    """At the confirm_then_lock level: a required field
    that is None fails before any compute."""
    repo, pub = repo_pub
    compute = Computer(_binding())
    expected = _binding(digest=None, required=frozenset({"tips", "digest"}))
    with pytest.raises(pubrepo.ConfigError, match="digest") as e:
        with pubrepo.confirm_then_lock(repo, pub, compute, None, expected=expected):
            pass
    assert e.value.exit_code == 1 and compute.calls == 0


def test_expect_tip_matches_plan_changed_exit1(repo_pub):
    """The tip still matches --expect-tip, but the plan
    digest no longer matches --expect-plan."""
    repo, pub = repo_pub
    expected = _binding(TIP_A, "d-from-plan", required=frozenset({"tips", "digest"}))
    with pytest.raises(pubrepo.ConfigError, match="--expect-plan") as e:
        with pubrepo.confirm_then_lock(repo, pub, Computer(_binding(TIP_A, "d-now")), None,
                                       expected=expected):
            pass
    assert e.value.exit_code == 1


def test_expect_tip_mismatch_exit5(repo_pub):
    repo, pub = repo_pub
    expected = _binding(TIP_B, "d1", required=frozenset({"tips", "digest"}))
    with pytest.raises(pubrepo.Diverged):
        with pubrepo.confirm_then_lock(repo, pub, Computer(_binding(TIP_A, "d1")), None,
                                       expected=expected):
            pass


def test_expected_never_replaces_pre_post_comparison(repo_pub):
    """`expected` matching the recompute does not
    excuse a recompute that differs from the pre-lock compute."""
    repo, pub = repo_pub
    expected = _binding(TIP_A, "d2", required=frozenset({"tips", "digest"}))
    with pytest.raises(pubrepo.ConfigError):
        with pubrepo.confirm_then_lock(repo, pub, Computer(_binding(TIP_A, "d1"),
                                                           _binding(TIP_A, "d2")),
                                       None, expected=expected):
            pass


def test_expected_matching_yields(repo_pub):
    repo, pub = repo_pub
    expected = _binding(required=frozenset({"tips", "digest"}))
    with pubrepo.confirm_then_lock(repo, pub, Computer(_binding()), None,
                                   expected=expected) as bound:
        assert bound == _binding()


def test_caller_holding_a_lock_is_a_defect(repo_pub):
    repo, pub = repo_pub
    with pubrepo.publish_lock(pub):
        with pytest.raises(AssertionError):
            with pubrepo.confirm_then_lock(repo, pub, Computer(_binding()), None):
                pass
    with pubrepo.source_lock(repo, exclusive=False):
        with pytest.raises(AssertionError):
            with pubrepo.confirm_then_lock(repo, pub, Computer(_binding()), None):
                pass


def test_pause_at_confirm_before_lock_other_worktree_mutator_succeeds(
        source_worktrees, lock_holder, fault_seam):
    """While one process waits between confirmation and locking, a
    mutator in another worktree takes the exclusive lock (not 3)."""
    wt = source_worktrees()
    flag = fault_seam("confirm.before_lock", "pause-file")
    result = {}

    def run():
        try:
            with pubrepo.confirm_then_lock(wt.a, None, Computer(_binding()), lambda b: True):
                result["ok"] = True
        except BaseException as e:  # surfaced below
            result["error"] = e

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    assert t.is_alive(), result
    other = lock_holder.try_take(wt.b, "source-ex")
    flag.touch()
    t.join(timeout=30)
    assert other.returncode == 0, other.stderr
    assert result == {"ok": True}


def test_post_lock_recompute_catches_mutation_during_pause(
        source_worktrees, fault_seam, tmp_path):
    """A real change made in another worktree while this run is paused
    at confirm.before_lock is caught by the recompute (tip moved → 5)."""
    wt = source_worktrees()
    flag = fault_seam("confirm.before_lock", "pause-file")

    def compute():
        head = git_run("rev-parse", "refs/heads/wt-b", cwd=wt.a).strip()
        return _binding(head)

    result = {}

    def run():
        try:
            with pubrepo.confirm_then_lock(wt.a, None, compute, lambda b: True):
                result["ok"] = True
        except BaseException as e:
            result["error"] = e

    t = threading.Thread(target=run)
    t.start()
    time.sleep(0.3)
    (wt.b / "a.py").write_text("changed\n")
    git_run("commit", "-qam", "moved", cwd=wt.b)
    flag.touch()
    t.join(timeout=30)
    assert isinstance(result.get("error"), pubrepo.Diverged)


# --- orphaned in-progress record (design §5.12, AC42b) ---

def test_orphaned_record_abort(source_worktrees, capsys):
    """locks::orphaned_record_abort — the owner worktree is unregistered and
    absent on disk: `pull --abort` from another worktree is allowed, needs
    --yes off a TTY, restores nothing and clears the record."""
    wt = source_worktrees()
    pubrepo.write_in_progress(wt.a, _in_progress(wt.b))
    git_run("worktree", "remove", "--force", str(wt.b), cwd=wt.a)
    a_before = {p: p.read_bytes() for p in wt.a.rglob("*") if p.is_file() and ".git" not in p.parts}

    pubrepo.assert_no_pull_in_progress(wt.a, command="pull --abort")   # allowed
    with pytest.raises(pubrepo.ConfigError, match="--yes"):
        pubrepo.abort_orphaned_pull(wt.a, yes=False)                    # off a TTY
    assert pubrepo.pull_in_progress(wt.a) is not None

    assert pubrepo.abort_orphaned_pull(wt.a, yes=True) == 0
    err = capsys.readouterr()
    for named in (str(wt.b.resolve()), "main", "f" * 64):
        assert named in err.err + err.out
    assert pubrepo.pull_in_progress(wt.a) is None
    after = {p: p.read_bytes() for p in wt.a.rglob("*") if p.is_file() and ".git" not in p.parts}
    assert after == a_before


def test_orphaned_abort_tty_confirmation(source_worktrees, monkeypatch):
    wt = source_worktrees()
    pubrepo.write_in_progress(wt.a, _in_progress(wt.b))
    git_run("worktree", "remove", "--force", str(wt.b), cwd=wt.a)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: "n")
    with pytest.raises(pubrepo.ConfigError, match="Cancelled"):
        pubrepo.abort_orphaned_pull(wt.a, yes=False)
    assert pubrepo.pull_in_progress(wt.a) is not None
    monkeypatch.setattr("builtins.input", lambda: "y")
    assert pubrepo.abort_orphaned_pull(wt.a, yes=False) == 0
    assert pubrepo.pull_in_progress(wt.a) is None


def test_orphan_abort_refused_while_path_exists(source_worktrees):
    """Owner unregistered but its directory still exists → refused (1)."""
    wt = source_worktrees()
    stray = wt.a.parent / "stray-owner"
    stray.mkdir()
    pubrepo.write_in_progress(wt.a, _in_progress(stray, branch="gone"))
    with pytest.raises(pubrepo.ConfigError, match="must run in the worktree"):
        pubrepo.assert_no_pull_in_progress(wt.a, command="pull --abort")
    with pytest.raises(pubrepo.ConfigError):
        pubrepo.abort_orphaned_pull(wt.a, yes=True)
    assert pubrepo.pull_in_progress(wt.a) is not None


def test_orphan_abort_refused_while_still_registered(source_worktrees):
    """Directory deleted but still in `git worktree list` → refused (1)."""
    import shutil
    wt = source_worktrees()
    pubrepo.write_in_progress(wt.a, _in_progress(wt.b))
    owner = str(wt.b.resolve())
    shutil.rmtree(wt.b)
    with pytest.raises(pubrepo.ConfigError, match="still registered"):
        pubrepo.assert_no_pull_in_progress(wt.a, command="pull --abort")
    with pytest.raises(pubrepo.ConfigError):
        pubrepo.abort_orphaned_pull(wt.a, yes=True)
    assert pubrepo.pull_in_progress(wt.a).worktree == owner


def test_orphan_continue_still_refused(source_worktrees):
    wt = source_worktrees()
    pubrepo.write_in_progress(wt.a, _in_progress(wt.b))
    git_run("worktree", "remove", "--force", str(wt.b), cwd=wt.a)
    with pytest.raises(pubrepo.ConfigError, match="no longer exists"):
        pubrepo.assert_no_pull_in_progress(wt.a, command="pull --continue")


@pytest.mark.parametrize("args", [["status"], ["status", "--check"], ["status", "--remote"]])
def test_status_during_publish_proceeds_unlocked_without_fetch(world, run, lock_holder,
                                                               git_recorder, args):
    """AC42 bullet 4: while a mutator holds the exclusive source
    lock, status tries the shared lock, proceeds unlocked, never fetches
    (even with --remote), labels the output, and computes its exit from the
    last-known state (0 or 6)."""
    w = world()
    w.snapshot()
    lock_holder.hold(w.src, "source-ex")
    calls = len(git_recorder.calls())
    try:
        r = run(args, cwd=w.src)
    finally:
        lock_holder.release()
    assert r.code in (0, 6), r.stderr
    assert "another pubrepo command is running; showing last known state" in r.stderr + r.stdout
    assert not [a for a, _ in git_recorder.calls()[calls:] if a and a[0] in ("fetch", "ls-remote")]
