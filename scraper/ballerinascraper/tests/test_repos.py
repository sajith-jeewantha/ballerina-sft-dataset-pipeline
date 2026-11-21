"""collect-repos: listing, licence gate, sparse snapshot, Swan Lake filter and dedup (local git repos)."""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from balpipe_common.config import RepoGroupCfg, Settings
from balpipe_common.db import Database
from balpipe_common.licences import LicenceRegister
from balpipe_common.lock import SourceLock

from ballerinascraper.repos import Project, classify_syntax, collect_repos, nearest_project, project_roots

APACHE = "Apache License\nVersion 2.0, January 2004\n"
SERVICE = """import ballerina/http;

service /orders on new http:Listener(9090) {
    resource function get [string id]() returns json|error {
        return {id, status: "shipped", items: [1, 2, 3], note: "order service for the shop demo"};
    }
}
"""
LEGACY = """import ballerina/http;

endpoint http:Listener listener { port: 9090 };

service<http:Service> hello bind listener {
    sayHello(endpoint caller, http:Request req) { }
}
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def make_repo(root: Path, name: str, files: dict[str, str]) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init")
    # let shallow, filtered fetches of a commit work against this local repo, like GitHub does
    for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
        _git(repo, "config", key, "true")
    return repo


class FakeApi:
    def __init__(self, metas: dict[str, list[dict]]) -> None:
        self.metas = metas

    def org_repos(self, org: str) -> list[dict]:
        return self.metas.get(org, [])

    def json(self, path: str) -> object:
        raise AssertionError(f"unexpected API call {path}")


def meta(owner: str, name: str, repo: Path | None, spdx: str | None, stars: int = 1) -> dict:
    return {"full_name": f"{owner}/{name}", "name": name, "default_branch": "main", "stargazers_count": stars,
            "size": 10, "fork": False, "archived": False, "pushed_at": "2025-01-01T00:00:00Z",
            "license": {"spdx_id": spdx} if spdx else None,
            "clone_url": f"file://{repo}" if repo else f"file:///nonexistent/{name}"}


@pytest.fixture()
def setup(tmp_path: Path):  # noqa: ANN201
    src = tmp_path / "remotes"
    toml = '[package]\norg = "demo"\nname = "shop"\nversion = "0.1.0"\ndistribution = "2201.12.0"\n'
    official = make_repo(src, "official", {
        "LICENSE": APACHE, "README.md": "# Shop", "shop/Ballerina.toml": toml, "shop/main.bal": SERVICE,
        "shop/tests/main_test.bal": "import ballerina/test;\n@test:Config {}\nfunction t() {}\n",
        "web/app.js": "console.log(1)",  # not in the sparse patterns: never downloaded
    })
    copycat = make_repo(src, "copycat", {
        "LICENSE": "MIT License\nPermission is hereby granted, free of charge, to any person",
        "svc/main.bal": "// Copied\n" + SERVICE,                           # exact copy (comment differs)
        "svc/near.bal": SERVICE.replace("shipped", "delivered"),          # near copy
        "old/hello.bal": LEGACY,                                          # Ballerina 1.x
    })
    nolicence = make_repo(src, "unlicensed", {"x.bal": SERVICE})
    mystery = make_repo(src, "mystery", {"LICENSE": "Copyright Me. All rights reserved.", "y.bal": SERVICE})
    s = Settings(repo_root=tmp_path / "proj", limit_repos=0)  # type: ignore[call-arg]
    s.repos.groups = [RepoGroupCfg(name="platform", org="p"), RepoGroupCfg(name="external", org="x")]
    s.repos.workers = 2
    api = FakeApi({
        "p": [meta("p", "official", official, "Apache-2.0")],
        "x": [meta("x", "copycat", copycat, "MIT"), meta("x", "unlicensed", nolicence, None),
              meta("x", "mystery", mystery, "NOASSERTION"), meta("x", "gone", None, "MIT")],
    })
    return s, api


def test_collect_gates_snapshots_and_dedups(setup) -> None:  # noqa: ANN001
    s, api = setup
    lock = SourceLock(s.path(s.sources.lock_file), s.sources.swan_lake)
    with Database(s.db_path) as db, db.run("collect-repos", s.limits()) as run:
        stats = collect_repos(s, db, run, api, lock)  # type: ignore[arg-type]
        status = {r["full_name"]: r["status"] for r in db.query("SELECT full_name, status FROM repos")}
        assert status == {"p/official": "collected", "x/copycat": "collected", "x/unlicensed": "excluded",
                          "x/mystery": "licence_check", "x/gone": "failed"}
        assert stats["dedup"] == {"bal_checked": 4, "duplicate": 1, "near_duplicate": 1, "kept": 2}

        files = {(r["full_name"], r["path"]): dict(r) for r in db.query(
            "SELECT r.full_name, f.* FROM repo_files f JOIN repos r ON r.id = f.repo_id")}
        assert ("p/official", "web/app.js") not in files
        main = files[("p/official", "shop/main.bal")]
        assert (main["status"], main["syntax"], main["project_root"], main["distribution"]) == \
               ("kept", "swan_lake", "shop", "2201.12.0")
        assert files[("x/copycat", "svc/main.bal")]["status"] == "duplicate"      # official copy wins
        assert files[("x/copycat", "svc/main.bal")]["dup_of"] == main["id"]
        assert files[("x/copycat", "svc/near.bal")]["status"] == "near_duplicate"
        assert files[("x/copycat", "old/hello.bal")]["status"] == "legacy"
        assert files[("p/official", "shop/Ballerina.toml")]["status"] == "context"

        # pinned in the lockfile, licence register updated, snapshot written
        assert SourceLock(lock.path, s.sources.swan_lake).get("repos", "p/official")["ref"] == "main"
        reg = LicenceRegister(s.path(s.repos.register_file))
        assert reg.get("p/official")["verdict"] == "safe" and reg.get("p/official")["licence_file_sha256"]
        assert reg.get("x/unlicensed") is None  # unlicensed search hit: database only, not the register
        assert reg.get("x/mystery")["verdict"] == "check"
        snap = db.scalar("SELECT snapshot FROM repos WHERE full_name = 'p/official'")
        with tarfile.open(s.data_root / snap) as tar:
            assert sorted(tar.getnames()) == ["LICENSE", "README.md", "shop/Ballerina.toml", "shop/main.bal",
                                              "shop/tests/main_test.bal"]

        # second run: nothing re-cloned; the failed repo is retried
        again = collect_repos(s, db, run, api, lock)  # type: ignore[arg-type]
        assert again["unchanged"] == 3 and again["cloned"] == 0 and again["failed"] == 1


def test_limit_caps_clones_per_run(setup) -> None:  # noqa: ANN001
    s, api = setup
    s.limit_repos = 1
    lock = SourceLock(s.path(s.sources.lock_file), s.sources.swan_lake)
    with Database(s.db_path) as db, db.run("collect-repos", s.limits()) as run:
        first = collect_repos(s, db, run, api, lock, groups=["platform"])  # type: ignore[arg-type]
        assert first["cloned"] == 1
        second = collect_repos(s, db, run, api, lock, groups=["external"])  # type: ignore[arg-type]
        assert second["cloned"] == 1  # unlicensed is excluded without a clone and doesn't count


def test_syntax_classification() -> None:
    swan = Project("2201.14.0", "swan_lake")
    assert classify_syntax(SERVICE, swan) == "swan_lake"
    assert classify_syntax(SERVICE, Project("slbeta6", "swan_lake")) == "legacy"
    assert classify_syntax(LEGACY, None) == "legacy"
    assert classify_syntax(SERVICE, None) == "swan_lake"
    assert classify_syntax("type T record {};", Project(None, "swan_lake")) == "swan_lake"  # [package], no distribution
    assert classify_syntax("type T record {};", Project(None, "legacy")) == "legacy"        # [project]: 1.x
    assert classify_syntax("function f() {}", None) == "unknown"
    roots = project_roots(["a/Ballerina.toml", "Ballerina.toml"],
                          {"a/Ballerina.toml": '[package]\ndistribution = "2201.1.0"', "Ballerina.toml": "[project]"})
    assert roots == {"a": Project("2201.1.0", "swan_lake"), "": Project(None, "legacy")}
    assert nearest_project("a/modules/x/y.bal", roots) == "a" and nearest_project("z.bal", roots) == ""
