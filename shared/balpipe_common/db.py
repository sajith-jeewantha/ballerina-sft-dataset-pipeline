"""The pipeline's single SQLite store (data/pipeline.db).

Tables, each with a `status` and `created_at`/`updated_at` timestamps:

* runs       one row per command run: limits, item counts, duration, outcome
* pages      one row per source page/file: raw cache, clean Markdown, hashes
* chunks     heading-aware pieces of a page; the grounding for questions
* questions  generated questions (pending -> answered | failed)
* answers    one per answered question (not_checked -> valid | invalid)
* llm_usage  tokens and cost of every model call, per phase
* repos      collected code repositories: licence verdict, pinned commit, snapshot
* repo_files files from those repos: Swan Lake filter, dedup status, fingerprints
* eval_items held-out evaluation items (BBE 30%): files, expected output, `covers`, verification
* fingerprints code fingerprints of the eval items, used to keep near-copies out of training

`chunks`, `questions` and `repo_files` carry `eval_overlap` (the eval item they copy, or
NULL) and `decontam_key` (the fingerprint set they were last checked against).

Writes are committed immediately, so an interrupted command loses at most the
item it was working on, and the next run picks up from the statuses.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from balpipe_common.ids import utc_now_iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    command      TEXT NOT NULL,
    status       TEXT NOT NULL,             -- running | succeeded | failed
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    duration_s   REAL,
    limits       TEXT NOT NULL DEFAULT '{}', -- JSON
    stats        TEXT NOT NULL DEFAULT '{}', -- JSON: items processed
    error        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source        TEXT NOT NULL,            -- bbe | spec | learn | central | github
    url           TEXT NOT NULL UNIQUE,     -- public URL, cited in answers
    fetch_url     TEXT,
    title         TEXT,
    status        TEXT NOT NULL,            -- fetched | chunked | empty | duplicate | blocked | failed
    error         TEXT,
    http_status   INTEGER,
    version_key   TEXT,                     -- blob SHAs / package version: skip without downloading
    etag          TEXT,
    last_modified TEXT,
    raw_body      TEXT,                     -- raw cache (HTML, Markdown, JSON bundle)
    raw_hash      TEXT,
    markdown      TEXT,                     -- clean Markdown
    content_hash  TEXT,
    license       TEXT,
    meta          TEXT NOT NULL DEFAULT '{}',
    fetch_count   INTEGER NOT NULL DEFAULT 0,
    last_run_id   INTEGER,
    fetched_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_pages_source ON pages(source, status);
CREATE INDEX IF NOT EXISTS ix_pages_content ON pages(content_hash);

CREATE TABLE IF NOT EXISTS chunks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id        INTEGER NOT NULL REFERENCES pages(id),
    source         TEXT NOT NULL,
    source_url     TEXT NOT NULL,
    ordinal        INTEGER NOT NULL,
    heading_path   TEXT NOT NULL,
    text           TEXT NOT NULL,
    text_hash      TEXT NOT NULL,
    token_estimate INTEGER NOT NULL,
    has_code       INTEGER NOT NULL,
    status         TEXT NOT NULL,           -- pending | questioned | too_small | failed | stale
    attempts       INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_chunks_status ON chunks(status, source);
CREATE INDEX IF NOT EXISTS ix_chunks_hash ON chunks(text_hash);
CREATE INDEX IF NOT EXISTS ix_chunks_page ON chunks(page_id);

CREATE TABLE IF NOT EXISTS questions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id      INTEGER NOT NULL REFERENCES chunks(id),
    source        TEXT NOT NULL,
    source_url    TEXT NOT NULL,
    question      TEXT NOT NULL,
    question_hash TEXT NOT NULL UNIQUE,
    category      TEXT NOT NULL,
    difficulty    TEXT NOT NULL,
    status        TEXT NOT NULL,            -- pending | answered | failed
    error         TEXT,
    model         TEXT,
    run_id        INTEGER,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_questions_status ON questions(status);

CREATE TABLE IF NOT EXISTS answers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    question_id       INTEGER NOT NULL UNIQUE REFERENCES questions(id),
    answer            TEXT NOT NULL,
    model             TEXT,
    status            TEXT NOT NULL,        -- not_checked | valid | invalid
    validation_detail TEXT,
    run_id            INTEGER,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_answers_status ON answers(status);

CREATE TABLE IF NOT EXISTS graph_items (
    ref           TEXT PRIMARY KEY,         -- node_id#item_id, the id samples use in `covers`
    node_id       TEXT NOT NULL,
    item_id       TEXT NOT NULL,
    node_type     TEXT NOT NULL,
    node_title    TEXT NOT NULL,
    priority      TEXT NOT NULL,            -- core | important | long_tail
    text          TEXT NOT NULL,
    difficulty    TEXT NOT NULL,            -- JSON list
    own_sources   INTEGER NOT NULL,         -- 0 = item inherits the node's sources
    graph_version TEXT NOT NULL,
    status        TEXT NOT NULL,            -- grounded | not_scraped | external_only | no_sources | retired
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_graph_items_node ON graph_items(node_id);

CREATE TABLE IF NOT EXISTS item_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ref          TEXT NOT NULL REFERENCES graph_items(ref),
    url          TEXT NOT NULL,
    kind         TEXT NOT NULL,             -- spec | bbe | central | docs | github | other
    note         TEXT,
    inherited    INTEGER NOT NULL,          -- 1 = node-level source used because the item has none
    page_id      INTEGER REFERENCES pages(id),
    chunk_ids    TEXT NOT NULL DEFAULT '[]', -- JSON: chunks that ground the item (section match for the spec)
    status       TEXT NOT NULL,             -- matched | not_scraped | external
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_item_sources_ref ON item_sources(ref);
CREATE INDEX IF NOT EXISTS ix_item_sources_page ON item_sources(page_id);

CREATE TABLE IF NOT EXISTS repos (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name        TEXT NOT NULL UNIQUE,  -- owner/name
    grp              TEXT NOT NULL,         -- platform | guides | wso2 | external | manual
    priority         INTEGER NOT NULL,      -- group order; lower wins when files are duplicates
    url              TEXT NOT NULL,
    default_branch   TEXT,
    commit_sha       TEXT,                  -- pinned commit (also in sources.lock.json)
    spdx_reported    TEXT,                  -- what GitHub reports
    spdx             TEXT,                  -- licence used (from the LICENSE file when found)
    verdict          TEXT,                  -- safe | check | exclude
    licence_note     TEXT,
    licence_sha256   TEXT,
    stars            INTEGER,
    size_kb          INTEGER,
    pushed_at        TEXT,
    files            INTEGER,               -- files in the sparse snapshot
    bal_files        INTEGER,
    snapshot         TEXT,                  -- data-relative path of the .tar.gz snapshot
    meta             TEXT NOT NULL DEFAULT '{}',
    status           TEXT NOT NULL,         -- collected | excluded | licence_check | failed
    error            TEXT,
    collected_at     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_repos_status ON repos(status);

CREATE TABLE IF NOT EXISTS repo_files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id          INTEGER NOT NULL REFERENCES repos(id) ON DELETE CASCADE,
    path             TEXT NOT NULL,
    kind             TEXT NOT NULL,         -- bal | toml | out | md
    bytes            INTEGER NOT NULL,
    text             TEXT,                  -- NULL when too_large
    project_root     TEXT,                  -- folder of the nearest Ballerina.toml ('' = repo root)
    distribution     TEXT,                  -- from that Ballerina.toml
    syntax           TEXT,                  -- swan_lake | legacy | unknown (.bal only)
    generated        INTEGER NOT NULL DEFAULT 0,
    code_hash        TEXT,                  -- tokens without comments/whitespace (.bal only)
    minhash          BLOB,                  -- 128 x uint32 MinHash signature (.bal only)
    dup_of           INTEGER,               -- repo_files.id this file duplicates
    status           TEXT NOT NULL,         -- kept | duplicate | near_duplicate | legacy | too_large | context
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (repo_id, path)
);
CREATE INDEX IF NOT EXISTS ix_repo_files_status ON repo_files(status);
CREATE INDEX IF NOT EXISTS ix_repo_files_hash ON repo_files(code_hash);

CREATE TABLE IF NOT EXISTS eval_items (
    id               TEXT PRIMARY KEY,      -- bbe/<example name>
    suite            TEXT NOT NULL,         -- bbe
    version          TEXT NOT NULL,         -- eval set version (eval.version), e.g. v1
    name             TEXT NOT NULL,
    title            TEXT,
    category         TEXT,
    url              TEXT NOT NULL,         -- public page (ballerina.io/learn/by-example/<name>/)
    source_url       TEXT,                  -- folder at the pinned commit
    commit_sha       TEXT,
    files            TEXT NOT NULL,         -- JSON {file name: text}
    covers           TEXT NOT NULL DEFAULT '[]', -- JSON: node#item refs whose own sources are this example
    nodes            TEXT NOT NULL DEFAULT '[]', -- JSON: nodes linking it as a node-level source
    verify_mode      TEXT NOT NULL,         -- run (bal run vs .out) | build | none
    verify_status    TEXT,                  -- pass | output_differs | build_failed | timeout | skipped
    verify_detail    TEXT,
    status           TEXT NOT NULL,         -- active | retired (no longer in the split)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fingerprints (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id          TEXT NOT NULL REFERENCES eval_items(id) ON DELETE CASCADE,
    path             TEXT NOT NULL,
    code_hash        TEXT NOT NULL,         -- tokens without comments/whitespace
    minhash          BLOB NOT NULL,         -- 128 x uint32 MinHash signature
    shingles         BLOB NOT NULL,         -- sorted uint32 hashes of distinctive 5-token shingles
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (item_id, path)
);
CREATE INDEX IF NOT EXISTS ix_fingerprints_hash ON fingerprints(code_hash);

CREATE TABLE IF NOT EXISTS llm_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER,
    phase             TEXT NOT NULL,        -- generate-questions | generate-answers
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cost_usd          REAL,
    cached            INTEGER NOT NULL DEFAULT 0,
    provider          TEXT,                 -- host that served the call (licence audit)
    status            TEXT NOT NULL,        -- ok | error
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.conn.commit()

    # Columns added after a table was first created: (table, column, type). Applied once, idempotently.
    MIGRATIONS = (("llm_usage", "provider", "TEXT"),
                  *((t, c, "TEXT") for t in ("chunks", "questions", "repo_files")
                    for c in ("eval_overlap", "decontam_key")))

    def _migrate(self) -> None:
        for table, column, ctype in self.MIGRATIONS:
            cols = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def insert(self, table: str, values: dict[str, Any]) -> int:
        now = utc_now_iso()
        values = {"created_at": now, "updated_at": now, **values}
        cols = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        cur = self.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
        return int(cur.lastrowid or 0)

    def update(self, table: str, row_id: int | str, values: dict[str, Any]) -> None:
        values = {**values, "updated_at": utc_now_iso()}
        sets = ", ".join(f"{k} = ?" for k in values)
        self.execute(f"UPDATE {table} SET {sets} WHERE id = ?", (*values.values(), row_id))

    # ------------------------------------------------------------------ #
    @contextmanager
    def run(self, command: str, limits: dict[str, Any]) -> Iterator[RunTracker]:
        """Record a command run: limits, counters, duration and outcome."""
        start = time.monotonic()
        now = utc_now_iso()
        run_id = self.insert("runs", {"command": command, "status": "running", "started_at": now,
                                      "limits": json.dumps(limits)})
        tracker = RunTracker(self, run_id)
        try:
            yield tracker
        except BaseException as exc:
            self.update("runs", run_id, {
                "status": "failed", "finished_at": utc_now_iso(), "duration_s": round(time.monotonic() - start, 2),
                "stats": json.dumps(tracker.stats), "error": f"{type(exc).__name__}: {exc}"[:2000],
            })
            raise
        self.update("runs", run_id, {
            "status": "succeeded", "finished_at": utc_now_iso(), "duration_s": round(time.monotonic() - start, 2),
            "stats": json.dumps(tracker.stats),
        })


class RunTracker:
    """Counters for one run; flushed to the `runs` row as the run goes."""

    def __init__(self, db: Database, run_id: int) -> None:
        self.db = db
        self.id = run_id
        self.stats: dict[str, int] = {}

    def count(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def flush(self) -> None:
        self.db.update("runs", self.id, {"stats": json.dumps(self.stats)})


def open_db(path: Path) -> Database:
    return Database(path)
