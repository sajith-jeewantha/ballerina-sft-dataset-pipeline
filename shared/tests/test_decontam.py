"""Decontamination: eval fingerprints flag chunks, questions and repo files that copy eval code."""

from __future__ import annotations

from pathlib import Path

from balpipe_common.config import EvalCfg
from balpipe_common.db import Database
from balpipe_common.decontam import Decontaminator, Fingerprint, apply_decontam, pack_shingles
from balpipe_common.dedup import MinHasher, code_hash, shingle_hashes

EVAL_CODE = """import ballerina/io;

type Order record {|
    string id;
    decimal total;
    string[] items;
|};

public function main() {
    Order[] orders = [{id: "o-17", total: 42.5, items: ["pen", "ink"]}, {id: "o-18", total: 7.25, items: []}];
    decimal sum = from Order o in orders where o.items.length() > 0 select o.total;
    io:println("Total of non-empty orders: ", sum);
}
"""
OTHER_CODE = """import ballerina/http;

service /pets on new http:Listener(8080) {
    resource function get [int petId]() returns json {
        return {petId, name: "rex", kind: "dog", vaccinated: true, owner: "sam"};
    }
}
"""
BOILERPLATE = "import ballerina/io;\n\npublic function main() {\n    io:println(\"hi\");\n}\n"


def fp(item: str, code: str, common: set[int] | None = None) -> Fingerprint:
    h = MinHasher()
    return Fingerprint(item, "main.bal", code_hash(code), h.signature(code), shingle_hashes(code) - (common or set()))


def test_check_exact_contained_and_near() -> None:
    dc = Decontaminator([fp("bbe/orders", EVAL_CODE)], EvalCfg())
    # comments and layout don't hide a copy
    assert dc.check("// copied\n" + EVAL_CODE.replace("    ", "\t")) == "bbe/orders"
    # the code inside a Markdown chunk, with prose around it
    chunk = "## Querying orders\n\nThis sums the totals.\n\n```ballerina\n" + EVAL_CODE + "```\n\nThat's all."
    assert dc.check(chunk) == "bbe/orders"
    # most of the code pasted into a bigger file
    assert dc.check(OTHER_CODE + "\n" + EVAL_CODE.replace("o-17", "o-99")) == "bbe/orders"
    # unrelated code, boilerplate and prose don't match
    assert dc.check(OTHER_CODE) is None
    assert dc.check(BOILERPLATE) is None
    assert dc.check("Ballerina is a programming language for integration.") is None
    assert dc.check("") is None


def test_small_items_match_exactly_only() -> None:
    common = shingle_hashes(BOILERPLATE)
    dc = Decontaminator([fp("bbe/hello", BOILERPLATE, common)], EvalCfg())
    assert dc.check(BOILERPLATE) == "bbe/hello"
    assert dc.check(BOILERPLATE + "\nfunction other() returns int { return 1; }\n") is None
    assert not Decontaminator([], EvalCfg()) and Decontaminator([], EvalCfg()).key == ""


def _seed(db: Database) -> dict[str, int]:
    h = MinHasher()
    db.insert("eval_items", {"id": "bbe/orders", "suite": "bbe", "version": "v1", "name": "orders",
                             "url": "https://ballerina.io/learn/by-example/orders/", "files": "{}",
                             "verify_mode": "none", "status": "active"})
    db.insert("fingerprints", {"item_id": "bbe/orders", "path": "orders.bal", "code_hash": code_hash(EVAL_CODE),
                               "minhash": h.to_bytes(h.signature(EVAL_CODE)),
                               "shingles": pack_shingles(shingle_hashes(EVAL_CODE))})
    ids = {}
    for name, url, text in (
        ("eval_page", "https://ballerina.io/learn/by-example/orders/", "Some explanation of queries."),
        ("copy", "https://ballerina.io/learn/query/", "```ballerina\n" + EVAL_CODE + "```"),
        ("clean", "https://ballerina.io/learn/services/", "```ballerina\n" + OTHER_CODE + "```"),
    ):
        page = db.insert("pages", {"source": "learn", "url": url, "status": "chunked", "meta": "{}"})
        ids[name] = db.insert("chunks", {"page_id": page, "source": "learn", "source_url": url, "ordinal": 0,
                                         "heading_path": "", "text": text, "text_hash": name, "token_estimate": 50,
                                         "has_code": 1, "status": "pending"})
    for name in ("clean", "clean2"):
        ids[f"q_{name}"] = db.insert("questions", {
            "chunk_id": ids["clean"], "source": "learn", "source_url": "u", "question": f"How? {name}",
            "question_hash": name, "category": "code", "difficulty": "basic", "status": "answered"})
    db.insert("answers", {"question_id": ids["q_clean"], "answer": "```ballerina\n" + OTHER_CODE + "```",
                          "status": "valid"})
    repo = db.insert("repos", {"full_name": "o/r", "grp": "external", "priority": 3, "url": "u", "status": "collected"})
    for path, code in (("a.bal", EVAL_CODE), ("b.bal", OTHER_CODE)):
        ids[path] = db.insert("repo_files", {"repo_id": repo, "path": path, "kind": "bal", "bytes": len(code),
                                             "text": code, "minhash": h.to_bytes(h.signature(code)),
                                             "code_hash": code_hash(code), "status": "kept"})
    return ids


def test_apply_decontam_flags_rows_and_rechecks_new_answers(tmp_path: Path) -> None:
    with Database(tmp_path / "p.db") as db:
        ids = _seed(db)
        stats = apply_decontam(db, EvalCfg())
        assert stats["chunks_overlap"] == 2 and stats["repo_files_overlap"] == 1 and stats["questions_overlap"] == 0
        chunk = lambda i: db.one("SELECT status, eval_overlap FROM chunks WHERE id = ?", (i,))  # noqa: E731
        assert tuple(chunk(ids["eval_page"])) == ("eval_holdout", "bbe/orders")   # the eval page itself
        assert tuple(chunk(ids["copy"])) == ("eval_holdout", "bbe/orders")        # a copy on another page
        assert tuple(chunk(ids["clean"])) == ("pending", None)
        assert db.scalar("SELECT eval_overlap FROM repo_files WHERE id = ?", (ids["a.bal"],)) == "bbe/orders"

        # nothing to re-check until something changes
        again = apply_decontam(db, EvalCfg())
        assert (again["chunks_checked"], again["questions_checked"], again["repo_files_checked"]) == (0, 0, 0)
        # a new answer that pastes the eval code: its question is re-checked and flagged
        db.insert("answers", {"question_id": ids["q_clean2"], "answer": EVAL_CODE, "status": "not_checked"})
        third = apply_decontam(db, EvalCfg())
        assert third["questions_checked"] == 1 and third["questions_overlap"] == 1

        # the eval set goes away: flags are cleared and held-out chunks are pending again
        db.execute("UPDATE eval_items SET status = 'retired'")
        apply_decontam(db, EvalCfg())
        assert tuple(chunk(ids["copy"])) == ("pending", None)
        assert db.scalar("SELECT COUNT(*) FROM questions WHERE eval_overlap IS NOT NULL") == 0
