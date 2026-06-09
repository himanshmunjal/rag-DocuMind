"""
Hallucination Guard — Day 9-10.
This is what separates a demo from a production system.

Logic:
  - Score the retrieved chunks' similarity scores
  - If max similarity is below threshold → refuse to generate, return canned response
  - If some chunks are below threshold → filter them out, reduce confidence
  - Attach a confidence level (HIGH / MEDIUM / LOW) to every response
"""
from enum import Enum
from typing import List, Tuple
from dataclasses import dataclass
from loguru import logger

from config import settings


class ConfidenceLevel(str, Enum):
    HIGH = "HIGH"       # All retrieved chunks above threshold, re-ranker agrees
    MEDIUM = "MEDIUM"   # Some chunks above threshold
    LOW = "LOW"         # Barely above threshold — answer with caveats
    INSUFFICIENT = "INSUFFICIENT"  # Below threshold — refuse to answer


@dataclass
class GuardResult:
    should_answer: bool
    confidence: ConfidenceLevel
    filtered_chunks: List[dict]     # chunks that passed the guard
    reason: str = ""


def evaluate_chunks(
    retrieved_chunks: List[dict],   # each has {"text", "metadata", "score"}
) -> GuardResult:
    """
    Evaluate retrieved chunks and decide whether to generate a response.

    Args:
        retrieved_chunks: output from retriever, with similarity scores

    Returns:
        GuardResult with confidence level and filtered chunks
    """
    if not retrieved_chunks:
        logger.warning("No chunks retrieved — insufficient information")
        return GuardResult(
            should_answer=False,
            confidence=ConfidenceLevel.INSUFFICIENT,
            filtered_chunks=[],
            reason="No relevant content found in uploaded documents.",
        )

    scores = [chunk.get("score", 0.0) for chunk in retrieved_chunks]
    max_score = max(scores)
    avg_score = sum(scores) / len(scores)

    logger.debug(f"Retrieval scores — max: {max_score:.3f}, avg: {avg_score:.3f}")

    # Hard floor: if best chunk is below threshold, refuse entirely
    if max_score < settings.CONFIDENCE_THRESHOLD:
        logger.info(f"Max score {max_score:.3f} below threshold {settings.CONFIDENCE_THRESHOLD} — refusing")
        return GuardResult(
            should_answer=False,
            confidence=ConfidenceLevel.INSUFFICIENT,
            filtered_chunks=[],
            reason=settings.LOW_CONFIDENCE_RESPONSE,
        )

    # Filter out weak chunks
    passing_chunks = [
        chunk for chunk in retrieved_chunks
        if chunk.get("score", 0.0) >= settings.SIMILARITY_THRESHOLD
    ]

    # Determine confidence tier
    if avg_score >= 0.70:
        confidence = ConfidenceLevel.HIGH
    elif avg_score >= 0.50:
        confidence = ConfidenceLevel.MEDIUM
    else:
        confidence = ConfidenceLevel.LOW

    logger.info(f"Guard passed — confidence: {confidence}, passing chunks: {len(passing_chunks)}")
    return GuardResult(
        should_answer=True,
        confidence=confidence,
        filtered_chunks=passing_chunks,
    )