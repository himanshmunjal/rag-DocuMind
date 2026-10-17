"""
Searcher.

The first half of the retrieval layer. Takes a user query string,
embeds it, and finds the top-k most semantically similar chunks
stored in ChromaDB.

Pipeline position:
    [query string]
         ↓
    embed_query()          ← embedder.py  (RETRIEVAL_QUERY task type)
         ↓
    ChromaDB.query()       ← cosine similarity search over HNSW index
         ↓
    List[SearchResult]     ← passed to reranker.py next
         ↓
    guard.evaluate_chunks() ← hallucination/guard.py

Key design decisions:
  - Source filtering: optional `sources` param lets the caller restrict
    search to a subset of uploaded documents. Used by the multi-doc
    comparison feature on Day 11 ("compare contract A vs B").
  - Score normalisation: ChromaDB returns cosine *distance* (0 = identical,
    2 = opposite). We convert to similarity (1 - distance/2) so scores
    are in [0, 1] and guard.py thresholds work intuitively.
  - Deduplication: ChromaDB can return near-identical chunks if the same
    passage was chunked with overlap. We deduplicate on chunk_index+source
    before returning so the reranker and generator see clean, distinct context.
  - Metadata passthrough: every result carries the full metadata dict so
    citation_builder.py can build citations without a second DB lookup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from loguru import logger

from config import settings
from core.ingestion.embedder import embed_query
from core.ingestion.indexer import get_collection


# ── result dataclass ──────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    """
    A single retrieved chunk with its similarity score and source metadata.
    This is the unit that flows through the rest of the retrieval pipeline.

    Fields:
        text        — raw chunk text, passed to the LLM as context
        score       — cosine similarity in [0, 1]; higher = more relevant
        metadata    — dict with source, page, chunk_index, chunk_type, file_path
        chunk_id    — ChromaDB internal ID (useful for dedup and debugging)
    """
    text:       str
    score:      float
    metadata:   dict
    chunk_id:   str = field(default="")

    def to_dict(self) -> dict:
        """Convert to plain dict — expected shape by guard.py and reranker.py."""
        return {
            "text":     self.text,
            "score":    self.score,
            "metadata": self.metadata,
            "chunk_id": self.chunk_id,
        }


# ── distance → similarity conversion ─────────────────────────────────────────

def _cosine_distance_to_similarity(distance: float) -> float:
    """
    ChromaDB hnsw:cosine returns distance, not similarity.
    Distance range: [0, 2]  (0 = identical, 2 = perfectly opposite)
    Similarity:     [0, 1]  (1 = identical, 0 = perfectly opposite)

    Formula: similarity = 1 - (distance / 2)
    """
    return round(max(0.0, min(1.0, 1.0 - distance / 2.0)), 4)


# ── deduplication ─────────────────────────────────────────────────────────────

def _deduplicate(results: List[SearchResult]) -> List[SearchResult]:
    """
    Remove near-duplicate chunks that arise from overlapping chunking windows.
    Two chunks are considered duplicates if they share (source, chunk_index).
    When duplicates exist, the one with the higher score is kept.
    """
    seen: dict[tuple, SearchResult] = {}
    for r in results:
        key = (r.metadata.get("source", ""), r.metadata.get("chunk_index", -1))
        if key not in seen or r.score > seen[key].score:
            seen[key] = r
    deduped = list(seen.values())
    if len(deduped) < len(results):
        logger.debug(f"Dedup removed {len(results) - len(deduped)} duplicate chunks")
    return deduped


def search(
    query: str,
    top_k: Optional[int] = None,
    sources: Optional[List[str]] = None,
    cik: Optional[str] = None,
    item_filter: Optional[str] = None,
) -> List["SearchResult"]:
    """
    Semantic search: embed the query and retrieve the top-k most relevant
    chunks from ChromaDB, optionally scoped by sources / cik / item.
    """
    from config import settings
    from core.ingestion.embedder import embed_query
    from core.ingestion.indexer import get_collection
    from loguru import logger
 
    k = top_k or settings.TOP_K
    collection = get_collection()
 
    if collection.count() == 0:
        logger.warning("search: ChromaDB collection is empty — no documents indexed yet")
        return []
 
    logger.debug(f"Embedding query for search: '{query[:80]}…'")
    query_vector = embed_query(query)
 
    # ── build the where clause ──────────────────────────────────────────────
    conditions = []
    if sources:
        conditions.append(
            {"source": sources[0]} if len(sources) == 1 else {"source": {"$in": sources}}
        )
    if cik:
        conditions.append({"cik": cik})
    if item_filter:
        conditions.append({"item": item_filter})
 
    where_filter = None
    if len(conditions) == 1:
        where_filter = conditions[0]
    elif len(conditions) > 1:
        where_filter = {"$and": conditions}
 
    if where_filter:
        logger.debug(f"Search filter applied: {where_filter}")
 
    # ── query ChromaDB ───────────────────────────────────────────────────────
    query_kwargs = dict(
        query_embeddings=[query_vector],
        n_results=min(k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    if where_filter:
        query_kwargs["where"] = where_filter
 
    raw = collection.query(**query_kwargs)
 
    # ── unpack parallel lists from ChromaDB response ────────────────────────
    ids       = raw["ids"][0]
    documents = raw["documents"][0]
    metadatas = raw["metadatas"][0]
    distances = raw["distances"][0]
 
    # ── build SearchResult objects ───────────────────────────────────────────
    results: List["SearchResult"] = []
    for chunk_id, text, meta, dist in zip(ids, documents, metadatas, distances):
        results.append(SearchResult(
            text=text,
            score=_cosine_distance_to_similarity(dist),
            metadata=meta,
            chunk_id=chunk_id,
        ))
 
    # ── deduplicate + sort ────────────────────────────────────────────────────
    results = _deduplicate(results)
    results.sort(key=lambda r: r.score, reverse=True)
 
    if results:
        logger.info(
            f"search('{query[:60]}…') → {len(results)} results "
            f"[top score: {results[0].score:.3f}]"
        )
    else:
        logger.info(f"search('{query[:60]}…') → 0 results")
 
    return results
 
 
def search_dicts(
    query: str,
    top_k: Optional[int] = None,
    sources: Optional[List[str]] = None,
    cik: Optional[str] = None,
    item_filter: Optional[str] = None,
) -> List[dict]:
    """Same passthrough wrapper as before, just forwarding the two new params."""
    return [
        r.to_dict()
        for r in search(query, top_k=top_k, sources=sources, cik=cik, item_filter=item_filter)
    ]



# ── public API ────────────────────────────────────────────────────────────────

# def search(
#     query: str,
#     top_k: Optional[int] = None,
#     sources: Optional[List[str]] = None,
# ) -> List[SearchResult]:
#     """
#     Semantic search: embed the query and retrieve the top-k most relevant
#     chunks from ChromaDB.

