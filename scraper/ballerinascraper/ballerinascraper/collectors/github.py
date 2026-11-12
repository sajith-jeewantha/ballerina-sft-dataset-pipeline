"""Collect files from allowlisted GitHub repos, pinned to a commit SHA.

Uses the REST API for repo metadata (license, default branch, commit SHA,
recursive tree with blob SHAs) and raw.githubusercontent.com for file bodies.
Files whose blob SHA was already collected are skipped without downloading.
"""

from __future__ import annotations

import re
from functools import lru_cache

from balpipe_common.config import GithubRepoCfg, Settings
from balpipe_common.schemas import GitInfo, License

from ballerinascraper.collectors.http import PoliteSession
from ballerinascraper.raw_sink import RawSink

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
MAX_FILE_BYTES = 1_000_000

MEDIA_TYPES = {
    ".md": "text/markdown",
    ".bal": "text/x-ballerina",
    ".metatags": "text/x-metatags",
    ".out": "text/plain",
    ".json": "application/json",
    ".toml": "application/toml",
    ".html": "text/html",
}


@lru_cache(maxsize=256)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Glob with `**` (any depth), `*` (one segment) and `?`, matched on POSIX paths."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pattern.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pattern[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif pattern[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(pattern[i]), i + 1
    return re.compile(f"^{out}$")


def path_selected(path: str, include: list[str], exclude: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in include) and not any(
        glob_to_regex(p).match(path) for p in exclude
    )


def media_type_for(path: str) -> str:
    for ext, mt in MEDIA_TYPES.items():
        if path.endswith(ext):
            return mt
    return "text/plain"


class GithubCollector:
    def __init__(self, settings: Settings, *, limit: int | None = None) -> None:
        self.settings = settings
        self.limit = limit
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token.get_secret_value()}"
        self.api = PoliteSession(
            settings.crawl.user_agent, delay_s=settings.crawl.collector_delay_s,
            timeout_s=settings.crawl.download_timeout_s, headers=headers,
        )
        # raw.githubusercontent.com must not receive the API token
        self.raw = PoliteSession(
            settings.crawl.user_agent, delay_s=settings.crawl.collector_delay_s,
            timeout_s=settings.crawl.download_timeout_s,
        )

    def _api_json(self, path: str) -> dict:
        resp = self.api.get(f"{API}{path}")
        resp.raise_for_status()
        return resp.json()

    def run(self) -> dict[str, int]:
        with RawSink(self.settings, "github", fetcher="github_api") as sink:
            for repo_cfg in self.settings.sources.github_repos:
                try:
                    self._collect_repo(sink, repo_cfg)
                except Exception as exc:  # one bad repo must not stop the others
                    sink.stage.error(repo_cfg.repo, exc)
                    sink.log.exception("repo failed", extra={"event": "github.repo_failed", "repo": repo_cfg.repo})
                if self.limit and sink.stage.counts["written"] >= self.limit:
                    break
            return dict(sink.stage.counts)

    def _collect_repo(self, sink: RawSink, cfg: GithubRepoCfg) -> None:
        meta = self._api_json(f"/repos/{cfg.repo}")
        ref = cfg.ref or meta["default_branch"]
        commit_sha = self._api_json(f"/repos/{cfg.repo}/commits/{ref}")["sha"]
        lic = meta.get("license") or {}
        spdx = lic.get("spdx_id") or "NOASSERTION"
        license_ = License(
            spdx="NOASSERTION" if spdx == "NOASSERTION" or spdx == "OTHER" else spdx,
            basis="repo_license" if lic else "unknown",
            url=f"https://github.com/{cfg.repo}/blob/{commit_sha}/LICENSE" if lic else None,
        )
        tree = self._api_json(f"/repos/{cfg.repo}/git/trees/{commit_sha}?recursive=1")
        if tree.get("truncated"):
            sink.log.warning("tree listing truncated", extra={"event": "github.tree_truncated", "repo": cfg.repo})
        files = [
            t for t in tree["tree"]
            if t["type"] == "blob" and path_selected(t["path"], cfg.include, cfg.exclude)
        ]
        sink.log.info(
            "repo listed",
            extra={"event": "github.repo_listed", "repo": cfg.repo, "commit": commit_sha, "files": len(files)},
        )
        for entry in files:
            if self.limit and sink.stage.counts["written"] >= self.limit:
                return
            path = entry["path"]
            if sink.blob_unchanged(cfg.repo, path, entry.get("sha")):
                sink.stage.count("unchanged")
                continue
            if entry.get("size", 0) > MAX_FILE_BYTES:
                sink.stage.count("skipped_too_large")
                continue
            url = f"{RAW}/{cfg.repo}/{commit_sha}/{path}"
            try:
                resp = self.raw.get(url)
                resp.raise_for_status()
            except Exception as exc:
                sink.stage.error(f"{cfg.repo}:{path}", exc)
                continue
            sink.emit(
                source_uri=f"https://github.com/{cfg.repo}/blob/{commit_sha}/{path}",
                identity_uri=f"https://github.com/{cfg.repo}/blob/{ref}/{path}",
                media_type=media_type_for(path),
                body=resp.content.decode("utf-8", errors="replace"),
                requested_url=url,
                final_url=resp.url,
                http_status=resp.status_code,
                content_type_header=resp.headers.get("Content-Type"),
                etag=resp.headers.get("ETag"),
                git=GitInfo(repo=cfg.repo, ref=ref, commit_sha=commit_sha, path=path, blob_sha=entry.get("sha")),
                license=license_,
            )
