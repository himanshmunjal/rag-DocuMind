"""
Chunker — Day 3-4.
NOT just fixed 500-token splits. Strategy:
  1. Try to split on section boundaries (double newlines = paragraph breaks)
  2. If a paragraph is still too long, split on sentence boundaries
  3. Last resort: fixed token window with overlap

This respects document structure and keeps citations meaningful.
"""
import re
from typing import List, Dict, Any
from loguru import logger

from config import settings


def _split_into_paragraphs(text: str) -> List[str]:
    """Split on double newlines (section/paragraph boundaries)."""
    paragraphs = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


def _split_into_sentences(text: str) -> List[str]:
    """Naive sentence splitter. Swap for spaCy if you need accuracy."""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip()]


def _token_estimate(text: str) -> int:
    """~4 chars per token heuristic — good enough for chunking decisions."""
    return len(text) // 4


def chunk_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Takes pages from pdf_loader and returns chunks with metadata.

    Each chunk:
    {
      "text": "...",
      "metadata": {
        "source": "contract_a.pdf",
        "page": 3,
        "chunk_index": 7,   ← global across the doc
        "chunk_type": "paragraph" | "sentence" | "fixed",
      }
    }
    """
    all_chunks = []
    chunk_index = 0

    for page in pages:
        text = page["text"]
        meta = page["metadata"]

        if settings.SEMANTIC_CHUNKING:
            paragraphs = _split_into_paragraphs(text)
        else:
            paragraphs = [text]  # treat whole page as one unit

        for para in paragraphs:
            if _token_estimate(para) <= settings.CHUNK_SIZE:
                # Paragraph fits in one chunk — ideal case
                all_chunks.append({
                    "text": para,
                    "metadata": {
                        **meta,
                        "chunk_index": chunk_index,
                        "chunk_type": "paragraph",
                        "extraction_method": meta.get("extraction_method", "digital"),
                        "ocr_confidence":    meta.get("ocr_confidence", 1.0),
                    }
                })
                chunk_index += 1
            else:
                # Paragraph too long — split by sentence with overlap
                sentences = _split_into_sentences(para)
                current_chunk = []
                current_tokens = 0

                for sentence in sentences:
                    s_tokens = _token_estimate(sentence)

                    if current_tokens + s_tokens > settings.CHUNK_SIZE and current_chunk:
                        all_chunks.append({
                            "text": " ".join(current_chunk),
                            "metadata": {
                                **meta,
                                "chunk_index": chunk_index,
                                "chunk_type": "sentence",
                            }
                        })
                        chunk_index += 1
                        # Overlap: keep last N sentences
                        overlap_tokens = 0
                        overlap_sents = []
                        for s in reversed(current_chunk):
                            if overlap_tokens + _token_estimate(s) > settings.CHUNK_OVERLAP:
                                break
                            overlap_sents.insert(0, s)
                            overlap_tokens += _token_estimate(s)
                        current_chunk = overlap_sents

                    current_chunk.append(sentence)
                    current_tokens = sum(_token_estimate(s) for s in current_chunk)

                if current_chunk:
                    all_chunks.append({
                        "text": " ".join(current_chunk),
                        "metadata": {
                            **meta,
                            "chunk_index": chunk_index,
                            "chunk_type": "sentence",
                        }
                    })
                    chunk_index += 1

    logger.info(f"Chunked into {len(all_chunks)} chunks")
    return all_chunks