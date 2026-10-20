"""The 1.1.0 skeleton: config keys, exit 8, CLI surface,
the frozen §2.3 interfaces, the fault seam and the publish_lock guard."""
import dataclasses
import inspect
import os
import subprocess
import sys

import pytest

import pubrepo
from conftest import SCRIPT_PATH, config_toml

P = inspect.Parameter


def _base(**extra):
    config = {"remote": "file:///tmp/public.git", "branch": "main", "dir": ".publish",
              "include": ["a.py"], "exclude": [], "keep": []}
    config.update(extra)
    return config


def _errors(config):
    return [f for f in pubrepo.validate_config(config) if f["level"] == "error"]


def _warnings(config):
    return [f for f in pubrepo.validate_config(config) if f["level"] == "warning"]


# --- exit code and exception --------------------------------------------------

def test_decision_required_exits_8_and_diverged_stays_5():
    assert pubrepo.EXIT_DECISION_REQUIRED == 8
    assert issubclass(pubrepo.DecisionRequired, pubrepo.PubrepoError)
    assert pubrepo.DecisionRequired("x").exit_code == 8
    assert pubrepo.Diverged([], "x").exit_code == 5


# --- config keys ----------------------------------------------------------------

_RELEASE = {"enabled": True, "version_file": "a.py"}


def test_every_new_key_validates_without_unknown_warnings():
    config = _base(
        # AC75 re-target (§10 item 20, R1): records_remote names a git remote
        records_remote="backup", tag_remote="backup",
        private_remotes=["/srv/git/source.git"], public_hosts=["git.example.invalid"],
        peers=["../other/.publish.toml"], private=["notes/**"],
        scrub={"forbidden": ["x"], "allow_opaque": ["docs/*.png", "a.xlsx!xl/media/*"],
               "allow_markers": ["docs/conflicts.rst"]},
        release=dict(_RELEASE, adopt_tags=["v1.0.0"], strip_env=["MYCORP_*", "EXTRA_TOKEN"]),
    )
    assert _errors(config) == []
    assert not [f for f in _warnings(config) if "unknown key" in f["message"]]


@pytest.mark.parametrize("extra,field", [
    ({"records_remote": ""}, "records_remote"),
    ({"records_remote": 5}, "records_remote"),
    ({"peers": "x"}, "peers"),
    ({"peers": [""]}, "peers"),
    ({"private": [3]}, "private"),
    ({"scrub": {"allow_opaque": "docs/*.png"}}, "scrub.allow_opaque"),
    ({"scrub": {"allow_markers": [""]}}, "scrub.allow_markers"),
    ({"release": dict(_RELEASE, adopt_tags="v1.0.0")}, "release.adopt_tags"),
    ({"release": dict(_RELEASE, strip_env="FOO")}, "release.strip_env"),
])
def test_new_key_type_errors_name_the_field(extra, field):
    assert any(f["field"] == field for f in _errors(_base(**extra)))


@pytest.mark.parametrize("entry,valid", [
    ("FOO", True), ("FOO_*", True), ("_X1", True),
    ("*", False), ("FOO*BAR", False), ("1FOO", False), ("FOO-BAR", False), ("FOO_**", False),
])
def test_strip_env_entry_format(entry, valid):
    errors = _errors(_base(release=dict(_RELEASE, strip_env=[entry])))
    assert (not errors) is valid
    if not valid:
        assert errors[0]["field"] == "release.strip_env"


def test_unknown_keys_still_warn_including_phase2_pull_check():
    config = _base(pull={"check": "make test"}, bogus=1)
    messages = [f["message"] for f in _warnings(config)]
    assert "unknown key 'pull' in [publish] (ignored)" in messages
    assert "unknown key 'bogus' in [publish] (ignored)" in messages
    assert _errors(config) == []


def test_disabled_release_ignores_new_release_keys_with_one_warning():
    findings = pubrepo.validate_release_config(
        _base(release={"enabled": False, "adopt_tags": 5, "strip_env": ["*"]}))
    assert findings == [{"level": "warning", "field": "release",
                         "message": pubrepo.RELEASE_DISABLED_WARNING}]


