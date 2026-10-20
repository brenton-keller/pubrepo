"""The outbound gate engine (design §4.6, INV-9). Fixtures are
generated at runtime; no binary is committed.
Publish, --stage and --dry-run call it through the snapshot builder."""
import io
import signal
import subprocess
import zipfile
from pathlib import Path

import pytest

import pubrepo
from conftest import git_run
from ff_fixtures import ooxml_full_docx, ooxml_full_xlsx, ooxml_write_zip

XLSX_SURFACES = {
    "core_creator": "CNRY_CORE_CREATOR", "core_lastmodby": "CNRY_CORE_LASTMODBY",
    "app_company": "CNRY_APP_COMPANY", "app_manager": "CNRY_APP_MANAGER",
    "hidden_sheetname": "CNRY_HIDDEN_SHEETNAME", "vhidden_sheetname": "CNRY_VHIDDEN_SHEETNAME",
    "custom_prop": "CNRY_CUSTOM_PROP", "customxml": "CNRY_CUSTOMXML",
    "defname_name": "CNRY_DEFNAME_NAME", "defname_value": "CNRY_DEFNAME_VALUE",
    "hidden_cell": "CNRY_HIDDEN_CELL", "sst_plain": "CNRY_SST_PLAIN",
    "sst_richrun": "CNRY_SST_RICHRUN", "formula_cached": "CNRY_FORMULA_CACHEDX",
    "formula_lit": "CNRY_FORMULA_LIT", "inline_str": "CNRY_INLINE_STR",
    "xlsx_header": "CNRY_XLSX_HEADER", "vhidden_cell": "CNRY_VHIDDEN_CELL",
    "legacy_comment_author": "CNRY_LEGACY_COMMENT_AUTHOR",
    "legacy_comment_text": "CNRY_LEGACY_COMMENT_TEXT", "threaded_text": "CNRY_THREADED_TEXT",
    "person_displayname": "CNRY_PERSON_DISPLAYNAME", "person_userid": "cnry_person_userid",
}
DOCX_SURFACES = {
    "docx_core_creator": "CNRY_DOCX_CORE_CREATOR", "docx_core_lastmodby": "CNRY_DOCX_CORE_LASTMODBY",
    "docx_app_company": "CNRY_DOCX_APP_COMPANY", "docx_app_manager": "CNRY_DOCX_APP_MANAGER",
    "docx_custom_prop": "CNRY_DOCX_CUSTOM_PROP", "docx_customxml": "CNRY_DOCX_CUSTOMXML",
    "docx_body": "CNRY_DOCX_BODY", "docx_deltext": "CNRY_DOCX_DELTEXT",
    "docx_rev_author": "CNRY_DOCX_REV_AUTHOR", "docx_vanish": "CNRY_DOCX_VANISH",
    "docx_comment_author": "CNRY_DOCX_COMMENT_AUTHOR", "docx_comment_text": "CNRY_DOCX_COMMENT_TEXT",
    "docx_header": "CNRY_DOCX_HEADER", "docx_footnote": "CNRY_DOCX_FOOTNOTE",
    "docx_instrtext": "cnry-docx-instr",
}
SURFACES = [("xlsx", k, v) for k, v in XLSX_SURFACES.items()] + \
           [("docx", k, v) for k, v in DOCX_SURFACES.items()]


def test_surface_inventory_is_38():
    assert len(XLSX_SURFACES) == 23 and len(DOCX_SURFACES) == 15 and len(SURFACES) == 38


@pytest.fixture
def pub(tmp_path):
    repo = tmp_path / "pub"
    git_run("init", "-q", str(repo))
    return repo


def _blob(repo: Path, data: bytes) -> str:
    return subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo, input=data,
                          capture_output=True, check=True).stdout.decode().strip()


def _config(forbidden=(), allow_opaque=(), allow_markers=()):
    return {"scrub": {"forbidden": list(forbidden), "allow_opaque": list(allow_opaque),
                      "allow_markers": list(allow_markers)}}


def _scan(pub, files: dict[str, bytes], config, carried=False):
    entries = [(path, _blob(pub, data)) for path, data in files.items()]
    return pubrepo.gate_scan(pub, entries, config, carried=carried)


def _zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buffer.getvalue()


def _xlsx(tmp_path) -> bytes:
    ooxml_full_xlsx(tmp_path / "full.xlsx")
    return (tmp_path / "full.xlsx").read_bytes()


def _docx(tmp_path) -> bytes:
    ooxml_full_docx(tmp_path / "full.docx")
    return (tmp_path / "full.docx").read_bytes()


