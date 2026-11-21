"""Grounding for one graph item: the excerpts a prompt writer, reviewer or teacher sees.

In order, until `prompts.max_grounding_tokens` is used up:

1. **The item's own sources** (`item_sources`, exact): the best-matching chunks of each matched
   page (for the spec, only chunks of the section the source names). A GitHub file that isn't a
   scraped page is read from `repo_files` (collected by `collect-repos`).
2. **Central symbols** the item names (`symbols.lookup`): exact signatures and fields.
3. **Node-level sources**, for items without sources of their own.
4. **FTS5 neighbours**: the best BM25 matches for the item text anywhere in the corpus.

Nothing that overlaps the eval set is used: eval chunks and repo files are skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from balpipe_common.config import PromptsCfg

from sftgen.graph.coverage import source_key
from sftgen.retrieval import fts, symbols

REPO_FILE_CHARS = 6000


@dataclass
class Excerpt:
    kind: str     # source | node_source | symbol | neighbour
    url: str
    title: str
    text: str

    @property
    def tokens(self) -> int:
        return len(self.text) // 4


@dataclass
class Grounding:
    excerpts: list[Excerpt] = field(default_factory=list)
    chunk_ids: list[int] = field(default_factory=list)
    repo_file_ids: list[int] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(e.tokens for e in self.excerpts)

    @property
    def urls(self) -> list[str]:
        return list(dict.fromkeys(e.url for e in self.excerpts))

    def summary(self) -> dict[str, Any]:
        return {"urls": self.urls, "chunk_ids": self.chunk_ids, "repo_file_ids": self.repo_file_ids,
                "symbols": self.symbols, "tokens": self.tokens,
                "kinds": sorted({e.kind for e in self.excerpts})}

    def render(self) -> str:
        if not self.excerpts:
            return "(no excerpts found: rely on general Ballerina Swan Lake knowledge, conservatively)"
        return "\n\n".join(f"[{i}] {e.title} ({e.url})\n{e.text.strip()}" for i, e in enumerate(self.excerpts, 1))


def _repo_file(db: Any, key: str) -> Any:
    """A collected repo file for a `gh:owner/repo:path` key ('*' = a README below the prefix)."""
    repo, _, path = key[3:].partition(":")
    if path.endswith("*"):
        return db.one("SELECT f.id, f.path, f.text FROM repo_files f JOIN repos r ON r.id = f.repo_id "
                      "WHERE r.full_name = ? AND f.path LIKE ? AND f.kind = 'md' AND f.text IS NOT NULL "
                      "ORDER BY length(f.path) LIMIT 1", (repo, path[:-1] + "%"))
    return db.one("SELECT f.id, f.path, f.text FROM repo_files f JOIN repos r ON r.id = f.repo_id "
                  "WHERE r.full_name = ? AND f.path = ? AND f.text IS NOT NULL AND f.eval_overlap IS NULL",
                  (repo, path))


def _relevant_window(text: str, query: str, chars: int) -> str:
    """The part of a long file around the first line that shares words with the query."""
    if len(text) <= chars:
        return text
    words = {w.lower() for w in fts.WORD_RE.findall(query)} - fts.STOP
    lines = text.splitlines(keepends=True)
    best, best_hits = 0, 0
    for i, line in enumerate(lines):
        hits = sum(1 for w in fts.WORD_RE.findall(line) if w.lower() in words)
        if hits > best_hits:
            best, best_hits = i, hits
    start = max(0, best - 15)
    out = "".join(lines[start:])[:chars]
    return ("…\n" if start else "") + out + "\n…"


def ground_item(db: Any, item: dict[str, Any], node: dict[str, Any], cfg: PromptsCfg) -> Grounding:
    """`item`: graph_items row (ref, text, ...); `node`: {title, summary, modules}."""
    g = Grounding()
    budget = cfg.max_grounding_tokens
    query = f"{item['text']} {node.get('title', '')}"
    used_chunks: set[int] = set()

    def add(ex: Excerpt) -> bool:
        if g.tokens + ex.tokens > budget and g.excerpts:
            return False
        g.excerpts.append(ex)
        return True

    def add_chunks(rows: list[Any], kind: str) -> None:
        for c in rows:
            if c["id"] in used_chunks:
                continue
            if add(Excerpt(kind, c["source_url"], c["heading_path"] or c["source_url"], c["text"])):
                used_chunks.add(c["id"])
                g.chunk_ids.append(c["id"])

    sources = db.query("SELECT * FROM item_sources WHERE ref = ? ORDER BY inherited, id", (item["ref"],))
    own = [s for s in sources if not s["inherited"]]
    inherited = [s for s in sources if s["inherited"]]

    def from_source(s: Any, kind: str) -> None:
        if s["page_id"]:
            allowed = set(json.loads(s["chunk_ids"] or "[]"))
            ranked = fts.search(db, query, k=40 if allowed else cfg.chunks_per_source, page_id=s["page_id"],
                                exclude=used_chunks)
            if allowed:
                ranked = [r for r in ranked if r["id"] in allowed] or db.query(
                    f"SELECT * FROM chunks WHERE id IN ({','.join('?' for _ in allowed)}) AND eval_overlap IS NULL "
                    "AND token_estimate >= 20 ORDER BY ordinal LIMIT ?", (*allowed, cfg.chunks_per_source))
            if not ranked:
                ranked = db.query("SELECT * FROM chunks c WHERE c.page_id = ? AND c.status != 'stale' AND "
                                  f"c.eval_overlap IS NULL AND c.token_estimate >= 20 AND {fts.NOT_BOILERPLATE} "
                                  "ORDER BY c.ordinal LIMIT 1", (s["page_id"],))
            add_chunks(ranked[:cfg.chunks_per_source], kind)
            return
        key = source_key(s["url"])[0]
        if key and key.startswith("gh:"):
            f = _repo_file(db, key)
            if f is not None and f["id"] not in g.repo_file_ids:
                if add(Excerpt(kind, s["url"], f["path"], _relevant_window(f["text"], query, REPO_FILE_CHARS))):
                    g.repo_file_ids.append(f["id"])

    for s in own:
        from_source(s, "source")
    for sym in symbols.lookup(db, item["text"], node.get("modules") or [], cfg.max_symbols):
        if add(Excerpt("symbol", f"central:{sym['module']}", f"{sym['prefix']}:{sym['name']} ({sym['kind']})",
                       sym["text"])):
            g.symbols.append(f"{sym['prefix']}:{sym['name']}")
    if not own:
        for s in inherited:
            from_source(s, "node_source")
    add_chunks(fts.search(db, query, k=cfg.fts_neighbours, exclude=used_chunks), "neighbour")
    return g
