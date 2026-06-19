# """
# Wraps the Gemini text-embedding-004 API with:
#   - Task-type separation: RETRIEVAL_DOCUMENT for indexing, RETRIEVAL_QUERY for queries.
#     This is not cosmetic — Gemini optimises the embedding space differently per task type,
#     so mixing them degrades retrieval quality.
#   - Batching: Gemini allows up to 100 texts per batch call, dramatically cutting
#     API round-trips on large PDFs.
#   - Retry with exponential backoff: embedding jobs are long-running; transient
#     failures shouldn't abort an entire ingestion.
#   - Dimensionality: text-embedding-004 outputs 768-dim vectors by default.
#     Exposed as EMBEDDING_DIM so ChromaDB and the indexer stay in sync.
# """

# import time
# import google.generativeai as genai
# from typing import List
# from loguru import logger

# from config import settings

# # ── configuration ──────────────────────────────────────────────────────────────

# genai.configure(api_key=settings.GEMINI_API_KEY)

# EMBEDDING_DIM: int = 768        # text-embedding-004 native dimension
# BATCH_SIZE: int = 100           # Gemini max texts per embed_content call
# MAX_RETRIES: int = 3
# RETRY_BASE_DELAY: float = 2.0   # seconds; doubles on each retry


# # ── internal helpers ───────────────────────────────────────────────────────────

# def _embed_batch(texts: List[str], task_type: str) -> List[List[float]]:
#     """
#     Call the Gemini batch embedding endpoint for a single batch.
#     Retries up to MAX_RETRIES times on transient errors.

#     Args:
#         texts:      list of strings, len ≤ BATCH_SIZE
#         task_type:  "RETRIEVAL_DOCUMENT" | "RETRIEVAL_QUERY"

#     Returns:
#         list of 768-dim float vectors, same order as input texts
#     """
#     for attempt in range(1, MAX_RETRIES + 1):
#         try:
#             result = genai.embed_content(
#                 model=settings.EMBEDDING_MODEL,
#                 content=texts,
#                 task_type=task_type,
#             )
#             # result.embedding is a list[list[float]] when content is a list
#             return result["embedding"]

#         except Exception as exc:
#             if attempt == MAX_RETRIES:
#                 logger.error(f"Embedding failed after {MAX_RETRIES} attempts: {exc}")
#                 raise
#             delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
#             logger.warning(
#                 f"Embedding attempt {attempt}/{MAX_RETRIES} failed ({exc}). "
#                 f"Retrying in {delay:.1f}s…"
#             )
#             time.sleep(delay)

#     # unreachable, but satisfies type checkers
#     raise RuntimeError("_embed_batch: exhausted retries")


# def _batched(items: list, size: int):
#     """Yield successive slices of `items` of length ≤ `size`."""
#     for i in range(0, len(items), size):
#         yield items[i : i + size]


# # ── public API ─────────────────────────────────────────────────────────────────

# def embed_chunks(chunks: List[dict]) -> List[dict]:
#     """
#     Embed a list of chunk dicts (output of chunker.chunk_pages).
#     Adds an "embedding" key to each chunk in-place and returns the list.

#     Uses task_type RETRIEVAL_DOCUMENT — correct for content being indexed.
#     Do NOT use this for query strings; use embed_query() instead.

#     Args:
#         chunks: list of {"text": str, "metadata": dict}

#     Returns:
#         same list with "embedding": List[float] added to each item

#     Example:
#         chunks = chunk_pages(pages)
#         chunks = embed_chunks(chunks)
#         # chunks[0]["embedding"] → [0.021, -0.003, ...]  (768 floats)
#     """
#     if not chunks:
#         logger.warning("embed_chunks called with empty list — nothing to do")
#         return chunks

#     texts = [c["text"] for c in chunks]
#     total = len(texts)
#     all_embeddings: List[List[float]] = []

#     logger.info(f"Embedding {total} chunks in batches of {BATCH_SIZE}…")

#     for batch_num, batch_texts in enumerate(_batched(texts, BATCH_SIZE), start=1):
#         logger.debug(
#             f"Batch {batch_num}: embedding {len(batch_texts)} texts "
#             f"({(batch_num - 1) * BATCH_SIZE + 1}–"
#             f"{min(batch_num * BATCH_SIZE, total)} of {total})"
#         )
#         batch_vectors = _embed_batch(batch_texts, task_type="RETRIEVAL_DOCUMENT")
#         all_embeddings.extend(batch_vectors)

#     # Attach vectors back to chunk dicts
#     for chunk, vector in zip(chunks, all_embeddings):
#         chunk["embedding"] = vector

