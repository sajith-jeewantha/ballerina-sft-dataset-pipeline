# Phase 3: Schemas

These schemas are built on the approved [recommendations.md](recommendations.md). In Phase 5 every schema becomes a Pydantic model in a single module, which validates on read and write. JSON Schema files are generated from those models into `docs/schemas/`.

```
collect ──► RawRecord ──► clean ──► Document ──┬─► extract ──► CodeExample ──┐
                                               └─► chunk ────► Chunk ────────┤
                                                                             ▼
                              export ◄── SFTMeta + messages ◄── generate / judge / validate
```

---

## 0. Conventions shared by every record

| Convention | Rule | Why |
|---|---|---|
| `schema_version` | Present on every record, e.g. `"document/1"`. It is bumped when a field changes meaning, not when a field is added. | Readers can reject or migrate old files rather than misreading them silently. |
| IDs | Deterministic and derived from content or identity. Never random. Format: `<prefix>_<16 hex>`. | Re-running a stage produces the same IDs, so stages are idempotent, resuming works, and Chroma `upsert` never creates duplicates. |
| Hashes | `sha256`, hex, written in full. | One hashing scheme everywhere. Truncation happens only inside IDs. |
| Timestamps | ISO-8601, UTC, ending in `Z`. | Unambiguous ordering across machines. |
| `run_id` | `YYYYMMDDTHHMMSSZ-<stage>`, set once per stage invocation. | Links each record to the manifest of the run that produced it (§7). |
| Licenses | SPDX identifier, or `"NOASSERTION"` when the license is unknown. | It's the industry standard. `NOASSERTION` makes "we don't know" explicit, and export can filter on it. |
| `split` | Either `"train"` or `"eval"`. It is assigned **once, on the Document**, and inherited by everything derived from it. | Holding out by source document prevents eval contamination. A paraphrase of an eval page can never reach training. |
| Optional fields | Present with value `null`, never left out. | Columns stay stable, so files load cleanly into pandas or HF `datasets`. |

---

## 1. RawRecord: `data/raw/{source}/{run_id}/part-NNNNN.jsonl.gz`

One record per fetched item, whether a web page, a git file or a Central package. Raw records are **never modified once written**.

```json
{
  "schema_version": "raw/1",
  "raw_id": "raw_3f9c1a7e0b2d4c55",
  "run_id": "20261112T091500Z-collect",
  "source": "github",
  "source_uri": "https://github.com/ballerina-platform/ballerina-distribution/blob/9a1f.../examples/hello-world/hello_world.md",
  "media_type": "text/markdown",
  "body": "# Hello world main\n\nLet's write ...",
  "raw_sha256": "b8967866...",
  "fetch": {
    "requested_url": "https://api.github.com/repos/.../contents/examples/hello-world/hello_world.md?ref=9a1f...",
    "final_url": "https://raw.githubusercontent.com/...",
    "http_status": 200,
    "content_type_header": "text/plain; charset=utf-8",
    "etag": "\"53edcb8a...\"",
    "last_modified": null,
    "fetched_at": "2026-11-12T09:15:03Z",
    "fetcher": "github",
    "fetcher_version": "1.0.0",
    "user_agent": "BallerinaCorpusBot/1.0 (+<contact url>)"
  },
  "git": {"repo": "ballerina-platform/ballerina-distribution", "ref": "master", "commit_sha": "9a1f...", "path": "examples/hello-world/hello_world.md"},
  "central": null,
  "license": {"spdx": "Apache-2.0", "basis": "repo_license", "url": "https://github.com/ballerina-platform/ballerina-distribution/blob/master/LICENSE"}
}
```

