"""Pair each Ballerina code block with the prose that explains it.

* bbe_directive : BBE docs — intro = prose before the code, notes = prose after,
                  expected_output = the following ```output fence.
* md_adjacent   : other Markdown — up to 2 prose blocks before the code in the
                  same section, and the next prose block if it explains the code.
* doc_comment   : .bal sources — `#` doc comments above each public declaration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from balpipe_common.schemas import Document

from sftgen import ballerina
from sftgen import markdown as md

PROSE_KINDS = {"para", "list", "quote", "table"}
EXPLAINS_RE = re.compile(r"^(in (this|the above) (code|example)|this (code|example|program|service|function)|here,|the (above|following)|note)", re.I)
FENCE_BODY_RE = re.compile(r"^\s*(`{3,}|~{3,})[^\n]*\n(.*?)\n\s*\1\s*$", re.DOTALL)
PUBLIC_DECL_RE = re.compile(r"^\s*(?:public\s+)(?:isolated\s+|transactional\s+|remote\s+)*(function|type|class|client\s+class|const|enum|annotation)\b")


@dataclass
class Pair:
    code: str
    intro: str
    notes: str
    heading_path: list[str]
    method: str
    expected_output: str | None = None
    ordinal: int = 0
    extra: dict = field(default_factory=dict)


def fence_body(block_text: str) -> str:
    m = FENCE_BODY_RE.match(block_text)
    return m.group(2) if m else block_text


def _same_section(a: md.Block, b: md.Block) -> bool:
    return a.heading_path == b.heading_path


def pairs_from_markdown(doc: Document) -> list[Pair]:
    blocks = md.segment_blocks(doc.markdown, title=doc.title)
    method = "bbe_directive" if doc.doc_type == "bbe" else "md_adjacent"
    out: list[Pair] = []
    for i, b in enumerate(blocks):
        if b.kind != "fence" or b.lang != "ballerina":
            continue
        code = fence_body(b.text).strip("\n")
        if not code.strip():
            continue
        # prose before: walk back within the section, stop at a heading or another code block
        before: list[str] = []
        j = i - 1
        while j >= 0 and len(before) < 2:
            pb = blocks[j]
            if pb.kind == "heading" or (pb.kind == "fence" and pb.lang == "ballerina") or not _same_section(pb, b):
                break
            if pb.kind in PROSE_KINDS:
                before.insert(0, pb.text.strip())
            j -= 1
        # prose / output after
        notes: list[str] = []
        expected = None
        k = i + 1
        while k < len(blocks):
            nb = blocks[k]
            if nb.kind == "heading" or not _same_section(nb, b) or (nb.kind == "fence" and nb.lang == "ballerina"):
                break
            if nb.kind == "fence" and nb.lang == "output" and expected is None:
                expected = fence_body(nb.text).strip("\n")
            elif nb.kind in PROSE_KINDS:
                if method == "bbe_directive" or (not notes and EXPLAINS_RE.match(nb.text.strip())):
                    notes.append(nb.text.strip())
                if method != "bbe_directive" or len(notes) >= 3:
                    break
            k += 1
        if method == "bbe_directive" and not before and doc.description:
            before = [doc.description]
        out.append(Pair(code=code, intro="\n\n".join(before), notes="\n\n".join(notes),
                        heading_path=b.heading_path, method=method, expected_output=expected, ordinal=len(out)))
    return out


def pairs_from_source(doc: Document) -> list[Pair]:
    """Public declarations with their `#` doc comments, from a module_source document."""
    blocks = [b for b in md.segment_blocks(doc.markdown, title=doc.title) if b.kind == "fence" and b.lang == "ballerina"]
    out: list[Pair] = []
    for b in blocks:
        src = fence_body(b.text)
        imports = [it.text.strip() for it in ballerina.split_top_level(src) if it.is_import]
        for item in ballerina.split_top_level(src):
            lines = item.text.split("\n")
            doc_lines = [ln.strip()[1:].strip() for ln in lines if ln.strip().startswith("#")]
            code_lines = [ln for ln in lines if not ln.strip().startswith("#")]
            decl = "\n".join(code_lines).strip()
            if not doc_lines or not PUBLIC_DECL_RE.match(decl):
                continue
            summary = [d for d in doc_lines if not d.startswith("+") and not d.startswith("return")]
            params = [d for d in doc_lines if d.startswith(("+", "return"))]
            if not " ".join(summary).strip():
                continue
            used_imports = [imp for imp in imports if any(
                re.search(rf"\b{re.escape(p)}:", decl) for p in ballerina.module_prefixes(imp))]
            code = "\n".join(used_imports + ([""] if used_imports else []) + [decl])
            out.append(Pair(code=code, intro=" ".join(summary).strip(), notes="\n".join(params),
                            heading_path=[doc.title], method="doc_comment", ordinal=len(out)))
    return out


def extract_pairs(doc: Document) -> list[Pair]:
    if doc.doc_type == "module_source":
        return pairs_from_source(doc)
    return pairs_from_markdown(doc)
