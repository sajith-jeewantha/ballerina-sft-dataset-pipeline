# Changelog

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
