"""Generation-side logic that does not need a model: mutations, planning, checks, export decisions."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import BaseModel

from balpipe_common.config import Settings
from balpipe_common.schemas import (
    Checks, CodeExample, CodeValidationResult, Decision, JudgeResult, License, Message, PipelineInfo, Seed, SFTCandidate,
)

from sftgen.export.stage import decide, max_overlap, near_duplicates, ngrams
from sftgen.generate.llm import LLMError, first_fence, parse_json
from sftgen.generate.mutation import candidate_mutants, remove_function_body
from sftgen.generate.stage import build_plan
from sftgen.validate.static_checks import ballerina_blocks, mentions_context, unknown_symbols

PROGRAM = ('import ballerina/io;\n\npublic function main() returns error? {\n    int count = 3;\n'
           '    int parsed = check int:fromString("42");\n    io:println(count + parsed);\n}\n')


def _example(i: int, code: str = PROGRAM, form: str = "program", status: str = "ok", doc: str | None = None) -> CodeExample:
    return CodeExample(example_id=f"ex_{i}", doc_id=doc or f"doc_{i}", run_id="r", source_uri="u", ordinal=0, title="t",
                       intro="Prints a number.", code=code, code_sha256=str(i), code_form=form,  # type: ignore[arg-type]
                       pairing_method="bbe_directive", validation=CodeValidationResult(compile_status=status),  # type: ignore[arg-type]
                       split="train", license=License(spdx="Apache-2.0"))


def _cand(**kw) -> SFTCandidate:
    base = dict(id="sft_1", run_id="r", task_type="write_code", construction="llm_grounded",
                seed=Seed(kind="code_example", id="ex_1", doc_id="doc_1"), split="train", licenses=["Apache-2.0"],
                pipeline=PipelineInfo(config_sha256="x"), created_at="t",
                messages=[Message(role="user", content="q"), Message(role="assistant", content="a")])
    base.update(kw)
    return SFTCandidate(**base)


# ------------------------------------------------------------------- mutations
def test_mutations_change_code_and_explain() -> None:
    muts = candidate_mutants(PROGRAM)
    kinds = [m.kind for m in muts]
    assert {"misspelled_function", "missing_check", "type_mismatch", "missing_semicolon"} <= set(kinds)
    for m in muts:
        assert m.code != PROGRAM and m.explanation
    mis = next(m for m in muts if m.kind == "misspelled_function")
    assert "int:parseString(" in mis.code and "`int:fromString`" in mis.explanation  # first qualified call
    # rotation gives different seeds different first mutation kinds
    assert candidate_mutants(PROGRAM, 1)[0].kind != candidate_mutants(PROGRAM, 0)[0].kind


def test_remove_function_body_prefers_main() -> None:
    skel, fn = remove_function_body(PROGRAM)  # type: ignore[misc]
    assert fn == "main" and "// TODO: implement" in skel and "io:println" not in skel
    assert remove_function_body("function f() { return; }") is None  # too small to be a task


# ---------------------------------------------------------------------- planning
def test_plan_respects_quotas_eligibility_and_per_doc_cap() -> None:
    s = Settings()
    s.generate.target_records = 20
    s.generate.max_per_doc = 2
    examples = [_example(i) for i in range(30)] + [_example(99, status="error"), _example(100, form="fragment", code="io:println(1);")]
    plan = build_plan(s, examples, [], can_compile=True)
    assert all(p.example and p.example.validation.compile_status == "ok" for p in plan)
    by_task: dict[str, int] = {}
    for p in plan:
        by_task[p.task] = by_task.get(p.task, 0) + 1
    assert by_task["write_code"] == round(20 * 0.25)
    assert "concept" not in by_task  # no chunks given
    per_doc: dict[str, int] = {}
    for p in plan:
        per_doc[p.doc_id] = per_doc.get(p.doc_id, 0) + 1
    assert max(per_doc.values()) <= 2
    assert build_plan(s, examples, [], can_compile=True) == plan  # deterministic
    assert not [p for p in build_plan(s, examples, [], can_compile=False) if p.task == "debug_fix"]


# ------------------------------------------------------------------ static checks
@pytest.mark.parametrize("text,leak", [
    ("Based on the provided context, you can use http:Client.", True),
    ("According to the documentation provided, ...", True),
    ("The reference material shows ...", True),
    ("You can use `http:Client` to call the service.", False),
    ("Based on your requirements, here is a service.", False),
])
def test_mentions_context(text: str, leak: bool) -> None:
    assert mentions_context(text) is leak


def test_ballerina_blocks_and_unknown_symbols() -> None:
    ans = "Use this:\n\n```ballerina\nimport ballerina/io;\npublic function main() { io:printlnx(\"a\"); io:println(\"b\"); }\n```\n\n```bash\nbal run\n```"
    blocks = ballerina_blocks(ans)
    assert len(blocks) == 1
    assert unknown_symbols(blocks, {"io:println", "http:Client"}) == ["io:printlnx"]


# -------------------------------------------------------------------- llm parsing
def test_parse_json_tolerates_prose_around_json() -> None:
    class Q(BaseModel):
        question: str | None

    assert parse_json('Sure! {"question": "How?"} Thanks', Q).question == "How?"
    with pytest.raises(LLMError):
        parse_json("no json here", Q)
    assert first_fence("x\n```go\nfunc main() {}\n```\n") == "func main() {}"


# ------------------------------------------------------------------- export logic
def _judge(**scores: int) -> JudgeResult:
    full = {"faithfulness": 5, "correctness": 5, "helpfulness": 5, "instruction_following": 5, "code_quality": 5, **scores}
    return JudgeResult(model="m", provider="p", prompt_id="j", prompt_version="1", scores=full, created_at="t")


def test_decide_thresholds_validate_rejections_and_licenses() -> None:
    s = Settings()
    assert decide(_cand(judge=_judge()), s) == []
    assert decide(_cand(judge=_judge(correctness=3)), s) == ["judge.correctness<4"]
    assert decide(_cand(), s) == ["not_judged"]
    rejected = _cand(decision=Decision(status="rejected", reasons=["compile_error"]), judge=None)
    assert decide(rejected, s) == ["compile_error"]
    assert "license_not_allowed" in decide(_cand(judge=_judge(), licenses=["NOASSERTION"]), s)
    s.export.reject_unknown_symbols = True
    assert "unknown_symbols" in decide(_cand(judge=_judge(), checks=Checks(unknown_symbols=["io:x"])), s)


def test_ngram_overlap_and_near_duplicates() -> None:
    eval_text = " ".join(f"w{i}" for i in range(40))
    grams = ngrams(eval_text, 13)
    assert max_overlap(eval_text, grams, 13) == 1.0
    assert max_overlap(" ".join(f"z{i}" for i in range(40)), grams, 13) == 0.0
    vecs = np.array([[1.0, 0.0], [0.99, 0.141], [0.0, 1.0]])
    vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    assert near_duplicates(["a", "b", "c"], vecs, 0.95) == [None, 0, None]