| Field | Why it exists |
|---|---|
| `raw_id` | `raw_` + sha256(`source_uri` + `raw_sha256`)[:16]. It is unique per *version* of a source, so a changed page gets a new raw record and history is kept. |
| `run_id` | Points to the collect run's manifest. |
| `source` | One of `github`, `central` or `web`. Selects the cleaner, and supports the "most canonical copy wins" rule in dedup (git > central > web). |
| `source_uri` | The canonical, human-openable location of the content. **Git URLs are pinned to the commit SHA**, so the link always shows exactly what was ingested. Tracking query parameters and fragments are removed. |
| `media_type` | The normalized content type: `text/html`, `text/markdown`, `text/x-ballerina` or `application/json`. Selects the extractor. It replaces today's overloaded `file_type`, whose `bal_embedded` label is what caused the raw-HTML bug. |
| `body` | The content exactly as fetched, as text. It is kept so the clean stage can be re-run without crawling again. |
| `raw_sha256` | Hash of `body`. Used to detect changes on re-collect: an unchanged hash means the record is skipped. This is **not** the dedup key (see `content_sha256` in §2). |
| `fetch.requested_url` / `final_url` | Shows redirects. Several Phase 1 URLs redirected (`blog/…` → 301). |
| `fetch.http_status` | Lets non-200 responses be audited. Only 200 responses are stored, but 304 Not Modified is used for conditional re-fetches. |
| `fetch.content_type_header` | The server's original header, kept for debugging `media_type` decisions. |
| `fetch.etag` / `last_modified` | Enable conditional requests (`If-None-Match`), so re-crawls are cheap and polite. |
| `fetch.fetched_at` | When the content was observed. Needed for freshness checks and for reasoning about licenses at a point in time. |
| `fetch.fetcher` / `fetcher_version` | Which collector produced the record, and which version of it. Lets you find records from a buggy collector version. |
| `fetch.user_agent` | Proof of how the content was fetched. Supports the compliance story (no spoofing). |
| `git` | `repo`, `ref`, `commit_sha` and `path`. Makes git-sourced records exactly reproducible. `null` for other sources. |
| `central` | `org`, `package`, `version`, `ballerina_version`, `api_doc_url`, `source_code_location`. Taken from the registry API. `null` for other sources. |
| `license.spdx` | The license that applies to this content. |
| `license.basis` | How the license was determined: `repo_license` (the GitHub API license field), `package_metadata` (the Central `licenses` field), `site_terms` or `unknown`. You can't audit a license claim without knowing where it came from. |
| `license.url` | Where to verify the license. |

---

## 2. Document: `data/clean/documents.jsonl`

One cleaned document per raw record that survives cleaning, dedup and relevance filtering.

```json
{
  "schema_version": "document/1",
  "doc_id": "doc_a41e09c2f7d35b18",
  "raw_id": "raw_3f9c1a7e0b2d4c55",
  "run_id": "20261112T093000Z-clean",
  "source": "github",
  "source_uri": "https://github.com/.../examples/hello-world/hello_world.md",
  "canonical_url": "https://ballerina.io/learn/by-example/hello-world/",
  "doc_type": "bbe",
  "title": "Hello world main",
  "description": "The classic \"Hello, World\" program in Ballerina",
  "category": "Language concepts",
  "markdown": "# Hello world main\n\nLet's write the `Hello World` program in Ballerina.\n\n```ballerina\nimport ballerina/io;\n...\n```\n...",
  "content_sha256": "c0ffee...",
  "ballerina_version": "2201.14.1",
  "license": {"spdx": "Apache-2.0", "basis": "repo_license", "url": "..."},
  "split": "train",
  "stats": {"word_count": 54, "code_block_count": 1, "ballerina_block_count": 1, "token_count": 112},
  "dedup": {"status": "unique", "duplicate_of": null, "near_dup_cluster": null},
  "extractor": {"name": "bbe_markdown", "version": "1.0.0"},
  "created_at": "2026-11-12T09:30:11Z"
}
```

