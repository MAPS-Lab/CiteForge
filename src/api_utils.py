from __future__ import annotations

from enum import Enum
from typing import Dict, Any, List, Tuple, Optional, Callable

from . import bibtex_utils as bt
from .exceptions import ALL_API_ERRORS
from .log_utils import logger, LogSource, LogCategory


class EnrichmentSource(str, Enum):
    """
    Standard enrichment sources for publication metadata.
    """

    SCHOLAR_BIB = "scholar_bib"
    SCHOLAR_PAGE = "scholar_page"
    SEMANTIC_SCHOLAR = "s2"
    CROSSREF = "crossref"
    OPENREVIEW = "openreview"
    ARXIV = "arxiv"
    OPENALEX = "openalex"
    PUBMED = "pubmed"
    EUROPEPMC = "europepmc"
    DOI_CSL = "doi_csl"
    DOI_BIBTEX = "doi_bibtex"
    DATACITE = "datacite"
    ORCID = "orcid"


def enrich_from_source(
    source: EnrichmentSource,
    search_func: Callable,
    build_func: Callable,
    title: str,
    author: Optional[str],
    baseline_entry: Dict[str, Any],
    keyhint: str,
    enr_list: List[Tuple[str, Dict[str, Any]]],
    flags: Dict[str, bool],
    **search_kwargs
) -> bool:
    """
    Execute enrichment workflow for a single API source by performing search,
    BibTeX construction, validation, and updating enrichment tracking structures
    when successful.
    """
    source_name = source.value if isinstance(source, EnrichmentSource) else str(source)


    log_source = _get_log_source(source)
    logger.info("Search and build BibTeX", category=LogCategory.SEARCH, source=log_source)

    try:
        result = search_func(title, author, **search_kwargs)

        if not result:
            logger.info("No result", category=LogCategory.SKIP, source=log_source)
            return False

        bibtex = build_func(result, keyhint=keyhint)

        if not bibtex:
            logger.info("Build failed", category=LogCategory.DEBUG, source=log_source)
            return False

        # Validate against baseline
        baseline_bib = bt.bibtex_from_dict(baseline_entry)
        if not bt.bibtex_entries_match_strict(baseline_bib, bibtex):
            logger.info("Did not match baseline; skipped", category=LogCategory.SKIP, source=log_source)
            return False

        # Success - add to enrichment list
        entry_dict = bt.parse_bibtex_to_dict(bibtex)
        enr_list.append((source_name, entry_dict))
        flags[source_name] = True
        logger.success("Matched and added", category=LogCategory.MATCH, source=log_source)
        return True

    except ALL_API_ERRORS as e:
        logger.warn(f"Enrich error: {e}", category=LogCategory.ERROR, source=log_source)
        return False


def _format_display_name(source: EnrichmentSource) -> str:
    """
    Format enrichment source name for display in logs.
    """
    display_names = {
        EnrichmentSource.SEMANTIC_SCHOLAR: "Semantic Scholar",
        EnrichmentSource.CROSSREF: "Crossref",
        EnrichmentSource.OPENREVIEW: "OpenReview",
        EnrichmentSource.ARXIV: "arXiv",
        EnrichmentSource.OPENALEX: "OpenAlex",
        EnrichmentSource.PUBMED: "PubMed",
        EnrichmentSource.EUROPEPMC: "Europe PMC",
        EnrichmentSource.SCHOLAR_BIB: "Scholar BibTeX",
        EnrichmentSource.SCHOLAR_PAGE: "Scholar Page",
        EnrichmentSource.DOI_CSL: "DOI CSL",
        EnrichmentSource.DOI_BIBTEX: "DOI BibTeX",
        EnrichmentSource.DATACITE: "DataCite",
        EnrichmentSource.ORCID: "ORCID",
    }
    return display_names.get(source, source.value)


def _get_log_source(source: EnrichmentSource) -> str:
    """
    Map EnrichmentSource to LogSource for coloring.
    """
    mapping = {
        EnrichmentSource.SCHOLAR_BIB: LogSource.SCHOLAR,
        EnrichmentSource.SCHOLAR_PAGE: LogSource.SCHOLAR,
        EnrichmentSource.SEMANTIC_SCHOLAR: LogSource.S2,
        EnrichmentSource.CROSSREF: LogSource.CROSSREF,
        EnrichmentSource.OPENREVIEW: LogSource.OPENREVIEW,
        EnrichmentSource.ARXIV: LogSource.ARXIV,
        EnrichmentSource.OPENALEX: LogSource.OPENALEX,
        EnrichmentSource.PUBMED: LogSource.PUBMED,
        EnrichmentSource.EUROPEPMC: LogSource.EUROPEPMC,
        EnrichmentSource.DOI_CSL: LogSource.DOI,
        EnrichmentSource.DOI_BIBTEX: LogSource.DOI,
        EnrichmentSource.DATACITE: LogSource.DOI,
        EnrichmentSource.ORCID: "ORCID",
    }
    return mapping.get(source, LogSource.SYSTEM)
