# """
# Owns the ChromaDB collection and all read/write operations against it.
# Sits at the end of the ingestion pipeline:

#     pdf_loader → chunker → embedder → indexer → ChromaDB

# Responsibilities:
#   - One ChromaDB collection per "document set" (default: "documind")
#   - Upsert semantics: re-uploading a PDF replaces its old chunks cleanly
#     without duplicates — uses chunk IDs derived from (source, chunk_index).
#   - Metadata stored alongside every vector so retrievers can return
#     citations (source, page, chunk_index, chunk_type) without a second lookup.
#   - document_exists() check so the API layer can skip re-ingestion.
#   - delete_document() for the document management UI on Day 13.
#   - list_documents() returns document-level summaries for the sidebar.
# """

# import hashlib
# from typing import List, Dict, Any, Optional
# from loguru import logger
# import chromadb
# from chromadb.config import Settings as ChromaSettings

# from config import settings as app_settings
# from core.ingestion.embedder import EMBEDDING_DIM


# # ── ChromaDB client (module-level singleton) ───────────────────────────────────

# client: Optional[chromadb.ClientAPI] = None
# collection: Optional[chromadb.Collection] = None

# COLLECTION_NAME = "documind"


# def get_collection() -> chromadb.Collection:
#     """
#     Lazily initialise the ChromaDB persistent client and collection.
#     Called on first use; subsequent calls return the cached object.
#     """
#     global client, collection

#     if collection is not None:
#         return collection

#     logger.info(f"Initialising ChromaDB at {app_settings.CHROMA_DIR}")
#     client = chromadb.PersistentClient(
#         path=str(app_settings.CHROMA_DIR),
#         settings=ChromaSettings(anonymized_telemetry=False),
#     )

#     collection = client.get_or_create_collection(
#         name=COLLECTION_NAME,
#         # cosine distance works better than L2 for text embeddings
#         metadata={"hnsw:space": "cosine"},
#     )

#     count = collection.count()
#     logger.success(
#         f"Collection '{COLLECTION_NAME}' ready — {count} vectors already stored"
#     )
#     return collection


# # ── chunk ID helpers ───────────────────────────────────────────────────────────

# def _make_chunk_id(source: str, chunk_index: int) -> str:
#     """
#     Deterministic, stable chunk ID.
#     Same (source, chunk_index) always produces the same ID → safe to upsert.
#     Using a short hash keeps IDs compact while avoiding collisions across
#     documents that share filenames on different upload sessions.
#     """
#     raw = f"{source}::{chunk_index}"
#     digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
#     return f"{digest}"


# # ── public API ─────────────────────────────────────────────────────────────────

# def index_chunks(chunks: List[Dict[str, Any]]) -> int:
#     """
#     Upsert a list of embedded chunks into ChromaDB.
#     Chunks must already have an "embedding" key (output of embedder.embed_chunks).

#     Uses upsert (not add) so re-uploading a document replaces stale vectors
#     without leaving orphaned duplicates.

#     Args:
#         chunks: list of {"text", "metadata", "embedding"} dicts

#     Returns:
#         number of chunks upserted

#     Raises:
#         ValueError: if any chunk is missing its embedding
#     """
#     if not chunks:
#         logger.warning("index_chunks called with empty list — nothing to index")
#         return 0

#     # Validate embeddings are present
#     missing = [i for i, c in enumerate(chunks) if "embedding" not in c]
#     if missing:
#         raise ValueError(
#             f"index_chunks: chunks at indices {missing} are missing embeddings. "
#             "Run embed_chunks() before index_chunks()."
#         )

#     collection = get_collection()

#     ids: List[str] = []
#     embeddings: List[List[float]] = []
#     documents: List[str] = []
#     metadatas: List[Dict[str, Any]] = []

#     for chunk in chunks:
#         meta = chunk["metadata"]
#         chunk_id = _make_chunk_id(meta["source"], meta["chunk_index"])

#         ids.append(chunk_id)
#         embeddings.append(chunk["embedding"])
#         documents.append(chunk["text"])

#         # ChromaDB metadata values must be str | int | float | bool — no Path objects
#         metadatas.append({
#             "source":       str(meta.get("source", "")),
#             "page":         int(meta.get("page", 0)),
#             "total_pages":  int(meta.get("total_pages", 0)),
#             "chunk_index":  int(meta.get("chunk_index", 0)),
#             "chunk_type":   str(meta.get("chunk_type", "unknown")),
#             "file_path":    str(meta.get("file_path", "")),
#             "char_count":   int(meta.get("char_count", len(chunk["text"]))),
#         })

#     logger.info(f"Upserting {len(ids)} chunks into ChromaDB…")
#     collection.upsert(
#         ids=ids,
#         embeddings=embeddings,
#         documents=documents,
#         metadatas=metadatas,
#     )

