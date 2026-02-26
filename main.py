from __future__ import annotations

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
from src.clients.helpers import extract_authors_from_article, get_article_year, get_current_year, strip_html_tags
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
    crossref_search_multiple,
    dblp_fetch_for_author,
    europepmc_search_papers_multiple,
    openalex_search_multiple,
    openreview_search_papers_multiple,
    pubmed_search_papers_multiple,
    s2_search_papers_multiple,
)
from src.config import (
    CONTRIBUTION_WINDOW_YEARS,
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
    REPOSITORY_AS_JOURNAL,
    REQUEST_DELAY_MAX,
    REQUEST_DELAY_MIN,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    SIM_PREPRINT_TITLE_THRESHOLD,
    SKIP_SCHOLAR_FOR_EXISTING_FILES,
)
from src.doi_utils import process_validated_doi
from src.exceptions import (
    ALL_API_ERRORS,
    FILE_IO_ERRORS,
    FILE_READ_ERRORS,
    FULL_OPERATION_ERRORS,
    PARSE_ERRORS,
)
from src.http_utils import get_api_call_counts, http_get_text, reset_api_call_counts
from src.io_utils import (
    append_summary_to_csv,
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
from src.text_utils import (
    author_name_matches,
    format_author_dirname,
    has_placeholder,
    title_similarity,
    trim_title_default,
)

FORCE_ENRICH = "--force" in sys.argv[1:]


def _is_garbage_title(title: str) -> bool:
    """Detect non-bibliographic titles from Scholar/DBLP artifacts.

    Catches institutional addresses, contact info, and other metadata
    that occasionally appear as "paper titles" in Scholar results.
    """
    if not title:
        return False
    if re.search(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', title):
        return True
    if re.search(r'\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b', title):
        return True
    if re.search(r'^\s*(Department|Faculty|School|Institute)\s+of\b', title, re.IGNORECASE):
        return True
    if re.search(r'\+?\d{1,4}[\.\-]\d{2,4}[\.\-]\d{2,}', title):
        return True
    if re.search(r'\bComplete\s+Volume\b', title, re.IGNORECASE):
        return True
    if re.search(r'^(OASIcs|LIPIcs|LNI|LNCS|Dagstuhl)\b.*\bVolume\s+\d+\b', title, re.IGNORECASE):
        return True
    if (
        re.search(r'\bFestschrift\b', title, re.IGNORECASE)
        and re.search(r',\s+[A-Z][a-z]+,\s+[A-Z][a-z]+\b.*\d{4}', title)
    ):
        return True
    # Proceedings volumes: "Proceedings of the 2023 Conference on X: Tutorial Abstracts"
    if re.match(r'^Proceedings\s+of\s+(the\s+)?\d{4}\s+', title, re.IGNORECASE):
        return True
    # Correction/erratum papers are non-research editorial content
    if re.match(r'^Correction(s)?\s+(to|of)\s*:', title, re.IGNORECASE):
        return True
    # Scholar artifacts: EasyChair preprint stubs, truncated titles
    return bool(re.search(r'\bEasyChair\s+Preprint\b', title, re.IGNORECASE))


def _is_corrupted_title(title: str) -> bool:
    """Detect DBLP-corrupted titles containing author names instead of real titles.

    Matches patterns like "Li2 ()" -- author name + numeric affiliation + empty parens.
    """
    affiliation_fragments = re.findall(r'\b[A-Z][a-z]+\d+\s*\(\)', title)
    return len(affiliation_fragments) >= 2


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
    has_venue = any(
        fields.get(v) and not has_placeholder(str(fields.get(v)))
        for v in ("journal", "booktitle")
    )

    # Determine completeness: check essential fields, venue, DOI, and preprint status
    doi_is_preprint = False
    journal_is_preprint = False

    has_essentials = all(
        fields.get(k) and not has_placeholder(str(fields.get(k)))
        for k in ("title", "author", "year")
    )
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
        has_essentials and has_venue and has_doi
        and not doi_is_preprint and not journal_is_preprint
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
                cand_doi = idu.normalize_doi(
                    (candidate_dict.get("fields") or {}).get("doi", "")
                )
                if cand_doi:
                    seen_dois.add(cand_doi)

            match = bt.bibtex_entries_match_strict(baseline_entry, candidate_dict)
            if match:
                enr_list.append((flag_key, candidate_dict))
                flags[flag_key] = True
                logger.success(
                    "Match validated and added to enrichment",
                    category=LogCategory.MATCH, source=source_name,
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
        art.get("result_id") if citation_id and art.get("result_id") != citation_id else None)
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

    word_count = len(title.split()) if title else 0
    corrupted = _is_corrupted_title(title) if title else False
    garbage = _is_garbage_title(title) if title else False
    title_valid = bool(title) and word_count >= MIN_TITLE_WORDS and not corrupted and not garbage
    logger.debug(
        f"TITLE_VALIDATE | raw={title[:60]} | words={word_count} | corrupted={corrupted} "
        f"| garbage={garbage} | valid={title_valid}",
        category=LogCategory.AUDIT,
    )

    if not title:
        logger.error("Missing required field: title; skipping article", category=LogCategory.SKIP)
        return 0
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

    idx_prefix = f"[{idx}/{total}] " if (isinstance(idx, int) and isinstance(total, int)) else ""
    art_source = (art.get("source") or "scholar").strip()

    logger.substep(f"{idx_prefix}Processing Article", category=LogCategory.ARTICLE)
    logger.info(f"Title: {title}", category=LogCategory.ARTICLE)
    if year_hint:
        logger.info(f"Year: {year_hint}", category=LogCategory.ARTICLE)
    if art_source:
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
        bib_files = sorted(f for f in os.listdir(author_dir) if f.endswith('.bib'))
        logger.debug(
            f"EXISTING_FILE_SCAN | dir={author_dir} | files_checked={len(bib_files)}",
            category=LogCategory.AUDIT,
        )
        for filename in bib_files:
            file_path = os.path.join(author_dir, filename)
            try:
                with open(file_path, encoding='utf-8') as f:
                    existing_bib = f.read()
                existing_entry = bt.parse_bibtex_to_dict(existing_bib)

                # Check if this file matches our article by comparing title
                if existing_entry:
                    existing_title = existing_entry.get('fields', {}).get('title', '')
                    if isinstance(existing_title, list):
                        existing_title = existing_title[0] if existing_title else ''

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
                            category=LogCategory.ARTICLE, source=LogSource.SYSTEM
                        )
                        is_complete = _entry_is_complete(existing_entry)
                        logger.debug(
                            f"EXISTING_FILE_LOADED | file={filename} | complete={is_complete}",
                            category=LogCategory.AUDIT,
                        )
                        break
            except (OSError, ValueError, TypeError):
                continue

    # Fixup stale entries loaded from disk: strip preprint journals and
    # downgrade @article→@misc so cached files from older pipeline runs
    # are corrected even when FILE_SKIP_WRITE blocks the new version.
    if existing_file_loaded and baseline_entry is not None:
        _bl_fields = baseline_entry.get("fields") or {}
        _bl_jnl = (_bl_fields.get("journal") or "").strip().lower()
        _bl_type = baseline_entry.get("type", "")
        _fixup_written = False

        # Strip preprint server names from journal field
        # Use substring match to catch suffixed forms like "arXiv (Cornell University)"
        if _bl_jnl and any(ps == _bl_jnl or ps in _bl_jnl for ps in PREPRINT_SERVERS):
            logger.debug(
                f"EXISTING_FIXUP | preprint_journal_stripped | journal={_bl_fields.get('journal')}",
                category=LogCategory.CLEANUP,
            )
            if _bl_type == "article":
                _bl_fields["howpublished"] = _bl_fields.pop("journal")
                baseline_entry["type"] = "misc"
                logger.debug(
                    "EXISTING_FIXUP | article_preprint_journal->misc",
                    category=LogCategory.CLEANUP,
                )
            else:
                _bl_fields.pop("journal", None)
            _fixup_written = True

        # Strip email addresses from author field
        _bl_author = _bl_fields.get("author", "")
        if isinstance(_bl_author, str) and re.search(r'\S+@\S+\.\S+', _bl_author):
            _bl_author_clean = re.sub(r'\s*\S+@\S+\.\S+', '', _bl_author).strip()
            _bl_author_clean = re.sub(r'\s*and\s*$', '', _bl_author_clean).strip()
            _bl_author_clean = re.sub(r'^\s*and\s*', '', _bl_author_clean).strip()
            if _bl_author_clean:
                logger.debug(
                    f"EXISTING_FIXUP | email_stripped_from_author | old={_bl_author[:60]}",
                    category=LogCategory.CLEANUP,
                )
                _bl_fields["author"] = _bl_author_clean
                _fixup_written = True

        # Strip [J] bracket artifacts from title
        _bl_title = _bl_fields.get("title", "")
        if isinstance(_bl_title, str) and re.search(r'\s*\[J\]\s*$', _bl_title):
            _bl_fields["title"] = re.sub(r'\s*\[J\]\s*$', '', _bl_title).strip()
            logger.debug(
                f"EXISTING_FIXUP | bracket_artifact_stripped | title={_bl_title[:60]}",
                category=LogCategory.CLEANUP,
            )
            _fixup_written = True

        # Fix ALL-CAPS titles
        _bl_title2 = _bl_fields.get("title", "")
        if isinstance(_bl_title2, str) and _bl_title2:
            _fixed_title = trim_title_default(_bl_title2)
            if _fixed_title != _bl_title2:
                logger.debug(
                    f"EXISTING_FIXUP | title_normalized | old={_bl_title2[:60]}",
                    category=LogCategory.CLEANUP,
                )
                _bl_fields["title"] = _fixed_title
                _fixup_written = True

        # Fix conference proceedings misclassified as @article with journal
        if baseline_entry.get("type") == "article" and _bl_fields.get("journal"):
            _conf_jnl = (_bl_fields.get("journal") or "").strip()
            if mu._is_conference_journal(_conf_jnl) and not _bl_fields.get("booktitle"):
                logger.debug(
                    f"EXISTING_FIXUP | conference_as_journal | journal={_conf_jnl[:60]}",
                    category=LogCategory.CLEANUP,
                )
                _bl_fields["booktitle"] = _bl_fields.pop("journal")
                baseline_entry["type"] = "inproceedings"
                _fixup_written = True

        # Reclassify @article with patent number as journal → @misc
        if (baseline_entry.get("type") == "article" and _bl_fields.get("journal")
                and re.match(r'(?i)^US\s+Patent', (_bl_fields.get("journal") or "").strip())):
                logger.debug(
                    f"EXISTING_FIXUP | article_patent->misc | journal={_bl_fields['journal'][:60]}",
                    category=LogCategory.CLEANUP,
                )
                _bl_fields["note"] = _bl_fields.pop("journal")
                baseline_entry["type"] = "misc"
                _fixup_written = True

        # Downgrade @article with repository/portal as journal → @misc
        if baseline_entry.get("type") == "article" and _bl_fields.get("journal"):
            _repo_jnl_bl = (_bl_fields.get("journal") or "").lower()
            if any(rj in _repo_jnl_bl for rj in REPOSITORY_AS_JOURNAL):
                logger.debug(
                    f"EXISTING_FIXUP | article_repository->misc | journal={_bl_fields['journal'][:60]}",
                    category=LogCategory.CLEANUP,
                )
                baseline_entry["type"] = "misc"
                _bl_fields.pop("journal", None)
                _fixup_written = True

        # Reclassify @article with university name as journal → @phdthesis
        if baseline_entry.get("type") == "article" and _bl_fields.get("journal"):
            _thesis_jnl_lower = (_bl_fields.get("journal") or "").lower()
            if "university" in _thesis_jnl_lower or "institut" in _thesis_jnl_lower:
                logger.debug(
                    f"EXISTING_FIXUP | article_thesis->phdthesis | journal={_bl_fields['journal'][:60]}",
                    category=LogCategory.CLEANUP,
                )
                _bl_fields["school"] = _bl_fields.pop("journal")
                baseline_entry["type"] = "phdthesis"
                _fixup_written = True

        # Backfill howpublished for @misc with preprint DOI or arXiv eprint
        if baseline_entry.get("type") == "misc" and not _bl_fields.get("howpublished"):
            _bl_doi_hp = (_bl_fields.get("doi") or "").strip()
            _inferred_hp = mu.infer_howpublished_from_doi(_bl_doi_hp) if _bl_doi_hp else None
            if _inferred_hp:
                _bl_fields["howpublished"] = _inferred_hp
                _fixup_written = True
            elif ((_bl_fields.get("archiveprefix") or "").lower() == "arxiv"):
                _bl_fields["howpublished"] = "arXiv"
                _fixup_written = True

        # Fix author casing (lowercase, ALL-CAPS, capital "And" separators)
        _bl_auth2 = _bl_fields.get("author", "")
        if isinstance(_bl_auth2, str) and _bl_auth2:
            _auth2_fixed, _auth2_changed = mu._fix_author_casing(_bl_auth2)
            if _auth2_changed:
                _bl_fields["author"] = _auth2_fixed
                logger.debug(
                    f"EXISTING_FIXUP | author_casing_fixed | old={_bl_auth2[:60]}",
                    category=LogCategory.CLEANUP,
                )
                _fixup_written = True

        # Normalize howpublished casing
        _bl_hp_before = (_bl_fields.get("howpublished") or "").strip()
        mu._normalize_howpublished(_bl_fields)
        if _bl_fields.get("howpublished", "") != _bl_hp_before and _bl_hp_before:
            logger.debug(
                f"EXISTING_FIXUP | howpublished_casing | {_bl_hp_before}->{_bl_fields['howpublished']}",
                category=LogCategory.CLEANUP,
            )
            _fixup_written = True

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
        if (
            bl_pub in PREPRINT_ONLY_PUBLISHERS
            and bl_jnl
            and not any(ps in bl_jnl for ps in PREPRINT_SERVERS)
        ):
            logger.debug(
                f"EXISTING_FIXUP | publisher_stripped={bl_fields['publisher']} "
                f"| journal={bl_fields.get('journal')}",
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
        authors_list = extract_authors_from_article(art) or []
        year = get_article_year(art)
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
        bl_fields = sorted((baseline_entry.get("fields") or {}).keys())
        logger.debug(
            f"BASELINE_CREATE | source=scholar_minimal | fields=[{', '.join(bl_fields)}]",
            category=LogCategory.AUDIT,
        )
    else:
        bl_fields = sorted((baseline_entry.get("fields") or {}).keys()) if baseline_entry else []
        logger.debug(
            f"BASELINE_CREATE | source=existing_file | fields=[{', '.join(bl_fields)}]",
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
        link_texts: list[str] = [
            str(art[k]) for k in ("link", "link_to_pdf") if art.get(k)
        ]
        for r in (art.get("resources") or []):
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
        bt.build_standard_citekey(baseline_entry, gemini_api_key=gemini_api_key)
        or baseline_entry.get("key")
        or "Entry"
    )
    baseline_entry["key"] = ck

    # Save baseline only if we didn't load from existing file
    if existing_file_loaded:
        path = existing_file_path
        logger.info(f"Using existing file: {path}", category=LogCategory.SKIP)
    else:
        # Defer disk write for bare baselines (no DOI) to avoid creating
        # transient files that get renamed/cleaned during enrichment.
        # The final entry will be written after Phase 4 by save_entry_to_file.
        bl_has_doi = bool((baseline_entry.get("fields") or {}).get("doi", "").strip())
        if not bl_has_doi:
            path = None
            logger.info(
                "Baseline deferred (no DOI; will write after enrichment)",
                category=LogCategory.SKIP, source=LogSource.SYSTEM,
            )
        else:
            path, was_written = mu.save_entry_to_file(
                out_dir, effective_id, baseline_entry,
                gemini_api_key=gemini_api_key, author_name=rec.name,
            )
            if was_written:
                logger.success(f"Saved baseline: {path}", category=LogCategory.SAVE, source=LogSource.SYSTEM)
            else:
                # save_entry_to_file found a duplicate and skipped writing —
                # the article is already on disk under a different name.
                # Skip enrichment entirely to avoid churn.
                logger.info(
                    f"Baseline duplicate detected; skipping enrichment: {path}",
                    category=LogCategory.SKIP, source=LogSource.SYSTEM,
                )
                if summary_csv_path and path:
                    try:
                        rel = os.path.relpath(path)
                    except (OSError, ValueError):
                        rel = path or ""
                    if rel and not is_known_summary_path(rel):
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
    p1_doi = (baseline_entry.get("fields") or {}).get("doi")
    logger.debug(
        f"PHASE1_START | doi={p1_doi} | has_doi={bool(p1_doi)}",
        category=LogCategory.AUDIT,
    )
    try:
        doi_early = idu.normalize_doi((baseline_entry.get("fields") or {}).get("doi"))
        if doi_early:
            logger.info(f"Validating DOI: {doi_early}", category=LogCategory.SEARCH, source=LogSource.DOI)
            doi_matched = process_validated_doi(
                doi_early, baseline_entry, result_id, enr_list, flags
            )

            # If DOI failed validation, stash it for Phase 3 and remove from baseline
            if not doi_matched:
                unvalidated_doi = doi_early
                baseline_entry.get("fields", {}).pop("doi", None)
                logger.warn(
                    "DOI validation failed, removed from baseline (will retry in Phase 3)",
                    category=LogCategory.ARTICLE, source=LogSource.DOI,
                )
            else:
                doi_validated = True
                logger.success("DOI validated successfully", category=LogCategory.MATCH, source=LogSource.DOI)
            logger.debug(
                f"PHASE1_RESULT | doi={doi_early} | validated={doi_validated} "
                f"| stashed={unvalidated_doi is not None}",
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
                                category=LogCategory.MATCH, source=LogSource.SCHOLAR,
                            )
                        else:
                            logger.info(
                                "Citation did not match baseline",
                                category=LogCategory.SKIP, source=LogSource.SCHOLAR,
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
                matched, s2_paper = _try_multiple_candidates(
                    LogSource.S2,
                    s2_papers,
                    build_bibtex_from_s2,
                    baseline_entry,
                    result_id,
                    enr_list,
                    flags,
                    "s2",
                    max_candidates=5,
                    seen_dois=all_candidate_dois
                )
                if not matched:
                    s2_paper = None
                elif s2_paper:
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
            matched, cr_item = _try_multiple_candidates(
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
            if not matched:
                cr_item = None
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
            matched, arxiv_entry = _try_multiple_candidates(
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
            if not matched:
                arxiv_entry = None
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.ARXIV)

    logger.debug(f"SEARCH_START | source=OpenAlex | title={title[:60]}", category=LogCategory.AUDIT)
    oa_work = None
    try:
        oa_works = openalex_search_multiple(title, rec.name, max_results=5)
        if oa_works:
            matched, oa_work = _try_multiple_candidates(
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
            if not matched:
                oa_work = None
            elif oa_work:
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
            matched, pm_article = _try_multiple_candidates(
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
            if not matched:
                pm_article = None
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.PUBMED)
    logger.debug(f"SEARCH_START | source=EuropePMC | title={title[:60]}", category=LogCategory.AUDIT)
    epmc_article = None
    try:
        epmc_articles = europepmc_search_papers_multiple(title, rec.name, max_results=5)
        if epmc_articles:
            matched, epmc_article = _try_multiple_candidates(
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
            if not matched:
                epmc_article = None
    except ALL_API_ERRORS as e:
        logger.warn(f"API error - {e}", category=LogCategory.ERROR, source=LogSource.EUROPEPMC)

    # ===== PHASE 3: Late DOI Discovery =====
    logger.info("▶ Phase 3: Late DOI Discovery", category=LogCategory.ARTICLE)
    # Do late DOI negotiation if we haven't validated a DOI, or if the validated
    # DOI is a preprint (we may find a published DOI to upgrade to)
    baseline_doi = idu.normalize_doi((baseline_entry.get("fields") or {}).get("doi"))
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
            category=LogCategory.SEARCH, source=LogSource.DOI,
        )
        try:
            doi_candidates: list[str] = []

            def _add_doi(source: str, doi: str | None) -> None:
                """Append a DOI candidate and log it for audit."""
                if doi:
                    doi_candidates.append(str(doi))
                    logger.debug(
                        f"DOI_CANDIDATE | source={source} | doi={doi} "
                        f"| is_secondary={idu.is_secondary_doi(str(doi))}",
                        category=LogCategory.AUDIT,
                    )

            # Include stashed unvalidated DOI from Phase 1 for retry
            _add_doi("phase1_stash", unvalidated_doi)

            # Infer arXiv DOIs from eprint fields or URLs in baseline and enrichers
            # (deterministic — no HTTP required)
            _bl_eprint = idu.extract_arxiv_eprint(baseline_entry)
            if _bl_eprint:
                _add_doi("baseline_eprint", f"10.48550/arxiv.{_bl_eprint}")
            _arxiv_url_re = re.compile(r'arxiv\.org/abs/(\d{4}\.\d{4,5})', re.IGNORECASE)
            _bl_url = (baseline_entry.get("fields") or {}).get("url", "")
            _bl_url_m = _arxiv_url_re.search(str(_bl_url))
            if _bl_url_m:
                _add_doi("baseline_url", f"10.48550/arxiv.{_bl_url_m.group(1)}")
            for _enr_src, _enr_data in enr_list:
                _eprint = idu.extract_arxiv_eprint(_enr_data)
                if _eprint:
                    _add_doi(f"eprint_{_enr_src}", f"10.48550/arxiv.{_eprint}")
                else:
                    # Check enricher's URL field for arXiv abstract links
                    _enr_url = (_enr_data.get("fields") or {}).get("url", "")
                    _m = _arxiv_url_re.search(str(_enr_url))
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
                    numeric_id = str(pmcid).removeprefix("PMC")
                    url_candidates.append(f"https://europepmc.org/article/PMC/{numeric_id}")

            # Deterministic DOI extraction from known URL patterns
            # (no HTTP required — prevents network non-determinism)
            _arxiv_abs_re = re.compile(r'arxiv\.org/abs/(\d{4}\.\d{4,5})', re.IGNORECASE)
            for u in filter(None, url_candidates):
                m = _arxiv_abs_re.search(str(u))
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
                    category=LogCategory.SEARCH, source=LogSource.DOI,
                )
                doi_matched = False

                # Try each DOI candidate until we find one that validates
                for doi_idx, doi_candidate in enumerate(doi_candidates, 1):
                    logger.info(
                        f"Validating DOI candidate: {doi_candidate}",
                        category=LogCategory.SEARCH, source=LogSource.DOI,
                    )
                    # When a DOI was inferred from an enricher's arXiv eprint,
                    # temporarily inject it into the baseline so DOI_EXACT match
                    # fires in validation (the eprint already confirmed identity;
                    # the CSL title may differ from the preprint title).
                    _bl_fields_p3 = baseline_entry.get("fields") or {}
                    _bl_doi_before = _bl_fields_p3.get("doi")
                    _doi_norm = idu.normalize_doi(doi_candidate)
                    _is_eprint_doi = _doi_norm and any(
                        idu.normalize_doi(f"10.48550/arxiv.{idu.extract_arxiv_eprint(ed) or ''}") == _doi_norm
                        for _, ed in enr_list
                        if idu.extract_arxiv_eprint(ed)
                    )
                    if _is_eprint_doi and not _bl_doi_before:
                        _bl_fields_p3["doi"] = doi_candidate
                    candidate_matched = process_validated_doi(
                        doi_candidate, baseline_entry, result_id, enr_list, flags
                    )
                    # Restore baseline DOI to avoid polluting later logic
                    if _is_eprint_doi and not _bl_doi_before:
                        _bl_fields_p3.pop("doi", None)
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
                        category=LogCategory.SKIP, source=LogSource.DOI,
                    )
            else:
                logger.info("No DOI discovered; skipped", category=LogCategory.SKIP, source=LogSource.DOI)
        except ALL_API_ERRORS as e:
            logger.warn(f"DOI negotiation error: {e}", category=LogCategory.ERROR, source=LogSource.DOI)
    else:
        logger.info(
            "DOI already validated early; skipping late DOI negotiation",
            category=LogCategory.SKIP, source=LogSource.DOI,
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

        # Downgrade @article to @misc when journal is missing (by Phase 4 all
        # enrichment is done, so a missing journal means no source could provide one)
        merged_fields = merged.get("fields") or {}
        if merged.get("type") == "article" and not merged_fields.get("journal"):
            logger.debug(
                "TYPE_CORRECT | article_no_journal->misc",
                category=LogCategory.AUDIT,
            )
            merged["type"] = "misc"

        # Downgrade @inproceedings without booktitle -> @misc (same rationale:
        # by Phase 4 enrichment is complete; @inproceedings without booktitle
        # is invalid BibTeX)
        if merged.get("type") == "inproceedings" and not merged_fields.get("booktitle"):
            logger.debug(
                "TYPE_CORRECT | inproceedings_no_booktitle->misc",
                category=LogCategory.AUDIT,
            )
            merged["type"] = "misc"

        # Downgrade @article with preprint server as journal -> @misc
        # Use substring match to catch suffixed forms like "arXiv (Cornell University)"
        if merged.get("type") == "article":
            j_lower = (merged_fields.get("journal") or "").lower().strip()
            if j_lower and any(ps == j_lower or ps in j_lower for ps in PREPRINT_SERVERS):
                logger.debug(
                    f"TYPE_CORRECT | article_preprint_journal->misc | journal={j_lower}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "misc"
                merged_fields["howpublished"] = merged_fields.pop("journal")

        # Reclassify @article with conference proceedings as journal -> @inproceedings
        if merged.get("type") == "article" and merged_fields.get("journal"):
            _p4_jnl = (merged_fields.get("journal") or "").strip()
            if mu._is_conference_journal(_p4_jnl) and not merged_fields.get("booktitle"):
                logger.debug(
                    f"TYPE_CORRECT | article_conference_journal->inproceedings | journal={_p4_jnl[:60]}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "inproceedings"
                merged_fields["booktitle"] = merged_fields.pop("journal")

        # Reclassify @article with patent number as journal → @misc
        if merged.get("type") == "article" and merged_fields.get("journal"):
            _patent_jnl = (merged_fields.get("journal") or "").strip()
            if re.match(r'(?i)^US\s+Patent', _patent_jnl):
                logger.debug(
                    f"TYPE_CORRECT | article_patent->misc | journal={_patent_jnl[:60]}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "misc"
                merged_fields["note"] = _patent_jnl
                merged_fields.pop("journal", None)

        # Reclassify @article with "Unpublished" journal → @misc
        if (merged.get("type") == "article" and merged_fields.get("journal")
                and (merged_fields.get("journal") or "").strip().lower() == "unpublished"):
            logger.debug("TYPE_CORRECT | article_unpublished->misc", category=LogCategory.AUDIT)
            merged["type"] = "misc"
            merged_fields.pop("journal", None)

        # PNAS is a journal despite "Proceedings" in its name
        if merged.get("type") == "inproceedings" and merged_fields.get("booktitle"):
            _bt_pnas = (merged_fields.get("booktitle") or "").lower()
            if "proceedings of the national academy" in _bt_pnas:
                logger.debug(
                    "TYPE_CORRECT | inproceedings_pnas->article | booktitle→journal",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "article"
                merged_fields["journal"] = merged_fields.pop("booktitle")

        # PVLDB is a journal despite "Proceedings" in its name
        if merged.get("type") == "inproceedings" and merged_fields.get("booktitle"):
            _bt_pvldb = (merged_fields.get("booktitle") or "").lower()
            if "proceedings of the vldb" in _bt_pvldb:
                logger.debug(
                    "TYPE_CORRECT | inproceedings_pvldb->article",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "article"
                merged_fields["journal"] = merged_fields.pop("booktitle")

        # Strip URL fragments from booktitle (e.g., "proceedings.mlr.press")
        if merged.get("type") == "inproceedings" and merged_fields.get("booktitle"):
            _bt_val = (merged_fields.get("booktitle") or "").strip()
            if re.match(r'^https?://|^[\w.-]+\.(com|org|net|io|press)\b', _bt_val, re.IGNORECASE):
                logger.debug(
                    f"TYPE_CORRECT | inproceedings_url_booktitle->misc | booktitle={_bt_val[:60]}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "misc"
                merged_fields.pop("booktitle", None)

        # Downgrade @inproceedings with "Preprint" as booktitle → @misc
        if (merged.get("type") == "inproceedings" and merged_fields.get("booktitle")
                and (merged_fields.get("booktitle") or "").strip().lower() == "preprint"):
            logger.debug("TYPE_CORRECT | inproceedings_preprint->misc", category=LogCategory.AUDIT)
            merged["type"] = "misc"
            merged_fields.pop("booktitle", None)

        # Downgrade @article with repository/portal as journal → @misc
        if merged.get("type") == "article" and merged_fields.get("journal"):
            _repo_jnl = (merged_fields.get("journal") or "").lower()
            if any(rj in _repo_jnl for rj in REPOSITORY_AS_JOURNAL):
                logger.debug(
                    f"TYPE_CORRECT | article_repository->misc | journal={merged_fields['journal'][:60]}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "misc"
                merged_fields.pop("journal", None)

        # Downgrade @inproceedings with repository as booktitle → @misc
        if merged.get("type") == "inproceedings" and merged_fields.get("booktitle"):
            _repo_bt = (merged_fields.get("booktitle") or "").lower()
            if any(rj in _repo_bt for rj in REPOSITORY_AS_JOURNAL):
                logger.debug(
                    f"TYPE_CORRECT | inproceedings_repository->misc | booktitle={merged_fields['booktitle'][:60]}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "misc"
                merged_fields.pop("booktitle", None)

        # Reclassify @article with university name as journal → @phdthesis
        # (Crossref sometimes returns thesis DOIs with the university as journal)
        if merged.get("type") == "article" and merged_fields.get("journal"):
            _thesis_jnl = (merged_fields.get("journal") or "").lower()
            if "university" in _thesis_jnl or "institut" in _thesis_jnl:
                logger.debug(
                    f"TYPE_CORRECT | article_thesis->phdthesis | journal={merged_fields['journal'][:60]}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "phdthesis"
                merged_fields["school"] = merged_fields.pop("journal")

        # Downgrade @article with preprint-only DOI -> @misc
        if merged.get("type") == "article":
            _merged_doi = (merged_fields.get("doi") or "").strip()
            if _merged_doi and idu.is_secondary_doi(_merged_doi):
                venue = merged_fields.get("journal", "")
                logger.debug(
                    f"TYPE_CORRECT | article_preprint_doi->misc | doi={_merged_doi} | venue={venue}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "misc"
                if venue:
                    merged_fields["howpublished"] = merged_fields.pop("journal")

        # Backfill howpublished for @misc entries with preprint DOI or arXiv eprint
        if merged.get("type") == "misc" and not merged_fields.get("howpublished"):
            _misc_doi = (merged_fields.get("doi") or "").strip()
            _inferred_hp = mu.infer_howpublished_from_doi(_misc_doi) if _misc_doi else None
            if _inferred_hp:
                merged_fields["howpublished"] = _inferred_hp
            elif (merged_fields.get("archiveprefix") or "").lower() == "arxiv":
                merged_fields["howpublished"] = "arXiv"

        # Fix ALL-CAPS titles and strip [J] artifacts from enrichment sources
        _p4_title = merged_fields.get("title", "")
        if isinstance(_p4_title, str) and _p4_title:
            _p4_fixed = trim_title_default(_p4_title)
            # Strip [J] bracket artifact (citation format leak from Scholar)
            _p4_fixed = re.sub(r'\s*\[J\]\s*$', '', _p4_fixed).strip()
            if _p4_fixed != _p4_title:
                merged_fields["title"] = _p4_fixed

        # Fix author casing + capital "And" separators from API sources
        _p4_auth = merged_fields.get("author", "")
        if isinstance(_p4_auth, str) and _p4_auth:
            _p4_auth_fixed, _ = mu._fix_author_casing(_p4_auth)
            if _p4_auth_fixed != _p4_auth:
                merged_fields["author"] = _p4_auth_fixed

        # Normalize howpublished casing after all journal→howpublished moves
        mu._normalize_howpublished(merged_fields)

        # Upgrade @misc with conference/workshop howpublished → @inproceedings
        # When howpublished is a venue name (not a preprint server), the entry
        # is a conference/workshop paper that should be @inproceedings.
        if merged.get("type") == "misc" and merged_fields.get("howpublished"):
            _hp_val = (merged_fields.get("howpublished") or "").strip()
            _hp_lower = _hp_val.lower()
            _is_preprint_hp = any(ps == _hp_lower or ps in _hp_lower for ps in PREPRINT_SERVERS) or _hp_lower in (
                "arxiv", "biorxiv", "medrxiv", "chemrxiv", "techrxiv",
                "ssrn", "ssrn electronic journal", "research square",
                "preprints.org", "authorea", "osf preprints", "openrxiv",
                "psyarxiv", "socarxiv", "edarxiv",
            )
            if not _is_preprint_hp and _hp_val:
                logger.debug(
                    f"TYPE_CORRECT | misc_workshop->inproceedings | howpublished={_hp_val}",
                    category=LogCategory.AUDIT,
                )
                merged["type"] = "inproceedings"
                merged_fields["booktitle"] = merged_fields.pop("howpublished")

        # Annotate bare stubs: no enrichers, no DOI, no venue
        is_bare_stub = (
            not enr_list
            and not (merged_fields.get("doi") or "").strip()
            and not (merged_fields.get("journal") or "").strip()
            and not (merged_fields.get("booktitle") or "").strip()
        )
        if is_bare_stub:
            merged_fields["note"] = "Unenriched: no enrichment sources matched"
            logger.warn(
                "Bare stub: no venue, no DOI, no enrichment; annotated with note",
                category=LogCategory.AUDIT,
            )

        # Skip entries with type "book" (proceedings volumes, edited books — not individual papers)
        if merged.get("type") == "book":
            has_file = bool(path and os.path.isfile(path))
            logger.debug(
                f"BOOK_SKIP | type=book | file_deleted={has_file}",
                category=LogCategory.AUDIT,
            )
            logger.warn(
                "Entry is a book/proceedings volume, not an individual paper; skipping",
                category=LogCategory.SKIP, source=LogSource.SYSTEM,
            )
            if has_file and path:
                os.remove(path)
            return 0

        # Verify target author appears in the paper's author list to catch
        # Scholar profile contamination (e.g., different person with same surname)
        merged_authors = (merged.get("fields") or {}).get("author", "")
        author_found = not merged_authors or author_name_matches(rec.name, merged_authors)
        logger.debug(
            f"AUTHOR_FILTER | target={rec.name} | paper_authors={str(merged_authors)[:80]} "
            f"| found={author_found}",
            category=LogCategory.AUDIT,
        )
        if merged_authors and not author_found:
            logger.warn(
                f"Target author '{rec.name}' not found in paper authors; skipping",
                category=LogCategory.SKIP, source=LogSource.SYSTEM,
            )
            if path and os.path.isfile(path):
                os.remove(path)
            return 0

        # Deterministic dedup: check if any DOI (from Phase 2 candidates, Phase 3
        # discovery, or the merged entry itself) already exists in a DIFFERENT file
        # on disk.  This prevents oscillation where a preprint/published pair creates
        # a file under the preprint title that gets enriched and renamed every run.
        merged_doi = idu.normalize_doi(merged_fields.get("doi", ""))
        # Also infer DOI from merged eprint field (merge_with_policy may have added it)
        _merged_eprint = idu.extract_arxiv_eprint(merged)
        if _merged_eprint:
            all_candidate_dois.add(idu.normalize_doi(f"10.48550/arxiv.{_merged_eprint}") or "")
        if merged_doi:
            all_candidate_dois.add(merged_doi)
        # Exclude the DOI of the prefer_path file itself to avoid self-matching
        _prefer_doi = ""
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as _pf:
                    _pd = bt.parse_bibtex_to_dict(_pf.read())
                _prefer_doi = idu.normalize_doi((_pd or {}).get("fields", {}).get("doi", "")) or ""
            except (OSError, UnicodeDecodeError):
                pass
        check_dois = all_candidate_dois - {_prefer_doi} if _prefer_doi else all_candidate_dois
        if check_dois:
            for existing_bib in os.listdir(author_dir):
                if not existing_bib.endswith(".bib"):
                    continue
                epath = os.path.join(author_dir, existing_bib)
                if path and os.path.abspath(epath) == os.path.abspath(path):
                    continue  # skip self
                try:
                    with open(epath, encoding="utf-8") as ef:
                        edict = bt.parse_bibtex_to_dict(ef.read())
                    if not edict:
                        continue
                    edoi = idu.normalize_doi((edict.get("fields") or {}).get("doi", ""))
                    if edoi and edoi in check_dois:
                        # Guard: verify the DOI match is genuine by comparing
                        # titles.  Phase-2 candidate DOIs can be false matches
                        # (API returned the wrong DOI for the query title).
                        e_title = (edict.get("fields") or {}).get("title", "")
                        m_title = merged_fields.get("title", "")
                        if e_title and m_title:
                            _doi_sim = title_similarity(e_title, m_title)
                            if _doi_sim < SIM_PREPRINT_TITLE_THRESHOLD:
                                logger.debug(
                                    f"CANDIDATE_DOI_DEDUP_REJECTED | doi={edoi}"
                                    f" | existing={existing_bib}"
                                    f" | sim={_doi_sim:.3f} | titles_differ",
                                    category=LogCategory.DEDUP,
                                )
                                continue
                        logger.debug(
                            f"CANDIDATE_DOI_DEDUP | doi={edoi} | existing={existing_bib} "
                            f"| skipping_write=True",
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

        merged["key"] = (
            bt.build_standard_citekey(merged, gemini_api_key=gemini_api_key)
            or merged.get("key")
            or "Entry"
        )
        path2, was_written = mu.save_entry_to_file(out_dir, effective_id, merged, prefer_path=path,
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
                f"DOI: {' + '.join(doi_methods)}", category=LogCategory.SAVE, source=LogSource.DOI,
            )

        # Count and log enrichment sources
        enriched_count = sum(1 for k in enrichment_sources if flags.get(k))
        total_sources = len(enrichment_sources)

        # Group matched and unmatched sources
        matched_sources: list[str] = []
        unmatched: list[str] = []
        for flag_key, source_label in enrichment_sources.items():
            if flags.get(flag_key):
                matched_sources.append(source_label)
            else:
                unmatched.append(source_label)

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
            category=LogCategory.SAVE, source=LogSource.SYSTEM,
        )

        if matched_sources:
            logger.success(f"Matched: {', '.join(matched_sources)}", category=LogCategory.SAVE, source=LogSource.SYSTEM)
        if unmatched:
            logger.info(f"Not matched: {', '.join(unmatched)}", category=LogCategory.SKIP, source=LogSource.SYSTEM)

        if summary_csv_path and was_written:
            append_summary_to_csv(
                summary_csv_path,
                rel,
                total_true,
                flags
            )
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
            category=LogCategory.AUTHOR, source=LogSource.SYSTEM,
        )

        current_year = get_current_year()
        min_year = current_year - (CONTRIBUTION_WINDOW_YEARS - 1)

        scholar_windowed = []
        if rec.scholar_id:
            logger.info("Request author publications", category=LogCategory.FETCH, source=LogSource.SCHOLAR)

            scholar_articles: list[dict[str, Any]] = []
            max_fetch_retries = 3

            # SerpAPI call — pagination handled internally by serpapi_scholar
            data: dict[str, Any] = {}
            for attempt in range(1, max_fetch_retries + 1):
                data = fetch_author_publications(
                    serpapi_key, rec.scholar_id, rec.name, num=MAX_PUBLICATIONS_PER_AUTHOR,
                )
                if data.get("articles"):
                    break  # Got articles -- valid response
                if attempt < max_fetch_retries:
                    logger.warn(
                        f"Scholar API returned empty (attempt {attempt}/{max_fetch_retries}), retrying...",
                        category=LogCategory.FETCH, source=LogSource.SCHOLAR,
                    )
                    time.sleep(2.0 * attempt)

            if not data.get("articles"):
                logger.warn(
                    f"Scholar API failed after {max_fetch_retries} attempts; continuing with DBLP only",
                    category=LogCategory.ERROR, source=LogSource.SCHOLAR,
                )
            else:
                status = (data.get("search_metadata") or {}).get("status")
                if status and status.lower() == "error":
                    err = data.get("error") or "Unknown error"
                    raise RuntimeError(f"CiteForge error for author {rec.scholar_id}: {err}")

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
                            a["title"] = trim_title_default(strip_html_tags(a.get("title") or ""))
                    except (TypeError, AttributeError):
                        pass
                logger.info(
                    f"{len(scholar_articles)} article(s) fetched",
                    category=LogCategory.FETCH, source=LogSource.SCHOLAR,
                )

            scholar_windowed = [a for a in scholar_articles if (get_article_year(a) or 0) >= min_year]
            logger.debug(
                f"YEAR_WINDOW | total={len(scholar_articles)} | windowed={len(scholar_windowed)} | min_year={min_year}",
                category=LogCategory.AUDIT,
            )
            logger.info(
                f"{len(scholar_windowed)}/{len(scholar_articles)} within "
                f"{CONTRIBUTION_WINDOW_YEARS}y window (>= {min_year})",
                category=LogCategory.FETCH,
                source=LogSource.SCHOLAR
            )
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.SCHOLAR)

        dblp_items = []
        if rec.dblp:
            try:
                dblp_items = dblp_fetch_for_author(rec.name, rec.dblp, min_year)
                logger.info(
                    f"{len(dblp_items)} item(s) fetched within window",
                    category=LogCategory.FETCH, source=LogSource.DBLP,
                )
            except FULL_OPERATION_ERRORS as e:
                logger.warn(f"Fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DBLP)
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.DBLP)

        if not scholar_windowed and not dblp_items:
            logger.info(f"No articles within last {CONTRIBUTION_WINDOW_YEARS} years", category=LogCategory.SKIP)
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
            category=LogCategory.PLAN
        )

        articles_sorted = sort_articles_by_year_current_first(merged_list)
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
                saved += process_article(
                    rec, art, serply_key, out_dir, s2_api_key, or_creds,
                    idx=idx + 1, total=total_entries,
                    gemini_api_key=gemini_api_key, summary_csv_path=summary_csv_path,
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

    if not os.path.exists(author_dir):
        return 0

    try:
        return sum(1 for f in os.listdir(author_dir) if f.endswith('.bib'))
    except OSError:
        return 0


def _load_csv_titles(csv_path: str) -> dict[str, list[str]]:
    """Load titles from CSV-tracked .bib files, grouped by author directory."""
    import csv as _csv

    result: dict[str, list[str]] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in _csv.DictReader(f):
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

    records_sorted = sorted(records, key=lambda r: (1 if _has_output(r) else 0, records.index(r)))

    logger.step(f"Starting parallel execution with {MAX_WORKERS} workers", category=LogCategory.PLAN)

    # Install thread exception hook to log uncaught exceptions in worker threads
    _orig_excepthook = threading.excepthook

    def _thread_excepthook(args: Any) -> None:
        logger.error(
            f"Thread '{args.thread.name if args.thread else '?'}' died: "
            f"{args.exc_type.__name__}: {args.exc_value}",
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
                summary_csv_path=summary_csv_path
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
                        f"[{processed}/{len(records)}] Error processing {rec.name} "
                        f"({rec.scholar_id or rec.dblp}): {e}",
                        category=LogCategory.ERROR,
                    )
        except TimeoutError:
            remaining = [r.name for f, r in future_to_author.items() if not f.done()]
            logger.error(
                f"Pipeline timed out with {len(remaining)} author(s) still pending: "
                + ", ".join(remaining[:5]),
                category=LogCategory.ERROR,
            )

    try:
        counts = get_api_call_counts()
        logger.step("Run complete", category=LogCategory.PLAN)
        logger.info(f"Records processed: {processed}", category=LogCategory.PLAN)
        logger.info(f"BibTeX files saved: {total_saved}", category=LogCategory.PLAN)
        if counts:
            logger.info(f"API calls: {counts}", category=LogCategory.PLAN)
            logger.info(f"Total API calls: {sum(counts.values())}", category=LogCategory.PLAN)
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
                    is_dup = any(
                        title_similarity(orphan_title, t) >= SIM_MERGE_DUPLICATE_THRESHOLD
                        for t in tracked_titles
                    ) if orphan_title else False

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

            # Write per-author baseline counts
            baseline: dict[str, int] = {}
            for entry in sorted(os.listdir(out_dir)):
                d = os.path.join(out_dir, entry)
                if os.path.isdir(d):
                    baseline[entry] = len([f for f in os.listdir(d) if f.endswith(".bib")])
            baseline_path = os.path.join(out_dir, "baseline.json")
            try:
                with open(baseline_path, "w", encoding="utf-8") as bf:
                    json.dump({"total": sum(baseline.values()), "authors": baseline}, bf, indent=2)
            except OSError:
                pass

            logger.info(f"Summary CSV: {summary_csv_path}", category=LogCategory.PLAN)
    finally:
        logger.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