| Field | Why it exists |
|---|---|
| `doc_id` | `doc_` + sha256(`canonical_url` or `source_uri`)[:16]. **Stable across content changes**, so one page keeps one ID and its version is told apart by `content_sha256`. |
| `raw_id` | Back-pointer for the provenance chain: SFT → chunk → document → raw. |
| `run_id` | The clean run that produced this record. |
| `source`, `source_uri` | Copied from the raw record so most consumers never need to open the raw files. |
| `canonical_url` | The public ballerina.io URL for the same content, when one exists. It links the git copy with the web copy for dedup, and gives a human-friendly citation. |
| `doc_type` | One of `bbe`, `guide`, `tutorial`, `spec`, `api_readme`, `module_source`, `blog`, `release_notes`, `other`. It drives which task types are generated, and lets marketing-like content be excluded from question generation (fixes the "subscribe to YouTube" trivia). |
| `title`, `description`, `category` | From front matter, `.metatags` or `index.json`. Used in chunk header prefixes and for analysing coverage by topic. |
| `markdown` | The clean content. Code is in fenced blocks **with a language label** (`ballerina`, `bash`, `toml`, `json`, …). |
| `content_sha256` | sha256 of the *normalized* Markdown (whitespace collapsed). This is the **exact-dedup key**, and also the version stamp used in chunk IDs. |
| `ballerina_version` | The Ballerina version the content targets: the Central `ballerinaVersion`, the repo release tag, or `null`. Lets old syntax be kept out, and lets the data be grouped by version. |
| `license` | Inherited from the raw record. Export filters on it. |
| `split` | `train` or `eval`, assigned by a deterministic hash of `doc_id` against the configured eval fraction. Everything downstream inherits it. |
| `stats.*` | Cheap quality signals for filtering and dashboards. `ballerina_block_count = 0` with `doc_type = blog` usually means low value for training. |
| `stats.token_count` | Measured with the embedding model's tokenizer. Used for budgeting chunks. |
| `dedup.status` | `unique`, `exact_dup` or `near_dup`. Duplicates are kept in the file but marked, so it's visible what was dropped and why. |
| `dedup.duplicate_of` | The `doc_id` of the canonical copy that was kept. |
| `dedup.near_dup_cluster` | MinHash cluster ID, for auditing the near-dup threshold. |
| `extractor.name` / `version` | Which extractor produced the Markdown: `bbe_markdown`, `nextjs_data`, `trafilatura_md`, `central_readme` or `bal_source`. When an extractor is fixed, only its documents need re-cleaning. |
| `created_at` | When this clean version was produced. |

---

## 3. CodeExample: `data/clean/code_examples.jsonl`

One Ballerina code block paired with the prose that explains it. These are the seeds for code-focused SFT tasks.

```json
{
  "schema_version": "code_example/1",
  "example_id": "ex_5d0b7e21c9a84f36",
  "doc_id": "doc_a41e09c2f7d35b18",
  "run_id": "20261112T094000Z-extract",
  "source_uri": "https://github.com/.../examples/hello-world/hello_world.bal",
  "ordinal": 0,
  "title": "Hello world main",
  "heading_path": ["Hello world main"],
  "intro": "Let's write the `Hello World` program in Ballerina.",
  "notes": "To run this sample, place the source code in a file named `hello_world.bal` and use the `bal run` command.",
  "code": "import ballerina/io;\n\npublic function main() {\n    io:println(\"Hello, World!\");\n}\n",
  "code_sha256": "e3b0c4...",
  "code_form": "program",
  "imports": ["ballerina/io"],
  "symbols": ["io:println"],
  "expected_output": "$ bal run hello_world.bal\nHello, World!",
  "pairing_method": "bbe_directive",
  "upstream_flags": {"verify_build": true, "verify_output": true, "need_central": false},
  "validation": {
    "compile_status": "ok",
    "errors": [],
    "wrapped": false,
    "imports_added": [],
    "output_match": true,
    "ballerina_version": "2201.14.1",
    "checked_at": "2026-11-12T09:41:02Z"
  },
  "dedup": {"status": "unique", "duplicate_of": null},
  "split": "train",
  "license": {"spdx": "Apache-2.0", "basis": "repo_license", "url": "..."}
}
```