#     logger.success(
#         f"Indexed {len(ids)} chunks from "
#         f"'{chunks[0]['metadata'].get('source', 'unknown')}' "
#         f"(collection now has {collection.count()} total vectors)"
#     )
#     return len(ids)


# def document_exists(source: str) -> bool:
#     """
#     Return True if any chunks from `source` are already in the collection.
#     Used by the API layer to skip re-ingestion if the same file is re-uploaded.

#     Args:
#         source: filename, e.g. "contract_a.pdf"
#     """
#     collection = get_collection()
#     results = collection.get(
#         where={"source": source},
#         limit=1,
#         include=[],   # we only need to know if results exist
#     )
#     exists = len(results["ids"]) > 0
#     logger.debug(f"document_exists('{source}') → {exists}")
#     return exists


# def delete_document(source: str) -> int:
#     """
#     Remove all chunks belonging to `source` from the collection.
#     Called when the user deletes a document from the UI (Day 13).

#     Args:
#         source: filename, e.g. "contract_a.pdf"

#     Returns:
#         number of chunks deleted
#     """
#     collection = get_collection()

#     # Get IDs first so we know what to delete and can report the count
#     results = collection.get(
#         where={"source": source},
#         include=[],
#     )
#     ids_to_delete = results["ids"]

#     if not ids_to_delete:
#         logger.warning(f"delete_document: no chunks found for '{source}'")
#         return 0

#     collection.delete(ids=ids_to_delete)
#     logger.success(f"Deleted {len(ids_to_delete)} chunks for '{source}'")
#     return len(ids_to_delete)


# def list_documents() -> List[Dict[str, Any]]:
#     """
#     Return one summary record per unique source document currently indexed.
#     Used to populate the document sidebar in the UI.

#     Returns:
#         [
#           {
#             "source": "contract_a.pdf",
#             "total_pages": 12,
#             "chunk_count": 47,
#           },
#           ...
#         ]
#     """
#     collection = get_collection()

#     if collection.count() == 0:
#         return []

#     # Fetch all metadatas (no embeddings or documents needed)
#     results = collection.get(include=["metadatas"])
#     metadatas = results.get("metadatas", [])

#     # Aggregate by source
#     doc_map: Dict[str, Dict[str, Any]] = {}
#     for meta in metadatas:
#         src = meta.get("source", "unknown")
#         if src not in doc_map:
#             doc_map[src] = {
#                 "source": src,
#                 "total_pages": meta.get("total_pages", 0),
#                 "chunk_count": 0,
#             }
#         doc_map[src]["chunk_count"] += 1

#     documents = sorted(doc_map.values(), key=lambda d: d["source"])
#     logger.debug(f"list_documents: {len(documents)} unique documents")
#     return documents


# def get_collection_stats() -> Dict[str, Any]:
#     """
#     Quick health-check summary for the /status endpoint or debugging.

#     Returns:
#         {"collection": str, "total_vectors": int, "document_count": int}
#     """
#     collection = get_collection()
#     docs = list_documents()
#     return {
#         "collection": COLLECTION_NAME,
#         "total_vectors": collection.count(),
#         "document_count": len(docs),
#     }

# def delete_existing_chunks(cik: str, accession_number: str) -> int:
#     """
#     Delete any previously-indexed chunks for this exact filing, so re-ingestion
#     doesn't create duplicates. Returns the number of chunks deleted (best-effort —
#     some Chroma versions don't return a count from delete(), so this may just
#     report the pre-delete match count).
 
#     Note: Chroma's `where` filter matches against metadata as STORED in the
#     collection (flat key-value pairs), regardless of how nested your chunk
#     dicts were before indexing — so "cik" and "accession_number" here refer
#     to the flat metadata keys inside chunk["metadata"], which is what actually
#     gets passed to collection.add(metadatas=...) below.
#     """
#     collection = get_collection()
 
#     try:
#         existing = collection.get(
#             where={
#                 "$and": [
#                     {"cik": cik},
#                     {"accession_number": accession_number},
#                 ]
#             },
#             include=[],  # ids are always returned; don't need metadatas/documents here
#         )
#         existing_ids = existing.get("ids", [])
#     except Exception as exc:
#         logger.warning(f"Could not check existing chunks for {cik}/{accession_number}: {exc}")
#         return 0
 
#     if not existing_ids:
#         return 0
 
#     collection.delete(ids=existing_ids)
#     logger.info(
#         f"Deleted {len(existing_ids)} existing chunk(s) for {cik}/{accession_number} "
#         f"before re-indexing"
#     )
#     return len(existing_ids)
 
 
# def index_chunks(embedded_chunks: list[dict]) -> None:
#     """
#     Index a batch of embedded chunks, replacing any prior chunks for the same
#     filing(s) in the batch first.
 
