"""
RAG-Grounded Synthetic SFT Dataset Generator
==============================================

Pipeline: source docs -> question generation -> retrieval -> grounded answer
generation -> verification/filtering -> SFT-ready JSONL.

Designed for generating a Ballerina-language SFT dataset, but generic to any
domain corpus. Uses the OpenAI API for generation; swap `call_llm()` for
Anthropic, any OpenAI-compatible endpoint (vLLM, Together, etc.), or a local
model if needed.

Env:
  export OPENAI_API_KEY=sk-...
"""

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
from tqdm import tqdm
from openai import OpenAI

client = OpenAI(api_key=API_KEY)


#
# LLM call wrapper — swap this for whichever "big LLM" you're using
#
def call_llm(prompt: str, system: str = "", max_tokens: int = 1024, model: str = ANSWER_GEN_MODEL) -> str:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    )
    return resp.choices[0].message.content or ""


# ---------------------------------------------------------------------------
# 0. Data structures
# ---------------------------------------------------------------------------
@dataclass
class SFTExample:
    instruction: str
    output: str
    source_chunk_ids: list[str] = field(default_factory=list)
    verification: dict = field(default_factory=dict)


#
# 1. Indexing (same as your RAG index, reused here)
#
class CorpusIndex:
    def __init__(self, collection_name=CHROMADB_COLLECTION_NAME, embed_model=EMBEDDING_MODEL) -> None:
        self.collection_name = collection_name
        self.embedder = SentenceTransformer(embed_model)
        self.client = chromadb.PersistentClient(path="./chroma_db")
        self.collection = self.client.get_or_create_collection(self.collection_name)

    def reset(self):
        """Wipe this collection. IMPORTANT: chroma_db is persisted to disk
        across runs — if you re-run ingestion and don't see the record
        count you expect, stale chunks from a previous run are almost
        always the cause. Call this before re-ingesting from scratch."""
        self.client.delete_collection(self.collection_name)
        self.collection = self.client.get_or_create_collection(self.collection_name)

    def count(self) -> int:
        return self.collection.count()

    def add_chunks(self, chunks: list[dict]):
        """chunks: [{"id": ..., "text": ..., "metadata": {...}}]"""
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
# 2. Stage 1 — Question generation FROM the source chunk
#    (grounds question generation the same way we ground answering)
#
def generate_questions(chunk: dict, n: int = 3) -> list[str]:
    raw = call_llm(QUESTION_GEN_PROMPT.format(n=n, chunk=chunk["text"]), model=QUESTION_GEN_MODEL)
    try:
        questions = json.loads(_extract_json(raw))
        return [q.strip() for q in questions if isinstance(q, str) and q.strip()]
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_json(text: str) -> str:
    """Strip markdown code fences if the model wraps its JSON output."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    return match.group(0) if match else text


#
# 3. Stage 2 + 3 — Retrieve context for the question, then answer grounded
#    in ONLY that context (this is the key fix for "LLM doesn't know this")
#
def generate_grounded_answer(index: CorpusIndex, question: str, top_k: int = 4) -> Optional[SFTExample]:
    retrieved = index.retrieve(question, top_k=top_k)
    context = "\n\n---\n\n".join(r["text"] for r in retrieved)

    answer = call_llm(ANSWER_GEN_PROMPT.format(context=context, question=question), model=ANSWER_GEN_MODEL)

    if "INSUFFICIENT_CONTEXT" in answer:
        return None  # LLM itself flagged it doesn't have grounding — drop it

    # return SFTExample(
    #     instruction=question,
    #     output=answer,
    #     source_chunk_ids=[r["id"] for r in retrieved],
    # )

    example = SFTExample(
        instruction=question,
        output=answer,
        source_chunk_ids=[r["id"] for r in retrieved],
    )
    example.verification["generation_mode"] = "corpus_grounded"
    return example


#
# 4. Stage 4 — Verification / filtering
#
def check_faithfulness(index: CorpusIndex, example: SFTExample) -> bool:
    context = "\n\n".join(
        index.collection.get(ids=[cid])["documents"][0] for cid in example.source_chunk_ids
    )
    verdict = call_llm(FAITHFULNESS_PROMPT.format(context=context, answer=example.output), model=JUDGE_MODEL)
    is_faithful = verdict.strip().upper().startswith("FAITHFUL")
    example.verification["faithfulness"] = verdict.strip()
    return is_faithful


def extract_code_blocks(text: str) -> list[str]:
    return re.findall(r"```(?:ballerina)?\n(.*?)```", text, re.DOTALL)


def check_code_compiles(example: SFTExample, ballerina_bin: str = "bal") -> bool:
    """Compile-check any Ballerina code blocks in the answer.
    Requires the Ballerina CLI installed and on PATH."""
    code_blocks = extract_code_blocks(example.output)
    if not code_blocks:
        example.verification["compiled"] = "n/a (no code)"
        return True  # nothing to check, don't penalize non-code answers

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


def _exact_dedup(examples: list[SFTExample]) -> list[SFTExample]:
    """Fast pre-filter: drop exact (instruction, output) string duplicates
    before running the more expensive embedding-based near-dup pass."""
    seen = set()
    keep = []
    for ex in examples:
        key = hashlib.md5(f"{ex.instruction.strip()}|{ex.output.strip()}".encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            keep.append(ex)
    return keep


# def deduplicate(examples: list[SFTExample], embedder: SentenceTransformer, threshold: float = 0.92) -> list[SFTExample]:
#     """Drop near-duplicate questions using cosine similarity on embeddings."""
#
#     if not examples:
#         return []
#     embs = embedder.encode([e.instruction for e in examples])
#     embs = embs / np.linalg.norm(embs, axis=1, keepdims=True)
#     keep = []
#     kept_embs = []
#     for i, emb in enumerate(embs):
#         if not kept_embs or max(emb @ k for k in kept_embs) < threshold:
#             keep.append(examples[i])
#             kept_embs.append(emb)
#     return keep

def deduplicate(
        examples: list[SFTExample],
        embedder: SentenceTransformer,
        question_threshold: float = 0.92,
        answer_threshold: float = 0.95,
) -> list[SFTExample]:
    """Two-stage dedup:
      1. Exact-match on (instruction, output) — cheap, catches perfect dupes.
      2. Near-duplicate filtering on questions AND answers separately — an
         example is dropped if either its question is near-duplicate to a
         kept question, OR its answer is near-duplicate to a kept answer
         (the latter catches cases where different phrasings collapsed to
         the same generated answer, often a sign of an overlapping/thin
         source chunk rather than a genuinely distinct training example).
    """
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
# 5. Full pipeline
#
def build_sft_dataset(index: CorpusIndex, questions_per_chunk: int = 3, require_code_check: bool = True,
                      output_path: str = "sft_dataset.jsonl", ):
    all_chunks = index.get_all_chunks()
    candidates: list[SFTExample] = []

    print(f"Generating questions from {len(all_chunks)} chunks...")
    for chunk in tqdm(all_chunks):
        questions = generate_questions(chunk, n=questions_per_chunk)
        for q in questions:
            example = generate_grounded_answer(index, q)
            if example is not None:
                candidates.append(example)

    candidates_with_code = sum(1 for c in candidates if extract_code_blocks(c.output))
    print(
        f"Generated {len(candidates)} candidate examples "
        f"({candidates_with_code} contain a fenced code block). Verifying..."
    )
    verified = []
    dropped_faithfulness = dropped_compile = 0
    for ex in tqdm(candidates):
        if not check_faithfulness(index, ex):
            dropped_faithfulness += 1
            continue
        if require_code_check and not check_code_compiles(ex):
            dropped_compile += 1
            continue
        verified.append(ex)

    verified_with_code = sum(1 for e in verified if extract_code_blocks(e.output))
    print(
        f"{len(verified)} passed verification "
        f"({verified_with_code} contain a fenced code block). "
        f"Dropped: {dropped_faithfulness} failed faithfulness, "
        f"{dropped_compile} failed compile check. Deduplicating..."
    )

    final = deduplicate(verified, index.embedder)
    final_with_code = sum(1 for e in final if extract_code_blocks(e.output))

    print(
        f"Final dataset size: {len(final)} "
        f"({final_with_code} contain a fenced code block). Writing to {output_path}..."
    )
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
if __name__ == "__main__":
    index = CorpusIndex()
    index.reset()

    # Index your Ballerina docs / stdlib source / spec here
    # index.add_chunks([
    #     {
    #         "id": "bal_services_1",
    #         "text": "In Ballerina, a service is a first-class construct for exposing "
    #                 "network-accessible APIs. A service declaration groups related "
    #                 "resource functions, each mapped to an HTTP method and path...",
    #         "metadata": {"source": "ballerina.io/learn/services"},
    #     },
    #     {
    #         "id": "bal_services_2",
    #         "text": "The Client is used to connect to and interact with HTTP endpoints. They support connection pooling and can be configured to have a maximum number of active connections that can be made with the remote endpoint. The Client activates connection eviction after a given idle period and also supports follow-redirects so that you do not have to manually handle 3xx HTTP status codes.",
    #         "metadata": {"source": "https://central.ballerina.io/ballerina/http/2.16.6"},
    #     },
    #     # ... load your full corpus here (from GCS)
    # ])

    # Load your crawled corpus from GCS, extracted + chunked + deduped.
    # See ingest_corpus.py for the html/code extraction and chunking logic.
    # chunk_batch = []
    # for chunk in build_chunks_from_gcs(bucket_name=BUCKET_NAME, prefix="ballerina/"):
    #     chunk_batch.append(chunk)
    #     if len(chunk_batch) >= 256:  # batch embedding calls
    #         index.add_chunks(chunk_batch)
    #         chunk_batch = []
    # if chunk_batch:
    #     index.add_chunks(chunk_batch)

    # Google Cloud storage
    # chunk_source = build_chunks_from_gcs(bucket_name="your-crawler-bucket", prefix="ballerina/"):

    # Local Data
    chunk_source = build_chunks_from_local_jsonl("../../data/data.jsonl")

    chunk_batch = []
    for chunk in chunk_source:
        chunk_batch.append(chunk)
        if len(chunk_batch) >= 256:  # batch embedding calls
            index.add_chunks(chunk_batch)
            chunk_batch = []
    if chunk_batch:
        index.add_chunks(chunk_batch)

    print(f"Indexed {index.count()} chunks total.")
    build_sft_dataset(index, questions_per_chunk=3, output_path="../../data/supervised/sft-dataset.jsonl")
