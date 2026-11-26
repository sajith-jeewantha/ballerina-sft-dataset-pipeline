# sft-dataset-generator — `sftgen`

The command-line tool for the whole pipeline. Setup and every command, step by step, are in the
[root README: Quick start](../README.md#quick-start-step-by-step). The design is in
[../ARCHITECTURE.md](../ARCHITECTURE.md) and the plan in [../docs/PLAN.md](../docs/PLAN.md).

```bash
# collect and hold out the eval set (no model calls)
sftgen scrape                     # also runs graph sync and decontamination
sftgen collect-repos              # LIMIT_REPOS repos per run
sftgen build-eval
sftgen codebases build            # then: codebases verify
sftgen corpus
sftgen mine-fixes

# prompts, verified answers, eval set v2 (model calls)
sftgen plan-prompts
sftgen generate-prompts
sftgen codebases attach
sftgen answer-prompts             # --retry-failed for another round of repairs
sftgen eval fetch | translate | tasks | ecosystem | run --model base

# the dataset
sftgen assemble                   # data/datasets/<version>/

# the original Q&A path
sftgen run-all                    # scrape → generate-questions → generate-answers → validate → export → export-sft
sftgen report
```

| Command | Module | What it does |
|---|---|---|
| `scrape` | `ballerinascraper.scrape` → `clean/stage.py` → `chunk/stage.py` | Fetch new pages, extract clean Markdown (`clean/extractors.py`), chunk by headings (`chunk/markdown_chunker.py`), then `graph sync` and decontamination |
| `graph validate · stats · sync` | `balpipe_common.graph`, `graph/coverage.py` | Check `graph/graph.json`; match each item's sources to scraped pages and spec sections |
| `collect-repos` | `ballerinascraper.repos` | Licence-gated, pinned, sparse repo snapshots; Swan Lake filter; dedup; licence register |
| `build-eval` | `eval/bbe.py`, `eval/verify.py` | BBE split 30/70, eval items checked with `bal`, fingerprints, `data/eval/<version>/` |
| `plan-prompts` | `plan/planner.py` | Budgets per graph item, slots over task mix and difficulty, open slots; builds the FTS5 index and symbol table |
| `generate-prompts` | `generate/prompts.py`, `retrieval/` | The writer writes prompts per item from its grounding; filters; the reviewer (another family) scores them |
| `answer-prompts` | `generate/responses.py`, `validate/answer_check.py` | Teacher answers; `bal` build of every block / package, tests type-checked; repairs with real errors; judge for prose; preference pairs |
| `codebases build · verify · attach` | `codebases/` | Pool of verified projects, broken states, codebase-task prompts with the project packed in |
| `eval fetch · translate · tasks · ecosystem · run` | `eval/benchmarks.py`, `tasks.py`, `runner.py` | HumanEval/MBPP in Ballerina, codebase and ecosystem tasks, pass@k baselines |
| `corpus` | `corpus/raw.py` | Raw CPT corpus: parts, manifest, attribution, register totals |
| `mine-fixes` | `corpus/miner.py` | Before/after fixes from repo history |
| `assemble` | `assemble/build.py`, `card.py` | Dataset v1: decontaminate, dedup, balance by item, split; SFT, DPO, CPT, coverage, gaps, card, manifest |
| `generate-questions` | `generate/questions.py` | `QUESTIONS_PER_CHUNK` questions per new chunk, round-robin across sources, up to `LIMIT_GENERATING`; near-duplicates skipped (`clean/dedup.py`) |
| `generate-answers` | `generate/answers.py` | Answer `pending` questions from their own chunk, up to `LIMIT_GENERATING` per run; citation enforced |
| `validate` | `validate/stage.py` + `validate/bal_compiler.py` | `bal build` on answer code (fragments wrapped in `main`, common imports added) |
| `export` · `export-sft` | `export/stage.py`, `export/sft.py` | Q&A as `data/dataset.jsonl` and `data/sft/` (chat `messages`) |
| `storage status · push · pull` | `balpipe_common.storage` | Mirror `LOCAL_DATA_DIR` to GCS or S3 |
| `report` | `report.py`, `report_history.py` | Terminal summary + `data/reports/report.html` (also refreshed after every command); every command also saves its report as data, `data/reports/history/<date>_<time>_<command>.js`; `report.html` has a picker for earlier reports and `data/reports/compare.html` compares any two |
| `run-all` | `cli.py` | The Q&A path in order |

Prompts are versioned YAML files in `src/sftgen/prompts/` (questions, answers, prompt writer and
reviewer, answer and repair, judge, benchmark translation, ecosystem tasks, eval answers).
`generate/llm.py` is the OpenAI-compatible client:

- rate limit and concurrency cap
- retries
- a disk cache of responses (seeded samples are cached separately)
- token and cost capture per call (OpenRouter's `usage.cost`, or the optional
  `generate.pricing` table)

Model-written code is only compiled. Tests run only with `answers.run_tests`,
`codebases.run_tests` or `eval.run_tests`, in a sandbox.

```bash
pytest sft-dataset-generator/tests
```
