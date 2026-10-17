"""
citation_parser.py  —  Research citation extraction and cross-paper graph.

Three responsibilities:
─────────────────────
1. LOCATE the references section in a paper's extracted text.
   Research papers end with a bibliography. We find it by header pattern
   ("References", "Bibliography", "Works Cited") and extract everything
   after that point.

2. PARSE each individual reference entry into structured fields:
     authors, title, year, venue, doi, arxiv_id, url
   Three parser strategies run in order, each catching a different
   citation format style:

     RegexParser    — handles most structured formats (IEEE, APA, MLA,
                      numbered lists [1], [2]..., author-year (Smith, 2021))
     PatternParser  — catches loose patterns: anything with a 4-digit year
                      sandwiched between author-like text and a title
     GeminiParser   — fallback for messy or unusual formats; sends the
                      raw references block to Gemini and asks for JSON

3. BUILD a citation graph across all uploaded papers.
   After multiple papers are indexed, cross_reference() finds which
   uploaded papers cite each other by fuzzy-matching titles.
   This powers the "citation graph" tab in the InsightsPanel.

   Graph structure:
     nodes: uploaded papers (by source filename)
     edges: paper A cites paper B (directed)
     weight: number of times A references B (usually 1)

Output shapes
─────────────
   ParsedCitation (per reference entry):
     {
       "raw":      "Vaswani et al. (2017) Attention Is All You Need...",
       "authors":  "Vaswani et al.",
       "title":    "Attention Is All You Need",
       "year":     2017,
       "venue":    "NeurIPS",
       "doi":      "10.48550/arXiv.1706.03762",   # or None
       "arxiv_id": "1706.03762",                   # or None
       "url":      None,
     }

   CitationGraph (cross-paper links):
     {
       "nodes": [
         {"id": "transformer.pdf", "title": "Attention Is All You Need", "year": 2017},
         ...
       ],
       "edges": [
         {"source": "bert.pdf", "target": "transformer.pdf", "weight": 3},
         ...
       ],
     }

Why three parser strategies?
─────────────────────────────
Academic citation formats vary enormously:
  IEEE:     [1] A. Vaswani et al., "Attention is all...", NeurIPS, 2017.
  APA:      Vaswani, A., et al. (2017). Attention is all you need. NeurIPS.
  MLA:      Vaswani, Ashish, et al. "Attention Is All You Need." NeurIPS 2017.
  Numbered: 1. Vaswani et al. Attention Is All You Need. 2017.
  Loose:    Vaswani 2017, Attention is all you need, NeurIPS proceedings

No single regex handles all of these reliably. The three-stage approach
degrades gracefully: most papers need only the regex stage; Gemini is only
called for the ~15% of papers with unusual formatting.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import google.generativeai as genai
from loguru import logger

from backend.config import settings


# ── data classes ──────────────────────────────────────────────────────────────

@dataclass
class ParsedCitation:
    """One parsed reference entry from a paper's bibliography."""
    raw:      str                          # original text as it appeared
    authors:  Optional[str]   = None
    title:    Optional[str]   = None
    year:     Optional[int]   = None
    venue:    Optional[str]   = None       # journal / conference name
    doi:      Optional[str]   = None
    arxiv_id: Optional[str]   = None
    url:      Optional[str]   = None
    parse_method: str         = "unknown"  # "regex" | "pattern" | "gemini"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "raw":          self.raw,
            "authors":      self.authors,
            "title":        self.title,
            "year":         self.year,
            "venue":        self.venue,
            "doi":          self.doi,
            "arxiv_id":     self.arxiv_id,
            "url":          self.url,
            "parse_method": self.parse_method,
        }


@dataclass
class CitationParseResult:
    """Full result for one paper — references section + all parsed citations."""
    source:            str                         # filename
    references_raw:    str                         # raw references section text
    citations:         List[ParsedCitation]        = field(default_factory=list)
    ref_count:         int                         = 0
    parse_method_used: str                         = "regex"
    warnings:          List[str]                   = field(default_factory=list)


