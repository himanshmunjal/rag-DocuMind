# # """
# # routes.py — All API route handlers.

# # Endpoints:
# #   POST   /upload              Ingest one PDF (multipart/form-data)
# #   POST   /query               Ask a question across indexed documents
# #   POST   /compare             Compare same question across multiple documents
# #   GET    /documents           List all indexed documents
# #   DELETE /documents/{source}  Remove a document from the index
# #   GET    /status              Health check + collection stats

# # Design rules:
# #   - Routes are thin: validate input → call core modules → return schema
# #   - No business logic here; all RAG logic lives in core/
# #   - Every error surfaces as an HTTPException with a clear message
# #   - Async where I/O can be awaited; sync for CPU-bound embedding/inference
# #     (FastAPI runs sync handlers in a thread pool automatically)
# # """

# # from __future__ import annotations
# # import shutil
# # from pathlib import Path
# # from typing import List, Optional

# # from fastapi import APIRouter, HTTPException, UploadFile, File, Form, Query
# # from loguru import logger

# # from config import settings
# # from api.schemas import (
# #     UploadResponse,
# #     QueryRequest, QueryResponse,
# #     CompareRequest, CompareResponse,
# #     DocumentListResponse, DocumentItem,
# #     DeleteResponse,
# #     StatusResponse,
# #     CitationItem,
# # )
# # from core.ingestion.pdf_loader import load_pdf
# # from core.ingestion.chunker import chunk_pages
# # from core.ingestion.embedder import embed_chunks, embed_query
# # from core.ingestion.indexer import (
# #     index_chunks,
# #     document_exists,
# #     delete_document,
# #     list_documents,
# #     get_collection_stats,
# # )
# # from core.hallucination.guard import evaluate_chunks, ConfidenceLevel
# # from core.multi_doc import compare, synthesise


# # router = APIRouter()

# # # ── helpers ────────────────────────────────────────────────────────────────────

# # ALLOWED_CONTENT_TYPES = {"application/pdf"}
# # MAX_FILE_SIZE_MB = 50


# # def _validate_pdf(file: UploadFile) -> None:
# #     if file.content_type not in ALLOWED_CONTENT_TYPES:
# #         raise HTTPException(
# #             status_code=415,
# #             detail=f"Only PDF files are accepted. Got: {file.content_type}",
# #         )


# # def _save_upload(file: UploadFile) -> Path:
# #     """Save the uploaded file to UPLOAD_DIR and return its path."""
# #     dest = settings.UPLOAD_DIR / file.filename
# #     with dest.open("wb") as f:
# #         shutil.copyfileobj(file.file, f)
# #     size_mb = dest.stat().st_size / (1024 * 1024)
# #     if size_mb > MAX_FILE_SIZE_MB:
# #         dest.unlink()
# #         raise HTTPException(
# #             status_code=413,
# #             detail=f"File exceeds {MAX_FILE_SIZE_MB} MB limit ({size_mb:.1f} MB)",
# #         )
# #     logger.info(f"Saved upload: {dest} ({size_mb:.2f} MB)")
# #     return dest


# # def _chunks_to_citations(chunks: list) -> List[CitationItem]:
# #     return [
# #         CitationItem(
# #             source      = c["metadata"]["source"],
# #             page        = c["metadata"]["page"],
# #             chunk_index = c["metadata"]["chunk_index"],
# #             chunk_type  = c["metadata"].get("chunk_type", "unknown"),
# #             excerpt     = c["text"][:300],
# #             score       = round(c.get("score", 0.0), 4),
# #         )
# #         for c in chunks
# #     ]


# # # ── POST /upload ───────────────────────────────────────────────────────────────

# # @router.post("/upload", response_model=UploadResponse, status_code=201)
# # def upload_document(
# #     file: UploadFile = File(...),
# #     force: bool = Form(False, description="Re-index even if document already exists"),
# # ):
# #     """
# #     Upload and index a PDF document.

# #     - Validates file type and size
# #     - Skips ingestion if the document is already indexed (unless force=True)
# #     - Runs the full pipeline: load → chunk → embed → index
# #     - Returns chunk count and skip status
# #     """
# #     _validate_pdf(file)

# #     source = file.filename

# #     # Skip re-ingestion unless forced
# #     if not force and document_exists(source):
# #         logger.info(f"Document '{source}' already indexed — skipping (force=False)")
# #         return UploadResponse(
# #             source        = source,
# #             chunks_indexed= 0,
# #             skipped       = True,
# #             message       = f"'{source}' is already indexed. Pass force=true to re-index.",
# #         )

# #     # Persist file to disk
# #     file_path = _save_upload(file)

# #     try:
# #         # Full ingestion pipeline
# #         pages  = load_pdf(file_path)
# #         chunks = chunk_pages(pages)
# #         chunks = embed_chunks(chunks)
# #         count  = index_chunks(chunks)
# #     except Exception as exc:
# #         logger.exception(f"Ingestion failed for '{source}': {exc}")
# #         # Clean up the saved file so a retry starts fresh
# #         file_path.unlink(missing_ok=True)
# #         raise HTTPException(status_code=500, detail=f"Ingestion failed: {str(exc)}")

# #     return UploadResponse(
# #         source        = source,
# #         chunks_indexed= count,
# #         skipped       = False,
# #         message       = f"Successfully indexed '{source}' ({count} chunks).",
# #     )


# # # ── POST /query ────────────────────────────────────────────────────────────────

# # @router.post("/query", response_model=QueryResponse)
# # def query_documents(body: QueryRequest):
# #     """
# #     Ask a question across all (or selected) indexed documents.

# #     Pipeline:
# #       embed query → ChromaDB search → hallucination guard →
# #       (optional: reranker) → Gemini generation → citation builder

# #     Note: reranker is imported lazily — drop in retrieval/reranker.py and
# #     uncomment the rerank call when ready (Day 7).
# #     """
# #     from core.ingestion.indexer import get_collection

# #     # Determine which documents to search
# #     all_docs    = [d["source"] for d in list_documents()]
# #     sources     = body.sources or all_docs

# #     if not sources:
# #         raise HTTPException(
# #             status_code=404,
# #             detail="No documents are indexed yet. Upload a PDF first.",
# #         )

# #     top_k = body.top_k or settings.TOP_K

# #     # Embed the query
# #     try:
# #         query_vector = embed_query(body.query)
# #     except Exception as exc:
# #         raise HTTPException(status_code=500, detail=f"Embedding failed: {str(exc)}")

# #     # Retrieve from ChromaDB
# #     collection = get_collection()
# #     where_filter = {"source": {"$in": sources}} if len(sources) > 1 else {"source": sources[0]}

# #     results = collection.query(
# #         query_embeddings=[query_vector],
# #         n_results=min(top_k, collection.count()),
# #         where=where_filter,
# #         include=["documents", "metadatas", "distances"],
# #     )

# #     # Unpack results
# #     chunks = []
# #     for doc_text, meta, distance in zip(
# #         results["documents"][0],
# #         results["metadatas"][0],
# #         results["distances"][0],
# #     ):
# #         chunks.append({
# #             "text":     doc_text,
# #             "metadata": meta,
# #             "score":    round(max(0.0, 1.0 - distance), 4),
# #         })

# #     # ── Hallucination guard ────────────────────────────────────────────────────
# #     guard = evaluate_chunks(chunks)

# #     if not guard.should_answer and not body.force_answer:
# #         return QueryResponse(
# #             query           = body.query,
# #             answer          = guard.reason,
# #             confidence      = ConfidenceLevel.INSUFFICIENT,
# #             citations       = [],
# #             sources_searched= sources,
# #         )

