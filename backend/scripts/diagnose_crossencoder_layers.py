"""
scripts/diagnose_crossencoder_layers.py — isolate exactly which layer crashes:
tokenizer alone, raw transformers model forward pass, or the sentence-transformers
CrossEncoder wrapper specifically. A bus error kills the whole process, so
whichever "Test X OK" line is the LAST one printed tells you where it died —
you won't get a clean traceback for a bus error, just silence after that point.
"""

import os
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")

MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L6-v2"

print("--- Test A: tokenizer only, no model forward pass ---")
from transformers import AutoTokenizer
tok = AutoTokenizer.from_pretrained(MODEL_NAME)
encoded = tok("hello world", "paris is in france", return_tensors="pt", truncation=True)
print(f"Test A OK — input_ids shape: {encoded['input_ids'].shape}")

print("\n--- Test B: raw transformers model forward pass (bypasses sentence-transformers) ---")
import torch
from transformers import AutoModelForSequenceClassification
model = AutoModelForSequenceClassification.from_pretrained(MODEL_NAME, attn_implementation="eager")
model.eval()
with torch.no_grad():
    output = model(**encoded)
print(f"Test B OK — raw model forward pass logits: {output.logits}")

print("\n--- Test C: sentence-transformers CrossEncoder wrapper (the actual code path) ---")
from sentence_transformers import CrossEncoder
ce = CrossEncoder(MODEL_NAME, max_length=512, device="cpu")
score = ce.predict([("hello world", "paris is in france")])
print(f"Test C OK — CrossEncoder.predict() score: {score}")