"""Shared fixtures for the 1.1.0 fast-forward work.

Registered through `tests/conftest.py`: a core block, then the "sync
 fixtures" block. Everything is synthetic:
identities use example.invalid, binaries are generated at runtime,
and nothing here may contain a string from either repo's scrub.forbidden.
"""
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace
from xml.sax.saxutils import escape as _esc

import pytest

from conftest import git_run

COLLAB_NAME = "Collab Orator"
COLLAB_EMAIL = "collab@example.invalid"


# --- core fixtures ---

@pytest.fixture
def bare_remote(tmp_path):
    """Factory: a bare repo with installable hooks.

    Returns a namespace with `path`, `url` (file://) and
    `install_hook(name, script)`, which writes an executable hook.
    """
    counter = {"n": 0}

    def _make(branch: str = "main") -> SimpleNamespace:
        counter["n"] += 1
        path = tmp_path / f"ff-remote{counter['n']}.git"
        git_run("init", "--bare", "-q", "-b", branch, str(path))

        def install_hook(name: str, script: str) -> Path:
            hook = path / "hooks" / name
            hook.write_text(script)
            hook.chmod(0o755)
            return hook

        return SimpleNamespace(path=path, url=path.as_uri(), install_hook=install_hook)

    return _make


@pytest.fixture
def collab_clone(tmp_path):
    """Factory: a collaborator clone of `url` with the synthetic identity.

    The namespace's `commit(files, message)` writes files (None deletes),
    commits everything, and pushes to the branch.
    """
    counter = {"n": 0}

    def _make(url: str, branch: str = "main") -> SimpleNamespace:
        counter["n"] += 1
        work = tmp_path / f"collab{counter['n']}"
        git_run("clone", "-q", url, str(work))
        git_run("config", "user.name", COLLAB_NAME, cwd=work)
        git_run("config", "user.email", COLLAB_EMAIL, cwd=work)

        def commit(files: dict, message: str = "collaborator change") -> str:
            for rel, content in files.items():
                target = work / rel
                if content is None:
                    target.unlink()
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if isinstance(content, bytes):
                    target.write_bytes(content)
                else:
                    target.write_text(content)
            git_run("add", "-A", cwd=work)
            git_run("commit", "-qm", message, cwd=work)
            git_run("push", "-q", "origin", f"HEAD:refs/heads/{branch}", cwd=work)
            return git_run("rev-parse", "HEAD", cwd=work).strip()

        return SimpleNamespace(path=work, commit=commit)

    return _make


def push_argv(argv) -> tuple | None:
    """The push part of a git argv (leading `-c k=v` pairs stripped), or None."""
    argv = list(argv)
    while len(argv) >= 2 and argv[0] == "-c":
        argv = argv[2:]
    return tuple(argv) if argv[:1] == ["push"] else None


@pytest.fixture
def fault_seam(monkeypatch, tmp_path):
    """Arm the PUBREPO_TEST_FAULT seam for one point and action.

    `arm(point, action)` sets the environment. For `pause-file`, the
    returned path is the flag file; create it to release the pause.
    """

    def arm(point: str, action: str) -> Path | None:
        monkeypatch.setenv("PUBREPO_TEST_FAULT", f"{point}:{action}")
        if action != "pause-file":
            return None
        flag = tmp_path / "fault-release"
        monkeypatch.setenv("PUBREPO_TEST_FAULT_FILE", str(flag))
        return flag

    return arm


@pytest.fixture
def git_recorder(tmp_path, monkeypatch):
    """A PATH shim in front of the real git that logs every argv.

    pubrepo runs `git` through PATH, so its calls are recorded, and so are
    the test's own `git_run` calls made while the shim is installed.
    `calls()` returns [(argv, cwd)] in order. Policy checks on the argv
    (AC62) belong to the AC62 shim below, which builds on this one.
    """
    real_git = shutil.which("git")
    assert real_git, "git not on PATH"
    shim_dir = tmp_path / "git-shim"
    shim_dir.mkdir()
    log = tmp_path / "git-calls.jsonl"
    shim = shim_dir / "git"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(log)!r}, 'a') as f:\n"
        "    f.write(json.dumps([sys.argv[1:], os.getcwd()]) + '\\n')\n"
        f"os.execv({real_git!r}, [{real_git!r}] + sys.argv[1:])\n")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    def calls() -> list[tuple[list[str], str]]:
        if not log.exists():
            return []
        return [tuple(json.loads(line)) for line in log.read_text().splitlines()]

    return SimpleNamespace(calls=calls, log=log)



