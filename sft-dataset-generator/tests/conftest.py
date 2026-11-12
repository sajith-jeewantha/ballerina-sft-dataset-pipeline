from __future__ import annotations

from pathlib import Path

import pytest

from balpipe_common.ids import raw_id, sha256_text
from balpipe_common.schemas import FetchInfo, GitInfo, License, RawRecord

FIXTURES = Path(__file__).parent / "fixtures"


def make_raw(body: str, *, source: str = "github", media_type: str = "text/markdown", path: str = "x.md",
             repo: str = "ballerina-platform/ballerina-dev-website", uri: str | None = None) -> RawRecord:
    uri = uri or f"https://github.com/{repo}/blob/abc/{path}"
    return RawRecord(
        raw_id=raw_id(uri, sha256_text(body)), run_id="r", source=source, source_uri=uri,  # type: ignore[arg-type]
        media_type=media_type, body=body, raw_sha256=sha256_text(body),
        fetch=FetchInfo(requested_url=uri, final_url=uri, http_status=200, fetched_at="2026-11-12T00:00:00Z",
                        fetcher="test", fetcher_version="1", user_agent="test"),
        git=GitInfo(repo=repo, ref="master", commit_sha="abc", path=path) if source == "github" else None,
        license=License(spdx="Apache-2.0", basis="repo_license"),
    )


def word_count(text: str) -> int:
    return len(text.split())


@pytest.fixture()
def fixtures() -> Path:
    return FIXTURES