@pytest.mark.no_git_calls  # AC63: refused before any git call
def test_validate_command_accepts_new_keys(source_repo, public_remote, run):
    repo = source_repo({"a.py": "x\n"}, {
        "remote": public_remote(), "include": ["a.py"], "peers": ["x.toml"],
        "scrub": {"forbidden": ["zzz"], "allow_opaque": ["*.png"]},
    })
    r = run(["validate"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "unknown key" not in r.stderr


# --- CLI surface -----------------------------------------------------------------

def test_new_commands_are_known():
    for command in ("pull", "repair", "release-retire"):
        assert command in pubrepo.KNOWN_COMMANDS


def test_pull_parses_every_flag():
    args = pubrepo.build_parser().parse_args([
        "pull", "--plan", "--yes", "--expect-tip", "a" * 40, "--expect-plan", "b" * 64,
        "--continue", "--abort", "--baseline", "--redecide", "p1", "--redecide", "p2",
        "--accept-rewritten-history", "--accept-source-divergence=" + "c" * 40,
        "--decisions", "d.tsv",
        *[x for d in pubrepo.PULL_DECISIONS for x in (f"--{d}", f"{d}.txt")],
        "--accept", "second.txt",
    ])
    assert args.plan and args.yes and args.continue_ and args.abort and args.baseline
    assert args.expect_tip == "a" * 40 and args.expect_plan == "b" * 64
    assert args.redecide == ["p1", "p2"]
    assert args.accept_source_divergence == "c" * 40
    assert args.decide_accept == ["accept.txt", "second.txt"]
    assert args.decide_accept_excluded == ["accept-excluded.txt"]
    assert len(pubrepo.PULL_DECISIONS) == 11


@pytest.mark.parametrize("argv,op", [
    (["repair", "rewind", "abc", "--plan"], "rewind"),
    (["repair", "remove-path", "a", "b", "--from", "abc", "--apply", "--yes",
      "--expect-tip", "t", "--expect-plan", "d", "--also-rewrite-ref", "refs/heads/x",
      "--also-delete-ref", "refs/heads/y", "--leave-reachable", "refs/tags/z",
      "--scan-refs", "refs/heads/*", "--json"], "remove-path"),
    (["repair", "restore-ancestry", "v1.0.0", "--plan"], "restore-ancestry"),
])
def test_repair_parses_each_operation(argv, op):
    args = pubrepo.build_parser().parse_args(argv)
    assert args.command == "repair" and args.repair_op == op


def test_repair_remove_path_requires_from_and_has_no_pattern_flag():
    parser = pubrepo.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["repair", "remove-path", "a"])
    with pytest.raises(SystemExit):
        parser.parse_args(["repair", "rewind", "abc", "--pattern", "x"])


def test_release_retire_and_publish_flags_parse():
    parser = pubrepo.build_parser()
    args = parser.parse_args(["release-retire", "v1.0.0", "--yes"])
    assert args.tag == "v1.0.0" and args.yes
    args = parser.parse_args(["publish", "--remote", "--accept-source-divergence", "s",
                              "--recreate-branch", "--accept-build-changes",
                              "--accept-unscanned", "a.png", "--accept-unscanned", "b.bin",
                              "--retry"])
    assert args.remote and args.recreate_branch and args.accept_build_changes and args.retry
    assert args.accept_unscanned == ["a.png", "b.bin"]
    assert args.force_overwrite is False  # still parsed, so it can be refused


@pytest.mark.no_git_calls  # AC63: refused before any git call
def test_bare_accept_source_divergence_exits_1(source_repo, public_remote, run):
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    assert run(["--accept-source-divergence"], cwd=repo).code == 1  # AC15


@pytest.mark.parametrize("flag", [["--accept-build-changes"], ["--accept-unscanned", "x.bin"]])
@pytest.mark.no_git_calls  # a flag conflict, refused before any git call
def test_release_acceptance_flags_without_a_release_are_flag_errors(source_repo, public_remote,
                                                                    run, flag):
    """Every 1.1.0 flag is built now: the
    release acceptance flags apply to --release/--bump only (exit 1)."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    r = run([*flag, "--yes"], cwd=repo)
    assert r.code == 1 and f"{flag[0]} applies to a release" in r.stderr


@pytest.mark.no_git_calls  # a flag conflict, refused before any git call
def test_retry_without_a_release_is_a_flag_error(source_repo, public_remote, run):
    """--retry applies to --release/--bump only (exit 1)."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    r = run(["--retry", "--yes"], cwd=repo)
    assert r.code == 1 and "--retry applies to a release" in r.stderr


# --- frozen §2.3 interfaces --------------------------------------------------------

def _shape(func):
    return [(p.name, p.kind, p.default) for p in inspect.signature(func).parameters.values()]


E, KW, PK = P.empty, P.KEYWORD_ONLY, P.POSITIONAL_OR_KEYWORD

FROZEN_FUNCTIONS = {
    # core → sync
    "managed_now": [("rel", PK, E), ("config", PK, E)],
    "build_projection": [("source_root", PK, E), ("publish_dir", PK, E), ("config", PK, E)],
    "projection_at": [("source_root", PK, E), ("publish_dir", PK, E), ("sigma", PK, E),
                      ("config_relpath", PK, E)],
    "project_blob": [("source_root", PK, E), ("publish_dir", PK, E), ("path", PK, E),
                     ("blob", PK, E), ("config", PK, E)],
    "gate_scan": [("publish_dir", PK, E), ("entries", PK, E), ("config", PK, E),
                  ("carried", KW, False)],
    "has_marker_triple": [("text", PK, E)],
    "extract_text": [("name", PK, E), ("data", PK, E)],
    "push_porcelain": [("publish_dir", PK, E), ("refspecs", PK, E), ("leases", KW, None),
                       ("atomic", KW, False)],
    "push_snapshot": [("publish_dir", PK, E), ("sha", PK, E), ("branch", PK, E)],
    "check_push_lock": [("publish_dir", PK, E)],
    "fetch_tip": [("publish_dir", PK, E), ("branch", PK, E)],
    "managed_release_targets": [("source_root", PK, E), ("publish_dir", PK, E), ("config", PK, E)],
    "snapshot_commit": [("publish_dir", PK, E), ("tree", PK, E), ("parents", PK, E),
                        ("message", PK, E)],
    "advance_local": [("publish_dir", PK, E), ("branch", PK, E), ("sha", PK, E), ("keep", PK, E),
                      ("replaced", KW, ())],
    "_test_fault": [("point", PK, E)],
    # accident prevention (design §17)
    "check_test_sandbox_url": [("url", PK, E)],
    "confirmation_mode": [("yes", KW, E), ("tty", KW, E)],
    "confirm_yes_no": [("summary", PK, E)],
    "build_commit_message": [("source_hash", PK, E), ("custom_message", KW, None),
                             ("added", KW, 0), ("modified", KW, 0), ("deleted", KW, 0),
                             ("transform_count", KW, 0), ("scrub_patterns", KW, 0)],
    # sync → core
    "source_lock": [("source_root", PK, E), ("exclusive", KW, E)],
    "assert_no_pull_in_progress": [("source_root", PK, E), ("command", KW, E)],
    "check_shallow": [("source_root", PK, E), ("publish_dir", PK, E)],
    "records_fetch": [("source_root", PK, E), ("config", PK, E)],
    "records_push_pending": [("source_root", PK, E), ("config", PK, E)],
    "unsynced_count": [("source_root", PK, E), ("config", PK, E)],
    "write_snapshot_record": [("source_root", PK, E), ("snapshot_sha", KW, E),
                              ("destination", KW, E), ("sigma_full", KW, E), ("written", KW, E),
                              ("nonderivable", KW, E), ("keep", KW, E), ("config_path", KW, E),
                              ("fingerprint", KW, E), ("decided", KW, None)],
    "write_status_marker": [("source_root", PK, E), ("snapshot_sha", PK, E), ("kind", PK, E),
                            ("reason", KW, E), ("new_sha", KW, None)],
    "register_target": [("source_root", PK, E), ("config_path", PK, E), ("destination", PK, E)],
    "records_has_destination": [("source_root", PK, E), ("destination", PK, E)],
    "is_retired": [("source_root", PK, E), ("tag", PK, E)],
    "compute_integration": [("source_root", PK, E), ("publish_dir", PK, E), ("config", PK, E),
                            ("tip", PK, E), ("proj", PK, E), ("accept_divergence", KW, E),
                            ("fetch_sigma", KW, E), ("config_relpath", KW, E)],
    "cmd_pull": [("source_root", PK, E), ("config", PK, E), ("args", PK, E)],
    "cmd_pull_plan": [("source_root", PK, E), ("config", PK, E), ("args", PK, E)],
    "cmd_repair": [("source_root", PK, E), ("config", PK, E), ("args", PK, E)],
    "cmd_release_retire": [("source_root", PK, E), ("config", PK, E), ("tag", PK, E),
                           ("yes", KW, E)],
    "pull_decisions_since": [("source_root", PK, E), ("public_sha", PK, E),
                             ("destination", KW, E)],
    "classify_commit": [("source_root", PK, E), ("publish_dir", PK, E), ("sha", PK, E)],
    "confirm_then_lock": [("source_root", PK, E), ("publish_dir", PK, E), ("compute", PK, E),
                          ("show_and_confirm", PK, E), ("expected", KW, None)],
    # §2.3 push targets (core)
    "remote_identity": [("url", PK, E)],
    "remote_url_values": [("repo", PK, E), ("name", PK, E)],
    "is_public_host": [("host", PK, E), ("config", PK, E)],
    "private_target_refusal": [("source_root", PK, E), ("config", PK, E), ("name", PK, E)],
    "build_push_plan": [("source_root", PK, E), ("publish_dir", PK, E), ("config", PK, E),
                        ("kind", KW, E), ("refs", KW, E)],
    "write_bookmark": [("source_root", PK, E)],
    "push_bookmark": [("source_root", PK, E), ("config", PK, E), ("ref", PK, E)],
    "check_publish_clone_origin": [("publish_dir", PK, E), ("config", PK, E)],
    "assert_parent_provenance": [("publish_dir", PK, E), ("parents", PK, E), ("tip", KW, E),
                                 ("fetched", KW, E), ("created", KW, E)],
    "leaked_ref_findings": [("source_root", PK, E), ("config", PK, E)],
    # the caller names the remote
    "private_remotes_line": [("source_root", PK, E), ("config", PK, E), ("name", PK, E)],
}

FROZEN_DATACLASSES = {
    "ProjEntry": ["blob", "mode", "nonderivable", "keep", "transformed"],
    "Projection": ["entries", "dirty_paths", "config"],
    "GateFinding": ["path", "part", "kind", "detail", "allowlisted"],
    "GateResult": ["findings"],
    "PushOutcome": ["status", "reason", "per_ref"],
    "Binding": ["tips", "digest", "managed_tree", "accepted_snapshot", "changed_paths", "required"],
    "SyncReport": ["remote", "skipped", "ok", "created", "rejected", "unsynced"],
    "Identity": ["sha", "kind", "sigma", "trailer", "record_ref", "via_repair"],
    "CommitInfo": ["sha", "author", "subject", "paths"],
    "PullItem": ["path", "cls", "base", "ours", "theirs", "origin", "snapshot", "flags",
                 "options", "default", "prefilled_from"],
    "DivergenceInfo": ["snapshot", "sigma", "kind", "branches", "accept_flag"],
    "Hit": ["path", "blob", "repair", "commit"],
    "Decision": ["path", "decision", "cls", "flags", "theirs", "result", "record"],
    "IntegrationState": ["state", "tip", "ip", "unpulled", "items", "missing_snapshots",
                         "divergence", "reintroduced", "copied_trailer", "baseline_items"],
    "RemoteId": ["host", "port", "path"],
    "PushPlanEntry": ["ref", "remote", "cls", "refusal"],
}


@pytest.mark.parametrize("name", sorted(FROZEN_FUNCTIONS))
def test_frozen_function_signature(name):
    assert _shape(getattr(pubrepo, name)) == FROZEN_FUNCTIONS[name]


@pytest.mark.parametrize("name", sorted(FROZEN_DATACLASSES))
def test_frozen_dataclass_fields(name):
    fields = [f.name for f in dataclasses.fields(getattr(pubrepo, name))]
    assert fields == FROZEN_DATACLASSES[name]


def test_binding_required_is_excluded_from_equality():
    a = pubrepo.Binding((("refs/heads/main", "a" * 40),), "d", None, None, ())
    b = dataclasses.replace(a, required=frozenset({"tips", "digest"}))
    assert a == b and b.required == frozenset({"tips", "digest"})


def test_push_outcome_vocabulary():
    hints = inspect.get_annotations(pubrepo.PushOutcome, eval_str=True)
    assert set(hints["status"].__args__) == {
        "ok", "rejected_nonff", "lease_stale", "host_refused", "atomic_unsupported",
        "ambiguous_landed", "ambiguous_absent"}
    assert set(pubrepo.PerRefStatus.__args__) == {
        "ok", "rejected_nonff", "lease_stale", "host_refused", "atomic_collateral"}


# Frozen names whose bodies are still stubs; existing commands must never
# reach one. The §2.3 push-target interface was stubbed first so
# the sync side can code against it; then remote_identity, remote_url_values,
# is_public_host, private_target_refusal and private_remotes_line,
# write_bookmark and push_bookmark, build_push_plan,
# check_publish_clone_origin and assert_parent_provenance, and
# leaked_ref_findings. None remain.
UNBUILT_STUBS: list[str] = []


def test_stubs_are_unreachable_from_existing_commands(source_repo, public_remote, run,
                                                      monkeypatch):
    called = []
    for name in UNBUILT_STUBS:
        monkeypatch.setattr(pubrepo, name, lambda *a, _n=name, **k: called.append(_n))
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"],
                                         "keep": ["LICENSE"]})
    for argv in (["init"], ["validate"], ["--dry-run"], ["--diff"], ["status"],
                 ["status", "--remote"], [], ["--stage"], ["integrate"]):
        r = run(argv, cwd=repo)
        assert r.code == 0, (argv, r.stderr)
    assert called == []


