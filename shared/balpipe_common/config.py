"""Typed, validated configuration.

Precedence (highest first): process environment > `.env` > `config/pipeline.yaml`
> defaults. Any YAML key can be overridden with `BALPIPE__SECTION__KEY=value`.
Secrets are `SecretStr`, so they never appear in logs or reprs. Empty values in
`.env` (e.g. `GITHUB_TOKEN=`) are ignored instead of overriding other sources.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

log = logging.getLogger(__name__)

CONFIG_RELPATH = Path("config") / "pipeline.yaml"
LEGACY_ENV_FILES = (
    Path("scraper") / "ballerinascraper" / ".env",
    Path("sft-dataset-generator") / ".env",
)


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or inconsistent."""


def find_repo_root(start: Path | None = None) -> Path:
    """Locate the repo root (the directory containing config/pipeline.yaml)."""
    env_root = os.environ.get("BALPIPE_ROOT")
    if env_root:
        return Path(env_root).resolve()
    candidates = [Path(start or Path.cwd()).resolve(), Path(__file__).resolve()]
    for base in candidates:
        for parent in (base, *base.parents):
            if (parent / CONFIG_RELPATH).is_file():
                return parent
    raise ConfigError(
        f"Could not find {CONFIG_RELPATH} above {Path.cwd()}. Run from inside the repo or set BALPIPE_ROOT."
    )


# --------------------------------------------------------------------------- #
# Sections (all overridable from YAML / BALPIPE__ env vars)
# --------------------------------------------------------------------------- #
class BallerinaIoCfg(BaseModel):
    sitemap: str = "https://ballerina.io/sitemap.xml"
    include_prefixes: list[str] = Field(default_factory=lambda: ["/learn/", "/spec/lang/2024R1/"])
    # regexes on the URL path; old-version and non-content paths
    exclude_patterns: list[str] = Field(
        default_factory=lambda: [r"/\d+\.\d+(\.\d+)?/", r"/v\d+-\d+/", r"/swan-lake/", r"/learn/api-docs/",
                                 r"^/learn/$", r"^/learn/by-example/$"]
    )


class GithubCfg(BaseModel):
    org: str = "ballerina-platform"
    bbe_repo: str = "ballerina-distribution"  # BBE sources: examples/<name>/{.md,.bal,.out}
    bbe_path: str = "examples"
    bbe_ref: str = "v{swan_lake}"  # release tag matching the pinned Swan Lake update
    module_repo_patterns: list[str] = Field(default_factory=lambda: [r"^module-ballerina-", r"^module-ballerinax-"])
    # files collected from each module repo (globs on the repo path)
    module_include: list[str] = Field(
        default_factory=lambda: ["README.md", "ballerina/Module.md", "ballerina/Package.md", "ballerina/README.md",
                                 "ballerina/*.bal"]
    )
    module_exclude: list[str] = Field(default_factory=lambda: ["ballerina/tests/**", "**/test*.bal"])
    max_file_bytes: int = 300_000


class CentralCfg(BaseModel):
    api: str = "https://api.central.ballerina.io/2.0"
    orgs: list[str] = Field(default_factory=lambda: ["ballerina", "ballerinax"])
    page_size: int = 50
    include_api_docs: bool = True


class SourcesCfg(BaseModel):
    swan_lake: str = "2201.14.1"  # the one Swan Lake update every phase is pinned to
    lock_file: str = "config/sources.lock.json"
    # processing order; BBE first (highest priority)
    order: list[str] = Field(default_factory=lambda: ["bbe", "spec", "learn", "central", "github"])
    ballerina_io: BallerinaIoCfg = Field(default_factory=BallerinaIoCfg)
    github: GithubCfg = Field(default_factory=GithubCfg)
    central: CentralCfg = Field(default_factory=CentralCfg)


class RepoGroupCfg(BaseModel):
    """One group of repositories for `collect-repos`."""

    name: str
    org: str | None = None            # list every public repo of this org ...
    query: str | None = None          # ... or a GitHub repository search (sorted by stars)
    repos: list[str] = Field(default_factory=list)  # ... or an explicit owner/name list
    include: list[str] = Field(default_factory=list)  # regexes on the repo name; empty = all
    exclude: list[str] = Field(default_factory=list)
    intended_use: str = "corpus"      # written to the licence register for new rows
    max_search_results: int = 1000    # GitHub search returns at most 1,000


