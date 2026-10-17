"""
Reranker — Day.

The second half of the retrieval layer. Takes the top-k chunks from
searcher.py (ranked by cosine similarity) and re-scores them with a
cross-encoder model for much higher precision.

Pipeline position:
    searcher.search_dicts()        ← cosine similarity, fast, approximate
             ↓
    reranker.rerank()              ← cross-encoder, slower, far more accurate
             ↓
    guard.evaluate_chunks()        ← decides whether to generate at all
             ↓
    generator.generate()

Why bother reranking?
  Bi-encoder similarity (what ChromaDB does) embeds query and document
  *independently* and compares their vectors. It is fast but misses subtle
  relevance signals — word order, negation, semantic nuance.

  A cross-encoder reads the query and each candidate chunk *together* in one
  forward pass. It sees the full interaction between them and produces a much
  more accurate relevance score. The trade-off is speed: you can't pre-compute
  cross-encoder scores at index time, only at query time. That's why we use
  the bi-encoder to narrow the field to top-k, then the cross-encoder to
  pick the best top-n from that shortlist.

Model: cross-encoder/ms-marco-MiniLM-L-6-v2  (default)
  - Trained on MS MARCO passage retrieval (140M+ query-passage pairs)
  - ~23 MB, runs on CPU in ~50ms for 6 chunks — fast enough for real-time use
  - Outputs logits in roughly [-10, 10]; we sigmoid-normalise to [0, 1]

Drop-in alternatives (change RERANKER_MODEL in config / .env):
  cross-encoder/ms-marco-MiniLM-L-12-v2   — more accurate, ~2× slower
  cross-encoder/ms-marco-electra-base      — best quality, heavier
  BAAI/bge-reranker-base                  — strong, multilingual-friendly
"""

from __future__ import annotations

from typing import List, Optional

# import torch
from sentence_transformers import CrossEncoder
from loguru import logger

from config import settings


# ── model singleton ───────────────────────────────────────────────────────────

_reranker: Optional[CrossEncoder] = None


def _get_reranker() -> CrossEncoder:
    """
    Load the cross-encoder once per process lifetime.
    Subsequent calls return the cached model instantly.
    """
    global _reranker
    if _reranker is not None:
        return _reranker

    logger.info(f"Loading reranker model '{settings.RERANKER_MODEL}' …")

    # CrossEncoder handles device placement internally.
    # Pass max_length to avoid silent truncation on long chunks.
    _reranker = CrossEncoder(
        settings.RERANKER_MODEL,
        max_length=512,
        device="cpu"
    )

    logger.success(f"Reranker ready: '{settings.RERANKER_MODEL}'")
    return _reranker


# ── score normalisation ───────────────────────────────────────────────────────

def _sigmoid(x: float) -> float:
    """
    Normalise raw cross-encoder logits to [0, 1].
    MS-MARCO cross-encoders output uncalibrated logits, not probabilities.
    Sigmoid maps them to a probability-like range that guard.py thresholds
    can reason about alongside the cosine similarity scores from the searcher.
    """
    import math
    return round(1.0 / (1.0 + math.exp(-x)), 4)


# ── public API ────────────────────────────────────────────────────────────────

def rerank(
    query: str,
    chunks: List[dict],
    top_n: Optional[int] = None,
) -> List[dict]:
    """
    Re-score and re-rank candidate chunks using a cross-encoder.

    Takes the broader top-k pool from the searcher and returns the top-n
    highest-quality chunks. The "score" field in each returned chunk is
    replaced with the cross-encoder score (sigmoid-normalised), which is
    more accurate than the original cosine similarity.

    The original cosine similarity score is preserved as "bi_encoder_score"
    so guard.py and citation_builder.py have both signals available.

    Args:
        query:  The user's original question string (same one passed to search).
        chunks: List of chunk dicts from searcher.search_dicts():
                [{"text", "score", "metadata", "chunk_id"}, ...]
        top_n:  How many chunks to return after reranking.
                Defaults to settings.RERANK_TOP_N (typically 3).

    Returns:
        List of chunk dicts, sorted by cross-encoder score descending,
        truncated to top_n. Each dict has:
          - "score"           → cross-encoder sigmoid score [0, 1]
          - "bi_encoder_score"→ original cosine similarity [0, 1]
          - "rerank_position" → position in the reranked list (1-indexed)
          - all original keys ("text", "metadata", "chunk_id") preserved

    Returns empty list if `chunks` is empty.

    Example:
        candidates = search_dicts("What are the payment terms?", top_k=8)
        top_chunks = rerank("What are the payment terms?", candidates, top_n=3)
        # top_chunks[0] is the single most relevant chunk
    """
    n = top_n or settings.RERANK_TOP_N

    if not chunks:
        logger.warning("rerank: received empty chunk list — nothing to rerank")
        return []

    if len(chunks) == 1:
        # No point running inference for a single chunk; just return it
        chunk = dict(chunks[0])
        chunk["bi_encoder_score"] = chunk.get("score", 0.0)
        chunk["rerank_position"] = 1
        return [chunk]

    reranker = _get_reranker()

    # Cross-encoder expects (query, passage) pairs
    pairs = [(query, chunk["text"]) for chunk in chunks]

    logger.debug(f"Reranking {len(pairs)} chunks for query: '{query[:60]}…'")

    # predict() returns a numpy array of raw logits, one per pair
    raw_scores = reranker.predict(pairs, show_progress_bar=False)

    # Attach cross-encoder scores and preserve original cosine scores
    scored: List[dict] = []
    for chunk, raw in zip(chunks, raw_scores):
        enriched = dict(chunk)                             # copy, don't mutate
        enriched["bi_encoder_score"] = chunk.get("score", 0.0)
        enriched["score"] = _sigmoid(float(raw))           # overwrite with CE score
        scored.append(enriched)

    # Sort by cross-encoder score, best first
    scored.sort(key=lambda c: c["score"], reverse=True)

    # Keep top_n and annotate position (useful for citation ordering in UI)
    top = scored[:n]
    for position, chunk in enumerate(top, start=1):
        chunk["rerank_position"] = position

    logger.info(
        f"Reranked {len(chunks)} → {len(top)} chunks | "
        f"top score: {top[0]['score']:.3f} "
        f"(was bi-encoder: {top[0]['bi_encoder_score']:.3f})"
    )

    return top


def retrieve(
    query: str,
    sources: Optional[List[str]] = None,
    cik: Optional[str] = None,
    item_filter: Optional[str] = None,
    top_k: Optional[int] = None,
    top_n: Optional[int] = None,
) -> List[dict]:
    """Full retrieval pipeline: search → rerank. Now with cik/item_filter scoping."""
    from config import settings
    from core.retrieval.searcher import search_dicts
    from loguru import logger
 
    k = top_k or settings.TOP_K
    n = top_n or settings.RERANK_TOP_N
    fetch_k = max(k, n * 3)
 
    candidates = search_dicts(
        query, top_k=fetch_k, sources=sources, cik=cik, item_filter=item_filter
    )
 
    if not candidates:
        logger.warning(f"retrieve: no candidates found for '{query[:60]}…'")
        return []
 
    from core.retrieval.reranker import rerank
    return rerank(query, candidates, top_n=n)