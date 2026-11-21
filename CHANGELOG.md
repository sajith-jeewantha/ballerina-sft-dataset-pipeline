# Changelog

## Unreleased: plan v2, milestones M0, M1 and M2 (branch `feat/pipeline-plan-v2`)

The plan is in [docs/PLAN.md](docs/PLAN.md), and the seed licence register in
[docs/licence-register.csv](docs/licence-register.csv).

### M0: pinning

- **One Swan Lake update** (`sources.swan_lake: 2201.14.1`). Resolved pins are kept in
  `config/sources.lock.json`:
  - BBE at the `ballerina-distribution` tag `v2201.14.1`, resolved to a commit
  - module repos at a locked commit
  - Central packages at the newest version whose `ballerinaVersion` ≤ the pin, found through
    `/2.0/registry/packages/<org>/<pkg>`
- **Pin changes trigger a re-download,** even without `--refresh`. `scrape --update-lock`
  re-resolves the pins.
- **Teacher provider pin:** `generate.providers` sends `deepseek/*` models to DeepSeek's own
  endpoint only (`allow_fallbacks: false`, `data_collection: deny`), because DeepSeek's terms
  explicitly allow distillation.
  - The routing is part of the LLM cache key, so a pinned call never reuses a response from an
    unpinned one.
  - The serving host is stored in the new `llm_usage.provider` column, added to existing
    databases automatically.
- **The knowledge graph is tracked in git:** `graph/graph.json`, plus `graph/SOURCE.json` with
  the upstream commit `64c8d80` and the file's sha256.

### M1: graph integration and coverage

- **`sftgen graph validate | stats | sync`.**
  - Validation uses the `ballerina_graph` package from ballerina-ecosystem, pinned to the same
    commit: strict schema 0.2 models and graph rules.
  - It also checks the schema version, the hash in `SOURCE.json`, and that top-level edges
    equal node edges.
- **Coverage:** `graph_items` and `item_sources` link every item's sources to scraped pages.
  - For the 500 KB spec, links resolve to the exact section: 1,610 of 1,620 spec links.
  - Sync runs after every scrape.
- **Report:** a new *Knowledge graph coverage* section and a summary line show items grounded
  by priority and node type, the source pages to scrape next, and the core items not yet
  grounded.

### M2: storage switch

- **New `.env` keys:** `STORAGE_BUCKET`, `STORAGE_PROVIDER` (`gcs` | `aws-s3`), `BUCKET_NAME`,
  `BUCKET_PREFIX`, `LOCAL_DATA_DIR`, plus `AWS_PROFILE`, `AWS_REGION` and `S3_ENDPOINT_URL`
  for S3 and S3-compatible stores such as R2. No cloud keys go in `.env`: GCS uses ADC and AWS
  uses a profile.
- **`shared/balpipe_common/storage.py`** (fsspec). The local folder is always the working copy;
  the bucket mirrors it with the same relative paths.
  - With the bucket on, a missing local database is restored from `db/LATEST`.
  - Each phase's new or changed files are uploaded when the phase succeeds (content-hashed;
    unchanged files are skipped).
  - A gzipped database snapshot is uploaded at the end (`storage.keep_db_snapshots`, default
    20). The live database is never written to the bucket.
  - `cache/` and OS files are never uploaded.
  - The lockfile and graph are copied for provenance (`sources/`, `graph/<commit>/`).
- **`sftgen storage status | push | pull [--overwrite]`.** The startup block shows the storage
  target.
- **If an upload fails,** local outputs are kept and the command exits with 1; `sftgen storage
  push` retries.
- Cloud clients are optional extras: `pip install -e "shared[gcs]"` or `"shared[s3]"`.
  `fsspec` is a new core dependency.
- `STORAGE_PROVIDER=local` (a folder as the bucket) is for trying the layout without a cloud
  account.

**Changed.**
- Output paths are now relative to `LOCAL_DATA_DIR`. Defaults: `export.path: dataset.jsonl`,
  `sft.dir: sft`, `report.path: reports/report.html`.
- **The report moved** from `reports/report.html` to `data/reports/report.html`, so all outputs
  live in one folder that mirrors the bucket. The old `reports/` folder can be deleted.
- Older configs with `data/…` paths keep working: the prefix is dropped.

### Tests

