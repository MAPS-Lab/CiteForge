from __future__ import annotations

from typing import Dict, Any, List, Tuple, Optional

from . import api_clients as api, bibtex_utils as bt
from .exceptions import ALL_API_ERRORS
from .log_utils import logger


def validate_doi_candidate(
    doi: str,
    baseline_entry: Dict[str, Any],
    result_id: str,
    is_early: bool = False
) -> Tuple[bool, bool, Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Validate a DOI by fetching metadata in multiple formats and checking baseline match,
    returning validation success flags and parsed entries.
    """
    csl_matched = False
    bibtex_matched = False
    csl_entry = None
    bibtex_entry = None
    csl_failed = False
    bibtex_failed = False
    doi_bib = None
    csl = None

    doi_prefix = "Early DOI" if is_early else "DOI"

    # Try CSL-JSON format
    try:
        csl = api.fetch_csl_via_doi(doi)
        if csl:
            csl_bib = api.bibtex_from_csl(csl, keyhint=result_id)
            if csl_bib and bt.bibtex_entries_match_strict(bt.bibtex_from_dict(baseline_entry), csl_bib):
                csl_entry = bt.parse_bibtex_to_dict(csl_bib)
                logger.success(f"{doi_prefix} {doi}: CSL format validated and added")
                csl_matched = True
            else:
                csl_failed = True
        else:
            csl_failed = True
    except ALL_API_ERRORS as e:
        logger.warn(f"{doi_prefix} {doi}: CSL fetch failed: {e}")
        csl_failed = True

    # Try BibTeX format
    try:
        doi_bib = api.fetch_bibtex_via_doi(doi)
        if doi_bib and bt.bibtex_entries_match_strict(bt.bibtex_from_dict(baseline_entry), doi_bib):
            bibtex_entry = bt.parse_bibtex_to_dict(doi_bib)
            logger.success(f"{doi_prefix} {doi}: BibTeX format validated and added")
            bibtex_matched = True
        else:
            bibtex_failed = True
    except ALL_API_ERRORS as e:
        logger.warn(f"{doi_prefix} {doi}: BibTeX fetch failed: {e}")
        bibtex_failed = True

    # Determine overall validation result
    doi_matched = csl_matched or bibtex_matched

    if not doi_matched and csl_failed and bibtex_failed:
        logger.warn(f"{doi_prefix} {doi} rejected: neither CSL nor BibTeX metadata matches baseline", indent=2)
        baseline_title = bt.normalize_title(baseline_entry.get("fields", {}).get("title"))

        # Check CSL title if available
        if csl and not csl_matched:
            try:
                csl_bib_check = api.bibtex_from_csl(csl, keyhint=result_id)
                if csl_bib_check:
                    csl_dict = bt.parse_bibtex_to_dict(csl_bib_check)
                    csl_title = bt.normalize_title(csl_dict.get("fields", {}).get("title"))
                    from .text_utils import title_similarity
                    sim = title_similarity(baseline_title, csl_title)
                    logger.info(f"    CSL title similarity: {sim:.2f}", indent=2)
            except Exception:
                pass

        # Check BibTeX title if available
        if doi_bib and not bibtex_matched:
            try:
                bib_dict = bt.parse_bibtex_to_dict(doi_bib)
                bib_title = bt.normalize_title(bib_dict.get("fields", {}).get("title"))
                from .text_utils import title_similarity
                sim = title_similarity(baseline_title, bib_title)
                logger.info(f"    BibTeX title similarity: {sim:.2f}", indent=2)
            except Exception:
                pass

    return csl_matched, bibtex_matched, csl_entry, bibtex_entry


def process_validated_doi(
    doi: str,
    baseline_entry: Dict[str, Any],
    result_id: str,
    enr_list: List[Tuple[str, Dict[str, Any]]],
    flags: Dict[str, bool],
    is_early: bool = False
) -> bool:
    """
    Validate a DOI and update enrichment tracking structures, returning True if DOI
    validated successfully in at least one format.
    """
    csl_matched, bibtex_matched, csl_entry, bibtex_entry = validate_doi_candidate(
        doi, baseline_entry, result_id, is_early
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
        prefix = "Early DOI" if is_early else "DOI"
        logger.success(f"{prefix} {doi} validated successfully ({', '.join(formats)})")

    return doi_matched
