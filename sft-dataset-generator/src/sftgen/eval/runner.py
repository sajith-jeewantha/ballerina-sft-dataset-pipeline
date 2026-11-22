"""`eval run`: baselines. k sampled answers per eval item per model, scored, summarised as pass@k.

* **Models:** `--model <label>` picks one of `eval.models` (`base` = qwen/qwen3.8-27b by default) or
  `teacher` (ANSWER_GEN_MODEL). Answers use the training system prompt (`sft.system_prompt`), so a
  baseline sees exactly what the fine-tuned model will.
* **Samples:** `eval.samples` answers per item (temperature `eval.sample_temperature`; one answer at
  temperature 0 when samples = 1). Each sample is its own cached call (sent with a seed).
* **Scoring** by item type:
  - benchmarks (`humaneval`, `mbpp`): the answer's function is built with the item's `bal` tests;
    with `eval.run_tests` the tests run and must pass, otherwise they must compile.
  - BBE: with `eval.run_tests` the program is run and its output compared with the `.out`
    transcript; otherwise it must compile.
  - codebase tasks: the changed files are laid over the state and the project must build (a stub
    left in place fails), as in `answer-prompts`.
  - ecosystem write_code: the code must compile. Explain tasks: the judge (`JUDGE_MODEL`) checks the
    answer against the reference.
* **Mode:** `test` when code was executed (`eval.run_tests`), else `compile`. Scores from different
  modes are never mixed: the report labels each run.
* **pass@k:** the unbiased estimator 1 - C(n-c, k) / C(n, k) per item (n samples, c passed),
  averaged per suite and per graph item (`covers`). Results are saved per sample, so an
  interrupted run resumes.
"""

from __future__ import annotations

import asyncio
import json
import logging
from math import comb
from typing import Any

from balpipe_common.config import AnswersCfg, ConfigError, Settings
from balpipe_common.db import Database, RunTracker

from sftgen.generate.llm import LLMClient, load_prompt
from sftgen.generate.questions import record_usage
from sftgen.generate.responses import Judgement
from sftgen.validate.answer_check import AnswerChecker, code_blocks
from sftgen.validate.bal_compiler import BalCompiler

log = logging.getLogger(__name__)
PHASE = "eval-run"


def pass_at_k(n: int, c: int, k: int) -> float:
    if n < k:
        return float(c > 0) if n else 0.0
    if n - c < k:
        return 1.0
    return 1.0 - comb(n - c, k) / comb(n, k)


def resolve_model(s: Settings, label: str) -> str:
    if label == "teacher" and "teacher" not in s.eval_.models:
        if not s.answer_gen_model:
            raise ConfigError("set ANSWER_GEN_MODEL for the teacher baseline")
        return s.answer_gen_model
    if label not in s.eval_.models:
        raise ConfigError(f"unknown model label {label!r}; eval.models has {sorted(s.eval_.models)} (and 'teacher')")
    return s.eval_.models[label]


