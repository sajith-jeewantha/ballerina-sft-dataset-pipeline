# Phase 2: Research and recommendations

Date: 2026-11-12. Every claim marked **(tested)** was checked against live sources or the local toolchain (Ballerina 2201.14.1, trafilatura 2.2.0) during this phase.

Decisions already made (Phase 1 Q&A):

- Remove the proxy rotation and the user-agent spoofing.
- Crawl only an allowlist of sites.
- Get GitHub content through the API or `git clone`.
- Use OpenRouter through the OpenAI SDK, with GCS as an optional storage target.
- Drop Stack Overflow.
- Treat the existing `data/` as throwaway.

---

## 0. The most important finding: most of the content already exists as clean source files

Most of what the scraper fetches as HTML is published upstream as clean, licensed source files **(tested)**:

| Source | What it contains | Format | License |
|---|---|---|---|
| `ballerina-platform/ballerina-distribution/examples/` | **596 Ballerina-by-Example (BBE) folders**, each with `x.bal` (code), `x.md` (explanation), `x.metatags` (description), `x.out` (expected output), and sometimes `tests/` | `.bal` + Markdown | Apache-2.0 |
| `ballerina-platform/ballerina-dev-website` | 738 `.md` files behind ballerina.io: learn docs, tutorials, the spec, release notes | Markdown | Apache-2.0 |
| Central registry API `api.central.ballerina.io/2.0/registry/packages` | Per package: `readme` (Markdown), `summary`, `licenses`, `version`, `ballerinaVersion`, `sourceCodeLocation`, `keywords` | JSON | per package |
| `module-ballerina-*` / `module-ballerinax-*` repos | stdlib and connector sources, `Module.md`, tests | `.bal` + Markdown | per repo |
| blog.ballerina.io | Blog posts | HTML only | site terms |

A BBE `.md` file marks exactly where its code goes, with `::: code hello_world.bal :::` and `::: out hello_world.out :::`. That gives **code-and-explanation pairs with no HTML parsing and no LLM involved**. The `.out` file is a ground-truth expected output.

Why this beats crawling the HTML **(tested)**:

- On a by-example page, trafilatura returns **only the left-hand navigation menu and zero code**. The code is never in the HTML. It sits in the Next.js `__NEXT_DATA__` JSON as syntax-highlighted fragments.
- A Central package page is **7.8 MB and took 17 s**, which already exceeds the current `DOWNLOAD_TIMEOUT=15`. The API returns the same readme as Markdown in one small JSON response.

**Recommendation: ingest from source first, and keep crawling HTML only as a fallback.**

1. **GitHub collector** (API plus shallow `git clone` pinned to a commit SHA) for the examples, the website and the stdlib/connector repos.
2. **Central collector** using the registry API.
3. **Scrapy HTML crawl**, kept for sites with no source: blog.ballerina.io and anything else on the allowlist. It uses one honest user-agent, obeys robots.txt (ballerina.io disallows `/1.x/` and old release notes), and runs with AutoThrottle and low concurrency.

Scrapy stays, because robots.txt handling, throttling and retries come built in. Its job shrinks to a small part of the corpus.

---

## 1. Storage format for cleaned content

