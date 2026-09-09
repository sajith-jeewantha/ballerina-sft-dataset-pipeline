# ballerina-sft-forge

A RAG-grounded synthetic dataset generator for creating supervised fine-tuning
(SFT) data for a Ballerina-language LLM.

Instead of asking an LLM to generate Q&A pairs from parametric memory alone
(which risks hallucinated or outdated answers), this pipeline grounds every
generated answer in retrieved context from a real Ballerina corpus (docs,
stdlib source, language spec), then verifies each pair before it makes it
into the final dataset.

## How it works

```
GCS bucket (raw crawled pages/code)
        │
        ▼
  Ingestion: dedupe (content_hash) + extract text
  per file_type (html/markdown/code) + chunk
  (prose: sliding window, code: syntactic boundary)
        │
        ▼
  Embed + index (vector DB)
        │
        ▼
  Generate questions from each chunk
        │
        ▼
  Retrieve relevant context per question
        │
        ▼
  Generate grounded answer (context-only)
        │
        ▼
  Verify: faithfulness check + code compiles
        │
        ▼
  Deduplicate
        │
        ▼
  SFT-ready JSONL
```

## Setup

```bash
pip install -r requirements.txt
export OPENAI_API_KEY=sk-...
```

## Usage

1. Point `ingest_corpus.py` at your crawler's GCS bucket/prefix (set
   `bucket_name` and `prefix` in `main.py`'s `__main__`).
2. Run the pipeline to ingest, generate, verify, and export the dataset:

```bash
python main.py
```

Output is written to `ballerina_sft.jsonl`, with each line in the form:

```json
{"instruction": "...", "output": "...", "source_chunk_ids": ["..."]}
```

## Pipeline stages

| Stage | Purpose |
|---|---|
| **Ingestion** | Read raw crawled records from GCS, dedupe by `content_hash`, extract clean text per `file_type` (HTML boilerplate stripped, code kept raw), and chunk (prose: sliding window, code: function/service boundaries) |
| **Indexing** | Embed chunks into a vector DB |
| **Question generation** | LLM generates diverse questions grounded in each chunk |
| **Retrieval** | For each question, retrieve the most relevant chunks |
| **Answer generation** | LLM answers using *only* retrieved context — flags itself when it can't (`INSUFFICIENT_CONTEXT`) |
| **Faithfulness check** | A second LLM pass verifies every claim in the answer is supported by context |
| **Compile check** | Any Ballerina code in the answer is compiled to confirm it's valid |
| **Deduplication** | Embedding-similarity filter removes near-duplicate questions |

## Configuration

Model routing is set in `main.py`:

- `QUESTION_GEN_MODEL` — cheaper model, used for question generation
- `ANSWER_GEN_MODEL` — strongest model, used for grounded answer generation
- `JUDGE_MODEL` — cheaper model, used for the faithfulness check

Adjust `top_k`, `questions_per_chunk`, and the dedup similarity `threshold`
as needed for your corpus size and desired dataset size.

## Requirements

- Python 3.11+
- Ballerina CLI (`bal`) on `PATH`, if using the compile-check step
- OpenAI API key

## Project status

Work in progress — part of a larger effort to continue-pretrain and
fine-tune a model on Ballerina code.