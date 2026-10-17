"""
paper_classifier.py  —  Research paper gate for ResearchMind.

Every PDF uploaded to ResearchMind passes through this module FIRST.
It answers two questions:
  1. Is this actually a research paper?  (gate decision)
  2. If yes, what kind?                  (paper type classification)

Why this matters
────────────────
Without a gate, users can upload a contract, a manual, or a slide deck
and get meaningless "research insights" output.  The gate makes ResearchMind
a domain tool, not a generic RAG wrapper.  A clean rejection message with a
reason ("No references section found") is a feature, not an error.

Detection strategy — five signal categories
────────────────────────────────────────────
No single signal reliably identifies a research paper.  A contract can have
section headers.  A book can have references.  We score five independent
categories and require a weighted sum above a threshold.

  1. STRUCTURAL signals   — section headers present (abstract, introduction,
                            methodology, results, conclusion, references)
  2. BIBLIOGRAPHIC signals— reference list patterns, citation markers [1] or
                            (Author, Year), DOI patterns, arXiv IDs
  3. VENUE signals        — known conference/journal names in header/footer
                            (NeurIPS, CVPR, ICML, Nature, IEEE, ACM, AAAI…)
  4. METADATA signals     — author line patterns, affiliation patterns,
                            "corresponding author", "received / accepted" dates
  5. CONTENT signals      — academic vocabulary density (methodology, empirical,
                            hypothesis, ablation, baseline, benchmark, corpus…)

Scoring: each category contributes 0.0–1.0; weighted average must exceed
PAPER_CONFIDENCE_THRESHOLD (default 0.60) to pass.

Paper type classification (runs only if paper passes gate)
──────────────────────────────────────────────────────────
  survey       — "survey", "review", "overview", "systematic review"
  empirical    — has experiments section + datasets + metrics
  theoretical  — heavy on proofs, theorems, propositions; few experiments
  position     — short, opinion-heavy, "we argue", "position paper"
  dataset      — primary contribution is a dataset/benchmark
  system       — describes a deployed system or tool ("we present X system")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from loguru import logger


# ── configuration ─────────────────────────────────────────────────────────────

# Weighted confidence threshold — below this the PDF is rejected
PAPER_CONFIDENCE_THRESHOLD: float = 0.60

# How many pages to scan (first + last cover most signals)
FIRST_PAGES: int = 3
LAST_PAGES:  int = 2

# Category weights (must sum to 1.0)
WEIGHTS: Dict[str, float] = {
    "structural":   0.30,   # section headers — strongest single signal
    "bibliographic": 0.28,  # references — nearly definitive
    "venue":        0.18,   # conference/journal name
    "metadata":     0.14,   # author/affiliation lines
    "content":      0.10,   # academic vocabulary
}


# ── signal pattern libraries ──────────────────────────────────────────────────

# Section headers — case-insensitive, allow numbering prefix (1. Introduction)
SECTION_PATTERNS: List[re.Pattern] = [
    re.compile(r"^\s*(?:\d[\d.]*\s+)?abstract\b",         re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?introduction\b",     re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?related\s+work\b",   re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?background\b",       re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?method(?:s|ology)?\b", re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?approach\b",         re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?experiment(?:s|al)?\b", re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?evaluation\b",       re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?results?\b",         re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?discussion\b",       re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?conclusion(?:s)?\b", re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?limitation(?:s)?\b", re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?future\s+work\b",    re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?references?\b",      re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?bibliography\b",     re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?acknowledgements?\b",re.I | re.M),
    re.compile(r"^\s*(?:\d[\d.]*\s+)?appendix\b",         re.I | re.M),
]

# Bibliographic patterns
BIB_PATTERNS: List[re.Pattern] = [
    re.compile(r"\[\d{1,3}\]"),                                       # [1], [23]
    re.compile(r"\(\w[\w\s,]+,\s*\d{4}[a-z]?\)"),                   # (Smith, 2021)
    re.compile(r"\b10\.\d{4,}/\S+"),                                  # DOI
    re.compile(r"arxiv[:\s]\d{4}\.\d{4,5}", re.I),                   # arXiv:2301.00001
    re.compile(r"et al\.", re.I),                                     # et al.
    re.compile(r"(?:pp?\.)\s*\d+[–\-]\d+"),                          # pp. 123–456
    re.compile(r"(?:vol|volume|no|issue)\.\s*\d+", re.I),            # vol. 3
    re.compile(r"(?:proceedings|proc\.)\s+of\b", re.I),              # Proceedings of
    re.compile(r"(?:journal|j\.)\s+of\b", re.I),                     # Journal of
    re.compile(r"(?:ieee|acm)\s+transactions?\b", re.I),             # IEEE Transactions
]

# Known venue names (abbreviated for pattern matching)
VENUE_KEYWORDS: List[str] = [
    "neurips", "nips", "icml", "iclr", "cvpr", "iccv", "eccv",
    "acl", "emnlp", "naacl", "coling", "aaai", "ijcai", "uai",
    "kdd", "www", "sigir", "cikm", "wsdm", "recsys",
    "icra", "iros", "rss", "corl",
    "nature", "science", "cell", "plos",
    "ieee transactions", "acm transactions", "springer",
    "arxiv", "biorxiv", "medrxiv", "preprint",
    "workshop on", "symposium on", "conference on",
    "advances in", "proceedings of",
]
VENUE_RE = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in VENUE_KEYWORDS) + r")\b",
    re.I
)

# Author / metadata patterns
METADATA_PATTERNS: List[re.Pattern] = [
    re.compile(r"corresponding\s+author", re.I),
    re.compile(r"@\w+\.\w+"),                                   # email address
    re.compile(r"(?:university|institute|lab(?:oratory)?|dept\.?|department)\s+of\b", re.I),
    re.compile(r"(?:received|accepted|published)\s*:?\s*\d+", re.I),
    re.compile(r"\borcid\b", re.I),
    re.compile(r"\baffiliation", re.I),
    re.compile(r"(?:equal\s+contribution|contributed\s+equally)", re.I),
    re.compile(r"(?:funded\s+by|supported\s+by|grant\s+no\.?)", re.I),
]

# Academic content vocabulary
ACADEMIC_VOCAB: List[str] = [
    "hypothesis", "empirical", "ablation", "baseline", "benchmark",
    "corpus", "dataset", "annotation", "evaluation", "metric",
    "outperform", "state.of.the.art", "sota", "fine.tun",
    "pretrain", "downstream", "inference", "convergence",
    "accuracy", "precision", "recall", "f1", "f-measure",
    "statistical", "significance", "p-value", "variance",
    "algorithm", "complexity", "theorem", "proposition", "proof",
    "architecture", "layer", "embedding", "attention", "transformer",
    "regression", "classification", "clustering", "generative",
    "sampling", "distribution", "likelihood", "posterior",
    "gradient", "loss function", "objective", "optimization",
]
ACADEMIC_RE = re.compile(
    r"\b(" + "|".join(academic_word.replace(".", r"\S") for academic_word in ACADEMIC_VOCAB) + r")\b",
    re.I,
)

# Survey / type signals
PAPER_TYPE_PATTERNS: Dict[str, List[re.Pattern]] = {
    "survey": [
        re.compile(r"\bsurvey\b", re.I),
        re.compile(r"\bliterature\s+review\b", re.I),
        re.compile(r"\bsystematic\s+review\b", re.I),
        re.compile(r"\boverview\s+of\b", re.I),
        re.compile(r"\bsummary\s+of\b", re.I),
    ],
    "dataset": [
        re.compile(r"\bwe\s+(?:introduce|present|release|propose)\s+(?:a\s+)?(?:new\s+)?(?:large[- ]scale\s+)?dataset\b", re.I),
        re.compile(r"\bbenchmark\s+dataset\b", re.I),
        re.compile(r"\bdata\s+collection\b", re.I),
        re.compile(r"\bannotation\s+(?:pipeline|process|schema)\b", re.I),
    ],
    "position": [
        re.compile(r"\bposition\s+paper\b", re.I),
        re.compile(r"\bwe\s+argue\s+that\b", re.I),
        re.compile(r"\bwe\s+posit\s+that\b", re.I),
        re.compile(r"\bopinionated\b", re.I),
    ],
    "theoretical": [
        re.compile(r"\btheorem\s+\d+\b", re.I),
        re.compile(r"\bproposition\s+\d+\b", re.I),
        re.compile(r"\blemma\s+\d+\b", re.I),
        re.compile(r"\bcorollary\s+\d+\b", re.I),
        re.compile(r"\bproof\s+(?:of|sketch|by)\b", re.I),
    ],
    "system": [
        re.compile(r"\bwe\s+(?:present|describe|introduce|deploy)\s+(?:a\s+)?(?:new\s+)?system\b", re.I),
        re.compile(r"\bproduction\s+system\b", re.I),
        re.compile(r"\bend.to.end\s+system\b", re.I),
        re.compile(r"\bdeployed\s+(?:at|in)\b", re.I),
    ],
}

# Non-paper rejection signals — strong negative indicators
REJECTION_PATTERNS: Dict[str, re.Pattern] = {
    "contract":      re.compile(r"\b(?:whereas|hereinafter|indemnif|licensor|licensee|termination\s+clause)\b", re.I),
    "legal":         re.compile(r"\b(?:plaintiff|defendant|jurisdiction|pursuant\s+to|hereby\s+agree)\b", re.I),
    "manual":        re.compile(r"\b(?:installation\s+guide|step\s+\d+:|click\s+(?:ok|next|cancel)|figure\s+\d+[.:]\s*screenshot)\b", re.I),
    "financial":     re.compile(r"\b(?:fiscal\s+year|earnings\s+per\s+share|balance\s+sheet|cash\s+flow\s+statement)\b", re.I),
    "presentation":  re.compile(r"\b(?:slide\s+\d+|click\s+to\s+edit|bullet\s+point|agenda\s+for\s+today)\b", re.I),
}


# ── result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ClassificationResult:
    """
    Full result from classify_paper().
    Passed to metadata_extractor and the API route layer.
    """
    is_paper:          bool
    confidence:        float                    # 0.0–1.0
    paper_type:        Optional[str]            # survey | empirical | theoretical | position | dataset | system
    rejection_reason:  Optional[str]            # human-readable if is_paper=False

    # Per-category scores (useful for debugging and UI display)
    category_scores:   Dict[str, float]         = field(default_factory=dict)

    # Signals found (for transparency / audit trail)
    sections_found:    List[str]                = field(default_factory=list)
    venue_hints:       List[str]                = field(default_factory=list)
    has_references:    bool                     = False
    page_count:        int                      = 0

    def summary(self) -> str:
        if self.is_paper:
            return (
                f"{self.paper_type or 'empirical'} paper "
                f"(confidence: {self.confidence:.0%}) — "
                f"sections: {', '.join(self.sections_found[:4])}"
            )
        return f"Rejected: {self.rejection_reason} (confidence: {self.confidence:.0%})"


# ── text extraction helpers ───────────────────────────────────────────────────

def _extract_scan_text(doc: fitz.Document) -> str:
    """
    Extract text from first FIRST_PAGES + last LAST_PAGES for signal scanning.
    Middle pages are skipped — classifier only needs structure signals,
    not full content.
    """
    total  = len(doc)
    pages  = list(range(min(FIRST_PAGES, total)))
    if total > FIRST_PAGES:
        last_start = max(FIRST_PAGES, total - LAST_PAGES)
        pages += list(range(last_start, total))

    texts = []
    for i in pages:
        page = doc[i]
        texts.append(page.get_text("text"))
    return "\n".join(texts)


def _extract_full_text(doc: fitz.Document) -> str:
    """
    Full document text — used only for content vocabulary scoring.
    Sampled (every 3rd page) to keep classification fast.
    """
    texts = []
    for i, page in enumerate(doc):
        if i % 3 == 0:
            texts.append(page.get_text("text"))
    return "\n".join(texts)


# ── per-category scorers ──────────────────────────────────────────────────────

def _score_structural(text: str) -> Tuple[float, List[str]]:
    """Score based on presence of standard paper section headers."""
    found = []
    for pat in SECTION_PATTERNS:
        match = pat.search(text)
        if match:
            section_name = match.group(0).strip().lower()
            section_name = re.sub(r"^\d[\d.]*\s*", "", section_name)
            found.append(section_name)

    # Deduplicate, keep order
    seen    = set()
    unique  = [s for s in found if not (s in seen or seen.add(s))]

    # Scoring: ≥4 sections = full score; scale linearly below
    # Core sections (abstract + references) each add 0.15 bonus
    score   = min(len(unique) / 4.0, 1.0)
    if any("abstract" in s for s in unique):   score = min(score + 0.15, 1.0)
    if any("reference" in s or "bibliography" in s for s in unique): score = min(score + 0.15, 1.0)

    return round(score, 3), unique


def _score_bibliographic(text: str) -> Tuple[float, bool]:
    """Score based on citation markers and reference list patterns."""
    matches = sum(1 for pat in BIB_PATTERNS if pat.search(text))
    score   = min(matches / len(BIB_PATTERNS), 1.0)

    # Bonus: dedicated references section heading is strong evidence
    has_ref_section = bool(
        re.search(r"^\s*(?:\d[\d.]*\s+)?(?:references?|bibliography)\s*$", text, re.I | re.M)
    )
    if has_ref_section:
        score = min(score + 0.25, 1.0)

    return round(score, 3), has_ref_section


def _score_venue(text: str) -> Tuple[float, List[str]]:
    """Score based on known conference / journal names."""
    hits  = VENUE_RE.findall(text)
    unique = list({h.lower() for h in hits})
    score = min(len(unique) / 3.0, 1.0)  # 3+ distinct venue hints = full score
    return round(score, 3), unique[:6]


def _score_metadata(text: str) -> float:
    """Score based on author / affiliation / funding patterns."""
    matches = sum(1 for pat in METADATA_PATTERNS if pat.search(text))
    return round(min(matches / len(METADATA_PATTERNS), 1.0), 3)


def _score_content(text: str) -> float:
    """Score based on academic vocabulary density."""
    hits   = ACADEMIC_RE.findall(text)
    words  = len(text.split())
    if words == 0:
        return 0.0
    density = len(hits) / words
    # 1% density = full score (typical academic text is 2–5%)
    return round(min(density / 0.01, 1.0), 3)


# ── rejection checker ─────────────────────────────────────────────────────────

def _check_rejections(text: str) -> Optional[str]:
    """
    Return a human-readable reason string if strong non-paper signals are found,
    or None if no clear disqualifier.
    """
    hits = {name: pat.search(text) for name, pat in REJECTION_PATTERNS.items() if pat.search(text)}
    if not hits:
        return None
    doc_type = max(hits.keys(), key=lambda k: len(hits[k].group(0)))
    return f"Document appears to be a {doc_type} rather than a research paper"


# ── paper type classifier ─────────────────────────────────────────────────────

def _classify_type(text: str) -> str:
    """
    Classify into one of: survey | dataset | position | theoretical | system | empirical.
    Empirical is the default fallback.
    """
    scores = {}
    for ptype, patterns in PAPER_TYPE_PATTERNS.items():
        score = sum(1 for pat in patterns if pat.search(text))
        if score > 0:
            scores[ptype] = score

    if not scores:
        return "empirical"

    best = max(scores, key=scores.get)
    # Require at least 2 matching patterns for non-empirical types
    # (single match could be coincidental)
    return best if scores[best] >= 2 else "empirical"


# ── public API ────────────────────────────────────────────────────────────────

def classify_paper(file_path: str | Path) -> ClassificationResult:
    """
    Classify a PDF as a research paper or not.

    Runs entirely on PyMuPDF text extraction (no OCR, no API calls) for speed.
    Typical runtime: 50–200ms per paper.

    Args:
        file_path: path to the PDF file (must already be saved to disk)

    Returns:
        ClassificationResult with is_paper, confidence, paper_type, and
        detailed per-category scores for debugging / audit trail.

    Example:
        result = classify_paper("uploads/attention_is_all_you_need.pdf")
        if not result.is_paper:
            return {"error": result.rejection_reason}
        # → proceed with metadata extraction and ingestion
    """
    file_path = Path(file_path)
    if not file_path.exists():
        raise FileNotFoundError(f"PDF not found: {file_path}")

    logger.info(f"Classifying: {file_path.name}")

    try:
        doc = fitz.open(str(file_path))
    except Exception as exc:
        raise ValueError(f"Cannot open PDF: {exc}") from exc

    page_count = len(doc)

    # ── 1. Extract scan text ──────────────────────────────────────────────────
    scan_text  = _extract_scan_text(doc)
    full_text  = _extract_full_text(doc)

    # ── 2. Check for hard rejections first (fast exit) ───────────────────────
    rejection = _check_rejections(scan_text)
    if rejection:
        doc.close()
        logger.info(f"  Rejected: {rejection}")
        return ClassificationResult(
            is_paper         = False,
            confidence       = 0.0,
            paper_type       = None,
            rejection_reason = rejection,
            page_count       = page_count,
        )

    # ── 3. Score each category ────────────────────────────────────────────────
    struct_score,  sections_found = _score_structural(scan_text)
    bib_score,     has_references = _score_bibliographic(scan_text + full_text[-3000:])
    venue_score,   venue_hints    = _score_venue(scan_text)
    meta_score                    = _score_metadata(scan_text)
    content_score                 = _score_content(full_text[:5000])  # sample for speed

    category_scores = {
        "structural":    struct_score,
        "bibliographic": bib_score,
        "venue":         venue_score,
        "metadata":      meta_score,
        "content":       content_score,
    }

    # ── 4. Weighted confidence ────────────────────────────────────────────────
    confidence = round(
        sum(category_scores[cat] * weight for cat, weight in WEIGHTS.items()), 3
    )

    # ── 5. Gate decision ──────────────────────────────────────────────────────
    is_paper = confidence >= PAPER_CONFIDENCE_THRESHOLD

    # Build rejection reason if failing — prioritise the biggest deficit
    rejection_reason = None
    if not is_paper:
        deficits = sorted(category_scores.items(), key=lambda x: x[1])
        worst    = deficits[0]
        messages = {
            "structural":    "No standard paper section headers found (abstract, introduction, methods, etc.)",
            "bibliographic": "No references section or citation markers found",
            "venue":         "No academic venue or journal name detected",
            "metadata":      "No author or affiliation information found",
            "content":       "Insufficient academic vocabulary — may not be a research paper",
        }
        rejection_reason = messages.get(worst[0], "Does not match research paper structure")
        logger.info(f"  Not a paper (conf={confidence:.2f}): {rejection_reason}")

    # ── 6. Paper type (only if passed gate) ──────────────────────────────────
    paper_type = _classify_type(full_text) if is_paper else None

    doc.close()

    result = ClassificationResult(
        is_paper         = is_paper,
        confidence       = confidence,
        paper_type       = paper_type,
        rejection_reason = rejection_reason,
        category_scores  = category_scores,
        sections_found   = sections_found,
        venue_hints      = venue_hints,
        has_references   = has_references,
        page_count       = page_count,
    )

    logger.success(f"  {result.summary()}")
    return result


def classify_paper_from_text(text: str, page_count: int = 0) -> ClassificationResult:
    """
    Classify from already-extracted text (e.g. after OCR).
    Used when the PDF has already been through the OCR pipeline and
    we don't want to re-open the file.

    Args:
        text:       full extracted text of the document
        page_count: number of pages (for metadata)

    Returns:
        ClassificationResult (same shape as classify_paper)
    """
    rejection = _check_rejections(text[:3000])
    if rejection:
        return ClassificationResult(
            is_paper=False, confidence=0.0, paper_type=None,
            rejection_reason=rejection, page_count=page_count,
        )

    struct_score,  sections_found = _score_structural(text)
    bib_score,     has_references = _score_bibliographic(text)
    venue_score,   venue_hints    = _score_venue(text[:3000])
    meta_score                    = _score_metadata(text[:3000])
    content_score                 = _score_content(text[:5000])

    category_scores = {
        "structural":    struct_score,
        "bibliographic": bib_score,
        "venue":         venue_score,
        "metadata":      meta_score,
        "content":       content_score,
    }
    confidence = round(
        sum(category_scores[cat] * weight for cat, weight in WEIGHTS.items()), 3
    )
    is_paper       = confidence >= PAPER_CONFIDENCE_THRESHOLD
    paper_type     = _classify_type(text) if is_paper else None
    rejection_reason = None
    if not is_paper:
        deficits       = sorted(category_scores.items(), key=lambda x: x[1])
        messages = {
            "structural":    "No standard paper section headers found",
            "bibliographic": "No references section or citation markers found",
            "venue":         "No academic venue detected",
            "metadata":      "No author or affiliation information found",
            "content":       "Insufficient academic vocabulary",
        }
        rejection_reason = messages.get(deficits[0][0], "Does not match research paper structure")

    return ClassificationResult(
        is_paper=is_paper, confidence=confidence, paper_type=paper_type,
        rejection_reason=rejection_reason, category_scores=category_scores,
        sections_found=sections_found, venue_hints=venue_hints,
        has_references=has_references, page_count=page_count,
    )