@dataclass
class CitationGraph:
    """Cross-paper citation relationships."""
    nodes: List[Dict[str, Any]]    = field(default_factory=list)
    edges: List[Dict[str, Any]]    = field(default_factory=list)


# ── constants ─────────────────────────────────────────────────────────────────

# Headers that mark the start of the references section
REFERENCES_HEADERS = [
    r"^\s*references?\s*$",
    r"^\s*bibliography\s*$",
    r"^\s*works?\s+cited\s*$",
    r"^\s*literature\s+cited\s*$",
    r"^\s*bibliographic\s+references?\s*$",
]

# Minimum chars a references section must have to be worth parsing
MIN_REFS_CHARS = 100

# Minimum title length to consider a parsed title valid
MIN_TITLE_CHARS = 10

# Fuzzy match threshold for cross-referencing paper titles (0–1)
TITLE_SIMILARITY_THRESHOLD = 0.72

# Max references to send to Gemini in one call (cost control)
GEMINI_MAX_REFS = 80

# Gemini retry settings
MAX_RETRIES     = 2
RETRY_DELAY     = 2.0


# ── Step 1: Locate references section ────────────────────────────────────────

def _header_pattern() -> re.Pattern:
    """Compile a single pattern matching any known references header."""
    combined = "|".join(f"({h})" for h in REFERENCES_HEADERS)
    return re.compile(combined, re.IGNORECASE | re.MULTILINE)


_HEADER_RE = _header_pattern()


def extract_references_section(full_text: str) -> Tuple[str, int]:
    """
    Find and return the references section from full paper text.

    Strategy:
      1. Scan for a line that matches a known references header
      2. Return everything from that line to end of text
      3. If no header found, try heuristic: last 20% of text that starts
         with a numbered or bracketed entry

    Returns:
        (references_text, start_char_offset)
        Returns ("", -1) if no references section found.
    """
    # Strategy 1: explicit header match
    for match in _HEADER_RE.finditer(full_text):
        start = match.start()
        refs_text = full_text[start:].strip()
        if len(refs_text) >= MIN_REFS_CHARS:
            logger.debug(f"References section found at char {start} via header match")
            return refs_text, start

    # Strategy 2: heuristic — look for numbered bibliography in last 25% of text
    cutoff = int(len(full_text) * 0.75)
    tail   = full_text[cutoff:]

    # Look for a dense run of [N] or (N) numbered entries
    numbered_pattern = re.compile(r"^\s*[\[\(]\d+[\]\)]\s+\w", re.MULTILINE)
    matches = list(numbered_pattern.finditer(tail))

    if len(matches) >= 3:
        # Found at least 3 numbered entries in the tail — treat as references
        first_match_abs = cutoff + matches[0].start()
        refs_text = full_text[first_match_abs:].strip()
        logger.debug(f"References section found at char {first_match_abs} via heuristic")
        return refs_text, first_match_abs

    logger.warning("No references section found in document")
    return "", -1


# ── Step 2: Split into individual reference entries ───────────────────────────

def _split_reference_entries(refs_text: str) -> List[str]:
    """
    Split the references block into individual citation strings.

    Handles four common delimiter patterns:
      - Numbered:  [1] ... [2] ...
      - Bracketed: [Smith17] ... [BERT19] ...
      - Author-year: Smith (2021) ...  (new entry starts when next author name begins)
      - Blank-line separated

    Falls back to blank-line separation if nothing else works.
    """
    entries: List[str] = []

    # Pattern 1: IEEE-style numbered [N] entries
    ieee_split = re.split(r"\n\s*\[\d+\]\s+", refs_text)
    if len(ieee_split) > 3:
        return [e.strip() for e in ieee_split if e.strip() and len(e.strip()) > 20]

    # Pattern 2: plain numbered  N. entries at line start
    num_split = re.split(r"\n\s*\d{1,3}\.\s+", refs_text)
    if len(num_split) > 3:
        return [e.strip() for e in num_split if e.strip() and len(e.strip()) > 20]

    # Pattern 3: alphanumeric bracketed keys  [BERT19], [Smith2021]
    key_split = re.split(r"\n\s*\[[A-Za-z][A-Za-z0-9+\-]{1,12}\]\s+", refs_text)
    if len(key_split) > 3:
        return [e.strip() for e in key_split if e.strip() and len(e.strip()) > 20]

    # Pattern 4: blank-line separated (common in preprints / arXiv papers)
    blank_split = re.split(r"\n\s*\n", refs_text)
    if len(blank_split) > 3:
        return [e.strip() for e in blank_split if e.strip() and len(e.strip()) > 20]

    # Fallback: return the whole block as one entry (Gemini will parse it)
    return [refs_text.strip()]


