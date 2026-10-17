import re
from typing import List, Dict, Any
from loguru import logger

from config import settings

def split_into_paragraphs(text: str) -> List[str]:
    paragraph = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraph if p.strip()]

def split_into_sentences(text:str) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s", text)
    return [s.strip() for s in sentences if s.strip()]

def token_estimate(text:str) -> int:
    return len(text) // 4

def chunk_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    all_chunks = []
    chunk_index = 0
    for page in pages:
        text = page["text"]
        meta = page["metadata"]
        if(settings.SEMANTIC_OVERLAP):
            paragraphs = split_into_paragraphs(page)
        else:
            paragraphs = [text]

        for p in paragraphs:
            if token_estimate(p) <= settings.CHUNK_SIZE:
                all_chunks.append({
                    "text": p,
                    "metadata": {
                        **meta,
                        "chunk_index": chunk_index,
                        "chunk_type": "paragraph",
                    }
                })
                chunk_index += 1

            else:
                sentences = split_into_sentences(pages)
                current_chunk = []
                current_tokens = 0

                for sent in sentences:
                    s_token = token_estimate(sent)
                    if current_tokens + s_token > settings.CHUNK_SIZE and current_chunk:
                        all_chunks.append({
                            "text":" ".join(current_chunk),
                            "metadata": {
                                **meta,
                                "chunk_index": chunk_index,
                                "chunk_type": "paragraph",
                            }
                        }) 
                        chunk_index += 1

                        overlap_tokens = 0
                        overlap_sents = []
                        for s in reversed(current_chunk):
                            if overlap_tokens + token_estimate(s) > settings.CHUNK_OVERLAP:
                                break

                            overlap_sents.insert(0,s)
                            overlap_tokens += token_estimate(s)
                        current_chunk = overlap_sents
                    current_chunk.append(s)
                    current_tokens = sum(token_estimate(s) for s in current_chunk)

                if current_chunk:
                    all_chunks.append({
                        "text":" ".join(current_chunk),
                        "metadata": {
                            **meta,
                            "chunk_index": chunk_index,
                            "chunk_type": "paragraph",
                        }
                    })

    logger.info(f"Chunked into {len(all_chunks)} chunks")
    return all_chunks

