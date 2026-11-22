"""M8: benchmark fetch and translation, codebase/BBE tasks, the eval runner, pass@k and the score map."""

from __future__ import annotations

import asyncio
import gzip
import json
from collections import Counter

import pytest

from balpipe_common.config import ConfigError, Settings
from balpipe_common.db import Database
from balpipe_common.licences import LicenceRegister
from balpipe_common.lock import SourceLock

from sftgen.eval import benchmarks, runner, tasks
from sftgen.generate.llm import Usage
from sftgen.validate.bal_compiler import CompileOutcome

HE = {"task_id": "HumanEval/0", "prompt": "def add(a: int, b: int) -> int:\n    \"\"\"Add.\"\"\"\n",
      "entry_point": "add", "canonical_solution": "    return a + b\n",
      "test": "def check(candidate):\n    assert candidate(1, 2) == 3\n"}
MBPP = [{"task_id": i, "text": f"Write a function {i}.", "code": "def f(): pass", "test_list": ["assert f() == None"],
         "test_setup_code": "", "challenge_test_list": []} for i in (5, 11, 12, 600)]


def test_pass_at_k() -> None:
    assert runner.pass_at_k(5, 0, 1) == 0.0 and runner.pass_at_k(5, 5, 5) == 1.0
    assert runner.pass_at_k(5, 1, 1) == pytest.approx(0.2) and runner.pass_at_k(5, 1, 5) == 1.0
    assert runner.pass_at_k(5, 2, 2) == pytest.approx(1 - 3 / 10)
    assert runner.pass_at_k(1, 1, 5) == 1.0 and runner.pass_at_k(0, 0, 1) == 0.0


def test_parse_problems_and_ranges() -> None:
    s = Settings()
    he, mb = s.eval_.benchmarks
    assert benchmarks.parse_problems(he, gzip.compress((json.dumps(HE) + "\n").encode()))[0][0] == "0"
    data = "\n".join(json.dumps(p) for p in MBPP).encode()
    assert [t for t, _ in benchmarks.parse_problems(mb, data)] == ["11", "12"]  # official test split only
    assert "assert candidate(1, 2) == 3" in benchmarks.source_text("humaneval", HE)


