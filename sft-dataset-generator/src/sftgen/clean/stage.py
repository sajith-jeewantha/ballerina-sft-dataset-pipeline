"""`sftgen clean`: raw records -> documents.jsonl

1. Load the newest raw record per identity (re-collected content supersedes old).
2. Route each record to its extractor (BBE folders are assembled from several files).
3. Relevance filter, exact + near-duplicate marking, split assignment.
4. Write documents.jsonl atomically + manifest.
"""

from __future__ import annotations

import re
from collections import defaultdict

from balpipe_common.config import Settings
from balpipe_common.ids import assign_split, doc_id, utc_now_iso
from balpipe_common.jsonl_io import iter_raw_dicts, write_jsonl_atomic
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun, package_versions
from balpipe_common.schemas import DedupInfo, DocStats, Document, ExtractorInfo, RawRecord

from sftgen import ballerina
from sftgen import markdown as md
from sftgen.clean import extractors as ex
from sftgen.clean.dedup import SOURCE_RANK, content_sha256, dedup
from sftgen.paths import Paths

BBE_PATH_RE = re.compile(r"^examples/([^/]+)/([^/]+)$")
BBE_REPO = "ballerina-platform/ballerina-distribution"
SITE_REPO = "ballerina-platform/ballerina-dev-website"
EIP_PATH_RE = re.compile(r"^pages/learn/enterprise-integration-patterns/enterprise-integration-patterns/([^/]+)/([^/]+)$")


def identity(rec: RawRecord) -> str:
    if rec.git:
        return f"github:{rec.git.repo}:{rec.git.path}"
    if rec.central:
        return f"central:{rec.central.org}/{rec.central.package}"
    return rec.source_uri


def stable_identity_url(it: ex.Extracted) -> str:
    """URL used for doc_id: the public page if known, else a commit-independent git URL.

    Using the commit-pinned source_uri would give a file a new doc_id (and possibly a
    different train/eval split) on every upstream commit.
    """
    if it.canonical_url:
        return it.canonical_url
    g = it.raw.git
    if g:
        return f"https://github.com/{g.repo}/blob/{g.ref}/{g.path}"
    return it.raw.source_uri


def load_latest_raw(paths: Paths) -> list[RawRecord]:
    latest: dict[str, RawRecord] = {}
    for source_dir in sorted(p for p in paths.raw.iterdir() if p.is_dir()) if paths.raw.exists() else []:
        for obj in iter_raw_dicts(source_dir):
            rec = RawRecord.model_validate(obj)
            key = identity(rec)
            if key not in latest or rec.fetch.fetched_at >= latest[key].fetch.fetched_at:
                latest[key] = rec
    return list(latest.values())


def extract_all(raws: list[RawRecord], log) -> tuple[list[ex.Extracted], int]:
    bbe_files: dict[str, dict[str, RawRecord]] = defaultdict(dict)
    eip_files: dict[str, dict[str, RawRecord]] = defaultdict(dict)
    bbe_index: dict[str, dict] = {}
    out: list[ex.Extracted] = []
    failed = 0
    for r in raws:
        if r.git and r.git.repo == BBE_REPO:
            if r.git.path == "examples/index.json":
                bbe_index = ex.parse_bbe_index(r.body)
                continue
            m = BBE_PATH_RE.match(r.git.path)
            if m:
                bbe_files[m.group(1)][m.group(2)] = r
                continue
        if r.git and r.git.repo == SITE_REPO:
            m = EIP_PATH_RE.match(r.git.path)
            if m:
                eip_files[m.group(1)][m.group(2)] = r
                continue
        try:
            item: ex.Extracted | None
            if r.source == "central":
                item = ex.extract_central(r)
            elif r.source == "web":
                item = ex.extract_html(r)
            elif r.media_type == "text/html" and r.git and "/spec/" in f"/{r.git.path}":
                item = ex.extract_spec_html(r)
            elif r.media_type == "text/markdown":
                item = ex.extract_github_markdown(r)
            elif r.media_type == "text/x-ballerina":
                item = ex.extract_bal_source(r)
            else:
                item = None
        except Exception as exc:  # one bad page must not stop the stage
            failed += 1
            log.warning("extract failed", extra={"event": "clean.extract_failed", "raw_id": r.raw_id, "err": str(exc)})
            continue
        if item:
            out.append(item)
    for name, files in sorted(eip_files.items()):
        item = ex.extract_eip(name, files)
        if item:
            out.append(item)
    for name, files in sorted(bbe_files.items()):
        item = ex.extract_bbe(name, files, bbe_index)
        if item:
            out.append(item)
    return out, failed


