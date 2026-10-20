"""Source-side records and their sync (design §2, §4.3, AC93, AC97), and
the named-remote rule: records go only to a named source remote that the
private-target check passes (§18.9 R1/R6, §18.11, §18.12; AC120, AC128,
AC129).

Command-level halves: AC78 (a
CI-shaped clone publishes with no baseline items) and AC92 (`--amend`,
sync, second clone) are in the publish and amend tests.
"""
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import git_run, pubrepo

# A local path that never exists: records code never contacts the public
# remote, and the sandbox wants no network-shaped remote in routine
# tests. Network-shaped values appear only through sandbox_unreachable.
PUBLIC = "file:///nonexistent/pubrepo-test-public.git"
DEST = ("github.com/acme/widget", "main")
S1 = "1" * 40
S2 = "2" * 40
SIGMA = "5" * 40


def _config(remote=PUBLIC, **kw):
    return {"remote": remote, "branch": "main", "include": ["a.py"], **kw}


def _listed(*values, **kw):
    """A config whose private_remotes lists `values` byte-exact (§18.11,
    §10 item 20)."""
    return _config(private_remotes=list(values), **kw)


@pytest.fixture
def two_clones(source_repo, tmp_path):
    """Source A (with a bare `origin`) and a second clone B of that origin.
    `origin` is the records remote by default (upstream of main)."""
    a = source_repo({"a.py": "x\n"}, _config())
    origin = git_run("remote", "get-url", "origin", cwd=a).strip()
    b = tmp_path / "clone-b"
    git_run("clone", "-q", origin, str(b))
    git_run("config", "user.name", "Owner Two", cwd=b)
    git_run("config", "user.email", "owner2@example.invalid", cwd=b)
    return a, b, origin


def _snap(repo, sha=S1, dest=DEST, written=None):
    return pubrepo.write_snapshot_record(
        repo, snapshot_sha=sha, destination=dest, sigma_full=SIGMA,
        written=written or {"a.py": "a" * 40}, nonderivable=[], keep=[],
        config_path=".publish.toml", fingerprint="f" * 64)


def _refs(repo, prefix="refs/pubrepo/"):
    out = git_run("for-each-ref", "--format=%(refname) %(objectname)", prefix, cwd=repo)
    return dict(line.split(" ") for line in out.splitlines())


# --- write_record / read_records ---

def test_record_shape_parentless_commit_with_record_json(source_repo):
    repo = source_repo({"a.py": "x\n"}, _config())
    sha = _snap(repo)
    assert _refs(repo) == {f"refs/pubrepo/snap/{S1}": sha}
    assert git_run("rev-list", "--parents", "-n1", sha, cwd=repo).split() == [sha]
    assert git_run("ls-tree", "--name-only", sha, cwd=repo).split() == ["record.json"]
    body = git_run("cat-file", "blob", f"{sha}:record.json", cwd=repo)
    data = json.loads(body)
    assert {"format", "destination", "source", "written", "nonderivable", "keep"} <= set(data)
    assert data["format"] == 1 and data["source"] == SIGMA
    assert data["destination"] == {"remote": DEST[0], "branch": "main"}
    assert body == json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    # records never touch the branch, index or worktree (§4.3)
    assert git_run("status", "--porcelain", cwd=repo) == ""
    assert git_run("log", "--format=%H", "main", cwd=repo).count("\n") == 1


def test_create_only_identical_rewrite_is_noop_and_different_conflicts(source_repo):
    repo = source_repo({"a.py": "x\n"}, _config())
    first = _snap(repo)
    assert _snap(repo) == first
    with pytest.raises(pubrepo.RecordConflict):
        _snap(repo, written={"a.py": "b" * 40})
    assert _refs(repo)[f"refs/pubrepo/snap/{S1}"] == first


@pytest.mark.parametrize("ref", ["refs/pubrepo/local/x", "refs/pubrepo/scan/1",
                                 "refs/heads/x", "refs/pubrepo/snap"])
def test_write_record_outside_allowlist_is_a_defect(source_repo, ref):
    repo = source_repo({"a.py": "x\n"}, _config())
    with pytest.raises(AssertionError):
        pubrepo.write_record(repo, ref, {"x": 1})


def test_same_record_same_sha_two_clones(two_clones):
    """records::same_record_same_sha_two_clones."""
    a, b, _ = two_clones
    assert _snap(a) == _snap(b)
    assert pubrepo.write_status_marker(a, S1, "dropped", reason="r") == \
        pubrepo.write_status_marker(b, S1, "dropped", reason="r")


def test_write_without_git_identity(source_repo, monkeypatch, tmp_path):
    """records::write_without_git_identity."""
    repo = source_repo({"a.py": "x\n"}, _config())
    git_run("config", "--unset", "user.name", cwd=repo)
    git_run("config", "--unset", "user.email", cwd=repo)
    git_run("config", "user.useConfigOnly", "true", cwd=repo)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(var, raising=False)
    sha = _snap(repo)
    assert git_run("log", "-1", "--format=%an <%ae> %at", sha, cwd=repo).strip() == \
        "pubrepo <records@pubrepo.invalid> 0"


