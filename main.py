from __future__ import annotations

import os
import re
import time
from typing import List, Optional, Dict, Any, Tuple, Callable

from src import bibtex_utils as bt, api_clients as api, merge_utils as mu
from src import id_utils as idu
from src.doi_utils import process_validated_doi
from src.config import (
    DATA_FILE,
    OUTPUT_DIR,
    REPORTS_DIR,
    SUMMARY_CSV,
    TRUST_ORDER,
    SKIP_SERPAPI_FOR_EXISTING_FILES,
    REQUEST_DELAY_BETWEEN_ARTICLES,
    CONTRIBUTION_WINDOW_YEARS,
)
from src.exceptions import (
    ALL_API_ERRORS,
    RateLimitException,
    NetworkException,
    DataParsingException,
    FileIOException,
    ConfigurationException,
)
from src.http_utils import (
    fetch_url_json,
    fetch_url_text,
)
from src.io_utils import read_api_key, read_semantic_api_key, read_records, read_openreview_credentials, \
    init_summary_csv, append_summary_to_csv, save_bibtex_file, load_existing_bibtex_files
from src.log_utils import logger
from src.models import Record
from src.text_utils import trim_title_default


def _try_multiple_candidates(
    source_name: str,
    candidates: List[Any],
    build_func: Callable,
    baseline_entry: Dict[str, Any],
    result_id: str,
    enr_list: List[Tuple[str, Dict[str, Any]]],
    flags: Dict[str, bool],
    flag_key: str,
    max_candidates: int = 5
) -> Tuple[bool, Optional[Any]]:
    """
    Try validating multiple candidates from an API source.

    Tries each candidate in order (assumed to be sorted by relevance),
    building BibTeX and validating against baseline until a match is found.

    Returns (matched: bool, matched_candidate: Optional[Any]) tuple.
    """
    if not candidates:
        return False, None

    candidates_to_try = candidates[:max_candidates]
    logger.info(f"{source_name}: trying {len(candidates_to_try)} candidate(s)", indent=2)

    for idx, candidate in enumerate(candidates_to_try, 1):
        try:
            candidate_bib = build_func(candidate, keyhint=result_id)
            if not candidate_bib:
                logger.info(f"Candidate {idx}: BibTeX build failed", indent=3)
                continue

            if bt.bibtex_entries_match_strict(bt.bibtex_from_dict(baseline_entry), candidate_bib):
                enr_list.append((flag_key, bt.parse_bibtex_to_dict(candidate_bib)))
                flags[flag_key] = True
                logger.success(f"Candidate {idx}: validated and enriched", indent=3)

                # Show what matched
                candidate_dict = bt.parse_bibtex_to_dict(candidate_bib)
                candidate_title = candidate_dict.get('fields', {}).get('title', '')
                if isinstance(candidate_title, list):
                    candidate_title = candidate_title[0] if candidate_title else ''

                from CiteForge.text_utils import normalize_title
                candidate_title_norm = normalize_title(candidate_title)
                logger.info(f"Title (normalized): {candidate_title_norm}", indent=4)
                return True, candidate
            else:
                logger.info(f"Candidate {idx}: did not match baseline", indent=3)
        except Exception as e:
            logger.info(f"Candidate {idx}: error - {e}", indent=3)
            continue

    return False, None


