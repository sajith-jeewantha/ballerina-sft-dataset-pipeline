# scraper — `sftgen scrape` and `sftgen collect-repos`

Fetches official Ballerina sources into `data/pipeline.db`, pinned to one Swan Lake update
(`sources.swan_lake`, resolved pins in `config/sources.lock.json`). It has no command of its own:

- `sftgen scrape` runs the collectors (`pages` table), then converts and chunks the new pages;
- `sftgen collect-repos` runs `repos.py`, which snapshots whole repositories for the codebase pool,
  the raw corpus and the commit miner (`repos` and `repo_files` tables).

Setup and every command, step by step, are in the
[root README: Quick start](../README.md#quick-start-step-by-step). The scrape commands are:

```bash
sftgen scrape                     # up to LIMIT_SCRAPE_PAGES new pages per source
sftgen scrape --source bbe        # one source only (bbe, spec, learn, central, github)
sftgen scrape --refresh           # re-check stored pages for upstream changes
sftgen scrape --update-lock       # re-resolve the pinned commits and Central versions
sftgen collect-repos              # up to LIMIT_REPOS repositories per run
sftgen collect-repos --group guides --repo owner/name
```

| Collector | Source | How |
|---|---|---|
| `collectors/github.py` · `BbeCollector` + `bbe_source.py` | Ballerina by Example | Page list from the ballerina.io sitemap; `.md` + `.bal` + `.out` from `ballerina-distribution/examples/<name>/` at the pinned release tag, read from a sparse git snapshot (no API quota; the blobs API is the fallback). Skipped without a download when the folder's blob SHAs are unchanged. |
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

## `repos.py`: `collect-repos`

For each repository in `repos.groups` (platform module repos, `ballerina-guides`, `wso2`,
permissive external repos from a GitHub search, plus `--repo`):

1. **Pre-gate** on the licence GitHub reports: no licence or a copyleft licence is excluded
   without a clone.
2. **Pin** the default branch with `git ls-remote` (no API quota) into `sources.lock.json`.
3. **Snapshot** with a blobless, shallow, sparse clone: `*.bal`, `Ballerina.toml`, `*.out`,
   READMEs and the root LICENSE. A `.tar.gz` goes to `data/repos/`.
4. **Gate** on the LICENSE text (`balpipe_common.licences`). Every repo seen gets a row in
   `docs/licence-register.csv`; people's `exclude` and `verified:` entries win.
5. **Store** the files with their project, a Swan Lake / 1.x classification, a generated-code
   flag and fingerprints; then exact and MinHash dedup across all repos (the earlier group wins).

```bash
pytest scraper/ballerinascraper/tests
```
