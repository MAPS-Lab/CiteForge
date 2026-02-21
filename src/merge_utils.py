from __future__ import annotations

import contextlib
import html
import os
import re
from typing import Any

from .bibtex_utils import bibtex_from_dict, short_filename_for_entry
from .config import (
    DEDUP_INTERNAL_FIELDS,
    GENERIC_SERIES_NAMES,
    PAGES_MAX_DIGITS,
    PREPRINT_DOI_PREFIXES,
    PREPRINT_SERVERS,
    SIM_DEDUP_COMPOSITE_THRESHOLD,
    SIM_FILE_DUPLICATE_THRESHOLD,
    SIM_PREPRINT_TITLE_THRESHOLD,
    TITLE_LENGTH_KEEP_RATIO,
    TRUST_DIFF_OVERRIDE_THRESHOLD,
    TRUST_ORDER,
)
from .id_utils import _norm_doi, allowlisted_url, external_ids_match, extract_arxiv_eprint
from .text_utils import (
    author_overlap_ratio,
    authors_overlap,
    compute_dedup_score,
    format_author_dirname,
    has_placeholder,
    title_is_truncated_match,
    title_similarity,
)


def _is_preprint_doi(doi: str) -> bool:
    """Check if a DOI belongs to a preprint server (arXiv, Research Square, etc.)."""
    return any(doi.lower().startswith(p) for p in PREPRINT_DOI_PREFIXES)


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

    def value_ok(val: str | None) -> bool:
        return val is not None and not has_placeholder(val)

    for src, e in enrichers:
        if not e:
            continue
        ktype = (e.get("type") or "").lower()
        if (
            ktype in ("article", "inproceedings", "incollection")
            and type_rank.get(src, 99) < type_rank.get(best_type_src, 99)
        ):
            etype = ktype
            best_type_src = src

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
                continue
            if not value_ok(cur):
                merged[k] = v
                field_sources[k] = src
                continue

            # special handling for DOI field: prefer published DOIs over preprint DOIs
            if k == "doi":
                cur_is_preprint = _is_preprint_doi(str(cur))
                new_is_preprint = _is_preprint_doi(str(v))
                # if current is preprint DOI but new one isn't, always prefer published
                if cur_is_preprint and not new_is_preprint:
                    merged[k] = v
                    field_sources[k] = src
                    continue
                # if new is preprint DOI but current isn't, keep current
                if not cur_is_preprint and new_is_preprint:
                    continue

            # special handling for pages field: must be actual page numbers only
            if k == "pages":
                new_str = str(v).strip()
                # Validate: pages must start with a digit (page numbers only)
                if not re.match(r'^\d', new_str):
                    continue
                # Reject manuscript IDs containing dots (e.g., "2025.11.07.685935")
                if '.' in new_str:
                    continue
                # Reject article IDs masquerading as pages (SAGE/Wiley use long numeric IDs)
                # Check each page component individually so ranges like "13905-13917" pass
                parts = re.split(r'[-\u2013\u2014,\s]+', new_str)
                if any(len(re.sub(r'[^0-9]', '', p)) > PAGES_MAX_DIGITS for p in parts if p.strip()):
                    continue

            # special handling for journal field: never downgrade from published journal to preprint server
            if k == "journal":
                cur_journal_lower = str(cur).lower() if cur else ''
                new_journal_lower = str(v).lower()

                # Check if current is NOT a preprint but new IS a preprint
                cur_is_preprint = any(ps in cur_journal_lower for ps in PREPRINT_SERVERS)
                new_is_preprint = any(ps in new_journal_lower for ps in PREPRINT_SERVERS)

                # Never replace a published journal with a preprint server
                if not cur_is_preprint and new_is_preprint:
                    continue

            # special handling for title field: prefer longer, more descriptive titles
            if k == "title":
                cur_len = len(str(cur)) if cur else 0
                new_len = len(str(v))

                # If new title is significantly shorter (< 70% of current length),
                # only replace if it comes from a MUCH more trusted source
                # (at least 3 positions higher in trust order)
                if cur_len > 0 and new_len < (cur_len * TITLE_LENGTH_KEEP_RATIO):
                    trust_diff = type_rank.get(cur_src, 99) - type_rank.get(src, 99)
                    if trust_diff < TRUST_DIFF_OVERRIDE_THRESHOLD:
                        # New source isn't significantly more trusted, keep longer title
                        continue

            # special handling for booktitle: prefer specific conference name over generic series
            if k == "booktitle":
                cur_lower = str(cur).lower().strip() if cur else ""
                new_lower = str(v).lower().strip()
                # If current is a generic series name and new is more specific, always accept
                if cur_lower in GENERIC_SERIES_NAMES and new_lower not in GENERIC_SERIES_NAMES:
                    merged[k] = v
                    field_sources[k] = src
                    continue
                # Never replace a specific conference name with a generic series
                if cur_lower not in GENERIC_SERIES_NAMES and new_lower in GENERIC_SERIES_NAMES:
                    continue

            # only replace if new source is more trustworthy
            if type_rank.get(src, 99) < type_rank.get(cur_src, 99):
                merged[k] = v
                field_sources[k] = src

    # normalize DOI and drop if invalid
    doi_norm = _norm_doi(merged.get("doi"))
    if doi_norm:
        merged["doi"] = doi_norm
    else:
        merged.pop("doi", None)

    # Validate DOI consistency: if enrichers have contradicting DOIs, keep the primary
    # Different DOIs indicate different papers that should not be merged
    primary_doi = _norm_doi(primary.get("fields", {}).get("doi"))
    has_doi_conflict = False

    if primary_doi and merged.get("doi"):
        merged_doi_norm = _norm_doi(merged.get("doi"))
        if merged_doi_norm and merged_doi_norm != primary_doi:
            # Enricher has different DOI - they're different papers, keep primary
            merged["doi"] = primary_doi
            has_doi_conflict = True

    # only trust DOIs from reliable sources (not random snippets)
    # UNLESS there was a DOI conflict (in which case we already kept the primary)
    if merged.get("doi") and not has_doi_conflict:
        # Trust DOIs from DOI registration agencies and authoritative databases
        # DataCite: DOI registration agency for datasets/software
        # PubMed/Europe PMC: NIH and European government biomedical databases
        # Crossref: DOI registration agency for scholarly publications
        trusted_doi_sources = {"csl", "doi_bibtex", "datacite", "pubmed", "europepmc", "crossref"}
        merged_doi_norm = _norm_doi(merged.get("doi"))
        doi_is_trusted = False
        for src, e in enrichers:
            if src not in trusted_doi_sources or not e:
                continue
            source_doi_norm = _norm_doi((e.get("fields") or {}).get("doi"))
            if source_doi_norm and source_doi_norm == merged_doi_norm:
                doi_is_trusted = True
                break
        if not doi_is_trusted:
            merged.pop("doi", None)

    # remove internal tracking fields used only for dedup
    for field in DEDUP_INTERNAL_FIELDS:
        merged.pop(field, None)

    # normalize arXiv metadata to standard BibTeX fields
    from .id_utils import normalize_arxiv_metadata
    merged = normalize_arxiv_metadata(merged)

    # remove fields that should not be saved
    # keywords and copyright often come from DOI BibTeX responses but are not needed
    unwanted_fields = {"keywords", "copyright"}
    for field in unwanted_fields:
        merged.pop(field, None)

    # validate and clean pages field: must contain actual page numbers only
    pages_val = merged.get("pages", "")
    if pages_val:
        pages_str = str(pages_val).strip()
        if not re.match(r'^\d', pages_str) or '.' in pages_str:
            merged.pop("pages", None)
        else:
            # Check each page component individually (ranges like "13905-13917" are valid)
            parts = re.split(r'[-\u2013\u2014,\s]+', pages_str)
            if any(len(re.sub(r'[^0-9]', '', p)) > PAGES_MAX_DIGITS for p in parts if p.strip()):
                merged.pop("pages", None)

    # remove volume if it equals year (common error in conference proceedings)
    # Conference volumes are typically series numbers, not years
    year_val = merged.get("year", "")
    volume_val = merged.get("volume", "")
    if year_val and volume_val and str(year_val) == str(volume_val):
        merged.pop("volume", None)

    # clean journal names: remove descriptive suffixes from preprint servers
    # PubMed/Europe PMC add descriptive text like " : the preprint server for biology"
    journal_val = merged.get("journal", "")
    if journal_val:
        # Remove " : the preprint server for X" patterns
        journal_cleaned = re.sub(r'\s*:\s*the preprint server for [\w\s]+$', '', journal_val, flags=re.IGNORECASE)
        if journal_cleaned != journal_val:
            merged["journal"] = journal_cleaned.strip()

    # Fix publisher for bioRxiv/medRxiv papers (APIs sometimes return garbage
    # publisher names like "openRxiv" instead of "Cold Spring Harbor Laboratory")
    journal_lower = (merged.get("journal") or "").lower()
    if journal_lower in ("biorxiv", "medrxiv"):
        merged["publisher"] = "Cold Spring Harbor Laboratory"

    # decode HTML entities then strip HTML/XML tags from text fields
    # Decode first so encoded tags (&lt;b&gt;) become real tags (<b>) before stripping
    # Common tags from publishers: <scp>, <i>, <b>, <sup>, <sub>, <em>, <strong>
    # Common entities: &amp; → &, &lt; → <, etc.
    text_fields_to_clean = ["title", "journal", "booktitle", "series"]
    for field in text_fields_to_clean:
        field_val = merged.get(field, "")
        if field_val and isinstance(field_val, str):
            cleaned = html.unescape(field_val)
            cleaned = re.sub(r'<[^>]+>', '', cleaned)
            if cleaned != field_val:
                merged[field] = cleaned.strip()

    # strip trailing asterisks from title (footnote markers from some sources)
    title_val = merged.get("title", "")
    if title_val and isinstance(title_val, str) and title_val.rstrip() != title_val.rstrip().rstrip('*'):
        merged["title"] = title_val.rstrip().rstrip('*').rstrip()

    # remove PMID notes from PubMed/Europe PMC enrichment
    note_val = merged.get("note", "")
    if note_val and note_val.strip().startswith("PMID:"):
        merged.pop("note", None)

    # only keep URLs from trusted sources (DOI resolver or arXiv)
    url_val = (merged.get("url") or "").strip()
    allowed = allowlisted_url(url_val)
    if url_val and not allowed:
        merged.pop("url", None)
    elif allowed:
        merged["url"] = allowed

    # handle published papers with arXiv preprint: keep both DOI and eprint fields
    # for pure arXiv preprints with arXiv DOI, the eprint fields are the primary reference
    doi_val = merged.get("doi")
    arxiv_id = extract_arxiv_eprint({"fields": merged})

    # when a published DOI exists alongside arXiv, remove eprint fields
    # (DOI is the primary identifier for published papers)
    if doi_val and arxiv_id and not _is_preprint_doi(doi_val):
        # remove eprint fields since DOI is the primary identifier
        merged.pop("eprint", None)
        merged.pop("archiveprefix", None)
        merged.pop("primaryclass", None)

    # re-validate entry type based on venue content
    # enrichers can provide incorrect types, so always check venue keywords
    from .bibtex_build import determine_entry_type, get_container_field

    venue_type = determine_entry_type(
        {
            "journal": merged.get("journal"),
            "booktitle": merged.get("booktitle"),
            "howpublished": merged.get("howpublished"),
            "publisher": merged.get("publisher"),
            "pages": merged.get("pages")
        },
        type_field="type",
        venue_hints={}  # no hints - rely only on keyword detection
    )

    # if venue clearly indicates conference, override enricher type
    if venue_type == "inproceedings":
        etype = "inproceedings"
    # if venue clearly indicates book chapter, override enricher type
    elif venue_type == "incollection":
        etype = "incollection"
    elif etype == "misc":
        # for misc entries, use full logic with venue hints
        venue_type_with_hints = determine_entry_type(
            {
                "journal": merged.get("journal"),
                "booktitle": merged.get("booktitle"),
                "howpublished": merged.get("howpublished"),
                "publisher": merged.get("publisher"),
                "pages": merged.get("pages")
            },
            type_field="type",
            venue_hints={"journal": "article", "booktitle": "inproceedings"}
        )

        if venue_type_with_hints != "misc":
            etype = venue_type_with_hints
        else:
            # fallback to simple field presence check
            journal = (merged.get("journal") or "").strip()
            booktitle = (merged.get("booktitle") or "").strip()
            if journal:
                etype = "article"
            elif booktitle:
                etype = "inproceedings"

    # for book chapters, convert howpublished to booktitle if booktitle is missing
    if etype == "incollection" and not merged.get("booktitle") and merged.get("howpublished"):
        merged["booktitle"] = merged["howpublished"]
        merged.pop("howpublished", None)

    # enforce container field exclusivity per BibTeX standards
    expected_container = get_container_field(etype)

    if expected_container == "journal":
        merged.pop("booktitle", None)
        merged.pop("howpublished", None)
    elif expected_container == "booktitle":
        if merged.get("journal") and not merged.get("booktitle"):
            merged["booktitle"] = merged["journal"]
        merged.pop("journal", None)
        merged.pop("howpublished", None)

    return {"type": etype, "key": primary.get("key"), "fields": merged}


