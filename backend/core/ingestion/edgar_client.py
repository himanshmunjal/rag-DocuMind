"""
edgar_client.py  —  SEC EDGAR API client.

Talks to two SEC hosts:
  www.sec.gov    — static files (company_tickers.json, filing archive documents)
  data.sec.gov   — JSON REST APIs (submissions, XBRL company facts)

Three public functions cover the whole ingestion pipeline's needs:
  search_company(query)        ticker/name  -> [CompanyMatch, ...]
  get_filings(cik, form_type)  CIK          -> [FilingMeta, ...]
  fetch_xbrl_facts(cik)        CIK          -> raw companyfacts JSON

Plus one helper to pull the actual filing document text:
  fetch_filing_document(cik, accession_number, primary_document) -> str (HTML)

Everything else in the ingestion pipeline (filing_chunker.py, structured_store.py)
consumes the output of these functions — this module is the only place that
knows about SEC's URL shapes, rate limits, and the User-Agent requirement.

Rules this module follows (all mandated by SEC, not optional):
  - Every request carries a descriptive User-Agent header (name + email).
    Requests without one are frequently blocked with a 403.
  - No more than 10 requests/second. We self-throttle well under that.
  - CIKs are always used zero-padded to 10 digits in URLs.

Caching:
  company_tickers.json (~1MB, ~10k companies) changes rarely, so it's cached
  to disk on first use and reused across process restarts. Filing lists and
  XBRL facts are NOT cached here — that's the job of the ingestion layer
  above this (structured_store.py decides what to persist and for how long).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger

from config import settings


# ── constants ─────────────────────────────────────────────────────────────────

SEC_WWW_BASE   = "https://www.sec.gov"
SEC_DATA_BASE  = "https://data.sec.gov"

TICKERS_URL       = f"{SEC_WWW_BASE}/files/company_tickers.json"
SUBMISSIONS_URL   = f"{SEC_DATA_BASE}/submissions/CIK{{cik}}.json"
COMPANYFACTS_URL  = f"{SEC_DATA_BASE}/api/xbrl/companyfacts/CIK{{cik}}.json"
COMPANYCONCEPT_URL = (
    f"{SEC_DATA_BASE}/api/xbrl/companyconcept/CIK{{cik}}/{{taxonomy}}/{{tag}}.json"
)
ARCHIVE_DOC_URL = (
    f"{SEC_WWW_BASE}/Archives/edgar/data/{{cik_int}}/{{accession_nodash}}/{{document}}"
)

TICKERS_CACHE_PATH = settings.BASE_DIR / "data" / "edgar_company_tickers.json"
TICKERS_CACHE_MAX_AGE_SECONDS = 7 * 24 * 60 * 60   # 1 week

MAX_RETRIES = 3
RETRY_BASE_DELAY = 1.5   # seconds; doubles each retry
MIN_REQUEST_INTERVAL = 0.12   # ~8 req/sec, safely under SEC's 10 req/sec cap


# ── data shapes ───────────────────────────────────────────────────────────────

@dataclass
class CompanyMatch:
    """One company result from search_company()."""
    cik:    str     # zero-padded 10-digit string, e.g. "0000320193"
    ticker: str     # e.g. "AAPL"
    name:   str     # e.g. "Apple Inc."

    def to_dict(self) -> dict:
        return {"cik": self.cik, "ticker": self.ticker, "name": self.name}


@dataclass
class FilingMeta:
    """One filing's metadata, from the submissions endpoint."""
    cik:               str
    accession_number:  str   # e.g. "0000320193-24-000123"
    form_type:         str   # "10-K" | "10-Q" | "8-K" | ...
    filing_date:       str   # "YYYY-MM-DD"
    report_date:       str   # period of report, "YYYY-MM-DD" (may be "")
    primary_document:  str   # filename of the main filing doc, e.g. "aapl-20240928.htm"
    company_name:      str = ""

    def to_dict(self) -> dict:
        return {
            "cik":               self.cik,
            "accession_number":  self.accession_number,
            "form_type":         self.form_type,
            "filing_date":       self.filing_date,
            "report_date":       self.report_date,
            "primary_document":  self.primary_document,
            "company_name":      self.company_name,
        }


