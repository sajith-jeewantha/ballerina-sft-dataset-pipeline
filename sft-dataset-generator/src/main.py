"""
RAG-Grounded Synthetic SFT Dataset Generator (async, corrected client)
========================================================================
Changes from your last version:
  1. CLIENT FIX: uses openai.AsyncOpenAI pointed at OpenRouter's base_url,
     NOT the `openrouter` package's OpenRouter class (which only has
     `.chat.send()`, not `.chat.completions.create()` — this was the
     actual cause of every single chunk failing across 3 runs).
  2. Concurrency: question-gen and answer-gen now run concurrently via
     asyncio + a semaphore, instead of one chunk at a time.

TEST THIS FILE ON A TINY SLICE BEFORE RUNNING THE FULL 582-CHUNK BATCH.
See the isolated sanity check at the bottom of this message first.
"""

import asyncio
import hashlib
import json
import re
import subprocess
import tempfile
import os
import numpy as np
import chromadb

from src.env_loader import API_KEY, QUESTION_GEN_MODEL, ANSWER_GEN_MODEL, JUDGE_MODEL, CHROMADB_PATH, EMBEDDING_MODEL, \
    CHROMADB_COLLECTION_NAME, BUCKET_NAME
from prompt_temple import QUESTION_GEN_PROMPT, ANSWER_GEN_PROMPT, FAITHFULNESS_PROMPT
from dataclasses import dataclass, field
from typing import Optional
from ingest_corpus import build_chunks_from_gcs, build_chunks_from_local_jsonl
from sentence_transformers import SentenceTransformer
from tqdm.asyncio import tqdm_asyncio
from openai import AsyncOpenAI

# --- THE FIX: standard OpenAI SDK pointed at OpenRouter, not the `openrouter` package ---
client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=API_KEY)

# tune this to your OpenRouter rate limit / desired throughput
SEM = asyncio.Semaphore(10)


async def call_llm(prompt: str, system: str = "", max_tokens: int = 2048, model: str = ANSWER_GEN_MODEL,
                    max_retries: int = 3) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_error = None
    async with SEM:
        for attempt in range(max_retries):
            try:
                resp = await client.chat.completions.create(
                    model=model,
                    max_tokens=max_tokens,
                    messages=messages,
                    extra_body={"reasoning": {"effort": "none"}},
                )
                text = resp.choices[0].message.content or ""
                if text.strip():
                    return text
                last_error = f"empty content (finish_reason={resp.choices[0].finish_reason})"
            except Exception as e:
                last_error = str(e)
            await asyncio.sleep(2 ** attempt)

    raise ValueError(f"Empty response from model {model} after {max_retries} retries — last error: {last_error}")


#
# 0. Data structures
#
@dataclass
class SFTExample:
    instruction: str
    output: str
    source_chunk_ids: list[str] = field(default_factory=list)
    verification: dict = field(default_factory=dict)


#
# 1. Indexing
#
class CorpusIndex:
    def __init__(self, collection_name=CHROMADB_COLLECTION_NAME, embed_model=EMBEDDING_MODEL) -> None:
        self.collection_name = collection_name
        self.embedder = SentenceTransformer(embed_model)
        db_path = CHROMADB_PATH or "./chroma_db"
        self.client = chromadb.PersistentClient(path=db_path)
        self.collection = self.client.get_or_create_collection(self.collection_name)

    def reset(self):
        self.client.delete_collection(self.collection_name)
        self.collection = self.client.get_or_create_collection(self.collection_name)

    def count(self) -> int:
        return self.collection.count()

    def add_chunks(self, chunks: list[dict]):
        embeddings = self.embedder.encode([c["text"] for c in chunks]).tolist()
        self.collection.add(
            ids=[c["id"] for c in chunks],
            embeddings=embeddings,
            documents=[c["text"] for c in chunks],
            metadatas=[c.get("metadata", {}) for c in chunks],
        )

    def retrieve(self, query: str, top_k: int = 4) -> list[dict]:
        q_emb = self.embedder.encode([query]).tolist()
        res = self.collection.query(query_embeddings=q_emb, n_results=top_k)
        return [
            {"id": i, "text": d, "metadata": m}
            for i, d, m in zip(res["ids"][0], res["documents"][0], res["metadatas"][0])
        ]

    def get_all_chunks(self) -> list[dict]:
        res = self.collection.get()
        return [
            {"id": i, "text": d, "metadata": m}
            for i, d, m in zip(res["ids"], res["documents"], res["metadatas"])
        ]


