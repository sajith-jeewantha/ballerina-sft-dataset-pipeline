"""Graph item sources matched to scraped pages, and the coverage summary."""

from __future__ import annotations

import json
from pathlib import Path

from balpipe_common.config import Settings
from balpipe_common.db import Database
from balpipe_common.graph import load_graph_json

from sftgen.graph.coverage import _section_chunks, coverage_summary, page_key, source_key, sync_loaded

FIXTURE = Path(__file__).resolve().parents[2] / "shared" / "tests" / "fixtures" / "mini_graph.json"


def test_source_keys() -> None:
    assert source_key("https://ballerina.io/spec/lang/2024R1/#section_5.4") == (
        "https://ballerina.io/spec/lang/2024R1/", "section_5.4")
    assert source_key("https://ballerina.io/learn/by-example/open-records")[0] == \
        "https://ballerina.io/learn/by-example/open-records/"
    assert source_key("https://central.ballerina.io/ballerina/http/latest")[0] == "https://central.ballerina.io/ballerina/http"
    assert source_key("https://github.com/ballerina-platform/module-ballerina-http/blob/master/ballerina/README.md")[0] \
        == "gh:ballerina-platform/module-ballerina-http:ballerina/README.md"
    assert source_key("https://github.com/ballerina-platform/ballerina-distribution/blob/master/examples/open-records/"
                      "open_records.bal")[0] == "https://ballerina.io/learn/by-example/open-records/"
    assert source_key("https://github.com/ballerina-platform/openapi-connectors/tree/main/openapi/x")[0] == \
        "gh:ballerina-platform/openapi-connectors:openapi/x/*"
    assert source_key("https://docs.github.com/en/webhooks") == (None, None)
    assert page_key("https://github.com/ballerina-platform/module-ballerina-http/blob/main/ballerina/README.md") == \
        "gh:ballerina-platform/module-ballerina-http:ballerina/README.md"  # branch ignored


def test_spec_section_matching() -> None:
    chunks = [(1, "Spec > 1. Introduction"), (2, "Spec > 5. Values > 5.4 Records"),
              (3, "Spec > 5. Values > 5.4 Records > 5.4.1 Open records"), (4, "Spec > 6. Expressions")]
    assert _section_chunks(chunks, "5.4 Records", "section_5.4") == [2, 3]
    assert _section_chunks(chunks, "5.4 Records with extra words", None) == [2, 3]
    assert _section_chunks(chunks, None, "introduction") == [1]
    assert _section_chunks(chunks, None, "nothing_here") == []


def _page(db: Database, url: str, source: str) -> int:
    return db.insert("pages", {"source": source, "url": url, "status": "chunked", "meta": "{}"})


def test_sync_and_summary(settings: Settings, db: Database) -> None:
    spec = _page(db, "https://ballerina.io/spec/lang/2024R1/", "spec")
    bbe = _page(db, "https://ballerina.io/learn/by-example/open-records/", "bbe")
    for pid, path in [(spec, "Spec > 5. Values > 5.4 Records"), (spec, "Spec > 6. Expressions"), (bbe, "Open records")]:
        db.insert("chunks", {"page_id": pid, "source": "x", "source_url": "u", "ordinal": 0, "heading_path": path,
                             "text": path, "text_hash": path, "token_estimate": 10, "has_code": 0, "status": "questioned"})
    chunk = db.scalar("SELECT id FROM chunks WHERE page_id = ?", (bbe,))
    db.insert("questions", {"chunk_id": chunk, "source": "bbe", "source_url": "u", "question": "q?", "question_hash": "h",
                            "category": "concept", "difficulty": "beginner", "status": "pending"})
    out = sync_loaded(db, load_graph_json(FIXTURE))
    assert out["items"] == 4 and out["grounded"] == 2 and out["not_scraped"] == 1 and out["external_only"] == 1
    status = {r["ref"]: r["status"] for r in db.query("SELECT ref, status FROM graph_items")}
    assert status == {"concept.records#open_records": "grounded",
                      "concept.records#record_spec": "grounded",      # inherits the node's spec section
                      "lib.http#client": "not_scraped",
                      "lib.http#webhooks": "external_only"}
    inherited = db.one("SELECT inherited, chunk_ids FROM item_sources WHERE ref = 'concept.records#record_spec'")
    assert inherited["inherited"] == 1 and len(json.loads(inherited["chunk_ids"])) == 1

    c = coverage_summary(db)
    assert c is not None and c["items"] == 4 and c["grounded"] == 2
    assert c["with_questions"] == 1 and c["by_priority"]["core"]["grounded"] == 2
    assert c["not_scraped_urls"] == {"github": 1}
    assert sync_loaded(db, load_graph_json(FIXTURE))["items"] == 4  # re-sync replaces, never duplicates
    assert db.scalar("SELECT COUNT(*) FROM graph_items") == 4


def test_report_shows_coverage(settings: Settings, db: Database) -> None:
    from sftgen.report import write_report

    path, summary = write_report(settings, db)
    assert "Not synced yet" in path.read_text()
    sync_loaded(db, load_graph_json(FIXTURE))
    path, summary = write_report(settings, db)
    html = path.read_text()
    assert "Knowledge graph coverage" in html and "Core items not grounded" in html
    assert "Graph      grounded 0/4 items" in summary


def test_graph_cli_validate(settings: Settings, tmp_path: Path, monkeypatch, capsys) -> None:  # noqa: ANN001
    from sftgen import cli

    monkeypatch.setattr(cli, "load_settings", lambda cfg=None: settings.model_copy(
        update={"graph": settings.graph.model_copy(update={"path": str(FIXTURE), "source_file": "none.json"})}))
    assert cli.main(["graph", "validate"]) == 0
    assert "OK: 2 nodes, 4 items (4 active), 1 edges, 0 warning(s)" in capsys.readouterr().out
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({**json.loads(FIXTURE.read_text()), "schema_version": "9"}))
    monkeypatch.setattr(cli, "load_settings", lambda cfg=None: settings.model_copy(
        update={"graph": settings.graph.model_copy(update={"path": str(bad), "source_file": "none.json"})}))
    assert cli.main(["graph", "validate"]) == 1
