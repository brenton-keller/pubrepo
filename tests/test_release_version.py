from pathlib import Path

import pytest

import pubrepo


@pytest.mark.parametrize("value", [
    "0.0.0", "1.2.3", "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-0.3.7",
    "1.0.0-x.7.z.92", "1.0.0+build.1", "1.0.0-alpha+build",
])
def test_parse_semver_valid(value):
    assert pubrepo.parse_semver(value)[:3] == tuple(map(int, value.split("-")[0].split("+")[0].split(".")))


@pytest.mark.parametrize("value", [
    "v1.0.0", "1", "1.0", "01.0.0", "1.01.0", "1.0.01", "1.0.0-01",
    "1.0.0-", "1.0.0+", "1.0.0_alpha", " 1.0.0", "1.0.0 ",
])
def test_parse_semver_invalid(value):
    with pytest.raises(ValueError):
        pubrepo.parse_semver(value)


def test_leading_v_hint_is_exactly_actionable():
    with pytest.raises(ValueError, match=r"pass 1\.1\.0; the tag format adds v"):
        pubrepo.parse_semver("v1.1.0")


def test_semver_spec_precedence_chain_and_pairwise_orderings():
    chain = [
        "1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta",
        "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0",
    ]
    assert sorted(chain, key=pubrepo.semver_key) == chain
    # 28 distinct pairwise ordering assertions.
    assert all(pubrepo.semver_key(a) < pubrepo.semver_key(b)
               for i, a in enumerate(chain) for b in chain[i + 1:])


@pytest.mark.parametrize("base,part,expected", [
    ("1.2.3", "patch", "1.2.4"),
    ("1.2.3", "minor", "1.3.0"),
    ("1.2.3", "major", "2.0.0"),
])
def test_bump_version(base, part, expected):
    assert pubrepo.bump_version(base, part) == expected


def test_read_version_multiline_and_exactly_once():
    text = 'x = 1\n__version__ = "1.2.3"\ny = 2\n'
    assert pubrepo.read_version_from_text(text, pubrepo.DEFAULT_VERSION_PATTERN) == "1.2.3"
    with pytest.raises(ValueError, match="matched 2 times"):
        pubrepo.read_version_from_text(text + '__version__ = "2.0.0"\n', pubrepo.DEFAULT_VERSION_PATTERN)
    with pytest.raises(ValueError, match="matched 0 times"):
        pubrepo.read_version_from_text("x = 1\n", pubrepo.DEFAULT_VERSION_PATTERN)


def test_default_pattern_reads_pubrepo_source():
    text = Path(pubrepo.__file__).read_text()
    assert pubrepo.read_version_from_text(text, pubrepo.DEFAULT_VERSION_PATTERN) == pubrepo.__version__


def test_tag_roundtrip_and_unmanaged():
    assert pubrepo.render_tag("release-{version}-final", "1.2.3") == "release-1.2.3-final"
    assert pubrepo.tag_version("release-{version}-final", "release-1.2.3-final") == "1.2.3"
    assert pubrepo.tag_version("v{version}", "published/now") is None


@pytest.mark.parametrize("version,expected", [
    ("1.2.0", "1.2.0"),
    ("1.2.0-alpha.1", "1.2.0a1"),
    ("1.2.0-beta.2", "1.2.0b2"),
    ("1.2.0-rc.3", "1.2.0rc3"),
])
def test_pep440_approved_mappings(version, expected):
    assert pubrepo.pep440_prerelease(version) == expected


@pytest.mark.parametrize("version", ["1.2.0-alpha", "1.2.0-beta.2.1", "1.2.0-pre.1"])
def test_pep440_refusals_show_both_contracts(version):
    with pytest.raises(ValueError) as exc:
        pubrepo.pep440_prerelease(version)
    assert version in str(exc.value)
    assert "-alpha.N, -beta.N, or -rc.N" in str(exc.value)
