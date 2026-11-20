"""LLM access: versioned prompts, an OpenAI-compatible async client (OpenRouter by
default), rate limiting, retries, a disk cache, usage/cost capture and JSON parsing.

Retries: the OpenAI SDK retries 408/409/429/5xx and connection errors with
exponential backoff and honours `Retry-After`; 400/401/403 are NOT retried.
Cache: responses are stored by sha256(model, messages, params), so re-running a
phase never pays twice for the same call (cached calls are recorded with cost 0).
Cost: OpenRouter returns the cost of each call (`usage.cost`); otherwise the
optional `generate.pricing` table is used; otherwise cost is unknown.
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
from balpipe_common.ids import sha256_text

T = TypeVar("T", bound=BaseModel)
JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)
FENCE_RE = re.compile(r"```[\w+#-]*\n(.*?)```", re.DOTALL)


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    cached: bool = False
    provider: str | None = None  # the host that served the call (OpenRouter `provider`), for licence audits


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
    def __init__(self, settings: Settings, cache_dir: Path) -> None:
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
        self.sem = asyncio.Semaphore(max(1, g.concurrency))
        self.rate = RateLimiter(g.requests_per_minute)
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / key[:2] / f"{key}.json"

    def _cost(self, model: str, usage: Any) -> float | None:
        extra = getattr(usage, "model_extra", None) or {}
        if isinstance(extra.get("cost"), (int, float)):
            return float(extra["cost"])
        price = self.settings.generate.pricing.get(model)
        if price and usage is not None:
            return (usage.prompt_tokens * price.get("input", 0) + usage.completion_tokens * price.get("output", 0)) / 1e6
        return None

    async def chat(self, model: str, prompt: Prompt, *, temperature: float, max_tokens: int,
                   **fmt: Any) -> tuple[str, Usage]:
        messages = prompt.messages(**fmt)
        params: dict[str, Any] = {"temperature": temperature, "max_tokens": max_tokens}
        extra = dict(self.settings.generate.extra_body)
        routing = self.settings.generate.provider_for(model)
        if routing:  # e.g. DeepSeek models only from DeepSeek's own endpoint (its terms allow distillation)
            extra["provider"] = routing
        # The routing is part of the cache key, so a pinned call never reuses a response that
        # an unpinned call may have got from another host.
        key = sha256_text(json.dumps({"model": model, "messages": messages, "params": params,
                                      "extra": {k: v for k, v in extra.items() if k != "usage"}}, sort_keys=True))
        cache = self._cache_path(key)
        if cache.is_file():
            data = json.loads(cache.read_text(encoding="utf-8"))
            u = data.get("usage") or {}
            return data["text"], Usage(model, u.get("prompt_tokens"), u.get("completion_tokens"), 0.0, cached=True,
                                       provider=data.get("provider"))

        async with self.sem:
            await self.rate.wait()
            resp = await self.client.chat.completions.create(
                model=model, messages=messages, extra_body=extra or None, **params  # type: ignore[arg-type]
            )
        choice = resp.choices[0] if resp.choices else None
        text = (choice.message.content or "").strip() if choice else ""
        if not text:
            raise LLMError(f"empty response from {model} (finish_reason={getattr(choice, 'finish_reason', None)})")
        served_by = (getattr(resp, "model_extra", None) or {}).get("provider")
        usage = Usage(model, provider=served_by)
        if resp.usage:
            usage = Usage(model, resp.usage.prompt_tokens, resp.usage.completion_tokens, self._cost(model, resp.usage),
                          provider=served_by)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"text": text, "provider": served_by,
                                     "usage": {"prompt_tokens": usage.prompt_tokens,
                                               "completion_tokens": usage.completion_tokens}}),
                         encoding="utf-8")
        return text, usage

    async def chat_json(self, model: str, prompt: Prompt, schema: type[T], *, temperature: float, max_tokens: int,
                        **fmt: Any) -> tuple[T, Usage]:
        text, usage = await self.chat(model, prompt, temperature=temperature, max_tokens=max_tokens, **fmt)
        return parse_json(text, schema), usage


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
