"""LLM access: versioned prompts, an OpenAI-compatible async client (OpenRouter by
default), rate limiting, retries, a disk cache and JSON-output parsing.

Retries: the OpenAI SDK retries 408/409/429/5xx and connection errors with
exponential backoff and honours `Retry-After`; 400/401/403 are NOT retried.
Cache: responses are stored by sha256(model, messages, params), so resuming or
re-running a stage never pays twice for the same call.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from balpipe_common.config import Settings
from balpipe_common.ids import sha256_text, utc_now_iso
from balpipe_common.schemas import LLMCall

T = TypeVar("T", bound=BaseModel)
JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)
FENCE_RE = re.compile(r"```[\w+#-]*\n(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# Prompts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    user: str

    def messages(self, **kw: Any) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system.format(**kw).strip()},
            {"role": "user", "content": self.user.format(**kw).strip()},
        ]


@lru_cache(maxsize=None)
def load_prompt(prompt_id: str) -> Prompt:
    text = resources.files("sftgen.prompts").joinpath(f"{prompt_id}.yaml").read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    return Prompt(id=data["id"], version=str(data["version"]), system=data.get("system", ""), user=data["user"])


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Spaces request starts so at most `rpm` begin per minute."""

    def __init__(self, rpm: int) -> None:
        self.interval = 60.0 / max(1, rpm)
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next - now
            self._next = max(now, self._next) + self.interval
        if delay > 0:
            await asyncio.sleep(delay)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #
class LLMClient:
    def __init__(self, settings: Settings, cache_dir: Path, *, concurrency: int | None = None) -> None:
        from openai import AsyncOpenAI

        settings.require("llm_api_key")
        g = settings.generate
        self.settings = settings
        self.client = AsyncOpenAI(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key.get_secret_value(),  # type: ignore[union-attr]
            max_retries=g.max_retries,
            timeout=g.request_timeout_s,
        )
        self.sem = asyncio.Semaphore(concurrency or g.concurrency)
        self.rate = RateLimiter(g.requests_per_minute)
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    async def chat(self, model: str, prompt: Prompt, *, temperature: float, max_tokens: int,
                   **fmt: Any) -> tuple[str, LLMCall]:
        messages = prompt.messages(**fmt)
        params: dict[str, Any] = {"temperature": temperature, "max_tokens": max_tokens}
        extra = self.settings.generate.extra_body
        key = sha256_text(json.dumps({"model": model, "messages": messages, "params": params, "extra": extra},
                                     sort_keys=True))
        call = LLMCall(model=model, provider=self.settings.llm_provider, prompt_id=prompt.id,
                       prompt_version=prompt.version, params=params, created_at=utc_now_iso())
        cache = self._cache_path(key)
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            call.usage, call.cached, call.created_at = data.get("usage"), True, data.get("created_at", call.created_at)
            return data["text"], call

        async with self.sem:
            await self.rate.wait()
            resp = await self.client.chat.completions.create(
                model=model, messages=messages, extra_body=extra or None, **params  # type: ignore[arg-type]
            )
        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content or "").strip() if choice else ""
        if not text:
            raise LLMError(f"empty response from {model} (finish_reason={getattr(choice, 'finish_reason', None)})")
        if resp.usage:
            call.usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"text": text, "usage": call.usage, "created_at": call.created_at}), encoding="utf-8")
        return text, call

    async def chat_json(self, model: str, prompt: Prompt, schema: type[T], *, temperature: float, max_tokens: int,
                        **fmt: Any) -> tuple[T, LLMCall]:
        text, call = await self.chat(model, prompt, temperature=temperature, max_tokens=max_tokens, **fmt)
        return parse_json(text, schema), call


def parse_json(text: str, schema: type[T]) -> T:
    candidates = [text]
    m = JSON_RE.search(text)
    if m:
        candidates.append(m.group(0))
    last: Exception | None = None
    for c in candidates:
        try:
            return schema.model_validate(json.loads(c))
        except (json.JSONDecodeError, ValidationError) as exc:
            last = exc
    raise LLMError(f"could not parse {schema.__name__} from model output: {last}")


def first_fence(text: str) -> str | None:
    m = FENCE_RE.search(text)
    return m.group(1).strip("\n") if m else None
