"""`sftgen index`: chunks.jsonl -> Chroma collection `<prefix>__<model>__<chunker>`.

Chroma is a derived index: delete `data/chroma/` and re-run to rebuild.
Writes use upsert keyed by chunk_id, so re-runs are idempotent.
"""

from __future__ import annotations

from typing import Any

from balpipe_common.config import Settings
from balpipe_common.jsonl_io import read_models
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun, package_versions
from balpipe_common.schemas import Chunk

from sftgen.index.embedder import embed, slug
from sftgen.paths import Paths


def collection_name(settings: Settings) -> str:
    raw = f"{settings.index.collection_prefix}__{slug(settings.embedding_model)}__{settings.chunk.slug}"
    return raw.replace(".", "-")[:63]


def chroma_metadata(c: Chunk) -> dict[str, Any]:
    return {"doc_id": c.doc_id, "doc_type": c.doc_type, "split": c.split, "has_code": c.has_code,
            "license_spdx": c.license_spdx, "source_uri": c.source_uri, "heading": c.context_header}


def open_collection(settings: Settings, *, create: bool = False):  # type: ignore[no-untyped-def]
    import chromadb

    client = chromadb.PersistentClient(path=str(Paths(settings).chroma))
    name = collection_name(settings)
    if create:
        return client.get_or_create_collection(name, metadata={"hnsw:space": "cosine",
                                                               "embedding_model": settings.embedding_model})
    return client.get_collection(name)


def run(settings: Settings, *, limit: int | None = None, force: bool = False) -> dict:
    paths = Paths(settings)
    with StageRun(settings, "index", paths.chroma, inputs=[paths.chunks],
                  extra_versions=package_versions("chromadb", "sentence-transformers")) as stage:
        log = get_logger(__name__, run_id=stage.run_id, stage="index")
        if force:
            import chromadb

            try:
                chromadb.PersistentClient(path=str(paths.chroma)).delete_collection(collection_name(settings))
            except Exception:
                pass
        col = open_collection(settings, create=True)
        existing = set(col.get(include=[])["ids"]) if col.count() else set()
        chunks = list(read_models(paths.chunks, Chunk))
        if limit:
            chunks = chunks[:limit]
        todo = [c for c in chunks if c.chunk_id not in existing]
        stage.count("already_indexed", len(chunks) - len(todo))
        bs = settings.index.batch_size
        for i in range(0, len(todo), bs):
            batch = todo[i : i + bs]
            vecs = embed(settings.embedding_model, [c.embed_text for c in batch], bs)
            col.upsert(ids=[c.chunk_id for c in batch], embeddings=vecs.tolist(),
                       documents=[c.embed_text for c in batch], metadatas=[chroma_metadata(c) for c in batch])
            stage.count("indexed", len(batch))
            stage.checkpoint()
        # drop vectors for chunks that no longer exist (content changed -> new chunk ids)
        stale = list(existing - {c.chunk_id for c in chunks}) if not limit else []
        for i in range(0, len(stale), 500):
            col.delete(ids=stale[i : i + 500])
        stage.count("stale_removed", len(stale))
        stage.count("collection_size", col.count())
        log.info("index finished", extra={"event": "index.done", "collection": collection_name(settings)})
        return dict(stage.counts)


def retrieve(col, settings: Settings, query: str, *, k: int, split: str) -> list[dict]:  # type: ignore[no-untyped-def]
    vec = embed(settings.embedding_model, [query])
    res = col.query(query_embeddings=vec.tolist(), n_results=k, where={"split": split})
    return [{"id": i, "text": d, "metadata": m, "distance": dist}
            for i, d, m, dist in zip(res["ids"][0], res["documents"][0], res["metadatas"][0], res["distances"][0], strict=True)]
