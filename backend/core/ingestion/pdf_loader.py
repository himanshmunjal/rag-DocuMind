# import fitz
# from pathlib import Path
# from typing import List, Dict, Any
# from loguru import logger

# from config import settings

# def load_path(file_path: str | Path) -> List[Dict[str, Any]]:
    
#     file = Path(file_path)
#     if not file.exists():
#         raise FileNotFoundError(f"File not found: {file_path}")
    
    
#     pages = []
#     doc = fitz.open(str(file))
#     total_pages = len(doc)

#     logger.info(f"Loading PDF: {file_path.name} ({total_pages} pages)")

#     for page_num, page in enumerate(doc, start=1):
#         text = page.get_text("text").strip()

#         if len(text) < 30:
#             logger.debug(f"Skipping near-blank page {page_num}")
#             continue

#         pages.append({
#             "text":text,
#             "metadata":{
#                 "source": file_path.name,
#                 "page": page_num,
#                 "total_pages":total_pages,
#                 "file_path": str(file_path),
#                 "char_count": len(text),
#             }
#         })

    
#     doc.close()

#     logger.success(f"Loaded {len(pages)} non-blank pages from {file_path.name}")
#     return pages

"""
pdf_loader.py  —  Unified PDF ingestion router  (replaces the original).

Handles every page type a real-world PDF can contain:

  Type             Detection signal                      Extraction method
  ───────────────  ────────────────────────────────────  ─────────────────────────────
  Digital text     get_text() returns >100 chars         PyMuPDF directly
  Scanned/printed  image coverage >50%, little text      Tesseract + preprocessing
  Handwritten      Tesseract confidence <45, sparse words  Gemini Vision transcription
  Mixed            partial text + embedded images        Per-region routing + merge
  Annotated        printed base + handwritten overlay    Gemini Vision (annotation mode)
  Table-heavy      Tesseract confidence low on grid page   Gemini Vision (table mode)

Return format is IDENTICAL to the original pdf_loader — chunker, embedder,
and indexer receive the same list-of-dicts and need zero changes.
Two extra metadata fields are added:
  extraction_method : "digital" | "tesseract" | "vision_*" | "mixed"
  ocr_confidence    : 0.0–1.0  (1.0 for digital text; model confidence otherwise)

These flow through to CitationItem so the UI can badge OCR'd citations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import fitz  # PyMuPDF
from loguru import logger

from core.ingestion.ocr_engine import (
    OCRResult,
    ocr_page,
    ocr_region,
)
from core.ingestion.vision_extractor import (
    VisionResult,
    transcribe_annotated_page,
    transcribe_mixed_page,
    transcribe_page,
    transcribe_region,
    transcribe_table_page,
)


# ── tuneable thresholds ───────────────────────────────────────────────────────

# Minimum selectable chars for a page to count as "digital text"
DIGITAL_TEXT_MIN_CHARS  = 100

# Image area / page area above this → page is image-heavy
IMAGE_COVERAGE_THRESHOLD = 0.50

# Tesseract confidence below this on a scanned page → try Vision instead
LOW_TESS_CONFIDENCE      = 0.45

# If a "digital" page still has significant image area, treat as mixed
MIXED_IMAGE_THRESHOLD    = 0.30

# Minimum extracted chars to index a page at all
MIN_PAGE_CHARS           = 30

# If a page has a grid-like structure and low Tesseract confidence, use table mode
TABLE_DETECTION_MIN_LINES = 8   # minimum "|" chars in Tesseract output to suspect a table


# ── page type classifier ──────────────────────────────────────────────────────

def _get_image_coverage(page: fitz.Page) -> float:
    """
    Return the fraction of page area occupied by embedded images.
    Uses image bounding boxes from PyMuPDF's image list.
    """
    page_area = page.rect.width * page.rect.height
    if page_area == 0:
        return 0.0

    image_area = 0.0
    for img in page.get_images(full=True):
        # get_image_bbox needs the xref (first element)
        try:
            bbox        = page.get_image_bbox(img[0])
            image_area += bbox.width * bbox.height
        except Exception:
            # Some PDFs have malformed image entries — skip safely
            continue

    return min(image_area / page_area, 1.0)


def _has_annotation_layer(page: fitz.Page) -> bool:
    """
    Return True if the page has PDF annotations (comments, ink, stamps).
    These are often handwritten marks added on top of a digital or scanned page.
    """
    return any(True for _ in page.annots())


def classify_page(page: fitz.Page) -> str:
    """
    Classify a page into one of five extraction strategies.

    Returns one of:
      "digital"    — use PyMuPDF get_text() directly
      "scanned"    — rasterise then Tesseract OCR
      "mixed"      — partial digital text + image regions
      "annotated"  — printed/digital base + handwritten annotation layer
    """
    text_chars     = len(page.get_text("text").strip())
    image_coverage = _get_image_coverage(page)
    has_annots     = _has_annotation_layer(page)

    # Priority 1: annotation layer present → annotated regardless of base content
    if has_annots:
        return "annotated"

    # Priority 2: plenty of selectable text
    if text_chars >= DIGITAL_TEXT_MIN_CHARS:
        if image_coverage >= MIXED_IMAGE_THRESHOLD:
            return "mixed"
        return "digital"

    # Priority 3: image-heavy, little text → scanned
    if image_coverage >= IMAGE_COVERAGE_THRESHOLD:
        return "scanned"

    # Priority 4: some text, some images → mixed
    if text_chars > 20 and image_coverage > 0.05:
        return "mixed"

    # Priority 5: tiny text, small images → assume scanned
    return "scanned"


# ── per-strategy extractors ───────────────────────────────────────────────────

def _extract_digital(page: fitz.Page) -> Tuple[str, str, float]:
    """Fast path: pull selectable text directly from the PDF."""
    text = page.get_text("text").strip()
    return text, "digital", 1.0


def _extract_scanned(page: fitz.Page) -> Tuple[str, str, float]:
    """
    Tesseract OCR with fallback to Gemini Vision.
    Decision tree:
      Tesseract OK (conf ≥ threshold, not handwritten)  → use Tesseract
      Tesseract detects handwriting                      → Gemini Vision (transcribe)
      Tesseract low confidence + looks like table        → Gemini Vision (table)
      Tesseract low confidence otherwise                 → Gemini Vision (transcribe)
    """
    result: OCRResult = ocr_page(page)

    if result.is_handwritten:
        logger.info(f"  Page {page.number + 1}: handwriting detected → Vision transcription")

        try:
            vision = transcribe_page(page)
            return vision.text, vision.method, vision.confidence
        except Exception as exc:
            logger.warning(f"Vision failed ({exc}) — falling back to Tesseract result")
            return result.text, "tesseract_lowconf", result.confidence

    if result.confidence >= LOW_TESS_CONFIDENCE:
        if result.warnings:
            for w in result.warnings:
                logger.warning(f"  Page {page.number + 1}: OCR warning — {w}")
        return result.text, result.method, result.confidence

    # Low Tesseract confidence — decide between table and generic Vision
    tess_text = result.text
    pipe_count = tess_text.count("|")
    is_table   = pipe_count >= TABLE_DETECTION_MIN_LINES

    if is_table:
        logger.info(f"  Page {page.number + 1}: low Tesseract conf + table structure → Vision table mode")
        vision = transcribe_table_page(page)
    else:
        logger.info(
            f"  Page {page.number + 1}: low Tesseract conf ({result.confidence:.2f}) → Vision transcription"
        )
        vision = transcribe_page(page)

    return vision.text, vision.method, vision.confidence


def _extract_mixed(page: fitz.Page) -> Tuple[str, str, float]:
    """
    Mixed page: combine digital text blocks + OCR'd image regions in reading order.

    Strategy:
      1. Pull all digital text blocks with their bounding boxes (y position)
      2. For each image region:
           a. OCR it with Tesseract
           b. If Tesseract detects handwriting, use Vision on that region
      3. Sort all blocks by top-y coordinate → reading order
      4. Join with blank lines between blocks
    """
    blocks: List[Dict[str, Any]] = []

    # ── Digital text blocks ────────────────────────────────────────────────────
    for block in page.get_text("blocks"):
        x0, y0, x1, y1, text, block_no, block_type = block
        if block_type == 0 and text.strip():   # block_type 0 = text
            blocks.append({
                "y":      y0,
                "text":   text.strip(),
                "method": "digital",
                "conf":   1.0,
            })

    # ── Image regions → OCR ───────────────────────────────────────────────────
    for img_info in page.get_images(full=True):
        xref = img_info[0]
        try:
            bbox = page.get_image_bbox(xref)
        except Exception:
            continue

        if bbox.width < 20 or bbox.height < 20:
            continue   # decorative element, skip

        region_result: OCRResult = ocr_region(page, bbox)

        if not region_result.is_usable():
            continue

        if region_result.is_handwritten:
            # Upgrade this region to Vision
            vision_region: VisionResult = transcribe_region(page, bbox, mode="transcribe")
            if vision_region.is_usable():
                blocks.append({
                    "y":      bbox.y0,
                    "text":   vision_region.text,
                    "method": vision_region.method,
                    "conf":   vision_region.confidence,
                })
        else:
            blocks.append({
                "y":      bbox.y0,
                "text":   region_result.text,
                "method": region_result.method,
                "conf":   region_result.confidence,
            })

    if not blocks:
        return "", "mixed", 0.0

    # Sort by vertical position (reading order)
    blocks.sort(key=lambda b: b["y"])
    combined = "\n\n".join(b["text"] for b in blocks)

    # Composite confidence: weight digital blocks at 1.0, others by their score
    total_conf = sum(b["conf"] for b in blocks) / len(blocks)

    return combined, "mixed", round(total_conf, 3)


def _extract_annotated(page: fitz.Page) -> Tuple[str, str, float]:
    """
    Pages with PDF annotation layers (ink, stamp, comment, text box annotations).

    Approach:
      1. If page has significant selectable text → Vision annotation mode
         (preserves both base text and annotation context in one pass)
      2. If page is largely image-based → Vision mixed mode (treats
         annotations as part of the image)

    Gemini's annotation prompt specifically handles signatures, margin notes,
    corrections, and initials — things that require visual understanding.
    """
    base_text_chars = len(page.get_text("text").strip())

    if base_text_chars > DIGITAL_TEXT_MIN_CHARS:
        # Rich base text + annotations → annotation mode gives best context
        logger.info(f"  Page {page.number + 1}: annotation layer on digital base → Vision annotation mode")
        result: VisionResult = transcribe_annotated_page(page)
    else:
        # Scanned base + annotations → mixed mode
        logger.info(f"  Page {page.number + 1}: annotation layer on scanned base → Vision mixed mode")
        result = transcribe_mixed_page(page)

    return result.text, result.method, result.confidence


# ── public API ────────────────────────────────────────────────────────────────

def load_pdf(file_path: str | Path) -> List[Dict[str, Any]]:
    """
    Load a PDF and return a list of page dicts with text + metadata.
    Drop-in replacement for the original pdf_loader.load_pdf().

    Each PDF page is classified and routed to the correct extraction method.
    Pages with no extractable text (blank, purely decorative) are skipped.

    Returns:
        [
          {
            "text": "...",
            "metadata": {
              "source":             "contract_a.pdf",
              "page":               1,
              "total_pages":        42,
              "file_path":          "/data/uploads/contract_a.pdf",
              "char_count":         1842,
              "extraction_method":  "digital" | "tesseract" | "vision_*" | "mixed",
              "ocr_confidence":     1.0,   # 0.0–1.0; 1.0 for digital text
              "page_type":          "digital" | "scanned" | "mixed" | "annotated",
            }
          },
          ...
        ]
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    doc         = fitz.open(str(file_path))
    total_pages = len(doc)
    pages       = []

    logger.info(f"Loading PDF: {file_path.name} ({total_pages} pages)")

    for page_num, page in enumerate(doc, start=1):
        page_type = classify_page(page)
        logger.debug(f"  Page {page_num}/{total_pages}: classified as '{page_type}'")

        try:
            if page_type == "digital":
                text, method, conf = _extract_digital(page)
            elif page_type == "scanned":
                text, method, conf = _extract_scanned(page)
            elif page_type == "mixed":
                text, method, conf = _extract_mixed(page)
            elif page_type == "annotated":
                text, method, conf = _extract_annotated(page)
            else:
                logger.warning(f"  Page {page_num}: unknown type '{page_type}' — skipping")
                continue

        except Exception as exc:
            logger.error(f"  Page {page_num}: extraction failed ({exc}) — skipping")
            continue

        # Skip near-blank pages regardless of extraction method
        if len(text.strip()) < MIN_PAGE_CHARS:
            logger.debug(f"  Page {page_num}: extracted <{MIN_PAGE_CHARS} chars — skipping")
            continue

        pages.append({
            "text": text,
            "metadata": {
                "source":            file_path.name,
                "page":              page_num,
                "total_pages":       total_pages,
                "file_path":         str(file_path),
                "char_count":        len(text),
                "page_type":         page_type,
                "extraction_method": method,
                "ocr_confidence":    conf,
            }
        })

    doc.close()

    # Summary log
    method_counts: Dict[str, int] = {}
    for p in pages:
        m = p["metadata"]["extraction_method"]
        method_counts[m] = method_counts.get(m, 0) + 1

    logger.success(
        f"Loaded {len(pages)}/{total_pages} pages from '{file_path.name}' | "
        + " · ".join(f"{v}×{k}" for k, v in method_counts.items())
    )
    return pages