| Field | Why it exists |
|---|---|
| `example_id` | `ex_` + sha256(`doc_id` + `ordinal` + `code_sha256`)[:16]. Changes only if the code itself changes. |
| `doc_id`, `source_uri`, `ordinal` | Where the block came from, and its position in the document. |
| `title`, `heading_path` | The section context, e.g. `["Write a RESTful API", "The complete code"]`. Prompts need it ("in the context of …"), and it helps explain a snippet. |
| `intro` | The prose *before* the code, which explains its purpose. Ground truth for `explain_code` tasks and the spec for `write_code` tasks. |
| `notes` | The prose *after* the code: what it does, prerequisites, how to run it. A second grounding source. |
| `code` | Verbatim code with newlines and indentation preserved. |
| `code_sha256` | Key for exact dedup and the compile-result cache. |
| `code_form` | `program` (has `main` or a service), `declarations` (top-level definitions only) or `fragment` (statements only). Decides how the code is compiled (§7 of recommendations) and which task types fit it. |
| `imports`, `symbols` | Parsed `import` lines and the `module:name` references used. They feed the known-symbols table that detects hallucinated APIs, and topic coverage stats (e.g. how many examples use `http:Client`). |
| `expected_output` | From `.out`. Lets corpus code be checked by running it, and supports "what does this print?" tasks. `null` when there isn't one. |
| `pairing_method` | `bbe_directive`, `md_adjacent` or `doc_comment`. Quality differs by method: `bbe_directive` is exact, `md_adjacent` is heuristic. Lets you filter or weight by it. |
| `upstream_flags` | From the examples' `index.json` (`verifyBuild`, `verifyOutput`, `needCentral`). The upstream maintainers' own statement of whether an example should build or produce matching output. |
| `validation.compile_status` | `ok`, `error` or `skipped`. Only `ok` examples seed code tasks. |
| `validation.errors` | Parsed compiler errors (`line`, `col`, `message`). Makes failures auditable, and is reused for `debug_fix` tasks. |
| `validation.wrapped` / `imports_added` | Shows whether the code was wrapped in `main` or had imports added before compiling, so a passing compile is never mistaken for the original snippet being complete. |
| `validation.output_match` | The result of comparing the `bal run` output with `.out` (trusted corpus only), or `null`. |
| `validation.ballerina_version` | The compiler version used. A pass on 2201.14.1 says nothing about 2201.8. |
| `validation.checked_at` | When validation ran. |
| `dedup`, `split`, `license` | Same meaning as on Document. `split` is inherited from the parent document. |

---

## 4. Chunk: `data/chunks/{chunker_version}/chunks.jsonl`, plus the vector DB

```json
{
  "schema_version": "chunk/1",
  "chunk_id": "chk_a41e09c2f7d35b18_c0ffee12_003",
  "doc_id": "doc_a41e09c2f7d35b18",
  "run_id": "20261112T095000Z-chunk",
  "ordinal": 3,
  "heading_path": ["Write a RESTful API", "The complete code"],
  "context_header": "Write a RESTful API > The complete code",
  "text": "The below is the complete code of the service implementation.\n\n```ballerina\nimport ballerina/http;\n...\n```",
  "token_count": 412,
  "has_code": true,
  "code_example_ids": ["ex_9e1f..."],
  "doc_type": "tutorial",
  "source_uri": "https://github.com/.../write-a-restful-api.md",
  "split": "train",
  "license_spdx": "Apache-2.0",
  "chunker": {"name": "md_structural", "version": "1.0.0", "max_tokens": 300, "tokenizer": "sentence-transformers/all-mpnet-base-v2"}
}
```

