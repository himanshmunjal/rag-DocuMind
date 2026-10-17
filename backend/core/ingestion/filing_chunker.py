# """
# filing_chunker.py — splits a raw SEC filing (10-K / 10-Q HTML) into
# Item-aware, size-bounded chunks ready for embedder.embed_chunks().

# Replaces the old paragraph-only chunker.py for filings. The key difference
# from generic chunking: filing text has real structural anchors (Item 1,
# Item 1A, Item 7, ...) that matter for retrieval — a question about risk
# factors should search Item 1A, not the whole document. This module
# preserves that structure in chunk metadata instead of throwing it away.

# Pipeline position:
#     edgar_client.fetch_filing_document() → filing_chunker.chunk_filing()
#         → embedder.embed_chunks() → indexer.index_chunks()

# Output contract (matches chunker.py / embedder.py's expected shape):
#     [{"text": str, "metadata": {...}}, ...]

# Known messiness this module works around:
#   - The Table of Contents lists every "Item N. <Title>" near the top of
#     the document, which looks identical to the real section header later
#     on. Naively splitting on the first match per item produces a giant
#     fake "chunk" containing the whole document. We handle this by keeping
#     only the LAST occurrence of each item number — real filings.
#   - Not every filer uses identical spacing/punctuation after "Item 7" —
#     the regex tolerates '.', ':', '-', '—', or nothing, and varying whitespace.
#   - Non-10-K/10-Q forms (8-K, etc.) usually don't have Item structure in
#     this sense — chunk_filing() falls back to flat chunking if no items
#     are found, so the pipeline never hard-fails on an unexpected form type.
# """

# import re
# from typing import List, Dict, Any, Optional

# from bs4 import BeautifulSoup
# from loguru import logger

# from config import settings

# from bs4 import XMLParsedAsHTMLWarning
# import warnings
# warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


# # ── standard 10-K / 10-Q item titles (fallback labels, not authoritative) ──────

# ITEM_TITLES: Dict[str, str] = {
#     "1":   "Business",
#     "1A":  "Risk Factors",
#     "1B":  "Unresolved Staff Comments",
#     "1C":  "Cybersecurity",
#     "2":   "Properties",
#     "3":   "Legal Proceedings",
#     "4":   "Mine Safety Disclosures",
#     "5":   "Market for Registrant's Common Equity",
#     "6":   "Selected Financial Data",
#     "7":   "Management's Discussion and Analysis (MD&A)",
#     "7A":  "Quantitative and Qualitative Disclosures About Market Risk",
#     "8":   "Financial Statements and Supplementary Data",
#     "9":   "Changes in and Disagreements with Accountants",
#     "9A":  "Controls and Procedures",
#     "9B":  "Other Information",
#     "10":  "Directors, Executive Officers and Corporate Governance",
#     "11":  "Executive Compensation",
#     "12":  "Security Ownership",
#     "13":  "Certain Relationships and Related Transactions",
#     "14":  "Principal Accountant Fees and Services",
#     "15":  "Exhibits, Financial Statement Schedules",
# }

# # Matches "Item 7A. Quantitative and Qualitative..." at the start of a line,
# # tolerating '.', ':', '-', '—', or nothing between the number and the title.
# ITEM_HEADER_RE = re.compile(
#     r"^\s*Item\s+(\d{1,2}[A-C]?)\s*[\.\:\-\u2014]?\s*([A-Z][^\n]{0,120})?\s*$",
#     re.IGNORECASE | re.MULTILINE,
# )


# # ── HTML → clean text ────────────────────────────────────────────────────────

# def html_to_text(html: str) -> str:
#     """
#     Strip a filing's HTML down to clean, line-structured plain text.
#     Keeps line breaks between block elements so ITEM_HEADER_RE can match
#     headers at the start of a line — collapsing everything to one blob
#     would destroy that structure.
#     """
#     soup = BeautifulSoup(html, "lxml")

#     # Filings often embed <script>/<style>/hidden XBRL viewer tags — drop them.
#     for tag in soup(["script", "style", "ix:header"]):
#         tag.decompose()