class ReposCfg(BaseModel):
    """`collect-repos`: blobless sparse snapshots of Ballerina repositories, licence-gated."""

    register_file: str = "docs/licence-register.csv"
    # earlier groups win when the same file appears in several repos
    groups: list[RepoGroupCfg] = Field(default_factory=lambda: [
        RepoGroupCfg(name="platform", org="ballerina-platform",
                     include=[r"^module-ballerina-", r"^module-ballerinax-"],
                     intended_use="corpus; commit-mining"),
        RepoGroupCfg(name="guides", org="ballerina-guides", intended_use="codebase-pool; commit-mining"),
        RepoGroupCfg(name="wso2", query="org:wso2 language:Ballerina fork:false",
                     intended_use="codebase-pool; corpus"),
        RepoGroupCfg(name="external",
                     query=("language:Ballerina fork:false archived:false pushed:>=2022-02-01 "
                            "-org:ballerina-platform -org:ballerina-guides -org:wso2 -org:wso2-enterprise"),
                     intended_use="corpus; codebase-pool candidate"),
    ])
    # git sparse-checkout patterns (gitignore syntax): only these blobs are downloaded
    sparse_patterns: list[str] = Field(default_factory=lambda: [
        "*.bal", "Ballerina.toml", "*.out", "README.md", "Package.md", "Module.md",
        "/LICENSE*", "/LICENCE*", "/COPYING*"])
    exclude_paths: list[str] = Field(default_factory=lambda: ["**/target/**", "**/node_modules/**", "**/.git/**"])
    max_file_bytes: int = 300_000     # larger files are recorded as too_large, text not stored
    max_repo_kb: int = 0              # skip repos larger than this (GitHub size, KB); 0 = no limit
    workers: int = 4                  # parallel clones
    clone_timeout_s: int = 900
    keep_snapshots: bool = True       # write data/repos/<owner>__<repo>@<sha12>.tar.gz
    near_duplicate_threshold: float = 0.8  # MinHash similarity of .bal files


class CrawlCfg(BaseModel):
    user_agent: str = (
        "BallerinaCorpusBot/2.1 (+https://github.com/sajith-jeewantha/ballerina-sft-dataset-pipeline)"
    )
    timeout_s: int = 60
    retries: int = 3
    respect_robots: bool = True
    # Documented JSON APIs, used under their API terms; robots.txt governs crawling of web pages.
    # Neither host serves a robots.txt today (404 = no restrictions), so this only skips a lookup.
    robots_exempt_hosts: list[str] = Field(default_factory=lambda: ["api.github.com", "api.central.ballerina.io"])
    listing_cache_hours: float = 24.0  # sitemap / repo list / tree / package list responses are reused this long


class ChunkCfg(BaseModel):
    max_tokens: int = 800  # approximate tokens (chars / 4); a chunk is the context for one LLM call
    min_tokens: int = 40   # smaller chunks are kept but not used for questions


class GenerateCfg(BaseModel):
    concurrency: int = 4
    requests_per_minute: int = 60
    question_temperature: float = 0.7
    answer_temperature: float = 0.2
    max_question_tokens: int = 1024
    max_answer_tokens: int = 2048
    request_timeout_s: float = 120.0
    max_retries: int = 4
    max_chunk_attempts: int = 3          # a chunk whose question call keeps failing is given up after this
    near_duplicate_threshold: float = 0.85  # word-set Jaccard similarity between questions
    # Provider-specific request fields. OpenRouter: no reasoning tokens, and return the cost of each call.
    extra_body: dict[str, Any] = Field(
        default_factory=lambda: {"reasoning": {"effort": "none"}, "usage": {"include": True}}
    )
    # Optional fallback prices (USD per 1M tokens) when the provider does not return a cost:
    # {"qwen/qwen3.8-flash": {"input": 0.15, "output": 0.47}}
    pricing: dict[str, dict[str, float]] = Field(default_factory=dict)
    # OpenRouter provider routing per model-id prefix, sent as the request's `provider` field.
    # DeepSeek's own terms explicitly allow distillation; other hosts have their own terms,
    # so DeepSeek models are pinned to the DeepSeek endpoint with no fallback, and prompts are
    # not shared for training by the provider.
    providers: dict[str, dict[str, Any]] = Field(
        default_factory=lambda: {
            "deepseek/": {"order": ["deepseek"], "allow_fallbacks": False, "data_collection": "deny"},
        }
    )

    def provider_for(self, model: str) -> dict[str, Any] | None:
        """Routing for `model`: the longest matching prefix wins."""
        matches = [p for p in self.providers if model.startswith(p)]
        return self.providers[max(matches, key=len)] if matches else None