| Field | Why it exists |
|---|---|
| `chunk_id` | `chk_<doc hash>_<content_sha256[:8]>_<ordinal>`. Readable, deterministic, and **it changes when the document content changes**, so stale vectors are never mixed with new text. |
| `doc_id`, `ordinal` | Position in the parent document. Neighbouring chunks can be fetched for more context. |
| `heading_path`, `context_header` | The section breadcrumb. The **text that gets embedded is `context_header + "\n\n" + text`**. That embed text isn't stored, because it can be derived. |
| `text` | The chunk body. It never contains a partial code fence. |
| `token_count` | Measured with `chunker.tokenizer`. A check enforces `≤ max_tokens ≤ the embedding model's max_seq_length`, which fixes the silent 384-token truncation. |
| `has_code`, `code_example_ids` | Which code examples sit inside this chunk. Question generation uses them to pick task types and to seed from verified code. |
| `doc_type`, `source_uri`, `split`, `license_spdx` | Denormalized from the document so retrieval can filter by them. For example, answers for training are never grounded in `eval` chunks. |
| `chunker.*` | Name, version and parameters. With the tokenizer, they define a chunk set, and they form part of the collection name. |

**Vector DB (Chroma).** The JSONL is the source of truth, and the vector index can be rebuilt from it at any time.

- Collection name: `bal__{embedding_model_slug}__{chunker_version}`, e.g. `bal__all-mpnet-base-v2__md_structural-1.0.0`. Changing the model or the chunker never mixes vectors in one collection.
- `id` = `chunk_id`, `document` = the embed text, `embedding` from `EMBEDDING_MODEL`.
- Metadata is flat scalars, because Chroma doesn't take lists or nested values: `doc_id`, `doc_type`, `split`, `has_code`, `license_spdx`, `source_uri`, `heading` (the `context_header`).
- Writes use `upsert`, so re-running the index stage is idempotent.

---

## 5. SFT output: two files linked by `id`

### 5a. Training file: `data/sft/{run_id}/train.jsonl` and `eval.jsonl`

Every trainer verified in Phase 2 (TRL, Axolotl, Unsloth) loads this file directly:

```json
{"id": "sft_7c2e90a1b4d35f68",
 "messages": [
   {"role": "system", "content": "You are an expert Ballerina programmer."},
   {"role": "user", "content": "Write a Ballerina HTTP service that ..."},
   {"role": "assistant", "content": "Here's a service that ...\n\n```ballerina\nimport ballerina/http;\n...\n```\n\n..."}]}
```

| Field | Why it exists |
|---|---|
| `id` | `sft_` + sha256(`task_type` + `seed.id` + normalized user message)[:16]. The join key to the metadata file. |
| `messages` | The chat format (`system`, `user`, `assistant`). The system prompt is a configurable constant, and it can be left out. |

### 5b. Metadata file: `data/sft/{run_id}/metadata.jsonl`

One line per *candidate*, including rejected ones, so the filtering can be audited and thresholds re-applied without regenerating.

```json
{
  "schema_version": "sft_meta/1",
  "id": "sft_7c2e90a1b4d35f68",
  "run_id": "20261112T100000Z-generate",
  "task_type": "write_code",
  "construction": "llm_grounded",
  "seed": {"kind": "code_example", "id": "ex_9e1f...", "doc_id": "doc_a41e...", "chunk_id": "chk_..."},
  "context_chunk_ids": ["chk_...", "chk_..."],
  "source_uris": ["https://github.com/.../write-a-restful-api.md"],
  "licenses": ["Apache-2.0"],
  "split": "train",
  "generation": {
    "question": {"model": "qwen/qwen3.8-flash", "provider": "openrouter", "prompt_id": "question.write_code", "prompt_version": "2", "params": {"temperature": 0.8, "max_tokens": 1024}, "created_at": "2026-11-12T10:00:04Z", "usage": {"prompt_tokens": 812, "completion_tokens": 96}},
    "answer":   {"model": "...", "provider": "openrouter", "prompt_id": "answer.grounded", "prompt_version": "3", "params": {"temperature": 0.2, "max_tokens": 2048}, "created_at": "...", "usage": {"prompt_tokens": 2310, "completion_tokens": 540}},
    "mutation": null
  },
  "judge": {
    "model": "...", "provider": "openrouter", "prompt_id": "judge.rubric", "prompt_version": "1",
    "scores": {"faithfulness": 5, "correctness": 4, "helpfulness": 4, "instruction_following": 5, "code_quality": 4},
    "reasons": {"faithfulness": "...", "correctness": "..."},
    "created_at": "..."
  },
  "code_validation": {
    "required": true,
    "blocks": [{"code_sha256": "...", "code_form": "program", "compile_status": "ok", "errors": [], "wrapped": false, "imports_added": []}],
    "ballerina_version": "2201.14.1"
  },
  "checks": {
    "mentions_context": false,
    "unknown_symbols": [],
    "max_ngram_overlap_eval": 0.02,
    "near_dup_of": null
  },
  "decision": {"status": "accepted", "reasons": [], "thresholds_version": "1"},
  "pipeline": {"git_commit": "a159c70", "config_sha256": "...", "schema_version": "sft_meta/1"},
  "created_at": "2026-11-12T10:00:31Z"
}
```

