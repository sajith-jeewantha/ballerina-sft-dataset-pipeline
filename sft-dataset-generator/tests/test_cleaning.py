"""Cleaning: every source shape -> clean Markdown with code kept exactly."""

from __future__ import annotations

import json

from sftgen import markdown as md
from sftgen.clean import extractors as ex

HELLO_BAL = 'import ballerina/io;\n\npublic function main() {\n    io:println("Hello, World!");\n}\n'


def _next_page(props: dict) -> str:
    data = json.dumps({"props": {"pageProps": props}})
    return f'<html><head><title>X - The Ballerina programming language</title></head><body><div id="__next"></div>' \
           f'<script id="__NEXT_DATA__" type="application/json">{data}</script></body></html>'


# ----------------------------------------------------------------- fence language
def test_detect_language() -> None:
    assert md.detect_language(HELLO_BAL) == "ballerina"
    assert md.detect_language("type R record {|\n    int id;\n|};") == "ballerina"
    assert md.detect_language("$ bal run hello.bal\nHello") == "bash"
    assert md.detect_language('{"a": 1}') == "json"
    assert md.detect_language("Hello, World!") == "text"


def test_relabel_fences_labels_unlabelled_and_aliases() -> None:
    src = f"Intro\n\n```\n{HELLO_BAL}```\n\n```bal\nint x = 1;\n```\n\n```\n$ bal run x.bal\n```\n"
    out = md.relabel_fences(src)
    assert [f.lang for f in md.iter_fences(out)] == ["ballerina", "ballerina", "bash"]
    assert '    io:println("Hello, World!");' in out  # indentation preserved


# ------------------------------------------------------------ Ballerina by Example
def test_bbe_bundle_inlines_code_and_output_exactly() -> None:
    bundle = {
        "hello-world.md": "# Hello world\n\nPrints a greeting.\n\n::: code hello_world.bal :::\n\n"
                          "Run it with `bal run`.\n\n::: out hello_world.out :::\n",
        "hello_world.bal": HELLO_BAL,
        "hello_world.out": "$ bal run hello_world.bal\nHello, World!\n",
        "hello-world.metatags": "description: A first program.\nkeywords: ballerina, hello\n",
    }
    got = ex.extract_bbe_bundle(json.dumps(bundle))
    assert got is not None and got.title == "Hello world"
    assert f"```ballerina\n{HELLO_BAL.rstrip()}\n```" in got.markdown  # verbatim code
    assert "```output\n$ bal run hello_world.bal\nHello, World!\n```" in got.markdown
    order = [got.markdown.index(s) for s in ("Prints a greeting", "```ballerina", "Run it with", "```output")]
    assert order == sorted(order)  # explanation and code stay in their original order
    assert "A first program." in got.markdown


def test_bbe_page_fallback_recovers_code(fixtures) -> None:
    got = ex.extract_bbe_page((fixtures / "bbe_nextjs.html").read_text(encoding="utf-8"))
    assert got is not None and "```ballerina" in got.markdown and "<span" not in got.markdown


# ----------------------------------------------------------------- learn pages
def test_learn_page_uses_embedded_markdown() -> None:
    content = "\n## Indentation\n\n* Use four spaces.\n\n```ballerina\npublic function main() {\n}\n```\n"
    html = _next_page({"frontmatter": {"title": "Coding conventions", "description": "Style rules."},
                       "content": content, "codes": "[[1, \"<pre>...</pre>\"]]"})
    got = ex.extract_learn(html, "https://ballerina.io/learn/style-guide/coding-conventions/")
    assert got is not None and got.extractor == "nextjs_markdown" and got.title == "Coding conventions"
    assert got.markdown.startswith("# Coding conventions\n\nStyle rules.")
    assert "```ballerina\npublic function main() {\n}\n```" in got.markdown


def test_integration_pattern_page() -> None:
    html = _next_page({"name": "Content-Based Router", "tagline": "Route by content", "desc": "Routes messages.",
                       "helps": "Use match statements.", "category": "Routing",
                       "content": [{"name": "router.bal", "raw": HELLO_BAL, "headerCode": "<pre>x</pre>"}]})
    got = ex.extract_learn(html, "https://ballerina.io/learn/enterprise-integration-patterns/content-based-router/")
    assert got is not None and got.extractor == "nextjs_pattern"
    assert "Use match statements." in got.markdown and f"```ballerina\n{HELLO_BAL.rstrip()}\n```" in got.markdown


