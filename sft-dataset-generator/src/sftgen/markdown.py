"""Markdown helpers built on markdown-it-py: fence discovery, language labelling,
front matter parsing and block segmentation (used by clean, extract and chunk)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import yaml
from markdown_it import MarkdownIt

from sftgen import ballerina

_MD = MarkdownIt("commonmark", {"html": True}).enable("table")
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
LANG_ALIASES = {
    "bal": "ballerina", "ballerina": "ballerina",
    "sh": "bash", "shell": "bash", "console": "bash", "bash": "bash", "zsh": "bash", "cmd": "bash",
    "yml": "yaml", "yaml": "yaml", "toml": "toml", "json": "json", "xml": "xml", "java": "java",
    "py": "python", "python": "python", "go": "go", "js": "javascript", "javascript": "javascript",
    "ts": "typescript", "typescript": "typescript", "sql": "sql", "graphql": "graphql", "proto": "protobuf",
    "text": "text", "txt": "text", "plaintext": "text", "output": "output", "http": "http", "dockerfile": "dockerfile",
}
SHELL_FIRST = {"bal", "cd", "mkdir", "curl", "docker", "kubectl", "export", "npm", "git", "java", "brew", "sudo",
               "sh", "wget", "cat", "ls", "echo", "pip", "mvn", "gradle", "helm", "kind", "minikube", "set"}


# --------------------------------------------------------------------------- #
# Fence language detection
# --------------------------------------------------------------------------- #
def detect_language(code: str) -> str:
    """Best-effort language for an unlabelled code block."""
    s = code.strip()
    if not s:
        return "text"
    if s[0] in "{[":
        try:
            json.loads(s)
            return "json"
        except ValueError:
            pass
    lines = [ln.strip() for ln in s.split("\n") if ln.strip()]
    first_tok = lines[0].lstrip("$> ").split(" ", 1)[0]
    if lines[0].startswith(("$ ", "> ")) or first_tok in SHELL_FIRST:
        return "bash"
    if ballerina.looks_like_ballerina(s):
        return "ballerina"
    if any(re.match(r"^\[\[?[\w.\-]+\]\]?$", ln) for ln in lines) and sum("=" in ln for ln in lines) >= 1:
        return "toml"
    if s.startswith("<") and s.endswith(">"):
        return "xml"
    if re.search(r"^\s*(package\s+[\w.]+;|import\s+java\.)", s, re.M) or "public static void main" in s:
        return "java"
    if re.search(r"^\s*(def |from \w+ import |import \w+$)", s, re.M) or "print(" in s and ";" not in s:
        return "python"
    if all(re.match(r"^[\w\-\".]+\s*:\s*\S?", ln) or ln.startswith(("-", "#")) for ln in lines) and ";" not in s:
        return "yaml"
    return "text"


def normalize_language(info: str, code: str) -> str:
    label = info.strip().split()[0].lower() if info.strip() else ""
    label = LANG_ALIASES.get(label, label)
    if label in ("", "text"):
        detected = detect_language(code)
        return detected if label == "" or detected != "text" else "text"
    return label


# --------------------------------------------------------------------------- #
# Fences
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Fence:
    info: str
    lang: str
    code: str
    start_line: int  # opening fence line (0-based)
    end_line: int  # exclusive


def iter_fences(markdown: str) -> list[Fence]:
    out = []
    for tok in _MD.parse(markdown):
        if tok.type == "fence" and tok.map:
            out.append(Fence(tok.info, normalize_language(tok.info, tok.content), tok.content, tok.map[0], tok.map[1]))
    return out


def relabel_fences(markdown: str) -> str:
    """Give every fenced block an explicit, normalized language label."""
    lines = markdown.split("\n")
    for f in iter_fences(markdown):
        opening = lines[f.start_line]
        m = re.match(r"^(\s*)(`{3,}|~{3,})(.*)$", opening)
        if not m:
            continue
        current = m.group(3).strip().split()[0].lower() if m.group(3).strip() else ""
        if current != f.lang:
            lines[f.start_line] = f"{m.group(1)}{m.group(2)}{f.lang}"
    return "\n".join(lines)


def fence_counts(markdown: str) -> tuple[int, int]:
    fences = iter_fences(markdown)
    return len(fences), sum(1 for f in fences if f.lang == "ballerina")


# --------------------------------------------------------------------------- #
# Front matter
# --------------------------------------------------------------------------- #
def split_front_matter(text: str) -> tuple[dict, str]:
    m = FRONT_MATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {}
    return (meta if isinstance(meta, dict) else {}), text[m.end():]


# --------------------------------------------------------------------------- #
# Block segmentation
# --------------------------------------------------------------------------- #
@dataclass
class Block:
    kind: str  # heading | fence | para | list | table | html | quote | hr
    text: str
    level: int = 0  # heading level
    lang: str = ""  # fence language
    heading_path: list[str] = field(default_factory=list)


_OPEN_KIND = {
    "paragraph_open": "para", "bullet_list_open": "list", "ordered_list_open": "list", "table_open": "table",
    "blockquote_open": "quote", "html_block": "html", "hr": "hr", "fence": "fence", "code_block": "fence",
    "heading_open": "heading",
}


def _dedup_path(path: list[tuple[int, str]]) -> list[str]:
    out: list[str] = []
    for _, h in path:
        if h and (not out or out[-1].strip().lower() != h.strip().lower()):
            out.append(h)
    return out


def segment_blocks(markdown: str, title: str | None = None) -> list[Block]:
    """Split Markdown into top-level blocks, each tagged with its heading path."""
    lines = markdown.split("\n")
    tokens = _MD.parse(markdown)
    blocks: list[Block] = []
    path: list[tuple[int, str]] = [(0, title)] if title else []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.level != 0 or tok.type not in _OPEN_KIND or not tok.map:
            i += 1
            continue
        kind = _OPEN_KIND[tok.type]
        text = "\n".join(lines[tok.map[0] : tok.map[1]]).rstrip()
        if kind == "heading":
            level = int(tok.tag[1])
            heading = tokens[i + 1].content.strip() if i + 1 < len(tokens) else ""
            path = [p for p in path if p[0] < level] + [(level, heading)]
            blocks.append(Block("heading", text, level=level, heading_path=_dedup_path(path)))
        elif kind == "fence":
            lang = normalize_language(tok.info, tok.content) if tok.type == "fence" else detect_language(tok.content)
            blocks.append(Block("fence", text, lang=lang, heading_path=_dedup_path(path)))
        else:
            blocks.append(Block(kind, text, heading_path=_dedup_path(path)))
        i += 1
    return blocks


def plain_words(markdown: str) -> int:
    return len(re.findall(r"\w+", markdown))