# --- AC62 git policy ---
# pubrepo resolves `git` through PATH, where the suite-wide sandbox shim
# (conftest) sits. While the `run` fixture executes main(), the shim
# logs every argv; at teardown the routine-policy check runs over what
# pubrepo itself did (test setup git calls are not logged).

def register_ac62_markers(config):
    """Called from conftest's pytest_configure (one hook for the suite)."""
    config.addinivalue_line("markers", "rewrite: the test exercises an explicit rewrite "
                            "(--amend, repair); the AC62 routine policy does not apply")
    config.addinivalue_line("markers", "no_git_calls: the CLI under test must make zero git "
                            "calls (AC63); asserts exactly 0 instead of at least 1")


_PUSH_FETCH_FLAGS = ("--force", "-f", "--prune", "--mirror", "--delete", "-d", "--no-verify")


def _strip_config_pairs(argv: list[str]) -> list[str]:
    """Drop leading `-c key=value` pairs:
    `git -c k=v push --force` must not pass as routine."""
    while len(argv) >= 2 and argv[0] == "-c":
        argv = argv[2:]
    return argv


def forbidden_git_argv(argv: list[str], created_tags: set[str]) -> str | None:
    """Why `argv` is forbidden for a routine command (AC62, INV-1), or None.

    Push/fetch: force, a lease, prune, mirror, delete, a `+` refspec, or a
    push refspec with an empty source. Tags: delete or force. update-ref -d
    of a tag. Exception (INV-1): deleting a tag created earlier in the same
    run (`created_tags`). Leading `-c` pairs are skipped; --no-verify
    is forbidden for pubrepo's own pushes (§17.2.1)."""
    argv = _strip_config_pairs(list(argv))
    if not argv:
        return None
    sub, args = argv[0], argv[1:]
    if sub in ("push", "fetch"):
        for a in args:
            if a in _PUSH_FETCH_FLAGS or a.startswith("--force-with-lease"):
                return f"{sub} {a}"
            if a.startswith("+"):
                return f"{sub} with a + refspec ({a})"
            if sub == "push" and a.startswith(":"):
                tag = a[1:].removeprefix("refs/tags/")
                if a[1:].startswith("refs/tags/") and tag in created_tags:
                    continue
                return f"push with an empty-source refspec ({a})"
    if sub == "tag":
        if any(a in ("-d", "--delete") for a in args):
            if all(a in created_tags for a in args if not a.startswith("-")):
                return None
            return "tag delete"
        if any(a in ("-f", "--force") for a in args):
            return "tag -f"
    if sub == "update-ref" and "-d" in args:
        refs = [a for a in args if a.startswith("refs/tags/")]
        if refs and not all(r.removeprefix("refs/tags/") in created_tags for r in refs):
            return "update-ref -d of a tag"
    return None


_TAG_VALUE_OPTIONS = ("-m", "-F", "-u", "--message", "--file", "--local-user")


def _tags_created(argv: list[str]) -> set[str]:
    """Tag names a `git tag` / `update-ref` call creates. `git tag -a <name>
    <commit> -m <msg>` creates <name> (rollbacks); a call with
    -d, -f or a listing flag creates nothing."""
    argv = _strip_config_pairs(list(argv))
    if argv[:1] == ["tag"]:
        if any(a in ("-d", "--delete", "-f", "--force", "-l", "--list", "-v") for a in argv[1:]):
            return set()
        rest, i = argv[1:], 0
        while i < len(rest):
            if rest[i] in _TAG_VALUE_OPTIONS:
                i += 2
                continue
            if rest[i].startswith("-"):
                i += 1
                continue
            return {rest[i]}
        return set()
    if argv[:1] == ["update-ref"] and "-d" not in argv:
        return {a.removeprefix("refs/tags/") for a in argv[1:2] if a.startswith("refs/tags/")}
    return set()


