"""
multi_doc.py — Cross-document intelligence layer

Two capabilities live here:

1. compare(query, sources)
   Runs the same query independently against each listed document and
   returns per-document answers.  The route layer then calls synthesise()
   to produce a single "what's different" paragraph.

   Example use-case: "Compare the termination clause in contract_a.pdf
   vs contract_b.pdf"

2. synthesise(query, doc_answers)
   Feeds all per-document answers back to Gemini in a single prompt and
   asks it to write a concise cross-document comparison paragraph.

Design notes:
  - Each per-document retrieval is independent — no cross-contamination
    of chunks between documents.
  - The hallucination guard runs per document, so a document that has no
    relevant content shows up as INSUFFICIENT rather than hallucinating.
  - synthesise() is a pure LLM call with no retrieval; it operates only
    on text that was already ground-truth-checked by the guard.
"""

from __future__ import annotations
from typing import List, Dict, Any
from loguru import logger
import google.generativeai as genai

from config import settings
from core.ingestion.indexer import get_collection
from core.ingestion.embedder import embed_query
from core.hallucination.guard import evaluate_chunks, ConfidenceLevel
from api.schemas import CitationItem, DocumentAnswer


# ── internal: per-document retrieval ──────────────────────────────────────────

def _retrieve_for_source(
    query_vector: List[float],
    source: str,
    top_k: int,
) -> List[Dict[str, Any]]:
    """
    Query ChromaDB filtered to a single document source.
    Returns chunks with text, metadata, and similarity score.
    """
    collection = get_collection()

    results = collection.query(
        query_embeddings=[query_vector],
        n_results=top_k,
        where={"source": source},
        include=["documents", "metadatas", "distances"],
    )

    chunks = []
    ids       = results["ids"][0]
    documents = results["documents"][0]
    metadatas = results["metadatas"][0]
    distances = results["distances"][0]   # cosine distance (lower = more similar)

    for doc_text, meta, distance in zip(documents, metadatas, distances):
        # Convert cosine distance → similarity score  (distance ∈ [0,2] for cosine)
        score = max(0.0, 1.0 - distance)
        chunks.append({
            "text":     doc_text,
            "metadata": meta,
            "score":    round(score, 4),
        })

    logger.debug(f"Retrieved {len(chunks)} chunks for source='{source}'")
    return chunks


def _chunks_to_citations(chunks: List[Dict[str, Any]]) -> List[CitationItem]:
    return [
        CitationItem(
            source      = c["metadata"]["source"],
            page        = c["metadata"]["page"],
            chunk_index = c["metadata"]["chunk_index"],
            chunk_type  = c["metadata"].get("chunk_type", "unknown"),
            excerpt     = c["text"][:300],   # truncate long chunks in the citation
            score       = c["score"],
        )
        for c in chunks
    ]


# ── internal: single-document answer via Gemini ───────────────────────────────

def _generate_for_chunks(
    query: str,
    chunks: List[Dict[str, Any]],
    source: str,
) -> str:
    """
    Generate an answer grounded strictly in the provided chunks.
    Called once per document during a compare() run.
    """
    context = "\n\n---\n\n".join(
        f"[Page {c['metadata']['page']}]\n{c['text']}" for c in chunks
    )

    prompt = f"""You are analysing the document "{source}".
Answer the question below using ONLY the excerpts provided.
If the document does not contain enough information, say so clearly.
Do not add any knowledge from outside these excerpts.

QUESTION: {query}

DOCUMENT EXCERPTS:
{context}

ANSWER:"""

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model  = genai.GenerativeModel(settings.LLM_MODEL)
    result = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=settings.TEMPERATURE,
            max_output_tokens=settings.MAX_TOKENS,
        ),
    )
    return result.text.strip()


# ── public: compare ────────────────────────────────────────────────────────────

def compare(
    query: str,
    sources: List[str],
    top_k: int | None = None,
) -> List[DocumentAnswer]:
    """
    Run the same query against each document in `sources` independently.
    Returns one DocumentAnswer per source — the route layer passes these
    to synthesise() for the final comparison paragraph.

    Args:
        query:   user's question
        sources: list of filenames to search, e.g. ["contract_a.pdf", "contract_b.pdf"]
        top_k:   chunks to retrieve per document (defaults to settings.TOP_K)

    Returns:
        list of DocumentAnswer — one per source, in the same order
    """
    k = top_k or settings.TOP_K
    query_vector = embed_query(query)
    results: List[DocumentAnswer] = []

    for source in sources:
        logger.info(f"compare: querying '{source}' for '{query[:60]}…'")

        chunks = _retrieve_for_source(query_vector, source, k)
        guard  = evaluate_chunks(chunks)

        if not guard.should_answer:
            results.append(DocumentAnswer(
                source     = source,
                answer     = f"[{source}] — {guard.reason}",
                confidence = ConfidenceLevel.INSUFFICIENT,
                citations  = [],
            ))
            continue

        answer = _generate_for_chunks(query, guard.filtered_chunks, source)
        results.append(DocumentAnswer(
            source     = source,
            answer     = answer,
            confidence = guard.confidence,
            citations  = _chunks_to_citations(guard.filtered_chunks),
        ))

    return results


# ── public: synthesise ─────────────────────────────────────────────────────────

def synthesise(query: str, doc_answers: List[DocumentAnswer]) -> str:
    """
    Feed all per-document answers into Gemini and ask for a concise
    cross-document comparison paragraph.

    This is a pure LLM call — no retrieval.  It only sees text that has
    already passed the hallucination guard, so it cannot hallucinate about
    source documents that were INSUFFICIENT.

    Args:
        query:       original user question
        doc_answers: output of compare()

    Returns:
        synthesis paragraph as a string
    """
    if not doc_answers:
        return "No documents were successfully analysed."

    # Build a labelled block per document
    blocks = []
    for da in doc_answers:
        blocks.append(
            f"=== {da.source} (confidence: {da.confidence}) ===\n{da.answer}"
        )
    answers_text = "\n\n".join(blocks)

    prompt = f"""You have been given answers to the same question from multiple documents.
Write a concise comparison paragraph (3-6 sentences) that highlights the KEY
SIMILARITIES and DIFFERENCES between the documents on this topic.
Be specific — reference document names directly.
Do not repeat the full answers; synthesise the important distinctions.

QUESTION: {query}

ANSWERS PER DOCUMENT:
{answers_text}

COMPARISON SUMMARY:"""

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model  = genai.GenerativeModel(settings.LLM_MODEL)
    result = model.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature=0.3,              # slightly higher for synthesis prose
            max_output_tokens=512,
        ),
    )
    return result.text.strip()