def _raw_record(repo, ref, body: bytes):
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=body,
                          capture_output=True, check=True).stdout.decode().strip()
    tree = subprocess.run(["git", "mktree"], cwd=repo, input=f"100644 blob {blob}\trecord.json\n",
                          capture_output=True, text=True, check=True).stdout.strip()
    commit = git_run("commit-tree", tree, "-m", "x", cwd=repo).strip()
    git_run("update-ref", ref, commit, cwd=repo)


def test_unknown_format_and_bad_json_warn_and_skip(source_repo, capsys):
    repo = source_repo({"a.py": "x\n"}, _config())
    _snap(repo)
    _raw_record(repo, f"refs/pubrepo/snap/{S2}", b'{"format": 2}')
    _raw_record(repo, f"refs/pubrepo/snap/{'3' * 40}", b"{not json")
    got = pubrepo.read_records(repo, "refs/pubrepo/snap/")
    assert list(got) == [f"refs/pubrepo/snap/{S1}"]
    err = capsys.readouterr().err
    assert err.count("ignored") == 2


def test_status_marker_shape():
    with pytest.raises(AssertionError):
        pubrepo.write_status_marker(None, S1, "moved", reason="x")
    with pytest.raises(AssertionError):
        pubrepo.write_status_marker(None, S1, "rewritten", reason="x")
    with pytest.raises(AssertionError):
        pubrepo.write_status_marker(None, S1, "dropped", reason="x", new_sha=S2)


def test_active_records_union_of_markers_and_destination_filter(source_repo):
    repo = source_repo({"a.py": "x\n"}, _config())
    _snap(repo, S1)
    _snap(repo, S2)
    _snap(repo, "4" * 40, dest=("github.com/acme/other", "main"))
    pubrepo.write_status_marker(repo, S2, "rewritten", reason="amend", new_sha="6" * 40)
    assert set(pubrepo.active_snapshot_records(repo, DEST)) == {S1}
    assert pubrepo.records_has_destination(repo, DEST)
    assert pubrepo.records_has_destination(repo, ("github.com/acme/other", "main"))
    assert not pubrepo.records_has_destination(repo, ("github.com/acme/none", "main"))


def test_retired(source_repo):
    repo = source_repo({"a.py": "x\n"}, _config())
    assert not pubrepo.is_retired(repo, "v1.0.0")
    pubrepo.write_retired(repo, "v1.0.0", "superseded")
    assert pubrepo.is_retired(repo, "v1.0.0")
    assert not pubrepo.is_retired(repo, "v1.0")


@pytest.mark.parametrize("remote,expected", [
    ("https://github.com/Acme/Widget.git", ("github.com/acme/widget", "main")),
    ("git@github.com:acme/widget", ("github.com/acme/widget", "main")),
    ("brenton@host.example:/srv/git/p.git", ("brenton@host.example:/srv/git/p.git", "main")),
])
def test_destination_identity(remote, expected):
    assert pubrepo.destination_identity({"remote": remote, "branch": "main"}) == expected


def test_register_target_atomic_and_one_entry_per_config(source_repo):
    repo = source_repo({"a.py": "x\n"}, _config())
    cfg = str(repo / ".publish.toml")
    pubrepo.register_target(repo, cfg, DEST)
    pubrepo.register_target(repo, cfg, ("github.com/acme/w2", "dev"))
    pubrepo.register_target(repo, str(repo / ".publish-alt.toml"), DEST)
    targets = pubrepo.registered_targets(repo)
    assert [t["config_path"] for t in targets] == sorted(
        [str((repo / ".publish-alt.toml").resolve()), str(Path(cfg).resolve())])
    assert {t["destination"]["branch"] for t in targets} == {"dev", "main"}
    names = sorted(p.name for p in (repo / ".git" / "pubrepo").iterdir())
    assert names == ["targets.json"]


# --- sync ---

def test_fresh_clone_has_no_records_until_fetch(two_clones):
    """A plain clone never copies records; the first fetch does."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, b, origin = two_clones
    _snap(a)
    assert pubrepo.records_push_pending(a, _listed(origin)).ok
    assert _refs(b) == {}
    report = pubrepo.records_fetch(b, _listed(origin))
    assert report.ok and report.created == (f"refs/pubrepo/snap/{S1}",)
    assert _refs(b) == _refs(a)


def test_union_across_two_clones_concurrent_writes(two_clones):
    """AC93: both clones write, both sync, neither loses the other's refs."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, b, origin = two_clones
    _snap(a, S1)
    _snap(b, S2)
    pubrepo.write_status_marker(b, S1, "dropped", reason="accepted")
    for repo in (a, b, a):
        pubrepo.records_push_pending(repo, _listed(origin))
        pubrepo.records_fetch(repo, _listed(origin))
    assert _refs(a) == _refs(b)
    assert len(_refs(a)) == 3
    assert set(pubrepo.active_snapshot_records(a, DEST)) == {S2}


def test_identical_record_written_on_both_syncs_without_warning(two_clones, capsys):
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, b, origin = two_clones
    _snap(a)
    _snap(b)
    capsys.readouterr()
    pubrepo.records_push_pending(a, _listed(origin))
    report = pubrepo.records_fetch(b, _listed(origin))
    push_b = pubrepo.records_push_pending(b, _listed(origin))
    assert report.ok and report.rejected == () and push_b.ok
    assert "WARNING" not in capsys.readouterr().err