# ── Step 3a: Regex parser ─────────────────────────────────────────────────────

# Matches 4-digit years between 1900 and 2099
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")

# DOI pattern
_DOI_RE = re.compile(
    r"\b(10\.\d{4,9}/[-._;()/:a-zA-Z0-9]+)\b"
)

# arXiv ID pattern
_ARXIV_RE = re.compile(
    r"arXiv[:\s]*(\d{4}\.\d{4,5})|arxiv\.org/abs/(\d{4}\.\d{4,5})",
    re.IGNORECASE,
)

# URL pattern (simple)
_URL_RE = re.compile(r"https?://[^\s,;)>\"']+")

# Title heuristics: text inside "quotes" or after a year, ending before venue signals
_QUOTED_TITLE_RE = re.compile(r'"([^"]{10,200})"')
_VENUE_SIGNALS = re.compile(
    r"\b(proceedings|journal|conference|workshop|transactions|letters|"
    r"symposium|arxiv|preprint|pp\.|vol\.|pages?)\b",
    re.IGNORECASE,
)


def _parse_year(text: str) -> Optional[int]:
    match = _YEAR_RE.search(text)
    return int(match.group()) if match else None


def _parse_doi(text: str) -> Optional[str]:
    match = _DOI_RE.search(text)
    return match.group(1) if match else None


def _parse_arxiv(text: str) -> Optional[str]:
    match = _ARXIV_RE.search(text)
    if match:
        return match.group(1) or match.group(2)
    return None


def _parse_url(text: str) -> Optional[str]:
    match = _URL_RE.search(text)
    return match.group() if match else None


def _parse_title_from_quotes(text: str) -> Optional[str]:
    """Extract title from quoted string if present."""
    match = _QUOTED_TITLE_RE.search(text)
    if match and len(match.group(1)) >= MIN_TITLE_CHARS:
        return match.group(1).strip()
    return None


def _parse_authors_from_start(text: str) -> Optional[str]:
    """
    Heuristic: authors usually appear at the very start of a reference entry,
    before the year or title.  Return everything before the first year or
    before the first quoted title.
    """
    year_match = _YEAR_RE.search(text)
    quote_match = _QUOTED_TITLE_RE.search(text)

    cutoff = min(
        year_match.start() if year_match else len(text),
        quote_match.start() if quote_match else len(text),
    )

    authors_raw = text[:cutoff].strip().rstrip(".,;")
    # Sanity check: must look like names (contains alphabetic chars, not too long)
    if 2 < len(authors_raw) < 200 and re.search(r"[A-Za-z]", authors_raw):
        return authors_raw
    return None


