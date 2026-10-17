# """
# diagnose_msft_acquisitions_v2.py

# Two checks:
#   1. Does the vector store contain a Note 7 / Business Combinations chunk
#      for Microsoft at all, post re-ingestion? (direct collection query,
#      bypasses search/rerank entirely)
#   2. Fixed version of the rerank diagnostic — converts SearchResult objects
#      to dicts before passing to rerank(), which is what crashed last time.
# """

# from core.ingestion.indexer import get_collection
# from core.retrieval.searcher import search
# from core.retrieval.reranker import rerank

# CIK = "0000789019"
# ACCESSION = "0000950170-25-100235"
# QUERY = "What acquisitions or divestitures does Microsoft mention in the most recent filing?"

# # ── check 1: is Note 7 actually indexed? ────────────────────────────────
# print("=" * 70)
# print("CHECK 1 — direct collection scan for Note 7 content")
# print("=" * 70)

# collection = get_collection()
# all_docs = collection.get(
#     where={"$and": [{"cik": CIK}, {"item": "8"}]},
#     include=["documents", "metadatas"],
# )

# print(f"{len(all_docs['documents'])} chunk(s) indexed for Microsoft Item 8\n")

# for doc, meta in zip(all_docs["documents"], all_docs["metadatas"]):
#     flag = ""
#     if "business combination" in doc.lower():
#         flag += " <-- BUSINESS COMBINATION"
#     if "activision" in doc.lower():
#         flag += " <-- ACTIVISION"
#     print(f"chunk_idx={meta.get('chunk_index','?'):<5}{flag}")
#     print(f"   {doc[:150].replace(chr(10), ' ')}")

# # ── check 2: fixed rerank diagnostic ────────────────────────────────────
# print("\n" + "=" * 70)
# print("CHECK 2 — full rerank, all candidates (SearchResult -> dict fixed)")
# print("=" * 70)

# results = search(QUERY, cik=CIK, sources=[ACCESSION])  # adjust kwargs if your search() signature differs

# def to_dict(r):
#     if isinstance(r, dict):
#         return r
#     if hasattr(r, "to_dict"):
#         return r.to_dict()
#     # dataclass fallback
#     return {
#         "text": r.text,
#         "score": r.score,
#         "metadata": getattr(r, "metadata", {}),
#     }

# chunks = [to_dict(r) for r in results]

# reranked = rerank(QUERY, chunks, top_n=len(chunks))

# for i, r in enumerate(reranked, 1):
#     text = r["text"]
#     flag = ""
#     if "business combination" in text.lower():
#         flag += " <-- BUSINESS COMBINATION"
#     if "activision" in text.lower():
#         flag += " <-- ACTIVISION"
#     print(f"\n[{i}] rerank score={r['score']:.4f}  bi-encoder was={r['bi_encoder_score']:.4f}{flag}")
#     print(f"    {text[:180].replace(chr(10), ' ')}")
# from core.ingestion.indexer import get_collection
# collection = get_collection()
# collection.delete(where={"cik": "0000789019"})
# print("Microsoft vectors deleted:", collection.count())
from core.ingestion.indexer import get_collection
col = get_collection()

# does ANY stored chunk for Microsoft even mention acquisitions?
results = col.query(query_texts=['business combination acquisition divestiture'], n_results=10,
                     where={'cik': '0000789019'})  # adjust filter key to your actual metadata field name
for doc, meta, dist in zip(results['documents'][0], results['metadatas'][0], results['distances'][0]):
    print(dist, meta.get('item'), doc[:120])