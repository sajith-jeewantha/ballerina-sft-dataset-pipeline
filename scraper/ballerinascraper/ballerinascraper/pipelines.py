"""Single item pipeline: persist each fetched page as an immutable RawRecord.

Extraction, quality filtering and dedup intentionally do NOT happen here any
more — they run in `sftgen clean`, so raw data can always be re-processed.
"""

from __future__ import annotations

from typing import Any

from balpipe_common.config import load_settings

from ballerinascraper.raw_sink import RawSink


class RawRecordPipeline:
    def __init__(self) -> None:
        self.sink: RawSink | None = None

    def open_spider(self) -> None:
        self.sink = RawSink(load_settings(), "web", fetcher="scrapy").__enter__()

    def process_item(self, item: dict[str, Any]) -> dict[str, Any]:
        assert self.sink is not None
        rec = self.sink.emit(**item)
        return {"raw_id": rec.raw_id if rec else None, "source_uri": item["source_uri"]}

    def close_spider(self) -> None:
        if self.sink is not None:
            self.sink.__exit__(None, None, None)