def test_clash_warns_keeps_local_and_other_refs_still_apply(two_clones, capsys):
    """Build note 1: a differing immutable ref is rejected per ref; the other
    refs in the same fetch are still created."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, b, origin = two_clones
    local = _snap(a, S1, written={"a.py": "a" * 40})
    _snap(b, S1, written={"a.py": "b" * 40})
    _snap(b, S2)
    assert pubrepo.records_push_pending(b, _listed(origin)).ok
    capsys.readouterr()
    report = pubrepo.records_fetch(a, _listed(origin))
    assert report.created == (f"refs/pubrepo/snap/{S2}",)
    assert [r for r, _ in report.rejected] == [f"refs/pubrepo/snap/{S1}"]
    assert _refs(a)[f"refs/pubrepo/snap/{S1}"] == local
    assert "Keeping the local value" in capsys.readouterr().err
    push = pubrepo.records_push_pending(a, _listed(origin))
    assert [r for r, _ in push.rejected] == [f"refs/pubrepo/snap/{S1}"]


def test_offline_push_retried_on_next_sync(two_clones, tmp_path, capsys):
    """Build note 2: a record written while the remote is unreachable is
    pushed by the next sync."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, _, origin = two_clones
    _snap(a)
    nowhere = str(tmp_path / "nowhere.git")
    git_run("remote", "set-url", "origin", nowhere, cwd=a)
    report = pubrepo.records_push_pending(a, _listed(origin, nowhere))
    assert not report.ok
    assert "records not synced; identity by verification only" in capsys.readouterr().err
    assert pubrepo.unsynced_count(a, _listed(origin, nowhere)) is None
    git_run("remote", "set-url", "origin", origin, cwd=a)
    assert pubrepo.unsynced_count(a, _listed(origin, nowhere)) == 1
    assert pubrepo.records_push_pending(a, _listed(origin, nowhere)).created == (f"refs/pubrepo/snap/{S1}",)
    assert pubrepo.unsynced_count(a, _listed(origin, nowhere)) == 0


def test_unreachable_records_remote_warns_and_does_not_raise(two_clones, tmp_path, capsys):
    """AC78 (module half): an unreachable records remote is a warning."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, _, origin = two_clones
    nowhere = str(tmp_path / "nowhere.git")
    git_run("remote", "set-url", "origin", nowhere, cwd=a)
    report = pubrepo.records_fetch(a, _listed(origin, nowhere))
    assert not report.ok
    assert "records not synced; identity by verification only" in capsys.readouterr().err


def test_sync_argv_allowlist_no_plus_no_prune(two_clones, git_recorder):
    """AC93: every sync fetch and push uses only the four
    namespaces, never `+` or `--prune`; backup, scan and local refs never
    leave the clone."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, b, origin = two_clones
    _snap(a, S1)
    _snap(b, S2)
    for name in ("refs/pubrepo/local/1", "refs/pubrepo/scan/0", "refs/pubrepo/repair/1"):
        git_run("update-ref", name, "HEAD", cwd=a)
    for repo in (a, b, a):
        pubrepo.records_push_pending(repo, _listed(origin))
        pubrepo.records_fetch(repo, _listed(origin))
    syncs = [argv for argv, _ in git_recorder.calls() if argv and argv[0] in ("fetch", "push")]
    assert any(argv[0] == "fetch" for argv in syncs) and any(argv[0] == "push" for argv in syncs)
    for argv in syncs:
        assert "--prune" not in argv and "--force" not in argv and "-f" not in argv
        assert "--mirror" not in argv and "--delete" not in argv
        refspecs = [x for x in argv[1:] if ":" in x and not x.startswith("-")
                    and not x.startswith(("/", "file:"))]
        assert refspecs, argv
        for spec in refspecs:
            assert not spec.startswith("+") and not spec.startswith(":"), spec
            src, dst = spec.split(":")
            assert src.split("/")[2] in ("snap", "status", "repairs", "retired"), spec
            assert dst == src, spec
    origin_refs = git_run("ls-remote", git_run("remote", "get-url", "origin", cwd=a).strip(),
                          "refs/pubrepo/*", cwd=a)
    assert "local" not in origin_refs and "scan" not in origin_refs and "repair/" not in origin_refs


# Deleted (§10 item 20, §18.11; the named-remote rule replaces the m-6 equality check):
# test_records_remote_equal_to_public_refused (3 params),
# test_records_remote_equal_to_other_targets_public_remote_refused,
# test_records_remote_name_resolves_to_url and test_git_net_sandbox_refuses_network_url
# (3 params). Their checks are now records_remote_is_publish_remote_even_if_listed,
# default_follows_push_resolution and public_records_remote_refused_publish_0 below.


