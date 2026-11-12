"""Per-run stage manifests: the mechanism behind resumable, auditable stages.

A stage opens a `StageRun`; it writes `_manifest.json` with status `running`
immediately, updates counts as it goes, and marks `completed` / `failed` on
exit. `find_resumable` returns the latest non-completed run whose inputs are
unchanged, so a re-invocation continues where the crash left off.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.ids import new_run_id, utc_now_iso
from balpipe_common.jsonl_io import file_sha256
from balpipe_common.schemas import FileRef, ItemError, Manifest

MANIFEST_NAME = "_manifest.json"
MAX_RECORDED_ERRORS = 500


def git_commit(repo_root: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def package_versions(*names: str) -> dict[str, str]:
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str] = {}
    for n in names:
        try:
            out[n] = version(n)
        except PackageNotFoundError:
            continue
    return out


def portable_path(p: Path) -> str:
    """Path relative to the repo root when inside it, so manifests are portable and
    don't embed machine-specific absolute paths (home directory, username)."""
    from balpipe_common.config import ConfigError, find_repo_root

    try:
        return Path(p).resolve().relative_to(find_repo_root()).as_posix()
    except (ValueError, ConfigError):
        return str(p)


def file_refs(paths: list[Path]) -> list[FileRef]:
    return [FileRef(path=portable_path(p), sha256=file_sha256(p)) for p in paths if p.is_file()]


def read_manifest(path: Path) -> Manifest | None:
    if not path.is_file():
        return None
    return Manifest.model_validate_json(path.read_text(encoding="utf-8"))


def find_resumable(stage_root: Path, stage: str, inputs: list[Path]) -> Path | None:
    """Return the run dir of the newest unfinished run of `stage` with identical inputs."""
    if not stage_root.is_dir():
        return None
    current = {r.path: r.sha256 for r in file_refs(inputs)}
    for run_dir in sorted((p for p in stage_root.iterdir() if p.is_dir()), reverse=True):
        m = read_manifest(run_dir / MANIFEST_NAME)
        if m and m.stage == stage and m.status != "completed":
            previous = {r.path: r.sha256 for r in m.inputs}
            if previous == current:
                return run_dir
    return None


class StageRun:
    """Context manager owning one stage invocation's manifest."""

    def __init__(
        self,
        settings: Settings,
        stage: str,
        manifest_dir: Path,
        *,
        inputs: list[Path] | None = None,
        run_id: str | None = None,
        extra_versions: dict[str, Any] | None = None,
        manifest_name: str = MANIFEST_NAME,
        resumable: bool = False,
    ) -> None:
        self.settings = settings
        self.stage = stage
        self.dir = manifest_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / manifest_name
        self.counts: Counter[str] = Counter()
        self.rejections: Counter[str] = Counter()
        self.errors: list[ItemError] = []
        self.outputs: list[Path] = []
        previous = read_manifest(self.path)
        # Only checkpointed stages (generate/validate/judge) continue a failed run; batch
        # stages always start fresh because they rewrite their whole output.
        self.resumed = resumable and previous is not None and previous.status != "completed"
        self.manifest = Manifest(
            run_id=run_id or (previous.run_id if self.resumed and previous else new_run_id(stage)),
            stage=stage,
            status="running",
            started_at=previous.started_at if self.resumed and previous else utc_now_iso(),
            inputs=file_refs(inputs or []),
            config_sha256=settings.config_sha256,
            config_snapshot=settings.redacted_snapshot(),
            versions={
                "pipeline_git_commit": git_commit(settings.repo_root),
                "python": platform.python_version(),
                **(extra_versions or {}),
            },
        )
        if self.resumed and previous:
            self.counts.update({k: v for k, v in previous.counts.items() if isinstance(v, int)})
            self.errors = list(previous.errors)

    @property
    def run_id(self) -> str:
        return self.manifest.run_id

    def count(self, key: str, n: int = 1) -> None:
        self.counts[key] += n

    def reject(self, reason: str, n: int = 1) -> None:
        self.rejections[reason] += n

    def error(self, item_id: str, err: BaseException | str) -> None:
        if len(self.errors) < MAX_RECORDED_ERRORS:
            self.errors.append(ItemError(item_id=item_id, error=str(err)[:500]))
        self.counts["errors"] += 1

    def add_output(self, path: Path) -> None:
        if path not in self.outputs:
            self.outputs.append(path)

    def _flush(self, status: str) -> None:
        self.manifest.status = status  # type: ignore[assignment]
        self.manifest.counts = {**dict(self.counts), **({"rejected": dict(self.rejections)} if self.rejections else {})}
        self.manifest.errors = self.errors
        self.manifest.outputs = [
            FileRef(path=portable_path(p), records=_count_lines(p)) for p in self.outputs if p.exists()
        ]
        if status != "running":
            self.manifest.finished_at = utc_now_iso()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.manifest.model_dump(mode="json"), indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def checkpoint(self) -> None:
        self._flush("running")

    def __enter__(self) -> StageRun:
        self._flush("running")
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: object) -> None:
        if exc is not None:
            self.error("<stage>", f"{exc_type.__name__ if exc_type else 'Error'}: {exc}")
        self._flush("failed" if exc is not None else "completed")


def _count_lines(path: Path) -> int | None:
    if path.is_dir():
        return None
    if path.suffix == ".gz":
        import gzip

        with gzip.open(path, "rb") as fh:
            return sum(1 for _ in fh)
    with open(path, "rb") as fh:
        return sum(1 for _ in fh)
