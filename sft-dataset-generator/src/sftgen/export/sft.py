"""`export-sft`: answered questions -> chat-format files for supervised fine-tuning.

Writes to `sft.dir` (default data/sft/):

* train.jsonl, eval.jsonl   {"id": ..., "messages": [system?, user, assistant]}
* metadata.jsonl            id, split, source_url, category, difficulty, validation

The `messages` format is model-agnostic: the trainer applies the base model's
chat template (`tokenizer.apply_chat_template`). It loads directly in TRL
`SFTTrainer`, Axolotl (`type: chat_template`) and Unsloth.

* The train/eval split is by source page (hash of `source_url`), so no page
  feeds both sides.
* The trailing `Source: <url>` line is removed by default, so the model does
  not learn to end answers with URLs it would then invent.
* Answers marked `invalid` by `validate` are left out, as in `export`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.ids import sha256_text

SOURCE_LINE_RE = re.compile(r"\n*\s*Source:\s*\S+\s*$")


def split_for(source_url: str, eval_fraction: float) -> str:
    bucket = int(sha256_text(f"split:{source_url}")[:8], 16) / 0x1_0000_0000
    return "eval" if bucket < eval_fraction else "train"


def strip_source(answer: str) -> str:
    return SOURCE_LINE_RE.sub("", answer).rstrip()


def to_messages(question: str, answer: str, system_prompt: str) -> list[dict[str, str]]:
    msgs = [{"role": "system", "content": system_prompt}] if system_prompt.strip() else []
    return msgs + [{"role": "user", "content": question.strip()}, {"role": "assistant", "content": answer.strip()}]


def _write_atomic(path: Path, rows: list[dict]) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def run(s: Settings, db: Database, run_: RunTracker) -> dict[str, int]:
    cfg = s.sft
    out = s.path(cfg.dir)
    out.mkdir(parents=True, exist_ok=True)
    status_filter = "" if s.export.include_invalid else "AND a.status != 'invalid'"
    rows = db.query(
        "SELECT q.id, q.question, a.answer, q.source_url, q.source, q.category, q.difficulty, a.status AS validation "
        f"FROM questions q JOIN answers a ON a.question_id = q.id WHERE q.status = 'answered' {status_filter} "
        "ORDER BY q.id")
    files: dict[str, list[dict]] = {"train": [], "eval": [], "metadata": []}
    for r in rows:
        answer = strip_source(r["answer"]) if cfg.strip_source_line else r["answer"]
        if not answer.strip():
            continue
        rid = f"q{r['id']}"
        split = split_for(r["source_url"], cfg.eval_fraction)
        files[split].append({"id": rid, "messages": to_messages(r["question"], answer, cfg.system_prompt)})
        files["metadata"].append({"id": rid, "split": split, "source_url": r["source_url"], "source": r["source"],
                                  "category": r["category"], "difficulty": r["difficulty"],
                                  "validation": r["validation"]})
    for name, items in files.items():
        _write_atomic(out / f"{name}.jsonl", items)
    stats = {"sft_train": len(files["train"]), "sft_eval": len(files["eval"])}
    run_.stats.update(stats)
    return stats