def _ooxml(parts: dict[str, str | bytes]) -> bytes:
    base = {"[Content_Types].xml": '<?xml version="1.0"?><Types/>'}
    base.update(parts)
    return _zip_bytes(base)


# --- AC20: every surface, plus comment, CDATA, entity-split, embedded, xlsb ----------

@pytest.mark.parametrize("kind,surface,canary", SURFACES, ids=[s for _k, s, _c in SURFACES])
def test_ooxml_surface_canary_blocks_naming_the_part(pub, tmp_path, kind, surface, canary):
    data = _xlsx(tmp_path) if kind == "xlsx" else _docx(tmp_path)
    result = _scan(pub, {f"deliverables/full.{kind}": data}, _config([canary]))
    hits = [f for f in result.blocking() if f.kind == "forbidden"]
    assert hits, f"{surface} not caught"
    assert all(f.path == f"deliverables/full.{kind}" and f.part for f in hits)


def test_clean_ooxml_passes(pub, tmp_path):
    result = _scan(pub, {"a.xlsx": _xlsx(tmp_path), "b.docx": _docx(tmp_path)},
                   _config(["not-present-anywhere"]))
    assert result.findings == []


@pytest.mark.parametrize("part,canary", [
    ('<?xml version="1.0"?><r><!-- CNRY_COMMENT --></r>', "CNRY_COMMENT"),
    ('<?xml version="1.0"?><r><![CDATA[CNRY_CDATA]]></r>', "CNRY_CDATA"),
    ('<?xml version="1.0"?><r>CNRY_&#69;NTITY</r>', "CNRY_ENTITY"),
    ('<?xml version="1.0"?><r a="CNRY_&#x41;TTR"/>', "CNRY_ATTR"),
    ('<?xml version="1.0"?><r><t>CNRY_</t><t>SPLIT</t></r>', "CNRY_SPLIT"),
], ids=["comment", "cdata", "entity_split", "attribute_charref", "cross_run"])
def test_xml_part_text_and_parsed_scans(pub, part, canary):
    result = _scan(pub, {"x.docx": _ooxml({"word/document.xml": part})}, _config([canary]))
    assert [f.part for f in result.blocking()] == ["word/document.xml"]


def test_embedded_xlsx_in_docx_is_recursed(pub, tmp_path):
    docx = _ooxml({"word/document.xml": '<?xml version="1.0"?><d/>',
                   "word/embeddings/Microsoft_Excel_Worksheet.xlsx": _xlsx(tmp_path)})
    result = _scan(pub, {"report.docx": docx}, _config(["CNRY_SST_PLAIN"]))
    [hit] = result.blocking()
    assert hit.part == "word/embeddings/Microsoft_Excel_Worksheet.xlsx!xl/sharedStrings.xml"


def test_xlsb_shaped_package_blocks_on_binary_parts(pub):
    xlsb = _ooxml({"xl/workbook.bin": b"\x83\x01\x00binary record",
                   "xl/worksheets/sheet1.bin": b"\x00\x01\x02"})
    result = _scan(pub, {"book.xlsb": xlsb}, _config())
    assert sorted(f.part for f in result.blocking() if f.kind == "unscannable") == [
        "xl/workbook.bin", "xl/worksheets/sheet1.bin"]


# --- AC21: corrupt, non-XML part, CFB ------------------------------------------------------

def test_corrupt_zip_blocks(pub):
    result = _scan(pub, {"broken.xlsx": b"PK\x03\x04" + b"garbage" * 20}, _config())
    [hit] = result.blocking()
    assert hit.kind == "corrupt" and hit.path == "broken.xlsx"


def test_non_xml_part_blocks_as_corrupt(pub):
    result = _scan(pub, {"bad.xlsx": _ooxml({"xl/workbook.xml": "this is not < xml"})}, _config())
    [hit] = result.blocking()
    assert hit.kind == "corrupt" and hit.part == "xl/workbook.xml"


def test_cfb_container_is_labelled_and_blocks(pub):
    cfb = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 100
    [hit] = _scan(pub, {"legacy.doc": cfb}, _config()).blocking()
    assert hit.kind == "cfb" and hit.detail == "encrypted or legacy Office container"


def test_cfb_follows_allow_opaque(pub):
    cfb = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 100
    result = _scan(pub, {"legacy.doc": cfb}, _config(allow_opaque=["*.doc"]))
    assert result.blocking() == [] and result.findings[0].allowlisted


# --- AC22: other binaries and allow_opaque ------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def test_non_allowlisted_binary_blocks(pub):
    [hit] = _scan(pub, {"docs/demo.png": PNG}, _config()).blocking()
    assert hit.kind == "unscannable" and hit.path == "docs/demo.png"


