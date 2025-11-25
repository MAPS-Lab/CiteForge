from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from .bibtex_utils import short_filename_for_entry, bibtex_from_dict
from .config import TRUST_ORDER
from .id_utils import _norm_doi, extract_arxiv_eprint, allowlisted_url
from .text_utils import has_placeholder, format_author_dirname


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

    # only trust DOIs from reliable sources (not random snippets)
    if merged.get("doi"):
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
    previous path when possible, otherwise appends a counter to avoid collisions
    and can remove an obsolete file when the location changes.

    If a colliding filename already exists with identical content, it will be
    reused (overwritten) instead of creating a numbered duplicate.
    """
    author_dirname = format_author_dirname(author_name, author_id)
    author_dir = os.path.join(out_dir, author_dirname)
    os.makedirs(author_dir, exist_ok=True)

    base_filename = short_filename_for_entry(entry, gemini_api_key=gemini_api_key)
    filename = base_filename
    counter = 1

    # Render once for comparison
    new_content = bibtex_from_dict(entry)

    # avoid overwriting unless it's the file we wrote earlier or content is identical
    while os.path.exists(os.path.join(author_dir, filename)):
        existing_path = os.path.join(author_dir, filename)
        # ok to overwrite if this is the previous version
        if prefer_path and os.path.abspath(existing_path) == os.path.abspath(prefer_path):
            break
        # if content is identical, reuse this file (avoid creating -N duplicates)
        try:
            with open(existing_path, "r", encoding="utf-8") as ef:
                if ef.read() == new_content:
                    # Prefer canonical base filename when possible
                    break
        except OSError:
            pass
        # otherwise add a number
        name, ext = os.path.splitext(base_filename)
        filename = f"{name}-{counter}{ext}"
        counter += 1

    path = os.path.join(author_dir, filename)

    # clean up old file if we're moving to a new location
    if prefer_path and os.path.abspath(prefer_path) != os.path.abspath(path):
        try:
            if os.path.exists(prefer_path):
                os.remove(prefer_path)
        except OSError:
            pass

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return path
