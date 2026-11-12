"""JSONL read/write helpers: streaming reads, durable appends, atomic rewrites.

* `JsonlAppender` flushes and fsyncs every record so a crash loses at most the
  record being written (used for checkpointed stages).
* `write_jsonl_atomic` writes to `*.tmp` and renames, so readers never see a
  half-written file (used for batch stages).
* `.gz` paths are transparently compressed.
"""

from __future__ import annotations

import gzip
import io
import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, Any, TypeVar

from pydantic import BaseModel

M = TypeVar("M", bound=BaseModel)


def _open_text(path: Path, mode: str) -> IO[str]:
    if path.suffix == ".gz":
        return io.TextIOWrapper(gzip.open(path, mode.replace("t", "") + "b"), encoding="utf-8")  # type: ignore[arg-type]
    return open(path, mode, encoding="utf-8")


def _to_line(record: BaseModel | dict[str, Any]) -> str:
    if isinstance(record, BaseModel):
        return record.model_dump_json()
    return json.dumps(record, ensure_ascii=False)


def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield one dict per non-empty line. Raises with file:line on malformed JSON."""
    path = Path(path)
    with _open_text(path, "rt") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{lineno}: invalid JSON ({exc})") from exc


def read_models(path: str | Path, model: type[M]) -> Iterator[M]:
    for obj in read_jsonl(path):
        yield model.model_validate(obj)


def read_models_many(paths: Iterable[Path], model: type[M]) -> Iterator[M]:
    for p in paths:
        yield from read_models(p, model)


def existing_ids(path: str | Path, key: str) -> set[str]:
    """IDs already present in a (possibly partial) JSONL output — for resuming."""
    path = Path(path)
    if not path.exists():
        return set()
    ids: set[str] = set()
    with _open_text(path, "rt") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ids.add(json.loads(line)[key])
            except (json.JSONDecodeError, KeyError):
                # A torn last line from a crash is expected; ignore it.
                continue
    return ids


def write_jsonl_atomic(path: str | Path, records: Iterable[BaseModel | dict[str, Any]]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    n = 0
    with _open_text(tmp, "wt") as fh:
        for rec in records:
            fh.write(_to_line(rec) + "\n")
            n += 1
        fh.flush()
    os.replace(tmp, path)
    return n


class JsonlAppender:
    """Append-only writer that makes every record durable before returning."""

    def __init__(self, path: str | Path, *, fsync: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._repair_torn_tail()
        self._fh = open(self.path, "a", encoding="utf-8")
        self._fsync = fsync
        self.count = 0

    def _repair_torn_tail(self) -> None:
        """Drop a partial last line left by a crash so appends start on a clean line."""
        if not self.path.exists() or self.path.stat().st_size == 0:
            return
        with open(self.path, "rb+") as fh:
            fh.seek(-1, os.SEEK_END)
            if fh.read(1) == b"\n":
                return
            fh.seek(0)
            data = fh.read()
            cut = data.rfind(b"\n") + 1
            fh.seek(cut)
            fh.truncate()

    def write(self, record: BaseModel | dict[str, Any]) -> None:
        self._fh.write(_to_line(record) + "\n")
        self._fh.flush()
        if self._fsync:
            os.fsync(self._fh.fileno())
        self.count += 1

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> JsonlAppender:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class PartWriter:
    """Writes gzipped JSONL parts (`part-00000.jsonl.gz`, ...) of at most `part_size` records."""

    def __init__(self, directory: str | Path, part_size: int = 1000) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.part_size = part_size
        # never overwrite parts from an earlier (e.g. resumed) writer in the same dir
        self._part = len(list(self.dir.glob("part-*.jsonl.gz")))
        self._in_part = 0
        self._fh: IO[str] | None = None
        self.count = 0
        self.paths: list[Path] = []

    def _rotate(self) -> None:
        if self._fh:
            self._fh.close()
        path = self.dir / f"part-{self._part:05d}.jsonl.gz"
        self.paths.append(path)
        self._fh = _open_text(path, "wt")
        self._part += 1
        self._in_part = 0

    def write(self, record: BaseModel | dict[str, Any]) -> None:
        if self._fh is None or self._in_part >= self.part_size:
            self._rotate()
        assert self._fh is not None
        self._fh.write(_to_line(record) + "\n")
        self._fh.flush()
        self._in_part += 1
        self.count += 1

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> PartWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def iter_raw_dicts(source_dir: str | Path) -> Iterator[dict[str, Any]]:
    """Yield records from every `<run>/part-*.jsonl.gz` under a raw source dir.

    Tolerates a truncated last part or torn final line left by an interrupted run.
    """
    import zlib

    for part in sorted(Path(source_dir).glob("*/part-*.jsonl.gz")):
        try:
            with gzip.open(part, "rt", encoding="utf-8") as fh:
                for line in fh:
                    if line.strip():
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            break
        except (EOFError, OSError, zlib.error):
            continue


def file_sha256(path: str | Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def mirror_to_gcs(local_path: Path, data_root: Path, bucket: str | None) -> str | None:
    """Optionally copy a file to gs://bucket/<path relative to data_root>. No-op without a bucket."""
    if not bucket:
        return None
    try:
        from google.cloud import storage  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("GCS mirroring needs `pip install -e shared[gcs]`") from exc
    rel = local_path.resolve().relative_to(data_root.resolve()).as_posix()
    storage.Client().bucket(bucket).blob(rel).upload_from_filename(str(local_path))
    return f"gs://{bucket}/{rel}"
