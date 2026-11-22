"""`answer-prompts`: verified teacher answers for accepted prompts, with a repair loop.

For each accepted prompt of the set without a response (up to LIMIT_GENERATING per run):

1. **Answer.** The teacher (`ANSWER_GEN_MODEL`, the DeepSeek teacher) answers with the item's
   grounding, following the answer-format contract (`prompts/prompt_answer.yaml`).
2. **Check** (`validate/answer_check.py`):
   - the format: no mention of reference material, no `Source:` lines;
   - the code: every ```ballerina block compiles; `// file:` blocks are built as one package;
     tests are compiled, and run only with `answers.run_tests`;
   - code tasks must contain code.
3. **Judge.** Non-code tasks (`answers.judge_tasks`), and any answer without code, are checked by
   the judge (`JUDGE_MODEL`, a different family from the teacher) against the grounding.
4. **Repair.** On failure the teacher gets its answer back with the real problems (compiler errors
   with file and line, failing tests, or the judge's issues) and rewrites it, up to
   `answers.max_attempts` attempts in all.

Every attempt is saved in `attempts`; the outcome per prompt in `responses` (`verified` = code
checked by the toolchain, `judged` = prose checked by the judge, `failed`). When a repair passes,
the first failed attempt and the passing one become a **preference pair**.
`--retry-failed` continues failed prompts with further repairs. Outputs: `data/answers/<set>/`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from balpipe_common.config import ConfigError, Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.decontam import Decontaminator

from sftgen.generate.llm import LLMClient, load_prompt
from sftgen.generate.prompts import family
from sftgen.generate.questions import record_usage
from sftgen.plan.planner import load_nodes
from sftgen.retrieval.grounding import ground_item
from sftgen.validate.answer_check import AnswerChecker, CodeCheck
from sftgen.validate.bal_compiler import BalCompiler, BalUnavailable

log = logging.getLogger(__name__)

ANSWER, REPAIR, JUDGE = "answer-prompts", "repair-answers", "judge-answers"
LEAK_RE = re.compile(r"\b(reference material|the (provided |given )?excerpts?|provided documentation|"
                     r"documentation (provided|above))\b|^\s*Source:\s*https?://", re.I | re.M)


class Judgement(BaseModel):
    correct: int = 0
    complete: int = 0
    clear: int = 0
    verdict: str = "fail"
    issues: list[str] = Field(default_factory=list)


def check_models(s: Settings) -> tuple[str, str]:
    teacher, judge = s.answer_gen_model, s.reviewer_model
    if not teacher or not judge:
        raise ConfigError("set ANSWER_GEN_MODEL (the teacher) and JUDGE_MODEL (or QUESTION_GEN_MODEL) in .env")
    if family(teacher) == family(judge):
        raise ConfigError(f"the judge ({judge}) must be a different model family from the teacher ({teacher})")
    return teacher, judge


@dataclass
class Result:
    status: str                  # pass | fail
    check: CodeCheck
    judge: dict[str, Any] | None
    feedback: str | None         # what failed, for the next repair
    reason: str = ""             # leak | compile | test | missing_code | timeout | judge


class Responder:
    def __init__(self, s: Settings, db: Database, run: RunTracker, client: LLMClient, checker: AnswerChecker) -> None:
        self.s = s
        self.cfg = s.answers
        self.db = db
        self.run = run
        self.client = client
        self.checker = checker
        self.teacher, self.judge = check_models(s)
        self.p_answer = load_prompt("prompt_answer")
        self.p_repair = load_prompt("prompt_answer_repair")
        self.p_judge = load_prompt("answer_judge")
        self.nodes = load_nodes(s)
        self.bal_slots = asyncio.Semaphore(max(1, s.validate_.workers))
        self.decontam = Decontaminator.from_db(db, s.eval_)

    def grounding(self, p: Any) -> str:
        item = self.db.one("SELECT * FROM graph_items WHERE ref = ?", (p["ref"],))
        if item is None:
            return "(no reference material)"
        return ground_item(self.db, dict(item), self.nodes.get(p["node_id"], {}), self.s.prompts).render()

    async def evaluate(self, p: Any, text: str, grounding: str) -> Result:
        if LEAK_RE.search(text):
            chk = CodeCheck("fail", "leak")
            return Result("fail", chk, None, "The answer mentions reference material, excerpts or a Source line. "
                                             "Answer the developer directly, without them.", "leak")
        async with self.bal_slots:
            chk = await asyncio.to_thread(self.checker.check, text, p["task_type"])
        if chk.status == "fail":
            return Result("fail", chk, None, chk.feedback(self.cfg.max_feedback_errors), chk.reason)
        if p["task_type"] not in self.cfg.judge_tasks and chk.status == "pass":
            return Result("pass", chk, None, None)
        j, usage = await self.client.chat_json(
            self.judge, self.p_judge, Judgement, temperature=0.0, max_tokens=self.cfg.max_judge_tokens,
            grounding=grounding, task_type=p["task_type"], difficulty=p["difficulty"], request=p["prompt"],
            answer=text)
        record_usage(self.db, self.run, JUDGE, usage, self.judge, True)
        ok = j.verdict.strip().lower() == "pass" and min(j.correct, j.complete, j.clear) >= self.cfg.judge_min_score
        if ok:
            return Result("pass", chk, j.model_dump(), None)
        issues = "\n".join(f"- {i}" for i in j.issues[:8]) or "- the answer is inaccurate or incomplete"
        return Result("fail", chk, j.model_dump(), f"A reviewer found these problems:\n{issues}", "judge")

    async def answer(self, p: Any) -> dict[str, Any]:
        """Run the attempt chain for one prompt. Returns counters; raises on model errors."""
        grounding = self.grounding(p)
        prev = self.db.query("SELECT * FROM attempts WHERE prompt_id = ? ORDER BY n", (p["id"],))
        n = len(prev)
        last_text = prev[-1]["answer"] if prev else None
        last_feedback = prev[-1]["feedback"] if prev else None
        first_failed = next((a for a in prev if a["status"] == "fail"), None)
        fields = dict(grounding=grounding, task_type=p["task_type"], difficulty=p["difficulty"], request=p["prompt"])
        # up to max_attempts per round: a chain cut short by a model error resumes within its round;
        # --retry-failed starts a new round of repairs after a failed one
        limit = n + self.cfg.max_attempts - n % self.cfg.max_attempts
        final: Any = None
        while n < limit:
            n += 1
            if last_text is None:
                text, usage = await self.client.chat(self.teacher, self.p_answer, temperature=self.cfg.temperature,
                                                     max_tokens=self.cfg.max_tokens, **fields)
                record_usage(self.db, self.run, ANSWER, usage, self.teacher, True)
            else:
                text, usage = await self.client.chat(self.teacher, self.p_repair, temperature=self.cfg.temperature,
                                                     max_tokens=self.cfg.max_tokens, previous=last_text,
                                                     feedback=last_feedback or "", **fields)
                record_usage(self.db, self.run, REPAIR, usage, self.teacher, True)
            res = await self.evaluate(p, text, grounding)
            aid = self.db.insert("attempts", {
                "prompt_id": p["id"], "n": n, "model": self.teacher, "answer": text, "feedback": res.feedback,
                "checks": json.dumps({**res.check.as_dict(), "judge": res.judge, "reason": res.reason}),
                "status": res.status, "run_id": self.run.id})
            final = self.db.one("SELECT * FROM attempts WHERE id = ?", (aid,))
            if res.status == "pass":
                break
            if first_failed is None:
                first_failed = final
            last_text, last_feedback = text, res.feedback

        passed = final["status"] == "pass"
        checks = json.loads(final["checks"])
        code = checks["status"] == "pass"
        judged = checks.get("judge") is not None
        verification = "+".join(x for x in ((checks["verification"] if code else ""), "judge" if judged else "") if x)
        status = ("verified" if code else "judged") if passed else "failed"
        overlap = self.decontam.check(f"{p['prompt']}\n{final['answer']}") if passed else None
        values = {"set_name": p["set_name"], "attempt_id": final["id"], "attempts": final["n"],
                  "repaired": int(passed and first_failed is not None), "verification": verification or None,
                  "status": status, "eval_overlap": overlap, "run_id": self.run.id}
        row = self.db.one("SELECT id FROM responses WHERE prompt_id = ?", (p["id"],))
        if row:
            self.db.update("responses", row["id"], values)
        else:
            self.db.insert("responses", {"prompt_id": p["id"], **values})
        if passed and first_failed is not None and not self.db.scalar(
                "SELECT 1 FROM preference_pairs WHERE prompt_id = ?", (p["id"],)):
            reason = json.loads(first_failed["checks"]).get("reason") or "compile"
            self.db.insert("preference_pairs", {"prompt_id": p["id"], "chosen_id": final["id"],
                                                "rejected_id": first_failed["id"], "reason": reason})
        return {"status": status, "repaired": int(passed and first_failed is not None), "attempts": final["n"]}


def pending(s: Settings, db: Database) -> list[Any]:
    return db.query(
        "SELECT p.* FROM prompts p WHERE p.set_name = ? AND p.status = 'accepted' AND p.eval_overlap IS NULL "
        "AND NOT EXISTS (SELECT 1 FROM responses r WHERE r.prompt_id = p.id) ORDER BY p.id",
        (s.prompts.set_name,))


async def _answer_all(s: Settings, db: Database, run: RunTracker, client: LLMClient,
                      checker: AnswerChecker) -> dict[str, Any]:
    r = Responder(s, db, run, client, checker)
    rows = pending(s, db)
    limit = s.limit_generating or math.inf
    rows = rows[: int(min(len(rows), limit))]
    stats: dict[str, Any] = {"prompts_pending": len(pending(s, db)), "answered": 0, "verified": 0, "judged": 0,
                             "failed": 0, "repaired": 0, "errors": 0}
    log.info("answering %d prompt(s) with %s; judge %s; tests %s", len(rows), r.teacher, r.judge,
             "run" if s.answers.run_tests else "compiled only")

    async def one(p: Any) -> tuple[Any, dict[str, Any] | None, Exception | None]:
        try:
            return p, await r.answer(p), None
        except Exception as exc:
            return p, None, exc

    step = max(1, s.generate.concurrency)
    for start in range(0, len(rows), step):
        for fut in asyncio.as_completed([one(p) for p in rows[start:start + step]]):
            p, out, exc = await fut
            if exc is not None or out is None:
                stats["errors"] += 1
                record_usage(db, run, ANSWER, None, r.teacher, False)
                log.warning("answer for prompt %s failed: %s", p["id"], exc)
                continue
            stats["answered"] += 1
            stats[out["status"]] += 1
            stats["repaired"] += out["repaired"]
            run.count(f"responses_{out['status']}")
        run.flush()
    return stats


def run(s: Settings, db: Database, run_: RunTracker, *, retry_failed: bool = False) -> dict[str, Any]:
    check_models(s)
    s.require("llm_api_key")
    try:
        compiler = BalCompiler(s.validate_, s.cache_dir / "bal")
    except BalUnavailable as exc:
        raise ConfigError(f"answer-prompts checks code with the Ballerina toolchain: {exc}") from exc
    if retry_failed:
        n = db.execute("DELETE FROM responses WHERE set_name = ? AND status = 'failed'",
                       (s.prompts.set_name,)).rowcount
        log.info("continuing %d failed prompt(s) with more repairs", n)
    compiler.prewarm()
    client = LLMClient(s, s.cache_dir / "llm")
    stats = asyncio.run(_answer_all(s, db, run_, client, AnswerChecker(s.answers, compiler)))
    stats["files"] = write_outputs(s, db)
    run_.stats.update({k: v for k, v in stats.items() if isinstance(v, int)})
    return stats


# --------------------------------------------------------------------------- #
# Outputs
# --------------------------------------------------------------------------- #
def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_outputs(s: Settings, db: Database) -> dict[str, str]:
    """sft.jsonl (verified and judged answers as chat messages), pairs.jsonl (DPO), failed.jsonl."""
    out = s.answers_dir
    system = s.sft.system_prompt.strip()
    head = [{"role": "system", "content": system}] if system else []
    rows = db.query(
        "SELECT r.*, p.prompt, p.ref, p.covers, p.task_type, p.difficulty, a.answer, a.checks "
        "FROM responses r JOIN prompts p ON p.id = r.prompt_id JOIN attempts a ON a.id = r.attempt_id "
        "WHERE r.set_name = ? ORDER BY r.prompt_id", (s.prompts.set_name,))
    sft, failed = [], []
    for r in rows:
        meta = {"id": f"p{r['prompt_id']}", "covers": json.loads(r["covers"]), "task_type": r["task_type"],
                "difficulty": r["difficulty"], "verification": r["verification"], "attempts": r["attempts"],
                "repaired": bool(r["repaired"])}
        if r["status"] == "failed":
            failed.append({**meta, "prompt": r["prompt"], "answer": r["answer"], "checks": json.loads(r["checks"])})
        elif r["eval_overlap"] is None:
            sft.append({**meta, "messages": head + [{"role": "user", "content": r["prompt"].strip()},
                                                    {"role": "assistant", "content": r["answer"].strip()}]})
    pairs = []
    for r in db.query(
            "SELECT pp.*, p.prompt, p.covers, c.answer AS chosen, x.answer AS rejected FROM preference_pairs pp "
            "JOIN prompts p ON p.id = pp.prompt_id JOIN attempts c ON c.id = pp.chosen_id "
            "JOIN attempts x ON x.id = pp.rejected_id JOIN responses r ON r.prompt_id = pp.prompt_id "
            "WHERE p.set_name = ? AND r.eval_overlap IS NULL ORDER BY pp.prompt_id", (s.prompts.set_name,)):
        pairs.append({"id": f"p{r['prompt_id']}", "covers": json.loads(r["covers"]), "reason": r["reason"],
                      "prompt": head + [{"role": "user", "content": r["prompt"].strip()}],
                      "chosen": [{"role": "assistant", "content": r["chosen"].strip()}],
                      "rejected": [{"role": "assistant", "content": r["rejected"].strip()}]})
    _write_jsonl(out / "sft.jsonl", sft)
    _write_jsonl(out / "pairs.jsonl", pairs)
    _write_jsonl(out / "failed.jsonl", failed)
    rel = lambda p: str(p.relative_to(s.data_root))  # noqa: E731
    return {"sft": rel(out / "sft.jsonl"), "pairs": rel(out / "pairs.jsonl"), "failed": rel(out / "failed.jsonl")}
