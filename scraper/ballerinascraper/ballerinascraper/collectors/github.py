"""GitHub REST API collectors (authenticated with GITHUB_TOKEN when set).

* `BbeCollector`: Ballerina by Example. The page list comes from the
  ballerina.io sitemap; each example's sources (`.md` explanation, `.bal`
  code, `.out` output) come from `ballerina-distribution/examples/<name>/` at
  the release tag of the pinned Swan Lake update (e.g. `v2201.14.1`), so code
  is kept exactly as written, with its explanation. If an example has no
  folder there, the ballerina.io page itself is used (code only).
* `ModuleCollector`: READMEs and `.bal` API source files from the org's
  `module-ballerina-*` / `module-ballerinax-*` repos, most-starred first, each
  pinned to the commit recorded in `config/sources.lock.json`.

File bodies are read through the blobs API; a file whose blob SHA has not
changed since the last run is never downloaded again.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Iterator
from functools import lru_cache
from pathlib import PurePosixPath

from balpipe_common.config import GithubCfg
from balpipe_common.ids import short_hash
from balpipe_common.lock import SourceLock

from ballerinascraper.collectors.ballerina_io import BBE_RE, Sitemap, fetch_page
from ballerinascraper.collectors.base import Candidate, Fetched
from ballerinascraper.collectors.http import PoliteSession

API = "https://api.github.com"
BBE_FILE_EXTS = (".md", ".bal", ".out", ".metatags")


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


class GithubApi:
    def __init__(self, http: PoliteSession, ttl_hours: float) -> None:
        self.http = http
        self.ttl_hours = ttl_hours

    def json(self, path: str, *, cached: bool = True) -> object:
        url = f"{API}{path}"
        resp = self.http.get_cached(url, ttl_hours=self.ttl_hours) if cached else self.http.get(url)
        return resp.json()

    def commit_sha(self, repo: str, ref: str) -> str:
        """Resolve a branch or tag to its commit SHA."""
        data = self.json(f"/repos/{repo}/commits/{ref}", cached=False)
        assert isinstance(data, dict) and data.get("sha"), f"cannot resolve {repo}@{ref}"
        return str(data["sha"])

    def pinned_commit(self, lock: SourceLock, section: str, repo: str, ref: str, *, update: bool) -> str:
        """The locked commit for repo@ref; resolved and recorded the first time (or on update)."""
        locked = lock.get(section, repo)
        if locked and locked.get("ref") == ref and locked.get("commit") and not update:
            return str(locked["commit"])
        commit = self.commit_sha(repo, ref)
        lock.set(section, repo, {"ref": ref, "commit": commit})
        return commit

    def tree(self, repo: str, ref: str) -> list[dict]:
        data = self.json(f"/repos/{repo}/git/trees/{ref}?recursive=1")
        assert isinstance(data, dict)
        return [t for t in data.get("tree", []) if t.get("type") == "blob"]

    def blob_text(self, repo: str, sha: str) -> str:
        data = self.json(f"/repos/{repo}/git/blobs/{sha}", cached=False)
        assert isinstance(data, dict)
        return base64.b64decode(data.get("content") or "").decode("utf-8", errors="replace")

    def org_repos(self, org: str) -> list[dict]:
        repos: list[dict] = []
        page = 1
        while True:
            batch = self.json(f"/orgs/{org}/repos?per_page=100&type=public&page={page}")
            if not isinstance(batch, list) or not batch:
                return repos
            repos.extend(batch)
            if len(batch) < 100:
                return repos
            page += 1


def repo_license(repo: dict) -> str | None:
    spdx = (repo.get("license") or {}).get("spdx_id")
    return None if spdx in (None, "NOASSERTION", "OTHER") else spdx


# --------------------------------------------------------------------------- #
class BbeCollector:
    source = "bbe"

    def __init__(self, api: GithubApi, cfg: GithubCfg, sitemap: Sitemap, web: PoliteSession,
                 lock: SourceLock, *, update_lock: bool = False) -> None:
        self.api = api
        self.cfg = cfg
        self.sitemap = sitemap
        self.web = web
        self.lock = lock
        self.update_lock = update_lock

    def candidates(self) -> Iterator[Candidate]:
        repo = f"{self.cfg.org}/{self.cfg.bbe_repo}"
        meta = self.api.json(f"/repos/{repo}")
        assert isinstance(meta, dict)
        ref = self.cfg.bbe_ref.format(swan_lake=self.lock.swan_lake)
        commit = self.api.pinned_commit(self.lock, "bbe", repo, ref, update=self.update_lock)
        lic = repo_license(meta)
        folders: dict[str, dict[str, str]] = {}  # name -> {file name: blob sha}
        prefix = self.cfg.bbe_path.rstrip("/") + "/"
        for entry in self.api.tree(repo, commit):
            parts = entry["path"].removeprefix(prefix).split("/") if entry["path"].startswith(prefix) else []
            if len(parts) == 2 and parts[1].endswith(BBE_FILE_EXTS):
                folders.setdefault(parts[0], {})[parts[1]] = entry["sha"]

        for url in self.sitemap.urls("bbe"):
            m = BBE_RE.match("/" + url.split("/", 3)[3])
            name = m.group(1) if m else ""
            files = folders.get(name)
            if not files or not any(f.endswith(".md") for f in files):
                yield Candidate(self.source, url, fetch=lambda row, u=url: self._fallback(u, row),
                                meta={"origin": "ballerina.io", "name": name})
                continue
            version = short_hash(*(f"{f}:{s}" for f, s in sorted(files.items())))
            yield Candidate(
                self.source, url, version_key=version, license=lic,
                fetch=lambda row, n=name, fs=files: self._bundle(repo, ref, n, fs),
                meta={"origin": "github", "repo": repo, "folder": f"{prefix}{name}", "ref": ref, "commit": commit},
            )

    def _bundle(self, repo: str, branch: str, name: str, files: dict[str, str]) -> Fetched:
        bundle = {fname: self.api.blob_text(repo, sha) for fname, sha in sorted(files.items())}
        return Fetched(json.dumps(bundle, ensure_ascii=False, sort_keys=True), 200,
                       f"https://github.com/{repo}/tree/{branch}/{self.cfg.bbe_path}/{name}")

    def _fallback(self, url: str, row: object) -> Fetched:
        return fetch_page(self.web, url, row)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
class ModuleCollector:
    source = "github"

    def __init__(self, api: GithubApi, cfg: GithubCfg, lock: SourceLock, *, update_lock: bool = False) -> None:
        self.api = api
        self.cfg = cfg
        self.lock = lock
        self.update_lock = update_lock

    def repos(self) -> list[dict]:
        pats = [re.compile(p) for p in self.cfg.module_repo_patterns]
        repos = [r for r in self.api.org_repos(self.cfg.org)
                 if any(p.search(r["name"]) for p in pats) and not r.get("archived") and not r.get("fork")]
        return sorted(repos, key=lambda r: (-int(r.get("stargazers_count") or 0), r["name"]))

    def candidates(self) -> Iterator[Candidate]:
        for repo in self.repos():
            full, branch, lic = repo["full_name"], repo["default_branch"], repo_license(repo)
            commit = self.api.pinned_commit(self.lock, "repos", full, branch, update=self.update_lock)
            for entry in self.api.tree(full, commit):
                path = entry["path"]
                if not path_selected(path, self.cfg.module_include, self.cfg.module_exclude):
                    continue
                if int(entry.get("size") or 0) > self.cfg.max_file_bytes:
                    continue
                url = f"https://github.com/{full}/blob/{branch}/{path}"
                yield Candidate(
                    self.source, url, version_key=entry["sha"], license=lic,
                    title=f"{repo['name']}: {PurePosixPath(path).name}",
                    fetch=lambda row, f=full, s=entry["sha"], u=url: Fetched(self.api.blob_text(f, s), 200, u),
                    meta={"repo": full, "path": path, "commit": commit},
                )
