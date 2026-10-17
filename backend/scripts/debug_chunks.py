# """
# scripts/debug_chunks.py — inspect what filing_chunker actually produces for one filing.

# Run this BEFORE re-ingesting, to confirm Item detection is working, e.g.:
#     python3 scripts/debug_chunks.py --cik 0000320193 --form_type 10-K

# It fetches the filing fresh (does not touch Chroma/structured_store), chunks it,
# and prints a per-chunk summary plus a raw-text preview around the spots where
# "Item 7" etc. should appear — useful for spotting why detection is failing
# (e.g. tags split across spans, table-of-contents false matches).
# """

# from __future__ import annotations

# import argparse
# import re
# from collections import Counter

# from core.ingestion import edgar_client
# from core.ingestion.filing_chunker import chunk_filing


# def main():
#     parser = argparse.ArgumentParser()
#     parser.add_argument("--cik", required=True)
#     parser.add_argument("--form_type", default="10-K")
#     parser.add_argument("--limit", type=int, default=1, help="how many recent filings to check")
#     args = parser.parse_args()

#     filings = edgar_client.get_filings(args.cik, form_type=args.form_type, limit=args.limit)
#     if not filings:
#         print(f"No {args.form_type} filings found for CIK {args.cik}")
#         return

#     for f in filings:
#         print("=" * 80)
#         print(f"{f.company_name} — {f.form_type} — accession {f.accession_number} — filed {f.filing_date}")
#         print("=" * 80)

#         html = edgar_client.fetch_filing_document(f.cik, f.accession_number, f.primary_document)
#         print(f"Raw HTML length: {len(html):,} chars")

#         # crude check: does the raw HTML even contain recognizable Item headers?
#         raw_matches = re.findall(r"item\s*\d+[a-c]?\.?", html, flags=re.IGNORECASE)
#         print(f"Raw regex hits for 'Item N' pattern in HTML: {len(raw_matches)} "
#               f"(sample: {raw_matches[:10]})")

#         filing_meta = {
#             "cik": f.cik,
#             "accession_number": f.accession_number,
#             "form_type": f.form_type,
#             "company_name": f.company_name,
#             "filing_date": f.filing_date,
#             "report_date": f.report_date,
#         }
#         chunks = chunk_filing(html=html, filing_meta=filing_meta)
#         print(f"\nTotal chunks produced: {len(chunks)}")

#         if chunks:
#             print(f"\nFirst chunk raw keys: {list(chunks[0].keys())}")
#             print(f"First chunk raw dict (truncated): "
#                   f"{ {k: (v[:80] if isinstance(v, str) else v) for k, v in chunks[0].items()} }")

#         def _item_of(c: dict) -> str:
#             # handle both possible shapes until we confirm which one filing_chunker uses:
#             # flat: {"item": ..., "text": ...}  vs  nested: {"metadata": {"item": ...}, "text": ...}
#             if "item" in c:
#                 return c.get("item") or "unknown"
#             if "metadata" in c and isinstance(c["metadata"], dict):
#                 return c["metadata"].get("item") or "unknown"
#             return "unknown"

#         item_counts = Counter(_item_of(c) for c in chunks)
#         print("\nChunk count per item tag:")
#         for item, count in sorted(item_counts.items()):
#             print(f"  {item:20s} {count:4d} chunks")

#         def _title_of(c: dict) -> str:
#             if "item_title" in c:
#                 return c.get("item_title") or ""
#             if "metadata" in c and isinstance(c["metadata"], dict):
#                 return c["metadata"].get("item_title") or ""
#             return ""

#         def _text_of(c: dict) -> str:
#             return c.get("text") or c.get("content") or ""

#         print("\n--- Per-chunk summary (first 20) ---")
#         for c in chunks[:20]:
#             item = _item_of(c)
#             title = _title_of(c)
#             text = _text_of(c)
#             preview = text[:80].replace("\n", " ")
#             print(f"[{item:12s}] title='{title[:30]:30s}' len={len(text):5d}  \"{preview}...\"")

#         if item_counts.get("unknown", 0) == len(chunks):
#             print("\n⚠️  EVERY chunk is 'unknown' — Item header detection is not matching.")
#             print("    Next step: inspect the text passed into the chunker's header regex,")
#             print("    not just the raw HTML above — the bug is likely in how HTML gets")
#             print("    converted to plain text (headers split across <span> tags, or the")
#             print("    primary_document pointing at a TOC/wrapper page instead of the real filing body).")