| Field | Why it exists |
|---|---|
| `id` | Join key with the training file. |
| `run_id` | The generation run that produced the record. |
| `task_type` | `write_code`, `complete_code`, `explain_code`, `debug_fix`, `convert_from_language`, `concept` or `api_usage`. Used to enforce the mix of task types and to analyse it. |
| `construction` | `llm_grounded`, `mutation` or `back_translation`. Tells you how trustworthy the answer is by design: mutation and back-translation answers are verified code. |
| `seed` | **The chunk or code example the question was generated from.** Today this is lost; only the retrieved chunks are kept. It's the main fix for traceability. |
| `context_chunk_ids` | The chunks retrieved and shown to the answer model. The judge checks faithfulness against exactly these. |
| `source_uris`, `licenses` | Every source behind the answer and its license. Needed for attribution, and to filter the dataset by license at export. |
| `split` | Inherited from the seed's document. Retrieval for a `train` record never returns `eval` chunks. |
| `generation.question` / `answer` | `model`, `provider`, `prompt_id`, `prompt_version`, `params`, `created_at` and `usage` for each LLM call. The record can be reproduced, and per-model quality and cost can be compared. |
| `generation.mutation` | For `debug_fix` records: `{"kind": "type_mismatch", "original_example_id": "...", "compiler_error": "..."}`. Shows how the bug was injected. |
| `judge.*` | The judge model, its prompt version, and **every score with its reason**, kept instead of being collapsed to a single pass or fail. |
| `code_validation.required` | Whether this task type must contain compiling code. Closes today's "no code passes automatically" loophole. |
| `code_validation.blocks[]` | The compile result for each code block, with errors. |
| `code_validation.ballerina_version` | The version the compile check ran on. |
| `checks.mentions_context` | Regex flag for "based on the provided context" and similar. Accepted records must be `false`. |
| `checks.unknown_symbols` | `module:symbol` references not found in the known-symbols table. These are hallucinated APIs. |
| `checks.max_ngram_overlap_eval` | The highest 13-gram overlap with any eval record. Detects contamination. |
| `checks.near_dup_of` | The `id` of the earlier near-duplicate this record matched, if any. |
| `decision.status` | `accepted`, or `rejected` with `decision.reasons` (e.g. `["judge.correctness<4", "compile_error"]`). |
| `decision.thresholds_version` | Export re-computes the decision from scores whenever thresholds change. This field records which thresholds produced it. |
| `pipeline.git_commit`, `config_sha256` | The exact code and config that produced the record. |
| `created_at` | When the record was finalized. |

---

## 6. What changed from the current schemas

