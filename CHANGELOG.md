# Changelog

## Unreleased: plan v2, milestones M0–M8 (branch `feat/pipeline-plan-v2`)

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

### M3: licence register and repo collector

- **`sftgen collect-repos`** takes whole Ballerina repositories in four groups
  (`repos.groups`): `platform` (module repos), `guides` (`ballerina-guides`), `wso2`, and
  `external` (GitHub search, Swan Lake era, not forks).
  - `--group` and `--repo owner/name` narrow a run; `--refresh` and `--update-lock` re-collect.
  - New `LIMIT_REPOS` (default 20) caps clones per run.
- **Licence gate** (`balpipe_common/licences.py`):
  - A pre-gate on GitHub's licence: no licence or copyleft means no clone.
  - The root LICENSE text is then read and recognised: Apache-2.0, MIT, BSD, ISC, BSL-1.0,
    CC0, Unlicense, GPL family, MPL, EPL.
  - Verdicts are `safe`, `check` or `exclude`; only `safe` repos keep their files.
- **The licence register** (`docs/licence-register.csv`) is updated every run.
  - New columns: `licence_file_sha256` and `files`. Extra columns people add are kept.
  - `exclude` rows always win.
  - `safe` with notes starting `verified:` overrides the gate after a manual check.
  - Unlicensed search hits stay in the database only.
- **Pinning and snapshots:**
  - The default branch is pinned with `git ls-remote` in `sources.lock.json` (`repos`).
  - Blobless shallow sparse clone of `*.bal`, `Ballerina.toml`, `*.out`, READMEs and LICENSE.
  - A `.tar.gz` snapshot goes to `data/repos/` and is synced to the bucket.
- **Swan Lake filter per file:** `swan_lake`, `legacy` or `unknown`, from the project's
  `distribution`, the `[package]`/`[project]` layout of `Ballerina.toml`, and 1.x or Swan Lake
  syntax markers. Generated files are flagged.
- **Dedup across repos** (`balpipe_common/dedup.py`):
  - Exact: sha256 of tokens without comments or whitespace.
  - Near-duplicates: MinHash with 128 permutations over 5-token shingles, LSH, similarity ≥ 0.8.
  - Earlier groups keep the original.
  - M4's decontamination will reuse the same fingerprints.
- **New tables** `repos` and `repo_files`.
- **The report** gets a *Code repositories* section, and the terminal summary a Repos line.
- `numpy` is a new core dependency, for MinHash.

**Live run (no GitHub token):**
- `ballerina-guides`: 15 of 15 collected, all confirmed Apache-2.0.
- `external`: 123 of 1,000 collected; 873 excluded for having no licence; 4 need a licence check.
- In total, about 2,800 unique `.bal` files (~3.1M tokens).

### M4: eval set v1 and decontamination

- **`sftgen build-eval`**:
  - **Pin:** `ballerina-distribution` at the Swan Lake tag (`bbe` in the lockfile), with
    `examples/` read from a sparse clone.
  - **Split:** 30% eval / 70% train **by example**, decided by a hash of the example name.
  - **Tags:** eval items tagged with graph `node#item` refs.
  - **Checks:** with the pinned toolchain, `bal run` output against the `.out` transcript, or
    `bal build`. Results are cached, and `--recheck` re-runs them.
  - **Outputs:** `data/eval/v1/bbe.jsonl`, `decontam.jsonl` and `split.json`.
- **Decontamination** (`balpipe_common.decontam`):
  - **What counts as a copy:** exact code (comments and layout ignored), MinHash ≥ 0.8, or
    ≥ 50% of an eval item's distinctive shingles (boilerplate shingles are ignored).
  - **What's flagged:** chunks, questions (with their answers) and repo `.bal` files, in
    `eval_overlap`.
  - **Held out:** eval pages and overlapping chunks become `eval_holdout`, so no questions are
    generated from them.
  - **Exports:** `export` and `export-sft` leave out overlapping questions and re-check what
    they wrote (`eval_overlap_in_export`).
  - **When it runs:** incrementally, after `scrape` and `collect-repos`, and before
    `generate-questions` and the exports.
- **New tables** `eval_items` and `fingerprints`; new columns `eval_overlap` and
  `decontam_key` on `chunks`, `questions` and `repo_files` (added automatically).
- **Config:** a new `eval:` section in `pipeline.yaml`, and new flags `build-eval --no-verify`
  and `--recheck`.
