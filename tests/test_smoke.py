"""T6 smoke tests: prove the fixtures and the happy path end to end."""


def test_publish_roundtrip(source_repo, public_remote, run, published_tree):
    """init -> publish -> the remote receives exactly the manifest projection."""
    remote = public_remote()
    repo = source_repo(
        {
            "src/app.py": "print('hi')\n",
            "README.md": "# readme\n",
            ".env": "SECRET=1\n",
        },
        {"remote": remote, "include": ["src/", "README.md"]},
    )
    r = run(["init"], cwd=repo)
    assert r.code == 0

    r = run([], cwd=repo)
    assert r.code == 0

    assert published_tree(remote) == {
        "src/app.py": b"print('hi')\n",
        "README.md": b"# readme\n",
    }


def test_second_publish_nothing(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"a.py": "x = 1\n"}, {"remote": remote, "include": ["a.py"]})
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0

    r = run([], cwd=repo)
    assert r.code == 0
    assert "Nothing to publish" in r.stdout
