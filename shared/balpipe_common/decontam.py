"""Decontamination: keep copies and near-copies of eval items out of everything used for training.

The eval items' code is fingerprinted by `sftgen build-eval` (table `fingerprints`). A text
overlaps an eval item when any of these hold:

* **exact**: the same code with comments and layout ignored (`code_hash`), for the whole
  text or for any fenced code block in it;
* **near**: MinHash similarity >= `eval.near_duplicate_threshold` (whole files);
* **contained**: the text holds >= `eval.containment` of the item's *distinctive* 5-token
  shingles. Shingles common to many BBE examples (imports, `public function main()`, ...)
  were dropped when the fingerprints were built, so boilerplate never triggers a match, and
  items with fewer than `eval.min_shingles` distinctive shingles are matched exactly only.

`apply_decontam` checks chunks, questions (with their answers), prompts, prompt answers and repo
`.bal` files and
records the overlapping item in `eval_overlap`. Rows are re-checked only when the
fingerprint set changes (`decontam_key`) or a question gets its answer. Chunks of eval pages,
and chunks that overlap, move from `pending` to `eval_holdout`, so no questions are generated
from them. Every export leaves out questions with an `eval_overlap`.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

import numpy as np

from balpipe_common.config import EvalCfg
from balpipe_common.dedup import LSH, MinHasher, code_hash, shingle_hashes
from balpipe_common.ids import sha256_text, utc_now_iso

FENCE_RE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)


def pack_shingles(hashes: set[int]) -> bytes:
    return np.array(sorted(hashes), dtype="<u4").tobytes()


def unpack_shingles(data: bytes) -> set[int]:
    return set(np.frombuffer(data, dtype="<u4").tolist())


@dataclass
class Fingerprint:
    item_id: str
    path: str
    code_hash: str
    minhash: np.ndarray
    shingles: set[int]


class Decontaminator:
    def __init__(self, fps: list[Fingerprint], cfg: EvalCfg) -> None:
        self.cfg = cfg
        self.fps = fps
        self.hasher = MinHasher()
        self.exact = {f.code_hash: f.item_id for f in fps}
        self.lsh = LSH(threshold=cfg.near_duplicate_threshold)
        self.by_key: dict[int, Fingerprint] = {}
        self.index: dict[int, list[int]] = defaultdict(list)  # shingle -> fingerprint keys
        for k, f in enumerate(fps):
            self.by_key[k] = f
            self.lsh.add(k, f.minhash)
            if len(f.shingles) >= cfg.min_shingles:
                for h in f.shingles:
                    self.index[h].append(k)
        basis = sorted(f"{f.item_id}:{f.path}:{f.code_hash}" for f in fps)
        settings = f"{cfg.near_duplicate_threshold}:{cfg.containment}:{cfg.min_shingles}"
        self.key = sha256_text("\n".join([settings, *basis]))[:16] if fps else ""

    def __bool__(self) -> bool:
        return bool(self.fps)

    @classmethod
    def from_db(cls, db: Any, cfg: EvalCfg) -> Decontaminator:
        hasher = MinHasher()
        rows = db.query("SELECT f.item_id, f.path, f.code_hash, f.minhash, f.shingles FROM fingerprints f "
                        "JOIN eval_items e ON e.id = f.item_id WHERE e.status = 'active' ORDER BY f.id")
        return cls([Fingerprint(r["item_id"], r["path"], r["code_hash"], hasher.from_bytes(r["minhash"]),
                                unpack_shingles(r["shingles"])) for r in rows], cfg)

    def check(self, text: str, minhash: bytes | None = None) -> str | None:
        """The id of the eval item this text copies, or None."""
        if not self.fps or not text or not text.strip():
            return None
        for code in (text, *FENCE_RE.findall(text)):
            hit = self.exact.get(code_hash(code))
            if hit:
                return hit
        sig = self.hasher.from_bytes(minhash) if minhash is not None else self.hasher.signature(text)
        near = self.lsh.query(sig)
        if near is not None:
            return self.by_key[near].item_id
        counts: dict[int, int] = defaultdict(int)
        for h in shingle_hashes(text):
            for k in self.index.get(h, ()):
                counts[k] += 1
        best, best_cov = None, 0.0
        for k, n in counts.items():
            cov = n / len(self.by_key[k].shingles)
            if cov >= self.cfg.containment and cov > best_cov:
                best, best_cov = self.by_key[k].item_id, cov
        return best


def apply_decontam(db: Any, cfg: EvalCfg, dc: Decontaminator | None = None) -> dict[str, int]:
    """Flag chunks, questions and repo files that overlap the eval set. Idempotent and incremental."""
    dc = dc if dc is not None else Decontaminator.from_db(db, cfg)
    key = dc.key
    now = utc_now_iso()
    eval_pages = {r["url"]: r["id"] for r in db.query("SELECT id, url FROM eval_items WHERE status = 'active'")}
    stats = {"chunks_checked": 0, "questions_checked": 0, "prompts_checked": 0, "responses_checked": 0,
             "repo_files_checked": 0}

    # chunks: eval pages are held out whole; other chunks by their text
    rows = db.query("SELECT c.id, c.text, c.status, p.url FROM chunks c JOIN pages p ON p.id = c.page_id "
                    "WHERE c.decontam_key IS NOT ?", (key,))
    updates = []
    for r in rows:
        hit = eval_pages.get(r["url"]) or dc.check(r["text"])
        status = r["status"]
        if hit and status == "pending":
            status = "eval_holdout"
        elif not hit and status == "eval_holdout":
            status = "pending"
        updates.append((hit, key, status, now, r["id"]))
    db.conn.executemany("UPDATE chunks SET eval_overlap = ?, decontam_key = ?, status = ?, updated_at = ? "
                        "WHERE id = ?", updates)
    db.conn.commit()  # one table at a time: other writers wait only for the UPDATE itself
    stats["chunks_checked"] = len(updates)

    # questions: the source chunk, the question and its answer; re-checked once an answer arrives
    rows = db.query(
        "SELECT q.id, q.question, a.answer, a.id AS answer_id, c.eval_overlap AS chunk_overlap FROM questions q "
        "LEFT JOIN answers a ON a.question_id = q.id LEFT JOIN chunks c ON c.id = q.chunk_id "
        "WHERE q.decontam_key IS NOT (? || ':' || COALESCE(a.id, 0))", (key,))
    updates = []
    for r in rows:
        hit = r["chunk_overlap"] or dc.check(f"{r['question']}\n{r['answer'] or ''}")
        updates.append((hit, f"{key}:{r['answer_id'] or 0}", now, r["id"]))
    db.conn.executemany("UPDATE questions SET eval_overlap = ?, decontam_key = ?, updated_at = ? WHERE id = ?",
                        updates)
    db.conn.commit()  # one table at a time: other writers wait only for the UPDATE itself
    stats["questions_checked"] = len(updates)

    # prompts (generate-prompts already skips overlapping ones; this re-checks after an eval change)
    rows = db.query("SELECT id, prompt FROM prompts WHERE decontam_key IS NOT ?", (key,))
    updates = [(dc.check(r["prompt"]), key, now, r["id"]) for r in rows]
    db.conn.executemany("UPDATE prompts SET eval_overlap = ?, decontam_key = ?, updated_at = ? WHERE id = ?",
                        updates)
    db.conn.commit()  # one table at a time: other writers wait only for the UPDATE itself
    stats["prompts_checked"] = len(updates)

    # final answers to prompts
    rows = db.query("SELECT r.id, p.prompt, a.answer FROM responses r JOIN prompts p ON p.id = r.prompt_id "
                    "JOIN attempts a ON a.id = r.attempt_id WHERE r.decontam_key IS NOT ?", (key,))
    updates = [(dc.check(f"{r['prompt']}\n{r['answer']}"), key, now, r["id"]) for r in rows]
    db.conn.executemany("UPDATE responses SET eval_overlap = ?, decontam_key = ?, updated_at = ? WHERE id = ?",
                        updates)
    db.conn.commit()
    stats["responses_checked"] = len(updates)

    # repo .bal files (the raw corpus and the codebase pool)
    rows = db.query("SELECT id, text, minhash FROM repo_files WHERE kind = 'bal' AND text IS NOT NULL "
                    "AND decontam_key IS NOT ?", (key,))
    updates = [(dc.check(r["text"], r["minhash"]), key, now, r["id"]) for r in rows]
    db.conn.executemany("UPDATE repo_files SET eval_overlap = ?, decontam_key = ?, updated_at = ? WHERE id = ?",
                        updates)
    stats["repo_files_checked"] = len(updates)
    db.conn.commit()

    stats.update(overlap_counts(db))
    return stats


def overlap_counts(db: Any) -> dict[str, int]:
    return {
        "chunks_overlap": int(db.scalar("SELECT COUNT(*) FROM chunks WHERE eval_overlap IS NOT NULL") or 0),
        "questions_overlap": int(db.scalar("SELECT COUNT(*) FROM questions WHERE eval_overlap IS NOT NULL") or 0),
        "repo_files_overlap": int(db.scalar("SELECT COUNT(*) FROM repo_files WHERE eval_overlap IS NOT NULL")
                                  or 0),
        "prompts_overlap": int(db.scalar("SELECT COUNT(*) FROM prompts WHERE eval_overlap IS NOT NULL") or 0),
        "responses_overlap": int(db.scalar("SELECT COUNT(*) FROM responses WHERE eval_overlap IS NOT NULL") or 0),
    }


def export_guard(db: Any, cfg: EvalCfg, texts: list[str]) -> dict[str, int]:
    """Re-check exported texts against the fingerprints: `eval_overlap_in_export` should be 0."""
    dc = Decontaminator.from_db(db, cfg)
    if not dc:
        return {}
    return {"eval_overlap_in_export": sum(1 for t in texts if dc.check(t))}
