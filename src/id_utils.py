from __future__ import annotations

import re
import urllib.parse
from typing import Any

from .config import (
    _DOI_REGEX,
    ARXIV_DOI_EXTRACT_PATTERN,
    DATA_DOI_PREFIXES,
    DEDUP_INTERNAL_FIELDS,
    PREPRINT_DOI_PREFIXES,
)
from .log_utils import LogCategory, LogSource, logger

_ARXIV_PUBLISHER_NAMES = ("arxiv", "arxiv.org", "arxiv e-prints")


def _norm_doi(doi: str | None) -> str | None:
    """
    Clean up a DOI string by removing common URL and prefix wrappers and
    normalizing to lowercase for case-insensitive comparison, as DOIs are
    case-insensitive identifiers per the DOI specification.
    """
    if not doi:
        return None
    d = str(doi).strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d, flags=re.IGNORECASE)
    d = re.sub(r"^doi:\s*", "", d, flags=re.IGNORECASE).strip()
    if not d:
        return None
    # URL-decode percent-encoded characters (e.g., %2F → /)
    return urllib.parse.unquote(d).lower()


def normalize_doi(doi: str | None) -> str | None:
    """
    Provide a public helper that normalizes DOIs into a consistent canonical
    form suitable for comparison and lookups.
    """
    return _norm_doi(doi)


def is_secondary_doi(doi: str) -> bool:
    """Check if DOI belongs to preprint or data repository (deprioritize in selection)."""
    lower = doi.lower()
    preprint = any(lower.startswith(p) for p in PREPRINT_DOI_PREFIXES)
    data = any(lower.startswith(p) for p in DATA_DOI_PREFIXES)
    return preprint or data


def _norm_arxiv_id(s: str | None) -> str | None:
    """
    Clean an arXiv identifier by stripping the arXiv prefix and any version
    suffix so that different versions map to the same base ID.
    """
    if not s:
        return None
    t = str(s).strip()
    t = re.sub(r'(?i)^arxiv:\s*', '', t)
    t = re.sub(r'v\d+$', '', t)
    return t.strip() or None


# meta tag patterns for finding DOIs in HTML
_DOI_META_PATTERNS = [
    r'<meta[^>]+name=["\']citation_doi["\'][^>]+content=["\']([^"\']+)["\']',
    r'<meta[^>]+name=["\']dc\.identifier["\'][^>]+content=["\']doi:?\s*([^"\']+)["\']',
    r'<meta[^>]+property=["\']og:doi["\'][^>]+content=["\']([^"\']+)["\']',
]


def find_doi_in_html(html: str) -> str | None:
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
                logger.debug(
                    f"DOI_HTML_SEARCH | meta_match=True | doi={d}",
                    category=LogCategory.AUDIT, source=LogSource.DOI,
                )
                return d
    m = re.search(_DOI_REGEX, html, flags=re.IGNORECASE)
    fulltext_result = _norm_doi(m.group(1)) if m else None
    if fulltext_result:
        logger.debug(
            f"DOI_HTML_SEARCH | fulltext_match=True | doi={fulltext_result}",
            category=LogCategory.AUDIT, source=LogSource.DOI,
        )
    return fulltext_result


def find_doi_in_text(text: str) -> str | None:
    """
    Scan arbitrary text for something that looks like a DOI and return a
    normalized version when one is found.
    """
    if not text:
        return None
    m = re.search(_DOI_REGEX, text, flags=re.IGNORECASE)
    return _norm_doi(m.group(1)) if m else None


def find_arxiv_in_text(text: str) -> str | None:
    """
    Scan text and URLs for an arXiv identifier, handling plain IDs,
    arxiv.org links, and arXiv DOIs before returning a normalized form.
    """
    if not text:
        return None

    # Ordered patterns: prefix, URL, DOI
    _patterns: list[tuple[str, str, int]] = [
        (r'(?i)arxiv[:/\s]*(\d{4}\.\d{4,5})(?:v\d+)?', "prefix", 1),
        (r'(?i)arxiv\.org/(abs|pdf)/(\d{4}\.\d{4,5})', "url", 2),
        (ARXIV_DOI_EXTRACT_PATTERN, "doi", 1),
    ]
    for pattern, label, group in _patterns:
        m = re.search(pattern, text)
        if m:
            result = _norm_arxiv_id(m.group(group))
            logger.debug(
                f"TEXT_SEARCH | matched={label} | found={result or 'none'}",
                category=LogCategory.ARXIV, source=LogSource.ARXIV,
            )
            return result

    return None