class ValidateCfg(BaseModel):
    bal_path: str = "bal"
    workers: int = 2
    timeout_s: int = 60
    java_heap: str = "1g"
    offline: bool = True
    prewarm_modules: list[str] = Field(default_factory=lambda: ["ballerina/io", "ballerina/http", "ballerina/log"])
    auto_imports: dict[str, str] = Field(
        default_factory=lambda: {
            "io": "ballerina/io",
            "http": "ballerina/http",
            "log": "ballerina/log",
            "time": "ballerina/time",
            "regex": "ballerina/lang.regexp",
            "value": "ballerina/lang.value",
        }
    )


class ExportCfg(BaseModel):
    path: str = "dataset.jsonl"  # relative to LOCAL_DATA_DIR
    include_invalid: bool = False  # answers whose code failed to compile are left out by default


class SftCfg(BaseModel):
    """`export-sft`: chat-format (`messages`) files for supervised fine-tuning."""

    dir: str = "sft"                 # under LOCAL_DATA_DIR: train.jsonl, eval.jsonl, metadata.jsonl
    eval_fraction: float = Field(default=0.05, ge=0.0, lt=1.0)  # split by source page, never by row
    strip_source_line: bool = True   # drop the trailing "Source: <url>" so the model does not learn to invent URLs
    # Repeated in every training row; use exactly the same text at inference. "" = no system message.
    system_prompt: str = (
        "You are an expert Ballerina (Swan Lake) programmer. Answer questions about the Ballerina language, "
        "its standard library, connectors and tools accurately and concisely. When code helps, give complete, "
        "idiomatic Ballerina that compiles, with the imports it needs, in a ```ballerina code block. Use only "
        "real Ballerina syntax and APIs; if you are not sure something exists or how it behaves, say so "
        "instead of guessing."
    )


PILOT_NODES = ["lib.http", "topic.rest_api", "concept.services_listeners", "concept.resource_methods",
               "concept.client_objects", "lib.auth", "lib.jwt", "lib.oauth2", "lib.mime"]
TASK_TYPES = ["write_code", "use_api", "explain", "fix_code", "design"]


class PromptsCfg(BaseModel):
    """`plan-prompts` / `generate-prompts`: prompts planned per graph item, written and reviewed."""

    set_name: str = "pilot"           # outputs go to <data>/prompts/<set_name>/
    nodes: list[str] = Field(default_factory=lambda: list(PILOT_NODES))  # [] = every active node
    # prompts per item, by the node's priority; spread over the item's difficulties and the task mix
    budgets: dict[str, int] = Field(default_factory=lambda: {"core": 10, "important": 6, "long_tail": 3})
    task_mix: dict[str, float] = Field(default_factory=lambda: {
        "write_code": 0.35, "use_api": 0.2, "explain": 0.2, "fix_code": 0.15, "design": 0.1})
    max_rounds: int = 3               # writer calls per item (first try + gap-fill for rejected slots)
    writer_temperature: float = 0.8
    reviewer_temperature: float = 0.0
    max_writer_tokens: int = 4000
    max_reviewer_tokens: int = 2500
    min_score: int = 3                # every review score (1-5) must reach this to accept
    near_duplicate_threshold: float = 0.8   # word-set Jaccard between prompts
    # grounding passed to the writer and the reviewer
    max_grounding_tokens: int = 3500  # approximate (chars / 4)
    chunks_per_source: int = 2        # best-matching chunks of each matched source page
    fts_neighbours: int = 2           # extra chunks found by full-text search on the item text
    max_symbols: int = 6              # Central API entries (signatures, fields) named in the item


