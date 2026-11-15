"""Phases end to end on a temporary SQLite database, with a fake LLM client."""

from __future__ import annotations

import asyncio
import json

from balpipe_common.config import Settings
from balpipe_common.db import Database

from sftgen.clean.dedup import QuestionDeduper
from sftgen.clean.stage import process_pages
from sftgen.export.stage import run as export
from sftgen.generate import answers, questions
from sftgen.generate.llm import Usage
from sftgen.report import write_report

BBE_MD = "# Hello\n\n" + "Ballerina prints text with the io module. " * 6 + \
         "\n\n```ballerina\nimport ballerina/io;\n\npublic function main() {\n    io:println(\"hi\");\n}\n```\n"


def _page(db: Database, url: str, source: str, markdown_body: str) -> int:
    bundle = json.dumps({"x.md": markdown_body})
    return db.insert("pages", {"source": source, "url": url, "status": "fetched", "raw_body": bundle, "raw_hash": "h",
                               "meta": json.dumps({"origin": "github"})})


def _process(s: Settings, db: Database) -> dict[str, int]:
    with db.run("scrape", s.limits()) as run:
        return process_pages(s, db, run)


class FakeClient:
    """Returns canned questions; answers echo the question."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.calls = 0

    async def chat_json(self, model, prompt, schema, *, temperature, max_tokens, **fmt):  # noqa: ANN001
        self.calls += 1
        base = fmt["source_url"].rstrip("/").rsplit("/", 1)[-1]
        topics = ["print a value", "define a record type with optional fields", "start an HTTP listener",
                  "handle errors with check", "iterate over a map", "write a unit test"]
        qs = [{"question": f"In {base}, how do I {topics[i % len(topics)]}?", "category": "Code Generation",
               "difficulty": "Beginner"} for i in range(fmt["n"])]
        qs.append({"question": "What does the excerpt above say?", "category": "concept", "difficulty": "x"})
        return schema.model_validate({"questions": qs}), Usage(model, 100, 50, 0.001)

    async def chat(self, model, prompt, *, temperature, max_tokens, **fmt):  # noqa: ANN001
        self.calls += 1
        if self.fail_on and self.fail_on in fmt["question"]:
            raise RuntimeError("model unavailable")
        return f"Answer to: {fmt['question']}\n\n```ballerina\nint x = 1;\n```", Usage(model, 200, 80, None)


def test_process_pages_chunks_and_rechunks(settings: Settings, db: Database) -> None:
    pid = _page(db, "https://ballerina.io/learn/by-example/hello/", "bbe", BBE_MD)
    stats = _process(settings, db)
    assert stats["pages_chunked"] == 1 and stats["chunks_new"] >= 1
    first = db.query("SELECT id, text FROM chunks WHERE page_id = ?", (pid,))
    # the same page changes: a section is added; the unchanged chunk keeps its id
    db.update("pages", pid, {"status": "fetched", "raw_body": json.dumps(
        {"x.md": BBE_MD + "\n## More\n\n" + "A second section about services and listeners. " * 6})})
    _process(settings, db)
    after = {r["text"]: r for r in db.query("SELECT * FROM chunks WHERE page_id = ?", (pid,))}
    assert first[0]["text"] in after and after[first[0]["text"]]["id"] == first[0]["id"]
    assert len(after) > len(first)


def test_duplicate_page_is_marked_not_chunked(settings: Settings, db: Database) -> None:
    _page(db, "https://ballerina.io/learn/by-example/a/", "bbe", BBE_MD)
    _page(db, "https://ballerina.io/learn/by-example/b/", "bbe", BBE_MD)
    stats = _process(settings, db)
    assert stats["pages_chunked"] == 1 and stats["pages_duplicate"] == 1


def _questions(s: Settings, db: Database, client: FakeClient) -> dict[str, int]:
    with db.run("generate-questions", s.limits()) as run:
        return asyncio.run(questions._generate(s, db, run, client))  # noqa: SLF001


def test_questions_respect_limit_round_robin_and_filters(settings: Settings, db: Database) -> None:
    for i in range(3):
        _page(db, f"https://ballerina.io/learn/by-example/ex{i}/", "bbe", BBE_MD.replace("Hello", f"Hello {i}") +
              f"\n\nUnique sentence number {i}.\n")
    db.insert("pages", {"source": "github", "url": "https://github.com/o/r/blob/main/README.md", "status": "fetched",
                        "raw_body": "# R\n\n" + "The module offers clients and listeners for messaging. " * 6,
                        "meta": json.dumps({"repo": "ballerina-platform/module-ballerina-r", "path": "README.md"})})
    _process(settings, db)
    s = settings.model_copy(update={"limit_generating": 4, "questions_per_chunk": 3})
    stats = _questions(s, db, FakeClient())
    rows = db.query("SELECT * FROM questions ORDER BY id")
    assert len(rows) == 4 == stats["questions_saved"]
    assert {r["source"] for r in rows} == {"bbe", "github"}  # round-robin reaches the second source
    assert all(r["category"] == "code-writing" and r["difficulty"] == "beginner" for r in rows)
    assert all("excerpt" not in r["question"] for r in rows)
    assert db.scalar("SELECT COUNT(*) FROM llm_usage WHERE phase = 'generate-questions'") >= 2
    # a second run only takes chunks that still have no questions
    stats2 = _questions(s, db, FakeClient())
    assert stats2["chunks_available"] < stats["chunks_available"]


def test_question_deduper() -> None:
    d = QuestionDeduper(0.85)
    d.add("How do I read a file in Ballerina?")
    assert d.is_duplicate("how do I read a file in ballerina")
    assert not d.is_duplicate("How do I write a file in Ballerina?")


def test_answers_cite_source_and_record_failures(settings: Settings, db: Database) -> None:
    _page(db, "https://ballerina.io/learn/by-example/hello/", "bbe", BBE_MD)
    _process(settings, db)
    _questions(settings.model_copy(update={"limit_generating": 3}), db, FakeClient())
    first = db.scalar("SELECT question FROM questions ORDER BY id LIMIT 1")
    with db.run("generate-answers", settings.limits()) as run:
        stats = asyncio.run(answers._answer_all(settings, db, run, FakeClient(fail_on=first)))  # noqa: SLF001
    assert stats == {"questions_pending": 3, "answered": 2, "failed": 1}
    failed = db.one("SELECT status, error FROM questions WHERE question = ?", (first,))
    assert failed["status"] == "failed" and "model unavailable" in failed["error"]
    for a in db.query("SELECT a.answer, q.source_url FROM answers a JOIN questions q ON q.id = a.question_id"):
        assert a["answer"].rstrip().endswith(f"Source: {a['source_url']}")


def test_export_skips_invalid_and_report_renders(settings: Settings, db: Database) -> None:
    _page(db, "https://ballerina.io/learn/by-example/hello/", "bbe", BBE_MD)
    _process(settings, db)
    _questions(settings.model_copy(update={"limit_generating": 2}), db, FakeClient())
    with db.run("generate-answers", settings.limits()) as run:
        asyncio.run(answers._answer_all(settings, db, run, FakeClient()))  # noqa: SLF001
    db.execute("UPDATE answers SET status = 'invalid' WHERE id = (SELECT MIN(id) FROM answers)")
    with db.run("export", settings.limits()) as run:
        stats = export(settings, db, run)
    lines = (settings.repo_root / "data" / "dataset.jsonl").read_text(encoding="utf-8").splitlines()
    assert stats == {"rows_exported": 1, "invalid_skipped": 1} and len(lines) == 1
    assert set(json.loads(lines[0])) == {"question", "answer", "source_url", "category", "difficulty"}

    db.insert("pages", {"source": "learn", "url": "https://ballerina.io/learn/broken/", "status": "failed",
                        "error": "HTTPError: 500 <b>", "meta": "{}"})
    path, summary = write_report(settings, db)
    page = path.read_text(encoding="utf-8")
    for heading in ("Pages scraped", "Failed pages (1)", "Chunks per source", "By category", "By difficulty",
                    "Validation", "Token usage and cost per phase", "Run history", "Sample Q&amp;A"):
        assert heading in page
    assert "HTTPError: 500 &lt;b&gt;" in page  # errors are escaped
    assert "Questions  2" in summary and "answered 2" in summary


def test_ensure_citation() -> None:
    assert answers.ensure_citation("Text", "https://x/") == "Text\n\nSource: https://x/"
    assert answers.ensure_citation("Text\n\nSource: https://x/", "https://x/") == "Text\n\nSource: https://x/"


def test_normalizers() -> None:
    assert questions.normalize_category("Error Handling") == "error-fixing"
    assert questions.normalize_category("weird") == "concept"
    assert questions.normalize_difficulty("ADVANCED") == "advanced"


def test_export_sft_messages_split_and_source_stripped(settings: Settings, db: Database) -> None:
    from sftgen.export import sft

    for i in range(6):
        _page(db, f"https://ballerina.io/learn/by-example/p{i}/", "bbe",
              BBE_MD.replace("Hello", f"Hello {i}") + f"\n\nUnique sentence {i}.\n")
    _process(settings, db)
    _questions(settings.model_copy(update={"limit_generating": 12}), db, FakeClient())
    with db.run("generate-answers", settings.limits()) as run:
        asyncio.run(answers._answer_all(settings, db, run, FakeClient()))  # noqa: SLF001
    db.execute("UPDATE answers SET status = 'invalid' WHERE id = (SELECT MIN(id) FROM answers)")
    s = settings.model_copy(update={"sft": settings.sft.model_copy(update={"eval_fraction": 0.5})})
    with db.run("export-sft", s.limits()) as run:
        stats = sft.run(s, db, run)
    out = s.repo_root / "data" / "sft"
    train = [json.loads(x) for x in (out / "train.jsonl").read_text().splitlines()]
    evals = [json.loads(x) for x in (out / "eval.jsonl").read_text().splitlines()]
    meta = [json.loads(x) for x in (out / "metadata.jsonl").read_text().splitlines()]
    assert stats["sft_train"] + stats["sft_eval"] == 11 == len(meta)  # 12 answers, 1 invalid left out
    row = (train + evals)[0]
    assert [m["role"] for m in row["messages"]] == ["system", "user", "assistant"]
    assert all("Source:" not in r["messages"][-1]["content"] for r in train + evals)
    by_split: dict[str, set[str]] = {"train": set(), "eval": set()}
    for m in meta:
        by_split[m["split"]].add(m["source_url"])
    assert not by_split["train"] & by_split["eval"]  # no page on both sides


def test_sft_helpers() -> None:
    from sftgen.export.sft import split_for, strip_source, to_messages

    assert strip_source("Text\n\n```ballerina\nint x;\n```\n\nSource: https://x/y/") == "Text\n\n```ballerina\nint x;\n```"
    assert strip_source("No citation") == "No citation"
    assert [m["role"] for m in to_messages("q", "a", "")] == ["user", "assistant"]
    assert split_for("https://a/", 0.0) == "train" and split_for("https://a/", 0.999999) == "eval"
