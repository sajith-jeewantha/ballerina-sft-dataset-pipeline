"""Full-text search over chunks: SQLite FTS5 with BM25 ranking, inside pipeline.db.

`chunks_fts` is an external-content index on `chunks(text, heading_path)`. `refresh` rebuilds
it when the chunk table has changed since the last build (a few seconds for tens of thousands
of chunks). `search` turns free text (an item's description) into an OR query of its words, so
any overlap ranks, and BM25 orders the hits. Chunks that are stale, too small, or overlap the
eval set are never returned.
"""

from __future__ import annotations

import re
from typing import Any

WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
STOP = set("""the and for with that this from into are can not you your use using used when then than its
also any all each such only one two how what which while where who will would should must may might
via per but about over under more most other others their there them they these those been being
was were has have had does did done make makes made like just very some same both well
""".split())


# link lists and setup steps carry no knowledge of their own
NOT_BOILERPLATE = ("c.heading_path NOT LIKE '%> Related links' AND c.heading_path NOT LIKE '%> Prerequisites' "
                   "AND c.heading_path NOT LIKE '%> Prerequisite'")


def refresh(db: Any) -> bool:
    """Rebuild the index if chunks changed since the last build. Returns True when rebuilt."""
    state = db.one("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m, COALESCE(MAX(updated_at), '') AS u FROM chunks")
    key = f"{state['n']}:{state['m']}:{state['u']}"
    if db.scalar("SELECT value FROM kv WHERE key = 'chunks_fts'") == key:
        return False
    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('rebuild')")
    db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES ('chunks_fts', ?)", (key,))
    return True


def to_query(text: str, max_terms: int = 24) -> str:
    """'"word1" OR "word2" ...' from the distinctive words of the text."""
    seen: list[str] = []
    for w in WORD_RE.findall(text.replace("`", " ")):
        lw = w.lower()
        if lw in STOP or lw in seen:
            continue
        seen.append(lw)
        if len(seen) >= max_terms:
            break
    return " OR ".join(f'"{w}"' for w in seen)


def search(db: Any, text: str, k: int = 5, *, page_id: int | None = None, exclude: set[int] | None = None,
           min_tokens: int = 40) -> list[Any]:
    """Best-matching usable chunks for the text, best first (optionally within one page)."""
    q = to_query(text)
    if not q:
        return []
    exclude = exclude or set()
    page_sql = "AND c.page_id = ?" if page_id is not None else ""
    params: tuple = (q, *((page_id,) if page_id is not None else ()), min_tokens, k + len(exclude))
    rows = db.query(
        "SELECT c.id, c.page_id, c.source, c.source_url, c.heading_path, c.text, c.token_estimate, "
        "bm25(chunks_fts) AS score FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        f"WHERE chunks_fts MATCH ? {page_sql} AND c.status NOT IN ('stale') AND c.eval_overlap IS NULL "
        f"AND {NOT_BOILERPLATE} "
        "AND c.token_estimate >= ? ORDER BY score LIMIT ?", params)
    return [r for r in rows if r["id"] not in exclude][:k]
