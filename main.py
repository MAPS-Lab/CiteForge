from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Dict, Any, Tuple, Callable

from src import bibtex_utils as bt, api_clients as api, merge_utils as mu
from src import id_utils as idu
from src.doi_utils import process_validated_doi
from src.config import (
    DEFAULT_INPUT,
    DEFAULT_KEY_FILE,
    DEFAULT_S2_KEY_FILE,
    DEFAULT_GEMINI_KEY_FILE,
    DEFAULT_OUT_DIR,
    TRUST_ORDER,
    SKIP_SERPAPI_FOR_EXISTING_FILES,
    REQUEST_DELAY_BETWEEN_ARTICLES,
    CONTRIBUTION_WINDOW_YEARS,
    MAX_PUBLICATIONS_PER_AUTHOR,
    SIM_MERGE_DUPLICATE_THRESHOLD,
)
from src.exceptions import (
    ALL_API_ERRORS,
    FULL_OPERATION_ERRORS,
    FILE_IO_ERRORS,
    FILE_READ_ERRORS,
    PARSE_ERRORS,
)
from src.http_utils import http_get_text

from src.io_utils import read_api_key, read_semantic_api_key, read_records, read_openreview_credentials, \
    init_summary_csv, append_summary_to_csv, read_gemini_api_key
from src.log_utils import logger, LogSource, LogCategory
from src.models import Record
from src.text_utils import trim_title_default, format_author_dirname


