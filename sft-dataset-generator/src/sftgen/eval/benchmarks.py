"""Eval set v2 benchmarks: HumanEval and MBPP translated into Ballerina, with `bal` tests.

* `eval fetch`: each source in `eval.benchmarks` is pinned (`git ls-remote`, lockfile section
  `benchmarks`), its problems file and LICENSE are fetched with a sparse clone, the licence is
  checked and recorded in the licence register (intended use `eval-benchmark`, with the
  attribution duty in the notes), and the problems are stored in `benchmark_problems`.
  HumanEval: all 164 (MIT). MBPP: the official test split, task ids 11-510 (CC BY 4.0).
* `eval translate`: the teacher translates each pending problem into a Ballerina task: a prompt
  with the function signature, a reference solution and a test file. The pair must build together
  (tests compiled; with `eval.run_tests` the tests are also run and must pass). Failures go back
  to the teacher with the real errors, up to `eval.translate_attempts`. Translated problems become
  eval items (suite `humaneval` / `mbpp`, task `write_code`). LIMIT_GENERATING caps problems per run.

Translations are only ever eval items: they are fingerprinted like the BBE eval items (build-eval
decontamination), so near-copies stay out of training.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
from typing import Any

from pydantic import BaseModel

from balpipe_common.config import BenchmarkSource, ConfigError, Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.licences import LicenceRegister, detect_licence
from balpipe_common.lock import SourceLock

from sftgen.generate.llm import LLMClient, load_prompt
from sftgen.generate.questions import record_usage
from sftgen.validate.bal_compiler import BalCompiler

log = logging.getLogger(__name__)
PHASE = "translate-benchmarks"


class Translation(BaseModel):
    function_name: str
    prompt: str
    solution: str
    tests: str


def source_text(suite: str, p: dict[str, Any]) -> str:
    if suite == "humaneval":
        return f"{p['prompt']}\n# reference solution\n{p['canonical_solution']}\n# tests\n{p['test']}\n" \
               f"# entry point: {p['entry_point']}"
    tests = "\n".join(p.get("test_list") or [])
    setup = p.get("test_setup_code") or ""
    return f"# task\n{p['text']}\n# reference solution\n{p['code']}\n# tests\n{setup}\n{tests}"


def parse_problems(src: BenchmarkSource, data: bytes) -> list[tuple[str, dict[str, Any]]]:
    text = gzip.decompress(data).decode("utf-8") if src.file.endswith(".gz") else data.decode("utf-8")
    out = []
    for line in text.splitlines():
        if not line.strip():
            continue
        p = json.loads(line)
        tid = str(p["task_id"]).split("/")[-1]
        if src.task_ids and not (src.task_ids[0] <= int(tid) <= src.task_ids[-1]):
            continue
        out.append((tid, p))
    return out


def fetch(s: Settings, db: Database, run: RunTracker, lock: SourceLock, *, update_lock: bool = False) -> dict[str, Any]:
    from ballerinascraper.repos import remote_head, take_snapshot

    register = LicenceRegister(s.path(s.repos.register_file))
    stats: dict[str, Any] = {}
    try:
        for src in s.eval_.benchmarks:
            url = f"https://github.com/{src.repo}.git"
            locked = lock.get("benchmarks", src.name)
            commit = str(locked["commit"]) if locked and not update_lock else remote_head(url, "HEAD")
            lock.set("benchmarks", src.name, {"repo": src.repo, "file": src.file, "commit": commit})
            cfg = s.repos.model_copy(update={"sparse_patterns": [f"/{src.file}", "/LICENSE*"], "exclude_paths": [],
                                             "max_file_bytes": 20_000_000})
            snap = take_snapshot(url, commit, s.cache_dir / "benchmarks" / src.name, cfg)
            if src.file not in snap.files:
                raise RuntimeError(f"{src.file} not found in {src.repo}@{commit[:12]}")
            detected = detect_licence(snap.licence_text or "")
            register.upsert(src.repo, spdx=src.spdx, verdict="safe", intended_use="eval-benchmark",
                            pinned_sha=commit, notes=f"{src.licence_note} (repo LICENSE reads {detected or 'unknown'})")
            n_new = 0
            for tid, p in parse_problems(src, snap.files[src.file]):
                pid = f"{src.name}/{tid}"
                if db.scalar("SELECT 1 FROM benchmark_problems WHERE id = ?", (pid,)):
                    continue
                db.insert("benchmark_problems", {"id": pid, "suite": src.name, "task_id": tid,
                                                 "source": json.dumps(p, ensure_ascii=False), "commit_sha": commit,
                                                 "spdx": src.spdx, "status": "pending"})
                n_new += 1
            total = db.scalar("SELECT COUNT(*) FROM benchmark_problems WHERE suite = ?", (src.name,))
            stats[src.name] = {"commit": commit[:12], "licence": detected, "problems": total, "new": n_new}
    finally:
        lock.save()
        register.save()
    run.stats.update({f"{k}_problems": v["problems"] for k, v in stats.items()})
    return stats


class Translator:
    def __init__(self, s: Settings, db: Database, run: RunTracker, client: LLMClient, compiler: BalCompiler) -> None:
        self.s, self.db, self.run, self.client, self.compiler = s, db, run, client, compiler
        self.prompt = load_prompt("benchmark_translate")
        if not s.answer_gen_model:
            raise ConfigError("set ANSWER_GEN_MODEL (the teacher) in .env")
        self.model = s.answer_gen_model

    def check(self, t: Translation) -> tuple[bool, str]:
        files = {"main.bal": t.solution, f"tests/{t.function_name}_test.bal": t.tests}
        res = self.compiler.build_package(files, test=self.s.eval_.run_tests)
        if res.status == "ok":
            return True, ""
        lines = [f"- {e.get('file') or 'code'}:{e.get('line') or ''} {e.get('message')}" for e in res.errors[:8]]
        return False, "\n".join(lines) or res.status

    async def translate(self, row: Any) -> dict[str, Any]:
        src = json.loads(row["source"])
        previous = ""
        for attempt in range(1, self.s.eval_.translate_attempts + 1):
            t, usage = await self.client.chat_json(
                self.model, self.prompt, Translation, temperature=0.2, max_tokens=4000, problem_id=row["id"],
                source=source_text(row["suite"], src), previous=previous)
            record_usage(self.db, self.run, PHASE, usage, self.model, True)
            ok, errors = await asyncio.to_thread(self.check, t)
            if ok:
                return {"ok": True, "attempts": attempt, "t": t}
            previous = (f"\nYour previous translation failed `bal {'test' if self.s.eval_.run_tests else 'build'}`:"
                        f"\n{errors}\nPrevious solution:\n{t.solution}\nPrevious tests:\n{t.tests}\nFix it.\n")
        return {"ok": False, "attempts": self.s.eval_.translate_attempts, "error": previous[:2000]}

    def save(self, row: Any, out: dict[str, Any]) -> None:
        if not out["ok"]:
            self.db.update("benchmark_problems", row["id"], {"status": "failed", "attempts": out["attempts"],
                                                             "error": out["error"]})
            return
        t: Translation = out["t"]
        mode = "test" if self.s.eval_.run_tests else "compile"
        files = {f"tests/{t.function_name}_test.bal": t.tests}
        values = {"suite": row["suite"], "version": "v2", "name": row["task_id"], "title": t.function_name,
                  "category": "benchmark", "url": row["id"], "source_url": None, "commit_sha": row["commit_sha"],
                  "files": json.dumps(files), "covers": "[]", "nodes": "[]", "verify_mode": "test",
                  "verify_status": "pass" if mode == "test" else "compiled",
                  "verify_detail": f"reference + tests verified with bal ({mode})", "status": "active",
                  "prompt": t.prompt, "task_type": "write_code", "reference": t.solution,
                  "meta": json.dumps({"function": t.function_name, "attempts": out["attempts"], "mode": mode})}
        if self.db.scalar("SELECT 1 FROM eval_items WHERE id = ?", (row["id"],)):
            self.db.update("eval_items", row["id"], values)
        else:
            self.db.insert("eval_items", {"id": row["id"], **values})
        from sftgen.eval.tasks import fingerprint

        fingerprint(self.db, row["id"], {"solution.bal": t.solution})
        self.db.update("benchmark_problems", row["id"], {"status": "translated", "attempts": out["attempts"],
                                                         "error": None})


async def _translate(s: Settings, db: Database, run: RunTracker, client: LLMClient,
                     compiler: BalCompiler) -> dict[str, int]:
    tr = Translator(s, db, run, client, compiler)
    rows = db.query("SELECT * FROM benchmark_problems WHERE status = 'pending' ORDER BY suite, CAST(task_id AS INT)")
    limit = s.limit_generating or math.inf
    rows = rows[: int(min(len(rows), limit))]
    stats = {"pending": len(rows), "translated": 0, "failed": 0, "errors": 0}

    async def one(r: Any) -> tuple[Any, dict[str, Any] | None, Exception | None]:
        try:
            return r, await tr.translate(r), None
        except Exception as exc:
            return r, None, exc

    step = max(1, s.generate.concurrency)
    for start in range(0, len(rows), step):
        for fut in asyncio.as_completed([one(r) for r in rows[start:start + step]]):
            r, out, exc = await fut
            if exc is not None or out is None:
                stats["errors"] += 1
                record_usage(db, run, PHASE, None, tr.model, False)
                log.warning("translation of %s failed: %s", r["id"], exc)
                continue
            tr.save(r, out)
            stats["translated" if out["ok"] else "failed"] += 1
        run.flush()
    return stats


def translate(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    s.require("llm_api_key", "answer_gen_model")
    compiler = BalCompiler(s.validate_, s.cache_dir / "bal")
    client = LLMClient(s, s.cache_dir / "llm")
    stats = asyncio.run(_translate(s, db, run, client, compiler))
    run.stats.update(stats)
    return stats
