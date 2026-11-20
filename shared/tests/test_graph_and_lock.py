"""Knowledge graph loading/validation, the sources lockfile and provider routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from balpipe_common.config import GenerateCfg
from balpipe_common.graph import GraphError, graph_stats, load_graph_json
from balpipe_common.ids import sha256_text
from balpipe_common.lock import SourceLock, compatible, version_tuple

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"
REAL_GRAPH = Path(__file__).resolve().parents[2] / "graph" / "graph.json"


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def test_mini_graph_loads_with_refs_and_stats() -> None:
    lg = load_graph_json(FIXTURE)
    st = graph_stats(lg)
    assert (st["nodes"], st["items"], st["edges"], st["warnings"]) == (2, 4, 1, 0)
    refs = [lg.graph.ref(n, i) for n, i in lg.graph.iter_items()]
    assert "concept.records#open_records" in refs and "lib.http#client" in refs
    assert st["items_without_own_sources"] == 1


def test_schema_and_rule_violations_are_reported(tmp_path: Path) -> None:
    data = json.loads(FIXTURE.read_text())
    data["nodes"][1]["modules"] = []           # a library node needs at least one module
    data["nodes"][0]["edges"][0]["to"] = "lib.missing"
    data["edges"][0]["target"] = "lib.missing"
    with pytest.raises(GraphError) as exc:
        load_graph_json(_write(tmp_path, data))
    text = " ".join(exc.value.errors)
    assert "lib.http" in text and "lib.missing" in text


def test_wrong_schema_version_and_hash_mismatch(tmp_path: Path) -> None:
    data = json.loads(FIXTURE.read_text())
    data["schema_version"] = "0.1"
    p = _write(tmp_path, data)
    src = tmp_path / "SOURCE.json"
    src.write_text(json.dumps({"commit": "abc", "sha256": "0" * 64}))
    with pytest.raises(GraphError) as exc:
        load_graph_json(p, src)
    assert any("schema_version" in e for e in exc.value.errors)
    assert any("sha256" in e for e in exc.value.errors)


def test_graph_version_prefers_upstream_commit(tmp_path: Path) -> None:
    text = FIXTURE.read_text()
    src = tmp_path / "SOURCE.json"
    src.write_text(json.dumps({"commit": "64c8d80be237d20893934dc9b50055bba8f9a51d", "sha256": sha256_text(text)}))
    lg = load_graph_json(FIXTURE, src)
    assert lg.version == "64c8d80be237"


@pytest.mark.skipif(not REAL_GRAPH.is_file(), reason="graph/graph.json not present")
def test_real_graph_is_valid() -> None:
    lg = load_graph_json(REAL_GRAPH, REAL_GRAPH.parent / "SOURCE.json")
    st = graph_stats(lg)
    assert (st["nodes"], st["items_total"], st["edges"]) == (242, 3257, 1704)


# ------------------------------------------------------------------ lock
def test_version_compare() -> None:
    assert version_tuple("2201.14.1") == (2201, 14, 1)
    assert compatible("2201.12.0", "2201.14.1") and compatible("2201.14.1", "2201.14.1")
    assert not compatible("2201.15.0", "2201.14.1") and not compatible(None, "2201.14.1")


def test_lock_roundtrip_and_new_pin_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "sources.lock.json"
    lock = SourceLock(path, "2201.14.1")
    lock.set("repos", "o/r", {"ref": "main", "commit": "abc"})
    lock.save()
    again = SourceLock(path, "2201.14.1")
    assert again.get("repos", "o/r")["commit"] == "abc"
    again.set("repos", "o/r", {"ref": "main", "commit": "abc"})  # same value: no rewrite
    assert not again.dirty
    newer = SourceLock(path, "2201.15.0")
    assert newer.get("repos", "o/r") is None and newer.data["previous_swan_lake"] == "2201.14.1"


def test_provider_routing_by_prefix() -> None:
    g = GenerateCfg()
    assert g.provider_for("deepseek/deepseek-v4.1-flash-1017") == {
        "order": ["deepseek"], "allow_fallbacks": False, "data_collection": "deny"}
    assert g.provider_for("qwen/qwen3.8-flash") is None
