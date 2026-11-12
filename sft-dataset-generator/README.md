# sft-dataset-generator — `sftgen`

Turns collected raw records into a filtered, chat-format SFT dataset. Setup and the end-to-end
run are in the [root README](../README.md); the design is in [../ARCHITECTURE.md](../ARCHITECTURE.md).

| Stage | Reads → writes | What it does |
|---|---|---|
| `clean` | `data/raw` → `data/clean/documents.jsonl` | Per-source extractors (BBE folders, docs Markdown, `.bal` sources, Central READMEs, Next.js data, trafilatura Markdown); labels every code fence's language; relevance filter; exact + MinHash dedup (git > central > web); doc-level train/eval split |
| `extract` | documents → `code_examples.jsonl` | Pairs code with its explanation; compiles each example; runs BBE programs against `.out` |
| `chunk` | documents → `data/chunks/<chunker>/chunks.jsonl` | Heading-aware, token-budgeted chunks; fences never split |
| `index` | chunks → `data/chroma/` | Embeds and upserts into `bal__<model>__<chunker>` |
| `generate` | chunks + examples → `data/sft/<run>/candidates.jsonl` | 7 task types with quotas; grounded answers, mutation (debug_fix), back-translation (convert), seed-verified completions |
| `validate` | → `validated.jsonl` | `bal build` on answer code, context-leak regex, unknown-symbol check |
| `judge` | → `judged.jsonl` | 1–5 rubric on five criteria from a different-family judge |
| `export` | → `train.jsonl`, `eval.jsonl`, `metadata.jsonl` | Thresholds, license allowlist, near-dup questions, 13-gram decontamination |

Prompts are versioned YAML files in `src/sftgen/prompts/`; their `id`/`version` are recorded on
every generated record. Bump `version` when you change a prompt.

```bash
pytest sft-dataset-generator/tests
```
