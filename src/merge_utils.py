from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .bibtex_utils import short_filename_for_entry, bibtex_from_dict
from .config import TRUST_ORDER
from .id_utils import _norm_doi, extract_arxiv_eprint, allowlisted_url
from .text_utils import has_placeholder, format_author_dirname, title_similarity


def merge_with_policy(primary: Dict[str, Any], enrichers: List[Tuple[str, Dict[str, Any]]]) -> Dict[str, Any]:
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

    def value_ok(val: Optional[str]) -> bool:
        return val is not None and not has_placeholder(val)

    for src, e in enrichers:
        if not e:
            continue
        ktype = (e.get("type") or "").lower()
        if ktype in ("article", "inproceedings", "incollection"):
            if type_rank.get(src, 99) < type_rank.get(best_type_src, 99):
                etype = ktype
                best_type_src = src

    # track where each field came from to know when to replace
    field_sources: Dict[str, str] = {k: "scholar_min" for k in fields}
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

            # special handling for DOI field: prefer non-arXiv DOIs over arXiv DOIs
            if k == "doi":
                cur_is_arxiv = bool(re.search(r'10\.48550/arxiv', str(cur), re.IGNORECASE))
                new_is_arxiv = bool(re.search(r'10\.48550/arxiv', str(v), re.IGNORECASE))
                # if current is arXiv DOI but new one isn't, always prefer the non-arXiv DOI
                if cur_is_arxiv and not new_is_arxiv:
                    merged[k] = v
                    field_sources[k] = src
                    continue
                # if new is arXiv DOI but current isn't, keep current
                if not cur_is_arxiv and new_is_arxiv:
                    continue

            # special handling for pages field: must be actual page numbers only
            if k == "pages":
                new_str = str(v)
                # Validate: pages must start with a digit (page numbers only)
                if not re.match(r'^\d', new_str):
                    # New value is not a valid page number (starts with non-digit)
                    continue

            # special handling for journal field: never downgrade from published journal to preprint server
            if k == "journal":
                # List of preprint servers (not peer-reviewed journals)
                preprint_servers = {
                    'arxiv', 'biorxiv', 'medrxiv', 'chemrxiv', 'research square',
                    'ssrn', 'preprints', 'psyarxiv', 'socarxiv', 'edarxiv',
                    'arxiv e-prints', 'e-prints', 'preprint'
                }
                cur_journal_lower = str(cur).lower() if cur else ''
                new_journal_lower = str(v).lower()

                # Check if current is NOT a preprint but new IS a preprint
                cur_is_preprint = any(ps in cur_journal_lower for ps in preprint_servers)
                new_is_preprint = any(ps in new_journal_lower for ps in preprint_servers)

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
                if cur_len > 0 and new_len < (cur_len * 0.7):
                    trust_diff = type_rank.get(cur_src, 99) - type_rank.get(src, 99)
                    if trust_diff < 3:
                        # New source isn't significantly more trusted, keep longer title
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

    # remove internal tracking fields
    merged.pop("x_scholar_citation_id", None)

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
    if pages_val and not re.match(r'^\d', str(pages_val)):
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

    # strip HTML/XML tags from text fields (prevents LaTeX compilation errors)
    # Common tags from publishers: <scp>, <i>, <b>, <sup>, <sub>, <em>, <strong>
    text_fields_to_clean = ["title", "journal", "booktitle", "series"]
    for field in text_fields_to_clean:
        field_val = merged.get(field, "")
        if field_val and isinstance(field_val, str):
            # Remove HTML/XML tags: <tag>, </tag>, <tag attr="value">
            cleaned = re.sub(r'<[^>]+>', '', field_val)
            if cleaned != field_val:
                merged[field] = cleaned.strip()

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
    if doi_val and arxiv_id and not re.search(r'10\.48550/arxiv', doi_val, re.IGNORECASE):
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
    if etype == "incollection":
        if not merged.get("booktitle") and merged.get("howpublished"):
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


