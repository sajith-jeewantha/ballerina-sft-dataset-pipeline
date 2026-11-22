"""M5: planner budgets, FTS5 and symbol retrieval, grounding, and the writer/reviewer loop."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

import pytest

from balpipe_common.config import ConfigError, Settings
from balpipe_common.db import Database
from balpipe_common.dedup import MinHasher, code_hash, shingle_hashes
from balpipe_common.decontam import pack_shingles

from sftgen.generate import prompts as gp
from sftgen.generate.llm import Usage
from sftgen.plan.planner import plan, slots_for, summary, task_sequence
from sftgen.retrieval import fts, symbols
from sftgen.retrieval.grounding import ground_item

CENTRAL_MD = """# ballerina/http

## Overview

HTTP clients and services.

## API reference: module `http`

### Client: Client

The HTTP client.

#### get

```ballerina
remote isolated function get(string path) returns Response|ClientError
```

### Record: ClientConfiguration

Fields:
- `timeout` (`decimal`): request timeout in seconds

### Annotation: Cache

Response caching. Fields:
- `maxAge` (`decimal`): seconds
"""

EVAL_CODE = """import ballerina/http;

service /albums on new http:Listener(9090) {
    resource function get [string id]() returns Album|http:NotFound {
        Album? album = albums[id];
        return album is Album ? album : http:NOT_FOUND;
    }
}
"""


def _graph(root: Path) -> None:
    nodes = [{"id": "lib.http", "type": "library", "title": "HTTP", "summary": "HTTP clients and services.",
              "priority": "core", "status": "draft", "modules": ["ballerina/http"], "out_of_scope": ["gRPC"],
              "must_cover": [], "sources": [], "edges": []},
             {"id": "concept.x", "type": "concept", "title": "X", "summary": "", "priority": "important",
              "status": "draft", "modules": [], "out_of_scope": [], "must_cover": [], "sources": [], "edges": []}]
    (root / "graph").mkdir(exist_ok=True)
    (root / "graph" / "graph.json").write_text(json.dumps({"schema_version": "0.2", "nodes": nodes, "edges": []}))


def _seed(s: Settings, db: Database) -> None:
    _graph(s.repo_root)
    for ref, node, prio, diff, text in (
        ("lib.http#client_timeout", "lib.http", "core", ["beginner", "intermediate"],
         "Set a request timeout on `http:Client` with `ClientConfiguration` `timeout`"),
        ("lib.http#caching", "lib.http", "core", ["advanced"], "Cache responses with `@http:Cache {maxAge: 15}`"),
        ("concept.x#a", "concept.x", "important", ["beginner"], "Unrelated concept item"),
    ):
        db.insert("graph_items", {"ref": ref, "node_id": node, "item_id": ref.split("#")[1], "node_type": "library",
                                  "node_title": node, "priority": prio, "text": text, "difficulty": json.dumps(diff),
                                  "own_sources": 1, "graph_version": "g", "status": "grounded"})
    central = db.insert("pages", {"source": "central", "url": "https://central.ballerina.io/ballerina/http",
                                  "status": "chunked", "markdown": CENTRAL_MD,
                                  "meta": json.dumps({"org": "ballerina", "package": "http"})})
    bbe = db.insert("pages", {"source": "bbe", "url": "https://ballerina.io/learn/by-example/http-timeout/",
                              "status": "chunked", "meta": "{}"})
    for i, (page, heading, text) in enumerate((
        (bbe, "HTTP client - Timeout", "Set the timeout field of http:ClientConfiguration to bound how long the "
                                       "client waits for a response before it fails with an error. " * 3),
        (bbe, "HTTP client - Timeout > Related links", "Links to related examples about clients and timeouts " * 3),
        (central, "ballerina/http > Overview", "HTTP response caching uses the Cache annotation with maxAge. " * 4),
    )):
        db.insert("chunks", {"page_id": page, "source": "bbe", "source_url": "u" + str(page), "ordinal": i,
                             "heading_path": heading, "text": text, "text_hash": str(i), "token_estimate": 60,
                             "has_code": 0, "status": "pending"})
    db.insert("item_sources", {"ref": "lib.http#client_timeout", "url": "https://ballerina.io/learn/by-example/"
                               "http-timeout/", "kind": "bbe", "inherited": 0, "page_id": bbe, "status": "matched"})
    repo = db.insert("repos", {"full_name": "ballerina-platform/module-ballerina-http", "grp": "platform",
                               "priority": 0, "url": "u", "status": "collected"})
    db.insert("repo_files", {"repo_id": repo, "path": "ballerina/http_cache.bal", "kind": "bal", "bytes": 10,
                             "text": "public type CacheConfig record {| decimal maxAge = 3600; |};", "status": "kept"})
    db.insert("item_sources", {"ref": "lib.http#caching", "url": "https://github.com/ballerina-platform/"
                               "module-ballerina-http/blob/master/ballerina/http_cache.bal", "kind": "github",
                               "inherited": 0, "status": "not_scraped"})


@pytest.fixture()
def pilot(settings: Settings, db: Database):  # noqa: ANN201
    s = settings.model_copy(update={"prompts": settings.prompts.model_copy(update={"nodes": ["lib.http",
                                                                                              "concept.x"]})})
    _seed(s, db)
    gp.prepare(db)
    return s, db


def test_task_sequence_and_slots() -> None:
    mix = {"write_code": 0.35, "use_api": 0.2, "explain": 0.2, "fix_code": 0.15, "design": 0.1}
    seq = task_sequence(10, mix)
    assert Counter(seq) == {"write_code": 4, "use_api": 2, "explain": 2, "fix_code": 1, "design": 1}
    assert seq[:5] == ["write_code", "use_api", "explain", "fix_code", "design"]  # interleaved
    s = Settings()
    slots = slots_for("core", ["beginner", "advanced"], s.prompts)
    assert len(slots) == 10 and Counter(d for _, d in slots) == {"beginner": 5, "advanced": 5}
    assert len(slots_for("long_tail", [], s.prompts)) == 3 and slots_for("unknown", ["x"], s.prompts) == []


def test_symbols_fts_and_grounding(pilot) -> None:  # noqa: ANN001
    s, db = pilot
    assert {(r["prefix"], r["name"], r["kind"]) for r in db.query("SELECT * FROM symbols")} == {
        ("http", "Client", "Client"), ("http", "ClientConfiguration", "Record"), ("http", "Cache", "Annotation")}
    found = symbols.lookup(db, "Use `http:Client` with `ClientConfiguration`", ["ballerina/http"])
    assert [r["name"] for r in found] == ["Client", "ClientConfiguration"]
    assert "remote isolated function get" in found[0]["text"]
    hits = fts.search(db, "client timeout response", k=5)
    assert hits and "timeout" in hits[0]["text"]
    assert all("Related links" not in h["heading_path"] for h in hits)  # boilerplate chunks are skipped

    plans = {p.ref: p for p in plan(s, db)}
    g = ground_item(db, plans["lib.http#client_timeout"].item, plans["lib.http#client_timeout"].node, s.prompts)
    assert [e.kind for e in g.excerpts][:1] == ["source"] and "http:ClientConfiguration" in g.symbols
    g2 = ground_item(db, plans["lib.http#caching"].item, plans["lib.http#caching"].node, s.prompts)
    assert g2.repo_file_ids and "maxAge" in g2.render()  # GitHub source read from the collected repo


def test_plan_counts_and_unknown_nodes(pilot) -> None:  # noqa: ANN001
    s, db = pilot
    out = summary(plan(s, db), s.prompts)
    assert (out["items"], out["budget"], out["missing"]) == (3, 26, 26)
    assert out["nodes"]["lib.http"]["budget"] == 20 and out["nodes"]["concept.x"]["budget"] == 6
    bad = s.model_copy(update={"prompts": s.prompts.model_copy(update={"nodes": ["lib.nope"]})})
    with pytest.raises(ConfigError, match="lib.nope"):
        plan(bad, db)


class FakeClient:
    """Writer: one prompt per slot (slot 1 refers to its sources, slot 2 pastes eval code, slot 3 repeats
    slot 4). Reviewer: rejects prompts containing 'vague', accepts the rest."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_json(self, model, prompt, schema, **fmt):  # noqa: ANN001, ANN201
        self.calls.append(prompt.id)
        if prompt.id == "prompt_writer":
            slots = [ln for ln in fmt["slots"].splitlines() if ln.strip()]
            out = []
            for i, line in enumerate(slots, 1):
                task = line.split(". ", 1)[1]
                rnd = fmt["existing"].count("\n- ")
                words = " ".join(f"t{abs(hash((fmt['ref'], i, rnd, j))) % 10**8}" for j in range(8))
                text = f"Request {task} {words}"
                if i == 1 and not fmt["existing"]:
                    text = "Based on the excerpts, what does the documentation above say?"
                elif i == 2 and not fmt["existing"]:
                    text = "Why does this fail?\n```ballerina\n" + EVAL_CODE + "```"
                elif i == 3 and not fmt["existing"]:
                    text = "vague question with no detail at all, please answer anything"
                out.append({"slot": i, "prompt": text})
            return schema.model_validate({"prompts": out}), Usage(model, 100, 50, 0.001)
        reviews = []
        for block in fmt["candidates"].split("--- slot ")[1:]:
            slot = int(block.split(" ", 1)[0])
            ok = "vague" not in block
            reviews.append({"slot": slot, "on_item": 4, "answerable": 4, "clear": 4 if ok else 2, "realistic": 4,
                            "task_fit": 4, "verdict": "accept" if ok else "reject", "reason": "" if ok else "vague"})
        return schema.model_validate({"reviews": reviews}), Usage(model, 80, 40, 0.0005)


