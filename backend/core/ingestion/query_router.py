# """
# core/query_router.py — decides how a question should be answered.

# Three routes, in order of preference (numbers should never come from the LLM):

#   numeric      "What was Apple's revenue in FY2024?"
#                -> single fact lookup in structured_store, no generation involved

#   computed     "How did gross margin change YoY?" / "Compare revenue growth"
#                -> 2+ facts pulled from structured_store, arithmetic done in
#                   Python (routes.py), LLM only narrates the result

#   qualitative  "Why did margins change?" / "What risks does Apple flag?"
#                -> normal retrieve -> rerank -> guard -> generate pipeline

# This is deliberately keyword/regex based, not an LLM classifier. For a v1
# with a handful of query shapes, heuristics are faster, free, and fully
# debuggable — you can see exactly why a question was routed a certain way.
# If real usage surfaces question shapes these heuristics miss, that's the
# signal to graduate to an LLM-based router, not a reason to build one now.

# Fail-safe design: if we can't confidently identify a metric, we return
# query_type="qualitative" rather than guessing — routes.py's numeric/computed
# handlers already fall back to qualitative on missing metric/cik too, so this
# module and routes.py both err toward "ask the retrieval pipeline" over
# "guess a number".
# """

# from __future__ import annotations

# import re
# from typing import Literal, Optional, TypedDict

# QueryType = Literal["numeric", "computed", "qualitative"]


# # ── metric vocabulary ────────────────────────────────────────────────────────
# # Canonical metric names here MUST match the keys structured_store.get_metric()
# # expects (i.e. the same canonical names used in config.METRIC_TAG_ALIASES).
# # Ordered longest-phrase-first within each list isn't required — matching
# # below sorts by phrase length automatically to prefer specific over vague
# # (e.g. "gross margin" wins over a bare "margin").

# METRIC_KEYWORDS: dict[str, list[str]] = {
#     "revenue": ["revenue", "revenues", "total revenue", "net sales", "sales"],
#     "net_income": ["net income", "net earnings", "net profit", "earnings"],
#     "gross_profit": ["gross profit"],
#     "gross_margin": ["gross margin"],
#     "operating_income": ["operating income", "income from operations"],
#     "operating_margin": ["operating margin"],
#     "eps": ["eps", "earnings per share", "diluted eps", "basic eps"],
#     "total_assets": ["total assets"],
#     "total_liabilities": ["total liabilities"],
#     "operating_cash_flow": [
#         "operating cash flow",
#         "cash flow from operations",
#         "cash from operations",
#     ],
#     "rnd_expense": ["r&d", "r & d", "research and development"],
# }

# # Flattened + sorted longest-first so "gross margin" is checked before "margin"
# # ever gets a chance to (falsely) match inside it.
# _ALL_PHRASES: list[tuple[str, str]] = sorted(
#     ((phrase, metric) for metric, phrases in METRIC_KEYWORDS.items() for phrase in phrases),
#     key=lambda pair: len(pair[0]),
#     reverse=True,
# )

# QUALITATIVE_OVERRIDE_SIGNALS = [
#     "why", "explain", "describe",
#     "what risks", "what does", "discuss", "summarize", "summarise",
#     "outline", "what challenges", "what factors",
# ]
# # Note: "how did X change" is intentionally NOT here — it's ambiguous between
# # "how did it change [give me the numbers]" (computed) and "how did it change
# # [explain the reason]" (qualitative). We resolve it in favor of computed when
# # a metric + comparison signal (YoY, trend, etc.) is present, since that's the
# # more common phrasing for this question shape; a bare "how did X change" with
# # no resolvable metric still falls through to the qualitative default at the
# # end of classify().

# COMPUTED_SIGNALS = [
#     "compare", "comparison", " vs ", " versus ", "trend", "yoy",
#     "year over year", "year-over-year", "change from", "growth",
#     "increase", "decrease", "grew", "declined", "improved", "worsened",
#     "how did .* change", "difference between",
# ]

# NUMERIC_SIGNALS = [
#     "what was", "what is", "what were", "what are",
#     "how much", "how many", "give me", "tell me the", "value of",
# ]

# YEAR_PATTERN = re.compile(r"\b(19|20)\d{2}\b")
# FISCAL_YEAR_PATTERN = re.compile(r"\bfy\s?(\d{2,4})\b", re.IGNORECASE)


# class MetricRequest(TypedDict):
#     metric: Optional[str]
#     fiscal_year: Optional[int]
#     all_years_mentioned: list[int]


# # ── public API ────────────────────────────────────────────────────────────────

# def classify(question: str) -> QueryType:
#     """
#     Classify a question into numeric | computed | qualitative.

