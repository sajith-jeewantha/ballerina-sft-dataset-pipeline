# scraper — collect stage

Writes immutable `RawRecord`s to `data/raw/<source>/<run_id>/part-*.jsonl.gz`
(schema: [../docs/schemas.md](../docs/schemas.md) §1). Setup and the full pipeline are in the
[root README](../README.md).

| Command | Source | Notes |
|---|---|---|
| `python -m ballerinascraper.collect github` | Repos in `sources.github_repos` | GitHub API + raw files pinned to a commit SHA; files whose blob SHA is unchanged are not re-downloaded. `GITHUB_TOKEN` is optional but raises API limits. |
| `python -m ballerinascraper.collect central` | `api.central.ballerina.io` registry | Package README + license + version; volatile fields (signed URLs, pull counts) are dropped before hashing. |
| `scrapy crawl web` (from `scraper/ballerinascraper/`) | `sources.web_sitemaps` | Sitemap-only (no link following), robots.txt obeyed, AutoThrottle, ≤ 2 requests/domain, RFC 2616 HTTP cache (conditional GETs). |

Politeness and limits are configured in `config/pipeline.yaml` (`crawl:` section). There is no
proxy or User-Agent rotation: the crawler identifies itself honestly.

Every run writes `_manifest.json` with counts (`written`, `unchanged`, errors). Content identical
to an earlier run is skipped, so re-running is cheap and safe.
