"""Chunking: code fences are never split, budgets hold, headings drive sections."""

from __future__ import annotations

import re

from conftest import word_count

from sftgen import markdown as md
from sftgen.chunk.markdown_chunker import chunk_markdown

FENCE = re.compile(r"```")


def _fences_balanced(text: str) -> bool:
    return len(FENCE.findall(text)) % 2 == 0


def _service(n: int) -> str:
    return "\n".join(f"    resource function get r{i}() returns string {{\n        return \"{i}\";\n    }}" for i in range(n))


def test_small_doc_single_chunk_with_heading_path() -> None:
    doc = "# Title\n\nIntro paragraph.\n\n```ballerina\nint x = 1;\n```\n"
    drafts = chunk_markdown(doc, "Title", 100, word_count)
    assert len(drafts) == 1
    assert drafts[0].heading_path == ["Title"] and drafts[0].has_code
    assert "```ballerina\nint x = 1;\n```" in drafts[0].text


def test_sections_follow_headings() -> None:
    doc = "# T\n\nA para.\n\n## One\n\nFirst section.\n\n## Two\n\nSecond section.\n\n### Deep\n\nDeep text.\n"
    drafts = chunk_markdown(doc, "T", 200, word_count)
    assert [d.heading_path for d in drafts] == [["T"], ["T", "One"], ["T", "Two"], ["T", "Two", "Deep"]]


def test_code_fence_never_split_when_it_fits() -> None:
    code = "```ballerina\n" + "\n".join(f"int v{i} = {i};" for i in range(30)) + "\n```"
    prose = "\n\n".join("Sentence number %d is here to take space." % i for i in range(12))
    doc = f"# T\n\n{prose}\n\n{code}\n\nAfter the code.\n"
    drafts = chunk_markdown(doc, "T", 160, word_count)
    holding = [d for d in drafts if "int v0 = 0;" in d.text]
    assert len(holding) == 1 and "int v29 = 29;" in holding[0].text
    assert all(_fences_balanced(d.text) for d in drafts)
    assert all(d.token_count <= 160 for d in drafts)


def test_oversized_fence_split_at_declarations_with_imports_repeated() -> None:
    funcs = "\n\n".join(f"function f{i}(int a) returns int {{\n    return a + {i};\n}}" for i in range(40))
    doc = f"# T\n\n```ballerina\nimport ballerina/io;\n\n{funcs}\n```\n"
    drafts = chunk_markdown(doc, "T", 80, word_count)
    assert len(drafts) > 1
    for d in drafts:
        assert _fences_balanced(d.text) and d.text.startswith("```ballerina\nimport ballerina/io;")
        assert d.token_count <= 80
        body = d.text.split("\n", 1)[1]
        assert body.count("function ") == body.count("return a +")  # no function cut in half
    joined = "\n".join(d.text for d in drafts)
    assert all(f"function f{i}(" in joined for i in range(40))


def test_overlap_carries_previous_paragraph() -> None:
    paras = [f"Paragraph {i} " + "word " * 25 for i in range(6)]
    doc = "# T\n\n" + "\n\n".join(paras) + "\n"
    drafts = chunk_markdown(doc, "T", 70, word_count)
    assert len(drafts) >= 2
    first_last_para = drafts[0].text.split("\n\n")[-1]
    assert drafts[1].text.startswith(first_last_para)


def test_service_block_kept_whole_if_it_fits() -> None:
    doc = f"# S\n\n```ballerina\nimport ballerina/http;\n\nservice / on new http:Listener(9090) {{\n{_service(3)}\n}}\n```\n"
    drafts = chunk_markdown(doc, "S", 200, word_count)
    assert len(drafts) == 1 and drafts[0].text.count("resource function") == 3


def test_segment_blocks_kinds() -> None:
    blocks = md.segment_blocks("# A\n\ntext\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |\n\n```bash\nls\n```\n", "A")
    assert [b.kind for b in blocks] == ["heading", "para", "list", "table", "fence"]
    assert blocks[-1].lang == "bash"


def test_single_huge_line_is_split_to_fit() -> None:
    """Regression: a 2k-token minified JSON line in a bash block exceeded the budget."""
    blob = ",".join(f'{{"k{i}":"v{i}"}}' for i in range(400))
    doc = f"# T\n\n```bash\ncurl x\n{blob}\n```\n"
    char_count = lambda s: len(s) // 4 + 1  # noqa: E731 - roughly tokenizer-like, no spaces in blob
    drafts = chunk_markdown(doc, "T", 120, char_count)
    assert len(drafts) > 1 and all(d.token_count <= 120 for d in drafts)
    assert all(_fences_balanced(d.text) for d in drafts)
