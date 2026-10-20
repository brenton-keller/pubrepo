import json
from pathlib import Path

import pytest

import pubrepo


def _publish(tmp_path: Path) -> Path:
    root = tmp_path / "source"
    (root / ".publish" / ".git").mkdir(parents=True)
    return root


def _journal(root, version="1.0.0", *, writes=None, state="active", created="1"):
    publish = root / ".publish"
    journal = pubrepo.new_journal(
        publish, version, command=f"--release {version}", source_sha="s",
        public_baseline="b", tag=f"v{version}", strategy="new_commit",
        config={"remote": "x", "branch": "main", "dir": ".publish", "release": {}},
    )
    journal.data["created"] = created
    journal.data["remote_writes"] = list(writes or [])
    journal.data["state"] = state
    pubrepo.journal_write(journal)
    return journal


def test_journal_atomic_write_preserves_old_file_on_replace_failure(tmp_path, monkeypatch):
    root = _publish(tmp_path)
    journal = _journal(root)
    before = journal.path.read_bytes()
    journal.data["in_flight"] = "new"
    monkeypatch.setattr(pubrepo.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("boom")))
    with pytest.raises(OSError):
        pubrepo.journal_write(journal)
    assert journal.path.read_bytes() == before


def test_record_is_parseable_and_tracks_remote_writes(tmp_path):
    root = _publish(tmp_path)
    journal = _journal(root)
    pubrepo.journal_record(journal, "tag", remote=True, public_tag="abc")
    loaded = pubrepo.load_journal(root / ".publish", "1.0.0")
    assert loaded.data["completed"] == ["tag"]
    assert loaded.data["remote_writes"] == ["tag"]
    assert loaded.data["observed_remote"]["public_tag"] == "abc"


def test_target_wide_supersession_cross_version_config_and_source(tmp_path):
    root = _publish(tmp_path)
    old = _journal(root, "1.0.0")
    new = _journal(root, "2.0.0")
    new.data["source_sha"] = "different"
    new.data["config_fingerprint"] = "different"
    superseded = pubrepo.resolve_supersession(root / ".publish", new)
    assert [x.version for x in superseded] == ["1.0.0"]
    assert pubrepo.load_journal(root / ".publish", "1.0.0").data["state"] == "superseded"


def test_remote_written_journal_blocks_different_attempt(tmp_path):
    root = _publish(tmp_path)
    _journal(root, "1.0.0", writes=["branch_push"])
    new = _journal(root, "2.0.0")
    with pytest.raises(pubrepo.ReleaseError, match="remote writes"):
        pubrepo.resolve_supersession(root / ".publish", new)


def test_coexisting_unpushed_is_superseded_while_remote_written_blocks(tmp_path):
    root = _publish(tmp_path)
    unpushed = _journal(root, "1.0.0", created="1")
    _journal(root, "1.1.0", writes=["branch_push"], created="2")
    new = _journal(root, "2.0.0", created="3")
    with pytest.raises(pubrepo.ReleaseError):
        pubrepo.resolve_supersession(root / ".publish", new)
    assert json.loads(unpushed.path.read_text())["state"] == "superseded"


def test_blocking_ignores_complete_abandoned_and_superseded(tmp_path):
    root = _publish(tmp_path)
    for i, state in enumerate(("complete", "abandoned", "superseded"), 1):
        _journal(root, f"1.0.{i}", writes=["branch_push"], state=state)
    assert pubrepo.blocking_journals(root / ".publish") == []


def test_corrupt_journal_refuses_with_path(tmp_path):
    root = _publish(tmp_path)
    path = pubrepo.journal_path(root / ".publish", "1.0.0")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{")
    with pytest.raises(pubrepo.ReleaseError) as exc:
        pubrepo.blocking_journals(root / ".publish")
    assert str(path) in str(exc.value)


def test_release_abandon_never_runs_remote_tools(tmp_path, monkeypatch):
    root = _publish(tmp_path)
    journal = _journal(root, writes=["branch_push"])
    calls = []
    monkeypatch.setattr(pubrepo, "git", lambda *a, **k: calls.append(a))
    config = {"dir": ".publish"}
    assert pubrepo.cmd_release_abandon(root, config, "1.0.0", True) == 0
    assert json.loads(journal.path.read_text())["state"] == "abandoned"
    assert calls == []


def test_release_abandon_noninteractive_requires_yes(tmp_path, monkeypatch):
    root = _publish(tmp_path)
    _journal(root)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: False)
    with pytest.raises(pubrepo.ConfigError, match="--yes"):
        pubrepo.cmd_release_abandon(root, {"dir": ".publish"}, "1.0.0", False)


def test_init_refuses_unresolved_journal(tmp_path):
    root = _publish(tmp_path)
    _journal(root)
    with pytest.raises(pubrepo.ReleaseError, match="unresolved"):
        pubrepo.cmd_init(root, {"dir": ".publish", "remote": "x", "branch": "main"})


def test_release_abandon_tty_requires_exact_version(tmp_path, monkeypatch):
    root = _publish(tmp_path)
    _journal(root)
    monkeypatch.setattr(pubrepo, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _="": "wrong")
    with pytest.raises(pubrepo.ConfigError, match="did not match"):
        pubrepo.cmd_release_abandon(root, {"dir": ".publish"}, "1.0.0", False)
    monkeypatch.setattr("builtins.input", lambda _="": "1.0.0")
    assert pubrepo.cmd_release_abandon(root, {"dir": ".publish"}, "1.0.0", False) == 0
    assert pubrepo.load_journal(root / ".publish", "1.0.0").data["state"] == "abandoned"
