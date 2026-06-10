"""T23: --config flag + dir key — multi-target isolation by construction."""
from conftest import config_toml


def test_two_targets_full_isolation(source_repo, public_remote, run, published_tree):
    """The §5 T23 proof: one source repo, two manifests, two dirs, two
    remotes — each publish ships only its own manifest; neither ships the
    other's workdir or either config file."""
    remote_main = public_remote()
    remote_sdk = public_remote()
    repo = source_repo(
        {"src/app.py": "app\n", "sdk/client.py": "client\n", "README.md": "readme\n"},
        {"remote": remote_main, "include": ["src/", "README.md"],
         "scrub": {"forbidden": ["sekrit-main"]}},
    )
    (repo / ".publish-sdk.toml").write_text(config_toml({
        "remote": remote_sdk,
        "dir": ".publish-sdk",
        "include": ["sdk/", "README.md"],
        "scrub": {"forbidden": ["sekrit-sdk"]},
    }))

    assert run(["init"], cwd=repo).code == 0
    assert run(["init", "--config", ".publish-sdk.toml"], cwd=repo).code == 0
    assert (repo / ".publish-sdk" / ".git").exists()

    assert run([], cwd=repo).code == 0
    assert run(["publish", "--config", ".publish-sdk.toml"], cwd=repo).code == 0

    main_tree = published_tree(remote_main)
    sdk_tree = published_tree(remote_sdk)

    assert set(main_tree) == {"src/app.py", "README.md"}
    assert set(sdk_tree) == {"sdk/client.py", "README.md"}
    for tree in (main_tree, sdk_tree):
        assert not any(p.startswith(".publish") for p in tree)  # no workdirs, no configs


def test_root_include_two_targets_no_cross_leak(source_repo, public_remote, run, published_tree):
    """Even include=['.'] cannot ship the sibling target's workdir or config."""
    remote_a = public_remote()
    remote_b = public_remote()
    repo = source_repo(
        {"a.py": "x\n"},
        {"remote": remote_a, "include": ["."]},
    )
    (repo / ".publish-b.toml").write_text(config_toml({
        "remote": remote_b, "dir": ".publish-b", "include": ["."],
    }))
    run(["init"], cwd=repo)
    run(["init", "--config", ".publish-b.toml"], cwd=repo)

    assert run([], cwd=repo).code == 0
    assert run(["publish", "--config", ".publish-b.toml"], cwd=repo).code == 0

    for remote in (remote_a, remote_b):
        tree = published_tree(remote)
        assert "a.py" in tree
        assert not any(p.startswith(".publish") for p in tree)


def test_in_tree_config_bad_name_rejected(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    (repo / "myconfig.toml").write_text((repo / ".publish.toml").read_text())
    r = run(["validate", "--config", "myconfig.toml"], cwd=repo)
    assert r.code == 1
    assert "must be named" in r.stderr


def test_out_of_tree_config_any_name_allowed(source_repo, public_remote, run, tmp_path,
                                             published_tree):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    external = tmp_path / "external-config.toml"
    external.write_text((repo / ".publish.toml").read_text())

    assert run(["init", "--config", str(external)], cwd=repo).code == 0
    assert run(["publish", "--config", str(external)], cwd=repo).code == 0
    assert "a.py" in published_tree(remote)


def test_status_honors_config_dir(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    (repo / ".publish-x.toml").write_text(config_toml({
        "remote": remote, "dir": ".publish-x", "include": ["a.py"],
    }))
    r = run(["status", "--config", ".publish-x.toml"], cwd=repo)
    assert r.code == 0
    assert "Not initialized" in r.stdout  # .publish-x/ doesn't exist yet

    run(["init", "--config", ".publish-x.toml"], cwd=repo)
    r = run(["status", "--config", ".publish-x.toml"], cwd=repo)
    assert "Dir:      .publish-x" in r.stdout
