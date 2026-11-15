"""`validate` (optional): compile the Ballerina code in answers with the `bal` CLI.

Answer status after this step:
  valid        every ```ballerina block compiled
  invalid      at least one block failed to compile (errors recorded)
  not_checked  no Ballerina code in the answer, `bal` is not installed, or the build timed out

Statement-only snippets are wrapped in `main` and well-known imports are added
before compiling (see `bal_compiler.prepare`); the answer text is not changed.
"""

from __future__ import annotations

import json
import logging

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker

from sftgen import markdown as md
from sftgen.validate.bal_compiler import BalCompiler, BalUnavailable

log = logging.getLogger(__name__)


def ballerina_blocks(answer: str) -> list[str]:
    return [f.code.strip("\n") for f in md.iter_fences(answer) if f.lang == "ballerina" and f.code.strip()]


def run(s: Settings, db: Database, run_: RunTracker, *, recheck: bool = False) -> dict[str, int]:
    # answers with no code never need another look; everything else not yet checked does
    where = "1 = 1" if recheck else "status = 'not_checked' AND COALESCE(validation_detail, '') != 'no Ballerina code'"
    rows = db.query(f"SELECT id, answer FROM answers WHERE {where} ORDER BY id")
    stats = {"answers_checked": 0, "valid": 0, "invalid": 0, "not_checked": 0}
    try:
        compiler = BalCompiler(s.validate_, s.cache_dir / "bal")
    except BalUnavailable as exc:
        log.warning("%s; answers stay not_checked", exc)
        for r in rows:
            db.update("answers", r["id"], {"status": "not_checked", "validation_detail": "bal CLI not installed"})
        stats.update({"not_checked": len(rows), "bal_available": 0})
        run_.stats.update(stats)
        return stats
    stats["bal_available"] = 1
    log.info("validating %d answers with Ballerina %s", len(rows), compiler.version)
    if rows:
        compiler.prewarm()
    for r in rows:
        blocks = ballerina_blocks(r["answer"])
        stats["answers_checked"] += 1
        if not blocks:
            db.update("answers", r["id"], {"status": "not_checked", "validation_detail": "no Ballerina code"})
            stats["not_checked"] += 1
            continue
        outcomes = compiler.compile_many(blocks)
        if any(o.status == "timeout" for o in outcomes):
            status, detail = "not_checked", "build timed out"
        elif all(o.status == "ok" for o in outcomes):
            status, detail = "valid", f"{len(blocks)} block(s) compiled"
        else:
            status = "invalid"
            detail = json.dumps([{"block": i + 1, "errors": o.errors[:5]} for i, o in enumerate(outcomes)
                                 if o.status == "error"])
        db.update("answers", r["id"], {"status": status, "validation_detail": detail})
        stats[status] += 1
        run_.count(f"answers_{status}")
    run_.stats.update(stats)
    return stats