def is_relevant(item: ex.Extracted, s: Settings) -> str | None:
    """Return a rejection reason, or None if the document should be kept."""
    url = item.canonical_url or item.raw.source_uri
    if any(re.search(p, url) for p in s.clean.exclude_url_patterns):
        return "excluded_url"
    if item.doc_type in ("module_source", "bbe"):
        return None
    words = md.plain_words(item.markdown)
    if words < s.clean.min_words:
        return "too_short"
    _, bal_blocks = md.fence_counts(item.markdown)
    if item.doc_type in ("blog", "other") and bal_blocks == 0 and ballerina.signal_count(item.markdown) < s.clean.ballerina_signal_min:
        return "off_topic"
    return None


def run(settings: Settings, *, limit: int | None = None) -> dict:
    paths = Paths(settings)
    with StageRun(settings, "clean", paths.clean, manifest_name="_manifest.clean.json",
                  inputs=paths.raw_parts(), extra_versions=package_versions("trafilatura", "datasketch")) as stage:
        log = get_logger(__name__, run_id=stage.run_id, stage="clean")
        raws = load_latest_raw(paths)
        stage.count("raw_records", len(raws))
        items, failed = extract_all(raws, log)
        stage.count("extract_failed", failed)
        kept: list[ex.Extracted] = []
        for it in items:
            reason = is_relevant(it, settings)
            if reason:
                stage.reject(reason)
            else:
                kept.append(it)
        if limit:
            kept = kept[:limit]

        docs: list[Document] = []
        now = utc_now_iso()
        for it in kept:
            did = doc_id(stable_identity_url(it))
            fences, bal_blocks = md.fence_counts(it.markdown)
            docs.append(Document(
                doc_id=did, raw_id=it.raw.raw_id, run_id=stage.run_id, source=it.raw.source,
                source_uri=it.raw.source_uri, canonical_url=it.canonical_url, doc_type=it.doc_type,  # type: ignore[arg-type]
                title=it.title, description=it.description, category=it.category, markdown=it.markdown,
                content_sha256=content_sha256(it.markdown), ballerina_version=it.ballerina_version,
                license=it.raw.license, split=assign_split(did, settings.split.eval_fraction),
                stats=DocStats(word_count=md.plain_words(it.markdown), code_block_count=fences,
                               ballerina_block_count=bal_blocks),
                extractor=ExtractorInfo(name=it.extractor, version=it.version), bbe=it.bbe, created_at=now,
            ))

        # two raw records can map to the same doc_id (e.g. web + git copy of one page): keep the canonical one
        by_id: dict[str, Document] = {}
        for d in sorted(docs, key=lambda d: SOURCE_RANK[d.source]):
            by_id.setdefault(d.doc_id, d)
        docs = list(by_id.values())

        results = dedup(
            docs, key=lambda d: d.doc_id, text=lambda d: d.markdown,  # type: ignore[attr-defined]
            rank=lambda d: (SOURCE_RANK[d.source], -len(d.markdown), d.doc_id),  # type: ignore[attr-defined]
            threshold=settings.clean.near_dup_threshold, shingle_size=settings.clean.shingle_size,
            num_perm=settings.clean.minhash_num_perm,
        )
        for d in docs:
            r = results[d.doc_id]
            d.dedup = DedupInfo(status=r.status, duplicate_of=r.duplicate_of, near_dup_cluster=r.cluster)  # type: ignore[arg-type]
            stage.count(f"dedup_{r.status}")
        docs.sort(key=lambda d: d.doc_id)
        n = write_jsonl_atomic(paths.documents, docs)
        stage.add_output(paths.documents)
        stage.count("documents_written", n)
        for d in docs:
            stage.count(f"doc_type_{d.doc_type}")
        log.info("clean finished", extra={"event": "clean.done", "documents": n})
        return dict(stage.counts)

