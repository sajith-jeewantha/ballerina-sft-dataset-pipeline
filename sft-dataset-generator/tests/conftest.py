from __future__ import annotations

from pathlib import Path

import pytest

from balpipe_common.config import Settings
from balpipe_common.db import Database

FIXTURES = Path(__file__).parent / "fixtures"


def word_count(text: str) -> int:
    return len(text.split())


@pytest.fixture()
def fixtures() -> Path:
    return FIXTURES


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(repo_root=tmp_path, question_gen_model="test/q-model", answer_gen_model="other/a-model",
                    llm_api_key="test-key")  # type: ignore[arg-type]


@pytest.fixture()
def db(settings: Settings):
    with Database(settings.db_path) as d:
        yield d
