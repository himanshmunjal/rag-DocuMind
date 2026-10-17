"""
scripts/test_routes.py — integration test for Day 3 endpoints.

Hits a RUNNING server (not TestClient), because /filings/ingest makes real
calls out to EDGAR (rate-limited, network-dependent) and we want to test the
actual deployed behavior, not an in-process mock.

Usage:
    # terminal 1 (from backend/, matching your project layout)
    uvicorn main:app --reload --port 8000

    # terminal 2
    python scripts/test_routes.py
    python scripts/test_routes.py --ticker MSFT --form 10-Q
    python scripts/test_routes.py --skip-ingest

    # base URL defaults to http://localhost:8000/api/v1 (matches the
    # app.include_router(router, prefix="/api/v1") in main.py). Override if
    # you ever change the prefix or run on a different port:
    python scripts/test_routes.py --base-url http://localhost:8000/api/v1

Covers:
    GET  /companies/search
    GET  /companies/{cik}/filings
    POST /filings/ingest

Does NOT cover /query or /filings/compare yet — those depend on
query_router.py (Day 4) and will fail to import until then. Once Day 4 is
done, uncomment test_query() in main() below.
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional

import httpx


PASS = "✅"
FAIL = "❌"
INFO = "ℹ️ "


class TestFailure(Exception):
    pass


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise TestFailure(message)


def test_search_companies(client: httpx.Client, ticker: str) -> dict:
    print(f"\n--- GET /companies/search?q={ticker} ---")
    resp = client.get("companies/search", params={"q": ticker})
    _check(resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}")

    data = resp.json()
    _check(data["results"], f"no company results for '{ticker}'")

    top = data["results"][0]
    _check(top["ticker"].upper() == ticker.upper(), f"expected top match ticker={ticker}, got {top['ticker']}")
    _check(len(top["cik"]) == 10 and top["cik"].isdigit(), f"CIK not 10-digit zero-padded: {top['cik']}")

    print(f"{PASS} found {len(data['results'])} match(es), top: {top['name']} (CIK {top['cik']})")
    return top


def test_list_filings(client: httpx.Client, cik: str, form_type: str) -> dict:
    print(f"\n--- GET /companies/{cik}/filings?form_type={form_type} ---")
    resp = client.get(f"companies/{cik}/filings", params={"form_type": form_type, "limit": 5})
    _check(resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}")

    data = resp.json()
    _check(data["filings"], f"no {form_type} filings found for CIK {cik}")

    for f in data["filings"]:
        _check(f["form_type"] == form_type, f"form_type mismatch: expected {form_type}, got {f['form_type']}")
        _check(f["accession_number"], "filing missing accession_number")
        _check(f["primary_document"], "filing missing primary_document")

    latest = data["filings"][0]
    print(
        f"{PASS} found {len(data['filings'])} {form_type} filing(s) for {data['company_name']}. "
        f"Latest: {latest['accession_number']} filed {latest['filing_date']} "
        f"(already ingested: {latest['ingested']})"
    )
    return latest


def test_ingest_filing(client: httpx.Client, cik: str, filing: dict, form_type: str) -> dict:
    print(f"\n--- POST /filings/ingest ({filing['accession_number']}) ---")
    payload = {
        "cik": cik,
        "accession_number": filing["accession_number"],
        "primary_document": filing["primary_document"],
        "form_type": form_type,
    }

    t0 = time.monotonic()
    resp = client.post("filings/ingest", json=payload, timeout=120.0)
    elapsed = time.monotonic() - t0

    _check(resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}")
    data = resp.json()

    _check(data["status"] in ("success", "partial"), f"unexpected status: {data['status']} — {data.get('message')}")
    _check(data["chunks_indexed"] > 0, "zero chunks indexed — chunker likely produced nothing")
    _check(data["items_found"], "no Items detected — check filing_chunker's Item regex")

    if data["status"] == "partial":
        print(f"{INFO} partial ingest: {data['message']}")
    else:
        print(f"{PASS} full ingest succeeded")

    print(
        f"    chunks_indexed={data['chunks_indexed']}  "
        f"xbrl_facts_stored={data['xbrl_facts_stored']}  "
        f"items_found={data['items_found']}  "
        f"({elapsed:.1f}s)"
    )
    return data


def test_ingest_is_idempotent_ish(client: httpx.Client, cik: str, filing: dict, form_type: str) -> None:
    """Re-ingesting the same filing shouldn't crash or silently duplicate chunk counts wildly."""
    print(f"\n--- POST /filings/ingest again (idempotency check) ---")
    payload = {
        "cik": cik,
        "accession_number": filing["accession_number"],
        "primary_document": filing["primary_document"],
        "form_type": form_type,
    }
    resp = client.post("filings/ingest", json=payload, timeout=120.0)
    _check(resp.status_code == 200, f"re-ingest failed: {resp.status_code}: {resp.text}")
    print(f"{PASS} re-ingest did not error (dedup/upsert behavior not strictly verified here)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Day 3 routes against a running server")
    parser.add_argument("--base-url", default="http://localhost:8000/api/v1/")
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--form", default="10-K")
    parser.add_argument("--skip-ingest", action="store_true", help="skip the (slower) ingest test")
    args = parser.parse_args()

    # httpx.Client base_url joining drops any path prefix if base_url lacks a
    # trailing slash — normalize here so this can't silently break again.
    if not args.base_url.endswith("/"):
        args.base_url += "/"

    failures = 0

    with httpx.Client(base_url=args.base_url) as client:
        try:
            top_company = test_search_companies(client, args.ticker)
        except TestFailure as e:
            print(f"{FAIL} test_search_companies: {e}")
            return 1  # can't continue without a CIK

        try:
            latest_filing = test_list_filings(client, top_company["cik"], args.form)
        except TestFailure as e:
            print(f"{FAIL} test_list_filings: {e}")
            return 1  # can't continue without a filing

        if not args.skip_ingest:
            try:
                test_ingest_filing(client, top_company["cik"], latest_filing, args.form)
            except TestFailure as e:
                print(f"{FAIL} test_ingest_filing: {e}")
                failures += 1

            try:
                test_ingest_is_idempotent_ish(client, top_company["cik"], latest_filing, args.form)
            except TestFailure as e:
                print(f"{FAIL} test_ingest_is_idempotent_ish: {e}")
                failures += 1
        else:
            print(f"\n{INFO} --skip-ingest passed, skipping ingestion tests")

    print("\n" + "=" * 50)
    if failures == 0:
        print(f"{PASS} All Day 3 route tests passed")
        return 0
    else:
        print(f"{FAIL} {failures} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())