# --- fault seam -------------------------------------------------------------------

def test_fault_seam_is_inert_when_unset(monkeypatch):
    monkeypatch.delenv("PUBREPO_TEST_FAULT", raising=False)
    for point in ("publish.after_push", "pull.after_write:1", "confirm.before_lock", "nonsense"):
        assert pubrepo._test_fault(point) is None


def test_fault_seam_ignores_other_points(monkeypatch):
    monkeypatch.setenv("PUBREPO_TEST_FAULT", "pull.after_write:2:exit128")
    pubrepo._test_fault("pull.after_write:1")
    pubrepo._test_fault("publish.after_push")


def test_fault_seam_rejects_typos(monkeypatch):
    monkeypatch.setenv("PUBREPO_TEST_FAULT", "publish.after_psh:sigterm")
    with pytest.raises(ValueError, match="unknown point"):
        pubrepo._test_fault("publish.after_push")
    monkeypatch.setenv("PUBREPO_TEST_FAULT", "publish.after_push:explode")
    with pytest.raises(ValueError, match="unknown action"):
        pubrepo._test_fault("publish.after_push")


def test_fault_seam_pause_file_releases_when_file_exists(fault_seam):
    flag = fault_seam("confirm.before_lock", "pause-file")
    flag.write_text("")
    pubrepo._test_fault("confirm.before_lock")  # returns at once: the file exists


