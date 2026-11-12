from __future__ import annotations

import json
from pathlib import Path

import pytest

from balpipe_common import ids
from balpipe_common.config import ConfigError, load_settings, model_family
from balpipe_common.jsonl_io import JsonlAppender, PartWriter, existing_ids, read_jsonl, write_jsonl_atomic
from balpipe_common.manifest import StageRun, find_resumable, read_manifest
from balpipe_common.schemas import RawRecord


# --------------------------------------------------------------------------- ids
def test_ids_are_deterministic_and_prefixed() -> None:
    assert ids.doc_id("https://x") == ids.doc_id("https://x")
    assert ids.doc_id("https://x").startswith("doc_") and len(ids.doc_id("a")) == 20
    assert ids.short_hash("ab", "c") != ids.short_hash("a", "bc")
    cid = ids.chunk_id("doc_0123456789abcdef", "chunk text", 7)
    assert cid == f"chk_0123456789abcdef_{ids.sha256_text('chunk text')[:8]}_007"
    assert ids.chunk_id("doc_0123456789abcdef", "changed", 7) != cid


def test_sft_id_ignores_whitespace_differences() -> None:
    assert ids.sft_id("write_code", "ex_1", "Write  a\nfunction") == ids.sft_id("write_code", "ex_1", "Write a function")


def test_split_is_stable_and_roughly_proportional() -> None:
    docs = [ids.doc_id(f"u{i}") for i in range(4000)]
    splits = [ids.assign_split(d, 0.1) for d in docs]
    assert splits == [ids.assign_split(d, 0.1) for d in docs]
    assert 0.07 < splits.count("eval") / len(splits) < 0.13
    assert all(ids.assign_split(d, 0.0) == "train" for d in docs[:50])
    with pytest.raises(ValueError):
        ids.assign_split(docs[0], 1.0)


# ------------------------------------------------------------------------ config
@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipeline.yaml").write_text(
        "data_dir: mydata\nchunk:\n  max_tokens: 111\nsplit:\n  eval_fraction: 0.2\n", encoding="utf-8"
    )
    for var in ("API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY", "QUESTION_GEN_MODEL", "BALPIPE__CHUNK__MAX_TOKENS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("BALPIPE_ROOT", raising=False)
    return tmp_path


def test_yaml_values_and_defaults(repo: Path) -> None:
    s = load_settings(root=repo)
    assert s.chunk.max_tokens == 111
    assert s.split.eval_fraction == 0.2
    assert s.data_path("raw") == repo / "mydata" / "raw"
    assert s.generate.top_k == 4  # default


def test_env_overrides_yaml_and_legacy_key_alias(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / ".env").write_text("API_KEY=sk-legacy\nQUESTION_GEN_MODEL=qwen/a\n", encoding="utf-8")
    monkeypatch.setenv("BALPIPE__CHUNK__MAX_TOKENS", "222")
    s = load_settings(root=repo)
    assert s.chunk.max_tokens == 222
    assert s.llm_api_key is not None and s.llm_api_key.get_secret_value() == "sk-legacy"
    assert "sk-legacy" not in json.dumps(s.redacted_snapshot())


def test_require_reports_missing(repo: Path) -> None:
    s = load_settings(root=repo)
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        s.require_llm()


def test_bad_task_weights_rejected(repo: Path) -> None:
    (repo / "config" / "pipeline.yaml").write_text("generate:\n  tasks: {write_code: 0.5}\n", encoding="utf-8")
    with pytest.raises(Exception, match="sum to 1.0"):
        load_settings(root=repo)


def test_model_family() -> None:
    assert model_family("qwen/qwen3.8-flash") == "qwen"
    assert model_family("anthropic/claude-x") == "anthropic"


# ---------------------------------------------------------------------- jsonl io
def test_atomic_write_and_read(tmp_path: Path) -> None:
    p = tmp_path / "a" / "x.jsonl"
    assert write_jsonl_atomic(p, [{"id": 1}, {"id": 2}]) == 2
    assert [r["id"] for r in read_jsonl(p)] == [1, 2]
    assert not (tmp_path / "a" / "x.jsonl.tmp").exists()


def test_appender_repairs_torn_tail(tmp_path: Path) -> None:
    p = tmp_path / "c.jsonl"
    p.write_text('{"id": "a"}\n{"id": "b', encoding="utf-8")  # crash mid-line
    with JsonlAppender(p, fsync=False) as w:
        w.write({"id": "c"})
    assert [r["id"] for r in read_jsonl(p)] == ["a", "c"]
    assert existing_ids(p, "id") == {"a", "c"}


def test_part_writer_rotates_gz(tmp_path: Path) -> None:
    with PartWriter(tmp_path, part_size=2) as w:
        for i in range(5):
            w.write({"i": i})
    assert [p.name for p in w.paths] == ["part-00000.jsonl.gz", "part-00001.jsonl.gz", "part-00002.jsonl.gz"]
    assert [r["i"] for p in w.paths for r in read_jsonl(p)] == [0, 1, 2, 3, 4]


def test_schema_rejects_unknown_fields() -> None:
    with pytest.raises(Exception):
        RawRecord.model_validate({"raw_id": "x", "typo_field": 1})


# ---------------------------------------------------------------------- manifest
def test_manifest_lifecycle_and_resume(repo: Path) -> None:
    s = load_settings(root=repo)
    inp = repo / "in.jsonl"
    inp.write_text('{"a":1}\n', encoding="utf-8")
    run_dir = repo / "runs" / "r1"
    with pytest.raises(RuntimeError):
        with StageRun(s, "generate", run_dir, inputs=[inp]) as run:
            run.count("written", 3)
            raise RuntimeError("boom")
    m = read_manifest(run_dir / "_manifest.json")
    assert m and m.status == "failed" and m.counts["written"] == 3
    assert find_resumable(repo / "runs", "generate", [inp]) == run_dir

    with StageRun(s, "generate", run_dir, inputs=[inp], resumable=True) as run2:
        assert run2.resumed and run2.run_id == m.run_id
        run2.count("written", 2)
    m2 = read_manifest(run_dir / "_manifest.json")
    assert m2 and m2.status == "completed" and m2.counts["written"] == 5
    assert find_resumable(repo / "runs", "generate", [inp]) is None

    inp.write_text('{"a":2}\n', encoding="utf-8")  # changed input -> no resume
    with pytest.raises(RuntimeError):
        with StageRun(s, "generate", repo / "runs" / "r2", inputs=[repo / "in.jsonl"]):
            raise RuntimeError("x")
    inp.write_text('{"a":3}\n', encoding="utf-8")
    assert find_resumable(repo / "runs", "generate", [inp]) is None
