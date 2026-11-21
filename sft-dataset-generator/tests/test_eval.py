"""build-eval: BBE split, eval items, fingerprints, output check, and decontaminated exports."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from balpipe_common.config import Settings
from balpipe_common.db import Database
from balpipe_common.lock import SourceLock

from sftgen.eval import bbe
from sftgen.eval.bbe import build_eval, parse_examples, split_for
from sftgen.eval.verify import Verifier, compare_output, run_transcript, verify_mode
from sftgen.export.stage import run as export

ORDERS = """import ballerina/io;

type Order record {|
    string id;
    decimal total;
    string[] items;
|};

public function main() {
    Order[] orders = [{id: "o-17", total: 42.5, items: ["pen", "ink"]}, {id: "o-18", total: 7.25, items: []}];
    decimal[] totals = from Order o in orders where o.items.length() > 0 select o.total;
    io:println("Totals of non-empty orders: ", totals);
}
"""


def _examples(n: int) -> dict[str, bytes]:
    """A fake examples/ folder: n generated examples plus `orders`, listed in index.json."""
    files: dict[str, bytes] = {}
    samples = []
    for i in range(n):
        name = f"example-{i}"
        code = f"import ballerina/io;\n\npublic function main() {{\n    int v{i} = {i} * {i + 3};\n" \
               f"    io:println(\"value {i}: \", v{i});\n}}\n"
        files[f"examples/{name}/example_{i}.bal"] = code.encode()
        files[f"examples/{name}/example_{i}.out"] = f"$ bal run example_{i}.bal\nvalue {i}: {i * (i + 3)}\n".encode()
        files[f"examples/{name}/example_{i}.md"] = f"# Example {i}\n".encode()
        samples.append({"name": f"Example {i}", "url": name, "verifyBuild": True, "verifyOutput": i % 2 == 0})
    files["examples/orders/orders.bal"] = ORDERS.encode()
    files["examples/orders/orders.out"] = b"$ bal run orders.bal\nTotals of non-empty orders: [42.5]\n"
    files["examples/orders/orders.md"] = b"# Orders\n\nQuery expressions over records.\n"
    files["examples/orders/tests/orders_test.bal"] = b"// not part of the item\n"
    files["examples/unlisted/x.bal"] = b"public function main() {}\n"
    samples.append({"name": "Orders", "url": "orders", "verifyBuild": True, "verifyOutput": True})
    files["examples/index.json"] = json.dumps([{"title": "Basics", "category": "Language concepts",
                                                "samples": samples}]).encode()
    return files


def test_split_is_by_name_and_stable() -> None:
    names = [f"ex-{i}" for i in range(2000)]
    share = sum(split_for(n, 0.3) == "eval" for n in names) / len(names)
    assert 0.27 < share < 0.33
    assert [split_for(n, 0.3) for n in names[:50]] == [split_for(n, 0.3) for n in names[:50]]
    # a bigger fraction only moves examples from train to eval, never back
    assert all(split_for(n, 0.4) == "eval" for n in names if split_for(n, 0.3) == "eval")


def test_parse_examples_reads_index_and_top_level_files() -> None:
    examples, unlisted = parse_examples(_examples(3), "examples")
    assert [e.name for e in examples] == ["example-0", "example-1", "example-2", "orders"]
    orders = examples[-1]
    assert set(orders.files) == {"orders.bal", "orders.out", "orders.md"}  # tests/ is left out
    assert orders.category == "Language concepts" and orders.verify_output and unlisted == ["unlisted"]


def test_verify_mode_and_output_comparison() -> None:
    files = {"a.bal": "x", "a.out": "$ bal run a.bal\nhello\n"}
    assert run_transcript(files) == ("a.bal", ["hello"])
    assert verify_mode(files, True, True) == "run"
    assert verify_mode(files, False, True) == "build"
    assert verify_mode({"a.bal": "x", "a.server.out": "$ bal run a.bal\n", "a.client.out": "$ curl x\n"},
                       True, True) == "build"
    assert verify_mode({"a.bal": "x", "a.out": "$ bal run a.bal\nhi\n$ curl localhost\n"}, True, True) == "build"
    assert verify_mode({"a.md": "x"}, True, True) == "none" and verify_mode(files, False, False) == "none"
    banner = "WARNING: Final field argList\nCompiling source\n\ta.bal\n\nRunning executable\n\n"
    assert compare_output(["hello", ""], "hello  \n", banner) == (True, "")
    assert compare_output(["hello", "error: boom"], "hello\n", banner + "error: boom\n") == (True, "")
    # stack traces: spaces in the transcript, tabs from bal
    assert compare_output(["error: x", "        at m:main(m.bal:3)"], "", banner + "error: x\n\tat m:main(m.bal:3)\n")[0]
    ok, why = compare_output(["hello"], "goodbye\n", banner)
    assert not ok and "expected 'hello'" in why


@pytest.mark.skipif(shutil.which("bal") is None, reason="needs the Ballerina toolchain")
def test_verifier_runs_bal(tmp_path: Path) -> None:
    v = Verifier(Settings().validate_, tmp_path / "cache", timeout_s=180)
    files = {"orders.bal": ORDERS, "orders.out": "$ bal run orders.bal\nTotals of non-empty orders: [42.5]\n"}
    assert v.verify(files, "run").status == "pass"
    wrong = {**files, "orders.out": "$ bal run orders.bal\nTotal: 0\n"}
    assert v.verify(wrong, "run").status == "output_differs"
    assert v.verify({"bad.bal": "public function main() { int x = \"s\"; }\n"}, "build").status == "build_failed"


def test_build_eval_writes_items_and_decontaminates_exports(settings: Settings, db: Database,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    files = _examples(40)
    monkeypatch.setattr(bbe, "tag_commit", lambda url, tag: "c0ffee" * 6 + "abcd")
    monkeypatch.setattr(bbe, "examples_snapshot", lambda s, url, commit: files)
    monkeypatch.setattr(bbe, "split_for", lambda name, frac, salt="": "eval" if name in
                        ("orders", "example-1", "example-2") else "train")
    # a scraped copy of the eval example on another page, and a question that pastes its code
    page = db.insert("pages", {"source": "learn", "url": "https://ballerina.io/learn/queries/", "status": "chunked",
                               "meta": "{}"})
    chunk = db.insert("chunks", {"page_id": page, "source": "learn", "source_url": "u", "ordinal": 0,
                                 "heading_path": "", "text": "```ballerina\n" + ORDERS + "```", "text_hash": "h",
                                 "token_estimate": 99, "has_code": 1, "status": "pending"})
    for i, answer in enumerate((ORDERS, "Use `from ... select` to build a list.")):
        q = db.insert("questions", {"chunk_id": chunk if i == 0 else chunk, "source": "learn", "source_url": "u",
                                    "question": f"Question {i}?", "question_hash": str(i), "category": "code",
                                    "difficulty": "basic", "status": "answered"})
        db.insert("answers", {"question_id": q, "answer": answer, "status": "valid"})
    db.execute("UPDATE chunks SET text = 'Queries over records.' WHERE id = ?", (chunk,))  # only q0 pastes the code

    lock = SourceLock(settings.path(settings.sources.lock_file), settings.sources.swan_lake)
    with db.run("build-eval", settings.limits()) as run:
        stats = build_eval(settings, db, run, lock, verify=False)
    assert (stats["examples"], stats["eval"], stats["train"], stats["unlisted_folders"]) == (41, 3, 38, 1)
    assert lock.get("bbe", "ballerina-platform/ballerina-distribution")["ref"] == "v2201.14.1"
    item = db.one("SELECT * FROM eval_items WHERE id = 'bbe/orders'")
    assert item["verify_mode"] == "run" and item["status"] == "active" and item["commit_sha"].startswith("c0ffee")
    assert db.scalar("SELECT COUNT(*) FROM fingerprints") == 3
    assert stats["decontam"]["questions_overlap"] == 1

    out = settings.eval_dir
    rows = [json.loads(x) for x in (out / "bbe.jsonl").read_text().splitlines()]
    assert [r["id"] for r in rows] == ["bbe/example-1", "bbe/example-2", "bbe/orders"]
    orders = rows[-1]
    assert orders["code"] == {"orders.bal": ORDERS} and "orders.out" in orders["outputs"]
    assert orders["explanation"].startswith("# Orders") and orders["verify"]["mode"] == "run"
    split = json.loads((out / "split.json").read_text())
    assert len(split["eval"]) == 3 and len(split["train"]) == 38 and split["commit"].startswith("c0ffee")
    assert len((out / "decontam.jsonl").read_text().splitlines()) == 3

    with db.run("export", settings.limits()) as run:
        ex = export(settings, db, run)
    assert ex["rows_exported"] == 1 and ex["eval_overlap_dropped"] == 1 and ex["eval_overlap_in_export"] == 0

    # an example leaving the eval side is retired, and its fingerprints go with it
    monkeypatch.setattr(bbe, "split_for", lambda name, frac, salt="": "eval" if name == "example-1" else "train")
    with db.run("build-eval", settings.limits()) as run:
        again = build_eval(settings, db, run, lock, verify=False)
    assert again["eval"] == 1 and again["retired"] == 2 and again["fingerprints"] == 1
    assert db.scalar("SELECT status FROM eval_items WHERE id = 'bbe/orders'") == "retired"
    assert again["decontam"]["questions_overlap"] == 0