class AnswersCfg(BaseModel):
    """`answer-prompts`: teacher answers to accepted prompts, checked with `bal` and repaired."""

    max_attempts: int = Field(default=3, ge=1)  # first answer + up to 2 repairs with the real errors
    temperature: float = 0.2
    max_tokens: int = 4096
    # Tasks whose answer must contain Ballerina code; and tasks whose prose a judge always checks.
    # Any other answer without code is judged too.
    code_tasks: list[str] = Field(default_factory=lambda: ["write_code", "fix_code"])
    judge_tasks: list[str] = Field(default_factory=lambda: ["explain", "design"])
    judge_min_score: int = 3           # every judge score (1-5) must reach this
    max_judge_tokens: int = 1200
    # `bal test` runs tests that the answer itself contains. That EXECUTES model-written code on this
    # machine (secrets are stripped from its environment, and it is killed at the timeout). Off by
    # default: turn it on in a sandbox (container/VM). Without it, test files are compiled only.
    run_tests: bool = False
    max_feedback_errors: int = 8       # compiler errors passed back to the teacher per repair


class CodebasesCfg(BaseModel):
    """`codebases`: the codebase pool, its states, and codebase-task prompts (M7)."""

    pool_size: int = 100              # verified projects to keep (plan: 50-150)
    eval_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)  # held out for eval tasks (M8), by id hash
    split_salt: str = "codebase-split"
    groups: list[str] = Field(default_factory=lambda: ["guides", "wso2", "external", "platform"])  # preference
    min_files: int = 2
    max_files: int = 40
    min_tokens: int = 800
    max_tokens: int = 10000           # so the whole project fits the prompt with room for the answer
    max_per_repo: int = 3
    kinds: list[str] = Field(default_factory=lambda: ["compile_error", "unfinished_feature", "version_drift",
                                                      "failing_test"])
    # failing_test states need the project's tests RUN (third-party code). Off by default, like
    # answers.run_tests; without it those states are recorded as skipped and not used.
    run_tests: bool = False
    build_timeout_s: int = 300        # first builds pull dependencies from Central
    context_max_tokens: int = 14000   # code packed into a codebase prompt (Qwen3.8 server budget ~16k)
    max_candidates: int = 250         # candidates tried per run while filling the pool


class BenchmarkSource(BaseModel):
    """A public coding benchmark translated into Ballerina (`eval fetch` / `eval translate`)."""

    name: str                         # humaneval | mbpp (also the eval suite name)
    repo: str                         # GitHub owner/name
    file: str                         # problems file in the repo (.jsonl or .jsonl.gz)
    spdx: str                         # licence of the problems
    licence_note: str = ""            # duties (attribution) recorded in the register and the dataset card
    task_ids: list[int] = Field(default_factory=list)  # [first, last] task ids to use; [] = all


def _benchmarks() -> list[BenchmarkSource]:
    return [
        BenchmarkSource(name="humaneval", repo="openai/human-eval", file="data/HumanEval.jsonl.gz", spdx="MIT",
                        licence_note="MIT (repo LICENSE): keep the copyright notice"),
        BenchmarkSource(name="mbpp", repo="google-research/google-research", file="mbpp/mbpp.jsonl",
                        spdx="CC-BY-4.0", task_ids=[11, 510],
                        licence_note="dataset released under CC BY 4.0 (dataset card; the repo code is Apache-2.0): "
                                     "attribute Austin et al. 2021 in the dataset card"),
    ]


class CorpusCfg(BaseModel):
    """`corpus`: the raw pretraining corpus from collected repos (M9)."""

    version: str = "raw-v1"           # <data>/corpus/<version>/
    part_tokens: int = 5_000_000      # approximate tokens per part file
    generated_weight: float = 0.3     # sampling weight of generated code (OpenAPI connectors etc.)
    min_tokens: int = 20              # smaller files are left out
    include_eval_codebases: bool = False  # eval-split codebase files never go in unless this is set


class MinerCfg(BaseModel):
    """`mine-fixes`: before/after bug fixes mined from repo history (M9)."""

    groups: list[str] = Field(default_factory=lambda: ["guides", "platform", "wso2"])
    max_commits: int = 400            # most recent non-merge commits scanned per repo
    max_files: int = 3                # .bal files changed by a fix commit
    max_lines: int = 80               # added + deleted lines
    max_fixes_per_repo: int = 25
    clone_timeout_s: int = 900