@pytest.mark.parametrize("action,expected", [("exit128", 128), ("sigterm", -15)])
def test_fault_seam_actions_in_a_child_process(action, expected, tmp_path):
    code = ("import importlib.machinery, importlib.util\n"
            f"loader = importlib.machinery.SourceFileLoader('pubrepo', {str(SCRIPT_PATH)!r})\n"
            "spec = importlib.util.spec_from_loader('pubrepo', loader)\n"
            "mod = importlib.util.module_from_spec(spec); loader.exec_module(mod)\n"
            "mod._test_fault('publish.after_push')\n")
    # §17.4 sandbox (d): the child env carries the sandbox variable.
    env = {"PATH": "/usr/bin:/bin", "PUBREPO_TEST_FAULT": f"publish.after_push:{action}",
           "PUBREPO_TEST_SANDBOX": os.environ["PUBREPO_TEST_SANDBOX"]}
    proc = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path)
    assert proc.returncode == expected


# --- publish_lock re-entrancy guard ------------------------------------------

def test_publish_lock_reentrant_assertion(tmp_path):
    publish_dir = tmp_path / ".publish"
    (publish_dir / ".git").mkdir(parents=True)
    with pubrepo.publish_lock(publish_dir):
        with pytest.raises(AssertionError, match="re-acquired"):
            with pubrepo.publish_lock(publish_dir):
                pass
    with pubrepo.publish_lock(publish_dir):  # acquire-after-release is allowed
        pass
    other = tmp_path / ".publish-other"
    (other / ".git").mkdir(parents=True)
    with pubrepo.publish_lock(publish_dir), pubrepo.publish_lock(other):
        pass  # distinct destinations are distinct locks