@pytest.fixture
def git_policy(request, tmp_path, monkeypatch):
    """AC62 for the in-process CLI. `active()` wraps one main() call; the
    suite-wide sandbox git shim (conftest) logs pubrepo's argv while
    PUBREPO_AC62_LOG is set; the teardown fails the test on a forbidden
    routine argv, or on vacuity."""
    log = tmp_path / "ac62-git.jsonl"
    runs: list[list[list[str]]] = []

    @contextlib.contextmanager
    def active():
        start = log.stat().st_size if log.exists() else 0
        with monkeypatch.context() as m:
            m.setenv("PUBREPO_AC62_LOG", str(log))
            try:
                yield
            finally:
                data = log.read_bytes()[start:] if log.exists() else b""
                runs.append([record.decode(errors="replace").split("\0")[:-1]
                             for record in data.split(b"\036") if record])

    yield SimpleNamespace(active=active, runs=runs)

    if not runs:
        return
    calls = [argv for run_calls in runs for argv in run_calls]
    if request.node.get_closest_marker("no_git_calls"):
        assert calls == [], f"@no_git_calls: the CLI made git calls: {calls[:5]}"
        return
    assert calls, ("AC62 non-vacuity: the CLI made no git call through the shim; "
                   "mark the test @pytest.mark.no_git_calls if that is intended")
    if request.node.get_closest_marker("rewrite"):
        return
    for run_calls in runs:
        created: set[str] = set()
        for argv in run_calls:
            why = forbidden_git_argv(argv, created)
            assert why is None, f"AC62: routine command ran a forbidden git call ({why}): {argv}"
            created |= _tags_created(argv)

# Runtime OOXML generator.
# Every text or metadata surface carries a unique canary CNRY_<SURFACE>. It
# shares no code with pubrepo's walker (an independent oracle for AC20).

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PR = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'


def _core_xml(creator, lastmod, modified="2026-01-01T00:00:00Z", title="Synthetic"):
    return _DECL + (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{_esc(title)}</dc:title><dc:creator>{_esc(creator)}</dc:creator>'
        f'<cp:lastModifiedBy>{_esc(lastmod)}</cp:lastModifiedBy>'
        '<dcterms:created xsi:type="dcterms:W3CDTF">2026-01-01T00:00:00Z</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{modified}</dcterms:modified></cp:coreProperties>')


def _app_xml(company, manager, titles):
    vec = "".join(f"<vt:lpstr>{_esc(t)}</vt:lpstr>" for t in titles)
    return _DECL + (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f'<Application>Microsoft Excel</Application><Manager>{_esc(manager)}</Manager><Company>{_esc(company)}</Company>'
        f'<TitlesOfParts><vt:vector size="{len(titles)}" baseType="lpstr">{vec}</vt:vector></TitlesOfParts></Properties>')


def _custom_props(val):
    return _DECL + (
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        '<property fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}" pid="2" name="ClientCode">'
        f'<vt:lpwstr>{_esc(val)}</vt:lpwstr></property></Properties>')


_CUSTOMXML_ITEM = _DECL + '<root xmlns="urn:synthetic"><client>CNRY_CUSTOMXML</client></root>'
_CUSTOMXML_PROPS = _DECL + (
    '<ds:datastoreItem ds:itemID="{00000000-0000-0000-0000-000000000001}" '
    'xmlns:ds="http://schemas.openxmlformats.org/officeDocument/2006/customXml"><ds:schemaRefs/></ds:datastoreItem>')


def ooxml_write_zip(path: Path, files: dict[str, str], compress=zipfile.ZIP_DEFLATED) -> None:
    with zipfile.ZipFile(path, "w", compress) as z:
        for name, data in files.items():
            zi = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            zi.compress_type = compress
            z.writestr(zi, data)