class AssembleCfg(BaseModel):
    """`assemble`: dataset v1 from verified answers, Q&A, (optionally) mined fixes and the raw corpus (M10)."""

    version: str = "v1"               # <data>/datasets/<version>/
    prompt_sets: list[str] = Field(default_factory=list)  # [] = prompts.set_name
    include_questions: bool = True    # chunk-grounded Q&A (generate-questions/answers), `invalid` left out
    include_mined_fixes: bool = False  # mined fixes are not built at their commit: opt in after checking them
    mined_fix_max_lines: int = 40     # only small fixes become fix_code samples
    # most samples kept per graph item, by priority (spread over task, difficulty and context mode)
    caps: dict[str, int] = Field(default_factory=lambda: {"core": 14, "important": 9, "long_tail": 5})
    max_per_page: int = 6             # Q&A samples kept per source page when no graph item is known
    near_duplicate_threshold: float = 0.85  # MinHash similarity between prompts
    eval_fraction: float = Field(default=0.03, ge=0.0, lt=1.0)  # validation split, by group (codebase, page, prompt)
    split_salt: str = "dataset-split"
    dpo: bool = True                  # dpo/pairs.jsonl from repaired answers (train split only)
    cpt: bool = True                  # copy the raw corpus (corpus.version) into cpt/


class EvalCfg(BaseModel):
    """`build-eval` (eval set v1, decontamination) and `eval` (eval set v2, baselines)."""

    version: str = "v1"               # files go to <data>/eval/<version>/
    dir: str = "eval"                 # relative to LOCAL_DATA_DIR
    bbe_eval_fraction: float = Field(default=0.30, gt=0.0, lt=1.0)  # share of BBE examples held out
    split_salt: str = "bbe-split"     # changing it reshuffles the split: don't, once training data exists
    verify: bool = True               # bal run / bal build every eval item against the pinned toolchain
    run_timeout_s: int = 120
    workers: int = 2
    # Decontamination: a text overlaps an eval item when it has the same code (comments and layout
    # ignored), a MinHash similarity >= near_duplicate_threshold, or contains >= containment of the
    # item's distinctive 5-token shingles (shingles found in >= common_df BBE examples don't count).
    near_duplicate_threshold: float = 0.8
    containment: float = Field(default=0.5, gt=0.0, le=1.0)
    min_shingles: int = 15            # items with fewer distinctive shingles are matched exactly only
    common_df: int = 10
    # --- eval set v2 (M8) ---
    benchmarks: list[BenchmarkSource] = Field(default_factory=_benchmarks)
    translate_attempts: int = 3       # teacher translation + repairs with the real bal errors
    ecosystem_tasks_per_node: int = 2  # tasks the teacher writes per core graph node
    # baselines: label -> OpenRouter model id; "teacher" defaults to ANSWER_GEN_MODEL
    models: dict[str, str] = Field(default_factory=lambda: {"base": "qwen/qwen3.8-27b"})
    samples: int = 5                  # answers per item per model, for pass@k
    ks: list[int] = Field(default_factory=lambda: [1, 5])
    sample_temperature: float = 0.8
    max_answer_tokens: int = 3000
    # Scoring with tests (benchmarks) or by running programs (BBE output) EXECUTES model-written code.
    # Off by default: scores are then compile-only (and the judge), recorded as mode "compile".
    run_tests: bool = False


class GraphCfg(BaseModel):
    path: str = "graph/graph.json"         # export of ballerina-ecosystem (schema 0.2)
    source_file: str = "graph/SOURCE.json"  # upstream repo, commit and sha256 of graph.json


class ReportCfg(BaseModel):
    path: str = "reports/report.html"  # relative to LOCAL_DATA_DIR
    samples: int = 5


class StorageCfg(BaseModel):
    """What is copied to the bucket when STORAGE_BUCKET=true (see storage.py)."""

    snapshot_db: bool = True      # upload a gzipped copy of pipeline.db after each command
    keep_db_snapshots: int = Field(default=20, ge=0)  # older snapshots are deleted; 0 = keep all
    exclude: list[str] = Field(default_factory=lambda: ["cache/**", ".storage/**"])  # never uploaded


class LoggingCfg(BaseModel):
    level: str = "INFO"
    format: str = "text"


