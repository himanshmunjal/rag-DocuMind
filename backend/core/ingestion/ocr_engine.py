"""
ocr_engine.py  —  Tesseract OCR with production-grade preprocessing.

Handles: scanned PDFs, photocopied pages, printed forms, typed text
embedded as images, tables photographed or screenshotted.

Pipeline per page
─────────────────
  1. Rasterise  — page → high-res PIL image (300 DPI default)
  2. Preprocess — greyscale → deskew → denoise → binarise
  3. OCR        — Tesseract LSTM engine with confidence scoring
  4. Post-proc  — strip junk characters, normalise whitespace
  5. Verdict    — decide whether to hand off to vision_extractor
                  (low avg confidence + irregular word pattern → handwritten)
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import List, Tuple

import cv2
import fitz  # PyMuPDF
import numpy as np
import pytesseract
from PIL import Image
from loguru import logger

# ── tuneable constants ────────────────────────────────────────────────────────

RASTERISE_DPI          = 300     # dots per inch for page→image conversion
RASTERISE_DPI_FAST     = 200     # used when a quick first-pass is enough
MIN_TEXT_CHARS         = 30      # below this a page is considered blank

# Deskew
MAX_DESKEW_ANGLE       = 20.0    # degrees; larger angles are unlikely to be skew
MIN_DESKEW_ANGLE       = 0.5     # smaller angles aren't worth correcting

# Binarisation
ADAPTIVE_BLOCK_SIZE    = 15      # px; must be odd
ADAPTIVE_C             = 8       # constant subtracted from mean

# Handwriting detection thresholds
HANDWRITING_CONF_THRESHOLD   = 45    # avg Tesseract confidence (0–100)
HANDWRITING_WORD_THRESHOLD   = 60    # word count; below this, sparse → suspect
HANDWRITING_SINGLE_CHAR_RATIO= 0.35  # fraction of words that are 1 char long

# Tesseract configs
TESS_CONFIG_PAGE   = "--oem 3 --psm 3"   # full page, auto layout detection
TESS_CONFIG_BLOCK  = "--oem 3 --psm 6"   # single uniform block (sub-regions)
TESS_CONFIG_SPARSE = "--oem 3 --psm 11"  # sparse text (forms, headers)


# ── result dataclass ──────────────────────────────────────────────────────────

@dataclass
class OCRResult:
    """
    Return type from ocr_page() and ocr_region().
    Carries everything the caller and the metadata layer need.
    """
    text:             str
    confidence:       float          # 0.0–1.0
    is_handwritten:   bool           # True → caller should use vision_extractor
    word_count:       int
    method:           str            # "tesseract" | "tesseract_region"
    warnings:         List[str]      = field(default_factory=list)

    def is_usable(self) -> bool:
        """Enough text was extracted to be worth indexing."""
        return len(self.text.strip()) >= MIN_TEXT_CHARS


# ── image preprocessing ───────────────────────────────────────────────────────

def _to_numpy_grey(pil_image: Image.Image) -> np.ndarray:
    """Convert PIL image to greyscale numpy array."""
    return cv2.cvtColor(np.array(pil_image.convert("RGB")), cv2.COLOR_RGB2GRAY)


def _deskew(grey: np.ndarray) -> Tuple[np.ndarray, float]:
    """
    Detect and correct page skew up to ±MAX_DESKEW_ANGLE degrees.

    Method: find all dark pixels (text strokes), fit a minimum area
    rectangle to them, read the angle.  Rotate the whole image to correct.

    Returns corrected image and the angle applied (degrees).
    """
    # Threshold to isolate dark pixels (text strokes)
    _, binary = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords     = np.column_stack(np.where(binary > 0))

    if len(coords) < 100:
        # Not enough dark pixels to estimate angle reliably
        return grey, 0.0

    angle = cv2.minAreaRect(coords)[-1]

    # OpenCV minAreaRect returns angles in [-90, 0); normalise to [-45, 45)
    if angle < -45:
        angle += 90

    if abs(angle) < MIN_DESKEW_ANGLE or abs(angle) > MAX_DESKEW_ANGLE:
        return grey, 0.0

    (h, w)  = grey.shape
    centre  = (w // 2, h // 2)
    M       = cv2.getRotationMatrix2D(centre, angle, 1.0)
    rotated = cv2.warpAffine(
        grey, M, (w, h),
        flags=cv2.INTER_CUBIC,
        borderMode=cv2.BORDER_REPLICATE,
    )
    logger.debug(f"Deskewed by {angle:.2f}°")
    return rotated, angle


def _denoise(grey: np.ndarray) -> np.ndarray:
    """
    Non-local means denoising — removes scanner grain without blurring strokes.
    h=10 is a conservative setting; increase to 15 for very noisy scans.
    """
    return cv2.fastNlMeansDenoising(grey, None, h=10, templateWindowSize=7, searchWindowSize=21)


def _binarise(grey: np.ndarray) -> np.ndarray:
    """
    Adaptive Gaussian threshold — handles pages where the background
    brightness varies (common in photocopies and phone camera scans).

    Falls back to global Otsu threshold if the adaptive result looks wrong
    (very few or very many white pixels).
    """
    adaptive = cv2.adaptiveThreshold(
        grey, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        ADAPTIVE_BLOCK_SIZE,
        ADAPTIVE_C,
    )

    # Sanity check: if result is >95% white or <20% white, use Otsu instead
    white_ratio = np.sum(adaptive == 255) / adaptive.size
    if white_ratio > 0.95 or white_ratio < 0.20:
        _, otsu = cv2.threshold(grey, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        logger.debug(f"Adaptive binarise gave white_ratio={white_ratio:.2f} — falling back to Otsu")
        return otsu

    return adaptive


def _scale_if_small(grey: np.ndarray, min_height: int = 1000) -> np.ndarray:
    """
    Upscale images that are too small for reliable OCR.
    Tesseract performs best on images where text height is ≥ 30px.
    """
    h, w = grey.shape
    if h < min_height:
        scale  = min_height / h
        new_w  = int(w * scale)
        grey   = cv2.resize(grey, (new_w, min_height), interpolation=cv2.INTER_CUBIC)
        logger.debug(f"Upscaled from {h}×{w} to {min_height}×{new_w}")
    return grey


def preprocess(pil_image: Image.Image) -> Image.Image:
    """
    Full preprocessing pipeline: grey → scale → deskew → denoise → binarise.
    Returns a PIL Image ready for Tesseract.
    """
    grey             = _to_numpy_grey(pil_image)
    grey             = _scale_if_small(grey)
    grey, skew_angle = _deskew(grey)
    grey             = _denoise(grey)
    binary           = _binarise(grey)
    return Image.fromarray(binary)


# ── rasterisation ─────────────────────────────────────────────────────────────

def rasterise_page(page: fitz.Page, dpi: int = RASTERISE_DPI) -> Image.Image:
    """
    Render a PyMuPDF page to a PIL Image at the given DPI.
    300 DPI is the minimum for reliable OCR; use 200 for a quick first pass.
    """
    scale  = dpi / 72          # PDF internal units are 72 pts/inch
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)


def rasterise_region(page: fitz.Page, rect: fitz.Rect, dpi: int = RASTERISE_DPI) -> Image.Image:
    """Render a sub-region (clip rect) of a page."""
    scale  = dpi / 72
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, clip=rect, colorspace=fitz.csRGB, alpha=False)
    return Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)


# ── handwriting detection ─────────────────────────────────────────────────────

def _detect_handwriting(
    data: dict,
    word_count: int,
    avg_conf: float,
) -> Tuple[bool, str]:
    """
    Decide if a Tesseract result looks like handwritten text.

    Three-signal heuristic — all three must fire:
      1. Low average confidence
      2. Low word count relative to what a printed page would have
      3. High ratio of single-character 'words' (broken strokes misread as letters)

    Returns (is_handwritten: bool, reason: str)
    """
    words = [w for w in data.get("text", []) if w.strip()]

    if not words:
        return False, ""

    single_char_count = sum(1 for w in words if len(w.strip()) == 1)
    single_char_ratio = single_char_count / len(words)

    signals = {
        "low_confidence":    avg_conf < HANDWRITING_CONF_THRESHOLD,
        "low_word_count":    word_count < HANDWRITING_WORD_THRESHOLD,
        "fragmented_words":  single_char_ratio > HANDWRITING_SINGLE_CHAR_RATIO,
    }

    is_handwritten = all(signals.values())
    reason = ", ".join(k for k, v in signals.items() if v) if is_handwritten else ""

    if is_handwritten:
        logger.info(
            f"Handwriting detected — "
            f"conf={avg_conf:.1f}, words={word_count}, "
            f"single_char_ratio={single_char_ratio:.2f}"
        )
    return is_handwritten, reason


# ── text postprocessing ───────────────────────────────────────────────────────

def _clean_ocr_text(raw: str) -> str:
    """
    Remove common OCR artefacts:
      - Page numbers / headers extracted as isolated numbers
      - Lines that are entirely punctuation / junk characters
      - Excessive whitespace and blank lines
      - Form-feed characters
    """
    lines   = raw.replace("\f", "\n").split("\n")
    cleaned = []

    for line in lines:
        stripped = line.strip()

        # Skip blank lines (will re-add controlled blank lines below)
        if not stripped:
            continue

        # Skip lines that are just numbers (likely page numbers)
        if re.fullmatch(r"\d{1,4}", stripped):
            continue

        # Skip lines with almost no alphanumeric content (junk strokes)
        alnum = sum(c.isalnum() for c in stripped)
        if len(stripped) > 2 and alnum / len(stripped) < 0.25:
            continue

        cleaned.append(stripped)

    # Re-join with single newlines; group into paragraphs at double newlines
    return "\n".join(cleaned)


# ── public API ────────────────────────────────────────────────────────────────

def ocr_page(page: fitz.Page, fast: bool = False) -> OCRResult:
    """
    Run the full OCR pipeline on a single PyMuPDF page.

    Args:
        page: PyMuPDF page object
        fast: if True, use 200 DPI instead of 300 (faster, slightly lower quality)

    Returns:
        OCRResult with text, confidence, handwriting flag, and warnings
    """
    dpi = RASTERISE_DPI_FAST if fast else RASTERISE_DPI
    logger.debug(f"OCR page {page.number + 1} at {dpi} DPI")

    # 1. Rasterise
    raw_img   = rasterise_page(page, dpi=dpi)

    # 2. Preprocess
    processed = preprocess(raw_img)

    # 3. Run Tesseract with full per-word confidence data
    try:
        data = pytesseract.image_to_data(
            processed,
            output_type=pytesseract.Output.DICT,
            config=TESS_CONFIG_PAGE,
        )
    except pytesseract.TesseractNotFoundError:
        logger.error(
            "Tesseract binary not found. "
            "Install it: sudo apt-get install tesseract-ocr  |  brew install tesseract"
        )
        raise

    # 4. Extract words and confidences
    words       = [w for w in data["text"] if w.strip()]
    confidences = [
        int(c) for c, w in zip(data["conf"], data["text"])
        if w.strip() and int(c) >= 0   # -1 means Tesseract didn't score it
    ]

    word_count = len(words)
    avg_conf   = (sum(confidences) / len(confidences)) if confidences else 0.0
    raw_text   = " ".join(words)

    # 5. Post-process
    clean_text = _clean_ocr_text(raw_text)

    # 6. Handwriting detection
    is_handwritten, hw_reason = _detect_handwriting(data, word_count, avg_conf)

    warnings = []
    if avg_conf < 50 and not is_handwritten:
        warnings.append(f"Low OCR confidence ({avg_conf:.0f}/100) — check scan quality")
    if word_count < 10 and len(clean_text) > MIN_TEXT_CHARS:
        warnings.append("Very few words detected — page may have unusual layout")

    logger.debug(
        f"OCR result: {word_count} words, "
        f"avg_conf={avg_conf:.1f}, "
        f"handwritten={is_handwritten}, "
        f"chars={len(clean_text)}"
    )

    return OCRResult(
        text           = clean_text,
        confidence     = round(avg_conf / 100, 3),   # normalise to 0–1
        is_handwritten = is_handwritten,
        word_count     = word_count,
        method         = "tesseract",
        warnings       = warnings,
    )


def ocr_region(page: fitz.Page, rect: fitz.Rect, block_text: bool = True) -> OCRResult:
    """
    OCR a specific rectangular region of a page (used in mixed-page extraction).

    Args:
        page:       PyMuPDF page object
        rect:       bounding box of the region to OCR
        block_text: if True, use block-text config (PSM 6);
                    if False, use sparse config (PSM 11, better for forms/headers)

    Returns:
        OCRResult for just this region
    """
    raw_img   = rasterise_region(page, rect, dpi=RASTERISE_DPI)
    processed = preprocess(raw_img)

    config = TESS_CONFIG_BLOCK if block_text else TESS_CONFIG_SPARSE

    data = pytesseract.image_to_data(
        processed,
        output_type=pytesseract.Output.DICT,
        config=config,
    )

    words       = [w for w in data["text"] if w.strip()]
    confidences = [int(c) for c, w in zip(data["conf"], data["text"])
                   if w.strip() and int(c) >= 0]

    word_count = len(words)
    avg_conf   = (sum(confidences) / len(confidences)) if confidences else 0.0
    clean_text = _clean_ocr_text(" ".join(words))

    is_handwritten, _ = _detect_handwriting(data, word_count, avg_conf)

    return OCRResult(
        text           = clean_text,
        confidence     = round(avg_conf / 100, 3),
        is_handwritten = is_handwritten,
        word_count     = word_count,
        method         = "tesseract_region",
    )