#     Priority order matters:
#       1. Qualitative override signals always win — "why did revenue change"
#          contains a comparison word ("change") but the user wants an
#          explanation, not two numbers.
#       2. Computed signals + a resolvable metric -> computed.
#       3. Numeric signals + a resolvable metric -> numeric.
#       4. Everything else -> qualitative (the safe default).
#     """
#     q = question.lower().strip()
#     metric = _find_metric(q)

#     if _matches_any(q, QUALITATIVE_OVERRIDE_SIGNALS):
#         return "qualitative"

#     if metric and _matches_any(q, COMPUTED_SIGNALS):
#         return "computed"

#     if metric and _matches_any(q, NUMERIC_SIGNALS):
#         return "numeric"

#     # metric mentioned with no clear intent verb — lean numeric only if the
#     # question is short and metric-only, e.g. "Apple revenue 2024"
#     if metric and len(q.split()) <= 8:
#         return "numeric"

#     return "qualitative"


# def extract_metric_request(question: str) -> MetricRequest:
#     """
#     Best-effort extraction of (metric, fiscal_year) from a question.

#     For computed questions, fiscal_year is deliberately left as None even if
#     a year is mentioned — routes.py's computed handler wants the full metric
#     history so it can pick the latest two periods itself. Restricting to one
#     year here would starve it of the comparison point.

#     Example:
#         extract_metric_request("What was Apple's revenue in FY2024?")
#         -> {"metric": "revenue", "fiscal_year": 2024, "all_years_mentioned": [2024]}

#         extract_metric_request("How did gross margin change YoY?")
#         -> {"metric": "gross_margin", "fiscal_year": None, "all_years_mentioned": []}
#     """
#     q = question.lower().strip()
#     metric = _find_metric(q)
#     years = _extract_years(q)

#     is_computed = _matches_any(q, COMPUTED_SIGNALS) and not _matches_any(q, QUALITATIVE_OVERRIDE_SIGNALS)
#     fiscal_year = None if is_computed else (years[0] if years else None)

#     return MetricRequest(
#         metric=metric,
#         fiscal_year=fiscal_year,
#         all_years_mentioned=years,
#     )


# def get_supported_metrics() -> list[str]:
#     """Exposed for debugging / test assertions — canonical metric names this router knows about."""
#     return sorted(METRIC_KEYWORDS.keys())


# # ── internals ─────────────────────────────────────────────────────────────────

# def _matches_any(q: str, signals: list[str]) -> bool:
#     for s in signals:
#         if s.startswith(("how did", "how does")) and ".*" in s:
#             if re.search(s, q):
#                 return True
#         elif s in q:
#             return True
#     return False


# def _find_metric(q: str) -> Optional[str]:
#     for phrase, metric in _ALL_PHRASES:
#         if phrase in q:
#             return metric
#     return None


# def _extract_years(q: str) -> list[int]:
#     years: list[int] = []

#     for m in FISCAL_YEAR_PATTERN.finditer(q):
#         digits = m.group(1)
#         year = int(digits) if len(digits) == 4 else 2000 + int(digits)
#         years.append(year)

#     for m in YEAR_PATTERN.finditer(q):
#         year = int(m.group(0))
#         if year not in years:
#             years.append(year)

#     return years

"""
query_router.py — classifies a question into numeric | computed | qualitative,
and for numeric/computed extracts which metric and period(s) it's about.

*** ASSUMPTION THAT NEEDS VERIFYING ***
METRIC_SYNONYMS below maps natural-language phrases to CANONICAL metric names
("revenue", "net_income", ...). These canonical names MUST exactly match the
keys in settings.METRIC_TAG_ALIASES (config.py) that structured_store.py
reads from — I haven't seen that dict, so I'm using a reasonable guess.
If your actual keys differ (e.g. "total_revenue" instead of "revenue"), the
fix is just updating the dict keys below to match. Paste that section of
config.py and I'll reconcile it exactly.

Consumers (api/routes.py):
    classify(question)               -> "numeric" | "computed" | "qualitative"
    extract_metric_request(question) -> dict for numeric/computed lookups
    suggest_item_filter(question)    -> "1A" | "7" | ... | None, for scoping
                                         qualitative retrieval by 10-K Item
"""

from __future__ import annotations

import re
from typing import Literal, Optional

# ── canonical metric -> natural-language synonyms ──────────────────────────
# NOTE: keys here must match settings.METRIC_TAG_ALIASES keys in config.py.