#     text = soup.get_text(separator="\n")

#     # Collapse runs of blank lines and trailing whitespace, but keep line breaks.
#     lines = [line.strip() for line in text.split("\n")]
#     lines = [line for line in lines if line]
#     return "\n".join(lines)


# # ── item splitting ────────────────────────────────────────────────────────────

# def _find_item_boundaries(text: str) -> List[Dict[str, Any]]:
#     """
#     Find every "Item N" header match, then keep only the LAST match per
#     item number — this is what skips past the Table of Contents and lands
#     on the real section header.

#     Returns:
#         list of {"item": "7A", "title": str, "start": int}, sorted by
#         position in the document.
#     """
#     matches = list(ITEM_HEADER_RE.finditer(text))
#     if not matches:
#         return []

#     last_match_for_item: Dict[str, re.Match] = {}
#     for m in matches:
#         item_num = m.group(1).upper()
#         last_match_for_item[item_num] = m  # later matches overwrite earlier ones

#     boundaries = []
#     for item_num, m in last_match_for_item.items():
#         title = (m.group(2) or "").strip() or ITEM_TITLES.get(item_num, "")
#         # body_start = end of the header line itself, so the header text
#         # ("Item 1. Business") doesn't leak into the section body as a
#         # standalone junk chunk — it's already captured in metadata.
#         boundaries.append({
#             "item": item_num,
#             "title": title,
#             "start": m.start(),
#             "body_start": m.end(),
#         })

#     boundaries.sort(key=lambda b: b["start"])

#     # Guard against the "everything collapses to one match" degenerate case:
#     # if we only found 1-2 boundaries in a document this long, the regex
#     # probably didn't actually find real section headers — treat as no match.
#     if len(boundaries) < 3 and len(text) > 20_000:
#         logger.warning(
#             f"Only {len(boundaries)} item boundaries found in a "
#             f"{len(text):,}-char document — falling back to flat chunking."
#         )
#         return []

#     return boundaries


# def split_into_items(text: str) -> List[Dict[str, Any]]:
#     """
#     Slice filing text into per-Item sections using the boundaries found above.

#     Returns:
#         [{"item": "1A", "title": "Risk Factors", "text": "..."}, ...]
#         Empty list if no reliable item structure was found.
#     """
#     boundaries = _find_item_boundaries(text)
#     if not boundaries:
#         return []

#     sections = []
#     for i, b in enumerate(boundaries):
#         body_start = b["body_start"]
#         end = boundaries[i + 1]["start"] if i + 1 < len(boundaries) else len(text)
#         section_text = text[body_start:end].strip()

#         if len(section_text) < 50:
#             # Header with essentially no body (e.g. "Item 4. [Not applicable]")
#             continue

#         sections.append({
#             "item": b["item"],
#             "title": b["title"] or ITEM_TITLES.get(b["item"], ""),
#             "text": section_text,
#         })

#     return sections


# # ── size-bounded sub-chunking (paragraph → sentence fallback) ─────────────────

# def _split_into_paragraphs(text: str) -> List[str]:
#     paragraphs = re.split(r"\n\s*\n|\n(?=[A-Z])", text)
#     return [p.strip() for p in paragraphs if p.strip()]


# def _split_into_sentences(text: str) -> List[str]:
#     sentences = re.split(r"(?<=[.!?])\s+", text)
#     return [s.strip() for s in sentences if s.strip()]


# def _token_estimate(text: str) -> int:
#     return len(text) // 4


# def _chunk_section_text(text: str) -> List[str]:
#     """
#     Split one Item's text into chunks <= settings.CHUNK_SIZE tokens, with
#     settings.CHUNK_OVERLAP tokens of overlap between consecutive chunks.

#     Paragraphs are PACKED together up to CHUNK_SIZE (not one chunk per
#     paragraph) — this matters a lot for filing HTML, which tends to produce
#     many short paragraphs/table-rows that would otherwise each become their
#     own tiny chunk. Only a paragraph that alone exceeds CHUNK_SIZE falls back
#     to sentence-level packing.
#     """
#     paragraphs = _split_into_paragraphs(text) if settings.SEMANTIC_OVERLAP else [text]
#     chunks: List[str] = []
#     current: List[str] = []
#     current_tokens = 0