| Format | (a) Chunking and embedding for RAG | (b) Keeping code and its explanation together |
|---|---|---|
| HTML | Poor. Tags and nav are noise, and needs cleaning before every use | Code is present, but scattered across highlighting `<span>`s with no language label |
| TXT | OK for embedding, but structure is gone, so chunks can't follow section boundaries | **Poor.** Fences and language labels disappear, and code can't be told apart from prose (this is today's bug) |
| **Markdown** | **Best.** Headings let chunks follow sections, LLMs read it natively, and it embeds cleanly | **Best.** ```` ```ballerina ```` fences keep code intact and labelled, right next to the prose |
| JSONL | Not a content format but a *container*. Ideal for metadata and streaming | Depends on what is stored in it |

**Recommendation: Markdown content stored inside JSONL records**, one document per line. Each line has a `markdown` field next to its provenance metadata.

- **Single JSONL, not a folder of `.md` files.** Provenance travels with the content. The files are appendable, streamable and easy to dedup and diff. They also mirror cleanly to GCS. A folder of `.md` files is nice to browse, so offer it as an optional `export-md` debug command, not as the system of record.
- **Keep raw and clean data strictly separate**:
  - Raw data is immutable and append-only, split by source and run: `data/raw/{source}/{run_id}/part-00000.jsonl.gz`.
    - HTML pages keep the full raw body, so extraction can be re-run without crawling again.
    - Git sources record the repo, the commit SHA and the file path. Their content is stored too, so a deleted upstream file doesn't break reproducibility.
  - Clean data is derived and reproducible from raw: `data/clean/documents.jsonl` and `data/clean/code_examples.jsonl`. Each record carries `extractor_version`, so changing an extractor means re-running only the clean stage.

---

## 2. Turning HTML into clean text

Tests on real pages **(tested)**:

| Page | trafilatura default (plain text, what runs today) | trafilatura `output_format="markdown", include_formatting=True` |
|---|---|---|
| by-example page | nav only, 0 code | nav only, 0 code (the code isn't in the HTML at all) |
| Tutorial (`write-a-restful-api`) | code present, but fences lost | **19/19 code blocks kept as fences**, indentation intact |
| Blog post | code flattened into the prose | fences kept, indentation intact |

The site's `<pre>` tags carry **no language class**, only `shiki github-light`. So every fence comes out as bare ```` ``` ````, and shell commands (`bal run`) get fenced exactly like Ballerina code.

**Recommendation:** HTML extraction runs in three steps.

1. **Site-specific structured extraction first.** If `__NEXT_DATA__` is present, read the code and frontmatter from the JSON. Your scratch `text.py` already does this; it gets turned into a proper extractor.
2. **trafilatura, set to `output_format="markdown"`, `include_formatting=True`, `include_tables=True`, `favor_recall=True`**, for everything else. Keep trafilatura: it's maintained, it's already a dependency, and it beat the alternatives on removing boilerplate. markdownify and html2text keep the nav and footer unless you strip them by hand, and docling is heavy.
3. **Label the language of every fence.** A small rule-based classifier tags each fence as ballerina, shell, toml, json, yaml, java or other. For Ballerina it looks for `import ballerina/`, `function … returns`, `resource function`, `service … on`, `|}`, `check `. For shell it looks for a leading `$`, `bal run`, `bal new`. Ballerina-labelled blocks are then confirmed by the compiler (section 7), which tags each with `compiles` and `ballerina_version`.

---

## 3. Pairing code with its description

Each kind of source gets its own deterministic method. No LLM is involved, so every pair can be traced and re-run.

| Source | How pairs are built |
|---|---|
| By-example folders | The code is `x.bal`. The **intro** is the prose before `::: code :::`, plus the title and the `.metatags` description. The **notes** are the prose after it (prerequisites, run steps). `x.out` is the expected output. |
| Markdown docs and blogs | Parse with `markdown-it-py`. For each Ballerina fence, take the heading path (H1 > H2 > H3) and the 1–2 paragraphs before it in the same section. Also take the paragraph right after it if it explains the code ("In this code…", "This …", a list). |
| `.bal` sources from stdlib and connector repos | Ballerina `#` doc comments plus the signature give a description for each function or type. `Module.md` gives the module-level description. |

These go into a separate **`code_examples.jsonl`**, linked back to the parent document by `doc_id`. They become the seeds for code-focused SFT tasks (section 6). Pre-Swan-Lake syntax (Ballerina 1.x) fails the compile check and is excluded, which removes a real risk of contaminating the data with outdated syntax.

---

## 4. Chunking

Two current bugs must go:

- Every newline is destroyed by the `split()`/`" ".join` call.
- The embedding model is `all-mpnet-base-v2`, whose `max_seq_length` is **384 tokens (tested)**. Chunks are 500 *words*, roughly 650–800 tokens, so **about half of every chunk is silently cut off before it is embedded**.

**Recommendation: chunk along the document's structure.**

- Split by heading into sections, then pack blocks (paragraphs, lists, tables, code fences) up to a **token** budget. Count tokens with the embedding model's own tokenizer, aiming for about 80% of its maximum length.
- **A code fence is never split.** If it doesn't fit, it starts a new chunk. If a single fence is bigger than the maximum, split it at top-level Ballerina declarations, tracking `{}` depth so nothing breaks mid-block, and repeat the `import` lines in each piece.
- **Overlap:** don't use a sliding word window. When a section has to be split, carry the previous paragraph forward, and only then.
- **Contextual header:** prefix each chunk's embedded text with `Title > Section > Subsection`. This helps retrieval, and the clean body is stored separately.
- **Embedding model:** make it configurable, and pick it **by measurement** rather than by reputation. Question generation gives a free retrieval eval set: each generated question should retrieve the chunk it came from. Track recall@k on about 200 such pairs, then compare a long-context code-aware model with `all-mpnet-base-v2`, for example `jinaai/jina-embeddings-v2-base-code` (8k context) or `BAAI/bge-m3` (8k context).

---

## 5. Deduplication

- **Exact.** Two separate hashes with separate jobs:
  - `raw_sha256` over the fetched bytes, for change detection when re-crawling.
  - `content_sha256` over normalized clean Markdown (whitespace collapsed), for dedup.

  Dedup state must persist across runs (today it's an in-memory `set`, which is where the 41 duplicates came from). Rebuild it from the existing clean records at the start of each run.
- **Near-duplicate documents and examples:** use MinHash LSH (`datasketch` 2.0.0) over word 5-gram shingles, threshold Jaccard ≥ 0.8. For code, normalize first: strip comments, collapse whitespace, shingle on tokens. When duplicates are found, **keep the most canonical source**, in the order git repo > Central > HTML. A by-example page seen on both the website and in the repo keeps the repo copy.
- **SFT records:** keep the current near-dup check on question embeddings (≈0.92), but vectorize it, since today it's an O(n²) Python loop. Also cap records per source document so a few long pages don't dominate.

---

## 6. SFT quality

The current output shows the problems:

- 85 of 119 answers start with "Based on the provided context". The fine-tuned model would learn to cite a context it won't have when actually used.
- Some questions are trivia (event dates, "subscribe to YouTube").
- The same model (`qwen3.8-flash`) generates, answers *and* judges, so the judge favours its own answers.

**Task types, with quotas per run (configurable):**

| Type | How the answer is made trustworthy |
|---|---|
| `write_code`, `complete_code` | Seeded from verified `code_examples`. The answer must compile. |
| `explain_code` | The code is real code from the corpus. The explanation is judged against the source's own description. |
| `debug_fix` | **Built by mutation.** Take verified code, inject a realistic bug (wrong type, missing `check`, misspelled function), and capture the *real* compiler error. The target answer is the original code plus an explanation. Correct by construction. |
| `convert_from_language` | **Built by back-translation.** An LLM writes the Python/Java/Go equivalent of verified Ballerina code. The target answer is the original Ballerina code. |
| `concept`, `api_usage` | Grounded in docs chunks and judged for faithfulness. |

**Filtering what reaches question generation.** Tag chunks with a cheap rule-based or LLM topic label. Skip case studies, newsletters, events and marketing pages; only *feature* sections of release notes are used.

**Answer style.** Use a system prompt that forbids mentioning "context", "the documentation provided" and similar, and back it with a regex filter after generation.

**Judge:**

- **Use a different model family from the answer model.**
- The judge returns structured JSON with a score of 1–5 for each of: `faithfulness`, `correctness`, `helpfulness`, `instruction_following`, `code_quality`, plus a short reason for each.
- **Default thresholds:** faithfulness ≥ 4, correctness ≥ 4, every other score ≥ 3.
- **Thresholds are applied when exporting, not when generating.** Every score is stored, so the cut-offs can change without regenerating anything.
- Calibrate the thresholds once against about 100 records labelled by hand.

**Hallucinated APIs.**

- Build a symbol table of known modules and public functions from the Central API and the stdlib sources.
- Flag any `module:symbol` in an answer that isn't in the table.
- Most such errors fail compilation anyway; this check also catches them in the prose.

**Contamination and evaluation:**

- **Split by source document**, never by record. For example, 5% of the by-example folders and docs pages are held out and never used to seed training data.
- Before export, check 13-gram overlap between training records and the held-out set, and against any external Ballerina benchmark you plan to use.

---

## 7. Validating code with `bal`

Tested on Ballerina 2201.14.1, which is installed at `/opt/homebrew/bin/bal`:

| Case | Result | Time |
|---|---|---|
| Full program with `main` | ✅ builds | 6.2 s cold (JVM start) |
| Top-level function, **no `main`, no imports** | ✅ builds | 1.4 s |
| Code with errors | ❌ structured errors: `ERROR [bad.bal:(2:26,2:44)] undefined function 'printline'` | 3.4 s |
| `http` service, first build | ✅ builds, after pulling `ballerina/http` from Central | 14.2 s |
| Same service, `--offline`, cached | ✅ builds | 2.0 s |

**Correction to Phase 1:** I wrote that snippets with no `main` and no imports always fail. That was wrong: top-level declarations compile fine. Only *statement-level fragments* fail.

**Recommendation: yes, use it as a hard filter**, with these rules:

- **Choose the code's form before compiling.**
  - Top-level declarations compile as they are.
  - Statement-only fragments get wrapped in `public function main() returns error? { … }`.
  - Missing `import` lines are added only from a fixed allowlist of stdlib modules. Record that wrapping or imports were added.
- **Make it sandboxed and deterministic:**
  - Each build runs in its own temp directory as its working directory, with `--offline`, so the `main.jar` litter stops.
  - Run a pre-warm step once that pulls the common stdlib packages.
  - Add a timeout and a JVM heap cap (`JAVA_OPTS=-Xmx…`) to prevent another 3.6 GB heap dump.
  - Use a small process pool, about 4 workers, rather than calling it synchronously inside asyncio.
- **Compile only.** Model-generated code is never *run*. Running `bal run` and diffing against `.out` is reserved for the trusted by-example corpus, as a check on the corpus itself.
- **Cache results by `sha256(code) + ballerina_version`.** Store `compile_status`, the parsed `compile_errors` and `ballerina_version` on every record.
- **Policy:** if a record's task type expects code, it must contain code and the code must compile. "No code passes automatically", as today, goes away.

---

## 8. Output format

**Recommendation: move to the chat `messages` format.**

```json
{"id": "...", "messages": [
  {"role": "system", "content": "You are an expert Ballerina programmer."},
  {"role": "user", "content": "..."},
  {"role": "assistant", "content": "..."}]}
```

Verified against the current docs (2026-11):

- **TRL `SFTTrainer`** accepts conversational `messages`, plus a `tools` column if needed.
- **Axolotl** uses `type: chat_template`. `sharegpt` is deprecated. Its default is `roles_to_train: ["assistant"]`, which means the loss is computed only on the assistant's replies.
- **Unsloth** takes ChatML-style `messages` through `get_chat_template` / `apply_chat_template`.

None of the three documents arbitrary extra columns. So **export two files that share the same `id`**:

- `train.jsonl` / `eval.jsonl`, containing only `id` and `messages`, which trainers can use directly.
- `metadata.jsonl`, with full provenance, scores and generation info. This is the Phase 3 SFT record.

The `instruction`/`output` format can still be exported as an option if needed.

---

## 9. Cross-cutting choices: keep it simple, keep dependencies few

| Concern | Choice | Why |
|---|---|---|
| Config | `pydantic-settings` (`.env` plus an optional YAML file) | Typed config, checked when the program starts. Fixes `EMBEDDING_MODEL=None` slipping through. |
| LLM retries and rate limits | The OpenAI SDK's built-in `max_retries` (exponential backoff, honours `Retry-After`) plus an `asyncio.Semaphore` with a requests-per-minute limiter | No extra dependency. Today 400/401 errors are retried too. |
| HTTP retries (GitHub, Central) | `requests` + `urllib3.Retry` | Already a dependency |
| Logging | stdlib `logging` with a JSON formatter | No extra dependency |
| Resuming after failure | Each stage writes its outputs plus a `_manifest.json` (inputs, config hash, versions, counts). Generation appends one JSONL line per item and skips keys already done. | A crash loses at most the item in progress |
| Vector DB | Keep Chroma as a **derived, rebuildable** index. Chunks in JSONL are the source of truth. The collection name includes the embedding model and chunker version. | Stops different embedding runs mixing silently |
| Tests | `pytest` on extraction, fence language labelling, pairing, chunking (never split a fence, respect the token budget), dedup, code wrapping | |

Current versions on PyPI (2026-11-12), to pin in Phase 5:

| Package | Version |
|---|---|
| scrapy | 2.20.0 |
| trafilatura | 2.3.1 |
| datasketch | 2.0.0 |
| chromadb | 1.5.9 |
| sentence-transformers | 6.3.0 |
| openai | 3.49.0 |
| pydantic-settings | 2.16.2 |
| markdown-it-py | 4.3.0 |
| pytest | 9.2.0 |
| python-dotenv | 1.2.4 |
| pyyaml | 6.0.3 |
