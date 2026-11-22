"""M6: answer contract checks, the repair loop, the judge, preference pairs and outputs."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from balpipe_common.config import AnswersCfg, ConfigError, Settings
from balpipe_common.db import Database

from sftgen.generate import responses as gr
from sftgen.generate.llm import Usage
from sftgen.validate.answer_check import AnswerChecker, code_blocks
from sftgen.validate.bal_compiler import BalCompiler, CompileOutcome

GOOD = "```ballerina\nimport ballerina/io;\n\npublic function main() {\n    io:println(\"ok\");\n}\n```"
BAD = "```ballerina\npublic function main() {\n    int x = \"BAD\";\n}\n```"


class FakeCompiler:
    """Code containing BAD fails with an error at line 2."""

    def __init__(self) -> None:
        self.packages: list[dict[str, str]] = []

    def compile(self, code: str) -> CompileOutcome:
        if "BAD" in code:
            return CompileOutcome("error", [{"line": 2, "col": 13, "message": "incompatible types: expected 'int', "
                                                                             "found 'string'"}])
        return CompileOutcome("ok")

    def build_package(self, files: dict[str, str], *, test: bool = False, timeout_s: int | None = None
                      ) -> CompileOutcome:
        self.packages.append(files)
        bad = [f for f, t in files.items() if "BAD" in t]
        if bad:
            return CompileOutcome("error", [{"file": bad[0], "line": 3, "col": 1, "message": "undefined symbol"}])
        return CompileOutcome("ok")


def test_code_blocks_and_checker() -> None:
    answer = ("Two files:\n\n```ballerina\n// file: main.bal\npublic function main() {}\n```\n\n"
              "```ballerina\n// file: tests/main_test.bal\nimport ballerina/test;\n```\n\nRun it:\n```bash\nbal run\n```")
    blocks = code_blocks(answer)
    assert [b.file for b in blocks] == ["main.bal", "tests/main_test.bal"]
    fc = FakeCompiler()
    chk = AnswerChecker(AnswersCfg(), fc)  # type: ignore[arg-type]
    res = chk.check(answer, "write_code")
    assert res.status == "pass" and res.package_files == ["main.bal", "tests/main_test.bal"]
    assert set(fc.packages[0]) == {"main.bal", "tests/main_test.bal"}
    assert chk.check("Just prose.", "write_code").reason == "missing_code"
    assert chk.check("Just prose.", "explain").status == "no_code"
    bad = chk.check(f"Fixed:\n\n{BAD}", "fix_code")
    assert bad.status == "fail" and bad.reason == "compile"
    fb = bad.feedback(8)
    assert "does not compile" in fb and "code block 1:2:13: incompatible types" in fb


@pytest.mark.skipif(shutil.which("bal") is None, reason="needs the Ballerina toolchain")
def test_checker_with_real_bal(tmp_path: Path) -> None:
    compiler = BalCompiler(Settings().validate_, tmp_path / "bal")
    chk = AnswerChecker(AnswersCfg(), compiler)
    assert chk.check(GOOD, "write_code").status == "pass"
    two = ("```ballerina\n// file: main.bal\nimport ballerina/io;\npublic function main() { io:println(add(1, 2)); }\n"
           "```\n\n```ballerina\n// file: util.bal\nfunction add(int a, int b) returns int => a + b;\n```\n\n"
           "```ballerina\n// file: tests/util_test.bal\nimport ballerina/test;\n@test:Config {}\n"
           "function t() { test:assertEquals(add(2, 2), 5); }\n```")
    assert chk.check(two, "write_code").status == "pass"  # the failing test is compiled, not run
    ran = AnswerChecker(AnswersCfg(run_tests=True), compiler).check(two, "write_code")
    assert (ran.status, ran.reason, ran.verification) == ("fail", "test", "test")
    assert "expected: '5'" in ran.feedback(8)
    broken = chk.check(two.replace("a + b", "a + \"b\""), "write_code")
    assert broken.reason == "compile" and broken.errors[0]["file"] == "util.bal"


class FakeClient:
    """Teacher: answers per prompt by script; the judge passes prose unless it says WRONG."""

    def __init__(self, script: dict[str, list[str]]) -> None:
        self.script = script
        self.calls: Counter[str] = Counter()
        self.feedback: list[str] = []

    async def chat(self, model, prompt, **fmt):  # noqa: ANN001, ANN201
        key = fmt["request"].split(":", 1)[0]
        n = self.calls[key]
        self.calls[key] += 1
        if prompt.id == "prompt_answer_repair":
            self.feedback.append(fmt["feedback"])
        answers = self.script[key]
        return answers[min(n, len(answers) - 1)], Usage(model, 1000, 300, 0.0005)

    async def chat_json(self, model, prompt, schema, **fmt):  # noqa: ANN001, ANN201
        ok = "WRONG" not in fmt["answer"]
        return schema.model_validate({"correct": 5 if ok else 2, "complete": 4, "clear": 4,
                                      "verdict": "pass" if ok else "fail",
                                      "issues": [] if ok else ["http:Client has no `fetch` method"]}), \
            Usage(model, 800, 80, 0.0002)


def _seed(s: Settings, db: Database) -> None:
    (s.repo_root / "graph").mkdir(exist_ok=True)
    (s.repo_root / "graph" / "graph.json").write_text(json.dumps({"schema_version": "0.2", "nodes": [
        {"id": "lib.http", "title": "HTTP", "summary": "", "priority": "core", "modules": ["ballerina/http"]}]}))
    db.insert("graph_items", {"ref": "lib.http#a", "node_id": "lib.http", "item_id": "a", "node_type": "library",
                              "node_title": "HTTP", "priority": "core", "text": "Use http:Client",
                              "difficulty": "[]", "own_sources": 0, "graph_version": "g", "status": "grounded"})
    for i, (key, task) in enumerate((("CODE", "write_code"), ("PROSE", "explain"), ("NOCODE", "fix_code"),
                                     ("LEAK", "use_api"), ("WRONGPROSE", "design"))):
        db.insert("prompts", {"set_name": "pilot", "ref": "lib.http#a", "node_id": "lib.http", "task_type": task,
                              "difficulty": "intermediate", "prompt": f"{key}: please help {i}",
                              "prompt_hash": str(i), "covers": '["lib.http#a"]', "status": "accepted"})


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"answer_gen_model": "deepseek/deepseek-v4.1-flash",
                                       "judge_model": "qwen/qwen3.8-flash", "limit_generating": 0})


def test_answer_loop_repairs_judges_and_pairs(settings: Settings, db: Database) -> None:
    s = _settings(settings)
    _seed(s, db)
    client = FakeClient({
        "CODE": [f"Here you go:\n\n{BAD}", f"Fixed:\n\n{GOOD}"],                 # repaired after a compile error
        "PROSE": ["Ballerina services are values attached to listeners."],       # judged
        "NOCODE": ["Change the type of x to string."],                          # never gives code: fails
        "LEAK": [f"Per the reference material:\n\n{GOOD}", f"Use it like this:\n\n{GOOD}"],
        "WRONGPROSE": ["Call http:Client fetch(). WRONG", "Use a client per host, created once."],
    })
    with db.run("answer-prompts", s.limits()) as run:
        stats = asyncio.run(gr._answer_all(s, db, run, client, AnswerChecker(s.answers, FakeCompiler())))  # noqa: SLF001
    assert (stats["verified"], stats["judged"], stats["failed"], stats["repaired"]) == (2, 2, 1, 3)

    def response(key: str) -> dict:
        return dict(db.one("SELECT r.* FROM responses r JOIN prompts p ON p.id = r.prompt_id WHERE p.prompt LIKE ?",
                           (f"{key}:%",)))

    code = response("CODE")
    assert (code["status"], code["attempts"], code["repaired"], code["verification"]) == ("verified", 2, 1, "compile")
    assert response("PROSE")["verification"] == "judge" and response("PROSE")["attempts"] == 1
    nocode = response("NOCODE")
    assert nocode["status"] == "failed" and nocode["attempts"] == 3
    assert response("WRONGPROSE")["status"] == "judged" and response("LEAK")["repaired"] == 1
    # repairs get the real problem back
    assert any("incompatible types" in f for f in client.feedback)
    assert any("has no ```ballerina code block" in f for f in client.feedback)
    assert any("http:Client has no `fetch` method" in f for f in client.feedback)
    assert any("reference material" in f for f in client.feedback)
    reasons = {r["reason"] for r in db.query("SELECT reason FROM preference_pairs")}
    assert reasons == {"compile", "leak", "judge"}
    attempt = db.one("SELECT * FROM attempts WHERE n = 1 AND status = 'fail' LIMIT 1")
    assert attempt["feedback"] and json.loads(attempt["checks"])["reason"]

    files = gr.write_outputs(s, db)
    sft = [json.loads(x) for x in (s.data_root / files["sft"]).read_text().splitlines()]
    assert len(sft) == 4 and sft[0]["messages"][0]["role"] == "system"
    assert sft[0]["messages"][-1]["content"].startswith("Fixed:") and sft[0]["covers"] == ["lib.http#a"]
    pairs = [json.loads(x) for x in (s.data_root / files["pairs"]).read_text().splitlines()]
    assert len(pairs) == 3 and pairs[0]["chosen"][0]["role"] == "assistant"
    assert "BAD" in pairs[0]["rejected"][0]["content"]
    assert len((s.data_root / files["failed"]).read_text().splitlines()) == 1

    from sftgen.report import write_report

    path, summary = write_report(s, db)
    page = path.read_text(encoding="utf-8")
    assert "<h2>Answers</h2>" in page and "Why attempts failed" in page and "Failed after all repairs (1 shown)" in page
    assert "Answers    pilot: 4/5 pass (80%; first try 20%, after repair 3)" in summary

    # --retry-failed: a new round of repairs continues the failed chain
    client.script["NOCODE"] = ["still no code", f"Use a string:\n\n{GOOD}"]
    db.execute("DELETE FROM responses WHERE status = 'failed'")
    with db.run("answer-prompts", s.limits()) as run:
        again = asyncio.run(gr._answer_all(s, db, run, client, AnswerChecker(s.answers, FakeCompiler())))  # noqa: SLF001
    assert again["answered"] == 1 and again["verified"] == 1
    assert response("NOCODE")["attempts"] == 4  # attempt 4 = first of the new round
    assert db.scalar("SELECT COUNT(*) FROM responses WHERE status = 'failed'") == 0


def test_teacher_and_judge_must_differ(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="different model family"):
        gr.check_models(settings.model_copy(update={"answer_gen_model": "qwen/a", "judge_model": "qwen/b"}))
