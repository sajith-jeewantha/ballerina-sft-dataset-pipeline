"""The fetch half of `scrape`: walk every source's candidates and store raw pages.

Per source, at most LIMIT_SCRAPE_PAGES pages are downloaded per run (0 = no
limit). Pages already in the database are not downloaded again unless their
pinned version changed (version key: blob SHAs / package version, known without
a request) or `refresh=True`, which also re-checks pages without a version key
(HTTP 304 or an identical content hash means unchanged). Pins come from
`config/sources.lock.json` (see `balpipe_common.lock`). A failure is logged and
recorded on the page row; it never stops the run.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.ids import sha256_text, utc_now_iso
from balpipe_common.lock import SourceLock

from ballerinascraper.collectors.ballerina_io import Sitemap, WebPageCollector
from ballerinascraper.collectors.base import Candidate, Collector
from ballerinascraper.collectors.central import CentralCollector
from ballerinascraper.collectors.github import BbeCollector, GithubApi, ModuleCollector
from ballerinascraper.collectors.http import BlockedByRobots, PoliteSession

log = logging.getLogger(__name__)

SOURCES = ("bbe", "spec", "learn", "central", "github")
# outcomes that cost a download and count towards LIMIT_SCRAPE_PAGES
DOWNLOADS = ("downloaded", "updated", "not_modified", "failed")


def open_lock(s: Settings) -> SourceLock:
    return SourceLock(s.path(s.sources.lock_file), s.sources.swan_lake)


def build_collectors(s: Settings, lock: SourceLock, *, update_lock: bool = False) -> dict[str, Collector]:
    c = s.crawl
    common: dict[str, Any] = dict(
        delay_s=s.request_delay_seconds, timeout_s=c.timeout_s, retries=c.retries,
        respect_robots=c.respect_robots, robots_exempt_hosts=tuple(c.robots_exempt_hosts),
        cache_dir=s.cache_dir / "http",
    )
    web = PoliteSession(c.user_agent, **common)
    gh_headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if s.github_token:
        gh_headers["Authorization"] = f"Bearer {s.github_token.get_secret_value()}"
    else:
        log.warning("GITHUB_TOKEN is not set: the GitHub API allows only 60 requests per hour")
    github = GithubApi(PoliteSession(c.user_agent, headers=gh_headers, **common), c.listing_cache_hours)
    central = PoliteSession(c.user_agent, headers={"Accept": "application/json"}, **common)
    sitemap = Sitemap(web, s.sources.ballerina_io, c.listing_cache_hours)
    return {
        "bbe": BbeCollector(github, s.sources.github, sitemap, web, lock, update_lock=update_lock),
        "spec": WebPageCollector("spec", sitemap, web),
        "learn": WebPageCollector("learn", sitemap, web),
        "central": CentralCollector(central, s.sources.central, c.listing_cache_hours, lock, update_lock=update_lock),
        "github": ModuleCollector(github, s.sources.github, lock, update_lock=update_lock),
    }


def _save_failure(db: Database, cand: Candidate, status: str, error: str, run: RunTracker) -> None:
    row = db.one("SELECT id FROM pages WHERE url = ?", (cand.url,))
    values = {"status": status, "error": error[:2000], "last_run_id": run.id, "fetched_at": utc_now_iso()}
    if row:
        db.update("pages", row["id"], values)
    else:
        db.insert("pages", {"source": cand.source, "url": cand.url, "title": cand.title, "license": cand.license,
                            "meta": json.dumps(cand.meta), **values})


def fetch_candidate(db: Database, cand: Candidate, run: RunTracker, *, refresh: bool) -> str:
    """Fetch one candidate if needed. Returns what happened:
    cached | unchanged (no request) | not_modified | downloaded | updated | failed | blocked."""
    row = db.one("SELECT * FROM pages WHERE url = ?", (cand.url,))
    ok_before = row is not None and row["status"] not in ("failed", "blocked") and row["raw_body"] is not None
    pin_changed = ok_before and bool(cand.version_key) and row["version_key"] != cand.version_key
    if ok_before and not refresh and not pin_changed:
        return "cached"
    if ok_before and cand.version_key and row["version_key"] == cand.version_key:
        return "unchanged"
    try:
        got = cand.fetch(row)
    except BlockedByRobots as exc:
        _save_failure(db, cand, "blocked", str(exc), run)
        return "blocked"
    except Exception as exc:  # logged and recorded; the run goes on
        log.warning("fetch failed: %s: %s", cand.url, exc)
        _save_failure(db, cand, "failed", f"{type(exc).__name__}: {exc}", run)
        return "failed"

    now = utc_now_iso()
    if got.body is None or (ok_before and row["raw_hash"] == sha256_text(got.body)):
        db.update("pages", row["id"], {"fetched_at": now, "version_key": cand.version_key, "last_run_id": run.id,
                                       "etag": got.etag or row["etag"], "fetch_count": row["fetch_count"] + 1})
        return "not_modified"
    values = {
        "source": cand.source, "fetch_url": got.fetch_url, "title": got.title or cand.title, "status": "fetched",
        "error": None, "http_status": got.http_status, "version_key": cand.version_key, "etag": got.etag,
        "last_modified": got.last_modified, "raw_body": got.body, "raw_hash": sha256_text(got.body),
        "license": cand.license, "meta": json.dumps({**cand.meta, **got.meta}), "last_run_id": run.id,
        "fetched_at": now,
    }
    if row:
        db.update("pages", row["id"], {**values, "fetch_count": row["fetch_count"] + 1})
        return "updated"
    db.insert("pages", {"url": cand.url, "fetch_count": 1, **values})
    return "downloaded"


def scrape_sources(s: Settings, db: Database, run: RunTracker, *, refresh: bool = False,
                   only: list[str] | None = None, update_lock: bool = False) -> dict[str, dict[str, int]]:
    lock = open_lock(s)
    try:
        return _scrape(s, db, run, lock, refresh=refresh, only=only, update_lock=update_lock)
    finally:
        lock.save()  # keep pins resolved so far, even after a failure or interrupt
        run.stats["lock"] = lock.counts()  # type: ignore[assignment]


def _scrape(s: Settings, db: Database, run: RunTracker, lock: SourceLock, *, refresh: bool,
            only: list[str] | None, update_lock: bool) -> dict[str, dict[str, int]]:
    collectors = build_collectors(s, lock, update_lock=update_lock)
    log.info("pinned to Swan Lake %s (lock: %s)", lock.swan_lake, s.sources.lock_file)
    limit = s.limit_scrape_pages
    per_source: dict[str, dict[str, int]] = {}
    for source in s.sources.order:
        if only and source not in only:
            continue
        stats: dict[str, int] = {}
        per_source[source] = stats
        attempts = 0
        log.info("scraping %s (limit %s)", source, limit or "none")
        try:
            for cand in collectors[source].candidates():
                if limit and attempts >= limit:
                    stats["stopped_at_limit"] = 1
                    break
                outcome = fetch_candidate(db, cand, run, refresh=refresh)
                stats[outcome] = stats.get(outcome, 0) + 1
                run.count(f"pages_{outcome}")
                if outcome in DOWNLOADS:
                    attempts += 1
                if sum(stats.values()) % 25 == 0:
                    run.flush()
        except Exception as exc:  # a listing (sitemap, repo list, ...) failed: record and move on
            log.error("listing failed for %s: %s", source, exc)
            stats["listing_failed"] = 1
            listing = {"bbe": "github+sitemap", "spec": "sitemap", "learn": "sitemap", "central": "central-api",
                       "github": "github-org"}[source]
            _save_failure(db, Candidate(source, f"listing:{source}:{listing}", fetch=lambda r: None),  # type: ignore
                          "failed", f"{type(exc).__name__}: {exc}", run)
        log.info("%s: %s", source, ", ".join(f"{k}={v}" for k, v in stats.items()) or "nothing to do")
    run.flush()
    return per_source
