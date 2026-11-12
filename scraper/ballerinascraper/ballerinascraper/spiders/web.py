"""Sitemap-driven crawl of allowlisted sites that have no upstream source repo
(currently blog.ballerina.io). Seeds come only from configured sitemaps; no
link-following, so the crawl can never wander off the allowlist."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from balpipe_common.config import load_settings
from balpipe_common.schemas import License
from scrapy.http import Response
from scrapy.spiders import SitemapSpider

_cfg = load_settings()


def canonical_url(url: str) -> str:
    """Drop tracking params (utm_*, fbclid, ...) and fragments."""
    p = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(p.query) if not (k.lower().startswith("utm_") or k.lower() in {"fbclid", "gclid"})]
    return urlunparse(p._replace(query=urlencode(query), fragment=""))


class WebSpider(SitemapSpider):
    name = "web"
    allowed_domains = _cfg.sources.web_allowed_domains
    sitemap_urls = _cfg.sources.web_sitemaps
    sitemap_rules = [(pattern, "parse_page") for pattern in _cfg.sources.web_sitemap_follow]

    def parse_page(self, response: Response) -> Iterator[dict[str, Any]]:
        content_type = response.headers.get("Content-Type", b"").decode(errors="ignore")
        if "html" not in content_type:
            self.logger.info("skipping non-HTML %s (%s)", response.url, content_type)
            return
        redirects = response.request.meta.get("redirect_urls") or [response.request.url]
        yield {
            "source_uri": canonical_url(response.url),
            "media_type": "text/html",
            "body": response.text,
            "requested_url": redirects[0],
            "final_url": response.url,
            "http_status": response.status,
            "content_type_header": content_type or None,
            "etag": (response.headers.get("ETag") or b"").decode() or None,
            "last_modified": (response.headers.get("Last-Modified") or b"").decode() or None,
            "license": License(spdx="NOASSERTION", basis="site_terms", url=f"https://{urlparse(response.url).netloc}/"),
        }
