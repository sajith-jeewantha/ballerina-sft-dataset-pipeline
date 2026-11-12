"""Embedding model wrapper (sentence-transformers), loaded lazily and once."""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache

import numpy as np


@lru_cache(maxsize=2)
def _model(name: str):  # type: ignore[no-untyped-def]
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(name)


def load_tokenizer(name: str) -> tuple[Callable[[str], int], int]:
    """Token counter matching the embedding model, and its max sequence length."""
    model = _model(name)
    tok = model.tokenizer

    def count(text: str) -> int:
        return len(tok.encode(text, add_special_tokens=False))

    return count, int(model.max_seq_length)


def embed(name: str, texts: list[str], batch_size: int = 64) -> np.ndarray:
    """L2-normalized embeddings (cosine similarity == dot product)."""
    return _model(name).encode(texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False)


def slug(name: str) -> str:
    return name.split("/")[-1].lower().replace("_", "-")
