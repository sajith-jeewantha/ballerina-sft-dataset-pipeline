"""Structure-aware Markdown chunker.

Rules:
* sections follow headings; a chunk never spans two sections,
* blocks (paragraph, list, table, code fence) are packed up to a token budget,
* a code fence is never split if it fits; an oversized fence is split at
  top-level Ballerina declarations (imports repeated), and only a single
  declaration larger than the budget is split by lines (marked `// ...`),
* when a section spills into a new chunk, the previous prose block is carried
  over as overlap (only if it fits).

The budget applies to the *embedded* text: `context_header + "\n\n" + body`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sftgen import ballerina
from sftgen import markdown as md

TokenCounter = Callable[[str], int]
PROSE = {"para", "list", "quote", "table", "html"}
FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})([^\n]*)\n(.*?)\n\s*\2\s*$", re.DOTALL)
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
SPLIT_MARGIN = 8


@dataclass
class ChunkDraft:
    heading_path: list[str]
    text: str
    token_count: int
    has_code: bool


def context_header(heading_path: list[str]) -> str:
    return " > ".join(h for h in heading_path if h)


def _split_fence(block: md.Block, budget: int, count: TokenCounter) -> list[str]:
    m = FENCE_RE.match(block.text)
    if not m:
        return _split_lines(block.text, budget, count, "")
    _, ticks, info, code = m.groups()
    wrap = lambda body: f"{ticks}{info}\n{body}\n{ticks}"  # noqa: E731
    items = ballerina.split_top_level(code) if block.lang == "ballerina" else []
    if not items:
        return [wrap(p) for p in _split_lines(code, budget - count(wrap("")), count, "")]
    imports = "\n".join(it.text for it in items if it.is_import)
    decls = [it.text for it in items if not it.is_import]
    prefix = f"{imports}\n\n" if imports else ""
    pieces: list[str] = []
    cur = ""
    for d in decls:
        candidate = f"{cur}\n\n{d}" if cur else d
        if count(wrap(prefix + candidate)) <= budget:
            cur = candidate
            continue
        if cur:
            pieces.append(wrap(prefix + cur))
        if count(wrap(prefix + d)) <= budget:
            cur = d
        else:  # a single declaration bigger than the budget: last-resort line split
            room = budget - count(wrap(prefix))
            pieces.extend(wrap(prefix + part) for part in _split_lines(d, room, count, "// ..."))
            cur = ""
    if cur:
        pieces.append(wrap(prefix + cur))
    return pieces


def _split_long_line(line: str, budget: int, count: TokenCounter) -> list[str]:
    """Break one over-long line (e.g. minified JSON) by words, then by characters."""
    pieces, cur = [], ""
    for word in line.split(" "):
        candidate = f"{cur} {word}" if cur else word
        if count(candidate) <= budget:
            cur = candidate
            continue
        if cur:
            pieces.append(cur)
        if count(word) <= budget:
            cur = word
            continue
        step = max(1, len(word) * budget // max(1, count(word)) - 1)
        parts = [word[k : k + step] for k in range(0, len(word), step)]
        pieces.extend(parts[:-1])
        cur = parts[-1]
    if cur:
        pieces.append(cur)
    return pieces


def _split_lines(text: str, budget: int, count: TokenCounter, marker: str) -> list[str]:
    out, cur = [], []
    lines: list[str] = []
    for line in text.split("\n"):
        lines.extend(_split_long_line(line, budget, count) if count(line) > budget else [line])
    for line in lines:
        candidate = "\n".join([*cur, line])
        if cur and count(candidate) > budget:
            out.append("\n".join(cur) + (f"\n{marker}" if marker else ""))
            cur = [line]
        else:
            cur.append(line)
    if cur:
        out.append("\n".join(cur))
    return out


def _split_prose(text: str, budget: int, count: TokenCounter) -> list[str]:
    out, cur = [], ""
    for sent in SENTENCE_RE.split(text):
        candidate = f"{cur} {sent}".strip()
        if cur and count(candidate) > budget:
            out.append(cur)
            cur = sent
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return [p for piece in out for p in (_split_lines(piece, budget, count, "") if count(piece) > budget else [piece])]


def chunk_markdown(markdown: str, title: str, max_tokens: int, count: TokenCounter) -> list[ChunkDraft]:
    blocks = md.segment_blocks(markdown, title=title)
    # group into sections (a heading starts a new one)
    sections: list[list[md.Block]] = []
    for b in blocks:
        if b.kind == "heading" or not sections or sections[-1][0].heading_path != b.heading_path:
            sections.append([b])
        else:
            sections[-1].append(b)

    drafts: list[ChunkDraft] = []
    for sec in sections:
        path = sec[0].heading_path
        header = context_header(path)
        overhead = count(header + "\n\n") if header else 0
        budget = max(32, max_tokens - overhead - SPLIT_MARGIN)  # margin: tokenizing a join can add tokens
        prefix = f"{header}\n\n" if header else ""

        units: list[tuple[str, str]] = []  # (kind, text) each <= budget
        for b in sec:
            if b.kind == "heading":
                continue  # carried by the context header, not repeated in the body
            t = count(b.text)
            if t <= budget:
                units.append((b.kind, b.text))
            elif b.kind == "fence":
                units.extend(("fence", p) for p in _split_fence(b, budget, count))
            else:
                units.extend((b.kind, p) for p in _split_prose(b.text, budget, count))

        cur: list[tuple[str, str]] = []
        for kind, text in units:
            candidate = "\n\n".join(x for _, x in [*cur, (kind, text)])
            if cur and count(prefix + candidate) > max_tokens:  # check the exact text that gets embedded
                drafts.append(_draft(path, cur, header, count))
                last_kind, last_text = cur[-1]
                overlap = ([(last_kind, last_text)] if last_kind in PROSE
                           and count(f"{prefix}{last_text}\n\n{text}") <= max_tokens else [])
                cur = [*overlap, (kind, text)]
            else:
                cur.append((kind, text))
        if cur:  # a heading with no content produces no chunk
            drafts.append(_draft(path, cur, header, count))
    return drafts


def _draft(path: list[str], units: list[tuple[str, str]], header: str, count: TokenCounter) -> ChunkDraft:
    body = "\n\n".join(t for _, t in units)
    embed = f"{header}\n\n{body}" if header else body
    return ChunkDraft(heading_path=path, text=body, token_count=count(embed), has_code=any(k == "fence" for k, _ in units))