# --------------------------------------------------------------------------- #
# Root settings
# --------------------------------------------------------------------------- #
_ACTIVE_YAML: Path | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BALPIPE__",
        env_nested_delimiter="__",
        env_ignore_empty=True,
        extra="ignore",
        populate_by_name=True,
    )

    # --- secrets, models and run limits (.env) ---
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENROUTER_API_KEY", "LLM_API_KEY", "API_KEY")
    )
    llm_base_url: str = Field(default="https://openrouter.ai/api/v1", validation_alias=AliasChoices("LLM_BASE_URL"))
    question_gen_model: str | None = Field(default=None, validation_alias=AliasChoices("QUESTION_GEN_MODEL"))
    answer_gen_model: str | None = Field(default=None, validation_alias=AliasChoices("ANSWER_GEN_MODEL"))
    # M5: DeepSeek writes the prompts; a judge model of a different family reviews them
    prompt_writer_model: str | None = Field(default=None, validation_alias=AliasChoices("PROMPT_WRITER_MODEL"))
    judge_model: str | None = Field(default=None, validation_alias=AliasChoices("JUDGE_MODEL"))
    github_token: SecretStr | None = Field(default=None, validation_alias=AliasChoices("GITHUB_TOKEN"))
    limit_generating: int = Field(default=10, ge=0, validation_alias=AliasChoices("LIMIT_GENERATING"))
    limit_scrape_pages: int = Field(default=20, ge=0, validation_alias=AliasChoices("LIMIT_SCRAPE_PAGES"))
    questions_per_chunk: int = Field(default=3, ge=1, le=20, validation_alias=AliasChoices("QUESTIONS_PER_CHUNK"))
    request_delay_seconds: float = Field(default=1.0, ge=0, validation_alias=AliasChoices("REQUEST_DELAY_SECONDS"))
    limit_repos: int = Field(default=20, ge=0, validation_alias=AliasChoices("LIMIT_REPOS"))

    # --- storage switch (.env) ---
    storage_bucket: bool = Field(default=False, validation_alias=AliasChoices("STORAGE_BUCKET"))
    storage_provider: Literal["gcs", "aws-s3", "local"] = Field(
        default="gcs", validation_alias=AliasChoices("STORAGE_PROVIDER"))  # local = a folder, for testing
    bucket_name: str | None = Field(default=None, validation_alias=AliasChoices("BUCKET_NAME"))
    bucket_prefix: str = Field(default="", validation_alias=AliasChoices("BUCKET_PREFIX"))
    aws_profile: str | None = Field(default=None, validation_alias=AliasChoices("AWS_PROFILE"))
    aws_region: str | None = Field(default=None, validation_alias=AliasChoices("AWS_REGION"))
    s3_endpoint_url: str | None = Field(default=None, validation_alias=AliasChoices("S3_ENDPOINT_URL"))

    # --- pipeline settings (pipeline.yaml) ---
    data_dir: str = Field(default="data", validation_alias=AliasChoices("LOCAL_DATA_DIR", "data_dir"))
    db_file: str = "pipeline.db"
    sources: SourcesCfg = Field(default_factory=SourcesCfg)
    crawl: CrawlCfg = Field(default_factory=CrawlCfg)
    chunk: ChunkCfg = Field(default_factory=ChunkCfg)
    generate: GenerateCfg = Field(default_factory=GenerateCfg)
    validate_: ValidateCfg = Field(default_factory=ValidateCfg, alias="validate")
    export: ExportCfg = Field(default_factory=ExportCfg)
    sft: SftCfg = Field(default_factory=SftCfg)
    eval_: EvalCfg = Field(default_factory=EvalCfg, alias="eval")
    prompts: PromptsCfg = Field(default_factory=PromptsCfg)
    answers: AnswersCfg = Field(default_factory=AnswersCfg)
    codebases: CodebasesCfg = Field(default_factory=CodebasesCfg)
    corpus: CorpusCfg = Field(default_factory=CorpusCfg)
    miner: MinerCfg = Field(default_factory=MinerCfg)
    assemble: AssembleCfg = Field(default_factory=AssembleCfg)
    graph: GraphCfg = Field(default_factory=GraphCfg)
    repos: ReposCfg = Field(default_factory=ReposCfg)
    report: ReportCfg = Field(default_factory=ReportCfg)
    storage: StorageCfg = Field(default_factory=StorageCfg)
    logging: LoggingCfg = Field(default_factory=LoggingCfg)

    # set by load_settings
    repo_root: Path = Field(default_factory=Path.cwd, exclude=True)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        if _ACTIVE_YAML is not None:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=_ACTIVE_YAML))
        return tuple(sources)

    # ------------------------------------------------------------------ #
    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.repo_root / p

    @property
    def data_root(self) -> Path:
        return self.path(self.data_dir)

    def data_path(self, rel: str) -> Path:
        """An output path under LOCAL_DATA_DIR. Older configs wrote `data/...`; that prefix is dropped."""
        p = Path(rel)
        if p.is_absolute():
            return p
        if p.parts and p.parts[0] == "data" and len(p.parts) > 1:
            p = Path(*p.parts[1:])
        return self.data_root / p

    @property
    def export_path(self) -> Path:
        return self.data_path(self.export.path)

    @property
    def sft_dir(self) -> Path:
        return self.data_path(self.sft.dir)

    @property
    def writer_model(self) -> str | None:
        """PROMPT_WRITER_MODEL, else the teacher (ANSWER_GEN_MODEL)."""
        return self.prompt_writer_model or self.answer_gen_model

    @property
    def reviewer_model(self) -> str | None:
        """JUDGE_MODEL, else QUESTION_GEN_MODEL."""
        return self.judge_model or self.question_gen_model

    @property
    def prompts_dir(self) -> Path:
        return self.data_path("prompts") / self.prompts.set_name

    @property
    def corpus_dir(self) -> Path:
        return self.data_path("corpus") / self.corpus.version

    @property
    def dataset_dir(self) -> Path:
        return self.data_path("datasets") / self.assemble.version

    @property
    def codebases_dir(self) -> Path:
        return self.data_path("codebases")

    @property
    def answers_dir(self) -> Path:
        return self.data_path("answers") / self.prompts.set_name

    @property
    def eval_dir(self) -> Path:
        """<data>/eval/<version>/: the eval items and their fingerprints."""
        return self.data_path(self.eval_.dir) / self.eval_.version

    @property
    def report_path(self) -> Path:
        return self.data_path(self.report.path)

    @property
    def db_path(self) -> Path:
        return self.data_root / self.db_file

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    def limits(self) -> dict[str, Any]:
        """The run limits, as printed at startup and stored on every run."""
        return {
            "LIMIT_SCRAPE_PAGES": self.limit_scrape_pages,
            "LIMIT_GENERATING": self.limit_generating,
            "QUESTIONS_PER_CHUNK": self.questions_per_chunk,
            "REQUEST_DELAY_SECONDS": self.request_delay_seconds,
            "LIMIT_REPOS": self.limit_repos,
        }

    def require(self, *fields: str) -> None:
        """Fail fast when a command needs settings that are unset."""
        missing = [f for f in fields if getattr(self, f) in (None, "")]
        if missing:
            env_names = ", ".join(f.upper() if f != "llm_api_key" else "OPENROUTER_API_KEY" for f in missing)
            raise ConfigError(f"Missing required settings: {env_names}. Set them in .env (see .env.example).")


def load_settings(config_path: str | Path | None = None, root: Path | None = None) -> Settings:
    """Build Settings from env, .env files and the YAML config."""
    global _ACTIVE_YAML
    repo_root = root or find_repo_root()
    yaml_path = Path(config_path) if config_path else repo_root / CONFIG_RELPATH
    if not yaml_path.is_file():
        raise ConfigError(f"Config file not found: {yaml_path}")

    env_files: list[Path] = []
    for legacy in LEGACY_ENV_FILES:  # lower priority; kept so existing setups keep working
        if (repo_root / legacy).is_file():
            env_files.append(repo_root / legacy)
            log.warning("Reading legacy env file %s; move its keys to %s", legacy, repo_root / ".env")
    if (repo_root / ".env").is_file():
        env_files.append(repo_root / ".env")  # last file wins

    _ACTIVE_YAML = yaml_path
    try:
        settings = Settings(_env_file=tuple(env_files) or None, repo_root=repo_root)  # type: ignore[call-arg]
    finally:
        _ACTIVE_YAML = None
    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
