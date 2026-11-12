"""A polite `requests` session: honest User-Agent, retries with backoff that
honour `Retry-After`, a fixed delay between requests, and GitHub rate-limit
awareness."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

MAX_RATE_LIMIT_SLEEP_S = 15 * 60


class PoliteSession:
    def __init__(
        self,
        user_agent: str,
        *,
        delay_s: float = 0.2,
        timeout_s: float = 60,
        retries: int = 4,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.delay_s = delay_s
        self.timeout_s = timeout_s
        self._last = 0.0
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

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        for _ in range(3):
            wait = self.delay_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            resp = self.session.get(url, timeout=self.timeout_s, **kwargs)
            self._last = time.monotonic()
            if not (resp.status_code in (403, 429) and resp.headers.get("X-RateLimit-Remaining") == "0"):
                return resp
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            sleep_s = min(max(reset - time.time(), 1) + 1, MAX_RATE_LIMIT_SLEEP_S)
            log.warning("rate limited; sleeping %.0fs", sleep_s, extra={"event": "http.rate_limited", "url": url})
            time.sleep(sleep_s)
        return resp
