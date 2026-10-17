# """
# schemas.py — Pydantic models for every API request and response.

# Keeping all shapes here means:
#   - routes.py stays thin (logic only, no inline type definitions)
#   - frontend devs have one file to read to understand the contract
#   - validation errors are caught at the boundary before any business logic runs

# Naming convention:
#   <Resource>Request   → body of an incoming POST/PUT
#   <Resource>Response  → body of an outgoing 200 response
#   <Resource>Item      → a single record inside a list response
# """

# from __future__ import annotations
# from typing import List, Optional
# from pydantic import BaseModel, Field


# # ── Shared sub-models ──────────────────────────────────────────────────────────

# class CitationItem(BaseModel):
#     """A single source reference attached to an answer."""
#     source: str         = Field(..., description="Filename, e.g. 'contract_a.pdf'")
#     page: int           = Field(..., description="1-based page number")
#     chunk_index: int    = Field(..., description="Index of the chunk within the document")
#     chunk_type: str     = Field(..., description="'paragraph' | 'sentence' | 'fixed'")
#     excerpt: str        = Field(..., description="The exact chunk text that was used")
#     score: float        = Field(..., description="Similarity / re-rank score (0–1)")


# class DocumentItem(BaseModel):
#     """One document currently indexed — used in list and status responses."""
#     source: str         = Field(..., description="Filename")
#     total_pages: int    = Field(..., description="Total pages in the original PDF")
#     chunk_count: int    = Field(..., description="Number of chunks stored in ChromaDB")


# # ── Upload ─────────────────────────────────────────────────────────────────────

# class UploadResponse(BaseModel):
#     """
#     Returned after POST /upload.
#     `skipped` is True when the document was already indexed and force=False.
#     """
#     source: str         = Field(..., description="Filename that was processed")
#     chunks_indexed: int = Field(..., description="Number of chunks written to ChromaDB")
#     skipped: bool       = Field(False, description="True if document already existed and was not re-indexed")
#     message: str        = Field(..., description="Human-readable summary")


# # ── Query ──────────────────────────────────────────────────────────────────────

# class QueryRequest(BaseModel):
#     """
#     Body of POST /query.

#     `sources` is optional — omit it to search across ALL indexed documents,
#     or pass a list of filenames to restrict the search (multi-doc comparison).
#     `force_answer` bypasses the hallucination guard and always generates a
#     response; useful for debugging but should be off in production.
#     """
#     query: str                          = Field(..., min_length=3, description="User question")
#     sources: Optional[List[str]]        = Field(None, description="Restrict search to these filenames")
#     top_k: Optional[int]                = Field(None, ge=1, le=20, description="Override default TOP_K")
#     force_answer: bool                  = Field(False, description="Skip hallucination guard (debug only)")


# class QueryResponse(BaseModel):
#     """
#     Returned after POST /query.

#     `confidence` is one of HIGH | MEDIUM | LOW | INSUFFICIENT.
#     When confidence is INSUFFICIENT, `answer` contains the canned refusal
#     message and `citations` is empty.
#     """
#     answer: str                         = Field(..., description="Generated answer text")
#     confidence: str                     = Field(..., description="HIGH | MEDIUM | LOW | INSUFFICIENT")
#     citations: List[CitationItem]       = Field(default_factory=list)
#     sources_searched: List[str]         = Field(default_factory=list, description="Documents that were queried")
#     query: str                          = Field(..., description="Echo of the original query")


# # ── Multi-document comparison ──────────────────────────────────────────────────

# class CompareRequest(BaseModel):
#     """
#     Body of POST /compare.

#     Ask the same question against multiple documents and get per-document
#     answers side by side.  Requires at least 2 sources.
#     """
#     query: str                          = Field(..., min_length=3)
#     sources: List[str]                  = Field(..., min_length=2, description="At least 2 filenames to compare")


# class DocumentAnswer(BaseModel):
#     """One document's answer within a compare response."""
#     source: str
#     answer: str
#     confidence: str
#     citations: List[CitationItem]


