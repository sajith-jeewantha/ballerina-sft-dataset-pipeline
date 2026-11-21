"""Storage switch: the local data folder, mirrored to a cloud bucket with the same layout.

The working copy is always local (`LOCAL_DATA_DIR`), because SQLite and `bal build` need a
real disk. With `STORAGE_BUCKET=true`, every file under it is also kept in the bucket under the
same relative path, so switching is one flag:

    LOCAL_DATA_DIR/dataset.jsonl          ->  gs://BUCKET/PREFIX/dataset.jsonl
    LOCAL_DATA_DIR/sft/train.jsonl        ->  gs://BUCKET/PREFIX/sft/train.jsonl
    LOCAL_DATA_DIR/reports/report.html    ->  gs://BUCKET/PREFIX/reports/report.html

A few inputs that live in the repo are copied too, for provenance:

    config/sources.lock.json              ->  sources/sources.lock.json
    graph/graph.json, graph/SOURCE.json   ->  graph/<graph version>/graph.json, SOURCE.json

The database is never edited in the bucket. After each command a gzipped snapshot is uploaded
to `db/pipeline-<UTC time>.db.gz` and `db/LATEST` names the newest one. `cache/` (HTTP, LLM
and compiler caches) and `.storage/` (this module's bookkeeping) are never uploaded.

Providers: `gcs` (auth: `gcloud auth application-default login`), `aws-s3` (auth:
AWS_PROFILE / AWS_REGION; S3_ENDPOINT_URL for S3-compatible stores such as Cloudflare R2) and
`local` (BUCKET_NAME is a folder; for testing the layout without a cloud account). Cloud
clients are optional extras: `pip install -e "shared[gcs]"` or `"shared[s3]"`.
"""

from __future__ import annotations

import fnmatch
import gzip
import hashlib
import json
import logging
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import ConfigError, Settings

log = logging.getLogger(__name__)

PROTOCOLS = {"gcs": "gs", "aws-s3": "s3", "local": "file"}
EXTRAS = {"gcs": "gcs", "aws-s3": "s3"}
DB_PREFIX = "db/"
LATEST_KEY = "db/LATEST"
STATE_DIR = ".storage"
JUNK = {".DS_Store", "Thumbs.db", "desktop.ini"}  # OS files, never uploaded


class StorageError(RuntimeError):
    """The bucket can't be reached, or its client library isn't installed."""


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@dataclass
class SyncResult:
    uploaded: list[str] = field(default_factory=list)
    skipped: int = 0
    downloaded: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    snapshot: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"uploaded": len(self.uploaded), "unchanged": self.skipped, "downloaded": len(self.downloaded),
                "snapshots_deleted": len(self.deleted), "db_snapshot": self.snapshot}


