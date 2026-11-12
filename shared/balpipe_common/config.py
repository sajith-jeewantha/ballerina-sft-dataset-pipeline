"""Typed, validated configuration.

Precedence (highest first): process environment > `.env` > `config/pipeline.yaml`
> defaults. Any YAML key can be overridden with `BALPIPE__SECTION__KEY=value`.
Secrets are `SecretStr`, so they never appear in logs, reprs or manifests.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from balpipe_common.ids import sha256_text

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
class GithubRepoCfg(BaseModel):
    repo: str
    ref: str | None = None  # None = the repo's default branch
    include: list[str]
    exclude: list[str] = Field(default_factory=list)


class SourcesCfg(BaseModel):
    github_repos: list[GithubRepoCfg] = Field(default_factory=list)
    central_orgs: list[str] = Field(default_factory=lambda: ["ballerina"])
    central_page_size: int = 50
    central_max_packages: int | None = None
    web_sitemaps: list[str] = Field(default_factory=list)
    web_allowed_domains: list[str] = Field(default_factory=list)
    web_sitemap_follow: list[str] = Field(default_factory=lambda: [".*"])


class CrawlCfg(BaseModel):
    user_agent: str = "BallerinaCorpusBot/1.0 (+https://github.com/ballerina-platform; research corpus)"
    concurrent_per_domain: int = 2
    download_delay_s: float = 1.0
    download_timeout_s: int = 60
    retry_times: int = 3
    http_cache: bool = True
    collector_delay_s: float = 0.2


class CleanCfg(BaseModel):
    min_words: int = 30
    near_dup_threshold: float = 0.8
    minhash_num_perm: int = 128
    shingle_size: int = 5
    ballerina_signal_min: int = 2
    exclude_url_patterns: list[str] = Field(
        default_factory=lambda: [r"privacy-policy", r"cookie-policy", r"terms-of-(use|service)", r"/legal/", r"/careers/"]
    )


class ChunkCfg(BaseModel):
    name: str = "md_structural"
    version: str = "1.0.0"
    max_tokens: int = 300

    @property
    def slug(self) -> str:
        return f"{self.name}-{self.version}"


class IndexCfg(BaseModel):
    collection_prefix: str = "bal"
    batch_size: int = 64


class GenerateCfg(BaseModel):
    target_records: int = 500
    tasks: dict[str, float] = Field(
        default_factory=lambda: {
            "write_code": 0.25,
            "explain_code": 0.2,
            "debug_fix": 0.15,
            "convert_from_language": 0.1,
            "complete_code": 0.1,
            "concept": 0.1,
            "api_usage": 0.1,
        }
    )
    top_k: int = 4
    max_per_doc: int = 12
    concurrency: int = 8
    requests_per_minute: int = 60
    question_temperature: float = 0.8
    answer_temperature: float = 0.2
    max_answer_tokens: int = 2048
    request_timeout_s: float = 120.0
    max_retries: int = 4
    # Provider-specific request fields. The previous generator sent OpenRouter's
    # {"reasoning": {"effort": "none"}}; kept as the default to preserve behaviour.
    extra_body: dict[str, Any] = Field(default_factory=lambda: {"reasoning": {"effort": "none"}})
    system_prompt: str = "You are an expert Ballerina programmer. Answer precisely and use idiomatic Ballerina."
    source_languages: list[str] = Field(default_factory=lambda: ["Python", "Java", "Go"])

    @field_validator("tasks")
    @classmethod
    def _tasks_sum(cls, v: dict[str, float]) -> dict[str, float]:
        if not v or abs(sum(v.values()) - 1.0) > 1e-6:
            raise ValueError(f"generate.tasks weights must sum to 1.0, got {sum(v.values()):.3f}")
        return v


class ValidateCfg(BaseModel):
    enabled: bool = True
    bal_path: str = "bal"
    workers: int = 4
    timeout_s: int = 60
    java_heap: str = "1g"
    offline: bool = True
    run_bbe_output: bool = True
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


class JudgeCfg(BaseModel):
    thresholds: dict[str, int] = Field(
        default_factory=lambda: {
            "faithfulness": 4,
            "correctness": 4,
            "helpfulness": 3,
            "instruction_following": 3,
            "code_quality": 3,
        }
    )
    thresholds_version: str = "1"
    strict_family_check: bool = False
    concurrency: int = 8


class SplitCfg(BaseModel):
    eval_fraction: float = 0.05


class ExportCfg(BaseModel):
    near_dup_threshold: float = 0.92
    ngram_size: int = 13
    max_ngram_overlap: float = 0.2
    allowed_licenses: list[str] = Field(default_factory=lambda: ["Apache-2.0", "MIT", "BSD-3-Clause"])
    include_system_prompt: bool = True
    reject_unknown_symbols: bool = False


class LoggingCfg(BaseModel):
    level: str = "INFO"
    format: str = "json"


# --------------------------------------------------------------------------- #
# Root settings
# --------------------------------------------------------------------------- #
_ACTIVE_YAML: Path | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BALPIPE__",
        env_nested_delimiter="__",
        extra="ignore",
        populate_by_name=True,
    )

    # --- secrets & model choice (.env) ---
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENROUTER_API_KEY", "LLM_API_KEY", "API_KEY")
    )
    llm_base_url: str = Field(default="https://openrouter.ai/api/v1", validation_alias=AliasChoices("LLM_BASE_URL"))
    llm_provider: str = Field(default="openrouter", validation_alias=AliasChoices("LLM_PROVIDER"))
    question_gen_model: str | None = Field(default=None, validation_alias=AliasChoices("QUESTION_GEN_MODEL"))
    answer_gen_model: str | None = Field(default=None, validation_alias=AliasChoices("ANSWER_GEN_MODEL"))
    judge_model: str | None = Field(default=None, validation_alias=AliasChoices("JUDGE_MODEL"))
    embedding_model: str = Field(
        default="sentence-transformers/all-mpnet-base-v2", validation_alias=AliasChoices("EMBEDDING_MODEL")
    )
    github_token: SecretStr | None = Field(default=None, validation_alias=AliasChoices("GITHUB_TOKEN"))
    gcs_bucket: str | None = Field(
        default=None, validation_alias=AliasChoices("GCS_BUCKET", "GCS_BUCKET_NAME", "BUCKET_NAME")
    )

    # --- pipeline settings (pipeline.yaml) ---
    data_dir: str = "data"
    gcs_mirror: bool = False  # opt-in: copy stage outputs to gs://$GCS_BUCKET/<same path>
    sources: SourcesCfg = Field(default_factory=SourcesCfg)
    crawl: CrawlCfg = Field(default_factory=CrawlCfg)
    clean: CleanCfg = Field(default_factory=CleanCfg)
    chunk: ChunkCfg = Field(default_factory=ChunkCfg)
    index: IndexCfg = Field(default_factory=IndexCfg)
    generate: GenerateCfg = Field(default_factory=GenerateCfg)
    validate_: ValidateCfg = Field(default_factory=ValidateCfg, alias="validate")
    judge: JudgeCfg = Field(default_factory=JudgeCfg)
    split: SplitCfg = Field(default_factory=SplitCfg)
    export: ExportCfg = Field(default_factory=ExportCfg)
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

    @model_validator(mode="after")
    def _check_split(self) -> Settings:
        if not 0.0 <= self.split.eval_fraction < 1.0:
            raise ValueError("split.eval_fraction must be in [0, 1)")
        return self

    # ------------------------------------------------------------------ #
    @property
    def data_root(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else self.repo_root / p

    def data_path(self, *parts: str) -> Path:
        return self.data_root.joinpath(*parts)

    def require(self, *fields: str) -> None:
        """Fail fast when a stage needs settings that are unset."""
        missing = [f for f in fields if getattr(self, f) in (None, "")]
        if missing:
            env_names = ", ".join(f.upper() if f != "llm_api_key" else "OPENROUTER_API_KEY" for f in missing)
            raise ConfigError(f"Missing required settings: {env_names}. Set them in .env (see .env.example).")

    def require_llm(self, *, judge: bool = False) -> None:
        self.require("llm_api_key", "question_gen_model", "answer_gen_model", *(("judge_model",) if judge else ()))
        if judge:
            fam_a, fam_j = model_family(self.answer_gen_model or ""), model_family(self.judge_model or "")
            if fam_a == fam_j:
                msg = (
                    f"JUDGE_MODEL ({self.judge_model}) is from the same family as ANSWER_GEN_MODEL "
                    f"({self.answer_gen_model}); judges favour their own family. Use a different provider family."
                )
                if self.judge.strict_family_check:
                    raise ConfigError(msg)
                log.warning(msg, extra={"event": "config.judge_same_family"})

    def redacted_snapshot(self) -> dict[str, Any]:
        """JSON-safe config with secrets masked (SecretStr dumps as '**********')."""
        return self.model_dump(mode="json", by_alias=True)

    @property
    def config_sha256(self) -> str:
        return sha256_text(json.dumps(self.redacted_snapshot(), sort_keys=True))


def model_family(model_id: str) -> str:
    """'qwen/qwen3.8-flash' -> 'qwen'; 'anthropic/claude-x' -> 'anthropic'."""
    return model_id.split("/", 1)[0].lower() if "/" in model_id else model_id.split("-", 1)[0].lower()


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
            log.warning(
                "Reading legacy env file %s; move its keys to %s",
                legacy,
                repo_root / ".env",
                extra={"event": "config.legacy_env_file"},
            )
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
