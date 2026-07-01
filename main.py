from __future__ import annotations

import csv
import json
import os
import random
import re
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from src import bibtex_utils as bt
from src import id_utils as idu
from src import merge_utils as mu
from src.cache import get_cache_hit_counts
from src.canonicalize import (
    CanonicalStage,
    _fixup_bib_entry,
    canonicalize,
)
from src.clients.helpers import extract_authors_from_article, get_article_year, strip_html_tags
from src.clients.scholar import (
    build_bibtex_from_scholar_fields,
    fetch_author_publications,
    fetch_scholar_citation,
    merge_publication_lists,
    sort_articles_by_year_current_first,
)
from src.clients.search_apis import (
    arxiv_search,
    build_bibtex_from_arxiv,
    build_bibtex_from_crossref,
    build_bibtex_from_europepmc,
    build_bibtex_from_openalex,
    build_bibtex_from_openreview,
    build_bibtex_from_pubmed,
    build_bibtex_from_s2,
    crossref_search_by_venue,
    crossref_search_multiple,
    dblp_fetch_for_author,
    europepmc_search_papers_multiple,
    openalex_search_by_venue,
    openalex_search_multiple,
    openreview_search_papers_multiple,
    pubmed_search_papers_multiple,
    s2_search_papers_multiple,
)
from src.config import (
    DEFAULT_A2I2_INPUT,
    DEFAULT_INPUT,
    DEFAULT_OUT_DIR,
    DEFAULT_S2_KEY_FILE,
    DEFAULT_SERPAPI_KEY_FILE,
    DEFAULT_SERPLY_KEY_FILE,
    GENERIC_SERIES_NAMES,
    MAX_PUBLICATIONS_PER_AUTHOR,
    MAX_WORKERS,
    MIN_TITLE_WORDS,
    PREPRINT_ONLY_PUBLISHERS,
    PREPRINT_SERVERS,
    PUB_PARSE_TIER1_MIN_CONFIDENCE,
    PUB_PARSE_TIER2_MIN_CONFIDENCE,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    SIM_PREPRINT_TITLE_THRESHOLD,
    SKIP_SCHOLAR_FOR_EXISTING_FILES,
    get_min_year,
)
from src.doi_utils import process_validated_doi
from src.exceptions import (
    ALL_API_ERRORS,
    FILE_IO_ERRORS,
    FILE_READ_ERRORS,
    FULL_OPERATION_ERRORS,
    PARSE_ERRORS,
)
from src.fixup.text import (
    _fix_fused_compounds,  # noqa: F401
    _is_corrupted_title,
    _is_garbage_title,
)
from src.fsscan import iter_author_bibs, iter_output_dirs
from src.http_utils import get_api_call_counts, http_get_text, reset_api_call_counts
from src.io_utils import (
    append_summary_to_csv,
    build_a2i2_folder,
    collect_orphan_files,
    flush_summary_csv,
    init_summary_csv,
    is_known_summary_path,
    read_gemini_api_key,
    read_openreview_credentials,
    read_records,
    read_semantic_api_key,
    read_serpapi_api_key,
    read_serply_api_key,
    reconcile_summary_csv,
    safe_write_file,
)
from src.log_utils import LogCategory, LogSource, logger
from src.models import Record
from src.publication_parser import parse_publication_string
from src.text_utils import (
    author_name_matches,
    extract_year_from_any,
    format_author_dirname,
    has_placeholder,
    title_similarity,
    trim_title_default,
)

FORCE_ENRICH = "--force" in sys.argv[1:]

