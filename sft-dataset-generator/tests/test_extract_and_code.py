"""Code/description pairing, Ballerina heuristics and compiler helpers."""

from __future__ import annotations

import shutil

import pytest

from balpipe_common.config import ValidateCfg
from balpipe_common.schemas import BBEInfo, Document, ExtractorInfo, License

from sftgen import ballerina
from sftgen.extract.pairing import extract_pairs
from sftgen.validate.bal_compiler import parse_errors, prepare


def _doc(markdown: str, doc_type: str = "guide", title: str = "T", **kw) -> Document:
    return Document(doc_id="doc_1", raw_id="raw_1", run_id="r", source="github", source_uri="u", doc_type=doc_type,
                    title=title, markdown=markdown, content_sha256="x", license=License(), split="train",
                    extractor=ExtractorInfo(name="t", version="1"), created_at="t", **kw)


# ---------------------------------------------------------------------- pairing
def test_bbe_pairing_intro_notes_output() -> None:
    md_text = ("# Hello world main\n\nLet's write the program.\n\n```ballerina\nimport ballerina/io;\n\n"
               "public function main() {\n    io:println(\"Hi\");\n}\n```\n\nTo run this sample, use `bal run`.\n\n"
               "```output\n$ bal run x.bal\nHi\n```\n")
    pairs = extract_pairs(_doc(md_text, "bbe", "Hello world main", bbe=BBEInfo(name="hello-world")))
    assert len(pairs) == 1
    p = pairs[0]
    assert p.method == "bbe_directive" and p.intro == "Let's write the program."
    assert p.notes.startswith("To run this sample") and p.expected_output == "$ bal run x.bal\nHi"
    assert p.code.startswith("import ballerina/io;")


def test_md_adjacent_pairing_stays_in_section() -> None:
    md_text = ("# Guide\n\n## A\n\nUnrelated text in A.\n\n## B\n\nFirst B paragraph.\n\nThe record below models a user.\n\n"
               "```ballerina\ntype User record {|\n    string name;\n|};\n```\n\nIn this code, `name` is required.\n\n"
               "Something else entirely.\n")
    (p,) = extract_pairs(_doc(md_text, title="Guide"))
    assert p.intro == "First B paragraph.\n\nThe record below models a user."
    assert p.notes == "In this code, `name` is required."
    assert p.heading_path == ["Guide", "B"] and "Unrelated" not in p.intro


def test_doc_comment_pairing_from_source() -> None:
    src = ("import ballerina/time;\n\n# Returns the current time.\n# + zone - the zone\n# + return - the time\n"
           "public isolated function now(string zone) returns time:Utc {\n    return time:utcNow();\n}\n\n"
           "function internal() {}\n")
    doc = _doc(f"# io: x.bal\n\n```ballerina\n{src}```\n", "module_source")
    (p,) = extract_pairs(doc)
    assert p.method == "doc_comment" and p.intro == "Returns the current time."
    assert p.code.startswith("import ballerina/time;\n\npublic isolated function now")
    assert "+ zone - the zone" in p.notes


# -------------------------------------------------------------------- heuristics
def test_code_form() -> None:
    assert ballerina.code_form('import ballerina/io;\npublic function main() { io:println(1); }') == "program"
    assert ballerina.code_form("service / on new http:Listener(9090) {}") == "program"
    assert ballerina.code_form("type T record {| int a; |};\nconst X = 1;\nint y = 2;") == "declarations"
    assert ballerina.code_form('io:println("x");') == "fragment"
    assert ballerina.code_form("int x = 1;\nx += 2;") == "fragment"
    assert ballerina.code_form("foreach int i in 0...3 {\n}") == "fragment"


def test_split_top_level_keeps_annotations_and_docs_with_declaration() -> None:
    code = ('import ballerina/http;\n\n# Docs\n@http:ServiceConfig {\n    cors: {allowOrigins: ["*"]}\n}\n'
            'service / on new http:Listener(8080) {\n    resource function get a() returns string {\n'
            '        return "}";\n    }\n}\n\ntype R record {|\n    int id;\n|};\n')
    items = ballerina.split_top_level(code)
    assert [it.is_import for it in items] == [True, False, False]
    assert items[1].text.startswith("# Docs\n@http:ServiceConfig") and items[1].text.rstrip().endswith("}")
    assert items[2].text.startswith("type R")


def test_symbols_only_for_imported_prefixes() -> None:
    code = 'import ballerina/http;\nimport ballerina/lang.regexp as re;\nmap<int> m = {a:1};\nhttp:Client c = check new ("http://x");\nstring s = re:replaceAll(x, y);'
    assert ballerina.symbols(code) == ["http:Client", "re:replaceAll"]
    assert ballerina.imports(code) == ["ballerina/http", "ballerina/lang.regexp"]


# ---------------------------------------------------------------- compiler helpers
def test_prepare_wraps_fragment_and_adds_imports() -> None:
    p = prepare('int x = check int:fromString("5");\nio:println(x);', ValidateCfg().auto_imports)
    assert p.wrapped and p.imports_added == ["ballerina/io"]
    assert "public function main() returns error? {" in p.source
    assert p.source.startswith("import ballerina/io;")
    errs = parse_errors("ERROR [main.bal:(5:9,5:20)] undefined symbol 'y'\nerror: compilation contains errors", p.line_offset)
    assert errs == [{"line": 5 - p.line_offset, "col": 9, "message": "undefined symbol 'y'"}]


def test_prepare_leaves_programs_alone() -> None:
    code = 'import ballerina/io;\npublic function main() { io:println(1); }'
    p = prepare(code, ValidateCfg().auto_imports)
    assert p.source == code and not p.wrapped and p.imports_added == []


@pytest.mark.skipif(shutil.which("bal") is None, reason="Ballerina not installed")
def test_real_compiler_round_trip(tmp_path) -> None:
    from sftgen.validate.bal_compiler import BalCompiler

    c = BalCompiler(ValidateCfg(offline=False), tmp_path)
    ok, bad = c.compile_many(['int x = 5;\nio:println(x);',
                              'import ballerina/io;\npublic function main() { io:printline("x"); }'])
    assert ok.status == "ok" and ok.wrapped
    assert bad.status == "error" and "printline" in bad.errors[0]["message"] and bad.errors[0]["line"] == 2
    assert c.compile('int x = 5;\nio:println(x);').cached


def test_unterminated_string_keeps_line_count() -> None:
    """Regression: the stripper swallowed the newline after an unterminated string literal."""
    code = 'string s = "oops\nint x = 1;\nstring t = "ok";\n'
    assert ballerina.strip_comments_and_strings(code).count("\n") == code.count("\n")
    assert len(ballerina.strip_comments_and_strings(code)) == len(code)
    assert [it.text for it in ballerina.split_top_level(code)][-1] == 'string t = "ok";'
