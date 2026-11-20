"""Knowledge-graph coverage: which graph items the scraped sources can ground.

`sync` loads and validates `graph/graph.json`, writes every item to
`graph_items`, and matches each item's sources (or its node's sources, when the
item has none) to scraped pages in `item_sources`:

* ballerina.io pages match by URL (anchors dropped). For the language spec,
  which is one huge page, the match is narrowed to the section: the chunks
  under the heading named by the source's note or `#section_N.N` anchor.
* Central links (`/latest`, versions, sub-pages) match the package page.
* GitHub blob links match the module file. Tree and repo links match any file
  below them. `ballerina-distribution/examples/<name>` links match the BBE page.
* Other hosts are `external`: not a source this pipeline scrapes.

Item status: `grounded` (a matched page or spec section), `not_scraped` (its
sources are scrapeable but not in the database yet), `external_only`, or
`retired` (deprecated node). `coverage_summary` feeds the report.

Questions are linked to items approximately (a question from a page or section
that grounds the item) until prompts are planned per item and tagged with
`covers` (milestone M5).
"""

from __future__ import annotations

import bisect
import json
import re
from collections import defaultdict
from typing import Any
from urllib.parse import urlsplit

from balpipe_common.config import Settings
from balpipe_common.db import Database
from balpipe_common.graph import LoadedGraph, load_graph_json

BBE_PAGE = "https://ballerina.io/learn/by-example/{name}/"
SECTION_RE = re.compile(r"section_(\d+(?:\.\d+)*)", re.IGNORECASE)


def _val(x: Any) -> str:
    return x.value if hasattr(x, "value") else str(x)


def source_key(url: str) -> tuple[str | None, str | None]:
    """(match key, anchor). A key ending in '*' matches every page key with that prefix."""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower()
    segs = [p for p in parts.path.split("/") if p]
    anchor = parts.fragment or None
    if host in ("ballerina.io", "www.ballerina.io"):
        return f"https://ballerina.io/{'/'.join(segs)}/" if segs else "https://ballerina.io/", anchor
    if host == "central.ballerina.io" and len(segs) >= 2:
        return f"https://central.ballerina.io/{segs[0]}/{segs[1]}", None
    if host == "github.com" and len(segs) >= 2:
        owner, repo = segs[0], segs[1]
        rest = segs[4:] if len(segs) >= 4 and segs[2] in ("blob", "tree") else []
        if repo == "ballerina-distribution" and len(rest) >= 2 and rest[0] == "examples":
            return BBE_PAGE.format(name=rest[1]), None
        if len(segs) >= 4 and segs[2] == "blob" and rest:
            return f"gh:{owner}/{repo}:{'/'.join(rest)}", None
        prefix = "/".join(rest)
        return f"gh:{owner}/{repo}:{prefix + '/' if prefix else ''}*", None
    return None, None


def page_key(url: str) -> str:
    """The match key of a scraped page (GitHub module files are keyed without their branch)."""
    parts = urlsplit(url)
    segs = [p for p in parts.path.split("/") if p]
    if parts.netloc == "github.com" and len(segs) >= 5 and segs[2] == "blob":
        return f"gh:{segs[0]}/{segs[1]}:{'/'.join(segs[4:])}"
    return url


def _section_chunks(chunks: list[tuple[int, str]], note: str | None, anchor: str | None) -> list[int]:
    """Chunks of the spec page under the heading the source points at."""
    wanted: list[str] = []
    if note:
        wanted.append(note.strip().lower())
    m = SECTION_RE.search(anchor or "")
    # the section number: from a `#section_N.N` anchor, else from the note ("11.3.1 lang.array iterator")
    nm = re.match(r"(\d+(?:\.\d+)*)\.?\s", note.strip()) if note else None
    number = m.group(1) if m else (nm.group(1) if nm else None)
    word = (anchor or "").replace("-", " ").replace("_", " ").strip().lower() if anchor and not m else None
    out = []
    for cid, path in chunks:
        for seg in (x.strip().lower() for x in path.split(" > ")):
            title = re.sub(r"^\d+(\.\d+)*\.?\s+", "", seg)
            seg_number = re.match(r"(\d+(?:\.\d+)*)\.?(\s|$)", seg)
            if (seg in wanted or (number and seg_number and seg_number.group(1) == number)
                    or (word and title == word)):
                out.append(cid)
                break
    return out


def sync(s: Settings, db: Database) -> dict[str, Any]:
    lg = load_graph_json(s.path(s.graph.path), s.path(s.graph.source_file))
    return sync_loaded(db, lg)


