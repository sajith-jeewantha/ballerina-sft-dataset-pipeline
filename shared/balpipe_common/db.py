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
* prompts    prompts planned per graph item, written by the writer model, reviewed by the judge
* symbols    Central API entries (signatures, fields), looked up by `module:Name`
* chunks_fts full-text index (FTS5, BM25) over chunk text, for grounding
* attempts   every teacher answer to a prompt (first try and repairs) with its check results
* responses  the outcome per prompt: the final attempt, verified / judged / failed
* preference_pairs  a failed attempt and the passing repair of the same prompt (for DPO)
* codebases  the codebase pool: verified Swan Lake projects, graph tags, train/eval split
* codebase_states  broken or unfinished versions of a codebase (the starting point of a task)
* benchmark_problems  HumanEval / MBPP problems fetched for translation into Ballerina
* mined_fixes  before/after bug fixes mined from the commit history of collected repos
* eval_runs / eval_results  baseline runs: k sampled answers per eval item per model, and scores

`chunks`, `questions`, `repo_files`, `prompts` and `responses` carry `eval_overlap` (the eval item they copy, or
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

CREATE TABLE IF NOT EXISTS prompts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    set_name         TEXT NOT NULL,         -- prompts.set_name, e.g. pilot
    ref              TEXT NOT NULL,         -- node#item the prompt is planned for
    node_id          TEXT NOT NULL,
    task_type        TEXT NOT NULL,         -- write_code | use_api | explain | fix_code | design
    difficulty       TEXT NOT NULL,         -- beginner | intermediate | advanced
    context_mode     TEXT NOT NULL DEFAULT 'standalone',  -- standalone | codebase (M7)
    prompt           TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL UNIQUE,
    covers           TEXT NOT NULL DEFAULT '[]', -- JSON: node#item refs
    grounding        TEXT NOT NULL DEFAULT '{}', -- JSON: urls, chunk ids, repo file ids, symbols used
    round            INTEGER NOT NULL DEFAULT 1, -- writer call for this item (gap-fill rounds > 1)
    writer_model     TEXT,
    reviewer_model   TEXT,
    review           TEXT,                  -- JSON: verdict, scores, reason
    status           TEXT NOT NULL,         -- accepted | rejected | duplicate | eval_overlap
    error            TEXT,
    eval_overlap     TEXT,
    decontam_key     TEXT,
    run_id           INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_prompts_ref ON prompts(set_name, ref, status);

CREATE TABLE IF NOT EXISTS attempts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id        INTEGER NOT NULL REFERENCES prompts(id),
    n                INTEGER NOT NULL,      -- 1 = first answer, 2.. = repairs
    model            TEXT NOT NULL,
    answer           TEXT NOT NULL,
    feedback         TEXT,                  -- what failed in this answer (errors / judge issues); sent with the repair
    checks           TEXT NOT NULL DEFAULT '{}', -- JSON: code blocks, packages, compile/test results, judge
    status           TEXT NOT NULL,         -- pass | fail
    run_id           INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (prompt_id, n)
);

