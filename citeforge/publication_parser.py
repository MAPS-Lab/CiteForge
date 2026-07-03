"""Parse SerpAPI ``publication`` strings into structured BibTeX metadata.

SerpAPI returns a free-text ``publication`` field for each Google Scholar
article (e.g. ``"ACM Computing Surveys 56 (1), 1-34, 2023"``).  This module
extracts venue name, volume, issue, pages, year, and identifiers from that
string so the pipeline can use them for venue-based API searches (Tier 1)
and direct field population (Tier 2).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import (
    CONFERENCE_AS_JOURNAL,
    CONFERENCE_KEYWORDS,
    KNOWN_CONFERENCE_VENUES,
    PREPRINT_SERVERS,
)

_CONFERENCE_AS_JOURNAL_LOWER: frozenset[str] = frozenset(c.lower() for c in CONFERENCE_AS_JOURNAL)

# ---------------------------------------------------------------------------
# Pre-compiled regexes (applied in cascade order)
# ---------------------------------------------------------------------------

# Pattern 1: Journal vol (issue), pages, year
#   e.g. "ACM Computing Surveys 56 (1), 1-34, 2023"
_JOURNAL_VOL_ISSUE_PAGES_RE = re.compile(
    r"^(.+?)\s+(\d+)\s*\((\d+(?:\s*-\s*\d+)?)\)\s*,"
    r"\s*(\w[\w\s,\u2013-]*?)\s*,\s*(\d{4})\s*$"
)

# Pattern 2: Journal vol, pages, year  (no issue)
#   e.g. "IEEE Access 14, 9506-9531, 2026"
_JOURNAL_VOL_PAGES_RE = re.compile(r"^(.+?)\s+(\d+)\s*,\s*(\w[\w\s,\u2013-]*?)\s*,\s*(\d{4})\s*$")

# Pattern 3: arXiv preprint
#   e.g. "arXiv preprint arXiv:2407.18753, 2024"
_ARXIV_RE = re.compile(
    r"^[Aa]r[Xx]iv\s+(?:preprint\s+)?(?:arXiv:)?(\d{4}\.\d{4,5}(?:v\d+)?)"
    r"(?:\s*,\s*(\d{4}))?\s*$"
)

# Pattern 4: bioRxiv / medRxiv / chemRxiv with DOI fragment
#   e.g. "BioRxiv, 10.1101/2025.05.22.655348, 2025"
_BIORXIV_DOI_RE = re.compile(
    r"^((?:bio|med|chem)rxiv)\s*,\s*(10\.\d{4,9}/[\w./\-]+)\s*,\s*(\d{4})\s*$",
    re.IGNORECASE,
)

# Pattern 5: US Patent
#   e.g. "US Patent 10,901,713, 2021" or "US Patent App. 16/234,567, 2019"
_PATENT_RE = re.compile(r"^US\s+Patent(?:\s+App\.?)?\s+([\d,/]+)\s*,\s*(\d{4})\s*$", re.IGNORECASE)

# Pattern 6/7: Venue with pages, year  (conference or generic)
#   e.g. "Proceedings of the 2020 GECCO conference, 1-8, 2020"
_VENUE_PAGES_YEAR_RE = re.compile(r"^(.+?)\s*,\s*(\w[\w\s,\u2013-]*?)\s*,\s*(\d{4})\s*$")

# Pattern 8: Venue, year  (no pages, no volume)
#   e.g. "Sage, 2021"
_VENUE_YEAR_RE = re.compile(r"^(.+?)\s*,\s*(\d{4})\s*$")


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedPublication:
    """Structured fields extracted from a SerpAPI publication string."""

    venue_name: str = ""
    venue_type: str = "unknown"  # journal | conference | preprint | patent | publisher | unknown
    volume: str = ""
    issue: str = ""
    pages: str = ""
    year: int | None = None
    arxiv_id: str = ""
    doi_fragment: str = ""
    patent_number: str = ""
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Venue-type classifier
# ---------------------------------------------------------------------------


def _classify_venue_type(venue: str) -> str:
    """Classify a venue name as journal, conference, preprint, or unknown."""
    low = venue.lower().strip()

    if any(ps in low for ps in PREPRINT_SERVERS):
        return "preprint"

    if (
        any(kw in low for kw in CONFERENCE_KEYWORDS)
        or any(known in low for known in KNOWN_CONFERENCE_VENUES)
        or low in _CONFERENCE_AS_JOURNAL_LOWER
    ):
        return "conference"

    return "journal"


_TRAILING_PREPOSITIONS = re.compile(
    r"\s+(?:in|on|for|of|and|the|a|an|with|at|to|from|by)\s*$",
    re.IGNORECASE,
)


def _strip_ellipsis(text: str) -> str:
    """Strip trailing ``...`` and dangling prepositions from truncated strings."""
    t = text.rstrip()
    for suffix in ("...", "\u2026"):
        if t.endswith(suffix):
            t = t[: -len(suffix)].rstrip()
            t = _TRAILING_PREPOSITIONS.sub("", t)
            return t.rstrip(" ,;:")
    return text


def _is_page_like(token: str) -> bool:
    """Return True if *token* looks like a page range or article number."""
    t = token.strip().rstrip(",").strip()
    return bool(t and re.fullmatch(r"\w[\w\u2013-]*", t))


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------


def parse_publication_string(pub: str | None) -> ParsedPublication | None:
    """Parse a SerpAPI ``publication`` string into structured metadata.

    Returns ``None`` for empty / unparseable input.
    """
    if not pub or not isinstance(pub, str):
        return None
    pub = _strip_ellipsis(pub.strip())
    if not pub:
        return None

    # --- Pattern 3: arXiv preprint ---
    m = _ARXIV_RE.match(pub)
    if m:
        return ParsedPublication(
            venue_name="arXiv",
            venue_type="preprint",
            year=int(m.group(2)) if m.group(2) else None,
            arxiv_id=m.group(1),
            confidence=0.95,
        )

    # --- Pattern 4: bioRxiv / medRxiv / chemRxiv with DOI ---
    m = _BIORXIV_DOI_RE.match(pub)
    if m:
        server = m.group(1)
        return ParsedPublication(
            venue_name=server,
            venue_type="preprint",
            year=int(m.group(3)),
            doi_fragment=m.group(2),
            confidence=0.90,
        )

    # --- Pattern 5: US Patent ---
    m = _PATENT_RE.match(pub)
    if m:
        return ParsedPublication(
            venue_name="US Patent",
            venue_type="patent",
            year=int(m.group(2)),
            patent_number=m.group(1),
            confidence=0.90,
        )

    # --- Pattern 1: Journal vol (issue), pages, year ---
    m = _JOURNAL_VOL_ISSUE_PAGES_RE.match(pub)
    if m:
        venue = _strip_ellipsis(m.group(1).strip())
        vtype = _classify_venue_type(venue)
        return ParsedPublication(
            venue_name=venue,
            venue_type=vtype,
            volume=m.group(2).strip(),
            issue=m.group(3).strip(),
            pages=m.group(4).strip().rstrip(",").strip(),
            year=int(m.group(5)),
            confidence=0.95,
        )

    # --- Pattern 2: Journal vol, pages, year ---
    m = _JOURNAL_VOL_PAGES_RE.match(pub)
    if m:
        venue = _strip_ellipsis(m.group(1).strip())
        pages_token = m.group(3).strip().rstrip(",").strip()
        vtype = _classify_venue_type(venue)
        return ParsedPublication(
            venue_name=venue,
            venue_type=vtype,
            volume=m.group(2).strip(),
            pages=pages_token,
            year=int(m.group(4)),
            confidence=0.90,
        )

    # --- Pattern 6: Venue, pages, year (conference or generic) ---
    m = _VENUE_PAGES_YEAR_RE.match(pub)
    if m:
        venue = _strip_ellipsis(m.group(1).strip())
        pages_token = m.group(2).strip().rstrip(",").strip()
        year = int(m.group(3))
        vtype = _classify_venue_type(venue)

        # Check if the middle token is actually page-like
        if _is_page_like(pages_token):
            conf = 0.80 if vtype == "conference" else 0.60
            return ParsedPublication(
                venue_name=venue,
                venue_type=vtype,
                pages=pages_token,
                year=year,
                confidence=conf,
            )
        # Middle token isn't page-like → treat as venue+year with extra text
        # Reconstruct full venue including middle token
        full_venue = _strip_ellipsis(f"{venue}, {pages_token}")
        vtype = _classify_venue_type(full_venue)
        conf = 0.70 if vtype == "conference" else 0.40
        return ParsedPublication(
            venue_name=full_venue,
            venue_type=vtype,
            year=year,
            confidence=conf,
        )

    # --- Pattern 7/8: Venue, year (no pages) ---
    m = _VENUE_YEAR_RE.match(pub)
    if m:
        venue = _strip_ellipsis(m.group(1).strip())
        year = int(m.group(2))
        vtype = _classify_venue_type(venue)

        if vtype == "conference":
            conf = 0.70
        elif len(venue.split()) <= 2:
            vtype = "publisher"
            conf = 0.30
        else:
            conf = 0.50

        return ParsedPublication(
            venue_name=venue,
            venue_type=vtype,
            year=year,
            confidence=conf,
        )

    # Nothing matched
    return None
