"""Ballerina Central, through its public JSON API (no HTML scraping needed).

* package list + README: `GET /2.0/registry/packages?org=<org>`
* API docs:              `GET /2.0/docs/<org>/<package>/<version>`

One page per package, pinned in `config/sources.lock.json` to the newest version
built for the pinned Swan Lake update or earlier (`ballerinaVersion` <= pin;
`GET /2.0/registry/packages/<org>/<pkg>` lists the versions). Packages with no
such version are skipped. The page body is the package's stable metadata,
README and a compact form of its API docs, so answers can be checked against
real function and type signatures.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

from balpipe_common.config import CentralCfg
from balpipe_common.lock import SourceLock, compatible

from ballerinascraper.collectors.base import Candidate, Fetched
from ballerinascraper.collectors.http import PoliteSession

PACKAGE_FIELDS = (
    "organization", "name", "version", "summary", "readme", "licenses", "sourceCodeLocation", "keywords",
    "ballerinaVersion", "languageSpecificationVersion",
)
DOC_KINDS = ("clients", "listeners", "functions", "classes", "objectTypes", "records", "enums", "errors",
             "types", "unionTypes", "constants", "annotations", "serviceTypes")


def type_str(t: dict[str, Any] | None) -> str:
    """Render a Central docs type object as Ballerina source text (best effort)."""
    if not t:
        return ""
    members = t.get("memberTypes") or []
    if t.get("isTuple"):
        s = "[" + ", ".join(type_str(m) for m in members) + "]"
    elif members and (t.get("isAnonymousUnionType") or not t.get("name")):
        joiner = "&" if t.get("isIntersectionType") else "|"
        s = joiner.join(type_str(m) for m in members)
        if t.get("isParenthesisedType") or t.get("isArrayType"):
            s = f"({s})"
    else:
        s = t.get("name") or ""
        if t.get("constraint"):
            s += f"<{type_str(t['constraint'])}>"
        if t.get("elementType"):
            s = type_str(t["elementType"])
    s += "[]" * int(t.get("arrayDimensions") or 0)
    if t.get("isNullable") and not s.endswith("?"):
        s += "?"
    if t.get("isTypeDesc"):
        s = f"typedesc<{s}>"
    if t.get("isRestParam"):
        s += "..."
    return s or "anydata"


def _function(f: dict[str, Any]) -> dict[str, Any]:
    params = [
        {"name": p.get("name"), "type": type_str(p.get("type")), "default": p.get("defaultValue") or None,
         "description": (p.get("description") or "").strip()}
        for p in f.get("parameters") or []
    ]
    returns = [{"type": type_str(r.get("type")), "description": (r.get("description") or "").strip()}
               for r in f.get("returnParameters") or []]
    qualifiers = [q for q, on in (("isolated", f.get("isIsolated")), ("remote", f.get("isRemote")),
                                  ("resource", f.get("isResource"))) if on]
    return {"name": f.get("name"), "qualifiers": qualifiers, "accessor": f.get("accessor") or None,
            "resourcePath": f.get("resourcePath") or None, "description": (f.get("description") or "").strip(),
            "parameters": params, "returns": returns}


def compact_docs(docs: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep names, descriptions and signatures; drop rendering metadata (docs JSON can be megabytes)."""
    out = []
    for mod in (docs.get("docsData") or {}).get("modules") or []:
        entry: dict[str, Any] = {"id": mod.get("id"), "description": (mod.get("description") or "").strip()}
        for kind in DOC_KINDS:
            items = []
            for it in mod.get(kind) or []:
                item: dict[str, Any] = {"name": it.get("name"), "description": (it.get("description") or "").strip()}
                if kind == "functions":
                    item = _function(it)
                if it.get("fields"):
                    item["fields"] = [{"name": f.get("name"), "type": type_str(f.get("type")),
                                       "description": (f.get("description") or "").strip()} for f in it["fields"]]
                methods = [*([it["initMethod"]] if it.get("initMethod") else []), *(it.get("methods") or []),
                           *(it.get("remoteMethods") or []), *(it.get("resourceMethods") or []),
                           *(it.get("otherMethods") or [])]
                if methods:
                    item["methods"] = [_function(m) for m in methods if not m.get("isDeprecated")]
                if it.get("type") and kind in ("constants", "types"):
                    item["type"] = type_str(it.get("type"))
                if not it.get("isDeprecated"):
                    items.append(item)
            if items:
                entry[kind] = items
        out.append(entry)
    return out


