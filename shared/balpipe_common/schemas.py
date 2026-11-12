"""Record schemas for every pipeline stage (see docs/schemas.md for field rationale).

Conventions: every record carries `schema_version`; optional fields are
always present (null when unknown); unknown fields are rejected so typos fail
loudly instead of silently producing half-empty records.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Split = Literal["train", "eval"]
Source = Literal["github", "central", "web"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Shared parts
# --------------------------------------------------------------------------- #
class License(_Strict):
    spdx: str = "NOASSERTION"
    basis: Literal["repo_license", "package_metadata", "site_terms", "unknown"] = "unknown"
    url: str | None = None


class DedupInfo(_Strict):
    status: Literal["unique", "exact_dup", "near_dup"] = "unique"
    duplicate_of: str | None = None
    near_dup_cluster: str | None = None


# --------------------------------------------------------------------------- #
# 1. RawRecord
# --------------------------------------------------------------------------- #
class FetchInfo(_Strict):
    requested_url: str
    final_url: str
    http_status: int
    content_type_header: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: str
    fetcher: str
    fetcher_version: str
    user_agent: str


class GitInfo(_Strict):
    repo: str
    ref: str
    commit_sha: str
    path: str
    blob_sha: str | None = None


class CentralInfo(_Strict):
    org: str
    package: str
    version: str
    ballerina_version: str | None = None
    api_doc_url: str | None = None
    source_code_location: str | None = None


class RawRecord(_Strict):
    schema_version: Literal["raw/1"] = "raw/1"
    raw_id: str
    run_id: str
    source: Source
    source_uri: str
    media_type: str
    body: str
    raw_sha256: str
    fetch: FetchInfo
    git: GitInfo | None = None
    central: CentralInfo | None = None
    license: License = Field(default_factory=License)


# --------------------------------------------------------------------------- #
# 2. Document
# --------------------------------------------------------------------------- #
DocType = Literal[
    "bbe", "guide", "tutorial", "spec", "api_readme", "module_source", "blog", "release_notes", "other"
]


class DocStats(_Strict):
    word_count: int = 0
    code_block_count: int = 0
    ballerina_block_count: int = 0
    token_count: int | None = None


class ExtractorInfo(_Strict):
    name: str
    version: str


class BBEInfo(_Strict):
    """Extra metadata for Ballerina-by-Example documents (from examples/index.json)."""

    name: str
    verify_build: bool | None = None
    verify_output: bool | None = None
    need_central: bool | None = None


class Document(_Strict):
    schema_version: Literal["document/1"] = "document/1"
    doc_id: str
    raw_id: str
    run_id: str
    source: Source
    source_uri: str
    canonical_url: str | None = None
    doc_type: DocType
    title: str
    description: str | None = None
    category: str | None = None
    markdown: str
    content_sha256: str
    ballerina_version: str | None = None
    license: License
    split: Split
    stats: DocStats = Field(default_factory=DocStats)
    dedup: DedupInfo = Field(default_factory=DedupInfo)
    extractor: ExtractorInfo
    bbe: BBEInfo | None = None
    created_at: str


# --------------------------------------------------------------------------- #
# 3. CodeExample
# --------------------------------------------------------------------------- #
CodeForm = Literal["program", "declarations", "fragment"]
CompileStatus = Literal["ok", "error", "skipped", "timeout"]


class CompileError(_Strict):
    line: int | None = None
    col: int | None = None
    message: str


class UpstreamFlags(_Strict):
    verify_build: bool | None = None
    verify_output: bool | None = None
    need_central: bool | None = None


class CodeValidationResult(_Strict):
    compile_status: CompileStatus = "skipped"
    errors: list[CompileError] = Field(default_factory=list)
    wrapped: bool = False
    imports_added: list[str] = Field(default_factory=list)
    output_match: bool | None = None
    ballerina_version: str | None = None
    checked_at: str | None = None


class CodeExample(_Strict):
    schema_version: Literal["code_example/1"] = "code_example/1"
    example_id: str
    doc_id: str
    run_id: str
    source_uri: str
    ordinal: int
    title: str
    heading_path: list[str] = Field(default_factory=list)
    intro: str = ""
    notes: str = ""
    code: str
    code_sha256: str
    code_form: CodeForm
    imports: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)
    expected_output: str | None = None
    pairing_method: Literal["bbe_directive", "md_adjacent", "doc_comment"]
    upstream_flags: UpstreamFlags | None = None
    validation: CodeValidationResult = Field(default_factory=CodeValidationResult)
    dedup: DedupInfo = Field(default_factory=DedupInfo)
    split: Split
    license: License


# --------------------------------------------------------------------------- #
# 4. Chunk
# --------------------------------------------------------------------------- #
class ChunkerInfo(_Strict):
    name: str
    version: str
    max_tokens: int
    tokenizer: str


class Chunk(_Strict):
    schema_version: Literal["chunk/1"] = "chunk/1"
    chunk_id: str
    doc_id: str
    run_id: str
    ordinal: int
    heading_path: list[str] = Field(default_factory=list)
    context_header: str
    text: str
    token_count: int
    has_code: bool
    code_example_ids: list[str] = Field(default_factory=list)
    doc_type: DocType
    source_uri: str
    split: Split
    license_spdx: str
    chunker: ChunkerInfo

    @property
    def embed_text(self) -> str:
        return f"{self.context_header}\n\n{self.text}" if self.context_header else self.text


# --------------------------------------------------------------------------- #
# 5. SFT
# --------------------------------------------------------------------------- #
TaskType = Literal[
    "write_code", "complete_code", "explain_code", "debug_fix", "convert_from_language", "concept", "api_usage"
]
Construction = Literal["llm_grounded", "mutation", "back_translation", "seed_verified"]


class Message(_Strict):
    role: Literal["system", "user", "assistant"]
    content: str


class TrainRow(_Strict):
    id: str
    messages: list[Message]


class Seed(_Strict):
    kind: Literal["chunk", "code_example"]
    id: str
    doc_id: str
    chunk_id: str | None = None


class LLMCall(_Strict):
    model: str
    provider: str
    prompt_id: str
    prompt_version: str
    params: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    usage: dict[str, int] | None = None
    cached: bool = False


class Mutation(_Strict):
    kind: str
    original_example_id: str
    compiler_error: str


class Generation(_Strict):
    question: LLMCall | None = None
    answer: LLMCall | None = None
    mutation: Mutation | None = None


class JudgeResult(_Strict):
    model: str
    provider: str
    prompt_id: str
    prompt_version: str
    scores: dict[str, int]
    reasons: dict[str, str] = Field(default_factory=dict)
    created_at: str


class CodeBlockCheck(_Strict):
    code_sha256: str
    code_form: CodeForm
    compile_status: CompileStatus
    errors: list[CompileError] = Field(default_factory=list)
    wrapped: bool = False
    imports_added: list[str] = Field(default_factory=list)


class CodeValidation(_Strict):
    required: bool
    blocks: list[CodeBlockCheck] = Field(default_factory=list)
    ballerina_version: str | None = None


class Checks(_Strict):
    mentions_context: bool = False
    unknown_symbols: list[str] = Field(default_factory=list)
    max_ngram_overlap_eval: float | None = None
    near_dup_of: str | None = None


class Decision(_Strict):
    status: Literal["pending", "accepted", "rejected"] = "pending"
    reasons: list[str] = Field(default_factory=list)
    thresholds_version: str | None = None


class PipelineInfo(_Strict):
    git_commit: str | None = None
    config_sha256: str
    schema_version: str = "sft_meta/1"


class SFTMeta(_Strict):
    schema_version: Literal["sft_meta/1"] = "sft_meta/1"
    id: str
    run_id: str
    task_type: TaskType
    construction: Construction
    seed: Seed
    context_chunk_ids: list[str] = Field(default_factory=list)
    source_uris: list[str] = Field(default_factory=list)
    licenses: list[str] = Field(default_factory=list)
    split: Split
    generation: Generation = Field(default_factory=Generation)
    judge: JudgeResult | None = None
    code_validation: CodeValidation | None = None
    checks: Checks = Field(default_factory=Checks)
    decision: Decision = Field(default_factory=Decision)
    pipeline: PipelineInfo
    created_at: str


class SFTCandidate(SFTMeta):
    """Working record between generate → validate → judge: metadata plus the conversation."""

    messages: list[Message]

    def meta(self) -> SFTMeta:
        return SFTMeta.model_validate(self.model_dump(exclude={"messages"}))


# --------------------------------------------------------------------------- #
# 7. Run manifest
# --------------------------------------------------------------------------- #
class FileRef(_Strict):
    path: str
    sha256: str | None = None
    records: int | None = None


class ItemError(_Strict):
    item_id: str
    error: str


class Manifest(_Strict):
    schema_version: Literal["manifest/1"] = "manifest/1"
    run_id: str
    stage: str
    status: Literal["running", "completed", "failed"]
    started_at: str
    finished_at: str | None = None
    inputs: list[FileRef] = Field(default_factory=list)
    outputs: list[FileRef] = Field(default_factory=list)
    config_sha256: str
    config_snapshot: dict[str, Any] = Field(default_factory=dict)
    versions: dict[str, Any] = Field(default_factory=dict)
    counts: dict[str, Any] = Field(default_factory=dict)
    errors: list[ItemError] = Field(default_factory=list)


SCHEMA_MODELS: dict[str, type[BaseModel]] = {
    "raw_record": RawRecord, "document": Document, "code_example": CodeExample, "chunk": Chunk,
    "train_row": TrainRow, "sft_meta": SFTMeta, "manifest": Manifest,
}


def export_json_schemas(out_dir: str) -> list[str]:
    """Write JSON Schema files for every record type (python -m balpipe_common.schemas docs/schemas)."""
    import json
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in SCHEMA_MODELS.items():
        path = out / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2) + "\n", encoding="utf-8")
        written.append(str(path))
    return written


if __name__ == "__main__":
    import sys

    print("\n".join(export_json_schemas(sys.argv[1] if len(sys.argv) > 1 else "docs/schemas")))
