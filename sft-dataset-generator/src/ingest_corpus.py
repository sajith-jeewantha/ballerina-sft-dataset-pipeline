"""
GCS Corpus Ingestion
=====================

Bridges the web-crawler's raw output (stored in GCS) into chunk dicts ready
for CorpusIndex.add_chunks() in main.py.

Handles:
  - Reading records from GCS (supports either one-JSON-object-per-blob, or
    newline-delimited JSON files)
  - Deduplication via the crawler's own `content_hash`
  - Content extraction per file_type:
        html      -> boilerplate-stripped main text (trafilatura)
        markdown  -> lightly cleaned as-is
        bal (code)-> kept as raw source, chunked on syntactic boundaries
  - Routing each extracted text to the right chunker (prose vs. code)

Install:
  pip install google-cloud-storage trafilatura beautifulsoup4 --break-system-packages
"""

import hashlib
import json
import os
import re
from typing import Iterator, Optional

from google.cloud import storage
import trafilatura


#
# 1. Read raw records from GCS
#

def iter_gcs_records(bucket_name: str, prefix: str = "") -> Iterator[dict]:
    """Yields each crawled record as a dict. Supports both:
      - one JSON object per blob (most crawlers write one file per page)
      - newline-delimited JSON (.jsonl) blobs
    """
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    for blob in bucket.list_blobs(prefix=prefix):
        if not (blob.name.endswith(".json") or blob.name.endswith(".jsonl")):
            continue
        raw = blob.download_as_text()

        if blob.name.endswith(".jsonl"):
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    yield json.loads(line)
        else:
            yield json.loads(raw)


def iter_local_jsonl_records(path: str) -> Iterator[dict]:
    """Local-file equivalent of iter_gcs_records, for testing without GCS.

    Robust to several real-world shapes, not just strict one-object-per-line
    JSONL:
      - strict JSONL (one compact object per line)
      - pretty-printed objects spanning multiple lines, with or without
        trailing commas between them (e.g. crawler output written with
        json.dumps(record, indent=2) + ",\\n")
      - the whole file wrapped in a top-level JSON array
    It works by streaming raw JSON values out of the file directly, rather
    than assuming line boundaries mean anything.
    """
    abs_path = os.path.abspath(path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(
            f"data.jsonl not found at {abs_path}. "
            f"Check you're running from the right directory, or pass an absolute path."
        )

    with open(abs_path, "r", encoding="utf-8") as f:
        raw = f.read()

    stripped = raw.strip()
    if not stripped:
        print(f"WARNING: {abs_path} exists but is empty.")
        return

    decoder = json.JSONDecoder()
    idx = 0
    n = len(stripped)
    count = 0
    skip_chars = " \t\r\n,[]"  # whitespace, separators, and optional array brackets

    while idx < n:
        while idx < n and stripped[idx] in skip_chars:
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(stripped, idx)
        except json.JSONDecodeError as e:
            print(
                f"Stopped parsing at character {idx} of {abs_path}: {e}. "
                f"Nearby content: {stripped[idx:idx + 80]!r}"
            )
            break
        yield obj
        count += 1
        idx = end

    if count == 0:
        print(
            f"WARNING: no JSON objects could be parsed from {abs_path}. "
            f"First 200 chars: {stripped[:200]!r}"
        )


#
# 2. Deduplicate by content_hash (your crawler already computes this)
#

class HashDeduper:
    def __init__(self):
        self._seen: set[str] = set()

    def is_duplicate(self, content_hash: str) -> bool:
        if content_hash in self._seen:
            return True
        self._seen.add(content_hash)
        return False


#
# 3. Extract clean text per file_type
#

def extract_text(record: dict) -> Optional[str]:
    content = record.get("content", "")
    if not content or not content.strip():
        return None

    file_type = record.get("file_type", "").lower()

    if file_type == "html":
        extracted = trafilatura.extract(
            content,
            include_comments=False,
            include_tables=True,
            favor_recall=True,  # Ballerina docs often have short pages; don't over-trim
        )
        return extracted.strip() if extracted else None

    if file_type in ("md", "markdown"):
        # Strip markdown link/image syntax noise but keep code fences intact
        cleaned = re.sub(r"!\[.*?\]\(.*?\)", "", content)  # images
        return cleaned.strip()

    if file_type in ("bal", "ballerina", "txt", "code"):
        # Raw source — keep as-is, don't touch (chunking happens separately)
        return content.strip()

    # Unknown file_type — fall back to raw content, better than dropping it
    return content.strip()


#
# 4. Chunkers — prose vs. code need different strategies
#

def chunk_prose(text: str, chunk_size: int = 500, overlap: int = 50) -> list[str]:
    """Sliding-window word chunker for docs/prose (same as rag_example.py)."""
    words = text.split()
    chunks = []
    step = chunk_size - overlap
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i + chunk_size])
        if chunk:
            chunks.append(chunk)
    return chunks


