from __future__ import annotations

import re
from typing import Dict, Any, Optional

from .config import _DOI_REGEX


def _norm_doi(doi: Optional[str]) -> Optional[str]:
    """
    Clean up a DOI string by removing common URL and prefix wrappers and
    normalizing to lowercase for case-insensitive comparison, as DOIs are
    case-insensitive identifiers per the DOI specification.
    """
    if not doi:
        return None
    d = str(doi).strip()
    # strip URL prefixes
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    # remove "doi:" prefix
    d = re.sub(r"^doi:\s*", "", d, flags=re.IGNORECASE)
    d = d.strip()
    if not d:
        return None
    # lowercase entire DOI for case-insensitive comparison
    return d.lower()


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    """
    Provide a public helper that normalizes DOIs into a consistent canonical
    form suitable for comparison and lookups.
    """
    return _norm_doi(doi)


def _norm_arxiv_id(s: Optional[str]) -> Optional[str]:
    """
    Clean an arXiv identifier by stripping the arXiv prefix and any version
    suffix so that different versions map to the same base ID.
    """
    if not s:
        return None
    t = str(s).strip()
    t = re.sub(r'(?i)^arxiv:\s*', '', t)  # strip "arXiv:"
    t = re.sub(r'v\d+$', '', t)  # strip version
    return t.strip() or None


# meta tag patterns for finding DOIs in HTML
_DOI_META_PATTERNS = [
    r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+name=["\']dc\.identifier["\'][^>]+content=["\']doi:?\s*([^"\']+)["\']',
    r'<meta[^>]+property=["\']og:doi["\'][^>]+content=["\']([^"\']+)["\']',
]


def find_doi_in_html(html: str) -> Optional[str]:
    """
    Look for a DOI inside an HTML page by checking common meta tags first and
    then searching the full document as a fallback.
    """
    if not html:
        return None
    for pat in _DOI_META_PATTERNS:
        m = re.search(pat, html, flags=re.IGNORECASE)
        if m:
            d = _norm_doi(m.group(1))
            if d and re.search(_DOI_REGEX, d, flags=re.IGNORECASE):
                return d
    m = re.search(_DOI_REGEX, html, flags=re.IGNORECASE)
    return _norm_doi(m.group(1)) if m else None


def find_doi_in_text(text: str) -> Optional[str]:
    """
    Scan arbitrary text for something that looks like a DOI and return a
    normalized version when one is found.
    """
    if not text:
        return None
    m = re.search(_DOI_REGEX, text, flags=re.IGNORECASE)
    return _norm_doi(m.group(1)) if m else None


def find_arxiv_in_text(text: str) -> Optional[str]:
    """
    Scan text and URLs for an arXiv identifier, handling both plain IDs and
    arxiv.org links before returning a normalized form.
    """
    if not text:
        return None
    # look for ID with or without "arXiv:" prefix
    m = re.search(r'(?i)arxiv[:/\s]*([0-9]{4}\.[0-9]{4,5})(?:v\d+)?', text)
    if m:
        return _norm_arxiv_id(m.group(1))
    # look for arxiv.org URLs
    m = re.search(r'(?i)arxiv\.org/(abs|pdf)/([0-9]{4}\.[0-9]{4,5})', text)
    if m:
        return _norm_arxiv_id(m.group(2))
    return None


def allowlisted_url(url: Optional[str]) -> Optional[str]:
    """
    Accept only URLs that point to trusted resolvers such as doi.org or
    arxiv.org and discard links to generic publisher pages.
    """
    if not url:
        return None
    u = url.strip()
    if re.search(r'^https?://(dx\.)?doi\.org/\S+$', u, flags=re.IGNORECASE):
        return u
    if re.search(r'^https?://arxiv\.org/(abs|pdf)/\S+$', u, flags=re.IGNORECASE):
        return u
    return None


def extract_arxiv_eprint(entry: Dict[str, Any]) -> Optional[str]:
    """
    Try to recover an arXiv identifier from a BibTeX entry by checking the
    archive prefix, eprint field, and common text fields that may mention arXiv.
    """
    fields = entry.get("fields") or {}
    # check proper eprint field first
    ap = (fields.get("archiveprefix") or "").lower()
    if ap == "arxiv":
        return _norm_arxiv_id(fields.get("eprint"))
    # sometimes arXiv ID is in journal or howpublished field
    j = fields.get("journal") or fields.get("howpublished") or ""
    m = re.search(r'(?i)arxiv:\s*([0-9]{4}\.[0-9]{4,5})(v\d+)?', j)
    if m:
        return _norm_arxiv_id(m.group(1))
    return None