class FakeCompiler:
    """Code with BAD fails; tests are 'run' and fail when the solution says WRONG."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def build_package(self, files, *, test=False, timeout_s=None):  # noqa: ANN001, ANN201
        self.calls.append(test)
        text = "\n".join(files.values())
        if "BAD" in text:
            return CompileOutcome("error", [{"file": "main.bal", "line": 1, "col": 1, "message": "undefined symbol"}])
        if test and "WRONG" in text:
            return CompileOutcome("error", [{"file": "tests", "line": None, "col": None, "message": "test failed"}])
        return CompileOutcome("ok")

    def compile(self, code):  # noqa: ANN001, ANN201
        return CompileOutcome("error" if "BAD" in code else "ok",
                              [{"line": 1, "col": 1, "message": "bad"}] if "BAD" in code else [])


def test_fetch_registers_licences_and_stores_problems(settings: Settings, db: Database,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    import ballerinascraper.repos as repos
    from ballerinascraper.repos import Snapshot

    def snap(url, commit, workdir, cfg):  # noqa: ANN001, ANN202
        if "human-eval" in url:
            return Snapshot({"data/HumanEval.jsonl.gz": gzip.compress((json.dumps(HE) + "\n").encode())},
                            licence_text="MIT License\n\nPermission is hereby granted, free of charge, to any person")
        return Snapshot({"mbpp/mbpp.jsonl": "\n".join(json.dumps(p) for p in MBPP).encode()},
                        licence_text="Apache License\nVersion 2.0, January 2004\n")

    monkeypatch.setattr(repos, "remote_head", lambda url, branch: "c" * 40)
    monkeypatch.setattr(repos, "take_snapshot", snap)
    lock = SourceLock(settings.path(settings.sources.lock_file), settings.sources.swan_lake)
    with db.run("eval-fetch", settings.limits()) as run:
        out = benchmarks.fetch(settings, db, run, lock)
    assert (out["humaneval"]["problems"], out["mbpp"]["problems"]) == (1, 2)
    assert out["humaneval"]["licence"] == "MIT" and out["mbpp"]["licence"] == "Apache-2.0"
    reg = LicenceRegister(settings.path(settings.repos.register_file))
    row = reg.get("google-research/google-research")
    assert row["spdx"] == "CC-BY-4.0" and row["intended_use"] == "eval-benchmark" and "Austin" in row["notes"]
    assert SourceLock(lock.path, settings.sources.swan_lake).get("benchmarks", "mbpp")["commit"] == "c" * 40


class FakeLLM:
    def __init__(self, translations: list[dict], answers: dict[str, list[str]] | None = None) -> None:
        self.translations = translations
        self.answers = answers or {}
        self.feedback: list[str] = []
        self.samples: Counter[str] = Counter()

    async def chat_json(self, model, prompt, schema, **fmt):  # noqa: ANN001, ANN201
        if prompt.id == "benchmark_translate":
            if fmt["previous"]:
                self.feedback.append(fmt["previous"])
            t = self.translations[min(len(self.feedback), len(self.translations) - 1)]
            return schema.model_validate(t), Usage(model, 500, 400, 0.001)
        ok = "flows" in fmt["answer"]
        return schema.model_validate({"correct": 5 if ok else 1, "complete": 4, "clear": 4,
                                      "verdict": "pass" if ok else "fail", "issues": []}), Usage(model, 100, 20, 0.0)

    async def chat(self, model, prompt, *, temperature, max_tokens, sample=None, **fmt):  # noqa: ANN001, ANN201
        key = fmt["request"][:20]
        self.samples[key] += 1
        options = next((v for k, v in self.answers.items() if fmt["request"].startswith(k)), ["no code"])
        return options[(sample or 0) % len(options)], Usage(model, 200, 100, 0.0002)


def _benchmark_row(db: Database) -> None:
    db.insert("benchmark_problems", {"id": "humaneval/0", "suite": "humaneval", "task_id": "0",
                                     "source": json.dumps(HE), "commit_sha": "c", "spdx": "MIT", "status": "pending"})


def test_translate_repairs_then_saves_eval_item(settings: Settings, db: Database) -> None:
    s = settings.model_copy(update={"answer_gen_model": "deepseek/x", "limit_generating": 0})
    _benchmark_row(db)
    good = {"function_name": "add", "prompt": "Implement:\n```ballerina\n# Adds.\nfunction add(int a, int b) returns int {"
            "\n}\n```", "solution": "function add(int a, int b) returns int => a + b;",
            "tests": "import ballerina/test;\n@test:Config {}\nfunction t() { test:assertEquals(add(1, 2), 3); }"}
    llm = FakeLLM([{**good, "solution": "function add(int a, int b) returns int => BAD;"}, good])
    with db.run("eval-translate", s.limits()) as run:
        out = asyncio.run(benchmarks._translate(s, db, run, llm, FakeCompiler()))  # noqa: SLF001
    assert out == {"pending": 1, "translated": 1, "failed": 0, "errors": 0}
    assert "undefined symbol" in llm.feedback[0]
    item = db.one("SELECT * FROM eval_items WHERE id = 'humaneval/0'")
    assert item["task_type"] == "write_code" and item["verify_status"] == "compiled" and "add" in item["prompt"]
    assert json.loads(item["files"]) == {"tests/add_test.bal": good["tests"]}
    assert db.scalar("SELECT COUNT(*) FROM fingerprints WHERE item_id = 'humaneval/0'") == 1
    assert db.scalar("SELECT status FROM benchmark_problems WHERE id = 'humaneval/0'") == "translated"


def _codebase(db: Database) -> None:
    base = {"Ballerina.toml": "[package]\nname = \"x\"\n", "main.bal": "public function main() {\n}\n" * 3}
    db.insert("codebases", {"id": "demo-eval-1", "full_name": "o/r", "root": "", "files": json.dumps(base),
                            "file_count": 2, "tokens": 50, "has_tests": 0, "items": '["lib.http#a"]',
                            "nodes": '["lib.http"]', "split": "eval", "status": "active"})
    db.insert("codebase_states", {"codebase_id": "demo-eval-1", "kind": "compile_error", "target": "main.bal",
                                  "description": "typo", "changed": json.dumps({"main.bal": "BAD"}), "patch": "",
                                  "expect": "fails_build", "verify_status": "pass",
                                  "verify_detail": json.dumps({"errors": [{"file": "main.bal", "line": 1, "col": 1,
                                                                           "message": "undefined"}]}),
                                  "status": "active"})


def test_tasks_and_runner_with_pass_at_k_and_score_map(settings: Settings, db: Database) -> None:
    s = settings.model_copy(update={"answer_gen_model": "deepseek/x", "judge_model": "qwen/judge",
                                    "limit_generating": 0})
    _codebase(db)
    db.insert("eval_items", {"id": "bbe/hello", "suite": "bbe", "version": "v1", "name": "hello", "title": "Hello",
                             "url": "u", "files": json.dumps({"hello.md": "# Hello\n\nPrints a greeting.\n\n::: code "
                                                                          "hello.bal :::\n",
                                                              "hello.bal": "x", "hello.out": "$ bal run hello.bal\nHi\n"}),
                             "covers": '["concept.io#print"]', "verify_mode": "run", "status": "active"})
    with db.run("eval-tasks", s.limits()) as run:
        built = tasks.build_tasks(s, db, run)
    assert built == {"codebase_tasks": 1, "codebase_explain": 1, "bbe_prompts": 1}
    bbe = db.one("SELECT prompt FROM eval_items WHERE id = 'bbe/hello'")["prompt"]
    assert "Prints a greeting." in bbe and "```\nHi\n```" in bbe and "::: code" not in bbe
    fix = db.one("SELECT * FROM eval_items WHERE id = 'codebase/demo-eval-1/compile_error'")
    assert fix["task_type"] == "fix_code" and "undefined" in fix["prompt"] and json.loads(fix["covers"]) == ["lib.http#a"]
    _benchmark_row(db)
    db.insert("eval_items", {"id": "humaneval/0", "suite": "humaneval", "version": "v2", "name": "0", "title": "add",
                             "url": "u", "files": json.dumps({"tests/add_test.bal": "tests"}), "covers": '["lang#ints"]',
                             "verify_mode": "test", "status": "active", "prompt": "BENCH: implement add",
                             "task_type": "write_code", "meta": json.dumps({"function": "add"})})
    good_bench = "```ballerina\nfunction add(int a, int b) returns int => a + b;\n```"
    llm = FakeLLM([], {
        "BENCH": [good_bench, "```ballerina\nfunction add(int a, int b) returns int => BAD;\n```"],
        "Write a complete": ["```ballerina\nimport ballerina/io;\npublic function main() { io:println(\"Hi\"); }\n```"],
        "I'm working": ["```ballerina\n// file: main.bal\npublic function main() {\n}\n```"],
        "Here is a Ballerina": ["It flows from the listener to the service.", "No idea."],
    })
    s5 = s.model_copy(update={"eval_": s.eval_.model_copy(update={"samples": 4})})
    with db.run("eval-run", s5.limits()) as run:
        out = asyncio.run(runner._run(s5, db, run, llm, FakeCompiler(), "base", None))  # noqa: SLF001
    assert out["mode"] == "compile" and out["scored"] == 4 * 4  # 4 items x 4 samples
    sm = out["summary"]["suites"]
    assert sm["humaneval"]["pass@1"] == pytest.approx(0.5) and sm["humaneval"]["pass@5"] == pytest.approx(1.0)
    assert sm["bbe"]["pass@1"] == 1.0 and sm["codebase"]["items"] == 2
    assert out["summary"]["refs"]["lang#ints"] == pytest.approx(0.5)
    assert db.scalar("SELECT status FROM eval_runs WHERE id = ?", (out["eval_run"],)) == "done"
    # the explain task went to the judge
    assert db.scalar("SELECT COUNT(*) FROM llm_usage WHERE phase = 'eval-judge'") == 4

    # resume: a finished run isn't re-sampled; a new run (another model) starts fresh
    with db.run("eval-run", s5.limits()) as run:
        again = asyncio.run(runner._run(s5, db, run, llm, FakeCompiler(), "teacher", ["humaneval"]))  # noqa: SLF001
    assert again["model"] == "deepseek/x" and again["scored"] == 4

    from sftgen.report import write_report

    path, summary_text = write_report(s5, db)
    page = path.read_text(encoding="utf-8")
    assert "<h2>Eval set v2 and baselines</h2>" in page and "Per-item score map" in page and "lang#ints" in page
    assert "runs: base (compile, done), teacher (compile, done)" in summary_text


def test_unknown_model_label(settings: Settings) -> None:
    with pytest.raises(ConfigError, match="unknown model label"):
        runner.resolve_model(settings, "nope")
    assert runner.resolve_model(settings, "base") == "qwen/qwen3.8-27b"


def test_every_prompt_template_formats() -> None:
    """Literal braces in a template must be doubled, or the first live call fails."""
    import string
    from importlib import resources

    from sftgen.generate.llm import load_prompt

    for f in resources.files("sftgen.prompts").iterdir():
        if not f.name.endswith(".yaml"):
            continue
        p = load_prompt(f.name[:-5])
        names = {n for part in (p.system, p.user) for _, n, _, _ in string.Formatter().parse(part) if n is not None}
        assert all(n and not n.isdigit() for n in names), (f.name, names)
        p.messages(**{n: "x" for n in names})
