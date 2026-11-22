"""`codebases attach`: codebase-task prompts from verified states, with the project packed in.

Each verified state of a **train-split** codebase becomes one prompt (`context_mode = codebase`,
`codebase_state_id` set) in the current prompt set, status `accepted` (the task is checked by the
toolchain, not reviewed). `answer-prompts` then answers it: the answer gives every changed file in
full under `// file: <path>`, those files are laid over the state, and the result must build
(and, for `unfinished_feature`, no longer contain the stub).

**Context packing** (`pack`), up to `codebases.context_max_tokens` (~14k of Qwen3.8's ~16k server
budget): Ballerina.toml, the file the task is about, the other source files from smallest, then the
tests. Files that don't fit are listed by name.

Tasks per kind (task type · difficulty):
* compile_error       fix_code · beginner/intermediate: the real `bal build` errors are quoted
* unfinished_feature  write_code · intermediate/advanced: implement the stubbed function
* version_drift       fix_code · advanced: migrate the 1.x-style service to Swan Lake
* failing_test        fix_code · intermediate: the failing test and its assertion are quoted
"""

from __future__ import annotations

import json
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.ids import content_hash

TASK = {"compile_error": "fix_code", "unfinished_feature": "write_code", "version_drift": "fix_code",
        "failing_test": "fix_code"}
STUB = 'panic error("not implemented");'


def _fence(path: str, text: str) -> str:
    if path.endswith(".toml"):
        return f"```toml\n# file: {path}\n{text.rstrip()}\n```"
    return f"```ballerina\n// file: {path}\n{text.rstrip()}\n```"


def pack(files: dict[str, str], target: str, max_tokens: int) -> tuple[str, list[str], list[str]]:
    """(packed text, files shown, files left out)."""
    order = (["Ballerina.toml"] if "Ballerina.toml" in files else []) + ([target] if target in files else [])
    rest = [p for p in files if p not in order]
    order += sorted((p for p in rest if not p.startswith("tests/")), key=lambda p: len(files[p]))
    order += sorted((p for p in rest if p.startswith("tests/")), key=lambda p: len(files[p]))
    shown, left, used, parts = [], [], 0, []
    for p in order:
        cost = len(files[p]) // 4 + 20
        if used + cost > max_tokens and p != target and shown:
            left.append(p)
            continue
        parts.append(_fence(p, files[p]))
        shown.append(p)
        used += cost
    text = "\n\n".join(parts)
    if left:
        text += "\n\n(Other files in the project, not shown: " + ", ".join(left) + ")"
    return text, shown, left


def task_text(kind: str, target: str, detail: dict[str, Any], description: str) -> str:
    path, _, fn = target.partition("::")
    if kind == "compile_error":
        errs = "\n".join(f"ERROR [{e.get('file') or path}:({e.get('line')}:{e.get('col')})] {e.get('message')}"
                         for e in detail.get("errors", [])[:5])
        return (f"`bal build` fails with:\n\n```\n{errs}\n```\n\nFix the project so it builds. "
                "Give each file you change in full.")
    if kind == "unfinished_feature":
        return (f"`{fn}` in `{path}` isn't implemented yet: it just panics. Implement it so it does what its name, "
                "signature, documentation and the rest of the project imply. Give each file you change in full.")
    if kind == "version_drift":
        return (f"The service in `{path}` was written for Ballerina 1.x and doesn't build on Swan Lake "
                "(2201.14.1). Migrate it to Swan Lake syntax, keeping its paths, methods and behaviour. "
                "Give each file you change in full.")
    errs = "\n".join(e.get("message", "") for e in detail.get("errors", [])[:5])
    return (f"`bal test` fails:\n\n```\n{errs}\n```\n\nFix the code (not the tests) so the tests pass. "
            "Give each file you change in full.")


def difficulty(kind: str, tokens: int) -> str:
    if kind == "version_drift":
        return "advanced"
    if kind == "compile_error":
        return "beginner" if tokens < 2500 else "intermediate"
    return "advanced" if tokens > 5000 else "intermediate"


def attach(s: Settings, db: Database, run: RunTracker) -> dict[str, Any]:
    cfg = s.codebases
    stats = {"states": 0, "added": 0, "existing": 0, "eval_split_skipped": 0}
    rows = db.query(
        "SELECT st.*, cb.files, cb.split, cb.items, cb.nodes, cb.tokens FROM codebase_states st "
        "JOIN codebases cb ON cb.id = st.codebase_id WHERE cb.status = 'active' AND st.status = 'active' "
        "AND st.verify_status = 'pass' ORDER BY st.codebase_id, st.kind")
    for r in rows:
        stats["states"] += 1
        if r["split"] != "train":
            stats["eval_split_skipped"] += 1
            continue
        if db.scalar("SELECT 1 FROM prompts WHERE codebase_state_id = ? AND set_name = ?",
                     (r["id"], s.prompts.set_name)):
            stats["existing"] += 1
            continue
        files = {**json.loads(r["files"]), **json.loads(r["changed"])}
        path = r["target"].split("::")[0]
        packed, _, _ = pack(files, path, cfg.context_max_tokens)
        detail = json.loads(r["verify_detail"] or "{}")
        text = (f"I'm working on this Ballerina project (Swan Lake 2201.14.1):\n\n{packed}\n\n"
                f"{task_text(r['kind'], r['target'], detail, r['description'])}")
        items = json.loads(r["items"]) or []
        nodes = json.loads(r["nodes"]) or []
        ref = items[0] if items else (f"{nodes[0]}#codebase" if nodes else "codebase#project")
        db.insert("prompts", {
            "set_name": s.prompts.set_name, "ref": ref, "node_id": ref.split("#")[0], "task_type": TASK[r["kind"]],
            "difficulty": difficulty(r["kind"], r["tokens"]), "context_mode": "codebase", "prompt": text,
            "prompt_hash": content_hash(text.lower()), "covers": json.dumps(items),
            "grounding": json.dumps({"codebase": r["codebase_id"], "state": r["kind"], "urls": []}),
            "round": 1, "review": json.dumps({"source": "codebase_state", "kind": r["kind"]}),
            "status": "accepted", "codebase_state_id": r["id"], "run_id": run.id})
        stats["added"] += 1
    run.stats.update(stats)
    return stats
