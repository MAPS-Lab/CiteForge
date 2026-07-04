"""DOI validation through content negotiation.

Confirms a candidate DOI by fetching its metadata in CSL-JSON and BibTeX and
checking it against the baseline entry with a strict title-and-author match, so
enrichment only accepts a DOI that genuinely describes the same work.
"""

from __future__ import annotations

import contextlib
from typing import Any

from . import bibtex_utils as bt
from .clients import search_apis
from .exceptions import ALL_API_ERRORS
from .log_utils import LogCategory, LogSource, logger
from .text_utils import normalize_title, title_similarity


def _parse_and_match(
    raw_bib: str,
    baseline_entry: dict[str, Any],
    doi: str,
    label: str,
    format_name: str,
) -> tuple[bool, dict[str, Any] | None]:
    """Parse a fetched BibTeX string and strict-match it against the baseline.

    Shared tail of the CSL and BibTeX validators. *label* prefixes the debug
    log lines and *format_name* names the format in the success message.
    """
    entry = bt.parse_bibtex_to_dict(raw_bib)
    logger.debug(
        f"{label}_PARSE | entry_ok={entry is not None}",
        category=LogCategory.DOI_VAL,
        source=LogSource.DOI,
    )
    if entry is None:
        return False, None

    strict_match = bt.bibtex_entries_match_strict(baseline_entry, entry)
    logger.debug(f"{label}_MATCH | strict_match={strict_match}", category=LogCategory.DOI_VAL, source=LogSource.DOI)
    if strict_match:
        logger.success(
            f"{doi}: {format_name} format validated and added",
            category=LogCategory.MATCH,
            source=LogSource.DOI,
        )
        return True, entry
    return False, None


