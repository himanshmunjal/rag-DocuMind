"""
core/multi_doc.py — cross-filing comparison layer.

A "document" here is identified by (cik, accession_number), not a filename,
since a company can have many filings and we need to know exactly which one.

Handles both flavors of comparison question in one pass:

  - numeric   "Compare R&D spending between Apple and Microsoft"
              -> pulled straight from structured_store per company, no LLM
                 involved in getting the numbers (only in narrating them)

  - qualitative  "Compare how Apple and Microsoft describe AI risk"
              -> retrieval scoped to each filing independently, so a
                 filing with no relevant content shows up as "insufficient"
                 rather than being silently skipped or hallucinated over

Most real comparison questions want both where applicable, so compare()
always checks for a resolvable metric via query_router AND always attempts
qualitative retrieval per filing — whichever comes back empty just
contributes less to the final synthesis, nothing errors out.

CONFIRMED interfaces (verified against real code during Day 5 debugging —
same conventions as api/routes.py, NOT the earlier draft assumptions):

  core.generation.generator.generate(query, sources=None, cik=None,
      item_filter=None, top_k=None, top_n=None, history=None) -> GenerationResult
      # generate() ALREADY runs retrieve -> guard.evaluate_chunks() ->
      # build_citations() -> Gemini internally. Do NOT call retrieve/guard/
      # citation_builder separately — this file used to (wrongly) do that.
      # GenerationResult fields: answer, citations (list[Citation]),
      # confidence (ConfidenceLevel enum), answered (bool), sources_used

  core.structured_store.get_metric(cik, metric, fiscal_year=None,
      fiscal_period="FY") -> MetricFact | None   # single fact, not a list
      # MetricFact fields: metric, value, unit, period_end, fiscal_year,
      # fiscal_period, form_type, xbrl_tag, accession_number
      # NOTE: no company_name field — see _numeric_facts_for_filings() below

  core.query_router.extract_metric_request(question) ->
      {"metric", "fiscal_period", "fiscal_year", "compare_to_fiscal_year"}

compare() is called from api/routes.py's /filings/compare endpoint as:
    multi_doc_compare(question, [target.model_dump() for target in req.targets])
where each target dict has: {cik, accession_number, primary_document, form_type}
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from loguru import logger

from config import settings
from core.generation.generator import generate
from core.generation.llm_client import complete
from core.ingestion.structured_store import get_metric
from core.ingestion.query_router import extract_metric_request


# ── internal: per-filing qualitative answer ─────────────────────────────────

def _qualitative_answer_for_filing(
    question: str,
    filing: Dict[str, Any],
    top_k: int,
) -> tuple[str, str, List[Any]]:
    """
    Run the full qualitative pipeline (generate() already handles retrieve
    -> guard -> cite -> generate internally) scoped to a single filing.

    Returns:
        (label, summary_text, citations)
        citations is empty if the guard refused — callers use that to decide
        whether this filing contributed anything to the final synthesis.
    """
    cik = filing["cik"]
    accession_number = filing["accession_number"]

    result = generate(
        question,
        sources=[accession_number],
        cik=cik,
        top_k=top_k,
        top_n=settings.RERANK_TOP_N,
    )

    label = _label_for_filing(filing, result)

    if not result.answered:
        return label, f"[{label}] Insufficient grounded information to answer confidently.", []

    return label, f"[{label}] {result.answer}", result.citations


def _label_for_filing(filing: Dict[str, Any], result: Any) -> str:
    """
    Best-effort human-readable label for a filing in synthesis output.
    Prefers company_name from the generation result's citations (set during
    ingestion, carried through by citation_builder.py) since the filing
    dict itself only carries cik/accession.
    """
    for cit in getattr(result, "citations", []) or []:
        name = getattr(cit, "company_name", "") or (cit.get("company_name") if isinstance(cit, dict) else "")
        if name:
            return f"{name} ({filing.get('form_type', '')} {filing['accession_number']})"

    return f"CIK {filing['cik']} ({filing.get('form_type', '')} {filing['accession_number']})"


# ── internal: numeric facts across filings ──────────────────────────────────

def _numeric_facts_for_filings(
    metric: str,
    fiscal_year: Optional[int],
    fiscal_period: str,
    filings: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Pull the requested metric for each company involved in the comparison.

    Dedupes by cik first, since multiple filings in `filings` can belong to
    the same company (e.g. comparing two years of the same company's
    10-Ks) — no need to query structured_store twice for one CIK.

    NOTE: get_metric() returns a single MetricFact (or None), not a list —
    if fiscal_year is None it returns the most recent available period.
    MetricFact doesn't carry company_name (a known gap flagged in routes.py
    too — cheapest real fix is adding a company_name column read in
    structured_store._row_to_fact(), not worked around here).
    """
    seen_ciks: set[str] = set()
    facts: List[Dict[str, Any]] = []

    for filing in filings:
        cik = filing["cik"]
        if cik in seen_ciks:
            continue
        seen_ciks.add(cik)

        fact = get_metric(cik, metric, fiscal_year=fiscal_year, fiscal_period=fiscal_period)
        if fact is None:
            logger.info(f"compare: no '{metric}' data for CIK {cik} — skipping in facts_used")
            continue

        facts.append({
            "metric": metric,
            "company_name": "",   # MetricFact doesn't carry this — see docstring above
            "fiscal_year": fact.fiscal_year,
            "fiscal_period": fact.fiscal_period,
            "value": fact.value,
            "unit": fact.unit,
            "source_accession": fact.accession_number or "",
        })

    return facts