CREATE TABLE IF NOT EXISTS responses (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id        INTEGER NOT NULL UNIQUE REFERENCES prompts(id),
    set_name         TEXT NOT NULL,
    attempt_id       INTEGER REFERENCES attempts(id),  -- the final attempt
    attempts         INTEGER NOT NULL,
    repaired         INTEGER NOT NULL DEFAULT 0,  -- 1 = passed after at least one repair
    verification     TEXT,                  -- compile | test | judge | compile+judge | test+judge
    status           TEXT NOT NULL,         -- verified (code checked) | judged (prose checked) | failed
    eval_overlap     TEXT,
    decontam_key     TEXT,
    run_id           INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_responses_status ON responses(set_name, status);

CREATE TABLE IF NOT EXISTS preference_pairs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    prompt_id        INTEGER NOT NULL UNIQUE REFERENCES prompts(id),
    chosen_id        INTEGER NOT NULL REFERENCES attempts(id),    -- the passing answer
    rejected_id      INTEGER NOT NULL REFERENCES attempts(id),    -- the first failed answer
    reason           TEXT NOT NULL,         -- compile | test | judge | missing_code: why it was rejected
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codebases (
    id               TEXT PRIMARY KEY,      -- <repo>-<project>-<hash6>
    repo_id          INTEGER REFERENCES repos(id),
    full_name        TEXT NOT NULL,         -- owner/repo
    root             TEXT NOT NULL,         -- project folder in the repo ('' = repo root)
    commit_sha       TEXT,
    spdx             TEXT,
    files            TEXT NOT NULL,         -- JSON {relative path: text}: Ballerina.toml and .bal files
    file_count       INTEGER NOT NULL,
    tokens           INTEGER NOT NULL,      -- approximate (chars / 4)
    has_tests        INTEGER NOT NULL,
    modules          TEXT NOT NULL DEFAULT '[]', -- JSON: imported modules
    nodes            TEXT NOT NULL DEFAULT '[]', -- JSON: graph nodes (from the modules)
    items            TEXT NOT NULL DEFAULT '[]', -- JSON: node#item refs whose names appear in the code
    split            TEXT NOT NULL,         -- train | eval (eval codebases never feed training prompts)
    score            REAL,                  -- selection rank
    status           TEXT NOT NULL,         -- active | rejected (base build failed) | dropped
    verify_detail    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codebase_states (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    codebase_id      TEXT NOT NULL REFERENCES codebases(id) ON DELETE CASCADE,
    kind             TEXT NOT NULL,         -- compile_error | unfinished_feature | version_drift | failing_test
    target           TEXT NOT NULL,         -- file, or file::function
    description      TEXT NOT NULL,         -- what was changed, for people
    changed          TEXT NOT NULL,         -- JSON {path: new text}: the files that differ from the base
    patch            TEXT NOT NULL,         -- unified diff from the base
    expect           TEXT NOT NULL,         -- fails_build | builds | fails_tests
    verify_status    TEXT,                  -- pass | fail | skipped
    verify_detail    TEXT,                  -- JSON: build errors / test failures of the state
    status           TEXT NOT NULL,         -- active | retired
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (codebase_id, kind)
);

CREATE TABLE IF NOT EXISTS benchmark_problems (
    id               TEXT PRIMARY KEY,      -- <suite>/<task id>
    suite            TEXT NOT NULL,         -- humaneval | mbpp
    task_id          TEXT NOT NULL,
    source           TEXT NOT NULL,         -- JSON: the original problem (prompt, solution, tests)
    commit_sha       TEXT,
    spdx             TEXT,
    status           TEXT NOT NULL,         -- pending | translated | failed
    attempts         INTEGER NOT NULL DEFAULT 0,
    error            TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL,         -- base | teacher | any eval.models key
    model            TEXT NOT NULL,
    mode             TEXT NOT NULL,         -- compile (no code executed) | test (tests and programs run)
    samples          INTEGER NOT NULL,
    suites           TEXT NOT NULL DEFAULT '[]',
    status           TEXT NOT NULL,         -- running | done
    run_id           INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_results (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    eval_run_id      INTEGER NOT NULL REFERENCES eval_runs(id) ON DELETE CASCADE,
    item_id          TEXT NOT NULL REFERENCES eval_items(id),
    sample           INTEGER NOT NULL,
    answer           TEXT,
    passed           INTEGER NOT NULL,
    detail           TEXT,                  -- JSON: why it failed (errors, judge issues)
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (eval_run_id, item_id, sample)
);

CREATE TABLE IF NOT EXISTS mined_fixes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id          INTEGER REFERENCES repos(id),
    full_name        TEXT NOT NULL,
    commit_sha       TEXT NOT NULL,
    parent_sha       TEXT NOT NULL,
    message          TEXT NOT NULL,         -- first line of the commit message
    files            TEXT NOT NULL,         -- JSON [{path, before, after}] (.bal files changed)
    diff             TEXT NOT NULL,         -- unified diff of those files
    lines_changed    INTEGER NOT NULL,
    touches_tests    INTEGER NOT NULL,      -- 1 = the commit also changed tests
    spdx             TEXT,
    fix_hash         TEXT NOT NULL UNIQUE,  -- dedup key: code hashes of before + after
    status           TEXT NOT NULL,         -- mined (unverified: the project isn't built at that commit)
    eval_overlap     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (full_name, commit_sha)
);

CREATE TABLE IF NOT EXISTS symbols (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    module           TEXT NOT NULL,         -- ballerina/http
    prefix           TEXT NOT NULL,         -- http (as used in code: http:Client)
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL,         -- Client | Record | Function | Union | ...
    text             TEXT NOT NULL,         -- the API reference entry (signatures, fields, docs)
    page_id          INTEGER REFERENCES pages(id),
    status           TEXT NOT NULL DEFAULT 'active',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (module, kind, name)
);
CREATE INDEX IF NOT EXISTS ix_symbols_name ON symbols(prefix, name);

CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);  -- small markers (index build state)

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, heading_path, content='chunks', content_rowid='id', tokenize='porter unicode61');

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
    MIGRATIONS = (("llm_usage", "provider", "TEXT"), ("prompts", "codebase_state_id", "INTEGER"),
                  ("eval_items", "prompt", "TEXT"), ("eval_items", "task_type", "TEXT"),
                  ("eval_items", "reference", "TEXT"), ("eval_items", "meta", "TEXT"),
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
        # Close the cursor: an unfinished SELECT keeps a read transaction open, and a later write on
        # this connection then fails with "database is locked" once another process has committed.
        cur = self.conn.execute(sql, params)
        try:
            return cur.fetchone()
        finally:
            cur.close()

    def scalar(self, sql: str, params: tuple | dict = ()) -> Any:
        row = self.one(sql, params)
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
