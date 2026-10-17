"""
run_eval.py — regression harness for the SEC filings RAG pipeline.

Usage:
    python -m backend.eval.run_eval
    python -m backend.eval.run_eval --base-url http://localhost:8000 --only numeric
    python -m backend.eval.run_eval --case num-001

What it does:
  1. Loads eval_set.json.
  2. For each case, POSTs the question to your running API's /query endpoint.
  3. Grades the response differently depending on case["type"]:
       numeric    -> extract a number from the answer, compare to expected_value
                      within tolerance_pct (or cross-check directly against
                      structured_store if you wire that in — see NOTE below)
       computed   -> same numeric extraction/comparison, looser tolerance
       qualitative-> checks that must_include_concepts show up (soft match),
                      and that citations are present (nothing should be
                      asserted qualitatively without a source)
       refusal    -> checks the app actually declined / returned low
                      confidence instead of guessing
  4. Prints a pass/fail table and writes results to eval_results.json.

ASSUMPTIONS ABOUT YOUR API — ADJUST THESE IF THEY DON'T MATCH:
  - POST {base_url}/query accepts JSON: {"question": str, "cik": Optional[str]}
  - Response JSON has: {"answer": str, "citations": [...], "confidence": float}
  - "citations" is a list where each item has at least a "text" or "excerpt" key
  If your schemas.py uses different field names, only REQUEST_FIELD_MAP and
  RESPONSE_FIELD_MAP below need to change — the grading logic doesn't.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

# ── config — adjust to match your actual API/schema ─────────────────────────

DEFAULT_BASE_URL = "http://localhost:8000/api/v1"
EVAL_SET_PATH = Path(__file__).parent / "eval_set.json"
RESULTS_PATH = Path(__file__).parent / "eval_results.json"

QUERY_ENDPOINT = "/query"

# If your schemas.py names fields differently, change these two mappings only.
REQUEST_FIELD_MAP = {
    "question": "question",   # request body key <- our internal key
    "cik": "cik",
}
RESPONSE_FIELD_MAP = {
    "answer": "answer",       # our internal key <- response body key
    "citations": "citations",
    "confidence": "confidence",
}

# Phrases that signal a proper refusal/low-confidence response rather than a
# hallucinated guess. Extend this list to match your guard's actual wording
# (see hallucination/guard.py's LOW_CONFIDENCE_RESPONSE in config.py).
REFUSAL_MARKERS = [
    "don't have enough information",
    "do not contain enough information",
    "not available in",
    "cannot find",
    "no information",
    "not in the filing",
    "unable to confirm",
]

NUMBER_TOLERANCE_DEFAULT_PCT = 1.0
LOW_CONFIDENCE_THRESHOLD = 0.4  # only applies if the API ever returns a numeric confidence
# The API returns guard.py's ConfidenceLevel as a string; this one means "refused".
REFUSAL_CONFIDENCE_LABELS = {"INSUFFICIENT"}

# Generation waits out Gemini's per-minute rate limit server-side, which can
# take ~60s per retry — the client timeout must outlast that.
REQUEST_TIMEOUT_S = 300.0


# ── data classes ──────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    id: str
    type: str
    question: str
    passed: bool
    reason: str
    answer: str = ""
    confidence: Optional[float] = None
    citation_count: int = 0
    latency_s: float = 0.0


# ── helpers ──────────────────────────────────────────────────────────────

def load_eval_set(path: Path) -> list[dict]:
    with path.open("r") as f:
        data = json.load(f)
    return data["cases"]


YEAR_CONTEXT_RE = re.compile(
    r"(?:fy|fiscal\s+year|year\s+end(?:ed|ing)?|q[1-4]|quarter)\s*'?\s*$",
    re.IGNORECASE,
)


def extract_number(text: str) -> Optional[float]:
    """
    Pull the most plausible numeric figure out of a natural-language answer.
    Handles things like "$391.04 billion", "391,035,000,000", "12.5%".

    Strategy: prefer numbers written WITH commas or a currency/unit marker
    (real figures almost always have these), and explicitly exclude bare
    4-digit numbers immediately preceded by "FY"/"fiscal year"/"year ended"
    etc. (those are dates, not figures). This is intentionally forgiving —
    the goal is regression testing, not perfect NLP; eyeball flagged
    mismatches manually.
    """
    # billions/millions/trillions shorthand, e.g. "$391.04 billion"
    m = re.search(r"\$?\s?([\d.]+)\s?(trillion|billion|million|thousand)", text, re.IGNORECASE)
    if m:
        value = float(m.group(1))
        multiplier = {
            "trillion": 1e12,
            "billion": 1e9,
            "million": 1e6,
            "thousand": 1e3,
        }[m.group(2).lower()]
        return value * multiplier

    # percentage, e.g. "12.5%"
    m = re.search(r"([\d.]+)\s?%", text)
    if m:
        return float(m.group(1))

    # numbers written with thousands-separators, e.g. "391,035,000,000"
    # — real financial figures almost always have commas; years never do.
    candidates: list[float] = []
    for m in re.finditer(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text):
        candidates.append(float(m.group(0).replace(",", "")))
    if candidates:
        return max(candidates)

    # raw digit run of 5+ (no commas) — still too long to be a bare year
    for m in re.finditer(r"(?<!\d)(\d{5,})(?:\.\d+)?(?!\d)", text):
        candidates.append(float(m.group(0)))
    if candidates:
        return max(candidates)

    # bare 4-digit number NOT immediately preceded by year-ish context
    for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)", text):
        prefix = text[:m.start()]
        if YEAR_CONTEXT_RE.search(prefix):
            continue
        candidates.append(float(m.group(0)))
    if candidates:
        return max(candidates)

    # plain decimal fallback, e.g. EPS "6.08"
    m = re.search(r"([\d]+\.\d+)", text)
    if m:
        return float(m.group(1))

    return None


def within_tolerance(actual: float, expected: float, tolerance_pct: float) -> bool:
    if expected == 0:
        return abs(actual) < 1e-6
    return abs(actual - expected) / abs(expected) * 100 <= tolerance_pct


def extract_all_numbers(text: str, unit: Optional[str]) -> list[float]:
    """Every figure of the given kind in the answer — for multi-company answers that list one value each."""
    if unit == "percent":
        return [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)\s?%", text)]
    return [float(v.replace(",", "")) for v in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text)]


def looks_like_refusal(answer: str, confidence: Any) -> bool:
    lowered = answer.lower()
    if any(marker in lowered for marker in REFUSAL_MARKERS):
        return True
    if isinstance(confidence, str):
        return confidence.upper() in REFUSAL_CONFIDENCE_LABELS
    if confidence is not None and confidence < LOW_CONFIDENCE_THRESHOLD:
        return True
    return False


def soft_concept_match(answer: str, concepts: list[str]) -> tuple[int, list[str]]:
    """
    Loose keyword-overlap check: how many of the expected concepts are
    (loosely) present in the answer. Not a substitute for human review —
    use this to catch obvious misses, not to certify correctness.
    """
    lowered = answer.lower()
    hits = []
    for concept in concepts:
        # split multi-word concepts, require at least one meaningful token to hit
        tokens = [t for t in re.split(r"\s+", concept.lower()) if len(t) > 3]
        if not tokens:
            # short single-word concepts ("AWS", "AI") — match the whole thing
            tokens = [concept.lower()]
        if any(t in lowered for t in tokens):
            hits.append(concept)
    return len(hits), hits


# ── API call ───────────────────────────────────────────────────────────────

def call_query(base_url: str, question: str, cik: Optional[str] = None) -> dict:
    body = {REQUEST_FIELD_MAP["question"]: question}
    if cik:
        body[REQUEST_FIELD_MAP["cik"]] = cik

    resp = httpx.post(f"{base_url}{QUERY_ENDPOINT}", json=body, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    raw = resp.json()

    return {
        "answer": raw.get(RESPONSE_FIELD_MAP["answer"], ""),
        "citations": raw.get(RESPONSE_FIELD_MAP["citations"], []) or [],
        "confidence": raw.get(RESPONSE_FIELD_MAP["confidence"], None),
    }


# ── grading per case type ────────────────────────────────────────────────

def grade_numeric_or_computed(case: dict, result: dict) -> tuple[bool, str]:
    expected = case.get("expected_value")
    tolerance = case.get("tolerance_pct") or NUMBER_TOLERANCE_DEFAULT_PCT

    pattern = case.get("must_match_regex")
    if pattern and not re.search(pattern, result["answer"], re.IGNORECASE):
        return False, f"FAIL — answer doesn't match /{pattern}/: '{result['answer'][:150]}'"

    if case.get("expected_values"):
        found = extract_all_numbers(result["answer"], case.get("unit"))
        missing = [
            e for e in case["expected_values"]
            if not any(within_tolerance(a, e, tolerance) for a in found)
        ]
        if missing:
            return False, f"FAIL — expected {missing} among extracted {found} (±{tolerance}%)"
        return True, f"PASS — all of {case['expected_values']} found in answer (±{tolerance}%)"

    if expected is None:
        return True, "SKIPPED — no expected_value filled in yet (see eval_set.json _note)"

    actual = extract_number(result["answer"])
    if actual is None:
        return False, f"FAIL — could not extract a number from answer: '{result['answer'][:120]}'"

    if within_tolerance(actual, expected, tolerance):
        return True, f"PASS — extracted {actual}, expected {expected} (±{tolerance}%)"
    return False, f"FAIL — extracted {actual}, expected {expected} (±{tolerance}%)"


def grade_qualitative(case: dict, result: dict) -> tuple[bool, str]:
    if not result["citations"]:
        return False, "FAIL — no citations returned for a qualitative claim"

    concepts = case.get("must_include_concepts", [])
    if concepts:
        hit_count, hits = soft_concept_match(result["answer"], concepts)
        coverage = hit_count / len(concepts)
        if coverage < 0.5:
            return False, f"FAIL — only matched {hit_count}/{len(concepts)} expected concepts: {hits}"
        return True, f"PASS — matched {hit_count}/{len(concepts)} concepts, {len(result['citations'])} citation(s)"

    return True, f"PASS — {len(result['citations'])} citation(s) present, no concept list to check"


def grade_refusal(case: dict, result: dict) -> tuple[bool, str]:
    if looks_like_refusal(result["answer"], result["confidence"]):
        return True, "PASS — correctly refused / returned low confidence"
    return False, (
        f"FAIL — expected a refusal but got a confident answer: "
        f"'{result['answer'][:150]}' (confidence={result['confidence']})"
    )


def grade_case(case: dict, result: dict) -> tuple[bool, str]:
    if case.get("expected_behavior") == "refuse_or_low_confidence":
        return grade_refusal(case, result)
    if case["type"] in ("numeric", "computed"):
        return grade_numeric_or_computed(case, result)
    if case["type"] == "qualitative":
        return grade_qualitative(case, result)
    return False, f"FAIL — unknown case type '{case['type']}'"


# ── main ──────────────────────────────────────────────────────────────────

def run(base_url: str, only_type: Optional[str], only_id: Optional[str]) -> list[CaseResult]:
    cases = load_eval_set(EVAL_SET_PATH)

    if only_id:
        cases = [c for c in cases if c["id"] == only_id]
    elif only_type:
        cases = [c for c in cases if c["type"] == only_type]

    if not cases:
        print("No matching cases to run.")
        sys.exit(1)

    results: list[CaseResult] = []

    for case in cases:
        print(f"→ [{case['id']}] {case['question']}")
        start = time.monotonic()
        try:
            api_result = call_query(base_url, case["question"], case.get("cik"))
        except Exception as exc:
            results.append(CaseResult(
                id=case["id"], type=case["type"], question=case["question"],
                passed=False, reason=f"FAIL — request error: {exc}",
            ))
            print(f"   ✗ request error: {exc}\n")
            continue

        latency = time.monotonic() - start
        passed, reason = grade_case(case, api_result)

        results.append(CaseResult(
            id=case["id"], type=case["type"], question=case["question"],
            passed=passed, reason=reason,
            answer=api_result["answer"], confidence=api_result["confidence"],
            citation_count=len(api_result["citations"]), latency_s=round(latency, 2),
        ))

        symbol = "✓" if passed else "✗"
        print(f"   {symbol} {reason}  ({latency:.1f}s)\n")

    return results


def print_summary(results: list[CaseResult]) -> None:
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    skipped = sum(1 for r in results if r.reason.startswith("SKIPPED"))

    print("=" * 60)
    print(f"RESULTS: {passed}/{total} passed  ({skipped} skipped — no ground truth set)")
    print("=" * 60)

    by_type: dict[str, list[CaseResult]] = {}
    for r in results:
        by_type.setdefault(r.type, []).append(r)

    for t, rs in by_type.items():
        p = sum(1 for r in rs if r.passed)
        print(f"  {t:12s}  {p}/{len(rs)}")

    failures = [r for r in results if not r.passed]
    if failures:
        print("\nFailed cases:")
        for r in failures:
            print(f"  [{r.id}] {r.reason}")


def main():
    parser = argparse.ArgumentParser(description="Run the RAG pipeline eval suite.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--only", choices=["numeric", "computed", "qualitative"], default=None,
                         help="Run only cases of this type.")
    parser.add_argument("--case", default=None, help="Run a single case by id.")
    args = parser.parse_args()

    results = run(args.base_url, args.only, args.case)
    print_summary(results)

    with RESULTS_PATH.open("w") as f:
        json.dump([r.__dict__ for r in results], f, indent=2)
    print(f"\nFull results written to {RESULTS_PATH}")


if __name__ == "__main__":
    main()