- **The report** gets an *Eval set* section, and the terminal summary an Eval set line.

**Live run:**
- 584 examples: 188 eval, 396 train. 187 eval items are tagged with graph items.
- 148 of the 150 checkable items pass. The 2 that differ (`flush`,
  `convert-from-json-to-user-defined-type`) behave differently on 2201.14.1.
- Held out: 131 chunks, 3 questions and 11 repo files. Both exports have 0 overlap.

### M5: item-driven prompts (pilot)

- **`sftgen plan-prompts`** (no model calls):
  - Prompts are planned per graph item for the nodes in `prompts.nodes`. The pilot is the HTTP
    cluster: 9 nodes, 122 items, **884 prompts**.
  - Budgets by priority: core 10, important 6, long tail 3.
  - Each budget is spread over the task mix (`write_code`, `use_api`, `explain`, `fix_code`,
    `design`) and the item's own difficulties.
- **`sftgen generate-prompts`**:
  - **Write:** per item, the writer (`PROMPT_WRITER_MODEL`, default the DeepSeek teacher) writes
    prompts for the open slots from the item's grounding.
  - **Filter:** prompts that refer to their sources, near-duplicates, and eval overlaps are
    dropped.
  - **Review:** a judge of a **different model family** (`JUDGE_MODEL`, default
    `QUESTION_GEN_MODEL`; enforced) scores each prompt on five criteria. Accepted = every score
    ≥ 3.
  - **Gap-fill:** rejected slots are rewritten on later runs, up to 3 writer calls per item.
  - **Bookkeeping:** `LIMIT_GENERATING` caps the prompts written per run; calls are recorded as
    `write-prompts` / `review-prompts` in `llm_usage`.
  - **Outputs:** `data/prompts/<set>/{accepted,rejected}.jsonl`, `coverage.json`.
- **Grounding** (`sftgen/retrieval/`):
  - **Own sources:** the item's own sources, as best-matching chunks or spec sections.
  - **GitHub sources:** read from `collect-repos` snapshots when not scraped.
  - **Central symbols:** a new `symbols` table with 3,248 entries from 90 modules, parsed from the
    Central API reference.
  - **Node sources**, for items without their own.
  - **FTS5 neighbours:** a new `chunks_fts` BM25 index.
  - Eval overlaps are never used.
- **BBE without the API:** the scraper reads BBE files from the same sparse git snapshot as
  `build-eval` (blob SHAs computed locally, so version keys are unchanged), instead of one
  GitHub API call per file.
- **Graph coverage** counts a GitHub source as matched when `collect-repos` has the file.
- **Fixes:**
  - `Database.one()`/`scalar()` now close their cursor. An unfinished read kept a read
    transaction open, so a later write failed with "database is locked" when another process
    (e.g. `collect-repos`) had committed meanwhile.
  - `apply_decontam` commits per table, so other writers wait less.
- **The report** gets a *Prompts* section with the plan per node, task × difficulty, review
  scores, rejection reasons and 100 accepted prompts. The terminal summary gets a Prompts line.

**Data (no model calls):**
- Full BBE and learn scrape: 593 BBE and 196 learn pages.
- Platform group collected: 435 of 436 module repos.
- Graph grounding: 2,600 of 3,231 items (core 667 of 676), up from 1,227 (412).

**Not run yet:** the live prompt run needs `OPENROUTER_API_KEY` in `.env`.

### M6: verified answers

- **`sftgen answer-prompts`:** the teacher answers each accepted prompt with the item's grounding,
  under an **answer-format contract**:
  - Code goes in ```` ```ballerina ```` blocks, and every block must compile.
  - A multi-file solution gives each file in full in a `// file: <path>` block. This is the plan's
    recommendation for §8 question 1, adopted and also used for codebase tasks.
  - No mention of reference material, and no `Source:` lines.
- **Checks:**
  - **Format:** reference-material leaks fail.
  - **Code:** every block is built with `bal`; `// file:` blocks are built as one package.
  - **Tests:** test files are type-checked by building them as ordinary sources with `bal build`
    (see the M7 fix: `--list-groups` turned out to run module initialisation). They are run only
    when `answers.run_tests` is on, which executes model-written code and is off by default.
  - **Code tasks** must contain code.
  - **Judge:** a judge of another family (enforced) scores `explain`/`design` answers, and any
    answer without code, on correctness, completeness and clarity.
