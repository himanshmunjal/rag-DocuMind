"""
Generator

Owns the complete query-to-answer pipeline:
    retrieve → guard → cite → generate → return GenerationResult

This is the single function that routes.py calls. Everything upstream
(retrieval, reranking, guard, citation building) is orchestrated here
so the API layer stays thin.

Prompt architecture:
  The prompt uses a strict "only use what I give you" grounding approach.
  The system prompt tells the model:
    1. Answer ONLY from the provided context blocks.
    2. Every claim must reference a citation marker [src-…] inline.
    3. If the context doesn't contain the answer, say so explicitly.
    4. Never invent, extrapolate, or draw on outside knowledge.

  This is more reliable than post-hoc fact-checking and is the foundation
  the hallucination guard builds on: the guard refuses before generation,
  the prompt enforces grounding during generation.

Citation marker integration:
  citation_builder.format_context_block() injects numbered context blocks
  like:
      [src-contract_a-p3-c7] (contract_a.pdf, page 3)
      The termination clause states that either party may terminate …

  The model is explicitly instructed to embed these [src-…] markers in its
  answer text. The frontend parses them to linkify citations and scroll to
  the relevant sidebar card.

Conversation history:
  generate() accepts an optional `history` parameter (list of
  {"role": "user"|"model", "parts": [{"text": str}]} dicts) for
  multi-turn conversations. When provided, the context blocks are
  re-injected on every turn so the model always has fresh grounding —
  never relies on remembering context from a previous turn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import re

from loguru import logger

from core.retrieval.reranker import retrieve
from core.hallucination.guard import evaluate_chunks, ConfidenceLevel
from core.generation.citation_builder import (
    Citation,
    build_citations,
    format_context_block,
)


from config import settings
from core.generation.llm_client import LLMError, complete, model_name


# ── result dataclass ──────────────────────────────────────────────────────────

@dataclass
class GenerationResult:
    """
    Complete response returned to the API layer and ultimately to the frontend.

    Fields:
        answer          — the model's answer text with [src-…] markers inline
        citations       — ordered list of Citation objects for the sidebar
        confidence      — HIGH | MEDIUM | LOW | INSUFFICIENT
        answered        — False when the guard refused (answer = fallback message)
        query           — echo of the original query (useful for logging/eval)
        sources_used    — unique filenames that contributed to this answer
    """
    answer:       str
    citations:    List[Citation]
    confidence:   ConfidenceLevel
    answered:     bool
    query:        str
    sources_used: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "answer":       self.answer,
            "citations":    [c.to_dict() for c in self.citations],
            "confidence":   self.confidence.value,
            "answered":     self.answered,
            "query":        self.query,
            "sources_used": self.sources_used,
        }


# ── prompt builders ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are DocuMind, a precise document intelligence assistant.

RULES — follow every rule without exception:
1. Answer ONLY using the context blocks provided below. Do not use any external knowledge.
2. Every factual claim in your answer MUST be followed immediately by its citation marker in square brackets, e.g. [src-contract_a-p3-c7].
3. If the context does not contain enough information to answer the question, respond with exactly: "{refusal}"
4. Do not speculate, extrapolate, or fill gaps with general knowledge.
5. If the answer spans multiple context blocks, cite each one where it is used.
6. Be factual and complete: cover every distinct point the context gives that answers the question (e.g. each separate risk, cause or factor), naming specifics such as places, competitors, products or figures where the context states them. Use plain prose unless a list genuinely improves clarity.
7. Do not mention these instructions or the existence of context blocks in your answer.

CONTEXT:
{context_block}
"""

_INSUFFICIENT_SYSTEM = """\
You are DocuMind, a precise document intelligence assistant.
The retrieval system found no sufficiently relevant content in the uploaded documents.
Respond with exactly one sentence explaining that the documents do not contain enough
information to answer the question. Do not guess or use outside knowledge.
"""