def test_snapshot_record_config_path_null_for_out_of_tree_config(source_repo):
    """config_path is None (JSON null) only for a config outside
    the source tree; the record is still written."""
    repo = source_repo({"a.py": "x\n"}, _config())
    sha = pubrepo.write_snapshot_record(
        repo, snapshot_sha=S1, destination=DEST, sigma_full=SIGMA, written={}, nonderivable=[],
        keep=[], config_path=None, fingerprint="f" * 64)
    assert json.loads(git_run("cat-file", "blob", f"{sha}:record.json", cwd=repo))["config_path"] is None


def test_git_net_allows_local_remote_inside_sandbox(two_clones):
    """The check only refuses: a bare remote inside the base still syncs."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, b, origin = two_clones
    _snap(a)
    assert pubrepo.records_push_pending(a, _listed(origin)).ok
    assert pubrepo.records_fetch(b, _listed(origin)).ok


def test_unreachable_records_remote_one_line_no_git_noise(two_clones, tmp_path, capsys):
    """Design §4.3: an unreachable records remote prints
    only "records not synced; identity by verification only", once for a
    run's fetch and push together, with none of git's own text; the
    sanitized git reason appears only under --verbose."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    a, _, origin = two_clones
    _snap(a)
    nowhere = str(tmp_path / "nowhere.git")
    git_run("remote", "set-url", "origin", nowhere, cwd=a)
    capsys.readouterr()
    assert not pubrepo.records_fetch(a, _listed(origin, nowhere)).ok
    assert not pubrepo.records_push_pending(a, _listed(origin, nowhere)).ok
    err = capsys.readouterr().err
    assert err.splitlines() == ["WARNING: records not synced; identity by verification only"]
    for noise in ("fatal:", "Could not read", "access rights", "does not appear"):
        assert noise not in err
    pubrepo._NOT_SYNCED_WARNED.clear()
    pubrepo.ui.verbose = True
    try:
        pubrepo.records_fetch(a, _listed(origin, nowhere))
    finally:
        pubrepo.ui.verbose = False
    verbose = capsys.readouterr().err
    assert "records not synced" in verbose and "records remote:" in verbose


def test_pull_plan_with_unreachable_records_remote_is_one_line(world, run, tmp_path):
    """The same through the CLI (pull --plan; no other remote write)."""
    # AC75 re-target (§10 item 20, §18.11): the records remote is listed
    nowhere = str(tmp_path / "nowhere.git")
    w = world(private_remotes=[nowhere])
    w.snapshot()
    git_run("remote", "set-url", "origin", nowhere, cwd=w.src)
    r = run(["pull", "--plan"], cwd=w.src)
    assert r.code == 0
    assert r.stderr.count("records not synced; identity by verification only") == 1
    assert "fatal:" not in r.stderr and "Could not read" not in r.stderr


# --- records on a named, listed source remote (§18.9 R1/R6, §18.11,
# §18.12; AC120, AC128, AC129) ---

def _fresh_run():
    """A new CLI process: the per-run "warned once" sets start empty (in
    production every run is its own process)."""
    pubrepo._RECORDS_REMOTE_WARNED.clear()
    pubrepo._NOT_SYNCED_WARNED.clear()


def _q(value: str) -> str:
    """A git config value, quoted only when it must be (as in
    test_ff_push_targets): git reads the bytes as written."""
    if value == value.strip() and not any(c in value for c in '"\\#;'):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _remote(repo, name, url, *pushurls, **keys):
    """remote <name> written into the repo's config file: the git shim
    refuses a URL in argv (same shape as test_ff_push_targets._add_remote)."""
    lines = [f'[remote "{name}"]', f"\turl = {_q(url)}",
             *(f"\tpushurl = {_q(p)}" for p in pushurls),
             f"\tfetch = +refs/heads/*:refs/remotes/{name}/*",
             *(f"\t{k} = {_q(v)}" for k, v in keys.items())]
    with open(repo / ".git" / "config", "a") as f:
        f.write("\n".join(lines) + "\n")


def _set_publish(w, **keys):
    """Set top-level [publish] keys in the world's committed config."""
    path = w.src / ".publish.toml"
    lines = path.read_text().split("\n")
    for key, value in keys.items():
        new = f"{key} = {json.dumps(value)}"
        at = [n for n, line in enumerate(lines) if line.startswith(f"{key} = ")]
        if at:
            lines[at[0]] = new
        else:
            lines.insert(lines.index("[publish]") + 1, new)
    path.write_text("\n".join(lines))
    git_run("commit", "-qam", "config: " + ", ".join(keys), cwd=w.src)


def _publish(run, w, change=None):
    """A source change, then `publish --yes` (a publish with nothing new
    does not sync records)."""
    w.commit_source({"a.py": change or f"a = {len(_refs(w.src))}\n"})
    _fresh_run()
    return run(["publish", "--yes"], cwd=w.src)


def _remote_records(url_or_path, cwd):
    out = git_run("ls-remote", url_or_path, "refs/pubrepo/*", cwd=cwd)
    return {name: sha for sha, name in (line.split("\t") for line in out.splitlines())}


def _records_only(refs):
    """The four synced namespaces; the bookmark (refs/pubrepo/
    bookmarks/*) is outside them and is never synced."""
    return {n: v for n, v in refs.items() if n.split("/")[2] in pubrepo.RECORD_NAMESPACES}


