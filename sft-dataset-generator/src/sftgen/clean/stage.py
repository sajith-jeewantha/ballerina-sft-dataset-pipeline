"""The processing half of `scrape`: newly fetched pages -> clean Markdown -> chunks.

Page status after this step:
  chunked    converted and chunked
  empty      nothing usable on the page (e.g. an index page)
  duplicate  same clean content as another page (kept once)
  failed     the extractor raised (error recorded)
"""

from __future__ import annotations

import json
import logging

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.ids import content_hash, utc_now_iso

from sftgen.chunk.stage import chunk_page
from sftgen.clean.extractors import extract

log = logging.getLogger(__name__)
MIN_WORDS = 20


def _retire_chunks(db: Database, page_id: int) -> None:
    """A page that no longer has usable content keeps no active chunks."""
    db.execute("UPDATE chunks SET status = 'stale', updated_at = ? WHERE page_id = ? AND status != 'stale'",
               (utc_now_iso(), page_id))


def process_pages(s: Settings, db: Database, run: RunTracker) -> dict[str, int]:
    stats: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        stats[key] = stats.get(key, 0) + n
        run.count(key, n)

    for page in db.query("SELECT * FROM pages WHERE status = 'fetched' ORDER BY id"):
        try:
            got = extract(page["source"], page["url"], page["raw_body"] or "", json.loads(page["meta"] or "{}"))
        except Exception as exc:
            log.warning("extract failed: %s: %s", page["url"], exc)
            db.update("pages", page["id"], {"status": "failed", "error": f"extract: {type(exc).__name__}: {exc}"})
            bump("pages_extract_failed")
            continue
        if got is None or len(got.markdown.split()) < MIN_WORDS:
            db.update("pages", page["id"], {"status": "empty", "markdown": got.markdown if got else None,
                                            "title": got.title if got else page["title"]})
            _retire_chunks(db, page["id"])
            bump("pages_empty")
            continue
        h = content_hash(got.markdown)
        twin = db.one("SELECT url FROM pages WHERE content_hash = ? AND id != ? AND status = 'chunked'", (h, page["id"]))
        if twin:
            db.update("pages", page["id"], {"status": "duplicate", "markdown": got.markdown, "content_hash": h,
                                            "title": got.title, "error": f"same content as {twin['url']}"})
            _retire_chunks(db, page["id"])
            bump("pages_duplicate")
            continue
        meta = {**json.loads(page["meta"] or "{}"), "extractor": got.extractor}
        db.update("pages", page["id"], {"markdown": got.markdown, "content_hash": h, "title": got.title,
                                        "meta": json.dumps(meta)})
        for key, n in chunk_page(s, db, page, got.title, got.markdown).items():
            bump(key, n)
        db.update("pages", page["id"], {"status": "chunked", "error": None})
        bump("pages_chunked")
    run.flush()
    return stats
