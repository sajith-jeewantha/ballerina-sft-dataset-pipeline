"""`sftgen chunk`: documents (+ code examples) -> chunks.jsonl"""

from __future__ import annotations

from balpipe_common.config import ConfigError, Settings
from balpipe_common.ids import chunk_id
from balpipe_common.jsonl_io import read_models, write_jsonl_atomic
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun
from balpipe_common.schemas import Chunk, ChunkerInfo, CodeExample, Document

from sftgen.chunk.markdown_chunker import chunk_markdown, context_header
from sftgen.index.embedder import load_tokenizer
from sftgen.paths import Paths


def run(settings: Settings, *, limit: int | None = None) -> dict:
    paths = Paths(settings)
    count, max_seq = load_tokenizer(settings.embedding_model)
    if settings.chunk.max_tokens > max_seq:
        raise ConfigError(
            f"chunk.max_tokens={settings.chunk.max_tokens} exceeds {settings.embedding_model} "
            f"max_seq_length={max_seq}; text beyond that would be silently truncated when embedded."
        )
    info = ChunkerInfo(name=settings.chunk.name, version=settings.chunk.version,
                       max_tokens=settings.chunk.max_tokens, tokenizer=settings.embedding_model)
    with StageRun(settings, "chunk", paths.chunks_dir, inputs=[paths.documents, paths.code_examples]) as stage:
        log = get_logger(__name__, run_id=stage.run_id, stage="chunk")
        docs = [d for d in read_models(paths.documents, Document) if d.dedup.status == "unique"]
        if limit:
            docs = docs[:limit]
        examples_by_doc: dict[str, list[CodeExample]] = {}
        if paths.code_examples.exists():
            for e in read_models(paths.code_examples, CodeExample):
                examples_by_doc.setdefault(e.doc_id, []).append(e)

        chunks: list[Chunk] = []
        over_budget = 0
        for d in docs:
            for i, draft in enumerate(chunk_markdown(d.markdown, d.title, settings.chunk.max_tokens, count)):
                over_budget += draft.token_count > settings.chunk.max_tokens
                ex_ids = [e.example_id for e in examples_by_doc.get(d.doc_id, []) if e.code.strip() and e.code.strip() in draft.text]
                chunks.append(Chunk(
                    chunk_id=chunk_id(d.doc_id, draft.text, i), doc_id=d.doc_id, run_id=stage.run_id,
                    ordinal=i, heading_path=draft.heading_path, context_header=context_header(draft.heading_path),
                    text=draft.text, token_count=draft.token_count, has_code=draft.has_code,
                    code_example_ids=ex_ids, doc_type=d.doc_type, source_uri=d.source_uri, split=d.split,
                    license_spdx=d.license.spdx, chunker=info,
                ))
        n = write_jsonl_atomic(paths.chunks, chunks)
        stage.add_output(paths.chunks)
        stage.count("documents", len(docs))
        stage.count("chunks_written", n)
        stage.count("chunks_over_budget", over_budget)
        if over_budget:
            log.warning("some chunks exceed the token budget", extra={"event": "chunk.over_budget", "n": over_budget})
        log.info("chunk finished", extra={"event": "chunk.done", "chunks": n})
        return dict(stage.counts)
