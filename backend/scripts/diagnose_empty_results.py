"""
scripts/diagnose_empty_results.py — checks whether data actually exists for
a given CIK in both the vector store and structured_store, and whether the
CIK format matches what's stored. Run this before debugging retrieve()/guard
logic further — if the data isn't there (or is stored under a different CIK
string), that's the whole story.

Run: python3 -m scripts.diagnose_empty_results --cik 0000320193
"""

import argparse

from core.ingestion.indexer import get_collection
from core.ingestion.structured_store import list_available_metrics, get_metric, _connect


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cik", required=True)
    args = parser.parse_args()
    cik = args.cik

    print("=" * 70)
    print(f"Checking CIK: '{cik}'")
    print("=" * 70)

    # ── Chroma / vector store ────────────────────────────────────────────────
    collection = get_collection()
    total = collection.count()
    print(f"\nChroma collection total chunk count: {total}")

    if total == 0:
        print("⚠️  Collection is completely empty — nothing has been indexed, "
              "or it was cleared/pointed at a different path.")
    else:
        # what CIK values actually exist in the collection?
        sample = collection.get(limit=min(total, 500), include=["metadatas"])
        ciks_present = sorted({m.get("cik", "<missing>") for m in sample["metadatas"]})
        print(f"Distinct CIK values found in (sample of) collection: {ciks_present}")

        exact_match = collection.get(where={"cik": cik}, include=["metadatas"])
        print(f"Chunks matching cik == '{cik}' exactly: {len(exact_match.get('ids', []))}")

        if cik not in ciks_present and ciks_present:
            print(f"⚠️  Your requested cik '{cik}' does NOT match any stored CIK. "
                  f"Check for leading-zero / padding differences, e.g. stored as "
                  f"'{ciks_present[0]}' vs requested '{cik}'.")

    # ── structured_store ──────────────────────────────────────────────────────
    print(f"\nstructured_store metrics available for cik='{cik}':")
    metrics = list_available_metrics(cik)
    print(f"  {metrics}")

    if not metrics:
        print("⚠️  No rows at all for this CIK in structured_store. Either "
              "upsert_facts() was never called for it, or it's stored under "
              "a different CIK string.")

        # check what CIKs DO exist in the facts table
        with _connect() as conn:
            rows = conn.execute("SELECT DISTINCT cik FROM facts").fetchall()
        ciks_in_db = sorted(r["cik"] for r in rows)
        print(f"  Distinct CIKs actually present in facts table: {ciks_in_db}")
    else:
        # try a direct lookup of "revenue" specifically, since that's what
        # query_router.py's METRIC_SYNONYMS assumes as the canonical name
        fact = get_metric(cik, "revenue", fiscal_period="FY")
        print(f"\nget_metric(cik, 'revenue', fiscal_period='FY') → {fact}")
        if fact is None:
            print("⚠️  'revenue' is not a canonical metric name in your data. "
                  f"Your ACTUAL available metric names are: {metrics}")
            print("    query_router.py's METRIC_SYNONYMS keys need to match these "
                  "exactly — that's the likely bug if this prints a mismatch.")


if __name__ == "__main__":
    main()