class CentralCollector:
    source = "central"

    def __init__(self, http: PoliteSession, cfg: CentralCfg, ttl_hours: float, lock: SourceLock,
                 *, update_lock: bool = False) -> None:
        self.http = http
        self.cfg = cfg
        self.ttl_hours = ttl_hours
        self.lock = lock
        self.update_lock = update_lock

    def _package(self, org: str, name: str, version: str) -> dict[str, Any]:
        url = f"{self.cfg.api}/registry/packages/{org}/{name}/{version}"
        return self.http.get_cached(url, ttl_hours=24 * 365).json() or {}  # a published version never changes

    def pinned_version(self, pkg: dict[str, Any]) -> str | None:
        """Newest version built for the pinned Swan Lake update or earlier; recorded in the lock."""
        o, n = pkg["organization"], pkg["name"]
        key = f"{o}/{n}"
        locked = self.lock.get("central", key)
        if locked and not self.update_lock:
            return locked.get("version")
        pin = self.lock.swan_lake
        choice: tuple[str | None, str | None] = (None, None)
        if compatible(pkg.get("ballerinaVersion"), pin):
            choice = (pkg["version"], pkg.get("ballerinaVersion"))
        else:
            versions = self.http.get_cached(f"{self.cfg.api}/registry/packages/{o}/{n}",
                                            ttl_hours=self.ttl_hours).json() or []
            for v in versions:  # newest first
                built = self._package(o, n, v).get("ballerinaVersion")
                if compatible(built, pin):
                    choice = (v, built)
                    break
        self.lock.set("central", key, {"version": choice[0], "ballerina_version": choice[1]})
        return choice[0]

    def packages(self, org: str) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            url = f"{self.cfg.api}/registry/packages?org={org}&limit={self.cfg.page_size}&offset={offset}"
            page = self.http.get_cached(url, ttl_hours=self.ttl_hours).json() or {}
            packages = page.get("packages") or []
            if not packages:
                return
            yield from packages
            offset += len(packages)
            if offset >= int(page.get("count") or 0):
                return

    def candidates(self) -> Iterator[Candidate]:
        for org in self.cfg.orgs:
            for pkg in self.packages(org):
                o, n = pkg.get("organization"), pkg.get("name")
                if not (o and n and pkg.get("version")) or pkg.get("isDeprecated"):
                    continue
                v = self.pinned_version(pkg)
                if not v:
                    continue  # no version built for the pinned Swan Lake update
                if v != pkg["version"]:
                    pkg = {**self._package(o, n, v), "organization": o, "name": n, "version": v}
                licenses = pkg.get("licenses") or []
                yield Candidate(
                    self.source, f"https://central.ballerina.io/{o}/{n}", version_key=v, title=f"{o}/{n}",
                    license=" OR ".join(licenses) or None,
                    fetch=lambda row, p=pkg: self._fetch(p), meta={"org": o, "package": n, "version": v},
                )

    def _fetch(self, pkg: dict[str, Any]) -> Fetched:
        o, n, v = pkg["organization"], pkg["name"], pkg["version"]
        body: dict[str, Any] = {"package": {k: pkg.get(k) for k in PACKAGE_FIELDS}, "api_docs": []}
        docs_url = f"{self.cfg.api}/docs/{o}/{n}/{v}"
        if self.cfg.include_api_docs:
            try:
                body["api_docs"] = compact_docs(self.http.get(docs_url).json() or {})
            except Exception as exc:  # README alone is still useful
                body["api_docs_error"] = f"{type(exc).__name__}: {exc}"
        return Fetched(json.dumps(body, ensure_ascii=False, sort_keys=True), 200, docs_url,
                       meta={"org": o, "package": n, "version": v})