#     Args:
#         query:    User's natural language question.
#         top_k:    Number of chunks to retrieve. Defaults to settings.TOP_K.
#                   Set higher (e.g. 12) when you plan to rerank, so the
#                   reranker has a wider pool to pick from.
#         sources:  Optional list of filenames to restrict search to, e.g.
#                   ["contract_a.pdf", "contract_b.pdf"].
#                   None (default) searches across all indexed documents.

#     Returns:
#         List of SearchResult, sorted by score descending.
#         Empty list if the collection has no documents.

#     Example:
#         results = search("What is the termination clause?")
#         results = search("Compare liability caps", sources=["nda_a.pdf", "nda_b.pdf"])
#     """
#     k = top_k or settings.TOP_K
#     collection = get_collection()

#     if collection.count() == 0:
#         logger.warning("search: ChromaDB collection is empty — no documents indexed yet")
#         return []

#     # Embed the query using RETRIEVAL_QUERY task type (asymmetric)
#     logger.debug(f"Embedding query for search: '{query[:80]}…'")
#     query_vector = embed_query(query)

#     # Build optional source filter for multi-document scoping
#     where_filter: Optional[dict] = None
#     if sources:
#         if len(sources) == 1:
#             where_filter = {"source": sources[0]}
#         else:
#             # ChromaDB $in operator for multiple values
#             where_filter = {"source": {"$in": sources}}
#         logger.debug(f"Source filter applied: {sources}")

#     # Query ChromaDB
#     query_kwargs = dict(
#         query_embeddings=[query_vector],
#         n_results=min(k, collection.count()),   # guard against k > collection size
#         include=["documents", "metadatas", "distances"],
#     )
#     if where_filter:
#         query_kwargs["where"] = where_filter

#     raw = collection.query(**query_kwargs)

#     # Unpack parallel lists from ChromaDB response
#     ids        = raw["ids"][0]           # list of chunk IDs
#     documents  = raw["documents"][0]     # list of chunk texts
#     metadatas  = raw["metadatas"][0]     # list of metadata dicts
#     distances  = raw["distances"][0]     # list of cosine distances [0, 2]

#     # Build SearchResult objects
#     results: List[SearchResult] = []
#     for chunk_id, text, meta, dist in zip(ids, documents, metadatas, distances):
#         results.append(SearchResult(
#             text=text,
#             score=_cosine_distance_to_similarity(dist),
#             metadata=meta,
#             chunk_id=chunk_id,
#         ))

#     # Deduplicate overlapping chunks, then sort by score
#     results = _deduplicate(results)
#     results.sort(key=lambda r: r.score, reverse=True)

#     logger.info(
#         f"search('{query[:60]}…') → {len(results)} results "
#         f"[top score: {results[0].score:.3f}]" if results else
#         f"search('{query[:60]}…') → 0 results"
#     )

#     return results


# def search_dicts(
#     query: str,
#     top_k: Optional[int] = None,
#     sources: Optional[List[str]] = None,
# ) -> List[dict]:
#     """
#     Convenience wrapper that returns plain dicts instead of SearchResult objects.
#     Output matches the shape expected by guard.evaluate_chunks() and reranker.rerank():
#         [{"text": str, "score": float, "metadata": dict, "chunk_id": str}, ...]

#     Use this when you don't need the SearchResult type (e.g. in routes.py).
#     """
#     return [r.to_dict() for r in search(query, top_k=top_k, sources=sources)]