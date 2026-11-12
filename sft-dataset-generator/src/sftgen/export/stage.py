"""`sftgen export`: judged.jsonl -> train.jsonl, eval.jsonl, metadata.jsonl

Decisions are (re)computed here from stored scores and checks, so thresholds
can change without regenerating anything:
  1. validate rejections stand (compile errors, context leaks, missing code),
  2. judge thresholds from config (records without a judge result are rejected),
  3. license allowlist,
  4. near-duplicate questions (embedding cosine) — first by id wins,
  5. train records overlapping the eval set (13-gram) are dropped.
"""

from __future__ import annotations

import re
from collections import Counter

import numpy as np

from balpipe_common.config import Settings
from balpipe_common.jsonl_io import read_models, write_jsonl_atomic
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun
from balpipe_common.schemas import Document, Message, SFTCandidate, TrainRow

from sftgen.paths import Paths

_WORD = re.compile(r"\w+")


def decide(c: SFTCandidate, s: Settings) -> list[str]:
    """Reasons to reject (empty list = accept), before dedup/decontamination."""
    reasons = [r for r in c.decision.reasons if c.decision.status == "rejected"]
    if not reasons:
        if c.judge is None:
            reasons.append("not_judged")
        else:
            for crit, minimum in s.judge.thresholds.items():
                if c.judge.scores.get(crit, 0) < minimum:
                    reasons.append(f"judge.{crit}<{minimum}")
    if any(lic not in s.export.allowed_licenses for lic in c.licenses):
        reasons.append("license_not_allowed")
    if s.export.reject_unknown_symbols and c.checks.unknown_symbols:
        reasons.append("unknown_symbols")
    return reasons


def ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    w = _WORD.findall(text.lower())
    return {tuple(w[i : i + n]) for i in range(len(w) - n + 1)}


def max_overlap(text: str, eval_grams: set[tuple[str, ...]], n: int) -> float:
    grams = ngrams(text, n)
    return len(grams & eval_grams) / len(grams) if grams else 0.0


def near_duplicates(texts: list[str], vecs: np.ndarray, threshold: float) -> list[int | None]:
    """For each row, the index of an earlier kept row it duplicates (cosine >= threshold), else None."""
    kept: list[int] = []
    out: list[int | None] = []
    for i in range(len(texts)):
        if kept:
            sims = vecs[kept] @ vecs[i]
            j = int(np.argmax(sims))
            if sims[j] >= threshold:
                out.append(kept[j])
                continue
        kept.append(i)
        out.append(None)
    return out


def to_row(c: SFTCandidate, s: Settings) -> TrainRow:
    msgs = list(c.messages)
    if s.export.include_system_prompt:
        msgs = [Message(role="system", content=s.generate.system_prompt), *msgs]
    return TrainRow(id=c.id, messages=msgs)


def run(settings: Settings, *, run_id: str) -> dict:
    from sftgen.index.embedder import embed

    paths = Paths(settings)
    run_dir = paths.sft_run(run_id)
    src = run_dir / "judged.jsonl"
    with StageRun(settings, "export", run_dir, inputs=[src], run_id=run_id, manifest_name="_manifest.export.json") as stage:
        log = get_logger(__name__, run_id=run_id, stage="export")
        cands = sorted(read_models(src, SFTCandidate), key=lambda c: c.id)
        reasons: dict[str, list[str]] = {c.id: decide(c, settings) for c in cands}

        # near-duplicate questions among otherwise-accepted records (per split)
        for split in ("train", "eval"):
            pool = [c for c in cands if c.split == split and not reasons[c.id]]
            if len(pool) > 1:
                qs = [c.messages[0].content for c in pool]
                dup_of = near_duplicates(qs, embed(settings.embedding_model, qs), settings.export.near_dup_threshold)
                for c, d in zip(pool, dup_of, strict=True):
                    if d is not None:
                        c.checks.near_dup_of = pool[d].id
                        reasons[c.id].append("near_duplicate")

        # decontamination: train text vs eval records + eval documents
        n = settings.export.ngram_size
        eval_grams: set[tuple[str, ...]] = set()
        for c in cands:
            if c.split == "eval":
                eval_grams |= ngrams(" ".join(m.content for m in c.messages), n)
        if paths.documents.exists():
            for d in read_models(paths.documents, Document):
                if d.split == "eval":
                    eval_grams |= ngrams(d.markdown, n)
        for c in cands:
            if c.split == "train":
                ov = max_overlap(" ".join(m.content for m in c.messages), eval_grams, n)
                c.checks.max_ngram_overlap_eval = round(ov, 4)
                if ov > settings.export.max_ngram_overlap:
                    reasons[c.id].append("eval_overlap")

        for c in cands:
            r = reasons[c.id]
            c.decision.status = "rejected" if r else "accepted"
            c.decision.reasons = r
            c.decision.thresholds_version = settings.judge.thresholds_version

        accepted = [c for c in cands if c.decision.status == "accepted"]
        n_train = write_jsonl_atomic(run_dir / "train.jsonl", (to_row(c, settings) for c in accepted if c.split == "train"))
        n_eval = write_jsonl_atomic(run_dir / "eval.jsonl", (to_row(c, settings) for c in accepted if c.split == "eval"))
        write_jsonl_atomic(run_dir / "metadata.jsonl", (c.meta() for c in cands))
        for name in ("train.jsonl", "eval.jsonl", "metadata.jsonl"):
            stage.add_output(run_dir / name)
        stage.count("candidates", len(cands))
        stage.count("train", n_train)
        stage.count("eval", n_eval)
        for r, k in Counter(r.split("<")[0] for c in cands for r in c.decision.reasons).items():
            stage.reject(r, k)
        for t, k in Counter(c.task_type for c in accepted).items():
            stage.count(f"accepted_{t}", k)
        log.info("export finished", extra={"event": "export.done", "train": n_train, "eval": n_eval})
        return {"run_id": run_id, **dict(stage.counts), "rejected": dict(stage.rejections)}
