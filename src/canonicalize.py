from __future__ import annotations

import re
from enum import Enum
from typing import Any

from src import merge_utils as mu
from src.config import (
    ABBREVIATED_VENUE_MAP,
    ACM_JOURNAL_PROCEEDINGS,
    INSTITUTIONAL_REPOSITORIES,
    JOURNAL_ONLY_PREFIXES,
    JOURNALS_NAMED_PROCEEDINGS,
    PROCEEDINGS_SERIES_AS_JOURNAL,
    PUBLISHER_CORRECTIONS,
    REPOSITORY_AS_JOURNAL,
    VENUE_CASE_CORRECTIONS,
)
from src.fixup.text import _apply_booktitle_fixups, _fix_title_text


class CanonicalStage(Enum):
    LOAD_REPAIR = "load_repair"
    COMPLETE_SKIP_FINALIZE = "complete_skip_finalize"
    POST_MERGE = "post_merge"
    POST_TIER2_VALIDATE = "post_tier2_validate"
    POSTRUN_ORPHAN_REPAIR = "postrun_orphan_repair"


# Pre-compiled patterns for three-way title/venue fixups (used in _fixup_bib_entry,
# existing-file fixup, and Phase 4 post-merge — each pattern appears 3 times)
_TRAILING_DASH_RE = re.compile(r"[\s][-\u2013]\s*$")
_SUBTITLE_WRAPPER_RE = re.compile(r":\s*-([^-]+)-\s*$")
_SPIRE_STRIP_RE = re.compile(r"\s*:\s*SPIRE\b.*$")
_OSF_DOI_RE = re.compile(r"^10\.31(219|234)/")
_DOI_VERSION_RE = re.compile(r"_v\d+$")
_LIPICS_PAGES_STRIP_RE = re.compile(r",\s*\d+:\s*\d+-\d+:\s*\d+\s*$")
_LIPICS_PAGES_EXTRACT_RE = re.compile(r",\s*(\d+:\s*\d+-\d+:\s*\d+)\s*$")
_PREPRINT_MARKER_RE = re.compile(r"\s*\[preprint\]\s*$", re.IGNORECASE)
_GECCO_RE = re.compile(r"\bgenetic and evolutionary computation conference\b", re.IGNORECASE)
_URL_IN_VENUE_RE = re.compile(r"https?://")
_URL_IN_VENUE_STRIP_RE = re.compile(r",?\s*https?://\S+")
_BOOK_CHAPTER_DOI_RE = re.compile(r"\.ch\d+$")

# Repeated string literals used in the three-way fix pattern
_GECCO_LOWER = "genetic and evolutionary computation conference"
_GECCO_PROPER = "Genetic and Evolutionary Computation Conference"
_ZENTRUM_FUR = "Zentrum fur Informatik"
_ZENTRUM_FUER = 'Zentrum f{\\"u}r Informatik'
_PROC_EXT_ABSTRACTS = "Proceedings of the Extended Abstracts"
_PROC_OF_THE = "Proceedings of the "


