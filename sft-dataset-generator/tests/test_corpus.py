"""M9: raw corpus export and the commit miner."""

from __future__ import annotations

import gzip
import json
import subprocess
from pathlib import Path

from balpipe_common.config import Settings
from balpipe_common.db import Database
from balpipe_common.licences import LicenceRegister

from sftgen.corpus import miner, raw

CODE = "import ballerina/io;\n\npublic function main() {\n    io:println(\"hello from the corpus test file\");\n}\n"
EVAL = ("type Album record {| string id; string title; |};\n\nfunction findAlbum(Album[] albums, string id) returns "
        "Album? {\n    foreach Album a in albums {\n        if a.id == id {\n            return a;\n        }\n    }\n"
        "    return ();\n}\n")


def _repo(db: Database, name: str, grp: str = "platform", spdx: str = "Apache-2.0", url: str = "u") -> int:
    return db.insert("repos", {"full_name": name, "grp": grp, "priority": 0, "url": url, "status": "collected",
                               "verdict": "safe", "spdx": spdx, "commit_sha": "abc123", "stars": 5})


def test_corpus_export_filters_weights_and_registers(settings: Settings, db: Database) -> None:
    rid = _repo(db, "ballerina-platform/module-a")
    mit = _repo(db, "someone/app", grp="external", spdx="MIT")
    rows = [
        (rid, "a.bal", "kept", "swan_lake", 0, None, CODE),
        (rid, "gen.bal", "kept", "swan_lake", 1, None, CODE * 3),             # generated: weight 0.3
        (rid, "dup.bal", "duplicate", "swan_lake", 0, None, CODE),
        (rid, "old.bal", "kept", "legacy", 0, None, CODE),
        (rid, "ev.bal", "kept", "swan_lake", 0, None, EVAL),                 # copies an eval item
        (rid, "tiny.bal", "kept", "swan_lake", 0, None, "int x = 1;"),
        (mit, "svc/main.bal", "kept", "unknown", 0, None, CODE),
        (mit, "evalcb/main.bal", "kept", "swan_lake", 0, None, CODE),        # part of an eval codebase
    ]
    for repo_id, path, status, syntax, gen, ov, text in rows:
        db.insert("repo_files", {"repo_id": repo_id, "path": path, "kind": "bal", "bytes": len(text), "text": text,
                                 "syntax": syntax, "generated": gen, "eval_overlap": ov, "status": status})
    from sftgen.eval.tasks import fingerprint

    db.insert("eval_items", {"id": "bbe/albums", "suite": "bbe", "version": "v1", "name": "albums", "url": "x",
                             "files": "{}", "verify_mode": "none", "status": "active"})
    fingerprint(db, "bbe/albums", {"albums.bal": EVAL})
    db.insert("codebases", {"id": "app-evalcb-1", "full_name": "someone/app", "root": "evalcb",
                            "files": json.dumps({"main.bal": CODE}), "file_count": 1, "tokens": 20, "has_tests": 0,
                            "split": "eval", "status": "active"})
    with db.run("corpus", settings.limits()) as run:
        m = raw.export(settings, db, run)
    assert m["files"] == 3 and m["repos"] == 2
    assert m["left_out"] == {"duplicate": 1, "eval overlap": 1, "eval-split codebase": 1, "legacy 1.x syntax": 1,
                             "too small": 1}
    assert m["generated_files"] == 1 and m["weighted_tokens"] < m["tokens"]
    assert set(m["by_licence"]) == {"Apache-2.0", "MIT"}
    root = settings.corpus_dir
    recs = [json.loads(x) for p in sorted(root.glob("part-*.jsonl.gz")) for x in gzip.open(p, "rt")]
    gen = next(r for r in recs if r["path"] == "gen.bal")
    assert gen["weight"] == 0.3 and gen["generated"] and gen["url"].endswith("/abc123/gen.bal")
    assert "someone/app" in (root / "ATTRIBUTION.md").read_text() and (root / "manifest.json").is_file()
    reg = LicenceRegister(settings.path(settings.repos.register_file))
    assert reg.get("ballerina-platform/module-a")["corpus_files"] == "2"