80 tests in total (+8 for M2: bucket layout, change-only uploads, pull to a fresh machine,
snapshot pruning, missing client library, data-relative paths, and the CLI end to end with
`STORAGE_BUCKET=false` then `true`).

M0 and M1: 72 tests (+16): graph loading and rules on a small fixture plus the real graph, the
lockfile, Central version pinning, commit locking, re-download on a pin change, source-URL
matching, spec sections, coverage, the report section, and `graph validate`.

## 2.1.0: Q&A pipeline on SQLite (2026-11-15, branch `feat/sqlite-qa-pipeline`)

The pipeline now builds a grounded **question-and-answer** dataset in two LLM phases, tracks every
step in one SQLite database and reports progress after every command. It is built for small local
test runs before a full run.

### Why

- Each step needs to be visible: what was scraped, what failed and why, how many questions and
  answers exist, and what the run cost.
- Small, cheap test runs need to be easy, with limits set in `.env` and printed at startup.
- The source list was narrowed to official sources only. The blog is no longer used.

### New

- **Commands:** `sftgen scrape`, `generate-questions`, `generate-answers`, `validate`, `export`,
  `report` and `run-all`.
- **Limits in `.env`:** `LIMIT_GENERATING`, `LIMIT_SCRAPE_PAGES` (per source), `QUESTIONS_PER_CHUNK`
  and `REQUEST_DELAY_SECONDS`, printed at startup and stored on every run.
- **Storage:** `data/pipeline.db`, with tables `pages`, `chunks`, `questions`, `answers`, `runs` and
  `llm_usage`. Every row has a status and timestamps.
- **Sources:**
  - ballerina.io sitemap filtered to `/learn/`, `/learn/by-example/` and `/spec/lang/2024R1/`.
  - Learn pages are read from their embedded Markdown source.
  - BBE content comes from `ballerina-distribution` through the GitHub API, because the
    ballerina.io page ships the explanation only inside compiled JavaScript.
  - The Central **docs API** is now used alongside the registry API, so answers can be grounded
    in real function and type signatures.
  - All `module-ballerina-*` and `module-ballerinax-*` repos, through the GitHub API.
- **Scraping:**
  - robots.txt is checked for every web page, with a parser that merges repeated
    `User-agent: *` groups (ballerina.io has two).
  - Conditional GETs, plus a 24-hour cache for listings.
  - Unchanged content is skipped by blob SHA, package version, HTTP 304 or content hash.
  - Failures are recorded per page and never stop the run.
- **Questions:**
  - Generated round-robin across sources, so small runs cover every source.
  - Each question is saved as it arrives.
  - Near-duplicates are skipped, and questions that refer to "the excerpt" are rejected.
  - Category and difficulty are normalized.
- **Answers:** generated from the question plus its own saved chunk, and always end with
  `Source: <url>`. A failure is recorded with its error message, and `--retry-failed` retries it.
- **Validate:** answers are marked `valid`, `invalid` (with compiler errors) or `not_checked`.
- **Report:** a terminal summary plus `reports/report.html`, a single file with tables, bar charts,
  run history, token usage and cost per phase, and 5 random Q&A samples.
- **`export-sft`:** writes `data/sft/train.jsonl`, `eval.jsonl` and `metadata.jsonl` in chat
  `messages` format, ready for TRL, Axolotl and Unsloth.
  - It is model-agnostic: the trainer applies the base model's chat template.
  - The train/eval split is by source page.
  - The `Source:` line is removed by default.
  - The system prompt is configurable.
  - It is included in `run-all`.
- **Cost tracking:** the OpenRouter cost is requested per call (`usage.include`). An optional
  `generate.pricing` table is the fallback.

### Removed (approved before removal)

- `index/` (embeddings + ChromaDB), the 7-task `generate/` (with mutation and back-translation),
  `judge/`, the chat-format `export/`, `extract/` pairing.
- The JSONL raw store, run manifests and Pydantic record schemas (`jsonl_io.py`, `manifest.py`,
  `schemas.py`), and the generated `docs/schemas/*.json`.
- The Scrapy web spider and its settings, pipelines and `scrapy.cfg`, the `balcollect` command,
  the old prompts.
- Dependencies: `scrapy`, `chromadb`, `sentence-transformers` (and with them torch and numpy),
  plus the optional GCS extra.

### Behaviour changes to know about

- **Chunk size:** with no embedding model to fit, chunks are now up to about 800 tokens
  (`chunk.max_tokens`, estimated as characters ÷ 4) instead of 300. That gives each question a
  fuller context.
