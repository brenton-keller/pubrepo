"""Push targets (design §18.9-§18.12): remote
identity (§18.10, AC127/AC128), the private-target check (§18.11,
§18.12, AC129), the R1 config keys and private_remotes_line.

Every non-local URL that reaches git config goes through
`sandbox_unreachable`, and is written into the config file
directly: the PATH git shim refuses a URL in argv. `ssh -G` is the real ssh
reading only PUBREPO_TEST_SSH_CONFIG. The git_oracle tests run the
real git through logging stubs and never connect."""
import os
import shutil
import tomllib
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest

import conftest
import pubrepo
from conftest import git_run
from ff_fixtures import push_argv

HAS_SSH = shutil.which("ssh") is not None
needs_ssh = pytest.mark.skipif(not HAS_SSH, reason="ssh -G (the host resolver) is not installed")


def _q(value: str) -> str:
    """A git config value, quoted only when it must be (surrounding
    whitespace or a config special character)."""
    if value == value.strip() and not any(c in value for c in '"\\#;'):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _add_remote(repo, name, url, *pushurls, **keys):
    """remote <name> with `url` and extra `pushurls`, written into the
    repo's config file (the git shim refuses a URL in argv)."""
    lines = [f'[remote "{name}"]', f"\turl = {_q(url)}",
             *(f"\tpushurl = {_q(p)}" for p in pushurls),
             f"\tfetch = +refs/heads/*:refs/remotes/{name}/*",
             *(f"\t{k} = {_q(v)}" for k, v in keys.items())]
    with open(repo / ".git" / "config", "a") as f:
        f.write("\n".join(lines) + "\n")


def _add_config(repo, section_line: str, *entries: str):
    with open(repo / ".git" / "config", "a") as f:
        f.write(section_line + "\n" + "".join(f"\t{e}\n" for e in entries))


@pytest.fixture
def ssh_config(tmp_path, monkeypatch):
    """The seam: the file `ssh -G -F` reads under the sandbox."""
    path = tmp_path / "ssh_config"
    path.write_text("")
    monkeypatch.setenv(pubrepo.TEST_SSH_CONFIG_VAR, str(path))
    return path


@pytest.fixture
def target(source_repo, public_remote):
    """A source repo with a local publish remote; `.check(name, **cfg)` is
    private_target_refusal with that config plus `cfg`."""
    remote = public_remote()

    def make(**cfg):
        config = {"remote": remote, "include": ["a.py"], **cfg}
        repo = source_repo({"a.py": "x\n"}, config, origin=False)
        ns = SimpleNamespace(repo=repo, remote=remote, config=config)
        ns.check = lambda name, **more: pubrepo.private_target_refusal(
            repo, {**config, **more}, name)
        return ns

    return make


# --- §18.10: identity (AC127) -------------------------------------------------------------

@needs_ssh
@pytest.mark.parametrize("url, identity", [
    ("https://github.com/o/r.git", ("github.com", None, "o/r")),
    ("ssh://git@github.com/o/r.git", ("github.com", None, "o/r")),
    ("git@github.com:o/r.git", ("github.com", None, "o/r")),
    ("github.com:o/r", ("github.com", None, "o/r")),                 # scp without a user
    ("https://GitHub.COM./O/R.GIT", ("github.com", None, "o/r")),     # case, trailing dot, .GIT
    ("https://github.com:443/o/r", ("github.com", None, "o/r")),      # default port
    ("ssh://git@github.com:22/o/r", ("github.com", None, "o/r")),
    ("https://github.com/o/%72.git", ("github.com", None, "o/r")),    # %-encoded path
    ("http://h.example.invalid:8080/x", ("h.example.invalid", 8080, "x")),
    ("git://h.example.invalid/x", ("h.example.invalid", None, "x")),
])
def test_identity_forms(ssh_config, url, identity):
    """push_targets::identity_forms (AC127)."""
    assert pubrepo.remote_identity(url) == pubrepo.RemoteId(*identity)


def test_identity_local_path_and_file_url(tmp_path):
    """push_targets::identity_forms, local: a path and its file:// URL name
    one repository (the resolved path, `.git` dropped)."""
    bare = tmp_path / "r.git"
    bare.mkdir()
    want = pubrepo.RemoteId("", None, str(tmp_path.resolve() / "r"))
    assert pubrepo.remote_identity(str(bare)) == want
    assert pubrepo.remote_identity(bare.as_uri()) == want


@pytest.mark.parametrize("url", ["https::https://github.com/o/r.git",
                                 "ext::ssh -p 22 example.invalid %S o/r", "ssh:///srv/r.git",
                                 "a::b", "1x::y", "1foo::x", "foo://h.example.invalid/x"])
def test_helper_and_hostless_urls_are_unknown(url):
    """push_targets::helper_and_hostless_urls_unknown (AC127): helper syntax,
    a host-less ssh URL and an unknown scheme."""
    assert pubrepo.remote_identity(url) is None


def test_helper_and_hostless_urls_refused_unless_the_exact_value_is_listed(
        target, sandbox_unreachable):
    """push_targets::helper_and_hostless_urls_unknown: refused while
    unlisted; the same exact value listed → passes; a differently spelled
    equivalent listed → still refused."""
    url = sandbox_unreachable("https::https://backup.example.invalid/o/r.git")
    t = target()
    _add_remote(t.repo, "t", url)
    assert "cannot be identified" in t.check("t")
    assert t.check("t", private_remotes=["https::https://backup.example.invalid/o/r.git"]) is None
    assert t.check("t", private_remotes=["https://backup.example.invalid/o/r.git"]) is not None


@needs_ssh
@pytest.mark.parametrize("url, host", [("ssh://git@[::1]/r.git", "::1"),
                                       ("[2001:db8::1]:o/r", "2001:db8::1")])
def test_ipv6_literals_are_not_helper_syntax(url, host):
    """push_targets::helper_prefix_anchored_ipv6_ok."""
    assert pubrepo.remote_identity(url).host == host


def test_double_colon_prefixes():
    """push_targets::helper_prefix_anchored_ipv6_ok: `::foo` is not helper
    syntax (nothing before `::`); `a::b`, `1x::y`, `1foo::x` are."""
    assert not pubrepo._HELPER_RE.match("::foo")
    for value in ("a::b", "1x::y", "1foo::x"):
        assert pubrepo._HELPER_RE.match(value)


@pytest.mark.parametrize("url", ["https://git%68ub.com/o/r", "https://github.com%2e/o/r",
                                 "https://ｇｉｔｈｕｂ.ｃｏｍ/o/r", "https://github。com/o/r",
                                 "git://ｅxample.invalid/r", "ssh://ｅxample.invalid/r",
                                 "example。invalid:r"])
def test_host_charset_unknown(url):
    """push_targets::host_charset_unknown: IDNA, fullwidth, U+3002 and
    %-encoded hosts are unknown (pubrepo-side; no git call)."""
    assert pubrepo.remote_identity(url) is None


def test_file_url_host_ignored(target):
    """push_targets::file_url_host_ignored: git's PROTO_FILE ignores
    the host, so file://localhost<path> and file://anyhost<path> are the
    publish repo: refused even when listed (rule 2)."""
    t = target()
    path = t.remote.removeprefix("file://")
    for url in (f"file://localhost{path}", f"file://anyhost{path}"):
        name = "l" + str(len(url))
        _add_remote(t.repo, name, url)
        assert "is the publish remote" in t.check(name, private_remotes=[url])


@needs_ssh
def test_ssh_g_on_host_part_only(ssh_config):
    """push_targets::ssh_g_on_host_part_only: `ssh -G -p 2222
    pubalias`, never `pubalias:2222`; a colon hostname counts only as an IP
    literal."""
    ssh_config.write_text("Host pubalias\n  HostName github.com\n"
                          "Host v6\n  HostName fe80::1\nHost bad\n  HostName not:an:ip\n")
    assert pubrepo.remote_identity("[pubalias:2222]:o/r") == \
        pubrepo.RemoteId("github.com", 2222, "o/r")
    assert pubrepo.remote_identity("v6:o/r").host == "fe80::1"
    assert pubrepo.remote_identity("bad:o/r") is None


@needs_ssh
def test_ssh_g_match_user(ssh_config):
    """push_targets::ssh_g_match_user: the URL's user goes to ssh as
    -l, so a `Match user` block resolves as it does for git."""
    ssh_config.write_text("Match user deploy host pubalias\n  HostName github.com\n"
                          "Host pubalias\n  HostName private.example.invalid\n")
    assert pubrepo.remote_identity("ssh://deploy@pubalias/o/r").host == "github.com"
    assert pubrepo.remote_identity("ssh://other@pubalias/o/r").host == "private.example.invalid"


@needs_ssh
def test_ssh_alias_resolves_to_github(target, ssh_config):
    """push_targets::ssh_alias_resolves_to_github (AC127): `Host
    pubalias / HostName github.com` → public, refused."""
    ssh_config.write_text("Host pubalias\n  HostName github.com\n")
    t = target()
    _add_remote(t.repo, "t", "pubalias:o/r.git")
    assert "'pubalias:o/r.git' is not listed" in t.check("t")
    assert "on a public host" in t.check("t")


@needs_ssh
def test_ssh_github_443_is_public(target, sandbox_unreachable):
    """push_targets::ssh_github_443_is_public (AC127): ssh.github.com is a
    subdomain of github.com (R4)."""
    url = sandbox_unreachable("ssh://git@ssh.github.com:443/o/r.git")
    assert pubrepo.remote_identity(url) == pubrepo.RemoteId("ssh.github.com", 443, "o/r")
    t = target()
    _add_remote(t.repo, "t", url)
    assert "on a public host" in t.check("t")


def test_subdomain_and_public_hosts_key():
    """push_targets::subdomain_and_public_hosts_key (AC127, R4)."""
    assert pubrepo.is_public_host("git.sr.ht", {})
    assert pubrepo.is_public_host("GitHub.com.", {})
    assert pubrepo.is_public_host("github.com:22", {})
    assert not pubrepo.is_public_host("notgithub.com", {})
    assert pubrepo.is_public_host("git.corp.example.invalid",
                                  {"public_hosts": ["corp.example.invalid"]})


@needs_ssh
def test_unknown_host_refused_unless_listed(target, ssh_config, monkeypatch):
    """push_targets::unknown_host_refused_unless_listed (R3): an
    unconfigured dotless alias (ssh -G echoes it) → unknown → refused;
    ssh absent → unknown; listed in private_remotes → passes."""
    t = target()
    _add_remote(t.repo, "t", "backupbox:o/r.git")
    assert "cannot be identified" in t.check("t")
    assert t.check("t", private_remotes=["backupbox:o/r.git"]) is None
    monkeypatch.setenv("PATH", os.pathsep.join(
        p for p in os.environ["PATH"].split(os.pathsep) if not (p and os.path.exists(
            os.path.join(p, "ssh")))))
    assert pubrepo.remote_identity("git@host.example.invalid:o/r") is None


# --- §18.10: git is the oracle (AC128) ----------------------------------------------

# Every ssh-family form and every bypass of 1.0.2 audit rounds 1-3.
SSH_CORPUS = [
    "git@github.com:owner/r.git", "github.com:owner/r.git", "ssh://git@github.com/owner/r.git",
    "ssh://git@ssh.github.com:443/owner/r.git", "ssh://git@[::1]:2222/x.git",
    "ssh://[::1]/x.git", "[::1]:x.git", "[2001:db8::1]:x.git", "[github.com:22]:owner/leak.git",
    "git@[github.com:22]:owner/leak.git", "[gh:22]:owner/leak.git",
    "[github.com]:owner/leak.git", "git@[gh:2222]:owner/leak.git",
    "[gitlab.com:2222]:owner/leak.git", "[user@gh:22]:p.git", "git+ssh://git@h.example/x",
    "ssh+git://h.example:2200/x", "ssh://git%40github.com/x.git", "host:22:path.git",
    "nas:backups/src.git", "box:/srv/git/pub.git", "git@git.example.invalid:Owner/pub.GIT",
    "[gh.example:2222]:o/r", "ssh://h.example:22/r",
]
GIT_CORPUS = ["git://h.example.invalid/x", "git://Host.Example.invalid:9419/o/r",
              "git://[::1]/r", "git://[::1]:9419/x"]
