"""Storage switch: the bucket mirrors LOCAL_DATA_DIR, with database snapshots instead of the live file."""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

import pytest

from balpipe_common.config import ConfigError, Settings
from balpipe_common.storage import Storage, StorageError


def _settings(root: Path, **kw) -> Settings:  # noqa: ANN003
    base = dict(repo_root=root, storage_bucket=True, storage_provider="local", bucket_name=str(root / "bucket"),
                bucket_prefix="team-a/")
    base.update(kw)
    return Settings(**base)  # type: ignore[arg-type]


def _make_outputs(s: Settings) -> None:
    d = s.data_root
    (d / "sft").mkdir(parents=True)
    (d / "reports").mkdir()
    (d / "cache" / "llm").mkdir(parents=True)
    s.export_path.write_text('{"q": 1}\n')
    (s.sft_dir / "train.jsonl").write_text('{"messages": []}\n')
    s.report_path.write_text("<html></html>")
    (d / "cache" / "llm" / "x.json").write_text("{}")  # never uploaded
    (d / "sft" / ".DS_Store").write_text("x")          # never uploaded
    conn = sqlite3.connect(s.db_path)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()
    lock = s.path(s.sources.lock_file)
    lock.parent.mkdir(parents=True)
    lock.write_text('{"swan_lake": "2201.14.1"}')
    graph = s.path(s.graph.path)
    graph.parent.mkdir(parents=True)
    graph.write_text("{}")
    s.path(s.graph.source_file).write_text(json.dumps({"commit": "64c8d80be237d20893934dc9b50055bba8f9a51d"}))


def test_off_by_default_and_needs_a_bucket_name(tmp_path: Path) -> None:
    s = Settings(repo_root=tmp_path)  # type: ignore[call-arg]
    store = Storage(s)
    assert not store.enabled and store.describe().startswith("local only")
    assert store.push(snapshot_db=True).as_dict()["uploaded"] == 0
    with pytest.raises(ConfigError):
        Storage(_settings(tmp_path, bucket_name=None))


def test_bucket_layout_matches_local_folder(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_outputs(s)
    store = Storage(s)
    assert store.root_url == f"file://{tmp_path / 'bucket'}/team-a"
    res = store.push(snapshot_db=True)
    assert sorted(res.uploaded) == [
        "dataset.jsonl", "graph/64c8d80be237/SOURCE.json", "graph/64c8d80be237/graph.json",
        "reports/report.html", "sft/train.jsonl", "sources/sources.lock.json"]
    bucket = tmp_path / "bucket" / "team-a"
    assert (bucket / "sft" / "train.jsonl").read_text() == (s.sft_dir / "train.jsonl").read_text()
    assert not (bucket / "cache").exists() and not (bucket / "pipeline.db").exists()
    assert not (bucket / ".storage").exists()
    # database: gzipped snapshot named by db/LATEST
    assert res.snapshot and (bucket / "db" / "LATEST").read_text().strip() == res.snapshot
    with gzip.open(bucket / res.snapshot) as f:
        assert f.read(16) == b"SQLite format 3\x00"


def test_second_push_uploads_only_changes(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_outputs(s)
    store = Storage(s)
    store.push()
    again = store.push()
    assert again.uploaded == [] and again.skipped == 6
    s.export_path.write_text('{"q": 2}\n')  # same size, different content
    assert store.push().uploaded == ["dataset.jsonl"]
    st = store.status()
    assert st["local_only"] == [] and st["changed"] == [] and st["remote_only"] == []


def test_pull_restores_a_fresh_machine(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    _make_outputs(s)
    Storage(s).push(snapshot_db=True)

    other = tmp_path / "laptop2"
    s2 = _settings(other, bucket_name=str(tmp_path / "bucket"))
    store2 = Storage(s2)
    assert store2.status()["remote_only"]  # nothing local yet
    res = store2.pull()
    assert any(k.startswith("db/") for k in res.downloaded)
    assert (s2.sft_dir / "train.jsonl").is_file() and s2.report_path.is_file()
    assert s2.path(s2.sources.lock_file).is_file()   # missing repo input restored
    assert not s2.path(s2.graph.path).exists()       # graph comes from git, never pulled
    conn = sqlite3.connect(s2.db_path)
    assert conn.execute("SELECT v FROM t").fetchone()[0] == "hello"
    conn.close()

    s2.export_path.write_text("local edit\n")
    assert store2.pull().downloaded == []           # existing files are kept
    store2.pull(overwrite=True)
    assert s2.export_path.read_text() == '{"q": 1}\n'
    assert list(s2.data_root.glob("pipeline.db.bak-*"))  # replaced database was backed up


def test_old_snapshots_are_pruned(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.storage.keep_db_snapshots = 2
    _make_outputs(s)
    store = Storage(s)
    for _ in range(4):
        store.push(snapshot_db=True)
    snaps = store.snapshots()
    assert len(snaps) == 2 and store.latest_snapshot() == snaps[-1]


def test_missing_client_library_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import fsspec

    def boom(*a, **kw):  # noqa: ANN002, ANN003, ANN202
        raise ImportError("no gcsfs")

    monkeypatch.setattr(fsspec, "filesystem", boom)
    store = Storage(_settings(tmp_path, storage_provider="gcs", bucket_name="b"))
    assert store.root_url == "gs://b/team-a"
    with pytest.raises(StorageError, match=r'shared\[gcs\]'):
        store.list()


def test_output_paths_are_under_local_data_dir(tmp_path: Path) -> None:
    s = Settings(repo_root=tmp_path, data_dir="out")  # type: ignore[call-arg]
    assert s.export_path == tmp_path / "out" / "dataset.jsonl"
    assert s.report_path == tmp_path / "out" / "reports" / "report.html"
    s.export.path = "data/old.jsonl"  # older configs: the data/ prefix is dropped
    assert s.export_path == tmp_path / "out" / "old.jsonl"
