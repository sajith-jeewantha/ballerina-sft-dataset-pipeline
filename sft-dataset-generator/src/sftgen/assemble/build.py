"""`assemble`: dataset v1 (M10) from everything the pipeline has verified.

**Sources** (each one a chat sample `system · user · assistant`):
* **prompts**: answers to accepted prompts of `assemble.prompt_sets` (default `prompts.set_name`) whose
  response is `verified` (code checked by `bal`) or `judged` (prose checked by the judge);
  standalone and codebase tasks (`context_mode`).
* **questions**: the chunk-grounded Q&A (`generate-questions` / `generate-answers`), answers marked
  `invalid` left out; tagged with the graph items their chunk grounds.
* **mined fixes** (opt in, `assemble.include_mined_fixes`): small before/after fixes from commit history
  as `fix_code` samples. They aren't built at their commit, so check them before turning this on.

**Steps:**
1. **Decontaminate:** `apply_decontam`, then every sample text is checked against the eval fingerprints
   again; anything that overlaps is dropped.
2. **Dedup:** exact (normalised prompt) and near-duplicate prompts (MinHash ≥
   `assemble.near_duplicate_threshold`); the better sample wins (verified > judged > Q&A, first try >
   repaired). Codebase tasks are unique per state and skip the near-duplicate check (they share the project).
3. **Balance by item:** at most `assemble.caps[priority]` samples per graph item, picked round-robin over
   (task type, difficulty, context mode) so one kind can't crowd out the rest; Q&A without an item is
   capped per page (`assemble.max_per_page`).
4. **Split:** a hash of the sample's group (codebase, source page or prompt) puts `assemble.eval_fraction`
   in the validation split, so a codebase or page never feeds both sides.
5. **Write** `<data>/datasets/<version>/`:
   `sft/{train,eval}.jsonl` (`{"id", "messages"}`, Qwen3.8 chat format), `metadata.jsonl`,
   `dpo/pairs.jsonl` (TRL format, train split only), `cpt/` (the raw corpus), `coverage.csv`, `gaps.jsonl`
   (items under their prompt budget: the gap-fill list), `ATTRIBUTION.md`, `CARD.md`, `card.json` and
   `manifest.json` (sha256 per file).
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.decontam import Decontaminator, apply_decontam
from balpipe_common.dedup import LSH, MinHasher
from balpipe_common.ids import content_hash, sha256_text

from sftgen.export.sft import strip_source

PRIORITY_RANK = {"core": 0, "important": 1, "long_tail": 2}


@dataclass
class Sample:
    id: str
    source: str                       # prompt | question | mined_fix
    prompt: str
    answer: str
    covers: list[str]
    ref: str | None                   # the graph item the sample is balanced under
    task_type: str
    difficulty: str
    context_mode: str                 # standalone | codebase
    verification: str
    group: str                        # split key: codebase:<id> | page:<url> | prompt:<id> | fix:<repo>
    quality: tuple[int, ...]          # lower is better
    licence: str | None = None        # licence of third-party code inside the sample (codebase, mined fix)
    repo: str | None = None
    commit: str | None = None
    prompt_id: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return f"{self.prompt}\n{self.answer}"


# --------------------------------------------------------------------------- sources
def _prompt_samples(s: Settings, db: Database, sets: list[str]) -> list[Sample]:
    marks = ",".join("?" for _ in sets)
    rows = db.query(
        "SELECT p.id, p.set_name, p.ref, p.task_type, p.difficulty, p.context_mode, p.prompt, p.covers, "
        "p.writer_model, p.reviewer_model, r.status, r.verification, r.attempts, r.repaired, a.answer, "
        "a.model AS teacher, st.codebase_id, st.kind AS state_kind, cb.full_name, cb.spdx, cb.commit_sha "
        "FROM prompts p JOIN responses r ON r.prompt_id = p.id JOIN attempts a ON a.id = r.attempt_id "
        "LEFT JOIN codebase_states st ON st.id = p.codebase_state_id LEFT JOIN codebases cb ON cb.id = st.codebase_id "
        f"WHERE p.set_name IN ({marks}) AND p.status = 'accepted' AND r.status IN ('verified', 'judged') "
        "AND p.eval_overlap IS NULL AND r.eval_overlap IS NULL ORDER BY p.id", tuple(sets))
    out = []
    for r in rows:
        codebase = r["codebase_id"]
        out.append(Sample(
            id=f"p{r['id']}", source="prompt", prompt=r["prompt"].strip(), answer=r["answer"].strip(),
            covers=json.loads(r["covers"] or "[]") or [r["ref"]], ref=r["ref"], task_type=r["task_type"],
            difficulty=r["difficulty"], context_mode=r["context_mode"] or "standalone",
            verification=r["verification"] or r["status"],
            group=f"codebase:{codebase}" if codebase else f"prompt:{r['id']}",
            quality=(0 if r["status"] == "verified" else 1, int(r["repaired"] or 0), int(r["attempts"] or 1)),
            licence=r["spdx"] if codebase else None, repo=r["full_name"] if codebase else None,
            commit=r["commit_sha"] if codebase else None, prompt_id=r["id"],
            meta={"set": r["set_name"], "teacher": r["teacher"], "writer": r["writer_model"],
                  "reviewer": r["reviewer_model"], "attempts": r["attempts"], "repaired": bool(r["repaired"]),
                  **({"codebase": codebase, "state": r["state_kind"]} if codebase else {})}))
    return out


def _chunk_refs(db: Database) -> dict[int, list[str]]:
    """chunk id -> graph items it grounds (as in the coverage report), best priority first."""
    prio = {r["ref"]: PRIORITY_RANK.get(r["priority"], 3)
            for r in db.query("SELECT ref, priority FROM graph_items WHERE status != 'retired'")}
    out: dict[int, set[str]] = defaultdict(set)
    for r in db.query(
            "SELECT s.ref, c.id AS chunk_id FROM item_sources s JOIN chunks c ON c.page_id = s.page_id "
            "JOIN pages p ON p.id = s.page_id WHERE s.status = 'matched' AND p.source != 'spec' "
            "UNION SELECT s.ref, j.value FROM item_sources s, json_each(s.chunk_ids) j WHERE s.status = 'matched'"):
        if r["ref"] in prio:
            out[int(r["chunk_id"])].add(r["ref"])
    return {k: sorted(v, key=lambda ref: (prio[ref], ref)) for k, v in out.items()}


def _question_samples(s: Settings, db: Database) -> list[Sample]:
    refs = _chunk_refs(db)
    rows = db.query(
        "SELECT q.id, q.chunk_id, q.source, q.source_url, q.question, q.category, q.difficulty, a.answer, "
        "a.status, a.model FROM questions q JOIN answers a ON a.question_id = q.id WHERE q.status = 'answered' "
        "AND a.status != 'invalid' AND q.eval_overlap IS NULL ORDER BY q.id")
    out = []
    for r in rows:
        answer = strip_source(r["answer"]) if s.sft.strip_source_line else r["answer"].strip()
        if not answer.strip():
            continue
        covers = refs.get(r["chunk_id"], [])[:8]
        out.append(Sample(
            id=f"q{r['id']}", source="question", prompt=r["question"].strip(), answer=answer.strip(), covers=covers,
            ref=covers[0] if covers else None, task_type="qa", difficulty=r["difficulty"] or "intermediate",
            context_mode="standalone", verification="compile" if r["status"] == "valid" else "none",
            group=f"page:{r['source_url']}", quality=(2 if r["status"] == "valid" else 3, 0, 1),
            meta={"docs_source": r["source"], "source_url": r["source_url"], "category": r["category"],
                  "teacher": r["model"]}))
    return out


def _fence(path: str, text: str) -> str:
    return f"```ballerina\n// file: {path}\n{text.rstrip()}\n```"


def _mined_samples(s: Settings, db: Database) -> list[Sample]:
    cfg = s.assemble
    out = []
    for r in db.query("SELECT * FROM mined_fixes WHERE eval_overlap IS NULL AND lines_changed <= ? ORDER BY id",
                      (cfg.mined_fix_max_lines,)):
        files = [f for f in json.loads(r["files"]) if f.get("before") is not None and f.get("after") is not None]
        if not files or sum(len(f["before"]) for f in files) > 40_000:
            continue
        shown = "\n\n".join(_fence(f["path"], f["before"]) for f in files)
        prompt = (f"A bug in this Ballerina code was fixed by a commit titled \"{r['message'].strip()}\".\n\n{shown}\n\n"
                  "Make that fix. Give each file you change in full.")
        answer = "\n\n".join(_fence(f["path"], f["after"]) for f in files if f["after"] != f["before"])
        out.append(Sample(
            id=f"m{r['id']}", source="mined_fix", prompt=prompt, answer=answer, covers=[], ref=None,
            task_type="fix_code", difficulty="intermediate", context_mode="standalone", verification="none",
            group=f"fix:{r['full_name']}", quality=(3, 0, 1), licence=r["spdx"], repo=r["full_name"],
            commit=r["commit_sha"], meta={"lines_changed": r["lines_changed"], "touches_tests": bool(r["touches_tests"])}))
    return out


# --------------------------------------------------------------------------- steps
def decontaminate(db: Database, s: Settings, samples: list[Sample]) -> tuple[list[Sample], int]:
    dc = Decontaminator.from_db(db, s.eval_)
    if not dc:
        return samples, 0
    kept = [x for x in samples if not dc.check(x.text)]
    return kept, len(samples) - len(kept)


def dedup(samples: list[Sample], threshold: float) -> tuple[list[Sample], dict[str, int]]:
    ordered = sorted(samples, key=lambda x: (x.quality, x.id))
    seen: set[str] = set()
    hasher, lsh = MinHasher(), LSH(threshold=threshold)
    kept, stats = [], {"exact": 0, "near": 0}
    for i, x in enumerate(ordered):
        key = content_hash(x.prompt.lower())
        if key in seen:
            stats["exact"] += 1
            continue
        seen.add(key)
        if x.context_mode != "codebase" and x.source != "mined_fix":
            sig = hasher.signature(x.prompt.lower())
            if lsh.query(sig) is not None:
                stats["near"] += 1
                continue
            lsh.add(i, sig)
        kept.append(x)
    return kept, stats


def _round_robin(samples: list[Sample], cap: int) -> list[Sample]:
    buckets: dict[tuple[str, str, str], list[Sample]] = {}
    for x in sorted(samples, key=lambda x: (x.quality, x.id)):
        buckets.setdefault((x.task_type, x.difficulty, x.context_mode), []).append(x)
    out: list[Sample] = []
    queues = list(buckets.values())
    while len(out) < cap and any(queues):
        for q in queues:
            if q and len(out) < cap:
                out.append(q.pop(0))
    return out


def balance(samples: list[Sample], priorities: dict[str, str], cfg: Any) -> tuple[list[Sample], int]:
    by_ref: dict[str, list[Sample]] = defaultdict(list)
    for x in samples:
        key = (f"item:{x.ref}" if x.ref else f"page:{x.meta['source_url']}" if x.source == "question"
               else f"other:{x.id}")
        by_ref[key].append(x)
    kept: list[Sample] = []
    for key, group in by_ref.items():
        if key.startswith("item:"):
            cap = cfg.caps.get(priorities.get(key[5:], "long_tail"), cfg.caps.get("long_tail", 5))
        elif key.startswith("page:"):
            cap = cfg.max_per_page
        else:
            cap = len(group)
        kept.extend(_round_robin(group, cap))
    kept.sort(key=lambda x: (x.source, x.id))
    return kept, len(samples) - len(kept)


def split_for(group: str, cfg: Any) -> str:
    bucket = int(sha256_text(f"{cfg.split_salt}:{group}")[:8], 16) / 0x1_0000_0000
    return "eval" if bucket < cfg.eval_fraction else "train"


# --------------------------------------------------------------------------- output
def _messages(system: str, x: Sample) -> list[dict[str, str]]:
    head = [{"role": "system", "content": system}] if system else []
    return head + [{"role": "user", "content": x.prompt}, {"role": "assistant", "content": x.answer}]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)
    return len(rows)


def _pairs(db: Database, system: str, train_prompt_ids: list[int]) -> list[dict[str, Any]]:
    if not train_prompt_ids:
        return []
    head = [{"role": "system", "content": system}] if system else []
    out = []
    ids = set(train_prompt_ids)
    for r in db.query(
            "SELECT pp.prompt_id, pp.reason, p.prompt, p.covers, c.answer AS chosen, x.answer AS rejected "
            "FROM preference_pairs pp JOIN prompts p ON p.id = pp.prompt_id JOIN attempts c ON c.id = pp.chosen_id "
            "JOIN attempts x ON x.id = pp.rejected_id ORDER BY pp.prompt_id"):
        if r["prompt_id"] not in ids:
            continue
        out.append({"id": f"p{r['prompt_id']}", "covers": json.loads(r["covers"] or "[]"), "reason": r["reason"],
                    "prompt": head + [{"role": "user", "content": r["prompt"].strip()}],
                    "chosen": [{"role": "assistant", "content": r["chosen"].strip()}],
                    "rejected": [{"role": "assistant", "content": r["rejected"].strip()}]})
    return out


def _copy_corpus(s: Settings, dest: Path) -> dict[str, Any] | None:
    src = s.corpus_dir
    if not (src / "manifest.json").is_file():
        return None
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for f in sorted(src.iterdir()):
        if f.is_file() and (f.name.startswith("part-") or f.name in ("manifest.json", "ATTRIBUTION.md")):
            try:
                os.link(f, dest / f.name)  # same disk: no second copy of ~100 MB
            except OSError:
                shutil.copy2(f, dest / f.name)
    return json.loads((src / "manifest.json").read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _scope_nodes(s: Settings, db: Database, sets: list[str]) -> set[str]:
    """Nodes the prompt plan covers: prompts.nodes, or every node prompts exist for when it's []."""
    if s.prompts.nodes:
        return set(s.prompts.nodes)
    marks = ",".join("?" for _ in sets)
    return {r[0] for r in db.query(f"SELECT DISTINCT node_id FROM prompts WHERE set_name IN ({marks})", tuple(sets))} \
        or {r[0] for r in db.query("SELECT DISTINCT node_id FROM graph_items WHERE status != 'retired'")}