| Current | New | Reason |
|---|---|---|
| `content` holds raw HTML, or extracted text, depending on the stage | `body` (raw only) vs `markdown` (clean only) | One meaning per field. Raw and clean are in separate files. |
| `file_type`: `html`, `bal`, `bal_embedded`, `md` | `media_type` + `doc_type` | `bal_embedded` sent HTML down the wrong path. Format and content kind are separate questions. |
| `content_hash`: sometimes of raw, sometimes of extracted text | `raw_sha256` (change detection) + `content_sha256` (dedup) | Each hash now has one job. |
| Chunk `metadata.source` only | Full provenance plus `heading_path`, `split`, `license` | Retrieval filtering and traceability. |
| `instruction` / `output` / `source_chunk_ids` | `messages` + a sidecar metadata file with `seed`, `context_chunk_ids`, models, prompts, scores and checks | Works with trainers directly, and is fully auditable. |

---

## 7. Run manifest: `{stage output dir}/_manifest.json`

Each stage writes one manifest for each run. It's what makes stages resumable and auditable.

```json
{
  "schema_version": "manifest/1",
  "run_id": "20261112T100000Z-generate",
  "stage": "generate",
  "status": "completed",
  "started_at": "...", "finished_at": "...",
  "inputs": [{"path": "data/chunks/md_structural-1.0.0/chunks.jsonl", "sha256": "..."}],
  "outputs": [{"path": "data/sft/20261112T100000Z-generate/metadata.jsonl", "records": 1840}],
  "config_sha256": "...", "config_snapshot": {"QUESTION_GEN_MODEL": "...", "...": "secrets redacted"},
  "versions": {"pipeline_git_commit": "a159c70", "python": "3.11.9", "ballerina": "2201.14.1", "packages": {"trafilatura": "2.3.1"}},
  "counts": {"seen": 920, "written": 1840, "skipped_existing": 0, "rejected": {"compile_error": 112, "judge": 230}},
  "errors": []
}
```

| Field | Why it exists |
|---|---|
| `status` | `running`, `completed` or `failed`. A stage restarted on top of a `running` or `failed` manifest **resumes**: it skips IDs already in its output. |
| `inputs[].sha256` | Detects when the input has changed since the run started. If it has, the stage refuses to resume and starts a new run. |
| `config_sha256` / `config_snapshot` | Exactly what settings were used. Secrets are always redacted. |
| `versions` | Code, Python, Ballerina and key package versions, for reproducibility. |
| `counts` | Funnel statistics for each stage. Today these are only `print`ed and lost. |
| `errors` | Item-level failures that were skipped, with their IDs, so they can be retried. |

---

## 8. Proposed repo layout (for approval)

Both projects need to share the schemas, config loading, logging and JSONL I/O. Today that logic is duplicated: extraction and dedup each exist in both projects. They also have very different dependencies: Scrapy versus torch, sentence-transformers and chromadb.

**Option A (recommended): two projects plus one small shared package.**

```
ballerina-llm-pipeline/
├── README.md  ARCHITECTURE.md  architecture.html  CHANGELOG.md
├── config/pipeline.yaml              # non-secret settings (sources, thresholds, quotas)
├── .env.example                      # secrets + model names only
├── docs/  (recommendations.md, schemas.md, schemas/*.json)
├── shared/                           # pip install -e shared  → package `balpipe_common`
│   └── balpipe_common/ schemas.py config.py logging.py jsonl_io.py manifest.py ids.py
├── scraper/                          # stage: collect → data/raw
│   └── ballerinascraper/  (Scrapy project for web; + collectors/github.py, collectors/central.py)
├── sft-dataset-generator/            # stages: clean, extract, chunk, index, generate, judge, validate, export
│   ├── src/sftgen/ {clean/, extract/, chunk/, index/, generate/, judge/, validate/, export/, prompts/, cli.py}
│   └── tests/
└── data/  (gitignored) raw/ clean/ chunks/ sft/ cache/
```

**Option B: merge into one package with optional extras** (`pip install -e .[collect]`, `.[generate]`). Simpler imports, but it means renaming both projects and changing more of the existing structure.

Either way, the two existing folder names stay as they are, as agreed.