# #     safe_chunks = guard.filtered_chunks if guard.should_answer else chunks

# #     # ── Generation ─────────────────────────────────────────────────────────────
# #     # Inline until generator.py is written (Day 6); replace with:
# #     #   from backend.core.generation.generator import generate
# #     #   answer = generate(body.query, safe_chunks)
# #     import google.generativeai as genai
# #     genai.configure(api_key=settings.GEMINI_API_KEY)

# #     context = "\n\n---\n\n".join(
# #         f"[{c['metadata']['source']} | Page {c['metadata']['page']}]\n{c['text']}"
# #         for c in safe_chunks
# #     )
# #     prompt = (
# #         f"Answer the question using ONLY the document excerpts below. "
# #         f"Cite the source and page for every claim.\n\n"
# #         f"QUESTION: {body.query}\n\nEXCERPTS:\n{context}\n\nANSWER:"
# #     )
# #     llm    = genai.GenerativeModel(settings.LLM_MODEL)
# #     resp   = llm.generate_content(
# #         prompt,
# #         generation_config=genai.types.GenerationConfig(
# #             temperature      = settings.TEMPERATURE,
# #             max_output_tokens= settings.MAX_TOKENS,
# #         ),
# #     )
# #     answer = resp.text.strip()

# #     return QueryResponse(
# #         query           = body.query,
# #         answer          = answer,
# #         confidence      = guard.confidence,
# #         citations       = _chunks_to_citations(safe_chunks),
# #         sources_searched= sources,
# #     )


# # # ── POST /compare ──────────────────────────────────────────────────────────────

# # @router.post("/compare", response_model=CompareResponse)
# # def compare_documents(body: CompareRequest):
# #     """
# #     Run the same query against multiple documents independently,
# #     then synthesise a cross-document comparison paragraph.

# #     Requires at least 2 sources.  Each source must be indexed.
# #     """
# #     # Validate all requested sources exist
# #     indexed = {d["source"] for d in list_documents()}
# #     missing = [s for s in body.sources if s not in indexed]
# #     if missing:
# #         raise HTTPException(
# #             status_code=404,
# #             detail=f"These documents are not indexed: {missing}",
# #         )

# #     try:
# #         doc_answers = compare(body.query, body.sources)
# #         synthesis   = synthesise(body.query, doc_answers)
# #     except Exception as exc:
# #         logger.exception(f"Compare failed: {exc}")
# #         raise HTTPException(status_code=500, detail=str(exc))

# #     return CompareResponse(
# #         query     = body.query,
# #         results   = doc_answers,
# #         synthesis = synthesis,
# #     )


# # # ── GET /documents ─────────────────────────────────────────────────────────────

# # @router.get("/documents", response_model=DocumentListResponse)
# # def list_indexed_documents():
# #     """Return all documents currently in the vector store."""
# #     docs = list_documents()
# #     return DocumentListResponse(
# #         documents=[DocumentItem(**d) for d in docs],
# #         total=len(docs),
# #     )


# # # ── DELETE /documents/{source} ────────────────────────────────────────────────

# # @router.delete("/documents/{source}", response_model=DeleteResponse)
# # def remove_document(source: str):
# #     """
# #     Delete all chunks for a document from ChromaDB.
# #     Also removes the original PDF from the upload directory.
# #     """
# #     if not document_exists(source):
# #         raise HTTPException(
# #             status_code=404,
# #             detail=f"Document '{source}' not found in the index.",
# #         )

# #     deleted = delete_document(source)

# #     # Remove the file from disk too (best-effort)
# #     file_path = settings.UPLOAD_DIR / source
# #     if file_path.exists():
# #         file_path.unlink()
# #         logger.info(f"Removed file from disk: {file_path}")

# #     return DeleteResponse(
# #         source         = source,
# #         chunks_deleted = deleted,
# #         message        = f"Removed '{source}' and {deleted} chunks.",
# #     )


# # # ── GET /status ────────────────────────────────────────────────────────────────

# # @router.get("/status", response_model=StatusResponse)
# # def service_status():
# #     """Health check — confirms ChromaDB is reachable and reports collection stats."""
# #     try:
# #         stats = get_collection_stats()
# #     except Exception as exc:
# #         raise HTTPException(
# #             status_code=503,
# #             detail=f"ChromaDB unavailable: {str(exc)}",
# #         )
# #     return StatusResponse(**stats)

# """
# api/routes.py — FastAPI routes for the SEC filings pipeline.

# ASSUMPTIONS about your Day 1 / Day 2 modules (adjust imports/calls if these
# don't match what you actually wrote):

#   structured_store.py:
#       upsert_facts(cik: str, xbrl_json: dict) -> int          # returns #facts stored
#       get_metric(cik, metric, fiscal_year=None) -> list[dict] # rows: {value, unit, fiscal_year, fiscal_period, source_accession, company_name}

#   filing_chunker.py:
#       chunk_filing(html: str, cik: str, accession_number: str,
#                    form_type: str, company_name: str) -> list[dict]
#       # each chunk dict: {id, text, item, item_title, cik, accession_number, ...}

#   embedder.py / indexer.py (unchanged from before):
#       embed_chunks(chunks: list[dict]) -> list[dict]      # adds "embedding" key
#       index_chunks(chunks: list[dict]) -> None
#       get_collection()

#   retrieval/searcher.py:
#       search(query: str, cik: str | None = None, item_filter: str | None = None,
#              accession_numbers: list[str] | None = None, top_k: int = ...) -> list[dict]

#   retrieval/reranker.py:
#       rerank(query: str, chunks: list[dict], top_n: int = ...) -> list[dict]

#   hallucination/guard.py:
#       check(answer: str, context_chunks: list[dict]) -> dict  # {passed, confidence, ...}

#   generation/generator.py:
#       generate(query: str, context_chunks: list[dict]) -> str

#   generation/citation_builder.py:
#       build_citations(chunks: list[dict]) -> list[dict]

#   query_router.py:
#       classify(question: str) -> Literal["numeric", "computed", "qualitative"]
#       extract_metric_request(question: str) -> dict  # best-effort parse for numeric/computed

#   multi_doc.py:
#       compare(question: str, filings: list[dict]) -> dict  # {answer, facts_used, citations}

# If a name differs, the fix is a one-line import/call change — ping me with
# the actual signature and I'll patch it.
# """

# from __future__ import annotations

# from typing import List

# from fastapi import APIRouter, HTTPException
# from loguru import logger

# from api.schemas import (
#     CompanyMatchOut,
#     CompanySearchResponse,
#     CompareRequest,
#     CompareResponse,
#     Citation,
#     FilingListResponse,
#     FilingMetaOut,
#     IngestFilingRequest,
#     IngestFilingResponse,
#     NumericFact,
#     QueryRequest,
#     QueryResponse,
# )
# from core.ingestion import edgar_client
# from core.ingestion.filing_chunker import chunk_filing
# from core.ingestion.embedder import embed_chunks
# from core.ingestion.indexer import index_chunks, get_collection
# from core.ingestion.structured_store import upsert_facts, get_metric
# from core.retrieval.searcher import search
# from core.retrieval.reranker import rerank
# from core.hallucination.guard import evaluate_chunks as guard_check
# from core.generation.generator import generate
# from core.generation.citation_builder import build_citations
# from core.ingestion.query_router import classify, extract_metric_request
# from core.multi_doc import compare as multi_doc_compare
# from config import settings

# router = APIRouter()


# # ── company search + filing listing ─────────────────────────────────────────

# @router.get("/companies/search", response_model=CompanySearchResponse)
# def search_companies(q: str, limit: int = 10):
#     if not q.strip():
#         raise HTTPException(400, "Query parameter 'q' is required")