def sync_loaded(db: Database, lg: LoadedGraph) -> dict[str, Any]:
    g = lg.graph
    pages = db.query("SELECT id, url, source FROM pages WHERE status = 'chunked'")
    by_key = {page_key(p["url"]): p for p in pages}
    keys = sorted(by_key)
    spec_chunks: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for c in db.query("SELECT c.id, c.page_id, c.heading_path FROM chunks c JOIN pages p ON p.id = c.page_id "
                      "WHERE p.source = 'spec' AND c.status != 'stale'"):
        spec_chunks[c["page_id"]].append((c["id"], c["heading_path"]))

    def find(key: str) -> Any:
        if not key.endswith("*"):
            return by_key.get(key)
        prefix = key[:-1]
        i = bisect.bisect_left(keys, prefix)
        hits = [by_key[k] for k in keys[i:i + 200] if k.startswith(prefix)]
        readme = [h for h in hits if re.search(r"(README|Module|Package)\.md$", h["url"])]
        return (readme or hits or [None])[0]

    active = {g.ref(n, i) for n, i in g.iter_items()}
    existing = {r["ref"]: r["created_at"] for r in db.query("SELECT ref, created_at FROM graph_items")}
    db.execute("DELETE FROM item_sources")
    db.execute("DELETE FROM graph_items")
    rows_items, rows_sources = [], []
    from balpipe_common.ids import utc_now_iso

    now = utc_now_iso()
    counts: dict[str, int] = defaultdict(int)
    for node, item in g.iter_items(include_retired=True):
        ref = g.ref(node, item)
        own = bool(item.sources)
        srcs = g.sources_for(node, item)
        statuses = []
        for src in srcs:
            key, anchor = source_key(src.url)
            page = find(key) if key else None
            chunk_ids: list[int] = []
            if key is None:
                st = "external"
            elif page is None:
                st = "not_scraped"
            else:
                st = "matched"
                if page["source"] == "spec":
                    chunk_ids = _section_chunks(spec_chunks.get(page["id"], []), getattr(src, "note", None), anchor)
            grounded = st == "matched" and (page["source"] != "spec" or bool(chunk_ids))
            statuses.append("grounded" if grounded else st)
            rows_sources.append((ref, src.url, _val(src.kind), getattr(src, "note", None), int(not own),
                                 page["id"] if page else None, json.dumps(chunk_ids), st, now, now))
        if ref not in active:
            status = "retired"
        elif "grounded" in statuses:
            status = "grounded"
        elif "not_scraped" in statuses or "matched" in statuses:
            status = "not_scraped"
        elif statuses:
            status = "external_only"
        else:
            status = "no_sources"
        counts[status] += 1
        rows_items.append((ref, node.id, item.id, _val(node.type), node.title, _val(node.priority), item.text,
                           json.dumps([_val(d) for d in item.difficulty]), int(own), lg.version, status,
                           existing.get(ref, now), now))
    db.conn.executemany("INSERT INTO graph_items (ref, node_id, item_id, node_type, node_title, priority, text, "
                        "difficulty, own_sources, graph_version, status, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows_items)
    db.conn.executemany("INSERT INTO item_sources (ref, url, kind, note, inherited, page_id, chunk_ids, status, "
                        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)", rows_sources)
    db.conn.commit()
    return {"graph_version": lg.version, "items": len(rows_items), "sources": len(rows_sources), **counts}


def coverage_summary(db: Database, top_missing: int = 25) -> dict[str, Any] | None:
    """Coverage numbers for the report; None when the graph was never synced."""
    if not db.scalar("SELECT COUNT(*) FROM graph_items"):
        return None
    # chunks that ground each item: the spec section, or every active chunk of a matched page
    grounding = db.query(
        "SELECT s.ref, c.id AS chunk_id FROM item_sources s JOIN chunks c ON c.page_id = s.page_id "
        "JOIN pages p ON p.id = s.page_id WHERE s.status = 'matched' AND p.source != 'spec' AND c.status != 'stale' "
        "UNION SELECT s.ref, j.value FROM item_sources s, json_each(s.chunk_ids) j WHERE s.status = 'matched'")
    q_chunks = {r["chunk_id"] for r in db.query("SELECT DISTINCT chunk_id FROM questions")}
    with_questions = {r["ref"] for r in grounding if r["chunk_id"] in q_chunks}

    items = db.query("SELECT ref, node_id, node_title, node_type, priority, status, own_sources, text "
                     "FROM graph_items WHERE status != 'retired'")
    order = ["core", "important", "long_tail"]
    by_priority: dict[str, dict[str, int]] = {p: defaultdict(int) for p in order}
    by_type: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for it in items:
        bp = by_priority.setdefault(it["priority"], defaultdict(int))
        bp["items"] += 1
        bp[it["status"]] += 1
        bp["with_questions"] += it["ref"] in with_questions
        bp["no_own_sources"] += not it["own_sources"]
        by_type[it["node_type"]][0] += 1
        by_type[it["node_type"]][1] += it["status"] == "grounded"
    missing = db.query(
        "SELECT g.ref, g.node_title, g.text, g.status, (SELECT url FROM item_sources s WHERE s.ref = g.ref "
        "AND s.status != 'external' LIMIT 1) AS url FROM graph_items g WHERE g.priority = 'core' "
        "AND g.status IN ('not_scraped', 'external_only', 'no_sources') ORDER BY g.ref LIMIT ?", (top_missing,))
    to_scrape = db.query(
        "SELECT kind, COUNT(DISTINCT url) AS n FROM item_sources WHERE status = 'not_scraped' GROUP BY kind "
        "ORDER BY n DESC")
    version = db.scalar("SELECT graph_version FROM graph_items LIMIT 1")
    return {
        "graph_version": version,
        "items": len(items),
        "grounded": sum(1 for it in items if it["status"] == "grounded"),
        "with_questions": len(with_questions & {it["ref"] for it in items}),
        "by_priority": {p: dict(v) for p, v in by_priority.items()},
        "by_type": {t: {"items": v[0], "grounded": v[1]} for t, v in sorted(by_type.items())},
        "missing_core": [dict(r) for r in missing],
        "missing_core_total": db.scalar("SELECT COUNT(*) FROM graph_items WHERE priority = 'core' AND status IN "
                                        "('not_scraped', 'external_only', 'no_sources')"),
        "not_scraped_urls": {r["kind"]: r["n"] for r in to_scrape},
    }
