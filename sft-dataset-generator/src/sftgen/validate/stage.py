"""`sftgen validate`: candidates.jsonl -> validated.jsonl

Free, deterministic checks before any judge tokens are spent:
* compile every Ballerina block in the answer (cached, sandboxed, pooled),
* reject answers that mention a hidden "context",
* record `module:symbol` references unknown to the corpus.
"""

from __future__ import annotations

from balpipe_common.config import Settings
from balpipe_common.ids import sha256_text
from balpipe_common.jsonl_io import JsonlAppender, existing_ids, read_models
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun
from balpipe_common.schemas import CodeBlockCheck, CodeExample, CodeValidation, SFTCandidate

from sftgen import ballerina
from sftgen.paths import Paths
from sftgen.validate.bal_compiler import BalCompiler, BalUnavailable
from sftgen.validate.static_checks import ballerina_blocks, mentions_context, unknown_symbols

REQUIRES_CODE = {"write_code", "complete_code", "debug_fix", "convert_from_language", "api_usage"}


def known_symbols(paths: Paths) -> set[str]:
    known: set[str] = set()
    if paths.code_examples.exists():
        for e in read_models(paths.code_examples, CodeExample):
            known.update(e.symbols)
    return known


def check_candidate(c: SFTCandidate, compiler: BalCompiler | None, known: set[str]) -> SFTCandidate:
    answer = c.messages[-1].content
    blocks = ballerina_blocks(answer)
    required = c.task_type in REQUIRES_CODE
    checks: list[CodeBlockCheck] = []
    if compiler:
        for code, o in zip(blocks, compiler.compile_many(blocks), strict=True):
            checks.append(CodeBlockCheck(code_sha256=sha256_text(code), code_form=o.code_form,  # type: ignore[arg-type]
                                         compile_status=o.status, errors=o.compile_errors(),  # type: ignore[arg-type]
                                         wrapped=o.wrapped, imports_added=o.imports_added))
    else:
        checks = [CodeBlockCheck(code_sha256=sha256_text(b), code_form=ballerina.code_form(b),  # type: ignore[arg-type]
                                 compile_status="skipped") for b in blocks]
    c.code_validation = CodeValidation(required=required, blocks=checks,
                                       ballerina_version=compiler.version if compiler else None)
    c.checks.mentions_context = mentions_context(answer)
    c.checks.unknown_symbols = unknown_symbols(blocks, known)

    reasons: list[str] = []
    if required and not blocks:
        reasons.append("missing_required_code")
    for b in checks:
        if b.compile_status in ("error", "timeout") and (required or b.code_form != "fragment"):
            reasons.append(f"compile_{b.compile_status}")
            break
    if c.checks.mentions_context:
        reasons.append("mentions_context")
    if reasons:
        c.decision.status, c.decision.reasons = "rejected", reasons
    return c


def run(settings: Settings, *, run_id: str, limit: int | None = None) -> dict:
    paths = Paths(settings)
    run_dir = paths.sft_run(run_id)
    src, out_path = run_dir / "candidates.jsonl", run_dir / "validated.jsonl"
    with StageRun(settings, "validate", run_dir, inputs=[src], run_id=run_id,
                  manifest_name="_manifest.validate.json", resumable=True) as stage:
        log = get_logger(__name__, run_id=run_id, stage="validate")
        compiler: BalCompiler | None = None
        if settings.validate_.enabled:
            try:
                compiler = BalCompiler(settings.validate_, paths.cache / "compile")
            except BalUnavailable as exc:
                log.warning("compile check unavailable", extra={"event": "validate.no_bal", "err": str(exc)})
        known = known_symbols(paths)
        done = existing_ids(out_path, "id")
        todo = [c for c in read_models(src, SFTCandidate) if c.id not in done]
        if limit:
            todo = todo[:limit]
        stage.count("skipped_existing", len(done))
        with JsonlAppender(out_path) as out:
            for c in todo:
                try:
                    c = check_candidate(c, compiler, known)
                except Exception as exc:  # noqa: BLE001
                    stage.error(c.id, exc)
                    continue
                out.write(c)
                stage.count(f"status_{c.decision.status}")
                for r in c.decision.reasons:
                    stage.reject(r)
                if out.count % 50 == 0:
                    stage.checkpoint()
        stage.add_output(out_path)
        return dict(stage.counts)
