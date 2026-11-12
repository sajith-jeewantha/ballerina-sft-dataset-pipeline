"""Cleaning: language labelling, extractors, relevance and dedup."""

from __future__ import annotations

import json

from balpipe_common.config import Settings
from conftest import make_raw

from sftgen import markdown as md
from sftgen.clean import extractors as ex
from sftgen.clean.dedup import content_sha256, dedup
from sftgen.clean.stage import is_relevant

HELLO_BAL = 'import ballerina/io;\n\npublic function main() {\n    io:println("Hello, World!");\n}\n'


# ----------------------------------------------------------------- fence language
def test_detect_language() -> None:
    assert md.detect_language(HELLO_BAL) == "ballerina"
    assert md.detect_language("type R record {|\n    int id;\n|};") == "ballerina"
    assert md.detect_language("$ bal run hello.bal\nHello") == "bash"
    assert md.detect_language("bal new greeter") == "bash"
    assert md.detect_language('{"a": 1}') == "json"
    assert md.detect_language('[package]\norg = "x"\nname = "y"') == "toml"
    assert md.detect_language("Hello, World!") == "text"


def test_relabel_fences_labels_unlabelled_and_aliases() -> None:
    src = f"Intro\n\n```\n{HELLO_BAL}```\n\n```bal\nint x = 1;\n```\n\n```\n$ bal run x.bal\n```\n"
    out = md.relabel_fences(src)
    assert [f.lang for f in md.iter_fences(out)] == ["ballerina", "ballerina", "bash"]
    assert "```ballerina\nimport ballerina/io;" in out
    assert '    io:println("Hello, World!");' in out  # indentation preserved


# ------------------------------------------------------------------- extractors
def test_bbe_assembly_inlines_code_and_output() -> None:
    repo = "ballerina-platform/ballerina-distribution"
    files = {
        "hello_world.md": make_raw("# Hello world main\n\nLet's write it.\n\n::: code hello_world.bal :::\n\nRun it.\n\n::: out hello_world.out :::\n",
                                   repo=repo, path="examples/hello-world/hello_world.md"),
        "hello_world.bal": make_raw(HELLO_BAL, repo=repo, path="examples/hello-world/hello_world.bal",
                                    media_type="text/x-ballerina"),
        "hello_world.out": make_raw("$ bal run hello_world.bal\nHello, World!\n", repo=repo,
                                    path="examples/hello-world/hello_world.out", media_type="text/plain"),
        "hello_world.metatags": make_raw('description: The classic "Hello, World" program\nkeywords: a, b\n',
                                         repo=repo, path="examples/hello-world/hello_world.metatags"),
    }
    index = ex.parse_bbe_index(json.dumps([{"title": "Hello World", "category": "Language concepts", "samples": [
        {"name": "Hello world main", "url": "hello-world", "verifyBuild": True, "verifyOutput": True, "needCentral": False}]}]))
    item = ex.extract_bbe("hello-world", files, index)
    assert item is not None
    assert "::: code" not in item.markdown and "```ballerina\nimport ballerina/io;" in item.markdown
    assert "```output\n$ bal run hello_world.bal\nHello, World!\n```" in item.markdown
    assert item.title == "Hello world main" and item.category == "Language concepts"
    assert item.canonical_url == "https://ballerina.io/learn/by-example/hello-world/"
    assert item.bbe and item.bbe.verify_output is True
    assert item.description and item.description.startswith("The classic")


def test_github_markdown_front_matter() -> None:
    body = ("---\ntitle: Type-safe client API\ndescription: Generated client.\n"
            "permalink: /learn/persist-client-api/\nintro: The generated client API is used for CRUD.\n---\n\n"
            "## Derived types\n\n```ballerina\ntype W record {|\n    string id;\n|};\n```\n")
    item = ex.extract_github_markdown(make_raw(body, path="swan-lake/development-tutorials/persist/a.md"))
    assert item.title == "Type-safe client API"
    assert item.canonical_url == "https://ballerina.io/learn/persist-client-api/"
    assert item.markdown.startswith("# Type-safe client API\n\nThe generated client API")
    assert item.doc_type == "tutorial" and "---" not in item.markdown.split("\n")[0]


def test_nextjs_extractor_recovers_code_trafilatura_misses(fixtures) -> None:
    raw = make_raw((fixtures / "bbe_nextjs.html").read_text(), source="web", media_type="text/html",
                   uri="https://ballerina.io/learn/by-example/x/")
    item = ex.extract_html(raw)
    assert item and item.extractor == "nextjs_data"
    assert "```ballerina\nimport ballerina/" in item.markdown
    assert "<span" not in item.markdown