def test_allowlisted_binary_is_reported_not_scanned(pub):
    result = _scan(pub, {"docs/demo.png": PNG}, _config(allow_opaque=["docs/*.png"]))
    assert result.blocking() == []
    [finding] = result.findings
    assert finding.allowlisted and finding.detail == "not scanned (allowlisted)"


def test_allow_opaque_globs_match_archive_members(pub):
    docx = _ooxml({"word/document.xml": '<?xml version="1.0"?><d/>', "word/media/image1.png": PNG})
    blocked = _scan(pub, {"r.docx": docx}, _config())
    assert [f.part for f in blocked.blocking()] == ["word/media/image1.png"]
    allowed = _scan(pub, {"r.docx": docx}, _config(allow_opaque=["r.docx!word/media/*"]))
    assert allowed.blocking() == []


def test_allow_opaque_never_clears_ooxml_text(pub, tmp_path):
    result = _scan(pub, {"a.xlsx": _xlsx(tmp_path)},
                   _config(["CNRY_SST_PLAIN"], allow_opaque=["a.xlsx", "*"]))
    assert [f.kind for f in result.blocking()] == ["forbidden"]


def test_top_level_plain_zip_is_opaque(pub):
    [hit] = _scan(pub, {"bundle.zip": _zip_bytes({"a.txt": "x"})}, _config()).blocking()
    assert hit.kind == "unscannable" and hit.part is None


# --- text files and AC23 markers ---------------------------------------------------------

def test_text_forbidden_is_case_insensitive(pub):
    [hit] = _scan(pub, {"notes.md": b"Contact Internal-Host today\n"},
                  _config(["internal-host"])).blocking()
    assert hit.kind == "forbidden" and hit.path == "notes.md" and hit.part is None


def test_marker_triple_blocks_and_rst_underline_passes(pub):
    conflict = b"a\n<<<<<<< ours\nmine\n=======\ntheirs\n>>>>>>> theirs\nb\n"
    rst = b"Title\n=======\n\ntext\n"
    result = _scan(pub, {"c.txt": conflict, "doc.rst": rst}, _config())
    [hit] = result.blocking()
    assert hit.kind == "marker" and hit.path == "c.txt" and "line(s) 2" in hit.detail


def test_allow_markers_allowlists_by_path(pub):
    conflict = b"<<<<<<< a\n=======\n>>>>>>> b\n"
    result = _scan(pub, {"docs/merging.md": conflict}, _config(allow_markers=["docs/*.md"]))
    assert result.blocking() == [] and result.findings[0].allowlisted


@pytest.mark.parametrize("text,expected", [
    ("<<<<<<< ours\nx\n=======\ny\n>>>>>>> theirs\n", [1]),
    ("<<<<<<< ours\nx\n||||||| base\nb\n=======\ny\n>>>>>>> theirs\n", [1]),   # diff3
    ("<<<<<<<\n=======\n>>>>>>>\n\n<<<<<<< a\n=======\n>>>>>>> b\n", [1, 5]),
    ("Heading\n=======\n", []),
    ("<<<<<<< only a start\n", []),
    ("=======\n<<<<<<< x\n>>>>>>> y\n", []),                                     # out of order
    ("<<<<<<< ours\r\nx\r\n=======\r\ny\r\n>>>>>>> theirs\r\n", [1]),            # CRLF
])
def test_has_marker_triple(text, expected):
    assert pubrepo.has_marker_triple(text) == expected


# --- carried content and release_blocking (design §4.6 item 6) -------------------

def test_carried_scan_never_blocks_publish_but_blocks_release(pub):
    result = _scan(pub, {"collab.txt": b"internal-host\n", "pic.png": PNG},
                   _config(["internal-host"]), carried=True)
    assert result.blocking() == []
    assert sorted(f.kind for f in result.release_blocking(set())) == ["forbidden", "unscannable"]


def test_accept_unscanned_clears_only_unscannable_and_cfb(pub):
    cfb = bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 10
    result = _scan(pub, {"collab.txt": b"internal-host\n", "pic.png": PNG, "old.doc": cfb},
                   _config(["internal-host"]), carried=True)
    remaining = result.release_blocking({"pic.png", "old.doc", "collab.txt"})
    assert [(f.path, f.kind) for f in remaining] == [("collab.txt", "forbidden")]


# --- XML safety and limits ---------------------------------------

BILLION_LAUGHS = '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">' + "".join(
    f'<!ENTITY lol{i} "{("&lol" + (str(i - 1) if i > 1 else "") + ";") * 10}">' for i in range(1, 10)
) + ']><lolz>&lol9;</lolz>'


