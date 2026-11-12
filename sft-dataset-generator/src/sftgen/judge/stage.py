"""`sftgen judge`: validated.jsonl -> judged.jsonl

Rubric judge (1-5 on five criteria with reasons) using JUDGE_MODEL, which
should come from a different model family than ANSWER_GEN_MODEL. Candidates
already rejected by validate are passed through without spending tokens.
"""

from __future__ import annotations

import asyncio
import json

from pydantic import BaseModel, Field, field_validator

from balpipe_common.config import Settings
from balpipe_common.ids import utc_now_iso
from balpipe_common.jsonl_io import JsonlAppender, existing_ids, read_models
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun
from balpipe_common.schemas import Chunk, CodeExample, JudgeResult, SFTCandidate

from sftgen.generate.llm import LLMClient, load_prompt
from sftgen.generate.stage import example_context
from sftgen.paths import Paths

CRITERIA = ("faithfulness", "correctness", "helpfulness", "instruction_following", "code_quality")


class _Verdict(BaseModel):
    scores: dict[str, int]
    reasons: dict[str, str] = Field(default_factory=dict)

    @field_validator("scores")
    @classmethod
    def _all_criteria(cls, v: dict[str, int]) -> dict[str, int]:
        missing = [c for c in CRITERIA if c not in v]
        if missing:
            raise ValueError(f"missing scores: {missing}")
        return {k: max(1, min(5, int(v[k]))) for k in CRITERIA}


def judge_context(c: SFTCandidate, examples: dict[str, CodeExample], chunks: dict[str, Chunk]) -> str:
    parts: list[str] = []
    if c.seed.kind == "code_example" and c.seed.id in examples:
        parts.append(example_context(examples[c.seed.id]))
    parts.extend(chunks[cid].embed_text for cid in c.context_chunk_ids if cid in chunks)
    return "\n\n---\n\n".join(parts) or "(no reference: the answer must stand on the code in the request)"


def compiler_summary(c: SFTCandidate) -> str:
    if not c.code_validation or not c.code_validation.blocks:
        return "No Ballerina code blocks in the answer."
    return json.dumps([{"block": i + 1, "status": b.compile_status, "errors": [e.message for e in b.errors][:3]}
                       for i, b in enumerate(c.code_validation.blocks)])


def run(settings: Settings, *, run_id: str, limit: int | None = None) -> dict:
    settings.require_llm(judge=True)
    paths = Paths(settings)
    run_dir = paths.sft_run(run_id)
    src, out_path = run_dir / "validated.jsonl", run_dir / "judged.jsonl"
    examples = {e.example_id: e for e in read_models(paths.code_examples, CodeExample)}
    chunks = {c.chunk_id: c for c in read_models(paths.chunks, Chunk)}
    prompt = load_prompt("judge.rubric")
    with StageRun(settings, "judge", run_dir, inputs=[src], run_id=run_id, manifest_name="_manifest.judge.json", resumable=True) as stage:
        log = get_logger(__name__, run_id=run_id, stage="judge")
        done = existing_ids(out_path, "id")
        todo = [c for c in read_models(src, SFTCandidate) if c.id not in done]
        if limit:
            todo = todo[:limit]
        stage.count("skipped_existing", len(done))
        llm = LLMClient(settings, paths.cache / "llm", concurrency=settings.judge.concurrency)

        async def _main() -> None:
            with JsonlAppender(out_path) as out:
                async def one(c: SFTCandidate) -> None:
                    if c.decision.status == "rejected":
                        out.write(c)
                        stage.count("passed_through_rejected")
                        return
                    try:
                        v, call = await llm.chat_json(
                            settings.judge_model, prompt, _Verdict, temperature=0.0, max_tokens=800,  # type: ignore[arg-type]
                            context=judge_context(c, examples, chunks), compiler=compiler_summary(c),
                            question=c.messages[0].content, answer=c.messages[-1].content)
                    except Exception as exc:  # noqa: BLE001
                        stage.error(c.id, exc)
                        return
                    c.judge = JudgeResult(model=call.model, provider=call.provider, prompt_id=call.prompt_id,
                                          prompt_version=call.prompt_version, scores=v.scores, reasons=v.reasons,
                                          created_at=utc_now_iso())
                    out.write(c)
                    stage.count("judged")

                await asyncio.gather(*(one(c) for c in todo))

        asyncio.run(_main())
        stage.add_output(out_path)
        log.info("judge finished", extra={"event": "judge.done"})
        return dict(stage.counts)