METRIC_SYNONYMS: dict[str, list[str]] = {
    "revenue":              ["revenue", "revenues", "sales", "net sales", "total revenue", "top line"],
    "net_income":           ["net income", "profit", "net profit", "earnings", "bottom line"],
    "gross_profit":         ["gross profit"],
    "operating_income":     ["operating income", "operating profit", "income from operations"],
    "eps_diluted":          ["eps", "earnings per share", "diluted eps"],
    "total_assets":         ["total assets", "assets"],
    "total_liabilities":    ["total liabilities", "liabilities"],
    "cash_and_equivalents": ["cash and equivalents", "cash and cash equivalents", "cash on hand", "cash"],
    "rd_expense":           ["r&d", "research and development", "rd spend", "r&d spending"],
    "cost_of_revenue":      ["cost of revenue", "cost of sales", "cost of goods sold", "cogs"],
    "long_term_debt":       ["long-term debt", "long term debt"],
    "stockholders_equity":  ["stockholders' equity", "shareholders' equity", "stockholders equity",
                             "shareholders equity", "total equity"],
    "shares_outstanding":   ["shares outstanding", "outstanding shares"],
    # derived ratios — computed by structured_store.get_ratio(), not stored facts
    "gross_margin":         ["gross margin", "gross profit margin"],
    "operating_margin":     ["operating margin", "operating profit margin"],
    "net_margin":           ["net margin", "net profit margin", "profit margin", "net income margin"],
}

# Metrics answered by structured_store.get_ratio() rather than get_metric().
RATIO_METRICS = {"gross_margin", "operating_margin", "net_margin", "rd_to_revenue"}

# "<metric> as a percentage of revenue" turns a base metric into its ratio.
PCT_OF_REVENUE_RE = re.compile(
    r"(as an?|in) (percentage|percent|%|share|proportion) of (revenue|sales)"
    r"|% of (revenue|sales)|percent of (revenue|sales)|relative to (revenue|sales)"
    r"|(r&d|research and development) intensity"
)
RATIO_OF_BASE: dict[str, str] = {
    "rd_expense": "rd_to_revenue",
    "net_income": "net_margin",
    "operating_income": "operating_margin",
    "gross_profit": "gross_margin",
}

# Phrasings that don't keep the metric phrase in one piece, e.g.
# "how many shares of Tesla common stock were outstanding".
METRIC_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bshares\b.*\boutstanding\b|\boutstanding\b.*\bshares\b"), "shares_outstanding"),
]

# Phrases signaling a comparison/trend question (→ "computed"), as long as a
# metric is also present. On their own these don't force "computed" — a
# comparison question with no recognizable metric falls through to qualitative.
COMPUTED_SIGNALS = [
    "change", "changed", "growth", "grew", "grow", "grown",
    "increase", "increased", "decrease", "decreased",
    "yoy", "year over year", "year-over-year",
    "compare", "compared", " vs ", " vs. ", "versus", "trend",
    "difference between", "how much more", "how much less",
]

# Phrases signaling a "why/explain" question. These win over metric/computed
# signals — "why did revenue grow" is qualitative (needs MD&A prose), not a
# numeric lookup, even though "revenue" and "grow" both appear.
QUALITATIVE_SIGNALS = [
    "why", "how did", "what caused", "explain", "discuss", "describe",
    "what are the risks", "what risks", "outlook", "strategy", "reason for",
    "reasons for", "drivers of", "what drove",
    # not filing content at all — must not be routed to a metric lookup just
    # because they contain "earnings" or "price"
    "earnings call", "conference call", "analyst", "price target", "stock price",
]

FISCAL_YEAR_RE = re.compile(r"\b(?:fy\s?)?(20\d{2})\b", re.IGNORECASE)

# Best-effort keyword -> 10-K Item mapping, using the same item codes
# filing_chunker.py produces ("1", "1A", "7", "7A", "8", ...).
ITEM_KEYWORDS: dict[str, list[str]] = {
    "1A": ["risk factor", "risks facing", "what risks", "key risks", "supply chain risk",
           "competitive risk", "main risks", "risks related", "risks does", "risks do"],
    "1":  ["business overview", "business description", "business model", "what does the company do", "products and services", "competition"],
    "7":  ["md&a", "management discussion", "management's discussion", "results of operations", "margin", "why did", "what drove", "outlook",
           "capital expenditure", "capex", "liquidity"],
    "7A": ["market risk", "interest rate risk", "currency risk", "foreign exchange risk"],
    "3":  ["legal proceeding", "lawsuit", "litigation"],
    "9A": ["controls and procedures", "internal controls"],
    "1C": ["cybersecurity", "data breach", "security incident"],
}

# Short names that don't appear in a company's legal name.
COMPANY_ALIASES: dict[str, str] = {"google": "alphabet", "facebook": "meta"}


