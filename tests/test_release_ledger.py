from pathlib import Path

import pubrepo
from conftest import git_run


def _remote_with_tags(tmp_path: Path):
    work = tmp_path / "work"
    remote = tmp_path / "remote.git"
    work.mkdir()
    git_run("init", "-q", "-b", "main", cwd=work)
    git_run("config", "user.name", "Tests", cwd=work)
    git_run("config", "user.email", "t@example.test", cwd=work)
    (work / "a").write_text("one")
    git_run("add", "a", cwd=work)
    git_run("commit", "-q", "-m", "one", cwd=work)
    first = git_run("rev-parse", "HEAD", cwd=work).strip()
    git_run("tag", "-a", "v1.0.0", "-m", "release", cwd=work)
    (work / "a").write_text("two")
    git_run("commit", "-qam", "two", cwd=work)
    tip = git_run("rev-parse", "HEAD", cwd=work).strip()
    git_run("tag", "foreign", cwd=work)
    git_run("init", "--bare", "-q", "-b", "main", str(remote))
    git_run("remote", "add", "origin", str(remote), cwd=work)
    git_run("push", "-q", "origin", "main", "--tags", cwd=work)
    clone = tmp_path / "clone"
    git_run("clone", "-q", str(remote), str(clone))
    return work, clone, first, tip


# AC75: §16 row "§4 Ledger: a matching remote tag is presumed to be a completed legacy/external release" (Replaced → §8.5: managed = marker trailers or adopt_tags)
def test_remote_ledger_uses_peeled_annotated_target(tmp_path):
    # §8.5: managed needs the marker trailers or adoption.
    _work, clone, first, _tip = _remote_with_tags(tmp_path)
    ledger = pubrepo.read_remote_ledger(clone, "v{version}", adopt_tags=["v1.0.0"])
    assert ledger.managed == {"v1.0.0": first}
    assert ledger.latest == ledger.latest_stable == "1.0.0"
    unadopted = pubrepo.read_remote_ledger(clone, "v{version}")
    assert unadopted.managed == {} and unadopted.foreign == {"v1.0.0": first}
    assert unadopted.latest is None


# AC75: §16 row "§4 Ledger: a matching remote tag is presumed to be a completed legacy/external release" (Replaced → §8.5: managed = marker trailers or adopt_tags)
def test_fresh_second_clone_sees_legacy_ledger(tmp_path):
    _work, clone, first, _tip = _remote_with_tags(tmp_path)
    second = tmp_path / "second"
    git_run("clone", "-q", git_run("remote", "get-url", "origin", cwd=clone).strip(), str(second))
    # §8.5: managed needs the marker trailers or adoption.
    assert pubrepo.read_remote_ledger(second, "v{version}",
                                      adopt_tags=["v1.0.0"]).managed["v1.0.0"] == first


def test_any_tag_head_protection_data(tmp_path):
    _work, clone, _first, tip = _remote_with_tags(tmp_path)
    ledger = pubrepo.read_remote_ledger(clone, "v{version}")
    assert pubrepo.tags_peeling_to(ledger, tip) == ["foreign"]


# AC75: §16 row "§3 interactive prompt line Public main: N snapshot commits ahead" (Replaced → §8.3) and §16 row "§4 Ledger: a matching remote tag is presumed to be a completed legacy/external release" (Replaced → §8.5: managed = marker trailers or adopt_tags)
def test_ancestry_and_ahead_count(tmp_path):
    _work, clone, _first, tip = _remote_with_tags(tmp_path)
    ledger = pubrepo.read_remote_ledger(clone, "v{version}", adopt_tags=["v1.0.0"])  # §8.5
    assert pubrepo.ancestry_violations(clone, ledger, tip) == []
    assert pubrepo.ahead_of_latest(clone, ledger, tip) == 1
    # §8.3: snapshots and collaborator commits are counted
    # separately; "two" has no source record or trailer, so it is a collaborator's.
    source = tmp_path / "source"
    git_run("init", "-q", "-b", "main", str(source))
    assert pubrepo.ahead_counts(clone, ledger, tip, source) == (0, 1)


def test_one_side_only_tags_are_reported(tmp_path):
    # §16: managed tags only. An adopted local-only v2.0.0 is
    # reported; an unmanaged local-only v3.0.0 is not a ledger entry.
    _work, clone, _first, _tip = _remote_with_tags(tmp_path)
    ledger = pubrepo.read_remote_ledger(clone, "v{version}", adopt_tags=["v1.0.0", "v2.0.0"])
    git_run("tag", "v2.0.0", cwd=clone)
    git_run("tag", "v3.0.0", cwd=clone)
    messages = pubrepo.local_tag_mismatches(clone, ledger)
    assert any("v2.0.0 exists locally but not remotely" in x for x in messages)
    assert not any("v3.0.0" in x for x in messages)


def test_remote_only_tag_is_reported(tmp_path):
    _work, clone, _first, _tip = _remote_with_tags(tmp_path)
    ledger = pubrepo.read_remote_ledger(clone, "v{version}", adopt_tags=["v1.0.0"])  # §16
    git_run("tag", "-d", "v1.0.0", cwd=clone)
    assert any("v1.0.0 exists remotely but not locally" in x
               for x in pubrepo.local_tag_mismatches(clone, ledger))


def test_no_force_tag_fetch_is_used(tmp_path, monkeypatch):
    _work, clone, _first, _tip = _remote_with_tags(tmp_path)
    calls = []
    real = pubrepo.git

    def spy(*args, **kwargs):
        calls.append(args)
        return real(*args, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spy)
    pubrepo.read_remote_ledger(clone, "v{version}")
    assert not any("+refs/tags" in " ".join(call) for call in calls)


# AC75: §16 row "§4 Ledger: a matching remote tag is presumed to be a completed legacy/external release" (Replaced → §8.5: managed = marker trailers or adopt_tags)
def test_moved_remote_tag_is_detected_without_force_fetch(tmp_path):
    work, clone, _first, tip = _remote_with_tags(tmp_path)
    git_run("tag", "-f", "v1.0.0", tip, cwd=work)
    git_run("push", "-q", "--force", "origin", "refs/tags/v1.0.0", cwd=work)
    ledger = pubrepo.read_remote_ledger(clone, "v{version}", adopt_tags=["v1.0.0"])  # §8.5
    assert any("differs locally and remotely" in message
               for message in pubrepo.local_tag_mismatches(clone, ledger))