def allowlisted_url(url: str | None) -> str | None:
    """
    Accept only URLs that point to trusted resolvers such as doi.org or
    arxiv.org and discard links to generic publisher pages.

    Normalizes DOI URLs to use HTTPS and the modern doi.org domain.
    """
    if not url:
        return None
    u = url.strip()

    # Check for DOI URLs and normalize them
    doi_match = re.search(r'^https?://(dx\.)?doi\.org/(\S+)$', u, flags=re.IGNORECASE)
    if doi_match:
        # Normalize to https://doi.org/...
        doi_suffix = doi_match.group(2)
        result = f"https://doi.org/{doi_suffix}"
        logger.debug(
            f"URL_ALLOWLIST | url={url[:60]} | type=doi | normalized={result}",
            category=LogCategory.AUDIT, source=LogSource.DOI,
        )
        return result

    # Check for arXiv URLs and normalize to HTTPS
    arxiv_match = re.search(r'^https?://arxiv\.org/(abs|pdf)/(\S+)$', u, flags=re.IGNORECASE)
    if arxiv_match:
        # Normalize to https://arxiv.org/...
        arxiv_type = arxiv_match.group(1)
        arxiv_id = arxiv_match.group(2)
        result = f"https://arxiv.org/{arxiv_type}/{arxiv_id}"
        logger.debug(
            f"URL_ALLOWLIST | url={url[:60]} | type=arxiv | normalized={result}",
            category=LogCategory.AUDIT, source=LogSource.ARXIV,
        )
        return result

    logger.debug(
        f"URL_ALLOWLIST | url={url[:60]} | type=rejected | normalized=none",
        category=LogCategory.AUDIT, source=LogSource.DOI,
    )
    return None


def extract_arxiv_eprint(entry: dict[str, Any]) -> str | None:
    """
    Try to recover an arXiv identifier from a BibTeX entry by checking the
    archive prefix, eprint field, DOI (10.48550/arxiv.*), and common text
    fields that may mention arXiv.
    """
    fields = entry.get("fields") or {}
    ap = (fields.get("archiveprefix") or "").lower()
    if ap == "arxiv":
        return _norm_arxiv_id(fields.get("eprint"))
    # Extract from arXiv DOI (10.48550/arxiv.XXXX.YYYYY)
    doi = (fields.get("doi") or "").strip().lower()
    doi_m = re.search(r'10\.48550/arxiv\.(\d{4}\.\d{4,5})', doi)
    if doi_m:
        return _norm_arxiv_id(doi_m.group(1))
    j = fields.get("journal") or fields.get("howpublished") or ""
    m = re.search(r'(?i)arxiv:\s*(\d{4}\.\d{4,5})(v\d+)?', j)
    if m:
        return _norm_arxiv_id(m.group(1))
    return None


def external_ids_match(fields_a: dict[str, Any], fields_b: dict[str, Any]) -> bool:
    """Check if any internal dedup IDs match between two BibTeX entries."""
    for field in DEDUP_INTERNAL_FIELDS:
        a_val = (fields_a.get(field) or "").strip()
        b_val = (fields_b.get(field) or "").strip()
        if a_val and b_val:
            match = a_val == b_val
            logger.debug(
                f"EXTERNAL_ID_CHECK | field={field}"
                f" | a={a_val[:30]} | b={b_val[:30]} | match={match}",
                category=LogCategory.DEDUP, source=LogSource.DOI,
            )
            if match:
                return True
    return False