class Scorer:
    def __init__(self, s: Settings, db: Database, run: RunTracker, client: Any, compiler: Any) -> None:
        self.s, self.db, self.run, self.client = s, db, run, client
        self.compiler = compiler
        self.checker = AnswerChecker(AnswersCfg(run_tests=s.eval_.run_tests), compiler)
        self.judge_prompt = load_prompt("answer_judge")
        self.judge = s.reviewer_model
        self.run_tests = s.eval_.run_tests

    def _main_code(self, answer: str, function: str | None) -> str | None:
        blocks = [b for b in code_blocks(answer)]
        if not blocks:
            return None
        if function:
            for b in blocks:
                if f"function {function}" in b.code:
                    return b.code
        return blocks[0].code

    def benchmark(self, item: Any, answer: str) -> tuple[bool, dict[str, Any]]:
        meta = json.loads(item["meta"] or "{}")
        code = self._main_code(answer, meta.get("function"))
        if code is None:
            return False, {"reason": "no code"}
        files = {"main.bal": code, **json.loads(item["files"])}
        res = self.compiler.build_package(files, test=self.run_tests)
        return res.status == "ok", {"reason": res.status, "errors": res.errors[:5]}

    def bbe(self, item: Any, answer: str) -> tuple[bool, dict[str, Any]]:
        code = self._main_code(answer, None)
        if code is None:
            return False, {"reason": "no code"}
        files = json.loads(item["files"])
        if self.run_tests and item["verify_mode"] == "run":
            from sftgen.eval.verify import Verifier

            bal_name = next(f for f in files if f.endswith(".bal"))
            out_name = next(f for f in files if f.endswith(".out"))
            v = Verifier(self.s.validate_, self.s.cache_dir / "bal-eval-answers", self.s.eval_.run_timeout_s)
            res = v.verify({bal_name: code, out_name: files[out_name]}, "run")
            return res.status == "pass", {"reason": res.status, "detail": res.detail}
        res = self.compiler.compile(code)
        return res.status == "ok", {"reason": res.status, "errors": res.errors[:5]}

    def codebase(self, item: Any, answer: str) -> tuple[bool, dict[str, Any]]:
        meta = json.loads(item["meta"] or "{}")
        chk = self.checker.check_codebase(answer, json.loads(item["files"]), meta.get("kind", ""),
                                          meta.get("target", ""), self.run_tests)
        return chk.status == "pass", {"reason": chk.reason, "errors": chk.errors[:5]}

    def build(self, item: Any, answer: str) -> tuple[bool, dict[str, Any]]:
        chk = self.checker.check(answer, "write_code")
        return chk.status == "pass", {"reason": chk.reason, "errors": chk.errors[:5]}

    async def judged(self, item: Any, answer: str) -> tuple[bool, dict[str, Any]]:
        if not self.judge:
            raise ConfigError("set JUDGE_MODEL (or QUESTION_GEN_MODEL) to score explain tasks")
        j, usage = await self.client.chat_json(
            self.judge, self.judge_prompt, Judgement, temperature=0.0, max_tokens=1200,
            grounding=f"Reference:\n{item['reference'] or ''}", task_type=item["task_type"] or "explain",
            difficulty="intermediate", request=item["prompt"], answer=answer)
        record_usage(self.db, self.run, "eval-judge", usage, self.judge, True)
        ok = j.verdict.strip().lower() == "pass" and min(j.correct, j.complete, j.clear) >= 3
        return ok, {"reason": "judge", "judge": j.model_dump()}

    async def score(self, item: Any, answer: str) -> tuple[bool, dict[str, Any]]:
        if item["task_type"] == "explain" or item["verify_mode"] == "judge":
            return await self.judged(item, answer)
        fn = {"humaneval": self.benchmark, "mbpp": self.benchmark, "bbe": self.bbe,
              "codebase": self.codebase}.get(item["suite"], self.build)
        return await asyncio.to_thread(fn, item, answer)


def items_for(db: Database, suites: list[str] | None) -> list[Any]:
    rows = db.query("SELECT * FROM eval_items WHERE status = 'active' AND prompt IS NOT NULL ORDER BY suite, id")
    return [r for r in rows if not suites or r["suite"] in suites]


