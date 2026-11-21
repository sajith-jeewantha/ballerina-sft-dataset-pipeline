"""sftgen: build a Ballerina question-and-answer dataset from official sources.

Commands (run in this order, or all at once with `run-all`):
  scrape               fetch sources into data/pipeline.db, convert to Markdown, chunk
  generate-questions   QUESTIONS_PER_CHUNK questions per new chunk, up to LIMIT_GENERATING
  generate-answers     answer every pending question from its source chunk
  validate             compile Ballerina code in answers with `bal` (optional)
  export               write data/dataset.jsonl (question, answer, source_url, category, difficulty)
  export-sft           write data/sft/{train,eval,metadata}.jsonl in chat `messages` format
  report               rebuild data/reports/report.html and print the summary
  run-all              scrape -> generate-questions -> generate-answers -> validate -> export -> export-sft
  graph validate       check graph/graph.json (schema 0.2, ballerina_graph rules, SOURCE.json hash)
  graph stats          node, item and edge counts of the knowledge graph
  graph sync           load the graph into the database and match item sources to scraped pages
                       (also runs automatically at the end of `scrape`)
  storage status       compare the local data folder with the bucket
  storage push         upload new or changed outputs and a database snapshot
  storage pull         download what is missing locally (newest database snapshot, outputs);
                       `--overwrite` replaces local files (the database is backed up first)

Storage: everything is written to LOCAL_DATA_DIR (default data/). With STORAGE_BUCKET=true
each command first restores a missing database from the bucket, then uploads its outputs
and a database snapshot when it finishes. The bucket layout is the same as the local folder.

Sources are pinned to one Swan Lake update (`sources.swan_lake`); resolved pins are kept
in config/sources.lock.json. `scrape --update-lock` re-resolves them.

Limits come from .env (LIMIT_SCRAPE_PAGES, LIMIT_GENERATING, QUESTIONS_PER_CHUNK,
REQUEST_DELAY_SECONDS) and are printed at startup. Every command ends by
refreshing the report.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from balpipe_common.config import ConfigError, Settings, load_settings
from balpipe_common.db import Database
from balpipe_common.logging import setup_logging
from balpipe_common.storage import Storage, StorageError

log = logging.getLogger("sftgen")

COMMANDS = ["scrape", "generate-questions", "generate-answers", "validate", "export", "export-sft", "report",
            "run-all", "graph", "storage"]
ACTIONS = {"graph": ["validate", "stats", "sync"], "storage": ["status", "push", "pull"]}
RUN_ALL = ["scrape", "generate-questions", "generate-answers", "validate", "export", "export-sft"]


def print_startup(s: Settings, command: str, store: Storage | None = None) -> None:
    def rel(p: Path) -> str:
        try:
            return str(p.relative_to(s.repo_root))
        except ValueError:
            return str(p)

    limit = lambda v, unit: f"{v} {unit}" if v else "0 (no limit)"  # noqa: E731
    lines = [
        f"sftgen {command}",
        f"  database               {rel(s.db_path)}",
        f"  LIMIT_SCRAPE_PAGES     {limit(s.limit_scrape_pages, 'pages per source per run')}",
        f"  LIMIT_GENERATING       {limit(s.limit_generating, 'questions per run')}",
        f"  QUESTIONS_PER_CHUNK    {s.questions_per_chunk}",
        f"  REQUEST_DELAY_SECONDS  {s.request_delay_seconds:g}",
        f"  GITHUB_TOKEN           {'set' if s.github_token else 'not set (GitHub API: 60 requests/hour)'}",
        f"  question model         {s.question_gen_model or '(not set)'}",
        f"  answer model           {s.answer_gen_model or '(not set)'}",
        f"  Swan Lake pin          {s.sources.swan_lake} (lock: {s.sources.lock_file})",
        f"  storage                {store.describe() if store else 'local only'}",
        "",
    ]
    print("\n".join(lines), flush=True)


def run_command(command: str, s: Settings, db: Database, a: argparse.Namespace) -> dict:
    with db.run(command, s.limits()) as run:
        if command == "scrape":
            from ballerinascraper.scrape import scrape_sources

            from sftgen.clean.stage import process_pages

            fetched = scrape_sources(s, db, run, refresh=a.refresh, only=a.source, update_lock=a.update_lock)
            processed = process_pages(s, db, run)
            return {"fetch": fetched, "process": processed, "graph": _sync_graph_quietly(s, db)}
        if command == "graph-sync":
            from sftgen.graph.coverage import sync

            out = sync(s, db)
            run.stats.update({k: v for k, v in out.items() if isinstance(v, int)})
            return out
        if command == "generate-questions":
            from sftgen.generate.questions import run as gen_questions

            return gen_questions(s, db, run)
        if command == "generate-answers":
            from sftgen.generate.answers import run as gen_answers

            return gen_answers(s, db, run, retry_failed=a.retry_failed)
        if command == "validate":
            from sftgen.validate.stage import run as validate

            return validate(s, db, run, recheck=a.recheck)
        if command == "export":
            from sftgen.export.stage import run as export

            return export(s, db, run)
        if command == "export-sft":
            from sftgen.export.sft import run as export_sft

            return export_sft(s, db, run)
    raise ValueError(command)


def _sync_graph_quietly(s: Settings, db: Database) -> dict:
    """Refresh graph coverage after a scrape; a graph problem never fails the scrape."""
    if not s.path(s.graph.path).is_file():
        return {"skipped": "no graph file"}
    from balpipe_common.graph import GraphError

    from sftgen.graph.coverage import sync

    try:
        return sync(s, db)
    except GraphError as exc:
        log.warning("graph not synced: %s (run `sftgen graph validate`)", exc)
        return {"error": str(exc)[:200]}


def graph_command(s: Settings, action: str) -> int:
    """`graph validate` and `graph stats` only read the graph file; they don't touch the database."""
    from balpipe_common.graph import GraphError, graph_stats, load_graph_json

    try:
        lg = load_graph_json(s.path(s.graph.path), s.path(s.graph.source_file))
    except FileNotFoundError:
        print(f"graph file not found: {s.graph.path}", file=sys.stderr)
        return 2
    except GraphError as exc:
        print(f"INVALID: {len(exc.errors)} error(s)", file=sys.stderr)
        for e in exc.errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    st = graph_stats(lg)
    if action == "validate":
        print(f"OK: {st['nodes']} nodes, {st['items_total']} items ({st['items']} active), {st['edges']} edges, "
              f"{st['warnings']} warning(s) · schema {st['schema_version']} · graph {st['version']}")
        for w in lg.warnings:
            print(f"  warning: {w}")
        return 0
    width = max(len(k) for k in st)
    for k, v in st.items():
        shown = ", ".join(f"{a} {b}" for a, b in v.items()) if isinstance(v, dict) else v
        print(f"{k:<{width}}  {shown}")
    return 0


