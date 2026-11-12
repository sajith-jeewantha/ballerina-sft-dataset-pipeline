"""`sftgen extract`: documents.jsonl -> code_examples.jsonl

Pairs code with prose, compiles every example (cached), runs trusted BBE
programs against their `.out` file when upstream says the output is
verifiable, and marks exact duplicates of code across documents.
"""

from __future__ import annotations

from balpipe_common.config import Settings
from balpipe_common.ids import example_id, normalize_ws, sha256_text
from balpipe_common.jsonl_io import read_models, write_jsonl_atomic
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun
from balpipe_common.schemas import CodeExample, CodeValidationResult, DedupInfo, Document, UpstreamFlags

from sftgen import ballerina
from sftgen.clean.dedup import SOURCE_RANK
from sftgen.extract.pairing import extract_pairs
from sftgen.paths import Paths
from sftgen.validate.bal_compiler import BalCompiler, BalUnavailable


def run(settings: Settings, *, limit: int | None = None) -> dict:
    paths = Paths(settings)
    with StageRun(settings, "extract", paths.clean, manifest_name="_manifest.extract.json",
                  inputs=[paths.documents]) as stage:
        log = get_logger(__name__, run_id=stage.run_id, stage="extract")
        docs = [d for d in read_models(paths.documents, Document) if d.dedup.status == "unique"]
        docs.sort(key=lambda d: (SOURCE_RANK[d.source], d.doc_id))
        examples: list[CodeExample] = []
        for d in docs:
            for p in extract_pairs(d):
                code_sha = sha256_text(p.code)
                examples.append(CodeExample(
                    example_id=example_id(d.doc_id, p.ordinal, code_sha), doc_id=d.doc_id, run_id=stage.run_id,
                    source_uri=d.source_uri, ordinal=p.ordinal, title=d.title, heading_path=p.heading_path,
                    intro=p.intro, notes=p.notes, code=p.code, code_sha256=code_sha,
                    code_form=ballerina.code_form(p.code),  # type: ignore[arg-type]
                    imports=ballerina.imports(p.code), symbols=ballerina.symbols(p.code),
                    expected_output=p.expected_output, pairing_method=p.method,  # type: ignore[arg-type]
                    upstream_flags=(UpstreamFlags(verify_build=d.bbe.verify_build, verify_output=d.bbe.verify_output,
                                                  need_central=d.bbe.need_central) if d.bbe else None),
                    split=d.split, license=d.license,
                ))
        if limit:
            examples = examples[:limit]

        # exact code duplicates (normalized whitespace) across documents: keep the first (canonical source)
        seen: dict[str, str] = {}
        for e in examples:
            key = sha256_text(normalize_ws(ballerina.strip_comments_and_strings(e.code)))
            if key in seen:
                e.dedup = DedupInfo(status="exact_dup", duplicate_of=seen[key])
                stage.count("dedup_exact_dup")
            else:
                seen[key] = e.example_id

        if settings.validate_.enabled:
            try:
                compiler = BalCompiler(settings.validate_, paths.cache / "compile")
                compiler.prewarm()
                # doc_comment pairs are API declarations lifted out of a module (often `external`
                # Java bindings); they cannot compile standalone, so they stay "skipped".
                unique = [e for e in examples if e.dedup.status == "unique" and e.pairing_method != "doc_comment"]
                outcomes = compiler.compile_many([e.code for e in unique])
                for e, o in zip(unique, outcomes, strict=True):
                    e.validation = CodeValidationResult(
                        compile_status=o.status, errors=o.compile_errors(), wrapped=o.wrapped,  # type: ignore[arg-type]
                        imports_added=o.imports_added, ballerina_version=o.ballerina_version, checked_at=o.checked_at)
                    stage.count(f"compile_{o.status}")
                    if (settings.validate_.run_bbe_output and o.status == "ok" and e.expected_output
                            and e.upstream_flags and e.upstream_flags.verify_output):
                        e.validation.output_match = compiler.run_and_compare(e.code, e.expected_output)
                        stage.count(f"output_match_{e.validation.output_match}")
            except BalUnavailable as exc:
                log.warning("compile check skipped", extra={"event": "extract.no_bal", "err": str(exc)})
                stage.count("compile_skipped", len(examples))

        n = write_jsonl_atomic(paths.code_examples, examples)
        stage.add_output(paths.code_examples)
        stage.count("examples_written", n)
        for e in examples:
            stage.count(f"pairing_{e.pairing_method}")
        log.info("extract finished", extra={"event": "extract.done", "examples": n})
        return dict(stage.counts)
