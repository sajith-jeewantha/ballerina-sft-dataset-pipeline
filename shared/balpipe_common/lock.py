"""`config/sources.lock.json`: the pinned versions every phase reads.

One Swan Lake update is pinned (`sources.swan_lake`, e.g. 2201.14.1). From it:

* `bbe`      ballerina-distribution at the matching release tag, resolved to a commit
* `repos`    each module repo pinned to the commit first scraped
* `central`  each package pinned to the newest version built for the pinned Swan Lake
             update or earlier (`ballerinaVersion` <= pin)

Entries are written the first time a source is seen and reused afterwards, so
re-runs read exactly the same versions. `sftgen scrape --update-lock` re-resolves
them. The file is committed to git; it holds no secrets.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from balpipe_common.ids import utc_now_iso

LOCK_VERSION = 1


def version_tuple(version: str | None) -> tuple[int, ...]:
    """'2201.14.1' -> (2201, 14, 1); unparseable -> () (sorts lowest)."""
    if not version:
        return ()
    return tuple(int(x) for x in re.findall(r"\d+", version.split("-", 1)[0])[:4])


def compatible(built_with: str | None, pin: str) -> bool:
    """A package built with an older or equal distribution works on the pinned one."""
    return bool(built_with) and version_tuple(built_with) <= version_tuple(pin)


class SourceLock:
    def __init__(self, path: Path, swan_lake: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {"lock_version": LOCK_VERSION, "swan_lake": swan_lake,
                                     "bbe": {}, "repos": {}, "central": {}}
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if loaded.get("swan_lake") == swan_lake:
                self.data.update(loaded)
            else:  # a new Swan Lake pin starts a fresh lock; the old one stays in git history
                self.data["previous_swan_lake"] = loaded.get("swan_lake")
        self.dirty = False

    @property
    def swan_lake(self) -> str:
        return str(self.data["swan_lake"])

    # ------------------------------------------------------------------ #
    def get(self, section: str, key: str) -> dict[str, Any] | None:
        value = self.data[section].get(key)
        return value if isinstance(value, dict) else None

    def set(self, section: str, key: str, value: dict[str, Any]) -> None:
        current = self.data[section].get(key)
        if current is not None and {k: v for k, v in current.items() if k != "locked_at"} == value:
            return
        self.data[section][key] = {**value, "locked_at": utc_now_iso()}
        self.dirty = True

    def counts(self) -> dict[str, int]:
        return {"repos": len(self.data["repos"]), "central": len(self.data["central"]),
                "bbe": 1 if self.data["bbe"] else 0}

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ordered = {k: (dict(sorted(v.items())) if isinstance(v, dict) and k in ("repos", "central") else v)
                   for k, v in self.data.items()}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".sources-lock-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ordered, f, indent=1, sort_keys=False, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp, self.path)
        self.dirty = False