#
# 2. Stage 1 — question generation (async)
#
async def generate_questions(chunk: dict, n: int = 3) -> list[str]:
    try:
        raw = await call_llm(QUESTION_GEN_PROMPT.format(n=n, chunk=chunk["text"]), model=QUESTION_GEN_MODEL)
    except ValueError as e:
        print(f"  SKIPPED chunk {chunk['id']}: {e}")
        return []
    try:
        questions = json.loads(_extract_json(raw))
        return [q.strip() for q in questions if isinstance(q, str) and q.strip()]
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_json(text: str) -> str:
    match = re.search(r"\[.*\]", text, re.DOTALL)
    return match.group(0) if match else text


#
# 3. Stage 2+3 — grounded answer generation (async)
#
async def generate_grounded_answer(index: CorpusIndex, question: str, top_k: int = 4) -> Optional[SFTExample]:
    retrieved = index.retrieve(question, top_k=top_k)  # sync — chromadb has no async client
    context = "\n\n---\n\n".join(r["text"] for r in retrieved)

    try:
        answer = await call_llm(ANSWER_GEN_PROMPT.format(context=context, question=question), model=ANSWER_GEN_MODEL)
    except ValueError as e:
        print(f"  SKIPPED question {question[:60]!r}: {e}")
        return None

    if not answer.strip() or "INSUFFICIENT_CONTEXT" in answer:
        return None

    example = SFTExample(
        instruction=question,
        output=answer,
        source_chunk_ids=[r["id"] for r in retrieved],
    )
    example.verification["generation_mode"] = "corpus_grounded"
    return example


#
# 3b. Per-chunk pipeline: questions -> answers, all concurrent
#
async def process_chunk(index: CorpusIndex, chunk: dict, n: int) -> list[SFTExample]:
    questions = await generate_questions(chunk, n=n)
    if not questions:
        return []
    answers = await asyncio.gather(*[generate_grounded_answer(index, q) for q in questions])
    return [a for a in answers if a is not None]


#
# 4. Verification (async)
#
async def check_faithfulness(index: CorpusIndex, example: SFTExample) -> bool:
    context = "\n\n".join(
        index.collection.get(ids=[cid])["documents"][0] for cid in example.source_chunk_ids
    )
    try:
        verdict = await call_llm(FAITHFULNESS_PROMPT.format(context=context, answer=example.output), model=JUDGE_MODEL)
    except ValueError as e:
        print(f"  SKIPPED faithfulness check: {e}")
        return False
    is_faithful = verdict.strip().upper().startswith("FAITHFUL")
    example.verification["faithfulness"] = verdict.strip()
    return is_faithful


def extract_code_blocks(text: str) -> list[str]:
    return re.findall(r"```(?:ballerina)?\n(.*?)```", text, re.DOTALL)