# ── internal: final synthesis ────────────────────────────────────────────────

def _synthesise(
    question: str,
    per_filing_summaries: List[str],
    facts: List[Dict[str, Any]],
) -> str:
    """
    Pure LLM call combining structured facts + per-filing qualitative
    summaries into one comparison paragraph. No retrieval happens here —
    it only sees text/numbers that already passed their respective checks
    (guard for qualitative, structured_store for numeric), so it can't
    introduce new unverified claims, only synthesise what's given.
    """
    facts_block = "No structured financial data was applicable to this question."
    if facts:
        lines = [
            f"- {f['company_name'] or 'CIK ' + str(f.get('source_accession', ''))}: "
            f"{f['metric']} = {f['value']:,.0f} {f['unit']} "
            f"({f['fiscal_period'] or ''} {f['fiscal_year'] or ''})".strip()
            for f in facts
        ]
        facts_block = "\n".join(lines)

    qualitative_block = "\n\n".join(per_filing_summaries) if per_filing_summaries else "No qualitative content retrieved."

    prompt = f"""You are comparing multiple SEC filings to answer a question.
Use ONLY the structured data and per-filing summaries provided below.
Do not add outside knowledge or invent numbers not listed in STRUCTURED DATA.
Write a concise comparison (3-6 sentences), referencing companies by name.

QUESTION: {question}

STRUCTURED DATA:
{facts_block}

PER-FILING SUMMARIES:
{qualitative_block}

COMPARISON:"""

    return complete([{"role": "user", "content": prompt}], temperature=0.3)


# ── public API ────────────────────────────────────────────────────────────────

def compare(question: str, filings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compare a question across 2+ filings, combining structured facts (when
    the question resolves to a known metric) and qualitative retrieval.

    Args:
        question: user's comparison question
        filings:  list of dicts, each {cik, accession_number, primary_document, form_type}

    Returns:
        {"answer": str, "facts_used": list[dict], "citations": list[dict]}
        matching api.schemas.CompareResponse field shapes directly.
    """
    if len(filings) < 2:
        raise ValueError("compare() requires at least 2 filings")

    metric_req = extract_metric_request(question)
    facts: List[Dict[str, Any]] = []
    if metric_req.get("metric"):
        facts = _numeric_facts_for_filings(
            metric_req["metric"],
            metric_req.get("fiscal_year"),
            metric_req.get("fiscal_period", "FY"),
            filings,
        )
        logger.info(f"compare: resolved metric='{metric_req['metric']}', found {len(facts)} fact(s)")

    per_filing_summaries: List[str] = []
    all_citations: List[Any] = []

    for filing in filings:
        logger.info(f"compare: retrieving for CIK={filing['cik']} accession={filing['accession_number']}")
        label, summary, citations = _qualitative_answer_for_filing(
            question, filing, top_k=settings.TOP_K
        )
        per_filing_summaries.append(summary)
        all_citations.extend(citations)

    if not facts and not all_citations:
        return {
            "answer": settings.LOW_CONFIDENCE_RESPONSE,
            "facts_used": [],
            "citations": [],
        }

    final_answer = _synthesise(question, per_filing_summaries, facts)

    # result.citations are Citation dataclass objects (from generate()) —
    # convert to dicts matching api.schemas.Citation, same as routes.py does.
    citation_dicts = [c.to_dict() if hasattr(c, "to_dict") else c for c in all_citations]

    return {
        "answer": final_answer,
        "facts_used": facts,
        "citations": citation_dicts,
    }