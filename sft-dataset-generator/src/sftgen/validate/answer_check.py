"""Check a teacher answer against the answer-format contract, with the real toolchain.

The contract (`prompts/answer_contract.yaml`, and §8 question 1 of docs/PLAN.md):

* Code goes in ```ballerina blocks, and every such block must compile with the pinned
  Swan Lake toolchain. Statement-only snippets are wrapped in `main` before compiling, as in
  `validate` (see `bal_compiler.prepare`).
* A solution with several files gives each file in full in its own block whose first line is
  `// file: <relative path>` (e.g. `// file: main.bal`, `// file: tests/main_test.bal`). Those
  blocks are built together as one package. This is also the format for codebase tasks (M7):
  every changed file in full, under its path.
* Tests (files under `tests/`) are compiled; they are run only when `answers.run_tests` is on.
* Code tasks (`answers.code_tasks`) must contain code.

`check()` returns the result and, when it fails, the feedback for a repair: the compiler errors
with file, line and message, or the failing tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from balpipe_common.config import AnswersCfg

from sftgen import markdown as md
from sftgen.validate.bal_compiler import BalCompiler, CompileOutcome

FILE_HEADER_RE = re.compile(r"^\s*//\s*file:\s*([\w./-]+\.(?:bal|toml))\s*$")


@dataclass
class Block:
    code: str
    file: str | None     # from a `// file: <path>` first line


@dataclass
class CodeCheck:
    status: str                         # pass | fail | no_code
    reason: str = ""                    # compile | test | missing_code | timeout
    verification: str = ""              # compile | test
    blocks: int = 0
    package_files: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "verification": self.verification,
                "blocks": self.blocks, "package_files": self.package_files, "errors": self.errors}

    def feedback(self, max_errors: int) -> str:
        if self.reason == "missing_code":
            return ("The request needs working Ballerina code, but the answer has no ```ballerina code block. "
                    "Give the complete code.")
        if self.reason == "timeout":
            return "Building the code timed out. Make sure it is a small, complete, compilable program."
        lines = []
        for e in self.errors[:max_errors]:
            where = e.get("file") or (f"code block {e['block']}" if e.get("block") else "code")
            pos = f":{e['line']}:{e['col']}" if e.get("line") else ""
            lines.append(f"- {where}{pos}: {e['message']}")
        head = ("The tests in the answer fail:" if self.reason == "test" else
                "The code does not compile with Ballerina Swan Lake (bal 2201.14.1):")
        return head + "\n" + "\n".join(lines)


def code_blocks(answer: str) -> list[Block]:
    out = []
    for f in md.iter_fences(answer):
        if f.lang != "ballerina" or not f.code.strip():
            continue
        code = f.code.strip("\n")
        first = next((ln for ln in code.splitlines() if ln.strip()), "")
        m = FILE_HEADER_RE.match(first)
        out.append(Block(code, m.group(1) if m else None))
    return out


class AnswerChecker:
    def __init__(self, cfg: AnswersCfg, compiler: BalCompiler) -> None:
        self.cfg = cfg
        self.compiler = compiler

    def check(self, answer: str, task_type: str) -> CodeCheck:
        blocks = code_blocks(answer)
        if not blocks:
            if task_type in self.cfg.code_tasks:
                return CodeCheck("fail", "missing_code")
            return CodeCheck("no_code")
        errors: list[dict[str, Any]] = []
        verification = "compile"
        files = {b.file: b.code for b in blocks if b.file}
        package_files = sorted(files)
        timed_out = False
        if files:
            if not any(f.endswith(".bal") and not f.startswith("tests/") for f in files):
                files["main.bal"] = "public function main() {\n}\n"  # tests alone need a module to test
            run_tests = self.cfg.run_tests and any(f.startswith("tests/") for f in files)
            res: CompileOutcome = self.compiler.build_package(files, test=run_tests)
            verification = "test" if run_tests else "compile"
            timed_out |= res.status == "timeout"
            errors.extend(res.errors if res.status == "error" else [])
        for i, b in enumerate(blocks, 1):
            if b.file:
                continue
            res = self.compiler.compile(b.code)
            timed_out |= res.status == "timeout"
            if res.status == "error":
                errors.extend({**e, "block": i} for e in res.errors)
        if errors:
            # failed test runs are reported as {"file": "tests", "line": None}; anything else is a build error
            only_tests = all(e.get("file") == "tests" and e.get("line") is None for e in errors)
            return CodeCheck("fail", "test" if only_tests else "compile", verification, len(blocks), package_files,
                             errors)
        if timed_out:
            return CodeCheck("fail", "timeout", verification, len(blocks), package_files)
        return CodeCheck("pass", "", verification, len(blocks), package_files)
