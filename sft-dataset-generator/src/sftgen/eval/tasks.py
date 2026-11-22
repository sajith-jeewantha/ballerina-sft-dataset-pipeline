"""Eval set v2 tasks besides the benchmarks (`eval tasks`, `eval ecosystem`).

* **Codebase tasks** (no model calls): every verified state of an **eval-split** codebase (M7)
  becomes an eval item (suite `codebase`): fix the compile error, implement the stub, migrate the
  1.x service. Each eval codebase also gets an `explain` task (what its main file does), scored by
  the judge against the code.
* **BBE tasks** (no model calls): each BBE eval item (M4) gets a prompt: write the program the
  example's explanation describes, with its expected output. Scored by running it against the
  `.out` transcript with `eval.run_tests`; otherwise by compiling.
* **Ecosystem tasks** (teacher): per core graph node, `eval.ecosystem_tasks_per_node` tasks with
  a reference answer, written from the node's grounding. A write_code reference must build.

Every task's code is fingerprinted (`fingerprints`), so `build-eval`'s decontamination keeps
near-copies out of training.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from balpipe_common.config import ConfigError, Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.decontam import pack_shingles
from balpipe_common.dedup import MinHasher, code_hash, shingle_hashes

from sftgen.codebases.attach import pack, task_text
from sftgen.generate.llm import LLMClient, load_prompt
from sftgen.generate.questions import record_usage
from sftgen.plan.planner import load_nodes
from sftgen.retrieval.grounding import ground_item
from sftgen.validate.answer_check import code_blocks
from sftgen.validate.bal_compiler import BalCompiler

log = logging.getLogger(__name__)
TASK = {"compile_error": "fix_code", "unfinished_feature": "write_code", "version_drift": "fix_code",
        "failing_test": "fix_code"}
RUN_LINE_RE = re.compile(r"(as follows|below)[.:]?\s*$|^\s*(Run|Execute|Invoke|Build) the (example|program|service|"
                         r"client|sample|code)", re.I)
MD_CODE_RE = re.compile(r"```.*?```|::: code.*?:::|::: out.*?:::|<[^>]+>", re.S)


def fingerprint(db: Database, item_id: str, codes: dict[str, str]) -> int:
    """(Re)write the fingerprints of an eval item's code."""
    h = MinHasher()
    db.execute("DELETE FROM fingerprints WHERE item_id = ?", (item_id,))
    n = 0
    for path, code in codes.items():
        if code and code.strip():
            db.insert("fingerprints", {"item_id": item_id, "path": path, "code_hash": code_hash(code),
                                       "minhash": h.to_bytes(h.signature(code)),
                                       "shingles": pack_shingles(shingle_hashes(code))})
            n += 1
    return n


def _upsert_item(db: Database, item_id: str, values: dict[str, Any]) -> None:
    values = {"version": "v2", "status": "active", "covers": "[]", "nodes": "[]", **values}
    if db.scalar("SELECT 1 FROM eval_items WHERE id = ?", (item_id,)):
        db.update("eval_items", item_id, values)
    else:
        db.insert("eval_items", {"id": item_id, **values})


def codebase_tasks(s: Settings, db: Database) -> dict[str, int]:
    n_fix, n_explain = 0, 0
    for cb in db.query("SELECT * FROM codebases WHERE status = 'active' AND split = 'eval' ORDER BY id"):
        base = json.loads(cb["files"])
        for st in db.query("SELECT * FROM codebase_states WHERE codebase_id = ? AND verify_status = 'pass' "
                           "AND status = 'active' ORDER BY kind", (cb["id"],)):
            files = {**base, **json.loads(st["changed"])}
            path = st["target"].split("::")[0]
            packed, _, _ = pack(files, path, s.codebases.context_max_tokens)
            prompt = (f"I'm working on this Ballerina project (Swan Lake 2201.14.1):\n\n{packed}\n\n"
                      + task_text(st["kind"], st["target"], json.loads(st["verify_detail"] or "{}"), st["description"]))
            iid = f"codebase/{cb['id']}/{st['kind']}"
            _upsert_item(db, iid, {
                "suite": "codebase", "name": cb["id"], "title": st["kind"], "category": "codebase", "url": cb["id"],
                "commit_sha": cb["commit_sha"], "files": json.dumps(files), "covers": cb["items"],
                "nodes": cb["nodes"], "verify_mode": "codebase", "verify_status": "pass", "prompt": prompt,
                "task_type": TASK[st["kind"]], "reference": json.dumps({p: base[p] for p in json.loads(st["changed"])}),
                "meta": json.dumps({"state_id": st["id"], "kind": st["kind"], "target": st["target"]})})
            fingerprint(db, iid, {p: base[p] for p in json.loads(st["changed"]) if p.endswith(".bal")})
            n_fix += 1
        main = max((p for p in base if p.endswith(".bal") and not p.startswith("tests/")),
                   key=lambda p: len(base[p]), default=None)
        if main:
            packed, _, _ = pack(base, main, s.codebases.context_max_tokens)
            iid = f"codebase/{cb['id']}/explain"
            _upsert_item(db, iid, {
                "suite": "codebase", "name": cb["id"], "title": "explain", "category": "codebase", "url": cb["id"],
                "commit_sha": cb["commit_sha"], "files": json.dumps(base), "covers": cb["items"], "nodes": cb["nodes"],
                "verify_mode": "judge", "verify_status": "pass", "task_type": "explain",
                "prompt": (f"Here is a Ballerina project:\n\n{packed}\n\nExplain what `{main}` does: its main "
                           "components, how a request or event flows through it, and the Ballerina features it relies "
                           "on. Be specific to this code."),
                "reference": base[main], "meta": json.dumps({"file": main})})
            fingerprint(db, iid, {p: t for p, t in base.items() if p.endswith(".bal")})
            n_explain += 1
    return {"codebase_tasks": n_fix, "codebase_explain": n_explain}


