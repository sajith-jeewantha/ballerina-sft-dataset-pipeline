"""Chunk one page's Markdown into the `chunks` table.

Re-chunking a changed page keeps chunks whose text is unchanged (and their
questions), marks vanished chunks `stale`, and adds new ones as `pending`.
A chunk whose text already exists on another page is skipped as a duplicate.
"""

from __future__ import annotations

import sqlite3

from balpipe_common.config import Settings
from balpipe_common.db import Database
from balpipe_common.ids import content_hash

from sftgen.chunk.markdown_chunker import chunk_markdown, context_header


def approx_tokens(text: str) -> int:
    """~4 characters per token: close enough for budgeting LLM context, and dependency-free."""
    return (len(text) + 3) // 4


def chunk_page(s: Settings, db: Database, page: sqlite3.Row, title: str, markdown: str) -> dict[str, int]:
    stats = {"chunks_new": 0, "chunks_kept": 0, "chunks_stale": 0, "chunks_duplicate": 0}
    drafts = chunk_markdown(markdown, title, s.chunk.max_tokens, approx_tokens)
    existing = {r["text_hash"]: r for r in db.query(
        "SELECT c.id, c.text_hash, c.status, (SELECT COUNT(*) FROM questions q WHERE q.chunk_id = c.id) AS nq "
        "FROM chunks c WHERE c.page_id = ?", (page["id"],))}
    seen: set[str] = set()
    for ordinal, d in enumerate(drafts):
        h = content_hash(d.text)
        if h in seen:
            continue
        seen.add(h)
        small = d.token_count < s.chunk.min_tokens
        values = {"ordinal": ordinal, "heading_path": context_header(d.heading_path), "text": d.text,
                  "token_estimate": d.token_count, "has_code": int(d.has_code), "source_url": page["url"]}
        if h in existing:
            old = existing[h]
            if old["status"] == "stale":
                values["status"] = "questioned" if old["nq"] else ("too_small" if small else "pending")
            db.update("chunks", old["id"], values)
            stats["chunks_kept"] += 1
            continue
        if db.scalar("SELECT 1 FROM chunks WHERE text_hash = ? AND page_id != ? AND status != 'stale' LIMIT 1",
                     (h, page["id"])):
            stats["chunks_duplicate"] += 1
            continue
        db.insert("chunks", {"page_id": page["id"], "source": page["source"], "text_hash": h,
                             "status": "too_small" if small else "pending", **values})
        stats["chunks_new"] += 1
    for h, old in existing.items():
        if h not in seen and old["status"] != "stale":
            db.update("chunks", old["id"], {"status": "stale"})
            stats["chunks_stale"] += 1
    return stats