# Matches top-level Ballerina constructs to chunk on syntactic boundaries
# rather than an arbitrary word count.
BAL_BOUNDARY_RE = re.compile(
    r"^(public\s+)?(function|service|class|type|configurable|listener)\b",
    re.MULTILINE,
)

def chunk_ballerina_code(code: str, max_chunk_chars: int = 2500) -> list[str]:
    """Chunk Ballerina source on function/service/type boundaries.
    Falls back to char-based splitting if a single construct is very large,
    or if no boundaries are found (e.g. a snippet with no top-level decls)."""
    boundaries = [m.start() for m in BAL_BOUNDARY_RE.finditer(code)]

    if not boundaries:
        return _char_split(code, max_chunk_chars)

    chunks = []
    for i, start in enumerate(boundaries):
        end = boundaries[i + 1] if i + 1 < len(boundaries) else len(code)
        segment = code[start:end].strip()
        if not segment:
            continue
        if len(segment) > max_chunk_chars:
            chunks.extend(_char_split(segment, max_chunk_chars))
        else:
            chunks.append(segment)

    # Keep any leading imports/module-level content before the first boundary
    if boundaries[0] > 0:
        header = code[:boundaries[0]].strip()
        if header:
            chunks.insert(0, header)

    return chunks


def _char_split(text: str, max_chars: int) -> list[str]:
    return [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [text]


def chunk_content(text: str, file_type: str) -> list[str]:
    if file_type in ("bal", "ballerina"):
        return chunk_ballerina_code(text)
    return chunk_prose(text)


#
# 4b. Relevance filtering — keeps the corpus from filling up with content
#     that merely mentions Ballerina in passing (comparison listicles,
#     cookie/privacy policy pages, general "top languages" blog posts).
#

# Pages matching these URL patterns are almost never useful training signal
# for a Ballerina coding assistant, regardless of what they say.
IRRELEVANT_URL_PATTERNS = re.compile(
    r"(privacy-policy|cookie-policy|terms-of-(use|service)|/legal/|/careers/)",
    re.IGNORECASE,
)

# A crude but effective density check: real Ballerina docs/code reference
# these terms constantly; generic "top programming languages" blog posts
# that mention Ballerina once or twice among many other languages don't.
BALLERINA_SIGNAL_RE = re.compile(
    r"\b(ballerina|bal:|http:Listener|http:Client|resource function|"
    r"service\s*/|import ballerina)\b",
    re.IGNORECASE,
)

def is_relevant_chunk(text: str, url: str, file_type: str, min_signal_density: float = 0.003) -> bool:
    """Heuristic relevance filter. Always keeps real Ballerina code
    (file_type == 'bal'). For prose/html, requires a minimum density of
    Ballerina-specific terms so generic listicle/comparison content that
    only name-drops Ballerina gets excluded."""
    if IRRELEVANT_URL_PATTERNS.search(url):
        return False

    if file_type in ("bal", "ballerina"):
        return True  # actual source code is always relevant

    signal_hits = len(BALLERINA_SIGNAL_RE.findall(text))
    density = signal_hits / max(len(text.split()), 1)
    return density >= min_signal_density


def has_code_block(text: str) -> bool:
    """Detects whether a prose/html chunk contains an embedded code sample
    (e.g. a Ballerina snippet inside a doc page), so question generation
    can be biased toward code-writing questions for these chunks."""
    return bool(re.search(r"```(?:ballerina)?\n.*?```", text, re.DOTALL)) or \
           bool(re.search(r"\b(resource function|service\s*/|import ballerina)\b", text))


#
# 5. Full ingestion pipeline: GCS record -> chunk dicts for CorpusIndex
#

def _process_records(records: Iterator[dict]) -> Iterator[dict]:
    """Shared core: dedupe, extract, filter for relevance, chunk. Used by
    both the GCS and local entry points so the two stay in sync."""
    deduper = HashDeduper()
    skipped_dupe = skipped_empty = skipped_extract = skipped_irrelevant = 0
    total = 0

    for record in records:
        total += 1
        content_hash = record.get("content_hash", "")
        url = record.get("url", "")

        if content_hash and deduper.is_duplicate(content_hash):
            skipped_dupe += 1
            continue

        text = extract_text(record)
        if text is None:
            skipped_extract += 1
            continue

        file_type = record.get("file_type", "").lower()

        if not is_relevant_chunk(text, url, file_type):
            skipped_irrelevant += 1
            continue

        pieces = chunk_content(text, file_type)

        # Stable chunk IDs derived from content_hash so re-runs don't
        # create duplicate vector DB entries for unchanged pages.
        stable_prefix = content_hash[:16] if content_hash else hashlib.md5(
            url.encode()
        ).hexdigest()[:16]

        for i, piece in enumerate(pieces):
            if not piece.strip():
                continue
            yield {
                "id": f"{stable_prefix}_chunk{i}",
                "text": piece,
                "metadata": {
                    "source": url,
                    "domain": record.get("source_domain", ""),
                    "fetched_at": record.get("fetched_at", ""),
                    "file_type": file_type,
                    "has_code": has_code_block(piece) or file_type in ("bal", "ballerina"),
                },
            }

    print(
        f"Ingestion summary: {total} records seen, "
        f"{skipped_dupe} duplicate, {skipped_extract} empty/unextractable, "
        f"{skipped_irrelevant} filtered as off-topic, "
        f"rest chunked and yielded."
    )
    if total == 0:
        print(
            "WARNING: 0 records were read from the source. If you're calling "
            "build_sft_dataset() right after this, it will silently reuse "
            "whatever is already in the persisted chroma_db collection from "
            "a previous run. Call index.reset() first if that's not intended, "
            "and check the source path/bucket."
        )


def build_chunks_from_gcs(bucket_name: str, prefix: str = "") -> Iterator[dict]:
    """Yields chunk dicts ready for CorpusIndex.add_chunks(), i.e.:
      {"id": ..., "text": ..., "metadata": {...}}
    """
    yield from _process_records(iter_gcs_records(bucket_name, prefix))


def build_chunks_from_local_jsonl(path: str) -> Iterator[dict]:
    """Local-file equivalent of build_chunks_from_gcs, for testing.

    Usage (drop-in replacement for the GCS call):
        for chunk in build_chunks_from_local_jsonl("data.jsonl"):
            chunk_batch.append(chunk)
            ...
    """
    yield from _process_records(iter_local_jsonl_records(path))




# if __name__ == "__main__":
#     from sft_dataset_generator import CorpusIndex, build_sft_dataset
#
#     index = CorpusIndex()
#     index.reset()  # start clean each run — see CorpusIndex.reset() docstring
#
#     # --- For local testing (no GCS needed) ---
#     chunk_source = build_chunks_from_local_jsonl("data.jsonl")
#
#     # --- For real runs against GCS ---
#     # chunk_source = build_chunks_from_gcs(bucket_name="your-crawler-bucket", prefix="ballerina/")
#
#     chunk_batch = []
#     for chunk in chunk_source:
#         chunk_batch.append(chunk)
#         if len(chunk_batch) >= 256:  # batch embedding calls
#             index.add_chunks(chunk_batch)
#             chunk_batch = []
#     if chunk_batch:
#         index.add_chunks(chunk_batch)
#
#     print(f"Indexed {index.count()} chunks total.")
#     build_sft_dataset(index, questions_per_chunk=3, output_path="ballerina_sft-old.jsonl")