def ooxml_full_xlsx(path: Path) -> dict[str, str]:
    """An xlsx with every text and metadata surface carrying a canary."""
    files = {}
    files["[Content_Types].xml"] = _DECL + (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Default Extension="vml" ContentType="application/vnd.openxmlformats-officedocument.vmlDrawing"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        + "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in (1, 2, 3)) +
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/comments1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.comments+xml"/>'
        '<Override PartName="/xl/threadedComments/threadedComment1.xml" ContentType="application/vnd.ms-excel.threadedcomments+xml"/>'
        '<Override PartName="/xl/persons/person.xml" ContentType="application/vnd.ms-excel.person+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/>'
        '<Override PartName="/customXml/itemProps1.xml" ContentType="application/vnd.openxmlformats-officedocument.customXmlProperties+xml"/>'
        '</Types>')
    files["_rels/.rels"] = _DECL + (
        f'<Relationships xmlns="{_NS_PR}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties" Target="docProps/custom.xml"/>'
        '</Relationships>')
    files["docProps/core.xml"] = _core_xml("CNRY_CORE_CREATOR", "CNRY_CORE_LASTMODBY")
    files["docProps/app.xml"] = _app_xml("CNRY_APP_COMPANY", "CNRY_APP_MANAGER",
                                         ["Data", "CNRY_HIDDEN_SHEETNAME", "CNRY_VHIDDEN_SHEETNAME"])
    files["docProps/custom.xml"] = _custom_props("CNRY_CUSTOM_PROP")
    files["customXml/item1.xml"] = _CUSTOMXML_ITEM
    files["customXml/itemProps1.xml"] = _CUSTOMXML_PROPS
    files["customXml/_rels/item1.xml.rels"] = _DECL + (
        f'<Relationships xmlns="{_NS_PR}"><Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/customXmlProps" Target="itemProps1.xml"/></Relationships>')
    files["xl/workbook.xml"] = _DECL + (
        f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_R}"><sheets>'
        '<sheet name="Data" sheetId="1" r:id="rId1"/>'
        '<sheet name="CNRY_HIDDEN_SHEETNAME" sheetId="2" state="hidden" r:id="rId2"/>'
        '<sheet name="CNRY_VHIDDEN_SHEETNAME" sheetId="3" state="veryHidden" r:id="rId3"/>'
        '</sheets><definedNames>'
        '<definedName name="CNRY_DEFNAME_NAME">Data!$A$1</definedName>'
        '<definedName name="Label">"CNRY_DEFNAME_VALUE"</definedName>'
        '</definedNames></workbook>')
    files["xl/_rels/workbook.xml.rels"] = _DECL + (
        f'<Relationships xmlns="{_NS_PR}">'
        + "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in (1, 2, 3)) +
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
        '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '<Relationship Id="rId6" Type="http://schemas.microsoft.com/office/2017/10/relationships/person" Target="persons/person.xml"/>'
        '</Relationships>')
    files["xl/styles.xml"] = _DECL + (
        f'<styleSheet xmlns="{_NS_MAIN}"><fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills><borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="1"><xf xfId="0"/></cellXfs></styleSheet>')
    files["xl/sharedStrings.xml"] = _DECL + (
        f'<sst xmlns="{_NS_MAIN}" count="3" uniqueCount="3">'
        '<si><t>CNRY_SST_PLAIN</t></si>'
        '<si><r><t>rich </t></r><r><rPr><b/></rPr><t>CNRY_SST_RICHRUN</t></r></si>'
        '<si><t>CNRY_HIDDEN_CELL</t></si></sst>')
    files["xl/worksheets/sheet1.xml"] = _DECL + (
        f'<worksheet xmlns="{_NS_MAIN}" xmlns:r="{_NS_R}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>'
        '<row r="2"><c r="A2" t="s"><v>1</v></c></row>'
        '<row r="3"><c r="A3" t="inlineStr"><is><t>CNRY_INLINE_STR</t></is></c></row>'
        '<row r="4"><c r="A4" t="str"><f>"CNRY_FORMULA_LIT"&amp;"x"</f><v>CNRY_FORMULA_CACHEDx</v></c></row>'
        '</sheetData><headerFooter><oddHeader>&amp;CCNRY_XLSX_HEADER</oddHeader></headerFooter>'
        '<legacyDrawing r:id="rId2"/></worksheet>')
    files["xl/worksheets/_rels/sheet1.xml.rels"] = _DECL + (
        f'<Relationships xmlns="{_NS_PR}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="../comments1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>'
        '<Relationship Id="rId3" Type="http://schemas.microsoft.com/office/2017/10/relationships/threadedComment" Target="../threadedComments/threadedComment1.xml"/>'
        '</Relationships>')
    files["xl/worksheets/sheet2.xml"] = _DECL + (
        f'<worksheet xmlns="{_NS_MAIN}"><sheetData>'
        '<row r="1"><c r="A1" t="s"><v>2</v></c></row></sheetData></worksheet>')
    files["xl/worksheets/sheet3.xml"] = _DECL + (
        f'<worksheet xmlns="{_NS_MAIN}"><sheetData>'
        '<row r="1"><c r="A1" t="inlineStr"><is><t>CNRY_VHIDDEN_CELL</t></is></c></row></sheetData></worksheet>')
    files["xl/comments1.xml"] = _DECL + (
        f'<comments xmlns="{_NS_MAIN}"><authors><author>CNRY_LEGACY_COMMENT_AUTHOR</author></authors>'
        '<commentList><comment ref="B1" authorId="0"><text><r><t>CNRY_LEGACY_COMMENT_TEXT</t></r></text></comment></commentList></comments>')
    files["xl/drawings/vmlDrawing1.vml"] = (
        '<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:x="urn:schemas-microsoft-com:office:excel">'
        '<v:shape type="#_x0000_t202"><x:ClientData ObjectType="Note"><x:Row>0</x:Row><x:Column>1</x:Column></x:ClientData></v:shape></xml>')
    files["xl/threadedComments/threadedComment1.xml"] = _DECL + (
        '<ThreadedComments xmlns="http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments">'
        '<threadedComment ref="B1" dT="2026-01-01T00:00:00.00" personId="{00000000-0000-0000-0000-0000000000AA}" id="{00000000-0000-0000-0000-0000000000BB}">'
        '<text>CNRY_THREADED_TEXT</text></threadedComment></ThreadedComments>')
    files["xl/persons/person.xml"] = _DECL + (
        '<personList xmlns="http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments">'
        '<person displayName="CNRY_PERSON_DISPLAYNAME" id="{00000000-0000-0000-0000-0000000000AA}" userId="cnry_person_userid@example.invalid" providerId="AD"/></personList>')
    ooxml_write_zip(path, files)
    return files


