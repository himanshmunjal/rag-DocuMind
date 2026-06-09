"""
Generator

Owns the complete query-to-answer pipeline:
    retrieve → guard → cite → generate → return GenerationResult

This is the single function that routes.py calls. Everything upstream
(retrieval, reranking, guard, citation building) is orchestrated here
so the API layer stays thin.

Prompt architecture:
  The prompt uses a strict "only use what I give you" grounding approach.
  The system prompt tells Gemini:
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

import google.generativeai as genai
from loguru import logger

from config import Settings
from core.retrieval.reranker import retrieve
from core.hallucination.guard import evaluate_chunks, ConfidenceLevel
from core.generation.citation_builder import (
    Citation,
    build_citations,
    format_context_block,
)


# ── Gemini client ─────────────────────────────────────────────────────────────

genai.configure(api_key=Settings.GEMINI_API_KEY)

_generation_config = genai.types.GenerationConfig(
    temperature=Settings.TEMPERATURE,
    max_output_tokens=Settings.MAX_TOKENS,
    candidate_count=1,
)


def _get_model() -> genai.GenerativeModel:
    """Return a configured Gemini GenerativeModel (no persistent state)."""
    return genai.GenerativeModel(
        model_name=Settings.LLM_MODEL,
        generation_config=_generation_config,
    )


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
3. If the context does not contain enough information to answer the question, respond with exactly: "The uploaded documents do not contain enough information to answer this question."
4. Do not speculate, extrapolate, or fill gaps with general knowledge.
5. If the answer spans multiple context blocks, cite each one where it is used.
6. Be concise and factual. Use plain prose unless a list genuinely improves clarity.
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


def _build_system_prompt(context_block: str) -> str:
    return _SYSTEM_PROMPT.format(context_block=context_block)


# ── main pipeline ─────────────────────────────────────────────────────────────

def generate(
    query: str,
    sources: Optional[List[str]] = None,
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
                  Format: [{"role": "user"|"model", "parts": [{"text": str}]}, ...]
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
    chunks = retrieve(query, sources=sources, top_k=top_k, top_n=top_n)

    # ── Step 2: Hallucination guard ───────────────────────────────────────────
    guard_result = evaluate_chunks(chunks)

    if not guard_result.should_answer:
        logger.info(f"Guard refused query — confidence: {guard_result.confidence}")
        return GenerationResult(
            answer      = guard_result.reason or Settings.LOW_CONFIDENCE_RESPONSE,
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

    # ── Step 4: Call Gemini ───────────────────────────────────────────────────
    model = _get_model()
    system_instruction = _build_system_prompt(context_block)

    # Build the message list: optional history + current query
    # We inject the system instruction as the first user/model exchange
    # because Gemini Flash doesn't support a separate system role in the API.
    messages: List[dict] = []

    # Prepend grounding instructions as the opening exchange
    messages.append({
        "role": "user",
        "parts": [{"text": system_instruction}],
    })
    messages.append({
        "role": "model",
        "parts": [{"text": "Understood. I will answer only from the provided context and cite every claim."}],
    })

    # Inject conversation history (re-grounding is handled by repeating the
    # system_instruction above on every call — no stale context risk)
    if history:
        messages.extend(history)

    # Current query
    messages.append({
        "role": "user",
        "parts": [{"text": query}],
    })

    try:
        response = model.generate_content(messages)
        answer_text = response.text.strip()
    except Exception as exc:
        logger.error(f"Gemini generation failed: {exc}")
        raise RuntimeError(f"Generation failed: {exc}") from exc

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

def can_answer(query: str, sources: Optional[List[str]] = None) -> bool:
    """
    Lightweight check: run retrieval + guard but skip generation.
    Useful for the frontend to show a "searching…" state before committing
    to a full generate() call, or for pre-flight validation in tests.

    Returns True if the guard would allow generation, False otherwise.
    """
    chunks = retrieve(query, sources=sources)
    guard_result = evaluate_chunks(chunks)
    return guard_result.should_answer