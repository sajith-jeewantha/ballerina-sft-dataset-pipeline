"""Check eval items with the pinned toolchain: `bal run` against the expected `.out`, or `bal build`.

* `run`    the example has one `.bal` file and one `.out` transcript of `$ bal run <file>`:
           stdout (plus stderr, without the toolchain's own banner lines) must equal the
           transcript after the command line, line by line. Indentation and trailing spaces
           are ignored (transcripts indent stack traces with spaces, `bal` with tabs).
* `build`  everything else that upstream CI builds (`verifyBuild` in examples/index.json):
           each `.bal` file must compile.

Each check runs in a fresh temp folder holding the example's files, `--offline` first (online
once when a module is not cached yet), with a timeout that kills the whole process group.
Results are cached by the files, the mode and the `bal` version.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from balpipe_common.config import ValidateCfg
from balpipe_common.ids import sha256_text

UNRESOLVED_RE = re.compile(r"cannot resolve module|could not resolve|unresolved module", re.IGNORECASE)
COMPILE_ERROR_RE = re.compile(r"^ERROR \[|compilation contains errors", re.M)
COMPARE_VERSION = 2  # bump when the comparison changes, so cached results are recomputed
BANNER_RE = re.compile(r"^(WARNING: .*|Compiling source|Running executable|Generating executable|\t\S+\.bal|"
                       r"\s*target/bin/\S+\.jar)$")


@dataclass
class VerifyResult:
    status: str        # pass | output_differs | build_failed | run_failed | timeout | skipped
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"status": self.status, "detail": self.detail}


def run_transcript(files: dict[str, str]) -> tuple[str, list[str]] | None:
    """(file to run, expected output lines) when the example is a single `bal run` transcript."""
    bals = [f for f in files if f.endswith(".bal")]
    outs = [f for f in files if f.endswith(".out")]
    if len(bals) != 1 or len(outs) != 1:
        return None
    lines = files[outs[0]].splitlines()
    if not lines or not re.match(rf"^\$ bal run {re.escape(bals[0])}\s*$", lines[0]):
        return None
    expected = []
    for line in lines[1:]:
        if line.startswith("$ "):
            return None  # more than one command: not a plain run transcript
        expected.append(line)
    return bals[0], expected


def verify_mode(files: dict[str, str], verify_output: bool, verify_build: bool) -> str:
    if verify_output and run_transcript(files):
        return "run"
    if (verify_build or verify_output) and any(f.endswith(".bal") for f in files):
        return "build"
    return "none"


def _norm(lines: list[str]) -> list[str]:
    out = [ln.strip() for ln in lines]
    while out and not out[-1]:
        out.pop()
    while out and not out[0]:
        out.pop(0)
    return out


def compare_output(expected: list[str], stdout: str, stderr: str) -> tuple[bool, str]:
    want = _norm(expected)
    got = _norm(stdout.splitlines())
    if got == want:
        return True, ""
    err = [ln for ln in stderr.splitlines() if not BANNER_RE.match(ln) and ln.strip()]
    for combined in (got + err, err + got):
        if _norm(combined) == want:
            return True, ""
    for i, (a, b) in enumerate(zip(want, got)):
        if a != b:
            return False, f"line {i + 1}: expected {a!r}, got {b!r}"
    return False, f"expected {len(want)} line(s), got {len(got)}" + (f"; stderr: {err[0][:200]!r}" if err else "")


class Verifier:
    def __init__(self, cfg: ValidateCfg, cache_dir: Path, timeout_s: int) -> None:
        if shutil.which(cfg.bal_path) is None:
            raise RuntimeError(f"`{cfg.bal_path}` not found on PATH")
        self.cfg = cfg
        self.timeout_s = timeout_s
        self.cache_dir = cache_dir
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = subprocess.run([cfg.bal_path, "version"], capture_output=True, text=True, timeout=60, env=self._env())
        m = re.search(r"Ballerina (\S+)", out.stdout + out.stderr)
        self.version = m.group(1) if m else "unknown"

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I)}
        env["JAVA_OPTS"] = f"{env.get('JAVA_OPTS', '')} -Xmx{self.cfg.java_heap}".strip()
        return env

    def _bal(self, args: list[str], cwd: Path) -> tuple[int | None, str, str]:
        proc = subprocess.Popen([self.cfg.bal_path, *args], cwd=cwd, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            out, err = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            return None, "", ""
        return proc.returncode, out, err

    def _bal_with_pull(self, args: list[str], cwd: Path) -> tuple[int | None, str, str]:
        rc, out, err = self._bal([args[0], "--offline", *args[1:]], cwd)
        if rc not in (0, None) and UNRESOLVED_RE.search(out + err):
            rc, out, err = self._bal(args, cwd)
        return rc, out, err

    def verify(self, files: dict[str, str], mode: str) -> VerifyResult:
        if mode == "none":
            return VerifyResult("skipped", "not checked upstream (no verifyBuild/verifyOutput)")
        key = sha256_text(json.dumps([mode, self.version, COMPARE_VERSION, sorted(files.items())]))
        cache = self.cache_dir / f"{key}.json"
        if cache.is_file():
            return VerifyResult(**json.loads(cache.read_text(encoding="utf-8")))
        result = self._verify(files, mode)
        if result.status != "timeout":
            cache.write_text(json.dumps(result.as_dict()), encoding="utf-8")
        return result

    def _verify(self, files: dict[str, str], mode: str) -> VerifyResult:
        with tempfile.TemporaryDirectory(prefix="baleval-") as tmp:
            for name, text in files.items():
                Path(tmp, name).write_text(text, encoding="utf-8")
            if mode == "run":
                transcript = run_transcript(files)
                assert transcript is not None
                main, expected = transcript
                rc, out, err = self._bal_with_pull(["run", main], Path(tmp))
                if rc is None:
                    return VerifyResult("timeout", f"bal run did not finish in {self.timeout_s}s")
                if COMPILE_ERROR_RE.search(err + out):
                    return VerifyResult("build_failed", _first_error(err + out))
                ok, why = compare_output(expected, out, err)
                if ok:
                    return VerifyResult("pass")
                return VerifyResult("output_differs" if rc == 0 or out.strip() else "run_failed", why[:500])
            for bal in sorted(f for f in files if f.endswith(".bal")):
                rc, out, err = self._bal_with_pull(["build", bal], Path(tmp))
                if rc is None:
                    return VerifyResult("timeout", f"bal build {bal} did not finish in {self.timeout_s}s")
                if rc != 0:
                    return VerifyResult("build_failed", f"{bal}: {_first_error(err + out)}")
            return VerifyResult("pass")


def _first_error(text: str) -> str:
    for line in text.splitlines():
        if line.startswith(("ERROR", "error")):
            return line[:300]
    return text.strip().splitlines()[-1][:300] if text.strip() else "failed"
