"""Central symbol table: exact API entries (signatures, fields, docs) looked up by name.

Built from the Central pages already scraped (`pages.source = 'central'`). Their Markdown has
an "API reference: module `<name>`" part with one `### <Kind>: <Name>` section per symbol
(clients, classes, records, functions, unions, ...), each with signatures and fields.

`lookup` finds the symbols a text names: `http:Client` style references, plus backticked or
capitalised names that exist in the modules the graph node declares (`node.modules`).
No embeddings: the teacher gets the real signature for the name it is asked about.
"""

from __future__ import annotations

import json
import re
from typing import Any

from balpipe_common.ids import utc_now_iso

MODULE_RE = re.compile(r"^## API reference: module `([^`]+)`\s*$", re.M)
SYMBOL_RE = re.compile(r"^### ([A-Za-z ]+): ([A-Za-z_'][\w']*)\s*$", re.M)
QUALIFIED_RE = re.compile(r"\b([a-z][a-z0-9_]*):([A-Za-z_][A-Za-z0-9_]*)")
NAME_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`|\b([A-Z][A-Za-z0-9]{3,})\b")
MAX_TEXT = 2500


def prefix_of(module: str) -> str:
    """'ballerina/http' -> 'http'; 'ballerina/data.jsondata' -> 'jsondata'; 'http.httpscerr' -> 'httpscerr'."""
    return module.split("/")[-1].split(".")[-1]


def parse_page(markdown: str, org: str, package: str) -> list[dict[str, str]]:
    """Symbols of one Central page: module, prefix, name, kind, text."""
    out: list[dict[str, str]] = []
    mods = list(MODULE_RE.finditer(markdown))
    for i, m in enumerate(mods):
        mod_name = m.group(1)
        module = f"{org}/{mod_name}"
        body = markdown[m.end(): mods[i + 1].start() if i + 1 < len(mods) else len(markdown)]
        heads = list(SYMBOL_RE.finditer(body))
        for j, h in enumerate(heads):
            end = heads[j + 1].start() if j + 1 < len(heads) else len(body)
            nxt = body.find("\n## ", h.end())
            if 0 <= nxt < end:
                end = nxt
            text = body[h.start():end].strip()
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT].rsplit("\n", 1)[0] + "\n…"
            out.append({"module": module, "prefix": prefix_of(mod_name), "kind": h.group(1).strip(),
                        "name": h.group(2), "text": text})
    return out


def build(db: Any) -> dict[str, int]:
    """Rebuild the table from the scraped Central pages (idempotent)."""
    now = utc_now_iso()
    rows = []
    for p in db.query("SELECT id, markdown, meta FROM pages WHERE source = 'central' AND status = 'chunked' "
                      "AND markdown IS NOT NULL"):
        meta = json.loads(p["meta"] or "{}")
        for sym in parse_page(p["markdown"], meta.get("org", "ballerina"), meta.get("package", "")):
            rows.append((sym["module"], sym["prefix"], sym["name"], sym["kind"], sym["text"], p["id"], now, now))
    db.execute("DELETE FROM symbols")
    db.conn.executemany("INSERT OR IGNORE INTO symbols (module, prefix, name, kind, text, page_id, created_at, "
                        "updated_at) VALUES (?,?,?,?,?,?,?,?)", rows)
    db.conn.commit()
    return {"symbols": int(db.scalar("SELECT COUNT(*) FROM symbols") or 0),
            "modules": int(db.scalar("SELECT COUNT(DISTINCT module) FROM symbols") or 0)}


def lookup(db: Any, text: str, modules: list[str] | None = None, limit: int = 6) -> list[Any]:
    """Symbols the text names, in order of first mention."""
    found: list[Any] = []
    seen: set[int] = set()

    def add(rows: list[Any]) -> None:
        for r in rows:
            if r["id"] not in seen and len(found) < limit:
                seen.add(r["id"])
                found.append(r)

    for m in QUALIFIED_RE.finditer(text):
        add(db.query("SELECT * FROM symbols WHERE prefix = ? AND name = ? ORDER BY length(module) LIMIT 2",
                      (m.group(1), m.group(2))))
    if modules:
        marks = ",".join("?" for _ in modules)
        for m in NAME_RE.finditer(text):
            name = m.group(1) or m.group(2)
            add(db.query(f"SELECT * FROM symbols WHERE name = ? AND module IN ({marks}) LIMIT 2", (name, *modules)))
    return found