#     matches = edgar_client.search_company(q, limit=limit)
#     return CompanySearchResponse(
#         query=q,
#         results=[CompanyMatchOut(**m.to_dict()) for m in matches],
#     )


# @router.get("/companies/{cik}/filings", response_model=FilingListResponse)
# def list_filings(cik: str, form_type: str = "10-K", limit: int = 10):
#     filings = edgar_client.get_filings(cik, form_type=form_type, limit=limit)
#     if not filings:
#         raise HTTPException(404, f"No {form_type} filings found for CIK {cik}")

#     collection = get_collection()
#     ingested_accessions = _get_ingested_accessions(collection, cik)

#     return FilingListResponse(
#         cik=filings[0].cik,
#         company_name=filings[0].company_name,
#         filings=[
#             FilingMetaOut(
#                 **f.to_dict(),
#                 ingested=(f.accession_number in ingested_accessions),
#             )
#             for f in filings
#         ],
#     )


# def _get_ingested_accessions(collection, cik: str) -> set:
#     """Best-effort check of which accession numbers already have chunks indexed."""
#     try:
#         existing = collection.get(where={"cik": cik}, include=["metadatas"])
#         return {m["accession_number"] for m in existing.get("metadatas", []) if m}
#     except Exception as exc:
#         logger.warning(f"Could not check existing ingestion state for CIK {cik}: {exc}")
#         return set()


# # ── ingestion ─────────────────────────────────────────────────────────────────

# @router.post("/filings/ingest", response_model=IngestFilingResponse)
# def ingest_filing(req: IngestFilingRequest):
#     try:
#         html = edgar_client.fetch_filing_document(
#             req.cik, req.accession_number, req.primary_document
#         )
#     except Exception as exc:
#         logger.error(f"Failed to fetch filing document: {exc}")
#         raise HTTPException(502, f"Could not fetch filing from EDGAR: {exc}")

#     # company name isn't in the ingest request, so pull it via submissions once
#     company_name = req.cik
#     try:
#         filings = edgar_client.get_filings(req.cik, form_type=req.form_type, limit=50)
#         match = next((f for f in filings if f.accession_number == req.accession_number), None)
#         if match:
#             company_name = match.company_name
#     except Exception as exc:
#         logger.warning(f"Could not resolve company name for CIK {req.cik}: {exc}")

#     # 1. chunk the filing text
#     try:
#         chunks = chunk_filing(
#             html=html,
#             cik=req.cik,
#             accession_number=req.accession_number,
#             form_type=req.form_type,
#             company_name=company_name,
#         )
#     except Exception as exc:
#         logger.error(f"Chunking failed for {req.cik}/{req.accession_number}: {exc}")
#         raise HTTPException(500, f"Filing chunking failed: {exc}")

#     if not chunks:
#         raise HTTPException(422, "Filing parsed but produced zero chunks — check document format")

#     # 2. embed + index
#     embedded = embed_chunks(chunks)
#     index_chunks(embedded)
#     items_found = sorted({c.get("item", "unknown") for c in chunks})

#     # 3. pull + store XBRL facts (best-effort — don't fail ingestion if this errors)
#     facts_stored = 0
#     status = "success"
#     message = None
#     try:
#         xbrl = edgar_client.fetch_xbrl_facts(req.cik)
#         facts_stored = upsert_facts(req.cik, xbrl)
#     except Exception as exc:
#         logger.warning(f"XBRL facts fetch/store failed for CIK {req.cik}: {exc}")
#         status = "partial"
#         message = f"Text ingested but XBRL facts unavailable: {exc}"

#     return IngestFilingResponse(
#         cik=req.cik,
#         accession_number=req.accession_number,
#         company_name=company_name,
#         chunks_indexed=len(embedded),
#         xbrl_facts_stored=facts_stored,
#         items_found=items_found,
#         status=status,
#         message=message,
#     )


# # ── query ─────────────────────────────────────────────────────────────────────

# @router.post("/query", response_model=QueryResponse)
# def query(req: QueryRequest):
#     if not req.question.strip():
#         raise HTTPException(400, "Question cannot be empty")

#     query_type = classify(req.question)

#     if query_type in ("numeric", "computed"):
#         return _handle_numeric_or_computed(req, query_type)

#     return _handle_qualitative(req, query_type)


# def _handle_numeric_or_computed(req: QueryRequest, query_type: str) -> QueryResponse:
#     parsed = extract_metric_request(req.question)
#     metric = parsed.get("metric")

#     if not metric or not req.cik:
#         # can't resolve a metric/company confidently — fall back to qualitative
#         # rather than guessing, since a wrong numeric answer is worse than a
#         # slower qualitative one.
#         logger.info("Numeric query could not be resolved to metric+cik, falling back to qualitative")
#         return _handle_qualitative(req, "qualitative")