# if __name__ == "__main__":
#     main()
"""
scripts/debug_chunks.py — inspect what filing_chunker actually produces for one filing.

Run this BEFORE re-ingesting, to confirm Item detection is working, e.g.:
    python3 scripts/debug_chunks.py --cik 0000320193 --form_type 10-K

It fetches the filing fresh (does not touch Chroma/structured_store), chunks it,
and prints a per-chunk summary plus a raw-text preview around the spots where
"Item 7" etc. should appear — useful for spotting why detection is failing
(e.g. tags split across spans, table-of-contents false matches).
"""

from __future__ import annotations

import argparse
import re
from collections import Counter

from core.ingestion import edgar_client
from core.ingestion.filing_chunker import chunk_filing


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cik", required=True)
    parser.add_argument("--form_type", default="10-K")
    parser.add_argument("--limit", type=int, default=1, help="how many recent filings to check")
    args = parser.parse_args()

    filings = edgar_client.get_filings(args.cik, form_type=args.form_type, limit=args.limit)
    if not filings:
        print(f"No {args.form_type} filings found for CIK {args.cik}")
        return

    for f in filings:
        print("=" * 80)
        print(f"{f.company_name} — {f.form_type} — accession {f.accession_number} — filed {f.filing_date}")
        print("=" * 80)

        html = edgar_client.fetch_filing_document(f.cik, f.accession_number, f.primary_document)
        print(f"Raw HTML length: {len(html):,} chars")

        # crude check: does the raw HTML even contain recognizable Item headers?
        raw_matches = re.findall(r"item\s*\d+[a-c]?\.?", html, flags=re.IGNORECASE)
        print(f"Raw regex hits for 'Item N' pattern in HTML: {len(raw_matches)} "
              f"(sample: {raw_matches[:10]})")

        filing_meta = {
            "cik": f.cik,
            "accession_number": f.accession_number,
            "form_type": f.form_type,
            "company_name": f.company_name,
            "filing_date": f.filing_date,
            "report_date": f.report_date,
        }
        chunks = chunk_filing(html=html, filing_meta=filing_meta)
        print(f"\nTotal chunks produced: {len(chunks)}")

        if chunks:
            print(f"\nFirst chunk raw keys: {list(chunks[0].keys())}")
            print(f"First chunk raw dict (truncated): "
                  f"{ {k: (v[:80] if isinstance(v, str) else v) for k, v in chunks[0].items()} }")

        def _item_of(c: dict) -> str:
            # handle both possible shapes until we confirm which one filing_chunker uses:
            # flat: {"item": ..., "text": ...}  vs  nested: {"metadata": {"item": ...}, "text": ...}
            if "item" in c:
                return c.get("item") or "unknown"
            if "metadata" in c and isinstance(c["metadata"], dict):
                return c["metadata"].get("item") or "unknown"
            return "unknown"

        item_counts = Counter(_item_of(c) for c in chunks)
        print("\nChunk count per item tag:")
        for item, count in sorted(item_counts.items()):
            print(f"  {item:20s} {count:4d} chunks")

        def _title_of(c: dict) -> str:
            if "item_title" in c:
                return c.get("item_title") or ""
            if "metadata" in c and isinstance(c["metadata"], dict):
                return c["metadata"].get("item_title") or ""
            return ""

        def _text_of(c: dict) -> str:
            return c.get("text") or c.get("content") or ""

        print("\n--- Per-chunk summary (first 20) ---")
        for c in chunks[:20]:
            item = _item_of(c)
            title = _title_of(c)
            text = _text_of(c)
            preview = text[:80].replace("\n", " ")
            print(f"[{item:12s}] title='{title[:30]:30s}' len={len(text):5d}  \"{preview}...\"")

        if item_counts.get("unknown", 0) == len(chunks):
            print("\n⚠️  EVERY chunk is 'unknown' — Item header detection is not matching.")
            print("    Next step: inspect the text passed into the chunker's header regex,")
            print("    not just the raw HTML above — the bug is likely in how HTML gets")
            print("    converted to plain text (headers split across <span> tags, or the")
            print("    primary_document pointing at a TOC/wrapper page instead of the real filing body).")


if __name__ == "__main__":
    main()