def _origin(w):
    return git_run("remote", "get-url", "origin", cwd=w.src).strip()


def _record_net_calls(recorder, start, cwd):
    """Records traffic since `start`: network git calls made in `cwd` that
    name refs/pubrepo (the bookmark push and R5's `ls-remote --get-url`
    are other code's and are not records)."""
    root = Path(cwd).resolve()
    return [a for a, c in recorder.calls()[start:]
            if Path(c).resolve() == root and a and a[0] in ("fetch", "push", "ls-remote")
            and any("refs/pubrepo" in x for x in a)]


def test_publish_records_arrive_on_the_listed_remote(world, run):
    """Arrival check, publish path: with `origin` listed (the
    fixture default), the records this publish wrote are on the remote."""
    w = world()
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    local = _records_only(_refs(w.src))
    assert local and _remote_records(_origin(w), w.src) == local
    assert "refusing to push private refs" not in r.stderr
    assert any("/bookmarks/" in n for n in _refs(w.src))
    assert not [n for n in _remote_records(_origin(w), w.src) if "/bookmarks/" in n]  # never synced


def test_records_unlisted_default_not_synced(world, run, git_recorder):
    """records::records_unlisted_default_not_synced (AC129, §18.11):
    the R6 default remote (`origin`, the upstream) unlisted → one warning
    naming its value, publish 0, zero records traffic to it; listed → the
    records arrive."""
    w = world(listed=False)
    origin = _origin(w)
    start = len(git_recorder.calls())
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    assert r.stderr.count("refusing to push private refs refs/pubrepo/* to origin:") == 1
    assert f"{origin!r} is not listed in [publish] private_remotes" in r.stderr
    assert _record_net_calls(git_recorder, start, w.src) == []
    assert _remote_records(origin, w.src) == {}
    _set_publish(w, private_remotes=[origin])
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    assert "refusing to push private refs" not in r.stderr
    assert _remote_records(origin, w.src) == _records_only(_refs(w.src)) != {}


def _first_records_run(run, w, first, leaked_from):
    """The first records-syncing command: `publish`, `pull --plan`,
    or an applied repair (its --plan run, then --apply; two processes).
    Returns the combined stderr and the last exit code."""
    if first == "publish":
        r = _publish(run, w)
        return r.stderr, r.code
    _fresh_run()
    if first == "pull":
        r = run(["pull", "--plan"], cwd=w.src)
        return r.stderr, r.code
    args = ["remove-path", "secret.txt", "--from", leaked_from]
    plan = run(["repair", *args, "--plan"], cwd=w.src)
    assert plan.code == 0, plan.stderr
    digest = plan.stdout.strip().splitlines()[-1].split(": ", 1)[1]
    tip = git_run("ls-remote", w.url, "refs/heads/main").split()[0]
    _fresh_run()
    r = run(["repair", *args, "--apply", "--yes", "--expect-tip", tip, "--expect-plan", digest],
            cwd=w.src)
    return plan.stderr + r.stderr, r.code


@pytest.mark.parametrize("first", ["publish", "pull",
                                   pytest.param("repair", marks=pytest.mark.rewrite)])
def test_one_time_private_remotes_notice(world, run, first):
    """records::one_time_private_remotes_notice (§18.11;
    parametrized over the first command): `status` never shows or
    consumes it; the first records-syncing command, whichever it is, shows
    the exact line once; later runs (publish, pull) show only the
    refusal; the config is byte-unchanged; once the line is added, the
    records sync."""
    leaked_from = None
    if first == "repair":
        from test_ff_repair import _leaky_world      # a published snapshot with a leak
        w, leaked_from = _leaky_world(world, listed=False)
    else:
        w = world(listed=False)
    config = w.src / ".publish.toml"
    before = config.read_bytes()
    line = pubrepo.private_remotes_line(w.src, w.config(), "origin")
    assert line == f"private_remotes = [{json.dumps(_origin(w))}]"
    notice = ("NOTICE: records are not synced until the records remote is listed; add this "
              f"line to [publish] in .publish.toml: {line}")
    _fresh_run()
    r = run(["status"], cwd=w.src)
    assert "NOTICE:" not in r.stderr
    assert not (w.src / ".git" / "pubrepo" / "notices.json").exists()
    err, code = _first_records_run(run, w, first, leaked_from)
    assert code == 0, err
    assert err.count(notice) == 1, err
    assert "refusing to push private refs refs/pubrepo/* to origin:" in err
    for again in ("publish", "pull"):
        err, code = _first_records_run(run, w, again, leaked_from)
        assert code == 0 and "NOTICE:" not in err, (again, err)
        assert err.count("refusing to push private refs") == 1, (again, err)
    assert config.read_bytes() == before                     # publish never edits config
    config.write_text(config.read_text().replace("[publish]\n", f"[publish]\n{line}\n", 1))
    git_run("commit", "-qam", "list the records remote", cwd=w.src)
    r = _publish(run, w)
    assert r.code == 0 and "NOTICE:" not in r.stderr and "refusing" not in r.stderr
    assert _remote_records(_origin(w), w.src) == _records_only(_refs(w.src)) != {}