# class CompareResponse(BaseModel):
#     """
#     Returned after POST /compare.
#     `synthesis` is an LLM-generated paragraph summarising the key differences
#     found across all per-document answers.
#     """
#     query: str
#     results: List[DocumentAnswer]
#     synthesis: str                      = Field(..., description="Cross-document summary of differences")


# # ── Documents ─────────────────────────────────────────────────────────────────

# class DocumentListResponse(BaseModel):
#     documents: List[DocumentItem]
#     total: int


# class DeleteResponse(BaseModel):
#     source: str
#     chunks_deleted: int
#     message: str


# # ── Status ────────────────────────────────────────────────────────────────────

# class StatusResponse(BaseModel):
#     status: str                         = Field("ok", description="'ok' when the service is healthy")
#     collection: str
#     total_vectors: int
#     document_count: int
#     version: str                        = Field("0.1.0")

# """
# api/schemas.py — request/response models for the SEC filings pipeline.

# Covers three groups of endpoints:
#   - company search + filing listing   (browse EDGAR)
#   - filing ingestion                  (pull + chunk + embed + store XBRL)
#   - query                             (numeric | computed | qualitative)
# """

# from __future__ import annotations

# from typing import Any, List, Literal, Optional

# from pydantic import BaseModel, Field


# # ── company search ───────────────────────────────────────────────────────────

# class CompanyMatchOut(BaseModel):
#     cik: str
#     ticker: str
#     name: str


# class CompanySearchResponse(BaseModel):
#     query: str
#     results: List[CompanyMatchOut]


# # ── filing listing ───────────────────────────────────────────────────────────

# class FilingMetaOut(BaseModel):
#     cik: str
#     accession_number: str
#     form_type: str
#     filing_date: str
#     report_date: str
#     primary_document: str
#     company_name: str
#     ingested: bool = False   # whether this filing is already in the vector store


# class FilingListResponse(BaseModel):
#     cik: str
#     company_name: str
#     filings: List[FilingMetaOut]


# # ── ingestion ─────────────────────────────────────────────────────────────────

# class IngestFilingRequest(BaseModel):
#     cik: str
#     accession_number: str
#     primary_document: str
#     form_type: str = "10-K"


# class IngestFilingResponse(BaseModel):
#     cik: str
#     accession_number: str
#     company_name: str
#     chunks_indexed: int
#     xbrl_facts_stored: int
#     items_found: List[str]   # e.g. ["item_1", "item_1a", "item_7", "item_8"]
#     status: Literal["success", "partial", "failed"]
#     message: Optional[str] = None


# # ── query ─────────────────────────────────────────────────────────────────────

# class QueryRequest(BaseModel):
#     question: str
#     cik: Optional[str] = None                 # scope to one company if provided
#     accession_numbers: Optional[List[str]] = None  # scope to specific filing(s)
#     item_filter: Optional[str] = None          # e.g. "item_7" to scope qualitative search


# class Citation(BaseModel):
#     chunk_id: str
#     cik: str
#     company_name: str
#     accession_number: str
#     item: Optional[str] = None
#     item_title: Optional[str] = None
#     excerpt: str
#     score: Optional[float] = None


# class NumericFact(BaseModel):
#     metric: str
#     company_name: str
#     fiscal_year: Optional[int] = None
#     fiscal_period: Optional[str] = None
#     value: float
#     unit: str
#     source_accession: str


# class QueryResponse(BaseModel):
#     question: str
#     query_type: Literal["numeric", "computed", "qualitative"]
#     answer: str
#     confidence: float
#     citations: List[Citation] = Field(default_factory=list)
#     facts_used: List[NumericFact] = Field(default_factory=list)
#     low_confidence: bool = False


# # ── compare / multi-doc ─────────────────────────────────────────────────────

# class CompareRequest(BaseModel):
#     question: str
#     targets: List[IngestFilingRequest]   # 2+ filings to compare across