def test_publish_lock_released_after_a_failing_body(tmp_path):
    publish_dir = tmp_path / ".publish"
    (publish_dir / ".git").mkdir(parents=True)
    with pytest.raises(RuntimeError):
        with pubrepo.publish_lock(publish_dir):
            raise RuntimeError("boom")
    with pubrepo.publish_lock(publish_dir):
        pass


# --- shared fixture module ----------------------------------------------------------

def test_ff_fixtures_registered(bare_remote, collab_clone, git_recorder, tmp_path):
    remote = bare_remote()
    hook = remote.install_hook("pre-receive", "#!/bin/sh\nexit 0\n")
    assert hook.stat().st_mode & 0o111
    seed = collab_clone(remote.url)
    sha = seed.commit({"README.md": "hello\n"}, "seed")
    assert len(sha) == 40
    assert any(argv[:1] == ["push"] for argv, _cwd in git_recorder.calls())
    from ff_fixtures import ooxml_full_xlsx
    files = ooxml_full_xlsx(tmp_path / "canaries.xlsx")
    assert "xl/sharedStrings.xml" in files and (tmp_path / "canaries.xlsx").stat().st_size > 0


def test_config_toml_serializes_new_keys():
    text = config_toml({"remote": "r", "include": ["a"], "peers": ["p"],
                        "scrub": {"allow_opaque": ["*.png"]}})
    assert 'peers = ["p"]' in text and 'allow_opaque = ["*.png"]' in text