def test_generate_prompts_filters_reviews_and_fills_gaps(pilot) -> None:  # noqa: ANN001
    s, db = pilot
    s = s.model_copy(update={"answer_gen_model": "deepseek/deepseek-v4.1-flash",
                             "question_gen_model": "qwen/qwen3.8-flash", "limit_generating": 0})
    h = MinHasher()
    db.insert("eval_items", {"id": "bbe/albums", "suite": "bbe", "version": "v1", "name": "albums", "url": "x",
                             "files": "{}", "verify_mode": "none", "status": "active"})
    db.insert("fingerprints", {"item_id": "bbe/albums", "path": "a.bal", "code_hash": code_hash(EVAL_CODE),
                               "minhash": h.to_bytes(h.signature(EVAL_CODE)),
                               "shingles": pack_shingles(shingle_hashes(EVAL_CODE))})
    client = FakeClient()
    with db.run("generate-prompts", s.limits()) as run:
        stats = asyncio.run(gp._generate(s, db, run, client))  # noqa: SLF001
    st = Counter(r["status"] for r in db.query("SELECT status FROM prompts"))
    # 3 items, first round: slot 1 refers to its sources (rejected by the filter), slot 2 pastes eval
    # code, slot 3 is vague: rejected by the reviewer once, then a near-duplicate for the other items
    assert (st["eval_overlap"], st["rejected"], st["duplicate"], st["accepted"]) == (3, 4, 2, 26 - 9)
    assert stats["items_done"] == 3 and client.calls.count("prompt_reviewer") == 3
    row = db.one("SELECT * FROM prompts WHERE status = 'accepted' AND ref = 'lib.http#client_timeout' LIMIT 1")
    assert json.loads(row["covers"]) == [row["ref"]] and json.loads(row["review"])["verdict"] == "accept"
    assert row["writer_model"].startswith("deepseek/") and row["reviewer_model"].startswith("qwen/")
    assert json.loads(row["grounding"])["tokens"] > 0

    # gap-fill: round 2 writes only the 9 open slots, shown the accepted prompts so it writes new ones
    with db.run("generate-prompts", s.limits()) as run:
        again = asyncio.run(gp._generate(s, db, run, client))  # noqa: SLF001
    assert again["written"] == 9 and again["accepted"] == 9
    assert summary(plan(s, db), s.prompts)["missing"] == 0
    assert db.scalar("SELECT MAX(round) FROM prompts") == 2

    files = gp.write_outputs(s, db)
    accepted = (s.data_root / files["accepted"]).read_text().splitlines()
    assert len(accepted) == 26 and json.loads(accepted[0])["covers"]
    cov = json.loads((s.data_root / files["coverage"]).read_text())
    assert cov["missing"] == 0 and cov["accepted"] == 26

    from sftgen.report import write_report

    path, summary_text = write_report(s, db)
    page = path.read_text(encoding="utf-8")
    assert "<h2>Prompts</h2>" in page and "26 accepted prompts" in page and "Accepted by task and difficulty" in page
    assert "Prompts    pilot: 26 accepted / 26 planned (0 open)" in summary_text


def test_reviewer_must_be_another_family(settings: Settings) -> None:
    same = settings.model_copy(update={"answer_gen_model": "deepseek/a", "judge_model": "deepseek/b"})
    with pytest.raises(ConfigError, match="different model family"):
        gp.check_models(same)
    ok = settings.model_copy(update={"answer_gen_model": "deepseek/a", "judge_model": "qwen/b"})
    assert gp.check_models(ok) == ("deepseek/a", "qwen/b")
    assert gp.family("qwen/qwen3.8-flash") == "qwen" and gp.family("gpt-5-mini") == "gpt"
