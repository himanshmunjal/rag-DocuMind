"""
scripts/diagnose_reranker_nan.py — isolate whether the cross-encoder model
itself is broken (NaN on everything) or something about the real filing
chunks specifically triggers it (NaN only on long/real text).

Run: python3 -m scripts.diagnose_reranker_nan
"""

from core.retrieval.reranker import _get_reranker, _sigmoid
from config import settings

print(f"RERANKER_MODEL = {settings.RERANKER_MODEL}")

model = _get_reranker()

print("\n--- Test 1: trivial short pair ---")
raw = model.predict([("What is the capital of France?", "Paris is the capital of France.")])
print(f"raw score: {raw}")
print(f"sigmoid:   {_sigmoid(float(raw[0]))}")

print("\n--- Test 2: trivial pair, unrelated ---")
raw = model.predict([("What is the capital of France?", "Bananas are yellow.")])
print(f"raw score: {raw}")
print(f"sigmoid:   {_sigmoid(float(raw[0]))}")

print("\n--- Test 3: realistic-length filing-style text ---")
long_text = (
    "The technology industry, including, in some instances, the Company, is "
    "subject to various legal proceedings and claims that arise in the ordinary "
    "course of business, and certain additional legal proceedings and claims "
    "not in the ordinary course of business. " * 5
)
raw = model.predict([("What risks does Apple flag around supply chain?", long_text)])
print(f"raw score: {raw}")
print(f"sigmoid:   {_sigmoid(float(raw[0]))}")

print("\n--- Test 4: batch of 3 at once (matches real usage) ---")
pairs = [
    ("What risks does Apple flag around supply chain?", long_text),
    ("What risks does Apple flag around supply chain?", "Something about litigation and commercial relationships."),
    ("What risks does Apple flag around supply chain?", "Regardless of the merit of particular claims, defending against litigation."),
]
raw = model.predict(pairs, show_progress_bar=False)
print(f"raw scores: {raw}")
print(f"any NaN: {any(x != x for x in raw)}")   # x != x is True only for NaN