# Log Messages
MSG_SEARCHING = "Searching and building BibTeX"
MSG_NO_CANDIDATES = "No candidates matched baseline"
MSG_NO_MATCH = "No matching publication found in database"


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
    logger.info(f"Trying {len(candidates_to_try)} candidate(s)", category=LogCategory.SEARCH, source=source_name)

    for idx, candidate in enumerate(candidates_to_try, 1):
        try:
            candidate_bib = build_func(candidate, keyhint=result_id)
            if not candidate_bib:
                # Skip candidates with missing required fields (title or authors)
                continue

            # Parse candidate once to get title and other fields
            candidate_dict = bt.parse_bibtex_to_dict(candidate_bib)
            candidate_title = candidate_dict.get('fields', {}).get('title', '')
            if isinstance(candidate_title, list):
                candidate_title = candidate_title[0] if candidate_title else ''

            if bt.bibtex_entries_match_strict(baseline_entry, candidate_dict):
                enr_list.append((flag_key, candidate_dict))
                flags[flag_key] = True
                logger.success(f"Validated: {candidate_title}", category=LogCategory.MATCH)
                return True, candidate

        except Exception as e:
            logger.info(f"Candidate {idx}: error - {e}", category=LogCategory.DEBUG)
            continue

    return False, None





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
    # Determine IDs; Scholar may provide multiple identifiers
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
        logger.error("Missing required field: title; skipping article", category=LogCategory.SKIP)
        return 0
    if not authors_list or not year_hint:
        logger.warn("Article missing authors and/or year; continuing with best-effort enrichment", category=LogCategory.ARTICLE)

    idx_prefix = f"[{idx}/{total}] " if (isinstance(idx, int) and isinstance(total, int)) else ""
    src = (art.get("source") or "scholar").strip()
    meta_bits = []
    if year_hint:
        meta_bits.append(str(year_hint))
    if src:
        meta_bits.append(src)
    meta = ", ".join(meta_bits)
    logger.substep(f"{idx_prefix}Article: {title}{(' (' + meta + ')') if meta else ''}", category=LogCategory.ARTICLE)

    # Check if BibTeX file already exists for this article (optimization to reduce SerpAPI usage)
    from src.text_utils import format_author_dirname
    # Use Scholar ID if available, otherwise fallback to DBLP ID
    effective_id = rec.scholar_id or rec.dblp or ""
    author_dirname = format_author_dirname(rec.name, effective_id)
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

                        from src.text_utils import title_similarity
                        if title_similarity(title, existing_title) > 0.9:
                            baseline_entry = existing_entry
                            existing_file_path = file_path
                            existing_file_loaded = True
                            logger.info(
                                f"Existing BibTeX found: {filename}. Using existing file as baseline seed - will update/enrich with fresh data",
                                category=LogCategory.SKIP
                            )
                            break
                except (OSError, ValueError, TypeError):
                    # Skip files that can't be read or parsed
                    continue

    # If no existing file found, build minimal BibTeX baseline
    if not existing_file_loaded:
        logger.info("Building minimal BibTeX baseline", category=LogCategory.ARTICLE, source=LogSource.SYSTEM)
        authors_list = api.extract_authors_from_article(art) or []
        year = api.get_article_year(art)
        scholar_bib = bt.build_minimal_bibtex(title, authors_list, year, keyhint=result_id)

        baseline_entry = bt.parse_bibtex_to_dict(scholar_bib)
        if baseline_entry is None:
            # Parse failed - should be rare since we generated the BibTeX
            logger.error("Failed to parse Scholar BibTeX; using minimal fallback structure", category=LogCategory.ERROR)
            baseline_entry = {
                "type": "misc",
                "key": result_id or "entry",
                "fields": {"title": title} if title else {}
            }
    # Keep track of where this came from by storing Scholar IDs (but skip x_scholar_citation_id)
    bf = baseline_entry.get("fields") or {}
    if cluster_id:
        bf["x_scholar_cluster_id"] = cluster_id
    # Attempt to locate arXiv ID or DOI in article snippet or links
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
        # DOI found in snippet is not added immediately due to potential unreliability.
        # DOIs are only added from reliable sources (Crossref, DOI resolver).
    except PARSE_ERRORS:
        pass
    baseline_entry["fields"] = bf
    ck = bt.build_standard_citekey(baseline_entry, gemini_api_key=gemini_api_key) or baseline_entry.get(
        "key") or "Entry"
    baseline_entry["key"] = ck

    # Save baseline only if we didn't load from existing file
    if existing_file_loaded:
        path = existing_file_path
        logger.info(f"Using existing file: {path}", category=LogCategory.SKIP)
    else:
        path = mu.save_entry_to_file(out_dir, effective_id, baseline_entry, gemini_api_key=gemini_api_key,
                                     author_name=rec.name)
        logger.success(f"Saved baseline: {path}", category=LogCategory.SAVE, source=LogSource.SYSTEM)

    enr_list: List[Tuple[str, Dict[str, Any]]] = []

    # if the baseline already has a DOI, use it to get better metadata early on
    doi_validated = False  # Track if we successfully validated the DOI
    try:
        doi_early = idu.normalize_doi((baseline_entry.get("fields") or {}).get("doi"))
        if doi_early:
            logger.info(f"DOI negotiation (early): validating baseline DOI {doi_early}", category=LogCategory.SEARCH)
            doi_matched = process_validated_doi(
                doi_early, baseline_entry, result_id, enr_list, flags
            )

            # If DOI failed validation, remove it from baseline
            if not doi_matched:
                baseline_entry.get("fields", {}).pop("doi", None)
                logger.warn(f"Removed unvalidated DOI {doi_early} from baseline", category=LogCategory.ARTICLE)
            else:
                doi_validated = True
    except PARSE_ERRORS:
        pass

    # Skip SerpAPI citation fetch if we loaded an existing file (optimization to reduce API usage)
    if existing_file_loaded:
        logger.info("Scholar citation: skipped (existing file loaded)", category=LogCategory.SKIP)
    else:
        logger.info("Fetch via SerpAPI", category=LogCategory.FETCH, source=LogSource.SCHOLAR)
        if citation_id:
            try:
                fields = api.fetch_scholar_citation_via_serpapi(api_key, rec.scholar_id, citation_id)
                if fields:
                    sch_page_bib = api.build_bibtex_from_scholar_fields(fields, keyhint=result_id)
                    if sch_page_bib:
                        sch_page_dict = bt.parse_bibtex_to_dict(sch_page_bib)
                        if sch_page_dict and bt.bibtex_entries_match_strict(baseline_entry, sch_page_dict):
                            enr_list.append(("scholar_page", sch_page_dict))
                            flags["scholar_page"] = True
                            logger.success("Citation matched and added", category=LogCategory.MATCH, source=LogSource.SCHOLAR)
                        else:
                            logger.info("Citation did not match baseline; skipped", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
                    else:
                        logger.info("No BibTeX generated from citation", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
                else:
                    logger.info("No data returned from SerpAPI", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
            except ALL_API_ERRORS as e:
                logger.warn(f"Citation fetch error: {e}", category=LogCategory.ERROR, source=LogSource.SCHOLAR)
        else:
            logger.info("No citation_id available; skipped", category=LogCategory.SKIP, source=LogSource.SCHOLAR)

    logger.info(MSG_SEARCHING, category=LogCategory.SEARCH, source=LogSource.S2)
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
                    logger.info(MSG_NO_CANDIDATES, category=LogCategory.SKIP, source=LogSource.S2)
                    s2_paper = None
            else:
                logger.info(MSG_NO_MATCH, category=LogCategory.SKIP, source=LogSource.S2)
        except ALL_API_ERRORS as e:
            logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.S2)
    else:
        logger.info("Skipped (no API key)", category=LogCategory.SKIP, source=LogSource.S2)

    logger.info(MSG_SEARCHING, category=LogCategory.SEARCH, source=LogSource.CROSSREF)
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
                logger.info(MSG_NO_CANDIDATES, category=LogCategory.SKIP, source=LogSource.CROSSREF)
                cr_item = None
        else:
            logger.info(MSG_NO_MATCH, category=LogCategory.SKIP, source=LogSource.CROSSREF)
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.CROSSREF)

    logger.info(MSG_SEARCHING, category=LogCategory.SEARCH, source=LogSource.OPENREVIEW)
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
                logger.info(MSG_NO_CANDIDATES, category=LogCategory.SKIP, source=LogSource.OPENREVIEW)
        else:
            logger.info(MSG_NO_MATCH, category=LogCategory.SKIP, source=LogSource.OPENREVIEW)
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.OPENREVIEW)

    logger.info(MSG_SEARCHING, category=LogCategory.SEARCH, source=LogSource.ARXIV)
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
                logger.info(MSG_NO_CANDIDATES, category=LogCategory.SKIP, source=LogSource.ARXIV)
                arxiv_entry = None
        else:
            logger.info(MSG_NO_MATCH, category=LogCategory.SKIP, source=LogSource.ARXIV)
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.ARXIV)

    logger.info(MSG_SEARCHING, category=LogCategory.SEARCH, source=LogSource.OPENALEX)
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
                logger.info(MSG_NO_CANDIDATES, category=LogCategory.SKIP, source=LogSource.OPENALEX)
                oa_work = None
        else:
            logger.info(MSG_NO_MATCH, category=LogCategory.SKIP, source=LogSource.OPENALEX)
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.OPENALEX)

    logger.info(MSG_SEARCHING, category=LogCategory.SEARCH, source=LogSource.PUBMED)
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
                logger.info(MSG_NO_CANDIDATES, category=LogCategory.SKIP, source=LogSource.PUBMED)
                pm_article = None
        else:
            logger.info(MSG_NO_MATCH, category=LogCategory.SKIP, source=LogSource.PUBMED)
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.PUBMED)

    logger.info(MSG_SEARCHING, category=LogCategory.SEARCH, source=LogSource.EUROPEPMC)
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
                logger.info(MSG_NO_CANDIDATES, category=LogCategory.SKIP, source=LogSource.EUROPEPMC)
                epmc_article = None
        else:
            logger.info(MSG_NO_MATCH, category=LogCategory.SKIP, source=LogSource.EUROPEPMC)
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.EUROPEPMC)

    # Only do late DOI negotiation if we haven't already validated a DOI early
    if not doi_validated:
        logger.info("Discover CSL/BibTeX via DOI", category=LogCategory.SEARCH, source=LogSource.DOI)
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
                logger.info(f"Found {len(doi_candidates)} DOI candidate(s): {', '.join(doi_candidates)}", category=LogCategory.SEARCH, source=LogSource.DOI)
                doi_matched = False

                # Try each DOI candidate until we find one that validates
                for doi_candidate in doi_candidates:
                    logger.info(f"Validating DOI candidate: {doi_candidate}", category=LogCategory.SEARCH, source=LogSource.DOI)
                    candidate_matched = process_validated_doi(
                        doi_candidate, baseline_entry, result_id, enr_list, flags
                    )

                    if candidate_matched:
                        doi_matched = True
                        break  # Stop after first successful validation
                    else:
                        logger.info("Trying next DOI candidate...", category=LogCategory.SEARCH, source=LogSource.DOI)

                # If none of the DOI candidates validated, warn the user
                if not doi_matched:
                    logger.warn(f"None of {len(doi_candidates)} DOI candidate(s) validated against baseline", category=LogCategory.SKIP, source=LogSource.DOI)
            else:
                logger.info("No DOI discovered; skipped", category=LogCategory.SKIP, source=LogSource.DOI)
        except ALL_API_ERRORS as e:
            logger.warn(f"DOI negotiation error: {e}", category=LogCategory.ERROR, source=LogSource.DOI)
    else:
        logger.info("DOI already validated early; skipping late DOI negotiation", category=LogCategory.SKIP, source=LogSource.DOI)

    logger.info("Merge: apply trust policy and save", category=LogCategory.SAVE, source=LogSource.SYSTEM)
    try:
        merged = mu.merge_with_policy(baseline_entry, enr_list)
        merged["key"] = bt.build_standard_citekey(merged, gemini_api_key=gemini_api_key) or merged.get("key") or "Entry"
        path2 = mu.save_entry_to_file(out_dir, effective_id, merged, prefer_path=path,
                                      gemini_api_key=gemini_api_key, author_name=rec.name)
        if path2 != path:
            logger.success(f"Enriched and renamed: {path2}", category=LogCategory.SAVE, source=LogSource.SYSTEM)
        else:
            logger.success(f"Enriched: {path2}", category=LogCategory.SAVE, source=LogSource.SYSTEM)
        # Summary log: relative path and success flags
        try:
            rel = os.path.relpath(path2)
        except (OSError, ValueError):
            rel = path2
        total_true = sum(1 for v in flags.values() if v)



        # Write summary to CSV if path is provided
        if summary_csv_path:
            append_summary_to_csv(
                summary_csv_path,
                rel,
                total_true,
                flags
            )

        if total_true == 0:
            logger.info("Entry saved with Scholar/DBLP data only; no additional enrichment available", category=LogCategory.SAVE, source=LogSource.SYSTEM)
    except (PARSE_ERRORS, OSError, RuntimeError) as e:
        logger.error(f"Merge error: {e}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
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
    # Setup thread-local logging for this author
    effective_id = rec.scholar_id or rec.dblp or ""
    author_dirname = format_author_dirname(rec.name, effective_id)
    author_log_path = os.path.join(out_dir, author_dirname, "author.log")
    
    logger.set_log_file(author_log_path)
    
    try:
        logger.step(f"Author: {rec.name} (Scholar={rec.scholar_id or 'N/A'}, DBLP={rec.dblp or 'N/A'})", category=LogCategory.AUTHOR, source=LogSource.SYSTEM)
        
        current_year = api.get_current_year()
        min_year = current_year - (CONTRIBUTION_WINDOW_YEARS - 1)

        scholar_windowed = []
        if rec.scholar_id:
            logger.info("Request author publications", category=LogCategory.FETCH, source=LogSource.SCHOLAR)

            # SerpAPI limits each request to 100 results, so we need pagination for MAX_PUBLICATIONS_PER_AUTHOR > 100
            scholar_articles = []
            start = 0
            batch_size = 100  # SerpAPI maximum per request
            total_requested = MAX_PUBLICATIONS_PER_AUTHOR

            while start < total_requested:
                remaining = total_requested - start
                num_this_batch = min(batch_size, remaining)

                data = api.fetch_author_publications(api_key, rec.scholar_id, num=num_this_batch, start=start)

                status = (data.get("search_metadata") or {}).get("status")
                if status and status.lower() == "error":
                    err = data.get("error") or "Unknown error"
                    raise RuntimeError(f"CiteForge error for author {rec.scholar_id}: {err}")

                batch_articles = data.get("articles", [])
                if not batch_articles:
                    # No more articles available, stop pagination
                    break

                scholar_articles.extend(batch_articles)

                # If we got fewer articles than requested, there are no more
                if len(batch_articles) < num_this_batch:
                    break

                start += len(batch_articles)

            if not scholar_articles:
                logger.info("No articles returned", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
                scholar_articles = []
            else:
                # Pre-clean titles to handle trailing periods consistently
                for a in scholar_articles:
                    try:
                        if a.get("title"):
                            a["title"] = trim_title_default(api.strip_html_tags(a.get("title") or ""))
                    except (TypeError, AttributeError):
                        pass
                logger.info(f"{len(scholar_articles)} article(s) fetched", category=LogCategory.FETCH, source=LogSource.SCHOLAR)

            scholar_windowed = [a for a in scholar_articles if (api.get_article_year(a) or 0) >= min_year]
            logger.info(
                f"{len(scholar_windowed)}/{len(scholar_articles)} within "
                f"{CONTRIBUTION_WINDOW_YEARS}y window (>= {min_year})",
                category=LogCategory.FETCH,
                source=LogSource.SCHOLAR
            )
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.SCHOLAR)

        # also grab stuff from DBLP if we can
        dblp_items = []
        if rec.dblp:
            try:
                dblp_items = api.dblp_fetch_for_author(rec.name, rec.dblp, min_year)
                logger.info(f"{len(dblp_items)} item(s) fetched within window", category=LogCategory.FETCH, source=LogSource.DBLP)
            except FULL_OPERATION_ERRORS as e:
                logger.warn(f"Fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DBLP)
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.DBLP)

        if not scholar_windowed and not dblp_items:
            logger.info(f"No articles within last {CONTRIBUTION_WINDOW_YEARS} years", category=LogCategory.SKIP)
            return 0

        # merge Scholar and DBLP with full deduplication (within and across sources)
        merged_list = api.merge_publication_lists(scholar_windowed, dblp_items, target_author=rec.name)
        logger.info(
            f"Union: Scholar={len(scholar_windowed)}, DBLP={len(dblp_items)} "
            f"→ {len(merged_list)} unique publications (threshold={SIM_MERGE_DUPLICATE_THRESHOLD})",
            category=LogCategory.PLAN
        )

        articles_sorted = api.sort_articles_by_year_current_first(merged_list)
        total_entries = len(articles_sorted) if max_pubs is None else min(len(articles_sorted), max_pubs)
        logger.info(
            f"Plan: process {total_entries}/{len(articles_sorted)} item(s) "
            f"(limit={'all' if max_pubs is None else max_pubs})",
            category=LogCategory.PLAN
        )

        saved = 0
        for idx, art in enumerate(articles_sorted):
            if max_pubs is not None and idx >= max_pubs:
                break
            try:
                saved += process_article(rec, art, api_key, out_dir, s2_api_key, or_creds, idx=idx + 1, total=total_entries,
                                         gemini_api_key=gemini_api_key, summary_csv_path=summary_csv_path)
            except FULL_OPERATION_ERRORS as e:
                logger.error(f"Article error: {e}", category=LogCategory.ERROR)
            if delay > 0:
                time.sleep(delay)
        logger.info(f"Author done: saved {saved} file(s)", category=LogCategory.PLAN)
        return saved
    finally:
        # Close the thread-local log file handler
        logger.close()


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
        logger.error(f"Cannot create output directory '{out_dir}': {e}", category=LogCategory.ERROR)
        return 2

    # Set main thread log file
    logger.set_log_file(os.path.join(out_dir, "run.log"))
    logger.step("CiteForge run started", category=LogCategory.PLAN)

    try:
        api_key = read_api_key(DEFAULT_KEY_FILE)
        logger.success("SerpAPI key loaded", category=LogCategory.PLAN)
    except FILE_IO_ERRORS as e:
        logger.error(f"Error reading SerpAPI key: {e}", category=LogCategory.ERROR)
        logger.close()
        return 2

    s2_api_key = read_semantic_api_key(DEFAULT_S2_KEY_FILE)
    if not s2_api_key:
        logger.warn("Semantic Scholar key not found; S2 enrichment disabled", category=LogCategory.PLAN)
    else:
        logger.success("Semantic Scholar key loaded", category=LogCategory.PLAN)

    or_creds = read_openreview_credentials()
    if not or_creds:
        logger.warn("OpenReview credentials not found; OpenReview enrichment may be limited", category=LogCategory.PLAN)
    else:
        logger.success("OpenReview credentials loaded", category=LogCategory.PLAN)

    gemini_api_key = read_gemini_api_key()
    if not gemini_api_key:
        logger.warn("Gemini API key not found; short titles will use fallback algorithm", category=LogCategory.PLAN)
    else:
        logger.success("Gemini API key loaded", category=LogCategory.PLAN)

    try:
        records = read_records(DEFAULT_INPUT)
        logger.success(f"Input loaded: {len(records)} record(s)", category=LogCategory.PLAN)
    except FILE_READ_ERRORS as e:
        logger.error(f"Error reading input file: {e}", category=LogCategory.ERROR)
        logger.close()
        return 2

    summary_csv_path = os.path.join(out_dir, "summary.csv")
    try:
        init_summary_csv(summary_csv_path)
        logger.success(f"Summary CSV initialized: {summary_csv_path}", category=LogCategory.PLAN)
    except FILE_IO_ERRORS as e:
        logger.warn(f"Could not initialize summary CSV: {e}", category=LogCategory.ERROR)
        summary_csv_path = None

    total_saved = 0
    processed = 0
    
    max_workers = 8  # Execute author processing in parallel using a thread pool.    
    logger.step(f"Starting parallel execution with {max_workers} workers", category=LogCategory.PLAN)
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_author = {
            executor.submit(
                process_record, 
                api_key, 
                rec, 
                out_dir, 
                max_pubs=None, 
                s2_api_key=s2_api_key,
                or_creds=or_creds, 
                delay=REQUEST_DELAY_BETWEEN_ARTICLES,
                gemini_api_key=gemini_api_key, 
                summary_csv_path=summary_csv_path
            ): rec for rec in records
        }
        
        for future in as_completed(future_to_author):
            rec = future_to_author[future]
            try:
                saved = future.result()
                total_saved += saved
                processed += 1
                logger.success(f"Finished author: {rec.name} ({saved} files saved)", category=LogCategory.AUTHOR)
            except Exception as e:
                logger.error(f"Author error for {rec.name} ({rec.scholar_id}): {e}", category=LogCategory.ERROR)

    logger.step("Run complete", category=LogCategory.PLAN)
    logger.info(f"Records processed: {processed}", category=LogCategory.PLAN)
    logger.info(f"BibTeX files saved: {total_saved}", category=LogCategory.PLAN)
    logger.info(f"Log file: {logger.log_file_path or 'n/a'}", category=LogCategory.PLAN)

    if summary_csv_path and os.path.exists(summary_csv_path):
        logger.info(f"Summary CSV: {summary_csv_path}", category=LogCategory.PLAN)

    logger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
