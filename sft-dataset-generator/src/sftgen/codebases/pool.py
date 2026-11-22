"""The codebase pool: real Swan Lake projects from collected repos, verified with the toolchain.

* **Candidates:** every `Ballerina.toml` project (`[package]` layout) in a collected,
  licence-`safe` repo whose files are all stored, with no 1.x syntax, no generated code and nothing
  that overlaps the eval set, within `codebases.min/max_files` and `min/max_tokens`.
* **Rank:** preferred group (`codebases.groups`: guides, wso2, external, platform), tests present,
  a moderate number of files, more imported modules; at most `max_per_repo` per repo; ties broken by
  a hash of the id, so the order is stable.
* **Verify:** the base project must build with the pinned `bal` (tests are compiled; run only with
  `codebases.run_tests`). Candidates are tried in rank order until `pool_size` are active.
* **Tag:** imports → graph nodes whose `modules` list them; then the items of those nodes whose
  qualified names (`http:Client`, `@http:Cache`) appear in the code.
* **Split:** a hash of the id puts `eval_fraction` of the pool on the eval side (for M8's tasks);
  eval codebases never feed training prompts.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from balpipe_common.config import CodebasesCfg, Settings
from balpipe_common.db import Database
from balpipe_common.ids import sha256_text, short_hash

from sftgen import ballerina
from sftgen.retrieval.symbols import QUALIFIED_RE

PACKAGE_RE = re.compile(r"^\s*\[package\]", re.M)


@dataclass
class Candidate:
    id: str
    repo_id: int
    full_name: str
    grp: str
    root: str
    commit: str | None
    spdx: str | None
    files: dict[str, str]

    @property
    def bal(self) -> dict[str, str]:
        return {p: t for p, t in self.files.items() if p.endswith(".bal")}

    @property
    def tokens(self) -> int:
        return sum(len(t) for t in self.files.values()) // 4

    @property
    def has_tests(self) -> bool:
        return any(p.startswith("tests/") for p in self.bal)

    @property
    def modules(self) -> list[str]:
        mods: set[str] = set()
        for t in self.bal.values():
            mods.update(ballerina.imports(t))
        return sorted(mods)


def codebase_id(full_name: str, root: str) -> str:
    repo = full_name.split("/")[-1]
    leaf = PurePosixPath(root).name if root else "root"
    slug = re.sub(r"[^a-z0-9]+", "-", f"{repo}-{leaf}".lower()).strip("-")[:48]
    return f"{slug}-{short_hash(full_name, root)[:6]}"


def split_for(cid: str, cfg: CodebasesCfg) -> str:
    bucket = int(sha256_text(f"{cfg.split_salt}:{cid}")[:8], 16) / 0x1_0000_0000
    return "eval" if bucket < cfg.eval_fraction else "train"


def candidates(db: Database, cfg: CodebasesCfg) -> list[Candidate]:
    out = []
    projects = db.query(
        "SELECT f.repo_id, f.project_root, f.text, r.full_name, r.grp, r.commit_sha, r.spdx FROM repo_files f "
        "JOIN repos r ON r.id = f.repo_id WHERE r.status = 'collected' AND r.verdict = 'safe' AND f.kind = 'toml' "
        "AND (f.path = 'Ballerina.toml' OR f.path LIKE '%/Ballerina.toml') ORDER BY r.priority, r.full_name, f.path")
    for p in projects:
        root = p["project_root"] or ""
        if not p["text"] or not PACKAGE_RE.search(p["text"]):
            continue
        rows = db.query("SELECT path, kind, text, syntax, generated, eval_overlap, status FROM repo_files "
                        "WHERE repo_id = ? AND project_root IS ? AND kind = 'bal'", (p["repo_id"], p["project_root"]))
        if not (cfg.min_files <= len(rows) <= cfg.max_files):
            continue
        if any(r["text"] is None or r["syntax"] == "legacy" or r["generated"] or r["eval_overlap"] for r in rows):
            continue
        prefix = f"{root}/" if root else ""
        files = {"Ballerina.toml": p["text"]}
        for r in rows:
            rel = r["path"][len(prefix):]
            if rel.startswith(("target/", "build/")):
                continue
            files[rel] = r["text"]
        c = Candidate(codebase_id(p["full_name"], root), p["repo_id"], p["full_name"], p["grp"], root,
                      p["commit_sha"], p["spdx"], dict(sorted(files.items())))
        if cfg.min_tokens <= c.tokens <= cfg.max_tokens:
            out.append(c)
    return out


def rank(cands: list[Candidate], cfg: CodebasesCfg) -> list[tuple[float, Candidate]]:
    def score(c: Candidate) -> float:
        g = cfg.groups.index(c.grp) if c.grp in cfg.groups else len(cfg.groups)
        n = len(c.bal)
        return (3.0 - g) + (2.0 if c.has_tests else 0.0) + (1.0 if 3 <= n <= 15 else 0.0) + \
            min(len(c.modules), 6) * 0.25
    scored = sorted(((score(c), c) for c in cands), key=lambda sc: (-sc[0], sha256_text(sc[1].id)))
    per_repo: dict[str, int] = {}
    out = []
    for s, c in scored:
        if per_repo.get(c.full_name, 0) >= cfg.max_per_repo:
            continue
        per_repo[c.full_name] = per_repo.get(c.full_name, 0) + 1
        out.append((s, c))
    return out


def tag(db: Database, nodes_info: dict[str, dict[str, Any]], c: Candidate) -> tuple[list[str], list[str]]:
    """Graph nodes (by imported modules) and items (qualified names found in the code)."""
    mods = set(c.modules)
    nodes = sorted(n for n, info in nodes_info.items() if mods & set(info.get("modules") or []))
    code = "\n".join(c.bal.values())
    used = {f"{m.group(1)}:{m.group(2)}" for m in QUALIFIED_RE.finditer(ballerina.strip_comments_and_strings(code))}
    items = []
    if nodes:
        marks = ",".join("?" for _ in nodes)
        for r in db.query(f"SELECT ref, text FROM graph_items WHERE node_id IN ({marks}) AND status != 'retired' "
                          "ORDER BY ref", tuple(nodes)):
            names = {f"{m.group(1)}:{m.group(2)}" for m in QUALIFIED_RE.finditer(r["text"])}
            if names & used:
                items.append(r["ref"])
    return nodes, items


def files_of(db: Database, cid: str) -> dict[str, str]:
    row = db.one("SELECT files FROM codebases WHERE id = ?", (cid,))
    return json.loads(row["files"]) if row else {}


def save(db: Database, s: Settings, c: Candidate, score: float, nodes: list[str], items: list[str], status: str,
         detail: str | None) -> None:
    values = {"repo_id": c.repo_id, "full_name": c.full_name, "root": c.root, "commit_sha": c.commit, "spdx": c.spdx,
              "files": json.dumps(c.files, ensure_ascii=False), "file_count": len(c.files), "tokens": c.tokens,
              "has_tests": int(c.has_tests), "modules": json.dumps(c.modules), "nodes": json.dumps(nodes),
              "items": json.dumps(items), "split": split_for(c.id, s.codebases), "score": round(score, 2),
              "status": status, "verify_detail": detail}
    if db.scalar("SELECT 1 FROM codebases WHERE id = ?", (c.id,)):
        db.update("codebases", c.id, values)
    else:
        db.insert("codebases", {"id": c.id, **values})