- **No retrieval:** each answer is grounded only in the chunk its question came from.
- **Removed settings:**
  - `JUDGE_MODEL`, `EMBEDDING_MODEL` and `GCS_BUCKET` are no longer read.
  - The judge and its thresholds are gone; validation by `bal` is the quality gate, and
    `invalid` answers are left out of the export by default.
- **Empty `.env` values are now ignored.** `OPENROUTER_API_KEY=` in the root `.env` no longer
  hides a key set elsewhere.
- **Logging and workers:** logs are plain text by default (`logging.format: text`), and
  `validate.workers` defaults to 2 instead of 4, which keeps a laptop cooler.
- **User-Agent:** now points to this repository.

### Tests

Shared, scraper and generator tests were rewritten for the new flow: 56 tests, including a real
`bal` compile round trip when Ballerina is installed.

## 2.0.0: pipeline-v2 refactor (2026-11-12, branch `refactor/pipeline-v2`)

A full refactor of both projects to match the approved design ([docs/recommendations.md](docs/recommendations.md), [docs/schemas.md](docs/schemas.md), [ARCHITECTURE.md](ARCHITECTURE.md)). The state before the refactor, including your uncommitted work, is saved in commit `60f142b`.

### Why

- 86% of the old corpus was raw HTML. Pages containing `ballerina/http` were labelled `bal_embedded` and never cleaned.
- Code was flattened onto one line by `" ".join(words)`.
- The embedding model cut off about half of every chunk (500 words against a 384-token limit).
- Dedup didn't work across runs: 41 duplicate hashes in 120 records.
- Nothing could resume after a crash.
- The generator's `requirements.txt` was the scraper's.
- Output records couldn't be traced back to the chunk that seeded them.

### Collect (`scraper/`)

- **Source first.**
  - A new GitHub collector, pinned to a commit SHA, gets the Ballerina-by-Example sources (`.bal` + `.md` + `.out`), the ballerina.io Markdown docs and stdlib modules.
  - A new Central registry API collector gets package READMEs and licenses.
  - HTML crawling is now a sitemap-only Scrapy spider for blog.ballerina.io.
- **Compliance.**
  - Removed free-proxy rotation and User-Agent spoofing.
  - The crawler sends one honest User-Agent, obeys robots.txt, uses AutoThrottle and makes at most 2 requests per domain.
  - It sends conditional GETs through the RFC 2616 HTTP cache.
  - Stack Overflow is dropped: its robots.txt disallows all bots.
- **Raw data is never modified.** Raw records are written to `data/raw/<source>/<run>/part-*.jsonl.gz`, with a license and the basis for it, fetch metadata and the git or Central details. Content identical to an earlier run is skipped, and git blobs that haven't changed aren't downloaded again.
- **Cleaning moved out of the scraper.** The Scrapy pipelines no longer extract, filter or dedup; that's `sftgen clean`'s job, so raw data can always be re-processed.
- **Removed:**
  - `rotating_proxies.py`, the proxy list, `user_agents.py`
  - the Common Crawl and GitHub seed scripts and the seed file
  - the template middlewares, `code.py`/`text.py` scratch
  - a duplicate `requirements.txt`

### Generate (`sft-dataset-generator/` → `sftgen` CLI)

- **New stages:** `clean`, `extract`, `chunk`, `index`, `generate`, `validate`, `judge`, `export`, `all`. They replace `main.py`, `ingest_corpus.py`, `env_loader.py` and `prompt_temple.py`.
- **Clean.**
  - Each source type gets its own extractor. The `__NEXT_DATA__` extractor recovers by-example code that trafilatura misses entirely; trafilatura now produces Markdown.
  - Every code block gets a language label.
  - A relevance filter drops off-topic pages.
  - Exact and MinHash near-duplicate marking keeps the most canonical copy.
  - The train/eval split is assigned per document.
- **Extract.**
  - Code is paired with its explanation from by-example directives, adjacent Markdown prose, or `#` doc comments.
  - Every example is compiled with `bal`. By-example programs are run and their output compared with `.out`.