def save_entry_to_file(out_dir: str, author_id: str, entry: dict[str, Any], prefer_path: str | None = None,
                       gemini_api_key: str | None = None, author_name: str | None = None) -> str:
    """
    Write a BibTeX entry to disk inside an author-specific output directory,
    choosing a short descriptive filename from the entry fields. It reuses a
    previous path when possible and can remove an obsolete file when the location changes.

    For filename collisions with different publications, more words from the title
    are used to create a unique filename (never appending numeric counters).

    If a colliding filename already exists with identical content, it will be
    reused (overwritten) instead of creating a duplicate.
    """
    author_dirname = format_author_dirname(author_name, author_id)
    author_dir = os.path.join(out_dir, author_dirname)
    os.makedirs(author_dir, exist_ok=True)

    # Collect existing files to enable collision detection
    # Use sorted lists for deterministic iteration order
    existing_files_for_collision = set()
    existing_files_for_duplicate_scan_list = []

    if os.path.exists(author_dir):
        all_files = sorted(f for f in os.listdir(author_dir) if f.endswith('.bib'))
        existing_files_for_duplicate_scan_list = all_files

        # If prefer_path is provided, exclude it from collision avoidance only
        # but still check it for duplicate detection
        if prefer_path:
            prefer_filename = os.path.basename(prefer_path)
            existing_files_for_collision = set(all_files) - {prefer_filename}
        else:
            existing_files_for_collision = set(all_files)

    # Generate unique filename by checking against existing files (excluding prefer_path)
    # short_filename_for_entry will automatically use more words from the title if needed
    base_filename = short_filename_for_entry(
        entry, gemini_api_key=gemini_api_key, existing_files=existing_files_for_collision,
    )
    filename = base_filename

    # Render once for comparison
    new_content = bibtex_from_dict(entry)

    # First, check ALL existing files for duplicates (not just filename collisions)
    # This catches cases where Gemini/cache returns different short titles for same publication
    # Use sorted list for deterministic iteration order
    duplicate_found = False
    duplicate_path = None

    # Skip prefer_path in the duplicate scan: it's the file we're updating and
    # is already handled by the while-loop's prefer_path check.  Scanning it
    # first would hide a real preprint/published duplicate sitting in another file.
    prefer_basename = os.path.basename(prefer_path) if prefer_path else None

    for existing_filename in existing_files_for_duplicate_scan_list:
        if existing_filename == prefer_basename:
            continue
        existing_path = os.path.join(author_dir, existing_filename)
        try:
            with open(existing_path, encoding="utf-8") as ef:
                existing_content = ef.read()
                from . import bibtex_utils as bt
                existing_entry = bt.parse_bibtex_to_dict(existing_content)

                if existing_entry:
                    existing_fields = existing_entry.get('fields', {})
                    new_fields = entry.get('fields', {})

                    # Compare by DOI (most reliable)
                    existing_doi = existing_fields.get('doi', '').strip().lower()
                    new_doi = new_fields.get('doi', '').strip().lower()

                    # If both have DOIs and they're SAME, it's a duplicate
                    if existing_doi and new_doi and existing_doi == new_doi:
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # If both have DOIs and they're DIFFERENT, check for preprint/published pair
                    if existing_doi and new_doi and existing_doi != new_doi:
                        # Use composite scoring only for preprint↔published pairs
                        # (XOR, not OR) — two preprints with different DOIs are
                        # different papers; titles are minimally gated to prevent
                        # false positives on papers by the same research group
                        e_preprint = _is_preprint_doi(existing_doi)
                        n_preprint = _is_preprint_doi(new_doi)
                        if e_preprint != n_preprint:
                            e_title = existing_fields.get('title', '')
                            n_title = new_fields.get('title', '')
                            if title_similarity(e_title, n_title) >= SIM_PREPRINT_TITLE_THRESHOLD:
                                score = compute_dedup_score(existing_fields, new_fields)
                                if score >= SIM_DEDUP_COMPOSITE_THRESHOLD:
                                    duplicate_found = True
                                    duplicate_path = existing_path
                                    break
                        continue

                    # Only check citation key and title if DOIs don't contradict
                    # (either both missing, or only one present)

                    # External ID match (cluster_id, S2, OpenAlex)
                    if external_ids_match(existing_fields, new_fields):
                        existing_title = existing_fields.get('title', '')
                        new_title = new_fields.get('title', '')
                        sim = title_similarity(existing_title, new_title)
                        if sim >= SIM_PREPRINT_TITLE_THRESHOLD:
                            duplicate_found = True
                            duplicate_path = existing_path
                            break

                    # Get titles for comparison (used in both checks below)
                    existing_title = existing_fields.get('title', '')
                    new_title = new_fields.get('title', '')

                    # Compare by citation key - BUT also verify titles are similar
                    # This prevents false positives when Gemini generates the same
                    # short title for different papers (e.g., both "LexicalBiasResolution"
                    # for "Resolving Lexical Bias in Model Editing" and
                    # "Resolving Lexical Bias in Edit Scoping with Projector Editor Networks")
                    existing_key = existing_entry.get('key', '').strip()
                    new_key = entry.get('key', '').strip()
                    if existing_key and new_key and existing_key == new_key:
                        # Citation keys match - verify titles are actually similar
                        key_title_sim = title_similarity(existing_title, new_title)
                        if key_title_sim > SIM_FILE_DUPLICATE_THRESHOLD:
                            duplicate_found = True
                            duplicate_path = existing_path
                            break
                        # Keys match but titles differ significantly - NOT a duplicate
                        # (Gemini generated same short title for different papers)
                        continue

                    # Compare by title similarity alone
                    sim = title_similarity(existing_title, new_title)
                    if sim > SIM_FILE_DUPLICATE_THRESHOLD:
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # Truncated title fallback: Scholar sometimes returns titles
                    # cut short (e.g., "Passive Co-presence" vs full subtitle).
                    # Pure title_similarity gives a low score because fuzz_ratio
                    # penalizes length differences.  Catch this by checking if
                    # one title is a strict prefix of the other AND the authors
                    # overlap (to avoid matching unrelated papers with common
                    # title prefixes).
                    if title_is_truncated_match(existing_title, new_title) and authors_overlap(
                        existing_fields.get('author'), new_fields.get('author')
                    ):
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # Strong author overlap fallback: same multi-author team with
                    # moderate title similarity indicates a title variant
                    # (e.g., LNCS vs full conference name). Require multiple authors
                    # on both sides to avoid false positives from single-author papers.
                    if sim >= 0.6:
                        from .text_utils import parse_authors_any
                        e_authors = parse_authors_any(existing_fields.get('author', ''))
                        n_authors = parse_authors_any(new_fields.get('author', ''))
                        if len(e_authors) >= 2 and len(n_authors) >= 2:
                            overlap = author_overlap_ratio(
                                existing_fields.get('author'), new_fields.get('author')
                            )
                            if overlap >= 0.9:
                                score = compute_dedup_score(existing_fields, new_fields)
                                if score >= SIM_DEDUP_COMPOSITE_THRESHOLD:
                                    duplicate_found = True
                                    duplicate_path = existing_path
                                    break

                    # Preprint/published fallback: relaxed threshold when exactly
                    # one entry looks like a preprint (DOI prefix or journal name)
                    # and the other has evidence of being published (DOI or journal).
                    # Entries with neither DOI nor journal are unknown, not published.
                    if sim >= SIM_PREPRINT_TITLE_THRESHOLD:
                        e_journal = existing_fields.get('journal', '').lower()
                        n_journal = new_fields.get('journal', '').lower()
                        e_preprint = _is_preprint_doi(existing_doi) or any(ps in e_journal for ps in PREPRINT_SERVERS)
                        n_preprint = _is_preprint_doi(new_doi) or any(ps in n_journal for ps in PREPRINT_SERVERS)
                        if (e_preprint ^ n_preprint) and authors_overlap(
                            existing_fields.get('author'), new_fields.get('author')
                        ):
                            # The non-preprint side must have a DOI or journal
                            # to count as published — bare entries are unknown
                            published_has_evidence = (
                                (e_preprint and (new_doi or n_journal))
                                or (n_preprint and (existing_doi or e_journal))
                            )
                            if published_has_evidence:
                                duplicate_found = True
                                duplicate_path = existing_path
                                break
        except OSError:
            pass

    # If duplicate found in a different file, decide: keep existing, overwrite, or rename
    skip_write = False
    if duplicate_found and duplicate_path:
        try:
            with open(duplicate_path, encoding="utf-8") as ef:
                existing_content = ef.read()
            from . import bibtex_utils as bt
            existing_entry = bt.parse_bibtex_to_dict(existing_content)

            if existing_entry:
                existing_fields = existing_entry.get('fields', {})
                new_fields = entry.get('fields', {})
                existing_year = existing_fields.get('year', '')
                new_year = new_fields.get('year', '')
                existing_doi = existing_fields.get('doi', '').strip()
                new_doi = new_fields.get('doi', '').strip()

                # Check preprint/published relationship when DOIs differ
                existing_is_preprint = _is_preprint_doi(existing_doi) if existing_doi else False
                new_is_preprint = _is_preprint_doi(new_doi) if new_doi else False

                if existing_doi and new_doi and existing_doi != new_doi:
                    # Different DOIs: one is preprint, one is published (dedup already confirmed match)
                    if not existing_is_preprint and (new_is_preprint or not new_doi):
                        # Existing is published, new is preprint → keep published
                        skip_write = True
                        filename = os.path.basename(duplicate_path)
                    elif existing_is_preprint and not new_is_preprint:
                        # Existing is preprint, new is published → replace preprint
                        with contextlib.suppress(OSError):
                            os.remove(duplicate_path)
                        # Use new entry's generated filename (with correct metadata)
                    else:
                        # Both preprint or both published — keep the one with more fields
                        if len({k: v for k, v in existing_fields.items() if v}) >= len(
                            {k: v for k, v in new_fields.items() if v}
                        ):
                            skip_write = True
                            filename = os.path.basename(duplicate_path)
                        else:
                            with contextlib.suppress(OSError):
                                os.remove(duplicate_path)
                elif existing_doi and not new_doi:
                    # Existing has DOI, new doesn't → keep existing (more complete)
                    skip_write = True
                    filename = os.path.basename(duplicate_path)
                elif new_doi and not existing_doi:
                    # New has DOI, existing doesn't → replace existing
                    with contextlib.suppress(OSError):
                        os.remove(duplicate_path)
                elif existing_year and new_year and existing_year != new_year:
                    # Year changed, same or no DOIs — keep generated filename for year correction
                    pass
                else:
                    # Same year, same DOI (or both missing) → reuse existing filename
                    filename = os.path.basename(duplicate_path)
                    existing_key = existing_entry.get('key', '')
                    if existing_key:
                        entry['key'] = existing_key
            else:
                filename = os.path.basename(duplicate_path)
                if existing_entry and existing_entry.get('key'):
                    entry['key'] = existing_entry['key']
        except OSError:
            filename = os.path.basename(duplicate_path)

    # When the duplicate scan determined the existing file is the better version
    # (e.g., published entry vs incoming preprint), skip writing and return early
    if skip_write:
        # Clean up the baseline file that was written before enrichment
        if prefer_path and os.path.abspath(prefer_path) != os.path.abspath(duplicate_path or ""):
            with contextlib.suppress(OSError):
                if os.path.exists(prefer_path):
                    os.remove(prefer_path)
        return duplicate_path or os.path.join(author_dir, filename)

    # avoid overwriting unless it's the file we wrote earlier or content is identical
    while os.path.exists(os.path.join(author_dir, filename)):
        existing_path = os.path.join(author_dir, filename)
        # ok to overwrite if this is the previous version
        if prefer_path and os.path.abspath(existing_path) == os.path.abspath(prefer_path):
            break
        # if content is identical, reuse this file (avoid creating -N duplicates)
        # Compare with normalized trailing whitespace to handle newline differences
        try:
            with open(existing_path, encoding="utf-8") as ef:
                existing_content = ef.read()
                # Compare with rstrip to ignore trailing newline differences
                if existing_content.rstrip() == new_content.rstrip():
                    # Prefer canonical base filename when possible
                    break

                # Check if same publication by DOI or citation key (different metadata formatting)
                from . import bibtex_utils as bt
                existing_entry = bt.parse_bibtex_to_dict(existing_content)
                if existing_entry:
                    existing_fields = existing_entry.get('fields', {})
                    new_fields = entry.get('fields', {})

                    # Compare by DOI (most reliable)
                    existing_doi = existing_fields.get('doi', '').strip().lower()
                    new_doi = new_fields.get('doi', '').strip().lower()

                    # If both have DOIs and they're SAME, it's the same publication
                    if existing_doi and new_doi and existing_doi == new_doi:
                        # Same publication, overwrite with enriched version
                        break

                    # If both have DOIs and they're DIFFERENT, check if this is a
                    # known duplicate (preprint/published pair detected by dedup scan)
                    if existing_doi and new_doi and existing_doi != new_doi:
                        if duplicate_found:
                            # Dedup scan confirmed these are the same paper — allow overwrite
                            break
                        # Otherwise this is a genuine collision bug
                        pass  # Fall through to raise error

                    # Only check citation key and title if DOIs don't contradict
                    else:
                        # Compare by citation key as fallback
                        existing_key = existing_entry.get('key', '').strip()
                        new_key = entry.get('key', '').strip()
                        if existing_key and new_key and existing_key == new_key:
                            # Same publication, overwrite with enriched version
                            break

                        # Compare by Title Similarity
                        existing_title = existing_fields.get('title', '')
                        new_title = new_fields.get('title', '')
                        sim = title_similarity(existing_title, new_title)
                        if sim > SIM_FILE_DUPLICATE_THRESHOLD:
                            break
        except OSError:
            pass

        # If we reach here, it means the file exists but it's a different publication
        # This should never happen because short_filename_for_entry should have created
        # a unique filename by using more words from the title
        # If it does happen, it indicates a bug in the filename generation logic
        raise ValueError(
            f"Cannot save entry: filename '{filename}' already exists with different content. "
            f"This suggests the title '{entry.get('fields', {}).get('title', '')}' is too similar "
            f"to an existing publication. Please check for duplicate entries or title conflicts."
        )

    path = os.path.join(author_dir, filename)

    # If file exists, check which version is better before overwriting
    should_write = True
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing_content = f.read()

            from . import bibtex_utils as bt
            existing_entry = bt.parse_bibtex_to_dict(existing_content)

            if existing_entry:
                # IMPORTANT: Keep citation key synchronized with filename
                # If we're updating an existing file, reuse its citation key
                # to prevent filename/key mismatches
                existing_key = existing_entry.get('key', '')
                if existing_key:
                    entry['key'] = existing_key

                # Count non-empty fields in each entry
                existing_fields = {k: v for k, v in existing_entry.get('fields', {}).items() if v}
                new_fields = {k: v for k, v in entry.get('fields', {}).items() if v}

                # If existing has more fields, don't overwrite (keep better version)
                # This prevents downgrading enriched entries with minimal baseline data
                # Apply this check even for prefer_path updates to prevent failed enrichments
                # from downgrading existing good data
                if len(existing_fields) > len(new_fields):
                    should_write = False

                # Prefer published DOI over preprint DOI: never replace a
                # published entry with a preprint-only entry
                existing_doi = existing_fields.get('doi', '')
                new_doi = new_fields.get('doi', '')
                if existing_doi and new_doi and not _is_preprint_doi(existing_doi) and _is_preprint_doi(new_doi):
                    should_write = False
        except OSError:
            pass

    # clean up old file if we're moving to a new location
    if prefer_path and os.path.abspath(prefer_path) != os.path.abspath(path):
        try:
            if os.path.exists(prefer_path):
                os.remove(prefer_path)
        except OSError:
            pass

    if should_write:
        # Re-render content to ensure citation key matches any updates made
        final_content = bibtex_from_dict(entry)
        with open(path, "w", encoding="utf-8") as f:
            f.write(final_content)

    return path
