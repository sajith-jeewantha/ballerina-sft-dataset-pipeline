"""What every collector yields: candidate pages that the scrape loop may fetch."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Fetched:
    body: str | None  # None = server said 304 Not Modified
    http_status: int
    fetch_url: str
    etag: str | None = None
    last_modified: str | None = None
    title: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Candidate:
    source: str  # bbe | spec | learn | central | github
    url: str     # public URL: the unique key and the citation in answers
    fetch: Callable[[sqlite3.Row | None], Fetched]
    version_key: str | None = None  # same as stored -> unchanged, nothing is downloaded
    title: str | None = None
    license: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Collector(Protocol):
    source: str

    def candidates(self) -> Iterator[Candidate]: ...
