"""Canonical data locations (see ARCHITECTURE.md §5)."""

from __future__ import annotations

from pathlib import Path

from balpipe_common.config import Settings


class Paths:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.raw = s.data_path("raw")
        self.clean = s.data_path("clean")
        self.documents = self.clean / "documents.jsonl"
        self.code_examples = self.clean / "code_examples.jsonl"
        self.chunks_dir = s.data_path("chunks", s.chunk.slug)
        self.chunks = self.chunks_dir / "chunks.jsonl"
        self.chroma = s.data_path("chroma")
        self.sft = s.data_path("sft")
        self.cache = s.data_path("cache")

    def sft_run(self, run_id: str) -> Path:
        return self.sft / run_id

    def raw_parts(self) -> list[Path]:
        return sorted(self.raw.glob("*/*/part-*.jsonl.gz"))
