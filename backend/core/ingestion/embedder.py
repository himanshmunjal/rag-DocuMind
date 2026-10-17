"""
embedder.py — embeds chunk text using a local sentence-transformers model.

Switched from the Gemini embedding API to a local model to avoid free-tier
rate limits during development (100 req/min and a daily cap made iterative
testing impractical). No network calls, no quota, works offline.

Model: sentence-transformers/all-MiniLM-L6-v2 (384-dim, ~80MB, fast on CPU).
Same package your reranker (cross-encoder/ms-marco-MiniLM-L6-v2) already
uses, so no new dependency should be needed.

IMPORTANT: query embeddings and document embeddings MUST come from the same
model — that's why embed_query() and embed_chunks() both route through the
same _get_model() singleton below. Never mix embedding sources.

If you ever switch EMBEDDING_MODEL again, you must wipe and rebuild the
Chroma collection — vectors from different models are not comparable.
"""

from __future__ import annotations

from typing import List

from loguru import logger
from sentence_transformers import SentenceTransformer

from config import settings

EMBEDDING_DIM = 384   # must match the output dim of settings.EMBEDDING_MODEL

_model: SentenceTransformer | None = None


def _get_model() -> SentenceTransformer:
    """Lazily load and cache the embedding model (loaded once per process)."""
    global _model
    if _model is None:
        logger.info(f"Loading local embedding model: {settings.EMBEDDING_MODEL}…")
        _model = SentenceTransformer(settings.EMBEDDING_MODEL)
        logger.success(f"Embedding model loaded (dim={_model.get_sentence_embedding_dimension()})")
    return _model


def embed_chunks(chunks: List[dict]) -> List[dict]:
    """
    Embed a list of chunk dicts (output of filing_chunker.chunk_filing).
    Adds an "embedding" key to each chunk in-place and returns the list.

    Args:
        chunks: list of {"text": str, "metadata": dict}

    Returns:
        same list with "embedding": List[float] added to each item

    Example:
        chunks = chunk_filing(html, filing_meta)
        chunks = embed_chunks(chunks)
        # chunks[0]["embedding"] -> [0.021, -0.003, ...]  (384 floats)
    """
    if not chunks:
        logger.warning("embed_chunks called with empty list — nothing to do")
        return chunks

    texts = [c["text"] for c in chunks]
    model = _get_model()

    logger.info(f"Embedding {len(texts)} chunks locally…")
    vectors = model.encode(
        texts,
        batch_size=32,
        show_progress_bar=False,
        normalize_embeddings=True,   # so cosine similarity == dot product downstream
        convert_to_numpy=True,
    )

    for chunk, vector in zip(chunks, vectors):
        chunk["embedding"] = vector.tolist()

    logger.success(f"Embedded {len(texts)} chunks ({EMBEDDING_DIM}-dim each)")
    return chunks


def embed_query(text: str) -> List[float]:
    """
    Embed a single query string for retrieval search.

    Uses the same model as embed_chunks() — query and document vectors must
    come from the same embedding space to be comparable.

    Args:
        text: the user's question

    Returns:
        a single 384-dim float vector

    Example:
        query_vector = embed_query("What was Apple's revenue in FY2024?")
    """
    model = _get_model()
    vector = model.encode(
        text,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return vector.tolist()