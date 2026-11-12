"""`sftgen generate`: seeds (code examples, chunks) -> candidates.jsonl

Task types and how their answers are made trustworthy:

| task                   | construction    | answer                                                  |
|------------------------|-----------------|---------------------------------------------------------|
| write_code             | llm_grounded    | LLM answer grounded in the seed example + retrieval     |
| explain_code           | llm_grounded    | LLM explanation grounded in the example's own docs      |
| concept, api_usage     | llm_grounded    | LLM answer grounded in the seed chunk + retrieval       |
| debug_fix              | mutation        | original verified code + deterministic explanation      |
| convert_from_language  | back_translation| original verified code (+ its human-written intro)      |
| complete_code          | seed_verified   | original verified code                                  |

Every finished item is appended (fsync) to candidates.jsonl; a re-run with the
same run id skips (task_type, seed) pairs already present.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from balpipe_common.config import Settings
from balpipe_common.ids import new_run_id, sft_id, sha256_text, utc_now_iso
from balpipe_common.jsonl_io import JsonlAppender, read_jsonl, read_models
from balpipe_common.logging import get_logger
from balpipe_common.manifest import StageRun, find_resumable, git_commit
from balpipe_common.schemas import (
    Chunk, CodeExample, Generation, Message, Mutation, PipelineInfo, Seed, SFTCandidate,
)

from sftgen.generate.llm import LLMClient, LLMError, first_fence, load_prompt
from sftgen.generate.mutation import candidate_mutants, remove_function_body
from sftgen.paths import Paths

CODE_SEED_TASKS = ("write_code", "explain_code", "debug_fix", "convert_from_language", "complete_code")
CONCEPT_DOC_TYPES = {"guide", "tutorial", "spec", "bbe"}
API_DOC_TYPES = {"api_readme"}
INSUFFICIENT = "INSUFFICIENT_CONTEXT"
EXPLAIN_TEMPLATES = [
    "Explain what the following Ballerina code does:\n\n```ballerina\n{code}\n```",
    "Can you walk me through this Ballerina program?\n\n```ballerina\n{code}\n```",
    "What does this Ballerina code do, and how does it work?\n\n```ballerina\n{code}\n```",
]


class _Question(BaseModel):
    question: str | None = None


@dataclass
class PlanItem:
    task: str
    seed_kind: str  # code_example | chunk
    example: CodeExample | None = None
    chunk: Chunk | None = None

    @property
    def seed_id(self) -> str:
        return self.example.example_id if self.example else self.chunk.chunk_id  # type: ignore[union-attr]

    @property
    def doc_id(self) -> str:
        return self.example.doc_id if self.example else self.chunk.doc_id  # type: ignore[union-attr]

    @property
    def split(self) -> str:
        return self.example.split if self.example else self.chunk.split  # type: ignore[union-attr]

    @property
    def key(self) -> str:
        return f"{self.task}:{self.seed_id}"


def _stable_order(items: list[Any], salt: str, key) -> list[Any]:  # type: ignore[no-untyped-def]
    return sorted(items, key=lambda x: sha256_text(f"{salt}:{key(x)}"))


def build_plan(settings: Settings, examples: list[CodeExample], chunks: list[Chunk], *, can_compile: bool) -> list[PlanItem]:
    g = settings.generate
    ok = [e for e in examples if e.dedup.status == "unique" and e.validation.compile_status == "ok"
          and e.pairing_method != "doc_comment"]
    pools: dict[str, list[PlanItem]] = {
        "write_code": [PlanItem("write_code", "code_example", example=e) for e in ok if e.intro.strip()],
        "explain_code": [PlanItem("explain_code", "code_example", example=e) for e in ok if len(e.code) < 4000],
        "debug_fix": [PlanItem("debug_fix", "code_example", example=e) for e in ok
                      if can_compile and e.code_form in ("program", "declarations")],
        "convert_from_language": [PlanItem("convert_from_language", "code_example", example=e) for e in ok
                                  if e.code_form == "program" and len(e.code) < 3000],
        "complete_code": [PlanItem("complete_code", "code_example", example=e) for e in ok
                          if e.code_form != "fragment" and remove_function_body(e.code)],
        "concept": [PlanItem("concept", "chunk", chunk=c) for c in chunks
                    if c.doc_type in CONCEPT_DOC_TYPES and len(c.text.split()) >= 40],
        "api_usage": [PlanItem("api_usage", "chunk", chunk=c) for c in chunks
                      if c.doc_type in API_DOC_TYPES and len(c.text.split()) >= 40],
    }
    per_doc: dict[str, int] = {}
    plan: list[PlanItem] = []
    for task, weight in g.tasks.items():
        quota = round(g.target_records * weight)
        taken = 0
        for item in _stable_order(pools.get(task, []), task, lambda p: p.seed_id):
            if taken >= quota:
                break
            if per_doc.get(item.doc_id, 0) >= g.max_per_doc:
                continue
            per_doc[item.doc_id] = per_doc.get(item.doc_id, 0) + 1
            plan.append(item)
            taken += 1
    return plan


def example_context(e: CodeExample) -> str:
    where = " > ".join(e.heading_path) or e.title
    parts = [f"### {where}", e.intro, f"```ballerina\n{e.code}\n```", e.notes]
    if e.expected_output:
        parts.append(f"Output:\n```\n{e.expected_output}\n```")
    return "\n\n".join(p for p in parts if p and p.strip())


class Generator:
    def __init__(self, settings: Settings, run_id: str, chunks: dict[str, Chunk], compiler: Any | None) -> None:
        self.s = settings
        self.run_id = run_id
        self.chunks = chunks
        self.compiler = compiler
        self.llm = LLMClient(settings, Paths(settings).cache / "llm")
        self.pipeline = PipelineInfo(git_commit=git_commit(settings.repo_root), config_sha256=settings.config_sha256)
        from sftgen.index.stage import open_collection

        self.collection = open_collection(settings)
        self.chunk_by_example = {eid: c.chunk_id for c in chunks.values() for eid in c.code_example_ids}

    # ------------------------------------------------------------------ #
    async def retrieve(self, query: str, split: str, exclude: set[str]) -> list[Chunk]:
        from sftgen.index.stage import retrieve

        hits = await asyncio.to_thread(retrieve, self.collection, self.s, query, k=self.s.generate.top_k + len(exclude),
                                       split=split)
        out = [self.chunks[h["id"]] for h in hits if h["id"] in self.chunks and h["id"] not in exclude]
        return out[: self.s.generate.top_k]

    def _candidate(self, item: PlanItem, construction: str, user: str, assistant: str, generation: Generation,
                   context: list[Chunk]) -> SFTCandidate:
        seed_uri = item.example.source_uri if item.example else item.chunk.source_uri  # type: ignore[union-attr]
        seed_lic = item.example.license.spdx if item.example else item.chunk.license_spdx  # type: ignore[union-attr]
        chunk_id = self.chunk_by_example.get(item.seed_id) if item.example else item.seed_id
        return SFTCandidate(
            id=sft_id(item.task, item.seed_id, user), run_id=self.run_id, task_type=item.task,  # type: ignore[arg-type]
            construction=construction,  # type: ignore[arg-type]
            seed=Seed(kind=item.seed_kind, id=item.seed_id, doc_id=item.doc_id, chunk_id=chunk_id),  # type: ignore[arg-type]
            context_chunk_ids=[c.chunk_id for c in context],
            source_uris=sorted({seed_uri, *(c.source_uri for c in context)}),
            licenses=sorted({seed_lic, *(c.license_spdx for c in context)}),
            split=item.split,  # type: ignore[arg-type]
            generation=generation, pipeline=self.pipeline, created_at=utc_now_iso(),
            messages=[Message(role="user", content=user.strip()), Message(role="assistant", content=assistant.strip())],
        )

    async def _grounded_answer(self, prompt_id: str, question: str, context_text: str) -> tuple[str | None, Any]:
        g = self.s.generate
        text, call = await self.llm.chat(self.s.answer_gen_model, load_prompt(prompt_id),  # type: ignore[arg-type]
                                         temperature=g.answer_temperature, max_tokens=g.max_answer_tokens,
                                         system_prompt=g.system_prompt, context=context_text, question=question)
        return (None if INSUFFICIENT in text else text), call

    async def _question(self, prompt_id: str, **fmt: Any) -> tuple[str | None, Any]:
        q, call = await self.llm.chat_json(self.s.question_gen_model, load_prompt(prompt_id), _Question,  # type: ignore[arg-type]
                                           temperature=self.s.generate.question_temperature, max_tokens=400, **fmt)
        return (q.question.strip() if q.question else None), call

    # ------------------------------------------------------------------ #
    async def build(self, item: PlanItem) -> tuple[SFTCandidate | None, str]:
        e, c = item.example, item.chunk
        if item.task == "write_code":
            assert e
            q, qcall = await self._question("question.write_code", intro=e.intro, notes=e.notes, code=e.code)
            if not q:
                return None, "no_question"
            ctx = await self.retrieve(q, e.split, exclude={self.chunk_by_example.get(e.example_id, "")})
            ans, acall = await self._grounded_answer("answer.grounded", q, "\n\n---\n\n".join(
                [example_context(e), *(x.embed_text for x in ctx)]))
            if not ans:
                return None, "insufficient_context"
            return self._candidate(item, "llm_grounded", q, ans, Generation(question=qcall, answer=acall), ctx), "ok"

        if item.task == "explain_code":
            assert e
            tmpl = EXPLAIN_TEMPLATES[int(sha256_text(e.example_id)[:4], 16) % len(EXPLAIN_TEMPLATES)]
            q = tmpl.format(code=e.code)
            ans, acall = await self._grounded_answer("answer.explain_code", q, example_context(e))
            if not ans:
                return None, "insufficient_context"
            return self._candidate(item, "llm_grounded", q, ans, Generation(answer=acall), []), "ok"

        if item.task in ("concept", "api_usage"):
            assert c
            q, qcall = await self._question(f"question.{item.task}", heading=c.context_header, text=c.text)
            if not q:
                return None, "no_question"
            ctx = [c, *await self.retrieve(q, c.split, exclude={c.chunk_id})][: self.s.generate.top_k]
            ans, acall = await self._grounded_answer("answer.grounded", q, "\n\n---\n\n".join(x.embed_text for x in ctx))
            if not ans:
                return None, "insufficient_context"
            return self._candidate(item, "llm_grounded", q, ans, Generation(question=qcall, answer=acall), ctx), "ok"

        if item.task == "convert_from_language":
            assert e
            langs = self.s.generate.source_languages
            lang = langs[int(sha256_text(e.example_id)[:4], 16) % len(langs)]
            text, call = await self.llm.chat(self.s.answer_gen_model, load_prompt("backtranslate"),  # type: ignore[arg-type]
                                             temperature=0.2, max_tokens=self.s.generate.max_answer_tokens,
                                             language=lang, code=e.code)
            foreign = first_fence(text)
            if not foreign:
                return None, "no_code_from_backtranslation"
            q = f"Convert this {lang} code to Ballerina:\n\n```{lang.lower()}\n{foreign}\n```"
            intro = f"\n\n{e.intro.strip()}" if e.intro.strip() else ""
            a = f"Here's the equivalent Ballerina code:\n\n```ballerina\n{e.code.strip()}\n```{intro}"
            return self._candidate(item, "back_translation", q, a, Generation(question=call), []), "ok"

        if item.task == "complete_code":
            assert e
            res = remove_function_body(e.code)
            if not res:
                return None, "no_function_body"
            skeleton, fn = res
            hint = f" {e.intro.strip().split(chr(10))[0]}" if e.intro.strip() else ""
            q = f"Complete the `{fn}` function in this Ballerina code.{hint}\n\n```ballerina\n{skeleton}\n```"
            a = f"Here is the completed code:\n\n```ballerina\n{e.code.strip()}\n```"
            if e.notes.strip():
                a += f"\n\n{e.notes.strip()}"
            return self._candidate(item, "seed_verified", q, a, Generation(), []), "ok"

        if item.task == "debug_fix":
            assert e and self.compiler
            for mutant in candidate_mutants(e.code, int(sha256_text(e.example_id)[:4], 16)):
                outcome = await asyncio.to_thread(self.compiler.compile, mutant.code)
                if outcome.status != "error" or not outcome.errors:
                    continue
                err = outcome.errors[0]
                loc = f"line {err['line']}: " if err.get("line") else ""
                q = (f"My Ballerina code fails to compile with this error:\n\n```\n{loc}{err['message']}\n```\n\n"
                     f"```ballerina\n{mutant.code.strip()}\n```\n\nHow do I fix it?")
                a = f"{mutant.explanation}\n\nHere is the corrected code:\n\n```ballerina\n{e.code.strip()}\n```"
                gen = Generation(mutation=Mutation(kind=mutant.kind, original_example_id=e.example_id,
                                                   compiler_error=err["message"]))
                return self._candidate(item, "mutation", q, a, gen, []), "ok"
            return None, "no_failing_mutation"

        return None, f"unknown_task:{item.task}"


def _resolve_run(paths: Paths, run_id: str | None, inputs: list[Path]) -> tuple[str, Path]:
    if run_id:
        return run_id, paths.sft_run(run_id)
    resumable = find_resumable(paths.sft, "generate", inputs)
    if resumable:
        return resumable.name, resumable
    rid = new_run_id("generate")
    return rid, paths.sft_run(rid)


def run(settings: Settings, *, limit: int | None = None, run_id: str | None = None, dry_run: bool = False) -> dict:
    paths = Paths(settings)
    inputs = [paths.code_examples, paths.chunks]
    rid, run_dir = _resolve_run(paths, run_id, inputs)
    examples = list(read_models(paths.code_examples, CodeExample))
    chunks = {c.chunk_id: c for c in read_models(paths.chunks, Chunk)}

    compiler = None
    if settings.validate_.enabled:
        from sftgen.validate.bal_compiler import BalCompiler, BalUnavailable

        try:
            compiler = BalCompiler(settings.validate_, paths.cache / "compile")
        except BalUnavailable:
            compiler = None
    plan = build_plan(settings, examples, list(chunks.values()), can_compile=compiler is not None)
    if limit:
        plan = plan[:limit]

    if dry_run:
        from collections import Counter

        summary = {"run_id": rid, "planned": len(plan), "by_task": dict(Counter(p.task for p in plan)),
                   "by_split": dict(Counter(p.split for p in plan))}
        get_logger(__name__, stage="generate").info("dry run", extra={"event": "generate.dry_run", **summary})
        return summary

    settings.require_llm()
    out_path = run_dir / "candidates.jsonl"
    with StageRun(settings, "generate", run_dir, inputs=inputs, run_id=rid, resumable=True) as stage:
        log = get_logger(__name__, run_id=rid, stage="generate")
        done = {f"{r['task_type']}:{r['seed']['id']}" for r in (read_jsonl(out_path) if out_path.exists() else [])}
        todo = [p for p in plan if p.key not in done]
        stage.count("planned", len(plan))
        stage.count("skipped_existing", len(plan) - len(todo))
        gen = Generator(settings, rid, chunks, compiler)
        log.info("generating", extra={"event": "generate.start", "todo": len(todo), "out": str(out_path)})

        async def _main() -> None:
            with JsonlAppender(out_path) as out:
                async def one(item: PlanItem) -> None:
                    try:
                        cand, reason = await gen.build(item)
                    except (LLMError, Exception) as exc:  # noqa: BLE001 - item-level failure must not stop the run
                        stage.error(item.key, exc)
                        return
                    if cand is None:
                        stage.reject(reason)
                        return
                    out.write(cand)
                    stage.count(f"written_{item.task}")
                    if out.count % 25 == 0:
                        stage.checkpoint()

                await asyncio.gather(*(one(p) for p in todo))

        asyncio.run(_main())
        stage.add_output(out_path)
        log.info("generate finished", extra={"event": "generate.done", "run_dir": str(run_dir)})
        return {"run_id": rid, **dict(stage.counts)}
