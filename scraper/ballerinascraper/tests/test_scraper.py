"""Scraping rules: robots.txt, sitemap filtering, Central docs, and the fetch loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from balpipe_common.config import BallerinaIoCfg, Settings
from balpipe_common.db import Database

from ballerinascraper.collectors.ballerina_io import classify, normalize_url, parse_sitemap
from ballerinascraper.collectors.base import Candidate, Fetched
from ballerinascraper.collectors.central import compact_docs, type_str
from ballerinascraper.collectors.github import path_selected
from ballerinascraper.collectors.robots import RobotsRules
from ballerinascraper.scrape import fetch_candidate

UA = "BallerinaCorpusBot/2.1 (+https://example.org)"

# shape of ballerina.io's robots.txt: two `User-agent: *` groups
ROBOTS = """User-Agent: *
Allow: /

User-agent: Twitterbot
Allow: *

User-agent: *
Disallow: /1.2/
Disallow: /swan-lake/learn/by-example/
Disallow: /learn/api-docs/
Disallow: /date?id=123
"""


def test_robots_merges_repeated_star_groups() -> None:
    r = RobotsRules.parse(ROBOTS, UA)
    assert r.allowed("/learn/by-example/hello-world/")
    assert not r.allowed("/learn/api-docs/ballerina/")
    assert not r.allowed("/1.2/learn/")
    assert not r.allowed("/date?id=123")


def test_robots_specific_agent_and_wildcards() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: BallerinaCorpusBot\nDisallow: /private/*.html$\nAllow: /\n"
    r = RobotsRules.parse(text, UA)
    assert r.allowed("/learn/") and not r.allowed("/private/x.html") and r.allowed("/private/x.html?y")
    assert RobotsRules.parse("", UA).allowed("/anything")


SITEMAP = """<urlset>
<url><loc>https://ballerina.io/learn/by-example/hello-world/</loc></url>
<url><loc>https://ballerina.io/learn/by-example/hello-world</loc></url>
<url><loc>https://ballerina.io/learn/by-example/</loc></url>
<url><loc>https://ballerina.io/learn/</loc></url>
<url><loc>https://ballerina.io/learn/style-guide/coding-conventions/</loc></url>
<url><loc>https://ballerina.io/spec/lang/2024R1/</loc></url>
<url><loc>https://ballerina.io/spec/lang/draft/</loc></url>
<url><loc>https://ballerina.io/spec/http/</loc></url>
<url><loc>https://ballerina.io/1.2/learn/by-example/hello/</loc></url>
<url><loc>https://ballerina.io/learn/api-docs/ballerina/</loc></url>
<url><loc>https://ballerina.io/downloads/swan-lake-release-notes/2201.1.0/</loc></url>
<url><loc>https://blog.ballerina.io/posts/x/</loc></url>
</urlset>"""


def test_sitemap_keeps_only_allowed_sections_once() -> None:
    urls = parse_sitemap(SITEMAP, BallerinaIoCfg())
    assert urls == {
        "bbe": ["https://ballerina.io/learn/by-example/hello-world/"],
        "spec": ["https://ballerina.io/spec/lang/2024R1/"],
        "learn": ["https://ballerina.io/learn/style-guide/coding-conventions/"],
    }
    assert classify("https://blog.ballerina.io/posts/x/", BallerinaIoCfg()) is None
    assert normalize_url("http://Ballerina.io/learn/x?y=1#z") == "https://ballerina.io/learn/x/"


def test_module_file_selection() -> None:
    inc = ["README.md", "ballerina/Module.md", "ballerina/*.bal"]
    exc = ["ballerina/tests/**", "**/test*.bal"]
    assert path_selected("ballerina/client.bal", inc, exc)
    assert path_selected("README.md", inc, exc)
    assert not path_selected("ballerina/tests/client_test.bal", inc, exc)
    assert not path_selected("ballerina/test_utils.bal", inc, exc)
    assert not path_selected("native/src/Foo.java", inc, exc)


def test_central_type_rendering_and_compaction() -> None:
    union = {"isAnonymousUnionType": True, "memberTypes": [{"name": "string"}, {"name": "Error", "category": "errors"}]}
    assert type_str(union) == "string|Error"
    assert type_str({"name": "byte", "arrayDimensions": 1}) == "byte[]"
    assert type_str({"name": "int", "isNullable": True}) == "int?"
    docs = {"docsData": {"modules": [{"id": "io", "description": "I/O", "functions": [{
        "name": "println", "isIsolated": True, "description": "Prints.", "parameters": [
            {"name": "values", "type": {"name": "Printable", "isRestParam": True}, "description": "Values"}],
        "returnParameters": []}], "classes": [{"name": "Old", "isDeprecated": True}]}]}}
    mod = compact_docs(docs)[0]
    assert mod["functions"][0]["parameters"][0]["type"] == "Printable..."
    assert mod["functions"][0]["qualifiers"] == ["isolated"] and "classes" not in mod


# ------------------------------------------------------------------ fetch loop
@pytest.fixture()
def db(tmp_path: Path):
    with Database(tmp_path / "p.db") as d:
        yield d


def _cand(url: str, body: str | None = "<html>v1</html>", version: str | None = None, fail: bool = False) -> Candidate:
    def fetch(row):  # noqa: ANN001, ANN202
        if fail:
            raise ConnectionError("network down")
        return Fetched(body, 200 if body is not None else 304, url, etag='"e1"')
    return Candidate("learn", url, fetch=fetch, version_key=version)


def test_fetch_cached_unchanged_updated_and_failed(db: Database) -> None:
    with db.run("scrape", {}) as run:
        url = "https://ballerina.io/learn/a/"
        assert fetch_candidate(db, _cand(url, version="v1"), run, refresh=False) == "downloaded"
        assert fetch_candidate(db, _cand(url, version="v1"), run, refresh=False) == "cached"      # no request
        assert fetch_candidate(db, _cand(url, version="v1"), run, refresh=True) == "unchanged"    # same version key
        assert fetch_candidate(db, _cand(url), run, refresh=True) == "not_modified"               # same content hash
        assert fetch_candidate(db, _cand(url, body=None), run, refresh=True) == "not_modified"    # HTTP 304
        assert fetch_candidate(db, _cand(url, body="<html>v2</html>"), run, refresh=True) == "updated"
        assert db.scalar("SELECT status FROM pages WHERE url = ?", (url,)) == "fetched"

        bad = "https://ballerina.io/learn/b/"
        assert fetch_candidate(db, _cand(bad, fail=True), run, refresh=False) == "failed"
        row = db.one("SELECT status, error FROM pages WHERE url = ?", (bad,))
        assert row["status"] == "failed" and "network down" in row["error"]
        assert fetch_candidate(db, _cand(bad), run, refresh=False) == "updated"  # failures are retried next run


def test_scrape_limit_is_per_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ballerinascraper import scrape

    class Fake:
        def __init__(self, source: str) -> None:
            self.source = source

        def candidates(self):  # noqa: ANN202
            for i in range(10):
                yield Candidate(self.source, f"https://x/{self.source}/{i}/",
                                fetch=lambda row: Fetched("body", 200, "u"))

    monkeypatch.setattr(scrape, "build_collectors", lambda s: {k: Fake(k) for k in scrape.SOURCES})
    s = Settings(repo_root=tmp_path, limit_scrape_pages=3)  # type: ignore[call-arg]
    with Database(s.db_path) as db, db.run("scrape", s.limits()) as run:
        stats = scrape.scrape_sources(s, db, run)
        assert all(v["downloaded"] == 3 for v in stats.values())
        assert db.scalar("SELECT COUNT(*) FROM pages") == 15
        again = scrape.scrape_sources(s, db, run)  # next run: cached pages are skipped, 3 new ones per source
        assert all(v["cached"] == 3 and v["downloaded"] == 3 for v in again.values())
