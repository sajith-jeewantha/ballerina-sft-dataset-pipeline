"""`generate-answers`: every question with status `pending` -> a grounded answer.

The answer prompt gets the question AND the saved chunk it was generated from,
so the answer is checked against the same source text. Each answer is saved
as soon as it arrives; the question becomes `answered`, or `failed` with the
error message. `--retry-failed` puts failed questions back to `pending` first.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker

from sftgen.generate.llm import LLMClient, Usage, load_prompt
from sftgen.generate.questions import record_usage

log = logging.getLogger(__name__)
PHASE = "generate-answers"


def ensure_citation(answer: str, source_url: str) -> str:
    """Every answer ends by citing its source URL."""
    if source_url in answer:
        return answer
    return f"{answer.rstrip()}\n\nSource: {source_url}"


async def _answer_all(s: Settings, db: Database, run: RunTracker, client: LLMClient) -> dict[str, int]:
    model = s.answer_gen_model or ""
    prompt = load_prompt("answer")
    rows = db.query(
        "SELECT q.id, q.question, q.source_url, c.text AS chunk, c.heading_path FROM questions q "
        "JOIN chunks c ON c.id = q.chunk_id WHERE q.status = 'pending' ORDER BY q.id")
    stats = {"questions_pending": len(rows), "answered": 0, "failed": 0}
    if s.limit_generating:
        rows = rows[:s.limit_generating]  # LIMIT_GENERATING: questions answered per run; the rest stay pending
    log.info("%d questions pending; answering %d", stats["questions_pending"], len(rows))

    async def answer(q: sqlite3.Row) -> tuple[sqlite3.Row, str | None, Usage | None, Exception | None]:
        try:
            chunk = f"{q['heading_path']}\n\n{q['chunk']}" if q["heading_path"] else q["chunk"]
            text, usage = await client.chat(model, prompt, temperature=s.generate.answer_temperature,
                                            max_tokens=s.generate.max_answer_tokens, question=q["question"],
                                            chunk=chunk, source_url=q["source_url"])
            return q, text, usage, None
        except Exception as exc:
            return q, None, None, exc

    step = max(1, s.generate.concurrency) * 4  # bounded number of tasks in flight; the client limits concurrency
    for start in range(0, len(rows), step):
        for fut in asyncio.as_completed([answer(q) for q in rows[start:start + step]]):
            q, text, usage, exc = await fut
            record_usage(db, run, PHASE, usage, model, exc is None)
            if exc is not None or not text:
                db.update("questions", q["id"], {"status": "failed", "error": f"{type(exc).__name__}: {exc}"[:2000]})
                stats["failed"] += 1
                run.count("answers_failed")
                log.warning("answer failed for question %s: %s", q["id"], exc)
                continue
            db.insert("answers", {"question_id": q["id"], "answer": ensure_citation(text, q["source_url"]),
                                  "model": model, "status": "not_checked", "run_id": run.id})
            db.update("questions", q["id"], {"status": "answered", "error": None})
            stats["answered"] += 1
            run.count("answers_saved")
        run.flush()
    return stats


def run(s: Settings, db: Database, run_: RunTracker, *, retry_failed: bool = False) -> dict[str, int]:
    s.require("llm_api_key", "answer_gen_model")
    if retry_failed:
        n = db.execute("UPDATE questions SET status = 'pending', error = NULL WHERE status = 'failed'").rowcount
        log.info("retrying %d failed questions", n)
    client = LLMClient(s, s.cache_dir / "llm")
    stats = asyncio.run(_answer_all(s, db, run_, client))
    run_.stats.update(stats)
    return stats
