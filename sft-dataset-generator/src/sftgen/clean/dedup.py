"""Exact + near-duplicate detection (MinHash LSH over word shingles).

Duplicates are marked, not deleted, and the most canonical copy wins:
github > central > web, then the longer document, then the smaller doc_id.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from datasketch import MinHash, MinHashLSH

from balpipe_common.ids import normalize_ws, sha256_text

SOURCE_RANK = {"github": 0, "central": 1, "web": 2}
_WORD_RE = re.compile(r"\w+")


def content_sha256(markdown: str) -> str:
    return sha256_text(normalize_ws(markdown))


def shingles(text: str, k: int) -> set[str]:
    words = _WORD_RE.findall(text.lower())
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def minhash(text: str, k: int, num_perm: int) -> MinHash:
    m = MinHash(num_perm=num_perm)
    for s in shingles(text, k):
        m.update(s.encode("utf-8"))
    return m


@dataclass
class DedupResult:
    status: str  # unique | exact_dup | near_dup
    duplicate_of: str | None = None
    cluster: str | None = None


def dedup(
    items: Sequence[object],
    *,
    key: Callable[[object], str],
    text: Callable[[object], str],
    rank: Callable[[object], tuple],
    threshold: float,
    shingle_size: int = 5,
    num_perm: int = 128,
) -> dict[str, DedupResult]:
    """Return {key: DedupResult}. Items are visited in canonical order (best first)."""
    ordered = sorted(items, key=rank)
    results: dict[str, DedupResult] = {}
    by_hash: dict[str, str] = {}
    lsh = MinHashLSH(threshold=threshold, num_perm=num_perm)
    ordered_index: dict[str, int] = {}
    for it in ordered:
        k, body = key(it), text(it)
        h = content_sha256(body)
        if h in by_hash:
            results[k] = DedupResult("exact_dup", by_hash[h], by_hash[h])
            continue
        by_hash[h] = k
        mh = minhash(body, shingle_size, num_perm)
        hits = lsh.query(mh)
        if hits:
            canon = min(hits, key=lambda x: ordered_index[x])
            results[k] = DedupResult("near_dup", canon, canon)
            continue
        lsh.insert(k, mh)
        results[k] = DedupResult("unique")
        ordered_index[k] = len(ordered_index)
    return results
