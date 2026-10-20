from pathlib import Path

import pytest

import pubrepo


def _base(release=None):
    config = {
        "remote": "file:///tmp/public.git", "branch": "main", "dir": ".publish",
        "include": ["pubrepo.py"], "exclude": [], "keep": [],
        "on_republish": "new_commit",
    }
    if release is not None:
        config["release"] = release
    return config


def test_release_absent_is_completely_inert():
    assert pubrepo.validate_release_config(_base()) == []
    assert not pubrepo.release_enabled(_base())


# AC75: §16 row "§13 AC2 Disabled/absent release config changes nothing observable" (Replaced → §10, AC75): the warning text names tag_format/adopt_tags
def test_disabled_release_settings_emit_exactly_one_warning():
    findings = pubrepo.validate_release_config(_base({"enabled": False, "bogus": 1}))
    assert findings == [{
        "level": "warning", "field": "release",
        "message": pubrepo.RELEASE_DISABLED_WARNING,
    }]


# AC75: §16 row "§13 AC2 Disabled/absent release config changes nothing observable" (Replaced → §10, AC75): the warning text names tag_format/adopt_tags
def test_disabled_release_skips_all_nested_semantic_validation():
    findings = pubrepo.validate_release_config(_base({
        "enabled": False,
        "version_file": "../../private",
        "version_pattern": "[",
        "tag_format": "bad tag {version}",
        "github": {"enabled": True, "repository": "", "assets": ["../../secret"]},
        "pypi": {"enabled": True, "package": "", "repository": "custom"},
        "future": {"arbitrary": True},
    }))
    assert findings == [{
        "level": "warning", "field": "release",
        "message": pubrepo.RELEASE_DISABLED_WARNING,
    }]


def test_design_example_release_config_validates():
    release = {
        "enabled": True, "version_file": "pubrepo.py",
        "version_pattern": pubrepo.DEFAULT_VERSION_PATTERN,
        "tag_format": "v{version}", "source_tag": True,
        "source_remote": "origin",
        "github": {"enabled": True, "repository": "owner/repo", "assets": ["pubrepo.py"],
                   "checksum_file": "SHA256SUMS", "notes_file": "pubrepo.py",
                   "include_distributions": True},
        "pypi": {"enabled": True, "package": "pubrepo", "repository": "pypi"},
    }
    assert [x for x in pubrepo.validate_release_config(_base(release))
            if x["level"] == "error"] == []


def test_custom_pypi_registry_is_static_error():
    release = {"enabled": True, "version_file": "pubrepo.py",
               "pypi": {"enabled": True, "package": "x", "repository": "custom"}}
    findings = pubrepo.validate_release_config(_base(release))
    assert any(x["field"] == "release.pypi.repository" and "only" in x["message"]
               for x in findings)


def test_version_pattern_requires_exactly_one_named_capture():
    for pattern in (r"(.*)", r"(?P<version>.*)(x)", "["):
        findings = pubrepo.validate_release_config(_base({
            "enabled": True, "version_file": "pubrepo.py", "version_pattern": pattern,
        }))
        assert any(x["field"] == "release.version_pattern" and x["level"] == "error"
                   for x in findings)


@pytest.mark.parametrize("tag_format", ["v1.0.0", "v{version}-{version}", "bad tag {version}"])
def test_tag_format_requires_one_placeholder_and_valid_git_ref(tag_format):
    findings = pubrepo.validate_release_config(_base({
        "enabled": True, "version_file": "pubrepo.py", "tag_format": tag_format,
    }))
    assert any(x["field"] == "release.tag_format" and x["level"] == "error"
               for x in findings)


@pytest.mark.parametrize("field,value", [
    ("assets", ["../../private"]),
    ("assets", [r"..\..\private"]),
    ("notes_file", "../../private"),
    ("checksum_file", "/tmp/private"),
])
def test_github_candidate_paths_cannot_escape(field, value):
    github = {"enabled": True, "repository": "owner/repo", field: value}
    findings = pubrepo.validate_release_config(_base({
        "enabled": True, "version_file": "pubrepo.py", "github": github,
    }))
    assert any(x["field"] == f"release.github.{field}" and x["level"] == "error"
               for x in findings)


def test_release_environment_requires_published_version_file(tmp_path: Path):
    (tmp_path / "version.txt").write_text("1.0.0")
    config = _base({"enabled": True, "version_file": "version.txt"})
    findings = pubrepo.validate_release_environment(config, tmp_path)
    assert any("not in the resolved publish set" in x["message"] for x in findings)


def test_release_environment_rejects_version_file_escape(tmp_path: Path):
    outside = tmp_path.parent / "private-version.txt"
    outside.write_text("1.0.0")
    config = _base({"enabled": True, "version_file": "../private-version.txt"})
    findings = pubrepo.validate_release_environment(config, tmp_path)
    assert any("escapes source root" in x["message"] for x in findings)


def test_release_environment_accepts_included_version_file(tmp_path: Path):
    (tmp_path / "pubrepo.py").write_text('__version__ = "1.0.0"\n')
    config = _base({"enabled": True, "version_file": "pubrepo.py"})
    assert pubrepo.validate_release_environment(config, tmp_path) == []


def test_unknown_enabled_release_key_warns():
    findings = pubrepo.validate_release_config(_base({
        "enabled": True, "version_file": "pubrepo.py", "future": True,
    }))
    assert any(x["level"] == "warning" and "future" in x["message"] for x in findings)


def test_disabled_release_keeps_tag_format_and_adopt_tags_without_a_warning():
    """Docs match: tag_format and adopt_tags still
    apply with releases disabled (managed_release_targets), so they draw no
    "ignored" warning; any other key does, and the warning names the two."""
    kept = {"enabled": False, "tag_format": "rel-{version}", "adopt_tags": ["v1.0.0"]}
    assert pubrepo.validate_release_config(_base(kept)) == []
    findings = pubrepo.validate_release_config(_base({**kept, "version_file": "x.py"}))
    assert [f["message"] for f in findings] == [pubrepo.RELEASE_DISABLED_WARNING]
    assert "other than tag_format and adopt_tags are ignored" in findings[0]["message"]