#     logger.success(f"Embedded {total} chunks ({EMBEDDING_DIM}-dim each)")
#     return chunks


# def embed_query(query: str) -> List[float]:
#     """
#     Embed a single user query string.
#     Uses task_type RETRIEVAL_QUERY — asymmetric to the document embeddings,
#     which is exactly what Gemini text-embedding-004 is optimised for.

#     Args:
#         query: raw user question string

#     Returns:
#         768-dim float vector

#     Example:
#         vec = embed_query("What is the termination clause?")
#         # use vec to search ChromaDB
#     """
#     if not query.strip():
#         raise ValueError("embed_query: query string is empty")

#     logger.debug(f"Embedding query ({len(query)} chars): '{query[:80]}…'")

#     result = genai.embed_content(
#         model=settings.EMBEDDING_MODEL,
#         content=query,
#         task_type="RETRIEVAL_QUERY",
#     )
#     # Single string input → result.embedding is a single list[float]
#     return result["embedding"]

"""
Embedder — local transformer-based, no external API.

Model choice: BAAI/bge-base-en-v1.5  (default)
  - 768-dim, Apache 2.0 licence, ~440 MB on disk
  - Consistently top-5 on the MTEB retrieval benchmark
  - Supports BGE's query prefix trick for asymmetric retrieval
    (the equivalent of Gemini's RETRIEVAL_QUERY / RETRIEVAL_DOCUMENT)
  - Runs on CPU fine for development; GPU used automatically if available

Other drop-in options (change EMBEDDING_MODEL in config / .env):
  Model name                          Dim   Notes
  BAAI/bge-small-en-v1.5             384   Fastest, smallest RAM, ~130 MB
  BAAI/bge-large-en-v1.5            1024   Best quality, ~1.3 GB
  sentence-transformers/all-MiniLM-L6-v2  384   Familiar default, symmetric only
  intfloat/e5-base-v2                768   Strong alternative to BGE
  thenlper/gte-base                  768   Good for long documents

Asymmetric retrieval (query ≠ document):
  BGE and E5 models use a lightweight prefix trick instead of a separate
  API parameter:
    documents → prepend  "passage: "   (or nothing for BGE, it ignores it)
    queries   → prepend  "Represent this sentence for searching relevant passages: "
  This is handled internally — callers just use embed_chunks() / embed_query().

Architecture:
  - Model loaded once at import time (singleton) so FastAPI workers don't
    reload weights on every request.
  - mean pooling over the last hidden state, then L2 normalisation —
    standard practice; produces unit vectors so cosine sim == dot product,
    which is what ChromaDB's hnsw:cosine space expects.
  - Batched inference with configurable BATCH_SIZE; tqdm progress bar for
    long ingestion jobs.
  - Device auto-detection: CUDA → MPS (Apple Silicon) → CPU.
"""

from __future__ import annotations

import os
from typing import List

import torch
import torch.nn.functional as F
from transformers import AutoTokenizer, AutoModel
from loguru import logger

from config import settings


# ── device selection ──────────────────────────────────────────────────────────

def _best_device() -> str:
    if torch.cuda.is_available():
        device = "cuda"
    elif torch.backends.mps.is_available():
        device = "mps"
    else:
        device = "cpu"
    logger.info(f"Embedder using device: {device}")
    return device


# ── model singleton ───────────────────────────────────────────────────────────

_tokenizer: AutoTokenizer | None = None
_model: AutoModel | None = None
_device: str | None = None

# Exported so ChromaDB collection and indexer can read the true dimension
# without loading the model. Overwritten after first load.
EMBEDDING_DIM: int = 768


def _load_model() -> tuple[AutoTokenizer, AutoModel, str]:
    """
    Load tokenizer + model once; cache in module globals.
    Thread-safe for read after first load (GIL protects the assignment).
    """
    global _tokenizer, _model, _device, EMBEDDING_DIM

    if _model is not None:
        return _tokenizer, _model, _device

    model_name = settings.EMBEDDING_MODEL
    logger.info(f"Loading embedding model '{model_name}' …")

    _tokenizer = AutoTokenizer.from_pretrained(model_name)
    _model = AutoModel.from_pretrained(model_name)
    _device = _best_device()
    _model = _model.to(_device)
    _model.eval()  # disable dropout; we never train here

    # Infer true output dimension from a dummy forward pass
    with torch.no_grad():
        dummy = _tokenizer("hello", return_tensors="pt").to(_device)
        out = _model(**dummy)
        EMBEDDING_DIM = out.last_hidden_state.shape[-1]

    logger.success(
        f"Model ready — dim={EMBEDDING_DIM}, "
        f"max_seq={_tokenizer.model_max_length} tokens, device={_device}"
    )
    return _tokenizer, _model, _device


