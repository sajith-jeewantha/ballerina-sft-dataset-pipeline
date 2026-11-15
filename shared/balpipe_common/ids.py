"""Hashes, normalisation and timestamps."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

_WS_RE = re.compile(r"\s+")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def short_hash(*parts: str, n: int = 16) -> str:
    """Hash of several parts joined by a unit separator (avoids 'ab'+'c' == 'a'+'bc')."""
    return sha256_text("\x1f".join(parts))[:n]


def normalize_ws(text: str) -> str:
    """Collapse all whitespace runs to one space; used for hash keys only."""
    return _WS_RE.sub(" ", text).strip()


def content_hash(text: str) -> str:
    """Hash that ignores whitespace-only differences."""
    return sha256_text(normalize_ws(text))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
