"""
Standalone sanity test for filing_chunker.py against a REAL EDGAR filing.

Run from the backend/ directory (so `from config import settings` resolves):
    cd backend
    python test_filing_chunker.py

Change COMPANY_QUERY below to test a different company.
"""
import sys
from pathlib import Path

# Add the backend directory to Python's import path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ingestion.edgar_client import search_company, get_filings, fetch_filing_document
from core.ingestion.filing_chunker import chunk_filing

COMPANY_QUERY = "apple"
FORM_TYPE = "10-K"

# 1. Resolve company -> CIK
matches = search_company(COMPANY_QUERY)
if not matches:
    raise SystemExit(f"No company found for '{COMPANY_QUERY}'")
company = matches[0]
print(f"Company: {company.name} ({company.ticker}, CIK {company.cik})")

# 2. Get most recent 10-K
filings = get_filings(company.cik, form_type=FORM_TYPE, limit=1)
if not filings:
    raise SystemExit(f"No {FORM_TYPE} filings found for {company.name}")
filing = filings[0]
print(f"Filing: {filing.form_type} filed {filing.filing_date} (period {filing.report_date})")
print(f"Accession: {filing.accession_number}, doc: {filing.primary_document}")

# 3. Fetch the actual filing HTML
html = fetch_filing_document(filing.cik, filing.accession_number, filing.primary_document)
print(f"Fetched HTML: {len(html):,} chars")

# 4. Chunk it
chunks = chunk_filing(html, filing.to_dict())
print(f"\nTotal chunks: {len(chunks)}")

# 5. Chunk counts per item
from collections import Counter
item_counts = Counter(c["metadata"]["item"] for c in chunks)
print("\nChunks per item:")
for item, count in sorted(item_counts.items()):
    title = chunks[0]["metadata"].get("item_title", "")
    # find the title for this item from any chunk that has it
    title = next((c["metadata"]["item_title"] for c in chunks if c["metadata"]["item"] == item), "")
    print(f"  Item {item:<4} ({title[:50]:<50}) — {count} chunks")

# 6. Eyeball 3 sample chunks: first, middle, last
print("\n--- Sample chunks ---")
sample_indices = [0, len(chunks) // 2, len(chunks) - 1]
for idx in sample_indices:
    c = chunks[idx]
    m = c["metadata"]
    print(f"\n[chunk {m['chunk_index']}] item={m['item']} ({m['item_title']}) chars={m['char_count']}")
    print(c["text"][:400] + ("..." if len(c["text"]) > 400 else ""))