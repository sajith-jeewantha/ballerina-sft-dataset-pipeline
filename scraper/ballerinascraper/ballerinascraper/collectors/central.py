"""Collect package metadata + README Markdown from the Ballerina Central registry API.

The listing returns signed, expiring download URLs and live counters; those
volatile fields are dropped before hashing so an unchanged package keeps an
unchanged `raw_sha256`.
"""

from __future__ import annotations

import json

from balpipe_common.config import Settings
from balpipe_common.schemas import CentralInfo, License

from ballerinascraper.collectors.http import PoliteSession
from ballerinascraper.raw_sink import RawSink

API = "https://api.central.ballerina.io/2.0/registry/packages"
STABLE_FIELDS = (
    "organization", "name", "version", "platform", "languageSpecificationVersion", "isDeprecated",
    "deprecateMessage", "summary", "readme", "template", "licenses", "authors", "sourceCodeLocation",
    "keywords", "ballerinaVersion", "modules",
)
MODULE_FIELDS = ("name", "summary", "apiDocURL")


def stable_package_view(pkg: dict) -> dict:
    view = {k: pkg.get(k) for k in STABLE_FIELDS}
    view["modules"] = [{k: m.get(k) for k in MODULE_FIELDS} for m in (pkg.get("modules") or [])]
    return view


class CentralCollector:
    def __init__(self, settings: Settings, *, limit: int | None = None) -> None:
        self.settings = settings
        self.limit = limit
        self.http = PoliteSession(
            settings.crawl.user_agent, delay_s=settings.crawl.collector_delay_s,
            timeout_s=settings.crawl.download_timeout_s, headers={"Accept": "application/json"},
        )

    def run(self) -> dict[str, int]:
        src = self.settings.sources
        with RawSink(self.settings, "central", fetcher="central_api") as sink:
            for org in src.central_orgs:
                offset, seen_in_org = 0, 0
                while True:
                    if self.limit and sink.stage.counts["written"] >= self.limit:
                        return dict(sink.stage.counts)
                    url = f"{API}?org={org}&limit={src.central_page_size}&offset={offset}"
                    try:
                        resp = self.http.get(url)
                        resp.raise_for_status()
                        page = resp.json()
                    except Exception as exc:
                        sink.stage.error(url, exc)
                        break
                    packages = page.get("packages") or []
                    if not packages:
                        break
                    for pkg in packages:
                        self._emit(sink, pkg, url, resp.status_code)
                        seen_in_org += 1
                        if src.central_max_packages and seen_in_org >= src.central_max_packages:
                            break
                    if src.central_max_packages and seen_in_org >= src.central_max_packages:
                        break
                    offset += len(packages)
                    if offset >= int(page.get("count") or 0):
                        break
            return dict(sink.stage.counts)

    def _emit(self, sink: RawSink, pkg: dict, url: str, status: int) -> None:
        org, name, version = pkg.get("organization"), pkg.get("name"), pkg.get("version")
        if not (org and name and version):
            sink.stage.count("skipped_incomplete")
            return
        if pkg.get("isDeprecated"):
            sink.stage.count("skipped_deprecated")
            return
        licenses = pkg.get("licenses") or []
        modules = pkg.get("modules") or []
        sink.emit(
            source_uri=f"https://central.ballerina.io/{org}/{name}/{version}",
            identity_uri=f"https://central.ballerina.io/{org}/{name}",
            media_type="application/json",
            body=json.dumps(stable_package_view(pkg), ensure_ascii=False, sort_keys=True),
            requested_url=url,
            final_url=url,
            http_status=status,
            content_type_header="application/json",
            central=CentralInfo(
                org=org, package=name, version=version,
                ballerina_version=pkg.get("ballerinaVersion"),
                api_doc_url=(modules[0].get("apiDocURL") if modules else None),
                source_code_location=pkg.get("sourceCodeLocation"),
            ),
            license=License(
                spdx=licenses[0] if len(licenses) == 1 else ("NOASSERTION" if not licenses else " OR ".join(licenses)),
                basis="package_metadata" if licenses else "unknown",
                url=pkg.get("sourceCodeLocation"),
            ),
        )
