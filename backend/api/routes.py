"""
routes.py — All API route handlers.

Endpoints:
  POST   /upload              Ingest one PDF (multipart/form-data)
  POST   /query               Ask a question across indexed documents
  POST   /compare             Compare same question across multiple documents
  GET    /documents           List all indexed documents
  DELETE /documents/{source}  Remove a document from the index
  GET    /status              Health check + collection stats

Design rules:
  - Routes are thin: validate input → call core modules → return schema
  - No business logic here; all RAG logic lives in core/
  - Every error surfaces as an HTTPException with a clear message
  - Async where I/O can be awaited; sync for CPU-bound embedding/inference
    (FastAPI runs sync handlers in a thread pool automatically)
"""

from __future__ import annotations
import shutil
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Query
from loguru import logger

from config import settings
from api.schemas import (
    UploadResponse,
    QueryRequest, QueryResponse,
    CompareRequest, CompareResponse,
    DocumentListResponse, DocumentItem,
    DeleteResponse,
    StatusResponse,
    CitationItem,
)
from core.ingestion.pdf_loader import load_pdf
from core.ingestion.chunker import chunk_pages
from core.ingestion.embedder import embed_chunks, embed_query
from core.ingestion.indexer import (
    index_chunks,
    document_exists,
    delete_document,
    list_documents,
    get_collection_stats,
)
from core.hallucination.guard import evaluate_chunks, ConfidenceLevel
from core.multi_doc import compare, synthesise


router = APIRouter()

# ── helpers ────────────────────────────────────────────────────────────────────

ALLOWED_CONTENT_TYPES = {"application/pdf"}
MAX_FILE_SIZE_MB = 50


def _validate_pdf(file: UploadFile) -> None:
    if file.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Only PDF files are accepted. Got: {file.content_type}",
        )


def _save_upload(file: UploadFile) -> Path:
    """Save the uploaded file to UPLOAD_DIR and return its path."""
    dest = settings.UPLOAD_DIR / file.filename
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    size_mb = dest.stat().st_size / (1024 * 1024)
    if size_mb > MAX_FILE_SIZE_MB:
        dest.unlink()
        raise HTTPException(
            status_code=413,
            detail=f"File exceeds {MAX_FILE_SIZE_MB} MB limit ({size_mb:.1f} MB)",
        )
    logger.info(f"Saved upload: {dest} ({size_mb:.2f} MB)")
    return dest


def _chunks_to_citations(chunks: list) -> List[CitationItem]:
    return [
        CitationItem(
            source      = c["metadata"]["source"],
            page        = c["metadata"]["page"],
            chunk_index = c["metadata"]["chunk_index"],
            chunk_type  = c["metadata"].get("chunk_type", "unknown"),
            excerpt     = c["text"][:300],
            score       = round(c.get("score", 0.0), 4),
        )
        for c in chunks
    ]


# ── POST /upload ───────────────────────────────────────────────────────────────

@router.post("/upload", response_model=UploadResponse, status_code=201)
def upload_document(
    file: UploadFile = File(...),
    force: bool = Form(False, description="Re-index even if document already exists"),
):
    """
    Upload and index a PDF document.

    - Validates file type and size
    - Skips ingestion if the document is already indexed (unless force=True)
    - Runs the full pipeline: load → chunk → embed → index
    - Returns chunk count and skip status
    """
    _validate_pdf(file)

    source = file.filename

    # Skip re-ingestion unless forced
    if not force and document_exists(source):
        logger.info(f"Document '{source}' already indexed — skipping (force=False)")
        return UploadResponse(
            source        = source,
            chunks_indexed= 0,
            skipped       = True,
            message       = f"'{source}' is already indexed. Pass force=true to re-index.",
        )

    # Persist file to disk
    file_path = _save_upload(file)

    try:
        # Full ingestion pipeline
        pages  = load_pdf(file_path)
        chunks = chunk_pages(pages)
        chunks = embed_chunks(chunks)
        count  = index_chunks(chunks)
    except Exception as exc:
        logger.exception(f"Ingestion failed for '{source}': {exc}")
        # Clean up the saved file so a retry starts fresh
        file_path.unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(exc)}")

    return UploadResponse(
        source        = source,
        chunks_indexed= count,
        skipped       = False,
        message       = f"Successfully indexed '{source}' ({count} chunks).",
    )


# ── POST /query ────────────────────────────────────────────────────────────────