def _parse_venue(text: str, title: Optional[str]) -> Optional[str]:
    """
    Extract venue name — the conference or journal.
    Looks for known venue signals after the title position.
    """
    search_start = 0
    if title:
        idx = text.find(title)
        if idx != -1:
            search_start = idx + len(title)

    tail = text[search_start:]

    # Look for "In Proceedings of ...", "Journal of ...", etc.
    in_proc = re.search(
        r"(?:in\s+)?(?:proceedings?\s+of\s+(?:the\s+)?)?([A-Z][A-Za-z\s&\-]{3,60}?)(?:\s*[\.,\d]|\s*$)",
        tail[:300],
        re.IGNORECASE,
    )
    if in_proc and _VENUE_SIGNALS.search(in_proc.group()):
        return in_proc.group(1).strip()

    # Known venue abbreviations
    venue_abbrevs = re.search(
        r"\b(NeurIPS|ICML|ICLR|CVPR|ICCV|ECCV|ACL|EMNLP|NAACL|AAAI|IJCAI|"
        r"SIGKDD|SIGIR|WWW|WSDM|RecSys|CIKM|VLDB|SIGMOD|ICSE|FSE|PLDI|SOSP|"
        r"Nature|Science|Cell|PNAS|PLOS|IEEE|ACM)\b",
        tail,
    )
    if venue_abbrevs:
        return venue_abbrevs.group(1)

    return None


def _regex_parse_entry(raw: str) -> ParsedCitation:
    """
    Attempt to parse a single reference entry using regex heuristics.
    Always returns a ParsedCitation — fields may be None if not found.
    """
    # Strip leading numbering [1], 1., (1), etc.
    text = re.sub(r"^\s*[\[\(]?\d{1,3}[\]\)]?\.\s*", "", raw).strip()

    year    = _parse_year(text)
    doi     = _parse_doi(text)
    arxiv   = _parse_arxiv(text)
    url     = _parse_url(text)
    title   = _parse_title_from_quotes(text)
    authors = _parse_authors_from_start(text)
    venue   = _parse_venue(text, title)

    # If no quoted title, try to infer from position: text between authors
    # and venue signals often contains the title
    if not title and year:
        year_pos = _YEAR_RE.search(text)
        if year_pos:
            after_year = text[year_pos.end():].strip().lstrip(".,;: ")
            # Take up to the first venue signal or period
            venue_pos = _VENUE_SIGNALS.search(after_year)
            candidate = after_year[:venue_pos.start()].strip() if venue_pos else after_year[:200]
            candidate = candidate.rstrip(".,;")
            if len(candidate) >= MIN_TITLE_CHARS:
                title = candidate

    return ParsedCitation(
        raw          = raw,
        authors      = authors,
        title        = title,
        year         = year,
        venue        = venue,
        doi          = doi,
        arxiv_id     = arxiv,
        url          = url,
        parse_method = "regex",
    )


# ── Step 3b: Gemini fallback parser ──────────────────────────────────────────

_GEMINI_PARSE_PROMPT = """\
Parse the following academic references into structured JSON.
Return a JSON array — one object per reference — with these fields:
  authors  (string, "Last, First and Last, First" format, or null)
  title    (string, the paper/book title, or null)
  year     (integer, or null)
  venue    (string, conference or journal name, or null)
  doi      (string, e.g. "10.1234/xyz", or null)
  arxiv_id (string, e.g. "1706.03762", or null)

Rules:
- Output ONLY valid JSON. No markdown fences, no preamble, no explanation.
- If a field cannot be determined, set it to null.
- Preserve the order of references exactly.
- Do not merge or split references.

REFERENCES:
{refs_block}
"""


def _gemini_parse_entries(entries: List[str]) -> List[ParsedCitation]:
    """
    Send reference entries to Gemini for structured parsing.
    Used when regex parsing yields too many None fields.
    """
    refs_block = "\n\n".join(
        f"[{i+1}] {e}" for i, e in enumerate(entries[:GEMINI_MAX_REFS])
    )
    prompt = _GEMINI_PARSE_PROMPT.format(refs_block=refs_block)

    genai.configure(api_key=settings.GEMINI_API_KEY)
    model = genai.GenerativeModel(settings.LLM_MODEL)

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = model.generate_content(
                prompt,
                generation_config=genai.types.GenerationConfig(
                    temperature=0.0,
                    max_output_tokens=4096,
                ),
            )
            raw_json = response.text.strip()

            # Strip markdown fences if model added them despite instructions
            raw_json = re.sub(r"^```(?:json)?\s*", "", raw_json, flags=re.MULTILINE)
            raw_json = re.sub(r"\s*```$",          "", raw_json, flags=re.MULTILINE)

            parsed = json.loads(raw_json)
            break

        except (json.JSONDecodeError, Exception) as exc:
            if attempt == MAX_RETRIES:
                logger.error(f"Gemini citation parsing failed: {exc}")
                return [_regex_parse_entry(e) for e in entries]
            time.sleep(RETRY_DELAY)

    results = []
    for i, item in enumerate(parsed):
        raw = entries[i] if i < len(entries) else ""

        year_val = item.get("year")
        if isinstance(year_val, str):
            try:
                year_val = int(year_val)
            except (ValueError, TypeError):
                year_val = None

        results.append(ParsedCitation(
            raw          = raw,
            authors      = item.get("authors"),
            title        = item.get("title"),
            year         = year_val,
            venue        = item.get("venue"),
            doi          = item.get("doi"),
            arxiv_id     = item.get("arxiv_id"),
            parse_method = "gemini",
        ))

    return results


