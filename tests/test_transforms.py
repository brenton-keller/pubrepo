"""T12: transform consolidation (L1) + the strip_between suite it never had."""
import pubrepo


def _rules(*rules):
    return list(rules)


# --- unit: apply_transform_rules ------------------------------------------------

def test_find_replace_all_occurrences():
    out = pubrepo.apply_transform_rules("aXbXc", _rules({"find": "X", "replace": "Y"}))
    assert out == "aYbYc"


def test_multi_block_removal():
    content = "keep1 <!--A-->secret1<!--B--> keep2 <!--A-->secret2<!--B--> keep3"
    out = pubrepo.apply_transform_rules(
        content, _rules({"strip_between": ["<!--A-->", "<!--B-->"]}))
    assert out == "keep1  keep2  keep3"
    assert "secret" not in out


def test_start_equals_end_marker():
    out = pubrepo.apply_transform_rules(
        "a@@x@@b@@y@@c", _rules({"strip_between": ["@@", "@@"]}))
    assert out == "abc"


def test_marker_at_position_zero_and_eof():
    out = pubrepo.apply_transform_rules(
        "<!--A-->lead<!--B-->mid<!--A-->tail<!--B-->",
        _rules({"strip_between": ["<!--A-->", "<!--B-->"]}))
    assert out == "mid"


def test_orphaned_start_leaves_content_and_warns():
    warnings = []
    content = "before <!--A-->orphaned tail with sekrit"
    out = pubrepo.apply_transform_rules(
        content, _rules({"strip_between": ["<!--A-->", "<!--B-->"]}),
        warn=warnings.append)
    assert out == content  # nothing removed
    assert len(warnings) == 1
    assert "end marker not found" in warnings[0]


def test_no_match_warns_only_with_warn():
    warnings = []
    pubrepo.apply_transform_rules("abc", _rules({"find": "zzz", "replace": "y"}),
                                  warn=warnings.append)
    assert warnings == ["rule 1: no match for: 'zzz'"]
    # Simulations pass no warn and stay quiet (no channel to emit through).
    assert pubrepo.apply_transform_rules("abc", _rules({"find": "zzz", "replace": "y"})) == "abc"


def test_rule_order_dependency():
    rules_replace_first = _rules(
        {"find": "X", "replace": "<!--A-->"},
        {"strip_between": ["<!--A-->", "<!--B-->"]},
    )
    rules_strip_first = _rules(
        {"strip_between": ["<!--A-->", "<!--B-->"]},
        {"find": "X", "replace": "<!--A-->"},
    )
    content = "Xhidden<!--B-->rest"
    assert pubrepo.apply_transform_rules(content, rules_replace_first) == "rest"
    assert pubrepo.apply_transform_rules(content, rules_strip_first) == "<!--A-->hidden<!--B-->rest"


# --- integration ----------------------------------------------------------------

def test_transforms_applied_in_published_bytes(source_repo, public_remote, run, published_tree):
    remote = public_remote()
    repo = source_repo(
        {"conf.py": 'name = "private-name"  # <!--A-->internal<!--B-->\n'},
        {
            "remote": remote, "include": ["conf.py"],
            "transforms": {"conf.py": [
                {"find": "private-name", "replace": "public-name"},
                {"strip_between": ["<!--A-->", "<!--B-->"]},
            ]},
        },
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 0
    tree = published_tree(remote)
    assert tree["conf.py"] == b'name = "public-name"  # \n'


def test_orphaned_start_scrub_backstop(source_repo, public_remote, run, published_tree):
    """§3's pinned relationship: an orphaned start marker leaves content in
    place, which is acceptable ONLY because the scrub gate backstops it."""
    remote = public_remote()
    repo = source_repo(
        {"doc.md": "public <!--A-->sekrit hostname, end marker missing\n"},
        {
            "remote": remote, "include": ["doc.md"],
            "transforms": {"doc.md": [{"strip_between": ["<!--A-->", "<!--B-->"]}]},
            "scrub": {"forbidden": ["sekrit"]},
        },
    )
    run(["init"], cwd=repo)
    r = run([], cwd=repo)
    assert r.code == 4                          # scrub caught the survivor
    assert "end marker not found" in r.stderr   # transform warned
    assert published_tree(remote) == {}         # nothing leaked


def test_post_transform_hash_parity(source_repo, public_remote, run):
    """The wanted-files hash must equal the published bytes for transformed
    files — the property that makes `status` honest. A publish followed by
    an immediate publish/status must report no changes."""
    remote = public_remote()
    repo = source_repo(
        {"conf.py": "private-name everywhere private-name\n"},
        {
            "remote": remote, "include": ["conf.py"],
            "transforms": {"conf.py": [{"find": "private-name", "replace": "public-name"}]},
        },
    )
    run(["init"], cwd=repo)
    assert run([], cwd=repo).code == 0

    r = run([], cwd=repo)
    assert r.code == 0
    assert "Nothing to publish" in r.stdout

    r = run(["status"], cwd=repo)
    assert r.code == 0
    assert "No changes to publish" in r.stdout