async def _run(s: Settings, db: Database, run: RunTracker, client: Any, compiler: Any, label: str,
               suites: list[str] | None) -> dict[str, Any]:
    model = resolve_model(s, label)
    mode = "test" if s.eval_.run_tests else "compile"
    n = max(1, s.eval_.samples)
    row = db.one("SELECT id FROM eval_runs WHERE label = ? AND model = ? AND mode = ? AND samples = ? "
                 "AND status = 'running' ORDER BY id DESC LIMIT 1", (label, model, mode, n))
    eval_run = row["id"] if row else db.insert("eval_runs", {"label": label, "model": model, "mode": mode,
                                                            "samples": n, "suites": json.dumps(suites or []),
                                                            "status": "running", "run_id": run.id})
    scorer = Scorer(s, db, run, client, compiler)
    prompt = load_prompt("eval_answer")
    system = s.sft.system_prompt.strip() or "You are an expert Ballerina programmer."
    temperature = s.eval_.sample_temperature if n > 1 else 0.0
    items = items_for(db, suites)
    done = {(r[0], r[1]) for r in db.query("SELECT item_id, sample FROM eval_results WHERE eval_run_id = ?",
                                           (eval_run,))}
    work = [(it, k) for it in items for k in range(n) if (it["id"], k) not in done]
    if s.limit_generating:
        work = work[: s.limit_generating]
    stats = {"items": len(items), "samples_todo": len(work), "scored": 0, "passed": 0, "errors": 0}
    log.info("eval run %d: %s (%s), mode %s, %d item(s) x %d sample(s), %d to do", eval_run, label, model, mode,
             len(items), n, len(work))

    async def one(it: Any, k: int) -> tuple[Any, int, str | None, bool, dict[str, Any], Exception | None]:
        try:
            text, usage = await client.chat(model, prompt, temperature=temperature, max_tokens=s.eval_.max_answer_tokens,
                                            sample=k, system=system, request=it["prompt"])
            record_usage(db, run, PHASE, usage, model, True)
            ok, detail = await scorer.score(it, text)
            return it, k, text, ok, detail, None
        except ConfigError:
            raise
        except Exception as exc:
            return it, k, None, False, {}, exc

    step = max(1, s.generate.concurrency)
    for start in range(0, len(work), step):
        for fut in asyncio.as_completed([one(it, k) for it, k in work[start:start + step]]):
            it, k, text, ok, detail, exc = await fut
            if exc is not None:
                stats["errors"] += 1
                record_usage(db, run, PHASE, None, model, False)
                log.warning("eval of %s sample %d failed: %s", it["id"], k, exc)
                continue
            db.insert("eval_results", {"eval_run_id": eval_run, "item_id": it["id"], "sample": k, "answer": text,
                                       "passed": int(ok), "detail": json.dumps(detail)})
            stats["scored"] += 1
            stats["passed"] += int(ok)
        run.flush()
    left = len(items) * n - (db.scalar("SELECT COUNT(*) FROM eval_results WHERE eval_run_id = ?", (eval_run,)) or 0)
    if left <= 0:
        db.update("eval_runs", eval_run, {"status": "done"})
    return {"eval_run": eval_run, "model": model, "mode": mode, **stats, "summary": summary(db, eval_run, s.eval_.ks)}


def summary(db: Database, eval_run: int, ks: list[int]) -> dict[str, Any]:
    """pass@k per suite and per covered graph item, from the saved results."""
    per_item: dict[str, tuple[int, int]] = {}
    for r in db.query("SELECT item_id, COUNT(*) AS n, SUM(passed) AS c FROM eval_results WHERE eval_run_id = ? "
                      "GROUP BY 1", (eval_run,)):
        per_item[r["item_id"]] = (r["n"], r["c"] or 0)
    meta = {r["id"]: r for r in db.query("SELECT id, suite, covers FROM eval_items")}
    suites: dict[str, dict[str, list[float]]] = {}
    refs: dict[str, list[float]] = {}
    for iid, (n, c) in per_item.items():
        it = meta.get(iid)
        if it is None:
            continue
        for k in ks:
            suites.setdefault(it["suite"], {}).setdefault(f"pass@{k}", []).append(pass_at_k(n, c, k))
        for ref in json.loads(it["covers"] or "[]"):
            refs.setdefault(ref, []).append(pass_at_k(n, c, 1))
    avg = lambda xs: round(sum(xs) / len(xs), 4) if xs else None  # noqa: E731
    return {"suites": {su: {"items": len(v[f"pass@{ks[0]}"]), **{m: avg(xs) for m, xs in v.items()}}
                       for su, v in sorted(suites.items())},
            "refs": {ref: avg(xs) for ref, xs in sorted(refs.items())}}


def run(s: Settings, db: Database, run_: RunTracker, *, label: str, suites: list[str] | None) -> dict[str, Any]:
    s.require("llm_api_key")
    resolve_model(s, label)
    compiler = BalCompiler(s.validate_, s.cache_dir / "bal")
    out = asyncio.run(_run(s, db, run_, LLMClient(s, s.cache_dir / "llm"), compiler, label, suites))
    run_.stats.update({k: v for k, v in out.items() if isinstance(v, int)})
    return out
