"""sftgen: build a Ballerina question-and-answer dataset from official sources.

Commands (run in this order, or all at once with `run-all`):
  scrape               fetch sources into data/pipeline.db, convert to Markdown, chunk
  generate-questions   QUESTIONS_PER_CHUNK questions per new chunk, up to LIMIT_GENERATING
  generate-answers     answer every pending question from its source chunk
  validate             compile Ballerina code in answers with `bal` (optional)
  export               write data/dataset.jsonl (question, answer, source_url, category, difficulty)
  export-sft           write data/sft/{train,eval,metadata}.jsonl in chat `messages` format
  report               rebuild reports/report.html and print the summary
  run-all              scrape -> generate-questions -> generate-answers -> validate -> export -> export-sft
  graph validate       check graph/graph.json (schema 0.2, ballerina_graph rules, SOURCE.json hash)
  graph stats          node, item and edge counts of the knowledge graph
  graph sync           load the graph into the database and match item sources to scraped pages
                       (also runs automatically at the end of `scrape`)

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

log = logging.getLogger("sftgen")

COMMANDS = ["scrape", "generate-questions", "generate-answers", "validate", "export", "export-sft", "report",
            "run-all", "graph"]
GRAPH_ACTIONS = ["validate", "stats", "sync"]
RUN_ALL = ["scrape", "generate-questions", "generate-answers", "validate", "export", "export-sft"]


def print_startup(s: Settings, command: str) -> None:
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sftgen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("action", nargs="?", choices=GRAPH_ACTIONS, help="graph: validate (default) | stats | sync")
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
    if a.action and a.command != "graph":
        p.error(f"'{a.action}' is only valid after 'graph'")
    if a.command == "graph" and (a.action or "validate") in ("validate", "stats"):
        return graph_command(s, a.action or "validate")
    print_startup(s, "graph sync" if a.command == "graph" else a.command)

    from sftgen.report import write_report

    code = 0
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
    print(summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
