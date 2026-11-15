# Data model: `data/pipeline.db`

One SQLite file holds everything the pipeline produces. Every table has an integer `id`, a
`status`, and `created_at` / `updated_at` timestamps (UTC, ISO 8601). The schema is created
automatically on first use (`shared/balpipe_common/db.py`).

```mermaid
erDiagram
    pages ||--o{ chunks : "split into"
    chunks ||--o{ questions : "generates"
    questions ||--o| answers : "answered by"
    runs ||--o{ llm_usage : "records"
    pages {
        int id
        text source
        text url
        text status
    }
    chunks {
        int id
        int page_id
        text text
        text status
    }
    questions {
        int id
        int chunk_id
        text question
        text category
        text difficulty
        text status
    }
    answers {
        int id
        int question_id
        text answer
        text status
    }
    runs {
        int id
        text command
        text limits
        text stats
        text status
    }
    llm_usage {
        int id
        int run_id
        text phase
        real cost_usd
        text status
    }
```

---

## `pages`

One row per source page or file.

| Column | Meaning |
|---|---|
| `source` | `bbe` · `spec` · `learn` · `central` · `github` |
| `url` | Public URL (unique). Cited in answers. BBE uses the ballerina.io page URL even though content comes from GitHub. |
| `fetch_url` | What was actually requested (GitHub folder, docs API URL, page URL) |
| `title` | Page title after cleaning |
| `status` | `fetched` (raw stored, not processed yet) · `chunked` · `empty` (no usable content) · `duplicate` (same clean content as another page) · `blocked` (robots.txt) · `failed` |
| `error` | Why it failed, was empty, or is a duplicate |
| `http_status`, `etag`, `last_modified` | Last response; used for conditional GETs |
| `version_key` | Blob SHA(s) or package version. When it is unchanged, the page is skipped without a download. |
| `raw_body`, `raw_hash` | Raw cache: HTML, Markdown, a JSON bundle of BBE files, or compacted Central JSON |
| `markdown`, `content_hash` | Clean Markdown, and its whitespace-insensitive hash (used for duplicate detection) |
| `license` | SPDX id from the GitHub repo or Central package, when known |
| `meta` | JSON: repo/path, package/version, extractor used, origin |
| `fetch_count`, `last_run_id`, `fetched_at` | Fetch bookkeeping |

## `chunks`

Heading-aware pieces of a page's Markdown: the grounding for questions and answers.

| Column | Meaning |
|---|---|
| `page_id`, `source`, `source_url` | Where the chunk came from |
| `ordinal` | Position in the page |
| `heading_path` | e.g. `Ballerina Language Specification, 2024R1 > 5. Values, types and variables > 5.5 Behavioral values` |
| `text`, `text_hash` | Chunk body (code blocks are never split), and its hash. An identical body on another page is skipped. |
| `token_estimate` | Characters ÷ 4; at most `chunk.max_tokens` |
| `has_code` | 1 if the chunk contains a code block |
| `status` | `pending` (needs questions) · `questioned` · `too_small` (under `chunk.min_tokens`, not used) · `failed` (question call failed `max_chunk_attempts` times) · `stale` (no longer on the page) |
| `attempts`, `error` | Question-generation attempts and the last error |

## `questions`

| Column | Meaning |
|---|---|
| `chunk_id`, `source`, `source_url` | The chunk the question was generated from, and the URL its answer must cite |
| `question`, `question_hash` | The question, and a unique hash of its normalized text |
| `category` | `concept` · `syntax` · `library` · `tooling` · `error-fixing` · `code-writing` · `best-practice` |
| `difficulty` | `beginner` · `intermediate` · `advanced` |
| `status` | `pending` · `answered` · `failed` |
| `error` | Why answering failed |
| `model`, `run_id` | Question model and the run that created it |

## `answers`

| Column | Meaning |
|---|---|
| `question_id` | One answer per question |
| `answer` | Markdown answer, ending with `Source: <url>` |
| `model`, `run_id` | Answer model and run |
| `status` | `not_checked` · `valid` (all Ballerina blocks compiled) · `invalid` (a block failed) |
| `validation_detail` | Compiler errors (JSON) for `invalid`; otherwise the reason (`no Ballerina code`, `bal CLI not installed`, `build timed out`, `N block(s) compiled`) |

## `runs`

One row per command run (`run-all` records one per phase).

| Column | Meaning |
|---|---|
| `command` | `scrape` · `generate-questions` · `generate-answers` · `validate` · `export` |
| `status` | `running` · `succeeded` · `failed` |
| `started_at`, `finished_at`, `duration_s` | Timing |
| `limits` | JSON: `LIMIT_SCRAPE_PAGES`, `LIMIT_GENERATING`, `QUESTIONS_PER_CHUNK`, `REQUEST_DELAY_SECONDS` used |
| `stats` | JSON: items processed (pages downloaded/cached/failed, chunks new, questions saved, answers, validation counts, rows exported) |
| `error` | Exception message when the run failed |

## `llm_usage`

One row per model call.

| Column | Meaning |
|---|---|
| `run_id`, `phase`, `model` | Which run, phase (`generate-questions` / `generate-answers`) and model |
| `prompt_tokens`, `completion_tokens` | Token usage reported by the provider |
| `cost_usd` | OpenRouter's reported cost; else computed from `generate.pricing`; else `NULL`. Cached calls are `0`. |
| `cached` | 1 if served from `data/cache/llm` |
| `status` | `ok` · `error` |

---

## `data/dataset.jsonl`

Written by `sftgen export`: one JSON object per answered question, leaving out `invalid` answers
unless `export.include_invalid` is true.

```json
{"question": "...", "answer": "...", "source_url": "https://...", "category": "code-writing", "difficulty": "beginner"}
```

## `data/sft/` (from `sftgen export-sft`)

| File | One line per | Fields |
|---|---|---|
| `train.jsonl`, `eval.jsonl` | answered question (invalid answers left out) | `id` (`q<question id>`), `messages`: optional `system`, then `user` (question) and `assistant` (answer, with the trailing `Source:` line removed when `sft.strip_source_line` is true) |
| `metadata.jsonl` | row in train or eval | `id`, `split`, `source_url`, `source`, `category`, `difficulty`, `validation` |

The split is decided by a hash of `source_url`, so all questions from one page land on the same
side.
