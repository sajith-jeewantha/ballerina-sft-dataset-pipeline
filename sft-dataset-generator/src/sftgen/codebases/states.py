"""Codebase states: a verified base project changed into the starting point of a task.

Every state is made by a deterministic code edit (no model calls), then verified with `bal`:

* `compile_error`       one call to a function defined in the project is misspelled
                        (else a used import is removed). Must FAIL to build, with an error in that file.
* `unfinished_feature`  the body of one non-trivial function is replaced by a stub that panics
                        ("not implemented"). Must BUILD (imports left unused by the stub are dropped).
                        The task is to implement it; the base is the reference solution.
* `version_drift`       an HTTP service is rewritten in Ballerina 1.x style (`@http:ServiceConfig
                        {basePath}`, named service, `@http:ResourceConfig {methods, path}`).
                        Must FAIL to build on Swan Lake; the task is to migrate it.
* `failing_test`        one operator in code the tests exercise is flipped. Must FAIL its tests: this
                        RUNS the project's tests, so it is only verified with `codebases.run_tests`
                        (else recorded as `skipped` and not used).

The choice of function, call or operator is stable for a given project.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from typing import Any

from balpipe_common.ids import sha256_text

from sftgen.ballerina import strip_comments_and_strings

FUNC_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?:(?:public|private|isolated|remote|resource|transactional)\s+)*function\s+"
    r"(?P<name>(?:get|post|put|delete|patch|head|options|default)\s+(?:[^\s(\[]|\[[^\]]*\])+|[A-Za-z_]\w*)\s*\(",
    re.M)
SERVICE_RE = re.compile(r"^(?P<indent>[ \t]*)(?:isolated\s+)?service\s+(?P<path>/[^\s{]*|/)\s+on\s+(?P<on>[^{]+?)\s*\{",
                        re.M)
RESOURCE_RE = re.compile(r"^(?P<indent>[ \t]*)resource\s+function\s+(?P<method>get|post|put|delete|patch|head|options|"
                         r"default)\s+(?P<path>(?:[^\s(\[]|\[[^\]]*\])+)\s*\(", re.M)
OPS = [(" + ", " - "), (" - ", " + "), (" >= ", " > "), (" <= ", " < "), (" > ", " >= "), (" < ", " <= "),
       (" == ", " != "), (" != ", " == "), (" && ", " || ")]


@dataclass
class State:
    kind: str
    target: str
    description: str
    changed: dict[str, str]
    expect: str                 # fails_build | builds | fails_tests


def patch(base: dict[str, str], changed: dict[str, str]) -> str:
    out = []
    for path, new in sorted(changed.items()):
        out.extend(difflib.unified_diff(base.get(path, "").splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{path}", tofile=f"b/{path}"))
    return "".join(out)


def _pick(options: list[Any], key: str) -> Any:
    """A stable choice among options, by hash of the project and the kind."""
    return options[int(sha256_text(key)[:8], 16) % len(options)] if options else None


def _match_brace(clean: str, open_pos: int) -> int | None:
    depth = 0
    for i in range(open_pos, len(clean)):
        if clean[i] == "{":
            depth += 1
        elif clean[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def functions(code: str) -> list[dict[str, Any]]:
    """Functions with a block body: name, indent, body span (inside the braces), body line count."""
    clean = strip_comments_and_strings(code)
    out = []
    for m in FUNC_RE.finditer(clean):
        depth, i = 0, m.end() - 1
        while i < len(clean):  # end of the parameter list
            if clean[i] == "(":
                depth += 1
            elif clean[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        j = i + 1
        while j < len(clean) and clean[j] not in "{;=":
            j += 1
        if j >= len(clean) or clean[j] != "{":
            continue  # expression-bodied (=>) or external function
        end = _match_brace(clean, j)
        if end is None:
            continue
        body = code[j + 1:end]
        out.append({"name": " ".join(m.group("name").split()), "indent": m.group("indent"), "open": j, "close": end,
                    "lines": body.count("\n"), "statements": clean[j + 1:end].count(";")})
    return out


def _main_files(files: dict[str, str]) -> list[str]:
    return sorted(p for p in files if p.endswith(".bal") and not p.startswith("tests/"))


def unfinished_feature(cid: str, files: dict[str, str]) -> State | None:
    options = []
    for path in _main_files(files):
        for f in functions(files[path]):
            if f["name"] != "main" and 4 <= f["lines"] <= 40 and f["statements"] >= 3:
                options.append((path, f))
    pick = _pick(sorted(options, key=lambda o: (o[0], o[1]["open"])), f"{cid}:unfinished")
    if not pick:
        return None
    path, f = pick
    code = files[path]
    inner = f["indent"] + "    "
    stub = f"\n{inner}// TODO: implement\n{inner}panic error(\"not implemented\");\n{f['indent']}"
    new = code[:f["open"] + 1] + stub + code[f["close"]:]
    return State("unfinished_feature", f"{path}::{f['name']}",
                 f"The body of `{f['name']}` in {path} ({f['lines']} lines) is replaced by a stub that panics.",
                 {path: new}, "builds")


def compile_error(cid: str, files: dict[str, str]) -> State | None:
    defined = {f["name"] for p in _main_files(files) for f in functions(files[p]) if re.fullmatch(r"\w+", f["name"])}
    defined -= {"main", "init"}
    calls = []
    for path in _main_files(files):
        clean = strip_comments_and_strings(files[path])
        for m in re.finditer(r"(?<![\w.:>$])([A-Za-z_]\w{3,})\s*\(", clean):
            name = m.group(1)
            before = clean[max(0, m.start() - 12):m.start()]
            if name in defined and "function" not in before:
                calls.append((path, m.start(1), name))
    pick = _pick(sorted(calls), f"{cid}:compile_error")
    if pick:
        path, pos, name = pick
        typo = name[:-2] + name[-1] + name[-2] if name[-1] != name[-2] else name[:-1]
        code = files[path]
        new = code[:pos] + typo + code[pos + len(name):]
        return State("compile_error", path, f"The call to `{name}` in {path} is misspelled `{typo}`.", {path: new},
                     "fails_build")
    imports = []
    for path in _main_files(files):
        for m in re.finditer(r"^import\s+[\w./]+(?:\s+as\s+\w+)?\s*;\s*\n", files[path], re.M):
            imports.append((path, m.start(), m.end()))
    pick = _pick(sorted(imports), f"{cid}:compile_error")
    if not pick:
        return None
    path, a, b = pick
    code = files[path]
    return State("compile_error", path, f"The import `{code[a:b].strip()}` is removed from {path}.",
                 {path: code[:a] + code[b:]}, "fails_build")


def version_drift(cid: str, files: dict[str, str]) -> State | None:
    for path in _main_files(files):
        code = files[path]
        clean = strip_comments_and_strings(code)
        if "ballerina/http" not in code:
            continue
        m = SERVICE_RE.search(clean)
        if not m:
            continue
        close = _match_brace(clean, m.end() - 1)
        if close is None:
            continue
        base_path = m.group("path").strip()
        name = re.sub(r"\W+", "_", base_path.strip("/")) or "root"
        header = (f"{m.group('indent')}@http:ServiceConfig {{\n{m.group('indent')}    basePath: \"{base_path}\"\n"
                  f"{m.group('indent')}}}\n{m.group('indent')}service {name} on {code[m.start('on'):m.end('on')]} {{")
        body = code[m.end():close]
        clean_body = clean[m.end():close]
        pieces, last, n = [], 0, 0
        for r in RESOURCE_RE.finditer(clean_body):
            path_txt = body[r.start("path"):r.end("path")]
            legacy_path = "/" + re.sub(r"\[\s*[\w:]+\s+(\w+)\s*\]", r"{\1}", path_txt).lstrip("/").replace(".", "")
            fname = re.sub(r"_+", "_", re.sub(r"\W+", "_", f"{r.group('method')}_{path_txt}")).strip("_") \
                or r.group("method")
            ind = r.group("indent")
            pieces.append(body[last:r.start()])
            pieces.append(f"{ind}@http:ResourceConfig {{\n{ind}    methods: [\"{r.group('method').upper()}\"],\n"
                          f"{ind}    path: \"{legacy_path}\"\n{ind}}}\n{ind}resource function {fname}(")
            last = r.end()
            n += 1
        if not n:
            continue
        pieces.append(body[last:])
        new = code[:m.start()] + header + "".join(pieces) + code[close:]
        return State("version_drift", path,
                     f"The service `{base_path}` in {path} is rewritten in Ballerina 1.x style ({n} resource(s)).",
                     {path: new}, "fails_build")
    return None


def failing_test(cid: str, files: dict[str, str]) -> State | None:
    if not any(p.startswith("tests/") for p in files):
        return None
    options = []
    for path in _main_files(files):
        clean = strip_comments_and_strings(files[path])
        for f in functions(files[path]):
            body = clean[f["open"]:f["close"]]
            for a, b in OPS:
                k = body.find(a)
                if k >= 0:
                    options.append((path, f["open"] + k, a, b, f["name"]))
                    break
    pick = _pick(sorted(options), f"{cid}:failing_test")
    if not pick:
        return None
    path, pos, a, b, fname = pick
    code = files[path]
    return State("failing_test", f"{path}::{fname}",
                 f"In `{fname}` ({path}) the operator `{a.strip()}` is changed to `{b.strip()}`.",
                 {path: code[:pos] + b + code[pos + len(a):]}, "fails_tests")


MAKERS = {"compile_error": compile_error, "unfinished_feature": unfinished_feature, "version_drift": version_drift,
          "failing_test": failing_test}


def make_states(cid: str, files: dict[str, str], kinds: list[str]) -> list[State]:
    return [st for k in kinds if k in MAKERS and (st := MAKERS[k](cid, files)) is not None]
