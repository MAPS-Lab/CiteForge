from __future__ import annotations

import contextlib
import html
import os
import re
from typing import Any

from .bibtex_build import determine_entry_type, get_container_field
from .bibtex_utils import bibtex_from_dict, parse_bibtex_to_dict, short_filename_for_entry
from .config import (
    ABBREVIATED_VENUE_MAP,
    CONFERENCE_AS_JOURNAL,
    DEDUP_INTERNAL_FIELDS,
    GENERIC_SERIES_NAMES,
    JOURNAL_ONLY_PREFIXES,
    JOURNALS_NAMED_PROCEEDINGS,
    PAGES_MAX_DIGITS,
    PREPRINT_DOI_PREFIXES,
    PREPRINT_ONLY_PUBLISHERS,
    PREPRINT_SERVERS,
    PUBLISHER_CORRECTIONS,
    SIM_DEDUP_COMPOSITE_THRESHOLD,
    SIM_FILE_DUPLICATE_THRESHOLD,
    SIM_PREPRINT_TITLE_THRESHOLD,
    TITLE_LENGTH_KEEP_RATIO,
    TRUST_DIFF_OVERRIDE_THRESHOLD,
    TRUST_ORDER,
)
from .id_utils import (
    _norm_doi,
    allowlisted_url,
    doi_bases_match,
    external_ids_match,
    extract_arxiv_eprint,
    is_secondary_doi,
    normalize_arxiv_metadata,
)
from .log_utils import LogCategory, logger
from .text_utils import (
    author_overlap_ratio,
    authors_overlap,
    compute_dedup_score,
    format_author_dirname,
    has_placeholder,
    normalize_title,
    parse_authors_any,
    title_is_truncated_match,
    title_similarity,
)

_DAGSTUHL_DOI_RE = re.compile(
    r"^10\.4230/(lipics|oasics)\.([a-z0-9]+)\.(\d+)(?:\.\d+)?$",
    re.IGNORECASE,
)

_AUTHOR_DIGIT_SUFFIX_RE = re.compile(r"\s+\d{1,4}\s*$")
_AUTHOR_PAREN_SUFFIX_RE = re.compile(r"\s*\(\d{1,4}\)\s*$")
_AUTHOR_GLUED_DIGIT_RE = re.compile(r"(?<=[A-Za-z]{2})\d{1,4}$")
_AUTHOR_INITIAL_RE = re.compile(r"^[A-Z]\.$")

_JOURNAL_URL_MAP: dict[str, str] = {
    "techrxiv.org": "TechRxiv",
    "ssrn.com": "SSRN Electronic Journal",
}

# Canonical casing for howpublished preprint server names.
# Used by merge_with_policy, save_entry_to_file fixup, and Phase 4 post-merge.
_OSF_PREPRINTS = "OSF Preprints"

_HOWPUB_CANONICAL: dict[str, str] = {
    "arxiv": "arXiv",
    "biorxiv": "bioRxiv",
    "medrxiv": "medRxiv",
    "chemrxiv": "ChemRxiv",
    "techrxiv": "TechRxiv",
    "research square": "Research Square",
    "ssrn": "SSRN",
    "osf preprints": _OSF_PREPRINTS,
    "preprints.org": "Preprints.org",
    "openrxiv": "openRxiv",
}

# Map preprint DOI prefixes → canonical howpublished value.
# Used to backfill howpublished on @misc entries with preprint DOIs.
_DOI_PREFIX_TO_HOWPUB: tuple[tuple[str, str], ...] = (
    ("10.48550/arxiv", "arXiv"),
    ("10.1101/", "bioRxiv"),
    ("10.21203/rs.", "Research Square"),
    ("10.31234/osf.io", _OSF_PREPRINTS),
    ("10.31219/osf.io", _OSF_PREPRINTS),
    ("10.26434/chemrxiv", "ChemRxiv"),
    ("10.20944/preprints", "Preprints.org"),
    ("10.2139/ssrn", "SSRN"),
    ("10.64898/", "openRxiv"),
    ("10.36227/techrxiv", "TechRxiv"),
    ("10.33774/", "Preprint"),
    ("10.5194/egusphere", "EGU"),
    ("10.2172/", "OSTI"),
    ("10.31220/agrirxiv", "agriRxiv"),
    ("10.32388/", "Qeios"),
    ("10.48448/", "Underline Science"),
    ("10.32920/", "Institutional Repository"),
    ("10.5281/zenodo", "Zenodo"),
)


def infer_howpublished_from_doi(doi: str) -> str | None:
    """Return canonical howpublished for a preprint DOI, or None."""
    dl = doi.lower()
    for prefix, name in _DOI_PREFIX_TO_HOWPUB:
        if dl.startswith(prefix):
            return name
    return None


def _matches_journal_named_proceedings(text_lower: str) -> bool:
    """Word-boundary match against JOURNALS_NAMED_PROCEEDINGS.

    Avoids false positives like "proceedings of the ieee/cvf winter
    conference" matching "proceedings of the ieee" (the journal).
    """
    for jnp in JOURNALS_NAMED_PROCEEDINGS:
        idx = text_lower.find(jnp)
        if idx == -1:
            continue
        end = idx + len(jnp)
        if end >= len(text_lower) or text_lower[end] in (" ", ",", ".", ";", ":"):
            return True
    return False


def _is_conference_journal(journal: str) -> bool:
    """Check if a journal name is actually a conference proceedings venue.

    Detects "Proceedings of ...", "Conference on ...", "Tagungsband" (German
    proceedings), "@" patterns (e.g. IberLEF@SEPLN), and entries in
    CONFERENCE_AS_JOURNAL.  Excludes journals whose names happen to contain
    "Proceedings" (e.g. PNAS, PVLDB, Proc. IEEE).
    """
    lower = journal.lower()
    # Exclude real journals that happen to contain "Proceedings"
    if _matches_journal_named_proceedings(lower):
        return False
    return (
        "proceedings" in lower
        or "tagungsband" in lower
        or lower.startswith("conference on")
        or "@" in journal
        or lower in CONFERENCE_AS_JOURNAL
    )


def _normalize_howpublished(fields: dict[str, Any]) -> None:
    """Normalize howpublished casing for known preprint servers in-place."""
    hp = (fields.get("howpublished") or "").strip()
    if hp:
        hp_key = hp.lower().split("(")[0].strip()
        if hp_key in _HOWPUB_CANONICAL:
            fields["howpublished"] = _HOWPUB_CANONICAL[hp_key]


_AUTHOR_SEPARATOR = " and "


def _fix_author_casing(author_val: str) -> tuple[str, bool]:
    """Fix author name casing: capitalize all-lowercase tokens, convert
    ALL-CAPS tokens (>2 chars) to title case, fix 2-char ALL-CAPS surnames
    when preceded by a mixed-case given name, and fix capital 'And' separators.

    Returns (fixed_string, was_modified).
    """
    # Fix capital "And" separator first (e.g. "and And Duncan" → "and Duncan")
    val = re.sub(r"\band\s+And\b", "and", author_val)
    val = val.replace(" And ", _AUTHOR_SEPARATOR)
    and_was_fixed = val != author_val
    parts = [p.strip() for p in val.split(_AUTHOR_SEPARATOR)]
    fixed_parts: list[str] = []
    any_fixed = False
    for ap in parts:
        tokens = ap.split()
        if len(tokens) >= 2:
            new_tokens: list[str] = []
            has_mixed_case = any(len(t) > 1 and not t.isupper() and t[0].isupper() for t in tokens if t.isalpha())
            for t in tokens:
                if not t or not t[0].isalpha():
                    new_tokens.append(t)
                elif (
                    # 3+ letter ALL-CAPS → always fix (e.g. "SMITH" → "Smith")
                    (len(t) > 2 and t.isupper())
                    # 2-letter ALL-CAPS as LAST token with mixed-case sibling → surname
                    # e.g. "Shu FU" → "Shu Fu", but "JI Munro" keeps "JI" as initials
                    or (len(t) == 2 and t.isupper() and has_mixed_case and t == tokens[-1])
                    # Lowercase start → capitalize
                    or (len(t) > 1 and t[0].islower())
                ):
                    new_tokens.append(t.capitalize())
                    any_fixed = True
                else:
                    new_tokens.append(t)
            fixed_parts.append(" ".join(new_tokens))
        else:
            fixed_parts.append(ap)
    return _AUTHOR_SEPARATOR.join(fixed_parts), any_fixed or and_was_fixed


