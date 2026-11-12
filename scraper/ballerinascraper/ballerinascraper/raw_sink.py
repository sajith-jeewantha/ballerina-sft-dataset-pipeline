"""Writes RawRecords for one collect run and skips content that has not changed.

Used by every collector (GitHub, Central) and by the Scrapy pipeline, so
change detection, file layout and manifests are identical for all sources:

    data/raw/<source>/<run_id>/part-NNNNN.jsonl.gz
    data/raw/<source>/<run_id>/_manifest.json
"""

from __future__ import annotations


from balpipe_common.config import Settings
from balpipe_common.ids import new_run_id, raw_id, sha256_text, utc_now_iso
from balpipe_common.jsonl_io import PartWriter, iter_raw_dicts, mirror_to_gcs
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun, package_versions
from balpipe_common.schemas import CentralInfo, FetchInfo, GitInfo, License, RawRecord, Source

FETCHER_VERSION = "1.0.0"


def identity_of(rec: dict) -> str:
    """Version-independent key for change detection (must match `identity_uri` passed to emit)."""
    git, central = rec.get("git"), rec.get("central")
    if git:
        return f"https://github.com/{git['repo']}/blob/{git['ref']}/{git['path']}"
    if central:
        return f"https://central.ballerina.io/{central['org']}/{central['package']}"
    return rec["source_uri"]


class RawSink:
    def __init__(self, settings: Settings, source: Source, *, fetcher: str) -> None:
        self.settings = settings
        self.source = source
        self.fetcher = fetcher
        self.source_dir = settings.data_path("raw", source)
        base_id = new_run_id(f"collect-{source}")
        self.run_id, n = base_id, 1
        while (self.source_dir / self.run_id).exists():  # two runs within the same second
            n += 1
            self.run_id = f"{base_id}-{n}"
        self.run_dir = self.source_dir / self.run_id
        self.log = get_logger(__name__, run_id=self.run_id, stage=f"collect.{source}")
        # change-detection index from all previous runs
        self.seen_sha: dict[str, str] = {}  # source_uri -> raw_sha256
        self.seen_blob: dict[str, str] = {}  # "repo:path" -> blob_sha
        for rec in iter_raw_dicts(self.source_dir):
            self.seen_sha[identity_of(rec)] = rec["raw_sha256"]
            git = rec.get("git")
            if git and git.get("blob_sha"):
                self.seen_blob[f"{git['repo']}:{git['path']}"] = git["blob_sha"]
        self._writer: PartWriter | None = None
        self.stage = StageRun(
            settings, f"collect-{source}", self.run_dir, run_id=self.run_id,
            extra_versions=package_versions("scrapy", "requests"),
        )

    # ------------------------------------------------------------------ #
    def __enter__(self) -> RawSink:
        self.stage.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._writer:
            self._writer.close()
            for p in self._writer.paths:
                self.stage.add_output(p)
                if self.settings.gcs_mirror:
                    try:
                        mirror_to_gcs(p, self.settings.data_root, self.settings.gcs_bucket)
                    except Exception as exc:  # mirroring is best-effort; local data is the source of truth
                        self.stage.error(str(p), f"gcs mirror failed: {exc}")
        self.stage.__exit__(*exc)  # type: ignore[arg-type]
        self.log.info("collect finished", extra={"event": "collect.done", "counts": dict(self.stage.counts)})

    def blob_unchanged(self, repo: str, path: str, blob_sha: str | None) -> bool:
        return bool(blob_sha) and self.seen_blob.get(f"{repo}:{path}") == blob_sha

    def emit(
        self,
        *,
        source_uri: str,
        media_type: str,
        body: str,
        requested_url: str,
        final_url: str,
        http_status: int,
        content_type_header: str | None = None,
        etag: str | None = None,
        last_modified: str | None = None,
        git: GitInfo | None = None,
        central: CentralInfo | None = None,
        license: License | None = None,
        identity_uri: str | None = None,
    ) -> RawRecord | None:
        """Write a RawRecord unless identical content was already collected.

        `identity_uri` is the version-independent key for change detection
        (git URIs are pinned to a commit, so they change on every commit).
        """
        raw_sha = sha256_text(body)
        key = identity_uri or source_uri
        if self.seen_sha.get(key) == raw_sha:
            self.stage.count("unchanged")
            return None
        rec = RawRecord(
            raw_id=raw_id(source_uri, raw_sha),
            run_id=self.run_id,
            source=self.source,
            source_uri=source_uri,
            media_type=media_type,
            body=body,
            raw_sha256=raw_sha,
            fetch=FetchInfo(
                requested_url=requested_url,
                final_url=final_url,
                http_status=http_status,
                content_type_header=content_type_header,
                etag=etag,
                last_modified=last_modified,
                fetched_at=utc_now_iso(),
                fetcher=self.fetcher,
                fetcher_version=FETCHER_VERSION,
                user_agent=self.settings.crawl.user_agent,
            ),
            git=git,
            central=central,
            license=license or License(),
        )
        if self._writer is None:
            self._writer = PartWriter(self.run_dir)
        self._writer.write(rec)
        self.seen_sha[key] = raw_sha
        self.stage.count("written")
        if self.stage.counts["written"] % 100 == 0:
            self.stage.checkpoint()
        return rec