def _fixup_bib_entry(entry: dict[str, Any]) -> bool:
    """Apply entry type and field corrections to a parsed BibTeX entry.

    Returns True if any changes were made.
    Used by both the per-article fixup and the post-run orphan fixup.
    """
    fields = entry.get("fields") or {}
    changed = False
    etype = entry.get("type", "")

    # Reclassify @article with Procedia/IFAC series → @inproceedings
    if etype == "article" and fields.get("journal"):
        jnl_lower = fields["journal"].strip().lower()
        if any(jnl_lower.startswith(ps) for ps in PROCEEDINGS_SERIES_AS_JOURNAL):
            fields["booktitle"] = fields.pop("journal")
            entry["type"] = "inproceedings"
            changed = True

    # Reclassify @inproceedings with PACM journal → @article
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        if any(bt_lower.startswith(pj) or bt_lower == pj for pj in ACM_JOURNAL_PROCEEDINGS):
            fields["journal"] = fields.pop("booktitle")
            entry["type"] = "article"
            changed = True

    # Reclassify @inproceedings with PNAS/PVLDB journal → @article
    # Guard: skip if the booktitle extends the journal name with conference keywords
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        jnp_match = next((j for j in JOURNALS_NAMED_PROCEEDINGS if bt_lower.startswith(j)), None)
        if jnp_match:
            suffix = bt_lower[len(jnp_match) :].lstrip(" /,")
            if not any(kw in suffix for kw in ("conference", "workshop", "symposium")):
                fields["journal"] = fields.pop("booktitle")
                entry["type"] = "article"
                changed = True

    # Reclassify @inproceedings with institutional repository → @phdthesis
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        if any(ir in bt_lower for ir in INSTITUTIONAL_REPOSITORIES):
            fields["school"] = fields.pop("booktitle")
            entry["type"] = "phdthesis"
            changed = True

    # Downgrade @inproceedings with repository as booktitle → @misc
    if (
        entry.get("type") == "inproceedings"
        and fields.get("booktitle")
        and any(rj in fields["booktitle"].lower() for rj in REPOSITORY_AS_JOURNAL)
    ):
        entry["type"] = "misc"
        fields.pop("booktitle", None)
        changed = True

    # Downgrade @article with repository as journal → @misc
    if (
        entry.get("type") == "article"
        and fields.get("journal")
        and any(rj in fields["journal"].lower() for rj in REPOSITORY_AS_JOURNAL)
    ):
        entry["type"] = "misc"
        fields.pop("journal", None)
        changed = True

    # Downgrade @inproceedings with "Preprint" as booktitle → @misc
    if (
        entry.get("type") == "inproceedings"
        and fields.get("booktitle")
        and fields["booktitle"].strip().lower() == "preprint"
    ):
        entry["type"] = "misc"
        fields.pop("booktitle", None)
        changed = True

    # Reclassify @article with university name as journal → @phdthesis
    if entry.get("type") == "article" and fields.get("journal"):
        _jnl_lower = fields["journal"].strip().lower()
        if "university" in _jnl_lower or "institut" in _jnl_lower:
            fields["school"] = fields.pop("journal")
            entry["type"] = "phdthesis"
            changed = True

    # Reclassify @inproceedings with journal name as booktitle → @article
    # (but NOT for PROCEEDINGS_SERIES_AS_JOURNAL which are genuinely proceedings)
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        is_proc_series = any(bt_lower.startswith(ps) for ps in PROCEEDINGS_SERIES_AS_JOURNAL)
        if not is_proc_series and any(bt_lower.startswith(jp) for jp in JOURNAL_ONLY_PREFIXES):
            fields["journal"] = fields.pop("booktitle")
            entry["type"] = "article"
            changed = True

    # Reclassify @inproceedings with "Handbook" in booktitle → @incollection
    if entry.get("type") == "inproceedings" and fields.get("booktitle") and "handbook" in fields["booktitle"].lower():
        entry["type"] = "incollection"
        changed = True

    # Reclassify @article with book-chapter DOI pattern → @incollection
    if (
        entry.get("type") == "article"
        and fields.get("journal")
        and fields.get("doi")
        and _BOOK_CHAPTER_DOI_RE.search(fields["doi"].strip())
    ):
        fields["booktitle"] = fields.pop("journal")
        entry["type"] = "incollection"
        changed = True

    # Reclassify @article with conference proceedings in journal → @inproceedings
    # (but NOT for ACM PACM journals which are legitimately named "Proceedings of...")
    if entry.get("type") == "article" and fields.get("journal"):
        jnl = fields["journal"].strip()
        jnl_lower = jnl.lower()
        is_pacm = any(jnl_lower.startswith(pj) or jnl_lower == pj for pj in ACM_JOURNAL_PROCEEDINGS)
        if mu._is_conference_journal(jnl) and not fields.get("booktitle") and not is_pacm:
            fields["booktitle"] = fields.pop("journal")
            entry["type"] = "inproceedings"
            changed = True

    # Strip [preprint] marker from title
    title = fields.get("title", "")
    if isinstance(title, str) and _PREPRINT_MARKER_RE.search(title):
        fields["title"] = _PREPRINT_MARKER_RE.sub("", title).strip()
        changed = True

    # Fix fused compounds, colon-space, hyphen-space, and acronym case
    title = fields.get("title", "")
    if isinstance(title, str) and title:
        fixed_title = _fix_title_text(title)
        if fixed_title != title:
            fields["title"] = fixed_title
            changed = True

    # Apply booktitle cleanup patterns (verbose metadata strip, abbreviations, typos, spacing)
    bt_fix = (fields.get("booktitle") or "").strip()
    if bt_fix:
        bt_fixed = _apply_booktitle_fixups(bt_fix)
        if bt_fixed != bt_fix:
            fields["booktitle"] = bt_fixed
            changed = True

    # Strip URLs embedded in booktitle/journal
    for url_field in ("booktitle", "journal"):
        url_val = (fields.get(url_field) or "").strip()
        if url_val and _URL_IN_VENUE_RE.search(url_val):
            url_cleaned = _URL_IN_VENUE_STRIP_RE.sub("", url_val).strip().rstrip(",")
            if url_cleaned and url_cleaned != url_val:
                fields[url_field] = url_cleaned
                changed = True

    # Apply publisher corrections
    pub_journal = (fields.get("journal") or "").lower()
    if pub_journal:
        for jnl_key, correct_pub in PUBLISHER_CORRECTIONS.items():
            if jnl_key in pub_journal:
                cur_pub = fields.get("publisher", "")
                if cur_pub and cur_pub != correct_pub:
                    fields["publisher"] = correct_pub
                    changed = True

    # Strip publisher when it duplicates the journal/booktitle name
    pub = (fields.get("publisher") or "").strip()
    container = (fields.get("journal") or fields.get("booktitle") or "").strip()
    if pub and container and pub.lower() == container.lower():
        del fields["publisher"]
        changed = True

    # Expand abbreviated venue names in booktitle (e.g., "NIME 2021" → full name)
    bt = (fields.get("booktitle") or "").strip()
    if bt and bt.lower() in ABBREVIATED_VENUE_MAP:
        _expanded_bt = ABBREVIATED_VENUE_MAP[bt.lower()]
        if _expanded_bt != bt:
            fields["booktitle"] = _expanded_bt
            changed = True

    # Correct ALL-CAPS venue names to proper case
    for _vcf in ("journal", "booktitle"):
        _vc_val = (fields.get(_vcf) or "").strip()
        if _vc_val and _vc_val in VENUE_CASE_CORRECTIONS:
            _corrected_vc = VENUE_CASE_CORRECTIONS[_vc_val]
            if _corrected_vc != _vc_val:
                fields[_vcf] = _corrected_vc
                changed = True
        # Fix lowercase _GECCO_LOWER in GECCO booktitles
        elif _vc_val and _GECCO_LOWER in _vc_val.lower():
            _vc_fixed = _GECCO_RE.sub(_GECCO_PROPER, _vc_val)
            if _vc_fixed != _vc_val:
                fields[_vcf] = _vc_fixed
                changed = True

    # Strip trailing " -" or en-dash from booktitle/title (truncation artifact)
    for _td_field in ("booktitle", "title"):
        _td_val = (fields.get(_td_field) or "").strip()
        if _td_val and _TRAILING_DASH_RE.search(_td_val):
            fields[_td_field] = _TRAILING_DASH_RE.sub("", _td_val)
            changed = True

    # Strip ": -...-" subtitle wrapper artifact from title
    title = fields.get("title", "")
    if isinstance(title, str) and ": -" in title:
        cleaned = _SUBTITLE_WRAPPER_RE.sub(r": \1", title)
        if cleaned != title:
            fields["title"] = cleaned
            changed = True

    # Strip SPIRE-style proceedings garbage suffix from booktitle
    bt_spire = (fields.get("booktitle") or "").strip()
    if bt_spire:
        bt_cleaned = _SPIRE_STRIP_RE.sub("", bt_spire)
        if bt_cleaned != bt_spire:
            fields["booktitle"] = bt_cleaned
            changed = True

    # Strip _v[N] version suffix from OSF/PsyArXiv DOIs
    doi_val = (fields.get("doi") or "").strip()
    if doi_val and _OSF_DOI_RE.match(doi_val):
        doi_stripped = _DOI_VERSION_RE.sub("", doi_val)
        if doi_stripped != doi_val:
            fields["doi"] = doi_stripped
            changed = True
            # Also fix the URL if it contains the versioned DOI
            url_val = (fields.get("url") or "").strip()
            if url_val and doi_val in url_val:
                fields["url"] = url_val.replace(doi_val, doi_stripped)

    # Remove pages field that contains no digits (location strings, not page numbers)
    pg = (fields.get("pages") or "").strip()
    if pg and not re.search(r"\d", pg):
        del fields["pages"]
        changed = True

    # Fix Schloss Dagstuhl missing umlaut ("fur" → "f{\"u}r")
    pub = (fields.get("publisher") or "").strip()
    if _ZENTRUM_FUR in pub:
        fields["publisher"] = pub.replace(_ZENTRUM_FUR, _ZENTRUM_FUER)
        changed = True

    # Strip page numbers embedded in booktitle (e.g., ", 17: 1-17: 18" from LIPIcs)
    bt_pages = (fields.get("booktitle") or "").strip()
    if bt_pages:
        bt_clean = _LIPICS_PAGES_STRIP_RE.sub("", bt_pages)
        if bt_clean != bt_pages:
            fields["booktitle"] = bt_clean
            if not fields.get("pages"):
                pages_match = _LIPICS_PAGES_EXTRACT_RE.search(bt_pages)
                if pages_match:
                    fields["pages"] = pages_match.group(1).replace(" ", "")
            changed = True

    # Strip duplicate "Proceedings of the" wrapper from booktitle
    bt_dup = (fields.get("booktitle") or "").strip()
    if bt_dup.startswith(_PROC_EXT_ABSTRACTS):
        fields["booktitle"] = bt_dup.removeprefix(_PROC_OF_THE)
        changed = True

    # Add URL from DOI when missing
    doi = (fields.get("doi") or "").strip()
    if doi and not fields.get("url"):
        fields["url"] = f"https://doi.org/{doi}"
        changed = True

    return changed
