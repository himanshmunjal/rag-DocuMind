"""
scripts/diagnose_qualitative.py — inspect exactly what retrieve() and
evaluate_chunks() do for one question, to distinguish two very different
possible causes of an empty-citations / "not enough info" response:

  (a) retrieval found the WRONG chunks (e.g. wrong Item, weak semantic match)
      -> scores will be genuinely low, guard correctly filtering them out
  (b) a threshold/logic bug is dropping chunks that should have passed
      -> scores will look reasonable but still end up filtered to zero

Run: python3 -m scripts.diagnose_qualitative --cik 0000320193 \
        --question "What risks does Apple flag around supply chain?" \
        --item 1A
"""

import argparse

from config import settings
from core.retrieval.reranker import retrieve
from core.hallucination.guard import evaluate_chunks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cik", required=True)
    parser.add_argument("--question", required=True)
    parser.add_argument("--item", default=None, help="optional item_filter, e.g. 1A")
    args = parser.parse_args()

    print(f"CONFIDENCE_THRESHOLD = {settings.CONFIDENCE_THRESHOLD}")
    print(f"SIMILARITY_THRESHOLD = {settings.SIMILARITY_THRESHOLD}")
    print(f"(hard floor uses CONFIDENCE_THRESHOLD; per-chunk pass filter uses SIMILARITY_THRESHOLD)")

    chunks = retrieve(
        args.question,
        cik=args.cik,
        item_filter=args.item,
        top_k=settings.TOP_K,
        top_n=settings.RERANK_TOP_N,
    )

    print(f"\nretrieve() returned {len(chunks)} chunk(s):")
    for c in chunks:
        meta = c.get("metadata", {})
        preview = c.get("text", "")[:70].replace("\n", " ")
        print(
            f"  score={c.get('score', 0):.4f}  "
            f"item={meta.get('item', '?'):4s}  "
            f"item_title='{meta.get('item_title', '')[:25]}'  "
            f"\"{preview}...\""
        )

    if not chunks:
        print("\n⚠️  retrieve() itself returned nothing — problem is upstream of the "
              "guard entirely (embedding/search/item_filter mismatch).")
        return

    result = evaluate_chunks(chunks)
    print(f"\nevaluate_chunks() result:")
    print(f"  should_answer:   {result.should_answer}")
    print(f"  confidence:      {result.confidence}")
    print(f"  filtered_chunks: {len(result.filtered_chunks)}")
    print(f"  reason:          {result.reason!r}")

    max_score = max(c.get("score", 0.0) for c in chunks)
    avg_score = sum(c.get("score", 0.0) for c in chunks) / len(chunks)
    print(f"\n  computed max_score={max_score:.4f}, avg_score={avg_score:.4f}")

    if result.should_answer and not result.filtered_chunks:
        print("\n⚠️  BUG CONFIRMED: guard passed the hard floor (should_answer=True) "
              "but filtered_chunks is empty. This means max_score >= CONFIDENCE_THRESHOLD "
              "but every individual chunk scored below SIMILARITY_THRESHOLD — check "
              "whether SIMILARITY_THRESHOLD is actually HIGHER than CONFIDENCE_THRESHOLD "
              "in your current config.py (it should be lower or equal).")
    elif result.should_answer and result.filtered_chunks:
        print("\n✅ Guard passed with real chunks. If the LLM still refused, the chunks "
              "retrieved likely aren't actually about the question's topic — check the "
              "item/item_title values above; if none say 'Risk Factors' or similar, this "
              "is a retrieval relevance issue, not a guard bug.")


if __name__ == "__main__":
    main()