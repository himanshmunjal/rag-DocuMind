"""
vision_extractor.py  —  Gemini Vision API transcription layer.

Called when ocr_engine detects handwritten content, or when Tesseract
confidence is too low to trust on a region.  Also handles:
  - Mixed printed + handwritten pages (e.g. signed contracts)
  - Complex table structures that Tesseract mangles
  - Pages with stamps, annotations, or rotated text blocks
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass, field
from typing import Optional

import fitz
from PIL import Image
from loguru import logger
import google.generativeai as genai

from config import settings


# ── constants ─────────────────────────────────────────────────────────────────

VISION_MODEL         = "models/gemini-2.5-flash"   # cheapest, still excellent for OCR
MAX_IMAGE_LONG_EDGE  = 1600                 # px; above this Vision sees no benefit
JPEG_QUALITY         = 88                   # good quality / reasonable token cost
RASTERISE_DPI        = 200                  # DPI for Vision (lower than Tesseract — Vision is resolution-agnostic)
MAX_RETRIES          = 3
RETRY_BASE_DELAY     = 2.0                  # seconds; doubles each retry

# ── prompts ───────────────────────────────────────────────────────────────────

PROMPT_TRANSCRIBE = """\
Transcribe ALL text visible in this document page exactly as it appears.

Rules:
- Include every word: headings, body text, labels, captions, annotations, stamps, and marginalia
- Preserve the reading order (top-to-bottom, left-to-right unless the layout dictates otherwise)
- For handwritten text: transcribe the actual words written, even if the handwriting is messy
- Separate clearly distinct sections or paragraphs with a blank line
- Do NOT summarise, interpret, or paraphrase — only transcribe
- If a word is completely illegible, write [illegible] in its place
- Do not add commentary, headings, or labels of your own
- Ignore decorative elements, borders, and images that contain no text
"""

PROMPT_MIXED = """\
This page contains both PRINTED text and HANDWRITTEN text.

Transcribe ALL text exactly as it appears, preserving reading order.

For each clearly separate section, prefix it with either:
  [PRINTED]      — for typeset or machine-printed text
  [HANDWRITTEN]  — for handwritten text

Rules:
- If a section is ambiguous, use [PRINTED]
- Transcribe handwritten text even if messy; mark illegible words as [illegible]
- Preserve line breaks within sections
- Do NOT summarise, interpret, or add commentary
"""

PROMPT_TABLE = """\
This page contains a table or structured form.

Extract all the data and reproduce it as plain pipe-separated text:
  Column1 | Column2 | Column3
  Value1  | Value2  | Value3

Rules:
- Include ALL rows including the header row
- If a cell is empty, output a single dash: -
- If the page has text outside the table, transcribe it before the table
- Do NOT summarise or describe the table — output the raw data
"""

PROMPT_ANNOTATION = """\
This document page has handwritten annotations, signatures, or notes added to it.

Transcribe:
1. All printed/typed text on the page
2. All handwritten additions, including:
   - Annotations in margins
   - Corrections written over or near printed text
   - Signatures (transcribe as [SIGNATURE: <name if legible>])
   - Dates written by hand (transcribe as written)
   - Initials (transcribe as written)