# Offline by construction: .invalid names, localhost, 127.0.0.0/8 and
# documentation literals only, so a proxy bypass could reach no external host.
HTTP_CORPUS = ["https://Host.Example.invalid.:8443/o/r", "https://example.invalid/o/r.git",
               "http://user@example.invalid/r", "http://h.example.invalid:8080/x",
               "https://[2001:db8::1]:8443/r", "http://localhost/x", "https://127.0.0.2/x",
               "http://192.0.2.7:81/x"]


def _default_none(kind: str, port):
    return None if port == {"ssh": 22, "git": 9418, "https": 443, "http": 80}[kind] else port


@pytest.mark.git_oracle
@pytest.mark.parametrize("url", SSH_CORPUS)
def test_identity_matches_git_ssh_oracle(git_oracle, url):
    """push_targets::identity_matches_git_ssh_oracle: pubrepo's (host,
    port) is the host and -p git passes to ssh (default ports → None)."""
    kind, _user, host, port, _path = pubrepo._git_remote_target(url)
    assert kind == "ssh"
    g_host, g_port = git_oracle.ssh(url)
    assert (host, _default_none("ssh", port)) == (g_host, _default_none("ssh", g_port))


@pytest.mark.git_oracle
@pytest.mark.parametrize("url", GIT_CORPUS)
def test_identity_matches_git_proxy_oracle(git_oracle, url):
    """push_targets::identity_matches_git_proxy_oracle: git://, through
    GIT_PROXY_COMMAND (host, port)."""
    kind, _user, host, port, _path = pubrepo._git_remote_target(url)
    assert kind == "git"
    assert (host, port or 9418) == git_oracle.git(url)


@pytest.mark.git_oracle
@pytest.mark.parametrize("url", HTTP_CORPUS)
def test_identity_matches_git_http_proxy_oracle(git_oracle, url):
    """push_targets::identity_matches_git_http_proxy_oracle: curl's
    parse via a loopback proxy. Exactly one request per form (fail loud: a
    form git sends without the proxy fails here)."""
    request = git_oracle.http(url)
    method, target_ = request[0].split(" ")[:2]
    scheme = url.split("://", 1)[0]
    if method == "CONNECT":
        g_host, _, g_port = target_.rpartition(":")
        g_port = int(g_port)
    else:
        parsed = urllib.parse.urlsplit(target_)
        g_host, g_port = parsed.hostname, parsed.port or 80
    kind, _user, host, port, _path = pubrepo._git_remote_target(url)
    assert kind == "curl:" + scheme
    assert (pubrepo._norm_host(host), _default_none(scheme, port)) == \
        (pubrepo._norm_host(g_host.strip("[]")), _default_none(scheme, g_port))


@pytest.mark.git_oracle
def test_git_oracle_push_by_remote_name(git_oracle, source_repo, public_remote,
                                        sandbox_unreachable):
    """The oracle also runs a push or fetch by remote NAME: git
    connects where pubrepo's parser says the remote's url points."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]},
                       origin=False)
    url = sandbox_unreachable("ssh://git@backup.example.invalid:2222/o/r.git")
    _add_remote(repo, "bk", url)
    found = git_oracle.remote(repo, "push", "bk", "HEAD:refs/heads/x")
    assert found and found[0][0] == "ssh"
    assert "-p" in found[0][1] and "git@backup.example.invalid" in found[0][1]
    assert pubrepo._git_remote_target(url)[2:4] == ("backup.example.invalid", 2222)
    found = git_oracle.remote(repo, "fetch", "bk")
    assert found and found[0][0] == "ssh"


# --- §18.11 (AC129): the allowlist gate -------------------------------------------------

def test_allowlist_listed_private_passes(target, tmp_path):
    """push_targets::listed_private_passes: every value listed → None."""
    t = target()
    backup = tmp_path / "backup.git"
    git_run("init", "--bare", "-q", str(backup))
    _add_remote(t.repo, "t", str(backup))
    assert t.check("t", private_remotes=[str(backup)]) is None


@needs_ssh
def test_allowlist_listed_ipv6_and_dotless_alias_pass(target, sandbox_unreachable):
    """push_targets::listed_private_passes: an IPv6 literal and a dotless
    ssh alias, listed → pass."""
    t = target()
    _add_remote(t.repo, "v6", sandbox_unreachable("ssh://git@[2001:db8::7]/o/r"))
    _add_remote(t.repo, "alias", "backup:o/r")
    assert t.check("v6", private_remotes=["ssh://git@[2001:db8::7]/o/r"]) is None
    assert t.check("alias", private_remotes=["backup:o/r"]) is None


@pytest.mark.parametrize("shape", ["two_pushurls", "push_insteadof", "insteadof"])
def test_allowlist_unlisted_value_refused(target, tmp_path, sandbox_unreachable, shape):
    """push_targets::allowlist_unlisted_value_refused: the refusal names
    each unlisted value exactly."""
    t = target()
    backup = str(tmp_path / "backup.git")
    if shape == "two_pushurls":
        url = sandbox_unreachable("https://github.com/owner/leak.git")
        _add_remote(t.repo, "t", backup, backup, url)
        unlisted = url
    elif shape == "push_insteadof":
        _add_remote(t.repo, "t", "priv:o/r")
        url = sandbox_unreachable("https://github.com/")
        _add_config(t.repo, f'[url "{url}"]', "pushInsteadOf = priv:")
        unlisted = "https://github.com/o/r"
    else:
        _add_remote(t.repo, "t", "mirror:o/r")
        _add_config(t.repo, f'[url "{backup}/"]', "insteadOf = mirror:")
        unlisted = f"{backup}/o/r"
    listed = [backup, "priv:o/r", "mirror:o/r"]
    why = t.check("t", private_remotes=listed)
    assert f"{unlisted!r} is not listed in [publish] private_remotes" in why


def test_two_pushurls_one_public(target, tmp_path, sandbox_unreachable):
    """push_targets::two_pushurls_one_public (AC124)."""
    t = target()
    backup = str(tmp_path / "backup.git")
    url = sandbox_unreachable("https://github.com/owner/leak.git")
    _add_remote(t.repo, "t", backup, backup, url)
    why = t.check("t", private_remotes=[backup])
    assert "'https://github.com/owner/leak.git' is not listed in [publish] private_remotes " \
           "(it is on a public host)" in why


def test_push_insteadof_private_to_public(target, sandbox_unreachable):
    """push_targets::push_insteadof_private_to_public (AC124):
    url.https://github.com/.pushInsteadOf=priv: with url priv:o/r."""
    t = target()
    _add_remote(t.repo, "t", "priv:o/r")
    url = sandbox_unreachable("https://github.com/")
    _add_config(t.repo, f'[url "{url}"]', "pushInsteadOf = priv:")
    why = t.check("t", private_remotes=["priv:o/r"])
    assert "'https://github.com/o/r' is not listed" in why and "public host" in why


def test_private_remotes_listing_passes(target, sandbox_unreachable):
    """push_targets::private_remotes_listing_passes: a github URL listed
    (the owner's assertion that it is private) → passes."""
    t = target()
    url = sandbox_unreachable("https://github.com/owner/private-backup.git")
    _add_remote(t.repo, "t", url)
    assert t.check("t", private_remotes=["https://github.com/owner/private-backup.git"]) is None


# Every bypass form of 1.0.2 audit rounds 1-4, unlisted (rows use .invalid analogs).
BYPASS_CORPUS = [
    "https::https://github.com/owner/leak.git", "file::/tmp/x.git", "foo://", "1foo::x",
    "https:///github.com/owner/leak.git", "[github.com]:owner/leak.git",
    "[github.com:22]:owner/leak.git", "git@[github.com:22]:owner/leak.git",
    "[gh:22]:owner/leak.git", "[gitlab.com:2222]:owner/leak.git",
    "https://ｅｘａｍｐｌｅ.invalid/owner/leak.git", "https://example。invalid/owner/leak.git",
    "https://ex%61mple.invalid/owner/leak.git", "https://example.invalid%2e/owner/leak.git",
    "git@gh2.invalid:owner/leak.git", "file://localhost/tmp/leak.git",
    "ssh://github.com:+22/owner/x.git",
]


@pytest.mark.parametrize("url", BYPASS_CORPUS)
def test_allowlist_bypass_forms_unlisted_refused(target, ssh_config, sandbox_unreachable, url):
    """push_targets::allowlist_bypass_forms_unlisted_refused: whatever the
    parser makes of it, an unlisted value is refused, named exactly."""
    ssh_config.write_text("Host gh\n  HostName github.com\n"
                          "Match user git host gh2.invalid\n  HostName github.com\n"
                          "Host gh2.invalid\n  HostName private.example.invalid\n")
    if conftest._network_url(url):
        sandbox_unreachable(url)
    t = target()
    _add_remote(t.repo, "t", url)
    assert f"{url!r} is not listed in [publish] private_remotes" in t.check("t")


@pytest.mark.parametrize("wrap", [False, True])
def test_listed_publish_repo_refused(target, wrap):
    """push_targets::listed_publish_repo_refused /
    private_remotes_never_waives_publish_remote: the publish repo
    listed, directly and inside a helper → refused."""
    t = target()
    url = f"file::{t.remote}" if wrap else t.remote
    _add_remote(t.repo, "t", url)
    assert "is the publish remote" in t.check("t", private_remotes=[url])


@needs_ssh
def test_publish_remote_in_other_form_refused(source_repo, sandbox_unreachable):
    """push_targets::publish_remote_in_other_form_refused (rule 2): the
    publish remote in https form, tag_remote in scp form, listed →
    refused."""
    publish = sandbox_unreachable("https://github.com/owner/pub.git")
    config = {"remote": publish, "include": ["a.py"]}
    repo = source_repo({"a.py": "x\n"}, config, origin=False)
    url = sandbox_unreachable("git@github.com:owner/pub.git")
    _add_remote(repo, "t", url)
    why = pubrepo.private_target_refusal(repo, {**config, "private_remotes": [url]}, "t")
    assert "is the publish remote" in why


@pytest.mark.parametrize("url", ["ext::git %s backup", "fd::7", "https::ext::sh -c x"])
def test_listed_ext_fd_refused(target, url):
    """push_targets::listed_ext_fd_refused / ext_fd_always_refused_even_listed
    """
    t = target()
    _add_remote(t.repo, "t", url)
    assert "can never be checked" in t.check("t", private_remotes=[url])


def test_listed_unidentifiable_passes_residual(target):
    """push_targets::listed_unidentifiable_passes_residual: a
    listed value the parser cannot identify passes."""
    t = target()
    _add_remote(t.repo, "t", "file::/srv/backups/src.git")
    assert t.check("t", private_remotes=["file::/srv/backups/src.git"]) is None


@needs_ssh
@pytest.mark.parametrize("url", ["https://github.com/owner/pub.git", "git@github.com:owner/pub",
                                 "https::https://github.com/owner/pub.git",
                                 "https::https::git@github.com:owner/pub.git"])
def test_public_or_publish_forms_refused(source_repo, sandbox_unreachable, url):
    """push_targets::public_or_publish_forms_refused (AC128): the publish
    repo in any form, through nested helpers, listed → refused."""
    publish = sandbox_unreachable("https://github.com/owner/pub.git")
    config = {"remote": publish, "include": ["a.py"], "private_remotes": [url]}
    repo = source_repo({"a.py": "x\n"}, config, origin=False)
    sandbox_unreachable(url)
    _add_remote(repo, "t", url)
    assert "is the publish remote" in pubrepo.private_target_refusal(repo, config, "t")


def test_a2_percent_decoded(source_repo, sandbox_unreachable):
    """push_targets::a2_percent_decoded: the listed
    https://github.com/o/%72 is the publish repo …/o/r → refused."""
    publish = sandbox_unreachable("https://github.com/o/r")
    config = {"remote": publish, "include": ["a.py"],
              "private_remotes": ["https://github.com/o/%72"]}
    repo = source_repo({"a.py": "x\n"}, config, origin=False)
    _add_remote(repo, "t", sandbox_unreachable("https://github.com/o/%72"))
    assert "is the publish remote" in pubrepo.private_target_refusal(repo, config, "t")