#     def flush_with_overlap() -> None:
#         nonlocal current, current_tokens
#         chunks.append(" ".join(current))

#         # carry the tail of the current chunk forward as overlap
#         overlap_tokens = 0
#         overlap_pieces: List[str] = []
#         for piece in reversed(current):
#             t = _token_estimate(piece)
#             if overlap_tokens + t > settings.CHUNK_OVERLAP:
#                 break
#             overlap_pieces.insert(0, piece)
#             overlap_tokens += t
#         current = overlap_pieces
#         current_tokens = overlap_tokens

#     for para in paragraphs:
#         p_tokens = _token_estimate(para)

#         if p_tokens > settings.CHUNK_SIZE:
#             # oversized paragraph: flush whatever's pending, then sentence-pack it
#             if current:
#                 chunks.append(" ".join(current))
#                 current, current_tokens = [], 0

#             for sent in _split_into_sentences(para):
#                 s_tokens = _token_estimate(sent)
#                 if current_tokens + s_tokens > settings.CHUNK_SIZE and current:
#                     flush_with_overlap()
#                 current.append(sent)
#                 current_tokens += s_tokens
#             continue

#         # normal case: pack this paragraph in with whatever's accumulated so far
#         if current_tokens + p_tokens > settings.CHUNK_SIZE and current:
#             flush_with_overlap()

#         current.append(para)
#         current_tokens += p_tokens

#     if current:
#         chunks.append(" ".join(current))

#     return chunks


# # ── public API ─────────────────────────────────────────────────────────────────

# def chunk_filing(html: str, filing_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
#     """
#     Full pipeline: raw filing HTML + its EDGAR metadata → chunk dicts ready
#     for embed_chunks().

#     Args:
#         html:         raw HTML from edgar_client.fetch_filing_document()
#         filing_meta:  dict with at least the fields on a FilingMeta —
#                       cik, accession_number, form_type, filing_date,
#                       report_date, company_name. Extra keys are ignored.

#     Returns:
#         [{"text": str, "metadata": {...}}, ...]

#     Metadata on each chunk:
#         source              accession_number (unique per filing — used for
#                              document_exists/delete_document lookups)
#         cik, company_name, form_type, filing_date, report_date
#         item, item_title    "" / "" if flat-chunked (no item structure found)
#         chunk_index         stable, increasing across the whole filing
#         chunk_type          "item_section" | "flat"
#         char_count

#     Example:
#         meta = get_filings(cik, "10-K", limit=1)[0].to_dict()
#         html = fetch_filing_document(meta["cik"], meta["accession_number"], meta["primary_document"])
#         chunks = chunk_filing(html, meta)
#         chunks = embed_chunks(chunks)
#         index_chunks(chunks)
#     """
#     text = html_to_text(html)
#     sections = split_into_items(text)

#     base_meta = {
#         "source":        filing_meta.get("accession_number", ""),
#         "cik":           filing_meta.get("cik", ""),
#         "company_name":  filing_meta.get("company_name", ""),
#         "form_type":     filing_meta.get("form_type", ""),
#         "filing_date":   filing_meta.get("filing_date", ""),
#         "report_date":   filing_meta.get("report_date", ""),
#     }

#     all_chunks: List[Dict[str, Any]] = []
#     chunk_index = 0

