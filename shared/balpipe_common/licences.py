"""Licence gate and the licence register (`docs/licence-register.csv`).

Only code under a permissive licence is collected. The licence GitHub reports is a hint; the
gate reads the repo's own LICENSE file and decides:

* `safe`     a permissive licence text was found and agrees with what GitHub reports
* `check`    needs a person: no LICENSE file, an unrecognised text, or a mismatch
* `exclude`  no licence at all (all rights reserved), or a copyleft / non-commercial licence

The register is the audit trail: one row per repo looked at, with the licence, verdict, pinned
commit and the sha256 of the LICENSE file. People can edit it:

* a row set to `exclude` is never collected, whatever the gate says;
* to allow a `check` repo after reading its licence, set `verdict` to `safe` and start `notes`
  with `verified:` (e.g. `verified: MIT text in COPYING, 2026-11-21`). The gate then keeps it.
"""

from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from pathlib import Path

from .ids import utc_now_iso

PERMISSIVE = {"Apache-2.0", "MIT", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense",
              "CC0-1.0", "Zlib", "BSL-1.0"}
# copyleft, weak copyleft, source-available and non-commercial licences: not used for training data
RESTRICTED_PREFIXES = ("GPL", "LGPL", "AGPL", "MPL", "EPL", "EUPL", "CDDL", "OSL", "CC-BY-SA", "CC-BY-NC",
                       "CC-BY-ND", "SSPL", "BUSL", "Elastic", "Commons-Clause", "CPAL", "RPL", "WTFPL")
UNKNOWN = {"", "NOASSERTION", "OTHER", "NONE"}

COLUMNS = ["owner", "repo", "spdx", "verdict", "intended_use", "size_kb", "pinned_sha", "checked_on", "notes",
           "licence_file_sha256", "files"]

# (SPDX id, every phrase must appear) checked in order; first match wins
_TEXTS: list[tuple[str, tuple[str, ...]]] = [
    ("AGPL-3.0", ("gnu affero general public license",)),
    ("LGPL", ("gnu lesser general public license",)),
    ("GPL", ("gnu general public license",)),
    ("MPL-2.0", ("mozilla public license",)),
    ("EPL", ("eclipse public license",)),
    ("Apache-2.0", ("apache license", "version 2.0")),
    ("BSL-1.0", ("boost software license",)),  # before MIT: shares its opening sentence
    ("MIT", ("permission is hereby granted, free of charge",)),
    ("BSD-3-Clause", ("redistribution and use in source and binary forms", "neither the name")),
    ("BSD-2-Clause", ("redistribution and use in source and binary forms",)),
    ("ISC", ("permission to use, copy, modify, and/or distribute this software for any purpose",)),
    ("Unlicense", ("this is free and unencumbered software released into the public domain",)),
    ("CC0-1.0", ("cc0 1.0 universal",)),
]


def normalize_spdx(value: str | None) -> str | None:
    """'Apache-2.0 (reported)' -> 'Apache-2.0'; NOASSERTION/OTHER/'' -> None."""
    if value is None:
        return None
    v = value.replace("(reported)", "").strip()
    return None if v.upper() in UNKNOWN else v


def is_permissive(spdx: str | None) -> bool:
    return spdx in PERMISSIVE


def is_restricted(spdx: str | None) -> bool:
    return bool(spdx) and any(spdx.startswith(p) for p in RESTRICTED_PREFIXES)  # type: ignore[union-attr]


def detect_licence(text: str | None) -> str | None:
    """SPDX id of a licence text, from fixed phrases; None if unrecognised."""
    if not text:
        return None
    t = re.sub(r"\s+", " ", text.lower())
    for spdx, phrases in _TEXTS:
        if all(p in t for p in phrases):
            return spdx
    return None