def test_trafilatura_markdown_keeps_and_labels_code() -> None:
    html = ("<html><head><title>Writing a service - The Ballerina programming language</title></head><body>"
            "<nav><a href='/a'>Home</a><a href='/b'>Docs</a></nav><article><h1>Writing a service</h1>"
            "<p>Ballerina services are first-class constructs. This tutorial shows how to write one, with a resource "
            "function that returns a greeting. Read the code below carefully and run it locally.</p>"
            '<pre class="shiki"><code>import ballerina/http;\n\nservice / on new http:Listener(8080) {\n'
            '    resource function get greeting() returns string {\n        return "Hello";\n    }\n}</code></pre>'
            "<p>In this code, the service listens on port 8080 and returns a greeting for every GET request.</p>"
            "</article><footer>Copyright</footer></body></html>")
    item = ex.extract_html(make_raw(html, source="web", media_type="text/html", uri="https://blog.ballerina.io/posts/x"))
    assert item and item.extractor == "trafilatura_md" and item.doc_type == "blog"
    fences = md.iter_fences(item.markdown)
    assert [f.lang for f in fences] == ["ballerina"]
    assert "    resource function get greeting()" in fences[0].code  # indentation intact
    assert item.title == "Writing a service"


def test_central_readme() -> None:
    pkg = {"organization": "ballerina", "name": "http", "version": "2.1.0", "summary": "HTTP APIs",
           "readme": "## Overview\n\n```ballerina\nhttp:Client c = check new (\"x\");\n```", "ballerinaVersion": "2201.14.0"}
    raw = make_raw(json.dumps(pkg), source="central", media_type="application/json",
                   uri="https://central.ballerina.io/ballerina/http/2.1.0")
    item = ex.extract_central(raw)
    assert item and item.title == "ballerina/http" and item.doc_type == "api_readme"
    assert item.ballerina_version == "2201.14.0" and "```ballerina" in item.markdown


# ------------------------------------------------------------------- relevance
def test_off_topic_blog_is_rejected() -> None:
    s = Settings()
    off = ex.Extracted(raw=make_raw("x", source="web", uri="https://blog.ballerina.io/p"),
                       markdown="# Ten languages to learn\n\n" + "Rust Go Python Ballerina are languages. " * 20,
                       title="t", doc_type="blog", extractor="trafilatura_md")
    assert is_relevant(off, s) == "off_topic"
    on = ex.Extracted(raw=off.raw, markdown=off.markdown + f"\n```ballerina\n{HELLO_BAL}```\n",
                      title="t", doc_type="blog", extractor="trafilatura_md")
    assert is_relevant(on, s) is None
    legal = ex.Extracted(raw=make_raw("x", source="web", uri="https://ballerina.io/privacy-policy/"),
                         markdown=on.markdown, title="t", doc_type="other", extractor="x")
    assert is_relevant(legal, s) == "excluded_url"


# ----------------------------------------------------------------------- dedup
def test_dedup_exact_near_and_canonical_priority() -> None:
    base = " ".join(f"word{i}" for i in range(400))
    items = [
        {"id": "web1", "src": "web", "text": base},
        {"id": "git1", "src": "github", "text": base},  # exact dup of web1; github wins
        {"id": "git2", "src": "github", "text": base.replace("word7 ", "changed ")},  # near dup
        {"id": "other", "src": "web", "text": " ".join(f"zz{i}" for i in range(400))},
    ]
    rank = {"github": 0, "central": 1, "web": 2}
    res = dedup(items, key=lambda x: x["id"], text=lambda x: x["text"], rank=lambda x: (rank[x["src"]], x["id"]),
                threshold=0.8)
    assert res["git1"].status == "unique"
    assert res["web1"].status == "exact_dup" and res["web1"].duplicate_of == "git1"
    assert res["git2"].status == "near_dup" and res["git2"].duplicate_of == "git1"
    assert res["other"].status == "unique"
    assert content_sha256("a  b\n") == content_sha256("a b")


