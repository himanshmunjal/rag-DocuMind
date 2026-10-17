"""
structured_store.py — SQLite store for XBRL financial facts.

This is the single source of truth for hard numbers. Every numeric or
computed answer the app gives should come from here, never from the LLM
reading prose in a filing. query_router.py is the main consumer.

Flow:
    edgar_client.fetch_xbrl_facts(cik)  -->  upsert_facts(cik, ...)  -->  get_metric(...)

The XBRL JSON from SEC nests facts as:
    facts[taxonomy][tag]["units"][unit] -> [ {end, val, fy, fp, form, ...}, ... ]
upsert_facts() walks METRIC_TAG_ALIASES, finds whichever tag a company
actually used for each canonical metric, and flattens the matching data
points into one flat table.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from typing import Iterator, Optional

from loguru import logger

# from config import settings, METRIC_TAG_ALIASES, DEI_METRICS, PREFERRED_FORM_TYPES
from config import settings

# ── schema ────────────────────────────────────────────────────────────────────

# period_start is part of the UNIQUE key (and '' rather than NULL for instant
# facts, since SQLite treats NULLs as distinct in UNIQUE constraints). Without
# it, a 10-K's 3-month Q4 figure and its 12-month annual figure share
# (period_end, fiscal_period="FY", form_type="10-K") and silently overwrite
# each other; same for a 10-Q's 3-month vs. year-to-date figures.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    cik               TEXT    NOT NULL,
    company_name      TEXT,
    metric            TEXT    NOT NULL,   -- canonical name, e.g. "revenue"
    xbrl_tag          TEXT    NOT NULL,   -- actual tag matched, e.g. "Revenues"
    unit              TEXT    NOT NULL,   -- "USD", "USD/shares", "shares"
    value             REAL    NOT NULL,
    period_start      TEXT    NOT NULL DEFAULT '',  -- "YYYY-MM-DD", '' for instant facts
    period_end        TEXT    NOT NULL,   -- "YYYY-MM-DD"
    fiscal_year       INTEGER,
    fiscal_period     TEXT,               -- "FY" | "Q1" | "Q2" | "Q3" | "Q4"
    form_type         TEXT,               -- "10-K" | "10-Q"
    filed_date        TEXT,
    accession_number  TEXT,
    UNIQUE(cik, metric, unit, period_start, period_end, fiscal_period, form_type)
);

CREATE INDEX IF NOT EXISTS idx_facts_lookup
    ON facts (cik, metric, fiscal_year, fiscal_period);
"""

# Duration windows (days) a fact must fall in to count as a full fiscal year /
# a single quarter. Instant facts (balance sheet, shares outstanding) have no
# start date and always qualify.
_DURATION_DAYS = {"FY": (330, 400), "Q": (75, 105)}


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.FILINGS_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with _connect() as conn:
        existing = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'facts'"
        ).fetchone()
        if existing and "period_start, period_end" not in existing["sql"]:
            # Old schema (period_start missing from the UNIQUE key). Carry the
            # rows over so nothing disappears, but they may contain collided
            # values — re-run upsert_facts() (scripts/rebuild_facts.py) to fix.
            logger.warning("facts table uses the old UNIQUE key — migrating; re-ingest XBRL to repair values")
            conn.execute("ALTER TABLE facts RENAME TO facts_old")
            conn.execute("DROP INDEX IF EXISTS idx_facts_lookup")
            conn.executescript(_SCHEMA)
            conn.execute(
                """
                INSERT OR IGNORE INTO facts (
                    cik, company_name, metric, xbrl_tag, unit, value, period_start, period_end,
                    fiscal_year, fiscal_period, form_type, filed_date, accession_number
                )
                SELECT cik, company_name, metric, xbrl_tag, unit, value, COALESCE(period_start, ''),
                       period_end, fiscal_year, fiscal_period, form_type, filed_date, accession_number
                FROM facts_old
                """
            )
            conn.execute("DROP TABLE facts_old")
        else:
            conn.executescript(_SCHEMA)


init_db()  # safe no-op if tables already exist


# ── result shape ──────────────────────────────────────────────────────────────