# ── low-level HTTP helper ────────────────────────────────────────────────────

_last_request_time: float = 0.0


def _throttle() -> None:
    """Enforce a minimum gap between requests so we never approach SEC's 10 req/s cap."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < MIN_REQUEST_INTERVAL:
        time.sleep(MIN_REQUEST_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def _headers() -> dict:
    """
    SEC requires a descriptive User-Agent identifying the requester.
    Set EDGAR_USER_AGENT in .env, e.g.:
        EDGAR_USER_AGENT="DocuMind research-tool your_email@example.com"
    """
    return {
        "User-Agent": settings.EDGAR_USER_AGENT,
        "Accept-Encoding": "gzip, deflate",
    }


def _get_json(url: str) -> dict:
    """GET a JSON endpoint with retries, backoff, and rate-limiting."""
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = httpx.get(url, headers=_headers(), timeout=15.0)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            # 404 means "this company/CIK has no data here" — don't retry, just raise
            if exc.response.status_code == 404:
                raise
            last_exc = exc
        except Exception as exc:
            last_exc = exc

        if attempt < MAX_RETRIES:
            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(f"EDGAR request failed (attempt {attempt}/{MAX_RETRIES}): {last_exc}. Retrying in {delay:.1f}s…")
            time.sleep(delay)

    logger.error(f"EDGAR request failed after {MAX_RETRIES} attempts: {url}")
    raise RuntimeError(f"EDGAR request failed: {url}") from last_exc


def _get_text(url: str) -> str:
    """GET a raw document (HTML filing) with retries and rate-limiting."""
    last_exc: Optional[Exception] = None

    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = httpx.get(url, headers=_headers(), timeout=30.0)
            resp.raise_for_status()
            return resp.text
        except Exception as exc:
            last_exc = exc
            if attempt < MAX_RETRIES:
                delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
                logger.warning(f"EDGAR document fetch failed (attempt {attempt}/{MAX_RETRIES}): {exc}. Retrying in {delay:.1f}s…")
                time.sleep(delay)

    logger.error(f"EDGAR document fetch failed after {MAX_RETRIES} attempts: {url}")
    raise RuntimeError(f"EDGAR document fetch failed: {url}") from last_exc


# ── CIK formatting ────────────────────────────────────────────────────────────

def _pad_cik(cik: str | int) -> str:
    """Zero-pad a CIK to 10 digits, as every data.sec.gov endpoint requires."""
    return str(cik).strip().lstrip("0").zfill(10) if str(cik).strip() else ""


# ── ticker directory (cached) ────────────────────────────────────────────────

def _load_ticker_directory() -> Dict[str, dict]:
    """
    Load the full company_tickers.json directory, using a local disk cache
    refreshed at most once a week (this file changes rarely and is ~1MB —
    no reason to refetch it on every search).

    Returns:
        dict keyed by row index (SEC's native format), each value:
        {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
    """
    if TICKERS_CACHE_PATH.exists():
        age = time.time() - TICKERS_CACHE_PATH.stat().st_mtime
        if age < TICKERS_CACHE_MAX_AGE_SECONDS:
            logger.debug(f"Using cached company_tickers.json (age: {age / 3600:.1f}h)")
            with TICKERS_CACHE_PATH.open("r") as f:
                return json.load(f)

    logger.info("Fetching fresh company_tickers.json from SEC…")
    data = _get_json(TICKERS_URL)

    TICKERS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TICKERS_CACHE_PATH.open("w") as f:
        json.dump(data, f)

    logger.success(f"Cached company_tickers.json ({len(data)} companies) → {TICKERS_CACHE_PATH}")
    return data


# ── public API ────────────────────────────────────────────────────────────────

def search_company(query: str, limit: int = 10) -> List[CompanyMatch]:
    """
    Look up companies by ticker or name substring.

    Matching strategy:
      1. Exact ticker match (case-insensitive) always ranks first.
      2. Ticker prefix match ranks next.
      3. Name substring match ranks last.

    Args:
        query: ticker (e.g. "AAPL") or partial company name (e.g. "apple")
        limit: max results to return

    Returns:
        List of CompanyMatch, best match first. Empty list if nothing matches.

    Example:
        search_company("apple")  -> [CompanyMatch(cik="0000320193", ticker="AAPL", name="Apple Inc.")]
        search_company("AAPL")   -> same result
    """
    if not query.strip():
        return []

    q = query.strip().lower()
    directory = _load_ticker_directory()

    exact_ticker: List[CompanyMatch] = []
    prefix_ticker: List[CompanyMatch] = []
    name_match: List[CompanyMatch] = []

    for row in directory.values():
        ticker = str(row.get("ticker", ""))
        name   = str(row.get("title", ""))
        cik    = _pad_cik(row.get("cik_str", ""))

        if not ticker or not cik:
            continue

        match = CompanyMatch(cik=cik, ticker=ticker, name=name)

        if ticker.lower() == q:
            exact_ticker.append(match)
        elif ticker.lower().startswith(q):
            prefix_ticker.append(match)
        elif q in name.lower():
            name_match.append(match)

    results = exact_ticker + prefix_ticker + name_match
    results = results[:limit]

    logger.info(f"search_company('{query}') → {len(results)} match(es)")
    return results


def get_filings(
    cik: str,
    form_type: str = "10-K",
    limit: int = 10,
) -> List[FilingMeta]:
    """
    List a company's filings of a given form type, most recent first.

    Args:
        cik:       company CIK (with or without zero-padding — normalised internally)
        form_type: SEC form type, e.g. "10-K", "10-Q", "8-K". Pass "" for all forms.
        limit:     max filings to return

    Returns:
        List of FilingMeta, most recent filing_date first.

    Note:
        The submissions endpoint only returns the ~1000 most recent filings
        directly ("recent" key). Very old filings require paginating through
        `filings.files` — not implemented here since 10-K/10-Q history rarely
        needs to go back that far for this use case.

    Example:
        get_filings("0000320193", form_type="10-K", limit=5)
        -> [FilingMeta(accession_number="0000320193-24-000123", filing_date="2024-11-01", ...), ...]
    """
    padded_cik = _pad_cik(cik)
    url = SUBMISSIONS_URL.format(cik=padded_cik)

    data = _get_json(url)
    company_name = data.get("name", "")

    recent = data.get("filings", {}).get("recent", {})
    forms          = recent.get("form", [])
    accessions     = recent.get("accessionNumber", [])
    filing_dates   = recent.get("filingDate", [])
    report_dates   = recent.get("reportDate", [])
    primary_docs   = recent.get("primaryDocument", [])

    results: List[FilingMeta] = []
    for form, accession, fdate, rdate, doc in zip(
        forms, accessions, filing_dates, report_dates, primary_docs
    ):
        if form_type and form != form_type:
            continue

        results.append(FilingMeta(
            cik               = padded_cik,
            accession_number  = accession,
            form_type         = form,
            filing_date       = fdate,
            report_date       = rdate,
            primary_document  = doc,
            company_name      = company_name,
        ))

        if len(results) >= limit:
            break

    logger.info(
        f"get_filings(cik={padded_cik}, form_type='{form_type}') → "
        f"{len(results)} filing(s) for {company_name}"
    )
    return results


def fetch_filing_document(cik: str, accession_number: str, primary_document: str) -> str:
    """
    Fetch the raw HTML of a specific filing's primary document.

    Args:
        cik:               company CIK
        accession_number:  e.g. "0000320193-24-000123" (with dashes, as returned by get_filings)
        primary_document:  filename, e.g. "aapl-20240928.htm" (from FilingMeta.primary_document)

    Returns:
        Raw HTML string of the filing document.

    Example:
        filings = get_filings("0000320193", "10-K", limit=1)
        html = fetch_filing_document(
            filings[0].cik, filings[0].accession_number, filings[0].primary_document
        )
    """
    padded_cik      = _pad_cik(cik)
    cik_int         = str(int(padded_cik))            # archive URLs use CIK without zero-padding
    accession_nodash = accession_number.replace("-", "")

    url = ARCHIVE_DOC_URL.format(
        cik_int=cik_int,
        accession_nodash=accession_nodash,
        document=primary_document,
    )

    logger.info(f"Fetching filing document: {url}")
    html = _get_text(url)
    logger.success(f"Fetched filing document ({len(html):,} chars)")
    return html


def fetch_xbrl_facts(cik: str) -> dict:
    """
    Fetch every XBRL-tagged financial fact a company has ever reported.

    This is the source of truth for structured_store.py — every hard number
    (revenue, net income, EPS, total assets, etc.) should come from here,
    never from parsing prose in the filing text.

    Args:
        cik: company CIK

    Returns:
        Raw JSON dict, shaped like:
        {
          "cik": 320193,
          "entityName": "Apple Inc.",
          "facts": {
            "us-gaap": {
              "Revenues": {
                "label": "Revenues",
                "units": {
                  "USD": [
                    {"end": "2024-09-28", "val": 391035000000,
                     "fy": 2024, "fp": "FY", "form": "10-K", ...},
                    ...
                  ]
                }
              },
              ...
            },
            "dei": { ... }   # entity-level facts like shares outstanding
          }
        }

    Note:
        Not every company tags every metric the same way — e.g. some report
        revenue as "Revenues", others as "RevenueFromContractWithCustomerExcludingAssessedTax".
        structured_store.py should try a small list of known alternate tags
        per metric rather than assuming one canonical name.

    Example:
        facts = fetch_xbrl_facts("0000320193")
        revenues = facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    """
    padded_cik = _pad_cik(cik)
    url = COMPANYFACTS_URL.format(cik=padded_cik)

    logger.info(f"Fetching XBRL company facts for CIK {padded_cik}…")
    data = _get_json(url)

    fact_count = sum(
        len(concept.get("units", {}).get(unit, []))
        for taxonomy in data.get("facts", {}).values()
        for concept in taxonomy.values()
        for unit in concept.get("units", {})
    )
    logger.success(
        f"Fetched {fact_count:,} XBRL facts for '{data.get('entityName', padded_cik)}'"
    )
    return data


def fetch_company_concept(cik: str, tag: str, taxonomy: str = "us-gaap") -> dict:
    """
    Fetch a single XBRL concept's full history for a company — lighter-weight
    than fetch_xbrl_facts() when you only need one metric (e.g. just Revenues).

    Args:
        cik:       company CIK
        tag:       XBRL tag, e.g. "Revenues", "NetIncomeLoss", "Assets"
        taxonomy:  "us-gaap" (default) | "ifrs-full" | "dei" | "srt"

    Returns:
        Raw JSON dict with the concept's full reporting history across all
        filings, units, and periods.

    Example:
        rev = fetch_company_concept("0000320193", "Revenues")
        rev["units"]["USD"]  # list of {end, val, fy, fp, form, ...}
    """
    padded_cik = _pad_cik(cik)
    url = COMPANYCONCEPT_URL.format(cik=padded_cik, taxonomy=taxonomy, tag=tag)

    logger.info(f"Fetching XBRL concept '{taxonomy}:{tag}' for CIK {padded_cik}…")
    data = _get_json(url)
    logger.success(f"Fetched concept '{tag}' — {len(data.get('units', {}).get('USD', []))} USD data points")
    return data