def _sanitize_author_digits(name: str) -> str:
    """Strip trailing digit suffixes from author names (e.g., 'Das1' -> 'Das')."""
    if not name:
        return name
    s = _AUTHOR_PAREN_SUFFIX_RE.sub("", name.strip())
    s = _AUTHOR_DIGIT_SUFFIX_RE.sub("", s)
    s = _AUTHOR_GLUED_DIGIT_RE.sub("", s)
    return s.strip()


def _is_preprint_doi(doi: str) -> bool:
    """Check if a DOI belongs to a preprint server (arXiv, Research Square, etc.)."""
    return any(doi.lower().startswith(p) for p in PREPRINT_DOI_PREFIXES)


def _pop_fields(target: dict[str, Any], field_names: set[str] | frozenset[str], log_tag: str) -> None:
    """Remove *field_names* from *target*, logging any that were actually present."""
    _sentinel = object()
    removed = [f for f in field_names if target.pop(f, _sentinel) is not _sentinel]
    if removed:
        logger.debug(f"{log_tag} | fields={removed}", category=LogCategory.CLEANUP)


def merge_with_policy(primary: dict[str, Any], enrichers: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """
    Combine a baseline BibTeX entry with metadata from multiple sources by
    following a trust hierarchy. Replace weaker fields with stronger ones and
    normalize identifiers such as DOIs and URLs.

    The result is a single cleaned entry that prefers reliable venues and removes
    arXiv eprint fields when a published DOI is present.
    """
    fields = dict(primary.get("fields") or {})
    etype = (primary.get("type") or "misc").lower()
    type_rank = {src: i for i, src in enumerate(TRUST_ORDER)}

    best_type_src = "scholar_min"

    active_sources = [s for s, e in enrichers if e]
    logger.debug(
        f"BEGIN | type={etype} | enrichers={active_sources} | baseline_fields={sorted(fields.keys())}",
        category=LogCategory.MERGE,
    )

    def value_ok(val: str | None) -> bool:
        return val is not None and not has_placeholder(val)

    valid_types = {"article", "inproceedings", "incollection", "book"}
    for src, e in enrichers:
        if not e:
            continue
        ktype = (e.get("type") or "").lower()
        rank = type_rank.get(src, 99)
        is_valid = ktype in valid_types
        is_better = rank < type_rank.get(best_type_src, 99)
        is_same_src_update = rank == type_rank.get(best_type_src, 99) and ktype != etype
        if is_valid and (is_better or is_same_src_update):
            prev_type, prev_src = etype, best_type_src
            etype = ktype
            best_type_src = src
            logger.debug(
                f"TYPE_CHANGE | {prev_type}->{etype} | src={src} (rank {rank})"
                f" beats {prev_src} (rank {type_rank.get(prev_src, 99)})",
                category=LogCategory.MERGE,
            )

    # track where each field came from to know when to replace
    field_sources: dict[str, str] = dict.fromkeys(fields, "scholar_min")
    merged = dict(fields)

    for src, e in enrichers:
        if not e:
            continue
        efields = e.get("fields") or {}
        for k, v in efields.items():
            cur = merged.get(k)
            cur_src = field_sources.get(k, "scholar_min")

            if not value_ok(v):
                logger.debug(f"FIELD_SKIP | {k} | src={src} | reason=placeholder_or_none", category=LogCategory.MERGE)
                continue
            if not value_ok(cur):
                merged[k] = v
                field_sources[k] = src
                logger.debug(
                    f"FIELD_ACCEPT | {k} (was empty) | src={src} | val={str(v)[:80]}",
                    category=LogCategory.MERGE,
                )
                continue

            # special handling for DOI field: prefer published DOIs over preprint DOIs
            if k == "doi":
                cur_is_preprint = _is_preprint_doi(str(cur))
                new_is_preprint = _is_preprint_doi(str(v))
                # if current is preprint DOI but new one isn't, always prefer published
                if cur_is_preprint and not new_is_preprint:
                    logger.debug(
                        f"DOI_UPGRADE | preprint->published | old={cur} | new={v} | src={src}",
                        category=LogCategory.MERGE,
                    )
                    merged[k] = v
                    field_sources[k] = src
                    continue
                # if new is preprint DOI but current isn't, keep current
                if not cur_is_preprint and new_is_preprint:
                    logger.debug(
                        f"DOI_KEEP | published beats preprint | cur={cur} | rejected={v} | src={src}",
                        category=LogCategory.MERGE,
                    )
                    continue

            # special handling for pages field: must be actual page numbers only
            if k == "pages":
                new_str = str(v).strip()
                # Validate: pages must start with a digit (page numbers only)
                if not re.match(r"^\d", new_str):
                    logger.debug(f"PAGES_REJECT | val={v} | reason=no_leading_digit", category=LogCategory.MERGE)
                    continue
                # Reject manuscript IDs containing dots (e.g., "2025.11.07.685935")
                if "." in new_str:
                    logger.debug(f"PAGES_REJECT | val={v} | reason=contains_dot", category=LogCategory.MERGE)
                    continue
                # Reject article IDs masquerading as pages (SAGE/Wiley use long numeric IDs)
                # Check each page component individually so ranges like "13905-13917" pass
                parts = re.split(r"[-\u2013\u2014,\s]+", new_str)
                overflow = [p for p in parts if p.strip() and len(re.sub(r"\D", "", p)) > PAGES_MAX_DIGITS]
                if overflow:
                    digits = len(re.sub(r"\D", "", overflow[0]))
                    logger.debug(
                        f"PAGES_REJECT | val={v} | reason=component_too_long({digits}digits)",
                        category=LogCategory.MERGE,
                    )
                    continue

            # special handling for journal field: never downgrade from published journal to preprint server
            if k == "journal":
                cur_journal_lower = str(cur).lower()
                new_journal_lower = str(v).lower()

                cur_is_preprint = any(ps in cur_journal_lower for ps in PREPRINT_SERVERS)
                new_is_preprint = any(ps in new_journal_lower for ps in PREPRINT_SERVERS)

                # Never replace a published journal with a preprint server
                if not cur_is_preprint and new_is_preprint:
                    logger.debug(
                        f"JOURNAL_KEEP | published beats preprint | cur={cur} | rejected={v} | src={src}",
                        category=LogCategory.MERGE,
                    )
                    continue

            # special handling for author field: prefer more complete (less abbreviated) names
            if k == "author":
                cur_parts = parse_authors_any(str(cur))
                new_parts = parse_authors_any(str(v))
                if cur_parts and new_parts and len(cur_parts) == len(new_parts):
                    # Count initials-only tokens (e.g. "J." but not "Jr.")
                    cur_inits = sum(1 for name in cur_parts for tok in name.split() if _AUTHOR_INITIAL_RE.match(tok))
                    new_inits = sum(1 for name in new_parts for tok in name.split() if _AUTHOR_INITIAL_RE.match(tok))
                    if new_inits > cur_inits:
                        logger.debug(
                            f"AUTHOR_KEEP_COMPLETE | cur_initials={cur_inits} new_initials={new_inits} "
                            f"| src={src} | keeping more complete names",
                            category=LogCategory.MERGE,
                        )
                        continue
                    # Also prefer the version with longer total author text
                    # (catches "Samuel" vs "Sam", middle initials dropped, etc.)
                    if new_inits == cur_inits:
                        cur_len = sum(len(n) for n in cur_parts)
                        new_len = sum(len(n) for n in new_parts)
                        if new_len < cur_len:
                            logger.debug(
                                f"AUTHOR_KEEP_LONGER | cur_len={cur_len} new_len={new_len} "
                                f"| src={src} | keeping longer author names",
                                category=LogCategory.MERGE,
                            )
                            continue

            # special handling for title field: prefer longer, more descriptive titles
            if k == "title":
                # Compare content length without whitespace so OCR artifacts
                # (e.g., "Un met" vs "Unmet") don't give the broken title a
                # false length advantage.
                cur_len = len(re.sub(r"\s+", "", str(cur)))
                new_len = len(re.sub(r"\s+", "", str(v)))

                # If new title is significantly shorter (< 70% of current length),
                # only replace if it comes from a MUCH more trusted source
                # (at least 3 positions higher in trust order)
                if cur_len > 0 and new_len < (cur_len * TITLE_LENGTH_KEEP_RATIO):
                    trust_diff = type_rank.get(cur_src, 99) - type_rank.get(src, 99)
                    if trust_diff < TRUST_DIFF_OVERRIDE_THRESHOLD:
                        # New source isn't significantly more trusted, keep longer title
                        logger.debug(
                            f"TITLE_KEEP_LONGER | cur_len={cur_len} new_len={new_len} "
                            f"ratio={new_len / cur_len:.2f} trust_diff={trust_diff}",
                            category=LogCategory.MERGE,
                        )
                        continue

            # special handling for booktitle: prefer specific conference name over generic series
            if k == "booktitle":
                cur_lower = str(cur).lower().strip()
                new_lower = str(v).lower().strip()
                # If current is a generic series name and new is more specific, always accept
                if cur_lower in GENERIC_SERIES_NAMES and new_lower not in GENERIC_SERIES_NAMES:
                    logger.debug(
                        f"BOOKTITLE_UPGRADE | generic->specific | old={str(cur)[:60]} | new={str(v)[:60]} | src={src}",
                        category=LogCategory.MERGE,
                    )
                    merged[k] = v
                    field_sources[k] = src
                    continue
                # Never replace a specific conference name with a generic series
                if cur_lower not in GENERIC_SERIES_NAMES and new_lower in GENERIC_SERIES_NAMES:
                    logger.debug(
                        f"BOOKTITLE_KEEP | specific beats generic | cur={str(cur)[:60]} "
                        f"| rejected={str(v)[:60]} | src={src}",
                        category=LogCategory.MERGE,
                    )
                    continue

            # only replace if new source is more trustworthy
            new_rank = type_rank.get(src, 99)
            cur_rank = type_rank.get(cur_src, 99)
            if new_rank < cur_rank:
                if str(cur) != str(v):
                    logger.debug(
                        f"FIELD_REPLACE | {k} | src={src} (rank {new_rank}) beats {cur_src} (rank {cur_rank}) "
                        f"| old={str(cur)[:60]} | new={str(v)[:60]}",
                        category=LogCategory.MERGE,
                    )
                merged[k] = v
                field_sources[k] = src
            else:
                logger.debug(
                    f"FIELD_KEEP | {k} | {cur_src} (rank {cur_rank}) over {src} (rank {new_rank})",
                    category=LogCategory.MERGE,
                )

    raw_doi = merged.get("doi")
    doi_norm = _norm_doi(raw_doi)
    if doi_norm:
        if str(raw_doi) != doi_norm:
            logger.debug(f"doi_normalize | {raw_doi}->{doi_norm}", category=LogCategory.CLEANUP)
        merged["doi"] = doi_norm
    elif raw_doi:
        logger.debug("doi_remove | invalid after normalization", category=LogCategory.CLEANUP)
        merged.pop("doi", None)

    # Validate DOI consistency: contradicting DOIs indicate different papers
    primary_doi = _norm_doi(primary.get("fields", {}).get("doi"))
    has_doi_conflict = False
    merged_doi_norm = _norm_doi(merged.get("doi"))

    if primary_doi and merged_doi_norm and merged_doi_norm != primary_doi:
        preprint_upgrade = _is_preprint_doi(primary_doi) and not _is_preprint_doi(merged_doi_norm)
        if preprint_upgrade:
            logger.debug(
                f"doi_conflict | primary={primary_doi} merged={merged_doi_norm} "
                f"| preprint_upgrade=True | kept={merged_doi_norm}",
                category=LogCategory.CLEANUP,
            )
        else:
            logger.debug(
                f"doi_conflict | primary={primary_doi} merged={merged_doi_norm} "
                f"| preprint_upgrade=False | kept={primary_doi}",
                category=LogCategory.CLEANUP,
            )
            merged["doi"] = primary_doi
            has_doi_conflict = True

    # Only trust DOIs from registration agencies and authoritative databases
    if merged.get("doi") and not has_doi_conflict:
        trusted_doi_sources = {"csl", "doi_bibtex", "datacite", "pubmed", "europepmc", "crossref"}
        merged_doi_norm = _norm_doi(merged["doi"])
        doi_trusted_src = next(
            (
                src
                for src, e in enrichers
                if src in trusted_doi_sources and e and _norm_doi((e.get("fields") or {}).get("doi")) == merged_doi_norm
            ),
            None,
        )
        if not doi_trusted_src:
            doi_src = field_sources.get("doi", "unknown")
            logger.debug(
                f"doi_untrusted | source={doi_src} | trusted_sources={sorted(trusted_doi_sources)} | action=removed",
                category=LogCategory.CLEANUP,
            )
            merged.pop("doi", None)
        else:
            logger.debug(
                f"doi_trusted | source={field_sources.get('doi', 'unknown')} | verified_via={doi_trusted_src}",
                category=LogCategory.CLEANUP,
            )

    _pop_fields(merged, DEDUP_INTERNAL_FIELDS, "dedup_fields_removed")
    merged = normalize_arxiv_metadata(merged)
    _pop_fields(merged, {"keywords", "copyright"}, "unwanted_removed")

    # Sanitize author names: strip trailing digit suffixes (e.g., "Das1" -> "Das")
    # that leak from Scholar/DBLP author disambiguation markers
    author_val = merged.get("author", "")
    if author_val:
        author_parts = [p.strip() for p in str(author_val).split(_AUTHOR_SEPARATOR)]
        author_cleaned = [_sanitize_author_digits(p) for p in author_parts]
        if author_cleaned != author_parts:
            merged["author"] = _AUTHOR_SEPARATOR.join(author_cleaned)
            logger.debug(
                f"author_digit_sanitize | before={author_val[:80]} | after={merged['author'][:80]}",
                category=LogCategory.CLEANUP,
            )

    # Fix author name casing: capitalize all-lowercase tokens AND convert
    # ALL-CAPS tokens to title case (catches "darren steeves", "F VARNO").
    author_val = merged.get("author", "")
    if author_val:
        fixed_author, author_was_fixed = _fix_author_casing(str(author_val))
        if author_was_fixed:
            merged["author"] = fixed_author
            logger.debug(
                f"author_capitalize | before={author_val[:80]} | after={merged['author'][:80]}",
                category=LogCategory.CLEANUP,
            )

    pages_val = merged.get("pages", "")
    if pages_val:
        pages_str = str(pages_val).strip()
        if not re.match(r"^\d", pages_str) or "." in pages_str:
            logger.debug(f"pages_remove | val={pages_str} | reason=invalid_format", category=LogCategory.CLEANUP)
            merged.pop("pages", None)
        else:
            parts = re.split(r"[-\u2013\u2014,\s]+", pages_str)
            if any(len(re.sub(r"\D", "", p)) > PAGES_MAX_DIGITS for p in parts if p.strip()):
                logger.debug(f"pages_remove | val={pages_str} | reason=digit_overflow", category=LogCategory.CLEANUP)
                merged.pop("pages", None)
            else:
                cleaned_pages = re.sub(r"\b0+(\d)", r"\1", pages_str)
                if cleaned_pages != pages_str:
                    logger.debug(f"pages_leading_zeros | {pages_str}->{cleaned_pages}", category=LogCategory.CLEANUP)
                    merged["pages"] = cleaned_pages

    # Remove volume if it equals year (common conference proceedings error)
    year_val = merged.get("year", "")
    volume_val = merged.get("volume", "")
    if year_val and volume_val and str(year_val) == str(volume_val):
        logger.debug(
            f"volume_equals_year | volume={volume_val} year={year_val} | action=volume_removed",
            category=LogCategory.CLEANUP,
        )
        merged.pop("volume", None)

    # Strip " : the preprint server for X" suffixes added by PubMed/Europe PMC
    journal_val = merged.get("journal", "")
    if journal_val:
        journal_cleaned = re.sub(r"\s*:\s*the preprint server for [\w\s]+$", "", journal_val, flags=re.IGNORECASE)
        if journal_cleaned != journal_val:
            logger.debug(
                f"journal_preprint_suffix | {journal_val}->{journal_cleaned.strip()}",
                category=LogCategory.CLEANUP,
            )
            merged["journal"] = journal_cleaned.strip()

    journal_val = merged.get("journal", "")
    if journal_val and journal_val.startswith("http"):
        jurl = journal_val.lower()
        resolved_name = next((name for domain, name in _JOURNAL_URL_MAP.items() if domain in jurl), None)
        if resolved_name:
            logger.debug(f"journal_url_to_name | {journal_val}->{resolved_name}", category=LogCategory.CLEANUP)
            merged["journal"] = resolved_name
        else:
            logger.debug(f"journal_url_removed | {journal_val}", category=LogCategory.CLEANUP)
            merged.pop("journal", None)

    # Correct bioRxiv/medRxiv publisher (APIs sometimes return wrong names)
    journal_lower = (merged.get("journal") or "").lower()
    if journal_lower in ("biorxiv", "medrxiv"):
        old_publisher = merged.get("publisher", "")
        if old_publisher != "Cold Spring Harbor Laboratory":
            logger.debug(
                f"publisher_correct | journal={journal_lower} "
                f"| publisher={old_publisher or '(empty)'}->Cold Spring Harbor Laboratory",
                category=LogCategory.CLEANUP,
            )
        merged["publisher"] = "Cold Spring Harbor Laboratory"

    # Apply journal-specific publisher corrections (e.g. SAGE → Mary Ann Liebert for JCB)
    if journal_lower:
        for _jnl_key, _correct_pub in PUBLISHER_CORRECTIONS.items():
            if _jnl_key in journal_lower:
                _cur_pub = merged.get("publisher", "")
                if _cur_pub and _cur_pub != _correct_pub:
                    logger.debug(
                        f"publisher_correct | journal={journal_lower[:50]} | publisher={_cur_pub}->{_correct_pub}",
                        category=LogCategory.CLEANUP,
                    )
                    merged["publisher"] = _correct_pub
                break

    # Strip preprint-only publishers from entries that have a real journal
    pub_lower = (merged.get("publisher") or "").lower().strip()
    journal_for_pub_check = (merged.get("journal") or "").lower()
    if (
        pub_lower in PREPRINT_ONLY_PUBLISHERS
        and journal_for_pub_check
        and not any(ps in journal_for_pub_check for ps in PREPRINT_SERVERS)
    ):
        logger.debug(
            f"publisher_preprint_stripped | publisher={merged['publisher']} "
            f"| journal={merged.get('journal')} | reason=preprint_publisher_on_published",
            category=LogCategory.CLEANUP,
        )
        merged.pop("publisher", None)

    # Decode HTML entities then strip HTML/XML tags from text fields
    text_fields_to_clean = ["title", "journal", "booktitle", "series"]
    for field in text_fields_to_clean:
        field_val = merged.get(field, "")
        if field_val and isinstance(field_val, str):
            cleaned = html.unescape(field_val)
            cleaned = re.sub(r"<[^>]+>", "", cleaned)
            if cleaned != field_val:
                logger.debug(f"html_decode | field={field} | changed=True", category=LogCategory.CLEANUP)
                merged[field] = cleaned.strip()

    title_val = merged.get("title", "")
    if title_val and isinstance(title_val, str) and title_val.rstrip().endswith("*"):
        logger.debug("title_asterisk | removed trailing *", category=LogCategory.CLEANUP)
        merged["title"] = title_val.rstrip().rstrip("*").rstrip()

    note_val = merged.get("note", "")
    if note_val and note_val.strip().startswith("PMID:"):
        logger.debug("note_pmid_removed", category=LogCategory.CLEANUP)
        merged.pop("note", None)

    url_val = (merged.get("url") or "").strip()
    allowed = allowlisted_url(url_val)
    if url_val and not allowed:
        logger.debug(f"url_rejected | {url_val}", category=LogCategory.CLEANUP)
        merged.pop("url", None)
    elif allowed:
        if allowed != url_val:
            logger.debug(f"url_canonicalized | {url_val}->{allowed}", category=LogCategory.CLEANUP)
        merged["url"] = allowed

    # When a published DOI exists alongside arXiv, remove eprint fields
    doi_val = merged.get("doi")
    arxiv_id = extract_arxiv_eprint({"fields": merged})
    if doi_val and arxiv_id and not _is_preprint_doi(doi_val):
        logger.debug(f"eprint_removed | doi={doi_val} (published) | arxiv={arxiv_id}", category=LogCategory.CLEANUP)
        merged.pop("eprint", None)
        merged.pop("archiveprefix", None)
        merged.pop("primaryclass", None)
        # Update URL to match the published DOI (remove stale preprint URLs)
        _current_url = (merged.get("url") or "").lower()
        if _current_url and any(
            ps in _current_url
            for ps in (
                "arxiv.org",
                "biorxiv.org",
                "medrxiv.org",
                "ssrn.com",
                "preprints.org",
                "techrxiv.org",
                "researchsquare.com",
                "10.1101/",
                "10.2139/",
                "10.20944/",
                "10.21203/",
            )
        ):
            merged["url"] = f"https://doi.org/{doi_val}"
            logger.debug(
                f"url_preprint_replaced | old={_current_url[:60]} | new={merged['url']}",
                category=LogCategory.CLEANUP,
            )
        # Clear any remaining arXiv journal value and backfill from enrichers.
        # normalize_arxiv_metadata may have already stripped the arXiv journal;
        # either way, try to recover the real journal from enrichers.
        journal_check = (merged.get("journal") or "").strip().lower()
        if journal_check in ("arxiv e-prints", "arxiv"):
            logger.debug(
                "phantom_journal_removed | journal was arXiv but entry has published DOI",
                category=LogCategory.CLEANUP,
            )
            merged.pop("journal", None)
        # Backfill from enrichers when journal is missing: find the best-ranked
        # source that carries a real (non-preprint) journal matching the published DOI.
        if not merged.get("journal"):
            doi_norm_check = _norm_doi(doi_val)
            best_journal: str | None = None
            best_journal_rank = 999
            for esrc, edata in enrichers:
                if not edata:
                    continue
                efields = edata.get("fields") or {}
                ej = (efields.get("journal") or "").strip()
                if not ej:
                    continue
                if any(ps in ej.lower() for ps in PREPRINT_SERVERS):
                    continue
                # Only accept journals from enrichers whose DOI matches the published DOI
                enricher_doi = _norm_doi(efields.get("doi"))
                if enricher_doi and doi_norm_check and enricher_doi != doi_norm_check:
                    continue
                erank = type_rank.get(esrc, 99)
                if erank < best_journal_rank:
                    best_journal = ej
                    best_journal_rank = erank
            if best_journal:
                merged["journal"] = best_journal
                field_sources["journal"] = next(
                    (s for s, e in enrichers if e and (e.get("fields") or {}).get("journal") == best_journal),
                    "unknown",
                )
                logger.debug(
                    f"journal_backfill | journal={best_journal} | src={field_sources['journal']}",
                    category=LogCategory.CLEANUP,
                )
            elif etype == "article":
                # No enricher carries a real journal for this DOI (e.g., OSTI
                # technical reports).  Downgrade @article -> @misc so the entry
                # doesn't violate BibTeX's requirement of a journal field.
                etype = "misc"
                logger.debug(
                    f"article_downgrade_no_journal | doi={doi_val} | phantom_removed=True | type->misc",
                    category=LogCategory.CLEANUP,
                )

    # Fix journal names misplaced in booktitle field.
    # Some publishers (e.g., Frontiers Media SA) only publish journals, never
    # conference proceedings. If booktitle matches a known journal-only prefix
    # and no journal is set, move it to the journal field.
    bt_val = (merged.get("booktitle") or "").strip()
    if bt_val and not merged.get("journal"):
        bt_lower = bt_val.lower()
        if any(bt_lower.startswith(prefix) for prefix in JOURNAL_ONLY_PREFIXES):
            logger.debug(
                f"frontiers_migrate | booktitle->journal={bt_val} | type->article",
                category=LogCategory.CLEANUP,
            )
            merged["journal"] = bt_val
            merged.pop("booktitle", None)
            etype = "article"

    # Fix conference proceedings misclassified as @article with journal.
    # Crossref registers some conference proceedings (AAAI, EMNLP, PVLDB,
    # PACMHCI, etc.) as journal volumes. Any venue named "Proceedings of..."
    # is proceedings, not a journal — reclassify as @inproceedings.
    if etype == "article" and merged.get("journal") and not merged.get("booktitle"):
        jnl_for_conf = merged["journal"].strip()
        if _is_conference_journal(jnl_for_conf):
            logger.debug(
                f"conference_as_journal | journal={jnl_for_conf} | type article->inproceedings",
                category=LogCategory.CLEANUP,
            )
            merged["booktitle"] = merged.pop("journal")
            etype = "inproceedings"

    # Resolve Dagstuhl LIPIcs/OASIcs DOIs to correct conference name.
    # S2 often returns bogus venue expansions for LIPIcs abbreviations (e.g.,
    # ESA -> "Embedded Systems and Applications", SEA -> "The Sea").
    # CSL returns @misc with howpublished for all Dagstuhl entries.
    # The DOI pattern 10.4230/{lipics|oasics}.{conf}.{year}.{paper} encodes
    # the conference abbreviation, so we can resolve it directly.
    doi_for_dagstuhl = (merged.get("doi") or "").strip()
    m_dag = _DAGSTUHL_DOI_RE.match(doi_for_dagstuhl) if doi_for_dagstuhl else None
    if m_dag:
        dag_conf = m_dag.group(2).lower()
        old_journal = merged.get("journal", "")
        old_booktitle = merged.get("booktitle", "")
        if dag_conf in ABBREVIATED_VENUE_MAP:
            dag_full = ABBREVIATED_VENUE_MAP[dag_conf]
            merged["booktitle"] = dag_full
            merged.pop("journal", None)
            merged.pop("howpublished", None)
            etype = "inproceedings"
            logger.debug(
                f"dagstuhl_resolve | doi={doi_for_dagstuhl} | conf={dag_conf} "
                f"| booktitle={dag_full} | old_journal={old_journal} "
                f"| old_booktitle={old_booktitle} | type->inproceedings",
                category=LogCategory.CLEANUP,
            )
        else:
            # Festschrift or unknown series: conf abbrev not in map but DOI
            # confirms it's a Dagstuhl proceedings publication (LIPIcs/OASIcs)
            etype = "inproceedings"
            if old_journal and not merged.get("booktitle"):
                merged["booktitle"] = old_journal
                merged.pop("journal", None)
            merged.pop("howpublished", None)
            logger.debug(
                f"dagstuhl_resolve_fallback | doi={doi_for_dagstuhl} | conf={dag_conf} "
                f"| booktitle={merged.get('booktitle', '')} | type->inproceedings",
                category=LogCategory.CLEANUP,
            )

    # expand abbreviated venue names (e.g., "SPIRE" -> full conference name)
    # S2 and DBLP often return just the abbreviation without descriptive keywords
    # All entries in ABBREVIATED_VENUE_MAP are conferences, so if the abbreviation
    # appears in 'journal', move it to 'booktitle' (correct BibTeX field for conferences)
    for venue_field in ("journal", "booktitle", "howpublished"):
        venue_val = (merged.get(venue_field) or "").strip()
        venue_key = venue_val.lower()
        if venue_val and venue_key in ABBREVIATED_VENUE_MAP:
            expanded = ABBREVIATED_VENUE_MAP[venue_key]
            logger.debug(f"venue_expand | {venue_val}->{expanded} | field={venue_field}", category=LogCategory.CLEANUP)
            if venue_field == "journal":
                merged.pop("journal", None)
                merged["booktitle"] = expanded
            else:
                merged[venue_field] = expanded

    venue_fields = {
        "journal": merged.get("journal"),
        "booktitle": merged.get("booktitle"),
        "howpublished": merged.get("howpublished"),
        "publisher": merged.get("publisher"),
        "pages": merged.get("pages"),
    }
    venue_type = determine_entry_type(venue_fields, type_field="type", venue_hints={})

    # Re-validate type from venue content, preserving authoritative book/article types
    pre_revalidate_type = etype
    if venue_type == "inproceedings" and etype != "book":
        is_authoritative_article = etype == "article" and best_type_src in ("csl", "doi_bibtex")
        if not is_authoritative_article or is_secondary_doi(str(merged.get("doi", ""))):
            etype = "inproceedings"
    elif venue_type == "incollection" and etype != "book":
        etype = "incollection"
    elif etype == "misc":
        venue_type_with_hints = determine_entry_type(
            venue_fields,
            type_field="type",
            venue_hints={"journal": "article", "booktitle": "inproceedings"},
        )

        if venue_type_with_hints != "misc":
            etype = venue_type_with_hints
        else:
            journal = (merged.get("journal") or "").strip()
            booktitle = (merged.get("booktitle") or "").strip()
            if journal:
                etype = "article"
            elif booktitle:
                etype = "inproceedings"
    if etype != pre_revalidate_type:
        logger.debug(
            f"type_revalidate | {pre_revalidate_type}->{etype} | venue_type={venue_type}",
            category=LogCategory.CLEANUP,
        )

    # Promote incollection to inproceedings for known proceedings series (LNCS, SHTI, etc.)
    if etype == "incollection" and merged.get("booktitle"):
        bt_lower = merged["booktitle"].lower().strip()
        series_lower = (merged.get("series") or "").lower().strip()
        if bt_lower in GENERIC_SERIES_NAMES or series_lower in GENERIC_SERIES_NAMES:
            logger.debug(
                f"incollection_upgrade | generic_series={bt_lower or series_lower} | type->inproceedings",
                category=LogCategory.CLEANUP,
            )
            etype = "inproceedings"

    if etype == "incollection" and not merged.get("booktitle") and merged.get("howpublished"):
        logger.debug(
            f"howpublished_migrate | howpublished->booktitle={merged['howpublished']}",
            category=LogCategory.CLEANUP,
        )
        merged["booktitle"] = merged["howpublished"]
        merged.pop("howpublished", None)

    expected_container = get_container_field(etype)
    removed_containers: list[str] = []
    migrated_container = ""

    if expected_container == "journal":
        if merged.get("booktitle"):
            removed_containers.append("booktitle")
        if merged.get("howpublished"):
            removed_containers.append("howpublished")
        merged.pop("booktitle", None)
        merged.pop("howpublished", None)
    elif expected_container == "booktitle":
        if merged.get("journal") and not merged.get("booktitle"):
            migrated_container = "journal->booktitle"
            merged["booktitle"] = merged["journal"]
        elif (
            merged.get("journal")
            and merged.get("booktitle")
            and (merged["booktitle"].lower().strip() in GENERIC_SERIES_NAMES)
            and (merged["journal"].lower().strip() not in GENERIC_SERIES_NAMES)
        ):
            migrated_container = "journal->booktitle(generic_upgrade)"
            merged["booktitle"] = merged["journal"]
        if merged.get("journal"):
            removed_containers.append("journal")
        if merged.get("howpublished"):
            removed_containers.append("howpublished")
        merged.pop("journal", None)
        merged.pop("howpublished", None)
    if removed_containers or migrated_container:
        logger.debug(
            f"container_enforce | type={etype} | expected={expected_container} "
            f"| removed={removed_containers} | migrated={migrated_container or 'none'}",
            category=LogCategory.CLEANUP,
        )

    _normalize_howpublished(merged)

    unique_sources = sorted(set(field_sources.values()))
    logger.debug(
        f"COMPLETE | type={etype} | key={primary.get('key')} | fields={sorted(merged.keys())} "
        f"| sources_used={unique_sources} | field_count={len(merged)}",
        category=LogCategory.MERGE,
    )

    return {"type": etype, "key": primary.get("key"), "fields": merged}


def save_entry_to_file(
    out_dir: str,
    author_id: str,
    entry: dict[str, Any],
    prefer_path: str | None = None,
    gemini_api_key: str | None = None,
    author_name: str | None = None,
) -> tuple[str, bool]:
    """
    Write a BibTeX entry to disk inside an author-specific output directory,
    choosing a short descriptive filename from the entry fields. It reuses a
    previous path when possible and can remove an obsolete file when the location changes.

    For filename collisions with different publications, more words from the title
    are used to create a unique filename (never appending numeric counters).

    If a colliding filename already exists with identical content, it will be
    reused (overwritten) instead of creating a duplicate.

    Returns (path, was_written) where was_written is False when the existing file
    was kept via SKIP_WRITE (duplicate with better version already on disk).
    """
    author_dirname = format_author_dirname(author_name, author_id)
    author_dir = os.path.join(out_dir, author_dirname)
    os.makedirs(author_dir, exist_ok=True)

    all_files = sorted(f for f in os.listdir(author_dir) if f.endswith(".bib"))
    collision_files = set(all_files) - {os.path.basename(prefer_path)} if prefer_path else set(all_files)

    filename = short_filename_for_entry(
        entry,
        gemini_api_key=gemini_api_key,
        existing_files=collision_files,
    )

    new_content = bibtex_from_dict(entry)
    new_fields = entry.get("fields", {})
    new_doi = _norm_doi(new_fields.get("doi")) or ""
    new_title_str = new_fields.get("title", "")
    duplicate_found = False
    duplicate_path = None
    scan_count = len(all_files)
    logger.debug(
        f"FILE_SCAN_START | title={new_title_str[:60]} | scanning={scan_count}_existing_files "
        f"| author_dir={author_dirname}",
        category=LogCategory.DEDUP,
    )

    # Skip prefer_path in the duplicate scan: it's the file we're updating and
    # is already handled by the while-loop's prefer_path check.  Scanning it
    # first would hide a real preprint/published duplicate sitting in another file.
    prefer_basename = os.path.basename(prefer_path) if prefer_path else None

    for existing_filename in all_files:
        if existing_filename == prefer_basename:
            continue
        existing_path = os.path.join(author_dir, existing_filename)
        try:
            with open(existing_path, encoding="utf-8") as ef:
                existing_content = ef.read()
                existing_entry = parse_bibtex_to_dict(existing_content)

                if existing_entry:
                    existing_fields = existing_entry.get("fields", {})
                    existing_doi = _norm_doi(existing_fields.get("doi")) or ""

                    if existing_doi and new_doi and existing_doi == new_doi:
                        logger.debug(
                            f"FILE_MATCH | DOI_EXACT | file={existing_filename} | doi={existing_doi}",
                            category=LogCategory.DEDUP,
                        )
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # DOI version variants (e.g. Preprints.org .v1 / .v2)
                    if existing_doi and new_doi and doi_bases_match(existing_doi, new_doi):
                        logger.debug(
                            f"FILE_MATCH | DOI_VERSION | file={existing_filename}"
                            f" | doi_a={existing_doi} | doi_b={new_doi}",
                            category=LogCategory.DEDUP,
                        )
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # Different DOIs: only match preprint/published pairs (XOR)
                    if existing_doi and new_doi and existing_doi != new_doi:
                        e_preprint = _is_preprint_doi(existing_doi)
                        n_preprint = _is_preprint_doi(new_doi)
                        if e_preprint != n_preprint:
                            # If both have distinct arXiv eprint IDs, they are
                            # different papers -- skip preprint pair matching.
                            e_eprint = extract_arxiv_eprint(existing_entry)
                            n_eprint = extract_arxiv_eprint(entry)
                            if e_eprint and n_eprint and e_eprint != n_eprint:
                                continue
                            e_title = existing_fields.get("title", "")
                            n_title = new_fields.get("title", "")
                            preprint_sim = title_similarity(e_title, n_title)
                            if preprint_sim >= SIM_PREPRINT_TITLE_THRESHOLD:
                                score = compute_dedup_score(existing_fields, new_fields)
                                # The preprint-pair bonus (0.10) in composite is
                                # circular here — we already verified the XOR
                                # precondition.  Subtract it to avoid inflating
                                # the composite with evidence we already used.
                                effective_score = score - 0.10
                                if effective_score >= SIM_DEDUP_COMPOSITE_THRESHOLD:
                                    logger.debug(
                                        f"FILE_MATCH | PREPRINT_PAIR | file={existing_filename}"
                                        f" | sim={preprint_sim:.3f} | composite={score:.3f}"
                                        f" | effective={effective_score:.3f}"
                                        f" | e_preprint={e_preprint} n_preprint={n_preprint}",
                                        category=LogCategory.DEDUP,
                                    )
                                    duplicate_found = True
                                    duplicate_path = existing_path
                                    break
                        continue

                    if external_ids_match(existing_fields, new_fields):
                        existing_title = existing_fields.get("title", "")
                        new_title = new_fields.get("title", "")
                        sim = title_similarity(existing_title, new_title)
                        if sim >= SIM_PREPRINT_TITLE_THRESHOLD:
                            logger.debug(
                                f"FILE_MATCH | EXTERNAL_ID | file={existing_filename} | sim={sim:.3f}",
                                category=LogCategory.DEDUP,
                            )
                            duplicate_found = True
                            duplicate_path = existing_path
                            break

                    existing_title = existing_fields.get("title", "")
                    new_title = new_fields.get("title", "")

                    # Citation key match requires title verification to avoid
                    # Gemini generating identical short titles for different papers
                    existing_key = existing_entry.get("key", "").strip()
                    new_key = entry.get("key", "").strip()
                    if existing_key and new_key and existing_key == new_key:
                        key_title_sim = title_similarity(existing_title, new_title)
                        # Also check if shorter title is a prefix of longer (truncated stub)
                        _e_norm = normalize_title(existing_title)
                        _n_norm = normalize_title(new_title)
                        _is_prefix = (_e_norm.startswith(_n_norm) and len(_n_norm) > 20) or (
                            _n_norm.startswith(_e_norm) and len(_e_norm) > 20
                        )
                        if key_title_sim >= SIM_FILE_DUPLICATE_THRESHOLD or _is_prefix:
                            logger.debug(
                                f"FILE_MATCH | KEY_TITLE | file={existing_filename} "
                                f"| key={existing_key} | sim={key_title_sim:.3f}",
                                category=LogCategory.DEDUP,
                            )
                            duplicate_found = True
                            duplicate_path = existing_path
                            break
                        # Keys match but titles differ -- check author overlap.
                        # Same key + strong author overlap = same paper with
                        # title change between preprint and publication.
                        _key_author_overlap = author_overlap_ratio(
                            existing_fields.get("author"), new_fields.get("author")
                        )
                        if _key_author_overlap >= 0.8 and key_title_sim >= 0.55:
                            logger.debug(
                                f"FILE_MATCH | KEY_AUTHOR_OVERLAP | file={existing_filename} "
                                f"| key={existing_key} | sim={key_title_sim:.3f} "
                                f"| author_overlap={_key_author_overlap:.3f}",
                                category=LogCategory.DEDUP,
                            )
                            duplicate_found = True
                            duplicate_path = existing_path
                            break

                        # Keys match but titles differ significantly -- check if
                        # this is a preprint/published pair before giving up
                        if existing_doi and new_doi and existing_doi != new_doi:
                            e_preprint = _is_preprint_doi(existing_doi)
                            n_preprint = _is_preprint_doi(new_doi)
                            # Distinct arXiv eprint IDs -> different papers
                            ke_eprint = extract_arxiv_eprint(existing_entry)
                            kn_eprint = extract_arxiv_eprint(entry)
                            if ke_eprint and kn_eprint and ke_eprint != kn_eprint:
                                continue
                            if (
                                (e_preprint ^ n_preprint)
                                and key_title_sim >= SIM_PREPRINT_TITLE_THRESHOLD
                                and authors_overlap(existing_fields.get("author"), new_fields.get("author"))
                            ):
                                key_preprint_score = compute_dedup_score(existing_fields, new_fields)
                                logger.debug(
                                    f"FILE_MATCH | KEY_PREPRINT_PAIR | file={existing_filename} "
                                    f"| sim={key_title_sim:.3f} | composite={key_preprint_score:.3f}",
                                    category=LogCategory.DEDUP,
                                )
                                duplicate_found = True
                                duplicate_path = existing_path
                                break
                        continue

                    # Compare by title similarity alone
                    sim = title_similarity(existing_title, new_title)
                    if sim >= SIM_FILE_DUPLICATE_THRESHOLD:
                        logger.debug(
                            f"FILE_MATCH | HIGH_TITLE_SIM | file={existing_filename} | sim={sim:.3f}",
                            category=LogCategory.DEDUP,
                        )
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # Truncated title fallback: one title is a prefix of the other
                    if title_is_truncated_match(existing_title, new_title) and authors_overlap(
                        existing_fields.get("author"), new_fields.get("author")
                    ):
                        logger.debug(
                            f"FILE_MATCH | TRUNCATED | file={existing_filename} | authors_overlap=True",
                            category=LogCategory.DEDUP,
                        )
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # Strong author overlap: multi-author team with moderate title similarity
                    if sim >= 0.6:
                        e_authors = parse_authors_any(existing_fields.get("author", ""))
                        n_authors = parse_authors_any(new_fields.get("author", ""))
                        if len(e_authors) >= 2 and len(n_authors) >= 2:
                            overlap = author_overlap_ratio(existing_fields.get("author"), new_fields.get("author"))
                            if overlap >= 0.9:
                                score = compute_dedup_score(existing_fields, new_fields)
                                if score >= SIM_DEDUP_COMPOSITE_THRESHOLD:
                                    logger.debug(
                                        f"FILE_MATCH | STRONG_AUTHOR | file={existing_filename} "
                                        f"| overlap={overlap:.3f} | sim={sim:.3f} "
                                        f"| composite={score:.3f} "
                                        f"| n_authors_a={len(e_authors)} n_authors_b={len(n_authors)}",
                                        category=LogCategory.DEDUP,
                                    )
                                    duplicate_found = True
                                    duplicate_path = existing_path
                                    break

                    # Preprint/published pair with evidence on the published side
                    if sim >= SIM_PREPRINT_TITLE_THRESHOLD:
                        e_journal = existing_fields.get("journal", "").lower()
                        n_journal = new_fields.get("journal", "").lower()
                        e_preprint = _is_preprint_doi(existing_doi) or any(ps in e_journal for ps in PREPRINT_SERVERS)
                        n_preprint = _is_preprint_doi(new_doi) or any(ps in n_journal for ps in PREPRINT_SERVERS)
                        if (e_preprint ^ n_preprint) and authors_overlap(
                            existing_fields.get("author"), new_fields.get("author")
                        ):
                            published_has_evidence = (e_preprint and (new_doi or n_journal)) or (
                                n_preprint and (existing_doi or e_journal)
                            )
                            if published_has_evidence:
                                logger.debug(
                                    f"FILE_MATCH | PREPRINT_RELAXED | file={existing_filename} | sim={sim:.3f} "
                                    f"| evidence=preprint_published_pair",
                                    category=LogCategory.DEDUP,
                                )
                                duplicate_found = True
                                duplicate_path = existing_path
                                break
        except OSError:
            logger.debug(f"FILE_READ_ERROR | file={existing_filename}", category=LogCategory.DEDUP)

    dup_filename = os.path.basename(duplicate_path) if duplicate_path else "none"
    logger.debug(
        f"FILE_SCAN_DONE | title={new_title_str[:60]} | duplicate_found={duplicate_found} "
        f"| duplicate_file={dup_filename} | scanned={scan_count}",
        category=LogCategory.DEDUP,
    )

    skip_write = False
    dedup_replaced = False
    if duplicate_found and duplicate_path:
        # Default: reuse duplicate's filename (overridden below when parseable)
        filename = os.path.basename(duplicate_path)
        try:
            with open(duplicate_path, encoding="utf-8") as ef:
                existing_content = ef.read()
            existing_entry = parse_bibtex_to_dict(existing_content)

            if existing_entry:
                existing_fields = existing_entry.get("fields", {})
                existing_year = existing_fields.get("year", "")
                new_year = new_fields.get("year", "")
                existing_doi = _norm_doi(existing_fields.get("doi")) or ""

                # Check preprint/published relationship when DOIs differ
                existing_is_preprint = existing_doi and _is_preprint_doi(existing_doi)
                new_is_preprint = new_doi and _is_preprint_doi(new_doi)

                def _replace_existing() -> None:
                    nonlocal dedup_replaced
                    dedup_replaced = True
                    with contextlib.suppress(OSError):
                        os.remove(duplicate_path)

                dup_basename = os.path.basename(duplicate_path)
                if existing_doi and new_doi and existing_doi != new_doi:
                    # Different DOIs: one is preprint, one is published (dedup already confirmed match)
                    if not existing_is_preprint and (new_is_preprint or not new_doi):
                        # Existing is published, new is preprint -> keep published
                        logger.debug(
                            f"DECISION | KEEP_EXISTING | reason=existing_published_new_preprint | file={dup_basename}",
                            category=LogCategory.DEDUP,
                        )
                        skip_write = True
                    elif existing_is_preprint and not new_is_preprint:
                        # Existing is preprint, new is published -> replace preprint
                        logger.debug(
                            f"DECISION | REPLACE | reason=new_published_beats_preprint | file={dup_basename}",
                            category=LogCategory.DEDUP,
                        )
                        _replace_existing()
                    else:
                        # Both preprint or both published -- prefer incoming entry
                        # (it went through merge_with_policy) unless existing has
                        # significantly more fields (3+ advantage).
                        existing_field_count = sum(1 for v in existing_fields.values() if v)
                        new_field_count = sum(1 for v in new_fields.values() if v)
                        if existing_field_count >= new_field_count + 3:
                            logger.debug(
                                f"DECISION | KEEP_EXISTING | reason=existing_more_complete "
                                f"| existing={existing_field_count} new={new_field_count} "
                                f"| file={dup_basename}",
                                category=LogCategory.DEDUP,
                            )
                            skip_write = True
                        else:
                            logger.debug(
                                f"DECISION | REPLACE | reason=new_more_complete | file={dup_basename}",
                                category=LogCategory.DEDUP,
                            )
                            _replace_existing()
                elif existing_doi and not new_doi:
                    # Existing has DOI, new doesn't -> keep existing (more complete)
                    logger.debug(
                        f"DECISION | KEEP_EXISTING | reason=existing_has_doi | file={dup_basename}",
                        category=LogCategory.DEDUP,
                    )
                    skip_write = True
                elif new_doi and not existing_doi:
                    # New has DOI, existing doesn't -> replace existing
                    logger.debug(
                        f"DECISION | REPLACE | reason=new_has_doi | file={dup_basename}",
                        category=LogCategory.DEDUP,
                    )
                    _replace_existing()
                elif existing_year and new_year and existing_year != new_year:
                    # Year changed, same or no DOIs -- keep generated filename for year correction
                    logger.debug(
                        f"DECISION | USE_NEW_NAME | reason=year_change | old_year={existing_year} new_year={new_year}",
                        category=LogCategory.DEDUP,
                    )
                else:
                    # Same year, same DOI (or both missing) -> reuse existing filename
                    existing_key = existing_entry.get("key", "")
                    if existing_key:
                        logger.debug(
                            f"DECISION | REUSE_KEY | reason=same_pub | key={existing_key}",
                            category=LogCategory.DEDUP,
                        )
                        entry["key"] = existing_key
        except OSError:
            pass

    # When the duplicate scan determined the existing file is the better version
    # (e.g., published entry vs incoming preprint), skip writing and return early
    if skip_write:
        logger.debug(f"SKIP_WRITE | file={duplicate_path} | reason=existing_is_better", category=LogCategory.DEDUP)
        # Clean up the baseline file that was written before enrichment
        if prefer_path and os.path.abspath(prefer_path) != os.path.abspath(duplicate_path or ""):
            with contextlib.suppress(OSError):
                if os.path.exists(prefer_path):
                    os.remove(prefer_path)
        return duplicate_path or os.path.join(author_dir, filename), False

    while os.path.exists(os.path.join(author_dir, filename)):
        existing_path = os.path.join(author_dir, filename)
        if prefer_path and os.path.abspath(existing_path) == os.path.abspath(prefer_path):
            break
        try:
            with open(existing_path, encoding="utf-8") as ef:
                existing_content = ef.read()
                if existing_content.rstrip() == new_content.rstrip():
                    logger.debug(
                        f"COLLISION_CHECK | file={filename} | identical_content=True",
                        category=LogCategory.DEDUP,
                    )
                    break

                existing_entry = parse_bibtex_to_dict(existing_content)
                if existing_entry:
                    existing_fields = existing_entry.get("fields", {})
                    existing_doi = _norm_doi(existing_fields.get("doi")) or ""

                    if existing_doi and new_doi and existing_doi == new_doi:
                        logger.debug(
                            f"COLLISION_CHECK | file={filename} | identical_content=False "
                            f"| same_doi=True | doi={existing_doi}",
                            category=LogCategory.DEDUP,
                        )
                        break

                    if existing_doi and new_doi and existing_doi != new_doi:
                        if duplicate_found:
                            logger.debug(
                                f"COLLISION_CHECK | file={filename} | identical_content=False "
                                f"| same_doi=False | dedup_confirmed=True",
                                category=LogCategory.DEDUP,
                            )
                            break
                        logger.debug(
                            f"COLLISION_CHECK | file={filename} | identical_content=False "
                            f"| same_doi=False | dedup_confirmed=False "
                            f"| existing_doi={existing_doi} | new_doi={new_doi}",
                            category=LogCategory.DEDUP,
                        )

                    else:
                        existing_key = existing_entry.get("key", "").strip()
                        new_key = entry.get("key", "").strip()
                        if existing_key and new_key and existing_key == new_key:
                            logger.debug(
                                f"COLLISION_CHECK | file={filename} | identical_content=False "
                                f"| same_doi=False | key_match=True | key={existing_key}",
                                category=LogCategory.DEDUP,
                            )
                            break

                        existing_title = existing_fields.get("title", "")
                        new_title = new_fields.get("title", "")
                        sim = title_similarity(existing_title, new_title)
                        logger.debug(
                            f"COLLISION_CHECK | file={filename} | identical_content=False "
                            f"| same_doi=False | key_match=False | title_sim={sim:.3f}",
                            category=LogCategory.DEDUP,
                        )
                        if sim >= SIM_FILE_DUPLICATE_THRESHOLD:
                            break
        except OSError:
            pass

        # If we reach here, the file exists with different content.  This
        # should be rare (short_filename_for_entry adds more words to avoid
        # collisions), but can happen for very similar titles.  Log and
        # return the existing path instead of crashing the pipeline.
        collision_title = entry.get("fields", {}).get("title", "")
        collision_path = os.path.join(author_dir, filename)
        logger.warn(
            f"Filename collision for '{collision_title}' -- existing file kept: {collision_path}",
        )
        return os.path.join(author_dir, filename), False

    path = os.path.join(author_dir, filename)

    # Skip pre-write check when dedup already replaced the old file
    should_write = True
    if os.path.exists(path) and not dedup_replaced:
        try:
            with open(path, encoding="utf-8") as f:
                existing_content = f.read()

            existing_entry = parse_bibtex_to_dict(existing_content)

            if existing_entry:
                # Reuse existing citation key to prevent filename/key mismatches
                existing_key = existing_entry.get("key", "")
                if existing_key:
                    entry["key"] = existing_key

                existing_nonempty = {k: v for k, v in existing_entry.get("fields", {}).items() if v}
                new_nonempty = {k: v for k, v in entry.get("fields", {}).items() if v}

                existing_doi = _norm_doi(existing_nonempty.get("doi")) or ""
                upgrading_from_preprint = (
                    existing_doi and new_doi and _is_preprint_doi(existing_doi) and not _is_preprint_doi(new_doi)
                )

                # Keep existing if it has more fields (prevents downgrading),
                # UNLESS this is a preprint→published upgrade (field count may
                # drop because arXiv-specific fields like eprint are removed).
                if len(existing_nonempty) > len(new_nonempty) and not upgrading_from_preprint:
                    should_write = False

                if existing_doi and new_doi and not _is_preprint_doi(existing_doi) and _is_preprint_doi(new_doi):
                    should_write = False

                # Never overwrite a specific conference booktitle with a generic series name
                existing_bt = (existing_nonempty.get("booktitle") or "").lower().strip()
                new_bt = (new_nonempty.get("booktitle") or "").lower().strip()
                if existing_bt and existing_bt not in GENERIC_SERIES_NAMES and new_bt in GENERIC_SERIES_NAMES:
                    should_write = False
                logger.debug(
                    f"PREWRITE_CHECK | file={path} | should_write={should_write} "
                    f"| existing_fields={len(existing_nonempty)} new_fields={len(new_nonempty)} "
                    f"| existing_published_doi={bool(existing_doi and not _is_preprint_doi(existing_doi))} "
                    f"| new_preprint_doi={bool(new_doi and _is_preprint_doi(new_doi))}",
                    category=LogCategory.DEDUP,
                )
        except OSError:
            pass

    if prefer_path and os.path.abspath(prefer_path) != os.path.abspath(path):
        try:
            if os.path.exists(prefer_path):
                # Guard: don't remove prefer_path if it's more complete than what
                # we're about to write (prevents enriched file from being replaced
                # by an unenriched stub when Scholar returns duplicate entries)
                with open(prefer_path, encoding="utf-8") as pf:
                    prefer_entry = parse_bibtex_to_dict(pf.read())
                if prefer_entry:
                    pf_fields = sum(1 for v in prefer_entry.get("fields", {}).values() if v)
                    nf_fields = sum(1 for v in entry.get("fields", {}).values() if v)
                    pf_doi = (prefer_entry.get("fields", {}).get("doi") or "").strip()
                    if pf_fields > nf_fields or (pf_fields == nf_fields and pf_doi):
                        logger.debug(
                            f"FILE_CLEANUP_BLOCKED | prefer={os.path.basename(prefer_path)} "
                            f"({pf_fields} fields, doi) > new ({nf_fields} fields) | keeping enriched",
                            category=LogCategory.DEDUP,
                        )
                        return prefer_path, False
                logger.debug(f"FILE_CLEANUP | removed={prefer_path} | new_location={path}", category=LogCategory.DEDUP)
                os.remove(prefer_path)
        except OSError:
            pass

    # Cross-file citation key collision check: scan other files in the
    # directory for the same key.  If found on a DIFFERENT paper, append
    # a distinguishing suffix to the key to avoid LaTeX collisions.
    new_key = (entry.get("key") or "").strip()
    if should_write and new_key:
        for other_file in os.listdir(author_dir):
            if not other_file.endswith(".bib"):
                continue
            other_path = os.path.join(author_dir, other_file)
            if os.path.abspath(other_path) == os.path.abspath(path):
                continue
            try:
                with open(other_path, encoding="utf-8") as of:
                    other_entry = parse_bibtex_to_dict(of.read())
                if other_entry and other_entry.get("key", "").strip() == new_key:
                    # Same key in another file — check if genuinely different
                    other_doi = _norm_doi((other_entry.get("fields") or {}).get("doi"))
                    this_doi = _norm_doi(new_fields.get("doi"))
                    if other_doi and this_doi and other_doi == this_doi:
                        continue  # Same paper, key collision is fine
                    # Different paper — disambiguate key
                    _old_key = new_key
                    # Use first significant title word not in the other key
                    _title_words = re.findall(r"[A-Z][a-z]+", new_fields.get("title", ""))
                    _suffix = next((w for w in _title_words if w not in new_key), "B")
                    entry["key"] = f"{new_key}{_suffix}"
                    logger.debug(
                        f"KEY_COLLISION_FIX | old={_old_key} | new={entry['key']} | other_file={other_file}",
                        category=LogCategory.DEDUP,
                    )
                    break
            except OSError:
                continue

    if should_write:
        entry_type = entry.get("type", "misc")
        field_count = sum(1 for v in entry.get("fields", {}).values() if v)
        logger.debug(
            f"FILE_WRITE | path={path} | type={entry_type} | fields={field_count}",
            category=LogCategory.DEDUP,
        )
        final_content = bibtex_from_dict(entry)
        with open(path, "w", encoding="utf-8") as f:
            f.write(final_content)
    else:
        logger.debug(f"FILE_SKIP_WRITE | path={path} | reason=existing_more_complete", category=LogCategory.DEDUP)

    return path, should_write