def test_doc_identity_is_stable_across_commits() -> None:
    from balpipe_common.schemas import GitInfo

    from sftgen.clean.stage import stable_identity_url

    a = make_raw("x", path="swan-lake/a.md")
    b = a.model_copy(update={"source_uri": a.source_uri.replace("/abc/", "/def/"),
                             "git": GitInfo(repo=a.git.repo, ref="master", commit_sha="def", path="swan-lake/a.md")})  # type: ignore[union-attr]
    ia = ex.Extracted(raw=a, markdown="m", title="t", doc_type="guide", extractor="x")
    ib = ex.Extracted(raw=b, markdown="m", title="t", doc_type="guide", extractor="x")
    assert stable_identity_url(ia) == stable_identity_url(ib)


def test_nextjs_extractor_ignores_non_bbe_pages() -> None:
    """Regression: the blog is also Next.js but its `codes` key is a string, not a code list."""
    html = ('<html><head><title>Post - Ballerina</title></head><body><article><p>' + "Real prose here. " * 40 +
            '</p></article><script id="__NEXT_DATA__" type="application/json">'
            '{"props": {"pageProps": {"codes": "[[1,2],[3]]", "frontmatter": {"title": "Post"}}}}</script></body></html>')
    raw = make_raw(html, source="web", media_type="text/html", uri="https://blog.ballerina.io/posts/p")
    assert ex.extract_nextjs(raw) is None
    item = ex.extract_html(raw)
    assert item and item.extractor == "trafilatura_md" and md.fence_counts(item.markdown) == (0, 0)


def test_spec_html_keeps_every_pre_and_labels_grammar() -> None:
    html = ("<html><body><h1>Ballerina Language Specification, 2024R1</h1><p>Contributors:</p>"
            "<ul><li>Jane <a href='mailto:jane@example.org'>jane@example.org</a>"  # unclosed list nests the rest
            "<h2>Table of contents</h2><p>1. Intro</p>"
            "<h2 id='types'>5. Types</h2><p>A <code>record</code> type has <em>fields</em>.</p>"
            "<pre class='grammar'><code>record-type-descriptor := inclusive-record | exclusive-record</code></pre>"
            "<pre>type Person record {|\n    string name;\n|};</pre>"
            "<pre class='langlib'>public isolated function length(string s) returns int = external;</pre>"
            "</body></html>")
    raw = make_raw(html, media_type="text/html", path="public/spec/lang/2024R1/index.html")
    item = ex.extract_spec_html(raw)
    assert item.doc_type == "spec" and item.canonical_url == "https://ballerina.io/spec/lang/2024R1/"
    assert [f.lang for f in md.iter_fences(item.markdown)] == ["ebnf", "ballerina", "ballerina"]
    assert "A `record` type has *fields*." in item.markdown
    assert "## 5. Types" in item.markdown and "Table of contents" not in item.markdown
    assert "jane@example.org" not in item.markdown


def test_eip_bundle_pairs_yaml_description_with_code() -> None:
    base = "pages/learn/enterprise-integration-patterns/enterprise-integration-patterns/aggregator"
    files = {
        "aggregator.yml": make_raw("tagline: Wait for a sequence of messages and combine them\n"
                                   "desc: Aggregator collects messages and combines them.\ncategory: Message Routing\n"
                                   "helps: Ballerina provides map and table structures for message storage.\n",
                                   path=f"{base}/aggregator.yml", media_type="text/plain"),
        "aggregator.bal": make_raw(HELLO_BAL, path=f"{base}/aggregator.bal", media_type="text/x-ballerina"),
    }
    item = ex.extract_eip("aggregator", files)
    assert item and item.canonical_url == "https://ballerina.io/learn/enterprise-integration-patterns/aggregator/"
    assert item.category == "Message Routing" and item.doc_type == "guide"
    assert "message storage" in item.markdown and [f.lang for f in md.iter_fences(item.markdown)] == ["ballerina"]
    from sftgen.extract.pairing import extract_pairs
    from balpipe_common.schemas import Document, ExtractorInfo, License

    doc = Document(doc_id="doc_e", raw_id="r", run_id="r", source="github", source_uri="u", doc_type="guide",
                   title=item.title, markdown=item.markdown, content_sha256="x", license=License(), split="train",
                   extractor=ExtractorInfo(name="eip_bundle", version="1"), created_at="t")
    (pair,) = extract_pairs(doc)
    assert "Aggregator collects" in pair.intro and "map and table structures" in pair.intro


def test_module_spec_gets_spec_type_and_canonical_url() -> None:
    item = ex.extract_github_markdown(make_raw("# Specification: Ballerina HTTP Library\n\nThe HTTP module ...\n",
                                               path="public/spec/http/spec.md"))
    assert item.doc_type == "spec" and item.canonical_url == "https://ballerina.io/spec/http/"