# ── pooling + normalisation ───────────────────────────────────────────────────

def _mean_pool(last_hidden_state: torch.Tensor, attention_mask: torch.Tensor) -> torch.Tensor:
    """
    Weighted mean pool: average token embeddings, ignoring padding tokens.
    Standard for sentence-transformers and BGE models.
    """
    # expand mask to match hidden state shape
    mask_expanded = attention_mask.unsqueeze(-1).expand(last_hidden_state.size()).float()
    sum_embeddings = torch.sum(last_hidden_state * mask_expanded, dim=1)
    sum_mask = torch.clamp(mask_expanded.sum(dim=1), min=1e-9)
    return sum_embeddings / sum_mask


def _encode(texts: List[str]) -> List[List[float]]:
    """
    Tokenise, forward-pass, pool, L2-normalise, return as plain Python lists.
    Handles the full batch in one call — caller is responsible for
    splitting into sub-batches that fit in memory.
    """
    tokenizer, model, device = _load_model()

    encoded = tokenizer(
        texts,
        padding=True,
        truncation=True,
        max_length=tokenizer.model_max_length,
        return_tensors="pt",
    ).to(device)

    with torch.no_grad():
        output = model(**encoded)

    pooled = _mean_pool(output.last_hidden_state, encoded["attention_mask"])
    normalised = F.normalize(pooled, p=2, dim=1)  # unit vectors → cosine == dot

    return normalised.cpu().tolist()


# ── batch helper ──────────────────────────────────────────────────────────────

def _batched(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ── query prefix (asymmetric retrieval) ───────────────────────────────────────

# BGE models improve retrieval when queries carry this prefix at inference time.
# Documents are indexed without any prefix (that's the asymmetry).
# For non-BGE models this prefix is harmless — the model treats it as context.
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

def _should_use_prefix() -> bool:
    return "bge" in settings.EMBEDDING_MODEL.lower()


# ── public API ────────────────────────────────────────────────────────────────

def embed_chunks(chunks: List[dict]) -> List[dict]:
    """
    Embed a list of chunk dicts produced by chunker.chunk_pages().
    Adds an "embedding" key (List[float]) to each chunk in-place.

    Documents are embedded WITHOUT the query prefix — correct for indexing.

    Args:
        chunks: [{"text": str, "metadata": dict}, ...]

    Returns:
        Same list with "embedding" added to every item.

    Example:
        chunks = chunk_pages(pages)
        chunks = embed_chunks(chunks)
        # chunks[0]["embedding"]  →  [0.031, -0.014, ...]  (EMBEDDING_DIM floats)
    """
    if not chunks:
        logger.warning("embed_chunks: empty list — nothing to do")
        return chunks

    texts = [c["text"] for c in chunks]
    total = len(texts)
    all_vectors: List[List[float]] = []

    logger.info(f"Embedding {total} chunks (batch_size={settings.EMBEDDING_BATCH_SIZE}) …")

    for batch_num, batch_texts in enumerate(_batched(texts, settings.EMBEDDING_BATCH_SIZE), start=1):
        start = (batch_num - 1) * settings.EMBEDDING_BATCH_SIZE + 1
        end = min(batch_num * settings.EMBEDDING_BATCH_SIZE, total)
        logger.debug(f"  batch {batch_num}: chunks {start}–{end}")
        all_vectors.extend(_encode(batch_texts))

    for chunk, vector in zip(chunks, all_vectors):
        chunk["embedding"] = vector

    logger.success(f"Embedded {total} chunks ({EMBEDDING_DIM}-dim, L2-normalised)")
    return chunks


def embed_query(query: str) -> List[float]:
    """
    Embed a single user query string for retrieval.

    For BGE models the query prefix is prepended automatically, producing
    vectors in the same space as — but directionally tuned toward — the
    document embeddings stored in ChromaDB.

    Args:
        query: raw user question string

    Returns:
        List[float] of length EMBEDDING_DIM, L2-normalised.

    Example:
        vec = embed_query("What is the termination clause?")
        # pass vec directly to ChromaDB query_embeddings
    """
    if not query.strip():
        raise ValueError("embed_query: query string is empty")

    text = (_BGE_QUERY_PREFIX + query) if _should_use_prefix() else query
    logger.debug(f"Embedding query ({len(query)} chars)")

    vectors = _encode([text])
    return vectors[0]