def classify(question: str) -> Literal["numeric", "computed", "qualitative"]:
    """
    Routing priority: qualitative signals win first (a "why" question about
    a metric still needs prose, not a number). Then: metric + comparison
    language -> computed. Then: metric alone -> numeric. Otherwise -> qualitative.
    """
    q = question.lower()

    if any(sig in q for sig in QUALITATIVE_SIGNALS):
        return "qualitative"

    metric = _find_metric(q)
    has_computed_signal = any(sig in q for sig in COMPUTED_SIGNALS)

    if metric and has_computed_signal:
        return "computed"
    if metric:
        return "numeric"

    return "qualitative"


def extract_metric_request(question: str) -> dict:
    """
    For numeric/computed questions: pull out the canonical metric name and
    any fiscal year(s) mentioned.

    Returns:
        {
          "metric": str | None,
          "fiscal_period": "FY",
          "fiscal_year": int | None,               # target/most-recent year
          "compare_to_fiscal_year": int | None,    # only set if 2 years mentioned
        }

    If no year is mentioned at all, both year fields are None — the caller
    (routes.py) should treat that as "most recent available" for numeric,
    or "most recent vs. prior" for computed (structured_store.compute_change
    already defaults compare_to_fiscal_year to fiscal_year - 1 when None).
    """
    q = question.lower()
    metric = _find_metric(q)
    years = _find_fiscal_years(q)

    result: dict = {"metric": metric, "fiscal_period": "FY"}

    if not years:
        result["fiscal_year"] = None
        result["compare_to_fiscal_year"] = None
    elif len(years) == 1:
        result["fiscal_year"] = years[0]
        result["compare_to_fiscal_year"] = None
    else:
        years_sorted = sorted(years)
        result["fiscal_year"] = years_sorted[-1]           # later year = "current"
        result["compare_to_fiscal_year"] = years_sorted[0]  # earlier year = "previous"

    return result


def suggest_item_filter(question: str) -> Optional[str]:
    """
    Best-effort guess at which 10-K Item a qualitative question concerns,
    for scoping searcher.search(item_filter=...). Returns None if nothing
    matches confidently — caller should search unscoped rather than guess
    wrong and miss relevant content.
    """
    q = question.lower()
    for item, keywords in ITEM_KEYWORDS.items():
        if any(kw in q for kw in keywords):
            return item
    return None


# ── internals ─────────────────────────────────────────────────────────────────

def find_companies(question: str, companies: list[dict]) -> list[str]:
    """
    CIKs of the known companies mentioned in a question, in order of mention.

    `companies`: [{"cik": str, "name": str, "ticker": str | None}, ...] — the
    caller supplies the ingested set, so this module stays DB-free. A company
    matches on the first word of its name ("apple" for "Apple Inc.") or its
    ticker, both as whole words.
    """
    q = question.lower()
    for alias, name in COMPANY_ALIASES.items():
        q = re.sub(rf"\b{alias}\b", name, q)

    found: list[tuple[int, str]] = []
    for company in companies:
        keys = []
        first_word = re.sub(r"[^a-z0-9&-]", "", (company.get("name") or "").lower().split(" ")[0])
        if len(first_word) >= 3:
            keys.append(first_word)
        if company.get("ticker"):
            keys.append(company["ticker"].lower())
        positions = [m.start() for k in keys for m in re.finditer(rf"\b{re.escape(k)}\b", q)]
        if positions:
            found.append((min(positions), company["cik"]))

    return [cik for _, cik in sorted(found)]


def _find_metric(q: str) -> Optional[str]:
    """Longest whole-word synonym match wins, so e.g. 'net sales' beats a
    shorter unrelated overlap (and 'eps' doesn't match inside 'steps').
    Ties broken by dict iteration order (first defined). A base metric
    followed by "as a percentage of revenue" becomes its ratio."""
    best_match: Optional[str] = None
    best_len = 0
    for canonical, synonyms in METRIC_SYNONYMS.items():
        for syn in synonyms:
            if len(syn) > best_len and re.search(rf"(?<![\w&]){re.escape(syn)}(?![\w&])", q):
                best_match = canonical
                best_len = len(syn)

    for pattern, canonical in METRIC_PATTERNS:
        if pattern.search(q):
            return canonical

    if best_match in RATIO_OF_BASE and PCT_OF_REVENUE_RE.search(q):
        return RATIO_OF_BASE[best_match]
    return best_match


def _find_fiscal_years(q: str) -> list[int]:
    years = {int(m.group(1)) for m in FISCAL_YEAR_RE.finditer(q)}
    return sorted(years)
