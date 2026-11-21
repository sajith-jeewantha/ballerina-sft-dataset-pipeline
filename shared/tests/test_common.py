"""Config (limits from .env), the SQLite store and hashing."""

from __future__ import annotations

from pathlib import Path

import pytest

from balpipe_common.config import ConfigError, load_settings
from balpipe_common.db import Database
from balpipe_common.ids import content_hash, short_hash


def _repo(tmp_path: Path, env: str = "", yaml: str = "data_dir: data\n") -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "pipeline.yaml").write_text(yaml, encoding="utf-8")
    (tmp_path / ".env").write_text(env, encoding="utf-8")
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("LIMIT_GENERATING", "LIMIT_SCRAPE_PAGES", "QUESTIONS_PER_CHUNK", "REQUEST_DELAY_SECONDS", "GITHUB_TOKEN",
              "OPENROUTER_API_KEY", "API_KEY", "QUESTION_GEN_MODEL", "ANSWER_GEN_MODEL"):
        monkeypatch.delenv(k, raising=False)


def test_limits_have_defaults(tmp_path: Path) -> None:
    s = load_settings(root=_repo(tmp_path))
    assert s.limits() == {"LIMIT_SCRAPE_PAGES": 20, "LIMIT_GENERATING": 10, "QUESTIONS_PER_CHUNK": 3,
                          "REQUEST_DELAY_SECONDS": 1.0, "LIMIT_REPOS": 20}


def test_limits_from_env_file_with_inline_comments(tmp_path: Path) -> None:
    env = "LIMIT_GENERATING=0      # no limit\nLIMIT_SCRAPE_PAGES=5\nQUESTIONS_PER_CHUNK=2\nREQUEST_DELAY_SECONDS=0.5\n"
    s = load_settings(root=_repo(tmp_path, env))
    assert (s.limit_generating, s.limit_scrape_pages, s.questions_per_chunk, s.request_delay_seconds) == (0, 5, 2, 0.5)


def test_process_env_beats_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIMIT_GENERATING", "3")
    assert load_settings(root=_repo(tmp_path, "LIMIT_GENERATING=10\n")).limit_generating == 3


def test_empty_values_do_not_hide_legacy_env_files(tmp_path: Path) -> None:
    """Regression: `OPENROUTER_API_KEY=` in .env used to hide the key in sft-dataset-generator/.env."""
    root = _repo(tmp_path, "OPENROUTER_API_KEY=\nGITHUB_TOKEN=\n")
    (root / "sft-dataset-generator").mkdir()
    (root / "sft-dataset-generator" / ".env").write_text("API_KEY=sk-legacy\nGITHUB_TOKEN=ghp_legacy\n")
    s = load_settings(root=root)
    assert s.llm_api_key is not None and s.llm_api_key.get_secret_value() == "sk-legacy"
    assert s.github_token is not None and s.github_token.get_secret_value() == "ghp_legacy"


def test_negative_limit_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_settings(root=_repo(tmp_path, "LIMIT_GENERATING=-1\n"))


def test_require_names_env_vars(tmp_path: Path) -> None:
    s = load_settings(root=_repo(tmp_path))
    with pytest.raises(ConfigError, match="OPENROUTER_API_KEY"):
        s.require("llm_api_key", "question_gen_model")


def test_secrets_not_in_repr(tmp_path: Path) -> None:
    s = load_settings(root=_repo(tmp_path, "OPENROUTER_API_KEY=sk-secret-123\n"))
    assert "sk-secret-123" not in repr(s) and "sk-secret-123" not in str(s.model_dump())


def test_db_insert_update_and_timestamps(tmp_path: Path) -> None:
    with Database(tmp_path / "p.db") as db:
        pid = db.insert("pages", {"source": "learn", "url": "https://x/", "status": "fetched"})
        row = db.one("SELECT * FROM pages WHERE id = ?", (pid,))
        assert row["status"] == "fetched" and row["created_at"] == row["updated_at"]
        db.update("pages", pid, {"status": "chunked"})
        assert db.scalar("SELECT status FROM pages WHERE id = ?", (pid,)) == "chunked"


def test_run_records_success_and_failure(tmp_path: Path) -> None:
    with Database(tmp_path / "p.db") as db:
        with db.run("scrape", {"LIMIT_SCRAPE_PAGES": 2}) as run:
            run.count("pages_downloaded", 2)
        with pytest.raises(RuntimeError), db.run("generate-answers", {}) as run:
            raise RuntimeError("boom")
        ok, bad = db.query("SELECT * FROM runs ORDER BY id")
        assert ok["status"] == "succeeded" and '"pages_downloaded": 2' in ok["stats"] and ok["duration_s"] >= 0
        assert '"LIMIT_SCRAPE_PAGES": 2' in ok["limits"]
        assert bad["status"] == "failed" and "boom" in bad["error"]


def test_hashes() -> None:
    assert content_hash("a  b\n c") == content_hash("a b c")
    assert short_hash("ab", "c") != short_hash("a", "bc")