def storage_command(s: Settings, store: Storage, action: str, overwrite: bool) -> int:
    if action != "status" and not store.enabled:
        print("STORAGE_BUCKET=false: nothing to sync. Set STORAGE_BUCKET=true and BUCKET_NAME in .env.",
              file=sys.stderr)
        return 2
    try:
        if action == "push":
            res = store.push(snapshot_db=True)
            print(f"pushed to {store.root_url}: {len(res.uploaded)} uploaded, {res.skipped} unchanged"
                  + (f", database snapshot {res.snapshot}" if res.snapshot else "")
                  + (f", {len(res.deleted)} old snapshot(s) deleted" if res.deleted else ""))
            for key in res.uploaded:
                print(f"  + {key}")
            return 0
        if action == "pull":
            res = store.pull(overwrite=overwrite)
            print(f"pulled from {store.root_url}: {len(res.downloaded)} downloaded, {res.skipped} kept")
            for key in res.downloaded:
                print(f"  + {key}")
            return 0
        st = store.status()
    except StorageError as exc:
        print(f"storage error: {exc}", file=sys.stderr)
        return 1
    print(f"storage      {st['root']}")
    print(f"local files  {st['local_files']}  ({store.local_root})")
    if store.enabled:
        print(f"bucket files {st['remote_files']}")
        print(f"db snapshots {st['db_snapshots']}" + (f" (latest {st['latest_snapshot']})" if st["latest_snapshot"] else ""))
        for label, key in (("local only (not pushed)", "local_only"), ("changed since push", "changed"),
                           ("bucket only (not pulled)", "remote_only")):
            items = st[key]
            print(f"{label}: {len(items)}")
            for k in items[:20]:
                print(f"  {k}")
            if len(items) > 20:
                print(f"  ... {len(items) - 20} more")
    return 0