def save_entry_to_file(out_dir: str, author_id: str, entry: Dict[str, Any], prefer_path: Optional[str] = None,
                       gemini_api_key: Optional[str] = None, author_name: Optional[str] = None) -> str:
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
    existing_files_for_collision = set()
    existing_files_for_duplicate_scan = set()

    if os.path.exists(author_dir):
        all_files = {f for f in os.listdir(author_dir) if f.endswith('.bib')}
        existing_files_for_duplicate_scan = all_files

        # If prefer_path is provided, exclude it from collision avoidance only
        # but still check it for duplicate detection
        if prefer_path:
            prefer_filename = os.path.basename(prefer_path)
            existing_files_for_collision = all_files - {prefer_filename}
        else:
            existing_files_for_collision = all_files

    # Generate unique filename by checking against existing files (excluding prefer_path)
    # short_filename_for_entry will automatically use more words from the title if needed
    base_filename = short_filename_for_entry(entry, gemini_api_key=gemini_api_key, existing_files=existing_files_for_collision)
    filename = base_filename

    # Render once for comparison
    new_content = bibtex_from_dict(entry)

    # First, check ALL existing files for duplicates (not just filename collisions)
    # This catches cases where Gemini/cache returns different short titles for same publication
    duplicate_found = False
    duplicate_path = None

    for existing_filename in existing_files_for_duplicate_scan:
        existing_path = os.path.join(author_dir, existing_filename)
        try:
            with open(existing_path, "r", encoding="utf-8") as ef:
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

                    # If both have DOIs and they're DIFFERENT, NOT a duplicate (different papers)
                    # Skip ALL other checks (citation key, title) since DOIs are authoritative
                    if existing_doi and new_doi and existing_doi != new_doi:
                        continue

                    # Only check citation key and title if DOIs don't contradict
                    # (either both missing, or only one present)

                    # Compare by citation key
                    existing_key = existing_entry.get('key', '').strip()
                    new_key = entry.get('key', '').strip()
                    if existing_key and new_key and existing_key == new_key:
                        duplicate_found = True
                        duplicate_path = existing_path
                        break

                    # Compare by title similarity
                    existing_title = existing_fields.get('title', '')
                    new_title = new_fields.get('title', '')
                    sim = title_similarity(existing_title, new_title)
                    if sim > 0.9:
                        duplicate_found = True
                        duplicate_path = existing_path
                        break
        except OSError:
            pass

    # If duplicate found in a different file, check if we should rename due to metadata changes
    if duplicate_found and duplicate_path:
        # Check if year changed (year corrections should trigger rename)
        try:
            with open(duplicate_path, "r", encoding="utf-8") as ef:
                existing_content = ef.read()
            from . import bibtex_utils as bt
            existing_entry = bt.parse_bibtex_to_dict(existing_content)

            if existing_entry:
                existing_year = existing_entry.get('fields', {}).get('year', '')
                new_year = entry.get('fields', {}).get('year', '')

                # If year changed, use new filename (don't reuse old one)
                if existing_year and new_year and existing_year != new_year:
                    # Keep the generated filename with correct year
                    pass
                else:
                    # Year unchanged, reuse existing filename
                    filename = os.path.basename(duplicate_path)
            else:
                filename = os.path.basename(duplicate_path)
        except OSError:
            filename = os.path.basename(duplicate_path)

    # avoid overwriting unless it's the file we wrote earlier or content is identical
    while os.path.exists(os.path.join(author_dir, filename)):
        existing_path = os.path.join(author_dir, filename)
        # ok to overwrite if this is the previous version
        if prefer_path and os.path.abspath(existing_path) == os.path.abspath(prefer_path):
            break
        # if content is identical, reuse this file (avoid creating -N duplicates)
        # Compare with normalized trailing whitespace to handle newline differences
        try:
            with open(existing_path, "r", encoding="utf-8") as ef:
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

                    # If both have DOIs and they're DIFFERENT, NOT the same paper
                    # This should never happen because short_filename_for_entry should have
                    # created a unique filename. If it does, it's an error.
                    if existing_doi and new_doi and existing_doi != new_doi:
                        # Different papers with same filename - this is a bug
                        pass  # Fall through to raise error

                    # Only check citation key and title if DOIs don't contradict
                    elif existing_key or new_key:
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
                        if sim > 0.9:
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
            with open(path, "r", encoding="utf-8") as f:
                existing_content = f.read()

            from . import bibtex_utils as bt
            existing_entry = bt.parse_bibtex_to_dict(existing_content)

            if existing_entry:
                # Count non-empty fields in each entry
                existing_fields = {k: v for k, v in existing_entry.get('fields', {}).items() if v}
                new_fields = {k: v for k, v in entry.get('fields', {}).items() if v}

                # If existing has more fields, don't overwrite (keep better version)
                # This prevents downgrading enriched entries with minimal baseline data
                # Apply this check even for prefer_path updates to prevent failed enrichments
                # from downgrading existing good data
                if len(existing_fields) > len(new_fields):
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
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)

    return path
