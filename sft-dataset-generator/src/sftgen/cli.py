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
            "run-all"]
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
        "",
    ]
    print("\n".join(lines), flush=True)


def run_command(command: str, s: Settings, db: Database, a: argparse.Namespace) -> dict:
    with db.run(command, s.limits()) as run:
        if command == "scrape":
            from ballerinascraper.scrape import scrape_sources

            from sftgen.clean.stage import process_pages

            fetched = scrape_sources(s, db, run, refresh=a.refresh, only=a.source)
            processed = process_pages(s, db, run)
            return {"fetch": fetched, "process": processed}
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


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sftgen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("--refresh", action="store_true", help="scrape: re-check pages already downloaded for changes")
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
    print_startup(s, a.command)

    from sftgen.report import write_report

    code = 0
    with Database(s.db_path) as db:
        for command in (RUN_ALL if a.command == "run-all" else [a.command]):
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