def test_default_follows_push_resolution(source_repo, tmp_path, capsys):
    """records::default_follows_push_resolution (R6): pushRemote beats
    pushDefault, which beats the upstream; a configured records_remote beats
    all three; with none of them there is no records remote, even with an
    `origin` (no origin fallback)."""
    repo = source_repo({"a.py": "x\n"}, _config())
    git_run("config", "--unset", "branch.main.remote", cwd=repo)
    bares = {}
    for name in ("up", "pdef", "prem"):
        bares[name] = str(tmp_path / f"{name}.git")
        git_run("init", "--bare", "-q", bares[name])
        git_run("remote", "add", name, bares[name], cwd=repo)
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    cfg = _listed(origin, *bares.values())
    _fresh_run()
    assert pubrepo.records_remote_name(repo, cfg) is None              # origin exists, unused
    assert "WARNING: no records remote; records not synced" in capsys.readouterr().err
    git_run("config", "branch.main.remote", "up", cwd=repo)
    assert pubrepo.records_remote_name(repo, cfg) == "up"
    git_run("config", "remote.pushDefault", "pdef", cwd=repo)
    assert pubrepo.records_remote_name(repo, cfg) == "pdef"
    git_run("config", "branch.main.pushRemote", "prem", cwd=repo)
    assert pubrepo.records_remote_name(repo, cfg) == "prem"
    assert pubrepo.records_remote_name(repo, {**cfg, "records_remote": "up"}) == "up"
    git_run("config", "--unset", "branch.main.pushRemote", cwd=repo)
    git_run("config", "--unset", "remote.pushDefault", cwd=repo)
    git_run("config", "branch.main.remote", ".", cwd=repo)          # a local upstream
    _fresh_run()
    assert pubrepo.records_remote_name(repo, cfg) is None


@pytest.mark.no_git_calls  # R1: refused at config validation, before any git call
def test_records_remote_must_be_a_name(world, run):
    """records::records_remote_must_be_a_name (R1, §18.12): a URL-shaped
    records_remote is a config error (1) before anything runs."""
    w = world()
    _set_publish(w, records_remote="git@example.invalid:acme/records.git")
    r = run(["publish", "--yes"], cwd=w.src)
    assert r.code == 1
    assert "'records_remote' must name a git remote of the source repo" in r.stderr


def test_records_remote_is_publish_remote_even_if_listed(world, run):
    """records::records_remote_is_publish_remote_even_if_listed
    (§18.11): a records remote whose URL is the publish remote, directly
    or behind a helper, is refused although listed; publish 0, a warning,
    no records on the public repo."""
    w = world()
    _remote(w.src, "pubrec", w.url)
    _set_publish(w, records_remote="pubrec", private_remotes=[_origin(w), w.url])
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    assert "refusing to push private refs refs/pubrepo/* to pubrec:" in r.stderr
    assert f"is the publish remote {w.url}" in r.stderr
    assert _remote_records(w.url, w.src) == {}
    # The helper form, module half: the CLI-entry sandbox scan refuses an
    # `https::file://…` source remote before pubrepo runs.
    helper = f"https::{w.url}"
    _remote(w.src, "pubhelper", helper)
    _fresh_run()
    cfg = {**w.config(), "records_remote": "pubhelper",
           "private_remotes": [_origin(w), w.url, helper]}
    assert pubrepo.records_remote_name(w.src, cfg) is None
    assert pubrepo.records_push_pending(w.src, cfg).remote is None
    assert _remote_records(w.url, w.src) == {}


def test_records_remote_bracket_and_ext_forms(source_repo, capsys, sandbox_unreachable):
    """records::records_remote_bracket_and_ext_forms (AC128, §18.10;
    §18.11): the publish remote written in the bracket scp form, and an
    ext:: command, are refused even when listed (module half: the remotes
    need hosts, which the sandbox never contacts)."""
    publish = sandbox_unreachable("ssh://git@pub.example.invalid:2222/acme/widget.git")
    bracket = sandbox_unreachable("[git@pub.example.invalid:2222]:acme/widget.git")
    ext = sandbox_unreachable("ext::ssh %S pub.example.invalid")
    repo = source_repo({"a.py": "x\n"}, _config(remote=publish), origin=False)
    _remote(repo, "br", bracket)
    _remote(repo, "ex", ext)
    for name in ("br", "ex"):
        _fresh_run()
        cfg = _listed(bracket, ext, remote=publish, records_remote=name)
        assert pubrepo.records_remote_name(repo, cfg) is None
        assert f"refusing to push private refs refs/pubrepo/* to {name}:" in capsys.readouterr().err
        assert pubrepo.records_push_pending(repo, cfg).remote is None


