# sft-dataset-generator — `sftgen`

The command-line tool for the whole pipeline. Setup and every command, step by step, are in the
[root README: Quick start](../README.md#quick-start-step-by-step). The design is in
[../ARCHITECTURE.md](../ARCHITECTURE.md).

```bash
sftgen run-all                    # everything, in order
# or one phase at a time:
sftgen scrape
sftgen generate-questions
sftgen generate-answers           # --retry-failed to retry failed ones
sftgen validate                   # optional; --recheck to re-validate all
sftgen export
sftgen export-sft
sftgen report
```

| Command | Module | What it does |
|---|---|---|
| `scrape` | `ballerinascraper.scrape` → `clean/stage.py` → `chunk/stage.py` | Fetch new pages, extract clean Markdown (`clean/extractors.py`), chunk by headings (`chunk/markdown_chunker.py`) |
| `generate-questions` | `generate/questions.py` | `QUESTIONS_PER_CHUNK` questions per new chunk, round-robin across sources, up to `LIMIT_GENERATING`; near-duplicates skipped (`clean/dedup.py`) |
| `generate-answers` | `generate/answers.py` | Answer each `pending` question from its own chunk; citation enforced |
| `validate` | `validate/stage.py` + `validate/bal_compiler.py` | `bal build` on answer code (fragments wrapped in `main`, common imports added) |
| `export` | `export/stage.py` | `data/dataset.jsonl` (review format) |
| `export-sft` | `export/sft.py` | `data/sft/train.jsonl`, `eval.jsonl` (chat `messages`), `metadata.jsonl`; split by page; `Source:` line removed |
| `report` | `report.py` | Terminal summary + `reports/report.html` (also refreshed after every command) |
| `run-all` | `cli.py` | All of the above in order |

Prompts are versioned YAML files in `src/sftgen/prompts/` (`questions.yaml`, `answer.yaml`).
`generate/llm.py` is the OpenAI-compatible client:

- rate limit and concurrency cap
- retries
- a disk cache of responses
- token and cost capture per call (OpenRouter's `usage.cost`, or the optional
  `generate.pricing` table)

```bash
pytest sft-dataset-generator/tests
```
