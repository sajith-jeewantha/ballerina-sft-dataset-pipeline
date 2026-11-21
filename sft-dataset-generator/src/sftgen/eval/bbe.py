"""`build-eval`: eval set v1 from Ballerina by Example, and the decontamination fingerprints.

1. **Pin** `ballerina-distribution` at the release tag of the pinned Swan Lake update
   (`v2201.14.1`), resolved to a commit with `git ls-remote` and recorded under `bbe` in
   `config/sources.lock.json`.
2. **Snapshot** `examples/` with a blobless, shallow, sparse fetch (cached in
   `<data>/cache/bbe/`). `examples/index.json` names every example with its title,
   category and whether upstream CI checks its build and output.
3. **Split** 30% eval / 70% train **by example**: sha256 of `bbe-split:<name>` decides the
   side, so an example never changes side when others are added or removed.
4. **Tag** each eval item with the graph items (`node#item`) whose own sources are the
   example, and the nodes that link it as a node-level source.
5. **Fingerprint** each eval `.bal` file: exact hash, MinHash, and the 5-token shingles not
   shared by `eval.common_df` or more BBE examples (table `fingerprints`).
6. **Verify** each eval item with the pinned toolchain: `bal run` against its `.out`
   transcript, else `bal build` (see `verify.py`).
7. **Write** `<data>/eval/<version>/`: `bbe.jsonl` (the items), `decontam.jsonl` (the
   fingerprints) and `split.json` (both sides of the split and the rule).
8. **Decontaminate** the database (`balpipe_common.decontam.apply_decontam`).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import tarfile
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from balpipe_common.config import Settings
from balpipe_common.db import Database, RunTracker
from balpipe_common.decontam import Decontaminator, apply_decontam, pack_shingles
from balpipe_common.dedup import MinHasher, code_hash, shingle_hashes
from balpipe_common.ids import sha256_text, utc_now_iso
from balpipe_common.lock import SourceLock

from sftgen.eval.verify import Verifier, verify_mode

log = logging.getLogger(__name__)

BBE_PAGE = "https://ballerina.io/learn/by-example/{name}/"
TEXT_EXTS = (".bal", ".out", ".md", ".toml", ".proto", ".graphql", ".json", ".yaml", ".yml", ".txt", ".metatags",
             ".sql", ".csv", ".xml", ".html", ".conf", ".properties")


@dataclass
class Example:
    name: str
    title: str
    category: str
    verify_build: bool
    verify_output: bool
    need_central: bool
    files: dict[str, str] = field(default_factory=dict)   # top-level files of examples/<name>/

    @property
    def url(self) -> str:
        return BBE_PAGE.format(name=self.name)

    @property
    def bal(self) -> dict[str, str]:
        return {k: v for k, v in self.files.items() if k.endswith(".bal")}


def split_for(name: str, fraction: float, salt: str = "bbe-split") -> str:
    """'eval' or 'train' for a BBE example; depends on its name only."""
    bucket = int(sha256_text(f"{salt}:{name}")[:8], 16) / 0x1_0000_0000
    return "eval" if bucket < fraction else "train"


# --------------------------------------------------------------------------- #
# Pin and snapshot
# --------------------------------------------------------------------------- #
def tag_commit(url: str, tag: str) -> str:
    from ballerinascraper.repos import _git

    out = _git("ls-remote", url, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}", timeout=120)
    refs = dict(reversed(line.split("\t", 1)) for line in out.splitlines() if "\t" in line)
    sha = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")  # annotated tags peel to the commit
    if not sha:
        raise RuntimeError(f"tag {tag} not found at {url}")
    return sha


def pinned_commit(lock: SourceLock, repo: str, url: str, tag: str, *, update: bool) -> str:
    locked = lock.get("bbe", repo)
    if locked and locked.get("ref") == tag and locked.get("commit") and not update:
        return str(locked["commit"])
    commit = tag_commit(url, tag)
    lock.set("bbe", repo, {"ref": tag, "commit": commit})
    return commit


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


def parse_examples(files: dict[str, bytes], root: str) -> tuple[list[Example], list[str]]:
    """Examples listed in index.json (with their top-level text files) and folders it doesn't list."""
    root = root.strip("/")
    index = json.loads(files[f"{root}/index.json"].decode("utf-8"))
    by_dir: dict[str, dict[str, str]] = defaultdict(dict)
    for path, data in files.items():
        parts = path.split("/")
        if len(parts) == 3 and parts[0] == root and parts[2].endswith(TEXT_EXTS):
            by_dir[parts[1]][parts[2]] = data.decode("utf-8", errors="replace")
    examples: dict[str, Example] = {}
    for cat in index:
        for sample in cat.get("samples", []):
            name = sample["url"]
            if name in examples or name not in by_dir:
                continue
            examples[name] = Example(
                name=name, title=sample.get("name") or name, category=cat.get("category") or cat.get("title", ""),
                verify_build=bool(sample.get("verifyBuild")), verify_output=bool(sample.get("verifyOutput")),
                need_central=bool(sample.get("needCentral")), files=dict(sorted(by_dir[name].items())))
    unlisted = sorted(set(by_dir) - set(examples))
    return sorted(examples.values(), key=lambda e: e.name), unlisted


