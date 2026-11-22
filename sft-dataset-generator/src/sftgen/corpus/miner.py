"""`mine-fixes`: real before/after bug fixes from the commit history of collected repos.

For each collected, licence-`safe` repo in `miner.groups` (guides, platform, wso2), up to
LIMIT_REPOS repos per run (0 = all), most-starred first:

1. A **blobless bare clone** with full history (commits and trees only; file contents are
   fetched lazily, and only for the commits that qualify), cached in `<data>/cache/mine/`.
2. The latest `miner.max_commits` non-merge commits touching `*.bal` are scanned
   (`git log --raw`: file names and change types from the trees, no file contents). A commit
   qualifies when:
   - its message reads like a fix (fix, bug, issue, resolve, correct, …) and not like a version
     bump, release, merge, revert, dependency update, regeneration or formatting change;
   - its author isn't a bot;
   - it changes 1–`miner.max_files` existing `.bal` source files (it may also change tests), with
     at most `miner.max_lines` lines added and deleted.
3. The files before and after are read (`git show parent:path`, `commit:path`). Pairs with 1.x
   syntax, or overlapping the eval set, are dropped; pairs are deduplicated by their code.

Pairs are stored in `mined_fixes` with the diff and whether the commit also touched tests. They are
**not built**: building each project at each commit would be slow, so M10 uses them as
fix tasks only after its own checks. Output: `<data>/corpus/<version>/fixes.jsonl`, and the
licence register gets `mined_fixes` per repo.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.decontam import Decontaminator
from balpipe_common.dedup import code_hash
from balpipe_common.ids import sha256_text
from balpipe_common.licences import LicenceRegister

log = logging.getLogger(__name__)

FIX_RE = re.compile(r"\b(fix(e[sd]|ing)?|bug(fix)?|issue|resolve[sd]?|correct(ed|s|ion)?|repair|broken|crash|"
                    r"wrong|incorrect|npe|nil pointer|handle (error|nil|null))\b", re.I)
SKIP_RE = re.compile(r"\b(bump|release|prepare|merge|revert|dependabot|update (the )?(dependenc|version|lang|"
                     r"distribution)|upgrade|regenerat|auto[- ]?generat|format(ting)?|lint|typo|readme|docs?\b|"
                     r"changelog|copyright|licen[cs]e|ci\b|workflow|gradle|spotbugs|\[automated\]|sync with|review|"
                     r"suggestion|documentation|comments?\b|tests?\b)", re.I)
BOT_RE = re.compile(r"bot\b|\[bot\]|dependabot|github-actions|jenkins|automation", re.I)
SEP = "\x1f"


def _git(*args: str, cwd: Path | None = None, timeout: int = 600, check: bool = True) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"}
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                         errors="replace")
    if check and res.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout


def clone(url: str, path: Path, timeout: int) -> None:
    if (path / "HEAD").is_file():
        _git("fetch", "-q", "--filter=blob:none", "origin", "+HEAD:HEAD", cwd=path, timeout=timeout, check=False)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _git("clone", "-q", "--bare", "--filter=blob:none", "--no-tags", url, str(path), timeout=timeout)


def commits(path: Path, n: int) -> list[dict[str, Any]]:
    """Recent non-merge commits touching .bal files: sha, parents, author, subject, files [(path, status)]."""
    out = _git("log", "--no-merges", f"-n{n}", f"--format=@@{SEP}%H{SEP}%P{SEP}%an{SEP}%ae{SEP}%s", "--raw",
               "--no-renames", "--", "*.bal", cwd=path)
    res, cur = [], None
    for line in out.splitlines():
        if line.startswith("@@" + SEP):
            _, sha, parents, an, ae, subject = line.split(SEP, 5)
            cur = {"sha": sha, "parents": parents.split(), "author": f"{an} {ae}", "subject": subject, "files": []}
            res.append(cur)
        elif line.startswith(":") and cur is not None:
            meta, _, file = line.partition("\t")
            cur["files"].append((file, meta.split()[-1][:1]))  # status: M, A, D, T
    return res


def is_test(path: str) -> bool:
    return "/tests/" in f"/{path}" or "/test/" in f"/{path}"


def line_changes(before: str, after: str) -> tuple[int, int]:
    added = deleted = 0
    for ln in difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0):
        if ln.startswith("+") and not ln.startswith("+++"):
            added += 1
        elif ln.startswith("-") and not ln.startswith("---"):
            deleted += 1
    return added, deleted


def qualifies(c: dict[str, Any], max_files: int) -> list[str] | None:
    """The modified .bal source files of a commit that reads like a fix, or None (size is checked later)."""
    if len(c["parents"]) != 1 or BOT_RE.search(c["author"]):
        return None
    if not FIX_RE.search(c["subject"]) or SKIP_RE.search(c["subject"]):
        return None
    src = [(p, st) for p, st in c["files"] if p.endswith(".bal") and not is_test(p)]
    if not src or len(src) > max_files or any(st != "M" for _, st in src):
        return None
    return [p for p, _ in src]


def show(path: Path, rev: str, file: str) -> str | None:
    try:
        return _git("show", f"{rev}:{file}", cwd=path, timeout=120)
    except RuntimeError:
        return None


def mine(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    from ballerinascraper.repos import classify_syntax

    cfg = s.miner
    marks = ",".join("?" for _ in cfg.groups)
    repos = db.query(f"SELECT * FROM repos WHERE status = 'collected' AND verdict = 'safe' AND grp IN ({marks}) "
                     "ORDER BY priority, COALESCE(stars, 0) DESC, full_name", tuple(cfg.groups))
    done = {r[0] for r in db.query("SELECT value FROM kv WHERE key LIKE 'mined:%'")}
    todo = [r for r in repos if r["full_name"] not in done]
    if s.limit_repos:
        todo = todo[: s.limit_repos]
    dc = Decontaminator.from_db(db, s.eval_)
    stats = {"repos": len(todo), "commits_scanned": 0, "qualified": 0, "saved": 0, "legacy": 0, "eval_overlap": 0,
             "duplicate": 0, "failed_repos": 0}
    for repo in todo:
        name = repo["full_name"]
        path = s.cache_dir / "mine" / f"{name.replace('/', '__')}.git"
        try:
            clone(repo["url"], path, cfg.clone_timeout_s)
            cs = commits(path, cfg.max_commits)
        except Exception as exc:
            stats["failed_repos"] += 1
            log.warning("mining %s failed: %s", name, exc)
            continue
        stats["commits_scanned"] += len(cs)
        saved = 0
        for c in cs:
            if saved >= cfg.max_fixes_per_repo:
                break
            files = qualifies(c, cfg.max_files)
            if not files:
                continue
            stats["qualified"] += 1
            pairs = []
            for f in files:
                before, after = show(path, c["parents"][0], f), show(path, c["sha"], f)
                if before is None or after is None or before == after:
                    pairs = []
                    break
                pairs.append({"path": f, "before": before, "after": after})
            if not pairs:
                continue
            changes = [line_changes(p["before"], p["after"]) for p in pairs]
            if sum(a + d for a, d in changes) > cfg.max_lines or sum(d for _, d in changes) == 0:
                continue  # too big, or pure additions (a feature, not a fix)
            if any(classify_syntax(p["after"], None) == "legacy" or classify_syntax(p["before"], None) == "legacy"
                   for p in pairs):
                stats["legacy"] += 1
                continue
            overlap = next((hit for p in pairs for hit in (dc.check(p["after"]), dc.check(p["before"])) if hit), None)
            if overlap:
                stats["eval_overlap"] += 1
                continue
            key = sha256_text("\n".join(code_hash(p["before"]) + code_hash(p["after"]) for p in pairs))
            if db.scalar("SELECT 1 FROM mined_fixes WHERE fix_hash = ? OR (full_name = ? AND commit_sha = ?)",
                         (key, name, c["sha"])):
                stats["duplicate"] += 1
                continue
            diff = "".join("".join(difflib.unified_diff(p["before"].splitlines(keepends=True),
                                                         p["after"].splitlines(keepends=True),
                                                         fromfile=f"a/{p['path']}", tofile=f"b/{p['path']}"))
                           for p in pairs)
            touches_tests = any(p.endswith(".bal") and is_test(p) for p, _ in c["files"])
            db.insert("mined_fixes", {"repo_id": repo["id"], "full_name": name, "commit_sha": c["sha"],
                                      "parent_sha": c["parents"][0], "message": c["subject"][:300],
                                      "files": json.dumps(pairs, ensure_ascii=False), "diff": diff,
                                      "lines_changed": sum(a + d for a, d in changes),
                                      "touches_tests": int(touches_tests), "spdx": repo["spdx"], "fix_hash": key,
                                      "status": "mined"})
            saved += 1
            stats["saved"] += 1
        db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (f"mined:{name}", name))
        log.info("mined %s: %d commit(s) scanned, %d fix(es) saved", name, len(cs), saved)
        run.flush()
    stats["files"] = write_fixes(s, db)
    run.stats.update({k: v for k, v in stats.items() if isinstance(v, int)})
    return stats


def write_fixes(s: Settings, db: Database) -> dict[str, str]:
    root = s.corpus_dir
    root.mkdir(parents=True, exist_ok=True)
    tmp = root / ".fixes.jsonl.part"
    per_repo: dict[str, int] = {}
    with tmp.open("w", encoding="utf-8") as f:
        for r in db.query("SELECT * FROM mined_fixes WHERE eval_overlap IS NULL ORDER BY full_name, id"):
            f.write(json.dumps({"id": f"fix{r['id']}", "repo": r["full_name"], "commit": r["commit_sha"],
                                "parent": r["parent_sha"], "message": r["message"], "licence": r["spdx"],
                                "files": json.loads(r["files"]), "diff": r["diff"],
                                "lines_changed": r["lines_changed"], "touches_tests": bool(r["touches_tests"]),
                                "status": r["status"]}, ensure_ascii=False) + "\n")
            per_repo[r["full_name"]] = per_repo.get(r["full_name"], 0) + 1
    tmp.replace(root / "fixes.jsonl")
    register = LicenceRegister(s.path(s.repos.register_file))
    for name, n in per_repo.items():
        register.upsert(name, mined_fixes=n)
    register.save()
    return {"fixes": str((root / "fixes.jsonl").relative_to(s.data_root)), "count": str(sum(per_repo.values()))}
