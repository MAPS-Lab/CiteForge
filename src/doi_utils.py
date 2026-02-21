from __future__ import annotations

from typing import Any

from . import bibtex_utils as bt
from .clients import search_apis
from .exceptions import ALL_API_ERRORS
from .log_utils import LogCategory, LogSource, logger
from .text_utils import title_similarity


def _validate_csl(
    doi: str,
    baseline_entry: dict[str, Any],
    result_id: str
) -> tuple[bool, dict[str, Any] | None, Any]:
    """
    Helper to validate DOI using CSL-JSON format.
    """
    try:
        csl = search_apis.fetch_csl_via_doi(doi)
        if csl:
            csl_bib = search_apis.bibtex_from_csl(csl, keyhint=result_id)
            if csl_bib:
                csl_entry = bt.parse_bibtex_to_dict(csl_bib)
                if csl_entry and bt.bibtex_entries_match_strict(baseline_entry, csl_entry):
                    logger.success(
                        f"{doi}: CSL format validated and added",
                        category=LogCategory.MATCH, source=LogSource.DOI,
                    )
                    return True, csl_entry, csl
    except ALL_API_ERRORS as e:
        logger.warn(f"{doi}: CSL fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DOI)

    return False, None, None


def _validate_bibtex(
    doi: str,
    baseline_entry: dict[str, Any]
) -> tuple[bool, dict[str, Any] | None, Any]:
    """
    Helper to validate DOI using BibTeX format.
    """
    try:
        doi_bib = search_apis.fetch_bibtex_via_doi(doi)
        if doi_bib:
            bibtex_entry = bt.parse_bibtex_to_dict(doi_bib)
            if bibtex_entry and bt.bibtex_entries_match_strict(baseline_entry, bibtex_entry):
                logger.success(
                    f"{doi}: BibTeX format validated and added",
                    category=LogCategory.MATCH, source=LogSource.DOI,
                )
                return True, bibtex_entry, doi_bib
    except ALL_API_ERRORS as e:
        logger.warn(f"{doi}: BibTeX fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DOI)

    return False, None, None


def _log_rejection_details(
    doi: str,
    baseline_entry: dict[str, Any],
    result_id: str,
    csl: Any,
    doi_bib: Any
) -> None:
    """
    Log details about why validation failed, checking title similarity.
    """
    logger.warn(
        f"{doi} rejected: neither CSL nor BibTeX metadata matches baseline",
        category=LogCategory.SKIP, source=LogSource.DOI,
    )
    baseline_title = bt.normalize_title(baseline_entry.get("fields", {}).get("title"))

    # Check CSL title if available
    if csl:
        try:
            csl_bib_check = search_apis.bibtex_from_csl(csl, keyhint=result_id)
            if csl_bib_check:
                csl_dict = bt.parse_bibtex_to_dict(csl_bib_check)
                csl_title = bt.normalize_title((csl_dict or {}).get("fields", {}).get("title"))
                sim = title_similarity(baseline_title, csl_title)
                logger.info(f"    CSL title similarity: {sim:.2f}", category=LogCategory.DEBUG, source=LogSource.DOI)
        except Exception:  # noqa: S110
            pass

    # Check BibTeX title if available
    if doi_bib:
        try:
            bib_dict = bt.parse_bibtex_to_dict(doi_bib)
            bib_title = bt.normalize_title((bib_dict or {}).get("fields", {}).get("title"))
            sim = title_similarity(baseline_title, bib_title)
            logger.info(f"    BibTeX title similarity: {sim:.2f}", category=LogCategory.DEBUG, source=LogSource.DOI)
        except Exception:  # noqa: S110
            pass


def validate_doi_candidate(
    doi: str,
    baseline_entry: dict[str, Any],
    result_id: str
) -> tuple[bool, bool, dict[str, Any] | None, dict[str, Any] | None]:
    """
    Validate a DOI by fetching metadata in multiple formats and checking baseline match,
    returning validation success flags and parsed entries.
    """
    # Try CSL-JSON format first
    csl_matched, csl_entry, csl = _validate_csl(doi, baseline_entry, result_id)

    # Skip BibTeX fetch when CSL already matched — saves 1 HTTP call per validated DOI
    if not csl_matched:
        bibtex_matched, bibtex_entry, doi_bib = _validate_bibtex(doi, baseline_entry)
    else:
        bibtex_matched, bibtex_entry, doi_bib = False, None, None

    # Determine overall validation result
    doi_matched = csl_matched or bibtex_matched

    if not doi_matched:
        _log_rejection_details(doi, baseline_entry, result_id, csl, doi_bib)

    return csl_matched, bibtex_matched, csl_entry, bibtex_entry


def process_validated_doi(
    doi: str,
    baseline_entry: dict[str, Any],
    result_id: str,
    enr_list: list[tuple[str, dict[str, Any]]],
    flags: dict[str, bool]
) -> bool:
    """
    Validate a DOI and update enrichment tracking structures, returning True if DOI
    validated successfully in at least one format.
    """
    csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
        doi, baseline_entry, result_id
    )

    # Add validated entries to enrichment list
    if csl_entry:
        enr_list.append(("csl", csl_entry))
        flags["doi_csl"] = True
    if bibtex_entry:
        enr_list.append(("doi_bibtex", bibtex_entry))
        flags["doi_bibtex"] = True

    doi_matched = csl_matched or bibtex_matched

    if doi_matched:
        # Build clear success message
        formats = []
        if csl_matched:
            formats.append("CSL")
        if bibtex_matched:
            formats.append("BibTeX")

        logger.success(
            f"{doi} validated successfully ({', '.join(formats)})",
            category=LogCategory.MATCH, source=LogSource.DOI,
        )

    return doi_matched
