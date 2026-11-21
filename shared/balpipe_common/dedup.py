"""Code fingerprints: exact hashes and MinHash for near-duplicate detection.

* `code_hash`   sha256 of the code with comments and whitespace removed: catches copies
                that differ only in a licence header, comments or formatting.
* `MinHasher`   128-value MinHash over 5-token shingles. Two files with signature agreement
                >= 0.8 (estimated Jaccard similarity) are treated as near-duplicates.
* `shingle_hashes` 32-bit hashes of the 5-token shingles, for containment checks
                (`decontam.py`: how much of an eval item a text contains).
* `LSH`         banding index (16 bands x 8 rows) so candidates are found without comparing
                every pair; candidates are then checked against the threshold.

Also used by the eval decontamination step (`decontam.py`), so eval items and training code
share one fingerprint definition.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict

import numpy as np

from .ids import sha256_text

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?m)(?<![:\"'])//.*$|^\s*#.*$")  # `//` comments; `#` doc comments
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?|\"(?:\\.|[^\"\\])*\"|\S")

_MERSENNE = np.uint64((1 << 61) - 1)
_MAX32 = np.uint64((1 << 32) - 1)


def strip_comments(code: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", code))


def code_tokens(code: str) -> list[str]:
    return _TOKEN.findall(strip_comments(code))


def code_hash(code: str) -> str:
    """Exact-duplicate key: tokens only, so comments, licence headers and layout don't matter."""
    return sha256_text("\x1f".join(code_tokens(code)))


def _h32(s: str) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode("utf-8"), digest_size=4).digest(), "little")


def shingles(tokens: list[str], k: int = 5) -> set[str]:
    return {" ".join(tokens[i:i + k]) for i in range(max(1, len(tokens) - k + 1))} if tokens else set()


def shingle_hashes(code: str, k: int = 5) -> set[int]:
    """32-bit hashes of the 5-token shingles of the code (comments removed); used for containment checks."""
    return {_h32(g) for g in shingles(code_tokens(code), k)}


class MinHasher:
    def __init__(self, num_perm: int = 128, shingle: int = 5, seed: int = 1) -> None:
        rng = np.random.RandomState(seed)
        self.num_perm = num_perm
        self.shingle = shingle
        self.a = rng.randint(1, (1 << 31) - 1, size=num_perm, dtype=np.uint64)
        self.b = rng.randint(0, (1 << 31) - 1, size=num_perm, dtype=np.uint64)

    def signature(self, code: str) -> np.ndarray:
        grams = shingles(code_tokens(code), self.shingle) or {""}
        hv = np.fromiter((_h32(g) for g in grams), dtype=np.uint64, count=len(grams))
        perm = ((hv[:, None] * self.a[None, :] + self.b[None, :]) % _MERSENNE) & _MAX32
        return perm.min(axis=0).astype(np.uint32)

    def to_bytes(self, sig: np.ndarray) -> bytes:
        return sig.astype("<u4").tobytes()

    def from_bytes(self, data: bytes) -> np.ndarray:
        return np.frombuffer(data, dtype="<u4")


def similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.mean(a == b))


class LSH:
    """Banded index over MinHash signatures; `add` returns earlier keys that look similar."""

    def __init__(self, threshold: float = 0.8, bands: int = 16, rows: int = 8) -> None:
        self.threshold = threshold
        self.bands = bands
        self.rows = rows
        self.tables: list[dict[bytes, list[int]]] = [defaultdict(list) for _ in range(bands)]
        self.sigs: dict[int, np.ndarray] = {}

    def query(self, sig: np.ndarray) -> int | None:
        """The first indexed key whose signature agreement is >= threshold, or None."""
        seen: set[int] = set()
        for i, table in enumerate(self.tables):
            band = sig[i * self.rows:(i + 1) * self.rows].tobytes()
            for key in table.get(band, ()):
                if key in seen:
                    continue
                seen.add(key)
                if similarity(sig, self.sigs[key]) >= self.threshold:
                    return key
        return None

    def add(self, key: int, sig: np.ndarray) -> None:
        self.sigs[key] = sig
        for i, table in enumerate(self.tables):
            table[sig[i * self.rows:(i + 1) * self.rows].tobytes()].append(key)