def pre_gate(reported: str | None, register_row: dict[str, str] | None, *,
             has_licence_key: bool = True) -> tuple[str, str] | None:
    """Decide without cloning when the answer is already `exclude`; None means 'clone and check'.

    `has_licence_key` is False when GitHub found no licence file at all (`license: null`);
    NOASSERTION/OTHER (a file GitHub could not classify) is cloned and read.
    """
    if register_row and register_row.get("verdict") == "exclude":
        return "exclude", register_row.get("notes") or "excluded in the register"
    if register_row and (register_row.get("notes") or "").lower().startswith("verified:"):
        return None
    if not has_licence_key:
        return "exclude", "no licence (all rights reserved)"
    if reported is None:
        return None  # GitHub may just not recognise the file: look at it
    if is_restricted(reported):
        return "exclude", f"{reported} is not a permissive licence"
    return None


def decide(reported: str | None, detected: str | None, register_row: dict[str, str] | None,
           *, licence_file: bool, has_licence_key: bool = True) -> tuple[str, str, str | None]:
    """(verdict, note, effective SPDX) from GitHub's licence and the LICENSE file text."""
    if register_row and register_row.get("verdict") == "exclude":
        return "exclude", register_row.get("notes") or "excluded in the register", reported or detected
    if register_row and (register_row.get("notes") or "").lower().startswith("verified:") \
            and register_row.get("verdict") in ("safe", "check"):
        return register_row["verdict"], register_row["notes"], normalize_spdx(register_row.get("spdx")) or detected
    if detected and is_restricted(detected):
        return "exclude", f"LICENSE file is {detected}", detected
    if detected and is_permissive(detected):
        if reported in (None, detected) or (reported and detected and reported.startswith(detected)):
            note = "" if reported == detected else f"licence detected from the LICENSE file ({detected})"
            return "safe", note, detected
        return "check", f"GitHub reports {reported} but the LICENSE file reads as {detected}", reported
    if not licence_file:
        if reported is None and not has_licence_key:
            return "exclude", "no licence (all rights reserved)", None
        return "check", f"no LICENSE file at the repo root (GitHub reports {reported or 'none'})", reported
    if reported and is_restricted(reported):
        return "exclude", f"{reported} is not a permissive licence", reported
    return "check", f"unrecognised LICENSE text (GitHub reports {reported or 'none'})", reported


class LicenceRegister:
    """Read, update and write the register CSV. Unknown extra columns are kept."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.columns = list(COLUMNS)
        self.rows: dict[tuple[str, str], dict[str, str]] = {}
        if path.is_file():
            with path.open(newline="", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for col in reader.fieldnames or []:
                    if col not in self.columns:
                        self.columns.append(col)
                for row in reader:
                    self.rows[(row["owner"], row["repo"])] = {k: (v or "") for k, v in row.items()}
        self.dirty = False

    def get(self, full_name: str) -> dict[str, str] | None:
        owner, _, repo = full_name.partition("/")
        return self.rows.get((owner, repo))

    def upsert(self, full_name: str, **fields: object) -> None:
        owner, _, repo = full_name.partition("/")
        row = self.rows.get((owner, repo)) or {c: "" for c in self.columns} | {"owner": owner, "repo": repo}
        before = dict(row)
        keep_if_set = {"intended_use"}  # written by people; only filled when empty
        for k, v in fields.items():
            if v is None:
                continue
            if k in keep_if_set and row.get(k):
                continue
            row[k] = str(v)
        if row != before:
            row["checked_on"] = utc_now_iso()[:10]
            self.rows[(owner, repo)] = row
            self.dirty = True

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in self.rows.values():
            out[r.get("verdict") or "unknown"] = out.get(r.get("verdict") or "unknown", 0) + 1
        return out

    def save(self) -> None:
        if not self.dirty:
            return
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=self.columns, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for key in sorted(self.rows):
            w.writerow({c: self.rows[key].get(c, "") for c in self.columns})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".licence-register-", suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(buf.getvalue())
        os.replace(tmp, self.path)
        self.dirty = False
