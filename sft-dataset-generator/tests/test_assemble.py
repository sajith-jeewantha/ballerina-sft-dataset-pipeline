"""M10: dataset assembly (sources, decontamination, dedup, balance, split, files, card)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from balpipe_common.config import Settings
from balpipe_common.db import Database

from sftgen.assemble import build

EVAL = ("type Album record {| string id; string title; |};\n\nfunction findAlbum(Album[] albums, string id) returns "
        "Album? {\n    foreach Album a in albums {\n        if a.id == id {\n            return a;\n        }\n    }\n"
        "    return ();\n}\n")


def _item(db: Database, ref: str, priority: str) -> None:
    node, item = ref.split("#")
    db.insert("graph_items", {"ref": ref, "node_id": node, "item_id": item, "node_type": "library",
                              "node_title": node, "priority": priority, "text": ref, "difficulty": "[]",
                              "own_sources": 1, "graph_version": "g1", "status": "grounded"})


def _answered(db: Database, i: int, ref: str, task: str, diff: str, prompt: str, answer: str, *,
              status: str = "verified", repaired: int = 0, state: int | None = None, pair: bool = False) -> int:
    pid = db.insert("prompts", {"set_name": "pilot", "ref": ref, "node_id": ref.split("#")[0], "task_type": task,
                                "difficulty": diff, "prompt": prompt, "prompt_hash": f"h{i}",
                                "covers": json.dumps([ref]), "status": "accepted", "writer_model": "deepseek/w",
                                "reviewer_model": "qwen/r", "context_mode": "codebase" if state else "standalone",
                                "codebase_state_id": state})
    first = db.insert("attempts", {"prompt_id": pid, "n": 1, "model": "deepseek/t", "answer": "broken " + answer,
                                   "status": "fail" if pair else "pass"})
    final = first
    if pair:
        final = db.insert("attempts", {"prompt_id": pid, "n": 2, "model": "deepseek/t", "answer": answer,
                                       "status": "pass"})
        db.insert("preference_pairs", {"prompt_id": pid, "chosen_id": final, "rejected_id": first,
                                       "reason": "compile"})
    db.insert("responses", {"prompt_id": pid, "set_name": "pilot", "attempt_id": final, "attempts": 2 if pair else 1,
                            "repaired": repaired, "verification": "compile", "status": status})
    return pid


def _seed(s: Settings, db: Database) -> None:
    _item(db, "lib.http#client", "core")
    _item(db, "lib.http#service", "core")
    _item(db, "lib.io#print", "long_tail")
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot"]
    tasks = [("write_code", "beginner"), ("explain", "intermediate"), ("write_code", "beginner"),
             ("use_api", "advanced"), ("write_code", "beginner"), ("design", "intermediate")]
    for i, (w, (task, diff)) in enumerate(zip(words, tasks)):
        _answered(db, i, "lib.http#client", task, diff,
                  f"How do I {w} with the {w} http:Client when calling {w} backends on port {i}?",
                  f"Use `http:Client` for {w}.", pair=(i == 1))
    _answered(db, 10, "lib.http#service", "write_code", "beginner", "HOW DO I ALPHA WITH THE ALPHA http:Client "
              "when calling alpha backends on port 0?", "Same prompt, other case.", status="judged")    # exact dup
    _answered(db, 11, "lib.http#service", "write_code", "beginner", "Write a service like this one:\n" + EVAL,
              "```ballerina\n" + EVAL + "```")                                                        # eval overlap
    _answered(db, 12, "lib.http#service", "fix_code", "beginner", "Never answered well", "x", status="failed")
    db.insert("codebases", {"id": "cb-1", "full_name": "someone/app", "root": "", "commit_sha": "c0ffee",
                            "spdx": "MIT", "files": "{}", "file_count": 1, "tokens": 10, "has_tests": 0,
                            "split": "train", "status": "active"})
    sid = db.insert("codebase_states", {"codebase_id": "cb-1", "kind": "compile_error", "target": "main.bal", "description": "d",
                                        "changed": "{}", "patch": "", "expect": "fails_build",
                                        "verify_status": "pass", "status": "active"})
    _answered(db, 13, "lib.http#service", "fix_code", "intermediate", "I'm working on this project... fix it",
              "```ballerina\n// file: main.bal\npublic function main() {}\n```", state=sid)
    # Q&A from a page whose chunk grounds lib.io#print, and one invalid answer
    page = db.insert("pages", {"source": "learn", "url": "https://ballerina.io/learn/io", "status": "chunked"})
    chunk = db.insert("chunks", {"page_id": page, "source": "learn", "source_url": "https://ballerina.io/learn/io",
                                 "ordinal": 0, "heading_path": "IO", "text": "io:println prints", "text_hash": "t",
                                 "token_estimate": 5, "has_code": 0, "status": "questioned"})
    db.insert("item_sources", {"ref": "lib.io#print", "url": "https://ballerina.io/learn/io", "kind": "docs",
                               "inherited": 0, "page_id": page, "status": "matched"})
    for i, st in enumerate(("valid", "invalid")):
        q = db.insert("questions", {"chunk_id": chunk, "source": "learn", "source_url": "https://ballerina.io/learn/io",
                                    "question": f"What does io:println do in case {i} of the printing guide?",
                                    "question_hash": f"q{i}", "category": "api", "difficulty": "beginner",
                                    "status": "answered"})
        db.insert("answers", {"question_id": q, "answer": "It prints a line.\n\nSource: https://ballerina.io/learn/io",
                              "status": st, "model": "deepseek/t"})
    db.insert("mined_fixes", {"full_name": "ballerina-platform/module-x", "commit_sha": "abc", "parent_sha": "def",
                              "message": "Fix off-by-one", "files": json.dumps([{"path": "a.bal", "before": "int i = 1;",
                                                                                   "after": "int i = 0;"}]),
                              "diff": "", "lines_changed": 2, "touches_tests": 0, "spdx": "Apache-2.0",
                              "fix_hash": "fx", "status": "mined"})
    db.insert("mined_fixes", {"full_name": "ballerina-platform/module-x", "commit_sha": "abd", "parent_sha": "def",
                              "message": "Fix album lookup", "files": json.dumps([{"path": "b.bal", "before": EVAL,
                                                                                   "after": EVAL + "// fixed\n"}]),
                              "diff": "", "lines_changed": 1, "touches_tests": 0, "spdx": "Apache-2.0",
                              "fix_hash": "fy", "status": "mined"})                 # copies an eval item
    from sftgen.eval.tasks import fingerprint

    db.insert("eval_items", {"id": "bbe/albums", "suite": "bbe", "version": "v1", "name": "albums", "url": "x",
                             "files": "{}", "verify_mode": "none", "status": "active"})
    fingerprint(db, "bbe/albums", {"albums.bal": EVAL})
    corpus = s.corpus_dir
    corpus.mkdir(parents=True)
    (corpus / "part-0000.jsonl.gz").write_bytes(b"x")
    (corpus / "ATTRIBUTION.md").write_text("# a\n")
    (corpus / "manifest.json").write_text(json.dumps({"version": "raw-v1", "files": 3, "tokens": 1000,
                                                      "weighted_tokens": 900, "repos": 1, "generated_tokens": 0, "by_group": {}, "left_out": {},
                                                      "by_licence": {"Apache-2.0": {"files": 3, "tokens": 1000}}}))


def test_assemble_dataset_v1(settings: Settings, db: Database) -> None:
    s = settings.model_copy(update={"assemble": settings.assemble.model_copy(update={
        "caps": {"core": 4, "important": 3, "long_tail": 2}, "include_mined_fixes": True, "eval_fraction": 0.0})})
    _seed(s, db)
    with db.run("assemble", s.limits()) as run:
        out = build.assemble(s, db, run)
    assert out["found"] == {"prompt": 8, "question": 1, "mined_fix": 2}
    # the overlapping prompt is flagged by apply_decontam before loading; the mined fix by the final check
    assert out["eval_overlap_dropped"] == 1 and out["eval_overlap_in_export"] == 0
    assert out["duplicates"]["exact"] == 1                  # the upper-case copy; the verified one is kept
    assert out["over_cap"] == 2                             # lib.http#client has 6 samples, cap 4
    root = s.dataset_dir
    meta = [json.loads(x) for x in (root / "metadata.jsonl").read_text().splitlines()]
    by_id = {m["id"]: m for m in meta}
    client = [m for m in meta if m["ref"] == "lib.http#client"]
    # round-robin over (task, difficulty): every kind present before a second write_code/beginner
    assert {m["task_type"] for m in client} == {"write_code", "explain", "use_api", "design"}
    assert by_id["p1"]["verification"] == "compile" and "p7" not in by_id      # p7 = the exact duplicate (judged)
    assert "p8" not in by_id and "m2" not in by_id                              # eval overlaps
    cb = next(m for m in meta if m["context_mode"] == "codebase")
    assert cb["licence"] == "MIT" and cb["repo"] == "someone/app" and cb["codebase"] == "cb-1"
    qa = next(m for m in meta if m["source"] == "question")
    assert qa["ref"] == "lib.io#print" and qa["docs_source"] == "learn"
    train = [json.loads(x) for x in (root / "sft" / "train.jsonl").read_text().splitlines()]
    row = next(r for r in train if r["id"] == qa["id"])
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]
    assert "Source:" not in row["messages"][2]["content"]
    assert (root / "sft" / "eval.jsonl").read_text() == ""
    pairs = [json.loads(x) for x in (root / "dpo" / "pairs.jsonl").read_text().splitlines()]
    assert len(pairs) == 1 and pairs[0]["chosen"][0]["content"].startswith("Use") and pairs[0]["rejected"]
    assert (root / "cpt" / "part-0000.jsonl.gz").is_file() and out["cpt_tokens"] == 1000
    gaps = [json.loads(x) for x in (root / "gaps.jsonl").read_text().splitlines()]
    assert {g["ref"] for g in gaps} == {"lib.http#client", "lib.http#service"} | (
        {"lib.io#print"} if "lib.io" in s.prompts.nodes else set())
    card = (root / "CARD.md").read_text()
    assert "Ballerina SFT dataset v1" in card and "`lib.http#client`" in card and "someone/app" in \
        (root / "ATTRIBUTION.md").read_text()
    cj = json.loads((root / "card.json").read_text())
    assert cj["models"]["teacher"] == ["deepseek/t"] and cj["graph"]["version"] == "g1"
    assert cj["composition"]["source"] == {"prompt": 5, "question": 1, "mined_fix": 1}
    manifest = json.loads((root / "manifest.json").read_text())
    f = manifest["files"]["sft/train.jsonl"]
    assert f["sha256"] == hashlib.sha256((root / "sft" / "train.jsonl").read_bytes()).hexdigest()

    from sftgen.report import write_report

    path, summary = write_report(s, db)
    assert "<h2>Dataset v1</h2>" in Path(path).read_text(encoding="utf-8") and "Dataset    v1: train 7" in summary


def test_split_is_by_group(settings: Settings) -> None:
    cfg = settings.assemble.model_copy(update={"eval_fraction": 0.5})
    sides = {build.split_for(f"codebase:cb-{i}", cfg) for i in range(40)}
    assert sides == {"train", "eval"}
    assert build.split_for("codebase:cb-1", cfg) == build.split_for("codebase:cb-1", cfg)
