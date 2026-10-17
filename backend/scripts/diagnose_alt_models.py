"""
scripts/diagnose_alt_models.py — test whether the crash is specific to
cross-encoder/ms-marco-MiniLM-L6-v2, or happens with ANY
AutoModelForSequenceClassification model (which would mean swapping models
won't help — the problem would be with that model class/head generally).

Run: python3 -m scripts.diagnose_alt_models
"""

import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

CANDIDATES = [
    "prajjwal1/bert-tiny",                    # tiny, ~4MB, fastest possible sanity check
    "cross-encoder/ms-marco-MiniLM-L-12-v2",  # bigger sibling of the crashing model
    "cross-encoder/stsb-distilroberta-base",  # different architecture family entirely
]

for name in CANDIDATES:
    print(f"\n--- Testing: {name} ---")
    try:
        tok = AutoTokenizer.from_pretrained(name)
        model = AutoModelForSequenceClassification.from_pretrained(name)
        model.eval()
        encoded = tok("hello world", "paris is in france", return_tensors="pt", truncation=True)
        with torch.no_grad():
            output = model(**encoded)
        print(f"  OK — logits: {output.logits}")
    except Exception as exc:
        print(f"  Python-level exception (not a crash): {exc}")
    # NOTE: if this crashes with a bus error, the script dies here with no
    # further output — whichever model name was printed last (without "OK")
    # is the one that crashed. Comment out earlier candidates and re-run to
    # keep testing the remaining ones once you know which are safe.