#     Confirmed chunk shape (from filing_chunker.py output):
#         {"text": ..., "metadata": {"cik": ..., "accession_number": ..., "item": ..., ...}}
#     Assumes embed_chunks() adds an "embedding" key at the top level (alongside
#     "text" and "metadata"), and each chunk also has a stable "id" — check your
#     embedder.py output shape and adjust the .add() call below if "id" lives
#     somewhere else (e.g. inside metadata, or generated here instead).
#     """
#     if not embedded_chunks:
#         return
 
#     seen_filings = {
#         (c["metadata"]["cik"], c["metadata"]["accession_number"]) for c in embedded_chunks
#     }
#     for cik, accession_number in seen_filings:
#         delete_existing_chunks(cik, accession_number)
 
#     collection = get_collection()
#     collection.add(
#         ids=[c["id"] for c in embedded_chunks],
#         embeddings=[c["embedding"] for c in embedded_chunks],
#         documents=[c["text"] for c in embedded_chunks],
#         metadatas=[c["metadata"] for c in embedded_chunks],
#     )
#     logger.success(f"Indexed {len(embedded_chunks)} chunk(s) across {len(seen_filings)} filing(s)")


"""
Owns the ChromaDB collection and all read/write operations against it.
Sits at the end of the ingestion pipeline:

    pdf_loader → chunker → embedder → indexer → ChromaDB

Responsibilities:
  - One ChromaDB collection per "document set" (default: "documind")
  - Upsert semantics: re-uploading a PDF replaces its old chunks cleanly
    without duplicates — uses chunk IDs derived from (source, chunk_index).
  - Metadata stored alongside every vector so retrievers can return
    citations (source, page, chunk_index, chunk_type) without a second lookup.
  - document_exists() check so the API layer can skip re-ingestion.
  - delete_document() for the document management UI on Day 13.
  - list_documents() returns document-level summaries for the sidebar.
"""

import hashlib
from typing import List, Dict, Any, Optional
from loguru import logger
import chromadb
from chromadb.config import Settings as ChromaSettings

from config import settings as app_settings
from core.ingestion.embedder import EMBEDDING_DIM


# ── ChromaDB client (module-level singleton) ───────────────────────────────────

client: Optional[chromadb.ClientAPI] = None
collection: Optional[chromadb.Collection] = None

COLLECTION_NAME = "documind"


def get_collection() -> chromadb.Collection:
    """
    Lazily initialise the ChromaDB persistent client and collection.
    Called on first use; subsequent calls return the cached object.
    """
    global client, collection

    if collection is not None:
        return collection

    logger.info(f"Initialising ChromaDB at {app_settings.CHROMA_DIR}")
    client = chromadb.PersistentClient(
        path=str(app_settings.CHROMA_DIR),
        settings=ChromaSettings(anonymized_telemetry=False),
    )

    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        # cosine distance works better than L2 for text embeddings
        metadata={"hnsw:space": "cosine"},
    )

    count = collection.count()
    logger.success(
        f"Collection '{COLLECTION_NAME}' ready — {count} vectors already stored"
    )
    return collection


# ── chunk ID helpers ───────────────────────────────────────────────────────────

def _make_chunk_id(source: str, chunk_index: int) -> str:
    """
    Deterministic, stable chunk ID.
    Same (source, chunk_index) always produces the same ID → safe to upsert.
    Using a short hash keeps IDs compact while avoiding collisions across
    documents that share filenames on different upload sessions.
    """
    raw = f"{source}::{chunk_index}"
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{digest}"


# ── public API ─────────────────────────────────────────────────────────────────