# class CompareResponse(BaseModel):
#     question: str
#     answer: str
#     facts_used: List[NumericFact] = Field(default_factory=list)
#     citations: List[Citation] = Field(default_factory=list)

"""
api/schemas.py — request/response models for the SEC filings pipeline.

Covers three groups of endpoints:
  - company search + filing listing   (browse EDGAR)
  - filing ingestion                  (pull + chunk + embed + store XBRL)
  - query                             (numeric | computed | qualitative)
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field


# ── company search ───────────────────────────────────────────────────────────

class CompanyMatchOut(BaseModel):
    cik: str
    ticker: str
    name: str


class CompanySearchResponse(BaseModel):
    query: str
    results: List[CompanyMatchOut]


# ── filing listing ───────────────────────────────────────────────────────────

class FilingMetaOut(BaseModel):
    cik: str
    accession_number: str
    form_type: str
    filing_date: str
    report_date: str
    primary_document: str
    company_name: str
    ingested: bool = False   # whether this filing is already in the vector store


class FilingListResponse(BaseModel):
    cik: str
    company_name: str
    filings: List[FilingMetaOut]


# ── ingestion ─────────────────────────────────────────────────────────────────

class IngestFilingRequest(BaseModel):
    cik: str
    accession_number: str
    primary_document: str
    form_type: str = "10-K"


class IngestFilingResponse(BaseModel):
    cik: str
    accession_number: str
    company_name: str
    chunks_indexed: int
    xbrl_facts_stored: int
    items_found: List[str]   # e.g. ["1", "1A", "7", "8"] — matches filing_chunker's item tags
    status: Literal["success", "partial", "failed"]
    message: Optional[str] = None


# ── query ─────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    cik: Optional[str] = None                 # scope to one company if provided
    accession_numbers: Optional[List[str]] = None  # scope to specific filing(s)
    item_filter: Optional[str] = None          # e.g. "1A" to scope qualitative search — matches filing_chunker's item tags


class Citation(BaseModel):
    """Matches Citation.to_dict() from citation_builder.py (post citation_builder_patch.py)."""
    id: str
    source: str            # accession_number for filings
    page: int = 0          # always 0 for filings — kept for schema compatibility with generic-PDF path
    chunk_index: int = 0
    chunk_type: str = "unknown"
    excerpt: str = ""
    score: float = 0.0
    bi_encoder_score: float = 0.0
    rerank_position: int = 1
    total_pages: int = 0
    cik: str = ""
    company_name: str = ""
    accession_number: str = ""
    form_type: str = ""
    item: str = ""
    item_title: str = ""


class NumericFact(BaseModel):
    metric: str
    company_name: str = ""   # structured_store.MetricFact doesn't carry this — see routes.py note
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None
    value: float
    unit: str
    source_accession: str = ""


class QueryResponse(BaseModel):
    question: str
    query_type: Literal["numeric", "computed", "qualitative"]
    answer: str
    # ConfidenceLevel from guard.py: "HIGH" | "MEDIUM" | "LOW" | "INSUFFICIENT".
    # Numeric/computed answers (exact structured facts, no LLM/retrieval involved)
    # use "HIGH" unconditionally — see routes.py.
    confidence: Literal["HIGH", "MEDIUM", "LOW", "INSUFFICIENT"]
    citations: List[Citation] = Field(default_factory=list)
    facts_used: List[NumericFact] = Field(default_factory=list)
    low_confidence: bool = False   # True when confidence == "INSUFFICIENT"


# ── compare / multi-doc ─────────────────────────────────────────────────────

class CompareRequest(BaseModel):
    question: str
    targets: List[IngestFilingRequest]   # 2+ filings to compare across


class CompareResponse(BaseModel):
    question: str
    answer: str
    facts_used: List[NumericFact] = Field(default_factory=list)
    citations: List[Citation] = Field(default_factory=list)

class DocumentSummary(BaseModel):
    source: str            # accession_number
    cik: str
    company_name: str
    form_type: str
    chunk_count: int

class DocumentListResponse(BaseModel):
    documents: list[DocumentSummary]