# --- §18.12 (AC129): the named remote ------------------------------------------------------

# every remote.<name>.* key in `git help --config` (git 2.56): can it
# change the destination, the program that receives the push, or what is
# pushed? url/pushurl are the private-target check's subject.
REMOTE_KEYS = {
    "url": "the destination (the private-target check)",
    "pushurl": "the destination (the private-target check)",
    "receivepack": "refused: the program run on the receiving side",
    "uploadpack": "refused: the program run for the fetch side",
    "vcs": "refused: the remote helper (transport)",
    "mirror": "refused: makes a push behave like --mirror (with a refspec git refuses; "
              "refusing fails closed)",
    "proxy": "allowed: forwards to the URL's host",
    "proxyAuthMethod": "allowed: proxy authentication only",
    "push": "allowed: default refspecs, ignored with an explicit refspec",
    "fetch": "allowed: fetch side only",
    "tagOpt": "allowed: fetch side only",
    "prune": "allowed: fetch side only",
    "pruneTags": "allowed: fetch side only",
    "skipDefaultUpdate": "allowed: fetch side only",
    "skipFetchAll": "allowed: fetch side only",
    "promisor": "allowed: fetch side only",
    "partialclonefilter": "allowed: fetch side only",
    "serverOption": "allowed: options sent to the server, not the destination",
    "followRemoteHEAD": "allowed: fetch side only",
    "negotiationRestrict": "allowed: which commits are offered as haves (fetch, and push with "
                           "push.negotiate), to the same URL",
    "negotiationInclude": "allowed: which commits are offered as haves (fetch, and push with "
                          "push.negotiate), to the same URL",
    "advertisedAs": "allowed: informational (a promisor remote's advertised name)",
}


def test_remote_keys_table_matches_git_help():
    """push_targets::remote_keys_table_matches_git_help: the table
    covers every remote.<name>.* key the installed git documents, and the
    refused keys are exactly TRANSPORT_ALTERING_KEYS."""
    documented = {line.strip().split(".", 2)[2] for line in
                  git_run("help", "--config").splitlines()
                  if line.strip().startswith("remote.<name>.")}
    assert documented <= set(REMOTE_KEYS), documented - set(REMOTE_KEYS)
    assert {k for k, v in REMOTE_KEYS.items() if v.startswith("refused")} == \
        set(pubrepo.TRANSPORT_ALTERING_KEYS)


@pytest.mark.parametrize("key, value", [("receivepack", "git-receive-pack /tmp/other.git"),
                                        ("uploadpack", "git-upload-pack /tmp/other.git"),
                                        ("vcs", "https"), ("mirror", "true")])
def test_transport_keys_refused(target, tmp_path, key, value):
    """push_targets::transport_keys_refused: refused even when every
    url is listed, naming the key."""
    t = target()
    backup = str(tmp_path / "backup.git")
    _add_remote(t.repo, "t", backup, **{key: value})
    assert f"remote.t.{key} is set" in t.check("t", private_remotes=[backup])


def test_transport_keys_proxy_not_refused(target, tmp_path):
    """push_targets::transport_keys_refused: remote.<n>.proxy is allowed."""
    t = target()
    backup = str(tmp_path / "backup.git")
    _add_remote(t.repo, "t", backup, proxy="http://127.0.0.1:9")
    assert t.check("t", private_remotes=[backup]) is None


def test_whitespace_byte_exact(target, tmp_path):
    """push_targets::whitespace_byte_exact: a value with a trailing
    space is refused, naming it; a spaced private_remotes entry is a config
    error."""
    t = target()
    backup = str(tmp_path / "backup.git")
    _add_remote(t.repo, "t", backup + " ")
    assert f"{backup + ' '!r} has leading or trailing whitespace" in \
        t.check("t", private_remotes=[backup, backup + " "])
    findings = pubrepo.validate_config({"remote": "file:///x.git", "include": ["a.py"],
                                        "private_remotes": ["/srv/x.git "]})
    assert any(f["field"] == "private_remotes" and "'/srv/x.git '" in f["message"]
               for f in findings if f["level"] == "error")


BAD_NAMES = [("a/b", "contains / or \\"), (".", "is . or .."), ("..", "is . or .."),
             ("-x", "starts with -"), ("", "empty"), (" t", "leading or trailing whitespace"),
             ("x:y", "contains :, so it reads as a URL")]


@pytest.mark.parametrize("name, why", BAD_NAMES)
@pytest.mark.parametrize("key", ["tag_remote", "records_remote", "release.source_remote"])
def test_remote_name_rules_validate(key, name, why):
    """push_targets::remote_name_rules, at validation (exit 1)."""
    config = {"remote": "file:///x.git", "include": ["a.py"]}
    if key == "release.source_remote":
        config["release"] = {"enabled": True, "version_file": "a.py", "source_tag": True,
                             "source_remote": name}
    else:
        config[key] = name
    findings = [f for f in pubrepo.validate_config(config) if f["level"] == "error"]
    if key == "release.source_remote" and name == "":
        assert any(f["field"] == key for f in findings)
        return
    assert {"level": "error", "field": key,
            "message": f"'{key}' must name a git remote of the source repo "
                       f"({why}; got {name!r})"} in findings


@pytest.mark.parametrize("name", ["a/b", ".", "..", "-x", "", "x:y", "nosuch"])
def test_remote_name_rules_at_push_time(target, name):
    """push_targets::remote_name_rules, at push time: a bad or
    missing name is refused even if validation was skipped."""
    t = target()
    why = t.check(name)
    assert why is not None and ("not a usable remote name" in why
                                or "is not a remote of the source repository" in why)


@pytest.mark.parametrize("key", ["tag_remote", "records_remote"])
def test_remote_name_must_exist_validate_is_1(source_repo, public_remote, run, key):
    """push_targets::remote_name_rules: a name with no remote →
    `validate` exits 1 naming it."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         key: "nosuch"}, origin=False)
    r = run(["validate"], cwd=repo)
    assert r.code == 1, r.stderr
    assert f"'{key}' must name a git remote of the source repo (no remote 'nosuch')" in \
        r.stdout + r.stderr


@pytest.mark.no_git_calls      # refused at config validation, before any git call
def test_config_rejects_url_valued_remote_keys(source_repo, public_remote, run):
    """push_targets::config_rejects_url_valued_remote_keys (R1):
    tag_remote = "https://…" → validate 1."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": "https://example.invalid/o/r.git"},
                       origin=False)
    r = run(["validate"], cwd=repo)
    assert r.code == 1
    assert "'tag_remote' must name a git remote of the source repo" in r.stdout + r.stderr


def test_new_keys_are_known():
    """tag_remote, private_remotes and public_hosts validate without an
    unknown-key warning."""
    findings = pubrepo.validate_config({"remote": "file:///x.git", "include": ["a.py"],
                                        "tag_remote": "backup",
                                        "private_remotes": ["/srv/x.git"],
                                        "public_hosts": ["git.example.invalid"]})
    assert [f for f in findings if "unknown key" in f["message"]] == []
    assert [f for f in findings if f["level"] == "error"
            and f["field"] in ("tag_remote", "private_remotes", "public_hosts")] == []


# --- §18.11: private_remotes_line ---------------------------------------

def test_private_remotes_line_lists_unlisted_values(target, tmp_path):
    """The exact TOML line for the named remote: existing entries kept, new
    values appended in remote_url_values order; valid TOML."""
    t = target(private_remotes=["/srv/keep.git"])
    a, b = str(tmp_path / "a.git"), str(tmp_path / "b.git")
    _add_remote(t.repo, "rec", a, b)
    line = pubrepo.private_remotes_line(t.repo, t.config, "rec")
    assert line == f'private_remotes = ["/srv/keep.git", "{a}", "{b}"]'
    assert tomllib.loads(line)["private_remotes"] == ["/srv/keep.git", a, b]
    listed = {**t.config, "private_remotes": [a, b]}
    assert pubrepo.private_remotes_line(t.repo, listed, "rec") is None


def test_private_remotes_line_never_lists_a_refused_value(target):
    """Refused values are never offered: the publish repo, ext::, and a
    value with surrounding whitespace; a missing remote → None."""
    t = target()
    _add_remote(t.repo, "rec", t.remote, "ext::sh -c x", "/srv/b.git ")
    assert pubrepo.private_remotes_line(t.repo, t.config, "rec") is None
    assert pubrepo.private_remotes_line(t.repo, t.config, "nosuch") is None



# --- the bookmark (R2, §18.3) at both sites -----------------------------------------

from test_release_transaction import _release  # noqa: E402

SITES = {"publish": ["publish", "--yes"], "release": ["--release", "1.0.0", "--yes"]}


def _bare(tmp_path, name):
    path = tmp_path / f"{name}.git"
    git_run("init", "--bare", "-q", "-b", "main", str(path))
    return path


def _private_refs(where) -> list[str]:
    """published/* tags and refs/pubrepo/* on a repository."""
    out = git_run("ls-remote", str(where), "refs/tags/published/*", "refs/pubrepo/*")
    return [line.split("\t")[1] for line in out.splitlines()]


def _bookmarks(repo) -> list[str]:
    out = git_run("for-each-ref", "--format=%(refname)", "refs/pubrepo/bookmarks/", cwd=repo)
    return out.split()


@pytest.fixture
def source_pushes(monkeypatch):
    """Every `git push` pubrepo runs in a source repo (not in .publish/)."""
    calls = []
    real = pubrepo.git

    def spy(*args, cwd=None, **kwargs):
        if args and args[0] == "push" and cwd is not None and ".publish" not in str(cwd):
            calls.append(list(args))
        return real(*args, cwd=cwd, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spy)
    return calls


def _site_repo(source_repo, remote, site, **cfg):
    config = {"remote": remote, "include": ["version.py", "a.txt"], **cfg}
    if site == "release":
        config["release"] = _release()
    return source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, config,
                       origin=False)


def _run_site(run, repo, site):
    assert run(["init"], cwd=repo).code == 0
    r = run(SITES[site], cwd=repo)
    assert r.code == 0, r.stderr
    assert len(_bookmarks(repo)) == 1                                    # recorded locally
    assert git_run("tag", "-l", "published/*", cwd=repo).strip() == ""  # R2: never a tag
    return r


@pytest.mark.parametrize("site", SITES)
def test_bookmark_origin_equal_to_publish_remote_gets_no_push(source_repo, public_remote, run,
                                                              source_pushes, site):
    """push_targets::bookmark_cases (AC119): origin == the publish remote →
    no push; the public remote holds no private ref."""
    remote = public_remote()
    repo = _site_repo(source_repo, remote, site)
    git_run("remote", "add", "origin", remote, cwd=repo)
    _run_site(run, repo, site)
    assert source_pushes == []
    assert _private_refs(remote.removeprefix("file://")) == []


@pytest.mark.parametrize("site", SITES)
def test_bookmark_origin_on_github_gets_no_push(source_repo, public_remote, run, source_pushes,
                                                sandbox_unreachable, site):
    """push_targets::bookmark_cases (AC119): origin on github.com, no
    tag_remote → no bookmark push, and nothing said about it by default."""
    repo = _site_repo(source_repo, public_remote(), site)
    url = sandbox_unreachable("https://github.com/owner/source.git")
    _add_remote(repo, "origin", url)
    r = _run_site(run, repo, site)
    assert source_pushes == []
    assert "Bookmark" not in r.stderr and "bookmark" not in r.stderr


@pytest.mark.parametrize("site", SITES)
def test_bookmark_private_tag_remote_receives_the_tag(source_repo, public_remote, run,
                                                     tmp_path, site):
    """push_targets::bookmark_cases (AC119, R2): a listed private
    tag_remote receives refs/tags/published/<ts>, pushed by name from the
    bookmark ref with --recurse-submodules=no."""
    remote = public_remote()
    backup = _bare(tmp_path, "private-backup")
    repo = _site_repo(source_repo, remote, site, tag_remote="backup",
                      private_remotes=[str(backup)])
    git_run("remote", "add", "backup", str(backup), cwd=repo)
    r = _run_site(run, repo, site)
    ts = _bookmarks(repo)[0].removeprefix("refs/pubrepo/bookmarks/")
    assert _private_refs(backup) == [f"refs/tags/published/{ts}"]
    assert git_run("rev-parse", f"refs/tags/published/{ts}", cwd=backup) == \
        git_run("rev-parse", "HEAD", cwd=repo)
    assert f"Bookmark {ts} pushed to backup" in r.stderr
    assert _private_refs(remote.removeprefix("file://")) == []