# --------------------------------------------------------------------------- #
# Graph tags and fingerprints
# --------------------------------------------------------------------------- #
def graph_tags(s: Settings) -> tuple[dict[str, list[str]], dict[str, list[str]], str | None]:
    """BBE example name -> item refs (own sources) and -> node ids (node-level sources)."""
    if not s.path(s.graph.path).is_file():
        return {}, {}, None
    from balpipe_common.graph import load_graph_json

    from sftgen.graph.coverage import source_key

    lg = load_graph_json(s.path(s.graph.path), s.path(s.graph.source_file), strict=False)
    g = lg.graph
    pat = re.compile(r"^https://ballerina\.io/learn/by-example/([^/]+)/$")

    def names(sources: Any) -> set[str]:
        out = set()
        for src in sources or []:
            key = source_key(src.url)[0]
            m = pat.match(key or "")
            if m:
                out.add(m.group(1))
        return out

    items: dict[str, set[str]] = defaultdict(set)
    nodes: dict[str, set[str]] = defaultdict(set)
    for node, item in g.iter_items():
        for n in names(item.sources):
            items[n].add(g.ref(node, item))
    for node in g.nodes.values():
        for n in names(node.sources):
            nodes[n].add(node.id)
    return ({k: sorted(v) for k, v in items.items()}, {k: sorted(v) for k, v in nodes.items()}, lg.version)


def common_shingles(examples: list[Example], min_df: int) -> set[int]:
    df: Counter[int] = Counter()
    for ex in examples:
        seen: set[int] = set()
        for code in ex.bal.values():
            seen |= shingle_hashes(code)
        df.update(seen)
    return {h for h, n in df.items() if n >= min_df}


def fingerprint_rows(ex: Example, item_id: str, common: set[int], hasher: MinHasher) -> list[dict[str, Any]]:
    rows = []
    for path, code in ex.bal.items():
        if not code.strip():
            continue
        rows.append({"item_id": item_id, "path": path, "code_hash": code_hash(code),
                     "minhash": hasher.to_bytes(hasher.signature(code)),
                     "shingles": pack_shingles(shingle_hashes(code) - common)})
    return rows


