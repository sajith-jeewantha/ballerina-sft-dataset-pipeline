"""`export`: answered questions -> data/dataset.jsonl

One JSON object per line: question, answer, source_url, category, difficulty.
Answers marked `invalid` by `validate` are left out unless
`export.include_invalid` is true. Questions that overlap the eval set
(`questions.eval_overlap`, see balpipe_common.decontam) are always left out, and
the written rows are re-checked against the eval fingerprints. The file is
written atomically.
"""

from __future__ import annotations

import json
import os
import tempfile

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.decontam import export_guard

FIELDS = ("question", "answer", "source_url", "category", "difficulty")


def run(s: Settings, db: Database, run_: RunTracker) -> dict[str, int]:
    out = s.export_path
    out.parent.mkdir(parents=True, exist_ok=True)
    status_filter = "" if s.export.include_invalid else "AND a.status != 'invalid'"
    rows = db.query(
        "SELECT q.question, a.answer, q.source_url, q.category, q.difficulty FROM questions q "
        f"JOIN answers a ON a.question_id = q.id WHERE q.status = 'answered' AND q.eval_overlap IS NULL "
        f"{status_filter} ORDER BY q.id")
    overlap = db.scalar("SELECT COUNT(*) FROM questions q JOIN answers a ON a.question_id = q.id "
                        f"WHERE q.status = 'answered' AND q.eval_overlap IS NOT NULL {status_filter}")
    skipped = db.scalar("SELECT COUNT(*) FROM answers WHERE status = 'invalid'") if not s.export.include_invalid else 0
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=".dataset-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: r[k] for k in FIELDS}, ensure_ascii=False) + "\n")
    os.replace(tmp, out)
    stats = {"rows_exported": len(rows), "invalid_skipped": int(skipped or 0),
             "eval_overlap_dropped": int(overlap or 0),
             **export_guard(db, s.eval_, [f"{r['question']}\n{r['answer']}" for r in rows])}
    run_.stats.update(stats)
    return stats