- **Chunk.** Chunks follow headings and fit a token budget. Code blocks are never split, and the budget is checked against the embedding model's `max_seq_length`.
- **Index.** The Chroma collection is named after the embedding model and chunker version, writes are upserts, and stale vectors are removed.
- **Generate.**
  - Seven task types with quotas and a per-document cap.
  - `debug_fix` is built by mutating verified code and capturing the real compiler error.
  - `convert_from_language` is built by back-translation: the answer is the original verified Ballerina code.
  - `complete_code` answers are the original verified code.
  - Retrieval is limited to the seed's split.
- **Validate,** now before judge (a deliberate order change, explained in ARCHITECTURE.md):
  - compiles answer code in isolated temp dirs, offline-first, with a timeout, heap cap and cache,
  - flags answers that mention a hidden "context",
  - flags unknown `module:symbol`s.
- **Judge.** Scores 1–5 on five criteria, with reasons. Warns if the judge model is from the same family as the answer model.
- **Export.**
  - Chat `messages` JSONL, plus `metadata.jsonl` with full provenance.
  - Thresholds come from the config and are re-applied at export time.
  - License allowlist, near-duplicate question filter, 13-gram decontamination.
- **Prompts** are versioned YAML files. No "based on the context" phrasing, and `INSUFFICIENT_CONTEXT` answers are dropped.
- **LLM calls.**
  - OpenAI SDK pointed at OpenRouter by default.
  - SDK retries with backoff that honours `Retry-After`; no retries on 400 or 401.
  - Concurrency limit and requests-per-minute limiter.
  - Responses cached on disk.
  - The old `{"reasoning": {"effort": "none"}}` request field is kept as the configurable default (`generate.extra_body`).
- **Removed:**
  - the `deepseek.py`/`test.py` scratch and `generated.jsonl`
  - the 3.6 GB JVM heap dump, `main.jar`, the old `chroma_db/`
  - the old `data/*.jsonl` (you agreed it was throwaway)

### Shared (`shared/balpipe_common`)

- Pydantic schemas for every record, with JSON Schemas generated into `docs/schemas/`.
- Typed config: `.env` for secrets, `config/pipeline.yaml` for everything else, with `BALPIPE__*` env overrides. Secrets are never logged.
- JSON logging, deterministic IDs, durable/atomic JSONL I/O, and run manifests that make resuming possible.

### Behaviour changes to know about

- Config: the root `.env` replaces the per-project `.env` files.
  - The old locations and the old names `API_KEY` and `BUCKET_NAME` still work, with a warning.
  - The per-project `.env.example` files are replaced by one at the root.
- **GCS is opt-in** (`gcs_mirror: false`). Before, setting `BUCKET_NAME` was enough to start uploading.
- Generated answers that should contain code must contain code that compiles. Before, answers with no code passed automatically.
- The judge result is now a score set compared against thresholds, not FAITHFUL/UNFAITHFUL.

### Added after review

- **Language specification 2024R1** is now a source (`public/spec/lang/2024R1/index.html` in `ballerina-dev-website`). It's converted by a structure-preserving extractor that keeps all 257 `<pre>` blocks: grammar becomes ```ebnf, langlib becomes ```ballerina. trafilatura kept only 36 of them. Contributor email addresses are removed.
- Manifests store paths relative to the repo root, not absolute paths on your machine.

### Bugs found by running on the real corpus (all fixed, each with a regression test)

- The blog's Next.js `codes` field is a string, so it was being split into thousands of one-character "code blocks". The Next.js extractor now only accepts BBE-style lists of code strings.
- An unterminated string literal in a docs snippet made the comment/string stripper drop a newline, which crashed extract.
- Single very long lines, like a 2,000-token minified JSON line, couldn't be split by line, so chunks went over budget. Long lines are now split by word, then by character, and the budget is checked against the exact embedded text.
- Chunk IDs came from the document hash, so a chunker change could keep an ID with different text, leaving a stale vector. They now come from the chunk's own text.
- Central change detection compared version-independent keys with version-pinned URIs, so it re-wrote every package on each run.
- Batch stages inherited the counts and errors of a previous failed run. Only checkpointed stages (generate, validate, judge) resume now.
- Concurrent BBE programs print in a non-deterministic order. Output comparison now also accepts the same lines in any order.

### Tests

60 tests across `shared/`, `scraper/` and `sft-dataset-generator/`, covering:

- cleaning, chunking and pairing
- the Ballerina heuristics
- the compiler wrapper, including a real `bal` round trip
- mutations and planning
- static checks and export decisions
- config, I/O and manifests