@router.post("/query", response_model=QueryResponse)
def query_documents(body: QueryRequest):
    """
    Ask a question across all (or selected) indexed documents.

    Pipeline:
      embed query → ChromaDB search → hallucination guard →
      (optional: reranker) → Gemini generation → citation builder

    Note: reranker is imported lazily — drop in retrieval/reranker.py and
    uncomment the rerank call when ready (Day 7).
    """
    from core.ingestion.indexer import get_collection

    # Determine which documents to search
    all_docs    = [d["source"] for d in list_documents()]
    sources     = body.sources or all_docs

    if not sources:
        raise HTTPException(
            status_code=404,
            detail="No documents are indexed yet. Upload a PDF first.",
        )

    top_k = body.top_k or settings.TOP_K

    # Embed the query
    try:
        query_vector = embed_query(body.query)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Embedding failed: {str(exc)}")

    # Retrieve from ChromaDB
    collection = get_collection()
    where_filter = {"source": {"$in": sources}} if len(sources) > 1 else {"source": sources[0]}

    results = collection.query(
        query_embeddings=[query_vector],
        n_results=min(top_k, collection.count()),
        where=where_filter,
        include=["documents", "metadatas", "distances"],
    )

    # Unpack results
    chunks = []
    for doc_text, meta, distance in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        chunks.append({
            "text":     doc_text,
            "metadata": meta,
            "score":    round(max(0.0, 1.0 - distance), 4),
        })

    # ── Hallucination guard ────────────────────────────────────────────────────
    guard = evaluate_chunks(chunks)

    if not guard.should_answer and not body.force_answer:
        return QueryResponse(
            query           = body.query,
            answer          = guard.reason,
            confidence      = ConfidenceLevel.INSUFFICIENT,
            citations       = [],
            sources_searched= sources,
        )

    safe_chunks = guard.filtered_chunks if guard.should_answer else chunks

    # ── Generation ─────────────────────────────────────────────────────────────
    # Inline until generator.py is written (Day 6); replace with:
    #   from backend.core.generation.generator import generate
    #   answer = generate(body.query, safe_chunks)
    import google.generativeai as genai
    genai.configure(api_key=settings.GEMINI_API_KEY)

    context = "\n\n---\n\n".join(
        f"[{c['metadata']['source']} | Page {c['metadata']['page']}]\n{c['text']}"
        for c in safe_chunks
    )
    prompt = (
        f"Answer the question using ONLY the document excerpts below. "
        f"Cite the source and page for every claim.\n\n"
        f"QUESTION: {body.query}\n\nEXCERPTS:\n{context}\n\nANSWER:"
    )
    llm    = genai.GenerativeModel(settings.LLM_MODEL)
    resp   = llm.generate_content(
        prompt,
        generation_config=genai.types.GenerationConfig(
            temperature      = settings.TEMPERATURE,
            max_output_tokens= settings.MAX_TOKENS,
        ),
    )
    answer = resp.text.strip()

    return QueryResponse(
        query           = body.query,
        answer          = answer,
        confidence      = guard.confidence,
        citations       = _chunks_to_citations(safe_chunks),
        sources_searched= sources,
    )


# ── POST /compare ──────────────────────────────────────────────────────────────

@router.post("/compare", response_model=CompareResponse)
def compare_documents(body: CompareRequest):
    """
    Run the same query against multiple documents independently,
    then synthesise a cross-document comparison paragraph.

    Requires at least 2 sources.  Each source must be indexed.
    """
    # Validate all requested sources exist
    indexed = {d["source"] for d in list_documents()}
    missing = [s for s in body.sources if s not in indexed]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"These documents are not indexed: {missing}",
        )

    try:
        doc_answers = compare(body.query, body.sources)
        synthesis   = synthesise(body.query, doc_answers)
    except Exception as exc:
        logger.exception(f"Compare failed: {exc}")
        raise HTTPException(status_code=500, detail=str(exc))

    return CompareResponse(
        query     = body.query,
        results   = doc_answers,
        synthesis = synthesis,
    )


# ── GET /documents ─────────────────────────────────────────────────────────────

@router.get("/documents", response_model=DocumentListResponse)
def list_indexed_documents():
    """Return all documents currently in the vector store."""
    docs = list_documents()
    return DocumentListResponse(
        documents=[DocumentItem(**d) for d in docs],
        total=len(docs),
    )


# ── DELETE /documents/{source} ────────────────────────────────────────────────

@router.delete("/documents/{source}", response_model=DeleteResponse)
def remove_document(source: str):
    """
    Delete all chunks for a document from ChromaDB.
    Also removes the original PDF from the upload directory.
    """
    if not document_exists(source):
        raise HTTPException(
            status_code=404,
            detail=f"Document '{source}' not found in the index.",
        )

    deleted = delete_document(source)

    # Remove the file from disk too (best-effort)
    file_path = settings.UPLOAD_DIR / source
    if file_path.exists():
        file_path.unlink()
        logger.info(f"Removed file from disk: {file_path}")

    return DeleteResponse(
        source         = source,
        chunks_deleted = deleted,
        message        = f"Removed '{source}' and {deleted} chunks.",
    )


# ── GET /status ────────────────────────────────────────────────────────────────

@router.get("/status", response_model=StatusResponse)
def service_status():
    """Health check — confirms ChromaDB is reachable and reports collection stats."""
    try:
        stats = get_collection_stats()
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"ChromaDB unavailable: {str(exc)}",
        )
    return StatusResponse(**stats)