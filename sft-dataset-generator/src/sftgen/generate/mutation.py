"""Deterministic bug injection for `debug_fix` tasks.

The *answer* to a debug_fix task is the original, compiler-verified code, so it
is correct by construction. A mutation is only accepted if the compiler really
rejects the mutated code; its actual error message goes into the question.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from sftgen import ballerina


@dataclass(frozen=True)
class Mutant:
    kind: str
    code: str
    explanation: str


MISSPELL = {"println": "printline", "print": "prnt", "fromString": "parseString", "toString": "toStr",
            "length": "size", "push": "append", "keys": "keySet", "get": "fetch"}


def _misspell_call(code: str) -> Mutant | None:
    clean = ballerina.strip_comments_and_strings(code)
    for m in re.finditer(r"\b([a-z]\w*):(\w+)\(", clean):
        prefix, fn = m.group(1), m.group(2)
        if fn in MISSPELL:
            start = m.start(2)
            bad = MISSPELL[fn]
            mutated = code[:start] + bad + code[start + len(fn):]
            return Mutant("misspelled_function", mutated,
                          f"`{prefix}:{bad}` does not exist in the `{prefix}` module; the function is `{prefix}:{fn}`.")
    return None


def _drop_check(code: str) -> Mutant | None:
    clean = ballerina.strip_comments_and_strings(code)
    m = re.search(r"=\s*check\s+", clean)
    if not m:
        return None
    s = m.start() + clean[m.start():].index("check")
    mutated = code[:s] + code[s + len("check "):]
    return Mutant("missing_check", mutated,
                  "The called expression can return an `error`, but its result is assigned to a variable whose type "
                  "does not allow `error`. Use `check` to propagate the error (the enclosing function must return "
                  "`error?` or a union with `error`).")


def _wrong_literal(code: str) -> Mutant | None:
    clean = ballerina.strip_comments_and_strings(code)
    m = re.search(r"\bint\s+(\w+)\s*=\s*(\d+)\s*;", clean)
    if not m:
        return None
    mutated = code[: m.start(2)] + f'"{m.group(2)}"' + code[m.end(2):]
    return Mutant("type_mismatch", mutated,
                  f"`{m.group(1)}` is declared as `int`, but a `string` literal is assigned to it. "
                  "Ballerina does not convert implicitly between `string` and `int`; assign an `int` value.")


def _missing_semicolon(code: str) -> Mutant | None:
    lines = code.split("\n")
    clean_lines = ballerina.strip_comments_and_strings(code).split("\n")
    for i, (ln, cl) in enumerate(zip(lines, clean_lines, strict=True)):
        if cl.rstrip().endswith(";") and not cl.strip().startswith(("import", "}")) and ln.startswith("    "):
            j = ln.rstrip().rfind(";")
            lines[i] = ln[:j] + ln[j + 1:]
            return Mutant("missing_semicolon", "\n".join(lines),
                          f"The statement on line {i + 1} is missing its terminating semicolon (`;`).")
    return None


MUTATORS: list[Callable[[str], Mutant | None]] = [_misspell_call, _drop_check, _wrong_literal, _missing_semicolon]


def candidate_mutants(code: str, start: int = 0) -> list[Mutant]:
    """All applicable mutations, rotated by `start` so different seeds get different bug kinds."""
    order = MUTATORS[start % len(MUTATORS):] + MUTATORS[: start % len(MUTATORS)]
    return [m for f in order if (m := f(code)) is not None]


def remove_function_body(code: str) -> tuple[str, str] | None:
    """For complete_code: blank the body of `main` (or the longest function). Returns (skeleton, fn name)."""
    clean = ballerina.strip_comments_and_strings(code)
    best = None
    for m in re.finditer(r"\bfunction\s+(\w+)\s*\([^)]*\)[^{;]*\{", clean):
        depth, i = 1, m.end()
        while i < len(clean) and depth:
            depth += clean[i] == "{"
            depth -= clean[i] == "}"
            i += 1
        body = code[m.end(): i - 1]
        if body.count(";") < 2:
            continue
        score = (m.group(1) == "main", len(body))
        if best is None or score > best[0]:
            best = (score, m.end(), i - 1, m.group(1))
    if best is None:
        return None
    _, s, e, name = best
    indent = "    "
    return code[:s] + f"\n{indent}// TODO: implement\n" + code[e:], name