Preserve the approximate position context: note if an annotation is in the
margin, above a line, or at the bottom of the page.
"""


# ── result dataclass ──────────────────────────────────────────────────────────

@dataclass
class VisionResult:
    """
    Return type from all public functions in this module.
    Compatible with ocr_engine.OCRResult for uniform handling in pdf_loader.
    """
    text:              str
    confidence:        float            # 0.0–1.0; Vision results are generally 0.75–0.95
    method:            str              # "vision_transcribe" | "vision_mixed" | "vision_table" | "vision_annotation"
    has_handwriting:   bool = False
    has_printed:       bool = True
    has_table:         bool = False
    warnings:          list = field(default_factory=list)

    def is_usable(self) -> bool:
        return len(self.text.strip()) >= 30


# ── image utilities ───────────────────────────────────────────────────────────

def _resize_for_vision(img: Image.Image) -> Image.Image:
    """
    Resize to ≤ MAX_IMAGE_LONG_EDGE on the long edge.
    Gemini Vision doesn't improve above ~1600px and costs more tokens.
    """
    w, h       = img.size
    long_edge  = max(w, h)
    if long_edge <= MAX_IMAGE_LONG_EDGE:
        return img
    scale  = MAX_IMAGE_LONG_EDGE / long_edge
    new_w  = int(w * scale)
    new_h  = int(h * scale)
    return img.resize((new_w, new_h), Image.LANCZOS)


def _encode_image(img: Image.Image) -> str:
    """Encode a PIL Image to a base64 JPEG string for the Gemini API."""
    img   = _resize_for_vision(img.convert("RGB"))
    buf   = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _rasterise(page: fitz.Page, dpi: int = RASTERISE_DPI) -> Image.Image:
    """Render a PyMuPDF page to a PIL RGB Image."""
    scale  = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)


def _rasterise_region(page: fitz.Page, rect: fitz.Rect, dpi: int = RASTERISE_DPI) -> Image.Image:
    """Render a rectangular sub-region of a page."""
    scale  = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, clip=rect, colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)


# ── Gemini API call with retry ────────────────────────────────────────────────

def _call_gemini(prompt: str, b64_image: str) -> str:
    """
    Send a prompt + image to Gemini Vision and return the text response.
    Retries up to MAX_RETRIES times with exponential backoff.
    """
    genai.configure(api_key=settings.GEMINI_API_KEY)
    model = genai.GenerativeModel(VISION_MODEL)

    image_part = {"mime_type": "image/jpeg", "data": b64_image}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = model.generate_content(
                [prompt, image_part],
                generation_config=genai.types.GenerationConfig(
                    temperature=0.1,      # low temp for faithful transcription
                    max_output_tokens=4096,
                ),
            )
            return response.text.strip()

        except Exception as exc:
            if attempt == MAX_RETRIES:
                logger.error(f"Gemini Vision failed after {MAX_RETRIES} attempts: {exc}")
                raise

            delay = RETRY_BASE_DELAY * (2 ** (attempt - 1))
            logger.warning(
                f"Gemini Vision attempt {attempt}/{MAX_RETRIES} failed ({exc}). "
                f"Retrying in {delay:.1f}s…"
            )
            time.sleep(delay)

    raise RuntimeError("_call_gemini: exhausted retries")   # unreachable


# ── text postprocessing ───────────────────────────────────────────────────────

def _parse_mixed_text(raw: str) -> tuple[str, bool, bool]:
    """
    Parse Vision output from PROMPT_MIXED.
    Returns (cleaned_text, has_handwriting, has_printed).
    """
    has_handwriting = "[HANDWRITTEN]" in raw
    has_printed     = "[PRINTED]"     in raw

    # Strip the section labels — downstream pipeline just needs the text
    clean = raw.replace("[PRINTED]", "").replace("[HANDWRITTEN]", "")
    clean = "\n".join(line for line in clean.splitlines() if line.strip())
    return clean, has_handwriting, has_printed


def _estimate_vision_confidence(
    text: str,
    illegible_count: int,
) -> float:
    """
    Estimate how confident Vision's transcription was.
    Base confidence is 0.82 (Vision on handwriting is generally reliable).
    Reduced by how many [illegible] markers appear relative to word count.
    """
    if not text.strip():
        return 0.0

    word_count = len(text.split())
    if word_count == 0:
        return 0.0

    illegible_ratio = illegible_count / max(word_count, 1)
    confidence      = max(0.40, 0.82 - illegible_ratio * 1.5)
    return round(confidence, 3)


def _count_illegible(text: str) -> int:
    import re
    return len(re.findall(r"\[illegible\]", text, re.IGNORECASE))


# ── public API ────────────────────────────────────────────────────────────────

def transcribe_page(page: fitz.Page) -> VisionResult:
    """
    Transcribe a handwritten or low-confidence page using Gemini Vision.
    Use this when ocr_engine.OCRResult.is_handwritten is True.

    Args:
        page: PyMuPDF page object

    Returns:
        VisionResult with transcribed text and metadata
    """
    logger.info(f"Vision transcribing page {page.number + 1} (handwritten/low-confidence)")

    img       = _rasterise(page)
    b64       = _encode_image(img)
    raw_text  = _call_gemini(PROMPT_TRANSCRIBE, b64)

    illegible = _count_illegible(raw_text)
    conf      = _estimate_vision_confidence(raw_text, illegible)

    warnings = []
    if illegible > 0:
        warnings.append(f"{illegible} illegible word(s) marked in transcription")
    if conf < 0.55:
        warnings.append("Low Vision confidence — handwriting may be difficult to read")

    logger.debug(
        f"Vision transcription: {len(raw_text.split())} words, "
        f"illegible={illegible}, confidence={conf}"
    )

    return VisionResult(
        text            = raw_text,
        confidence      = conf,
        method          = "vision_transcribe",
        has_handwriting = True,
        has_printed     = False,
        warnings        = warnings,
    )


def transcribe_mixed_page(page: fitz.Page) -> VisionResult:
    """
    Handle a page that contains both printed and handwritten content —
    the most complex case (signed contracts, annotated forms, etc.).

    Gemini labels sections as [PRINTED] or [HANDWRITTEN]; we strip the
    labels but preserve the reading order.

    Args:
        page: PyMuPDF page object

    Returns:
        VisionResult with combined text and handwriting/printed flags
    """
    logger.info(f"Vision mixed-page extraction for page {page.number + 1}")

    img      = _rasterise(page)
    b64      = _encode_image(img)
    raw_text = _call_gemini(PROMPT_MIXED, b64)

    clean_text, has_hw, has_printed = _parse_mixed_text(raw_text)

    illegible = _count_illegible(clean_text)
    conf      = _estimate_vision_confidence(clean_text, illegible)

    return VisionResult(
        text            = clean_text,
        confidence      = conf,
        method          = "vision_mixed",
        has_handwriting = has_hw,
        has_printed     = has_printed,
        warnings        = [f"{illegible} illegible word(s)"] if illegible else [],
    )


def transcribe_table_page(page: fitz.Page) -> VisionResult:
    """
    Extract a table that Tesseract mangled.
    Gemini outputs pipe-separated plain text which the chunker handles normally.

    Args:
        page: PyMuPDF page object

    Returns:
        VisionResult with table as pipe-separated text
    """
    logger.info(f"Vision table extraction for page {page.number + 1}")

    img      = _rasterise(page)
    b64      = _encode_image(img)
    raw_text = _call_gemini(PROMPT_TABLE, b64)

    conf = _estimate_vision_confidence(raw_text, _count_illegible(raw_text))

    return VisionResult(
        text            = raw_text,
        confidence      = conf,
        method          = "vision_table",
        has_handwriting = False,
        has_printed     = True,
        has_table       = True,
    )


def transcribe_annotated_page(page: fitz.Page) -> VisionResult:
    """
    Handle pages with handwritten annotations layered onto printed base text —
    e.g. a contract page with margin notes, corrections, initials, and signatures.

    The prompt asks Gemini to note the positional context of annotations so
    they stay meaningful in the transcription.

    Args:
        page: PyMuPDF page object

    Returns:
        VisionResult with both base text and annotations transcribed
    """
    logger.info(f"Vision annotation extraction for page {page.number + 1}")

    img      = _rasterise(page)
    b64      = _encode_image(img)
    raw_text = _call_gemini(PROMPT_ANNOTATION, b64)

    illegible = _count_illegible(raw_text)
    conf      = _estimate_vision_confidence(raw_text, illegible)

    return VisionResult(
        text            = raw_text,
        confidence      = conf,
        method          = "vision_annotation",
        has_handwriting = True,
        has_printed     = True,
        warnings        = [f"{illegible} illegible word(s)"] if illegible else [],
    )


def transcribe_region(
    page: fitz.Page,
    rect: fitz.Rect,
    mode: str = "transcribe",
) -> VisionResult:
    """
    Transcribe a specific rectangular region of a page.
    Used by the mixed-page handler in pdf_loader.py when a sub-region
    has been identified as handwritten via OCR confidence checks.

    Args:
        page:  PyMuPDF page object
        rect:  Bounding box of the region
        mode:  "transcribe" | "mixed" | "table" | "annotation"

    Returns:
        VisionResult for just this region
    """
    prompt_map = {
        "transcribe":  PROMPT_TRANSCRIBE,
        "mixed":       PROMPT_MIXED,
        "table":       PROMPT_TABLE,
        "annotation":  PROMPT_ANNOTATION,
    }
    prompt = prompt_map.get(mode, PROMPT_TRANSCRIBE)

    img      = _rasterise_region(page, rect)
    b64      = _encode_image(img)
    raw_text = _call_gemini(prompt, b64)

    illegible = _count_illegible(raw_text)
    conf      = _estimate_vision_confidence(raw_text, illegible)

    return VisionResult(
        text            = raw_text,
        confidence      = conf,
        method          = f"vision_{mode}",
        has_handwriting = mode in ("transcribe", "mixed", "annotation"),
        has_printed     = mode in ("mixed", "table", "annotation"),
    )