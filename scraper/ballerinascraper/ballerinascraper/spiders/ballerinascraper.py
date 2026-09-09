from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from ballerinascraper.items import BallerinascraperItem

import scrapy
from scrapy.linkextractors import LinkExtractor
import random


def clean_url(url: str) -> str:
    """Strip tracking params (utm_*, etc.) so the same page isn't
    fetched repeatedly under different query strings."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    qs = {k: v for k, v in qs.items() if not k.lower().startswith("utm_")}
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query, fragment=""))




class BallerinaSpider(scrapy.Spider):
    name = "ballerinascraper"

    custom_settings = {
        "ROBOTSTXT_OBEY": True,
        "AUTOTHROTTLE_ENABLED": True,
        "DOWNLOAD_DELAY": 1.0,
        "DEPTH_LIMIT": 3,  # add this
        "CLOSESPIDER_PAGECOUNT": 2000,  # add this — hard stop for testing
        "CLOSESPIDER_ITEMCOUNT": 500,  # add this
        # 'FEEDS': {
        #     'ballerina.json': {'format': 'json', 'overwrite': True},
        # }
    }

    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0",
    ]

    SEED_FILE = "../ballerinascraper/seeds/common_crawl_urls.txt"

    # async def start(self):
    #     print("*** start() called ***")
    #     with open(self.SEED_FILE) as f:
    #         for url in f:
    #             url = url.strip()
    #             if url:
    #                 yield scrapy.Request(
    #                     url,
    #                     callback=self.parse,
    #                     headers={"User-Agent": random.choice(self.USER_AGENTS)},
    #                 )
    #
    # def parse(self, response):
    #     relevance = self.classify(response)
    #     if relevance:
    #         item = BallerinascraperItem()
    #         item.url = response.url
    #         item.content = response.text
    #         item.file_type = relevance
    #         item.content_type = response.headers.get("Content-Type", b"").decode(errors="ignore")
    #         item.source_domain = urlparse(response.url).netloc
    #         item.fetched_at = datetime.now(timezone.utc).isoformat()
    #         yield item
    #
    #     for link in LinkExtractor().extract_links(response):
    #         yield response.follow(
    #             link.url,
    #             callback=self.parse,
    #             headers={"User-Agent": random.choice(self.USER_AGENTS)},
    #         )
    #
    # def classify(self, response):
    #     """Return a file_type string if relevant, else None."""
    #     url = response.url.lower()
    #     text = response.text.lower()
    #
    #     if url.endswith(".bal"):
    #         return "bal"
    #
    #     if "import ballerina/" in text or "ballerina/http" in text or "service /" in text:
    #         # looks like it contains actual Ballerina code, even embedded in a page
    #         return "bal_embedded"
    #
    #     if url.endswith(".md") and "ballerina" in text:
    #         return "md"
    #
    #     if text.count("ballerina") > 3:
    #         return "html"
    #
    #     return None


# **************************

    async def start(self):
        print("*** start() called ***")
        try:
            with open(self.SEED_FILE) as f:
                seed_urls = [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            self.logger.error(
                f"Seed file not found: {self.SEED_FILE}. "
                "Run scripts/generate_seeds.py and scripts/github_seeds.py first."
            )
            return

        self.logger.info(f"Starting crawl with {len(seed_urls)} seed URLs")

        for url in seed_urls:
            yield scrapy.Request(
                url,
                callback=self.parse,
                headers={"User-Agent": random.choice(self.USER_AGENTS)},
                errback=self.handle_error,
            )


    def parse(self, response):
        content_type = response.headers.get("Content-Type", b"").decode(errors="ignore")
        if "text" not in content_type and "html" not in content_type:
            return  # skip binaries (images, pdfs, etc.) — out of scope for this corpus

        relevance = self.classify(response)
        if relevance:
            item = BallerinascraperItem()
            item["url"] = response.url
            item["content"] = response.text
            item["file_type"] = relevance
            item["content_type"] = content_type
            item["source_domain"] = urlparse(response.url).netloc
            item["fetched_at"] = datetime.now(timezone.utc).isoformat()
            yield item

        for link in LinkExtractor().extract_links(response):
            next_url = clean_url(link.url)
            yield response.follow(
                next_url,
                callback=self.parse,
                headers={"User-Agent": random.choice(self.USER_AGENTS)},
                errback=self.handle_error,
            )

        for link in LinkExtractor().extract_links(response):
            if not self.is_link_worth_following(link):
                continue
            next_url = clean_url(link.url)
            yield response.follow(
                next_url,
                callback=self.parse,
                errback=self.handle_error,
                headers={"User-Agent": random.choice(self.USER_AGENTS)},
            )

    def is_link_worth_following(self, link):
        url = link.url.lower()
        text = (link.text or "").lower()
        trusted_domains = ("ballerina.io", "github.com", "central.ballerina.io", "dev.to", "medium.com")
        same_trusted_domain = any(d in urlparse(url).netloc for d in trusted_domains)
        mentions_ballerina = "ballerina" in url or "ballerina" in text
        return same_trusted_domain or mentions_ballerina

    def classify(self, response):
        """Return a file_type string if the page is Ballerina-relevant, else None."""
        url = response.url.lower()
        text = response.text.lower()

        if url.endswith(".bal"):
            return "bal"

        if any(marker in text for marker in ("import ballerina/", "ballerina/http", "ballerina/io")):
            return "bal_embedded"

        if url.endswith(".md") and "ballerina" in text:
            return "md"

        if text.count("ballerina") > 3:
            return "html"

        return None


    def handle_error(self, failure):
        self.logger.warning(f"Request failed: {failure.request.url} — {failure.value}")