def ooxml_full_docx(path: Path) -> dict[str, str]:
    """A docx with every text and metadata surface carrying a canary."""
    files = {}
    files["[Content_Types].xml"] = _DECL + (
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/comments.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"/>'
        '<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>'
        '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>'
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/docProps/custom.xml" ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml"/>'
        '</Types>')
    files["_rels/.rels"] = _DECL + (
        f'<Relationships xmlns="{_NS_PR}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>'
        '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties" Target="docProps/custom.xml"/>'
        '</Relationships>')
    files["docProps/core.xml"] = _core_xml("CNRY_DOCX_CORE_CREATOR", "CNRY_DOCX_CORE_LASTMODBY")
    files["docProps/app.xml"] = _app_xml("CNRY_DOCX_APP_COMPANY", "CNRY_DOCX_APP_MANAGER", ["Synthetic"])
    files["docProps/custom.xml"] = _custom_props("CNRY_DOCX_CUSTOM_PROP")
    files["customXml/item1.xml"] = _CUSTOMXML_ITEM.replace("CNRY_CUSTOMXML", "CNRY_DOCX_CUSTOMXML")
    files["word/document.xml"] = _DECL + (
        f'<w:document xmlns:w="{_NS_W}" xmlns:r="{_NS_R}"><w:body>'
        '<w:p><w:r><w:t>CNRY_DOCX_BODY</w:t></w:r></w:p>'
        '<w:p><w:del w:id="1" w:author="CNRY_DOCX_REV_AUTHOR" w:date="2026-01-01T00:00:00Z"><w:r><w:delText>CNRY_DOCX_DELTEXT</w:delText></w:r></w:del>'
        '<w:ins w:id="2" w:author="CNRY_DOCX_REV_AUTHOR" w:date="2026-01-01T00:00:00Z"><w:r><w:t>inserted</w:t></w:r></w:ins></w:p>'
        '<w:p><w:r><w:rPr><w:vanish/></w:rPr><w:t>CNRY_DOCX_VANISH</w:t></w:r></w:p>'
        '<w:p><w:commentRangeStart w:id="0"/><w:r><w:t>commented</w:t></w:r><w:commentRangeEnd w:id="0"/><w:r><w:commentReference w:id="0"/></w:r></w:p>'
        '<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "https://cnry-docx-instr.example.invalid" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>'
        '<w:p><w:r><w:footnoteReference w:id="1"/></w:r></w:p>'
        '<w:sectPr><w:headerReference w:type="default" r:id="rId2"/></w:sectPr></w:body></w:document>')
    files["word/_rels/document.xml.rels"] = _DECL + (
        f'<Relationships xmlns="{_NS_PR}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments" Target="comments.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>'
        '</Relationships>')
    files["word/comments.xml"] = _DECL + (
        f'<w:comments xmlns:w="{_NS_W}"><w:comment w:id="0" w:author="CNRY_DOCX_COMMENT_AUTHOR" w:initials="CX">'
        '<w:p><w:r><w:t>CNRY_DOCX_COMMENT_TEXT</w:t></w:r></w:p></w:comment></w:comments>')
    files["word/header1.xml"] = _DECL + f'<w:hdr xmlns:w="{_NS_W}"><w:p><w:r><w:t>CNRY_DOCX_HEADER</w:t></w:r></w:p></w:hdr>'
    files["word/footnotes.xml"] = _DECL + (
        f'<w:footnotes xmlns:w="{_NS_W}"><w:footnote w:id="1"><w:p><w:r><w:t>CNRY_DOCX_FOOTNOTE</w:t></w:r></w:p></w:footnote></w:footnotes>')
    ooxml_write_zip(path, files)
    return files