# --- retired pre-1.1.0 surface (design §17) ------------------------------------

@pytest.mark.no_git_calls      # validate reads no git state here
@pytest.mark.parametrize("key, value", [("unattended_push", ["file:///x.git main"]),
                                        ("unattended_ssh_key_env", "PUBREPO_DEPLOY_KEY")])
def test_retired_unattended_keys_are_unknown_keys(source_repo, public_remote, run, key, value):
    """The keys retired before 1.1.0; a config
    that still has one gets the standard unknown-key warning."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                          key: value})
    r = run(["validate"], cwd=repo)
    assert f"unknown key '{key}' in [publish] (ignored)" in r.stdout + r.stderr


@pytest.mark.no_git_calls      # argparse refuses before config load
def test_validate_publish_auth_is_retired(tmp_path, run):
    """`validate --publish-auth` is gone (usage error, 1)."""
    assert run(["validate", "--publish-auth"], cwd=tmp_path).code == 1


def test_yes_parses_for_every_publish_form():
    parser = pubrepo.build_parser()
    for argv in (["publish", "--yes"], ["publish", "--yes", "--no-release"],
                 ["publish", "--yes", "-m", "t"]):
        assert parser.parse_args(argv).yes is True




def test_no_quoted_annotations_on_dataclass_fields():
    """A quoted forward reference on a dataclass field makes `dataclasses`
    look the module up in sys.modules, which loaders such as the subprocess
    test helpers (SourceFileLoader without registration) skip: the import
    then dies."""
    import ast
    tree = ast.parse(SCRIPT_PATH.read_text())
    quoted = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        names = {d.id if isinstance(d, ast.Name) else getattr(getattr(d, "func", None), "id", "")
                 for d in node.decorator_list}
        if "dataclass" not in names:
            continue
        quoted += [f"{node.name}.{st.target.id}" for st in node.body
                   if isinstance(st, ast.AnnAssign) and isinstance(st.annotation, ast.Constant)]
    assert quoted == []


def test_nested_keep_error_states_the_rule_without_1_0_wording():
    """Docs match: the keep error states the rule;
    1.1.0 has no nuke, and nested keeps are not planned for "v1.1"."""
    [error] = _errors(_base(keep=["docs/LICENSE"]))
    assert "top-level-only" in error["message"]
    assert "nuke" not in error["message"] and "v1.1" not in error["message"]
