"""
metadata_extractor.py  —  Structured metadata extraction for research papers.

Runs immediately after paper_classifier confirms a PDF is a research paper.
Extracts everything needed to make 50 papers queryable as a structured
collection — not just a bag of text chunks.

What it extracts
────────────────
  Core metadata (stored on EVERY chunk from this paper in ChromaDB):
    title         — full paper title
    authors       — "Lastname et al." short form + full list
    year          — publication year (integer)
    venue         — conference or journal name
    doi           — DOI string if present
    arxiv_id      — arXiv identifier if present

  Research metadata (stored on paper-level record + relevant chunks):
    abstract      — full abstract text
    keywords      — author-provided keywords + auto-extracted
    paper_type    — from paper_classifier
    section_map   — maps section names → char offset ranges in the full text
                    enables targeted retrieval ("only search methodology section")

  Citation metadata (consumed by citation_parser.py):
    raw_references — list of raw reference strings from the references section
    reference_count— total number of references

Two-pass extraction strategy
─────────────────────────────
  Pass 1 — Regex + heuristics (fast, no API cost, catches ~85% of papers):
    Uses font-size analysis via PyMuPDF to find the title (largest text on
    first page), regex patterns for author lines, year, DOI, arXiv ID,
    venue name, abstract block, keywords block.

  Pass 2 — Gemini Vision fallback (only if Pass 1 leaves key fields empty):
    Sends the first page as an image to Gemini with a structured extraction
    prompt.  Returns JSON.  Only title + authors are required for Pass 2;
    other fields are best-effort.

  This keeps 85% of ingestions free of Vision API calls while ensuring
  even unusual paper formats (two-column IEEE, ACM, Nature, arXiv) are
  handled correctly.

Section map
───────────
  The section_map is the most valuable research-specific output.  It lets
  the retriever filter chunks by section:

    "What datasets were used?" → retrieve only from "experiments" section
    "What is the main contribution?" → retrieve only from "abstract" + "introduction"
    "What are the limitations?" → retrieve only from "limitations" + "discussion"

  Format:
    {
      "abstract":     {"start": 0,    "end": 450},
      "introduction": {"start": 450,  "end": 2100},
      "methodology":  {"start": 2100, "end": 4800},
      ...
    }
  Char offsets are into the concatenated full-document text returned by
  pdf_loader, so they remain valid after OCR extraction.
"""

from __future__ import annotations

import base64
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import google.generativeai as genai
from loguru import logger
from PIL import Image

from backend.config import settings


# ── configuration ─────────────────────────────────────────────────────────────

# Minimum font-size ratio vs page median to count as a title candidate
TITLE_FONT_RATIO: float = 1.4

# Maximum title length (chars) — longer strings are likely not a title
TITLE_MAX_CHARS: int = 220

# Minimum abstract length — shorter "abstracts" are likely false positives
ABSTRACT_MIN_CHARS: int = 80

# Gemini Vision model for Pass 2
VISION_MODEL: str = "gemini-1.5-flash"

# Section header vocabulary (canonical names → display name)
# Keys are lowercase regex fragments; values are canonical section names
SECTION_VOCAB: Dict[str, str] = {
    r"abstract":                    "abstract",
    r"introduction":                "introduction",
    r"related\s+work":              "related_work",
    r"background":                  "background",
    r"preliminaries":               "background",
    r"method(?:s|ology)?":          "methodology",
    r"approach":                    "methodology",
    r"proposed\s+method":           "methodology",
    r"framework":                   "methodology",
    r"model":                       "methodology",
    r"architecture":                "methodology",
    r"experiment(?:s|al\s+setup)?": "experiments",
    r"evaluation":                  "experiments",
    r"implementation\s+details?":   "experiments",
    r"result(?:s\s+and\s+discussion)?": "results",
    r"discussion":                  "discussion",
    r"analysis":                    "discussion",
    r"ablation":                    "discussion",
    r"conclusion(?:s\s+and\s+future\s+work)?": "conclusion",
    r"future\s+work":               "future_work",
    r"limitation(?:s)?":            "limitations",
    r"ethical?\s+(?:considerations?|statement)": "ethics",
    r"acknowledge?ments?":          "acknowledgements",
    r"references?":                 "references",
    r"bibliography":                "references",
    r"appendix":                    "appendix",
}

