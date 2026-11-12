"""sftgen — clean, extract, chunk, index, generate, validate, judge, export.

Examples:
  sftgen clean
  sftgen generate --limit 20 --dry-run
  sftgen validate            # defaults to the latest generate run
  sftgen all                 # every stage after collect
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from balpipe_common.config import ConfigError, Settings, load_settings
from balpipe_common.logging import setup_logging

STAGES = ["clean", "extract", "chunk", "index", "generate", "validate", "judge", "export"]


def latest_run(settings: Settings) -> str:
    sft = settings.data_path("sft")
    runs = sorted(p.name for p in sft.iterdir() if (p / "candidates.jsonl").exists()) if sft.exists() else []
    if not runs:
        raise ConfigError("No generate run found under data/sft; run `sftgen generate` first or pass --run-id.")
    return runs[-1]


def run_stage(stage: str, s: Settings, a: argparse.Namespace) -> dict:
    if stage == "clean":
        from sftgen.clean.stage import run
        return run(s, limit=a.limit)
    if stage == "extract":
        from sftgen.extract.stage import run
        return run(s, limit=a.limit)
    if stage == "chunk":
        from sftgen.chunk.stage import run
        return run(s, limit=a.limit)
    if stage == "index":
        from sftgen.index.stage import run
        return run(s, limit=a.limit, force=a.force)
    if stage == "generate":
        from sftgen.generate.stage import run
        out = run(s, limit=a.limit, run_id=a.run_id, dry_run=a.dry_run)
        a.run_id = out.get("run_id", a.run_id)
        return out
    run_id = a.run_id or latest_run(s)
    a.run_id = run_id
    if stage == "validate":
        from sftgen.validate.stage import run
        return run(s, run_id=run_id, limit=a.limit)
    if stage == "judge":
        from sftgen.judge.stage import run
        return run(s, run_id=run_id, limit=a.limit)
    if stage == "export":
        from sftgen.export.stage import run
        return run(s, run_id=run_id)
    raise ValueError(stage)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sftgen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("stage", choices=[*STAGES, "all"])
    p.add_argument("--limit", type=int, default=None, help="process at most N items (smoke tests)")
    p.add_argument("--run-id", default=None, help="SFT run id (generate/validate/judge/export); resumes if it exists")
    p.add_argument("--dry-run", action="store_true", help="generate: print the plan without calling any model")
    p.add_argument("--force", action="store_true", help="index: rebuild the Chroma collection")
    p.add_argument("--config", type=Path, default=None, help="path to pipeline.yaml")
    p.add_argument("--log-format", choices=["json", "text"], default=None)
    a = p.parse_args(argv)

    try:
        s = load_settings(a.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    setup_logging(s.logging.level, a.log_format or s.logging.format)

    stages = STAGES if a.stage == "all" else [a.stage]
    try:
        for st in stages:
            result = run_stage(st, s, a)
            print(json.dumps({"stage": st, **result}, default=str))
            if st == "generate" and a.dry_run:
                break
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