# --------------------------------------------------------------------------- #
# The command
# --------------------------------------------------------------------------- #
def build_eval(s: Settings, db: Database, run: RunTracker, lock: SourceLock, *, update_lock: bool = False,
               verify: bool | None = None, reverify: bool = False) -> dict[str, Any]:
    cfg = s.eval_
    gh = s.sources.github
    repo = f"{gh.org}/{gh.bbe_repo}"
    url = f"https://github.com/{repo}.git"
    tag = gh.bbe_ref.format(swan_lake=lock.swan_lake)
    try:
        commit = pinned_commit(lock, repo, url, tag, update=update_lock)
    finally:
        lock.save()
    files = examples_snapshot(s, url, commit)
    examples, unlisted = parse_examples(files, gh.bbe_path)
    if not examples:
        raise RuntimeError(f"no examples found in {repo}@{tag}/{gh.bbe_path}")

    split = {ex.name: split_for(ex.name, cfg.bbe_eval_fraction, cfg.split_salt) for ex in examples}
    evals = [ex for ex in examples if split[ex.name] == "eval"]
    covers, nodes, graph_version = graph_tags(s)
    common = common_shingles(examples, cfg.common_df)
    hasher = MinHasher()
    now = utc_now_iso()
    source_base = f"https://github.com/{repo}/tree/{tag}/{gh.bbe_path.strip('/')}"

    existing = {r["id"]: dict(r) for r in db.query("SELECT * FROM eval_items WHERE suite = 'bbe'")}
    active_ids = set()
    for ex in evals:
        item_id = f"bbe/{ex.name}"
        active_ids.add(item_id)
        files_json = json.dumps(ex.files, ensure_ascii=False, sort_keys=True)
        mode = verify_mode(ex.files, ex.verify_output, ex.verify_build)
        values = {"suite": "bbe", "version": cfg.version, "name": ex.name, "title": ex.title,
                  "category": ex.category, "url": ex.url, "source_url": f"{source_base}/{ex.name}",
                  "commit_sha": commit, "files": files_json, "covers": json.dumps(covers.get(ex.name, [])),
                  "nodes": json.dumps(nodes.get(ex.name, [])), "verify_mode": mode, "status": "active",
                  "updated_at": now}
        old = existing.get(item_id)
        if old is None:
            db.conn.execute(f"INSERT INTO eval_items (id, {', '.join(values)}, created_at) "
                            f"VALUES (?, {', '.join('?' for _ in values)}, ?)", (item_id, *values.values(), now))
        else:
            if old["files"] != files_json or old["verify_mode"] != mode:
                values.update({"verify_status": None, "verify_detail": None})
            db.conn.execute(f"UPDATE eval_items SET {', '.join(f'{k} = ?' for k in values)} WHERE id = ?",
                            (*values.values(), item_id))
        db.conn.execute("DELETE FROM fingerprints WHERE item_id = ?", (item_id,))
        for fp in fingerprint_rows(ex, item_id, common, hasher):
            db.conn.execute("INSERT INTO fingerprints (item_id, path, code_hash, minhash, shingles, created_at, "
                            "updated_at) VALUES (?,?,?,?,?,?,?)",
                            (fp["item_id"], fp["path"], fp["code_hash"], fp["minhash"], fp["shingles"], now, now))
    retired = [i for i in existing if i not in active_ids and existing[i]["status"] == "active"]
    for item_id in retired:
        db.conn.execute("UPDATE eval_items SET status = 'retired', updated_at = ? WHERE id = ?", (now, item_id))
        db.conn.execute("DELETE FROM fingerprints WHERE item_id = ?", (item_id,))
    db.conn.commit()

    verified = _verify(s, db, reverify) if (cfg.verify if verify is None else verify) else {"skipped": "--no-verify"}
    written = write_outputs(s, db, examples, split, unlisted, repo, tag, commit, graph_version)
    decontam = apply_decontam(db, cfg, Decontaminator.from_db(db, cfg))

    by_cat: dict[str, Counter[str]] = defaultdict(Counter)
    for ex in examples:
        by_cat[ex.category][split[ex.name]] += 1
    stats = {
        "pinned": f"{repo}@{tag} ({commit[:12]})",
        "examples": len(examples), "eval": len(evals), "train": len(examples) - len(evals),
        "unlisted_folders": len(unlisted), "retired": len(retired),
        "eval_with_covers": sum(1 for ex in evals if covers.get(ex.name)),
        "fingerprints": int(db.scalar("SELECT COUNT(*) FROM fingerprints") or 0),
        "verify": verified, "decontam": decontam, "files": written,
        "by_category": {k: dict(v) for k, v in sorted(by_cat.items())},
    }
    run.stats.update({"eval_items": len(evals), "fingerprints": stats["fingerprints"],
                      **{k: v for k, v in decontam.items() if k.endswith("_overlap")}})
    return stats


