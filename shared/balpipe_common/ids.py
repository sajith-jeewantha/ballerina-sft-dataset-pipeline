"""Deterministic IDs, hashes, timestamps and split assignment.

Every ID is derived from content or identity (never random) so re-running a
stage yields identical IDs; that is what makes stages idempotent and resumable.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Literal

_WS_RE = re.compile(r"\s+")

Split = Literal["train", "eval"]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def short_hash(*parts: str, n: int = 16) -> str:
    """Hash of several parts joined by a unit separator (avoids 'ab'+'c' == 'a'+'bc')."""
    return sha256_text("\x1f".join(parts))[:n]


def normalize_ws(text: str) -> str:
    """Collapse all whitespace runs to one space; used for dedup keys only."""
    return _WS_RE.sub(" ", text).strip()


def raw_id(source_uri: str, raw_sha256: str) -> str:
    return f"raw_{short_hash(source_uri, raw_sha256)}"


def doc_id(identity: str) -> str:
    """`identity` is the canonical public URL when known, else the source URI."""
    return f"doc_{short_hash(identity)}"


def example_id(doc_id_: str, ordinal: int, code_sha256: str) -> str:
    return f"ex_{short_hash(doc_id_, str(ordinal), code_sha256)}"


def chunk_id(doc_id_: str, chunk_text: str, ordinal: int) -> str:
    """Derived from the chunk's own text, so changed text can never keep a stale vector."""
    return f"chk_{doc_id_.removeprefix('doc_')}_{sha256_text(chunk_text)[:8]}_{ordinal:03d}"


def sft_id(task_type: str, seed_id: str, user_message: str) -> str:
    return f"sft_{short_hash(task_type, seed_id, normalize_ws(user_message))}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(stage: str) -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{stage}"


def assign_split(doc_id_: str, eval_fraction: float) -> Split:
    """Stable train/eval assignment by hashing the document ID.

    Splitting by document (not by record) keeps every derivative of an eval
    page out of training data.
    """
    if not 0.0 <= eval_fraction < 1.0:
        raise ValueError(f"eval_fraction must be in [0, 1): {eval_fraction}")
    bucket = int(sha256_text(f"split:{doc_id_}")[:8], 16) / 0x1_0000_0000
    return "eval" if bucket < eval_fraction else "train"
