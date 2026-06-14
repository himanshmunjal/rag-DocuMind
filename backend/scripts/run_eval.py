"""
scripts/run_eval.py — Entry point for Day-15 RAGAS evaluation.

Run from the project root (with the API server already running):

    # Basic run — uses built-in sample questions
    python scripts/run_eval.py

    # Point at your own test file (JSON array)
    python scripts/run_eval.py --test-file scripts/my_tests.json

    # Custom output path
    python scripts/run_eval.py --out reports/eval_v1.json

    # Only run on a specific document
    python scripts/run_eval.py --source contract_a.pdf

    # Dry-run: print the test cases that would be used, don't call the API
    python scripts/run_eval.py --dry-run

JSON test file format
─────────────────────
[
  {
    "question":     "What is the penalty for late payment?",
    "ground_truth": "A 2% monthly surcharge applies after 30 days.",
    "sources":      ["contract_a.pdf"]
  },
  {
    "question": "Who are the parties to the agreement?"
  }
]

`ground_truth` is optional — omitting it skips the context_recall metric
for that question. `sources` is optional — omitting it searches all documents.
"""

import sys
import json
import argparse
from pathlib import Path

# Make sure `backend` is importable when run from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from evaluation.ragas_eval import evaluate


# ── built-in sample test cases ─────────────────────────────────────────────────
# Replace or extend these with questions specific to your documents.
# A good test set has 10–20 questions spread across:
#   - factual lookups ("What is X?")
#   - reasoning ("Why did Y happen?")
#   - cross-document comparisons ("How does A differ from B?")
#   - edge cases ("What happens if Z is not specified?")

SAMPLE_TEST_CASES = [
    # ── Factual lookups ────────────────────────────────────────────────────────
    {
        "question": "What is the effective date of this agreement?",
        # No ground_truth — context_recall will be skipped for this question
    },
    {
        "question": "Who are the parties named in this contract?",
    },
    {
        "question": "What is the payment schedule described in the document?",
    },

    # ── With ground truth (enables context_recall) ─────────────────────────────
    # Fill these in after you've manually read your test documents.
    # {
    #     "question":     "What is the notice period for contract termination?",
    #     "ground_truth": "Either party may terminate with 30 days written notice.",
    # },
    # {
    #     "question":     "What is the governing law clause?",
    #     "ground_truth": "This agreement is governed by the laws of the State of Delaware.",
    # },

    # ── Edge cases: the hallucination guard should fire on these ───────────────
    {
        "question": "What is the CEO's home address?",
        # Expect: INSUFFICIENT — this should not be in any contract
    },
    {
        "question": "What did the parties discuss over lunch on March 3rd?",
        # Expect: INSUFFICIENT — not in any document
    },

    # ── Reasoning ─────────────────────────────────────────────────────────────
    {
        "question": "What are the consequences of a material breach?",
    },
    {
        "question": "Under what conditions can the contract be automatically renewed?",
    },
]


# ── CLI ────────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run RAGAS evaluation against the DocuMind API.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--test-file", "-t",
        type=Path,
        default=None,
        help="Path to a JSON file containing test cases (array of objects). "
             "Defaults to built-in sample questions.",
    )
    p.add_argument(
        "--out", "-o",
        type=Path,
        default=Path("eval_results.json"),
        help="Output path for the JSON report (default: eval_results.json).",
    )
    p.add_argument(
        "--source", "-s",
        type=str,
        default=None,
        help="Restrict all test questions to a single document filename.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the test cases that would be run, then exit without calling the API.",
    )
    return p.parse_args()


def load_test_cases(args: argparse.Namespace) -> list:
    if args.test_file:
        path = args.test_file
        if not path.exists():
            logger.error(f"Test file not found: {path}")
            sys.exit(1)
        with path.open() as f:
            cases = json.load(f)
        if not isinstance(cases, list):
            logger.error("Test file must contain a JSON array of objects.")
            sys.exit(1)
        logger.info(f"Loaded {len(cases)} test cases from {path}")
    else:
        cases = SAMPLE_TEST_CASES
        logger.info(f"Using {len(cases)} built-in sample test cases")

    # Apply --source filter if given
    if args.source:
        for c in cases:
            c.setdefault("sources", [args.source])
        logger.info(f"Restricting all questions to source: {args.source}")

    return cases


def validate_api_is_running() -> None:
    """Fail fast with a clear message if the backend is not up."""
    import requests
    try:
        r = requests.get("http://localhost:8000/api/v1/status", timeout=5)
        r.raise_for_status()
        stats = r.json()
        logger.info(
            f"API is running — {stats['total_vectors']} vectors, "
            f"{stats['document_count']} document(s) indexed"
        )
        if stats["document_count"] == 0:
            logger.warning(
                "No documents are indexed yet. "
                "Upload a PDF first with POST /upload or via the UI."
            )
    except Exception as exc:
        logger.error(
            f"Cannot reach the DocuMind API at localhost:8000.\n"
            f"  Start it with: uvicorn backend.main:app --port 8000\n"
            f"  Error: {exc}"
        )
        sys.exit(1)


# ── main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    args  = parse_args()
    cases = load_test_cases(args)

    # ── Dry run: just show the test cases ─────────────────────────────────────
    if args.dry_run:
        print(f"\n{'─'*56}")
        print(f"  DRY RUN — {len(cases)} test case(s) would be evaluated")
        print(f"{'─'*56}")
        for i, c in enumerate(cases, 1):
            gt  = c.get("ground_truth", "(no ground truth)")
            src = c.get("sources", "all documents")
            print(f"\n  [{i}] {c['question']}")
            print(f"       ground_truth : {gt[:60]}{'…' if len(gt) > 60 else ''}")
            print(f"       sources      : {src}")
        print(f"\n{'─'*56}\n")
        return

    # ── Live run ───────────────────────────────────────────────────────────────
    validate_api_is_running()

    report = evaluate(test_cases=cases, out_path=args.out)

    # Exit with non-zero if faithfulness or answer_relevancy are failing
    summary = report.get("summary", {})
    low_metrics = [
        name for name, data in summary.items()
        if data.get("grade") in ("C", "D")
    ]
    if low_metrics:
        logger.warning(
            f"Low scores on: {', '.join(low_metrics)}. "
            "Review chunking strategy and retrieval thresholds."
        )
        sys.exit(2)   # non-zero so CI pipelines can catch regressions

    logger.success("All metrics passed minimum threshold.")


if __name__ == "__main__":
    main()