def _verify(s: Settings, db: Database, reverify: bool) -> dict[str, int]:
    cond = "" if reverify else "AND verify_status IS NULL"
    rows = db.query(f"SELECT id, files, verify_mode FROM eval_items WHERE status = 'active' {cond} ORDER BY id")
    if rows:
        verifier = Verifier(s.validate_, s.cache_dir / "bal-eval", s.eval_.run_timeout_s)
        log.info("verifying %d eval item(s) with bal %s", len(rows), verifier.version)

        def one(r: Any) -> tuple[str, Any]:
            return r["id"], verifier.verify(json.loads(r["files"]), r["verify_mode"])

        with ThreadPoolExecutor(max_workers=max(1, s.eval_.workers)) as pool:
            for item_id, res in pool.map(one, rows):
                db.update("eval_items", item_id, {"verify_status": res.status, "verify_detail": res.detail})
    return {r[0]: r[1] for r in db.query("SELECT COALESCE(verify_status, 'not_checked'), COUNT(*) FROM eval_items "
                                         "WHERE status = 'active' GROUP BY 1 ORDER BY 2 DESC")}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=path.suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def write_outputs(s: Settings, db: Database, examples: list[Example], split: dict[str, str], unlisted: list[str],
                  repo: str, tag: str, commit: str, graph_version: str | None) -> dict[str, str]:
    out = s.eval_dir
    items = []
    for r in db.query("SELECT * FROM eval_items WHERE status = 'active' AND suite = 'bbe' ORDER BY id"):
        files = json.loads(r["files"])
        items.append({
            "id": r["id"], "suite": "bbe", "version": r["version"], "name": r["name"], "title": r["title"],
            "category": r["category"], "url": r["url"], "source_url": r["source_url"], "commit": r["commit_sha"],
            "covers": json.loads(r["covers"]), "nodes": json.loads(r["nodes"]),
            "explanation": next((files[f] for f in files if f.endswith(".md")), None),
            "code": {f: t for f, t in files.items() if f.endswith(".bal")},
            "outputs": {f: t for f, t in files.items() if f.endswith(".out")},  # `$ command` transcripts
            "other_files": {f: t for f, t in files.items() if not f.endswith((".bal", ".md", ".metatags", ".out"))},
            "verify": {"mode": r["verify_mode"], "status": r["verify_status"], "detail": r["verify_detail"]},
        })
    fps = [{"item_id": r["item_id"], "path": r["path"], "code_hash": r["code_hash"], "minhash": r["minhash"].hex(),
            "shingles": r["shingles"].hex()}
           for r in db.query("SELECT f.* FROM fingerprints f JOIN eval_items e ON e.id = f.item_id "
                             "WHERE e.status = 'active' ORDER BY f.item_id, f.path")]
    _write_jsonl(out / "bbe.jsonl", items)
    _write_jsonl(out / "decontam.jsonl", fps)
    meta = {
        "version": s.eval_.version, "source": f"{repo}@{tag}", "commit": commit, "graph_version": graph_version,
        "rule": f"sha256('{s.eval_.split_salt}:' + example name)[:8] / 2^32 < {s.eval_.bbe_eval_fraction} -> eval",
        "fingerprints": {"shingle": 5, "minhash_perm": 128, "common_df": s.eval_.common_df,
                         "near_duplicate_threshold": s.eval_.near_duplicate_threshold,
                         "containment": s.eval_.containment, "min_shingles": s.eval_.min_shingles},
        "eval": sorted(n for n, v in split.items() if v == "eval"),
        "train": sorted(n for n, v in split.items() if v == "train"),
        "unlisted_folders": unlisted,
    }
    (out / "split.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    rel = lambda p: str(p.relative_to(s.data_root))  # noqa: E731
    return {"items": rel(out / "bbe.jsonl"), "fingerprints": rel(out / "decontam.jsonl"),
            "split": rel(out / "split.json")}