def test_records_remote_non_ascii_host_unknown(source_repo, capsys, sandbox_unreachable):
    """records::records_remote_non_ascii_host_unknown (§18.10, §18.11):
    a fullwidth or %-encoded host is never parsed into a match;
    unlisted it is refused, naming the exact value; listed byte-exact, the
    records go to that remote by name (the sandbox then stops the network
    call, which shows sync was attempted there and nowhere else)."""
    for url in (sandbox_unreachable("ssh://git@ｅｘａｍｐｌｅ.invalid/acme/records.git"),
                sandbox_unreachable("ssh://git@rec%2eexample.invalid/acme/records.git")):
        repo = source_repo({"a.py": "x\n"}, _config(), origin=False)
        _remote(repo, "rec", url)
        _fresh_run()
        assert pubrepo.records_remote_name(repo, _config(records_remote="rec")) is None
        assert f"{url!r} is not listed in [publish] private_remotes" in capsys.readouterr().err
        cfg = _listed(url, records_remote="rec")
        assert pubrepo.records_remote_name(repo, cfg) == "rec"
        _snap(repo)
        with pytest.raises(pubrepo.PublishError, match="test sandbox: refusing"):
            pubrepo.records_push_pending(repo, cfg)


def test_helper_url_records_remote_refused(source_repo, capsys, git_recorder,
                                          sandbox_unreachable):
    """records::helper_url_records_remote_refused (§18.11):
    an `https::` helper value unlisted → refused, no network call; listed
    byte-exact → the records go to that remote by name. An `ext::` value is
    refused even when listed."""
    helper = sandbox_unreachable("https::https://records.example.invalid/acme/records.git")
    ext = sandbox_unreachable("ext::ssh %S records.example.invalid")
    repo = source_repo({"a.py": "x\n"}, _config(), origin=False)
    _remote(repo, "hp", helper)
    _remote(repo, "ex", ext)
    _snap(repo)
    start = len(git_recorder.calls())
    _fresh_run()
    assert pubrepo.records_push_pending(repo, _config(records_remote="hp")).remote is None
    assert pubrepo.records_fetch(repo, _config(records_remote="hp")).remote is None
    assert f"{helper!r} is not listed" in capsys.readouterr().err
    _fresh_run()
    assert pubrepo.records_remote_name(repo, _listed(ext, records_remote="ex")) is None
    assert "refusing to push private refs refs/pubrepo/* to ex:" in capsys.readouterr().err
    assert _record_net_calls(git_recorder, start, repo) == []
    assert pubrepo.records_remote_name(repo, _listed(helper, records_remote="hp")) == "hp"


def test_public_records_remote_refused_publish_0(world, run, git_recorder, sandbox_unreachable):
    """records::public_records_remote_refused_publish_0 (AC120, extends
    AC97): a records_remote on github.com, not listed → a warning, publish
    0, zero network calls to it; listed → the sync goes to it by name (the
    sandbox stops that push, exit 2, which shows the attempt)."""
    gh = sandbox_unreachable("git@github.com:acme/widget-records.git")
    w = world()
    _remote(w.src, "gh", gh)
    _set_publish(w, records_remote="gh")
    start = len(git_recorder.calls())
    r = _publish(run, w)
    assert r.code == 0, r.stderr
    assert r.stderr.count("refusing to push private refs refs/pubrepo/* to gh:") == 1
    assert f"{gh!r} is not listed in [publish] private_remotes" in r.stderr
    assert not [a for a, _ in git_recorder.calls()[start:]       # --get-url is a local lookup
                if a and a[0] in ("fetch", "push", "ls-remote") and "--get-url" not in a
                and ("gh" in a or gh in a)]
    cfg = {**w.config(), "private_remotes": [_origin(w), gh]}
    assert pubrepo.records_remote_name(w.src, cfg) == "gh"
    with pytest.raises(pubrepo.PublishError, match="test sandbox: refusing"):
        pubrepo.records_push_pending(w.src, cfg)


def test_records_push_hardening(world, run, git_recorder):
    """records::records_push_hardening (§18.12): receivepack,
    uploadpack or vcs on the records remote → refused with a warning,
    publish 0; a listed remote's record push carries
    --recurse-submodules=no. (A bad name is a config error, 1, in
    records_remote_must_be_a_name; at push time, below.)"""
    w = world()
    origin = _origin(w)
    start = len(git_recorder.calls())
    assert _publish(run, w).code == 0
    pushes = [a for a in _record_net_calls(git_recorder, start, w.src) if a[0] == "push"]
    # (the bookmark push is core's, not a records push)
    assert pushes and all("--recurse-submodules=no" in a for a in pushes)
    for key, value in (("receivepack", "git-receive-pack"), ("uploadpack", "git-upload-pack"),
                       ("vcs", "git")):
        git_run("config", f"remote.origin.{key}", value, cwd=w.src)
        r = _publish(run, w)
        assert r.code == 0, r.stderr
        assert f"remote.origin.{key} is set" in r.stderr
        assert "refusing to push private refs refs/pubrepo/* to origin:" in r.stderr
        git_run("config", "--unset", f"remote.origin.{key}", cwd=w.src)
    _fresh_run()
    cfg = {**w.config(), "records_remote": "a/b"}             # edited after validation
    assert pubrepo.records_remote_name(w.src, cfg) is None
    assert pubrepo.records_push_pending(w.src, cfg).remote is None
    assert _remote_records(origin, w.src) != {}


