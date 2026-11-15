"""A polite `requests` session.

* one honest User-Agent,
* a fixed delay between requests (REQUEST_DELAY_SECONDS),
* retries with backoff that honour `Retry-After`, and GitHub rate-limit awareness,
* robots.txt checked before every web page request (documented JSON APIs are
  exempt; see `crawl.robots_exempt_hosts`),
* conditional requests (ETag / Last-Modified) so unchanged pages are not re-downloaded,
* an on-disk cache with a TTL for listing calls (sitemap, repo lists, trees).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from balpipe_common.ids import sha256_text

from ballerinascraper.collectors.robots import RobotsRules

log = logging.getLogger(__name__)

MAX_RATE_LIMIT_SLEEP_S = 15 * 60


class BlockedByRobots(RuntimeError):
    pass


@dataclass
class Response:
    status: int
    text: str | None  # None when the server answered 304 Not Modified
    url: str
    etag: str | None = None
    last_modified: str | None = None
    content_type: str | None = None

    def json(self) -> Any:
        return json.loads(self.text or "null")


class PoliteSession:
    def __init__(
        self,
        user_agent: str,
        *,
        delay_s: float = 1.0,
        timeout_s: float = 60,
        retries: int = 3,
        headers: dict[str, str] | None = None,
        respect_robots: bool = True,
        robots_exempt_hosts: tuple[str, ...] = (),
        cache_dir: Path | None = None,
    ) -> None:
        self.user_agent = user_agent
        self.delay_s = delay_s
        self.timeout_s = timeout_s
        self.respect_robots = respect_robots
        self.robots_exempt_hosts = set(robots_exempt_hosts)
        self.cache_dir = cache_dir
        self._last = 0.0
        self._robots: dict[str, RobotsRules] = {}
        self.requests_made = 0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, **(headers or {})})
        retry = Retry(
            total=retries,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET", "HEAD"),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    # ------------------------------------------------------------------ #
    def _wait(self) -> None:
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)

    def _raw_get(self, url: str, headers: dict[str, str] | None = None) -> requests.Response:
        resp: requests.Response | None = None
        for _ in range(3):
            self._wait()
            resp = self.session.get(url, timeout=self.timeout_s, headers=headers or {})
            self._last = time.monotonic()
            self.requests_made += 1
            if not (resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0"):
                return resp
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            sleep_s = min(max(reset - time.time(), 1) + 1, MAX_RATE_LIMIT_SLEEP_S)
            log.warning("rate limited by %s; sleeping %.0fs", urlsplit(url).netloc, sleep_s)
            time.sleep(sleep_s)
        assert resp is not None
        return resp

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parts = urlsplit(url)
        if parts.netloc in self.robots_exempt_hosts:
            return True
        rules = self._robots.get(parts.netloc)
        if rules is None:
            robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
            try:
                r = self._raw_get(robots_url)
                text = r.text if r.status_code == 200 and "html" not in r.headers.get("Content-Type", "") else ""
                if r.status_code in (401, 403):  # RFC 9309: unreachable robots.txt due to auth = disallow all
                    text = "User-agent: *\nDisallow: /"
            except requests.RequestException:
                text = ""  # robots.txt unreachable: treat as no restrictions (RFC 9309, 5xx aside)
            rules = RobotsRules.parse(text, self.user_agent)
            self._robots[parts.netloc] = rules
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        return rules.allowed(path)

    def get(self, url: str, *, etag: str | None = None, last_modified: str | None = None) -> Response:
        """GET with robots check and optional conditional headers. Raises on HTTP errors."""
        if not self.allowed(url):
            raise BlockedByRobots(f"robots.txt disallows {url}")
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        resp = self._raw_get(url, headers)
        if resp.status_code == 304:
            return Response(304, None, resp.url, etag, last_modified)
        resp.raise_for_status()
        return Response(
            resp.status_code, resp.content.decode("utf-8", errors="replace"), resp.url,
            resp.headers.get("ETag"), resp.headers.get("Last-Modified"), resp.headers.get("Content-Type"),
        )

    def get_cached(self, url: str, *, ttl_hours: float) -> Response:
        """GET for listings (sitemaps, repo lists, trees), cached on disk for `ttl_hours`."""
        path = self.cache_dir / f"{sha256_text(url)[:24]}.json" if self.cache_dir else None
        if path and path.is_file() and (time.time() - path.stat().st_mtime) < ttl_hours * 3600:
            data = json.loads(path.read_text(encoding="utf-8"))
            return Response(data["status"], data["text"], data["url"])
        resp = self.get(url)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"status": resp.status, "text": resp.text, "url": resp.url}), encoding="utf-8")
        return resp
