"""
Pulls .bal file URLs directly from GitHub's REST API — both the official
ballerina-platform org and a broader code search across all of GitHub.
This is the highest-signal source for actual code (vs. the web crawl,
which mostly finds docs/marketing pages).

Requires a GitHub personal access token in the GITHUB_TOKEN env var
(unauthenticated requests are heavily rate-limited — 10/min vs 30/min
for search, and much lower for content endpoints).

Usage:
    export GITHUB_TOKEN=ghp_xxx
    python scripts/github_seeds.py
"""
import requests
import os
import time

from dotenv import load_dotenv
load_dotenv()

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
HEADERS = {"Accept": "application/vnd.github+json"}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"

OUTPUT_PATH = "../seeds/common_crawl_urls.txt"  # shared seed file with generate_seeds.py


def get_org_repos(org: str) -> list:
    repos = []
    page = 1
    while True:
        resp = requests.get(
            f"https://api.github.com/orgs/{org}/repos",
            headers=HEADERS,
            params={"per_page": 100, "page": page},
            timeout=30,
        )
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        repos.extend(batch)
        page += 1
        time.sleep(0.5)
    return repos


def search_bal_files(query: str = "extension:bal", max_pages: int = 10) -> list:
    """GitHub code search — finds .bal files anywhere on GitHub, not just
    the official org. Rate-limited to 10 req/min even with a token."""
    urls = []
    for page in range(1, max_pages + 1):
        resp = requests.get(
            "https://api.github.com/search/code",
            headers=HEADERS,
            params={"q": query, "per_page": 100, "page": page},
            timeout=30,
        )
        if resp.status_code == 403:
            print("  Rate limited — waiting 60s")
            time.sleep(60)
            continue
        resp.raise_for_status()
        data = resp.json()
        items = data.get("items", [])
        if not items:
            break
        for item in items:
            urls.append(item["html_url"].replace("/blob/", "/raw/"))
        time.sleep(6)  # stay under 10 req/min
    return urls


def main():
    all_urls = set()

    print("Fetching ballerina-platform org repos...")
    repos = get_org_repos("ballerina-platform")
    print(f"  found {len(repos)} repos")
    for repo in repos:
        # seed the repo root; the spider's own link-following (if pointed at
        # github.com HTML pages) or a dedicated GitHub-tree walk can expand
        # this further — kept simple here on purpose
        all_urls.add(repo["html_url"])
        if repo.get("homepage"):
            all_urls.add(repo["homepage"])

    print("Searching GitHub code for .bal files...")
    bal_urls = search_bal_files()
    print(f"  found {len(bal_urls)} .bal file URLs")
    all_urls.update(bal_urls)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
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
    if not GITHUB_TOKEN:
        print("WARNING: no GITHUB_TOKEN set — you will hit rate limits almost immediately.")
    main()
