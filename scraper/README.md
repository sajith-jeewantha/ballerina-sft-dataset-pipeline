# scraper — the fetch half of `sftgen scrape`

Fetches official Ballerina sources into `data/pipeline.db` (`pages` table). It has no command of
its own: `sftgen scrape` runs it, then converts and chunks the new pages.

Setup and every command, step by step, are in the
[root README: Quick start](../README.md#quick-start-step-by-step). The scrape commands are:

```bash
sftgen scrape                     # up to LIMIT_SCRAPE_PAGES new pages per source
sftgen scrape --source bbe        # one source only (bbe, spec, learn, central, github)
sftgen scrape --refresh           # re-check stored pages for upstream changes
```

| Collector | Source | How |
|---|---|---|
| `collectors/github.py` · `BbeCollector` | Ballerina by Example | Page list from the ballerina.io sitemap; `.md` + `.bal` + `.out` from `ballerina-distribution/examples/<name>/` via the GitHub blobs API. Skipped without a download when the folder's blob SHAs are unchanged. |
| `collectors/ballerina_io.py` · `WebPageCollector` | Learn docs, spec 2024R1 | ballerina.io HTML pages listed in the sitemap; conditional GET (ETag / Last-Modified) on refresh |
| `collectors/central.py` · `CentralCollector` | Ballerina Central | `/2.0/registry/packages` (README) + `/2.0/docs/<org>/<pkg>/<version>` (API docs, compacted); skipped when the version is unchanged |
| `collectors/github.py` · `ModuleCollector` | `module-ballerina-*`, `module-ballerinax-*` repos | READMEs and `ballerina/*.bal` (tests excluded) via the GitHub API, most-starred repos first |

`scrape.py` drives them:

- at most `LIMIT_SCRAPE_PAGES` downloads per source per run
- pages already stored are skipped unless `--refresh` is passed
- a failure is recorded on the page row (`failed` with the error) and the run goes on

`collectors/http.py` is the polite session:

- one honest User-Agent and `REQUEST_DELAY_SECONDS` between requests
- retries with `Retry-After` and GitHub rate-limit handling
- robots.txt checked before each web page, via `collectors/robots.py`, which merges repeated
  `User-agent: *` groups. The documented JSON APIs (`api.github.com`, `api.central.ballerina.io`)
  are exempt.
- a 24-hour disk cache for listings (sitemap, repo lists, trees, package lists)

There is no proxy or User-Agent rotation.

```bash
pytest scraper/ballerinascraper/tests
```
