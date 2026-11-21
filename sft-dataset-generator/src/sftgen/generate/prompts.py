"""`generate-prompts`: fill each planned item's open slots with written and reviewed prompts.

Per item with open slots (see `sftgen.plan.planner`), in priority order:

1. **Ground** it (`sftgen.retrieval.grounding`): its own sources, Central symbols, FTS5 neighbours.
2. **Write**: one call to the writer model (`PROMPT_WRITER_MODEL`, default the DeepSeek teacher)
   with the item, the grounding and the slots (task type × difficulty). Prompts already accepted
   for the item are shown, so gap-fill rounds don't repeat them.
3. **Filter**: too short, refers to "the excerpt", a near-duplicate of a prompt in the set, or
   overlapping the eval set (`balpipe_common.decontam`).
4. **Review**: one call to the judge model (`JUDGE_MODEL`, a different family) scores every
   remaining prompt (on_item, answerable, clear, realistic, task_fit, 1-5). Accepted = verdict
   accept and every score >= `prompts.min_score`.

Every prompt is saved with its status (`accepted` · `rejected` · `duplicate` · `eval_overlap`),
review and grounding. LIMIT_GENERATING caps the prompts written per run (0 = no limit). A
failed call leaves the item's slots open for the next run. At the end, `data/prompts/<set>/`
gets `accepted.jsonl`, `rejected.jsonl` and `coverage.json`.
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
from balpipe_common.ids import content_hash

from sftgen.clean.dedup import QuestionDeduper
from sftgen.generate.llm import LLMClient, Usage, load_prompt
from sftgen.generate.questions import record_usage
from sftgen.plan.planner import ItemPlan, plan, summary
from sftgen.retrieval import fts, symbols
from sftgen.retrieval.grounding import Grounding, ground_item

log = logging.getLogger(__name__)

WRITE, REVIEW = "write-prompts", "review-prompts"
SCORES = ("on_item", "answerable", "clear", "realistic", "task_fit")
CONTEXT_REF_RE = re.compile(r"\b(the|this|above|these) (excerpts?|reference|passages?|documentation above|"
                            r"knowledge item|slot)\b", re.I)


class Written(BaseModel):
    slot: int
    prompt: str


class WrittenList(BaseModel):
    prompts: list[Written] = Field(default_factory=list)


class Review(BaseModel):
    slot: int
    on_item: int = 0
    answerable: int = 0
    clear: int = 0
    realistic: int = 0
    task_fit: int = 0
    verdict: str = "reject"
    reason: str = ""


class ReviewList(BaseModel):
    reviews: list[Review] = Field(default_factory=list)


def family(model: str) -> str:
    """'deepseek/deepseek-v4.1-flash' -> 'deepseek'; 'qwen/qwen3.8-flash' -> 'qwen'."""
    return model.split("/", 1)[0].lower() if "/" in model else re.split(r"[-_.:]", model.lower())[0]


def check_models(s: Settings) -> tuple[str, str]:
    writer, reviewer = s.writer_model, s.reviewer_model
    if not writer or not reviewer:
        raise ConfigError("set PROMPT_WRITER_MODEL (or ANSWER_GEN_MODEL) and JUDGE_MODEL (or QUESTION_GEN_MODEL) "
                          "in .env")
    if family(writer) == family(reviewer):
        raise ConfigError(f"the reviewer ({reviewer}) must be a different model family from the writer ({writer})")
    return writer, reviewer


def accept(r: Review, min_score: int) -> bool:
    return r.verdict.strip().lower() == "accept" and all(getattr(r, k) >= min_score for k in SCORES)


@dataclass
class Outcome:
    plan: ItemPlan
    grounding: Grounding
    round: int
    rows: list[dict[str, Any]]
    error: str | None = None


def _slots_text(slots: list[tuple[str, str]]) -> str:
    return "\n".join(f"{i}. {task} · {diff}" for i, (task, diff) in enumerate(slots, 1))


def _existing_text(prompts: list[str]) -> str:
    if not prompts:
        return ""
    shown = "\n".join(f"- {p[:300]}" for p in prompts[:12])
    return f"\nRequests already written for this item (write different ones):\n{shown}\n"


class PromptGenerator:
    def __init__(self, s: Settings, db: Database, run: RunTracker, client: LLMClient) -> None:
        self.s = s
        self.cfg = s.prompts
        self.db = db
        self.run = run
        self.client = client
        self.writer, self.reviewer = check_models(s)
        self.w_prompt = load_prompt("prompt_writer")
        self.r_prompt = load_prompt("prompt_reviewer")
        self.dedup = QuestionDeduper(self.cfg.near_duplicate_threshold)
        for r in db.query("SELECT prompt FROM prompts WHERE set_name = ? AND status IN ('accepted', 'rejected')",
                          (self.cfg.set_name,)):
            self.dedup.add(r["prompt"])
        self.decontam = Decontaminator.from_db(db, s.eval_)

    async def item(self, p: ItemPlan, slots: list[tuple[str, str]]) -> Outcome:
        g = ground_item(self.db, p.item, p.node, self.cfg)
        rnd = p.rounds + 1
        existing = [r["prompt"] for r in self.db.query(
            "SELECT prompt FROM prompts WHERE set_name = ? AND ref = ? AND status = 'accepted' ORDER BY id",
            (self.cfg.set_name, p.ref))]
        common = {"ref": p.ref, "node_title": p.node["title"], "item_text": p.item["text"],
                  "grounding": g.render()}
        try:
            written, usage = await self.client.chat_json(
                self.writer, self.w_prompt, WrittenList, temperature=self.cfg.writer_temperature,
                max_tokens=self.cfg.max_writer_tokens, node_summary=p.node["summary"],
                modules=", ".join(p.node["modules"]) or "(language feature)",
                out_of_scope="; ".join(p.node["out_of_scope"]) or "none", existing=_existing_text(existing),
                slots=_slots_text(slots), **common)
            record_usage(self.db, self.run, WRITE, usage, self.writer, True)
        except Exception as exc:
            record_usage(self.db, self.run, WRITE, None, self.writer, False)
            return Outcome(p, g, rnd, [], f"writer: {type(exc).__name__}: {exc}"[:500])

        rows: list[dict[str, Any]] = []
        to_review: list[dict[str, Any]] = []
        for w in written.prompts:
            if not 1 <= w.slot <= len(slots):
                continue
            task, diff = slots[w.slot - 1]
            text = w.prompt.strip()
            row = {"slot": w.slot, "task_type": task, "difficulty": diff, "prompt": text, "status": "",
                   "review": None, "eval_overlap": None}
            if len(text) < 20 or CONTEXT_REF_RE.search(text):
                row.update(status="rejected", review={"filter": "too short or refers to its sources"})
            elif self.dedup.is_duplicate(text) or self.db.scalar("SELECT 1 FROM prompts WHERE prompt_hash = ?",
                                                                 (content_hash(text.lower()),)):
                row.update(status="duplicate")
            elif (hit := self.decontam.check(text)) is not None:
                row.update(status="eval_overlap", eval_overlap=hit)
            else:
                to_review.append(row)
                self.dedup.add(text)
            rows.append(row)

        if to_review:
            candidates = "\n\n".join(f"--- slot {r['slot']} · {r['task_type']} · {r['difficulty']}\n{r['prompt']}"
                                     for r in to_review)
            try:
                reviews, usage = await self.client.chat_json(
                    self.reviewer, self.r_prompt, ReviewList, temperature=self.cfg.reviewer_temperature,
                    max_tokens=self.cfg.max_reviewer_tokens, candidates=candidates,
                    task_types=", ".join(self.cfg.task_mix), **common)
                record_usage(self.db, self.run, REVIEW, usage, self.reviewer, True)
            except Exception as exc:
                record_usage(self.db, self.run, REVIEW, None, self.reviewer, False)
                # not saved: the slots stay open and the writer's response is cached for the retry
                return Outcome(p, g, rnd, [], f"reviewer: {type(exc).__name__}: {exc}"[:500])
            by_slot = {r.slot: r for r in reviews.reviews}
            for row in to_review:
                rv = by_slot.get(row["slot"])
                if rv is None:
                    row.update(status="rejected", review={"filter": "no review returned"})
                    continue
                row.update(status="accepted" if accept(rv, self.cfg.min_score) else "rejected",
                           review=rv.model_dump(exclude={"slot"}))
        return Outcome(p, g, rnd, rows)

    def save(self, out: Outcome) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in out.rows:
            h = content_hash(row["prompt"].lower())
            if self.db.scalar("SELECT 1 FROM prompts WHERE prompt_hash = ?", (h,)):
                h = content_hash(f"{row['prompt'].lower()}\x1f{out.plan.ref}\x1f{out.round}\x1f{row['slot']}")
                if self.db.scalar("SELECT 1 FROM prompts WHERE prompt_hash = ?", (h,)):
                    continue
            self.db.insert("prompts", {
                "set_name": self.cfg.set_name, "ref": out.plan.ref, "node_id": out.plan.item["node_id"],
                "task_type": row["task_type"], "difficulty": row["difficulty"], "prompt": row["prompt"],
                "prompt_hash": h, "covers": json.dumps([out.plan.ref]),
                "grounding": json.dumps(out.grounding.summary()), "round": out.round,
                "writer_model": self.writer, "reviewer_model": self.reviewer if row["review"] else None,
                "review": json.dumps(row["review"]) if row["review"] is not None else None,
                "status": row["status"], "eval_overlap": row["eval_overlap"], "run_id": self.run.id})
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            self.run.count(f"prompts_{row['status']}")
        return counts


async def _generate(s: Settings, db: Database, run: RunTracker, client: LLMClient) -> dict[str, Any]:
    gen = PromptGenerator(s, db, run, client)
    cfg = s.prompts
    limit = s.limit_generating or math.inf
    work = [(p, p.missing) for p in plan(s, db) if p.missing and p.rounds < cfg.max_rounds]
    stats: dict[str, Any] = {"items_open": len(work), "items_done": 0, "items_failed": 0, "written": 0,
                             "accepted": 0, "rejected": 0, "duplicate": 0, "eval_overlap": 0}
    log.info("%d item(s) have open slots; writing up to %s prompts with %s, reviewed by %s", len(work),
             "all" if limit == math.inf else int(limit), gen.writer, gen.reviewer)
    queue = list(work)
    written = 0
    while queue and written < limit:
        wave = []
        while queue and len(wave) < max(1, s.generate.concurrency) and written < limit:
            p, slots = queue.pop(0)
            take = slots[: int(min(len(slots), limit - written))]
            written += len(take)
            wave.append(gen.item(p, take))
        for fut in asyncio.as_completed(wave):
            out = await fut
            if out.error:
                stats["items_failed"] += 1
                log.warning("prompts for %s failed: %s", out.plan.ref, out.error)
                continue
            for k, n in gen.save(out).items():
                stats[k] = stats.get(k, 0) + n
            stats["written"] += len(out.rows)
            stats["items_done"] += 1
        run.flush()
    return stats


def prepare(db: Database) -> dict[str, Any]:
    """The indexes grounding needs: FTS5 over chunks and the Central symbol table."""
    rebuilt = fts.refresh(db)
    return {"fts_rebuilt": rebuilt, **symbols.build(db)}


def run(s: Settings, db: Database, run_: RunTracker) -> dict[str, Any]:
    check_models(s)
    s.require("llm_api_key")
    prep = prepare(db)
    client = LLMClient(s, s.cache_dir / "llm")
    stats = asyncio.run(_generate(s, db, run_, client))
    stats["files"] = write_outputs(s, db)
    stats["plan"] = {k: v for k, v in summary(plan(s, db), s.prompts).items() if k != "nodes"}
    run_.stats.update({k: v for k, v in stats.items() if isinstance(v, int)})
    return {**prep, **stats}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_outputs(s: Settings, db: Database) -> dict[str, str]:
    out = s.prompts_dir
    rows = db.query("SELECT * FROM prompts WHERE set_name = ? ORDER BY id", (s.prompts.set_name,))

    def record(r: Any) -> dict[str, Any]:
        g = json.loads(r["grounding"] or "{}")
        return {"id": f"p{r['id']}", "ref": r["ref"], "node": r["node_id"], "covers": json.loads(r["covers"]),
                "task_type": r["task_type"], "difficulty": r["difficulty"], "context_mode": r["context_mode"],
                "prompt": r["prompt"], "grounding_urls": g.get("urls", []), "symbols": g.get("symbols", []),
                "review": json.loads(r["review"]) if r["review"] else None, "status": r["status"],
                "round": r["round"], "writer_model": r["writer_model"], "reviewer_model": r["reviewer_model"]}

    _write_jsonl(out / "accepted.jsonl", [record(r) for r in rows if r["status"] == "accepted"
                                          and r["eval_overlap"] is None])
    _write_jsonl(out / "rejected.jsonl", [record(r) for r in rows if r["status"] != "accepted"])
    plans = plan(s, db)
    coverage = {"set": s.prompts.set_name, **summary(plans, s.prompts), "items": [
        {"ref": p.ref, "priority": p.item["priority"], "budget": len(p.slots), "missing": len(p.missing),
         "rounds": p.rounds} for p in plans]}
    (out / "coverage.json").write_text(json.dumps(coverage, indent=1) + "\n", encoding="utf-8")
    rel = lambda p: str(p.relative_to(s.data_root))  # noqa: E731
    return {"accepted": rel(out / "accepted.jsonl"), "rejected": rel(out / "rejected.jsonl"),
            "coverage": rel(out / "coverage.json")}
