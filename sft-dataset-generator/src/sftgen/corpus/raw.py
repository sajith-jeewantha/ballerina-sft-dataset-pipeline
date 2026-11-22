"""`corpus`: the raw pretraining corpus (CPT) from the collected repos.

What goes in: `.bal` files of collected, licence-`safe` repos that are
* the kept copy after exact and near-duplicate removal (`repo_files.status = 'kept'`),
* Swan Lake or unclassified (never `legacy` 1.x syntax),
* not overlapping the eval set (`eval_overlap`; decontamination runs first), and not part of an
  eval-split codebase (`corpus.include_eval_codebases`),
* at least `corpus.min_tokens` tokens.

Generated code (OpenAPI connectors, "do not edit" files) stays in with `weight` =
`corpus.generated_weight` (0.3), so a trainer can down-weight it; everything else has weight 1.

Outputs in `<data>/corpus/<version>/`:
* `part-NNNN.jsonl.gz`: one record per file: `id`, `text`, `repo`, `path`, `commit`, `url`,
  `licence`, `group`, `syntax`, `generated`, `weight`, `tokens` (≈ chars / 4)
* `manifest.json`: totals by group, licence and syntax, weighted tokens, and what was left out
* `ATTRIBUTION.md`: every repo in the corpus with its licence, pinned commit and size
The licence register gets `corpus_files` and `corpus_tokens` per repo.
"""

from __future__ import annotations

import gzip
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.decontam import apply_decontam
from balpipe_common.ids import sha256_text, utc_now_iso
from balpipe_common.licences import LicenceRegister

EXCLUDED = {"duplicate": "duplicate", "near_duplicate": "near duplicate", "legacy": "legacy 1.x syntax",
            "too_large": "too large"}


def _eval_codebase_files(db: Database) -> set[tuple[str, str]]:
    """(repo, path) of every file in an eval-split codebase."""
    out = set()
    for cb in db.query("SELECT full_name, root, files FROM codebases WHERE split = 'eval' AND status = 'active'"):
        prefix = f"{cb['root']}/" if cb["root"] else ""
        out.update((cb["full_name"], prefix + p) for p in json.loads(cb["files"]))
    return out


class PartWriter:
    def __init__(self, root: Path, part_tokens: int) -> None:
        self.root, self.part_tokens = root, part_tokens
        self.n, self.tokens, self.f = 0, 0, None
        self.files: list[str] = []

    def write(self, rec: dict[str, Any]) -> None:
        if self.f is None or self.tokens >= self.part_tokens:
            self.close()
            name = f"part-{self.n:04d}.jsonl.gz"
            self.f = gzip.open(self.root / f".{name}.part", "wt", encoding="utf-8", compresslevel=6)
            self.files.append(name)
            self.n += 1
            self.tokens = 0
        self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.tokens += rec["tokens"]

    def close(self) -> None:
        if self.f is not None:
            self.f.close()
            name = self.files[-1]
            os.replace(self.root / f".{name}.part", self.root / name)
            self.f = None