def _validate_csl(doi: str, baseline_entry: dict[str, Any], result_id: str) -> tuple[bool, dict[str, Any] | None, Any]:
    """Validate a DOI through its CSL-JSON metadata."""
    logger.debug(f"CSL_START | doi={doi}", category=LogCategory.DOI_VAL, source=LogSource.DOI)
    try:
        csl = search_apis.fetch_csl_via_doi(doi)
        logger.debug(f"CSL_FETCH | result={csl is not None}", category=LogCategory.DOI_VAL, source=LogSource.DOI)
        if not csl:
            return False, None, None

        csl_bib = search_apis.bibtex_from_csl(csl, keyhint=result_id)
        logger.debug(
            f"CSL_CONVERT | bibtex_ok={csl_bib is not None}",
            category=LogCategory.DOI_VAL,
            source=LogSource.DOI,
        )
        if not csl_bib:
            return False, None, None

        matched, csl_entry = _parse_and_match(csl_bib, baseline_entry, doi, "CSL", "CSL")
        if matched:
            return True, csl_entry, csl

    except ALL_API_ERRORS as e:
        logger.debug(
            f"CSL_ERROR | doi={doi} | error={type(e).__name__}: {e}",
            category=LogCategory.DOI_VAL,
            source=LogSource.DOI,
        )
        logger.warn(f"{doi}: CSL fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DOI)

    return False, None, None


def _validate_bibtex(doi: str, baseline_entry: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, Any]:
    """Validate a DOI through its BibTeX metadata."""
    logger.debug(f"BIBTEX_START | doi={doi}", category=LogCategory.DOI_VAL, source=LogSource.DOI)
    try:
        doi_bib = search_apis.fetch_bibtex_via_doi(doi)
        logger.debug(f"BIBTEX_FETCH | result={doi_bib is not None}", category=LogCategory.DOI_VAL, source=LogSource.DOI)
        if not doi_bib:
            return False, None, None

        matched, bibtex_entry = _parse_and_match(doi_bib, baseline_entry, doi, "BIBTEX", "BibTeX")
        if matched:
            return True, bibtex_entry, doi_bib

    except ALL_API_ERRORS as e:
        logger.debug(
            f"BIBTEX_ERROR | doi={doi} | error={type(e).__name__}: {e}",
            category=LogCategory.DOI_VAL,
            source=LogSource.DOI,
        )
        logger.warn(f"{doi}: BibTeX fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DOI)

    return False, None, None


def _log_rejection_details(doi: str, baseline_entry: dict[str, Any], result_id: str, csl: Any, doi_bib: Any) -> None:
    """
    Log details about why validation failed, checking title similarity.
    """
    logger.warn(
        f"{doi} rejected: neither CSL nor BibTeX metadata matches baseline",
        category=LogCategory.SKIP,
        source=LogSource.DOI,
    )
    baseline_title = normalize_title(baseline_entry.get("fields", {}).get("title"))

    def _title_sim_from_bibtex(raw_bibtex: str | None) -> float:
        """Extract title similarity from raw BibTeX string, returning -1.0 on failure."""
        if not raw_bibtex:
            return -1.0
        try:
            parsed = bt.parse_bibtex_to_dict(raw_bibtex)
            if parsed:
                return title_similarity(
                    baseline_title,
                    normalize_title(parsed.get("fields", {}).get("title")),
                )
        except Exception:  # noqa: S110
            pass
        return -1.0

    csl_bib = None
    if csl:
        with contextlib.suppress(Exception):
            csl_bib = search_apis.bibtex_from_csl(csl, keyhint=result_id)
    csl_title_sim = _title_sim_from_bibtex(csl_bib)
    bibtex_title_sim = _title_sim_from_bibtex(doi_bib)

    logger.debug(
        f"REJECTION_DETAIL | doi={doi}"
        f" | csl_title_sim={csl_title_sim:.2f}"
        f" | bibtex_title_sim={bibtex_title_sim:.2f}"
        f" | baseline_title={baseline_title[:50] if baseline_title else 'none'}",
        category=LogCategory.DOI_VAL,
        source=LogSource.DOI,
    )


def validate_doi_candidate(
    doi: str, baseline_entry: dict[str, Any], result_id: str
) -> tuple[bool, bool, dict[str, Any] | None, dict[str, Any] | None]:
    """
    Validate a DOI by fetching metadata in multiple formats and checking baseline match,
    returning validation success flags and parsed entries.
    """
    csl_matched, csl_entry, csl = _validate_csl(doi, baseline_entry, result_id)

    # Skip BibTeX fetch when CSL already matched
    if not csl_matched:
        bibtex_matched, bibtex_entry, doi_bib = _validate_bibtex(doi, baseline_entry)
    else:
        bibtex_matched, bibtex_entry, doi_bib = False, None, None

    doi_matched = csl_matched or bibtex_matched

    logger.debug(
        f"VALIDATE | doi={doi} | csl_tried=True | csl_matched={csl_matched}"
        f" | bibtex_tried={not csl_matched} | bibtex_matched={bibtex_matched}"
        f" | overall={doi_matched}",
        category=LogCategory.DOI_VAL,
        source=LogSource.DOI,
    )

    if not doi_matched:
        _log_rejection_details(doi, baseline_entry, result_id, csl, doi_bib)

    return csl_matched, bibtex_matched, csl_entry, bibtex_entry


def process_validated_doi(
    doi: str,
    baseline_entry: dict[str, Any],
    result_id: str,
    enr_list: list[tuple[str, dict[str, Any]]],
    flags: dict[str, bool],
) -> bool:
    """
    Validate a DOI and update enrichment tracking structures, returning True if DOI
    validated successfully in at least one format.
    """
    csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(doi, baseline_entry, result_id)

    if csl_entry:
        enr_list.append(("csl", csl_entry))
        flags["doi_csl"] = True
    if bibtex_entry:
        enr_list.append(("doi_bibtex", bibtex_entry))
        flags["doi_bibtex"] = True

    if csl_matched or bibtex_matched:
        formats = [name for name, ok in [("CSL", csl_matched), ("BibTeX", bibtex_matched)] if ok]
        logger.success(
            f"{doi} validated successfully ({', '.join(formats)})",
            category=LogCategory.MATCH,
            source=LogSource.DOI,
        )

    return csl_matched or bibtex_matched