def _on_alarm(_signum, _frame):
    raise TimeoutError("gate scan exceeded 10 s")


def test_billion_laughs_is_a_limit_finding_without_hanging(pub):
    """Bounded expansion; the plan's 10 s test bound, enforced with
    SIGALRM because pytest-timeout is not a dependency."""
    previous = signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(10)
    try:
        [hit] = _scan(pub, {"bomb.xlsx": _ooxml({"xl/sharedStrings.xml": BILLION_LAUGHS})},
                      _config()).blocking()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
    assert hit.kind == "limit" and hit.part == "xl/sharedStrings.xml"


def test_external_entity_is_never_resolved(pub, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("CNRY_EXTERNAL\n")
    part = (f'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY ext SYSTEM "{secret.as_uri()}">]>'
            '<r>&ext;</r>')
    result = _scan(pub, {"x.docx": _ooxml({"word/document.xml": part})}, _config(["CNRY_EXTERNAL"]))
    assert [f.kind for f in result.blocking()] == ["limit"]


def test_small_internal_entity_expands_and_is_scanned(pub):
    part = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY e "CNRY_ENT">]><r>&e;</r>'
    result = _scan(pub, {"x.docx": _ooxml({"word/document.xml": part})}, _config(["CNRY_ENT"]))
    assert [f.kind for f in result.blocking()] == ["forbidden"]


def test_nesting_deeper_than_limit_is_a_limit_finding(pub, monkeypatch):
    monkeypatch.setattr(pubrepo, "OOXML_MAX_DEPTH", 2)
    inner = _ooxml({"a.xml": "<a/>"})
    middle = _ooxml({"m.xlsx": inner})
    outer = _ooxml({"x.xlsx": middle})
    kinds = [f.kind for f in _scan(pub, {"o.docx": outer}, _config()).blocking()]
    assert kinds == ["limit"]


def test_member_count_limit(pub, monkeypatch):
    monkeypatch.setattr(pubrepo, "OOXML_MAX_MEMBERS", 3)
    data = _ooxml({f"p{i}.xml": "<p/>" for i in range(5)})
    [hit] = _scan(pub, {"many.xlsx": data}, _config()).blocking()
    assert hit.kind == "limit" and "members" in hit.detail


def test_expanded_size_limit(pub, monkeypatch):
    monkeypatch.setattr(pubrepo, "OOXML_MAX_EXPANDED", 1000)
    data = _ooxml({"big.xml": "<b>" + "x" * 5000 + "</b>"})
    [hit] = _scan(pub, {"big.xlsx": data}, _config()).blocking()
    assert hit.kind == "limit" and "expands" in hit.detail


def test_encrypted_member_is_unscannable(pub):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("xl/workbook.xml", "<w/>")
    data = bytearray(buffer.getvalue())
    # zipfile clears flag bits on write, so set "encrypted" (bit 0) in every
    # local (offset 6) and central (offset 8) header. Stored, short members
    # cannot contain the signatures by accident.
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = data.find(signature)
        while start != -1:
            data[start + offset] |= 0x1
            start = data.find(signature, start + 4)
    findings = _scan(pub, {"enc.xlsx": bytes(data)}, _config()).blocking()
    assert [(f.part, f.kind, f.detail) for f in findings] == [
        ("[Content_Types].xml", "unscannable", "encrypted member"),
        ("xl/workbook.xml", "unscannable", "encrypted member")]


# --- extract_text ------------------------------------------------------------------

def test_extract_text_covers_every_part(tmp_path):
    text = pubrepo.extract_text("full.xlsx", _xlsx(tmp_path))
    for canary in XLSX_SURFACES.values():
        assert canary.lower() in text.lower(), canary


def test_extract_text_none_for_non_ooxml():
    assert pubrepo.extract_text("a.png", PNG) is None
    assert pubrepo.extract_text("b.zip", _zip_bytes({"a.txt": "x"})) is None
    assert pubrepo.extract_text("c.xlsx", b"PK\x03\x04garbage") is None


def test_gate_reads_blobs_from_publish_odb_only(pub, tmp_path):
    """The blobs come from publish_dir's ODB (design §4.6; Π's objects live there)."""
    other = tmp_path / "other"
    git_run("init", "-q", str(other))
    blob = _blob(other, b"internal-host\n")
    with pytest.raises(pubrepo.PublishError, match="missing"):
        pubrepo.gate_scan(pub, [("x.txt", blob)], _config(["internal-host"]))