def coverage(s: Settings, db: Database, kept: list[Sample], splits: dict[str, str], sets: list[str]
             ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(per-item rows for every active item, gap rows for in-scope items under their prompt budget)."""
    per: dict[str, Counter[str]] = defaultdict(Counter)
    for x in kept:
        for ref in set(x.covers) | ({x.ref} if x.ref else set()):
            per[ref]["samples"] += 1
            per[ref][splits[x.id]] += 1
            per[ref][f"task:{x.task_type}"] += 1
            per[ref]["codebase"] += x.context_mode == "codebase"
    scope = _scope_nodes(s, db, sets)
    rows, gaps = [], []
    for it in db.query("SELECT ref, node_id, node_title, priority, status FROM graph_items WHERE status != 'retired' "
                       "ORDER BY node_id, ref"):
        c = per.get(it["ref"], Counter())
        tasks = {k[5:]: v for k, v in sorted(c.items()) if k.startswith("task:")}
        row = {"ref": it["ref"], "node": it["node_id"], "node_title": it["node_title"], "priority": it["priority"],
               "grounding": it["status"], "in_scope": it["node_id"] in scope, "samples": c["samples"],
               "train": c["train"], "eval": c["eval"], "codebase": c["codebase"], "tasks": tasks}
        rows.append(row)
        budget = s.prompts.budgets.get(it["priority"], 0)
        if row["in_scope"] and c["samples"] < budget:
            gaps.append({"ref": it["ref"], "node": it["node_id"], "priority": it["priority"],
                         "grounding": it["status"], "samples": c["samples"], "budget": budget,
                         "missing": budget - c["samples"]})
    return rows, gaps


def _write_coverage_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ref", "node", "priority", "grounding", "in_scope", "samples", "train", "eval", "codebase",
                    "tasks"])
        for r in rows:
            w.writerow([r["ref"], r["node"], r["priority"], r["grounding"], int(r["in_scope"]), r["samples"],
                        r["train"], r["eval"], r["codebase"], ";".join(f"{k}={v}" for k, v in r["tasks"].items())])


def _write_attribution(path: Path, kept: list[Sample], corpus: dict[str, Any] | None) -> list[dict[str, Any]]:
    repos: dict[str, dict[str, Any]] = {}
    for x in kept:
        if x.repo:
            r = repos.setdefault(x.repo, {"repo": x.repo, "licence": x.licence, "commits": set(), "samples": 0})
            r["samples"] += 1
            if x.commit:
                r["commits"].add(x.commit)
    rows = [{**r, "commits": sorted(r["commits"])} for r in sorted(repos.values(), key=lambda r: r["repo"])]
    lines = ["# Dataset attribution", "",
             "Third-party code inside SFT samples (codebase tasks and mined fixes), each under the licence shown at the "
             "commit shown. Documentation-grounded samples were written by the teacher model from the pinned Ballerina "
             "sources; their licences are in CARD.md (Licences).", "",
             "| Repository | Licence | Commit(s) | Samples |", "|---|---|---|---|"]
    for r in rows:
        commits = ", ".join(f"[{c[:12]}](https://github.com/{r['repo']}/tree/{c})" for c in r["commits"][:3])
        lines.append(f"| [{r['repo']}](https://github.com/{r['repo']}) | {r['licence'] or '?'} | {commits} | "
                     f"{r['samples']} |")
    if corpus:
        lines += ["", f"The CPT corpus in `cpt/` has its own attribution: `cpt/ATTRIBUTION.md` "
                      f"({corpus.get('repos', 0)} repositories)."]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rows


def assemble(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    from sftgen.assemble.card import write_card

    cfg = s.assemble
    sets = cfg.prompt_sets or [s.prompts.set_name]
    decontam = apply_decontam(db, s.eval_)
    found: dict[str, list[Sample]] = {"prompt": _prompt_samples(s, db, sets)}
    if cfg.include_questions:
        found["question"] = _question_samples(s, db)
    if cfg.include_mined_fixes:
        found["mined_fix"] = _mined_samples(s, db)
    samples = [x for v in found.values() for x in v]
    stats: dict[str, Any] = {"found": {k: len(v) for k, v in found.items()}}

    samples, stats["eval_overlap_dropped"] = decontaminate(db, s, samples)
    samples, stats["duplicates"] = dedup(samples, cfg.near_duplicate_threshold)
    priorities = {r["ref"]: r["priority"] for r in db.query("SELECT ref, priority FROM graph_items")}
    kept, stats["over_cap"] = balance(samples, priorities, cfg)
    splits = {x.id: split_for(x.group, cfg) for x in kept}

    root = s.dataset_dir
    root.mkdir(parents=True, exist_ok=True)
    system = s.sft.system_prompt.strip()
    rows = {"train": [], "eval": []}
    meta = []
    for x in kept:
        rows[splits[x.id]].append({"id": x.id, "messages": _messages(system, x)})
        meta.append({"id": x.id, "split": splits[x.id], "source": x.source, "covers": x.covers, "ref": x.ref,
                     "priority": priorities.get(x.ref) if x.ref else None, "task_type": x.task_type,
                     "difficulty": x.difficulty, "context_mode": x.context_mode, "verification": x.verification,
                     "licence": x.licence, "repo": x.repo, "commit": x.commit, **x.meta})
    _write_jsonl(root / "sft" / "train.jsonl", rows["train"])
    _write_jsonl(root / "sft" / "eval.jsonl", rows["eval"])
    _write_jsonl(root / "metadata.jsonl", meta)
    pairs = _pairs(db, system, [x.prompt_id for x in kept if x.prompt_id and splits[x.id] == "train"]) \
        if cfg.dpo else []
    if cfg.dpo:
        _write_jsonl(root / "dpo" / "pairs.jsonl", pairs)
    elif (root / "dpo").exists():
        shutil.rmtree(root / "dpo")
    corpus = _copy_corpus(s, root / "cpt") if cfg.cpt else None
    if not corpus and (root / "cpt").exists():
        shutil.rmtree(root / "cpt")
    cov, gaps = coverage(s, db, kept, splits, sets)
    _write_coverage_csv(root / "coverage.csv", cov)
    _write_jsonl(root / "gaps.jsonl", gaps)
    attribution = _write_attribution(root / "ATTRIBUTION.md", kept, corpus)

    # the guard: what was written must not overlap the eval set
    dc = Decontaminator.from_db(db, s.eval_)
    stats["eval_overlap_in_export"] = sum(1 for x in kept if dc.check(x.text)) if dc else 0
    stats.update({"train": len(rows["train"]), "eval": len(rows["eval"]), "dpo_pairs": len(pairs),
                  "cpt_files": corpus.get("files", 0) if corpus else 0,
                  "cpt_tokens": corpus.get("tokens", 0) if corpus else 0, "gaps": len(gaps)})
    card = write_card(s, db, root, kept=kept, meta=meta, splits=splits, stats=stats, coverage_rows=cov, gaps=gaps,
                      attribution=attribution, corpus=corpus, decontam=decontam, sets=sets)
    files = {}
    for f in sorted(root.rglob("*")):
        if f.is_file() and f.name != "manifest.json" and not f.name.startswith("."):
            files[str(f.relative_to(root))] = {"bytes": f.stat().st_size, "sha256": _sha256(f)}
    (root / "manifest.json").write_text(json.dumps({"version": cfg.version, "created_at": card["created_at"],
                                                    "files": files}, indent=1) + "\n", encoding="utf-8")
    run.stats.update({k: v for k, v in stats.items() if isinstance(v, int)})
    return {"dir": str(root.relative_to(s.data_root)), **stats, "created_at": card["created_at"]}


def card_data(s: Settings) -> dict[str, Any] | None:
    p = s.dataset_dir / "card.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

