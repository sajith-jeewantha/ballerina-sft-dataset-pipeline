# Ballerina LLM Pipeline

Tools to build a RAG-grounded supervised fine-tuning (SFT) dataset for Ballerina.

Structure
- scraper/ballerinascraper: Scrapy crawler that collects Ballerina-related pages and writes JSONL to data/scraper/ballerina_corpus.jsonl
- sft-dataset-generator: RAG + LLM pipeline that ingests the corpus, generates Q/A pairs grounded in retrieved chunks, verifies faithfulness and compile-checks code, and writes SFT JSONL to data/supervised/
- data/: local corpus and generated datasets

Prerequisites
- Python 3.10+ (3.11 recommended)
- Optional: Ballerina CLI (`bal`) on PATH for compile checks
- Git

Environment
Copy the .env.example files in each component and set values.
Important env vars (sft-dataset-generator):
- OPENAI_API_KEY - API key for LLM calls
- QUESTION_GEN_MODEL, ANSWER_GEN_MODEL, JUDGE_MODEL - model names
- EMBEDDING_MODEL - model used for embeddings (sentence-transformers)
- CHROMADB_PATH - optional path for chroma persistent DB (defaults to ./chroma_db)
- CHROMADB_COLLECTION_NAME - collection name
- BUCKET_NAME - GCS bucket (optional)

Quickstart
1) Scraper

- Create and activate a venv inside scraper/ (or use system venv)
  cd scraper/ballerinascraper
  python -m venv .venv && source .venv/bin/activate
  pip install scrapy trafilatura beautifulsoup4 google-cloud-storage

- Run the crawler (will use seeds/common_crawl_urls.txt):
  scrapy crawl ballerinascraper -L DEBUG

- Output (local): data/scraper/ballerina_corpus.jsonl

2) SFT dataset generator (RAG + LLM)

- Create and activate a venv inside sft-dataset-generator/:
  cd sft-dataset-generator
  python -m venv .venv && source .venv/bin/activate
  pip install openai chromadb sentence-transformers numpy tqdm trafilatura

- Populate .env with OPENAI_API_KEY and model names.
- Local run (the script expects a local corpus JSONL at ../../data/scraper/ballerina_corpus.jsonl by default):
  python src/main.py

- The pipeline will:
  - build a Chroma persistent collection (CHROMADB_PATH)
  - generate questions per chunk
  - retrieve context and generate grounded answers
  - verify faithfulness and (optionally) compile code blocks
  - deduplicate and write final SFT JSONL to data/supervised/sft-dataset.jsonl