_ARXIV_ABS_RE = re.compile(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", re.IGNORECASE)

_FILENAME_YEAR_RE = re.compile(r"/[A-Za-z]+(\d{4})-")


def _entry_is_complete(entry: dict[str, Any]) -> bool:
    """Check if a BibTeX entry has all essential fields filled with non-placeholder values.

    Returns False for preprint entries (DOI from arXiv/Research Square or journal
    in PREPRINT_SERVERS) so they are re-enriched and potentially upgraded to the
    published version.
    """
    fields = entry.get("fields") or {}
    title = fields.get("title") or ""
    author = fields.get("author") or ""
    year = fields.get("year") or ""
    doi = fields.get("doi")
    has_venue = any(fields.get(v) and not has_placeholder(str(fields.get(v))) for v in ("journal", "booktitle"))

    # Determine completeness: check essential fields, venue, DOI, and preprint status
    doi_is_preprint = False
    journal_is_preprint = False

    has_essentials = all(fields.get(k) and not has_placeholder(str(fields.get(k))) for k in ("title", "author", "year"))
    has_doi = bool(doi) and not has_placeholder(str(doi))

    if has_essentials and has_venue and has_doi:
        doi_is_preprint = idu.is_secondary_doi(str(doi))
        if not doi_is_preprint:
            journal = str(fields.get("journal") or "").lower()
            journal_is_preprint = any(ps in journal for ps in PREPRINT_SERVERS)

    # Treat generic series booktitles as incomplete so they get re-enriched
    venue_is_generic = False
    if has_venue:
        bt_val = (fields.get("booktitle") or "").lower().strip()
        venue_is_generic = bt_val in GENERIC_SERIES_NAMES and not fields.get("journal")

    result = (
        has_essentials
        and has_venue
        and has_doi
        and not doi_is_preprint
        and not journal_is_preprint
        and not venue_is_generic
    )

    logger.debug(
        f"COMPLETE_CHECK | title={title[:50]} | has_title={bool(title)} "
        f"| has_author={bool(author)} | has_year={bool(year)} "
        f"| has_venue={has_venue} | has_doi={has_doi} "
        f"| doi_is_preprint={doi_is_preprint} | journal_is_preprint={journal_is_preprint} "
        f"| result={result}",
        category=LogCategory.AUDIT,
    )
    return result


def _read_doi_from_file(filepath: str) -> str:
    """Read and normalize the DOI from a .bib file on disk, returning '' on failure."""
    try:
        with open(filepath, encoding="utf-8") as f:
            parsed = bt.parse_bibtex_to_dict(f.read())
        return idu.normalize_doi((parsed or {}).get("fields", {}).get("doi", "")) or ""
    except (OSError, UnicodeDecodeError):
        return ""


def _revert_misattributed_doi(
    merged_fields: dict[str, Any],
    bad_doi: str,
    doi_validated: bool,
    doi_early: str | None,
) -> None:
    """Replace a mis-attributed DOI with the Phase-1-validated DOI (if any), or remove it."""
    if merged_fields.get("doi") != bad_doi:
        return
    fallback = idu.normalize_doi(doi_early) if doi_validated and doi_early else None
    if fallback and fallback != bad_doi:
        merged_fields["doi"] = fallback
    else:
        merged_fields.pop("doi", None)
    merged_fields.pop("url", None)
    logger.debug(
        f"DOI_REVERT | removed={bad_doi} | restored={fallback or 'none'} | reason=misattributed_candidate",
        category=LogCategory.DEDUP,
    )


def _try_multiple_candidates(
    source_name: str,
    candidates: list[Any],
    build_func: Callable[..., str | None],
    baseline_entry: dict[str, Any],
    result_id: str,
    enr_list: list[tuple[str, dict[str, Any]]],
    flags: dict[str, bool],
    flag_key: str,
    max_candidates: int = 5,
    seen_dois: set[str] | None = None,
) -> tuple[bool, Any | None]:
    """Try candidates from an API source in relevance order until one matches the baseline.

    When *seen_dois* is provided, every DOI encountered across all candidates
    (matched or not) is collected.  This enables downstream duplicate detection
    against files already on disk even when the candidate was rejected by the
    matching gate.

    Returns (matched, matched_candidate) tuple.
    """
    if not candidates:
        return False, None

    candidates_to_try = candidates[:max_candidates]

    for idx, candidate in enumerate(candidates_to_try, 1):
        try:
            candidate_bib = build_func(candidate, keyhint=result_id)
            if not candidate_bib:
                continue

            candidate_dict = bt.parse_bibtex_to_dict(candidate_bib)
            if not candidate_dict:
                continue

            # Collect DOI from every parsed candidate for dedup
            if seen_dois is not None:
                cand_doi = idu.normalize_doi((candidate_dict.get("fields") or {}).get("doi", ""))
                if cand_doi:
                    seen_dois.add(cand_doi)

            match = bt.bibtex_entries_match_strict(baseline_entry, candidate_dict)
            if match:
                enr_list.append((flag_key, candidate_dict))
                flags[flag_key] = True
                logger.success(
                    "Match validated and added to enrichment",
                    category=LogCategory.MATCH,
                    source=source_name,
                )
                return True, candidate

        except Exception as e:
            logger.debug(
                f"CANDIDATE_ERROR | source={source_name} #{idx} | error={type(e).__name__}: {e}",
                category=LogCategory.AUDIT,
            )
            logger.info(f"Candidate {idx}: error - {e}", category=LogCategory.DEBUG, source=source_name)

    return False, None


def process_article(
    rec: Record,
    art: dict[str, Any],
    serply_key: str | None,
    out_dir: str,
    s2_api_key: str | None,
    or_creds: tuple[str, str] | None,
    idx: int | None = None,
    total: int | None = None,
    gemini_api_key: str | None = None,
    summary_csv_path: str | None = None,
    min_year: int = 0,
) -> int:
    """Enrich a single publication from baseline through 4-phase pipeline and save to disk.

    Returns 1 when a file was written, or 0 when the article was skipped or failed.
    """
    title = trim_title_default(strip_html_tags(art.get("title") or ""))
    authors_list = extract_authors_from_article(art) or []
    year_hint = get_article_year(art) or None
    # Determine IDs; Scholar may provide multiple identifiers
    citation_id = art.get("citation_id") or art.get("result_id")
    cluster_id = art.get("cluster_id") or (
        art.get("result_id") if citation_id and art.get("result_id") != citation_id else None
    )
    result_id = citation_id or re.sub(r"\W+", "_", title or "untitled")
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

    if not title:
        logger.error("Missing required field: title; skipping article", category=LogCategory.SKIP)
        return 0

    word_count = len(title.split())
    corrupted = _is_corrupted_title(title)
    garbage = _is_garbage_title(title)
    logger.debug(
        f"TITLE_VALIDATE | raw={title[:60]} | words={word_count} | corrupted={corrupted} "
        f"| garbage={garbage} | valid={word_count >= MIN_TITLE_WORDS and not corrupted and not garbage}",
        category=LogCategory.AUDIT,
    )

    if word_count < MIN_TITLE_WORDS:
        logger.warn(
            f"Title too short (probable artifact): '{title}'; skipping",
            category=LogCategory.SKIP,
        )
        return 0
    if corrupted:
        logger.warn(
            f"Corrupted title (probable DBLP artifact): '{title}'; skipping",
            category=LogCategory.SKIP,
        )
        return 0
    if garbage:
        logger.warn(
            f"Garbage title (non-bibliographic content): '{title}'; skipping",
            category=LogCategory.SKIP,
        )
        return 0
    if not authors_list or not year_hint:
        logger.warn(
            "Article missing authors and/or year; continuing with best-effort enrichment",
            category=LogCategory.ARTICLE,
        )

    idx_prefix = f"[{idx}/{total}] " if idx is not None and total is not None else ""
    art_source = (art.get("source") or "scholar").strip()

    logger.step(f"{idx_prefix}Processing Article", category=LogCategory.ARTICLE)
    logger.info(f"Title: {title}", category=LogCategory.ARTICLE)
    if year_hint:
        logger.info(f"Year: {year_hint}", category=LogCategory.ARTICLE)
    logger.info(f"Source: {art_source}", category=LogCategory.ARTICLE)

    effective_id = rec.scholar_id or rec.dblp or ""
    author_dirname = format_author_dirname(rec.name, effective_id)
    author_dir = os.path.join(out_dir, author_dirname)
    existing_file_loaded = False
    baseline_entry = None
    existing_file_path = None

    # Try to find existing BibTeX file to use as enrichment seed
    # If found, load it and use as baseline - enrichment process will update/fix fields
    if SKIP_SCHOLAR_FOR_EXISTING_FILES and os.path.exists(author_dir):
        # Sort filenames for deterministic iteration order
        bib_files = iter_author_bibs(author_dir)
        logger.debug(
            f"EXISTING_FILE_SCAN | dir={author_dir} | files_checked={len(bib_files)}",
            category=LogCategory.AUDIT,
        )
        for filename in bib_files:
            file_path = os.path.join(author_dir, filename)
            try:
                with open(file_path, encoding="utf-8") as f:
                    existing_bib = f.read()
                existing_entry = bt.parse_bibtex_to_dict(existing_bib)

                # Check if this file matches our article by comparing title
                if existing_entry:
                    existing_title = existing_entry.get("fields", {}).get("title", "")
                    if isinstance(existing_title, list):
                        existing_title = existing_title[0] if existing_title else ""

                    # Purge stale files whose titles now fail validation
                    if _is_garbage_title(existing_title) or _is_corrupted_title(existing_title):
                        logger.warn(
                            f"STALE_FILE_REMOVED | file={filename} | reason=title_now_invalid",
                            category=LogCategory.CLEANUP,
                        )
                        os.remove(file_path)
                        continue

                    sim = title_similarity(title, existing_title)
                    if sim >= SIM_MERGE_DUPLICATE_THRESHOLD:
                        baseline_entry = existing_entry
                        existing_file_path = file_path
                        existing_file_loaded = True
                        logger.info(
                            f"Using existing BibTeX as baseline: {filename}",
                            category=LogCategory.ARTICLE,
                            source=LogSource.SYSTEM,
                        )
                        is_complete = _entry_is_complete(existing_entry)
                        logger.debug(
                            f"EXISTING_FILE_LOADED | file={filename} | complete={is_complete}",
                            category=LogCategory.AUDIT,
                        )
                        break
            except (OSError, ValueError, TypeError):
                continue

    # Fixup stale entries loaded from disk before enrichment: the pure entry
    # field/type rewrites are single-sourced in src/canonicalize.py at the
    # LOAD_REPAIR stage. The destructive title==venue delete (N22) and the
    # bare-& rewrite trigger stay here in the pipeline.
    if existing_file_loaded and baseline_entry is not None:
        _fixup_written = canonicalize(baseline_entry, stage=CanonicalStage.LOAD_REPAIR)
        _bl_fields = baseline_entry.get("fields") or {}

        # Delete entries where title equals journal or booktitle (corrupted Scholar data)
        _bl_title_venue = (_bl_fields.get("title") or "").strip().lower()
        if _bl_title_venue:
            _bl_journal_venue = (_bl_fields.get("journal") or "").strip().lower()
            _bl_booktitle_venue = (_bl_fields.get("booktitle") or "").strip().lower()
            if (_bl_journal_venue and _bl_title_venue == _bl_journal_venue) or (
                _bl_booktitle_venue and _bl_title_venue == _bl_booktitle_venue
            ):
                logger.debug(
                    f"EXISTING_FIXUP | title_is_venue | title={_bl_title_venue[:60]} | deleting",
                    category=LogCategory.CLEANUP,
                )
                if existing_file_path and os.path.exists(existing_file_path):
                    os.remove(existing_file_path)
                return 0

        # Escape bare & in field values (bibtex_from_dict handles this on write,
        # but we need to trigger a rewrite for files that were never re-serialized)
        for _fk, _fv in _bl_fields.items():
            if _fk not in ("url", "doi") and isinstance(_fv, str) and "&" in _fv and r"\&" not in _fv:
                _fixup_written = True
                break

        if _fixup_written and existing_file_path:
            bib_str = bt.bibtex_from_dict(baseline_entry)
            safe_write_file(existing_file_path, bib_str)

    # Skip enrichment entirely if entry is already complete (unless --force)
    if not FORCE_ENRICH and existing_file_loaded and baseline_entry is not None and _entry_is_complete(baseline_entry):
        # Quick fixup: strip preprint-only publishers from complete entries
        bl_fields = baseline_entry.get("fields") or {}
        bl_pub = (bl_fields.get("publisher") or "").lower().strip()
        bl_jnl = (bl_fields.get("journal") or "").lower()
        if bl_pub in PREPRINT_ONLY_PUBLISHERS and bl_jnl and not any(ps in bl_jnl for ps in PREPRINT_SERVERS):
            logger.debug(
                f"EXISTING_FIXUP | publisher_stripped={bl_fields['publisher']} | journal={bl_fields.get('journal')}",
                category=LogCategory.CLEANUP,
            )
            bl_fields.pop("publisher", None)
            if existing_file_path:
                bib_str = bt.bibtex_from_dict(baseline_entry)
                safe_write_file(existing_file_path, bib_str)

        # Quick fixup: downgrade @article with preprint DOI -> @misc
        bl_doi = (bl_fields.get("doi") or "").strip()
        if baseline_entry.get("type") == "article" and bl_doi and idu.is_secondary_doi(bl_doi):
            venue = bl_fields.get("journal", "")
            logger.debug(
                f"EXISTING_FIXUP | article_preprint_doi->misc | doi={bl_doi} | venue={venue}",
                category=LogCategory.CLEANUP,
            )
            baseline_entry["type"] = "misc"
            if venue:
                bl_fields["howpublished"] = bl_fields.pop("journal")
            if existing_file_path:
                bib_str = bt.bibtex_from_dict(baseline_entry)
                safe_write_file(existing_file_path, bib_str)

        logger.info("Entry already complete; skipping enrichment", category=LogCategory.SKIP, source=LogSource.SYSTEM)
        if summary_csv_path and existing_file_path:
            try:
                rel = os.path.relpath(existing_file_path)
            except (OSError, ValueError):
                rel = existing_file_path
            # Only write a new CSV row if this file has no entry from a previous run
            if not is_known_summary_path(rel):
                append_summary_to_csv(summary_csv_path, rel, 0, flags)
        return 1

    # If no existing file found, build minimal BibTeX baseline
    if not existing_file_loaded:
        logger.info("Creating baseline BibTeX entry", category=LogCategory.ARTICLE, source=LogSource.SYSTEM)
        scholar_bib = bt.build_minimal_bibtex(title, authors_list, year_hint or 0, keyhint=result_id)

        baseline_entry = bt.parse_bibtex_to_dict(scholar_bib)
        if baseline_entry is None:
            # Parse failed - should be rare since we generated the BibTeX
            logger.error("Failed to parse Scholar BibTeX; using minimal fallback structure", category=LogCategory.ERROR)
            baseline_entry = {"type": "misc", "key": result_id or "entry", "fields": {"title": title} if title else {}}
        _bl_source = "scholar_minimal"
    else:
        _bl_source = "existing_file"
    _bl_field_names = sorted((baseline_entry.get("fields") or {}).keys()) if baseline_entry else []
    logger.debug(
        f"BASELINE_CREATE | source={_bl_source} | fields=[{', '.join(_bl_field_names)}]",
        category=LogCategory.AUDIT,
    )
    if baseline_entry is None:  # Safety net (should not happen)
        logger.error("Failed to create baseline entry; skipping article", category=LogCategory.ERROR)
        return 0
    bf = baseline_entry.get("fields") or {}
    if cluster_id:
        bf["x_scholar_cluster_id"] = cluster_id
    # Attempt to locate arXiv ID or DOI in article snippet or links
    try:
        snippet = art.get("snippet") or art.get("publication_info") or ""
        ax_from_snip = idu.find_arxiv_in_text(snippet)

        # Collect all link URLs from the article metadata
        link_texts: list[str] = [str(art[k]) for k in ("link", "link_to_pdf") if art.get(k)]
        for r in art.get("resources") or []:
            if isinstance(r, dict):
                for lk in ("link", "file_link", "url"):
                    if r.get(lk):
                        link_texts.append(str(r[lk]))
        for fld in ("versions", "resources", "websites"):
            for it in (art.get("inline_links") or {}).get(fld) or []:
                if isinstance(it, dict) and it.get("link"):
                    link_texts.append(str(it["link"]))

        # Extract arXiv ID and DOI from collected links
        ax_from_links = None
        doi_from_links = None
        for u in link_texts:
            if not ax_from_links:
                ax_from_links = idu.find_arxiv_in_text(u)
            if not doi_from_links:
                doi_from_links = idu.find_doi_in_text(u)
        ax_pick = ax_from_snip or ax_from_links
        if ax_pick:
            bf["eprint"] = ax_pick
            bf["archiveprefix"] = "arXiv"
        # Store DOI from DBLP/Scholar links if baseline has none
        if doi_from_links and not bf.get("doi"):
            bf["doi"] = doi_from_links
        logger.debug(
            f"SNIPPET_EXTRACT | arxiv_from_snippet={ax_from_snip} "
            f"| arxiv_from_links={ax_from_links} | doi_from_links={doi_from_links}",
            category=LogCategory.AUDIT,
        )
    except PARSE_ERRORS:
        pass
    baseline_entry["fields"] = bf
    ck = (
        bt.build_standard_citekey(baseline_entry, gemini_api_key=gemini_api_key) or baseline_entry.get("key") or "Entry"
    )
    baseline_entry["key"] = ck

    # Save baseline only if we didn't load from existing file
    if existing_file_loaded:
        # Remove existing files outside the contribution window
        if min_year > 0 and existing_file_path:
            ex_year = extract_year_from_any((baseline_entry or {}).get("fields", {}).get("year"), fallback=0) or 0
            if 0 < ex_year < min_year:
                logger.info(
                    f"Removing out-of-window existing file (year={ex_year} < {min_year}): "
                    f"{os.path.basename(existing_file_path)}",
                    category=LogCategory.CLEANUP,
                )
                os.remove(existing_file_path)
                return 0
        path = existing_file_path
        logger.info(f"Using existing file: {path}", category=LogCategory.SKIP)
    else:
        # Defer disk write for bare baselines (no DOI) to avoid creating
        # transient files that get renamed/cleaned during enrichment.
        # The final entry will be written after Phase 4 by save_entry_to_file.
        if not bf.get("doi", "").strip():
            path = None
            logger.info(
                "Baseline deferred (no DOI; will write after enrichment)",
                category=LogCategory.SKIP,
                source=LogSource.SYSTEM,
            )
        else:
            path, was_written = mu.save_entry_to_file(
                out_dir,
                effective_id,
                baseline_entry,
                gemini_api_key=gemini_api_key,
                author_name=rec.name,
            )
            if was_written:
                logger.success(f"Saved baseline: {path}", category=LogCategory.SAVE, source=LogSource.SYSTEM)
            else:
                # save_entry_to_file found a duplicate and skipped writing —
                # the article is already on disk under a different name.
                # Skip enrichment entirely to avoid churn.
                logger.info(
                    f"Baseline duplicate detected; skipping enrichment: {path}",
                    category=LogCategory.SKIP,
                    source=LogSource.SYSTEM,
                )
                if summary_csv_path and path:
                    try:
                        rel = os.path.relpath(path)
                    except (OSError, ValueError):
                        rel = path
                    if not is_known_summary_path(rel):
                        append_summary_to_csv(summary_csv_path, rel, 0, flags)
                return 1

    enr_list: list[tuple[str, dict[str, Any]]] = []
    # Collect DOIs from ALL Phase 2 candidates (matched or not) for
    # deterministic dedup: if a candidate's DOI already exists on disk,
    # we skip writing even when the candidate was rejected by the match gate.
    all_candidate_dois: set[str] = set()

    # ===== PHASE 1: Early DOI Validation =====
    logger.info("▶ Phase 1: Early DOI Validation", category=LogCategory.ARTICLE)

    # if the baseline already has a DOI, use it to get better metadata early on
    doi_validated = False  # Track if we successfully validated the DOI
    unvalidated_doi: str | None = None  # Stash failed DOI for Phase 3 retry
    p1_doi = bf.get("doi")
    logger.debug(
        f"PHASE1_START | doi={p1_doi} | has_doi={bool(p1_doi)}",
        category=LogCategory.AUDIT,
    )
    try:
        doi_early = idu.normalize_doi(bf.get("doi"))
        if doi_early:
            logger.info(f"Validating DOI: {doi_early}", category=LogCategory.SEARCH, source=LogSource.DOI)
            doi_matched = process_validated_doi(doi_early, baseline_entry, result_id, enr_list, flags)

            # If DOI failed validation, stash it for Phase 3 and remove from baseline
            if not doi_matched:
                unvalidated_doi = doi_early
                bf.pop("doi", None)
                logger.warn(
                    "DOI validation failed, removed from baseline (will retry in Phase 3)",
                    category=LogCategory.ARTICLE,
                    source=LogSource.DOI,
                )
            else:
                doi_validated = True
                logger.success("DOI validated successfully", category=LogCategory.MATCH, source=LogSource.DOI)
            logger.debug(
                f"PHASE1_RESULT | doi={doi_early} | validated={doi_validated} | stashed={unvalidated_doi is not None}",
                category=LogCategory.AUDIT,
            )
    except PARSE_ERRORS:
        pass

    # ===== PHASE 2: API Enrichment =====
    logger.info("▶ Phase 2: API Enrichment", category=LogCategory.ARTICLE)
    logger.debug(
        f"PHASE2_START | title={title[:60]} | doi_validated={doi_validated}",
        category=LogCategory.AUDIT,
    )

    # Skip Scholar citation fetch if we loaded an existing file (optimization to reduce API usage)
    if existing_file_loaded:
        logger.info("Skipped (using existing file as baseline)", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
    elif not serply_key:
        logger.info("Skipped (no Serply key)", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
    else:
        logger.info("Fetching citation metadata", category=LogCategory.FETCH, source=LogSource.SCHOLAR)
        if title:
            try:
                fields = fetch_scholar_citation(serply_key, title, rec.name)
                if fields:
                    sch_page_bib = build_bibtex_from_scholar_fields(fields, keyhint=result_id)
                    if sch_page_bib:
                        sch_page_dict = bt.parse_bibtex_to_dict(sch_page_bib)
                        if sch_page_dict and bt.bibtex_entries_match_strict(baseline_entry, sch_page_dict):
                            enr_list.append(("scholar_page", sch_page_dict))
                            flags["scholar_page"] = True
                            logger.success(
                                "Match validated and added to enrichment",
                                category=LogCategory.MATCH,
                                source=LogSource.SCHOLAR,
                            )
                        else:
                            logger.info(
                                "Citation did not match baseline",
                                category=LogCategory.SKIP,
                                source=LogSource.SCHOLAR,
                            )
                    else:
                        logger.info("No BibTeX generated", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
                else:
                    logger.info("No data returned", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
            except ALL_API_ERRORS as e:
                logger.warn(f"Citation fetch error: {e}", category=LogCategory.ERROR, source=LogSource.SCHOLAR)
        else:
            logger.info("No title available; skipped", category=LogCategory.SKIP, source=LogSource.SCHOLAR)

    logger.debug(f"SEARCH_START | source=S2 | title={title[:60]}", category=LogCategory.AUDIT)
    s2_paper = None
    if s2_api_key:
        try:
            s2_papers = s2_search_papers_multiple(title, rec.name, s2_api_key, max_results=5)
            if s2_papers:
                _, s2_paper = _try_multiple_candidates(
                    LogSource.S2,
                    s2_papers,
                    build_bibtex_from_s2,
                    baseline_entry,
                    result_id,
                    enr_list,
                    flags,
                    "s2",
                    max_candidates=5,
                    seen_dois=all_candidate_dois,
                )
                if s2_paper:
                    s2_id = s2_paper.get("paperId")
                    if s2_id:
                        baseline_entry["fields"]["x_s2_paper_id"] = str(s2_id)
                        logger.debug(
                            f"ID_EXTRACT | api=S2 | field=x_s2_paper_id | value={s2_id}",
                            category=LogCategory.AUDIT,
                        )
        except ALL_API_ERRORS as e:
            logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.S2)

    logger.debug(f"SEARCH_START | source=Crossref | title={title[:60]}", category=LogCategory.AUDIT)
    cr_item = None
    try:
        cr_items = crossref_search_multiple(title, rec.name, max_results=5)
        if cr_items:
            _, cr_item = _try_multiple_candidates(
                LogSource.CROSSREF,
                cr_items,
                build_bibtex_from_crossref,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "crossref",
                max_candidates=5,
                seen_dois=all_candidate_dois,
            )
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.CROSSREF)

    logger.debug(f"SEARCH_START | source=OpenReview | title={title[:60]}", category=LogCategory.AUDIT)
    try:
        or_notes = openreview_search_papers_multiple(title, rec.name, or_creds, max_results=5)
        if or_notes:
            _try_multiple_candidates(
                LogSource.OPENREVIEW,
                or_notes,
                build_bibtex_from_openreview,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "openreview",
                max_candidates=5,
                seen_dois=all_candidate_dois,
            )
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.OPENREVIEW)

    logger.debug(f"SEARCH_START | source=arXiv | title={title[:60]}", category=LogCategory.AUDIT)
    arxiv_entry = None
    try:
        arxiv_entries = arxiv_search(title, rec.name, year_hint)
        if arxiv_entries:
            _, arxiv_entry = _try_multiple_candidates(
                LogSource.ARXIV,
                arxiv_entries,
                build_bibtex_from_arxiv,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "arxiv",
                max_candidates=5,
                seen_dois=all_candidate_dois,
            )
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.ARXIV)

    logger.debug(f"SEARCH_START | source=OpenAlex | title={title[:60]}", category=LogCategory.AUDIT)
    oa_work = None
    try:
        oa_works = openalex_search_multiple(title, rec.name, max_results=5)
        if oa_works:
            _, oa_work = _try_multiple_candidates(
                LogSource.OPENALEX,
                oa_works,
                build_bibtex_from_openalex,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "openalex",
                max_candidates=5,
                seen_dois=all_candidate_dois,
            )
            if oa_work:
                oa_id = oa_work.get("id")
                if oa_id:
                    baseline_entry["fields"]["x_openalex_id"] = str(oa_id)
                    logger.debug(
                        f"ID_EXTRACT | api=OpenAlex | field=x_openalex_id | value={oa_id}",
                        category=LogCategory.AUDIT,
                    )
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.OPENALEX)
    logger.debug(f"SEARCH_START | source=PubMed | title={title[:60]}", category=LogCategory.AUDIT)
    pm_article = None
    try:
        pm_articles = pubmed_search_papers_multiple(title, rec.name, max_results=5)
        if pm_articles:
            _, pm_article = _try_multiple_candidates(
                LogSource.PUBMED,
                pm_articles,
                build_bibtex_from_pubmed,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "pubmed",
                max_candidates=5,
                seen_dois=all_candidate_dois,
            )
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.PUBMED)
    logger.debug(f"SEARCH_START | source=EuropePMC | title={title[:60]}", category=LogCategory.AUDIT)
    epmc_article = None
    try:
        epmc_articles = europepmc_search_papers_multiple(title, rec.name, max_results=5)
        if epmc_articles:
            _, epmc_article = _try_multiple_candidates(
                LogSource.EUROPEPMC,
                epmc_articles,
                build_bibtex_from_europepmc,
                baseline_entry,
                result_id,
                enr_list,
                flags,
                "europepmc",
                max_candidates=5,
                seen_dois=all_candidate_dois,
            )
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.EUROPEPMC)

    # ===== PHASE 2.5: Venue-Based Search (SerpAPI publication string) =====
    # Only attempt when no enrichment matched so far — avoids redundant API calls.
    if not enr_list:
        pub_string = art.get("publication") or ""
        if pub_string:
            parsed_pub = parse_publication_string(pub_string)
            if parsed_pub and parsed_pub.confidence >= PUB_PARSE_TIER1_MIN_CONFIDENCE:
                logger.info(
                    f"▶ Phase 2.5: Venue search | venue={parsed_pub.venue_name[:40]} "
                    f"| type={parsed_pub.venue_type} | conf={parsed_pub.confidence:.2f}",
                    category=LogCategory.ARTICLE,
                )

                # Inject arXiv ID from publication string (enables Phase 3 DOI discovery)
                if parsed_pub.arxiv_id and not bf.get("eprint"):
                    bf["eprint"] = parsed_pub.arxiv_id
                    bf["archiveprefix"] = "arXiv"
                    ax_doi = idu.normalize_doi(f"10.48550/arxiv.{parsed_pub.arxiv_id}")
                    if ax_doi:
                        all_candidate_dois.add(ax_doi)

                # Inject DOI fragment from bioRxiv/medRxiv publication string
                if parsed_pub.doi_fragment and not bf.get("doi"):
                    bf["doi"] = parsed_pub.doi_fragment
                    norm_doi = idu.normalize_doi(parsed_pub.doi_fragment)
                    if norm_doi:
                        all_candidate_dois.add(norm_doi)

                # Tier 1: venue-based Crossref search (journal/conference only)
                if parsed_pub.venue_type in ("journal", "conference"):
                    try:
                        cr_venue_items = crossref_search_by_venue(
                            title,
                            rec.name,
                            container_title=parsed_pub.venue_name,
                            max_results=5,
                        )
                        if cr_venue_items:
                            _try_multiple_candidates(
                                LogSource.CROSSREF,
                                cr_venue_items,
                                build_bibtex_from_crossref,
                                baseline_entry,
                                result_id,
                                enr_list,
                                flags,
                                "crossref",
                                max_candidates=5,
                                seen_dois=all_candidate_dois,
                            )
                    except ALL_API_ERRORS as e:
                        logger.warn(
                            f"Venue-based Crossref error: {e}",
                            category=LogCategory.ERROR,
                            source=LogSource.CROSSREF,
                        )

                # Tier 1: venue-based OpenAlex search (only if Crossref missed)
                if not enr_list and parsed_pub.venue_type in ("journal", "conference"):
                    try:
                        oa_venue_items = openalex_search_by_venue(
                            title,
                            rec.name,
                            venue_name=parsed_pub.venue_name,
                            max_results=5,
                        )
                        if oa_venue_items:
                            _try_multiple_candidates(
                                LogSource.OPENALEX,
                                oa_venue_items,
                                build_bibtex_from_openalex,
                                baseline_entry,
                                result_id,
                                enr_list,
                                flags,
                                "openalex",
                                max_candidates=5,
                                seen_dois=all_candidate_dois,
                            )
                    except ALL_API_ERRORS as e:
                        logger.warn(
                            f"Venue-based OpenAlex error: {e}",
                            category=LogCategory.ERROR,
                            source=LogSource.OPENALEX,
                        )

    # ===== PHASE 3: Late DOI Discovery =====
    logger.info("▶ Phase 3: Late DOI Discovery", category=LogCategory.ARTICLE)
    # Do late DOI negotiation if we haven't validated a DOI, or if the validated
    # DOI is a preprint (we may find a published DOI to upgrade to)
    baseline_doi = idu.normalize_doi(bf.get("doi"))
    is_secondary = bool(baseline_doi and idu.is_secondary_doi(baseline_doi))
    run_phase3 = not doi_validated or is_secondary
    logger.debug(
        f"PHASE3_START | doi_validated={doi_validated} | baseline_doi={baseline_doi} "
        f"| is_secondary={is_secondary} | run_phase3={run_phase3}",
        category=LogCategory.AUDIT,
    )
    if run_phase3:
        logger.info(
            "Extracting DOI candidates from enrichment sources",
            category=LogCategory.SEARCH,
            source=LogSource.DOI,
        )
        try:
            doi_candidates: list[str] = []

            def _add_doi(source: str, doi: str | None) -> None:
                """Append a DOI candidate and log it for audit."""
                if doi:
                    doi_candidates.append(str(doi))
                    logger.debug(
                        f"DOI_CANDIDATE | source={source} | doi={doi} | is_secondary={idu.is_secondary_doi(str(doi))}",
                        category=LogCategory.AUDIT,
                    )

            # Include stashed unvalidated DOI from Phase 1 for retry
            _add_doi("phase1_stash", unvalidated_doi)

            # Infer arXiv DOIs from eprint fields or URLs in baseline and enrichers
            # (deterministic — no HTTP required)
            _bl_eprint = idu.extract_arxiv_eprint(baseline_entry)
            if _bl_eprint:
                _add_doi("baseline_eprint", f"10.48550/arxiv.{_bl_eprint}")
            _bl_url = bf.get("url", "")
            _bl_url_m = _ARXIV_ABS_RE.search(str(_bl_url))
            if _bl_url_m:
                _add_doi("baseline_url", f"10.48550/arxiv.{_bl_url_m.group(1)}")
            for _enr_src, _enr_data in enr_list:
                _eprint = idu.extract_arxiv_eprint(_enr_data)
                if _eprint:
                    _add_doi(f"eprint_{_enr_src}", f"10.48550/arxiv.{_eprint}")
                else:
                    # Check enricher's URL field for arXiv abstract links
                    _enr_url = (_enr_data.get("fields") or {}).get("url", "")
                    _m = _ARXIV_ABS_RE.search(str(_enr_url))
                    if _m:
                        _add_doi(f"url_{_enr_src}", f"10.48550/arxiv.{_m.group(1)}")

            # Only extract DOIs from API results that successfully matched baseline
            if s2_paper and flags.get("s2"):
                ext = s2_paper.get("externalIds") or {}
                for doi_field in (ext.get("DOI") if isinstance(ext, dict) else None, s2_paper.get("doi")):
                    _add_doi("S2", doi_field)
            if cr_item and cr_item.get("DOI") and flags.get("crossref"):
                _add_doi("Crossref", cr_item.get("DOI"))
            if arxiv_entry and arxiv_entry.get("doi") and flags.get("arxiv"):
                _add_doi("arXiv", arxiv_entry.get("doi"))
            if oa_work and oa_work.get("doi") and flags.get("openalex"):
                _add_doi("OpenAlex", oa_work.get("doi"))
            if pm_article and flags.get("pubmed"):
                for aid in pm_article.get("articleids") or []:
                    if aid.get("idtype") == "doi":
                        _add_doi("PubMed", aid.get("value") or "")
            if epmc_article and epmc_article.get("doi") and flags.get("europepmc"):
                _add_doi("EuropePMC", epmc_article.get("doi"))

            url_candidates: list[str] = []
            # URLs from baseline are always safe to use
            base_url = bf.get("url")
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
                    numeric_id = str(pmcid).removeprefix("PMC")
                    url_candidates.append(f"https://europepmc.org/article/PMC/{numeric_id}")

            # Deterministic DOI extraction from known URL patterns
            # (no HTTP required — prevents network non-determinism)
            for u in filter(None, url_candidates):
                m = _ARXIV_ABS_RE.search(str(u))
                if m:
                    inferred = f"10.48550/arxiv.{m.group(1)}"
                    doi_candidates.append(inferred)
                    logger.debug(
                        f"DOI_FROM_URL | url={u} | doi_inferred={inferred}",
                        category=LogCategory.AUDIT,
                    )
                    break

            # Fall back to cached HTML scraping only if no DOI found yet
            if not doi_candidates:
                from src.cache import response_cache as _doi_cache

                for u in filter(None, url_candidates):
                    _u_str = str(u)
                    _cached_doi = _doi_cache.get("doi_from_html", _u_str)
                    if _cached_doi is not None:
                        _cd = _cached_doi.get("doi", "")
                        if _cd:
                            doi_candidates.append(_cd)
                            logger.debug(
                                f"DOI_FROM_HTML | url={_u_str} | doi_found={_cd} | cached=True",
                                category=LogCategory.AUDIT,
                            )
                            break
                        continue  # negative cache hit
                    try:
                        html = http_get_text(u)
                    except ALL_API_ERRORS:
                        _doi_cache.put("doi_from_html", _u_str, {"doi": ""}, ttl_days=60)
                        continue
                    d = idu.find_doi_in_html(html)
                    _doi_cache.put("doi_from_html", _u_str, {"doi": d or ""}, ttl_days=60)
                    if d:
                        logger.debug(
                            f"DOI_FROM_HTML | url={_u_str} | doi_found={d}",
                            category=LogCategory.AUDIT,
                        )
                        doi_candidates.append(d)
                        break

            doi_candidates = [d for d in {idu.normalize_doi(d) for d in doi_candidates if d} if d]
            # Feed Phase 3 DOIs into the candidate set for deterministic dedup
            all_candidate_dois.update(doi_candidates)
            # Published DOIs first, preprint/data DOIs last
            doi_candidates.sort(key=lambda d: 1 if idu.is_secondary_doi(d) else 0)
            published_first = bool(doi_candidates and not idu.is_secondary_doi(doi_candidates[0]))
            logger.debug(
                f"DOI_CANDIDATES_RANKED | count={len(doi_candidates)} "
                f"| order=[{', '.join(doi_candidates)}] | published_first={published_first}",
                category=LogCategory.AUDIT,
            )

            if doi_candidates:
                logger.info(
                    f"Found {len(doi_candidates)} DOI candidate(s): {', '.join(doi_candidates)}",
                    category=LogCategory.SEARCH,
                    source=LogSource.DOI,
                )
                doi_matched = False

                # Try each DOI candidate until we find one that validates
                for doi_idx, doi_candidate in enumerate(doi_candidates, 1):
                    logger.info(
                        f"Validating DOI candidate: {doi_candidate}",
                        category=LogCategory.SEARCH,
                        source=LogSource.DOI,
                    )
                    # When a DOI was inferred from an enricher's arXiv eprint,
                    # temporarily inject it into the baseline so DOI_EXACT match
                    # fires in validation (the eprint already confirmed identity;
                    # the CSL title may differ from the preprint title).
                    _bl_doi_before = bf.get("doi")
                    _doi_norm = idu.normalize_doi(doi_candidate)
                    _is_eprint_doi = _doi_norm and any(
                        idu.normalize_doi(f"10.48550/arxiv.{idu.extract_arxiv_eprint(ed) or ''}") == _doi_norm
                        for _, ed in enr_list
                        if idu.extract_arxiv_eprint(ed)
                    )
                    if _is_eprint_doi and not _bl_doi_before:
                        bf["doi"] = doi_candidate
                    candidate_matched = process_validated_doi(doi_candidate, baseline_entry, result_id, enr_list, flags)
                    # Restore baseline DOI to avoid polluting later logic
                    if _is_eprint_doi and not _bl_doi_before:
                        bf.pop("doi", None)
                    logger.debug(
                        f"DOI_VALIDATE_ATTEMPT | #{doi_idx} | doi={doi_candidate} | result={candidate_matched}",
                        category=LogCategory.AUDIT,
                    )

                    if candidate_matched:
                        doi_matched = True
                        break  # Stop after first successful validation
                    else:
                        logger.info("Trying next DOI candidate...", category=LogCategory.SEARCH, source=LogSource.DOI)

                # If none of the DOI candidates validated, warn the user
                if not doi_matched:
                    logger.warn(
                        f"None of {len(doi_candidates)} DOI candidate(s) validated against baseline",
                        category=LogCategory.SKIP,
                        source=LogSource.DOI,
                    )
            else:
                logger.info("No DOI discovered; skipped", category=LogCategory.SKIP, source=LogSource.DOI)
        except ALL_API_ERRORS as e:
            logger.warn(f"DOI negotiation error: {e}", category=LogCategory.ERROR, source=LogSource.DOI)
    else:
        logger.info(
            "DOI already validated early; skipping late DOI negotiation",
            category=LogCategory.SKIP,
            source=LogSource.DOI,
        )

    # ===== PHASE 4: Merge & Save =====
    logger.info("▶ Phase 4: Merge & Save", category=LogCategory.ARTICLE)
    enr_source_names = [name for name, _ in enr_list]
    logger.debug(
        f"PHASE4_START | enricher_count={len(enr_list)} | sources=[{', '.join(enr_source_names)}]",
        category=LogCategory.AUDIT,
    )

    logger.info("Applying trust policy and merging enrichments", category=LogCategory.SAVE, source=LogSource.SYSTEM)
    try:
        merged = mu.merge_with_policy(baseline_entry, enr_list)
        merged_fields = merged.get("fields") or {}

        # Phase-4 post-merge canonicalization: entry-type reclassification and
        # text/venue normalization, single-sourced in src/canonicalize.py.
        # POST_MERGE is the terminal stage; absence rules (article/inproceedings
        # missing venue, preprint-DOI downgrade) fire here after enrichment.
        canonicalize(merged, stage=CanonicalStage.POST_MERGE)

        # Annotate bare stubs: no enrichers, no DOI, no venue
        is_bare_stub = (
            not enr_list
            and not (merged_fields.get("doi") or "").strip()
            and not (merged_fields.get("journal") or "").strip()
            and not (merged_fields.get("booktitle") or "").strip()
        )
        if is_bare_stub:
            # Tier 2: populate fields directly from SerpAPI publication string
            pub_string = art.get("publication") or ""
            parsed_pub = parse_publication_string(pub_string)
            tier2_applied = False

            if parsed_pub and parsed_pub.confidence >= PUB_PARSE_TIER2_MIN_CONFIDENCE:
                if parsed_pub.venue_type == "journal":
                    merged_fields["journal"] = parsed_pub.venue_name
                    if parsed_pub.volume:
                        merged_fields["volume"] = parsed_pub.volume
                    if parsed_pub.issue:
                        merged_fields["number"] = parsed_pub.issue
                    if parsed_pub.pages:
                        merged_fields["pages"] = parsed_pub.pages
                    merged["type"] = "article"
                    merged_fields["note"] = "Venue from SerpAPI publication string (unverified)"
                    tier2_applied = True
                    logger.info(
                        f"TIER2 | journal={parsed_pub.venue_name} | vol={parsed_pub.volume} | pages={parsed_pub.pages}",
                        category=LogCategory.AUDIT,
                    )
                elif parsed_pub.venue_type == "conference":
                    merged_fields["booktitle"] = parsed_pub.venue_name
                    if parsed_pub.pages:
                        merged_fields["pages"] = parsed_pub.pages
                    merged["type"] = "inproceedings"
                    merged_fields["note"] = "Venue from SerpAPI publication string (unverified)"
                    tier2_applied = True
                    logger.info(
                        f"TIER2 | booktitle={parsed_pub.venue_name} | pages={parsed_pub.pages}",
                        category=LogCategory.AUDIT,
                    )

            if parsed_pub and not tier2_applied:
                if parsed_pub.venue_type == "patent":
                    merged_fields["note"] = f"US Patent {parsed_pub.patent_number}"
                    tier2_applied = True
                    logger.info(
                        f"TIER2 | patent={parsed_pub.patent_number}",
                        category=LogCategory.AUDIT,
                    )
                elif parsed_pub.venue_type == "preprint":
                    merged_fields["howpublished"] = parsed_pub.venue_name
                    if parsed_pub.arxiv_id:
                        merged_fields["eprint"] = parsed_pub.arxiv_id
                        merged_fields["archiveprefix"] = "arXiv"
                    tier2_applied = True
                    logger.info(
                        f"TIER2 | preprint={parsed_pub.venue_name}",
                        category=LogCategory.AUDIT,
                    )

            if not tier2_applied:
                merged_fields["note"] = "Unenriched: no enrichment sources matched"
                logger.warn(
                    "Bare stub: no venue, no DOI, no enrichment; annotated with note",
                    category=LogCategory.AUDIT,
                )

        # Delete entries where title equals journal or booktitle (corrupted Scholar data).
        # Placed after Tier 2 filling so it catches entries populated from SerpAPI pub strings.
        _p4_title_lower = (merged_fields.get("title") or "").strip().lower()
        if _p4_title_lower:
            _p4_journal_lower = (merged_fields.get("journal") or "").strip().lower()
            _p4_booktitle_lower = (merged_fields.get("booktitle") or "").strip().lower()
            if (_p4_journal_lower and _p4_title_lower == _p4_journal_lower) or (
                _p4_booktitle_lower and _p4_title_lower == _p4_booktitle_lower
            ):
                logger.debug(
                    f"TITLE_IS_VENUE | title={_p4_title_lower[:60]} | skipping entry",
                    category=LogCategory.AUDIT,
                )
                if path and os.path.isfile(path):
                    os.remove(path)
                return 0

        # Skip entries with type "book" (proceedings volumes, edited books — not individual papers)
        if merged.get("type") == "book":
            has_file = bool(path and os.path.isfile(path))
            logger.debug(
                f"BOOK_SKIP | type=book | file_deleted={has_file}",
                category=LogCategory.AUDIT,
            )
            logger.warn(
                "Entry is a book/proceedings volume, not an individual paper; skipping",
                category=LogCategory.SKIP,
                source=LogSource.SYSTEM,
            )
            if has_file and path:
                os.remove(path)
            return 0

        # Verify target author appears in the paper's author list to catch
        # Scholar profile contamination (e.g., different person with same surname)
        merged_authors = merged_fields.get("author", "")
        author_found = not merged_authors or author_name_matches(rec.name, merged_authors)
        logger.debug(
            f"AUTHOR_FILTER | target={rec.name} | paper_authors={str(merged_authors)[:80]} | found={author_found}",
            category=LogCategory.AUDIT,
        )
        if merged_authors and not author_found:
            # Check if enrichment corrupted the author field (misattributed DOI).
            # If the ORIGINAL file had the correct author, keep it — don't delete.
            if path and os.path.isfile(path) and baseline_entry:
                baseline_authors = bf.get("author", "")
                if baseline_authors and author_name_matches(rec.name, baseline_authors):
                    logger.warn(
                        f"Enrichment corrupted author field ('{str(merged_authors)[:60]}'); "
                        f"keeping original file: {os.path.basename(path)}",
                        category=LogCategory.SKIP,
                        source=LogSource.SYSTEM,
                    )
                    return 0
            logger.warn(
                f"Target author '{rec.name}' not found in paper authors; skipping",
                category=LogCategory.SKIP,
                source=LogSource.SYSTEM,
            )
            if path and os.path.isfile(path):
                os.remove(path)
            return 0

        # Deterministic dedup: check if any DOI (from Phase 2 candidates, Phase 3
        # discovery, or the merged entry itself) already exists in a DIFFERENT file
        # on disk.  This prevents oscillation where a preprint/published pair creates
        # a file under the preprint title that gets enriched and renamed every run.
        merged_doi = idu.normalize_doi(merged_fields.get("doi", ""))
        _merged_eprint = idu.extract_arxiv_eprint(merged)
        if _merged_eprint:
            all_candidate_dois.add(idu.normalize_doi(f"10.48550/arxiv.{_merged_eprint}") or "")
        if merged_doi:
            all_candidate_dois.add(merged_doi)
        # Exclude the DOI of the prefer_path file itself to avoid self-matching
        prefer_doi = _read_doi_from_file(path) if path and os.path.isfile(path) else ""
        check_dois = all_candidate_dois - {prefer_doi} if prefer_doi else all_candidate_dois
        if check_dois:
            for existing_bib in iter_author_bibs(author_dir):
                epath = os.path.join(author_dir, existing_bib)
                if path and os.path.abspath(epath) == os.path.abspath(path):
                    continue  # skip self
                try:
                    with open(epath, encoding="utf-8") as ef:
                        edict = bt.parse_bibtex_to_dict(ef.read())
                    if not edict:
                        continue
                    edoi = idu.normalize_doi((edict.get("fields") or {}).get("doi", ""))
                    if not edoi or edoi not in check_dois:
                        continue
                    # Guard: verify the DOI match is genuine by comparing titles.
                    # Phase-2 candidate DOIs can be false matches (API returned
                    # the wrong DOI for the query title).
                    e_title = (edict.get("fields") or {}).get("title", "")
                    m_title = merged_fields.get("title", "")
                    if e_title and m_title:
                        doi_sim = title_similarity(e_title, m_title)
                        if doi_sim < SIM_PREPRINT_TITLE_THRESHOLD:
                            logger.debug(
                                f"CANDIDATE_DOI_DEDUP_REJECTED | doi={edoi}"
                                f" | existing={existing_bib}"
                                f" | sim={doi_sim:.3f} | titles_differ",
                                category=LogCategory.DEDUP,
                            )
                            _revert_misattributed_doi(merged_fields, edoi, doi_validated, doi_early)
                            continue
                    logger.debug(
                        f"CANDIDATE_DOI_DEDUP | doi={edoi} | existing={existing_bib} | skipping_write=True",
                        category=LogCategory.DEDUP,
                    )
                    if path and os.path.isfile(path):
                        os.remove(path)
                        logger.debug(
                            f"FILE_CLEANUP | removed={path} | reason=candidate_doi_on_disk",
                            category=LogCategory.DEDUP,
                        )
                    return 0
                except (OSError, UnicodeDecodeError):
                    continue

        merged["key"] = bt.build_standard_citekey(merged, gemini_api_key=gemini_api_key) or merged.get("key") or "Entry"

        # Year-window guard: reject files whose enriched year falls outside the window
        if min_year > 0:
            final_year = extract_year_from_any(merged.get("fields", {}).get("year"), fallback=0) or 0
            if 0 < final_year < min_year:
                logger.info(
                    f"Skipping out-of-window entry (year={final_year} < {min_year}): {title[:60]}",
                    category=LogCategory.SKIP,
                )
                # Clean up baseline file if we created one
                if path and os.path.isfile(path):
                    os.remove(path)
                return 0

        path2, was_written = mu.save_entry_to_file(
            out_dir, effective_id, merged, prefer_path=path, gemini_api_key=gemini_api_key, author_name=rec.name
        )
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

        # ===== Enrichment Summary =====
        logger.info("▶ Enrichment Summary", category=LogCategory.ARTICLE)

        # Count total enrichment sources (excluding doi_csl and doi_bibtex as they're part of doi_validated)
        enrichment_sources = {
            "scholar_page": "Scholar Citation",
            "s2": "Semantic Scholar",
            "crossref": "Crossref",
            "openreview": "OpenReview",
            "arxiv": "arXiv",
            "openalex": "OpenAlex",
            "pubmed": "PubMed",
            "europepmc": "Europe PMC",
        }

        # Log DOI status separately
        doi_methods = [m for m, k in [("CSL", "doi_csl"), ("BibTeX", "doi_bibtex")] if flags.get(k)]
        if doi_methods:
            logger.success(
                f"DOI: {' + '.join(doi_methods)}",
                category=LogCategory.SAVE,
                source=LogSource.DOI,
            )

        # Count and log enrichment sources
        enriched_count = sum(1 for k in enrichment_sources if flags.get(k))
        total_sources = len(enrichment_sources)

        # Group matched and unmatched sources
        matched_sources = [label for key, label in enrichment_sources.items() if flags.get(key)]
        unmatched = [label for key, label in enrichment_sources.items() if not flags.get(key)]

        if flags.get("doi_csl"):
            doi_status = "csl"
        elif flags.get("doi_bibtex"):
            doi_status = "bibtex"
        else:
            doi_status = "none"
        logger.debug(
            f"ENRICHMENT_SUMMARY | doi_status={doi_status} | enriched={enriched_count}/{total_sources} "
            f"| matched=[{', '.join(matched_sources)}] | unmatched=[{', '.join(unmatched)}]",
            category=LogCategory.AUDIT,
        )

        logger.info(
            f"Coverage: {enriched_count}/{total_sources} sources",
            category=LogCategory.SAVE,
            source=LogSource.SYSTEM,
        )

        if matched_sources:
            logger.success(f"Matched: {', '.join(matched_sources)}", category=LogCategory.SAVE, source=LogSource.SYSTEM)
        if unmatched:
            logger.info(f"Not matched: {', '.join(unmatched)}", category=LogCategory.SKIP, source=LogSource.SYSTEM)

        if summary_csv_path and was_written:
            append_summary_to_csv(summary_csv_path, rel, total_true, flags)
    except (*PARSE_ERRORS, OSError, RuntimeError) as e:
        logger.error(f"Merge error: {e}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return 0

    return 1


def process_record(
    serpapi_key: str,
    serply_key: str | None,
    rec: Record,
    out_dir: str,
    max_pubs: int | None = 1,
    s2_api_key: str | None = None,
    or_creds: tuple[str, str] | None = None,
    delay: float = 0.0,
    gemini_api_key: str | None = None,
    summary_csv_path: str | None = None,
) -> int:
    """Fetch, deduplicate, and enrich recent publications for one author.

    Returns the number of BibTeX files successfully written.
    """
    # Setup thread-local logging for this author
    effective_id = rec.scholar_id or rec.dblp or ""
    author_dirname = format_author_dirname(rec.name, effective_id)
    author_log_path = os.path.join(out_dir, author_dirname, "author.log")

    logger.set_log_file(author_log_path)

    try:
        logger.step(
            f"Author: {rec.name} (Scholar={rec.scholar_id or 'N/A'}, DBLP={rec.dblp or 'N/A'})",
            category=LogCategory.AUTHOR,
            source=LogSource.SYSTEM,
        )

        min_year = get_min_year()

        scholar_windowed = []
        if rec.scholar_id:
            logger.info("Request author publications", category=LogCategory.FETCH, source=LogSource.SCHOLAR)

            scholar_articles: list[dict[str, Any]] = []
            max_fetch_retries = 3

            # SerpAPI call — pagination handled internally by serpapi_scholar
            data = {}
            for attempt in range(1, max_fetch_retries + 1):
                data = fetch_author_publications(
                    serpapi_key,
                    rec.scholar_id,
                    rec.name,
                    num=MAX_PUBLICATIONS_PER_AUTHOR,
                    min_year=min_year,
                )
                if data.get("articles"):
                    break  # Got articles -- valid response
                if attempt < max_fetch_retries:
                    logger.warn(
                        f"Scholar API returned empty (attempt {attempt}/{max_fetch_retries}), retrying...",
                        category=LogCategory.FETCH,
                        source=LogSource.SCHOLAR,
                    )
                    time.sleep(2.0 * attempt)

            if not data.get("articles"):
                logger.warn(
                    f"Scholar API failed after {max_fetch_retries} attempts; continuing with DBLP only",
                    category=LogCategory.ERROR,
                    source=LogSource.SCHOLAR,
                )
            else:
                status = (data.get("search_metadata") or {}).get("status", "")
                if status.lower() == "error":
                    raise RuntimeError(
                        f"CiteForge error for author {rec.scholar_id}: {data.get('error') or 'Unknown error'}"
                    )

                scholar_articles = data.get("articles", [])
                logger.debug(
                    f"SCHOLAR_FETCH | articles={len(scholar_articles)}",
                    category=LogCategory.AUDIT,
                )

            if not scholar_articles:
                logger.warn("No articles returned from Scholar", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
            else:
                # Pre-clean titles to handle trailing periods consistently
                for a in scholar_articles:
                    try:
                        if a.get("title"):
                            a["title"] = trim_title_default(strip_html_tags(a["title"]))
                    except (TypeError, AttributeError):
                        pass
                logger.info(
                    f"{len(scholar_articles)} article(s) fetched",
                    category=LogCategory.FETCH,
                    source=LogSource.SCHOLAR,
                )

            scholar_windowed = [a for a in scholar_articles if (get_article_year(a) or 0) >= min_year]
            logger.debug(
                f"YEAR_WINDOW | total={len(scholar_articles)} | windowed={len(scholar_windowed)} | min_year={min_year}",
                category=LogCategory.AUDIT,
            )
            logger.info(
                f"{len(scholar_windowed)}/{len(scholar_articles)} within year window (>= {min_year})",
                category=LogCategory.FETCH,
                source=LogSource.SCHOLAR,
            )
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.SCHOLAR)

        dblp_items = []
        if rec.dblp:
            try:
                dblp_items = dblp_fetch_for_author(rec.name, rec.dblp, min_year)
                logger.info(
                    f"{len(dblp_items)} item(s) fetched within window",
                    category=LogCategory.FETCH,
                    source=LogSource.DBLP,
                )
            except FULL_OPERATION_ERRORS as e:
                logger.warn(f"Fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DBLP)
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.DBLP)

        if not scholar_windowed and not dblp_items:
            logger.info(f"No articles within year window (>= {min_year})", category=LogCategory.SKIP)
            return 0

        # merge Scholar and DBLP with full deduplication (within and across sources)
        merged_list = merge_publication_lists(scholar_windowed, dblp_items, target_author=rec.name)
        dedup_removed = len(scholar_windowed) + len(dblp_items) - len(merged_list)
        logger.debug(
            f"PUB_MERGE | scholar={len(scholar_windowed)} | dblp={len(dblp_items)} "
            f"| merged={len(merged_list)} | dedup_removed={dedup_removed}",
            category=LogCategory.AUDIT,
        )
        logger.info(
            f"Union: Scholar={len(scholar_windowed)}, DBLP={len(dblp_items)} "
            f"→ {len(merged_list)} unique publications (threshold={SIM_MERGE_DUPLICATE_THRESHOLD})",
            category=LogCategory.PLAN,
        )

        articles_sorted = sort_articles_by_year_current_first(merged_list)
        total_entries = len(articles_sorted) if max_pubs is None else min(len(articles_sorted), max_pubs)
        logger.info(
            f"Plan: process {total_entries}/{len(articles_sorted)} item(s) "
            f"(limit={'all' if max_pubs is None else max_pubs})",
            category=LogCategory.PLAN,
        )

        saved = 0
        for idx, art in enumerate(articles_sorted):
            if max_pubs is not None and idx >= max_pubs:
                break
            try:
                saved += process_article(
                    rec,
                    art,
                    serply_key,
                    out_dir,
                    s2_api_key,
                    or_creds,
                    idx=idx + 1,
                    total=total_entries,
                    gemini_api_key=gemini_api_key,
                    summary_csv_path=summary_csv_path,
                    min_year=min_year,
                )
            except FULL_OPERATION_ERRORS as e:
                logger.error(f"Article error: {e}", category=LogCategory.ERROR)
            if delay > 0:
                jittered = random.uniform(REQUEST_DELAY_MIN, REQUEST_DELAY_MAX)
                time.sleep(jittered)
        logger.info(f"Author done: saved {saved} file(s)", category=LogCategory.PLAN)
        return saved
    finally:
        # Close the thread-local log file handler
        logger.close()


def count_existing_papers(rec: Record, out_dir: str) -> int:
    """Count existing .bib files in the author's output directory."""
    effective_id = rec.scholar_id or rec.dblp or ""
    author_dirname = format_author_dirname(rec.name, effective_id)
    author_dir = os.path.join(out_dir, author_dirname)
    try:
        return len(iter_author_bibs(author_dir))
    except OSError:
        return 0


def _load_csv_titles(csv_path: str) -> dict[str, list[str]]:
    """Load titles from CSV-tracked .bib files, grouped by author directory."""
    result: dict[str, list[str]] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                fp = row.get("file_path", "")
                abs_fp = os.path.abspath(fp)
                author_dir_path = os.path.dirname(abs_fp)
                try:
                    with open(abs_fp, encoding="utf-8") as bf:
                        entry = bt.parse_bibtex_to_dict(bf.read())
                    t = (entry or {}).get("fields", {}).get("title", "")
                    if t:
                        result.setdefault(author_dir_path, []).append(t)
                except (OSError, ValueError):
                    pass
    except (OSError, ValueError):
        pass
    return result


def main() -> int:
    """Set up the run, load API keys and author records, and process all authors in parallel.

    Returns an exit code suitable for use as a command-line entry point.
    """
    out_dir = os.path.join(os.path.dirname(__file__), DEFAULT_OUT_DIR)
    try:
        os.makedirs(out_dir, exist_ok=True)
    except OSError as e:
        logger.error(f"Cannot create output directory '{out_dir}': {e}", category=LogCategory.ERROR)
        return 2

    # Set main thread log file
    logger.set_log_file(os.path.join(out_dir, "run.log"))
    reset_api_call_counts()
    logger.step("CiteForge run started", category=LogCategory.PLAN)

    serpapi_key = read_serpapi_api_key(DEFAULT_SERPAPI_KEY_FILE)
    if not serpapi_key:
        logger.error("SerpAPI key not found; cannot fetch author publications", category=LogCategory.PLAN)
        logger.close()
        return 2
    logger.success("SerpAPI key loaded", category=LogCategory.PLAN)

    serply_key = read_serply_api_key(DEFAULT_SERPLY_KEY_FILE)
    if not serply_key:
        logger.warn("Serply API key not found; Scholar citation detail will be skipped", category=LogCategory.PLAN)
    else:
        logger.success("Serply API key loaded", category=LogCategory.PLAN)

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

    # Sort authors by existing paper count (descending) so authors with more papers finish first
    # Use (count desc, name, id) for deterministic ordering when counts are equal
    logger.info(
        "Sorting authors by existing paper count (authors with more papers will be processed first)",
        category=LogCategory.PLAN,
    )
    records_with_counts = [(rec, count_existing_papers(rec, out_dir)) for rec in records]
    records_with_counts.sort(key=lambda x: (-x[1], x[0].name.lower(), x[0].scholar_id or x[0].dblp or ""))
    records = [rec for rec, _ in records_with_counts]

    # Log sorting results
    if records_with_counts:
        max_papers = records_with_counts[0][1]
        min_papers = records_with_counts[-1][1]
        logger.info(f"Author range: {max_papers} papers (max) to {min_papers} papers (min)", category=LogCategory.PLAN)

    csv_path = os.path.join(out_dir, "summary.csv")
    summary_csv_path: str | None = csv_path
    try:
        init_summary_csv(csv_path, preserve_existing=True)
        logger.success(f"Summary CSV initialized: {csv_path}", category=LogCategory.PLAN)
    except FILE_IO_ERRORS as e:
        logger.warn(f"Could not initialize summary CSV: {e}", category=LogCategory.ERROR)
        summary_csv_path = None

    total_saved = 0
    processed = 0

    # Prioritize new authors (no existing output dir) so they get browser/API
    # resources first, before cached authors consume worker slots
    def _has_output(r: Record) -> bool:
        eid = r.scholar_id or r.dblp or ""
        return os.path.isdir(os.path.join(out_dir, format_author_dirname(r.name, eid)))

    records_sorted = [r for _, r in sorted(enumerate(records), key=lambda ir: (_has_output(ir[1]), ir[0]))]

    logger.step(f"Starting parallel execution with {MAX_WORKERS} workers", category=LogCategory.PLAN)

    # Install thread exception hook to log uncaught exceptions in worker threads
    _orig_excepthook = threading.excepthook

    def _thread_excepthook(args: Any) -> None:
        logger.error(
            f"Thread '{args.thread.name if args.thread else '?'}' died: {args.exc_type.__name__}: {args.exc_value}",
            category=LogCategory.ERROR,
        )
        _orig_excepthook(args)

    threading.excepthook = _thread_excepthook

    # Per-author timeout: 30 minutes per author to handle large publication lists
    # Each article takes ~60-90s across all API calls, so 24 articles ≈ 36 minutes
    author_timeout = 1800  # seconds

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all tasks and track them
        future_to_author = {}
        for idx, rec in enumerate(records_sorted, 1):
            effective_id = rec.scholar_id or rec.dblp or "N/A"
            logger.info(f"[{idx}/{len(records)}] Queued: {rec.name} (ID: {effective_id})", category=LogCategory.PLAN)

            future = executor.submit(
                process_record,
                serpapi_key,
                serply_key,
                rec,
                out_dir,
                max_pubs=None,
                s2_api_key=s2_api_key,
                or_creds=or_creds,
                delay=REQUEST_DELAY_MIN,
                gemini_api_key=gemini_api_key,
                summary_csv_path=summary_csv_path,
            )
            future_to_author[future] = rec

        logger.step(f"All {len(records)} authors queued for processing", category=LogCategory.PLAN)

        try:
            for future in as_completed(future_to_author, timeout=author_timeout * len(records)):
                rec = future_to_author[future]
                try:
                    saved = future.result(timeout=30)
                    total_saved += saved
                    processed += 1
                    logger.success(
                        f"[{processed}/{len(records)}] Completed: {rec.name} ({saved} files saved)",
                        category=LogCategory.AUTHOR,
                    )
                except TimeoutError:
                    processed += 1
                    logger.error(
                        f"[{processed}/{len(records)}] Timeout retrieving result for {rec.name}",
                        category=LogCategory.ERROR,
                    )
                except Exception as e:
                    processed += 1
                    logger.error(
                        f"[{processed}/{len(records)}] Error processing {rec.name} ({rec.scholar_id or rec.dblp}): {e}",
                        category=LogCategory.ERROR,
                    )
        except TimeoutError:
            remaining = [r.name for f, r in future_to_author.items() if not f.done()]
            logger.error(
                f"Pipeline timed out with {len(remaining)} author(s) still pending: " + ", ".join(remaining[:5]),
                category=LogCategory.ERROR,
            )

    try:
        counts = get_api_call_counts()
        logger.step("Run complete", category=LogCategory.PLAN)
        logger.info(f"Records processed: {processed}", category=LogCategory.PLAN)
        logger.info(f"BibTeX files saved: {total_saved}", category=LogCategory.PLAN)
        if counts:
            logger.info(f"API calls: {counts}", category=LogCategory.PLAN)
        logger.info(f"Total API calls: {sum(counts.values()) if counts else 0}", category=LogCategory.PLAN)
        cache_counts = get_cache_hit_counts()
        logger.info(
            f"Cache: {cache_counts['positive']} positive, "
            f"{cache_counts['negative']} negative, {cache_counts['miss']} miss",
            category=LogCategory.PLAN,
        )
        logger.info(f"Log file: {logger.log_file_path or 'n/a'}", category=LogCategory.PLAN)

        if summary_csv_path and os.path.exists(summary_csv_path):
            flush_summary_csv(summary_csv_path)

            # Remove phantom CSV entries
            phantoms = reconcile_summary_csv(summary_csv_path)
            if phantoms:
                logger.info(f"Reconciled summary CSV: removed {phantoms} phantom entries", category=LogCategory.CLEANUP)

            # Safe orphan removal (duplicates only)
            orphans = collect_orphan_files(summary_csv_path, out_dir)
            if orphans:
                csv_titles = _load_csv_titles(summary_csv_path)
                removed = 0
                for orphan in orphans:
                    try:
                        with open(orphan, encoding="utf-8") as of:
                            orphan_entry = bt.parse_bibtex_to_dict(of.read())
                        orphan_title = (orphan_entry or {}).get("fields", {}).get("title", "")
                    except (OSError, ValueError):
                        orphan_title = ""

                    author_dir_path = os.path.dirname(orphan)
                    tracked_titles = csv_titles.get(author_dir_path, [])
                    is_dup = (
                        any(title_similarity(orphan_title, t) >= SIM_MERGE_DUPLICATE_THRESHOLD for t in tracked_titles)
                        if orphan_title
                        else False
                    )

                    if is_dup:
                        os.remove(orphan)
                        removed += 1
                        logger.info(
                            f"Removed duplicate orphan: {os.path.basename(orphan)}",
                            category=LogCategory.CLEANUP,
                        )
                    else:
                        logger.warn(
                            f"Orphan kept (no duplicate found): {os.path.basename(orphan)}",
                            category=LogCategory.CLEANUP,
                        )
                if removed:
                    logger.info(
                        f"Removed {removed}/{len(orphans)} orphan .bib files (duplicates only)",
                        category=LogCategory.CLEANUP,
                    )

            # Remove .bib files outside the contribution window
            window_min = get_min_year()
            window_removed = 0
            for entry in os.listdir(out_dir):
                d = os.path.join(out_dir, entry)
                if not os.path.isdir(d) or entry == "a2i2":
                    continue
                for fname in os.listdir(d):
                    if not fname.endswith(".bib"):
                        continue
                    fpath = os.path.join(d, fname)
                    # Try filename year first
                    m = _FILENAME_YEAR_RE.search(f"/{fname}")
                    if m:
                        if int(m.group(1)) < window_min:
                            logger.debug(
                                f"YEAR_WINDOW | removing {fname} (year={m.group(1)} < {window_min})",
                                category=LogCategory.CLEANUP,
                            )
                            os.remove(fpath)
                            window_removed += 1
                        continue
                    # Fallback: read BibTeX year field for non-standard filenames
                    try:
                        with open(fpath, encoding="utf-8") as bf:
                            parsed = bt.parse_bibtex_to_dict(bf.read())
                        bib_year = extract_year_from_any((parsed or {}).get("fields", {}).get("year"), fallback=0) or 0
                        if 0 < bib_year < window_min:
                            logger.debug(
                                f"YEAR_WINDOW | removing {fname} (bib_year={bib_year} < {window_min})",
                                category=LogCategory.CLEANUP,
                            )
                            os.remove(fpath)
                            window_removed += 1
                    except (OSError, ValueError):
                        pass
            if window_removed:
                logger.info(
                    f"Removed {window_removed} out-of-window files (year < {window_min})",
                    category=LogCategory.CLEANUP,
                )

            # Post-run fixup: apply entry type and field corrections to ALL .bib files
            # This catches orphans (files not processed during enrichment) and any
            # entries where Phase 4 corrections were undone by Tier 2 filling.
            postrun_fixed = 0
            for pr_entry_name in iter_output_dirs(out_dir):
                pr_dir = os.path.join(out_dir, pr_entry_name)
                if pr_entry_name == "a2i2":
                    continue
                for pr_fname in sorted(os.listdir(pr_dir)):
                    if not pr_fname.endswith(".bib"):
                        continue
                    pr_fpath = os.path.join(pr_dir, pr_fname)
                    try:
                        with open(pr_fpath, encoding="utf-8") as prf:
                            pr_content = prf.read()
                        pr_parsed = bt.parse_bibtex_to_dict(pr_content)
                        if pr_parsed and _fixup_bib_entry(pr_parsed):
                            bib_str = bt.bibtex_from_dict(pr_parsed)
                            if bib_str != pr_content:
                                safe_write_file(pr_fpath, bib_str)
                                postrun_fixed += 1
                    except (OSError, ValueError):
                        pass
            if postrun_fixed:
                logger.info(
                    f"Post-run fixup: corrected {postrun_fixed} .bib files",
                    category=LogCategory.CLEANUP,
                )

            # Build a2i2 joint output folder
            a2i2_count = build_a2i2_folder(DEFAULT_A2I2_INPUT, records, out_dir)
            if a2i2_count:
                logger.info(
                    f"Built a2i2 folder: {a2i2_count} deduplicated files",
                    category=LogCategory.CLEANUP,
                )

            # Write per-author baseline counts
            baseline: dict[str, int] = {}
            for entry in iter_output_dirs(out_dir):
                d = os.path.join(out_dir, entry)
                baseline[entry] = len(iter_author_bibs(d))
            baseline_path = os.path.join(out_dir, "baseline.json")
            try:
                with open(baseline_path, "w", encoding="utf-8") as bf:
                    json.dump({"total": sum(baseline.values()), "authors": baseline}, bf, indent=2)
            except OSError:
                pass

            # Write badge data for README workflow updates
            badges_path = os.path.join(out_dir, "badges.json")
            try:
                with open(badges_path, "w", encoding="utf-8") as bf:
                    total = cache_counts["positive"] + cache_counts["negative"] + cache_counts["miss"]
                    hit_rate = ((cache_counts["positive"] + cache_counts["negative"]) / total * 100) if total else 0
                    json.dump(
                        {
                            "last_updated": time.strftime("%Y-%m"),
                            "cache_positive_hits": cache_counts["positive"],
                            "cache_negative_hits": cache_counts["negative"],
                            "cache_misses": cache_counts["miss"],
                            "total_queries": total,
                            "hit_rate": round(hit_rate, 1),
                        },
                        bf,
                        indent=2,
                    )
            except OSError:
                pass

            logger.info(f"Summary CSV: {summary_csv_path}", category=LogCategory.PLAN)
    finally:
        logger.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
