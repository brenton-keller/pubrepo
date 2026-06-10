"""T7: the three-rule matcher (compile_patterns) — unit + integration."""
import pubrepo


# --- unit: the three rules ----------------------------------------------------

def test_rule1_trailing_slash_dirs_only_any_depth():
    m = pubrepo.compile_patterns(["internal/"])
    assert m("internal", True)
    assert m("src/internal", True)        # the L2 fix: any depth, not just top
    assert not m("internal", False)       # dirs-only: a FILE named internal passes
    assert not m("src/internal/file.py", False)  # the dir matches; walks prune below


def test_rule2_anchored_with_slash():
    m = pubrepo.compile_patterns(["src/gen/*.py"])
    assert m("src/gen/foo.py", False)
    assert not m("other/gen/foo.py", False)
    assert m("src/gen/sub/foo.py", False)  # '*' crosses '/' (fnmatch) — documented


def test_rule3_basename_any_depth():
    m = pubrepo.compile_patterns(["*.pyc"])
    assert m("a.pyc", False)
    assert m("deep/down/b.pyc", False)
    assert not m("a.py", False)
    m = pubrepo.compile_patterns(["README.md"])
    assert m("README.md", False)
    assert m("docs/README.md", False)


def test_trailing_slash_strips_before_anchor_test():
    # 'internal/' must be a bare name (rule 3), NOT anchored — gitignore's rule.
    m = pubrepo.compile_patterns(["internal/"])
    assert m("a/b/c/internal", True)


def test_literal_bracket_escaping():
    m = pubrepo.compile_patterns(["data[[]1].csv"])
    assert m("data[1].csv", False)
    assert not m("data1.csv", False)


def test_case_sensitive_like_git():
    m = pubrepo.compile_patterns(["README.md"])
    assert not m("readme.md", False)


def test_include_ancestor_domination():
    m = pubrepo.compile_patterns(["internal/"])
    assert pubrepo._include_matches_excludes("internal/notes.txt", False, m)
    assert not pubrepo._include_matches_excludes("external/notes.txt", False, m)


# --- integration --------------------------------------------------------------

def test_nested_internal_dir_excluded_everywhere(source_repo, public_remote, run, published_tree):
    """The live leak path from L2: exclude=['internal/'] must catch src/internal."""
    remote = public_remote()
    repo = source_repo(
        {
            "src/ok.py": "ok\n",
            "src/internal/secret.py": "s\n",
            "internal/x.py": "x\n",
        },
        {"remote": remote, "include": ["."], "exclude": ["internal/"]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    paths = set(published_tree(remote))
    assert "src/ok.py" in paths
    assert not any(p.startswith(("internal/", "src/internal/")) for p in paths)


def test_anchored_exclude_only_matches_anchored(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {"src/gen/a.py": "a\n", "other/gen/a.py": "b\n"},
        {"remote": remote, "include": ["."], "exclude": ["src/gen/*.py"]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    paths = set(published_tree(remote))
    assert "other/gen/a.py" in paths
    assert "src/gen/a.py" not in paths


def test_dirs_only_spares_file_of_same_name(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {"internal": "a plain FILE named internal\n", "a.py": "x\n"},
        {"remote": remote, "include": ["."], "exclude": ["internal/"]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "internal" in published_tree(remote)


def test_exclude_beats_direct_include_with_warning(source_repo, public_remote, run, published_tree):
    """L3: explicit exclude wins over explicit include, with a warning."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "a\n", "b.py": "b\n"},
        {"remote": remote, "include": ["a.py", "b.py"], "exclude": ["a.py"]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "exclude wins" in r.stderr
    assert set(published_tree(remote)) == {"b.py"}


def test_excluded_dir_dominates_include_below_it(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {"internal/notes.txt": "n\n", "a.py": "x\n"},
        {"remote": remote, "include": ["a.py", "internal/notes.txt"], "exclude": ["internal/"]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "exclude wins" in r.stderr
    assert set(published_tree(remote)) == {"a.py"}


def test_explicit_include_beats_defaults_at_entry_but_not_contents(
        source_repo, public_remote, run, published_tree):
    """§6 precedence, pinned (audit Finding C): an explicit include of a
    default-excluded name is honored at the entry level, but default
    excludes still filter the walked CONTENTS — a partial copy by design."""
    remote = public_remote()
    repo = source_repo(
        {
            "__pycache__/readme.txt": "not bytecode\n",
            "__pycache__/mod.pyc": "fake bytecode\n",
            "a.py": "x\n",
        },
        {"remote": remote, "include": ["a.py", "__pycache__/"]},
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    tree = published_tree(remote)
    assert "__pycache__/readme.txt" in tree   # entry-level include honored
    assert "__pycache__/mod.pyc" not in tree  # defaults still filter contents


def test_collision_skip_keeps_status_parity(source_repo, public_remote, run):
    """wanted-files and copied-files must agree on collision skips, or status
    would report phantom pending changes forever."""
    remote = public_remote()
    repo = source_repo(
        {"a.py": "a\n", "b.py": "b\n"},
        {"remote": remote, "include": ["a.py", "b.py"], "exclude": ["a.py"]},
    )
    run(["init"], cwd=repo)
    run([], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    assert "Nothing to publish" in r.stdout
