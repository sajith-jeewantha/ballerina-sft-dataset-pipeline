"""
Generates seed URLs from Common Crawl's public index (CDX API).

IMPORTANT: CDX only supports domain (*.example.com) and prefix
(example.com/*) matching — NOT free-text keyword search across the
whole index. This script queries known Ballerina-relevant domains
and expands them into their actual crawled pages. Open-web discovery
beyond these domains happens via the spider's own link-following,
not via this script.

Usage:
    python scripts/generate_seeds.py
"""
import requests
import json
import os

DOMAIN_PATTERNS = [
    "ballerina.io/*",
    "*.ballerina.io",
    "central.ballerina.io/*",
    "github.com/ballerina-platform/*",
    "stackoverflow.com/questions/tagged/ballerina*",
    "dev.to/*ballerina*",
    "medium.com/*ballerina*",
    "*.ballerina.*",
    "*ballerina*",
]

OUTPUT_PATH = "../seeds/common_crawl_urls.txt"


def get_latest_crawl_id() -> str:
    resp = requests.get("https://index.commoncrawl.org/collinfo.json", timeout=30)
    resp.raise_for_status()
    collections = resp.json()
    return collections[0]["id"]  # newest-first


def fetch_urls_for_pattern(crawl_id: str, pattern: str, limit: int = 1000) -> set:
    resp = requests.get(
        f"https://index.commoncrawl.org/{crawl_id}-index",
        params={"url": pattern, "output": "json", "limit": limit},
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  {pattern}: HTTP {resp.status_code} — skipping")
        return set()

    urls = set()
    for line in resp.text.strip().splitlines():
        try:
            urls.add(json.loads(line)["url"])
        except (json.JSONDecodeError, KeyError):
            continue
    return urls


def main():
    crawl_id = get_latest_crawl_id()
    print(f"Using Common Crawl index: {crawl_id}")

    all_urls = set()
    for pattern in DOMAIN_PATTERNS:
        print(f"Fetching: {pattern}")
        urls = fetch_urls_for_pattern(crawl_id, pattern)
        print(f"  found {len(urls)}")
        all_urls.update(urls)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)

    # merge with any existing seeds (e.g. from github_seeds.py) rather than overwrite
    existing = set()
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH) as f:
            existing = {line.strip() for line in f if line.strip()}

    all_urls |= existing

    with open(OUTPUT_PATH, "w") as f:
        for url in sorted(all_urls):
            f.write(url + "\n")

    print(f"Wrote {len(all_urls)} total seed URLs to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