def check_code_compiles(example: SFTExample, ballerina_bin: str = "bal") -> bool:
    code_blocks = extract_code_blocks(example.output)
    if not code_blocks:
        example.verification["compiled"] = "n/a (no code)"
        return True

    all_ok = True
    for i, code in enumerate(code_blocks):
        with tempfile.TemporaryDirectory() as tmp:
            src_path = os.path.join(tmp, "main.bal")
            with open(src_path, "w") as f:
                f.write(code)
            result = subprocess.run(
                [ballerina_bin, "build", src_path],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                all_ok = False
                example.verification[f"compile_error_{i}"] = result.stderr[:500]
    example.verification["compiled"] = all_ok
    return all_ok


async def verify_example(index: CorpusIndex, ex: SFTExample, require_code_check: bool) -> Optional[SFTExample]:
    if not await check_faithfulness(index, ex):
        return None
    if require_code_check and not check_code_compiles(ex):  # sync — subprocess, no async win here
        return None
    return ex


#
# Dedup (unchanged, sync — local embedding compute, not API-bound)
#
def _exact_dedup(examples: list[SFTExample]) -> list[SFTExample]:
    seen = set()
    keep = []
    for ex in examples:
        key = hashlib.md5(f"{ex.instruction.strip()}|{ex.output.strip()}".encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            keep.append(ex)
    return keep


def deduplicate(examples, embedder, question_threshold=0.92, answer_threshold=0.95):
    if not examples:
        return []
    examples = _exact_dedup(examples)
    q_embs = embedder.encode([e.instruction for e in examples])
    a_embs = embedder.encode([e.output for e in examples])
    q_embs = q_embs / np.linalg.norm(q_embs, axis=1, keepdims=True)
    a_embs = a_embs / np.linalg.norm(a_embs, axis=1, keepdims=True)

    keep = []
    kept_q_embs, kept_a_embs = [], []
    for i in range(len(examples)):
        q_dup = kept_q_embs and max(q_embs[i] @ k for k in kept_q_embs) >= question_threshold
        a_dup = kept_a_embs and max(a_embs[i] @ k for k in kept_a_embs) >= answer_threshold
        if not q_dup and not a_dup:
            keep.append(examples[i])
            kept_q_embs.append(q_embs[i])
            kept_a_embs.append(a_embs[i])
    return keep


#
# 5. Full pipeline (async)
#
async def build_sft_dataset(index: CorpusIndex, questions_per_chunk: int = 3, require_code_check: bool = True,
                             output_path: str = "sft_dataset.jsonl"):
    all_chunks = index.get_all_chunks()

    print(f"Generating questions + answers for {len(all_chunks)} chunks (concurrent, sem={SEM._value})...")
    chunk_results = await tqdm_asyncio.gather(
        *[process_chunk(index, c, questions_per_chunk) for c in all_chunks]
    )
    candidates: list[SFTExample] = [ex for sub in chunk_results for ex in sub]

    candidates_with_code = sum(1 for c in candidates if extract_code_blocks(c.output))
    print(f"Generated {len(candidates)} candidate examples ({candidates_with_code} with code). Verifying...")

    verify_results = await tqdm_asyncio.gather(
        *[verify_example(index, ex, require_code_check) for ex in candidates]
    )
    verified = [ex for ex in verify_results if ex is not None]
    dropped = len(candidates) - len(verified)

    verified_with_code = sum(1 for e in verified if extract_code_blocks(e.output))
    print(f"{len(verified)} passed verification ({verified_with_code} with code). Dropped: {dropped}. Deduplicating...")

    final = deduplicate(verified, index.embedder)
    final_with_code = sum(1 for e in final if extract_code_blocks(e.output))

    print(f"Final dataset size: {len(final)} ({final_with_code} with code). Writing to {output_path}...")
    with open(output_path, "w") as f:
        for ex in final:
            f.write(json.dumps({
                "instruction": ex.instruction,
                "output": ex.output,
                "source_chunk_ids": ex.source_chunk_ids,
                "generation_mode": ex.verification.get("generation_mode", "unknown"),
            }) + "\n")

    return final


#
# Example usage
#
async def main():
    index = CorpusIndex()
    index.reset()

    chunk_source = build_chunks_from_local_jsonl("../../data/scraper/ballerina_corpus.jsonl")

    chunk_batch = []
    for chunk in chunk_source:
        chunk_batch.append(chunk)
        if len(chunk_batch) >= 256:
            index.add_chunks(chunk_batch)
            chunk_batch = []
    if chunk_batch:
        index.add_chunks(chunk_batch)

    print(f"Indexed {index.count()} chunks total.")
    await build_sft_dataset(index, questions_per_chunk=3, output_path="../../data/supervised/sft-dataset.jsonl")


if __name__ == "__main__":
    asyncio.run(main())