@pytest.mark.parametrize("site", SITES)
def test_bookmark_unlisted_github_tag_remote_refused(source_repo, public_remote, run,
                                                    source_pushes, sandbox_unreachable, site):
    """push_targets::bookmark_cases (AC119): a tag_remote on github.com not
    in private_remotes → refused before any network call, one warning
    naming the value, exit 0."""
    repo = _site_repo(source_repo, public_remote(), site, tag_remote="gh")
    _add_remote(repo, "gh", sandbox_unreachable("https://github.com/owner/source.git"))
    r = _run_site(run, repo, site)
    assert source_pushes == []
    assert "WARNING: refusing to push private ref refs/pubrepo/bookmarks/" in r.stderr
    assert ("'https://github.com/owner/source.git' is not listed in [publish] "
            "private_remotes (it is on a public host)") in r.stderr
    assert "; kept locally" in r.stderr


@pytest.mark.parametrize("site", SITES)
def test_bookmark_tag_remote_equal_to_publish_remote_in_other_form_refused(
        source_repo, run, tmp_path, source_pushes, site):
    """push_targets::bookmark_cases (AC119, rule 2): the publish remote as
    a path, tag_remote its file:// spelling, listed → refused."""
    public = _bare(tmp_path, "pub")
    repo = _site_repo(source_repo, str(public), site, tag_remote="mirror",
                      private_remotes=[public.as_uri()])
    git_run("remote", "add", "mirror", public.as_uri(), cwd=repo)
    r = _run_site(run, repo, site)
    assert source_pushes == []
    assert "is the publish remote" in r.stderr
    assert _private_refs(public) == []


def test_source_origin_is_the_public_repo_shape(source_repo, public_remote, run):
    """push_targets::source_origin_is_the_public_repo_shape (AC119): the source
    origin is the public repo, no tag_remote; after a clean publish the public
    remote has no published/* tag, no refs/pubrepo/* and not the source commit."""
    remote = public_remote()
    bare = remote.removeprefix("file://")
    repo = source_repo({"src/app.py": "print('public')\n", "notes/private.md": "private\n"},
                       {"remote": remote, "include": ["src"]}, origin=False)
    git_run("remote", "add", "origin", remote, cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    source_commit = git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert _private_refs(bare) == []
    assert pubrepo.git("cat-file", "-e", source_commit, cwd=bare, check=False).returncode != 0
    assert git_run("ls-tree", "-r", "--name-only", "main", cwd=bare).split() == ["src/app.py"]


def test_push_tags_carries_no_bookmark(source_repo, public_remote, run, tmp_path):
    """push_targets::push_tags_carries_no_bookmark (AC125): `git push --tags
    origin` after a publish sends no published/* (the bookmark is not a
    tag), and there is no local published/* tag."""
    origin = _bare(tmp_path, "origin")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]},
                       origin=False)
    git_run("remote", "add", "origin", str(origin), cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    # the bare must have a ref: records no longer reach origin by default
    git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=repo)
    git_run("push", "-q", "--tags", "origin", cwd=repo)
    assert git_run("ls-remote", str(origin), "refs/tags/*").strip() == ""
    assert git_run("for-each-ref", "refs/tags/published", cwd=repo).strip() == ""
    assert len(_bookmarks(repo)) == 1


