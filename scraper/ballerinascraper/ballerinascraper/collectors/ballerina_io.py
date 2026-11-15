"""ballerina.io: the sitemap decides which pages exist.

Only `/learn/` (including `/learn/by-example/`) and `/spec/lang/2024R1/` are
kept; old version paths, robots-disallowed paths and duplicates are dropped.
Learn and spec pages are fetched as HTML from ballerina.io. BBE pages are
listed here but their content comes from GitHub (see `github.BbeCollector`),
because the site ships BBE explanations only inside compiled JavaScript.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from urllib.parse import urlsplit, urlunsplit

from balpipe_common.config import BallerinaIoCfg

from ballerinascraper.collectors.base import Candidate, Fetched
from ballerinascraper.collectors.http import PoliteSession

LOC_RE = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.DOTALL)
BBE_RE = re.compile(r"^/learn/by-example/([^/]+)/$")
SPEC_PATH = "/spec/lang/2024R1/"


def normalize_url(url: str) -> str:
    """https, no query/fragment, trailing slash: one spelling per page."""
    parts = urlsplit(url.strip())
    path = parts.path if parts.path.endswith("/") else parts.path + "/"
    return urlunsplit(("https", parts.netloc.lower(), path, "", ""))


def classify(url: str, cfg: BallerinaIoCfg) -> str | None:
    """'bbe' | 'spec' | 'learn' for kept URLs, None for everything else."""
    path = urlsplit(url).path
    if not any(path.startswith(p) for p in cfg.include_prefixes):
        return None
    if any(re.search(p, path) for p in cfg.exclude_patterns):
        return None
    if path == SPEC_PATH:
        return "spec"
    if BBE_RE.match(path):
        return "bbe"
    if path.startswith("/learn/"):
        return "learn"
    return None


def parse_sitemap(xml: str, cfg: BallerinaIoCfg) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"bbe": [], "spec": [], "learn": []}
    seen: set[str] = set()
    for loc in LOC_RE.findall(xml):
        url = normalize_url(loc)
        if url in seen:
            continue
        seen.add(url)
        kind = classify(url, cfg)
        if kind:
            out[kind].append(url)
    return out


class Sitemap:
    def __init__(self, http: PoliteSession, cfg: BallerinaIoCfg, ttl_hours: float) -> None:
        self.http = http
        self.cfg = cfg
        self.ttl_hours = ttl_hours
        self._urls: dict[str, list[str]] | None = None

    def urls(self, kind: str) -> list[str]:
        if self._urls is None:
            xml = self.http.get_cached(self.cfg.sitemap, ttl_hours=self.ttl_hours).text or ""
            self._urls = parse_sitemap(xml, self.cfg)
        return [u for u in self._urls[kind] if self.http.allowed(u)]


def fetch_page(http: PoliteSession, url: str, row: sqlite3.Row | None) -> Fetched:
    """Conditional GET when we already hold a cached copy."""
    cached = row is not None and row["raw_body"]
    resp = http.get(url, etag=row["etag"] if cached else None, last_modified=row["last_modified"] if cached else None)
    return Fetched(resp.text, resp.status, resp.url, resp.etag, resp.last_modified)


class WebPageCollector:
    """Learn pages or the spec, straight from ballerina.io."""

    def __init__(self, source: str, sitemap: Sitemap, http: PoliteSession) -> None:
        self.source = source
        self.sitemap = sitemap
        self.http = http

    def candidates(self) -> Iterator[Candidate]:
        for url in self.sitemap.urls(self.source):
            yield Candidate(self.source, url, fetch=lambda row, u=url: fetch_page(self.http, u, row))
