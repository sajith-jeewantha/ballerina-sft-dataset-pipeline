"""M7: codebase pool selection, states and their verification, context packing and attachment."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from balpipe_common.config import AnswersCfg, Settings
from balpipe_common.db import Database

from sftgen.codebases import attach, pool, stage
from sftgen.codebases.states import compile_error, failing_test, functions, make_states, unfinished_feature, \
    version_drift
from sftgen.validate.answer_check import AnswerChecker
from sftgen.validate.bal_compiler import BalCompiler, CompileOutcome

TOML = '[package]\norg = "demo"\nname = "albums"\nversion = "0.1.0"\ndistribution = "2201.12.0"\n'
SERVICE = """import ballerina/http;
import ballerina/log;

type Album record {|
    readonly string id;
    string title;
|};

table<Album> key(id) albums = table [];

service /albums on new http:Listener(9090) {
    resource function get .() returns Album[] {
        return albums.toArray();
    }

    resource function get [string id]() returns Album|http:NotFound {
        Album? album = albums[id];
        if album is () {
            return http:NOT_FOUND;
        }
        return album;
    }

    resource function post .(Album album) returns Album|error {
        check validateAlbum(album);
        albums.add(album);
        log:printInfo("added album", id = album.id);
        return album;
    }
}
"""
UTIL = """function validateAlbum(Album album) returns error? {
    if album.title.length() == 0 {
        return error("title is empty");
    }
    if album.id.length() > 10 {
        return error("id is too long");
    }
    return;
}
"""
TEST = """import ballerina/test;