# Build compiled section-detection pattern
_SECTION_PATTERN = re.compile(
    r"^\s*(?:\d[\d.]*\s+)?(" +
    "|".join(SECTION_VOCAB.keys()) +
    r")\s*$",
    re.I | re.M,
)

# ── known venue map (abbreviation → full name) ───────────────────────────────

VENUE_MAP: Dict[str, str] = {
    "neurips": "NeurIPS", "nips": "NeurIPS",
    "icml": "ICML", "iclr": "ICLR",
    "cvpr": "CVPR", "iccv": "ICCV", "eccv": "ECCV",
    "acl": "ACL", "emnlp": "EMNLP", "naacl": "NAACL",
    "aaai": "AAAI", "ijcai": "IJCAI",
    "kdd": "KDD", "www": "WWW", "sigir": "SIGIR",
    "icra": "ICRA", "iros": "IROS",
    "nature": "Nature", "science": "Science",
    "arxiv": "arXiv",
}

# ── regex patterns ────────────────────────────────────────────────────────────

_YEAR_RE     = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
_DOI_RE      = re.compile(r"\b10\.\d{4,}/\S+")
_ARXIV_RE    = re.compile(r"arXiv[:\s](\d{4}\.\d{4,5}(?:v\d+)?)", re.I)
_EMAIL_RE    = re.compile(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}")
_KEYWORDS_RE = re.compile(
    r"(?:keywords?|index\s+terms?)\s*[:\—–-]\s*(.+?)(?=\n\n|\Z)",
    re.I | re.S,
)
_ABSTRACT_RE = re.compile(
    r"(?:^|\n)\s*abstract\s*[:\—–]?\s*\n*(.+?)(?=\n\s*(?:\d[\d.]*\s+)?\w|\Z)",
    re.I | re.S,
)

# Author line: "FirstName LastName, FirstName LastName" with optional superscripts
_AUTHOR_LINE_RE = re.compile(
    r"^(?:[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][A-Za-z'-]+)"
    r"(?:\s*[,;]\s*[A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][A-Za-z'-]+){1,10}",
    re.M,
)


# ── result dataclass ──────────────────────────────────────────────────────────

@dataclass
class PaperMetadata:
    """
    All structured metadata extracted from one research paper.

    Stored at two levels:
      - Paper level: in a dedicated "paper_registry" ChromaDB collection
                     (one record per paper, for library-wide queries)
      - Chunk level: core fields (title, authors, year, venue, section)
                     duplicated onto every chunk for citation rendering
    """
    # ── core ──
    source:           str                         # filename, e.g. "attention.pdf"
    title:            str        = ""
    authors:          str        = ""             # "Vaswani et al." short form
    authors_full:     List[str]  = field(default_factory=list)  # ["Ashish Vaswani", ...]
    year:             Optional[int] = None
    venue:            str        = ""
    doi:              str        = ""
    arxiv_id:         str        = ""

    # ── research ──
    abstract:         str        = ""
    keywords:         List[str]  = field(default_factory=list)
    paper_type:       str        = "empirical"    # from paper_classifier
    section_map:      Dict[str, Dict[str, int]] = field(default_factory=dict)

    # ── citation ──
    raw_references:   List[str]  = field(default_factory=list)
    reference_count:  int        = 0

    # ── extraction quality ──
    extraction_method: str       = "regex"        # "regex" | "vision" | "hybrid"
    completeness:     float      = 0.0            # 0–1, fraction of core fields filled

    def to_chunk_metadata(self, section: str = "", section_index: int = 0) -> Dict[str, Any]:
        """
        Flatten to a dict suitable for ChromaDB chunk metadata.
        ChromaDB metadata values must be str | int | float | bool.
        """
        return {
            "source":        self.source,
            "title":         self.title,
            "authors":       self.authors,
            "year":          self.year or 0,
            "venue":         self.venue,
            "doi":           self.doi,
            "arxiv_id":      self.arxiv_id,
            "paper_type":    self.paper_type,
            "section":       section,
            "section_index": section_index,
            "abstract":      self.abstract[:500],  # truncate for metadata storage
            "keywords":      ", ".join(self.keywords[:10]),
        }

    def compute_completeness(self) -> float:
        """Fraction of the 7 core fields that are non-empty."""
        core = [self.title, self.authors, self.venue, self.doi or self.arxiv_id,
                self.abstract, str(self.year or ""), self.keywords]
        filled = sum(1 for f in core if f and str(f).strip())
        self.completeness = round(filled / len(core), 2)
        return self.completeness


