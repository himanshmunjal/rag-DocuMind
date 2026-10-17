"""
rebuild_facts.py — re-pull XBRL facts from EDGAR for every company already in
filings.db and rebuild their rows from scratch.

Needed once after the structured_store schema fix: rows written by the old
UNIQUE key may hold a 3-month value where a 12-month value belongs (or the
wrong alias tag's value), and an upsert alone won't remove those.

Run from the `backend/` directory:
    python scripts/rebuild_facts.py
    python scripts/rebuild_facts.py 0000320193 0000789019   # specific CIKs
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.ingestion.edgar_client import fetch_xbrl_facts
from core.ingestion.structured_store import _connect, list_companies, upsert_facts


def main(ciks: list[str]) -> None:
    ciks = ciks or [cik for cik, _ in list_companies()]
    for cik in ciks:
        xbrl = fetch_xbrl_facts(cik)
        with _connect() as conn:
            conn.execute("DELETE FROM facts WHERE cik = ?", (cik,))
        rows = upsert_facts(cik, xbrl)
        print(f"{cik}  {xbrl.get('entityName', '')}: {rows} rows")


if __name__ == "__main__":
    main(sys.argv[1:])