def _log_enrichment_decision(source_name: str, found: bool, built: bool, matched: bool,
                             baseline_entry: Optional[Dict] = None, candidate_bib: Optional[str] = None,
                             reason: Optional[str] = None):
    """
    Log structured information about an enrichment decision.

    Shows what was found, compared, and why it was accepted/rejected.
    """
    if not found:
        logger.info(f"{source_name}: no matching publication found in database", indent=1)
        return

    if not built:
        logger.warn(f"{source_name}: API returned data but BibTeX construction failed", indent=1)
        return

    if matched:
        # Success case - show what was validated (using normalized comparison data)
        candidate = bt.parse_bibtex_to_dict(candidate_bib)
        candidate_title = candidate.get('fields', {}).get('title', '')
        candidate_authors = candidate.get('fields', {}).get('author', '')

        # Handle title being a list (extract first element)
        if isinstance(candidate_title, list):
            candidate_title = candidate_title[0] if candidate_title else ''

        # Show the normalized data that was actually compared
        from CiteForge.text_utils import normalize_title, parse_authors_any, name_signature
        candidate_title_norm = normalize_title(candidate_title)
        candidate_authors_parsed = parse_authors_any(candidate_authors)
        candidate_signatures = [name_signature(n) for n in candidate_authors_parsed]

        logger.success(f"{source_name}: validated and enriched", indent=1)
        logger.info(f"Title (normalized): {candidate_title_norm}", indent=2)
        logger.info(f"Authors parsed: {candidate_authors_parsed}", indent=2)
        logger.info(f"Authors signatures: {candidate_signatures}", indent=2)
    else:
        # Rejection case - show comparison
        baseline = baseline_entry or {}
        candidate = bt.parse_bibtex_to_dict(candidate_bib)

        base_title = baseline.get('fields', {}).get('title', '')
        base_authors = baseline.get('fields', {}).get('author', '')
        base_year = baseline.get('fields', {}).get('year', '')

        cand_title = candidate.get('fields', {}).get('title', '')
        cand_authors = candidate.get('fields', {}).get('author', '')
        cand_year = candidate.get('fields', {}).get('year', '')

        # Handle titles being lists (extract first element)
        if isinstance(base_title, list):
            base_title = base_title[0] if base_title else ''
        if isinstance(cand_title, list):
            cand_title = cand_title[0] if cand_title else ''

        logger.info(f"{source_name}: found but validation failed", indent=1)
        logger.info(f"Comparison with baseline:", indent=2)

        # Show title comparison using normalized values (actual comparison data)
        from CiteForge.text_utils import normalize_title
        base_title_norm = normalize_title(base_title)
        cand_title_norm = normalize_title(cand_title)

        if base_title_norm != cand_title_norm:
            logger.info(f"Title mismatch", indent=3)
            logger.info(f"Baseline (normalized): {base_title_norm}", indent=4)
            logger.info(f"{source_name} (normalized): {cand_title_norm}", indent=4)
        else:
            logger.info(f"Title matches", indent=3)
            logger.info(f"Normalized: {base_title_norm}", indent=4)

        # Show author comparison using actual comparison logic
        from CiteForge.text_utils import authors_overlap, parse_authors_any, name_signature
        if base_authors and cand_authors:
            # Show what the comparison logic actually sees
            base_authors_parsed = parse_authors_any(base_authors)
            cand_authors_parsed = parse_authors_any(cand_authors)
            base_signatures = [name_signature(n) for n in base_authors_parsed]
            cand_signatures = [name_signature(n) for n in cand_authors_parsed]

            overlap = authors_overlap(base_authors, cand_authors)
            if not overlap:
                logger.info(f"Authors mismatch", indent=3)
                logger.info(f"Baseline parsed: {base_authors_parsed}", indent=4)
                logger.info(f"Baseline signatures: {base_signatures}", indent=4)
                logger.info(f"{source_name} parsed: {cand_authors_parsed}", indent=4)
                logger.info(f"{source_name} signatures: {cand_signatures}", indent=4)
            else:
                logger.info(f"Authors overlap", indent=3)
                logger.info(f"Baseline signatures: {base_signatures}", indent=4)
                logger.info(f"{source_name} signatures: {cand_signatures}", indent=4)

        # Show year comparison
        if base_year and cand_year:
            if str(base_year) != str(cand_year):
                logger.info(f"Year mismatch", indent=3)
                logger.info(f"Baseline: {base_year}", indent=4)
                logger.info(f"{source_name}: {cand_year}", indent=4)
            else:
                logger.info(f"Year matches: {cand_year}", indent=3)

        if reason:
            logger.info(f"Reason: {reason}", indent=2)


