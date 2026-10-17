"""
api/routes.py — FastAPI routes for the SEC filings pipeline.

Confirmed signatures (verified against actual code, Days 1-5):

  structured_store.py:
      upsert_facts(cik, xbrl_json) -> int
      get_metric(cik, metric, fiscal_year=None, fiscal_period="FY") -> MetricFact | None
      get_metric_history(cik, metric, fiscal_period="FY", limit=8) -> list[MetricFact]
      compute_change(cik, metric, fiscal_year, compare_to_fiscal_year=None, fiscal_period="FY") -> dict | None
          # {"metric","current_year","current_value","previous_year","previous_value",
          #   "abs_change","pct_change","unit"}
      list_available_metrics(cik) -> list[str]
      MetricFact fields: metric, value, unit, period_end, fiscal_year, fiscal_period,
                         form_type, xbrl_tag, accession_number

  filing_chunker.py:
      chunk_filing(html: str, filing_meta: dict) -> list[dict]
      # each chunk: {"text": str, "metadata": {source, cik, company_name, form_type,
      #              filing_date, report_date, item, item_title, chunk_index,
      #              chunk_type, char_count}}
      # NOTE: "source" in metadata IS the accession_number (see filing_chunker docstring)

  query_router.py:
      classify(question) -> "numeric" | "computed" | "qualitative"
      extract_metric_request(question) -> {"metric", "fiscal_period", "fiscal_year", "compare_to_fiscal_year"}
      suggest_item_filter(question) -> str | None   # e.g. "1A", for scoping qualitative search

  generation/generator.py — the qualitative pipeline's SINGLE entry point.
  Internally it already runs retrieve() -> guard.evaluate_chunks() ->
  build_citations() -> Gemini generate_content() -> GenerationResult.
  routes.py does NOT call retrieve/guard/citation_builder directly for
  qualitative queries — just this one function:
      generate(query, sources=None, cik=None, item_filter=None,
                top_k=None, top_n=None, history=None) -> GenerationResult
      GenerationResult fields: answer, citations (list[Citation]), confidence
      (ConfidenceLevel enum: HIGH/MEDIUM/LOW/INSUFFICIENT), answered (bool),
      query, sources_used
  can_answer(query, sources=None, cik=None, item_filter=None) -> bool
      (cheap retrieve+guard-only check, skips generation — not currently
      used in routes.py but available if you want a pre-flight check)

  REQUIRES three patches merged into your real files before this works:
      searcher_patch.py    -> core/retrieval/searcher.py   (adds cik/item_filter)
      reranker_patch.py    -> core/retrieval/reranker.py   (forwards them through retrieve())
      generator_patch.py   -> core/generation/generator.py (forwards them through generate()/can_answer())
      citation_builder_patch.py -> core/generation/citation_builder.py
          (adds item/item_title/cik/company_name/accession_number fields to
          Citation, since the original was built for generic page-numbered
          PDFs and filings don't have real page numbers)

  STILL ASSUMED (not yet confirmed against real code):
      embedder.py / indexer.py:
          embed_chunks(chunks: list[dict]) -> list[dict]   # adds "embedding" + "id" keys
          index_chunks(chunks: list[dict]) -> None
          get_collection()
      multi_doc.py:
          compare(question: str, filings: list[dict]) -> dict  # {answer, facts_used, citations}

If a name differs, the fix is a one-line import/call change — ping me with
the actual signature and I'll patch it.

"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import List, Optional

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
    DocumentSummary,
    DocumentListResponse,
)
from core.ingestion import edgar_client
from core.ingestion.filing_chunker import chunk_filing
from core.ingestion.embedder import embed_chunks
from core.ingestion.indexer import index_chunks, get_collection
from core.ingestion.structured_store import (
    upsert_facts, get_metric, compute_change, get_metric_history, get_ratio, list_companies,
)
from core.generation.generator import generate
from core.ingestion.query_router import (
    RATIO_METRICS, classify, extract_metric_request, find_companies, suggest_item_filter,
)
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
        # NOTE: filing_chunker stores the accession number under "source",
        # not "accession_number" — see filing_chunker.py docstring.
        return {m["source"] for m in existing.get("metadatas", []) if m}
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
    filing_date = report_date = ""
    try:
        filings = edgar_client.get_filings(req.cik, form_type=req.form_type, limit=50)
        match = next((f for f in filings if f.accession_number == req.accession_number), None)
        if match:
            company_name = match.company_name
            filing_date, report_date = match.filing_date, match.report_date
    except Exception as exc:
        logger.warning(f"Could not resolve company name for CIK {req.cik}: {exc}")

    # 1. chunk the filing text
    filing_meta = {
        "cik": req.cik,
        "accession_number": req.accession_number,
        "form_type": req.form_type,
        "company_name": company_name,
        "primary_document": req.primary_document,
        "filing_date": filing_date,
        "report_date": report_date,
    }
    try:
        chunks = chunk_filing(html=html, filing_meta=filing_meta)
    except Exception as exc:
        logger.error(f"Chunking failed for {req.cik}/{req.accession_number}: {exc}")
        raise HTTPException(500, f"Filing chunking failed: {exc}")

    if not chunks:
        raise HTTPException(422, "Filing parsed but produced zero chunks — check document format")

    # 2. embed + index. Drop any previous chunks for this filing first —
    # upsert only replaces matching chunk ids, so a re-ingest that produces
    # fewer chunks would otherwise leave stale ones behind.
    embedded = embed_chunks(chunks)
    get_collection().delete(where={"source": req.accession_number})
    index_chunks(embedded)
    items_found = sorted({c["metadata"].get("item", "unknown") for c in chunks})

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
    mentioned = find_companies(req.question, _known_companies())

    # The question can name a company the request didn't scope to (or name
    # several). An explicit cik that the question doesn't contradict wins.
    if not req.cik and len(mentioned) == 1:
        req = req.model_copy(update={"cik": mentioned[0]})

    if query_type in ("numeric", "computed"):
        if len(mentioned) >= 2:
            return _handle_cross_company(req, query_type, mentioned)
        return _handle_numeric_or_computed(req, query_type)

    return _handle_qualitative(req, query_type)


@lru_cache(maxsize=1)
def _tickers_by_cik() -> dict[str, str]:
    tickers: dict[str, str] = {}
    try:
        for entry in edgar_client._load_ticker_directory().values():
            tickers.setdefault(str(entry["cik_str"]).zfill(10), entry["ticker"])
    except Exception as exc:
        logger.warning(f"Ticker directory unavailable, matching companies by name only: {exc}")
    return tickers


def _known_companies() -> list[dict]:
    """Companies with stored facts, plus their tickers when the SEC directory has them."""
    tickers = _tickers_by_cik()
    return [
        {"cik": cik, "name": name, "ticker": tickers.get(cik)}
        for cik, name in list_companies()
    ]


def _display_name(company_name: str) -> str:
    """"MICROSOFT CORPORATION" -> "Microsoft Corporation"; mixed-case names are left alone."""
    return company_name.title() if company_name.isupper() else company_name


def _insufficient(req: QueryRequest, query_type: str) -> QueryResponse:
    return QueryResponse(
        question=req.question, query_type=query_type,
        answer=settings.LOW_CONFIDENCE_RESPONSE, confidence="INSUFFICIENT", low_confidence=True,
    )


def _fact_out(fact) -> NumericFact:
    return NumericFact(
        metric=fact.metric,
        company_name=_display_name(fact.company_name),
        fiscal_year=fact.fiscal_year,
        fiscal_period=fact.fiscal_period,
        value=fact.value,
        unit=fact.unit,
        source_accession=fact.accession_number or "",
    )


def _handle_numeric_or_computed(req: QueryRequest, query_type: str) -> QueryResponse:
    parsed = extract_metric_request(req.question)
    metric = parsed.get("metric")
    fiscal_period = parsed.get("fiscal_period", "FY")

    if not metric or not req.cik:
        # can't resolve a metric/company confidently — fall back to qualitative
        # rather than guessing, since a wrong numeric answer is worse than a
        # slower qualitative one.
        logger.info("Numeric query could not be resolved to metric+cik, falling back to qualitative")
        return _handle_qualitative(req, "qualitative")

    if metric in RATIO_METRICS:
        return _handle_ratio(req, query_type, metric, parsed, fiscal_period)

    if query_type == "computed":
        trend_years = _detect_trend_years(req.question)
        if trend_years:
            return _handle_trend(req, metric, trend_years, fiscal_period)
        return _handle_computed(req, metric, parsed, fiscal_period)

    fact = get_metric(req.cik, metric, fiscal_year=parsed.get("fiscal_year"), fiscal_period=fiscal_period)
    if fact is None:
        return _insufficient(req, query_type)

    label = metric.replace("_", " ").title()
    company = _display_name(fact.company_name)
    prefix = f"{company} — " if company else ""
    answer = f"{prefix}{label} for {fact.fiscal_period} {fact.fiscal_year}: {fact.value:,.0f} {fact.unit}."
    if metric == "shares_outstanding":
        answer = (
            f"{prefix}{fact.value:,.0f} shares of common stock outstanding as of {fact.period_end} "
            f"(reported in the {fact.fiscal_period} {fact.fiscal_year} {fact.form_type})."
        )

    return QueryResponse(
        question=req.question,
        query_type=query_type,
        answer=answer,
        confidence="HIGH",   # structured fact, no LLM guessing involved
        facts_used=[_fact_out(fact)],
    )


def _handle_ratio(
    req: QueryRequest, query_type: str, metric: str, parsed: dict, fiscal_period: str,
) -> QueryResponse:
    """Derived ratios (margins, R&D % of revenue): single year, or the change between two years."""
    label = metric.replace("_", " ").replace("rd to revenue", "R&D as a percentage of revenue")
    current = get_ratio(req.cik, metric, fiscal_year=parsed.get("fiscal_year"), fiscal_period=fiscal_period)
    if current is None:
        return _insufficient(req, query_type)

    company = _display_name(current["components"][0].company_name)
    prefix = f"{company} — " if company else ""
    facts = [_fact_out(f) for f in current["components"]]

    if query_type != "computed":
        answer = f"{prefix}{label.capitalize()} for {fiscal_period} {current['fiscal_year']}: {current['value_pct']:.2f}%."
        return QueryResponse(question=req.question, query_type=query_type, answer=answer,
                             confidence="HIGH", facts_used=facts)

    prev_year = parsed.get("compare_to_fiscal_year") or current["fiscal_year"] - 1
    previous = get_ratio(req.cik, metric, fiscal_year=prev_year, fiscal_period=fiscal_period)
    if previous is None:
        return _insufficient(req, query_type)

    delta = current["value_pct"] - previous["value_pct"]
    direction = "rose" if delta >= 0 else "fell"
    answer = (
        f"{prefix}{label.capitalize()} {direction} from {previous['value_pct']:.2f}% in {fiscal_period} {prev_year} "
        f"to {current['value_pct']:.2f}% in {fiscal_period} {current['fiscal_year']} "
        f"({abs(delta):.2f} percentage points)."
    )
    facts += [_fact_out(f) for f in previous["components"]]
    return QueryResponse(question=req.question, query_type=query_type, answer=answer,
                         confidence="HIGH", facts_used=facts)


def _handle_cross_company(req: QueryRequest, query_type: str, ciks: list[str]) -> QueryResponse:
    """
    Same metric for 2+ companies named in the question, each at its own most
    recent (or requested) fiscal year — fiscal calendars differ (Apple ends in
    September, Microsoft in June), so "most recent" is resolved per company.
    """
    parsed = extract_metric_request(req.question)
    metric = parsed.get("metric")
    fiscal_period = parsed.get("fiscal_period", "FY")
    if not metric:
        return _handle_qualitative(req, "qualitative")

    is_ratio = metric in RATIO_METRICS
    rows: list[tuple[str, float, int, str]] = []   # (company, value, fiscal_year, formatted)
    facts: list[NumericFact] = []
    for cik in ciks:
        if is_ratio:
            r = get_ratio(cik, metric, fiscal_year=parsed.get("fiscal_year"), fiscal_period=fiscal_period)
            if r is None:
                continue
            company = _display_name(r["components"][0].company_name) or f"CIK {cik}"
            rows.append((company, r["value_pct"], r["fiscal_year"], f"{r['value_pct']:.2f}%"))
            facts += [_fact_out(f) for f in r["components"]]
        else:
            f = get_metric(cik, metric, fiscal_year=parsed.get("fiscal_year"), fiscal_period=fiscal_period)
            if f is None:
                continue
            company = _display_name(f.company_name) or f"CIK {cik}"
            rows.append((company, f.value, f.fiscal_year, f"{f.value:,.0f} {f.unit}"))
            facts.append(_fact_out(f))

    if len(rows) < 2:
        # comparing against a company we have no data for would be a guess
        return _insufficient(req, query_type)

    label = metric.replace("_", " ").replace("rd to revenue", "R&D as a percentage of revenue")
    listing = "; ".join(f"{name}: {formatted} ({fiscal_period} {year})" for name, _, year, formatted in rows)
    ranked = sorted(rows, key=lambda r: r[1], reverse=True)
    top, runner_up = ranked[0], ranked[1]
    gap = f"{top[1] - runner_up[1]:.2f} percentage points" if is_ratio else f"{top[1] - runner_up[1]:,.0f}"
    answer = (
        f"{label.capitalize()} — {listing}. "
        f"{top[0]} is higher than {runner_up[0]} by {gap}."
    )
    return QueryResponse(question=req.question, query_type="computed", answer=answer,
                         confidence="HIGH", facts_used=facts)


def _handle_computed(req: QueryRequest, metric: str, parsed: dict, fiscal_period: str) -> QueryResponse:
    fiscal_year = parsed.get("fiscal_year")

    # question didn't mention a year at all ("how has revenue changed") —
    # anchor on the most recent available year for this metric.
    if fiscal_year is None:
        latest = get_metric(req.cik, metric, fiscal_period=fiscal_period)
        if latest is None or latest.fiscal_year is None:
            return _insufficient(req, "computed")
        fiscal_year = latest.fiscal_year

    result = compute_change(
        req.cik, metric,
        fiscal_year=fiscal_year,
        compare_to_fiscal_year=parsed.get("compare_to_fiscal_year"),
        fiscal_period=fiscal_period,
    )

    if result is None:
        return _insufficient(req, "computed")

    pct = result["pct_change"]
    label = metric.replace("_", " ")
    if pct is None:
        answer = (
            f"{label} in {result['current_year']}: {result['current_value']:,.0f} {result['unit']} "
            f"(prior period value was 0, can't compute a % change)."
        )
    else:
        direction = "increased" if pct >= 0 else "decreased"
        answer = (
            f"{label.capitalize()} {direction} by {abs(pct):.2f}% from {fiscal_period} {result['previous_year']} "
            f"({result['previous_value']:,.0f} {result['unit']}) to {fiscal_period} {result['current_year']} "
            f"({result['current_value']:,.0f} {result['unit']})."
        )

    facts = [
        NumericFact(metric=metric, company_name="", fiscal_year=result["previous_year"],
                    fiscal_period=fiscal_period, value=result["previous_value"],
                    unit=result["unit"], source_accession=""),
        NumericFact(metric=metric, company_name="", fiscal_year=result["current_year"],
                    fiscal_period=fiscal_period, value=result["current_value"],
                    unit=result["unit"], source_accession=""),
    ]

    return QueryResponse(
        question=req.question, query_type="computed", answer=answer,
        confidence="HIGH", facts_used=facts,
    )


def _handle_qualitative(req: QueryRequest, query_type: str) -> QueryResponse:
    item_filter = req.item_filter or suggest_item_filter(req.question)

    try:
        result = generate(
            req.question,
            sources=req.accession_numbers,
            cik=req.cik,
            item_filter=item_filter,
            top_k=settings.TOP_K,
            top_n=settings.RERANK_TOP_N,
        )

        # if a suggested (not user-specified) filter led to a refusal, retry
        # unscoped rather than dead-ending — a wrong guess shouldn't block the answer.
        if not result.answered and item_filter and not req.item_filter:
            logger.info(f"No answer with suggested item_filter='{item_filter}', retrying unscoped")
            result = generate(
                req.question,
                sources=req.accession_numbers,
                cik=req.cik,
                item_filter=None,
                top_k=settings.TOP_K,
                top_n=settings.RERANK_TOP_N,
            )
    except RuntimeError as exc:
        # LLM outage / quota exhaustion — say so instead of a bare 500
        raise HTTPException(503, f"Answer generation is temporarily unavailable: {exc}")

    return QueryResponse(
        question=req.question,
        query_type="qualitative",
        answer=result.answer,
        confidence=result.confidence.value,   # ConfidenceLevel enum -> "HIGH"/"MEDIUM"/"LOW"/"INSUFFICIENT"
        low_confidence=(result.confidence.value == "INSUFFICIENT"),
        citations=[Citation(**c.to_dict()) for c in result.citations],
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


@router.get("/documents", response_model=DocumentListResponse)
def list_documents():
    """Every distinct ingested filing, with chunk counts, for the right-hand index panel."""
    collection = get_collection()
    try:
        all_chunks = collection.get(include=["metadatas"])
    except Exception as exc:
        logger.error(f"Failed to read collection for /documents: {exc}")
        raise HTTPException(500, "Could not read document index")

    by_source: dict[str, dict] = {}
    for meta in all_chunks.get("metadatas", []):
        if not meta:
            continue
        source = meta.get("source")  # accession_number
        if not source:
            continue
        if source not in by_source:
            by_source[source] = {
                "source": source,
                "cik": meta.get("cik", ""),
                "company_name": meta.get("company_name", ""),
                "form_type": meta.get("form_type", ""),
                "chunk_count": 0,
            }
        by_source[source]["chunk_count"] += 1

    return DocumentListResponse(
        documents=[DocumentSummary(**d) for d in by_source.values()]
    )


@router.delete("/documents/{source}")
def delete_document(source: str):
    """Remove all chunks for a given accession number (source) from the index."""
    collection = get_collection()
    try:
        collection.delete(where={"source": source})
    except Exception as exc:
        logger.error(f"Failed to delete document {source}: {exc}")
        raise HTTPException(500, f"Could not delete document: {exc}")
    return {"status": "deleted", "source": source}

def _detect_trend_years(question: str) -> Optional[int]:
    """
    Detect open-ended multi-year trend questions ("how has revenue changed
    over the last 5 years") that a single compute_change() pairwise diff
    can't answer well. Returns a lookback count, or None if this looks like
    a normal single-year/single-comparison question.
    """
    q = question.lower()

    # Primary: catches "past 5 years", "last 3 years", "over past 5 years", etc.
    # regardless of "the" — this is the pattern that was missing your phrasing.
    m = re.search(r"(?:last|past)\s+(\d+)\s+years?", q)
    if m:
        return int(m.group(1))

    # Fallback: open-ended trend language with no explicit year count
    trend_keywords = (
        "trend", "trajectory", "over time", "historically", "history",
        "changed over", "growth over", "growing over",
    )
    if any(kw in q for kw in trend_keywords):
        return 5  # default lookback

    return None


def _handle_trend(req: QueryRequest, metric: str, years: int, fiscal_period: str) -> QueryResponse:
    history = get_metric_history(req.cik, metric, fiscal_period=fiscal_period, limit=years)

    if not history:
        return _insufficient(req, "computed")

    series = sorted(history, key=lambda f: f.fiscal_year)
    label = metric.replace("_", " ")
    first, last = series[0], series[-1]

    year_by_year = "; ".join(f"{f.fiscal_year}: {f.value:,.0f} {f.unit}" for f in series)

    if first.value:
        pct = (last.value - first.value) / abs(first.value) * 100
        direction = "grown" if pct >= 0 else "declined"
        answer = (
            f"{label.title()} has {direction} {abs(pct):.1f}% from {first.fiscal_year} "
            f"({first.value:,.0f} {first.unit}) to {last.fiscal_year} ({last.value:,.0f} {last.unit}). "
            f"Year-by-year: {year_by_year}."
        )
    else:
        answer = f"{label.title()} by year: {year_by_year}."

    facts = [
        NumericFact(metric=metric, company_name="", fiscal_year=f.fiscal_year,
                    fiscal_period=f.fiscal_period, value=f.value, unit=f.unit,
                    source_accession=f.accession_number or "")
        for f in series
    ]

    return QueryResponse(
        question=req.question, query_type="computed", answer=answer,
        confidence="HIGH", facts_used=facts,
    )