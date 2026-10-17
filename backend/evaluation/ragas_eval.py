"""
ragas_eval.py — Automated RAG evaluation using RAGAS  (Day 15)

RAGAS measures four things that actually matter for a RAG system:

  faithfulness        — Does the answer stay within what the retrieved context says?
                        Catches hallucinations: high score = model didn't invent facts.

  answer_relevancy    — Does the answer actually address the question asked?
                        Catches vague or off-topic responses.

  context_precision   — Are the retrieved chunks relevant to the question?
                        Catches retrieval noise: high score = retriever is pulling
                        the right paragraphs, not random filler.

  context_recall      — Does the retrieved context cover the ground truth answer?
                        Requires ground_truth labels; skip on first run if you
                        don't have them yet.

Usage
-----
Run from the project root:

    python scripts/run_eval.py

Or call evaluate() directly:

    from evaluation.ragas_eval import evaluate
    report = evaluate(test_cases, out_path="eval_results.json")

Test dataset format
-------------------
A list of dicts:

    [
      {
        "question":     "What is the notice period for termination?",
        "ground_truth": "30 days written notice is required.",   # optional
        "sources":      ["contract_a.pdf"],                      # optional filter
      },
      ...
    ]

The evaluator queries your live API for each question and collects the
answer + retrieved contexts automatically — no manual labelling needed
(except ground_truth, which is only used for context_recall).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

import requests
from loguru import logger
from datasets import Dataset

from ragas import evaluate as ragas_evaluate
from ragas.metrics import (
    faithfulness,
    answer_relevancy,
    context_precision,
    context_recall,
)
import google.generativeai as genai
from ragas.llms import LangchainLLMWrapper
from langchain_google_genai import ChatGoogleGenerativeAI

from config import settings


# ── Configuration ──────────────────────────────────────────────────────────────

API_BASE      = "http://localhost:8000/api/v1"
QUERY_TIMEOUT = 60   # seconds per API call
SLEEP_BETWEEN = 1.5  # seconds between requests to avoid rate-limiting


# ── Step 1: collect answers from live API ──────────────────────────────────────

def _query_api(question: str, sources: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    Send a question to POST /query and return the response dict.
    Raises on HTTP errors or timeouts.
    """
    payload: Dict[str, Any] = {"query": question}
    if sources:
        payload["sources"] = sources

    resp = requests.post(
        f"{API_BASE}/query",
        json=payload,
        timeout=QUERY_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def collect_responses(test_cases: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Run each test case through the live API and collect:
      - question
      - answer (from API)
      - contexts (the excerpts from citations)
      - ground_truth (from test case, if provided)
      - confidence
      - sources_searched

    This is the RAGAS "dataset" before it goes into the evaluator.
    """
    records = []

    for i, case in enumerate(test_cases, start=1):
        question     = case["question"]
        ground_truth = case.get("ground_truth", "")
        sources      = case.get("sources")

        logger.info(f"[{i}/{len(test_cases)}] Querying: '{question[:70]}…'")

        try:
            result = _query_api(question, sources)
        except requests.RequestException as exc:
            logger.error(f"  API call failed: {exc} — skipping")
            continue

        # Extract context excerpts from citations
        contexts = [c["excerpt"] for c in result.get("citations", [])]

        # RAGAS requires at least one context string
        if not contexts:
            logger.warning(f"  No citations returned — using answer as fallback context")
            contexts = [result["answer"]]

        records.append({
            "question":        question,
            "answer":          result["answer"],
            "contexts":        contexts,
            "ground_truth":    ground_truth,
            "confidence":      result.get("confidence", "UNKNOWN"),
            "sources_searched":result.get("sources_searched", []),
        })

        time.sleep(SLEEP_BETWEEN)

    logger.info(f"Collected {len(records)} records from {len(test_cases)} test cases")
    return records


# ── Step 2: run RAGAS metrics ──────────────────────────────────────────────────

def _build_ragas_dataset(records: List[Dict[str, Any]]) -> Dataset:
    """Convert records to a HuggingFace Dataset for RAGAS."""
    return Dataset.from_list([
        {
            "question":     r["question"],
            "answer":       r["answer"],
            "contexts":     r["contexts"],
            "ground_truth": r["ground_truth"],
        }
        for r in records
    ])


def _get_metrics(has_ground_truth: bool) -> list:
    """
    Return the metric list appropriate for the dataset.
    context_recall needs ground_truth labels; skip it if none are provided.
    """
    base = [faithfulness, answer_relevancy, context_precision]
    if has_ground_truth:
        base.append(context_recall)
    else:
        logger.info("No ground_truth labels found — skipping context_recall")
    return base


# ── Step 3: format and persist results ────────────────────────────────────────

def _format_report(
    ragas_result,
    records: List[Dict[str, Any]],
    elapsed: float,
) -> Dict[str, Any]:
    """Build the final JSON report dict."""
    scores = ragas_result.to_pandas().mean(numeric_only=True).to_dict()

    # Map float scores to letter grades for quick readability
    def grade(score: Optional[float]) -> str:
        if score is None:
            return "N/A"
        if score >= 0.85:
            return "A"
        if score >= 0.70:
            return "B"
        if score >= 0.55:
            return "C"
        return "D"

    summary = {
        metric: {"score": round(float(v), 4), "grade": grade(v)}
        for metric, v in scores.items()
    }

    # Confidence distribution across test set
    conf_counts: Dict[str, int] = {}
    for r in records:
        c = r.get("confidence", "UNKNOWN")
        conf_counts[c] = conf_counts.get(c, 0) + 1

    return {
        "summary":               summary,
        "confidence_distribution": conf_counts,
        "total_questions":       len(records),
        "elapsed_seconds":       round(elapsed, 1),
        "per_question":          records,
    }


# ── Public entry point ─────────────────────────────────────────────────────────

def evaluate(
    test_cases: List[Dict[str, Any]],
    out_path: str | Path = "eval_results.json",
) -> Dict[str, Any]:
    """
    Full evaluation run: collect API responses → RAGAS metrics → save report.

    Args:
        test_cases: list of {"question", "ground_truth"?, "sources"?} dicts
        out_path:   where to write the JSON report

    Returns:
        report dict (also written to out_path)

    Example:
        from evaluation.ragas_eval import evaluate

        test_cases = [
            {
                "question":     "What is the penalty for late payment?",
                "ground_truth": "A 2% monthly surcharge applies after 30 days.",
                "sources":      ["contract_a.pdf"],
            },
            {
                "question": "Who are the parties to this agreement?",
                "sources":  ["contract_a.pdf"],
            },
        ]
        report = evaluate(test_cases, out_path="eval_results.json")
        print(report["summary"])
    """
    if not test_cases:
        raise ValueError("test_cases must not be empty")

    logger.info(f"Starting RAGAS evaluation on {len(test_cases)} questions…")
    t0 = time.time()

    # ── 1. Collect live API responses ──────────────────────────────────────────
    records = collect_responses(test_cases)
    if not records:
        raise RuntimeError("No records collected — is the API running on localhost:8000?")

    # ── 2. Build RAGAS dataset ─────────────────────────────────────────────────
    dataset = _build_ragas_dataset(records)
    has_gt  = any(r["ground_truth"] for r in records)
    metrics = _get_metrics(has_gt)

    # ── 3. Configure RAGAS to use Gemini as the judge LLM ─────────────────────
    # RAGAS needs an LLM to evaluate faithfulness and answer_relevancy.
    # We use the same Gemini model to keep the stack consistent.
    judge_llm = LangchainLLMWrapper(
        ChatGoogleGenerativeAI(
            model          = "gemini-1.5-flash",
            google_api_key = settings.GEMINI_API_KEY,
            temperature    = 0.0,   # deterministic judgements
        )
    )

    logger.info(f"Running RAGAS metrics: {[m.name for m in metrics]}")
    ragas_result = ragas_evaluate(
        dataset    = dataset,
        metrics    = metrics,
        llm        = judge_llm,
        raise_exceptions = False,   # log failures, don't abort the whole run
    )

    # ── 4. Format and save report ──────────────────────────────────────────────
    elapsed = time.time() - t0
    report  = _format_report(ragas_result, records, elapsed)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # ── 5. Print summary table ─────────────────────────────────────────────────
    logger.success(f"\nEvaluation complete in {elapsed:.1f}s  →  {out_path}\n")
    print("\n" + "─" * 52)
    print(f"  {'Metric':<28}  {'Score':>6}  {'Grade':>5}")
    print("─" * 52)
    for metric, data in report["summary"].items():
        print(f"  {metric:<28}  {data['score']:>6.4f}  {data['grade']:>5}")
    print("─" * 52)
    print(f"  Questions evaluated: {report['total_questions']}")
    print(f"  Confidence distribution: {report['confidence_distribution']}")
    print("─" * 52 + "\n")

    return report