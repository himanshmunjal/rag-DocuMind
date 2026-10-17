"""
test_structured_store.py — standalone smoke test for the EDGAR -> structured_store pipeline.

Run from the `backend/` directory so `from config import settings` resolves:
    cd backend
    python scripts/test_structured_store.py apple

Or test a different company:
    python scripts/test_structured_store.py microsoft
    python scripts/test_structured_store.py TSLA
"""

import sys
from pathlib import Path

# Add the backend directory to Python's import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ingestion.edgar_client import search_company, fetch_xbrl_facts
from core.ingestion.structured_store import upsert_facts, get_metric, get_metric_history, compute_change, list_available_metrics


def main(query: str) -> None:
    print(f"\n=== 1. search_company('{query}') ===")
    matches = search_company(query)
    if not matches:
        print(f"No company found for '{query}'. Try a different name/ticker.")
        sys.exit(1)

    for m in matches[:5]:
        print(f"  {m.ticker:8s} {m.cik}  {m.name}")

    company = matches[0]
    print(f"\nUsing top match: {company.name} (CIK {company.cik})")

    print(f"\n=== 2. fetch_xbrl_facts(cik={company.cik}) ===")
    xbrl_json = fetch_xbrl_facts(company.cik)
    print(f"  entityName: {xbrl_json.get('entityName')}")
    print(f"  taxonomies present: {list(xbrl_json.get('facts', {}).keys())}")

    print(f"\n=== 3. upsert_facts(cik={company.cik}) ===")
    rows_written = upsert_facts(company.cik, xbrl_json)
    print(f"  rows written: {rows_written}")

    print(f"\n=== 4. list_available_metrics(cik={company.cik}) ===")
    available = list_available_metrics(company.cik)
    print(f"  {available}")

    print(f"\n=== 5. get_metric(cik={company.cik}, metric='revenue') ===")
    revenue = get_metric(company.cik, "revenue")
    if revenue is None:
        print("  !! No revenue found — check METRIC_TAG_ALIASES or company's tagging.")
    else:
        print(f"  FY{revenue.fiscal_year} revenue: {revenue.value:,.0f} {revenue.unit}")
        print(f"  (tag used: {revenue.xbrl_tag}, form: {revenue.form_type}, period_end: {revenue.period_end})")

    print(f"\n=== 6. get_metric_history(cik={company.cik}, metric='revenue', limit=5) ===")
    history = get_metric_history(company.cik, "revenue", limit=5)
    for fact in history:
        print(f"  FY{fact.fiscal_year}: {fact.value:,.0f} {fact.unit}  (form: {fact.form_type})")

    if len(history) >= 2:
        fy_latest = history[0].fiscal_year
        print(f"\n=== 7. compute_change(cik={company.cik}, metric='revenue', fiscal_year={fy_latest}) ===")
        change = compute_change(company.cik, "revenue", fiscal_year=fy_latest)
        if change:
            print(
                f"  FY{change['previous_year']} -> FY{change['current_year']}: "
                f"{change['previous_value']:,.0f} -> {change['current_value']:,.0f} "
                f"({change['pct_change']:+.1f}%)"
            )
        else:
            print("  Not enough history to compute change.")

    print("\n✅ Pipeline test complete.")


if __name__ == "__main__":
    company_query = sys.argv[1] if len(sys.argv) > 1 else "apple"
    main(company_query)