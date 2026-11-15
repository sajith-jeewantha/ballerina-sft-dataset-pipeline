"""Near-duplicate detection for questions (MinHash LSH over word sets).

MinHash finds candidates quickly; the exact Jaccard similarity then decides,
so "How do I read a file?" and "How do I write a file?" stay distinct while
rephrasings with the same words are caught.
"""

from __future__ import annotations

import re

from datasketch import MinHash, MinHashLSH

_WORD_RE = re.compile(r"\w+")
NUM_PERM = 128


def word_set(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def minhash(words: set[str]) -> MinHash:
    m = MinHash(num_perm=NUM_PERM)
    for w in words:
        m.update(w.encode("utf-8"))
    return m


class QuestionDeduper:
    def __init__(self, threshold: float = 0.8) -> None:
        self.threshold = threshold
        self.lsh = MinHashLSH(threshold=max(0.1, threshold - 0.1), num_perm=NUM_PERM)  # wider net, exact check after
        self.words: dict[str, set[str]] = {}

    def is_duplicate(self, text: str) -> bool:
        words = word_set(text)
        if not words:
            return True
        for key in self.lsh.query(minhash(words)):
            if jaccard(words, self.words[key]) >= self.threshold:
                return True
        return False

    def add(self, text: str) -> None:
        words = word_set(text)
        key = str(len(self.words))
        if words and key not in self.words:
            self.words[key] = words
            self.lsh.insert(key, minhash(words))