#     rows = get_metric(req.cik, metric, fiscal_year=parsed.get("fiscal_year"))
#     if not rows:
#         return QueryResponse(
#             question=req.question,
#             query_type=query_type,
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     facts = [
#         NumericFact(
#             metric=metric,
#             company_name=r.get("company_name", ""),
#             fiscal_year=r.get("fiscal_year"),
#             fiscal_period=r.get("fiscal_period"),
#             value=r["value"],
#             unit=r.get("unit", "USD"),
#             source_accession=r.get("source_accession", ""),
#         )
#         for r in rows
#     ]

#     if query_type == "computed" and len(facts) >= 2:
#         answer = _compose_computed_answer(req.question, metric, facts)
#     else:
#         f = facts[0]
#         answer = f"{f.company_name}'s {metric} for {f.fiscal_period or ''} {f.fiscal_year or ''}: {f.value:,.0f} {f.unit}.".strip()

#     return QueryResponse(
#         question=req.question,
#         query_type=query_type,
#         answer=answer,
#         confidence=1.0,   # structured facts are exact — no LLM guessing involved
#         facts_used=facts,
#     )


# def _compose_computed_answer(question: str, metric: str, facts: List[NumericFact]) -> str:
#     """Simple YoY-style computation. Extend as more computed question types show up."""
#     sorted_facts = sorted(facts, key=lambda f: (f.fiscal_year or 0))
#     if len(sorted_facts) < 2:
#         return f"Not enough periods of {metric} data to compute a comparison."

#     latest, prior = sorted_facts[-1], sorted_facts[-2]
#     if prior.value == 0:
#         return f"{latest.company_name}'s {metric} in {latest.fiscal_year}: {latest.value:,.0f} {latest.unit} (prior period value was 0, can't compute % change)."

#     pct_change = ((latest.value - prior.value) / abs(prior.value)) * 100
#     direction = "increased" if pct_change >= 0 else "decreased"
#     return (
#         f"{latest.company_name}'s {metric} {direction} by {abs(pct_change):.1f}% "
#         f"from {prior.fiscal_year} ({prior.value:,.0f} {prior.unit}) to "
#         f"{latest.fiscal_year} ({latest.value:,.0f} {latest.unit})."
#     )


# def _handle_qualitative(req: QueryRequest, query_type: str) -> QueryResponse:
#     raw_chunks = search(
#         query=req.question,
#         cik=req.cik,
#         item_filter=req.item_filter,
#         accession_numbers=req.accession_numbers,
#         top_k=settings.TOP_K,
#     )

#     if not raw_chunks:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     top_chunks = rerank(req.question, raw_chunks, top_n=settings.RERANK_TOP_N)
#     top_chunks = [c for c in top_chunks if c.get("score", 0) >= settings.SIMILARITY_THRESHOLD]

#     if not top_chunks:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     answer = generate(req.question, top_chunks)
#     guard_result = guard_check(answer, top_chunks)

#     if not guard_result.get("passed", True) or guard_result.get("confidence", 1.0) < settings.CONFIDENCE_THRESHOLD:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=guard_result.get("confidence", 0.0),
#             low_confidence=True,
#             citations=[Citation(**c) for c in build_citations(top_chunks)],
#         )

#     return QueryResponse(
#         question=req.question,
#         query_type="qualitative",
#         answer=answer,
#         confidence=guard_result.get("confidence", 1.0),
#         citations=[Citation(**c) for c in build_citations(top_chunks)],
#     )


# # ── compare / multi-doc ─────────────────────────────────────────────────────

# @router.post("/filings/compare", response_model=CompareResponse)
# def compare_filings(req: CompareRequest):
#     if len(req.targets) < 2:
#         raise HTTPException(400, "Comparison requires at least 2 filings")

#     result = multi_doc_compare(req.question, [t.model_dump() for t in req.targets])
#     return CompareResponse(
#         question=req.question,
#         answer=result["answer"],
#         facts_used=[NumericFact(**f) for f in result.get("facts_used", [])],
#         citations=[Citation(**c) for c in result.get("citations", [])],
#     )

"""
api/routes.py — FastAPI routes for the SEC filings pipeline.

ASSUMPTIONS about your Day 1 / Day 2 modules (adjust imports/calls if these
don't match what you actually wrote):

  structured_store.py:
      upsert_facts(cik: str, xbrl_json: dict) -> int          # returns #facts stored
      get_metric(cik, metric, fiscal_year=None) -> list[dict] # rows: {value, unit, fiscal_year, fiscal_period, source_accession, company_name}

  filing_chunker.py:
      chunk_filing(html: str, filing_meta: dict) -> list[dict]
      # filing_meta expects: {cik, accession_number, form_type, company_name, primary_document}
      # each returned chunk dict: {id, text, item, item_title, cik, accession_number, ...}

  embedder.py / indexer.py (unchanged from before):
      embed_chunks(chunks: list[dict]) -> list[dict]      # adds "embedding" key
      index_chunks(chunks: list[dict]) -> None
      get_collection()

  retrieval/searcher.py:
      search(query: str, cik: str | None = None, item_filter: str | None = None,
             accession_numbers: list[str] | None = None, top_k: int = ...) -> list[dict]

  retrieval/reranker.py:
      rerank(query: str, chunks: list[dict], top_n: int = ...) -> list[dict]

  hallucination/guard.py:
      check(answer: str, context_chunks: list[dict]) -> dict  # {passed, confidence, ...}

  generation/generator.py:
      generate(query: str, context_chunks: list[dict]) -> str

  generation/citation_builder.py:
      build_citations(chunks: list[dict]) -> list[dict]

  query_router.py:
      classify(question: str) -> Literal["numeric", "computed", "qualitative"]
      extract_metric_request(question: str) -> dict  # best-effort parse for numeric/computed

  multi_doc.py:
      compare(question: str, filings: list[dict]) -> dict  # {answer, facts_used, citations}

If a name differs, the fix is a one-line import/call change — ping me with
the actual signature and I'll patch it.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException
from loguru import logger

from api.schemas import (
    CompanyMatchOut,
    CompanySearchResponse,
    CompareRequest,
    CompareResponse,
    Citation,
    FilingListResponse,
    FilingMetaOut,
    IngestFilingRequest,
    IngestFilingResponse,
    NumericFact,
    QueryRequest,
    QueryResponse,
)
from core.ingestion import edgar_client
from core.ingestion.filing_chunker import chunk_filing
from core.ingestion.embedder import embed_chunks
from core.ingestion.indexer import index_chunks, get_collection
from core.ingestion.structured_store import upsert_facts, get_metric
from core.retrieval.searcher import search
from core.retrieval.reranker import rerank
from core.hallucination.guard import evaluate_chunks as guard_check
from core.generation.generator import generate
from core.generation.citation_builder import build_citations
from core.ingestion.query_router import classify, extract_metric_request
from core.multi_doc import compare as multi_doc_compare
from config import settings

router = APIRouter()


# ── company search + filing listing ─────────────────────────────────────────

@router.get("/companies/search", response_model=CompanySearchResponse)
def search_companies(q: str, limit: int = 10):
    if not q.strip():
        raise HTTPException(400, "Query parameter 'q' is required")

    matches = edgar_client.search_company(q, limit=limit)
    return CompanySearchResponse(
        query=q,
        results=[CompanyMatchOut(**m.to_dict()) for m in matches],
    )


@router.get("/companies/{cik}/filings", response_model=FilingListResponse)
def list_filings(cik: str, form_type: str = "10-K", limit: int = 10):
    filings = edgar_client.get_filings(cik, form_type=form_type, limit=limit)
    if not filings:
        raise HTTPException(404, f"No {form_type} filings found for CIK {cik}")

    collection = get_collection()
    ingested_accessions = _get_ingested_accessions(collection, cik)

    return FilingListResponse(
        cik=filings[0].cik,
        company_name=filings[0].company_name,
        filings=[
            FilingMetaOut(
                **f.to_dict(),
                ingested=(f.accession_number in ingested_accessions),
            )
            for f in filings
        ],
    )


def _get_ingested_accessions(collection, cik: str) -> set:
    """Best-effort check of which accession numbers already have chunks indexed."""
    try:
        existing = collection.get(where={"cik": cik}, include=["metadatas"])
        return {m["accession_number"] for m in existing.get("metadatas", []) if m}
    except Exception as exc:
        logger.warning(f"Could not check existing ingestion state for CIK {cik}: {exc}")
        return set()


# ── ingestion ─────────────────────────────────────────────────────────────────

@router.post("/filings/ingest", response_model=IngestFilingResponse)
def ingest_filing(req: IngestFilingRequest):
    try:
        html = edgar_client.fetch_filing_document(
            req.cik, req.accession_number, req.primary_document
        )
    except Exception as exc:
        logger.error(f"Failed to fetch filing document: {exc}")
        raise HTTPException(502, f"Could not fetch filing from EDGAR: {exc}")

    # company name isn't in the ingest request, so pull it via submissions once
    company_name = req.cik
    try:
        filings = edgar_client.get_filings(req.cik, form_type=req.form_type, limit=50)
        match = next((f for f in filings if f.accession_number == req.accession_number), None)
        if match:
            company_name = match.company_name
    except Exception as exc:
        logger.warning(f"Could not resolve company name for CIK {req.cik}: {exc}")

    # 1. chunk the filing text
    filing_meta = {
        "cik": req.cik,
        "accession_number": req.accession_number,
        "form_type": req.form_type,
        "company_name": company_name,
        "primary_document": req.primary_document,
    }
    try:
        chunks = chunk_filing(html=html, filing_meta=filing_meta)
    except Exception as exc:
        logger.error(f"Chunking failed for {req.cik}/{req.accession_number}: {exc}")
        raise HTTPException(500, f"Filing chunking failed: {exc}")

    if not chunks:
        raise HTTPException(422, "Filing parsed but produced zero chunks — check document format")

    # 2. embed + index
    embedded = embed_chunks(chunks)
    index_chunks(embedded)
    items_found = sorted({c.get("item", "unknown") for c in chunks})

    # 3. pull + store XBRL facts (best-effort — don't fail ingestion if this errors)
    facts_stored = 0
    status = "success"
    message = None
    try:
        xbrl = edgar_client.fetch_xbrl_facts(req.cik)
        facts_stored = upsert_facts(req.cik, xbrl)
    except Exception as exc:
        logger.warning(f"XBRL facts fetch/store failed for CIK {req.cik}: {exc}")
        status = "partial"
        message = f"Text ingested but XBRL facts unavailable: {exc}"

    return IngestFilingResponse(
        cik=req.cik,
        accession_number=req.accession_number,
        company_name=company_name,
        chunks_indexed=len(embedded),
        xbrl_facts_stored=facts_stored,
        items_found=items_found,
        status=status,
        message=message,
    )


# ── query ─────────────────────────────────────────────────────────────────────

@router.post("/query", response_model=QueryResponse)
def query(req: QueryRequest):
    if not req.question.strip():
        raise HTTPException(400, "Question cannot be empty")

    query_type = classify(req.question)

    if query_type in ("numeric", "computed"):
        return _handle_numeric_or_computed(req, query_type)

    return _handle_qualitative(req, query_type)


def _handle_numeric_or_computed(req: QueryRequest, query_type: str) -> QueryResponse:
    parsed = extract_metric_request(req.question)
    metric = parsed.get("metric")

    if not metric or not req.cik:
        # can't resolve a metric/company confidently — fall back to qualitative
        # rather than guessing, since a wrong numeric answer is worse than a
        # slower qualitative one.
        logger.info("Numeric query could not be resolved to metric+cik, falling back to qualitative")
        return _handle_qualitative(req, "qualitative")

    rows = get_metric(req.cik, metric, fiscal_year=parsed.get("fiscal_year"))
    if not rows:
        return QueryResponse(
            question=req.question,
            query_type=query_type,
            answer=settings.LOW_CONFIDENCE_RESPONSE,
            confidence=0.0,
            low_confidence=True,
        )

    facts = [
        NumericFact(
            metric=metric,
            company_name=r.get("company_name", ""),
            fiscal_year=r.get("fiscal_year"),
            fiscal_period=r.get("fiscal_period"),
            value=r["value"],
            unit=r.get("unit", "USD"),
            source_accession=r.get("source_accession", ""),
        )
        for r in rows
    ]

    if query_type == "computed" and len(facts) >= 2:
        answer = _compose_computed_answer(req.question, metric, facts)
    else:
        f = facts[0]
        answer = f"{f.company_name}'s {metric} for {f.fiscal_period or ''} {f.fiscal_year or ''}: {f.value:,.0f} {f.unit}.".strip()

    return QueryResponse(
        question=req.question,
        query_type=query_type,
        answer=answer,
        confidence=1.0,   # structured facts are exact — no LLM guessing involved
        facts_used=facts,
    )


def _compose_computed_answer(question: str, metric: str, facts: List[NumericFact]) -> str:
    """Simple YoY-style computation. Extend as more computed question types show up."""
    sorted_facts = sorted(facts, key=lambda f: (f.fiscal_year or 0))
    if len(sorted_facts) < 2:
        return f"Not enough periods of {metric} data to compute a comparison."

    latest, prior = sorted_facts[-1], sorted_facts[-2]
    if prior.value == 0:
        return f"{latest.company_name}'s {metric} in {latest.fiscal_year}: {latest.value:,.0f} {latest.unit} (prior period value was 0, can't compute % change)."

    pct_change = ((latest.value - prior.value) / abs(prior.value)) * 100
    direction = "increased" if pct_change >= 0 else "decreased"
    return (
        f"{latest.company_name}'s {metric} {direction} by {abs(pct_change):.1f}% "
        f"from {prior.fiscal_year} ({prior.value:,.0f} {prior.unit}) to "
        f"{latest.fiscal_year} ({latest.value:,.0f} {latest.unit})."
    )


def _handle_qualitative(req: QueryRequest, query_type: str) -> QueryResponse:
    raw_chunks = search(
        query=req.question,
        cik=req.cik,
        item_filter=req.item_filter,
        accession_numbers=req.accession_numbers,
        top_k=settings.TOP_K,
    )

    if not raw_chunks:
        return QueryResponse(
            question=req.question,
            query_type="qualitative",
            answer=settings.LOW_CONFIDENCE_RESPONSE,
            confidence=0.0,
            low_confidence=True,
        )

    top_chunks = rerank(req.question, raw_chunks, top_n=settings.RERANK_TOP_N)
    top_chunks = [c for c in top_chunks if c.get("score", 0) >= settings.SIMILARITY_THRESHOLD]

    if not top_chunks:
        return QueryResponse(
            question=req.question,
            query_type="qualitative",
            answer=settings.LOW_CONFIDENCE_RESPONSE,
            confidence=0.0,
            low_confidence=True,
        )

    answer = generate(req.question, top_chunks)
    guard_result = guard_check(answer, top_chunks)

    if not guard_result.get("passed", True) or guard_result.get("confidence", 1.0) < settings.CONFIDENCE_THRESHOLD:
        return QueryResponse(
            question=req.question,
            query_type="qualitative",
            answer=settings.LOW_CONFIDENCE_RESPONSE,
            confidence=guard_result.get("confidence", 0.0),
            low_confidence=True,
            citations=[Citation(**c) for c in build_citations(top_chunks)],
        )

    return QueryResponse(
        question=req.question,
        query_type="qualitative",
        answer=answer,
        confidence=guard_result.get("confidence", 1.0),
        citations=[Citation(**c) for c in build_citations(top_chunks)],
    )


# ── compare / multi-doc ─────────────────────────────────────────────────────

@router.post("/filings/compare", response_model=CompareResponse)
def compare_filings(req: CompareRequest):
    if len(req.targets) < 2:
        raise HTTPException(400, "Comparison requires at least 2 filings")

    result = multi_doc_compare(req.question, [t.model_dump() for t in req.targets])
    return CompareResponse(
        question=req.question,
        answer=result["answer"],
        facts_used=[NumericFact(**f) for f in result.get("facts_used", [])],
        citations=[Citation(**c) for c in result.get("citations", [])],
    )


# """
# api/routes.py — FastAPI routes for the SEC filings pipeline.

# ASSUMPTIONS about your Day 1 / Day 2 modules (adjust imports/calls if these
# don't match what you actually wrote):

#   structured_store.py:
#       upsert_facts(cik: str, xbrl_json: dict) -> int          # returns #facts stored
#       get_metric(cik, metric, fiscal_year=None) -> list[dict] # rows: {value, unit, fiscal_year, fiscal_period, source_accession, company_name}

#   filing_chunker.py:
#       chunk_filing(html: str, filing_meta: dict) -> list[dict]
#       # filing_meta expects: {cik, accession_number, form_type, company_name, primary_document}
#       # each returned chunk dict: {id, text, item, item_title, cik, accession_number, ...}

#   embedder.py / indexer.py (unchanged from before):
#       embed_chunks(chunks: list[dict]) -> list[dict]      # adds "embedding" key
#       index_chunks(chunks: list[dict]) -> None
#       get_collection()

#   retrieval/searcher.py:
#       search(query: str, cik: str | None = None, item_filter: str | None = None,
#              accession_numbers: list[str] | None = None, top_k: int = ...) -> list[dict]

#   retrieval/reranker.py:
#       rerank(query: str, chunks: list[dict], top_n: int = ...) -> list[dict]

#   hallucination/guard.py:
#       check(answer: str, context_chunks: list[dict]) -> dict  # {passed, confidence, ...}

#   generation/generator.py:
#       generate(query: str, context_chunks: list[dict]) -> str

#   generation/citation_builder.py:
#       build_citations(chunks: list[dict]) -> list[dict]

#   query_router.py:
#       classify(question: str) -> Literal["numeric", "computed", "qualitative"]
#       extract_metric_request(question: str) -> dict  # best-effort parse for numeric/computed

#   multi_doc.py:
#       compare(question: str, filings: list[dict]) -> dict  # {answer, facts_used, citations}

# If a name differs, the fix is a one-line import/call change — ping me with
# the actual signature and I'll patch it.
# """

# from __future__ import annotations

# from typing import List

# from fastapi import APIRouter, HTTPException
# from loguru import logger

# from api.schemas import (
#     CompanyMatchOut,
#     CompanySearchResponse,
#     CompareRequest,
#     CompareResponse,
#     Citation,
#     FilingListResponse,
#     FilingMetaOut,
#     IngestFilingRequest,
#     IngestFilingResponse,
#     NumericFact,
#     QueryRequest,
#     QueryResponse,
# )
# from core.ingestion import edgar_client
# from core.ingestion.filing_chunker import chunk_filing
# from core.ingestion.embedder import embed_chunks
# from core.ingestion.indexer import index_chunks, get_collection
# from core.structured_store import upsert_facts, get_metric
# from core.retrieval.searcher import search
# from core.retrieval.reranker import rerank
# from core.hallucination.guard import check as guard_check
# from core.generation.generator import generate
# from core.generation.citation_builder import build_citations
# from core.query_router import classify, extract_metric_request
# from core.multi_doc import compare as multi_doc_compare
# from config import settings

# router = APIRouter()


# # ── company search + filing listing ─────────────────────────────────────────

# @router.get("/companies/search", response_model=CompanySearchResponse)
# def search_companies(q: str, limit: int = 10):
#     if not q.strip():
#         raise HTTPException(400, "Query parameter 'q' is required")

#     matches = edgar_client.search_company(q, limit=limit)
#     return CompanySearchResponse(
#         query=q,
#         results=[CompanyMatchOut(**m.to_dict()) for m in matches],
#     )


# @router.get("/companies/{cik}/filings", response_model=FilingListResponse)
# def list_filings(cik: str, form_type: str = "10-K", limit: int = 10):
#     filings = edgar_client.get_filings(cik, form_type=form_type, limit=limit)
#     if not filings:
#         raise HTTPException(404, f"No {form_type} filings found for CIK {cik}")

#     collection = get_collection()
#     ingested_accessions = _get_ingested_accessions(collection, cik)

#     return FilingListResponse(
#         cik=filings[0].cik,
#         company_name=filings[0].company_name,
#         filings=[
#             FilingMetaOut(
#                 **f.to_dict(),
#                 ingested=(f.accession_number in ingested_accessions),
#             )
#             for f in filings
#         ],
#     )


# def _get_ingested_accessions(collection, cik: str) -> set:
#     """Best-effort check of which accession numbers already have chunks indexed."""
#     try:
#         existing = collection.get(where={"cik": cik}, include=["metadatas"])
#         return {m["accession_number"] for m in existing.get("metadatas", []) if m}
#     except Exception as exc:
#         logger.warning(f"Could not check existing ingestion state for CIK {cik}: {exc}")
#         return set()


# # ── ingestion ─────────────────────────────────────────────────────────────────

# @router.post("/filings/ingest", response_model=IngestFilingResponse)
# def ingest_filing(req: IngestFilingRequest):
#     try:
#         html = edgar_client.fetch_filing_document(
#             req.cik, req.accession_number, req.primary_document
#         )
#     except Exception as exc:
#         logger.error(f"Failed to fetch filing document: {exc}")
#         raise HTTPException(502, f"Could not fetch filing from EDGAR: {exc}")

#     # company name isn't in the ingest request, so pull it via submissions once
#     company_name = req.cik
#     try:
#         filings = edgar_client.get_filings(req.cik, form_type=req.form_type, limit=50)
#         match = next((f for f in filings if f.accession_number == req.accession_number), None)
#         if match:
#             company_name = match.company_name
#     except Exception as exc:
#         logger.warning(f"Could not resolve company name for CIK {req.cik}: {exc}")

#     # 1. chunk the filing text
#     filing_meta = {
#         "cik": req.cik,
#         "accession_number": req.accession_number,
#         "form_type": req.form_type,
#         "company_name": company_name,
#         "primary_document": req.primary_document,
#     }
#     try:
#         chunks = chunk_filing(html=html, filing_meta=filing_meta)
#     except Exception as exc:
#         logger.error(f"Chunking failed for {req.cik}/{req.accession_number}: {exc}")
#         raise HTTPException(500, f"Filing chunking failed: {exc}")

#     if not chunks:
#         raise HTTPException(422, "Filing parsed but produced zero chunks — check document format")

#     # 2. embed + index
#     embedded = embed_chunks(chunks)
#     index_chunks(embedded)
#     items_found = sorted({c["metadata"].get("item", "unknown") or "flat" for c in chunks})

#     # 3. pull + store XBRL facts (best-effort — don't fail ingestion if this errors)
#     facts_stored = 0
#     status = "success"
#     message = None
#     try:
#         xbrl = edgar_client.fetch_xbrl_facts(req.cik)
#         facts_stored = upsert_facts(req.cik, xbrl)
#     except Exception as exc:
#         logger.warning(f"XBRL facts fetch/store failed for CIK {req.cik}: {exc}")
#         status = "partial"
#         message = f"Text ingested but XBRL facts unavailable: {exc}"

#     return IngestFilingResponse(
#         cik=req.cik,
#         accession_number=req.accession_number,
#         company_name=company_name,
#         chunks_indexed=len(embedded),
#         xbrl_facts_stored=facts_stored,
#         items_found=items_found,
#         status=status,
#         message=message,
#     )


# # ── query ─────────────────────────────────────────────────────────────────────

# @router.post("/query", response_model=QueryResponse)
# def query(req: QueryRequest):
#     if not req.question.strip():
#         raise HTTPException(400, "Question cannot be empty")

#     query_type = classify(req.question)

#     if query_type in ("numeric", "computed"):
#         return _handle_numeric_or_computed(req, query_type)

#     return _handle_qualitative(req, query_type)


# def _handle_numeric_or_computed(req: QueryRequest, query_type: str) -> QueryResponse:
#     parsed = extract_metric_request(req.question)
#     metric = parsed.get("metric")

#     if not metric or not req.cik:
#         # can't resolve a metric/company confidently — fall back to qualitative
#         # rather than guessing, since a wrong numeric answer is worse than a
#         # slower qualitative one.
#         logger.info("Numeric query could not be resolved to metric+cik, falling back to qualitative")
#         return _handle_qualitative(req, "qualitative")

#     rows = get_metric(req.cik, metric, fiscal_year=parsed.get("fiscal_year"))
#     if not rows:
#         return QueryResponse(
#             question=req.question,
#             query_type=query_type,
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     facts = [
#         NumericFact(
#             metric=metric,
#             company_name=r.get("company_name", ""),
#             fiscal_year=r.get("fiscal_year"),
#             fiscal_period=r.get("fiscal_period"),
#             value=r["value"],
#             unit=r.get("unit", "USD"),
#             source_accession=r.get("source_accession", ""),
#         )
#         for r in rows
#     ]

#     if query_type == "computed" and len(facts) >= 2:
#         answer = _compose_computed_answer(req.question, metric, facts)
#     else:
#         f = facts[0]
#         answer = f"{f.company_name}'s {metric} for {f.fiscal_period or ''} {f.fiscal_year or ''}: {f.value:,.0f} {f.unit}.".strip()

#     return QueryResponse(
#         question=req.question,
#         query_type=query_type,
#         answer=answer,
#         confidence=1.0,   # structured facts are exact — no LLM guessing involved
#         facts_used=facts,
#     )


# def _compose_computed_answer(question: str, metric: str, facts: List[NumericFact]) -> str:
#     """Simple YoY-style computation. Extend as more computed question types show up."""
#     sorted_facts = sorted(facts, key=lambda f: (f.fiscal_year or 0))
#     if len(sorted_facts) < 2:
#         return f"Not enough periods of {metric} data to compute a comparison."

#     latest, prior = sorted_facts[-1], sorted_facts[-2]
#     if prior.value == 0:
#         return f"{latest.company_name}'s {metric} in {latest.fiscal_year}: {latest.value:,.0f} {latest.unit} (prior period value was 0, can't compute % change)."

#     pct_change = ((latest.value - prior.value) / abs(prior.value)) * 100
#     direction = "increased" if pct_change >= 0 else "decreased"
#     return (
#         f"{latest.company_name}'s {metric} {direction} by {abs(pct_change):.1f}% "
#         f"from {prior.fiscal_year} ({prior.value:,.0f} {prior.unit}) to "
#         f"{latest.fiscal_year} ({latest.value:,.0f} {latest.unit})."
#     )


# def _handle_qualitative(req: QueryRequest, query_type: str) -> QueryResponse:
#     raw_chunks = search(
#         query=req.question,
#         cik=req.cik,
#         item_filter=req.item_filter,
#         accession_numbers=req.accession_numbers,
#         top_k=settings.TOP_K,
#     )

#     if not raw_chunks:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     top_chunks = rerank(req.question, raw_chunks, top_n=settings.RERANK_TOP_N)
#     top_chunks = [c for c in top_chunks if c.get("score", 0) >= settings.SIMILARITY_THRESHOLD]

#     if not top_chunks:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     answer = generate(req.question, top_chunks)
#     guard_result = guard_check(answer, top_chunks)

#     if not guard_result.get("passed", True) or guard_result.get("confidence", 1.0) < settings.CONFIDENCE_THRESHOLD:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=guard_result.get("confidence", 0.0),
#             low_confidence=True,
#             citations=[Citation(**c) for c in build_citations(top_chunks)],
#         )

#     return QueryResponse(
#         question=req.question,
#         query_type="qualitative",
#         answer=answer,
#         confidence=guard_result.get("confidence", 1.0),
#         citations=[Citation(**c) for c in build_citations(top_chunks)],
#     )


# # ── compare / multi-doc ─────────────────────────────────────────────────────

# @router.post("/filings/compare", response_model=CompareResponse)
# def compare_filings(req: CompareRequest):
#     if len(req.targets) < 2:
#         raise HTTPException(400, "Comparison requires at least 2 filings")

#     result = multi_doc_compare(req.question, [t.model_dump() for t in req.targets])
#     return CompareResponse(
#         question=req.question,
#         answer=result["answer"],
#         facts_used=[NumericFact(**f) for f in result.get("facts_used", [])],
#         citations=[Citation(**c) for c in result.get("citations", [])],
#     )

# """
# api/routes.py — FastAPI routes for the SEC filings pipeline.

# ASSUMPTIONS about your Day 1 / Day 2 modules (adjust imports/calls if these
# don't match what you actually wrote):

#   structured_store.py:
#       upsert_facts(cik: str, xbrl_json: dict) -> int          # returns #facts stored
#       get_metric(cik, metric, fiscal_year=None) -> list[dict] # rows: {value, unit, fiscal_year, fiscal_period, source_accession, company_name}

#   filing_chunker.py:
#       chunk_filing(html: str, filing_meta: dict) -> list[dict]
#       # filing_meta expects: {cik, accession_number, form_type, company_name, primary_document}
#       # each returned chunk dict: {id, text, item, item_title, cik, accession_number, ...}

#   embedder.py / indexer.py (unchanged from before):
#       embed_chunks(chunks: list[dict]) -> list[dict]      # adds "embedding" key
#       index_chunks(chunks: list[dict]) -> None
#       get_collection()

#   retrieval/searcher.py:
#       search(query: str, cik: str | None = None, item_filter: str | None = None,
#              accession_numbers: list[str] | None = None, top_k: int = ...) -> list[dict]

#   retrieval/reranker.py:
#       rerank(query: str, chunks: list[dict], top_n: int = ...) -> list[dict]

#   hallucination/guard.py:
#       check(answer: str, context_chunks: list[dict]) -> dict  # {passed, confidence, ...}

#   generation/generator.py:
#       generate(query: str, context_chunks: list[dict]) -> str

#   generation/citation_builder.py:
#       build_citations(chunks: list[dict]) -> list[dict]

#   query_router.py:
#       classify(question: str) -> Literal["numeric", "computed", "qualitative"]
#       extract_metric_request(question: str) -> dict  # best-effort parse for numeric/computed

#   multi_doc.py:
#       compare(question: str, filings: list[dict]) -> dict  # {answer, facts_used, citations}

# If a name differs, the fix is a one-line import/call change — ping me with
# the actual signature and I'll patch it.
# """

# from __future__ import annotations

# from typing import List

# from fastapi import APIRouter, HTTPException
# from loguru import logger

# from api.schemas import (
#     CompanyMatchOut,
#     CompanySearchResponse,
#     CompareRequest,
#     CompareResponse,
#     Citation,
#     FilingListResponse,
#     FilingMetaOut,
#     IngestFilingRequest,
#     IngestFilingResponse,
#     NumericFact,
#     QueryRequest,
#     QueryResponse,
# )
# from core.ingestion import edgar_client
# from core.ingestion.filing_chunker import chunk_filing
# from core.ingestion.embedder import embed_chunks
# from core.ingestion.indexer import index_chunks, get_collection
# from core.ingestion.structured_store import upsert_facts, get_metric
# from core.retrieval.searcher import search
# from core.retrieval.reranker import rerank
# from core.hallucination.guard import evaluate_chunks as guard_check
# from core.generation.generator import generate
# from core.generation.citation_builder import build_citations
# from core.ingestion.query_router import classify, extract_metric_request
# from core.multi_doc import compare as multi_doc_compare
# from config import settings

# router = APIRouter()


# # ── company search + filing listing ─────────────────────────────────────────

# @router.get("/companies/search", response_model=CompanySearchResponse)
# def search_companies(q: str, limit: int = 10):
#     if not q.strip():
#         raise HTTPException(400, "Query parameter 'q' is required")

#     matches = edgar_client.search_company(q, limit=limit)
#     return CompanySearchResponse(
#         query=q,
#         results=[CompanyMatchOut(**m.to_dict()) for m in matches],
#     )


# @router.get("/companies/{cik}/filings", response_model=FilingListResponse)
# def list_filings(cik: str, form_type: str = "10-K", limit: int = 10):
#     filings = edgar_client.get_filings(cik, form_type=form_type, limit=limit)
#     if not filings:
#         raise HTTPException(404, f"No {form_type} filings found for CIK {cik}")

#     collection = get_collection()
#     ingested_accessions = _get_ingested_accessions(collection, cik)

#     return FilingListResponse(
#         cik=filings[0].cik,
#         company_name=filings[0].company_name,
#         filings=[
#             FilingMetaOut(
#                 **f.to_dict(),
#                 ingested=(f.accession_number in ingested_accessions),
#             )
#             for f in filings
#         ],
#     )


# def _get_ingested_accessions(collection, cik: str) -> set:
#     """Best-effort check of which accession numbers already have chunks indexed."""
#     try:
#         existing = collection.get(where={"cik": cik}, include=["metadatas"])
#         return {m["accession_number"] for m in existing.get("metadatas", []) if m}
#     except Exception as exc:
#         logger.warning(f"Could not check existing ingestion state for CIK {cik}: {exc}")
#         return set()


# # ── ingestion ─────────────────────────────────────────────────────────────────

# @router.post("/filings/ingest", response_model=IngestFilingResponse)
# def ingest_filing(req: IngestFilingRequest):
#     try:
#         html = edgar_client.fetch_filing_document(
#             req.cik, req.accession_number, req.primary_document
#         )
#     except Exception as exc:
#         logger.error(f"Failed to fetch filing document: {exc}")
#         raise HTTPException(502, f"Could not fetch filing from EDGAR: {exc}")

#     # company name isn't in the ingest request, so pull it via submissions once
#     company_name = req.cik
#     try:
#         filings = edgar_client.get_filings(req.cik, form_type=req.form_type, limit=50)
#         match = next((f for f in filings if f.accession_number == req.accession_number), None)
#         if match:
#             company_name = match.company_name
#     except Exception as exc:
#         logger.warning(f"Could not resolve company name for CIK {req.cik}: {exc}")

#     # 1. chunk the filing text
#     filing_meta = {
#         "cik": req.cik,
#         "accession_number": req.accession_number,
#         "form_type": req.form_type,
#         "company_name": company_name,
#         "primary_document": req.primary_document,
#     }
#     try:
#         chunks = chunk_filing(html=html, filing_meta=filing_meta)
#     except Exception as exc:
#         logger.error(f"Chunking failed for {req.cik}/{req.accession_number}: {exc}")
#         raise HTTPException(500, f"Filing chunking failed: {exc}")

#     if not chunks:
#         raise HTTPException(422, "Filing parsed but produced zero chunks — check document format")

#     # 2. embed + index
#     embedded = embed_chunks(chunks)
#     index_chunks(embedded)
#     items_found = sorted({c["metadata"].get("item", "unknown") for c in chunks})

#     # 3. pull + store XBRL facts (best-effort — don't fail ingestion if this errors)
#     facts_stored = 0
#     status = "success"
#     message = None
#     try:
#         xbrl = edgar_client.fetch_xbrl_facts(req.cik)
#         facts_stored = upsert_facts(req.cik, xbrl)
#     except Exception as exc:
#         logger.warning(f"XBRL facts fetch/store failed for CIK {req.cik}: {exc}")
#         status = "partial"
#         message = f"Text ingested but XBRL facts unavailable: {exc}"

#     return IngestFilingResponse(
#         cik=req.cik,
#         accession_number=req.accession_number,
#         company_name=company_name,
#         chunks_indexed=len(embedded),
#         xbrl_facts_stored=facts_stored,
#         items_found=items_found,
#         status=status,
#         message=message,
#     )


# # ── query ─────────────────────────────────────────────────────────────────────

# @router.post("/query", response_model=QueryResponse)
# def query(req: QueryRequest):
#     if not req.question.strip():
#         raise HTTPException(400, "Question cannot be empty")

#     query_type = classify(req.question)

#     if query_type in ("numeric", "computed"):
#         return _handle_numeric_or_computed(req, query_type)

#     return _handle_qualitative(req, query_type)


# def _handle_numeric_or_computed(req: QueryRequest, query_type: str) -> QueryResponse:
#     parsed = extract_metric_request(req.question)
#     metric = parsed.get("metric")

#     if not metric or not req.cik:
#         # can't resolve a metric/company confidently — fall back to qualitative
#         # rather than guessing, since a wrong numeric answer is worse than a
#         # slower qualitative one.
#         logger.info("Numeric query could not be resolved to metric+cik, falling back to qualitative")
#         return _handle_qualitative(req, "qualitative")

#     rows = get_metric(req.cik, metric, fiscal_year=parsed.get("fiscal_year"))
#     if not rows:
#         return QueryResponse(
#             question=req.question,
#             query_type=query_type,
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     facts = [
#         NumericFact(
#             metric=metric,
#             company_name=r.get("company_name", ""),
#             fiscal_year=r.get("fiscal_year"),
#             fiscal_period=r.get("fiscal_period"),
#             value=r["value"],
#             unit=r.get("unit", "USD"),
#             source_accession=r.get("source_accession", ""),
#         )
#         for r in rows
#     ]

#     if query_type == "computed" and len(facts) >= 2:
#         answer = _compose_computed_answer(req.question, metric, facts)
#     else:
#         f = facts[0]
#         answer = f"{f.company_name}'s {metric} for {f.fiscal_period or ''} {f.fiscal_year or ''}: {f.value:,.0f} {f.unit}.".strip()

#     return QueryResponse(
#         question=req.question,
#         query_type=query_type,
#         answer=answer,
#         confidence=1.0,   # structured facts are exact — no LLM guessing involved
#         facts_used=facts,
#     )


# def _compose_computed_answer(question: str, metric: str, facts: List[NumericFact]) -> str:
#     """Simple YoY-style computation. Extend as more computed question types show up."""
#     sorted_facts = sorted(facts, key=lambda f: (f.fiscal_year or 0))
#     if len(sorted_facts) < 2:
#         return f"Not enough periods of {metric} data to compute a comparison."

#     latest, prior = sorted_facts[-1], sorted_facts[-2]
#     if prior.value == 0:
#         return f"{latest.company_name}'s {metric} in {latest.fiscal_year}: {latest.value:,.0f} {latest.unit} (prior period value was 0, can't compute % change)."

#     pct_change = ((latest.value - prior.value) / abs(prior.value)) * 100
#     direction = "increased" if pct_change >= 0 else "decreased"
#     return (
#         f"{latest.company_name}'s {metric} {direction} by {abs(pct_change):.1f}% "
#         f"from {prior.fiscal_year} ({prior.value:,.0f} {prior.unit}) to "
#         f"{latest.fiscal_year} ({latest.value:,.0f} {latest.unit})."
#     )


# def _handle_qualitative(req: QueryRequest, query_type: str) -> QueryResponse:
#     raw_chunks = search(
#         query=req.question,
#         cik=req.cik,
#         item_filter=req.item_filter,
#         accession_numbers=req.accession_numbers,
#         top_k=settings.TOP_K,
#     )

#     if not raw_chunks:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     top_chunks = rerank(req.question, raw_chunks, top_n=settings.RERANK_TOP_N)
#     top_chunks = [c for c in top_chunks if c.get("score", 0) >= settings.SIMILARITY_THRESHOLD]

#     if not top_chunks:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=0.0,
#             low_confidence=True,
#         )

#     answer = generate(req.question, top_chunks)
#     guard_result = guard_check(answer, top_chunks)

#     if not guard_result.get("passed", True) or guard_result.get("confidence", 1.0) < settings.CONFIDENCE_THRESHOLD:
#         return QueryResponse(
#             question=req.question,
#             query_type="qualitative",
#             answer=settings.LOW_CONFIDENCE_RESPONSE,
#             confidence=guard_result.get("confidence", 0.0),
#             low_confidence=True,
#             citations=[Citation(**c) for c in build_citations(top_chunks)],
#         )

#     return QueryResponse(
#         question=req.question,
#         query_type="qualitative",
#         answer=answer,
#         confidence=guard_result.get("confidence", 1.0),
#         citations=[Citation(**c) for c in build_citations(top_chunks)],
#     )


# # ── compare / multi-doc ─────────────────────────────────────────────────────

# @router.post("/filings/compare", response_model=CompareResponse)
# def compare_filings(req: CompareRequest):
#     if len(req.targets) < 2:
#         raise HTTPException(400, "Comparison requires at least 2 filings")

#     result = multi_doc_compare(req.question, [t.model_dump() for t in req.targets])
#     return CompareResponse(
#         question=req.question,
#         answer=result["answer"],
#         facts_used=[NumericFact(**f) for f in result.get("facts_used", [])],
#         citations=[Citation(**c) for c in result.get("citations", [])],
#     )