@dataclass
class MetricFact:
    metric: str
    value: float
    unit: str
    period_end: str
    fiscal_year: Optional[int]
    fiscal_period: Optional[str]
    form_type: Optional[str]
    xbrl_tag: str
    accession_number: Optional[str]
    company_name: str = ""

    def to_dict(self) -> dict:
        return {
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
            "period_end": self.period_end,
            "fiscal_year": self.fiscal_year,
            "fiscal_period": self.fiscal_period,
            "form_type": self.form_type,
            "xbrl_tag": self.xbrl_tag,
            "accession_number": self.accession_number,
            "company_name": self.company_name,
        }


# ingestion

def _days_between(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def upsert_facts(cik: str, xbrl_json: dict) -> int:
    """
    Flatten a company's raw XBRL companyfacts JSON (from
    edgar_client.fetch_xbrl_facts) into the facts table.

    For each canonical metric in METRIC_TAG_ALIASES, ingests data from
    EVERY alias tag that has data — not just the first one found.

    Why: companies change which XBRL tag they report a metric under over
    time (e.g. Apple's revenue moved from "Revenues" to
    "RevenueFromContractWithCustomerExcludingAssessedTax" around ASC 606
    adoption in ~2019). Stopping at the first alias with ANY data means
    that if the old tag still has a few legacy datapoints, you silently
    ingest only the stale years and never see the tag that has your
    current data.

    When two aliases report the SAME period, the one listed first in
    METRIC_TAG_ALIASES wins (e.g. NetIncomeLoss over ProfitLoss, which
    includes noncontrolling interests). Within one tag, the most recently
    filed value wins, so restatements replace originals.

    fiscal_year is derived per datapoint, not copied from XBRL's "fy": "fy"
    is the fiscal year of the FILING, so a FY2025 10-K tags its FY2023 and
    FY2024 comparatives as fy=2025 too. We subtract the number of whole
    years between the datapoint's end date and the filing's own period end.

    Quarter-length datapoints tagged fp="FY" (quarterly breakdowns inside a
    10-K) are skipped — they are not fiscal-year figures, and the 10-Qs
    already carry them under their real quarter.

    Returns: number of rows inserted/updated.
    """
    company_name = xbrl_json.get("entityName", "")
    all_facts = xbrl_json.get("facts", {})
    us_gaap = all_facts.get("us-gaap", {})
    dei = all_facts.get("dei", {})

    rows: list[tuple] = []

    for metric, tag_aliases in settings.METRIC_TAG_ALIASES.items():
        taxonomy = dei if metric in settings.DEI_METRICS else us_gaap
        # key -> (alias priority, filed date, row tuple)
        chosen: dict[tuple, tuple[int, str, tuple]] = {}

        for priority, tag in enumerate(tag_aliases):
            concept = taxonomy.get(tag)
            if concept is None or not concept.get("units"):
                continue

            for unit, datapoints in concept["units"].items():
                filing_period_end: dict[str, str] = {}
                for dp in datapoints:
                    if dp.get("accn") and dp.get("end"):
                        filing_period_end[dp["accn"]] = max(filing_period_end.get(dp["accn"], ""), dp["end"])

                for dp in datapoints:
                    val, end = dp.get("val"), dp.get("end")
                    if val is None or not end:
                        continue
                    start = dp.get("start") or ""
                    fp = dp.get("fp")
                    if fp == "FY" and start:
                        lo, hi = _DURATION_DAYS["FY"]
                        if not lo <= _days_between(start, end) <= hi:
                            continue

                    fiscal_year = dp.get("fy")
                    own_end = filing_period_end.get(dp.get("accn"))
                    if fiscal_year is not None and own_end:
                        fiscal_year -= round(_days_between(end, own_end) / 365.25)

                    key = (unit, start, end, fp, dp.get("form"))
                    filed = dp.get("filed") or ""
                    # lower alias priority wins; within a tag, later filing wins
                    if key in chosen:
                        cur_priority, cur_filed, _ = chosen[key]
                        if cur_priority < priority or (cur_priority == priority and cur_filed >= filed):
                            continue

                    chosen[key] = (priority, filed, (
                        cik, company_name, metric, tag, unit, float(val), start, end,
                        fiscal_year, fp, dp.get("form"), filed or None, dp.get("accn"),
                    ))

        rows.extend(row for _, _, row in chosen.values())

    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO facts (
                cik, company_name, metric, xbrl_tag, unit, value,
                period_start, period_end, fiscal_year, fiscal_period,
                form_type, filed_date, accession_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cik, metric, unit, period_start, period_end, fiscal_period, form_type)
            DO UPDATE SET
                value = excluded.value,
                xbrl_tag = excluded.xbrl_tag,
                fiscal_year = excluded.fiscal_year,
                company_name = excluded.company_name,
                filed_date = excluded.filed_date,
                accession_number = excluded.accession_number
            """,
            rows,
        )

    logger.success(f"upsert_facts(cik={cik}) → {len(rows)} rows for {company_name}")
    return len(rows)


# ── lookups ───────────────────────────────────────────────────────────────────

def _duration_clause(fiscal_period: str) -> str:
    """SQL fragment restricting rows to full-year (FY) or single-quarter (Qn) facts."""
    lo, hi = _DURATION_DAYS["FY" if fiscal_period == "FY" else "Q"]
    return (
        f" AND (period_start = '' OR "
        f"julianday(period_end) - julianday(period_start) BETWEEN {lo} AND {hi})"
    )


def _form_priority(r: sqlite3.Row) -> int:
    try:
        return settings.PREFERRED_FORM_TYPES.index(r["form_type"])
    except ValueError:
        return len(settings.PREFERRED_FORM_TYPES)


def _best_per_period(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Collapse rows sharing a period_end (10-K vs 10-K/A vs 10-Q) to the preferred form, most recent first."""
    by_end: dict[str, sqlite3.Row] = {}
    for r in rows:
        if r["period_end"] not in by_end or _form_priority(r) < _form_priority(by_end[r["period_end"]]):
            by_end[r["period_end"]] = r
    return [by_end[end] for end in sorted(by_end, reverse=True)]


def get_metric(
    cik: str,
    metric: str,
    fiscal_year: Optional[int] = None,
    fiscal_period: str = "FY",
) -> Optional[MetricFact]:
    """
    Get a single metric value for a company/period.

    If fiscal_year is None, returns the most recent available value.
    When multiple form types report the same period (10-K restating a
    10-Q figure, etc.), prefers the order in PREFERRED_FORM_TYPES.
    Only full-year facts count for "FY" and only 3-month facts for "Qn".

    Example:
        get_metric("0000320193", "revenue", fiscal_year=2024)
        -> MetricFact(value=391035000000.0, unit="USD", ...)
    """
    query = "SELECT * FROM facts WHERE cik = ? AND metric = ? AND fiscal_period = ?"
    params: list = [cik, metric, fiscal_period]

    if fiscal_year is not None:
        query += " AND fiscal_year = ?"
        params.append(fiscal_year)

    query += _duration_clause(fiscal_period)

    with _connect() as conn:
        rows = conn.execute(query, params).fetchall()

    best = _best_per_period(rows)
    return _row_to_fact(best[0]) if best else None


def get_metric_history(
    cik: str,
    metric: str,
    fiscal_period: str = "FY",
    limit: int = 8,
) -> list[MetricFact]:
    """Full history of one metric, one fact per period, most recent period first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM facts WHERE cik = ? AND metric = ? AND fiscal_period = ?"
            + _duration_clause(fiscal_period),
            (cik, metric, fiscal_period),
        ).fetchall()

    return [_row_to_fact(r) for r in _best_per_period(rows)[:limit]]


def compute_change(
    cik: str,
    metric: str,
    fiscal_year: int,
    compare_to_fiscal_year: Optional[int] = None,
    fiscal_period: str = "FY",
) -> Optional[dict]:
    """
    YoY (or arbitrary period-over-period) change for a metric.
    Defaults to comparing fiscal_year against fiscal_year - 1.

    Returns:
        {"current": ..., "previous": ..., "abs_change": ..., "pct_change": ...}
        or None if either period is missing.
    """
    prev_year = compare_to_fiscal_year or (fiscal_year - 1)

    current = get_metric(cik, metric, fiscal_year=fiscal_year, fiscal_period=fiscal_period)
    previous = get_metric(cik, metric, fiscal_year=prev_year, fiscal_period=fiscal_period)

    if current is None or previous is None:
        return None

    abs_change = current.value - previous.value
    pct_change = (abs_change / previous.value * 100) if previous.value else None

    return {
        "metric": metric,
        "current_year": fiscal_year,
        "current_value": current.value,
        "previous_year": prev_year,
        "previous_value": previous.value,
        "abs_change": abs_change,
        "pct_change": pct_change,
        "unit": current.unit,
    }


# ── derived ratios ────────────────────────────────────────────────────────────
# Ratios aren't reported as XBRL facts, so they're computed here from two
# stored facts for the SAME fiscal year — never by the LLM.
# Each entry: list of (numerator, denominator) recipes, tried in order.
# A numerator of ("revenue", "-cost_of_revenue") means revenue - cost_of_revenue,
# for filers (e.g. Amazon) that don't tag GrossProfit.

RATIO_DEFINITIONS: dict[str, list[tuple[tuple[str, ...], str]]] = {
    "gross_margin":     [(("gross_profit",), "revenue"), (("revenue", "-cost_of_revenue"), "revenue")],
    "operating_margin": [(("operating_income",), "revenue")],
    "net_margin":       [(("net_income",), "revenue")],
    "rd_to_revenue":    [(("rd_expense",), "revenue")],
}


def get_ratio(
    cik: str,
    ratio: str,
    fiscal_year: Optional[int] = None,
    fiscal_period: str = "FY",
) -> Optional[dict]:
    """
    Compute a derived ratio (as a percentage) for one fiscal year.

    If fiscal_year is None, anchors on the most recent year the denominator
    is available for — NOT the most recent year of the numerator, which can
    be stale (Amazon's last GrossProfit tag is from 2009).

    Returns:
        {"ratio", "value_pct", "fiscal_year", "fiscal_period", "components": [MetricFact, ...]}
        or None if the inputs aren't available for that year.
    """
    for numerator_parts, denominator in RATIO_DEFINITIONS[ratio]:
        if fiscal_year is None:
            anchor = get_metric(cik, denominator, fiscal_period=fiscal_period)
            if anchor is None:
                return None
            year = anchor.fiscal_year
        else:
            year = fiscal_year

        denom = get_metric(cik, denominator, fiscal_year=year, fiscal_period=fiscal_period)
        if denom is None or not denom.value:
            continue

        parts = []
        for part in numerator_parts:
            fact = get_metric(cik, part.lstrip("-"), fiscal_year=year, fiscal_period=fiscal_period)
            if fact is None or fact.period_end != denom.period_end:
                break
            parts.append((part, fact))
        else:
            numerator = sum(-f.value if p.startswith("-") else f.value for p, f in parts)
            return {
                "ratio": ratio,
                "value_pct": numerator / denom.value * 100,
                "fiscal_year": year,
                "fiscal_period": fiscal_period,
                "components": [f for _, f in parts] + [denom],
            }

    return None


def list_available_metrics(cik: str) -> list[str]:
    """Which canonical metrics do we actually have data for, for this company."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT metric FROM facts WHERE cik = ?", (cik,)
        ).fetchall()
    return sorted(r["metric"] for r in rows)


def list_companies() -> list[tuple[str, str]]:
    """(cik, company_name) for every company with stored facts."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT cik, MAX(company_name) AS company_name FROM facts GROUP BY cik"
        ).fetchall()
    return [(r["cik"], r["company_name"] or "") for r in rows]


def _row_to_fact(row: sqlite3.Row) -> MetricFact:
    return MetricFact(
        metric=row["metric"],
        value=row["value"],
        unit=row["unit"],
        period_end=row["period_end"],
        fiscal_year=row["fiscal_year"],
        fiscal_period=row["fiscal_period"],
        form_type=row["form_type"],
        xbrl_tag=row["xbrl_tag"],
        accession_number=row["accession_number"],
        company_name=row["company_name"] or "",
    )
