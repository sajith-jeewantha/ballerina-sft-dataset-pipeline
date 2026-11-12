"""Lightweight, dependency-free analysis of Ballerina source text.

These are heuristics used for routing and metadata (code form, imports,
symbols, top-level splitting). The compiler (`validate/bal_compiler.py`) is the
authority on correctness.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

IMPORT_RE = re.compile(r"^\s*import\s+([\w.]+/[\w.]+)(?:\s+as\s+(\w+))?\s*;", re.MULTILINE)
MAIN_RE = re.compile(r"\bpublic\s+(?:isolated\s+)?function\s+main\s*\(")
SERVICE_RE = re.compile(r"^\s*(?:isolated\s+)?service\b[^{;]*\bon\b", re.MULTILINE)
QUALIFIED_RE = re.compile(r"(?<![\w.$])([a-z_]\w*):([A-Za-z_]\w*)")
_ANNOT_HEAD = re.compile(r"@[\w:]+\s*")


def _is_annotation_only(clean_body: str) -> bool:
    """True if the text is just `@annot { ... }` (optionally preceded by doc lines)."""
    lines = [ln for ln in clean_body.split("\n") if ln.strip() and not ln.strip().startswith("#")]
    text = "\n".join(lines).strip()
    while text:
        m = _ANNOT_HEAD.match(text)
        if not m:
            return False
        rest = text[m.end():]
        if not rest.startswith("{"):
            text = rest.strip()
            continue
        depth = 0
        for i, ch in enumerate(rest):
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                text = rest[i + 1 :].strip()
                break
        else:
            return False
    return True

DECL_START = re.compile(
    r"^(?:@|#|//|import\b|public\b|private\b|isolated\b|function\b|type\b|class\b|const\b|configurable\b|"
    r"enum\b|annotation\b|final\b|listener\b|service\b|xmlns\b|client\b|distinct\b|readonly\b|transactional\b|"
    r"[\w:]+(?:<[^>]*>)?(?:\[\])*[?]?\s+\w+\s*(?:=|;))"
)
STATEMENT_START = re.compile(
    r"^(?:if|else|foreach|while|return|match|do|lock|transaction|retry|panic|fail|break|continue|check|checkpanic|"
    r"wait|fork|worker)\b|^[a-z_]\w*(?:[.:]\w+)*\s*(?:\(|\+=|-=|=(?!=)|->)"
)

BALLERINA_SIGNALS = [
    re.compile(p, re.MULTILINE)
    for p in (
        r"^\s*import\s+ballerinax?/",
        r"\bresource\s+function\b",
        r"\bservice\s+[^{;]*\bon\s+(?:new\s+)?\w+:",
        r"\|\}",
        r"\brecord\s*\{\|",
        r"\bfunction\s+\w+\s*\([^)]*\)\s*(?:returns\b|\{)",
        r"\b(?:io|log|http):\w+\(",
        r"\bcheck\s+\w",
        r"\breturns\s+[\w:|?()\[\]]+",
        r"\bisolated\s+(?:function|service|class)\b",
        r"\bpublic\s+function\s+main\b",
        r"->\w+\(",
    )
]


def strip_comments_and_strings(code: str) -> str:
    """Replace string/template literals and comments with spaces, keeping newlines and length."""
    out: list[str] = []
    i, n = 0, len(code)
    while i < n:
        ch = code[i]
        if code.startswith("//", i):
            j = code.find("\n", i)
            j = n if j == -1 else j
            out.append(" " * (j - i))
            i = j
        elif ch == '"':
            j = i + 1
            while j < n and code[j] not in '"\n':
                j += 2 if code[j] == "\\" and j + 1 < n and code[j + 1] != "\n" else 1
            j = min(j, n)
            end = j + 1 if j < n and code[j] == '"' else j  # never consume the newline of an unterminated string
            out.append('"' + " " * max(0, end - i - 2) + ('"' if end - i >= 2 else ""))
            i = end
        elif ch == "`":
            j = code.find("`", i + 1)
            j = n if j == -1 else j + 1
            seg = code[i:j]
            out.append("".join("\n" if c == "\n" else " " for c in seg))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass(frozen=True)
class TopLevelItem:
    text: str
    start_line: int  # 0-based, inclusive
    end_line: int  # 0-based, inclusive
    is_import: bool


def split_top_level(code: str) -> list[TopLevelItem]:
    """Split source into top-level items (imports, declarations, stray statements).

    Items end at a depth-0 `;` or at the `}` closing a depth-1 block. Leading
    comment/doc/annotation lines attach to the following item.
    """
    lines = code.split("\n")
    clean_lines = strip_comments_and_strings(code).split("\n")
    items: list[TopLevelItem] = []
    depth, start = 0, None
    for idx, (raw, clean) in enumerate(zip(lines, clean_lines, strict=True)):
        if start is None:
            if not raw.strip():
                continue
            start = idx
        for ch in clean:
            if ch in "{[(":
                depth += 1
            elif ch in "}])":
                depth = max(0, depth - 1)
        if depth != 0 or not clean.rstrip().endswith((";", "}")):
            continue  # doc comments (#), // comments and open blocks keep accumulating
        body = "\n".join(clean_lines[start : idx + 1])
        if _is_annotation_only(body):
            continue  # `@annot {..}` belongs to the declaration that follows
        text = "\n".join(lines[start : idx + 1])
        items.append(TopLevelItem(text, start, idx, bool(IMPORT_RE.match(text))))
        start = None
    if start is not None:
        text = "\n".join(lines[start:]).rstrip()
        if text.strip():
            items.append(TopLevelItem(text, start, len(lines) - 1, bool(IMPORT_RE.match(text))))
    return items


def code_form(code: str) -> str:
    """'program' (has main/service), 'declarations' (module-level items only) or 'fragment'."""
    clean = strip_comments_and_strings(code)
    if MAIN_RE.search(clean) or SERVICE_RE.search(clean):
        return "program"
    for item in split_top_level(code):
        first = next(
            (ln.strip() for ln in strip_comments_and_strings(item.text).split("\n") if ln.strip()), ""
        )
        if not first:
            continue
        if STATEMENT_START.match(first) or not DECL_START.match(first):
            return "fragment"
    return "declarations"


def imports(code: str) -> list[str]:
    return sorted({m.group(1) for m in IMPORT_RE.finditer(code)})


def module_prefixes(code: str) -> dict[str, str]:
    """prefix -> module, e.g. {'http': 'ballerina/http', 'regexp': 'ballerina/lang.regexp'}."""
    out: dict[str, str] = {}
    for m in IMPORT_RE.finditer(code):
        module, alias = m.group(1), m.group(2)
        prefix = alias or module.split("/")[-1].split(".")[-1]
        out[prefix] = module
    return out


def symbols(code: str) -> list[str]:
    """Qualified references `prefix:name` whose prefix is an imported module."""
    prefixes = module_prefixes(code)
    clean = strip_comments_and_strings(code)
    found = {f"{m.group(1)}:{m.group(2)}" for m in QUALIFIED_RE.finditer(clean) if m.group(1) in prefixes}
    return sorted(found)


def signal_count(text: str) -> int:
    return sum(1 for rx in BALLERINA_SIGNALS if rx.search(text))


def looks_like_ballerina(code: str) -> bool:
    return signal_count(code) >= 2 or (signal_count(code) == 1 and code.count(";") >= 1)
