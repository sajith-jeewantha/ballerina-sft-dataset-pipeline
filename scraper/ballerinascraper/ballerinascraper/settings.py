"""Scrapy settings, derived from config/pipeline.yaml (crawl section).

Politeness defaults: robots.txt obeyed, one honest User-Agent, AutoThrottle,
at most `crawl.concurrent_per_domain` concurrent requests per domain, and an
RFC 2616 HTTP cache so re-crawls send conditional requests (ETag / 304).
"""

from balpipe_common.config import load_settings

_cfg = load_settings()

BOT_NAME = "ballerinascraper"
SPIDER_MODULES = ["ballerinascraper.spiders"]
NEWSPIDER_MODULE = "ballerinascraper.spiders"

USER_AGENT = _cfg.crawl.user_agent
ROBOTSTXT_OBEY = True

CONCURRENT_REQUESTS = 8
CONCURRENT_REQUESTS_PER_DOMAIN = _cfg.crawl.concurrent_per_domain
DOWNLOAD_DELAY = _cfg.crawl.download_delay_s
DOWNLOAD_TIMEOUT = _cfg.crawl.download_timeout_s
RETRY_ENABLED = True
RETRY_TIMES = _cfg.crawl.retry_times
COOKIES_ENABLED = False
TELNETCONSOLE_ENABLED = False

DEFAULT_REQUEST_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en",
}

AUTOTHROTTLE_ENABLED = True
AUTOTHROTTLE_START_DELAY = max(1.0, _cfg.crawl.download_delay_s)
AUTOTHROTTLE_MAX_DELAY = 30
AUTOTHROTTLE_TARGET_CONCURRENCY = 1.0

HTTPCACHE_ENABLED = _cfg.crawl.http_cache
HTTPCACHE_POLICY = "scrapy.extensions.httpcache.RFC2616Policy"
HTTPCACHE_DIR = str(_cfg.data_path("cache", "http"))
HTTPCACHE_IGNORE_HTTP_CODES = [500, 502, 503, 504, 429]

ITEM_PIPELINES = {"ballerinascraper.pipelines.RawRecordPipeline": 100}

LOG_LEVEL = _cfg.logging.level
FEED_EXPORT_ENCODING = "utf-8"
