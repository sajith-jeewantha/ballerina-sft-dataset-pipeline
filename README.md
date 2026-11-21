# Ballerina SFT Dataset Pipeline

[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB.svg)
![Ballerina 2201.14](https://img.shields.io/badge/ballerina-2201.14.x-20b6b0.svg)
[![Architecture](https://img.shields.io/badge/docs-architecture-2f6f5e.svg)](https://sajith-jeewantha.github.io/ballerina-sft-dataset-pipeline/architecture.html)

Builds a **grounded question-and-answer dataset** for fine-tuning a small language model on the
[Ballerina](https://ballerina.io) programming language.

It scrapes official Ballerina sources, splits them into heading-aware chunks, generates questions
from each chunk, and answers every question from **the same chunk**, citing its source URL. The
`bal` compiler can then check the code in the answers. Every step is tracked in one SQLite
database, and an HTML report shows progress after every command.

**🔗 Interactive architecture page:** https://sajith-jeewantha.github.io/ballerina-sft-dataset-pipeline/architecture.html
**📦 Repository:** https://github.com/sajith-jeewantha/ballerina-sft-dataset-pipeline

---

## Contents

- [How it works](#how-it-works)
- [Data sources](#data-sources)
- [Phases and commands](#phases-and-commands)
- [Limits and small test runs](#limits-and-small-test-runs)
- [The report](#the-report)
- [Output format](#output-format)
- [Storage](#storage)
- [Repository structure](#repository-structure)
- [Quick start: step by step](#quick-start-step-by-step)
- [Configuration](#configuration)
- [Re-running and resuming](#re-running-and-resuming)
- [Tests](#tests)
- [Documentation](#documentation)
- [License](#license)

---

## How it works

```mermaid
flowchart LR
    subgraph SRC["Official sources"]
        IO[("ballerina.io<br/>sitemap: learn · spec 2024R1")]
        GH[("GitHub API<br/>BBE sources · module repos")]
        CEN[("Ballerina Central API<br/>README · API docs")]
    end

    subgraph SCRAPE["sftgen scrape"]
        F[fetch<br/>robots.txt · delay · cache] --> C[clean → Markdown] --> K[chunk by headings]
    end

    subgraph GEN["generation"]
        Q[generate-questions] --> A[generate-answers] --> V[validate<br/>bal build]
    end

    IO & GH & CEN --> F
    K --> Q
    V --> E[export]
    V --> ES[export-sft]
    E --> OUT[/"data/dataset.jsonl"/]
    ES --> SFT[/"data/sft/train.jsonl · eval.jsonl"/]

    DB[("data/pipeline.db<br/>pages · chunks · questions<br/>answers · runs · llm_usage")]
    SCRAPE <--> DB
    GEN <--> DB
    DB --> R[/"data/reports/report.html"/]
```

```mermaid
flowchart LR
    P[page] -->|clean + split| CH[chunk]
    CH -->|QUESTIONS_PER_CHUNK| QN[question<br/>pending]
    QN -->|question + same chunk| AN[answer]
    AN --> S{{"question: answered / failed<br/>answer: valid / invalid / not_checked"}}
```

**Principles:** official sources only · answers are grounded in the chunk the question came from
and cite its URL · every record has a status and timestamps · small runs are cheap and
re-runs never repeat finished work.

Full design: [ARCHITECTURE.md](ARCHITECTURE.md) · [interactive page](https://sajith-jeewantha.github.io/ballerina-sft-dataset-pipeline/architecture.html)

---

## Data sources

Only these sources are used. The blog is **not** used.

| Source | What is collected | How |
|---|---|---|
| [Ballerina by Example](https://ballerina.io/learn/by-example/) (highest priority) | Each example's explanation, code and expected output, **exactly as written** | Page list from the ballerina.io sitemap; content from `ballerina-distribution/examples/<name>/` through the GitHub API. The ballerina.io page only ships the explanation inside compiled JavaScript, so the source files are the faithful copy. |
| [Learn docs](https://ballerina.io/learn/) | Guides, tools, packages, tutorials, integration patterns | ballerina.io sitemap → each page's embedded Markdown source (exact text and code fences) |
| [Language spec 2024R1](https://ballerina.io/spec/lang/2024R1/) | The full spec, split by section headings, grammar kept as ```` ```ebnf ```` | One HTML page → structure-preserving Markdown |
| [Ballerina Central](https://central.ballerina.io/) | Package READMEs and API docs (function, client and type signatures) | Central's public JSON API: `/2.0/registry/packages` and `/2.0/docs/<org>/<pkg>/<version>`. No HTML scraping is needed. |
| [GitHub modules](https://github.com/orgs/ballerina-platform/repositories) | Module READMEs and `.bal` API source files from `module-ballerina-*` and `module-ballerinax-*` repos | GitHub REST API with `GITHUB_TOKEN`, most-starred repos first |

From the sitemap, only `/learn/`, `/learn/by-example/` and `/spec/lang/2024R1/` are kept. Old
version paths, robots-disallowed paths, index pages and duplicate URLs are skipped.

**Scraping rules:** one honest User-Agent, robots.txt respected for web pages,
`REQUEST_DELAY_SECONDS` between requests, raw pages cached in the database, unchanged content
skipped by blob SHA, package version, HTTP 304 or content hash, and failures logged per page
without stopping the run.

---

## Phases and commands

| Command | What it does | Status it sets |
|---|---|---|
| `sftgen scrape` | Fetch up to `LIMIT_SCRAPE_PAGES` new pages per source, convert them to clean Markdown and split them into heading-aware chunks | page: `chunked` · `empty` · `duplicate` · `blocked` · `failed` |
| `sftgen generate-questions` | For chunks with no questions yet, generate `QUESTIONS_PER_CHUNK` each, until `LIMIT_GENERATING` questions are saved. Each question is saved as soon as it is generated, with its chunk, source URL, category and difficulty. Near-duplicates are skipped. | question: `pending` |
| `sftgen generate-answers` | Answer every `pending` question using the question **and its saved chunk**. Answers include working Ballerina code when relevant, must not invent APIs, and cite the source URL. | question: `answered` · `failed` (with error) |
| `sftgen validate` *(optional)* | Compile the ```` ```ballerina ```` blocks in each answer with `bal` if it is installed | answer: `valid` · `invalid` · `not_checked` |
| `sftgen export` | Write `data/dataset.jsonl` for review (answers marked `invalid` are left out) | — |
| `sftgen export-sft` | Write fine-tuning files in chat `messages` format: `data/sft/train.jsonl`, `eval.jsonl`, `metadata.jsonl` | — |
| `sftgen report` | Rebuild `data/reports/report.html` and print the summary | — |
| `sftgen run-all` | `scrape` → `generate-questions` → `generate-answers` → `validate` → `export` → `export-sft` | — |
| `sftgen graph validate` | Check `graph/graph.json`: schema 0.2 and the graph rules from [ballerina-ecosystem](https://github.com/sajith-jeewantha/ballerina-ecosystem), plus the hash recorded in `graph/SOURCE.json` | — |
| `sftgen graph stats` | Nodes, items and edges by type, priority, status and difficulty | — |
| `sftgen graph sync` | Load the graph into the database and match each item's sources to scraped pages (also runs after every `scrape`) | item: `grounded` · `not_scraped` · `external_only` · `retired` |
| `sftgen collect-repos` | Snapshot whole Ballerina repositories: the platform's module repos, `ballerina-guides`, wso2 and permissive external repos. Licence-gated, pinned, Swan Lake filter, dedup; up to `LIMIT_REPOS` clones per run. See [Code repositories](#code-repositories) | repo: `collected` · `excluded` · `licence_check` · `failed`; file: `kept` · `duplicate` · `near_duplicate` · `legacy` · `too_large` · `context` |
| `sftgen storage status` | Compare the local data folder with the bucket: files not pushed, changed since the last push, or only in the bucket | — |
| `sftgen storage push` | Upload new or changed outputs and a database snapshot (commands already do this when `STORAGE_BUCKET=true`) | — |
| `sftgen storage pull` | Download what's missing locally: the newest database snapshot and the outputs. `--overwrite` replaces local files; the local database is backed up first | — |

Flags:
- `scrape --refresh`: re-check downloaded pages for changes.
- `scrape --update-lock`: re-resolve the pinned versions in `config/sources.lock.json`.
- `scrape --source bbe`: one source only (repeatable).
- `collect-repos --group guides`: one group only (repeatable). `--repo owner/name` adds one repo.
  `--refresh` re-clones. `--update-lock` re-pins to the latest default-branch commit.
- `generate-answers --retry-failed`
- `validate --recheck`
- `storage pull --overwrite`
- `--log-format text|json`

### Pinned sources

Everything is pinned to **one Swan Lake update**: `sources.swan_lake`, default `2201.14.1`.
The resolved pins are written to **`config/sources.lock.json`** the first time each source is
scraped, and reused on every later run:

| Source | Pinned to |
|---|---|
| Ballerina by Example | The `ballerina-distribution` release tag `v<swan_lake>`, resolved to a commit |
| Module repos | The commit seen on the first scrape |
| Ballerina Central | Per package, the newest version built for the pinned update or earlier (`ballerinaVersion` ≤ pin) |
| Spec | `2024R1` (the version is in the URL) |
| Learn pages | Not pinnable (live site); each page's content hash is kept in the database |

- **When a pin changes,** the page is downloaded again, even without `--refresh`.
- **To move to a new Swan Lake update,** change `sources.swan_lake`. A fresh lock starts, and
  the old one stays in git history.
- **The teacher's provider is pinned too.** Models whose id starts with `deepseek/` are sent
  to DeepSeek's own endpoint only (`generate.providers`), because DeepSeek's terms explicitly
  allow distillation. Each call records the provider that served it in `llm_usage.provider`.

Question categories: `concept`, `syntax`, `library`, `tooling`, `error-fixing`, `code-writing`,
`best-practice`. Difficulties: `beginner`, `intermediate`, `advanced`.

---

## Limits and small test runs

Set these in `.env`. Every command prints them at startup, so you can confirm what will run:

| Variable | Default | Meaning |
|---|---|---|
| `LIMIT_SCRAPE_PAGES` | `20` | Max pages to download **per source** per run. `0` = no limit. |
| `LIMIT_GENERATING` | `10` | Max questions to generate per run. `0` = no limit. |
| `QUESTIONS_PER_CHUNK` | `3` | Questions generated from each chunk |
| `REQUEST_DELAY_SECONDS` | `1` | Polite delay between requests |
| `LIMIT_REPOS` | `20` | `collect-repos`: max repositories cloned per run. `0` = no limit. Repos excluded by licence without cloning don't count. |
| `GITHUB_TOKEN` | — | GitHub API token: 5,000 requests/hour instead of 60 |

The answer phase answers every saved question that has no answer yet, so it is naturally limited
by `LIMIT_GENERATING`. With the defaults, one `run-all` downloads up to 100 pages (20 × 5
sources), generates 10 questions and answers them. Chunks are taken round-robin across sources,
so even 10 questions cover several sources. Each run continues where the last one stopped.

```
sftgen scrape
  database               data/pipeline.db
  LIMIT_SCRAPE_PAGES     20 pages per source per run
  LIMIT_GENERATING       10 questions per run
  QUESTIONS_PER_CHUNK    3
  REQUEST_DELAY_SECONDS  1
  GITHUB_TOKEN           set
  question model         qwen/qwen3.8-flash
  answer model           deepseek/deepseek-v4.1-flash
  Swan Lake pin          2201.14.1 (lock: config/sources.lock.json)
  storage                local only (STORAGE_BUCKET=false)
```

---

## The report

After every command a short summary is printed, and `data/reports/report.html` is rewritten. It's a
single file with no server needed:

```bash
open data/reports/report.html          # macOS
xdg-open data/reports/report.html      # Linux
start data\reports\report.html         # Windows
```

It shows:

- pages scraped: total, per source and per status, plus failed pages with their URLs and errors
- chunks created per source
- questions: total, and per source, category and difficulty
- answers: answered, pending and failed
- validation results
- run history: start time, duration, limits used and items processed
- token usage and cost per phase (OpenRouter returns the cost of each call)
- **knowledge-graph coverage:**
  - how many of the graph's 3,231 active items are grounded by a scraped page (or spec
    section), by priority and node type
  - which source pages to scrape next
  - the core items that are not grounded yet
- **code repositories:**
  - repos and unique `.bal` files per group
  - duplicates, legacy files and generated files
  - estimated tokens
  - licences of collected repos
  - the repos that need a manual licence check
- 5 random Q&A samples, so you can spot-check quality

Terminal summary (illustrative numbers):

```
── Summary ─────────────────────────────────────────────────
Pages      9 total, 0 failed  |  usable: bbe 2, spec 1, learn 2, central 2, github 2
Chunks     1272  |  bbe 6, spec 351, learn 7, central 877, github 31
Questions  10  |  pending 0, answered 10, failed 0
Validation valid 6, not_checked 3, invalid 1
LLM usage  48,210 tokens, $0.0123
Report     data/reports/report.html
```

---

## Output format

### `data/dataset.jsonl` (review format, from `export`)

One object per line:

```json
{"question": "How do I access a nested field of a json value in Ballerina?",
 "answer": "Field access works directly on `json` because it is a lax type ...\n\n```ballerina\nimport ballerina/io;\n...\n```\n\nSource: https://ballerina.io/learn/by-example/access-json-elements/",
 "source_url": "https://ballerina.io/learn/by-example/access-json-elements/",
 "category": "code-writing",
 "difficulty": "beginner"}
```

### `data/sft/` (fine-tuning format, from `export-sft`)

`train.jsonl` and `eval.jsonl` hold chat `messages`. They load directly in **TRL**
`SFTTrainer`, **Axolotl** (`type: chat_template`) and **Unsloth**:

```json
{"id": "q42",
 "messages": [
   {"role": "system", "content": "You are an expert Ballerina (Swan Lake) programmer. Answer questions about ... say so instead of guessing."},
   {"role": "user", "content": "How do I access a nested field of a json value in Ballerina?"},
   {"role": "assistant", "content": "Field access works directly on `json` because it is a lax type ...\n\n```ballerina\n...\n```"}]}
```

- **Any base model:** the files hold no model-specific template. The trainer applies the model's
  chat template (`tokenizer.apply_chat_template`). A base model has no template of its own, so
  set one (for example ChatML or its instruct sibling's), use the same one at inference, and
  compute the loss on the assistant turn only.
- **Train/eval split:** by source page (`sft.eval_fraction`, default 5%), so no page feeds both
  sides.
- **Citations removed:** the trailing `Source: <url>` line is dropped by default
  (`sft.strip_source_line`), so the model doesn't learn to end answers with URLs it would invent.
- **Metadata:** `metadata.jsonl` keeps `id`, `split`, `source_url`, `category`, `difficulty`
  and `validation` for filtering and analysis.
- **System prompt:** set by `sft.system_prompt`; use `""` for none. It tells the model to answer
  accurately and concisely, write complete Ballerina that compiles with its imports, and say when it
  is unsure instead of inventing APIs. It's in every training row, so **use exactly the same text
  at inference**. If you change it, re-run `sftgen export-sft`; nothing else needs regenerating.

---

## Storage

A single SQLite file, `data/pipeline.db`. Every row has a `status`, `created_at` and `updated_at`.

| Table | One row per | Statuses |
|---|---|---|
| `pages` | source page or file: raw cache, clean Markdown, hashes, errors | `fetched` · `chunked` · `empty` · `duplicate` · `blocked` · `failed` |
| `chunks` | heading-aware piece of a page | `pending` · `questioned` · `too_small` · `failed` · `stale` |
| `questions` | generated question, with chunk, source URL, category, difficulty | `pending` · `answered` · `failed` |
| `answers` | answer to a question | `not_checked` · `valid` · `invalid` |
| `runs` | command run: limits, items processed, duration | `running` · `succeeded` · `failed` |
| `llm_usage` | model call: phase, tokens, cost, serving provider | `ok` · `error` |
| `graph_items` | knowledge-graph item (`node#item`), with priority, difficulty and coverage | `grounded` · `not_scraped` · `external_only` · `retired` |
| `item_sources` | an item's source URL matched to a scraped page (or spec section) | `matched` · `not_scraped` · `external` |

Inspect it with any SQLite tool, e.g. `sqlite3 data/pipeline.db "SELECT status, COUNT(*) FROM questions GROUP BY 1"`.
Column reference: [docs/schemas.md](docs/schemas.md).

### Code repositories

`sftgen collect-repos` adds whole repositories, for the raw code corpus and the codebase pool
in later milestones. Groups are set in `repos.groups` in `pipeline.yaml`; earlier groups win
when files are duplicated:

| Group | Repos |
|---|---|
| `platform` | `ballerina-platform/module-ballerina-*` and `module-ballerinax-*` |
| `guides` | every `ballerina-guides` repo |
| `wso2` | `wso2` repos whose main language is Ballerina |
| `external` | other Ballerina repos pushed since Swan Lake GA (2022-02), most-starred first, not forks or archived |

For each repo:

1. **Licence pre-gate.** If GitHub reports no licence or a copyleft licence, the repo is
   excluded without downloading anything.
2. **Pin.** The default branch is pinned to a commit with `git ls-remote` (no API quota) in
   `config/sources.lock.json`.
3. **Snapshot.** A blobless, shallow, sparse clone downloads only `*.bal`, `Ballerina.toml`,
   `*.out`, READMEs and the root LICENSE.
4. **Licence gate on the LICENSE text.**
   - `safe` (a permissive licence that agrees with GitHub): the files are kept.
   - `check` (no file, unrecognised text, or a mismatch): the repo waits for a person.
   - `exclude` (copyleft, or no licence at all).
5. **Classify each `.bal` file** as `swan_lake`, `legacy` (1.x) or `unknown`, using the
   project's `distribution`, the `Ballerina.toml` layout and syntax markers. Generated files are
   flagged so later steps can down-weight them.
6. **Dedup** across all repos: an exact match on code without comments or whitespace, then
   MinHash near-duplicates (similarity ≥ 0.8).

Files are stored in `repo_files`, and a `.tar.gz` snapshot of each repo goes to
`data/repos/`.

**The licence register** is `docs/licence-register.csv`. It has one row per repo reviewed:
licence, verdict, pinned commit, sha256 of the LICENSE file and file count. Unlicensed search
results stay in the database only. People can edit it:
- **Block a repo:** set its row to `exclude`.
- **Allow a `check` repo after reading its licence:** set the row to `safe` and start `notes`
  with `verified:`.

### Local folder and cloud bucket

Everything the pipeline writes lives under **`LOCAL_DATA_DIR`** (default `data/`). That folder is
always the working copy, because SQLite and `bal build` need a real disk. Set
`STORAGE_BUCKET=true` to keep a copy in a GCS or S3 bucket **with the same layout**:

| Local (`data/…`) | Bucket (`gs://BUCKET/PREFIX/…`) |
|---|---|
| `dataset.jsonl`, `sft/*.jsonl`, `reports/report.html` | same paths |
| `pipeline.db` | `db/pipeline-<UTC time>.db.gz` snapshots; `db/LATEST` names the newest |
| `config/sources.lock.json` (repo) | `sources/sources.lock.json` |
| `graph/graph.json`, `graph/SOURCE.json` (repo) | `graph/<graph commit>/graph.json`, `SOURCE.json` |
| `cache/` (HTTP, model and compiler caches) | never uploaded |

With the bucket on, every command:
1. **Restores** the database from `db/LATEST` if there's no local `pipeline.db` (for example on a
   new machine or cloud VM).
2. **Uploads** each phase's new or changed files as soon as the phase succeeds. Unchanged files
   are skipped.
3. **Uploads a database snapshot** at the end. The 20 newest are kept (`storage.keep_db_snapshots`).

If an upload fails, the command still saves everything locally and exits with code 1; run
`sftgen storage push` to retry. Only one machine should write to a bucket prefix at a time.

**Set it up** in `.env`:

```env
STORAGE_BUCKET=true
STORAGE_PROVIDER=gcs        # or aws-s3
BUCKET_NAME=ballerina-slm-data
BUCKET_PREFIX=              # optional sub-folder
```

- **GCS:** run `pip install -e "shared[gcs]"` and `gcloud auth application-default login`.
- **AWS S3:** run `pip install -e "shared[s3]"` and set `AWS_PROFILE` (from `~/.aws/credentials`)
  and `AWS_REGION`.
- **S3-compatible stores** such as Cloudflare R2: use `aws-s3` and also set `S3_ENDPOINT_URL`.
- No keys go in `.env`.
- To try the layout without a cloud account, set `STORAGE_PROVIDER=local` and
  `BUCKET_NAME=/some/folder`.

---

## Repository structure

```
ballerina-sft-dataset-pipeline/
├── README.md · ARCHITECTURE.md · architecture.html · CHANGELOG.md · LICENSE
├── .env.example                  # secrets, models and run limits template
├── config/pipeline.yaml          # all non-secret settings
├── docs/
│   ├── recommendations.md        # design research (v2; partly superseded, see note inside)
│   └── schemas.md                # SQLite tables and columns
├── shared/balpipe_common/        # config · db (SQLite) · graph · lock · storage · licences · dedup · ids · logging
├── scraper/ballerinascraper/
│   └── ballerinascraper/
│       ├── scrape.py             # fetch loop: limits, caching, change detection, failures
│       ├── repos.py              # collect-repos: licence gate, sparse snapshots, Swan Lake filter, dedup
│       └── collectors/           # ballerina_io (sitemap) · github · central · http · robots
├── sft-dataset-generator/
│   ├── src/sftgen/
│   │   ├── cli.py                # the commands
│   │   ├── clean/                # extractors (page → Markdown) · question dedup
│   │   ├── chunk/                # heading-aware chunker
│   │   ├── generate/             # questions · answers · LLM client
│   │   ├── validate/             # bal compiler wrapper
│   │   ├── export/               # dataset.jsonl · sft (chat messages)
│   │   ├── report.py
│   │   └── prompts/              # questions.yaml · answer.yaml
│   └── tests/
└── data/                         # gitignored (LOCAL_DATA_DIR): pipeline.db · dataset.jsonl · sft/ ·
                                  #   reports/report.html · cache/ · .storage/ (push bookkeeping)
```

---

## Quick start: step by step

Everything from an empty machine to a fine-tuning dataset. Run the commands from the repo root.

### 0. Requirements

- **Python 3.11+**
- **An [OpenRouter](https://openrouter.ai) API key**, or any OpenAI-compatible endpoint
- **A GitHub token** (recommended): a fine-grained token with read-only access to public
  repositories is enough
- **Optional:** the [Ballerina CLI](https://ballerina.io/downloads/) (`bal`, tested with
  2201.14.x), needed only for `validate`

### 1. Get the code

```bash
git clone https://github.com/sajith-jeewantha/ballerina-sft-dataset-pipeline.git
cd ballerina-sft-dataset-pipeline
```

Already cloned? Update it instead:

```bash
git pull
```

### 2. Create a virtual environment and install

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e shared -r scraper/requirements.txt -e scraper \
            -r sft-dataset-generator/requirements.txt -e sft-dataset-generator -r requirements-dev.txt
```

> If you renamed or moved the project folder, delete `.venv` and run this step again: the
> virtual environment stores absolute paths.

### 3. Configure `.env`

```bash
cp .env.example .env
```

Open `.env`, fill these in and **save the file**:

```env
OPENROUTER_API_KEY=sk-or-...
QUESTION_GEN_MODEL=qwen/qwen3.8-flash
ANSWER_GEN_MODEL=deepseek/deepseek-v4.1-flash
GITHUB_TOKEN=github_pat_...

LIMIT_GENERATING=10        # small first run
LIMIT_SCRAPE_PAGES=20
QUESTIONS_PER_CHUNK=3
REQUEST_DELAY_SECONDS=1
```

### 4. Check the setup

```bash
sftgen report       # prints the limits, models and whether GITHUB_TOKEN is set; creates data/pipeline.db
bal version         # optional, for validate
pytest shared/tests scraper/ballerinascraper/tests sft-dataset-generator/tests
```

The startup block must show your models and `GITHUB_TOKEN set`. If it says `not set`, the `.env`
file wasn't saved or the value is empty.

### 5. Run everything with one command

```bash
sftgen run-all
```

This runs `scrape` → `generate-questions` → `generate-answers` → `validate` → `export` →
`export-sft`, then prints the summary and writes the report. With the limits above it downloads
up to 100 pages and generates and answers 10 questions, for a few cents of model calls.

### 6. Or run each phase yourself

Use this when you want to check each step before the next one:

```bash
sftgen scrape                # 1. fetch pages → clean Markdown → chunks
sftgen generate-questions    # 2. up to LIMIT_GENERATING new questions
sftgen generate-answers      # 3. answer every pending question from its chunk
sftgen validate              # 4. optional: compile the Ballerina code in answers
sftgen export                # 5. data/dataset.jsonl (review format)
sftgen export-sft            # 6. data/sft/train.jsonl, eval.jsonl, metadata.jsonl (fine-tuning)
```

Each command prints its limits first and a summary at the end, and refreshes the report.

### 7. Check the results

```bash
open data/reports/report.html        # macOS; Linux: xdg-open; Windows: start
head -n 3 data/dataset.jsonl
head -n 1 data/sft/train.jsonl
sqlite3 data/pipeline.db "SELECT status, COUNT(*) FROM questions GROUP BY 1"
```

In the report, read the **Sample Q&A** section and check the **Failed pages** table before you
scale up.

### 8. Scale up

When the samples look good, raise the limits in `.env` (for example `LIMIT_GENERATING=200`, or
`0` for no limit) and run again:

```bash
sftgen run-all
```

Every run continues where the last one stopped. Pages already downloaded aren't fetched again,
chunks that already have questions are skipped, and only `pending` questions are answered.

### 9. Fine-tune

Point your trainer at `data/sft/train.jsonl` and `data/sft/eval.jsonl`: TRL `SFTTrainer`,
Axolotl (`type: chat_template`) or Unsloth. See [Output format](#output-format).

### Useful extra commands

| Command | When |
|---|---|
| `sftgen scrape --source bbe` | Scrape one source only (`bbe`, `spec`, `learn`, `central`, `github`; repeatable) |
| `sftgen scrape --refresh` | Re-check pages you already have for upstream changes |
| `sftgen scrape --update-lock` | Re-resolve the pinned commits and Central versions in `config/sources.lock.json` |
| `sftgen graph validate` | Check the knowledge graph before using it |
| `sftgen collect-repos --group guides` | Collect one repo group; `LIMIT_REPOS=0` for all of it |
| `sftgen collect-repos --repo owner/name` | Collect one repository |
| `sftgen storage status` | See what's local only, changed, or only in the bucket |
| `sftgen storage pull` | On a new machine: get the database and outputs from the bucket |
| `sftgen generate-answers --retry-failed` | Retry questions whose answer call failed |
| `sftgen validate --recheck` | Re-compile all answers, e.g. after installing a new `bal` |
| `sftgen report` | Rebuild the report and summary at any time |
| `sftgen <command> --log-format json` | Machine-readable logs |
| `LIMIT_GENERATING=5 sftgen generate-questions` | Override a limit for one command |
| `rm -rf data` | Start over from scratch. This deletes the database, so any generated questions and answers are lost. The model response cache in `data/cache/llm` is deleted too. |

---

## Configuration

| Where | What | Committed? |
|---|---|---|
| `.env` (copy `.env.example`) | `OPENROUTER_API_KEY`, `LLM_BASE_URL`, `QUESTION_GEN_MODEL`, `ANSWER_GEN_MODEL`, `GITHUB_TOKEN`, the run limits above, and the storage switch (`STORAGE_BUCKET`, `STORAGE_PROVIDER`, `BUCKET_NAME`, `BUCKET_PREFIX`, `LOCAL_DATA_DIR`, `AWS_*`, `S3_ENDPOINT_URL`) | ❌ never |
| `config/pipeline.yaml` | source filters, crawl politeness, chunk size, concurrency, temperatures, compiler settings, export, SFT export (eval share, system prompt, citation stripping), report, and bucket snapshot settings. Output paths are relative to `LOCAL_DATA_DIR` | ✅ |

- Precedence: **environment > `.env` > `pipeline.yaml` > defaults**. Empty values in `.env` are
  ignored. Override any YAML key with `BALPIPE__SECTION__KEY`, e.g. `BALPIPE__GENERATE__CONCURRENCY=2`.
- Secrets are never logged or stored in the database.
- To keep a laptop cool during `validate`, lower `validate.workers` (default 2).

---

## Re-running and resuming

Just run the command again. Every write is committed immediately, and each phase picks up from
the statuses:

- `scrape` skips pages it already has (`--refresh` re-checks them). The sitemap, repo lists and
  trees are cached for 24 hours. Failed pages are retried.
- `generate-questions` only takes chunks with no questions. A chunk whose call fails is retried
  up to 3 times.
- `generate-answers` only takes `pending` questions (`--retry-failed` adds the failed ones).
- Model responses and compile results are cached in `data/cache/`, so repeated work costs nothing.

---

## Tests

```bash
pytest shared/tests scraper/ballerinascraper/tests sft-dataset-generator/tests
```

The tests cover:

- config and limits
- the SQLite store and run tracking
- robots.txt and sitemap filtering
- the fetch loop: caching, change detection, failures and per-source limits
- every extractor: BBE code kept verbatim, embedded Markdown, the spec, Central API docs
- chunking (code blocks never split, budgets)
- question limits, round-robin, filters and dedup
- answers with citations and failure recording
- export filtering, the SFT `messages` export (split by page, citation removed) and the HTML report
- a real `bal` compile round trip when Ballerina is installed

---

## Documentation

| Document | Contents |
|---|---|
| [Architecture page](https://sajith-jeewantha.github.io/ballerina-sft-dataset-pipeline/architecture.html) | Interactive diagrams and phase explorer |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Components, data flow, storage, reliability |
| [docs/schemas.md](docs/schemas.md) | SQLite tables, columns and statuses |
| [docs/recommendations.md](docs/recommendations.md) | Research behind the v2 design choices |
| [CHANGELOG.md](CHANGELOG.md) | What changed and why |
| [scraper/README.md](scraper/README.md) · [sft-dataset-generator/README.md](sft-dataset-generator/README.md) | Per-project details |

---

## License

The code is licensed under the [Apache License 2.0](LICENSE).

Datasets you generate with it are a separate matter. Their licensing depends on the upstream
sources (the license of each GitHub repo and Central package is stored per page) and on the
terms of the LLMs you use to generate them.