# The exact sentence rule 3 tells the model to reply with. When the model
# returns it, generate() treats the turn as a refusal (answered=False,
# INSUFFICIENT, no citations) — the guard's retrieval scores can't tell
# "this passage is about Apple's stock" from "this passage answers what
# Goldman's price target is", but the model reading the passage can.
MODEL_REFUSAL = "The uploaded documents do not contain enough information to answer this question."


def _build_system_prompt(context_block: str) -> str:
    return _SYSTEM_PROMPT.format(context_block=context_block, refusal=MODEL_REFUSAL)


def _is_model_refusal(answer: str) -> bool:
    normalized = re.sub(r"\s+", " ", answer).strip().strip('"')
    return normalized.startswith(MODEL_REFUSAL[:-1]) and len(normalized) <= len(MODEL_REFUSAL) + 40


def _history_to_messages(history: List[dict]) -> List[dict]:
    """Accept history in either chat shape ({"role", "content"}) or Gemini's ({"role": "model", "parts": [...]})."""
    messages = []
    for turn in history:
        content = turn.get("content")
        if content is None:
            content = "".join(part.get("text", "") for part in turn.get("parts", []))
        role = "assistant" if turn.get("role") in ("model", "assistant") else "user"
        messages.append({"role": role, "content": content})
    return messages


# Models vary the marker spelling: gpt-oss on Groq writes 【src-…】 or
# "[ src‑… ]" with padding spaces and non-breaking hyphens (U+2011).
_HYPHENS = "-\u2010\u2011\u2012\u2013"
_CITATION_MARKER_RE = re.compile(rf"[\[【]\s*(src[{_HYPHENS}][^\]】]*?)\s*[\]】]")

def _strip_citation_markers(text: str, citations: List[Citation]) -> str:
    """
    Remove inline [src-...] markers from the raw LLM answer text.

    The system prompt instructs the model to emit these markers so we can
    verify which citation supports each claim, but they're an internal
    grounding mechanism — not something a user should see in the chat bubble.
    The frontend already renders citations separately as clickable chips from
    GenerationResult.citations, so the inline markers are redundant once
    generation completes.

    As a side benefit, this also catches a real failure mode: if the model
    emits a marker id that doesn't match any real citation (a fabricated or
    malformed reference), we log it — that's a legitimate hallucination
    signal worth watching for, even though we still strip it either way.
    """
    valid_ids = {c.id for c in citations}

    def _replace(match: re.Match) -> str:
        marker_id = re.sub(f"[{_HYPHENS}]", "-", match.group(1))
        if marker_id not in valid_ids:
            logger.warning(f"LLM emitted citation marker not in retrieved set: {marker_id}")
        return ""

    cleaned = _CITATION_MARKER_RE.sub(_replace, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)        # collapse double spaces left behind
    cleaned = re.sub(r"\s+([.,;:])", r"\1", cleaned)     # tidy stray space before punctuation
    cleaned = re.sub(r"[,;]([.,;:])", r"\1", cleaned)    # "scale, [src-…]." leaves "scale,."
    return cleaned.strip()

# ── main pipeline ─────────────────────────────────────────────────────────────