# --- sync fixtures ---

@pytest.fixture
def source_worktrees(source_repo):
    """Factory: a source repo plus a second `git worktree` on its own branch.

    Returns a namespace with `a` (the main worktree) and `b` (the linked
    one, branch `wt-b`). Both share one git common dir, so source-side state
    (locks, the in-progress record, records) is shared between them.
    """

    def _make(files: dict | None = None, config: dict | None = None) -> SimpleNamespace:
        a = source_repo(files or {"a.py": "x\n"}, config or {"remote": "file:///unused",
                                                               "include": ["a.py"]})
        b = a.parent / f"{a.name}-wt-b"
        git_run("worktree", "add", "-q", "-b", "wt-b", str(b), cwd=a)
        return SimpleNamespace(a=a, b=b)

    return _make


@pytest.fixture
def lock_holder(tmp_path):
    """Run pubrepo's lock code in a separate process and hold it.

    `hold(cwd, kind)` starts a child that takes `kind` ("source-ex",
    "source-sh" or "publish") for `cwd` and blocks until released; it
    returns once the child holds the lock (or raises with the child's
    output if it could not). `release()` frees every held lock. `try_take`
    runs a child that takes the lock once and returns its exit code
    (0 = acquired, 3 = LockContention).
    """
    import time

    from conftest import SCRIPT_PATH
    script = tmp_path / "lock_child.py"
    script.write_text(
        "import importlib.machinery, importlib.util, os, sys, time\n"
        "from pathlib import Path\n"
        f"loader = importlib.machinery.SourceFileLoader('pubrepo', {str(SCRIPT_PATH)!r})\n"
        "spec = importlib.util.spec_from_loader('pubrepo', loader)\n"
        "m = importlib.util.module_from_spec(spec); loader.exec_module(m)\n"
        "cwd, kind, ready, release = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]\n"
        "ctx = {'source-ex': lambda: m.source_lock(cwd, exclusive=True),\n"
        "       'source-sh': lambda: m.source_lock(cwd, exclusive=False),\n"
        "       'publish': lambda: m.publish_lock(cwd)}[kind]()\n"
        "try:\n"
        "    with ctx:\n"
        "        if ready != '-':\n"
        "            Path(ready).write_text(str(os.getpid()))\n"
        "        while release != '-' and not os.path.exists(release):\n"
        "            time.sleep(0.02)\n"
        "except m.LockContention as e:\n"
        "    print(e, file=sys.stderr); sys.exit(3)\n")
    procs = []
    counter = {"n": 0}
    release_flag = tmp_path / "lock-release"

    def hold(cwd: Path, kind: str):
        counter["n"] += 1
        ready = tmp_path / f"lock-ready-{counter['n']}"
        proc = subprocess.Popen([sys.executable, str(script), str(cwd), kind, str(ready),
                                 str(release_flag)], cwd=tmp_path,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        procs.append(proc)
        for _ in range(500):
            if ready.exists():
                return proc
            if proc.poll() is not None:
                raise AssertionError(f"lock child exited {proc.returncode}: "
                                     f"{proc.communicate()}")
            time.sleep(0.01)
        raise AssertionError("lock child never acquired")

    def try_take(cwd: Path, kind: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(script), str(cwd), kind, "-", "-"],
                              cwd=tmp_path, capture_output=True, text=True, timeout=60)

    def release():
        release_flag.touch()
        for proc in procs:
            proc.wait(timeout=30)
        procs.clear()
        release_flag.unlink()

    yield SimpleNamespace(hold=hold, try_take=try_take, release=release)
    if procs:
        release_flag.touch()
        for proc in procs:
            proc.wait(timeout=30)


# Scenario world for the sync tests: a source repo, a public bare remote and
# `.publish` as its clone, with helpers for snapshots, collaborator commits,
# pull records and compute_integration.
from conftest import pubrepo  # noqa: E402

