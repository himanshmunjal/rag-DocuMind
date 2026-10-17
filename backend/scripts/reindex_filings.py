"""
reindex_filings.py — drop a company's indexed chunks and re-ingest its most
recent filing through the same path as POST /filings/ingest.

Use after changing filing_chunker.py: chunks indexed by an older chunker
version stay in ChromaDB (with their old Item tags and text) until replaced.

Run from the `backend/` directory, with the API server stopped:
    python scripts/reindex_filings.py                          # every company currently indexed
    python scripts/reindex_filings.py 0000320193 0000789019    # specific CIKs (indexed or not)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.routes import ingest_filing
from api.schemas import IngestFilingRequest
from core.ingestion import edgar_client
from core.ingestion.indexer import get_collection


def main(ciks: list[str], form_type: str = "10-K") -> None:
    collection = get_collection()
    if not ciks:
        metas = collection.get(include=["metadatas"])["metadatas"]
        ciks = sorted({m["cik"] for m in metas if m and m.get("cik")})

    for cik in ciks:
        latest = edgar_client.get_filings(cik, form_type=form_type, limit=1)
        if not latest:
            print(f"{cik}: no {form_type} filings found — skipped")
            continue
        filing = latest[0]

        collection.delete(where={"cik": cik})
        result = ingest_filing(IngestFilingRequest(
            cik=cik,
            accession_number=filing.accession_number,
            primary_document=filing.primary_document,
            form_type=form_type,
        ))
        print(
            f"{cik}  {result.company_name}  {filing.accession_number} ({filing.report_date}): "
            f"{result.chunks_indexed} chunks, items {result.items_found}, status={result.status}"
        )


if __name__ == "__main__":
    main(sys.argv[1:])