# ── Pass 1: font-size title extraction ───────────────────────────────────────

def _extract_title_by_font(doc: fitz.Document) -> str:
    """
    Find the title by looking for the largest non-header text block on page 1.

    Strategy:
      1. Collect all text spans on page 0 with their font sizes
      2. Compute median font size for the page
      3. Largest span ≥ TITLE_FONT_RATIO × median, length ≤ TITLE_MAX_CHARS
         and not all-caps (common for venue names, not titles) → title candidate
      4. If multiple candidates, prefer the one highest on the page (lower y)
    """
    if len(doc) == 0:
        return ""

    page   = doc[0]
    blocks = page.get_text("dict")["blocks"]

    spans: List[Dict[str, Any]] = []
    for block in blocks:
        if block.get("type") != 0:   # skip image blocks
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = span.get("text", "").strip()
                size = span.get("size", 0)
                if text and size > 0:
                    spans.append({
                        "text": text,
                        "size": size,
                        "y":    span.get("origin", [0, 0])[1],
                    })

    if not spans:
        return ""

    sizes  = [s["size"] for s in spans]
    median = sorted(sizes)[len(sizes) // 2]

    # Title candidates: large, not all-caps venue lines, reasonable length
    candidates = [
        s for s in spans
        if s["size"] >= TITLE_FONT_RATIO * median
        and len(s["text"]) >= 10
        and len(s["text"]) <= TITLE_MAX_CHARS
        and not s["text"].isupper()       # "NEURIPS 2023" is not a title
        and not re.match(r"^\d+$", s["text"])  # page numbers
    ]

    if not candidates:
        return ""

    # Highest on page (smallest y) and largest font wins
    candidates.sort(key=lambda s: (s["y"], -s["size"]))
    return candidates[0]["text"].strip()


def _merge_title_lines(doc: fitz.Document, first_candidate: str) -> str:
    """
    Titles sometimes span two lines.  Look for the span immediately below
    the first candidate and append if it looks like a continuation.
    """
    if not first_candidate:
        return first_candidate

    page   = doc[0]
    blocks = page.get_text("dict")["blocks"]
    all_spans = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                all_spans.append(span)

    # Find our candidate span's position
    candidate_y   = None
    candidate_size = None
    for span in all_spans:
        if first_candidate in span.get("text", ""):
            candidate_y    = span.get("origin", [0, 0])[1]
            candidate_size = span.get("size", 0)
            break

    if candidate_y is None:
        return first_candidate

    # Look for spans just below (within 2× font height) with same/similar size
    continuation = []
    for span in all_spans:
        y    = span.get("origin", [0, 0])[1]
        size = span.get("size", 0)
        text = span.get("text", "").strip()
        if (candidate_y < y <= candidate_y + candidate_size * 2.5
                and abs(size - candidate_size) < 2
                and len(text) > 3
                and text not in first_candidate):
            continuation.append(text)

    if continuation:
        return first_candidate + " " + " ".join(continuation)
    return first_candidate


# ── Pass 1: regex extractors ──────────────────────────────────────────────────

def _extract_authors(text: str) -> Tuple[str, List[str]]:
    """
    Extract author names from the first ~600 chars of page-1 text.
    Returns (short_form, full_list).
    """
    snippet = text[:600]
    match   = _AUTHOR_LINE_RE.search(snippet)
    if not match:
        return "", []

    raw     = match.group(0)
    # Split on commas and semicolons
    authors = [a.strip() for a in re.split(r"[,;]", raw) if a.strip()]
    authors = [a for a in authors if re.search(r"[A-Z][a-z]", a)]  # must have capitalised word

    if not authors:
        return "", []

    if len(authors) == 1:
        short = authors[0]
    elif len(authors) == 2:
        short = f"{authors[0]} and {authors[1]}"
    else:
        # Extract last name of first author
        first_author_last = authors[0].split()[-1]
        short = f"{first_author_last} et al."

    return short, authors


def _extract_year(text: str) -> Optional[int]:
    """Extract publication year — prefer years in first 200 chars (header area)."""
    # First try header area
    header_match = _YEAR_RE.search(text[:200])
    if header_match:
        return int(header_match.group(1))
    # Fall back to anywhere in first 1000 chars
    match = _YEAR_RE.search(text[:1000])
    return int(match.group(1)) if match else None


def _extract_venue(text: str, venue_hints: List[str]) -> str:
    """
    Extract venue name.  Uses two strategies:
      1. Normalise any venue hints from paper_classifier against VENUE_MAP
      2. Look for "In Proceedings of..." / "Published in..." patterns
    """
    # Strategy 1: normalise hints
    for hint in venue_hints:
        normalised = VENUE_MAP.get(hint.lower())
        if normalised:
            return normalised

    # Strategy 2: pattern matching
    proc_match = re.search(
        r"(?:in\s+(?:proceedings|proc\.)\s+of\s+)([^\n.]{5,80})",
        text[:1000], re.I
    )
    if proc_match:
        return proc_match.group(1).strip()

    pub_match = re.search(
        r"(?:published\s+in|appearing\s+in|accepted\s+(?:at|to))\s+([^\n.]{5,80})",
        text[:1000], re.I
    )
    if pub_match:
        return pub_match.group(1).strip()

    return ""


def _extract_abstract(text: str) -> str:
    """Extract the abstract section text."""
    match = _ABSTRACT_RE.search(text[:3000])
    if not match:
        return ""
    abstract = match.group(1).strip()
    # Trim at the next all-uppercase section header or numbered section
    abstract = re.split(r"\n\s*(?:[A-Z]{2,}\s*\n|\d[\d.]*\s+[A-Z])", abstract)[0]
    abstract = abstract.strip()
    return abstract if len(abstract) >= ABSTRACT_MIN_CHARS else ""


def _extract_keywords(text: str) -> List[str]:
    """Extract author-provided keywords."""
    match = _KEYWORDS_RE.search(text[:3000])
    if not match:
        return []
    raw       = match.group(1).strip()
    # Keywords are separated by commas, semicolons, or bullets
    keywords  = re.split(r"[,;·•]\s*", raw)
    keywords  = [k.strip().rstrip(".") for k in keywords if 2 < len(k.strip()) < 60]
    return keywords[:15]


def _extract_doi(text: str) -> str:
    match = _DOI_RE.search(text)
    return match.group(0) if match else ""


def _extract_arxiv_id(text: str) -> str:
    match = _ARXIV_RE.search(text)
    return match.group(1) if match else ""


def _extract_references_section(text: str) -> List[str]:
    """
    Extract the references section and split into individual reference strings.
    Returns list of raw reference strings (consumed by citation_parser.py).
    """
    # Find where references section starts
    ref_match = re.search(
        r"\n\s*(?:\d[\d.]*\s+)?(?:references?|bibliography)\s*\n",
        text, re.I
    )
    if not ref_match:
        return []

    ref_text = text[ref_match.end():]

    # Split on reference number patterns: [1], 1., (1)
    entries = re.split(r"\n\s*(?:\[\d+\]|\d+\.|(?:\d+\)))\s+", ref_text)
    entries = [e.strip() for e in entries if len(e.strip()) > 20]

    # Cap at 300 references (safety limit)
    return entries[:300]


# ── Section map builder ───────────────────────────────────────────────────────

def _build_section_map(full_text: str) -> Dict[str, Dict[str, int]]:
    """
    Build a map of section_name → {start, end} char offsets.

    Used by the retriever to restrict search to specific sections:
      searcher.search(query, section_filter="methodology")

    Algorithm:
      1. Find all section header matches in the full text
      2. For each match, record its start offset
      3. Each section's end = next section's start
      4. Map the header text to a canonical section name
    """
    hits: List[Tuple[int, str]] = []

    for match in _SECTION_PATTERN.finditer(full_text):
        raw_name = match.group(1).strip().lower()
        raw_name = re.sub(r"\s+", " ", raw_name)

        # Find canonical name
        canonical = None
        for pattern_key, canon in SECTION_VOCAB.items():
            if re.match(pattern_key, raw_name, re.I):
                canonical = canon
                break
        if canonical:
            hits.append((match.start(), canonical))

    if not hits:
        return {}

    section_map: Dict[str, Dict[str, int]] = {}
    for i, (start, name) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(full_text)
        # If same section appears multiple times, use first occurrence
        if name not in section_map:
            section_map[name] = {"start": start, "end": end}

    return section_map


# ── Pass 2: Gemini Vision fallback ───────────────────────────────────────────

_VISION_PROMPT = """\
Extract structured metadata from this research paper's first page.
Return ONLY valid JSON with exactly these keys (use empty string or null for missing fields):

{
  "title":   "Full paper title as written",
  "authors": ["Author One", "Author Two"],
  "year":    2023,
  "venue":   "Conference or journal name (e.g. NeurIPS 2023, Nature, arXiv)",
  "doi":     "10.xxxx/xxxxx or empty string",
  "arxiv_id":"2301.00001 or empty string"
}

Rules:
- title: complete title, preserve capitalisation, no truncation
- authors: list of full names as written, max 10
- year: integer only, the publication year
- venue: short canonical name (NeurIPS, CVPR, Nature Communications, arXiv)
- Return ONLY the JSON object, no explanation, no markdown fences
"""

def _vision_extract_first_page(doc: fitz.Document) -> Dict[str, Any]:
    """
    Render first page as image and send to Gemini Vision for structured extraction.
    Returns parsed JSON dict or empty dict on failure.
    """
    if len(doc) == 0:
        return {}

    page   = doc[0]
    scale  = 150 / 72          # 150 DPI — enough for text, keeps token cost low
    matrix = fitz.Matrix(scale, scale)
    pixmap = page.get_pixmap(matrix=matrix, colorspace=fitz.csRGB, alpha=False)
    img    = Image.frombytes("RGB", [pixmap.width, pixmap.height], pixmap.samples)

    # Resize to max 1200px on long edge
    w, h       = img.size
    long_edge  = max(w, h)
    if long_edge > 1200:
        scale_factor = 1200 / long_edge
        img = img.resize((int(w * scale_factor), int(h * scale_factor)), Image.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model = genai.GenerativeModel(VISION_MODEL)

    try:
        response = model.generate_content(
            [_VISION_PROMPT, {"mime_type": "image/jpeg", "data": b64}],
            generation_config=genai.types.GenerationConfig(
                temperature=0.0,
                max_output_tokens=512,
            ),
        )
        raw = response.text.strip()
        # Strip markdown fences if model adds them despite instructions
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.M).strip()
        return json.loads(raw)
    except (json.JSONDecodeError, Exception) as exc:
        logger.warning(f"Vision metadata extraction failed: {exc}")
        return {}


# ── completeness checker + fallback merger ────────────────────────────────────

def _is_complete_enough(meta: PaperMetadata) -> bool:
    """
    Return True if regex extraction got the two most critical fields.
    If title or authors are missing, trigger Vision fallback.
    """
    return bool(meta.title.strip()) and bool(meta.authors.strip())


def _merge_vision_result(meta: PaperMetadata, vision: Dict[str, Any]) -> PaperMetadata:
    """
    Fill empty fields in meta from vision dict.
    Never overwrites a field that regex already populated.
    """
    if not meta.title and vision.get("title"):
        meta.title = str(vision["title"]).strip()

    if not meta.authors and vision.get("authors"):
        authors_list = vision["authors"]
        if isinstance(authors_list, list) and authors_list:
            meta.authors_full = [str(a) for a in authors_list]
            if len(authors_list) == 1:
                meta.authors = authors_list[0]
            elif len(authors_list) == 2:
                meta.authors = f"{authors_list[0]} and {authors_list[1]}"
            else:
                last = str(authors_list[0]).split()[-1]
                meta.authors = f"{last} et al."

    if not meta.year and vision.get("year"):
        try:
            meta.year = int(vision["year"])
        except (ValueError, TypeError):
            pass

    if not meta.venue and vision.get("venue"):
        meta.venue = str(vision["venue"]).strip()

    if not meta.doi and vision.get("doi"):
        meta.doi = str(vision["doi"]).strip()

    if not meta.arxiv_id and vision.get("arxiv_id"):
        meta.arxiv_id = str(vision["arxiv_id"]).strip()

    meta.extraction_method = "hybrid"
    return meta


# ── public API ────────────────────────────────────────────────────────────────

def extract_metadata(
    file_path: str | Path,
    paper_type: str = "empirical",
    venue_hints: Optional[List[str]] = None,
    full_text: Optional[str] = None,
) -> PaperMetadata:
    """
    Extract all structured metadata from a research paper PDF.

    Args:
        file_path:   path to the PDF (must exist on disk)
        paper_type:  from paper_classifier.ClassificationResult.paper_type
        venue_hints: from paper_classifier.ClassificationResult.venue_hints
        full_text:   pre-extracted full text (skips re-opening if provided)

    Returns:
        PaperMetadata — store this and attach .to_chunk_metadata() to each chunk

    Typical call sequence:
        classification = classify_paper(file_path)
        if classification.is_paper:
            metadata = extract_metadata(
                file_path,
                paper_type  = classification.paper_type,
                venue_hints = classification.venue_hints,
            )
            chunks = chunk_pages(pages, metadata=metadata)
    """
    file_path = Path(file_path)
    logger.info(f"Extracting metadata: {file_path.name}")

    doc = fitz.open(str(file_path))

    # ── Build full text if not provided ──────────────────────────────────────
    if full_text is None:
        pages_text = []
        for page in doc:
            pages_text.append(page.get_text("text"))
        full_text = "\n".join(pages_text)

    first_page_text = doc[0].get_text("text") if len(doc) > 0 else ""

    # ── Pass 1: Regex + font analysis ────────────────────────────────────────
    title_raw  = _extract_title_by_font(doc)
    title      = _merge_title_lines(doc, title_raw)
    authors_short, authors_full = _extract_authors(first_page_text)

    meta = PaperMetadata(
        source        = file_path.name,
        title         = title,
        authors       = authors_short,
        authors_full  = authors_full,
        year          = _extract_year(first_page_text),
        venue         = _extract_venue(first_page_text, venue_hints or []),
        doi           = _extract_doi(full_text[:2000]),
        arxiv_id      = _extract_arxiv_id(full_text[:2000]),
        abstract      = _extract_abstract(full_text),
        keywords      = _extract_keywords(full_text[:3000]),
        paper_type    = paper_type,
        section_map   = _build_section_map(full_text),
        raw_references= _extract_references_section(full_text),
        extraction_method = "regex",
    )
    meta.reference_count = len(meta.raw_references)

    # ── Pass 2: Vision fallback if title or authors missing ──────────────────
    if not _is_complete_enough(meta):
        logger.info(f"  Incomplete regex extraction — falling back to Vision")
        vision_data = _vision_extract_first_page(doc)
        if vision_data:
            meta = _merge_vision_result(meta, vision_data)

    doc.close()

    # ── Compute and log completeness ──────────────────────────────────────────
    completeness = meta.compute_completeness()

    logger.success(
        f"  Metadata extracted ({meta.extraction_method}, "
        f"completeness={completeness:.0%}): "
        f"'{meta.title[:60]}' — {meta.authors} "
        f"({meta.year or 'year?'}) — {meta.venue or 'venue?'} — "
        f"{meta.reference_count} refs — "
        f"{len(meta.section_map)} sections mapped"
    )

    return meta


def extract_metadata_from_text(
    source: str,
    full_text: str,
    first_page_text: str = "",
    paper_type: str = "empirical",
    venue_hints: Optional[List[str]] = None,
) -> PaperMetadata:
    """
    Extract metadata purely from text strings (no file access).
    Used when the document has already been through the OCR pipeline and
    the text is in memory.  Vision fallback is unavailable in this path.

    Args:
        source:          filename for the metadata record
        full_text:       full document text
        first_page_text: first page text only (improves author/year extraction)
        paper_type:      from paper_classifier
        venue_hints:     from paper_classifier

    Returns:
        PaperMetadata (extraction_method will be "regex")
    """
    if not first_page_text:
        first_page_text = full_text[:2000]

    title_text = first_page_text[:1000]
    # Heuristic: first non-blank line that's long enough is likely the title
    for line in title_text.splitlines():
        line = line.strip()
        if 15 <= len(line) <= TITLE_MAX_CHARS and not line.isupper():
            title = line
            break
    else:
        title = ""

    authors_short, authors_full = _extract_authors(first_page_text)

    meta = PaperMetadata(
        source         = source,
        title          = title,
        authors        = authors_short,
        authors_full   = authors_full,
        year           = _extract_year(first_page_text),
        venue          = _extract_venue(first_page_text, venue_hints or []),
        doi            = _extract_doi(full_text[:2000]),
        arxiv_id       = _extract_arxiv_id(full_text[:2000]),
        abstract       = _extract_abstract(full_text),
        keywords       = _extract_keywords(full_text[:3000]),
        paper_type     = paper_type,
        section_map    = _build_section_map(full_text),
        raw_references = _extract_references_section(full_text),
        extraction_method = "regex",
    )
    meta.reference_count = len(meta.raw_references)
    meta.compute_completeness()
    return meta