- **Repair loop:** failed answers go back to the teacher with the real problems: compiler errors
  with file:line, failing test names and assertions, or the judge's issues. Up to 3 attempts;
  `--retry-failed` adds a round.
- **Records:** new tables `attempts` (every answer and its checks), `responses` (`verified` ·
  `judged` · `failed`, attempts, repaired) and `preference_pairs` (first failed vs passing
  repair).
- **Outputs:** `data/answers/<set>/sft.jsonl` (chat messages), `pairs.jsonl` (TRL conversational
  DPO) and `failed.jsonl`. Final answers are decontaminated too.
- **`BalCompiler.build_package`:** builds multi-file packages, type-checks tests or runs them,
  and reports errors with file names and failed tests with their assertion messages.
- **The report** gets an *Answers* section (pass rate, first-try rate, repair rate, attempts
  used, failure reasons, common compiler errors, results by task type, failed prompts) and a
  terminal Answers line. Cost per phase is in the usage table (`answer-prompts`,
  `repair-answers`, `judge-answers`).
- **Fix:** the template field for the request is `request`. The name `prompt` collided with
  `LLMClient.chat()`'s own `prompt` parameter, which would have broken every live call.

**Not run yet:** it needs `OPENROUTER_API_KEY` and accepted prompts from M5's live run.

### M7: codebase pool v1

- **`sftgen codebases build`:**
  - **Candidates:** `[package]` projects from collected, licence-safe repos, with no 1.x syntax,
    generated code or eval overlap; 2–40 files, ~0.8k–10k tokens.
  - **Rank:** guides › wso2 › external › platform, tests preferred, at most 3 per repo.
  - **Base must build** with `bal` 2201.14.1. Candidates are tried until `pool_size` (20) are
    active.
  - **Tagging:** graph nodes from imports, and items from qualified names in the code.
  - **Split:** 20% eval by an id hash, kept for M8.
- **States**, each a deterministic edit verified with `bal`:
  - `compile_error`: a misspelled call; must fail to build.
  - `unfinished_feature`: a stubbed function body; must still build, with imports the stub left
    unused dropped.
  - `version_drift`: an HTTP service rewritten in 1.x style; must fail.
  - `failing_test`: a flipped operator. It runs the project's tests, so it's verified only with
    `codebases.run_tests`; otherwise `skipped`.
- **`sftgen codebases verify`** re-checks every base and state and fails if anything is red.
- **`sftgen codebases attach`:**
  - Each verified state of a train codebase becomes a prompt (`context_mode = codebase`,
    `codebase_state_id`), with the real build errors quoted where relevant.
  - **Packing:** the project goes in under `// file:` headers, up to ~14k tokens (Ballerina.toml,
    the target file, other files smallest first, tests last).
  - **Answering:** `answer-prompts` lays the answer's changed files over the state and builds the
    project. A stub that is still there fails.
  - **Planning:** codebase prompts don't count toward the standalone plan.
- **New tables** `codebases` and `codebase_states`, and a `prompts.codebase_state_id` column.
  Files go to `data/codebases/`: `manifest.jsonl`, `<id>/base.tar.gz` and
  `<id>/states/<kind>.patch`.
- **The report** gets a *Codebase pool* section and a terminal Codebases line. The Prompts line
  lists codebase tasks separately.
- **Fix (affects M6):** tests were type-checked with `bal test --list-groups`, which turned out to
  **run module initialisation** (`init()`, module-level initialisers, required configurables). That
  showed up as projects rejected for missing configurable values. Test files are now built as
  ordinary sources with `bal build`, which runs nothing, and error paths are mapped back to
  `tests/…`. A regression test checks that an `init()` side effect doesn't happen.
  - **What ran:** before the fix, the first pool run started on 14 candidate projects. All are
    official or licence-safe repos, run with secrets stripped from the environment. Their module
    initialisation ran under `--list-groups`; no test bodies ran.
- **Resource functions with path parameters** (`get [string id]`) are recognised by the function
  scanner.

**Live run:**
- 251 candidates; 29 tried; 20 build. The 9 rejections are genuine: local multi-package
  dependencies, a module that won't load on 2201.14.1, and an upstream API change.
- Pool: 17 train and 3 eval projects; 19 of 20 have tests; about 82k tokens in total.
- States: 45 verified, 0 failed (20 compile_error, 19 unfinished_feature, 6 version_drift).
  15 failing_test states were skipped because tests aren't run.
- **`codebases verify`: all green.**
- `codebases attach`: 39 codebase prompts (6 eval states held back), up to ~9.6k tokens each,
  all with `covers`.

