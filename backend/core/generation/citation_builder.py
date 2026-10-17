"""
Citation Builder

Transforms raw chunk dicts (from reranker.py) into structured Citation
objects that the UI can render: source filename, page number, a text
excerpt, relevance score, and a stable anchor ID for scroll-to-highlight.

Written before generator.py because the generator builds its prompt using
citation markers that reference Citation objects produced here.

Design decisions:

  Excerpt trimming
  ----------------
  Chunks can be 400-500 tokens long. Showing the full text in a sidebar
  citation panel is overwhelming. We trim to the most relevant sentence
  window (EXCERPT_MAX_CHARS) while keeping the excerpt coherent — truncating
  mid-sentence is jarring, so we break on the last full stop before the limit.

  Citation IDs
  ------------
  Each Citation gets a stable `id` string like "src-contract_a-p3-c7".
  The generator embeds these markers inline in its response text as [src-…].
  The frontend uses them to link answer text spans to sidebar cards and to
  highlight the relevant chunk in a document viewer.

  Multi-document grouping
  -----------------------
  build_citations() returns citations in the order they should appear in the
  UI: sorted primarily by rerank_position (most relevant first), then grouped
  so citations from the same document are visually adjacent when the answer
  draws on multiple sources.

  Deduplication
  -------------
  If the reranker returns two chunks from the same page (possible with
  adjacent chunks), we merge them into one citation so the UI doesn't show
  the same page reference twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional
import re

from loguru import logger


# ── constants ─────────────────────────────────────────────────────────────────

EXCERPT_MAX_CHARS: int = 300    # max length of the citation text excerpt
EXCERPT_MIN_CHARS: int = 80     # don't trim shorter than this


# ── Citation dataclass ────────────────────────────────────────────────────────

@dataclass
class Citation:
    """
    Same fields as before, PLUS filing-aware fields (all default to ""/0 so
    the old generic-PDF call sites, if any remain, don't break).
    """
    id:               str
    source:           str    # accession_number for filings (filing_chunker sets metadata["source"] = accession_number)
    page:             int = 0
    chunk_index:      int = 0
    chunk_type:       str = "unknown"
    excerpt:          str = ""
    full_text:        str = ""
    score:            float = 0.0
    bi_encoder_score: float = 0.0
    rerank_position:  int = 1
    total_pages:      int = 0
 
    # ── filing-aware fields (NEW) ──────────────────────────────────────────
    cik:              str = ""
    company_name:     str = ""
    accession_number: str = ""   # same value as `source` for filings — kept separate for clarity in the schema
    form_type:        str = ""
    item:             str = ""
    item_title:       str = ""
 
    def to_dict(self) -> dict:
        return {
            "id":               self.id,
            "source":           self.source,
            "page":             self.page,
            "chunk_index":      self.chunk_index,
            "chunk_type":       self.chunk_type,
            "excerpt":          self.excerpt,
            "score":            self.score,
            "bi_encoder_score": self.bi_encoder_score,
            "rerank_position":  self.rerank_position,
            "total_pages":      self.total_pages,
            "cik":              self.cik,
            "company_name":     self.company_name,
            "accession_number": self.accession_number,
            "form_type":        self.form_type,
            "item":             self.item,
            "item_title":       self.item_title,
        }
 
 
def _citation_from_chunk(chunk: dict, existing_count: int) -> "Citation":
    """
    Replacement for the body of the for-loop inside build_citations().
    Swap the old loop body for this.
    """
    meta = chunk.get("metadata", {})
 
    source      = str(meta.get("source",      "unknown"))
    page        = int(meta.get("page",         0))
    chunk_index = int(meta.get("chunk_index",  0))
    chunk_type  = str(meta.get("chunk_type",   "unknown"))
    total_pages = int(meta.get("total_pages",  0))
 
    return Citation(
        id               = _make_citation_id_for_filing(meta, chunk_index),
        source           = source,
        page             = page,
        chunk_index      = chunk_index,
        chunk_type       = chunk_type,
        excerpt          = "",   # filled by caller via _trim_excerpt(chunk.get("text",""))
        score            = round(float(chunk.get("score",            0.0)), 4),
        bi_encoder_score = round(float(chunk.get("bi_encoder_score", 0.0)), 4),
        rerank_position  = int(chunk.get("rerank_position", existing_count + 1)),
        total_pages      = total_pages,
        cik              = str(meta.get("cik", "")),
        company_name     = str(meta.get("company_name", "")),
        accession_number = str(meta.get("source", "")),   # filing_chunker sets "source" = accession_number
        form_type        = str(meta.get("form_type", "")),
        item             = str(meta.get("item", "")),
        item_title       = str(meta.get("item_title", "")),
    )
 
 
def _make_citation_id_for_filing(meta: dict, chunk_index: int) -> str:
    """
    Replacement for _make_citation_id() — filings don't have pages, so build
    the anchor from accession_number + item instead:
        "src-0000320193-25-000079-item7A-c12"
    Falls back to the old page-based scheme if item is missing (flat-chunked
    8-Ks etc.), so nothing breaks for non-10-K/10-Q filings.
    """
    import re
 
    accession = str(meta.get("source", "unknown"))
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", accession)
    item = meta.get("item")
 
    if item:
        item_slug = re.sub(r"[^a-zA-Z0-9]", "", str(item))
        return f"src-{stem}-item{item_slug}-c{chunk_index}"
 
    page = meta.get("page", 0)
    return f"src-{stem}-p{page}-c{chunk_index}"

# ── internal helpers ──────────────────────────────────────────────────────────

def _make_citation_id(source: str, page: int, chunk_index: int) -> str:
    """
    Build a stable, URL-safe anchor ID from source + location.
    Example: "contract_a.pdf", page=3, chunk=7 → "src-contract_a-p3-c7"
    """
    # Strip extension, replace spaces and dots with underscores
    stem = re.sub(r"[^a-zA-Z0-9_-]", "_", source.rsplit(".", 1)[0])
    return f"src-{stem}-p{page}-c{chunk_index}"


def _trim_excerpt(text: str, max_chars: int = EXCERPT_MAX_CHARS) -> str:
    """
    Trim chunk text to a readable excerpt.

    Strategy:
      1. If text fits within max_chars, return as-is (cleaned up).
      2. Otherwise find the last sentence boundary (. ! ?) before max_chars.
      3. Append "…" to signal truncation.
      4. If no sentence boundary found, hard-cut at max_chars with "…".
    """
    text = text.strip()

    if len(text) <= max_chars:
        return text

    # Look for a sentence-ending punctuation before the limit
    window = text[:max_chars]
    last_break = max(
        window.rfind(". "),
        window.rfind("! "),
        window.rfind("? "),
    )

    if last_break >= EXCERPT_MIN_CHARS:
        return text[: last_break + 1].strip() + " …"

    # No clean sentence break — hard cut
    return window.rstrip() + " …"


def _deduplicate_citations(citations: List[Citation]) -> List[Citation]:
    """
    Merge citations that point to the same (source, page).
    When two chunks come from the same page, keep the one with the higher
    score and merge their excerpts if they are non-overlapping.
    """
    seen: dict[tuple, Citation] = {}

    for cit in citations:
        key = (cit.source, cit.page)
        if key not in seen:
            seen[key] = cit
        else:
            existing = seen[key]
            if cit.score > existing.score:
                # Keep the higher-scored citation but try to enrich its excerpt
                if cit.excerpt not in existing.excerpt:
                    merged_excerpt = _trim_excerpt(
                        existing.excerpt.rstrip(" …") + " … " + cit.excerpt,
                        max_chars=EXCERPT_MAX_CHARS + 100,   # slightly more room for merged
                    )
                    existing.excerpt = merged_excerpt
                seen[key] = cit   # replace with higher-scored one
            else:
                # Existing is higher-scored — just try to enrich it
                if cit.excerpt not in existing.excerpt:
                    existing.excerpt = _trim_excerpt(
                        existing.excerpt.rstrip(" …") + " … " + cit.excerpt,
                        max_chars=EXCERPT_MAX_CHARS + 100,
                    )

    return list(seen.values())


# ── public API ────────────────────────────────────────────────────────────────

def build_citations(chunks: List[dict]) -> List["Citation"]:
    """
    Same overall structure as your existing build_citations() — same
    _trim_excerpt() call, same _deduplicate_citations() + sort at the end.
    Only the per-chunk construction changes (use _citation_from_chunk above
    instead of building Citation(...) inline), and the dedup key needs a
    one-line change:
 
        OLD: key = (cit.source, cit.page)
        NEW: key = (cit.source, cit.page, cit.item)
 
    Reason: for filings, page is always 0, so the old key would collapse
    ALL chunks from the same filing into one citation regardless of which
    Item they came from — losing exactly the structure you built
    filing_chunker.py to preserve. Keying on (source, page, item) keeps
    filing citations distinct per-Item while still deduping true
    same-page-same-item repeats.
    """
    if not chunks:
        return []

    citations: List[Citation] = []
    for chunk in chunks:
        cit = _citation_from_chunk(chunk, len(citations))
        text = chunk.get("text", "")
        cit.excerpt = _trim_excerpt(text)   # short — UI preview only
        cit.full_text = text                # untruncated — generation context
        citations.append(cit)

    # ... rest unchanged (dedup, sort by rerank_position)
    return citations 

def _trim_excerpt_placeholder(text: str) -> str:
    """Stand-in — use your real _trim_excerpt() from citation_builder.py, unchanged."""
    return text[:300]


def format_context_block(citations: List[Citation]) -> str:
    """
    Format citations as a numbered context block for injection into the
    LLM prompt. Each block is prefixed with a citation marker that the
    generator instructs the model to reference in its answer.

    Output format:
        [src-contract_a-p3-c7] (contract_a.pdf, page 3)
        The termination clause states that either party may ...

        [src-nda_b-p1-c2] (nda_b.pdf, page 1)
        Confidential information means any information disclosed ...

    The generator appends this block to the prompt system instructions,
    and instructs the model: "When you use a piece of context, include
    its citation marker [src-…] inline in your answer."

    Args:
        citations: output of build_citations()

    Returns:
        Multi-line string ready for prompt injection
    """
    if not citations:
        return ""

    lines: List[str] = []
    for cit in citations:
        header = f"[{cit.id}] ({cit.source}, page {cit.page})"
        lines.append(header)
        lines.append(cit.full_text)     # <-- was cit.excerpt
        lines.append("")

    return "\n".join(lines).strip()