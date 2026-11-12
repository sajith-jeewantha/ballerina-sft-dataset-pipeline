# Ballerina LLM Pipeline

Builds a **traceable, compiler-verified supervised fine-tuning (SFT) dataset** for a Ballerina
small language model. It collects official Ballerina sources, cleans them into Markdown, pairs
code with its explanation, and generates and filters chat-format training data.

* Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) (or open [architecture.html](architecture.html))
* Design rationale: [docs/recommendations.md](docs/recommendations.md) · Record schemas: [docs/schemas.md](docs/schemas.md)
* What changed: [CHANGELOG.md](CHANGELOG.md)

```
collect (GitHub · Central API · blog sitemap) → data/raw
  → clean → extract → chunk → index → generate → validate → judge → export → data/sft/<run>/{train,eval}.jsonl
```

## Repository layout

| Path | What it is |
|---|---|
| `shared/` | `balpipe_common`: schemas, config, logging, JSONL I/O, manifests, IDs (used by both projects) |
| `scraper/` | **collect** stage: GitHub + Central API collectors, Scrapy sitemap crawler → `data/raw/` |
| `sft-dataset-generator/` | `sftgen` CLI: clean, extract, chunk, index, generate, validate, judge, export |
| `config/pipeline.yaml` | All non-secret settings (sources, politeness, chunking, quotas, thresholds) |
| `.env` | Secrets + model names (copy from `.env.example`; never committed) |
| `data/` | All pipeline data (gitignored; reproducible from upstream) |

## Setup

Requirements: Python 3.11+, and the Ballerina CLI (`bal`, validated with 2201.14.x) for compile checks.

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e shared -r scraper/requirements.txt -e scraper \
            -r sft-dataset-generator/requirements.txt -e sft-dataset-generator -r requirements-dev.txt
cp .env.example .env    # then fill in OPENROUTER_API_KEY and the model names
bal version
```

> **Migrating from the old layout:** the pipeline still reads `sft-dataset-generator/.env` and
> `scraper/ballerinascraper/.env` (with a warning), and still accepts the old `API_KEY` /
> `BUCKET_NAME` names. Move those keys into the root `.env` when convenient.

Choose `JUDGE_MODEL` from a **different model family** than `ANSWER_GEN_MODEL` (a warning is
logged otherwise; set `judge.strict_family_check: true` to make it an error).

## Run

```bash
# 1. collect (polite: robots.txt, one honest User-Agent, rate limits; unchanged content is skipped)
python -m ballerinascraper.collect github
python -m ballerinascraper.collect central
(cd scraper/ballerinascraper && scrapy crawl web)

# 2. process
sftgen clean && sftgen extract && sftgen chunk && sftgen index

# 3. generate → validate → judge → export
sftgen generate --dry-run          # shows the task plan, no model calls
sftgen generate                    # prints the run id; data/sft/<run_id>/
sftgen validate && sftgen judge && sftgen export     # default to the latest run

# or all of step 2 + 3:
sftgen all
```

Useful flags: `--limit N` (small test), `--run-id <id>` (resume / target a run), `--force`
(index: rebuild), `--log-format text` (human-readable logs; default is JSON).

Outputs: `data/sft/<run_id>/train.jsonl` and `eval.jsonl` (`{"id", "messages"}` — directly
loadable by TRL, Axolotl and Unsloth) and `metadata.jsonl` (full provenance, scores, checks and
the accept/reject decision for every candidate).

## Resuming after a failure

Just re-run the same command. Every stage writes a `_manifest*.json` (status, inputs, config
hash, versions, counts, item errors); generate/validate/judge append one fsync'ed line per item
and skip IDs already done; LLM responses and compile results are cached under `data/cache/`.
Batch stages write atomically. See ARCHITECTURE.md §9.

## Configuration

`config/pipeline.yaml` is documented inline. Any key can be overridden by environment variable,
e.g. `BALPIPE__GENERATE__TARGET_RECORDS=50 sftgen generate`. Precedence:
environment > `.env` > `pipeline.yaml` > defaults. Secrets are never logged or written to manifests.

## Tests

```bash
pytest shared/tests scraper/ballerinascraper/tests sft-dataset-generator/tests
```

Covers cleaning (extractors, code-fence language labelling, relevance, dedup), chunking (fences
never split, token budgets, heading paths, overlap), code/description pairing, Ballerina
heuristics, compiler helpers (plus a real `bal` round-trip when installed), mutations, task
planning, static checks, export decisions, config precedence, JSONL durability and manifests.