#     if sections:
#         logger.info(
#             f"chunk_filing: {len(sections)} item section(s) found in "
#             f"{filing_meta.get('form_type', '?')} {filing_meta.get('accession_number', '?')}"
#         )
#         for section in sections:
#             piece_texts = _chunk_section_text(section["text"])
#             for piece in piece_texts:
#                 all_chunks.append({
#                     "text": piece,
#                     "metadata": {
#                         **base_meta,
#                         "item":        section["item"],
#                         "item_title":  section["title"],
#                         "chunk_index": chunk_index,
#                         "chunk_type":  "item_section",
#                         "char_count":  len(piece),
#                     },
#                 })
#                 chunk_index += 1
#     else:
#         # Fallback: no reliable item structure (e.g. an 8-K, or parsing
#         # failure) — flat chunk the whole document rather than fail.
#         logger.warning(
#             f"chunk_filing: no item structure found for "
#             f"{filing_meta.get('accession_number', '?')} — flat chunking instead"
#         )
#         piece_texts = _chunk_section_text(text)
#         for piece in piece_texts:
#             all_chunks.append({
#                 "text": piece,
#                 "metadata": {
#                     **base_meta,
#                     "item":        "",
#                     "item_title":  "",
#                     "chunk_index": chunk_index,
#                     "chunk_type":  "flat",
#                     "char_count":  len(piece),
#                 },
#             })
#             chunk_index += 1

#     logger.success(
#         f"chunk_filing: produced {len(all_chunks)} chunks for "
#         f"{filing_meta.get('accession_number', '?')}"
#     )
#     return all_chunks


"""
filing_chunker.py — splits a raw SEC filing (10-K / 10-Q HTML) into
Item-aware, size-bounded chunks ready for embedder.embed_chunks().

Replaces the old paragraph-only chunker.py for filings. The key difference
from generic chunking: filing text has real structural anchors (Item 1,
Item 1A, Item 7, ...) that matter for retrieval — a question about risk
factors should search Item 1A, not the whole document. This module
preserves that structure in chunk metadata instead of throwing it away.

Pipeline position:
    edgar_client.fetch_filing_document() → filing_chunker.chunk_filing()
        → embedder.embed_chunks() → indexer.index_chunks()

Output contract (matches chunker.py / embedder.py's expected shape):
    [{"text": str, "metadata": {...}}, ...]

Known messiness this module works around:
  - The Table of Contents lists every "Item N. <Title>" near the top of
    the document, which looks identical to the real section header later
    on — a single stray match, isolated from everything else.
  - Large filers (Microsoft, Apple, etc.) print a running "Item N" header
    on EVERY page throughout that section, producing dozens of repeated
    matches for the same item number, not just one. Naively taking the
    "last" occurrence lands on the tail end of the section (or the next
    section's audit-report boilerplate) and throws away everything before
    it — including deep content like footnotes/notes to the financial
    statements. Naively taking the "first" occurrence can land on the TOC
    itself if the TOC entry isn't filtered out first.
  - Fix: for each item number, find the single LARGEST GAP between
    consecutive occurrences of that item. Everything before the largest
    gap is TOC/cross-reference noise (isolated, far from the real
    content); everything after it is the real section, appearing as a
    dense cluster of running-header repeats. The true section start is
    the FIRST match in that post-gap cluster.
  - Not every filer uses identical spacing/punctuation after "Item 7" —
    the regex tolerates '.', ':', '-', '—', or nothing, and varying whitespace.
  - Non-10-K/10-Q forms (8-K, etc.) usually don't have Item structure in
    this sense — chunk_filing() falls back to flat chunking if no items
    are found, so the pipeline never hard-fails on an unexpected form type.
"""

import re
from typing import List, Dict, Any, Optional

from bs4 import BeautifulSoup
from loguru import logger

from config import settings

from bs4 import XMLParsedAsHTMLWarning
import warnings
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


# ── standard 10-K / 10-Q item titles (fallback labels, not authoritative) ──────

ITEM_TITLES: Dict[str, str] = {
    "1":   "Business",
    "1A":  "Risk Factors",
    "1B":  "Unresolved Staff Comments",
    "1C":  "Cybersecurity",
    "2":   "Properties",
    "3":   "Legal Proceedings",
    "4":   "Mine Safety Disclosures",
    "5":   "Market for Registrant's Common Equity",
    "6":   "Selected Financial Data",
    "7":   "Management's Discussion and Analysis (MD&A)",
    "7A":  "Quantitative and Qualitative Disclosures About Market Risk",
    "8":   "Financial Statements and Supplementary Data",
    "9":   "Changes in and Disagreements with Accountants",
    "9A":  "Controls and Procedures",
    "9B":  "Other Information",
    "10":  "Directors, Executive Officers and Corporate Governance",
    "11":  "Executive Compensation",
    "12":  "Security Ownership",
    "13":  "Certain Relationships and Related Transactions",
    "14":  "Principal Accountant Fees and Services",
    "15":  "Exhibits, Financial Statement Schedules",
}