@pytest.mark.git_oracle
def test_push_argv_matches_git_oracle(source_repo, git_oracle, sandbox_unreachable):
    """records::push_argv_matches_git_oracle (§18.10,
    §18.11): for single-URL records remotes, the real git's
    `push --porcelain --recurse-submodules=no <name> <ref>:<ref>` and
    `fetch <name> …` connect only to the value the check saw (the one
    value listed), and every form whose value is the publish repository is
    refused although listed. Residual (stated): git stops at the first
    failing push URL, so remotes with several pushurls are checked by the
    unit tests (R1 checks every value), not by this oracle."""
    publish = sandbox_unreachable("ssh://git@pub.example.invalid:2222/acme/widget.git")
    forms = {
        "bracket": (sandbox_unreachable("[git@pub.example.invalid:2222]:acme/widget.git"),
                    ("pub.example.invalid", 2222), "publish"),
        "scp": (sandbox_unreachable("rec.example.invalid:acme/records.git"),
                ("rec.example.invalid", None), None),
        "port": (sandbox_unreachable("ssh://git@ssh.github.com:443/acme/records.git"),
                 ("ssh.github.com", 443), None),
        # git percent-decodes an ssh:// host before calling ssh (probe), so
        # the listed %-encoded value reaches the decoded host; accepted
        # it only because that exact value is listed (the refusal decodes it).
        "pct": (sandbox_unreachable("ssh://git@rec%2eexample.invalid/acme/records.git"),
                ("rec.example.invalid", None), None),
    }
    for name, (url, target, refused) in forms.items():
        repo = source_repo({"a.py": "x\n"}, _config(remote=publish), origin=False)
        _remote(repo, name, url)
        _snap(repo)
        cfg = _listed(url, remote=publish, records_remote=name)
        _fresh_run()
        resolved = pubrepo.records_remote_name(repo, cfg)
        if refused:
            assert resolved is None, name
            continue
        assert resolved == name
        assert pubrepo.remote_url_values(repo, name) == [url]
        ref = f"refs/pubrepo/snap/{S1}"
        for args in (("push", "--porcelain", "--recurse-submodules=no", name, f"{ref}:{ref}"),
                     ("fetch", "--no-tags", "--no-write-fetch-head", name,
                      "refs/pubrepo/snap/*:refs/pubrepo/snap/*")):
            found = git_oracle.remote(repo, *args)
            assert len(found) == 1 and found[0][0] == "ssh", (name, args, found)
            argv = found[0][1]
            host = next(a for a in argv if not a.startswith("-") and argv[argv.index(a) - 1]
                        not in ("-p", "-o", "-l", "-i", "-F")).rsplit("@", 1)[-1]
            port = int(argv[argv.index("-p") + 1]) if "-p" in argv else None
            assert (host, port) == target, (name, argv)


@pytest.mark.git_oracle
def test_push_argv_matches_git_oracle_insteadof(source_repo, git_oracle, sandbox_unreachable):
    """The same oracle for an insteadOf rewrite (§18.9 R1): the
    expanded value is what git contacts, so it must be listed too; listed
    as written only → refused."""
    written = sandbox_unreachable("git@rec.example.invalid:acme/records.git")
    expanded = sandbox_unreachable("ssh://git@github.com/acme/records.git")
    base = sandbox_unreachable("ssh://git@github.com/")
    repo = source_repo({"a.py": "x\n"}, _config(), origin=False)
    _remote(repo, "rw", written)
    with open(repo / ".git" / "config", "a") as f:
        f.write(f'[url "{base}"]\n\tinsteadOf = git@rec.example.invalid:\n')
    _snap(repo)
    _fresh_run()
    assert pubrepo.records_remote_name(repo, _listed(written, records_remote="rw")) is None
    cfg = _listed(written, expanded, records_remote="rw")
    assert pubrepo.records_remote_name(repo, cfg) == "rw"
    assert set(pubrepo.remote_url_values(repo, "rw")) == {written, expanded}
    ref = f"refs/pubrepo/snap/{S1}"
    found = git_oracle.remote(repo, "push", "--porcelain", "--recurse-submodules=no", "rw",
                              f"{ref}:{ref}")
    assert len(found) == 1 and "git@github.com" in found[0][1]


def test_validate_remote_reports_refused_default_records_remote(world, run, git_recorder):
    """records::validate_remote_reports_refused_default: only
    `validate --remote` reports a refused R6 default records remote (an
    unlisted `origin` upstream), with the check's reason; plain validate
    checks configured remotes only and makes none of the R6 lookups (AC63);
    a listed default is not reported."""
    w = world(listed=False)
    start = len(git_recorder.calls())
    r = run(["validate"], cwd=w.src)
    assert "records would sync to" not in r.stdout + r.stderr
    r6 = [a for a, _ in git_recorder.calls()[start:]
          if a[:2] == ["config", "--get"] and a[2].endswith((".pushRemote", "pushDefault"))]
    assert r6 == []
    r = run(["validate", "--remote"], cwd=w.src)
    out = r.stdout + r.stderr
    assert r.code == 0, out
    assert "records would sync to 'origin' (the push remote; no records_remote is set)" in out
    assert f"{_origin(w)!r} is not listed in [publish] private_remotes" in out
    _set_publish(w, private_remotes=[_origin(w)])
    r = run(["validate", "--remote"], cwd=w.src)
    assert "records would sync to" not in r.stdout + r.stderr
