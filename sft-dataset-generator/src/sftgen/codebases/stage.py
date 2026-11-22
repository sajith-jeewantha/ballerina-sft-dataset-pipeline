"""`codebases build | verify | attach`.

* `build`   fill the pool to `codebases.pool_size` (base must build), tag it, make and verify its
            states, and write `<data>/codebases/` (`<id>/base.tar.gz`, `<id>/states/<kind>.patch`,
            `manifest.jsonl`).
* `verify`  re-check every active base and state with the toolchain (cached by content), print a
            table, and fail when anything is red.
* `attach`  turn each verified state of a train-split codebase into a codebase-task prompt
            (`attach.py`).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from balpipe_common.config import ConfigError, Settings
from balpipe_common.db import Database, RunTracker

from sftgen.codebases import pool
from sftgen.codebases.states import State, make_states, patch
from sftgen.plan.planner import load_nodes
from sftgen.validate.bal_compiler import BalCompiler, BalUnavailable, CompileOutcome

log = logging.getLogger(__name__)
UNUSED_RE = re.compile(r"unused module prefix '(\w+)'")


def compiler(s: Settings) -> BalCompiler:
    try:
        return BalCompiler(s.validate_, s.cache_dir / "bal")
    except BalUnavailable as exc:
        raise ConfigError(f"codebases needs the Ballerina toolchain: {exc}") from exc


def _build(c: BalCompiler, s: Settings, files: dict[str, str], *, test: bool = False) -> CompileOutcome:
    return c.build_package(files, test=test, timeout_s=s.codebases.build_timeout_s)


def _brief(res: CompileOutcome) -> str:
    return json.dumps({"status": res.status, "errors": res.errors[:5]})


def drop_unused_imports(code: str, prefixes: set[str]) -> str:
    def keep(line: str) -> bool:
        m = re.match(r"^\s*import\s+([\w.]+/[\w.]+)(?:\s+as\s+(\w+))?\s*;", line)
        if not m:
            return True
        prefix = m.group(2) or m.group(1).split("/")[-1].split(".")[-1]
        return prefix not in prefixes
    return "".join(ln for ln in code.splitlines(keepends=True) if keep(ln))


def verify_state(c: BalCompiler, s: Settings, base: dict[str, str], st: State) -> tuple[str, str, State]:
    """(pass | fail | skipped, detail, state); `builds` states may lose imports their stub left unused."""
    files = {**base, **st.changed}
    target_file = st.target.split("::")[0]
    if st.expect == "fails_tests":
        if not s.codebases.run_tests:
            return "skipped", "needs codebases.run_tests (runs the project's tests)", st
        res = _build(c, s, files, test=True)
        tests_only = res.status == "error" and all(e.get("file") == "tests" and e.get("line") is None
                                                   for e in res.errors)
        return ("pass" if tests_only else "fail"), _brief(res), st
    res = _build(c, s, files)
    if st.expect == "fails_build":
        ok = res.status == "error" and any(e.get("file") in (target_file, None) for e in res.errors)
        return ("pass" if ok else "fail"), _brief(res), st
    if res.status == "error":
        unused = {m.group(1) for e in res.errors if e.get("file") == target_file
                  for m in [UNUSED_RE.search(e.get("message", ""))] if m}
        if unused and all(UNUSED_RE.search(e.get("message", "")) for e in res.errors):
            st = State(st.kind, st.target, st.description,
                       {**st.changed, target_file: drop_unused_imports(st.changed[target_file], unused)}, st.expect)
            res = _build(c, s, {**base, **st.changed})
    return ("pass" if res.status == "ok" else "fail"), _brief(res), st


def save_state(db: Database, cid: str, base: dict[str, str], st: State, status: str, detail: str) -> None:
    values = {"target": st.target, "description": st.description, "changed": json.dumps(st.changed),
              "patch": patch(base, st.changed), "expect": st.expect, "verify_status": status,
              "verify_detail": detail, "status": "active"}
    row = db.one("SELECT id FROM codebase_states WHERE codebase_id = ? AND kind = ?", (cid, st.kind))
    if row:
        db.update("codebase_states", row["id"], values)
    else:
        db.insert("codebase_states", {"codebase_id": cid, "kind": st.kind, **values})


def build(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    cfg = s.codebases
    c = compiler(s)
    c.prewarm()
    nodes = load_nodes(s)
    known = {r["id"]: r["status"] for r in db.query("SELECT id, status FROM codebases")}
    ranked = pool.rank(pool.candidates(db, cfg), cfg)
    active = sum(1 for v in known.values() if v == "active")
    todo = [(sc, cand) for sc, cand in ranked if cand.id not in known][: cfg.max_candidates]
    stats: dict[str, Any] = {"candidates": len(ranked), "tried": 0, "added": 0, "rejected": 0}
    log.info("%d candidate project(s); pool has %d of %d", len(ranked), active, cfg.pool_size)
    workers = max(1, s.validate_.workers)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        i = 0
        while active < cfg.pool_size and i < len(todo):
            batch = todo[i: i + min(workers, cfg.pool_size - active)]
            i += len(batch)
            for (sc, cand), res in zip(batch, ex.map(lambda t: _build(c, s, t[1].files, test=cfg.run_tests), batch)):
                stats["tried"] += 1
                n_ids, items = pool.tag(db, nodes, cand)
                if res.status == "ok":
                    pool.save(db, s, cand, sc, n_ids, items, "active", None)
                    active += 1
                    stats["added"] += 1
                    log.info("pool + %s (%d files, ~%d tokens)", cand.id, len(cand.files), cand.tokens)
                else:
                    pool.save(db, s, cand, sc, n_ids, items, "rejected", _brief(res))
                    stats["rejected"] += 1
    stats["active"] = active

    # states for active codebases that miss some
    work = []
    for cb in db.query("SELECT id, files FROM codebases WHERE status = 'active' ORDER BY id"):
        have = {r["kind"] for r in db.query("SELECT kind FROM codebase_states WHERE codebase_id = ?", (cb["id"],))}
        base = json.loads(cb["files"])
        for st in make_states(cb["id"], base, [k for k in cfg.kinds if k not in have]):
            work.append((cb["id"], base, st))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for (cid, base, _), (status, detail, st) in zip(
                work, ex.map(lambda w: verify_state(c, s, w[1], w[2]), work)):
            save_state(db, cid, base, st, status, detail)
            stats[f"state_{status}"] = stats.get(f"state_{status}", 0) + 1
    stats["files"] = write_files(s, db)
    run.stats.update({k: v for k, v in stats.items() if isinstance(v, int)})
    return stats


def verify(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    """Re-check every active base and state; `red` lists anything that no longer verifies."""
    c = compiler(s)
    red: list[str] = []
    rows = db.query("SELECT id, files FROM codebases WHERE status = 'active' ORDER BY id")
    table = []
    for cb in rows:
        base = json.loads(cb["files"])
        res = _build(c, s, base, test=s.codebases.run_tests)
        line = {"codebase": cb["id"], "base": "pass" if res.status == "ok" else "fail"}
        if res.status != "ok":
            red.append(f"{cb['id']}: base {_brief(res)}")
        for r in db.query("SELECT * FROM codebase_states WHERE codebase_id = ? AND status = 'active'", (cb["id"],)):
            st = State(r["kind"], r["target"], r["description"], json.loads(r["changed"]), r["expect"])
            status, detail, _ = verify_state(c, s, base, st)
            if status != r["verify_status"]:
                db.update("codebase_states", r["id"], {"verify_status": status, "verify_detail": detail})
            line[r["kind"]] = status
            if status == "fail":
                red.append(f"{cb['id']}: {r['kind']} {detail[:200]}")
        table.append(line)
    out = {"codebases": len(rows), "red": red, "table": table}
    run.stats.update({"codebases": len(rows), "red": len(red)})
    return out


def write_files(s: Settings, db: Database) -> dict[str, str]:
    root = s.codebases_dir
    manifest = []
    for cb in db.query("SELECT * FROM codebases WHERE status = 'active' ORDER BY id"):
        base = json.loads(cb["files"])
        d = root / cb["id"]
        (d / "states").mkdir(parents=True, exist_ok=True)
        tmp = d / ".base.tar.gz.part"
        with tarfile.open(tmp, "w:gz") as tar:
            for rel, text in base.items():
                data = text.encode("utf-8")
                ti = tarfile.TarInfo(rel)
                ti.size = len(data)
                ti.mtime = 0
                tar.addfile(ti, io.BytesIO(data))
        tmp.replace(d / "base.tar.gz")
        states = []
        for st in db.query("SELECT * FROM codebase_states WHERE codebase_id = ? AND status = 'active' ORDER BY kind",
                           (cb["id"],)):
            (d / "states" / f"{st['kind']}.patch").write_text(st["patch"], encoding="utf-8")
            states.append({"kind": st["kind"], "target": st["target"], "description": st["description"],
                           "expect": st["expect"], "verify": st["verify_status"]})
        manifest.append({"id": cb["id"], "repo": cb["full_name"], "root": cb["root"], "commit": cb["commit_sha"],
                         "licence": cb["spdx"], "split": cb["split"], "files": cb["file_count"],
                         "tokens": cb["tokens"], "has_tests": bool(cb["has_tests"]),
                         "modules": json.loads(cb["modules"]), "nodes": json.loads(cb["nodes"]),
                         "items": json.loads(cb["items"]), "states": states})
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".manifest-", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for m in manifest:
            f.write(json.dumps(m, ensure_ascii=False) + "\n")
    os.replace(tmp_name, root / "manifest.jsonl")
    return {"manifest": str((root / "manifest.jsonl").relative_to(s.data_root)), "codebases": str(len(manifest))}