def test_s_pushes_no_recursion_bookmark(source_repo, public_remote, run, tmp_path,
                                       source_pushes):
    """push_targets::s_pushes_no_recursion (the bookmark push;
    adds source_tag): under push.recurseSubmodules=on-demand with an
    unpushed submodule commit, the bookmark push carries
    --recurse-submodules=no, succeeds, and no submodule remote is touched."""
    sub_remote = _bare(tmp_path, "sub-remote")
    sub_src = tmp_path / "sub-src"
    git_run("clone", "-q", str(sub_remote), str(sub_src))
    (sub_src / "s.txt").write_text("s\n")
    git_run("add", "s.txt", cwd=sub_src)
    git_run("commit", "-qm", "s", cwd=sub_src)
    git_run("push", "-q", "origin", "HEAD:main", cwd=sub_src)
    backup = _bare(tmp_path, "backup")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": "backup",
                                         "private_remotes": [str(backup)]}, origin=False)
    git_run("remote", "add", "backup", str(backup), cwd=repo)
    git_run("-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub_remote),
            "sub", cwd=repo)
    (repo / "sub" / "s.txt").write_text("unpushed\n")
    git_run("commit", "-qam", "unpushed sub", cwd=repo / "sub")
    git_run("commit", "-qam", "bump sub", cwd=repo)
    git_run("config", "push.recurseSubmodules", "on-demand", cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert source_pushes and all("--recurse-submodules=no" in call for call in source_pushes)
    assert len(_private_refs(backup)) == 1
    assert len(git_run("ls-remote", str(sub_remote), "refs/heads/*").splitlines()) == 1


# The bookmark shapes of an earlier critique round and 1.0.2 audit
# round 5; the R5 shapes land with check_publish_clone_origin.

@pytest.mark.no_git_calls      # refused at config validation, before any git call
def test_critique_r2_tag_remote_as_a_path_is_a_config_error(source_repo, public_remote, run,
                                                           tmp_path, source_pushes):
    """push_targets::critique_r2_and_round5_shapes: tag_remote set to
    an absolute path whose [remote "<path>"] holds a listed url → config
    error (1); nothing is pushed into the path or the listed repo."""
    target = _bare(tmp_path, "path-target")
    listed = _bare(tmp_path, "listed")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": str(target),
                                         "private_remotes": [str(listed)]}, origin=False)
    git_run("config", f"remote.{target}.url", str(listed), cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 1 and "'tag_remote' must name a git remote" in r.stderr, r.stderr
    assert source_pushes == []
    assert _private_refs(target) == [] and _private_refs(listed) == []


@pytest.mark.parametrize("key", ["receivepack", "uploadpack", "vcs"])
def test_critique_r2_transport_keys_land_nowhere(source_repo, public_remote, run, tmp_path,
                                                 source_pushes, key):
    """A listed tag_remote with receivepack (aimed at
    another bare), uploadpack or vcs → refused, naming the key; nothing in
    either bare."""
    backup = _bare(tmp_path, "backup")
    other = _bare(tmp_path, "other")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": "t",
                                         "private_remotes": [str(backup)]}, origin=False)
    git_run("remote", "add", "t", str(backup), cwd=repo)
    value = {"receivepack": f"git-receive-pack {other}",
             "uploadpack": f"git-upload-pack {other}", "vcs": "file"}[key]
    git_run("config", f"remote.t.{key}", value, cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert f"remote.t.{key} is set" in r.stderr and source_pushes == []
    assert _private_refs(other) == [] and _private_refs(backup) == []


def test_critique_r2_trailing_whitespace_url_lands_nowhere(source_repo, public_remote, run,
                                                          tmp_path, source_pushes):
    """A tag_remote url with a trailing space (a different
    repository for git) → refused, naming it; nothing pushed."""
    backup = _bare(tmp_path, "backup")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": "t",
                                         "private_remotes": [str(backup)]}, origin=False)
    git_run("remote", "add", "t", str(backup), cwd=repo)
    git_run("config", "remote.t.url", str(backup) + " ", cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "has leading or trailing whitespace" in r.stderr and source_pushes == []
    assert _private_refs(backup) == []


def test_critique_r2_percent_encoded_publish_spelling_refused(source_repo, run, tmp_path,
                                                             source_pushes):
    """A listed %-encoded spelling of the publish repo as the
    tag_remote → refused; the publish repo gets no private ref."""
    public = _bare(tmp_path, "pub")
    encoded = public.as_uri().replace("/pub.git", "/%70ub.git")
    repo = source_repo({"a.py": "x\n"}, {"remote": str(public), "include": ["a.py"],
                                         "tag_remote": "t", "private_remotes": [encoded]},
                       origin=False)
    git_run("remote", "add", "t", encoded, cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert "is the publish remote" in r.stderr and source_pushes == []
    assert _private_refs(public) == []



# --- release source_tag and the push preflight (§18.2, §18.4, R1, R6) ------------

import json  # noqa: E402


@pytest.fixture
def git_calls(monkeypatch):
    """Every git call pubrepo makes: (args, cwd)."""
    calls = []
    real = pubrepo.git

    def spy(*args, cwd=None, **kwargs):
        calls.append((list(args), cwd))
        return real(*args, cwd=cwd, **kwargs)

    monkeypatch.setattr(pubrepo, "git", spy)
    return calls


def _release_repo(source_repo, remote, **cfg):
    config = {"remote": remote, "include": ["version.py", "a.txt"],
              "release": _release(source_tag=True, source_remote="src"), **cfg}
    return source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"}, config,
                       origin=False)


@pytest.mark.parametrize("shape", ["github", "publish_repo"])
def test_source_tag_public_remote_refused_7(source_repo, public_remote, run, git_calls,
                                            sandbox_unreachable, tmp_path, shape):
    """push_targets::source_tag_public_remote_refused_7 (AC121): source_remote
    on github.com, or the publish repo (listed), → 7 in preflight, with zero
    push and zero ls-remote to it."""
    remote = public_remote()
    if shape == "github":
        repo = _release_repo(source_repo, remote)
        _add_remote(repo, "src", sandbox_unreachable("https://github.com/owner/source.git"))
        want = "is not listed in [publish] private_remotes (it is on a public host)"
    else:
        repo = _release_repo(source_repo, remote, private_remotes=[remote])
        git_run("remote", "add", "src", remote, cwd=repo)
        want = "is the publish remote"
    assert run(["init"], cwd=repo).code == 0
    git_calls.clear()
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "ERROR: refusing to push private ref refs/tags/v1.0.0 to src:" in r.stderr
    assert want in r.stderr
    assert not [a for a, cwd in git_calls if "src" in a and a[0] in ("push", "ls-remote")]
    assert git_run("ls-remote", remote.removeprefix("file://"), "refs/tags/*").strip() == ""


def test_release_check_does_not_query_a_refused_source_remote(source_repo, public_remote, run,
                                                             git_calls, sandbox_unreachable):
    """`validate --release-check` reports the refusal and makes no
    ls-remote to the refused source_remote."""
    repo = _release_repo(source_repo, public_remote())
    _add_remote(repo, "src", sandbox_unreachable("https://github.com/owner/source.git"))
    assert run(["init"], cwd=repo).code == 0
    git_calls.clear()
    r = run(["validate", "--release-check"], cwd=repo)
    assert "refusing to push private ref refs/tags/v1.0.0 to src" in r.stdout + r.stderr
    assert not [a for a, cwd in git_calls if "src" in a and a[0] in ("push", "ls-remote")]


def _plan_file(repo) -> dict:
    common = git_run("rev-parse", "--git-common-dir", cwd=repo).strip()
    return json.loads(((repo / common).resolve() / "pubrepo" / "push-plan.json").read_text())


def test_preflight_plan_full_release(source_repo, public_remote, run, tmp_path, git_calls):
    """push_targets::preflight_plan_full_release (AC122): a full release with
    tag_remote and source_tag, all local and listed. The plan file lists
    exactly the expected entries; every push goes to .publish/'s origin (P)
    or to a passing source remote (S); --verbose prints the plan."""
    remote = public_remote()
    src, backup = _bare(tmp_path, "src"), _bare(tmp_path, "backup")
    repo = _release_repo(source_repo, remote, tag_remote="backup",
                         private_remotes=[str(src), str(backup)])
    git_run("remote", "add", "src", str(src), cwd=repo)
    git_run("remote", "add", "backup", str(backup), cwd=repo)
    git_run("push", "-q", "src", "main", cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    git_calls.clear()
    r = run(["--release", "1.0.0", "--yes", "--verbose"], cwd=repo)
    assert r.code == 0, r.stderr
    plan = _plan_file(repo)
    assert plan["kind"] == "release"
    assert plan["entries"] == [
        {"ref": "refs/heads/main", "remote": "origin", "cls": "P", "refusal": None},
        {"ref": "refs/tags/v1.0.0", "remote": "origin", "cls": "P", "refusal": None},
        {"ref": "refs/tags/published/*", "remote": "backup", "cls": "S", "refusal": None},
        {"ref": "refs/tags/v1.0.0", "remote": "src", "cls": "S", "refusal": None}]
    assert "push plan (release): P refs/heads/main → origin" in r.stderr
    assert "push plan (release): S refs/tags/v1.0.0 → src" in r.stderr
    pushes = [(push_argv(args), cwd) for args, cwd in git_calls if push_argv(args)]
    assert pushes
    for args, cwd in pushes:
        if cwd and Path(cwd).name == ".publish":
            assert "origin" in args, args
        else:
            assert "--recurse-submodules=no" in args and ("src" in args or "backup" in args), args
    assert git_run("ls-remote", str(src), "refs/tags/v1.0.0").strip()
    assert len(git_run("ls-remote", str(backup), "refs/tags/published/*").splitlines()) == 1


def test_preflight_plan_publish(source_repo, public_remote, run, tmp_path):
    """push_targets::preflight_plan_full_release, the publish kind: the
    branch (P) and the bookmark to tag_remote (S)."""
    backup = _bare(tmp_path, "backup")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": "backup",
                                         "private_remotes": [str(backup)]}, origin=False)
    git_run("remote", "add", "backup", str(backup), cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    assert _plan_file(repo) == {"kind": "publish", "entries": [
        {"ref": "refs/heads/main", "remote": "origin", "cls": "P", "refusal": None},
        {"ref": "refs/tags/published/*", "remote": "backup", "cls": "S", "refusal": None}]}


def test_preflight_plan_lists_records(source_repo, public_remote, run, tmp_path):
    """push_targets::preflight_plan_full_release, records: a listed records
    remote appears as an (S) refs/pubrepo/* entry."""
    rec = _bare(tmp_path, "rec")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "records_remote": "rec",
                                         "private_remotes": [str(rec)]}, origin=False)
    git_run("remote", "add", "rec", str(rec), cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    assert {"ref": "refs/pubrepo/*", "remote": "rec", "cls": "S", "refusal": None} in \
        _plan_file(repo)["entries"]


def test_push_plan_public_ref_to_another_remote_is_internal_error(target):
    """§18.4: a (P) ref planned for anything but .publish/'s origin is an
    internal error (exit 1) before any write."""
    t = target()
    with pytest.raises(pubrepo.PubrepoError, match="internal: public ref refs/heads/main "
                                                   "planned for backup") as e:
        pubrepo.build_push_plan(t.repo, t.repo / ".publish", t.config, kind="publish",
                                refs={"public": [("refs/heads/main", "backup")]})
    assert e.value.exit_code == 1


def test_s_pushes_no_recursion_source_tag(source_repo, public_remote, run, tmp_path,
                                          source_pushes):
    """push_targets::s_pushes_no_recursion, the source_tag half: under
    push.recurseSubmodules=on-demand every source push of a release carries
    --recurse-submodules=no."""
    src = _bare(tmp_path, "src")
    repo = _release_repo(source_repo, public_remote(), private_remotes=[str(src)])
    git_run("remote", "add", "src", str(src), cwd=repo)
    git_run("push", "-q", "src", "main", cwd=repo)
    git_run("config", "push.recurseSubmodules", "on-demand", cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert source_pushes and all("--recurse-submodules=no" in c for c in source_pushes)
    assert git_run("ls-remote", str(src), "refs/tags/v1.0.0").strip()



# --- the publish clone's own URLs (R5) and history (§18.13) -----------------

def _published(source_repo, run, tmp_path, files=None, **cfg):
    """A source repo published once to a fresh bare; (repo, bare)."""
    public = _bare(tmp_path, "pub")
    repo = source_repo(files or {"a.py": "x\n"},
                       {"remote": public.as_uri(), "include": ["a.py"], **cfg}, origin=False)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    return repo, public


def _no_publish_clone_network(git_calls):
    return not [a for a, cwd in git_calls if cwd and Path(cwd).name == ".publish"
                and (a[:1] == ["fetch"] or push_argv(a))]


@pytest.mark.parametrize("shape", ["file_url_spelling", "trailing_slash", "pushurl_elsewhere",
                                   "push_insteadof", "another_bare"])
def test_publish_clone_url_mismatch_is_2(source_repo, run, tmp_path, git_calls, shape):
    """push_targets::publish_clone_url_mismatch_is_2 (AC126, AC129 R5 rows):
    every value of .publish/'s origin must be byte-equal to [publish]
    remote; otherwise 2 with the set-url fix, before any fetch or push."""
    public = _bare(tmp_path, "pub")
    other = _bare(tmp_path, "other")
    repo = source_repo({"a.py": "x\n"}, {"remote": str(public), "include": ["a.py"]},
                       origin=False)
    assert run(["init"], cwd=repo).code == 0
    pd = repo / ".publish"
    if shape == "file_url_spelling":            # the same repository, written differently
        git_run("remote", "set-url", "origin", public.as_uri(), cwd=pd)
    elif shape == "trailing_slash":
        git_run("remote", "set-url", "origin", str(public) + "/", cwd=pd)
    elif shape == "pushurl_elsewhere":
        git_run("remote", "set-url", "--push", "origin", str(other), cwd=pd)
    elif shape == "push_insteadof":
        git_run("config", f"url.{other}.pushInsteadOf", str(public), cwd=pd)
    else:
        git_run("remote", "set-url", "origin", str(other), cwd=pd)
    git_calls.clear()
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert "publish clone's origin does not match [publish] remote" in r.stderr
    assert f"remote set-url origin {public}" in r.stderr
    assert _no_publish_clone_network(git_calls)
    assert git_run("ls-remote", str(other)).strip() == ""


def test_publish_clone_exact_ipv6_value_passes_r5(source_repo, sandbox_unreachable, tmp_path):
    """publish_clone_url_mismatch_is_2: an exact IPv6 value as both
    [publish] remote and origin passes R5 (function level; the sandbox
    allows no ssh transport)."""
    url = sandbox_unreachable("ssh://git@[2001:db8::7]/o/r")
    repo = source_repo({"a.py": "x\n"}, {"remote": url, "include": ["a.py"]}, origin=False)
    pd = repo / ".publish"
    git_run("init", "-q", str(pd))
    _add_remote(pd, "origin", url)
    pubrepo.check_publish_clone_origin(pd, {"remote": url})


def test_publish_clone_disabled_pushurl_keeps_the_lock_message(source_repo, run, tmp_path):
    """A DISABLED- pushurl gives the lock message, never the R5 one."""
    repo, public = _published(source_repo, run, tmp_path)
    git_run("remote", "set-url", "--push", "origin", "DISABLED-locked", cwd=repo / ".publish")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2 and "publishing is locked for this clone" in r.stderr, r.stderr
    assert "does not match" not in r.stderr


def test_r5_mismatch_message_says_recreate_with_init(source_repo, run, tmp_path):
    """§18.13 R5 message: the re-create-with-init line."""
    repo, public = _published(source_repo, run, tmp_path)
    git_run("remote", "set-url", "origin", str(_bare(tmp_path, "else")), cwd=repo / ".publish")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2
    assert ("If .publish/ was cloned from a different repository, re-create it with "
            "`pubrepo init`") in r.stderr


def test_r5_fetch_rewrite_needs_equal_sets(source_repo, run, tmp_path):
    """push_targets::r5_fetch_rewrite_needs_equal_sets: an
    insteadOf fetch rewrite to the private origin with pushInsteadOf back to
    the public repo → 2, nothing on the public bare; with equal sets (both
    rewritten to one mirror of the public repo) → publishes."""
    public, private = _bare(tmp_path, "pub"), _bare(tmp_path, "private")
    repo = source_repo({"a.py": "x\n"}, {"remote": public.as_uri(), "include": ["a.py"]},
                       origin=False)
    assert run(["init"], cwd=repo).code == 0
    pd = repo / ".publish"
    config = (pd / ".git" / "config").read_text()
    _add_config(pd, f'[url "{private.as_uri()}"]', f"insteadOf = {public.as_uri()}")
    _add_config(pd, f'[url "{public.as_uri()}"]', f"pushInsteadOf = {public.as_uri()}")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2 and "does not match [publish] remote" in r.stderr, r.stderr
    assert git_run("ls-remote", str(public)).strip() == ""
    (pd / ".git" / "config").write_text(config)
    mirror = public.with_name("pub-mirror.git")
    _add_config(pd, f'[url "{mirror.as_uri()}"]', f"insteadOf = {public.as_uri()}")
    git_run("clone", "-q", "--bare", str(public), str(mirror))
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert git_run("ls-tree", "-r", "--name-only", "main", cwd=mirror).split() == ["a.py"]


def test_relative_local_publish_remote(source_repo, run, tmp_path):
    """push_targets::relative_local_publish_remote: `remote =
    "../relpub.git"`: init clones from the source root, R5 accepts the
    stored join, publish → 0; a mismatch prints the absolute fix; a helper
    value is never absolutized."""
    repo = source_repo({"a.py": "x\n"}, {"remote": "../relpub.git", "include": ["a.py"]},
                       origin=False)
    rel = repo.parent / "relpub.git"
    git_run("init", "--bare", "-q", "-b", "main", str(rel))
    assert run(["init"], cwd=repo).code == 0
    assert git_run("remote", "get-url", "origin", cwd=repo / ".publish").strip() == \
        os.path.join(str(repo), "../relpub.git")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert git_run("ls-tree", "-r", "--name-only", "main", cwd=rel).split() == ["a.py"]
    git_run("remote", "set-url", "origin", str(_bare(tmp_path, "elsewhere")), cwd=repo / ".publish")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2
    assert f"remote set-url origin {os.path.join(str(repo), '../relpub.git')}" in r.stderr
    assert not pubrepo._is_relative_local_remote("https::../x")


def test_pt5_01_relative_remote_string_as_publish_origin_is_2(source_repo, run, tmp_path):
    """push_targets::pt5_01_relative_remote_string_as_publish_origin_is_2
    origin set to the relative string names <source>/pt501pub.git
    from .publish/, a private repo → 2 with the absolute fix; neither main
    moves."""
    repo = source_repo({"a.py": "x\n"}, {"remote": "../pt501pub.git", "include": ["a.py"]},
                       origin=False)
    public = repo.parent / "pt501pub.git"
    git_run("init", "--bare", "-q", "-b", "main", str(public))
    private = repo / "pt501pub.git"
    git_run("init", "--bare", "-q", "-b", "main", str(private))
    git_run("push", "-q", str(private), "main", cwd=repo)
    before = git_run("rev-parse", "main", cwd=private)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    published = git_run("rev-parse", "main", cwd=public)
    git_run("remote", "set-url", "origin", "../pt501pub.git", cwd=repo / ".publish")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert "does not match [publish] remote: ../pt501pub.git" in r.stderr
    assert f"remote set-url origin {os.path.join(str(repo), '../pt501pub.git')}" in r.stderr
    assert git_run("rev-parse", "main", cwd=private) == before
    assert git_run("rev-parse", "main", cwd=public) == published


# §18.13: insteadOf rewrites to a source remote ----------------------------------------

def test_pt3_01_rewrite_to_source_origin_is_2(source_repo, run, tmp_path):
    """push_targets::pt3_01_rewrite_to_source_origin_is_2: .publish/'s
    fetch and push both rewritten (insteadOf) to the source's origin → 2;
    the source origin's main is unchanged."""
    repo = source_repo({"a.py": "x\n", "secret.txt": "private\n"},
                       {"remote": str(tmp_path / "pt301-pub.git"), "include": ["a.py"]})
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    before = git_run("rev-parse", "main", cwd=origin)
    remote = str(tmp_path / "pt301-pub.git")
    _bare(tmp_path, "pt301-pub")
    assert run(["init"], cwd=repo).code == 0
    git_run("config", f"url.{origin}.insteadOf", remote, cwd=repo / ".publish")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert "which is a value of the source repository's remote 'origin'" in r.stderr
    assert git_run("rev-parse", "main", cwd=origin) == before


def test_pt4_01_path_and_file_url_spellings_of_a_source_remote_are_2(source_repo, run,
                                                                     tmp_path):
    """push_targets::pt4_01_path_and_file_url_spellings_of_a_source_remote_are_2:
    the source origin written as a path, the rewrite its file:// spelling:
    not byte-equal, same identity → 2; origin's main unchanged."""
    remote = str(tmp_path / "pt401-pub.git")
    _bare(tmp_path, "pt401-pub")
    repo = source_repo({"a.py": "x\n", "secret.txt": "private\n"},
                       {"remote": remote, "include": ["a.py"]})
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    before = git_run("rev-parse", "main", cwd=origin)
    assert run(["init"], cwd=repo).code == 0
    _add_config(repo / ".publish", f'[url "{Path(origin).as_uri()}"]', f"insteadOf = {remote}")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert "which is a value of the source repository's remote 'origin'" in r.stderr
    assert git_run("rev-parse", "main", cwd=origin) == before


def test_pt4_01_rewrite_to_a_different_bare_is_2(source_repo, run, tmp_path):
    """push_targets::pt4_01_rewrite_to_a_different_bare_is_2:
    the source origin written as the other bare's file:// URL, the rewrite
    its path; exemption (i) does not hold → 2; nothing moves."""
    public, other = _bare(tmp_path, "pt401-public"), _bare(tmp_path, "pt401-other")
    repo = source_repo({"a.py": "x\n", "secret.txt": "private\n"},
                       {"remote": public.as_uri(), "include": ["a.py"]}, origin=False)
    git_run("remote", "add", "origin", other.as_uri(), cwd=repo)
    git_run("push", "-q", "origin", "main", cwd=repo)
    before = git_run("rev-parse", "main", cwd=other)
    assert run(["init"], cwd=repo).code == 0
    git_run("config", f"url.{other}.insteadOf", public.as_uri(), cwd=repo / ".publish")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert git_run("rev-parse", "main", cwd=other) == before
    assert git_run("ls-remote", str(public)).strip() == ""


def test_pt4_01_file_only_stand_in_for_exemption_i_publishes(source_repo, run, tmp_path):
    """push_targets::pt4_01_file_only_stand_in_for_exemption_i_publishes:
    url.<bare path>.insteadOf <bare file:// URL>, the source origin written
    as that path: the rewrite IS the publish repository → publishes, with no
    private ref or source commit on the bare."""
    public = _bare(tmp_path, "standin-public")
    repo = source_repo({"src/app.py": "print('public')\n", "notes/private.md": "private\n"},
                       {"remote": public.as_uri(), "include": ["src"]}, origin=False)
    git_run("remote", "add", "origin", str(public), cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    git_run("config", f"url.{public}.insteadOf", public.as_uri(), cwd=repo / ".publish")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    source_commit = git_run("rev-parse", "HEAD", cwd=repo).strip()
    assert git_run("ls-remote", str(public), "refs/tags/published/*",
                   "refs/pubrepo/bookmarks/*").strip() == ""
    assert pubrepo.git("cat-file", "-e", source_commit, cwd=str(public), check=False).returncode
    assert git_run("ls-tree", "-r", "--name-only", "main", cwd=public).split() == ["src/app.py"]


def test_a17_origin_is_public_repo_with_global_insteadof_exemption_i(source_repo,
                                                                    sandbox_unreachable):
    """push_targets::a17_origin_is_public_repo_with_global_insteadof_publishes,
    at the private-target level: a source whose origin is the public repo, under
    `url.git@github.com:.insteadOf https://github.com/`. The rewrite equals
    origin's written value, but exemption (i) holds; a rewrite to another
    repository on the same host is still owned. The 1.1.0 sandbox allows no
    ssh or https transport (the git shim also refuses `ls-remote --get-url
    <https URL>`), so the end-to-end half is the file-only stand-in above
    (1.0.2 ran it through a GIT_SSH_COMMAND stub)."""
    url = sandbox_unreachable("https://github.com/owner/project.git")
    scp = sandbox_unreachable("git@github.com:owner/project.git")
    repo = source_repo({"src/app.py": "x\n"}, {"remote": url, "include": ["src"]}, origin=False)
    _add_remote(repo, "origin", scp)
    assert pubrepo._rewrite_owner(repo, url, scp) is None                   # (i)
    other = sandbox_unreachable("git@github.com:owner/private.git")
    _add_remote(repo, "backup", other)
    assert pubrepo._rewrite_owner(repo, url, other) == "backup"


def test_pt4_02_written_origin_rewritten_to_a_private_repo_is_2(source_repo, run, tmp_path):
    """push_targets::pt4_02_written_origin_rewritten_to_a_private_repo_is_2:
    the source origin written as [publish] remote, both rewritten to a
    private P holding the source main → 2; P's main unchanged."""
    remote = str(tmp_path / "pt402-pub.git")
    _bare(tmp_path, "pt402-pub")
    private = _bare(tmp_path, "pt402-private")
    repo = source_repo({"a.py": "x\n", "secret.txt": "private\n"},
                       {"remote": remote, "include": ["a.py"]}, origin=False)
    git_run("remote", "add", "origin", remote, cwd=repo)
    for where in (repo,):
        git_run("config", f"url.{private}.insteadOf", remote, cwd=where)
    git_run("push", "-q", "origin", "main", cwd=repo)
    before = git_run("rev-parse", "main", cwd=private)
    assert run(["init"], cwd=repo).code == 0
    git_run("config", f"url.{private}.insteadOf", remote, cwd=repo / ".publish")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert git_run("rev-parse", "main", cwd=private) == before


# Replace objects and transport keys ------------------------------------------------------

def _wrong_clone(source_repo, run, tmp_path, *, seed=False):
    """The R5b shape: [publish] remote named the private source
    origin at init (so .publish/ holds the private history), then the
    config and origin were corrected to the public repo. (repo, public,
    private origin path, private tip)."""
    public = _bare(tmp_path, "r5b-public")
    repo = source_repo({"a.py": "x\n", "secret.txt": "private\n"},
                       {"remote": "placeholder", "include": ["a.py"]})
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    cfg = repo / ".publish.toml"
    cfg.write_text(cfg.read_text().replace('remote = "placeholder"', f'remote = "{origin}"'))
    git_run("commit", "-qam", "remote = private origin", cwd=repo)
    git_run("push", "-q", "origin", "main", cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    cfg.write_text(cfg.read_text().replace(f'remote = "{origin}"',
                                           f'remote = "{public.as_uri()}"'))
    git_run("commit", "-qam", "remote = public", cwd=repo)
    git_run("remote", "set-url", "origin", public.as_uri(), cwd=repo / ".publish")
    if seed:
        work = tmp_path / "seed"
        git_run("clone", "-q", str(public), str(work))
        (work / "README").write_text("public\n")
        git_run("add", "README", cwd=work)
        git_run("commit", "-qm", "public readme", cwd=work)
        git_run("push", "-q", "origin", "HEAD:main", cwd=work)
    return repo, public, origin, git_run("rev-parse", "main", cwd=origin).strip()


def _private_shas(origin) -> list[str]:
    return git_run("rev-list", "main", cwd=origin).split()


def _none_on(public, shas) -> bool:
    return all(pubrepo.git("cat-file", "-e", sha, cwd=str(public), check=False).returncode
               for sha in shas)


@pytest.mark.rewrite
@pytest.mark.parametrize("args", [[], ["--amend"]], ids=["publish", "amend"])
def test_pt4_03_replace_ref_r5b_is_2(source_repo, run, tmp_path, args):
    """push_targets::pt4_03_replace_ref_r5b_is_2: the R5b shape with the
    public branch at T0 and, in .publish, a replace ref T0 → X (T0's tree,
    the private tip as parent) → 2 naming the ref; the public bare keeps
    only T0 and no private commit."""
    repo, public, origin, _tip = _wrong_clone(source_repo, run, tmp_path, seed=True)
    pd = repo / ".publish"
    git_run("fetch", "-q", "origin", "main", cwd=pd)
    t0 = git_run("rev-parse", "FETCH_HEAD", cwd=pd).strip()
    x = git_run("commit-tree", f"{t0}^{{tree}}", "-p", "HEAD", "-m", "x", cwd=pd).strip()
    git_run("replace", t0, x, cwd=pd)
    r = run(["publish", "--yes", *args], cwd=repo)
    assert r.code == 2, r.stderr
    assert f"refs/replace/{t0}" in r.stderr
    assert git_run("rev-list", "--count", "main", cwd=public).strip() == "1"
    assert _none_on(public, _private_shas(origin))


def test_pt4_03_grafts_file_is_2(source_repo, run, tmp_path):
    """push_targets::pt4_03_grafts_file_is_2."""
    repo, public = _published(source_repo, run, tmp_path)
    grafts = repo / ".publish" / ".git" / "info" / "grafts"
    grafts.parent.mkdir(exist_ok=True)
    grafts.write_text("")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2 and "info/grafts" in r.stderr, r.stderr


@pytest.mark.rewrite
def test_pt4_03_every_git_call_in_the_publish_clone_ignores_replace_objects(
        source_repo, run, tmp_path, monkeypatch):
    """push_targets::pt4_03_every_git_call_in_the_publish_clone_ignores_replace_objects:
    every git process pubrepo starts in .publish/ has GIT_NO_REPLACE_OBJECTS=1
    (publish and --amend); source-repo calls do not."""
    public = _bare(tmp_path, "pub")
    repo = source_repo({"a.py": "1\n"}, {"remote": public.as_uri(), "include": ["a.py"]},
                       origin=False)
    calls = []
    real = pubrepo.subprocess.run

    def spy(argv, *a, **kw):
        if isinstance(argv, list) and argv and argv[0] == "git":
            calls.append((kw.get("cwd"), (kw.get("env") or {}).get("GIT_NO_REPLACE_OBJECTS"),
                          argv[1:3]))
        return real(argv, *a, **kw)

    monkeypatch.setattr(pubrepo.subprocess, "run", spy)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    (repo / "a.py").write_text("2\n")
    git_run("commit", "-qam", "two", cwd=repo)
    assert run(["publish", "--yes", "--amend"], cwd=repo).code == 0
    in_clone = [(flag, argv) for cwd, flag, argv in calls
                if cwd and ".publish" in Path(cwd).parts]
    in_source = [flag for cwd, flag, _argv in calls if cwd and ".publish" not in Path(cwd).parts]
    assert len(in_clone) > 10
    assert [argv for flag, argv in in_clone if flag != "1"] == []
    assert in_source and set(in_source) == {None}


@pytest.mark.parametrize("key", ["receivepack", "uploadpack", "vcs", "mirror"])
def test_h102_11_transport_keys_on_publish_origin_are_2(source_repo, run, tmp_path, key):
    """push_targets::h102_11_*: receivepack, uploadpack, vcs (and
    mirror, the transport-altering keys) on .publish/'s origin → 2 naming the key."""
    repo, public = _published(source_repo, run, tmp_path)
    value = {"receivepack": f"git-receive-pack '{_bare(tmp_path, 'rp')}' #",
             "uploadpack": f"git-upload-pack '{_bare(tmp_path, 'up')}' #",
             "vcs": "file", "mirror": "true"}[key]
    git_run("config", f"remote.origin.{key}", value, cwd=repo / ".publish")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2, r.stderr
    assert f"remote.origin.{key} is set" in r.stderr


def test_h102_11_uploadpack_on_publish_origin_r5b_leak_shape_is_2(source_repo, run, tmp_path):
    """push_targets::h102_11_uploadpack_on_publish_origin_r5b_leak_shape_is_2:
    the R5b shape plus uploadpack aimed at the private origin → 2; no
    private commit on the public bare."""
    repo, public, origin, _tip = _wrong_clone(source_repo, run, tmp_path)
    git_run("config", "remote.origin.uploadpack", f"git-upload-pack '{origin}' #",
            cwd=repo / ".publish")
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2 and "remote.origin.uploadpack is set" in r.stderr, r.stderr
    assert git_run("ls-remote", str(public)).strip() == ""


def test_h102_11_receivepack_redirect_on_publish_origin_is_2(source_repo, run, tmp_path):
    """push_targets::h102_11_receivepack_redirect_on_publish_origin_is_2:
    nothing reaches the other bare."""
    repo, public = _published(source_repo, run, tmp_path)
    other = _bare(tmp_path, "rp-other")
    git_run("config", "remote.origin.receivepack", f"git-receive-pack '{other}' #",
            cwd=repo / ".publish")
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2 and "remote.origin.receivepack is set" in r.stderr, r.stderr
    assert git_run("ls-remote", str(other)).strip() == ""


# Provenance, not the literal U check ---------------------------------------------

def _recreate(run, repo, public):
    """1.1.0 on the R5b shape: the clone's refs/remotes/origin/main (from
    the private clone) is S11 evidence, so publish → 5 with nothing pushed;
    `--recreate-branch` then publishes a root snapshot."""
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 5 and "--recreate-branch" in r.stderr, r.stderr
    assert git_run("ls-remote", str(public)).strip() == ""
    return run(["publish", "--yes", "--recreate-branch"], cwd=repo)


def test_r5b_wrong_remote_then_set_url(source_repo, run, tmp_path):
    """push_targets::r5b_wrong_remote_then_set_url: .publish cloned
    from the private origin, corrected by set-url. Publish → 5 (S11: the
    private clone's tracking ref is evidence), then --recreate-branch → 0
    with zero private commits on the public bare; the private history is
    backed up to refs/pubrepo/local/<ts> with the §4.10 notice."""
    repo, public, origin, tip = _wrong_clone(source_repo, run, tmp_path)
    r = _recreate(run, repo, public)
    assert r.code == 0, r.stderr
    assert _none_on(public, _private_shas(origin))
    backups = git_run("for-each-ref", "--format=%(refname) %(objectname)",
                      "refs/pubrepo/local/", cwd=repo / ".publish").split("\n")
    assert any(line.endswith(tip) for line in backups)
    assert "were never published; saved to refs/pubrepo/local/" in r.stderr


@pytest.mark.parametrize("refspec", ["+refs/heads/*:refs/remotes/upstream/*",
                                     "+refs/heads/other:refs/remotes/origin/other"])
def test_r5b_nondefault_fetch_refspec(source_repo, run, tmp_path, git_calls, refspec):
    """push_targets::r5b_nondefault_fetch_refspec: with a non-default
    remote.origin.fetch, T still comes from this run's FETCH_HEAD; no
    private commit reaches the public bare; the push names the snapshot sha
    explicitly."""
    repo, public, origin, _tip = _wrong_clone(source_repo, run, tmp_path)
    git_run("config", "remote.origin.fetch", refspec, cwd=repo / ".publish")
    git_calls.clear()
    r = _recreate(run, repo, public)
    assert r.code == 0, r.stderr
    assert _none_on(public, _private_shas(origin))
    pushes = [push_argv(a) for a, cwd in git_calls if push_argv(a)]
    sha = git_run("rev-parse", "main", cwd=public).strip()
    assert any(f"{sha}:refs/heads/main" in p for p in pushes)


def test_r5a_rewrite_removed_later(source_repo, run, tmp_path, monkeypatch):
    """push_targets::r5a_rewrite_removed_later: init cloned the private
    history under an insteadOf rewrite that was later removed → 0, no
    private commit on the public bare, backup made."""
    public, private = _bare(tmp_path, "r5a-public"), _bare(tmp_path, "r5a-private")
    repo = source_repo({"a.py": "x\n", "secret.txt": "private\n"},
                       {"remote": public.as_uri(), "include": ["a.py"]}, origin=False)
    git_run("remote", "add", "origin", private.as_uri(), cwd=repo)
    git_run("push", "-q", "origin", "main", cwd=repo)
    # a private global config for this test (the session one is shared)
    original = Path(os.environ["GIT_CONFIG_GLOBAL"])
    glob = tmp_path / "r5a.gitconfig"
    glob.write_text(Path(os.environ["GIT_CONFIG_GLOBAL"]).read_text()
                    + f'[url "{private.as_uri()}"]\n\tinsteadOf = {public.as_uri()}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(glob))
    assert run(["init"], cwd=repo).code == 0
    assert git_run("rev-parse", "main", cwd=repo / ".publish").strip() == \
        git_run("rev-parse", "main", cwd=private).strip()           # the rewrite took effect
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(original))
    r = _recreate(run, repo, public)
    assert r.code == 0, r.stderr
    assert _none_on(public, git_run("rev-list", "main", cwd=private).split())
    assert git_run("for-each-ref", "refs/pubrepo/local/", cwd=repo / ".publish").strip()


def test_stale_private_tracking_ref_not_a_parent(source_repo, run, tmp_path):
    """push_targets::stale_private_tracking_ref_not_a_parent: refs/remotes/
    origin/main names the private tip and the public branch is absent → the
    private sha is never a parent and never on the public bare."""
    repo, public, origin, tip = _wrong_clone(source_repo, run, tmp_path)
    pd = repo / ".publish"
    assert git_run("rev-parse", "refs/remotes/origin/main", cwd=pd).strip() == tip
    r = _recreate(run, repo, public)
    assert r.code == 0, r.stderr
    assert git_run("rev-list", "--parents", "-n1", "main", cwd=public).split()[1:] == []
    assert _none_on(public, _private_shas(origin))


@pytest.mark.rewrite
def test_local_backup_never_pushed(source_repo, run, tmp_path, git_calls):
    """push_targets::local_backup_never_pushed: after an AC18 backup, no
    push argv names refs/pubrepo/local/* and the public bare has none."""
    repo, public, origin, _tip = _wrong_clone(source_repo, run, tmp_path)
    git_calls.clear()
    assert _recreate(run, repo, public).code == 0
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    assert run(["publish", "--yes", "--amend"], cwd=repo).code == 0
    assert git_run("for-each-ref", "refs/pubrepo/local/", cwd=repo / ".publish").strip()
    assert not [a for a, _cwd in git_calls
                if push_argv(a) and any("refs/pubrepo/local" in x for x in a)]
    assert git_run("ls-remote", str(public), "refs/pubrepo/*").strip() == ""


def test_provenance_assertion_refuses_foreign_parent(source_repo, run, tmp_path, monkeypatch):
    """push_targets::provenance_assertion_refuses_foreign_parent: a parent
    outside {tip, fetched, created, ancestors of tip} → 2 with the provenance
    wording; at the publish call site, nothing is committed or pushed."""
    repo, public = _published(source_repo, run, tmp_path)
    pd = repo / ".publish"
    tip = git_run("rev-parse", "main", cwd=public).strip()
    foreign = git_run("commit-tree", f"{tip}^{{tree}}", "-m", "foreign", cwd=pd).strip()
    pubrepo.assert_parent_provenance(pd, [tip], tip=tip, fetched=set(), created=set())
    pubrepo.assert_parent_provenance(pd, [foreign], tip=tip, fetched={foreign}, created=set())
    with pytest.raises(pubrepo.PublishError, match="did not come from \\[publish\\] remote"):
        pubrepo.assert_parent_provenance(pd, [foreign], tip=tip, fetched=set(), created=set())
    real = pubrepo.assert_parent_provenance
    monkeypatch.setattr(pubrepo, "assert_parent_provenance",
                        lambda d, parents, **kw: real(d, [*parents, foreign], **kw))
    (repo / "a.py").write_text("y\n")
    git_run("commit", "-qam", "y", cwd=repo)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 2 and "may have been cloned from another repository" in r.stderr
    assert git_run("rev-parse", "main", cwd=public).strip() == tip


@pytest.mark.rewrite
def test_normal_paths_still_publish(source_repo, run, tmp_path, monkeypatch):
    """push_targets::normal_paths_still_publish (also
    a16_retry_after_interrupted_push_publishes): repeated publish, --amend,
    and a retry after an interrupted push all publish."""
    repo, public = _published(source_repo, run, tmp_path)
    for n, args in ((2, []), (3, ["--amend"])):
        (repo / "a.py").write_text(f"{n}\n")
        git_run("commit", "-qam", str(n), cwd=repo)
        r = run(["publish", "--yes", *args], cwd=repo)
        assert r.code == 0, r.stderr
    real = pubrepo.push_porcelain

    def interrupted(*a, **kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(pubrepo, "push_porcelain", interrupted)
    (repo / "a.py").write_text("4\n")
    git_run("commit", "-qam", "4", cwd=repo)
    assert run(["publish", "--yes"], cwd=repo).code == 130
    monkeypatch.setattr(pubrepo, "push_porcelain", real)
    r = run(["publish", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert git_run("show", "main:a.py", cwd=public) == "4\n"



# --- validate (§18.5, R7) ----------------------------------------------

def test_validate_offline_reports_local(source_repo, public_remote, run, git_calls,
                                        sandbox_unreachable):
    """push_targets::validate_offline_reports_local: a local
    published/y tag and a github-hosted tag_remote → plain `validate` exits
    0, lists the tag with `git tag -d` and the tag_remote refusal naming the
    value to add, with zero network git calls."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": "gh"}, origin=False)
    _add_remote(repo, "gh", sandbox_unreachable("https://github.com/owner/source.git"))
    git_run("tag", "published/20260101T000000", cwd=repo)
    git_calls.clear()
    r = run(["validate"], cwd=repo)
    out = r.stdout + r.stderr
    assert r.code == 0, out
    assert "git tag -d published/20260101T000000" in out
    assert ("private refs will not be pushed to 'gh': 'https://github.com/owner/source.git' "
            "is not listed in [publish] private_remotes (it is on a public host)") in out
    assert not [a for a, _cwd in git_calls if (a[:1] == ["ls-remote"] and "--get-url" not in a)
                or a[:1] == ["fetch"] or push_argv(a)]               # local reads only


def test_validate_reports_leaks(source_repo, run, tmp_path):
    """push_targets::validate_reports_leaks (AC123): the publish remote holds
    published/x tags and a refs/pubrepo/snap/* ref → `validate --remote`
    exits 0 and lists them with a cleanup command; an unreachable source
    remote is a warning; the remote is unchanged."""
    repo, public = _published(source_repo, run, tmp_path)
    sha = git_run("rev-parse", "main", cwd=public).strip()
    git_run("update-ref", "refs/tags/published/20260101T000000", sha, cwd=public)
    git_run("update-ref", f"refs/pubrepo/snap/{sha}", sha, cwd=public)
    git_run("remote", "add", "gone", str(tmp_path / "gone.git"), cwd=repo)
    before = git_run("for-each-ref", cwd=public)
    r = run(["validate", "--remote"], cwd=repo)
    out = r.stdout + r.stderr
    assert r.code == 0, out
    assert f"refs/pubrepo/snap/{sha}" in out and "refs/tags/published/20260101T000000" in out
    assert ("to remove them: git -C .publish push origin --delete "
            f"refs/pubrepo/snap/{sha} refs/tags/published/20260101T000000") in out
    assert "could not list the refs on gone" in out
    assert git_run("for-each-ref", cwd=public) == before
    plain = run(["validate"], cwd=repo)
    assert "refs/pubrepo/snap" not in plain.stdout + plain.stderr     # offline by default


def test_validate_unlisted_wording(source_repo, public_remote, run, tmp_path):
    """push_targets::validate_unlisted_wording: refs on an unlisted
    source remote are reported with "if this remote is public" wording and
    a conditional delete."""
    backup = _bare(tmp_path, "bk")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]},
                       origin=False)
    git_run("remote", "add", "bk", str(backup), cwd=repo)
    git_run("tag", "published/20260617T101500", cwd=repo)
    git_run("push", "-q", "bk", "refs/tags/published/20260617T101500", cwd=repo)
    git_run("tag", "-d", "published/20260617T101500", cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    r = run(["validate", "--remote"], cwd=repo)
    out = r.stdout + r.stderr
    assert r.code == 0, out
    assert ("'bk' is not listed in private_remotes; if this remote is public, these refs "
            "expose private history") in out
    assert "If it is public: git push bk --delete refs/tags/published/20260617T101500" in out


def test_validate_remote_skips_listed_private_remote(source_repo, public_remote, run,
                                                     tmp_path):
    """R7: a listed private remote holding the pushed bookmark tags is not
    reported (those refs belong there)."""
    backup = _bare(tmp_path, "bk")
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         "tag_remote": "bk", "private_remotes": [str(backup)]},
                       origin=False)
    git_run("remote", "add", "bk", str(backup), cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    assert run(["publish", "--yes"], cwd=repo).code == 0
    assert git_run("ls-remote", str(backup), "refs/tags/published/*").strip()
    r = run(["validate", "--remote"], cwd=repo)
    assert r.code == 0 and "'bk'" not in r.stdout + r.stderr



def test_validate_warns_for_packed_published_tag_in_a_linked_worktree(source_repo,
                                                                     public_remote, run,
                                                                     tmp_path):
    """_may_hold_published_tags finds the common git dir without
    git when the source is a linked worktree (.git file → gitdir →
    commondir). The published/* tag exists only in the common dir's
    packed-refs, and plain validate in the worktree still warns."""
    main = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]},
                       origin=False)
    worktree = tmp_path / "wt"
    git_run("worktree", "add", "-q", "-b", "side", str(worktree), cwd=main)
    assert pubrepo._git_config_path(worktree) == (main / ".git" / "config").resolve()
    assert not pubrepo._may_hold_published_tags(worktree)      # found, not a fallback
    git_run("tag", "published/20260101T000000", cwd=main)
    git_run("pack-refs", "--all", cwd=main)
    common = main / ".git"
    assert not (common / "refs" / "tags" / "published").exists()
    assert b" refs/tags/published/20260101T000000" in (common / "packed-refs").read_bytes()
    assert (worktree / ".git").is_file()
    assert pubrepo._may_hold_published_tags(worktree)
    r = run(["validate"], cwd=worktree)
    assert r.code == 0, r.stderr
    assert "git tag -d published/20260101T000000" in r.stdout + r.stderr



# --- init offers the private_remotes line (§18.11) -----------------------------

def _offer_repo(source_repo, public_remote, **cfg):
    """A source repo whose upstream `origin` (a local bare, the R6 records
    remote) is not in private_remotes."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"],
                                         **cfg})
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    return repo, origin


@pytest.mark.pty
@pytest.mark.parametrize("answer", ["", "y"], ids=["enter", "yes"])
def test_init_tty_offers_private_remotes_default_no(source_repo, public_remote, run, tmp_path,
                                                    answer):
    """push_targets::init_tty_offers_private_remotes_default_no (pty): init
    names the records remote and its unlisted values and asks one [y/N].
    Enter → no edit, the line printed; `y` → the line inserted directly
    after [publish], and validate passes."""
    from test_ff_auth import _cli_on_a_pty
    repo, origin = _offer_repo(source_repo, public_remote)
    cfg = repo / ".publish.toml"
    before = cfg.read_bytes()
    code, text, _calls = _cli_on_a_pty(repo, ["init"], answer, tmp_path / "pty.log")
    assert code == 0, text
    line = f'private_remotes = ["{origin}"]'
    assert text.count("[y/N]") == 1, text
    assert f"Records remote 'origin' has these unlisted values:" in text
    assert ("Add these to [publish] private_remotes in .publish.toml? Only if these "
            "repositories are private. [y/N]") in text
    if answer == "":
        assert cfg.read_bytes() == before
        assert line in text
    else:
        assert cfg.read_text().startswith(f"[publish]\n{line}\n"), cfg.read_text()
        assert cfg.read_bytes().replace(f"{line}\n".encode(), b"", 1) == before
        config = pubrepo.load_publish_config(cfg)            # validates (strict)
        assert config["private_remotes"] == [origin]
        assert pubrepo.private_target_refusal(repo, config, "origin") is None


def test_init_offtty_prints_line_only(source_repo, public_remote, run):
    """push_targets::init_offtty_prints_line_only: off a terminal init prints
    the exact line, asks nothing, and the config is byte-unchanged."""
    repo, origin = _offer_repo(source_repo, public_remote)
    cfg = repo / ".publish.toml"
    before = cfg.read_bytes()
    r = run(["init"], cwd=repo)
    assert r.code == 0, r.stderr
    out = r.stdout + r.stderr
    assert f'private_remotes = ["{origin}"]' in out and "[y/N]" not in out
    assert cfg.read_bytes() == before


@pytest.mark.pty
def test_init_existing_private_remotes_prints_replacement(source_repo, public_remote, run,
                                                          tmp_path):
    """push_targets::init_existing_private_remotes_prints_replacement: with
    private_remotes already set, `y` does not edit the file; the full
    replacement line (existing entries kept, the new value appended) is
    printed."""
    from test_ff_auth import _cli_on_a_pty
    repo, origin = _offer_repo(source_repo, public_remote, private_remotes=["/srv/keep.git"])
    cfg = repo / ".publish.toml"
    before = cfg.read_bytes()
    code, text, _calls = _cli_on_a_pty(repo, ["init"], "y", tmp_path / "pty.log")
    assert code == 0, text
    assert cfg.read_bytes() == before
    assert f'private_remotes = ["/srv/keep.git", "{origin}"]' in text
    assert "already sets private_remotes; replace that line with:" in text


def test_init_no_offer_when_the_records_remote_is_listed(source_repo, public_remote, run):
    """No offer and no line when the records remote's values are listed."""
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]})
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    cfg = repo / ".publish.toml"
    cfg.write_text(cfg.read_text().replace(
        "[publish]\n", f'[publish]\nprivate_remotes = ["{origin}"]\n', 1))
    git_run("commit", "-qam", "list origin", cwd=repo)
    r = run(["init"], cwd=repo)
    assert r.code == 0 and "private_remotes = [" not in r.stdout + r.stderr



# --- review fixes --------------------------------------------------------------

@pytest.mark.pty
def test_init_offer_lists_only_addable_values(source_repo, public_remote, run, tmp_path):
    """The records remote has a private value and a pushurl to the
    publish repo. The prompt lists only the private value, names the
    other as "cannot be listed: <reason>", asks "Only if these repositories
    are private", and `y` inserts exactly the listed set."""
    from test_ff_auth import _cli_on_a_pty
    remote = public_remote()
    repo = source_repo({"a.py": "x\n"}, {"remote": remote, "include": ["a.py"]})
    origin = git_run("remote", "get-url", "origin", cwd=repo).strip()
    git_run("remote", "set-url", "--add", "--push", "origin", origin, cwd=repo)
    git_run("remote", "set-url", "--add", "--push", "origin", remote, cwd=repo)
    code, text, _calls = _cli_on_a_pty(repo, ["init"], "y", tmp_path / "pty.log")
    assert code == 0, text
    prompt = text.split("unlisted values:", 1)[1].split("[y/N]", 1)[0]
    assert f"  {origin}\r\n" in prompt and f"  {remote}\r\n" not in prompt, prompt
    assert f"cannot be listed: {remote} is the publish remote" in prompt, prompt
    assert "Only if these repositories are private." in prompt
    assert (repo / ".publish.toml").read_text().startswith(
        f'[publish]\nprivate_remotes = ["{origin}"]\n')


def test_validate_remote_does_not_run_a_refused_transport(source_repo, public_remote, run,
                                                          tmp_path):
    """A source remote with
    remote.<r>.uploadpack set is never contacted by `validate --remote`;
    the configured program does not run (no marker) and a finding names the
    key."""
    backup = _bare(tmp_path, "bk")
    marker = tmp_path / "uploadpack-ran"
    repo = source_repo({"a.py": "x\n"}, {"remote": public_remote(), "include": ["a.py"]},
                       origin=False)
    git_run("remote", "add", "bk", str(backup), cwd=repo)
    git_run("config", "remote.bk.uploadpack", f"touch {marker}; git-upload-pack", cwd=repo)
    assert run(["init"], cwd=repo).code == 0
    r = run(["validate", "--remote"], cwd=repo)
    out = r.stdout + r.stderr
    assert r.code == 0, out
    assert not marker.exists(), "validate --remote ran the configured uploadpack"
    assert "'bk' not contacted: remote.bk.uploadpack is set; check it by hand" in out


def _tag_release_repo(source_repo, public_remote, run):
    remote = public_remote()
    repo = source_repo({"version.py": 'VERSION = "1.0.0"\n', "a.txt": "a\n"},
                       {"remote": remote, "include": ["version.py", "a.txt"],
                        "release": _release()})
    assert run(["init"], cwd=repo).code == 0
    return repo, remote.removeprefix("file://")


def _fail_public_tag_push(monkeypatch, *, keep_tag: bool):
    """The release's public tag push fails once. With keep_tag the
    rollback does not run (as after a kill between tag creation and push),
    so the journal-created tag stays in .publish."""
    real_push, real_rollback = pubrepo.push_porcelain, pubrepo._rollback_created_tag
    state = {"failed": False}

    def push(publish_dir, refspecs, **kw):
        if not state["failed"] and refspecs and refspecs[0].startswith("refs/tags/"):
            state["failed"] = True
            return pubrepo.PushOutcome("host_refused", "injected tag push failure", [])
        return real_push(publish_dir, refspecs, **kw)

    monkeypatch.setattr(pubrepo, "push_porcelain", push)
    if keep_tag:
        monkeypatch.setattr(pubrepo, "_rollback_created_tag", lambda *a, **k: None)
    return lambda: (monkeypatch.setattr(pubrepo, "push_porcelain", real_push),
                    monkeypatch.setattr(pubrepo, "_rollback_created_tag", real_rollback))


@pytest.mark.parametrize("shape", ["hand_made_at_candidate", "journal_tag_remade_by_hand"])
def test_release_refuses_a_local_tag_it_did_not_create(source_repo, public_remote, run,
                                                        monkeypatch, shape):
    """A local annotated v<ver> in .publish at the candidate is
    published only if the journal records creating that exact tag object.
    A hand-made tag, or the journal's tag deleted and re-made by hand with
    the same name and target, → 7 before any push; no tag on the public
    bare."""
    repo, bare = _tag_release_repo(source_repo, public_remote, run)
    restore = _fail_public_tag_push(monkeypatch, keep_tag=(shape != "hand_made_at_candidate"))
    assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 7
    restore()
    pd = repo / ".publish"
    journal = pubrepo.load_journal(pd, "1.0.0")
    candidate = journal.data["public_candidate"]
    if shape == "journal_tag_remade_by_hand":
        assert git_run("rev-parse", "refs/tags/v1.0.0^{}", cwd=pd).strip() == candidate
        git_run("tag", "-d", "v1.0.0", cwd=pd)
    git_run("tag", "-a", "v1.0.0", candidate, "-m", "PRIVATE note", cwd=pd)
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 7, r.stderr
    assert "local tag v1.0.0 in .publish/ was not created by this release" in r.stderr
    assert git_run("ls-remote", bare, "refs/tags/*").strip() == ""


def test_release_resume_reuses_its_own_tag_object(source_repo, public_remote, run,
                                                  monkeypatch):
    """The journal records the object id of the tag the release
    created, and a normal resume reuses that object (the public tag is the
    journal's object)."""
    repo, bare = _tag_release_repo(source_repo, public_remote, run)
    restore = _fail_public_tag_push(monkeypatch, keep_tag=True)
    assert run(["--release", "1.0.0", "--yes"], cwd=repo).code == 7
    restore()
    pd = repo / ".publish"
    journal = pubrepo.load_journal(pd, "1.0.0")
    [entry] = [t for t in journal.data["local_tags_created"] if t["tag"] == "v1.0.0"]
    local = git_run("rev-parse", "refs/tags/v1.0.0", cwd=pd).strip()
    assert entry["object"] == local
    r = run(["--release", "1.0.0", "--yes"], cwd=repo)
    assert r.code == 0, r.stderr
    assert git_run("ls-remote", bare, "refs/tags/v1.0.0").split()[0] == local
