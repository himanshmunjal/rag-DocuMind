"""
schemas.py — Pydantic models for every API request and response.

Keeping all shapes here means:
  - routes.py stays thin (logic only, no inline type definitions)
  - frontend devs have one file to read to understand the contract
  - validation errors are caught at the boundary before any business logic runs

Naming convention:
  <Resource>Request   → body of an incoming POST/PUT
  <Resource>Response  → body of an outgoing 200 response
  <Resource>Item      → a single record inside a list response
"""

from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field


# ── Shared sub-models ──────────────────────────────────────────────────────────

class CitationItem(BaseModel):
    """A single source reference attached to an answer."""
    source: str         = Field(..., description="Filename, e.g. 'contract_a.pdf'")
    page: int           = Field(..., description="1-based page number")
    chunk_index: int    = Field(..., description="Index of the chunk within the document")
    chunk_type: str     = Field(..., description="'paragraph' | 'sentence' | 'fixed'")
    excerpt: str        = Field(..., description="The exact chunk text that was used")
    score: float        = Field(..., description="Similarity / re-rank score (0–1)")


class DocumentItem(BaseModel):
    """One document currently indexed — used in list and status responses."""
    source: str         = Field(..., description="Filename")
    total_pages: int    = Field(..., description="Total pages in the original PDF")
    chunk_count: int    = Field(..., description="Number of chunks stored in ChromaDB")


# ── Upload ─────────────────────────────────────────────────────────────────────

class UploadResponse(BaseModel):
    """
    Returned after POST /upload.
    `skipped` is True when the document was already indexed and force=False.
    """
    source: str         = Field(..., description="Filename that was processed")
    chunks_indexed: int = Field(..., description="Number of chunks written to ChromaDB")
    skipped: bool       = Field(False, description="True if document already existed and was not re-indexed")
    message: str        = Field(..., description="Human-readable summary")


# ── Query ──────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    """
    Body of POST /query.

    `sources` is optional — omit it to search across ALL indexed documents,
    or pass a list of filenames to restrict the search (multi-doc comparison).
    `force_answer` bypasses the hallucination guard and always generates a
    response; useful for debugging but should be off in production.
    """
    query: str                          = Field(..., min_length=3, description="User question")
    sources: Optional[List[str]]        = Field(None, description="Restrict search to these filenames")
    top_k: Optional[int]                = Field(None, ge=1, le=20, description="Override default TOP_K")
    force_answer: bool                  = Field(False, description="Skip hallucination guard (debug only)")


class QueryResponse(BaseModel):
    """
    Returned after POST /query.

    `confidence` is one of HIGH | MEDIUM | LOW | INSUFFICIENT.
    When confidence is INSUFFICIENT, `answer` contains the canned refusal
    message and `citations` is empty.
    """
    answer: str                         = Field(..., description="Generated answer text")
    confidence: str                     = Field(..., description="HIGH | MEDIUM | LOW | INSUFFICIENT")
    citations: List[CitationItem]       = Field(default_factory=list)
    sources_searched: List[str]         = Field(default_factory=list, description="Documents that were queried")
    query: str                          = Field(..., description="Echo of the original query")


# ── Multi-document comparison ──────────────────────────────────────────────────

class CompareRequest(BaseModel):
    """
    Body of POST /compare.

    Ask the same question against multiple documents and get per-document
    answers side by side.  Requires at least 2 sources.
    """
    query: str                          = Field(..., min_length=3)
    sources: List[str]                  = Field(..., min_length=2, description="At least 2 filenames to compare")


class DocumentAnswer(BaseModel):
    """One document's answer within a compare response."""
    source: str
    answer: str
    confidence: str
    citations: List[CitationItem]


class CompareResponse(BaseModel):
    """
    Returned after POST /compare.
    `synthesis` is an LLM-generated paragraph summarising the key differences
    found across all per-document answers.
    """
    query: str
    results: List[DocumentAnswer]
    synthesis: str                      = Field(..., description="Cross-document summary of differences")


# ── Documents ─────────────────────────────────────────────────────────────────

class DocumentListResponse(BaseModel):
    documents: List[DocumentItem]
    total: int


class DeleteResponse(BaseModel):
    source: str
    chunks_deleted: int
    message: str


# ── Status ────────────────────────────────────────────────────────────────────

class StatusResponse(BaseModel):
    status: str                         = Field("ok", description="'ok' when the service is healthy")
    collection: str
    total_vectors: int
    document_count: int
    version: str                        = Field("0.1.0")