def bbe_prompt(item: Any) -> str:
    files = json.loads(item["files"])
    md = next((t for f, t in files.items() if f.endswith(".md")), "")
    text = MD_CODE_RE.sub("", md)
    text = "\n".join(ln for ln in text.splitlines() if not ln.startswith("#") and "learn/by-example" not in ln
                     and not RUN_LINE_RE.search(ln))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    out = next((t for f, t in files.items() if f.endswith(".out")), "")
    expected = "\n".join(out.splitlines()[1:]).strip()
    return (f"Write a complete Ballerina program for this: {item['title']}.\n\n{text}"
            + (f"\n\nWhen run with `bal run`, it should print:\n\n```\n{expected}\n```" if expected and
               item["verify_mode"] == "run" else ""))


def bbe_tasks(db: Database) -> dict[str, int]:
    n = 0
    for it in db.query("SELECT * FROM eval_items WHERE suite = 'bbe' AND status = 'active'"):
        db.update("eval_items", it["id"], {"prompt": bbe_prompt(it), "task_type": "write_code"})
        n += 1
    return {"bbe_prompts": n}


def build_tasks(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    out = {**codebase_tasks(s, db), **bbe_tasks(db)}
    run.stats.update(out)
    return out


# --------------------------------------------------------------------------- #
# Ecosystem tasks (teacher)
# --------------------------------------------------------------------------- #
class EcoTask(BaseModel):
    task_type: str
    prompt: str
    reference: str
    covers: list[str] = Field(default_factory=list)


class EcoTasks(BaseModel):
    tasks: list[EcoTask] = Field(default_factory=list)


async def _ecosystem(s: Settings, db: Database, run: RunTracker, client: LLMClient,
                     compiler: BalCompiler) -> dict[str, int]:
    model = s.answer_gen_model or ""
    prompt = load_prompt("ecosystem_tasks")
    nodes = load_nodes(s)
    core = sorted(n for n, info in nodes.items() if info.get("priority") == "core")
    done = {r[0] for r in db.query("SELECT DISTINCT name FROM eval_items WHERE suite = 'ecosystem'")}
    todo = [n for n in core if n not in done]
    if s.limit_generating:
        todo = todo[: s.limit_generating]
    stats = {"nodes": len(todo), "tasks": 0, "rejected": 0, "errors": 0}
    for nid in todo:
        items = db.query("SELECT * FROM graph_items WHERE node_id = ? AND status != 'retired' ORDER BY ref", (nid,))
        if not items:
            continue
        g = ground_item(db, dict(items[0]), nodes[nid], s.prompts)
        try:
            out, usage = await client.chat_json(
                model, prompt, EcoTasks, temperature=0.4, max_tokens=4000, node_title=nodes[nid]["title"], node_id=nid,
                node_summary=nodes[nid]["summary"], items="\n".join(f"{r['item_id']}: {r['text']}" for r in items[:25]),
                grounding=g.render(), n=s.eval_.ecosystem_tasks_per_node)
            record_usage(db, run, "ecosystem-tasks", usage, model, True)
        except Exception as exc:
            record_usage(db, run, "ecosystem-tasks", None, model, False)
            stats["errors"] += 1
            log.warning("ecosystem tasks for %s failed: %s", nid, exc)
            continue
        valid = {r["item_id"] for r in items}
        for i, t in enumerate(out.tasks[: s.eval_.ecosystem_tasks_per_node], 1):
            task = t.task_type if t.task_type in ("write_code", "explain") else "write_code"
            if task == "write_code":
                blocks = code_blocks(t.reference) or code_blocks(f"```ballerina\n{t.reference}\n```")
                ok = bool(blocks) and all((await asyncio.to_thread(compiler.compile, b.code)).status == "ok"
                                          for b in blocks if not b.file)
                if not ok:
                    stats["rejected"] += 1
                    continue
            covers = [f"{nid}#{c}" for c in t.covers if c in valid]
            iid = f"ecosystem/{nid}/{i}"
            _upsert_item(db, iid, {"suite": "ecosystem", "name": nid, "title": nodes[nid]["title"],
                                   "category": "ecosystem", "url": nid, "files": "{}", "covers": json.dumps(covers),
                                   "nodes": json.dumps([nid]), "verify_mode": "build" if task == "write_code"
                                   else "judge", "verify_status": "pass", "prompt": t.prompt, "task_type": task,
                                   "reference": t.reference, "meta": json.dumps({"model": model})})
            fingerprint(db, iid, {"reference": t.reference})
            stats["tasks"] += 1
        run.flush()
    return stats


def ecosystem(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    s.require("llm_api_key", "answer_gen_model")
    if not s.path(s.graph.path).is_file():
        raise ConfigError("the graph file is missing")
    stats = asyncio.run(_ecosystem(s, db, run, LLMClient(s, s.cache_dir / "llm"),
                                   BalCompiler(s.validate_, s.cache_dir / "bal")))
    run.stats.update(stats)
    return stats