def index_chunks(chunks: List[Dict[str, Any]]) -> int:
    """
    Upsert a list of embedded chunks into ChromaDB.
    Chunks must already have an "embedding" key (output of embedder.embed_chunks).

    Uses upsert (not add) so re-uploading a document replaces stale vectors
    without leaving orphaned duplicates.

    Args:
        chunks: list of {"text", "metadata", "embedding"} dicts

    Returns:
        number of chunks upserted

    Raises:
        ValueError: if any chunk is missing its embedding
    """
    if not chunks:
        logger.warning("index_chunks called with empty list — nothing to index")
        return 0

    # Validate embeddings are present
    missing = [i for i, c in enumerate(chunks) if "embedding" not in c]
    if missing:
        raise ValueError(
            f"index_chunks: chunks at indices {missing} are missing embeddings. "
            "Run embed_chunks() before index_chunks()."
        )

    collection = get_collection()

    ids: List[str] = []
    embeddings: List[List[float]] = []
    documents: List[str] = []
    metadatas: List[Dict[str, Any]] = []

    for chunk in chunks:
        meta = chunk["metadata"]
        chunk_id = _make_chunk_id(meta["source"], meta["chunk_index"])

        ids.append(chunk_id)
        embeddings.append(chunk["embedding"])
        documents.append(chunk["text"])

        # ChromaDB metadata values must be str | int | float | bool — no Path objects.
        # Includes BOTH the original generic-PDF fields (page, total_pages, file_path —
        # kept for backward compat / the OCR fallback path) AND the filing-specific
        # fields filing_chunker.py actually populates (cik, company_name, form_type,
        # filing_date, report_date, item, item_title). The old version only kept the
        # first group and silently dropped everything filing-related — that's the bug
        # that made every chunk show up with a missing cik.
        metadatas.append({
            "source":       str(meta.get("source", "")),
            "page":         int(meta.get("page", 0)),
            "total_pages":  int(meta.get("total_pages", 0)),
            "chunk_index":  int(meta.get("chunk_index", 0)),
            "chunk_type":   str(meta.get("chunk_type", "unknown")),
            "file_path":    str(meta.get("file_path", "")),
            "char_count":   int(meta.get("char_count", len(chunk["text"]))),
            # filing-specific fields:
            "cik":          str(meta.get("cik", "")),
            "company_name": str(meta.get("company_name", "")),
            "form_type":    str(meta.get("form_type", "")),
            "filing_date":  str(meta.get("filing_date", "")),
            "report_date":  str(meta.get("report_date", "")),
            "item":         str(meta.get("item", "")),
            "item_title":   str(meta.get("item_title", "")),
        })

    logger.info(f"Upserting {len(ids)} chunks into ChromaDB…")
    collection.upsert(
        ids=ids,
        embeddings=embeddings,
        documents=documents,
        metadatas=metadatas,
    )

    logger.success(
        f"Indexed {len(ids)} chunks from "
        f"'{chunks[0]['metadata'].get('source', 'unknown')}' "
        f"(collection now has {collection.count()} total vectors)"
    )
    return len(ids)


def document_exists(source: str) -> bool:
    """
    Return True if any chunks from `source` are already in the collection.
    Used by the API layer to skip re-ingestion if the same file is re-uploaded.

    Args:
        source: filename, e.g. "contract_a.pdf"
    """
    collection = get_collection()
    results = collection.get(
        where={"source": source},
        limit=1,
        include=[],   # we only need to know if results exist
    )
    exists = len(results["ids"]) > 0
    logger.debug(f"document_exists('{source}') → {exists}")
    return exists


def delete_document(source: str) -> int:
    """
    Remove all chunks belonging to `source` from the collection.
    Called when the user deletes a document from the UI (Day 13).

    Args:
        source: filename, e.g. "contract_a.pdf"

    Returns:
        number of chunks deleted
    """
    collection = get_collection()

    # Get IDs first so we know what to delete and can report the count
    results = collection.get(
        where={"source": source},
        include=[],
    )
    ids_to_delete = results["ids"]

    if not ids_to_delete:
        logger.warning(f"delete_document: no chunks found for '{source}'")
        return 0

    collection.delete(ids=ids_to_delete)
    logger.success(f"Deleted {len(ids_to_delete)} chunks for '{source}'")
    return len(ids_to_delete)


def list_documents() -> List[Dict[str, Any]]:
    """
    Return one summary record per unique source document currently indexed.
    Used to populate the document sidebar in the UI.

    Returns:
        [
          {
            "source": "contract_a.pdf",
            "total_pages": 12,
            "chunk_count": 47,
          },
          ...
        ]
    """
    collection = get_collection()

    if collection.count() == 0:
        return []

    # Fetch all metadatas (no embeddings or documents needed)
    results = collection.get(include=["metadatas"])
    metadatas = results.get("metadatas", [])

    # Aggregate by source
    doc_map: Dict[str, Dict[str, Any]] = {}
    for meta in metadatas:
        src = meta.get("source", "unknown")
        if src not in doc_map:
            doc_map[src] = {
                "source": src,
                "total_pages": meta.get("total_pages", 0),
                "chunk_count": 0,
            }
        doc_map[src]["chunk_count"] += 1

    documents = sorted(doc_map.values(), key=lambda d: d["source"])
    logger.debug(f"list_documents: {len(documents)} unique documents")
    return documents


def get_collection_stats() -> Dict[str, Any]:
    """
    Quick health-check summary for the /status endpoint or debugging.

    Returns:
        {"collection": str, "total_vectors": int, "document_count": int}
    """
    collection = get_collection()
    docs = list_documents()
    return {
        "collection": COLLECTION_NAME,
        "total_vectors": collection.count(),
        "document_count": len(docs),
    }