def test_html_without_embedded_data_falls_back_to_trafilatura() -> None:
    body = "<p>" + "Ballerina services expose resources over HTTP. " * 8 + "</p>"
    html = f"<html><head><title>Services - Ballerina</title></head><body><article><h1>Services</h1>{body}" \
           f"<pre><code>{HELLO_BAL}</code></pre></article></body></html>"
    got = ex.extract_learn(html, "https://ballerina.io/learn/x/")
    assert got is not None and got.extractor == "trafilatura" and "Ballerina services" in got.markdown


# ----------------------------------------------------------------------- spec
def test_spec_keeps_every_pre_and_labels_grammar() -> None:
    html = ("<html><body><h1>Ballerina Language Specification</h1><h2>Table of contents</h2><ul><li>skip</li></ul>"
            "<h2>1. Introduction</h2><p>Ballerina is <code>statically</code> typed.</p>"
            "<pre class='grammar'>int-literal := DecimalNumber</pre><h3>1.1 Example</h3>"
            "<pre>int x = 1;\nstring s = \"a\";</pre><p>Contact a@b.org</p></body></html>")
    got = ex.extract_spec(html)
    assert got is not None and got.title == "Ballerina Language Specification"
    assert "```ebnf\nint-literal := DecimalNumber\n```" in got.markdown
    assert "## 1. Introduction" in got.markdown and "### 1.1 Example" in got.markdown
    assert "skip" not in got.markdown and "[email removed]" in got.markdown
    assert "`statically`" in got.markdown


# ---------------------------------------------------------------------- Central
def test_central_readme_and_api_reference() -> None:
    body = {
        "package": {"organization": "ballerina", "name": "io", "version": "1.8.0", "summary": "File and console I/O.",
                    "readme": "# Overview\n\nRead and write files.\n\n## Usage\n\nCall `io:fileReadString`."},
        "api_docs": [{"id": "io", "description": "I/O APIs.", "functions": [{
            "name": "fileReadString", "qualifiers": ["isolated"], "description": "Reads a file as a string.",
            "parameters": [{"name": "path", "type": "string", "default": None, "description": "File path"}],
            "returns": [{"type": "string|io:Error", "description": "The content"}]}],
            "records": [{"name": "Options", "description": "Read options.",
                         "fields": [{"name": "size", "type": "int", "description": "Block size"}]}]}],
    }
    got = ex.extract_central(json.dumps(body))
    assert got is not None and got.title == "ballerina/io"
    assert got.markdown.startswith("# ballerina/io\n\nFile and console I/O.")
    assert "## Overview" in got.markdown and "### Usage" in got.markdown  # README headings demoted below the title
    assert "```ballerina\nisolated function fileReadString(string path) returns string|io:Error\n```" in got.markdown
    assert "- `size` (`int`): Block size" in got.markdown


def test_central_module_description_repeating_readme_is_dropped() -> None:
    readme = "## Overview\n\nThe HTTP module provides clients and listeners."
    body = {"package": {"organization": "ballerina", "name": "http", "version": "2", "readme": readme},
            "api_docs": [{"id": "http", "description": readme, "functions": []},
                         {"id": "http.httpscerr", "description": "## Errors\n\nError types for HTTP status codes."}]}
    got = ex.extract_central(json.dumps(body))
    assert got is not None and got.markdown.count("The HTTP module provides") == 1
    assert "### Errors" in got.markdown  # another module's own description is kept, under its module heading


# --------------------------------------------------------------- GitHub modules
def test_github_bal_file_is_one_fenced_block() -> None:
    got = ex.extract_github_file(HELLO_BAL, {"repo": "ballerina-platform/module-ballerinax-kafka",
                                             "path": "ballerina/client.bal"})
    assert got is not None and got.title == "ballerinax/kafka: client.bal"
    assert f"```ballerina\n{HELLO_BAL.rstrip()}\n```" in got.markdown


def test_github_readme_title_gets_module_name() -> None:
    got = ex.extract_github_file("# Overview\n\nThe HTTP module.\n", {"repo": "ballerina-platform/module-ballerina-http",
                                                                       "path": "README.md"})
    assert got is not None and got.title == "ballerina/http: Overview"


def test_demote_headings_skips_code() -> None:
    src = "# A\n\n```bash\n# not a heading\n```\n\n## B\n"
    assert md.demote_headings(src, 2) == "## A\n\n```bash\n# not a heading\n```\n\n### B\n"