class Storage:
    """Local paths plus, when enabled, the bucket that mirrors them."""

    def __init__(self, s: Settings) -> None:
        self.s = s
        self.enabled = s.storage_bucket
        self._fs: Any = None
        if self.enabled and not s.bucket_name:
            raise ConfigError("STORAGE_BUCKET=true needs BUCKET_NAME (and STORAGE_PROVIDER). See .env.example.")

    # ------------------------------------------------------------------ addressing
    @property
    def local_root(self) -> Path:
        return self.s.data_root

    @property
    def _base(self) -> str:
        """The bucket root as fsspec sees it: `bucket/prefix` (or an absolute folder for `local`)."""
        name = str(self.s.bucket_name or "").strip().rstrip("/")
        if self.s.storage_provider == "local":
            name = str(self.s.path(name))
        prefix = self.s.bucket_prefix.strip("/")
        return f"{name}/{prefix}" if prefix else name

    @property
    def root_url(self) -> str:
        if not self.enabled:
            return str(self.local_root)
        return f"{PROTOCOLS[self.s.storage_provider]}://{self._base}"

    def remote(self, key: str) -> str:
        return f"{self._base}/{key}"

    def url(self, key: str) -> str:
        return f"{self.root_url}/{key}" if self.enabled else str(self.local_root / key)

    def describe(self) -> str:
        if not self.enabled:
            return "local only (STORAGE_BUCKET=false)"
        return f"local + {self.root_url}"

    # ------------------------------------------------------------------ bucket client
    @property
    def fs(self) -> Any:
        if not self.enabled:
            raise StorageError("storage bucket is off (STORAGE_BUCKET=false)")
        if self._fs is None:
            self._fs = self._make_fs()
        return self._fs

    def _make_fs(self) -> Any:
        import fsspec

        provider = self.s.storage_provider
        opts: dict[str, Any] = {}
        if provider == "local":
            opts["auto_mkdir"] = True
        elif provider == "aws-s3":
            if self.s.aws_profile:
                opts["profile"] = self.s.aws_profile
            client: dict[str, Any] = {}
            if self.s.aws_region:
                client["region_name"] = self.s.aws_region
            if self.s.s3_endpoint_url:
                client["endpoint_url"] = self.s.s3_endpoint_url
            if client:
                opts["client_kwargs"] = client
        try:
            return fsspec.filesystem(PROTOCOLS[provider], **opts)
        except ImportError as exc:
            extra = EXTRAS.get(provider, provider)
            raise StorageError(
                f"STORAGE_PROVIDER={provider} needs its client library: pip install -e \"shared[{extra}]\"") from exc

    def _call(self, what: str, fn: Any, *args: Any, **kw: Any) -> Any:
        try:
            return fn(*args, **kw)
        except (StorageError, FileNotFoundError):
            raise
        except Exception as exc:  # auth, network, permissions: one readable message
            raise StorageError(f"{what} failed for {self.root_url}: {type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------------ basic operations
    def exists(self, key: str) -> bool:
        return bool(self._call("exists", self.fs.exists, self.remote(key)))

    def list(self, prefix: str = "") -> dict[str, int]:
        """Remote keys under `prefix`, with their sizes."""
        fs = self.fs
        if hasattr(fs, "invalidate_cache"):
            fs.invalidate_cache()
        base = self._base
        start = f"{base}/{prefix}".rstrip("/")
        try:
            found = self._call("list", fs.find, start, detail=True)
        except FileNotFoundError:
            return {}
        out: dict[str, int] = {}
        strip = fs._strip_protocol(base).rstrip("/") + "/"
        for path, info in (found or {}).items():
            if info.get("type") == "directory":
                continue
            p = fs._strip_protocol(path)
            if p.startswith(strip):
                out[p[len(strip):]] = int(info.get("size") or 0)
        return out

    def put(self, local: Path, key: str) -> None:
        self._call(f"upload {key}", self.fs.put_file, str(local), self.remote(key))

    def get(self, key: str, local: Path) -> None:
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(f".{local.name}.part")
        self._call(f"download {key}", self.fs.get_file, self.remote(key), str(tmp))
        tmp.replace(local)

    def read_text(self, key: str) -> str:
        with self._call(f"read {key}", self.fs.open, self.remote(key), "rb") as f:
            return f.read().decode("utf-8")

    def write_text(self, key: str, text: str) -> None:
        with self._call(f"write {key}", self.fs.open, self.remote(key), "wb") as f:
            f.write(text.encode("utf-8"))

    def delete(self, key: str) -> None:
        self._call(f"delete {key}", self.fs.rm_file, self.remote(key))

    # ------------------------------------------------------------------ what gets synced
    def _excluded(self, key: str) -> bool:
        return key.rsplit("/", 1)[-1] in JUNK or any(fnmatch.fnmatchcase(key, pat) for pat in self.s.storage.exclude)

    def _is_db_file(self, key: str) -> bool:
        db = self.s.db_file
        return (key in (db, f"{db}-wal", f"{db}-shm", f"{db}-journal") or key.startswith(f"{db}.bak-")
                or key.endswith(".part"))

    def artifacts(self) -> dict[str, Path]:
        """Every file that belongs in the bucket, as {key: local path}."""
        out: dict[str, Path] = {}
        root = self.local_root
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if not p.is_file():
                    continue
                key = p.relative_to(root).as_posix()
                if not (self._excluded(key) or self._is_db_file(key)):
                    out[key] = p
        lock = self.s.path(self.s.sources.lock_file)
        if lock.is_file():
            out["sources/sources.lock.json"] = lock
        graph = self.s.path(self.s.graph.path)
        if graph.is_file():
            source = self.s.path(self.s.graph.source_file)
            version = _graph_version(graph, source)
            out[f"graph/{version}/graph.json"] = graph
            if source.is_file():
                out[f"graph/{version}/SOURCE.json"] = source
        return out

    # ------------------------------------------------------------------ bookkeeping
    @property
    def _state_file(self) -> Path:
        return self.local_root / STATE_DIR / "pushed.json"

    def _load_state(self) -> dict[str, str]:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return data.get(self.root_url, {})

    def _save_state(self, pushed: dict[str, str]) -> None:
        try:
            data = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            data = {}
        data[self.root_url] = pushed
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")

    # ------------------------------------------------------------------ sync
    def push(self, *, snapshot_db: bool = False) -> SyncResult:
        """Upload new or changed files; optionally a database snapshot. No-op when the bucket is off."""
        res = SyncResult()
        if not self.enabled:
            return res
        remote = self.list()
        pushed = self._load_state()
        for key, path in self.artifacts().items():
            digest = sha256_file(path)
            size = path.stat().st_size
            if remote.get(key) == size and pushed.get(key) == digest:
                res.skipped += 1
                continue
            self.put(path, key)
            pushed[key] = digest
            res.uploaded.append(key)
        self._save_state(pushed)
        if snapshot_db and self.s.storage.snapshot_db and self.s.db_path.is_file():
            res.snapshot = self.snapshot_db()
            res.deleted = self.prune_snapshots()
        return res

    def snapshot_db(self) -> str:
        """Upload a consistent, gzipped copy of pipeline.db and point db/LATEST at it."""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        key = f"{DB_PREFIX}{Path(self.s.db_file).stem}-{stamp}.db.gz"
        with tempfile.TemporaryDirectory(prefix="balpipe-snap-") as tmp:
            copy = Path(tmp) / "snapshot.db"
            src = sqlite3.connect(self.s.db_path)
            dst = sqlite3.connect(copy)
            try:
                src.backup(dst)  # consistent even while WAL has uncommitted pages
            finally:
                dst.close()
                src.close()
            gz = Path(tmp) / "snapshot.db.gz"
            with copy.open("rb") as f_in, gzip.open(gz, "wb", compresslevel=6) as f_out:
                shutil.copyfileobj(f_in, f_out, 1 << 20)
            self.put(gz, key)
        self.write_text(LATEST_KEY, key + "\n")
        return key

    def snapshots(self) -> list[str]:
        return sorted(k for k in self.list(DB_PREFIX) if k.endswith(".db.gz"))

    def prune_snapshots(self) -> list[str]:
        keep = self.s.storage.keep_db_snapshots
        snaps = self.snapshots()
        if keep <= 0 or len(snaps) <= keep:
            return []
        old = snaps[: len(snaps) - keep]
        for key in old:
            self.delete(key)
        return old

    def latest_snapshot(self) -> str | None:
        try:
            key = self.read_text(LATEST_KEY).strip()
        except FileNotFoundError:
            snaps = self.snapshots()
            return snaps[-1] if snaps else None
        return key or None

    def restore_db(self, key: str | None = None) -> str | None:
        """Download a snapshot (default: db/LATEST) to the local database path."""
        key = key or self.latest_snapshot()
        if not key:
            return None
        target = self.s.db_path
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="balpipe-restore-") as tmp:
            gz = Path(tmp) / "snapshot.db.gz"
            self.get(key, gz)
            part = target.with_name(f".{target.name}.part")
            with gzip.open(gz, "rb") as f_in, part.open("wb") as f_out:
                shutil.copyfileobj(f_in, f_out, 1 << 20)
            conn = sqlite3.connect(part)
            try:
                ok = conn.execute("PRAGMA quick_check").fetchone()[0]
            finally:
                conn.close()
            if ok != "ok":
                part.unlink(missing_ok=True)
                raise StorageError(f"snapshot {key} failed SQLite quick_check: {ok}")
            for suffix in ("-wal", "-shm"):
                target.with_name(target.name + suffix).unlink(missing_ok=True)
            part.replace(target)
        return key

    def pull(self, *, overwrite: bool = False) -> SyncResult:
        """Download what is missing locally: the newest database snapshot and data files.

        Existing local files are kept unless `overwrite`; a database that is replaced is moved
        to `pipeline.db.bak-<time>` first. Repo inputs (lockfile, graph) are only restored when
        missing, never overwritten: git owns them.
        """
        res = SyncResult()
        if not self.enabled:
            return res
        db = self.s.db_path
        if overwrite and db.is_file():
            backup = db.with_name(f"{db.name}.bak-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}")
            db.replace(backup)
            log.info("moved the local database to %s", backup)
        if not db.is_file():
            key = self.restore_db()
            if key:
                res.downloaded.append(key)
        lock = self.s.path(self.s.sources.lock_file)
        repo_inputs = {"sources/sources.lock.json": lock}
        for key, size in self.list().items():
            if key.startswith(DB_PREFIX) or key.startswith("graph/") or self._excluded(key):
                continue
            local = repo_inputs.get(key) or self.local_root / key
            if local.is_file() and (key in repo_inputs or not overwrite):
                res.skipped += 1
                continue
            self.get(key, local)
            res.downloaded.append(key)
        return res

    def status(self) -> dict[str, Any]:
        """Compare local artifacts with the bucket."""
        local = self.artifacts()
        out: dict[str, Any] = {"root": self.describe(), "local_files": len(local)}
        if not self.enabled:
            return out
        remote = self.list()
        pushed = self._load_state()
        files = {k: v for k, v in remote.items() if not k.startswith(DB_PREFIX)}
        changed = [k for k, p in local.items()
                   if k in files and (files[k] != p.stat().st_size or pushed.get(k) != sha256_file(p))]
        snaps = sorted(k for k in remote if k.startswith(DB_PREFIX) and k.endswith(".db.gz"))
        out.update({
            "remote_files": len(files),
            "local_only": sorted(set(local) - set(files)),
            "remote_only": sorted(set(files) - set(local)),
            "changed": sorted(changed),
            "db_snapshots": len(snaps),
            "latest_snapshot": snaps[-1] if snaps else None,
        })
        return out


def _graph_version(graph: Path, source: Path) -> str:
    try:
        commit = json.loads(source.read_text(encoding="utf-8")).get("commit")
    except (FileNotFoundError, ValueError):
        commit = None
    return (commit or sha256_file(graph))[:12]
