"""Ballerina heuristics, compiler helpers and answer code extraction."""

from __future__ import annotations

import shutil

import pytest

from balpipe_common.config import ValidateCfg

from sftgen import ballerina
from sftgen.validate.bal_compiler import parse_errors, prepare


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


def test_ballerina_blocks_from_answer() -> None:
    from sftgen.validate.stage import ballerina_blocks

    answer = "Use this:\n\n```ballerina\nint x = 1;\n```\n\nRun it:\n\n```bash\nbal run\n```\n"
    assert ballerina_blocks(answer) == ["int x = 1;"]