### M8: eval set v2 and baselines

- **Benchmarks (decided 2026-11-22: translate both):**
  - **`sftgen eval fetch`** pins and sparse-fetches HumanEval (`openai/human-eval`, MIT, 164
    problems) and MBPP (`google-research/google-research`, test split 11–510, 500 problems). It
    detects the repo licences, records both in the licence register (intended use
    `eval-benchmark`, with MBPP's CC BY 4.0 attribution duty), and stores the problems in the new
    `benchmark_problems` table.
  - **`sftgen eval translate`:** the teacher writes a Ballerina prompt with the signature, a
    reference solution and `bal` tests. These must build together (tests run with
    `eval.run_tests`), with repairs from the real errors. Translations become fingerprinted eval
    items.
- **`sftgen eval tasks`** (no model calls): eval-split codebase states become fix, implement and
  migrate tasks, plus an explain task per codebase. The 188 BBE eval items get prompts built from
  their explanation and expected output.
- **`sftgen eval ecosystem`:** the teacher writes tasks per core graph node with a reference; a
  code reference must compile.
- **`sftgen eval run --model base|teacher`:**
  - **Sampling:** `eval.samples` answers per item, using the training system prompt; each sample
    is a separately cached call with a seed (new `sample` argument to `LLMClient.chat`).
  - **Scoring** by suite: benchmark tests, BBE output, codebase overlay build, compile, or the
    judge for explain tasks.
  - **Summaries:** unbiased pass@k per suite and per graph item. Runs resume per sample.
  - **Modes:** `compile` vs `test` (code executed only with `eval.run_tests`) is recorded per run,
    and runs of different modes are never mixed.
- **New tables** `benchmark_problems`, `eval_runs` and `eval_results`; `eval_items` gains `prompt`,
  `task_type`, `reference` and `meta`; the lockfile gains a `benchmarks` section.
- **The report** gets *Eval set v2 and baselines*: items per suite, translation status, runs with
  pass@k per suite, and the **per-item score map** (pass@1 per graph item per run).
- **Fix:** the translation template's literal `@test:Config {}` broke Python formatting. A new
  test formats every template, so stray braces fail in CI.

**Run without a key:** HumanEval (164) and MBPP (500) fetched and registered; 9 codebase eval tasks
(3 eval codebases); 188 BBE prompts. **Not run yet:** `eval translate`, `eval ecosystem` and the
baselines need `OPENROUTER_API_KEY`.

### Tests

118 tests in total (+7 for M8: pass@k; problem parsing and the MBPP split; fetch with licences,
register and lockfile; translation with a repair, saved item and fingerprints; codebase and BBE
tasks plus a full eval run with pass@k, judge, resume, a second model and the score map; unknown
model labels; every prompt template formats).

M7: 111 tests in total (+5 for M7: the function scanner and each state edit; context packing order and
budget; pool candidates, tags and split; build, verify, attach and codebase answers with a fake
compiler, the report section, and the plan staying standalone; real `bal` state verification and
type-checking tests without running module init, skipped without `bal`).

M6: 106 tests in total (+4 for M6: code blocks and `// file:` packages, missing code and compiler
feedback; real `bal` checks of a multi-file package with tests, compiled and then run, skipped
without `bal`; the answer loop with compile, leak, missing-code and judge repairs, preference
pairs, outputs, the report section and `--retry-failed`; the different-family rule).

M5: 102 tests in total (+5 for M5: task mix and slots; symbol parsing and lookup, FTS5 search and
grounding from pages, symbols and collected repo files; plan totals and unknown nodes; the
writer/filter/reviewer loop with gap-fill, outputs and the report section, using a fake model
client; the different-family rule).

M4: 97 tests in total (+8 for M4: containment, exact and near matches and boilerplate; incremental
re-checks, held-out chunks and clearing; the split rule; parsing `index.json`; check modes and
output comparison; a real `bal run`/`bal build` check, skipped without `bal`; and `build-eval`
end to end, including a decontaminated export and a retired item).

M3: 89 tests (+9 for M3: licence detection and verdicts, the pre-gate, the register,
fingerprints and LSH; a collect run against local git repos covering the licence gate, sparse
snapshot, lockfile, register, tarball, Swan Lake classification, exact and near dedup, re-run
skips and the clone limit).

M2: 80 tests (+8 for M2: bucket layout, change-only uploads, pull to a fresh machine,
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