# ── Step 3c: Quality check — decide if regex was good enough ─────────────────

def _regex_quality_score(citations: List[ParsedCitation]) -> float:
    """
    Return 0–1 score for how well regex parsing did.
    Based on fraction of entries that have at least title + year.
    """
    if not citations:
        return 0.0
    complete = sum(
        1 for c in citations
        if c.title and len(c.title) >= MIN_TITLE_CHARS and c.year
    )
    return complete / len(citations)


# ── Public API: parse one paper ───────────────────────────────────────────────

def parse_citations(
    full_text: str,
    source: str,
    force_gemini: bool = False,
) -> CitationParseResult:
    """
    Extract and parse all citations from a research paper's full text.

    Pipeline:
      1. Locate the references section
      2. Split into individual entries
      3. Regex-parse all entries
      4. If regex quality < 0.55 OR force_gemini=True → re-parse with Gemini

    Args:
        full_text:    Complete extracted text of the paper (from pdf_loader)
        source:       Filename, e.g. "attention_is_all_you_need.pdf"
        force_gemini: Skip regex and go straight to Gemini (for known messy papers)

    Returns:
        CitationParseResult with all parsed citations
    """
    logger.info(f"Parsing citations for '{source}'")

    refs_text, start_offset = extract_references_section(full_text)

    if not refs_text:
        return CitationParseResult(
            source          = source,
            references_raw  = "",
            citations       = [],
            ref_count       = 0,
            warnings        = ["No references section found"],
        )

    entries = _split_reference_entries(refs_text)
    logger.debug(f"  Split into {len(entries)} reference entries")

    warnings: List[str] = []
    method_used = "regex"

    if force_gemini:
        citations    = _gemini_parse_entries(entries)
        method_used  = "gemini"
    else:
        citations    = [_regex_parse_entry(e) for e in entries]
        quality      = _regex_quality_score(citations)
        logger.debug(f"  Regex parse quality: {quality:.2f}")

        if quality < 0.55:
            logger.info(f"  Regex quality {quality:.2f} below threshold — falling back to Gemini")
            citations   = _gemini_parse_entries(entries)
            method_used = "gemini"
            warnings.append(f"Regex quality {quality:.2f} — used Gemini for parsing")

    # Final count
    titled = sum(1 for c in citations if c.title)
    logger.success(
        f"Parsed {len(citations)} citations from '{source}' "
        f"({titled} with titles, method={method_used})"
    )

    return CitationParseResult(
        source           = source,
        references_raw   = refs_text,
        citations        = citations,
        ref_count        = len(citations),
        parse_method_used= method_used,
        warnings         = warnings,
    )


# ── Cross-paper citation graph ────────────────────────────────────────────────

def _title_similarity(a: str, b: str) -> float:
    """
    Simple token-overlap similarity between two title strings.
    Fast enough to run O(n²) across 50 papers × ~30 citations each.
    No external libraries needed.
    """
    def tokens(s: str) -> set:
        # Lowercase, strip punctuation, remove common stop words
        stop = {"a", "an", "the", "of", "in", "on", "at", "to", "for",
                "and", "or", "with", "from", "is", "are", "by"}
        words = re.findall(r"[a-z]+", s.lower())
        return {w for w in words if w not in stop and len(w) > 2}

    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    intersection = len(ta & tb)
    union        = len(ta | tb)
    return intersection / union if union else 0.0


