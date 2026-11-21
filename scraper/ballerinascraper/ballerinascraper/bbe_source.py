"""Ballerina by Example sources from a git snapshot of `ballerina-distribution`.

The pinned tag is resolved with `git ls-remote` and `examples/` is fetched with a blobless,
shallow, sparse clone: no GitHub API quota is used, unlike reading each file through the
blobs API. The snapshot is cached per commit in `<data>/cache/bbe/`. Used by `scrape`
(BBE pages) and `build-eval` (the eval set).
"""

from __future__ import annotations

import hashlib
import io
import tarfile

from balpipe_common.config import Settings


def git_blob_sha(data: bytes) -> str:
    """The SHA git (and the GitHub API) gives this content, so version keys match the API's."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tag_commit(url: str, tag: str) -> str:
    from ballerinascraper.repos import _git

    out = _git("ls-remote", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}", timeout=120)
    refs = dict(reversed(line.split("\t", 1)) for line in out.splitlines() if "\t" in line)
    sha = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")  # annotated tags peel to the commit
    if not sha:
        raise RuntimeError(f"tag {tag} not found at {url}")
    return sha


def examples_snapshot(s: Settings, url: str, commit: str) -> dict[str, bytes]:
    """{path: bytes} under the examples folder at the commit; cached as a tarball."""
    cache = s.cache_dir / "bbe" / f"examples@{commit[:12]}.tar.gz"
    if cache.is_file():
        with tarfile.open(cache) as tar:
            return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]
    from ballerinascraper.repos import take_snapshot

    path = s.sources.github.bbe_path.strip("/")
    cfg = s.repos.model_copy(update={"sparse_patterns": [f"{path}/**"], "exclude_paths": [],
                                     "max_file_bytes": 2_000_000})
    snap = take_snapshot(url, commit, s.cache_dir / "bbe" / "work", cfg)
    cache.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache.with_name(f".{cache.name}.part")
    with tarfile.open(tmp, "w:gz") as tar:
        for rel, data in snap.files.items():
            ti = tarfile.TarInfo(rel)
            ti.size = len(data)
            tar.addfile(ti, io.BytesIO(data))
    tmp.replace(cache)
    return snap.files