@test:Config {}
function testValidate() {
    test:assertTrue(validateAlbum({id: "1", title: "Blue"}) is ());
}
"""
FILES = {"Ballerina.toml": TOML, "service.bal": SERVICE, "util.bal": UTIL, "tests/util_test.bal": TEST}


def test_function_scanner_and_states() -> None:
    names = [f["name"] for f in functions(SERVICE)]
    assert names == ["get .", "get [string id]", "post ."]
    assert [f["name"] for f in functions(UTIL)] == ["validateAlbum"]

    ce = compile_error("cb", FILES)
    assert ce and ce.expect == "fails_build" and "validateAlbmu" in ce.changed["service.bal"]
    uf = unfinished_feature("cb", FILES)
    assert uf and uf.expect == "builds" and 'panic error("not implemented");' in next(iter(uf.changed.values()))
    vd = version_drift("cb", FILES)
    new = vd.changed["service.bal"]
    assert 'basePath: "/albums"' in new and "service albums on new http:Listener(9090)" in new
    assert 'methods: ["GET"],' in new and 'path: "/{id}"' in new and "resource function get_string_id(" in new
    ft = failing_test("cb", FILES)
    assert ft and ft.expect == "fails_tests" and ft.target.startswith("util.bal::validateAlbum")
    assert [s.kind for s in make_states("cb", FILES, ["compile_error", "version_drift"])] == ["compile_error",
                                                                                              "version_drift"]
    assert failing_test("cb", {k: v for k, v in FILES.items() if not k.startswith("tests/")}) is None


def test_pack_puts_target_first_and_respects_budget() -> None:
    text, shown, left = attach.pack(FILES, "util.bal", max_tokens=100_000)
    assert shown == ["Ballerina.toml", "util.bal", "service.bal", "tests/util_test.bal"] and not left
    assert "```ballerina\n// file: util.bal\n" in text and "```toml\n# file: Ballerina.toml" in text
    text, shown, left = attach.pack(FILES, "util.bal", max_tokens=200)
    assert "util.bal" in shown and left and "not shown: " in text


class FakeCompiler:
    """Fails on a misspelled project function, on 1.x service syntax, or on code marked BROKEN."""

    def __init__(self) -> None:
        self.calls = 0

    def build_package(self, files: dict[str, str], *, test: bool = False, timeout_s: int | None = None
                      ) -> CompileOutcome:
        self.calls += 1
        code = {p: t for p, t in files.items() if p.endswith(".bal")}
        defined = {f["name"] for t in code.values() for f in functions(t)}
        for p, t in code.items():
            for d in defined:
                if re.fullmatch(r"\w{4,}", d) and d[-1] != d[-2] and (d[:-2] + d[-1] + d[-2]) + "(" in t:
                    return CompileOutcome("error", [{"file": p, "line": 1, "col": 1, "message": "undefined function"}])
            if "@http:ServiceConfig" in t or "BROKEN" in t:
                return CompileOutcome("error", [{"file": p, "line": 2, "col": 1, "message": "invalid syntax"}])
        return CompileOutcome("ok")

    def compile(self, code: str) -> CompileOutcome:
        return CompileOutcome("ok")

    def prewarm(self) -> None:
        pass


def _seed_repo(db: Database, root: str, files: dict[str, str], name: str = "ballerina-guides/samples",
               grp: str = "guides") -> None:
    repo = db.scalar("SELECT id FROM repos WHERE full_name = ?", (name,)) or db.insert(
        "repos", {"full_name": name, "grp": grp, "priority": 1, "url": "u", "status": "collected", "verdict": "safe",
                  "spdx": "Apache-2.0", "commit_sha": "abc"})
    for rel, text in files.items():
        kind = "toml" if rel.endswith(".toml") else "bal"
        db.insert("repo_files", {"repo_id": repo, "path": f"{root}/{rel}" if root else rel, "kind": kind,
                                 "bytes": len(text), "text": text, "project_root": root, "syntax": "swan_lake",
                                 "status": "kept"})


@pytest.fixture()
def seeded(settings: Settings, db: Database):  # noqa: ANN201
    (settings.repo_root / "graph").mkdir(exist_ok=True)
    (settings.repo_root / "graph" / "graph.json").write_text(json.dumps({"schema_version": "0.2", "nodes": [
        {"id": "lib.http", "title": "HTTP", "summary": "", "priority": "core", "modules": ["ballerina/http"]}]}))
    db.insert("graph_items", {"ref": "lib.http#not_found", "node_id": "lib.http", "item_id": "not_found",
                              "node_type": "library", "node_title": "HTTP", "priority": "core",
                              "text": "Return `http:NotFound` for a missing resource", "difficulty": "[]",
                              "own_sources": 0, "graph_version": "g", "status": "grounded"})
    _seed_repo(db, "albums", FILES)
    _seed_repo(db, "broken", {**FILES, "util.bal": UTIL.replace("return;", "BROKEN")})
    _seed_repo(db, "tiny", {"Ballerina.toml": TOML, "main.bal": "public function main() {}\n"})  # too small
    _seed_repo(db, "old", {"Ballerina.toml": "[project]\norg-name = \"x\"\n", "main.bal": SERVICE, "u.bal": UTIL})
    s = settings.model_copy(update={"codebases": settings.codebases.model_copy(update={
        "pool_size": 5, "min_tokens": 100, "eval_fraction": 0.0})})
    return s, db


def test_pool_selection_tags_and_split(seeded) -> None:  # noqa: ANN001
    s, db = seeded
    cands = pool.candidates(db, s.codebases)
    assert sorted(c.root for c in cands) == ["albums", "broken"]  # tiny is too small; [project] layout is 1.x
    c = next(c for c in cands if c.root == "albums")
    assert c.has_tests and c.modules == ["ballerina/http", "ballerina/log", "ballerina/test"] and set(c.files) == set(FILES)
    nodes, items = pool.tag(db, {"lib.http": {"modules": ["ballerina/http"]}}, c)
    assert nodes == ["lib.http"] and items == ["lib.http#not_found"]
    assert pool.split_for(c.id, s.codebases) == "train"
    assert pool.split_for(c.id, s.codebases.model_copy(update={"eval_fraction": 0.999})) == "eval"


def test_build_verify_attach_and_answer(seeded, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    s, db = seeded
    fake = FakeCompiler()
    monkeypatch.setattr(stage, "compiler", lambda s_: fake)
    with db.run("codebases-build", s.limits()) as run:
        out = stage.build(s, db, run)
    assert (out["added"], out["rejected"], out["active"]) == (1, 1, 1)
    states = {r["kind"]: r["verify_status"] for r in db.query("SELECT kind, verify_status FROM codebase_states")}
    assert states == {"compile_error": "pass", "unfinished_feature": "pass", "version_drift": "pass",
                      "failing_test": "skipped"}  # tests aren't run by default
    cid = db.scalar("SELECT id FROM codebases WHERE status = 'active'")
    manifest = [json.loads(x) for x in (s.codebases_dir / "manifest.jsonl").read_text().splitlines()]
    assert manifest[0]["id"] == cid and len(manifest[0]["states"]) == 4
    assert (s.codebases_dir / cid / "base.tar.gz").is_file()
    assert (s.codebases_dir / cid / "states" / "version_drift.patch").read_text().startswith("--- a/service.bal")

    with db.run("codebases-verify", s.limits()) as run:
        ver = stage.verify(s, db, run)
    assert ver["red"] == [] and ver["table"][0]["base"] == "pass"

    with db.run("codebases-attach", s.limits()) as run:
        att = attach.attach(s, db, run)
    assert att["added"] == 3 and att["states"] == 3  # the skipped failing_test state isn't used
    with db.run("codebases-attach", s.limits()) as run:
        assert attach.attach(s, db, run)["existing"] == 3
    p = db.one("SELECT * FROM prompts WHERE context_mode = 'codebase' AND task_type = 'write_code'")
    assert p["status"] == "accepted" and p["codebase_state_id"] and "// file: service.bal" in p["prompt"]
    assert "isn't implemented yet" in p["prompt"] and json.loads(p["covers"]) == ["lib.http#not_found"]

    # answers to codebase tasks: changed files laid over the state, then built
    from sftgen.generate.responses import Responder

    st = Responder.state(type("R", (), {"db": db})(), p)  # type: ignore[arg-type]
    checker = AnswerChecker(AnswersCfg(), fake)  # type: ignore[arg-type]
    target = st["target"]
    stub_file = target.split("::")[0]
    still = f"```ballerina\n// file: {stub_file}\n{st['files'][stub_file]}\n```"
    assert checker.check_codebase(still, st["files"], st["kind"], target, False).reason == "stub"
    fixed = f"Done:\n\n```ballerina\n// file: {stub_file}\n{FILES[stub_file]}\n```"
    assert checker.check_codebase(fixed, st["files"], st["kind"], target, False).status == "pass"
    assert checker.check_codebase("Just change it.", st["files"], st["kind"], target, False).reason == "missing_code"

    from sftgen.report import write_report

    path, summary_text = write_report(s, db)
    page = path.read_text(encoding="utf-8")
    assert "<h2>Codebase pool</h2>" in page and "Projects that did not build (1 shown)" in page
    assert "Codebases  1 active (0 eval) of 2 tried  |  states verified 3/4, codebase prompts 3" in summary_text

    # codebase prompts don't fill the standalone plan's slots
    from sftgen.plan.planner import plan

    plans = plan(s.model_copy(update={"prompts": s.prompts.model_copy(update={"nodes": ["lib.http"]})}), db)
    assert sum(p_.accepted.total() for p_ in plans) == 0


@pytest.mark.skipif(shutil.which("bal") is None, reason="needs the Ballerina toolchain")
def test_states_with_real_bal(tmp_path: Path) -> None:
    s = Settings()
    c = BalCompiler(s.validate_, tmp_path / "bal")
    base = {"Ballerina.toml": '[package]\norg = "demo"\nname = "calc"\nversion = "0.1.0"\n',
            "main.bal": "import ballerina/io;\n\npublic function main() {\n    io:println(total([1, 2, 3]));\n}\n",
            "calc.bal": "function total(int[] xs) returns int {\n    int sum = 0;\n    foreach int x in xs {\n"
                        "        sum = sum + x;\n    }\n    int count = xs.length();\n    if count == 0 {\n"
                        "        return 0;\n    }\n    return sum;\n}\n"}
    assert c.build_package(base).status == "ok"
    # tests are type-checked without running anything: module init and required configurables aren't touched
    marker = tmp_path / "ran.txt"
    side = {**base, "init.bal": f'import ballerina/io;\nconfigurable string needed = ?;\nfunction init() returns error? '
                                f'{{ check io:fileWriteString("{marker}", "ran"); }}\n',
            "tests/calc_test.bal": "import ballerina/test;\n@test:Config {}\nfunction t() { test:assertEquals("
                                   "total([2]), 99); }\n"}
    assert c.build_package(side).status == "ok" and not marker.exists()
    bad = {**side, "tests/calc_test.bal": side["tests/calc_test.bal"].replace("total([2])", "total(\"x\")")}
    res = c.build_package(bad)
    assert res.status == "error" and res.errors[0]["file"] == "tests/calc_test.bal" and not marker.exists()
    for st in make_states("calc", base, ["compile_error", "unfinished_feature"]):
        status, detail, _ = stage.verify_state(c, s, base, st)
        assert status == "pass", (st.kind, detail)


def test_state_variants_fall_back() -> None:
    """Each maker offers alternatives (`nth`) for when a variant doesn't verify; init is never stubbed."""
    first, second = compile_error("cb", FILES, 0), compile_error("cb", FILES, 1)
    assert first and second and first.description != second.description  # the second option: a removed import
    assert "import" in second.description and compile_error("cb", FILES, 9) is None
    assert version_drift("cb", FILES, 1) is None
    cls = ("class Store {\n    int n;\n    function init() {\n        self.n = 0;\n        int a = 1;\n        int b = 2;\n"
           "        self.n = a + b;\n    }\n}\n")
    assert unfinished_feature("cb", {"Ballerina.toml": TOML, "store.bal": cls}) is None
