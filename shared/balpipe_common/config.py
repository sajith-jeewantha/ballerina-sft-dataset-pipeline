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
from typing import Any

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
    path: str = "data/dataset.jsonl"
    include_invalid: bool = False  # answers whose code failed to compile are left out by default


class SftCfg(BaseModel):
    """`export-sft`: chat-format (`messages`) files for supervised fine-tuning."""

    dir: str = "data/sft"            # train.jsonl, eval.jsonl, metadata.jsonl
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


class GraphCfg(BaseModel):
    path: str = "graph/graph.json"         # export of ballerina-ecosystem (schema 0.2)
    source_file: str = "graph/SOURCE.json"  # upstream repo, commit and sha256 of graph.json


class ReportCfg(BaseModel):
    path: str = "reports/report.html"
    samples: int = 5


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
    github_token: SecretStr | None = Field(default=None, validation_alias=AliasChoices("GITHUB_TOKEN"))
    limit_generating: int = Field(default=10, ge=0, validation_alias=AliasChoices("LIMIT_GENERATING"))
    limit_scrape_pages: int = Field(default=20, ge=0, validation_alias=AliasChoices("LIMIT_SCRAPE_PAGES"))
    questions_per_chunk: int = Field(default=3, ge=1, le=20, validation_alias=AliasChoices("QUESTIONS_PER_CHUNK"))
    request_delay_seconds: float = Field(default=1.0, ge=0, validation_alias=AliasChoices("REQUEST_DELAY_SECONDS"))

    # --- pipeline settings (pipeline.yaml) ---
    data_dir: str = "data"
    db_file: str = "pipeline.db"
    sources: SourcesCfg = Field(default_factory=SourcesCfg)
    crawl: CrawlCfg = Field(default_factory=CrawlCfg)
    chunk: ChunkCfg = Field(default_factory=ChunkCfg)
    generate: GenerateCfg = Field(default_factory=GenerateCfg)
    validate_: ValidateCfg = Field(default_factory=ValidateCfg, alias="validate")
    export: ExportCfg = Field(default_factory=ExportCfg)
    sft: SftCfg = Field(default_factory=SftCfg)
    graph: GraphCfg = Field(default_factory=GraphCfg)
    report: ReportCfg = Field(default_factory=ReportCfg)
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