def export(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    cfg = s.corpus
    decontam = apply_decontam(db, s.eval_)
    root = s.corpus_dir
    root.mkdir(parents=True, exist_ok=True)
    for old in root.glob("part-*.jsonl.gz"):
        old.unlink()
    eval_files = set() if cfg.include_eval_codebases else _eval_codebase_files(db)
    writer = PartWriter(root, cfg.part_tokens)
    totals: dict[str, Any] = {"files": 0, "tokens": 0, "weighted_tokens": 0.0, "generated_files": 0,
                              "generated_tokens": 0}
    by: dict[str, dict[str, list[int]]] = {"group": defaultdict(lambda: [0, 0]), "licence": defaultdict(lambda: [0, 0]),
                                           "syntax": defaultdict(lambda: [0, 0])}
    left_out: dict[str, int] = defaultdict(int)
    per_repo: dict[str, dict[str, Any]] = {}
    rows = db.query(
        "SELECT f.path, f.status, f.text, f.syntax, f.generated, f.eval_overlap, r.full_name, r.grp, r.spdx, "
        "r.commit_sha FROM repo_files f JOIN repos r ON r.id = f.repo_id WHERE r.status = 'collected' "
        "AND r.verdict = 'safe' AND f.kind = 'bal' ORDER BY r.priority, r.full_name, f.path")
    for r in rows:
        if r["status"] != "kept":
            left_out[EXCLUDED.get(r["status"], r["status"])] += 1
            continue
        if r["text"] is None or r["syntax"] == "legacy":
            left_out["legacy 1.x syntax" if r["syntax"] == "legacy" else "no text"] += 1
            continue
        if r["eval_overlap"]:
            left_out["eval overlap"] += 1
            continue
        if (r["full_name"], r["path"]) in eval_files:
            left_out["eval-split codebase"] += 1
            continue
        tokens = len(r["text"]) // 4
        if tokens < cfg.min_tokens:
            left_out["too small"] += 1
            continue
        weight = cfg.generated_weight if r["generated"] else 1.0
        rec = {"id": sha256_text(f"{r['full_name']}:{r['path']}")[:16], "text": r["text"], "repo": r["full_name"],
               "path": r["path"], "commit": r["commit_sha"],
               "url": f"https://github.com/{r['full_name']}/blob/{r['commit_sha']}/{r['path']}",
               "licence": r["spdx"], "group": r["grp"], "syntax": r["syntax"] or "unknown",
               "generated": bool(r["generated"]), "weight": weight, "tokens": tokens}
        writer.write(rec)
        totals["files"] += 1
        totals["tokens"] += tokens
        totals["weighted_tokens"] += tokens * weight
        if r["generated"]:
            totals["generated_files"] += 1
            totals["generated_tokens"] += tokens
        for key, val in (("group", r["grp"]), ("licence", r["spdx"] or "?"), ("syntax", rec["syntax"])):
            by[key][val][0] += 1
            by[key][val][1] += tokens
        rp = per_repo.setdefault(r["full_name"], {"licence": r["spdx"], "commit": r["commit_sha"], "group": r["grp"],
                                                 "files": 0, "tokens": 0})
        rp["files"] += 1
        rp["tokens"] += tokens
    writer.close()
    totals["weighted_tokens"] = int(totals["weighted_tokens"])
    manifest = {"version": cfg.version, "created_at": utc_now_iso(), "swan_lake": s.sources.swan_lake, **totals,
                "repos": len(per_repo), "parts": writer.files, "generated_weight": cfg.generated_weight,
                **{f"by_{k}": {name: {"files": v[0], "tokens": v[1]} for name, v in sorted(d.items())}
                   for k, d in by.items()},
                "left_out": dict(sorted(left_out.items())), "decontamination": decontam}
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    write_attribution(root / "ATTRIBUTION.md", per_repo, manifest)
    register = LicenceRegister(s.path(s.repos.register_file))
    for name, rp in per_repo.items():
        register.upsert(name, corpus_files=rp["files"], corpus_tokens=rp["tokens"])
    for (owner, repo), row in list(register.rows.items()):  # repos that left the corpus since the last export
        if row.get("corpus_files") not in ("", "0") and f"{owner}/{repo}" not in per_repo:
            register.upsert(f"{owner}/{repo}", corpus_files=0, corpus_tokens=0)
    register.save()
    run.stats.update({"corpus_files": totals["files"], "corpus_tokens": totals["tokens"], "corpus_repos": len(per_repo)})
    return {k: v for k, v in manifest.items() if k not in ("parts", "decontamination")}


def write_attribution(path: Path, per_repo: dict[str, dict[str, Any]], manifest: dict[str, Any]) -> None:
    lines = [f"# Raw Ballerina code corpus {manifest['version']}: attribution", "",
             f"{manifest['files']:,} files (~{manifest['tokens']:,} tokens) from {len(per_repo)} repositories, each "
             "under the licence shown, at the pinned commit. Each repository's licence text is in its repository "
             "(LICENSE at the commit). Apache-2.0 sources: keep this notice and the licence; MIT/BSD: keep the "
             "copyright notice.", "", "| Repository | Licence | Commit | Files | Tokens |", "|---|---|---|---|---|"]
    for name, rp in sorted(per_repo.items(), key=lambda kv: (-kv[1]["tokens"], kv[0])):
        commit = (rp["commit"] or "")[:12]
        lines.append(f"| [{name}](https://github.com/{name}/tree/{rp['commit']}) | {rp['licence']} | {commit} | "
                     f"{rp['files']:,} | {rp['tokens']:,} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def manifest(s: Settings) -> dict[str, Any] | None:
    p = s.corpus_dir / "manifest.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