def _git(cwd: Path, *args: str, author: str = "Dev <dev@example.com>") -> None:
    name, email = author[:-1].split(" <")
    subprocess.run(["git", "-c", f"user.name={name}", "-c", f"user.email={email}", *args], cwd=cwd, check=True,
                   capture_output=True)


def _history(root: Path) -> Path:
    repo = root / "lib"
    (repo / "ballerina" / "tests").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    src = repo / "ballerina" / "calc.bal"
    base = ("public function total(int[] xs) returns int {\n    int sum = 0;\n    foreach int i in 1 ..< xs.length() {"
            "\n        sum += xs[i];\n    }\n    return sum;\n}\n")
    src.write_text(base)
    (repo / "ballerina" / "tests" / "calc_test.bal").write_text("// tests\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Initial import")
    src.write_text(base.replace("1 ..< xs.length()", "0 ..< xs.length()"))
    (repo / "ballerina" / "tests" / "calc_test.bal").write_text("// tests for the first element\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Fix total skipping the first element")
    src.write_text(src.read_text() + "\npublic function avg(int[] xs) returns int => total(xs) / xs.length();\n")
    _git(repo, "commit", "-qam", "Fix missing avg function")                       # pure addition: not a fix
    src.write_text(src.read_text().replace("int sum = 0;", "int sum = 0; // v2"))
    _git(repo, "commit", "-qam", "Fix bump version to 2.0.0")                       # a bump
    src.write_text(src.read_text().replace("// v2", "// v3"))
    _git(repo, "commit", "-qam", "Fix formatting", author="ballerina-bot <bot@example.com>")
    for key in ("uploadpack.allowFilter", "uploadpack.allowAnySHA1InWant"):
        _git(repo, "config", key, "true")
    return repo


def test_miner_finds_real_fixes_only(settings: Settings, db: Database, tmp_path: Path) -> None:
    repo = _history(tmp_path)
    cs = miner.commits(repo, 50)
    assert [c["subject"] for c in cs][:2] == ["Fix formatting", "Fix bump version to 2.0.0"]
    assert [miner.qualifies(c, 3) for c in cs] == [None, None, ["ballerina/calc.bal"], ["ballerina/calc.bal"], None]
    _repo(db, "ballerina-platform/module-ballerina-calc", url=f"file://{repo}")
    s = settings.model_copy(update={"limit_repos": 0})
    with db.run("mine-fixes", s.limits()) as run:
        out = miner.mine(s, db, run)
    assert (out["saved"], out["qualified"], out["failed_repos"]) == (1, 2, 0)  # the pure addition is dropped
    fix = db.one("SELECT * FROM mined_fixes")
    files = json.loads(fix["files"])
    assert fix["message"] == "Fix total skipping the first element" and fix["touches_tests"] == 1
    assert "1 ..< xs" in files[0]["before"] and "0 ..< xs" in files[0]["after"] and fix["lines_changed"] == 2
    lines = (s.corpus_dir / "fixes.jsonl").read_text().splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["diff"].startswith("--- a/ballerina/calc.bal")
    reg = LicenceRegister(s.path(s.repos.register_file))
    assert reg.get("ballerina-platform/module-ballerina-calc")["mined_fixes"] == "1"
    from sftgen.report import write_report

    path, summary_text = write_report(s, db)
    assert "<h2>Raw corpus and mined fixes</h2>" in path.read_text(encoding="utf-8")
    assert "mined fixes 1 from 1 repos" in summary_text

    # a second run skips repos already mined
    with db.run("mine-fixes", s.limits()) as run:
        assert miner.mine(s, db, run)["repos"] == 0