# Matches "Item 7A. Quantitative and Qualitative..." at the start of a line,
# tolerating '.', ':', '-', '—', or nothing between the number and the title.
# Whitespace is [ \t] only — "\s" would let a bare running header ("Item 1")
# swallow the NEXT line as its title. The \b stops combined running headers
# like "Item 1B, 1C" from being read as "Item 1" with title "B, 1C".
ITEM_HEADER_RE = re.compile(
    r"^[ \t]*Item[ \t]+(\d{1,2}[A-C]?)\b[ \t]*[\.\:\-–—]?[ \t]*([A-Z\[][^\n]{0,150})?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)

# Canonical 10-K item order — section boundaries must follow it.
ITEM_ORDER: List[str] = [
    "1", "1A", "1B", "1C", "2", "3", "4", "5", "6", "7", "7A", "8",
    "9", "9A", "9B", "9C", "10", "11", "12", "13", "14", "15", "16",
]
_ITEM_RANK: Dict[str, int] = {item: i for i, item in enumerate(ITEM_ORDER)}

# A section shorter than this is "empty" for boundary scoring (TOC entries,
# "Item 4. Not applicable"). Real short sections are still kept — they just
# don't earn a point.
MIN_SECTION_CHARS = 200

# Matches a BARE running-header line with no title at all, e.g. a lone
# "Item 8" line repeated at the top of every page — used to strip this
# litter out of section bodies once boundaries have been found, so it
# doesn't glue itself onto the start of real content mid-chunk.
BARE_ITEM_HEADER_RE = re.compile(
    r"^\s*Item\s+\d{1,2}[A-C]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Stop chunking once we hit the exhibit index / signatures — this is not
# substantive filing content (legal boilerplate listing every exhibit,
# indenture, stock plan form, etc.) and should never enter the vector store.
EXHIBIT_INDEX_ITEM = "15"


# ── HTML → clean text ────────────────────────────────────────────────────────

BLOCK_TAGS = [
    "p", "div", "br", "tr", "li", "table", "section", "ul", "ol", "hr",
    "h1", "h2", "h3", "h4", "h5", "h6",
]


def html_to_text(html: str) -> str:
    """
    Strip a filing's HTML down to clean, line-structured plain text.
    Keeps line breaks between block elements so ITEM_HEADER_RE can match
    headers at the start of a line — collapsing everything to one blob
    would destroy that structure.

    Line breaks are inserted only after BLOCK-level tags. Filings style
    headings with several inline <span>s ("RIS" + "K FACTORS"), so a blanket
    get_text(separator="\n") splits words mid-token and breaks both header
    detection and embedding quality. Table cells are joined with spaces so a
    financial-table row stays on one line.
    """
    soup = BeautifulSoup(html, "lxml")

    # Filings often embed <script>/<style>/hidden XBRL viewer tags — drop them.
    for tag in soup(["script", "style", "ix:header"]):
        tag.decompose()

    for tag in soup.find_all(["td", "th"]):
        tag.insert_after(" ")
    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_after("\n")

    text = soup.get_text().replace("\xa0", " ")

    # Collapse runs of blank lines and intra-line whitespace, keep line breaks.
    lines = [re.sub(r"[ \t​]+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines)


# ── item splitting ────────────────────────────────────────────────────────────

def _choose_header_chain(matches: List[re.Match], text_len: int) -> List[int]:
    """
    Pick at most one header match per Item, in canonical Item order, so that
    the resulting sections look real. Returns indices into `matches`.

    Scoring (dynamic programming over all candidate chains):
      +1  for each section with at least MIN_SECTION_CHARS of body
      -1  for each OTHER Item's header that falls inside a chosen section
    Repeated headers of the section's own Item (running page headers) are free.

    The Table of Contents loses on both counts — its "sections" are a few
    characters long, and a chain starting in the TOC has to swallow every
    other TOC entry to reach the real body. Ties prefer more Items, then
    earlier positions (the first header of a section, not a later page header).
    """
    n = len(matches)
    ranks = [_ITEM_RANK[m.group(1).upper()] for m in matches]
    starts = [m.start() for m in matches]

    # same_before[r][k] = number of matches with rank r among matches[:k]
    same_before = [[0] * (n + 1) for _ in ITEM_ORDER]
    for k, r in enumerate(ranks):
        for rr in range(len(ITEM_ORDER)):
            same_before[rr][k + 1] = same_before[rr][k] + (1 if rr == r else 0)

    def section_score(i: int, j: int) -> float:
        """Score of the section that starts at match i and ends at match j (j == n → end of text)."""
        end = starts[j] if j < n else text_len
        inside = j - i - 1
        own = same_before[ranks[i]][j] - same_before[ranks[i]][i + 1]
        others = inside - own
        return (1.0 if end - starts[i] >= MIN_SECTION_CHARS else 0.0) - others

    # tie-breakers, kept tiny so they never outweigh a real point
    def bonus(i: int) -> float:
        return 1e-3 - 1e-9 * starts[i]

    best = [bonus(i) for i in range(n)]
    prev: List[Optional[int]] = [None] * n
    for j in range(n):
        for i in range(j):
            if ranks[i] < ranks[j]:
                cand = best[i] + section_score(i, j) + bonus(j)
                if cand > best[j]:
                    best[j], prev[j] = cand, i

    last = max(range(n), key=lambda j: best[j] + section_score(j, n))
    chain: List[int] = []
    cur: Optional[int] = last
    while cur is not None:
        chain.append(cur)
        cur = prev[cur]
    return chain[::-1]


def _find_item_boundaries(text: str) -> List[Dict[str, Any]]:
    """
    Find the TRUE start of each Item section, robust to two separate
    sources of duplicate headers:
      1. The Table of Contents (one stray early match per item).
      2. Running page headers repeated throughout a large section (many
         matches for the same item number, densely packed).

    See _choose_header_chain() for how the real headers are picked.

    Returns:
        list of {"item": "7A", "title": str, "start": int, "body_start": int},
        sorted by position in the document.
    """
    matches = [m for m in ITEM_HEADER_RE.finditer(text) if m.group(1).upper() in _ITEM_RANK]
    if not matches:
        return []

    boundaries = []
    for idx in _choose_header_chain(matches, len(text)):
        m = matches[idx]
        item_num = m.group(1).upper()
        # The chosen match is often a bare running header ("Item 1A") sitting
        # just above the styled heading ("ITEM 1A. RISK FACTORS") — take the
        # title and body start from that heading when it's right there.
        header = m
        if not m.group(2):
            header = next(
                (
                    h for h in matches[idx + 1:]
                    if h.start() - m.start() < 500
                    and h.group(1).upper() == item_num and h.group(2)
                ),
                m,
            )
        title = (header.group(2) or "").strip() or ITEM_TITLES.get(item_num, "")
        boundaries.append({
            "item": item_num,
            "title": title,
            "start": m.start(),
            "body_start": header.end(),
        })

    # Guard against the "everything collapses to one match" degenerate case:
    # if we only found 1-2 boundaries in a document this long, the regex
    # probably didn't actually find real section headers — treat as no match.
    if len(boundaries) < 3 and len(text) > 20_000:
        logger.warning(
            f"Only {len(boundaries)} item boundaries found in a "
            f"{len(text):,}-char document — falling back to flat chunking."
        )
        return []

    return boundaries


def _strip_running_headers(section_text: str, item_num: str) -> str:
    """
    Remove bare repeated "Item N" running-header lines from within a
    section body (they're page-break litter, not content) so they don't
    glue onto the start of real sentences mid-chunk, e.g.
    "Item 8 NOTE 7 — BUSINESS COMBINATIONS..." -> "NOTE 7 — BUSINESS COMBINATIONS...".

    Only strips bare lines matching THIS item's own number — never touches
    a line that has real title text after it, since that could be a
    legitimate cross-reference sentence rather than a page marker.
    """
    lines = section_text.split("\n")
    cleaned = []
    for line in lines:
        stripped = line.strip()
        if re.fullmatch(rf"item\s+{re.escape(item_num)}", stripped, re.IGNORECASE):
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def split_into_items(text: str) -> List[Dict[str, Any]]:
    """
    Slice filing text into per-Item sections using the boundaries found above.
    Stops before the exhibit index (Item 15) — everything from there on
    (exhibit list, signatures page) is legal boilerplate, not substantive
    content, and is deliberately excluded from the returned sections.

    Returns:
        [{"item": "1A", "title": "Risk Factors", "text": "..."}, ...]
        Empty list if no reliable item structure was found.
    """
    boundaries = _find_item_boundaries(text)
    if not boundaries:
        return []

    # Cut off at the exhibit index, if present — nothing after it is worth
    # chunking, and including it just adds noise (exhibit titles, indenture
    # names, stock plan form references) that pollutes retrieval.
    cutoff_idx = next(
        (i for i, b in enumerate(boundaries) if b["item"] == EXHIBIT_INDEX_ITEM),
        None,
    )
    # Ends are taken from the FULL boundary list, so the section right before
    # the exhibit index stops at Item 15 instead of running to end-of-document.
    ends = [b["start"] for b in boundaries[1:]] + [len(text)]
    if cutoff_idx is not None:
        boundaries = boundaries[:cutoff_idx]

    sections = []
    for i, b in enumerate(boundaries):
        body_start = b["body_start"]
        end = ends[i]
        section_text = text[body_start:end].strip()
        section_text = _strip_running_headers(section_text, b["item"])

        if len(section_text) < 50:
            # Header with essentially no body (e.g. "Item 4. [Not applicable]")
            continue

        sections.append({
            "item": b["item"],
            "title": b["title"] or ITEM_TITLES.get(b["item"], ""),
            "text": section_text,
        })

    return sections


# ── size-bounded sub-chunking (paragraph → sentence fallback) ─────────────────

def _split_into_paragraphs(text: str) -> List[str]:
    paragraphs = re.split(r"\n\s*\n|\n(?=[A-Z])", text)
    return [p.strip() for p in paragraphs if p.strip()]


def _split_into_sentences(text: str) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text)
    return [s.strip() for s in sentences if s.strip()]


def _token_estimate(text: str) -> int:
    return len(text) // 4


def _chunk_section_text(text: str) -> List[str]:
    """
    Split one Item's text into chunks <= settings.CHUNK_SIZE tokens, with
    settings.CHUNK_OVERLAP tokens of overlap between consecutive chunks.

    Paragraphs are PACKED together up to CHUNK_SIZE (not one chunk per
    paragraph) — this matters a lot for filing HTML, which tends to produce
    many short paragraphs/table-rows that would otherwise each become their
    own tiny chunk. Only a paragraph that alone exceeds CHUNK_SIZE falls back
    to sentence-level packing.
    """
    paragraphs = _split_into_paragraphs(text) if settings.SEMANTIC_OVERLAP else [text]
    chunks: List[str] = []
    current: List[str] = []
    current_tokens = 0

    def flush_with_overlap() -> None:
        nonlocal current, current_tokens
        chunks.append(" ".join(current))

        # carry the tail of the current chunk forward as overlap
        overlap_tokens = 0
        overlap_pieces: List[str] = []
        for piece in reversed(current):
            t = _token_estimate(piece)
            if overlap_tokens + t > settings.CHUNK_OVERLAP:
                break
            overlap_pieces.insert(0, piece)
            overlap_tokens += t
        current = overlap_pieces
        current_tokens = overlap_tokens

    for para in paragraphs:
        p_tokens = _token_estimate(para)

        if p_tokens > settings.CHUNK_SIZE:
            # oversized paragraph: flush whatever's pending, then sentence-pack it
            if current:
                chunks.append(" ".join(current))
                current, current_tokens = [], 0

            for sent in _split_into_sentences(para):
                s_tokens = _token_estimate(sent)
                if current_tokens + s_tokens > settings.CHUNK_SIZE and current:
                    flush_with_overlap()
                current.append(sent)
                current_tokens += s_tokens
            continue

        # normal case: pack this paragraph in with whatever's accumulated so far
        if current_tokens + p_tokens > settings.CHUNK_SIZE and current:
            flush_with_overlap()

        current.append(para)
        current_tokens += p_tokens

    if current:
        chunks.append(" ".join(current))

    return chunks


# ── public API ─────────────────────────────────────────────────────────────────

def chunk_filing(html: str, filing_meta: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Full pipeline: raw filing HTML + its EDGAR metadata → chunk dicts ready
    for embed_chunks().

    Args:
        html:         raw HTML from edgar_client.fetch_filing_document()
        filing_meta:  dict with at least the fields on a FilingMeta —
                      cik, accession_number, form_type, filing_date,
                      report_date, company_name. Extra keys are ignored.

    Returns:
        [{"text": str, "metadata": {...}}, ...]

    Metadata on each chunk:
        source              accession_number (unique per filing — used for
                             document_exists/delete_document lookups)
        cik, company_name, form_type, filing_date, report_date
        item, item_title    "" / "" if flat-chunked (no item structure found)
        chunk_index         stable, increasing across the whole filing
        chunk_type          "item_section" | "flat"
        char_count

    Example:
        meta = get_filings(cik, "10-K", limit=1)[0].to_dict()
        html = fetch_filing_document(meta["cik"], meta["accession_number"], meta["primary_document"])
        chunks = chunk_filing(html, meta)
        chunks = embed_chunks(chunks)
        index_chunks(chunks)
    """
    text = html_to_text(html)
    sections = split_into_items(text)

    base_meta = {
        "source":        filing_meta.get("accession_number", ""),
        "cik":           filing_meta.get("cik", ""),
        "company_name":  filing_meta.get("company_name", ""),
        "form_type":     filing_meta.get("form_type", ""),
        "filing_date":   filing_meta.get("filing_date", ""),
        "report_date":   filing_meta.get("report_date", ""),
    }

    all_chunks: List[Dict[str, Any]] = []
    chunk_index = 0

    if sections:
        logger.info(
            f"chunk_filing: {len(sections)} item section(s) found in "
            f"{filing_meta.get('form_type', '?')} {filing_meta.get('accession_number', '?')}"
        )
        for section in sections:
            piece_texts = _chunk_section_text(section["text"])
            for piece in piece_texts:
                all_chunks.append({
                    "text": piece,
                    "metadata": {
                        **base_meta,
                        "item":        section["item"],
                        "item_title":  section["title"],
                        "chunk_index": chunk_index,
                        "chunk_type":  "item_section",
                        "char_count":  len(piece),
                    },
                })
                chunk_index += 1
    else:
        # Fallback: no reliable item structure (e.g. an 8-K, or parsing
        # failure) — flat chunk the whole document rather than fail.
        logger.warning(
            f"chunk_filing: no item structure found for "
            f"{filing_meta.get('accession_number', '?')} — flat chunking instead"
        )
        piece_texts = _chunk_section_text(text)
        for piece in piece_texts:
            all_chunks.append({
                "text": piece,
                "metadata": {
                    **base_meta,
                    "item":        "",
                    "item_title":  "",
                    "chunk_index": chunk_index,
                    "chunk_type":  "flat",
                    "char_count":  len(piece),
                },
            })
            chunk_index += 1

    logger.success(
        f"chunk_filing: produced {len(all_chunks)} chunks for "
        f"{filing_meta.get('accession_number', '?')}"
    )
    return all_chunks