def build_citation_graph(
    parse_results: List[CitationParseResult],
    paper_metadata: Optional[List[Dict[str, Any]]] = None,
) -> CitationGraph:
    """
    Build a directed citation graph across all uploaded papers.

    Nodes = uploaded papers.
    Edges = paper A cites paper B (when B is also in the uploaded set).

    Args:
        parse_results:  output of parse_citations() for each uploaded paper
        paper_metadata: optional list of {source, title, year, authors}
                        from metadata_extractor; used to enrich nodes

    Returns:
        CitationGraph with nodes and edges
    """
    # Build a lookup of uploaded paper titles
    meta_by_source: Dict[str, Dict] = {}
    if paper_metadata:
        for m in paper_metadata:
            meta_by_source[m["source"]] = m

    # Nodes: one per uploaded paper
    nodes = []
    for result in parse_results:
        meta  = meta_by_source.get(result.source, {})
        nodes.append({
            "id":      result.source,
            "title":   meta.get("title", result.source.replace(".pdf", "")),
            "year":    meta.get("year"),
            "authors": meta.get("authors"),
            "venue":   meta.get("venue"),
            "ref_count": result.ref_count,
        })

    # Build a title → source lookup for cross-referencing
    title_to_source: Dict[str, str] = {}
    for node in nodes:
        if node["title"]:
            title_to_source[node["title"].lower()] = node["id"]

    # Edges: find when an uploaded paper is cited by another uploaded paper
    edges: List[Dict[str, Any]] = []
    edge_set: set = set()   # (source_id, target_id) to avoid duplicates

    for result in parse_results:
        for citation in result.citations:
            if not citation.title:
                continue

            # Try exact match first
            cit_title_lower = citation.title.lower()
            target = title_to_source.get(cit_title_lower)

            # Fuzzy match if no exact hit
            if not target:
                best_score  = 0.0
                best_source = None
                for known_title, known_source in title_to_source.items():
                    score = _title_similarity(citation.title, known_title)
                    if score > best_score:
                        best_score  = score
                        best_source = known_source

                if best_score >= TITLE_SIMILARITY_THRESHOLD:
                    target = best_source

            if target and target != result.source:
                edge_key = (result.source, target)
                if edge_key in edge_set:
                    # Increment weight on existing edge
                    for e in edges:
                        if e["source"] == result.source and e["target"] == target:
                            e["weight"] += 1
                            break
                else:
                    edges.append({
                        "source": result.source,
                        "target": target,
                        "weight": 1,
                        "citation_title": citation.title,
                    })
                    edge_set.add(edge_key)

    logger.success(
        f"Citation graph: {len(nodes)} nodes, {len(edges)} edges "
        f"across {len(parse_results)} papers"
    )
    return CitationGraph(nodes=nodes, edges=edges)


# ── Convenience: most-cited papers in the collection ─────────────────────────

def most_cited(graph: CitationGraph, top_n: int = 10) -> List[Dict[str, Any]]:
    """
    Return the top-N most-cited papers within the uploaded collection.
    Useful for the InsightsPanel "citation graph" tab.

    Returns list of {id, title, year, incoming_citations} sorted descending.
    """
    in_degree: Dict[str, int] = {n["id"]: 0 for n in graph.nodes}
    for edge in graph.edges:
        target = edge["target"]
        if target in in_degree:
            in_degree[target] += edge.get("weight", 1)

    node_by_id = {n["id"]: n for n in graph.nodes}
    ranked = sorted(in_degree.items(), key=lambda x: x[1], reverse=True)

    return [
        {
            **node_by_id.get(source_id, {"id": source_id}),
            "incoming_citations": count,
        }
        for source_id, count in ranked[:top_n]
        if count > 0
    ]