def _push_quietly(store: Storage, *, snapshot_db: bool) -> str | None:
    """Upload outputs after a command. Returns an error message instead of raising."""
    if not store.enabled:
        return None
    try:
        res = store.push(snapshot_db=snapshot_db)
    except StorageError as exc:
        return str(exc)
    log.info("storage: %s", res.as_dict())
    return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sftgen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("action", nargs="?", choices=sorted({x for v in ACTIONS.values() for x in v}),
                   help="graph: validate (default) | stats | sync · storage: status (default) | push | pull")
    p.add_argument("--overwrite", action="store_true",
                   help="storage pull: replace local files (the local database is backed up first)")
    p.add_argument("--refresh", action="store_true", help="scrape: re-check pages already downloaded for changes")
    p.add_argument("--update-lock", action="store_true",
                   help="scrape: re-resolve pinned commits and package versions in config/sources.lock.json")
    p.add_argument("--source", action="append", choices=["bbe", "spec", "learn", "central", "github"],
                   help="scrape: only this source (repeatable)")
    p.add_argument("--retry-failed", action="store_true", help="generate-answers: retry questions that failed")
    p.add_argument("--recheck", action="store_true", help="validate: re-check answers already validated")
    p.add_argument("--config", type=Path, default=None, help="path to pipeline.yaml")
    p.add_argument("--log-format", choices=["json", "text"], default=None)
    a = p.parse_args(argv)

    try:
        s = load_settings(a.config)
    except (ConfigError, ValueError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(s.logging.level, a.log_format or s.logging.format)
    if a.action and a.action not in ACTIONS.get(a.command, []):
        p.error(f"'{a.action}' is not an action of '{a.command}'")
    if a.command == "graph" and (a.action or "validate") in ("validate", "stats"):
        return graph_command(s, a.action or "validate")
    try:
        store = Storage(s)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if a.command == "storage":
        return storage_command(s, store, a.action or "status", a.overwrite)
    print_startup(s, "graph sync" if a.command == "graph" else a.command, store)

    if store.enabled and not s.db_path.is_file():
        try:
            restored = store.restore_db()
        except StorageError as exc:
            print(f"storage error: {exc}", file=sys.stderr)
            return 1
        if restored:
            print(f"restored the database from {store.url(restored)}", flush=True)

    from sftgen.report import write_report

    code = 0
    push_error: str | None = None
    with Database(s.db_path) as db:
        commands = RUN_ALL if a.command == "run-all" else ["graph-sync" if a.command == "graph" else a.command]
        for command in commands:
            if command == "report":
                break
            if a.command == "run-all":
                print(f"== {command}", flush=True)
            try:
                stats = run_command(command, s, db, a)
                log.info("%s finished: %s", command, stats)
                push_error = push_error or _push_quietly(store, snapshot_db=False)
            except ConfigError as exc:
                print(f"config error: {exc}", file=sys.stderr)
                code = 2
                break
            except KeyboardInterrupt:
                print("interrupted; progress so far is saved", file=sys.stderr)
                code = 130
                break
            except Exception:
                log.exception("%s failed", command)
                code = 1
                break
        path, summary = write_report(s, db)
    # after the database is closed: the report, any files still unpushed, and a database snapshot
    push_error = push_error or _push_quietly(store, snapshot_db=True)
    print(summary)
    if store.enabled:
        if push_error:
            print(f"storage error: {push_error}\nlocal outputs are saved; run `sftgen storage push` to retry",
                  file=sys.stderr)
            code = code or 1
        else:
            print(f"Storage    synced to {store.root_url}")
    return code


if __name__ == "__main__":
    sys.exit(main())
