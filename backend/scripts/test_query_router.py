"""
scripts/test_query_router.py — sanity-check classify()/extract_metric_request()/
suggest_item_filter() against example questions, with no DB or network needed.

Run: python3 -m scripts.test_query_router
"""

from core.ingestion.query_router import classify, extract_metric_request, suggest_item_filter

TEST_QUESTIONS = [
    "What was Apple's revenue in FY2024?",
    "What was Apple's revenue in fiscal year 2024?",
    "How did gross margin change from 2023 to 2024?",
    "How has revenue grown over the last year?",
    "Why did Apple's gross margin change?",
    "What risks does Apple flag around supply chain?",
    "Compare Apple's R&D spending in 2023 vs 2024",
    "What was net income in 2024?",
    "Explain Apple's business model",
    "What is Apple's stock price today?",   # expect: no metric match -> qualitative fallback
]

if __name__ == "__main__":
    for q in TEST_QUESTIONS:
        qtype = classify(q)
        print(f"\nQ: {q}")
        print(f"  → classify: {qtype}")
        if qtype in ("numeric", "computed"):
            print(f"  → extract_metric_request: {extract_metric_request(q)}")
        else:
            print(f"  → suggest_item_filter: {suggest_item_filter(q)}")