def process_article(rec: Record, art: Dict[str, Any], api_key: str, out_dir: str, s2_api_key: Optional[str],
                    or_creds: Optional[tuple], idx: Optional[int] = None, total: Optional[int] = None,
                    gemini_api_key: Optional[str] = None, summary_csv_path: Optional[str] = None) -> int:
    """
    Handle a single publication.

    Start from the Scholar metadata, build a baseline BibTeX entry, enrich it
    with data from other services when available, and save the merged result to
    disk.

    Returns 1 when the article was processed successfully and a file was
    written, or 0 when the article had to be skipped or an unrecoverable error
    occurred.
    """
    title = trim_title_default(api.strip_html_tags(art.get("title") or ""))
    authors_list = api.extract_authors_from_article(art) or []
    year_hint = api.get_article_year(art) or None
    # figure out IDs - Scholar sometimes gives us multiple ways to identify an article
    citation_id = art.get("citation_id") or art.get("result_id")
    cluster_id = art.get("cluster_id") or (
        art.get("result_id") if citation_id and art.get("result_id") != citation_id else None)
    result_id = citation_id or re.sub(r"\W+", "_", title or "untitled")
    # Note: API result variables initialized before use in each search section below
    flags = {
        "scholar_bib": False,
        "scholar_page": False,
        "s2": False,
        "crossref": False,
        "openreview": False,
        "arxiv": False,
        "openalex": False,
        "pubmed": False,
        "europepmc": False,
        "doi_csl": False,
        "doi_bibtex": False,
    }
    # Note: DataCite and ORCID are utility APIs, not standard enrichment sources
    # - DataCite DOIs are handled via doi_csl/doi_bibtex (doi.org resolves both Crossref and DataCite)
    # - ORCID requires author ORCID ID and is for author-level publication fetching

    if not title:
        logger.error("Missing required field: title; skipping article", indent=1)
        return 0
    if not authors_list or not year_hint:
        logger.warn("Article missing authors and/or year; continuing with best-effort enrichment", indent=1)

    idx_prefix = f"[{idx}/{total}] " if (isinstance(idx, int) and isinstance(total, int)) else ""
    src = (art.get("source") or "scholar").strip()
    meta_bits = []
    if year_hint:
        meta_bits.append(str(year_hint))
    if src:
        meta_bits.append(src)
    meta = ", ".join(meta_bits)
    logger.substep(f"{idx_prefix}Article: {title}{(' (' + meta + ')') if meta else ''}")

    # Check if BibTeX file already exists for this article (optimization to reduce SerpAPI usage)
    from CiteForge.text_utils import format_author_dirname
    author_dirname = format_author_dirname(rec.name, rec.scholar_id)
    author_dir = os.path.join(out_dir, author_dirname)
    existing_file_loaded = False
    baseline_entry = None
    existing_file_path = None

    # Try to find existing BibTeX file to use as enrichment seed
    # If found, load it and use as baseline - enrichment process will update/fix fields
    if SKIP_SERPAPI_FOR_EXISTING_FILES and os.path.exists(author_dir):
        for filename in os.listdir(author_dir):
            if filename.endswith('.bib'):
                file_path = os.path.join(author_dir, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        existing_bib = f.read()
                    existing_entry = bt.parse_bibtex_to_dict(existing_bib)

                    # Check if this file matches our article by comparing title
                    if existing_entry:
                        existing_title = existing_entry.get('fields', {}).get('title', '')
                        if isinstance(existing_title, list):
                            existing_title = existing_title[0] if existing_title else ''

                        from CiteForge.text_utils import title_similarity
                        if title_similarity(title, existing_title) > 0.9:
                            baseline_entry = existing_entry
                            existing_file_path = file_path
                            existing_file_loaded = True
                            logger.info(f"Existing BibTeX found: {filename}", indent=1)
                            logger.success(
                                "Using existing file as baseline seed - "
                                "will update/enrich with fresh data",
                                indent=1
                            )
                            break
                except (OSError, ValueError, TypeError, UnicodeDecodeError):
                    # Skip files that can't be read or parsed
                    continue

    # If no existing file found, build minimal BibTeX baseline
    if not existing_file_loaded:
        logger.info("Building minimal BibTeX baseline", indent=1)
        authors_list = api.extract_authors_from_article(art) or []
        year = api.get_article_year(art)
        scholar_bib = bt.build_minimal_bibtex(title, authors_list, year, keyhint=result_id)

        baseline_entry = bt.parse_bibtex_to_dict(scholar_bib)
        if baseline_entry is None:
            # Parse failed - should be rare since we generated the BibTeX
            logger.error("Failed to parse Scholar BibTeX; using minimal fallback structure", indent=1)
            baseline_entry = {
                "type": "misc",
                "key": result_id or "entry",
                "fields": {"title": title} if title else {}
            }
    # Keep track of where this came from by storing Scholar IDs (but skip x_scholar_citation_id)
    bf = baseline_entry.get("fields") or {}
    if cluster_id:
        bf["x_scholar_cluster_id"] = cluster_id
    # Let's see if we can find an arXiv ID or DOI in the article snippet or links right away
    try:
        snippet = (art.get("snippet") or art.get("publication_info") or "")
        ax_from_snip = idu.find_arxiv_in_text(snippet)
        _doi_from_snip = idu.find_doi_in_text(snippet)  # Found but not used (snippets not trustworthy)
        # Gather all the links we can find in the article data
        link_texts: List[str] = []
        for k in ("link", "link_to_pdf"):
            if art.get(k):
                link_texts.append(str(art.get(k)))
        res = art.get("resources") or []
        if isinstance(res, list):
            for r in res:
                for lk in ("link", "file_link", "url"):
                    v = (r.get(lk) if isinstance(r, dict) else None) or None
                    if v:
                        link_texts.append(str(v))
        inline = art.get("inline_links") or {}
        for fld in ("versions", "resources", "websites"):
            arr = inline.get(fld) or []
            if isinstance(arr, list):
                for it in arr:
                    v = (it.get("link") if isinstance(it, dict) else None) or None
                    if v:
                        link_texts.append(str(v))
        ax_from_links = None
        doi_from_links = None
        for u in link_texts:
            if not ax_from_links:
                ax_from_links = idu.find_arxiv_in_text(u)
            if not doi_from_links:
                doi_from_links = idu.find_doi_in_text(u)
        # If we found an arXiv ID, add it now so it doesn't get lost later
        ax_pick = ax_from_snip or ax_from_links
        if ax_pick:
            bf["eprint"] = ax_pick
            bf["archiveprefix"] = "arXiv"
        # We found a DOI but we're not adding it yet - snippets aren't trustworthy enough.
        # We'll only add DOIs from reliable sources like Crossref or the DOI resolver itself.
    except PARSE_ERRORS:
        pass
    baseline_entry["fields"] = bf
    ck = bt.build_standard_citekey(baseline_entry, gemini_api_key=gemini_api_key) or baseline_entry.get(
        "key") or "Entry"
    baseline_entry["key"] = ck

    # Save baseline only if we didn't load from existing file
    if existing_file_loaded:
        path = existing_file_path
        logger.info(f"Using existing file: {path}", indent=1)
    else:
        path = mu.save_entry_to_file(out_dir, rec.scholar_id, baseline_entry, gemini_api_key=gemini_api_key,
                                     author_name=rec.name)
        logger.success(f"Saved baseline: {path}", indent=1)

    enr_list: List[Tuple[str, Dict[str, Any]]] = []

    # if the baseline already has a DOI, use it to get better metadata early on
    doi_validated = False  # Track if we successfully validated the DOI
    try:
        doi_early = idu.normalize_doi((baseline_entry.get("fields") or {}).get("doi"))
        if doi_early:
            logger.info(f"DOI negotiation (early): validating baseline DOI {doi_early}", indent=1)
            doi_matched = process_validated_doi(
                doi_early, baseline_entry, result_id, enr_list, flags, is_early=True
            )

            # If DOI failed validation, remove it from baseline
            if not doi_matched:
                baseline_entry.get("fields", {}).pop("doi", None)
                logger.warn(f"Removed unvalidated DOI {doi_early} from baseline", indent=2)
            else:
                doi_validated = True
    except PARSE_ERRORS:
        pass

    # Skip SerpAPI citation fetch if we loaded an existing file (optimization to reduce API usage)
    if existing_file_loaded:
        logger.info("Scholar citation: skipped (existing file loaded)", indent=1)
    else:
        logger.info("Scholar citation: fetch via SerpAPI", indent=1)
        if citation_id:
            try:
                fields = api.fetch_scholar_citation_via_serpapi(api_key, rec.scholar_id, citation_id)
                if fields:
                    sch_page_bib = api.build_bibtex_from_scholar_fields(fields, keyhint=result_id)
                    baseline_bib = bt.bibtex_from_dict(baseline_entry)
                    if sch_page_bib and bt.bibtex_entries_match_strict(baseline_bib, sch_page_bib):
                        enr_list.append(("scholar_page", bt.parse_bibtex_to_dict(sch_page_bib)))
                        flags["scholar_page"] = True
                        logger.success("Scholar citation matched and added", indent=1)
                    else:
                        logger.info("Scholar citation did not match baseline; skipped", indent=1)
                else:
                    logger.info("Scholar citation: no data returned from SerpAPI", indent=1)
            except ALL_API_ERRORS as e:
                logger.warn(f"Scholar citation fetch error: {e}", indent=1)
        else:
            logger.info("Scholar citation: no citation_id available; skipped", indent=1)

    logger.info("Semantic Scholar: search and build BibTeX", indent=1)
    s2_paper = None
    if s2_api_key:
        try:
            s2_papers = api.s2_search_papers_multiple(title, rec.name, s2_api_key, max_results=5)
            if s2_papers:
                matched, s2_paper = _try_multiple_candidates(
                    "Semantic Scholar",
                    s2_papers,
                    api.build_bibtex_from_s2,
                    baseline_entry,
                    result_id,
                    enr_list,
                    flags,
                    "s2",
                    max_candidates=5
                )
                if not matched:
                    logger.info("Semantic Scholar: no candidates matched baseline", indent=2)
                    s2_paper = None
            else:
                logger.info("Semantic Scholar: no matching publication found in database", indent=1)
        except ALL_API_ERRORS as e:
            logger.warn(f"Semantic Scholar: API error - {e}", indent=1)
    else:
        logger.info("Semantic Scholar: skipped (no API key)", indent=1)

    logger.info("Crossref: search and build BibTeX", indent=1)
    cr_item = None
    try:
        cr_items = api.crossref_search_multiple(title, rec.name, max_results=5)
        if cr_items:
            matched, cr_item = _try_multiple_candidates(
                "Crossref",
                cr_items,
                api.build_bibtex_from_crossref,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "crossref",
                max_candidates=5
            )
            if not matched:
                logger.info("Crossref: no candidates matched baseline", indent=2)
                cr_item = None
        else:
            logger.info("Crossref: no matching publication found in database", indent=1)
    except ALL_API_ERRORS as e:
        logger.warn(f"Crossref: API error - {e}", indent=1)

    logger.info("OpenReview: search and build BibTeX", indent=1)
    try:
        or_notes = api.openreview_search_papers_multiple(title, rec.name, or_creds, max_results=5)
        if or_notes:
            matched, _or_note = _try_multiple_candidates(
                "OpenReview",
                or_notes,
                api.build_bibtex_from_openreview,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "openreview",
                max_candidates=5
            )
            if not matched:
                logger.info("OpenReview: no candidates matched baseline", indent=2)
        else:
            logger.info("OpenReview: no matching publication found in database", indent=1)
    except ALL_API_ERRORS as e:
        logger.warn(f"OpenReview: API error - {e}", indent=1)

    logger.info("arXiv: search and build BibTeX", indent=1)
    arxiv_entry = None
    try:
        arxiv_entries = api.arxiv_search(title, rec.name, year_hint)
        if arxiv_entries:
            matched, arxiv_entry = _try_multiple_candidates(
                "arXiv",
                arxiv_entries,
                api.build_bibtex_from_arxiv,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "arxiv",
                max_candidates=5
            )
            if not matched:
                logger.info("arXiv: no candidates matched baseline", indent=2)
                arxiv_entry = None
        else:
            logger.info("arXiv: no matching publication found in database", indent=1)
    except ALL_API_ERRORS as e:
        logger.warn(f"arXiv: API error - {e}", indent=1)

    logger.info("OpenAlex: search and build BibTeX", indent=1)
    oa_work = None
    try:
        oa_works = api.openalex_search_multiple(title, rec.name, max_results=5)
        if oa_works:
            matched, oa_work = _try_multiple_candidates(
                "OpenAlex",
                oa_works,
                api.build_bibtex_from_openalex,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "openalex",
                max_candidates=5
            )
            if not matched:
                logger.info("OpenAlex: no candidates matched baseline", indent=2)
                oa_work = None
        else:
            logger.info("OpenAlex: no matching publication found in database", indent=1)
    except ALL_API_ERRORS as e:
        logger.warn(f"OpenAlex: API error - {e}", indent=1)

    logger.info("PubMed: search and build BibTeX", indent=1)
    pm_article = None
    try:
        pm_articles = api.pubmed_search_papers_multiple(title, rec.name, max_results=5)
        if pm_articles:
            matched, pm_article = _try_multiple_candidates(
                "PubMed",
                pm_articles,
                api.build_bibtex_from_pubmed,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "pubmed",
                max_candidates=5
            )
            if not matched:
                logger.info("PubMed: no candidates matched baseline", indent=2)
                pm_article = None
        else:
            logger.info("PubMed: no matching publication found in database", indent=1)
    except ALL_API_ERRORS as e:
        logger.warn(f"PubMed: API error - {e}", indent=1)

    logger.info("Europe PMC: search and build BibTeX", indent=1)
    epmc_article = None
    try:
        epmc_articles = api.europepmc_search_papers_multiple(title, rec.name, max_results=5)
        if epmc_articles:
            matched, epmc_article = _try_multiple_candidates(
                "Europe PMC",
                epmc_articles,
                api.build_bibtex_from_europepmc,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "europepmc",
                max_candidates=5
            )
            if not matched:
                logger.info("Europe PMC: no candidates matched baseline", indent=2)
                epmc_article = None
        else:
            logger.info("Europe PMC: no matching publication found in database", indent=1)
    except ALL_API_ERRORS as e:
        logger.warn(f"Europe PMC: API error - {e}", indent=1)

    # Only do late DOI negotiation if we haven't already validated a DOI early
    if not doi_validated:
        logger.info("DOI negotiation: discover CSL/BibTeX via DOI", indent=1)
        try:
            doi_candidates: List[str] = []
            # Only extract DOIs from API results that successfully matched baseline
            if s2_paper and flags.get("s2"):
                ext = s2_paper.get("externalIds") or {}
                if isinstance(ext, dict):
                    if ext.get("DOI"):
                        doi_candidates.append(ext.get("DOI"))
                if s2_paper.get("doi"):
                    doi_candidates.append(s2_paper.get("doi"))
            if cr_item and cr_item.get("DOI") and flags.get("crossref"):
                doi_candidates.append(cr_item.get("DOI"))
            if arxiv_entry and arxiv_entry.get("doi") and flags.get("arxiv"):
                doi_candidates.append(arxiv_entry.get("doi"))
            if oa_work and oa_work.get("doi") and flags.get("openalex"):
                doi_candidates.append(oa_work.get("doi"))
            if pm_article and flags.get("pubmed"):
                for aid in pm_article.get("articleids") or []:
                    if aid.get("idtype") == "doi":
                        doi_candidates.append(aid.get("value") or "")
            if epmc_article and epmc_article.get("doi") and flags.get("europepmc"):
                doi_candidates.append(epmc_article.get("doi"))

            url_candidates: List[str] = []
            # URLs from baseline are always safe to use
            base_url = (baseline_entry.get("fields") or {}).get("url")
            if base_url:
                url_candidates.append(base_url)
            # Only use URLs from API results that successfully matched baseline
            if s2_paper and s2_paper.get("url") and flags.get("s2"):
                url_candidates.append(s2_paper.get("url"))
            if cr_item and cr_item.get("URL") and flags.get("crossref"):
                url_candidates.append(cr_item.get("URL"))
            if arxiv_entry and arxiv_entry.get("abs_url") and flags.get("arxiv"):
                url_candidates.append(arxiv_entry.get("abs_url"))
            if oa_work and oa_work.get("id") and flags.get("openalex"):
                url_candidates.append(oa_work.get("id"))
            if pm_article and pm_article.get("uid") and flags.get("pubmed"):
                url_candidates.append(f"https://pubmed.ncbi.nlm.nih.gov/{pm_article.get('uid')}/")
            if epmc_article and flags.get("europepmc"):
                pmcid = epmc_article.get("pmcid")
                if pmcid:
                    url_candidates.append(f"https://europepmc.org/article/MED/{pmcid}")

            for u in [u for u in url_candidates if u]:
                try:
                    html = http_get_text(u)
                except ALL_API_ERRORS:
                    continue
                d = idu.find_doi_in_html(html)
                if d:
                    doi_candidates.append(d)

            doi_candidates = [d for d in {idu.normalize_doi(d) for d in doi_candidates if d} if d]

            if doi_candidates:
                logger.info(f"Found {len(doi_candidates)} DOI candidate(s): {', '.join(doi_candidates)}", indent=2)
                doi_matched = False

                # Try each DOI candidate until we find one that validates
                for doi_candidate in doi_candidates:
                    logger.info(f"Validating DOI candidate: {doi_candidate}", indent=2)
                    candidate_matched = process_validated_doi(
                        doi_candidate, baseline_entry, result_id, enr_list, flags, is_early=False
                    )

                    if candidate_matched:
                        doi_matched = True
                        break  # Stop after first successful validation
                    else:
                        logger.info(f"Trying next DOI candidate...", indent=2)

                # If none of the DOI candidates validated, warn the user
                if not doi_matched:
                    logger.warn(f"None of {len(doi_candidates)} DOI candidate(s) validated against baseline", indent=2)
            else:
                logger.info("No DOI discovered; skipped", indent=2)
        except ALL_API_ERRORS as e:
            logger.warn(f"DOI negotiation error: {e}", indent=1)
    else:
        logger.info("DOI already validated early; skipping late DOI negotiation", indent=1)

    logger.info("Merge: apply trust policy and save", indent=1)
    try:
        merged = mu.merge_with_policy(baseline_entry, enr_list)
        merged["key"] = bt.build_standard_citekey(merged, gemini_api_key=gemini_api_key) or merged.get("key") or "Entry"
        path2 = mu.save_entry_to_file(out_dir, rec.scholar_id, merged, prefer_path=path,
                                      gemini_api_key=gemini_api_key, author_name=rec.name)
        if path2 != path:
            logger.success(f"Enriched and renamed: {path2}", indent=1)
        else:
            logger.success(f"Enriched: {path2}", indent=1)
        # Summary log: relative path and success flags
        try:
            rel = os.path.relpath(path2)
        except (OSError, ValueError):
            rel = path2
        total_true = sum(1 for v in flags.values() if v)
        flags_str = ", ".join([f"{k}={'1' if v else '0'}" for k, v in flags.items()])
        logger.info(f"Summary: {rel} | trust_hits={total_true} | {flags_str}", indent=1)

        # Write summary to CSV if path is provided
        if summary_csv_path:
            append_summary_to_csv(
                summary_csv_path,
                rel,
                total_true,
                flags
            )

        if total_true == 0:
            logger.warn("Entry was not enriched by any source; resulting BibTeX may be incomplete", indent=1)
    except (PARSE_ERRORS, OSError, RuntimeError) as e:
        logger.error(f"Merge error: {e}", indent=1)
        return 0

    return 1


def process_record(api_key: str, rec: Record, out_dir: str, max_pubs: Optional[int] = 1,
                   s2_api_key: Optional[str] = None, or_creds: Optional[tuple] = None, delay: float = 0.0,
                   gemini_api_key: Optional[str] = None, summary_csv_path: Optional[str] = None) -> int:
    """
    Process recent publications for one author.

    Query Scholar and DBLP, merge and deduplicate their results, and then call
    process_article on each selected item. Returns the number of BibTeX files
    successfully written for this author.
    """
    logger.step(f"Author: {rec.name} (id={rec.scholar_id})")
    logger.info("Scholar: request author publications", indent=1)
    data = api.fetch_author_publications(api_key, rec.scholar_id)

    status = (data.get("search_metadata") or {}).get("status")
    if status and status.lower() == "error":
        err = data.get("error") or "Unknown error"
        raise RuntimeError(f"CiteForge error for author {rec.scholar_id}: {err}")

    scholar_articles = data.get("articles", [])
    if not scholar_articles:
        logger.info("No articles returned by Scholar", indent=1)
        scholar_articles = []
    else:
        # clean up the titles now so we don't have to worry about trailing periods later
        for a in scholar_articles:
            try:
                if a.get("title"):
                    a["title"] = trim_title_default(api.strip_html_tags(a.get("title") or ""))
            except (TypeError, AttributeError):
                pass
        logger.info(f"Scholar: {len(scholar_articles)} article(s) fetched", indent=1)

    current_year = api.get_current_year()
    min_year = current_year - (CONTRIBUTION_WINDOW_YEARS - 1)
    scholar_windowed = [a for a in scholar_articles if (api.get_article_year(a) or 0) >= min_year]
    logger.info(
        f"Scholar: {len(scholar_windowed)}/{len(scholar_articles)} within "
        f"{CONTRIBUTION_WINDOW_YEARS}y window (>= {min_year})",
        indent=1
    )

    # also grab stuff from DBLP if we can
    try:
        dblp_items = api.dblp_fetch_for_author(rec.name, rec.dblp, min_year)
        logger.info(f"DBLP: {len(dblp_items)} item(s) fetched within window", indent=1)
    except FULL_OPERATION_ERRORS as e:
        logger.warn(f"DBLP fetch failed: {e}", indent=1)
        dblp_items = []

    if not scholar_windowed and not dblp_items:
        logger.info(f"No articles within last {CONTRIBUTION_WINDOW_YEARS} years", indent=1)
        return 0

    # merge Scholar and DBLP with full deduplication (within and across sources)
    merged_list = api.merge_publication_lists(scholar_windowed, dblp_items, target_author=rec.name)
    logger.info(
        f"Union: Scholar={len(scholar_windowed)}, DBLP={len(dblp_items)} "
        f"→ {len(merged_list)} unique publications (threshold={SIM_MERGE_DUPLICATE_THRESHOLD})",
        indent=1
    )

    articles_sorted = api.sort_articles_by_year_current_first(merged_list)
    total_entries = len(articles_sorted) if max_pubs is None else min(len(articles_sorted), max_pubs)
    logger.info(
        f"Plan: process {total_entries}/{len(articles_sorted)} item(s) "
        f"(limit={'all' if max_pubs is None else max_pubs})",
        indent=1
    )

    saved = 0
    for idx, art in enumerate(articles_sorted):
        if max_pubs is not None and idx >= max_pubs:
            break
        try:
            saved += process_article(rec, art, api_key, out_dir, s2_api_key, or_creds, idx=idx + 1, total=total_entries,
                                     gemini_api_key=gemini_api_key, summary_csv_path=summary_csv_path)
        except FULL_OPERATION_ERRORS as e:
            logger.error(f"Article error: {e}", indent=1)
        if delay > 0:
            time.sleep(delay)
    logger.info(f"Author done: saved {saved} file(s)", indent=1)
    return saved


def main() -> int:
    """
    Set up the run by creating output directories, loading API keys and
    author records, and iterating over all authors while logging progress.

    Returns an exit code
    suitable for use as a command-line entry point.
    """
    out_dir = os.path.join(os.path.dirname(__file__), DEFAULT_OUT_DIR)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create output directory '{out_dir}': {e}", indent=0)
        return 2

    logger.set_log_file(os.path.join(out_dir, "run.log"))
    logger.step("CiteForge run started")

    try:
        api_key = read_api_key(DEFAULT_KEY_FILE)
        logger.success("SerpAPI key loaded", indent=0)
    except FILE_IO_ERRORS as e:
        logger.error(f"Error reading SerpAPI key: {e}", indent=0)
        logger.close()
        return 2

    s2_api_key = read_semantic_api_key(DEFAULT_S2_KEY_FILE)
    if not s2_api_key:
        logger.warn("Semantic Scholar key not found; S2 enrichment disabled", indent=0)
    else:
        logger.success("Semantic Scholar key loaded", indent=0)

    or_creds = read_openreview_credentials()
    if not or_creds:
        logger.warn("OpenReview credentials not found; OpenReview enrichment may be limited", indent=0)
    else:
        logger.success("OpenReview credentials loaded", indent=0)

    gemini_api_key = read_gemini_api_key()
    if not gemini_api_key:
        logger.warn("Gemini API key not found; short titles will use fallback algorithm", indent=0)
    else:
        logger.success("Gemini API key loaded", indent=0)

    try:
        records = read_records(DEFAULT_INPUT)
        logger.success(f"Input loaded: {len(records)} record(s)", indent=0)
    except FILE_READ_ERRORS as e:
        logger.error(f"Error reading input file: {e}", indent=0)
        logger.close()
        return 2

    summary_csv_path = os.path.join(out_dir, "summary.csv")
    try:
        init_summary_csv(summary_csv_path)
        logger.success(f"Summary CSV initialized: {summary_csv_path}", indent=0)
    except FILE_IO_ERRORS as e:
        logger.warn(f"Could not initialize summary CSV: {e}", indent=0)
        summary_csv_path = None

    total_saved = 0
    processed = 0
    for rec in records:
        try:
            total_saved += process_record(api_key, rec, out_dir, max_pubs=None, s2_api_key=s2_api_key,
                                          or_creds=or_creds, delay=REQUEST_DELAY_BETWEEN_ARTICLES,
                                          gemini_api_key=gemini_api_key, summary_csv_path=summary_csv_path)
        except FULL_OPERATION_ERRORS as e:
            logger.error(f"Author error for {rec.name} ({rec.scholar_id}): {e}", indent=1)
        processed += 1

    logger.step("Run complete")
    logger.info(f"Records processed: {processed}", indent=0)
    logger.info(f"BibTeX files saved: {total_saved}", indent=0)
    logger.info(f"Log file: {logger.log_file_path or 'n/a'}", indent=0)

    if summary_csv_path and os.path.exists(summary_csv_path):
        logger.info(f"Summary CSV: {summary_csv_path}", indent=0)

    logger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