OWNER = ("Owner", "owner@example.invalid")
CONFIG_REL = ".publish.toml"



def _git(repo, *args, env=None):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, env=env).stdout


@pytest.fixture
def world(source_repo, public_remote, collab_clone, tmp_path):
    """Factory: a source repo, an empty public bare remote, and `.publish`
    as a clone of it. Returns a namespace of helpers.

    `listed` (default) lists the source's `origin`, the records remote by
    default (R6), in `private_remotes`, so records sync as they did before
    §18.11 (§10 item 20). `listed=False` leaves it
    unlisted; any `private_remotes` passed in are kept after it."""

    def _make(files=None, include=None, listed=True, **extra):
        url = public_remote()
        private = list(extra.pop("private_remotes", []))
        cfg = {"remote": url, "include": include or ["a.py", "docs"], **extra}
        src = source_repo(files or {"a.py": "a = 1\n", "docs/guide.md": "# Guide\n"}, cfg)
        if listed:
            private.insert(0, git_run("remote", "get-url", "origin", cwd=src).strip())
        if private:
            # Written into the committed config (the init commit is amended, so
            # the history keeps its shape); values are byte-exact.
            path = src / CONFIG_REL
            line = "private_remotes = [" + ", ".join(json.dumps(v) for v in private) + "]"
            path.write_text(path.read_text().replace("[publish]\n", f"[publish]\n{line}\n", 1))
            git_run("commit", "-q", "--amend", "--no-edit", "-a", cwd=src)
            git_run("push", "-q", "-f", "origin", "main", cwd=src)
        pub = src / ".publish"
        git_run("clone", "-q", url, str(pub))
        git_run("config", "user.name", OWNER[0], cwd=pub)
        git_run("config", "user.email", OWNER[1], cwd=pub)
        ns = SimpleNamespace(src=src, pub=pub, url=url, collab=None)
        ns.config = lambda: pubrepo.load_publish_config(src / CONFIG_REL)
        ns.head = lambda: git_run("rev-parse", "HEAD", cwd=src).strip()

        def commit_source(files, message="source change"):
            for rel, content in files.items():
                p = src / rel
                if content is None:
                    p.unlink()
                else:
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content)
            git_run("add", "-A", cwd=src)
            git_run("commit", "-qm", message, cwd=src)
            return ns.head()

        def snapshot(*, sigma=None, carry=False, extra=None, drop=(), dirty=False,
                     record=False, title="publish: 2026-10-14 12:00"):
            """A pubrepo-shaped snapshot. Default: legacy nuke-rebuild (tree =
            the included files at σ). carry=True: start from the public tip
            and overlay them (carry-forward). `extra` adds files (dirty or
            untracked content); `drop` removes paths afterwards."""
            sigma = sigma or ns.head()
            remote_has = _git(pub, "ls-remote", "origin", "refs/heads/main").strip()
            if remote_has:
                git_run("fetch", "-q", "origin", "main", cwd=pub)
                git_run("checkout", "-q", "-B", "main", "origin/main", cwd=pub)
            if not carry:
                for p in list(pub.iterdir()):
                    if p.name != ".git":
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
            config = ns.config()
            at = pubrepo.projection_at(src, pub, sigma, CONFIG_REL)   # Π(σ), transforms applied
            for rel, entry in (at.entries.items() if at else []):
                target = pub / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                repo = pub if entry.transformed else src
                target.write_bytes(subprocess.run(
                    ["git", "cat-file", "blob", entry.blob], cwd=repo, capture_output=True,
                    check=True).stdout)
            for rel, content in (extra or {}).items():
                target = pub / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            for rel in drop:
                (pub / rel).unlink()
            git_run("add", "-A", cwd=pub)
            msg = (f"{title}\n\nSource: {sigma[:7]}{' (dirty)' if dirty else ''}\n"
                   f"Branch: main\nFiles: 1 (0 added, 1 modified, 0 deleted)\n")
            git_run("commit", "-q", "--allow-empty", "-m", msg, cwd=pub)
            sha = git_run("rev-parse", "HEAD", cwd=pub).strip()
            git_run("push", "-q", "origin", "HEAD:refs/heads/main", cwd=pub)
            if record:
                written = {}
                parent = _git(pub, "rev-parse", f"{sha}^").strip() if _has_parent(pub, sha) else None
                for line in _git(pub, "diff-tree", "-r", "--no-renames", *(
                        [parent, sha] if parent else ["--root", sha])).splitlines():
                    if line.startswith(":"):
                        meta, path = line.split("\t", 1)
                        new = meta.split()[3]
                        written[path] = "deleted" if set(new) == {"0"} else new
                pubrepo.write_snapshot_record(
                    src, snapshot_sha=sha, destination=pubrepo.destination_identity(config),
                    sigma_full=sigma, written=written, nonderivable=[], keep=[],
                    config_path=CONFIG_REL, fingerprint="f" * 64)
            return sha

        def collab(files, message="collaborator change"):
            if ns.collab is None:
                ns.collab = collab_clone(url)
            if _git(ns.collab.path, "ls-remote", "origin", "refs/heads/main").strip():
                git_run("pull", "-q", "--ff-only", "origin", "main", cwd=ns.collab.path)
            return ns.collab.commit(files, message)

        def integ(accept=None, fetch_sigma=False, config=None, config_relpath=CONFIG_REL,
                  src_dir=None):
            config = config or ns.config()
            git_run("fetch", "-q", "origin", cwd=pub)
            tip = _git(pub, "rev-parse", "-q", "--verify", "origin/main^{commit}").strip() or None \
                if _git(pub, "ls-remote", "origin", "refs/heads/main").strip() else None
            proj = pubrepo.build_projection(src_dir or src, pub, config)
            return pubrepo.compute_integration(src_dir or src, pub, config, tip, proj,
                                               accept_divergence=accept,
                                               fetch_sigma=fetch_sigma,
                                               config_relpath=config_relpath)

        def pull_record(entries=(), *, through, ip=None, files=None, destination="own",
                        accepted=None, parent_branch=None):
            """A pull commit on source HEAD (§5.7 shape), built directly."""
            if files:
                for rel, content in files.items():
                    p = src / rel
                    if content is None:
                        p.unlink()
                    else:
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_text(content)
                git_run("add", "-A", cwd=src)
            trailers = [f"Pubrepo-Pulled-Through: {through}",
                        f"Pubrepo-Pulled-Range: {ip or 'root'}..{through}"]
            if accepted:
                trailers.append(f"Pubrepo-Accepted-Divergence: {accepted}")
            if destination == "own":
                destination = pubrepo.destination_identity(ns.config())
            if destination:
                trailers.append(f"Pubrepo-Destination: {destination[0]} {destination[1]}")
            body = json.dumps([{**{"class": "C1", "base": "absent", "ours": "absent",
                                   "result": "absent", "managed_at_decision": True,
                                   "config": CONFIG_REL, "prefilled_from": None}, **e}
                               for e in entries], indent=1)
            msg = f"pull: integrate public changes\n\n```json\n{body}\n```\n\n" + "\n".join(trailers)
            git_run("commit", "-q", "--allow-empty", "-m", msg, cwd=src)
            return ns.head()

        ns.commit_source, ns.snapshot, ns.collab_commit = commit_source, snapshot, collab
        ns.integ, ns.pull_record = integ, pull_record
        def blob(rev, path):
            git_run("fetch", "-q", "origin", cwd=pub)
            return _git(pub, "rev-parse", f"{rev}:{path}").strip()

        ns.blob = blob

        def records_synced(src_dir=None):
            """Arrival check: every local record (the four synced
            namespaces) is on the source's `origin` with the same sha, and
            there is at least one. A missing private_remotes listing fails
            here instead of silently not syncing."""
            repo = src_dir or src
            spaces = [f"refs/pubrepo/{ns_}/" for ns_ in pubrepo.RECORD_NAMESPACES]
            local = dict(reversed(line.split(" ", 1)) for line in git_run(
                "for-each-ref", "--format=%(objectname) %(refname)", *spaces,
                cwd=repo).splitlines())
            remote = dict(reversed(line.split("\t", 1)) for line in git_run(
                "ls-remote", git_run("remote", "get-url", "origin", cwd=repo).strip(),
                "refs/pubrepo/*", cwd=repo).splitlines())
            return bool(local) and all(remote.get(name) == sha for name, sha in local.items())

        ns.records_synced = records_synced
        ns.tip = lambda: (git_run("fetch", "-q", "origin", cwd=pub),
                          _git(pub, "rev-parse", "origin/main").strip())[1]
        return ns

    return _make


def _has_parent(repo, sha):
    return subprocess.run(["git", "rev-parse", "-q", "--verify", f"{sha}^"], cwd=repo,
                          capture_output=True).returncode == 0