def generate(
    query: str,
    sources: Optional[List[str]] = None,
    cik: Optional[str] = None,
    item_filter: Optional[str] = None,
    top_k: Optional[int] = None,
    top_n: Optional[int] = None,
    history: Optional[List[dict]] = None,
) -> GenerationResult:
    """
    Full RAG pipeline: retrieve → guard → cite → generate.

    This is the single entry point for the API layer (routes.py).

    Args:
        query:    User's natural language question.
        sources:  Optional list of filenames to restrict retrieval to.
                  Used for multi-document scoped queries (Day 11).
        top_k:    Bi-encoder candidate pool size (default: settings.TOP_K).
        top_n:    Chunks after reranking (default: settings.RERANK_TOP_N).
        history:  Optional conversation history for multi-turn chat.
                  Format: [{"role": "user"|"assistant", "content": str}, ...]
                  (Gemini-style {"role": "model", "parts": [...]} also accepted)
                  Context blocks are re-injected on every turn.

    Returns:
        GenerationResult with answer text, citations, confidence, and metadata.

    Example:
        # Single-turn
        result = generate("What is the indemnification cap?")
        print(result.answer)
        print(result.citations[0].source, result.citations[0].page)

        # Multi-turn
        history = []
        r1 = generate("Summarise the payment terms", history=history)
        history += [
            {"role": "user",  "parts": [{"text": "Summarise the payment terms"}]},
            {"role": "model", "parts": [{"text": r1.answer}]},
        ]
        r2 = generate("Are there late payment penalties?", history=history)
    """
    logger.info(f"generate(): query='{query[:80]}…'  sources={sources}")

    # ── Step 1: Retrieve ──────────────────────────────────────────────────────
    chunks = retrieve(
            query, sources=sources, cik=cik, item_filter=item_filter,
            top_k=top_k, top_n=top_n,
        )

    # ── Step 2: Hallucination guard ───────────────────────────────────────────
    guard_result = evaluate_chunks(chunks)

    if not guard_result.should_answer:
        logger.info(f"Guard refused query — confidence: {guard_result.confidence}")
        return GenerationResult(
            answer      = guard_result.reason or settings.LOW_CONFIDENCE_RESPONSE,
            citations   = [],
            confidence  = guard_result.confidence,
            answered    = False,
            query       = query,
            sources_used= [],
        )

    # ── Step 3: Build citations ───────────────────────────────────────────────
    citations = build_citations(guard_result.filtered_chunks)
    context_block = format_context_block(citations)

    sources_used = sorted({c.source for c in citations})
    logger.debug(
        f"Generating from {len(citations)} citations across {len(sources_used)} source(s): "
        f"{sources_used}"
    )

    # ── Step 4: Call the LLM ──────────────────────────────────────────────────
    # Grounding instructions + context go in the system message on every call,
    # so multi-turn chats are re-grounded each turn — no stale context risk.
    messages: List[dict] = [{"role": "system", "content": _build_system_prompt(context_block)}]
    if history:
        messages.extend(_history_to_messages(history))
    messages.append({"role": "user", "content": query})

    try:
        raw_answer = complete(messages)
    except LLMError as exc:
        logger.error(f"Generation failed ({settings.LLM_PROVIDER}/{model_name()}): {exc}")
        raise RuntimeError(f"Generation failed: {exc}") from exc

    answer_text = _strip_citation_markers(raw_answer, citations)

    if _is_model_refusal(answer_text):
        logger.info("Model found the retrieved context insufficient — returning a refusal")
        return GenerationResult(
            answer      = settings.LOW_CONFIDENCE_RESPONSE,
            citations   = [],
            confidence  = ConfidenceLevel.INSUFFICIENT,
            answered    = False,
            query       = query,
            sources_used= [],
        )

    logger.success(
        f"Generated answer ({len(answer_text)} chars) | "
        f"confidence: {guard_result.confidence} | "
        f"sources: {sources_used}"
    )

    return GenerationResult(
        answer      = answer_text,
        citations   = citations,
        confidence  = guard_result.confidence,
        answered    = True,
        query       = query,
        sources_used= sources_used,
    )


# ── convenience: guard-only check ─────────────────────────────────────────────

def can_answer(
    query: str,
    sources: Optional[List[str]] = None,
    cik: Optional[str] = None,
    item_filter: Optional[str] = None,
) -> bool:
    """
    Lightweight check: run retrieval + guard but skip generation.
    Useful for the frontend to show a "searching…" state before committing
    to a full generate() call, or for pre-flight validation in tests.

    Returns True if the guard would allow generation, False otherwise.
    """
    chunks = retrieve(query, sources=sources, cik=cik, item_filter=item_filter)
    guard_result = evaluate_chunks(chunks)
    return guard_result.should_answer