def normalize_arxiv_metadata(fields: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize arXiv metadata to standard BibTeX fields following best practices.

    Extracts arXiv ID from multiple sources (DOI, pages, journal, URL) and
    converts to standard eprint/archivePrefix/primaryClass fields. Removes
    incorrect publisher='arXiv' entries and handles transition from preprint
    to published version.

    Returns updated fields dictionary with normalized arXiv metadata.
    """
    fields = dict(fields)
    arxiv_id = None
    primary_class = fields.get("primaryclass")

    # 1. check proper eprint field
    if fields.get("archiveprefix", "").lower() == "arxiv" and fields.get("eprint"):
        arxiv_id = _norm_arxiv_id(fields.get("eprint"))

    # 2. check DOI for arXiv pattern (10.48550/arxiv.XXXX.XXXXX)
    if not arxiv_id:
        doi = fields.get("doi", "")
        if doi:
            m = re.search(ARXIV_DOI_EXTRACT_PATTERN, doi)
            if m:
                arxiv_id = _norm_arxiv_id(m.group(1))

    # 3. check pages field for arXiv ID (pages = "arXiv: 2401.12345")
    # Always check and remove pages if it contains arXiv ID (not valid page numbers)
    pages = fields.get("pages", "")
    if pages:
        m = re.search(r'(?i)arxiv:\s*(\d{4}\.\d{4,5})', pages)
        if m:
            if not arxiv_id:
                arxiv_id = _norm_arxiv_id(m.group(1))
            # remove arXiv ID from pages - it doesn't belong there
            logger.debug(
                f"PAGES_REMOVE | val={pages} | reason=contains_arxiv_id",
                category=LogCategory.ARXIV, source=LogSource.ARXIV,
            )
            fields.pop("pages", None)

    # 4. check journal field for arXiv patterns
    if not arxiv_id:
        journal = fields.get("journal", "")
        if journal:
            m = re.search(r'(?i)arxiv:\s*(\d{4}\.\d{4,5})', journal)
            if m:
                arxiv_id = _norm_arxiv_id(m.group(1))

    # 5. check URL for arXiv link
    if not arxiv_id:
        url = fields.get("url", "")
        if url:
            m = re.search(r'(?i)arxiv\.org/(abs|pdf)/(\d{4}\.\d{4,5})', url)
            if m:
                arxiv_id = _norm_arxiv_id(m.group(2))

    logger.debug(
        f"ID_SOURCE | eprint={bool(fields.get('eprint'))}"
        f" | doi={bool(fields.get('doi'))}"
        f" | pages={bool(fields.get('pages'))}"
        f" | journal={bool(fields.get('journal'))}"
        f" | url={bool(fields.get('url'))}"
        f" | id={arxiv_id or 'none'}",
        category=LogCategory.ARXIV, source=LogSource.ARXIV,
    )

    if arxiv_id:
        logger.debug(
            f"SET_EPRINT | id={arxiv_id} | archiveprefix=arXiv"
            f" | primaryclass={primary_class or 'none'}",
            category=LogCategory.ARXIV, source=LogSource.ARXIV,
        )
        fields["eprint"] = arxiv_id
        fields["archiveprefix"] = "arXiv"

        if primary_class:
            fields["primaryclass"] = primary_class

        publisher_val = (fields.get("publisher") or "").strip()
        if publisher_val.lower() in _ARXIV_PUBLISHER_NAMES:
            logger.debug(
                f"PUBLISHER_REMOVE | val={publisher_val}",
                category=LogCategory.ARXIV, source=LogSource.ARXIV,
            )
            fields.pop("publisher", None)

        journal = (fields.get("journal") or "").strip()
        journal_lower = journal.lower()
        is_arxiv_journal = (
            journal_lower in _ARXIV_PUBLISHER_NAMES
            or "arxiv preprint" in journal_lower
            or bool(re.search(r'arxiv:\s*\d{4}\.\d{4,5}', journal_lower))
        )
        if is_arxiv_journal:
            logger.debug(
                f"JOURNAL_REMOVE | old={journal} | reason=arxiv_is_preprint",
                category=LogCategory.ARXIV, source=LogSource.ARXIV,
            )
            fields.pop("journal", None)

        url = fields.get("url", "")
        if not (url and "doi.org" in url.lower()):
            arxiv_url = f"https://arxiv.org/abs/{arxiv_id}"
            logger.debug(
                f"URL_SET | url={arxiv_url}",
                category=LogCategory.ARXIV, source=LogSource.ARXIV,
            )
            fields["url"] = arxiv_url
    else:
        # fallback: normalize journal even if we couldn't extract an arXiv ID
        journal = (fields.get("journal") or "").strip()
        journal_lower = journal.lower()
        if journal_lower in _ARXIV_PUBLISHER_NAMES:
            fields.pop("journal", None)

    return fields
