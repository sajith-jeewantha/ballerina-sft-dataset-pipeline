"""Cheap, deterministic checks on generated answers (run before the paid judge)."""

from __future__ import annotations

import re

from sftgen import ballerina
from sftgen import markdown as md

CONTEXT_LEAK_RE = re.compile(
    r"\b(based on (the|this) (provided |given )?(context|documentation|reference|excerpt|material|text)|"
    r"(the|this) (provided|given) (context|documentation|reference|excerpt|material)|"
    r"according to the (context|documentation provided|reference|excerpt|provided)|"
    r"in the (provided|given) (context|documentation|reference|excerpt)|"
    r"the context (does not|doesn't|provided)|reference material)\b",
    re.IGNORECASE,
)


def mentions_context(answer: str) -> bool:
    return bool(CONTEXT_LEAK_RE.search(answer))


def ballerina_blocks(answer: str) -> list[str]:
    return [f.code.strip("\n") for f in md.iter_fences(answer) if f.lang == "ballerina" and f.code.strip()]


def unknown_symbols(code_blocks: list[str], known: set[str]) -> list[str]:
    """`prefix:name` references whose module prefix is known but the symbol is not."""
    known_prefixes = {s.split(":", 1)[0] for s in known}
    found: set[str] = set()
    for code in code_blocks:
        for sym in ballerina.symbols(code):
            if sym.split(":", 1)[0] in known_prefixes and sym not in known:
                found.add(sym)
    return sorted(found)
