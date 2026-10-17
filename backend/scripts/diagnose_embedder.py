"""
scripts/diagnose_embedder.py — verify the current embedding model works
cleanly, and test alternatives in case it needs swapping too.

Run: python3 -m scripts.diagnose_embedder
"""

from sentence_transformers import SentenceTransformer
from config import settings

print(f"Current EMBEDDING_MODEL = {settings.EMBEDDING_MODEL}")

CANDIDATES = [
    settings.EMBEDDING_MODEL,                     # your current one — confirm it's actually fine
    "sentence-transformers/all-MiniLM-L12-v2",     # slightly larger sibling, if you need a fallback
    "BAAI/bge-small-en-v1.5",                      # different family entirely, if all-MiniLM needs replacing
]

for name in CANDIDATES:
    print(f"\n--- Testing: {name} ---")
    try:
        model = SentenceTransformer(name)
        vectors = model.encode(
            ["This is a test sentence.", "Apple reported revenue growth."],
            normalize_embeddings=True,
        )
        print(f"  OK — dim={model.get_sentence_embedding_dimension()}, "
              f"sample vector[0][:5]={vectors[0][:5]}")
    except Exception as exc:
        print(f"  Python-level exception (not a crash): {exc}")
    # if this crashes with a bus error, whichever name printed last without
    # "OK" after it is the one that failed — comment it out and re-run to
    # keep testing the rest.