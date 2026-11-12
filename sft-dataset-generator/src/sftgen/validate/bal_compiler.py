"""Compile-check Ballerina code with the real `bal` toolchain.

Safety & determinism:
* every build runs in a fresh temp dir as CWD (no stray main.jar files),
* `--offline` first (after a one-time prewarm); online retry only when a
  module is not in the local cache,
* hard timeout and JVM heap cap (`JAVA_OPTS=-Xmx…`),
* generated code is only ever *compiled*; `run_and_compare` is used solely for
  trusted upstream BBE programs,
* results cached on disk by sha256(code) + Ballerina version.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path

from balpipe_common.config import ValidateCfg
from balpipe_common.ids import sha256_text, utc_now_iso
from balpipe_common.schemas import CompileError

from sftgen import ballerina

ERROR_RE = re.compile(r"^ERROR \[(?P<file>[^\]]+?):\((?P<l>\d+):(?P<c>\d+),\d+:\d+\)\] (?P<msg>.+)$", re.MULTILINE)
GENERIC_ERR_RE = re.compile(r"^error: (?P<msg>.+)$", re.MULTILINE)
UNRESOLVED_RE = re.compile(r"cannot resolve module|could not resolve|unresolved module", re.IGNORECASE)
PREFIX_USE_RE = re.compile(r"(?<![\w.$])([a-z_]\w*):[A-Za-z_]\w*")
LANGLIB_PREFIXES = {"int", "float", "decimal", "string", "boolean", "array", "map", "error", "xml", "table",
                    "stream", "object", "function", "future", "typedesc", "byte"}


class BalUnavailable(RuntimeError):
    pass


@dataclass
class Prepared:
    source: str
    code_form: str
    wrapped: bool = False
    imports_added: list[str] = field(default_factory=list)
    line_offset: int = 0


@dataclass
class CompileOutcome:
    status: str  # ok | error | timeout
    errors: list[dict] = field(default_factory=list)
    code_form: str = "program"
    wrapped: bool = False
    imports_added: list[str] = field(default_factory=list)
    ballerina_version: str | None = None
    checked_at: str = ""
    cached: bool = False

    def compile_errors(self) -> list[CompileError]:
        return [CompileError(**e) for e in self.errors]


def prepare(code: str, auto_imports: dict[str, str]) -> Prepared:
    """Make a snippet compilable without changing its meaning: wrap statement
    fragments in `main`, and add imports for well-known module prefixes."""
    form = ballerina.code_form(code)
    items = ballerina.split_top_level(code)
    import_lines = [it.text.strip() for it in items if it.is_import]
    body = "\n".join(it.text for it in items if not it.is_import) if form == "fragment" else code
    existing = ballerina.module_prefixes(code)
    used = {m.group(1) for m in PREFIX_USE_RE.finditer(ballerina.strip_comments_and_strings(code))}
    added = sorted(auto_imports[p] for p in used if p in auto_imports and p not in existing and p not in LANGLIB_PREFIXES)
    header = [f"import {mod};" for mod in added]
    if form == "fragment":
        returns = " returns error?" if re.search(r"\bcheck\b", body) else ""
        lines = header + import_lines + ["", f"public function main(){returns} {{", textwrap.indent(body, "    "), "}", ""]
        return Prepared("\n".join(lines), form, wrapped=True, imports_added=added,
                        line_offset=len(header) + len(import_lines) + 2)
    if added:
        return Prepared("\n".join(header) + "\n" + code, form, imports_added=added, line_offset=len(header))
    return Prepared(code, form)


def parse_errors(output: str, line_offset: int = 0) -> list[dict]:
    errs = [
        {"line": max(1, int(m.group("l")) - line_offset), "col": int(m.group("c")), "message": m.group("msg").strip()}
        for m in ERROR_RE.finditer(output)
    ]
    if not errs:
        errs = [{"line": None, "col": None, "message": m.group("msg").strip()} for m in GENERIC_ERR_RE.finditer(output)]
    return errs[:20]


class BalCompiler:
    def __init__(self, cfg: ValidateCfg, cache_dir: Path) -> None:
        self.cfg = cfg
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if shutil.which(cfg.bal_path) is None:
            raise BalUnavailable(f"`{cfg.bal_path}` not found on PATH; install Ballerina or set validate.enabled=false")
        self.version = self._detect_version()

    # ------------------------------------------------------------------ #
    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I)}
        env["JAVA_OPTS"] = f"{env.get('JAVA_OPTS', '')} -Xmx{self.cfg.java_heap}".strip()
        return env

    def _detect_version(self) -> str:
        out = subprocess.run([self.cfg.bal_path, "version"], capture_output=True, text=True, timeout=60, env=self._env())
        m = re.search(r"Ballerina (\S+)", out.stdout + out.stderr)
        return m.group(1) if m else "unknown"

    def _bal(self, args: list[str], src: str, timeout: int) -> tuple[int | None, str]:
        with tempfile.TemporaryDirectory(prefix="balcheck-") as tmp:
            Path(tmp, "main.bal").write_text(src, encoding="utf-8")
            try:
                p = subprocess.run([self.cfg.bal_path, *args, "main.bal"], cwd=tmp, capture_output=True, text=True,
                                   timeout=timeout, env=self._env())
            except subprocess.TimeoutExpired:
                return None, ""
            return p.returncode, p.stdout + "\n" + p.stderr

    def _cache_path(self, kind: str, code: str) -> Path:
        key = sha256_text(f"{kind}\x1f{self.version}\x1f{code}")
        return self.cache_dir / key[:2] / f"{key}.json"

    # ------------------------------------------------------------------ #
    def prewarm(self) -> None:
        """Pull commonly used modules once so later builds can run --offline."""
        # Unused imports fail compilation, but module resolution (the download) happens first.
        src = "\n".join(f"import {m} as m{i};" for i, m in enumerate(self.cfg.prewarm_modules))
        self._bal(["build"], src + "\npublic function main() {}\n", max(self.cfg.timeout_s, 300))

    def compile(self, code: str) -> CompileOutcome:
        cache = self._cache_path("compile", code)
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            return CompileOutcome(**{**data, "cached": True})
        prep = prepare(code, self.cfg.auto_imports)
        args = ["build", "--offline"] if self.cfg.offline else ["build"]
        rc, out = self._bal(args, prep.source, self.cfg.timeout_s)
        if rc not in (0, None) and self.cfg.offline and UNRESOLVED_RE.search(out):
            rc, out = self._bal(["build"], prep.source, self.cfg.timeout_s)  # module not cached yet: pull once
        outcome = CompileOutcome(
            status="timeout" if rc is None else ("ok" if rc == 0 else "error"),
            errors=[] if rc == 0 else parse_errors(out, prep.line_offset),
            code_form=prep.code_form,
            wrapped=prep.wrapped,
            imports_added=prep.imports_added,
            ballerina_version=self.version,
            checked_at=utc_now_iso(),
        )
        if outcome.status != "timeout":
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({k: v for k, v in asdict(outcome).items() if k != "cached"}), encoding="utf-8")
        return outcome

    def compile_many(self, codes: list[str]) -> list[CompileOutcome]:
        with ThreadPoolExecutor(max_workers=self.cfg.workers) as pool:
            return list(pool.map(self.compile, codes))

    def run_and_compare(self, code: str, expected_out: str) -> bool | None:
        """Run a trusted program and compare stdout with a BBE `.out` file.

        Returns None when the comparison is not meaningful (services, timeouts).
        """
        if ballerina.SERVICE_RE.search(ballerina.strip_comments_and_strings(code)):
            return None
        cache = self._cache_path("run/2", code + "\x1f" + expected_out)
        if cache.is_file():
            return json.loads(cache.read_text(encoding="utf-8"))["match"]
        rc, out = self._bal(["run", "--offline"], code, self.cfg.timeout_s)
        if rc is None:
            return None
        got = _normalize_output(_program_stdout(out))
        want = _normalize_output("\n".join(ln for ln in expected_out.splitlines() if not ln.startswith("$ ")))
        # concurrent BBEs (workers, strands) print in nondeterministic order: accept the same lines in any order
        match = rc == 0 and (got == want or sorted(got.splitlines()) == sorted(want.splitlines()))
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"match": match}), encoding="utf-8")
        return match


def _program_stdout(out: str) -> str:
    noise = ("WARNING:", "Compiling source", "Running executable", "\tmain.bal", "Generating executable")
    return "\n".join(ln for ln in out.splitlines() if not ln.startswith(noise) and ln.strip() != "main.bal")


def _normalize_output(text: str) -> str:
    return "\n".join(ln.rstrip() for ln in text.strip().splitlines() if ln.strip())
