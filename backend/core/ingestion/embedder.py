"""
Wraps the Gemini text-embedding-004 API with:
  - Task-type separation: RETRIEVAL_DOCUMENT for indexing, RETRIEVAL_QUERY for queries.
    This is not cosmetic — Gemini optimises the embedding space differently per task type,
    so mixing them degrades retrieval quality.
  - Batching: Gemini allows up to 100 texts per batch call, dramatically cutting
    API round-trips on large PDFs.
  - Retry with exponential backoff: embedding jobs are long-running; transient
    failures shouldn't abort an entire ingestion.
  - Dimensionality: text-embedding-004 outputs 768-dim vectors by default.
    Exposed as EMBEDDING_DIM so ChromaDB and the indexer stay in sync.
"""

import time
import google.generativeai as genai
from typing import List
from loguru import logger

from config import settings

# ── configuration ──────────────────────────────────────────────────────────────

genai.configure(api_key=settings.GEMINI_API_KEY)

EMBEDDING_DIM: int = 768        # text-embedding-004 native dimension
BATCH_SIZE: int = 100           # Gemini max texts per embed_content call
MAX_RETRIES: int = 3
RETRY_BASE_DELAY: float = 2.0   # seconds; doubles on each retry


# ── internal helpers ───────────────────────────────────────────────────────────

def _embed_batch(texts: List[str], task_type: str) -> List[List[float]]:
    """
    Call the Gemini batch embedding endpoint for a single batch.
    Retries up to MAX_RETRIES times on transient errors.

    Args:
        texts:      list of strings, len ≤ BATCH_SIZE
        task_type:  "RETRIEVAL_DOCUMENT" | "RETRIEVAL_QUERY"

    Returns:
        list of 768-dim float vectors, same order as input texts
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            result = genai.embed_content(
                model=settings.EMBEDDING_MODEL,
                content=texts,
                task_type=task_type,
            )
            # result.embedding is a list[list[float]] when content is a list
            return result["embedding"]

        except Exception as exc:
            if attempt == MAX_RETRIES:
                logger.error(f"Embedding failed after {MAX_RETRIES} attempts: {exc}")
                raise
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                f"Embedding attempt {attempt}/{MAX_RETRIES} failed ({exc}). "
                f"Retrying in {delay:.1f}s…"
            )
            time.sleep(delay)

    # unreachable, but satisfies type checkers
    raise RuntimeError("_embed_batch: exhausted retries")


def _batched(items: list, size: int):
    """Yield successive slices of `items` of length ≤ `size`."""
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ── public API ─────────────────────────────────────────────────────────────────

def embed_chunks(chunks: List[dict]) -> List[dict]:
    """
    Embed a list of chunk dicts (output of chunker.chunk_pages).
    Adds an "embedding" key to each chunk in-place and returns the list.

    Uses task_type RETRIEVAL_DOCUMENT — correct for content being indexed.
    Do NOT use this for query strings; use embed_query() instead.

    Args:
        chunks: list of {"text": str, "metadata": dict}

    Returns:
        same list with "embedding": List[float] added to each item

    Example:
        chunks = chunk_pages(pages)
        chunks = embed_chunks(chunks)
        # chunks[0]["embedding"] → [0.021, -0.003, ...]  (768 floats)
    """
    if not chunks:
        logger.warning("embed_chunks called with empty list — nothing to do")
        return chunks

    texts = [c["text"] for c in chunks]
    total = len(texts)
    all_embeddings: List[List[float]] = []

    logger.info(f"Embedding {total} chunks in batches of {BATCH_SIZE}…")

    for batch_num, batch_texts in enumerate(_batched(texts, BATCH_SIZE), start=1):
        logger.debug(
            f"Batch {batch_num}: embedding {len(batch_texts)} texts "
            f"({(batch_num - 1) * BATCH_SIZE + 1}–"
            f"{min(batch_num * BATCH_SIZE, total)} of {total})"
        )
        batch_vectors = _embed_batch(batch_texts, task_type="RETRIEVAL_DOCUMENT")
        all_embeddings.extend(batch_vectors)

    # Attach vectors back to chunk dicts
    for chunk, vector in zip(chunks, all_embeddings):
        chunk["embedding"] = vector

    logger.success(f"Embedded {total} chunks ({EMBEDDING_DIM}-dim each)")
    return chunks


def embed_query(query: str) -> List[float]:
    """
    Embed a single user query string.
    Uses task_type RETRIEVAL_QUERY — asymmetric to the document embeddings,
    which is exactly what Gemini text-embedding-004 is optimised for.

    Args:
        query: raw user question string

    Returns:
        768-dim float vector

    Example:
        vec = embed_query("What is the termination clause?")
        # use vec to search ChromaDB
    """
    if not query.strip():
        raise ValueError("embed_query: query string is empty")

    logger.debug(f"Embedding query ({len(query)} chars): '{query[:80]}…'")

    result = genai.embed_content(
        model=settings.EMBEDDING_MODEL,
        content=query,
        task_type="RETRIEVAL_QUERY",
    )
    # Single string input → result.embedding is a single list[float]
    return result["embedding"]