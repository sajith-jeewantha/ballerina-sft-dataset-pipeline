"""`generate-questions`: chunks without questions -> QUESTIONS_PER_CHUNK questions each.

* Stops once LIMIT_GENERATING questions were saved in this run (0 = no limit).
* Chunks are taken round-robin across sources (in `sources.order`, BBE first),
  so even a small run covers every source.
* Each question is saved the moment its chunk's response arrives.
* Near-duplicate questions (vs. everything already saved) are skipped.
* A chunk whose call fails is retried on later runs, up to
  `generate.max_chunk_attempts`; then it is marked `failed`.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import sqlite3
from collections import defaultdict, deque

from pydantic import BaseModel, Field

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.ids import content_hash

from sftgen.clean.dedup import QuestionDeduper
from sftgen.generate.llm import LLMClient, Usage, load_prompt

log = logging.getLogger(__name__)

PHASE = "generate-questions"
CATEGORIES = ("concept", "syntax", "library", "tooling", "error-fixing", "code-writing", "best-practice")
DIFFICULTIES = ("beginner", "intermediate", "advanced")
CONTEXT_REF_RE = re.compile(r"\b(the|this|above) (excerpt|text|passage|documentation|page|section)\b", re.IGNORECASE)


class QItem(BaseModel):
    question: str
    category: str = "concept"
    difficulty: str = "intermediate"


class QList(BaseModel):
    questions: list[QItem] = Field(default_factory=list)


def normalize_category(value: str) -> str:
    v = value.strip().lower().replace("_", "-").replace(" ", "-")
    aliases = {"error-handling": "error-fixing", "debugging": "error-fixing", "code-generation": "code-writing",
               "coding": "code-writing", "api": "library", "api-usage": "library", "tools": "tooling",
               "best-practices": "best-practice", "concepts": "concept"}
    v = aliases.get(v, v)
    return v if v in CATEGORIES else "concept"


def normalize_difficulty(value: str) -> str:
    v = value.strip().lower()
    return v if v in DIFFICULTIES else "intermediate"


def record_usage(db: Database, run: RunTracker, phase: str, usage: Usage | None, model: str, ok: bool) -> None:
    u = usage or Usage(model)
    db.insert("llm_usage", {"run_id": run.id, "phase": phase, "model": u.model, "prompt_tokens": u.prompt_tokens,
                            "completion_tokens": u.completion_tokens, "cost_usd": u.cost_usd,
                            "cached": int(u.cached), "provider": u.provider, "status": "ok" if ok else "error"})


def round_robin(rows: list[sqlite3.Row], order: list[str]) -> deque[sqlite3.Row]:
    by_source: dict[str, deque[sqlite3.Row]] = defaultdict(deque)
    for r in rows:
        by_source[r["source"]].append(r)
    sources = [s for s in order if s in by_source] + sorted(set(by_source) - set(order))
    out: deque[sqlite3.Row] = deque()
    while any(by_source[s] for s in sources):
        for s in sources:
            if by_source[s]:
                out.append(by_source[s].popleft())
    return out


def pending_chunks(s: Settings, db: Database) -> deque[sqlite3.Row]:
    rows = db.query(
        "SELECT c.* FROM chunks c WHERE c.status = 'pending' AND c.eval_overlap IS NULL AND c.attempts < ? "
        "AND NOT EXISTS (SELECT 1 FROM questions q WHERE q.chunk_id = c.id) ORDER BY c.page_id, c.ordinal",
        (s.generate.max_chunk_attempts,),
    )
    return round_robin(rows, s.sources.order)


async def _generate(s: Settings, db: Database, run: RunTracker, client: LLMClient) -> dict[str, int]:
    model = s.question_gen_model or ""
    prompt = load_prompt("questions")
    qpc = s.questions_per_chunk
    limit = s.limit_generating or math.inf
    queue = pending_chunks(s, db)
    dedup = QuestionDeduper(s.generate.near_duplicate_threshold)
    for row in db.query("SELECT question FROM questions"):
        dedup.add(row["question"])
    stats = {"chunks_available": len(queue), "chunks_processed": 0, "questions_saved": 0,
             "questions_duplicate": 0, "questions_rejected": 0, "chunks_failed": 0}
    log.info("%d chunks need questions; generating up to %s questions", len(queue),
             "all" if limit == math.inf else int(limit))

    async def ask(chunk: sqlite3.Row) -> tuple[sqlite3.Row, QList | None, Usage | None, Exception | None]:
        try:
            parsed, usage = await client.chat_json(
                model, prompt, QList, temperature=s.generate.question_temperature,
                max_tokens=s.generate.max_question_tokens, n=qpc, source_url=chunk["source_url"],
                heading=chunk["heading_path"], chunk=chunk["text"])
            return chunk, parsed, usage, None
        except Exception as exc:
            return chunk, None, None, exc

    saved = 0
    while queue and saved < limit:
        wave_size = min(s.generate.concurrency, len(queue), math.ceil((limit - saved) / qpc) if limit != math.inf
                        else len(queue))
        wave = [queue.popleft() for _ in range(max(1, wave_size))]
        for fut in asyncio.as_completed([ask(c) for c in wave]):
            chunk, parsed, usage, exc = await fut
            record_usage(db, run, PHASE, usage, model, exc is None)
            if exc is not None:
                attempts = chunk["attempts"] + 1
                status = "failed" if attempts >= s.generate.max_chunk_attempts else "pending"
                db.update("chunks", chunk["id"], {"attempts": attempts, "status": status,
                                                  "error": f"{type(exc).__name__}: {exc}"[:2000]})
                stats["chunks_failed"] += 1
                log.warning("question generation failed for chunk %s: %s", chunk["id"], exc)
                continue
            assert parsed is not None
            if saved >= limit:
                continue  # response is cached on disk; a later run reuses it at no cost
            n_saved = 0
            for item in parsed.questions[:qpc]:
                if saved >= limit:
                    break
                text = re.sub(r"\s+", " ", item.question).strip()
                if len(text) < 12 or CONTEXT_REF_RE.search(text):
                    stats["questions_rejected"] += 1
                    continue
                if dedup.is_duplicate(text) or db.scalar("SELECT 1 FROM questions WHERE question_hash = ?",
                                                         (content_hash(text.lower()),)):
                    stats["questions_duplicate"] += 1
                    continue
                db.insert("questions", {
                    "chunk_id": chunk["id"], "source": chunk["source"], "source_url": chunk["source_url"],
                    "question": text, "question_hash": content_hash(text.lower()),
                    "category": normalize_category(item.category), "difficulty": normalize_difficulty(item.difficulty),
                    "status": "pending", "model": model, "run_id": run.id,
                })
                dedup.add(text)
                saved += 1
                n_saved += 1
                run.count("questions_saved")
            db.update("chunks", chunk["id"], {"status": "questioned", "attempts": chunk["attempts"] + 1,
                                              "error": None if n_saved else "no new questions (duplicates/rejected)"})
            stats["chunks_processed"] += 1
        run.flush()
    stats["questions_saved"] = saved
    return stats


def run(s: Settings, db: Database, run_: RunTracker) -> dict[str, int]:
    s.require("llm_api_key", "question_gen_model")
    client = LLMClient(s, s.cache_dir / "llm")
    stats = asyncio.run(_generate(s, db, run_, client))
    run_.stats.update(stats)
    return stats

