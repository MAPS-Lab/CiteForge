from __future__ import annotations

import re
from enum import Enum
from typing import Any

from src import id_utils as idu
from src import merge_utils as mu
from src.config import (
    ABBREVIATED_VENUE_MAP,
    ACM_JOURNAL_PROCEEDINGS,
    INSTITUTIONAL_REPOSITORIES,
    JOURNAL_ONLY_PREFIXES,
    JOURNALS_NAMED_PROCEEDINGS,
    PREPRINT_SERVERS,
    PROCEEDINGS_SERIES_AS_JOURNAL,
    PUBLISHER_CORRECTIONS,
    REPOSITORY_AS_JOURNAL,
    VENUE_CASE_CORRECTIONS,
)
from src.fixup.text import _apply_booktitle_fixups, _fix_title_text
from src.publication_parser import _strip_ellipsis
from src.text_utils import trim_title_default


class CanonicalStage(Enum):
    LOAD_REPAIR = "load_repair"
    COMPLETE_SKIP_FINALIZE = "complete_skip_finalize"
    POST_MERGE = "post_merge"
    POST_TIER2_VALIDATE = "post_tier2_validate"
    POSTRUN_ORPHAN_REPAIR = "postrun_orphan_repair"


# Pre-compiled patterns for the multi-site title/venue fixups. These rule bodies
# are single-sourced as helper functions below and dispatched per CanonicalStage
# by canonicalize(); _fixup_bib_entry (Site A / orphan repair) and the Phase-4
# post-merge block (Site C) share the SAME helper bodies.
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
_US_PATENT_RE = re.compile(r"(?i)^US\s+Patent")
_BRACKET_J_RE = re.compile(r"\s*\[J\]\s*$")
_URL_BOOKTITLE_RE = re.compile(r"^https?://|^[\w.-]+\.(com|org|net|io|press)\b", re.IGNORECASE)

# Repeated string literals used in the shared fixups
_GECCO_LOWER = "genetic and evolutionary computation conference"
_GECCO_PROPER = "Genetic and Evolutionary Computation Conference"
_ZENTRUM_FUR = "Zentrum fur Informatik"
_ZENTRUM_FUER = 'Zentrum f{\\"u}r Informatik'
_PROC_EXT_ABSTRACTS = "Proceedings of the Extended Abstracts"
_PROC_OF_THE = "Proceedings of the "

# Preprint howpublished names checked by the misc->inproceedings upgrade (R20).
_R20_PREPRINT_HOWPUBLISHED = (
    "arxiv",
    "biorxiv",
    "medrxiv",
    "chemrxiv",
    "techrxiv",
    "ssrn",
    "ssrn electronic journal",
    "research square",
    "preprints.org",
    "authorea",
    "osf preprints",
    "openrxiv",
    "psyarxiv",
    "socarxiv",
    "edarxiv",
)


# ---------------------------------------------------------------------------
# Shared reclassification rules (used by BOTH Site A and Site C)
# ---------------------------------------------------------------------------
def _rule_procedia_to_inproceedings(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with a Procedia/IFAC proceedings-series journal -> @inproceedings."""
    if entry.get("type") == "article" and fields.get("journal"):
        jnl_lower = fields["journal"].strip().lower()
        if any(jnl_lower.startswith(ps) for ps in PROCEEDINGS_SERIES_AS_JOURNAL):
            fields["booktitle"] = fields.pop("journal")
            entry["type"] = "inproceedings"
            return True
    return False


def _rule_pacm_booktitle_to_article(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with a PACM journal as booktitle -> @article."""
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        if any(bt_lower.startswith(pj) or bt_lower == pj for pj in ACM_JOURNAL_PROCEEDINGS):
            fields["journal"] = fields.pop("booktitle")
            entry["type"] = "article"
            return True
    return False


def _rule_named_proceedings_to_article(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with a PNAS/PVLDB-style journal as booktitle -> @article.

    Guard: skip when the booktitle extends the journal name with conference keywords.
    """
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        jnp_match = next((j for j in JOURNALS_NAMED_PROCEEDINGS if bt_lower.startswith(j)), None)
        if jnp_match:
            suffix = bt_lower[len(jnp_match) :].lstrip(" /,")
            if not any(kw in suffix for kw in ("conference", "workshop", "symposium")):
                fields["journal"] = fields.pop("booktitle")
                entry["type"] = "article"
                return True
    return False


def _rule_institutional_repo_to_phdthesis(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with an institutional repository as booktitle -> @phdthesis."""
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        if any(ir in bt_lower for ir in INSTITUTIONAL_REPOSITORIES):
            fields["school"] = fields.pop("booktitle")
            entry["type"] = "phdthesis"
            return True
    return False


def _rule_repo_booktitle_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with a repository/portal as booktitle -> @misc."""
    if (
        entry.get("type") == "inproceedings"
        and fields.get("booktitle")
        and any(rj in fields["booktitle"].lower() for rj in REPOSITORY_AS_JOURNAL)
    ):
        entry["type"] = "misc"
        fields.pop("booktitle", None)
        return True
    return False


def _rule_repo_journal_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with a repository/portal as journal -> @misc."""
    if (
        entry.get("type") == "article"
        and fields.get("journal")
        and any(rj in fields["journal"].lower() for rj in REPOSITORY_AS_JOURNAL)
    ):
        entry["type"] = "misc"
        fields.pop("journal", None)
        return True
    return False


def _rule_preprint_booktitle_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with "Preprint" as booktitle -> @misc."""
    if (
        entry.get("type") == "inproceedings"
        and fields.get("booktitle")
        and fields["booktitle"].strip().lower() == "preprint"
    ):
        entry["type"] = "misc"
        fields.pop("booktitle", None)
        return True
    return False


def _rule_university_to_phdthesis(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with a university/institute name as journal -> @phdthesis."""
    if entry.get("type") == "article" and fields.get("journal"):
        jnl_lower = fields["journal"].strip().lower()
        if "university" in jnl_lower or "institut" in jnl_lower:
            fields["school"] = fields.pop("journal")
            entry["type"] = "phdthesis"
            return True
    return False


def _rule_journal_prefix_to_article(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with a journal name as booktitle -> @article.

    But NOT for PROCEEDINGS_SERIES_AS_JOURNAL which are genuinely proceedings.
    """
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_lower = fields["booktitle"].strip().lower()
        is_proc_series = any(bt_lower.startswith(ps) for ps in PROCEEDINGS_SERIES_AS_JOURNAL)
        if not is_proc_series and any(bt_lower.startswith(jp) for jp in JOURNAL_ONLY_PREFIXES):
            fields["journal"] = fields.pop("booktitle")
            entry["type"] = "article"
            return True
    return False


def _rule_handbook_to_incollection(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with "Handbook" in booktitle -> @incollection."""
    if entry.get("type") == "inproceedings" and fields.get("booktitle") and "handbook" in fields["booktitle"].lower():
        entry["type"] = "incollection"
        return True
    return False


def _rule_book_chapter_doi_to_incollection(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with a book-chapter DOI pattern (.chNN) -> @incollection."""
    if (
        entry.get("type") == "article"
        and fields.get("journal")
        and fields.get("doi")
        and _BOOK_CHAPTER_DOI_RE.search(fields["doi"].strip())
    ):
        fields["booktitle"] = fields.pop("journal")
        entry["type"] = "incollection"
        return True
    return False


def _rule_conference_journal_to_inproceedings(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with conference proceedings in journal -> @inproceedings.

    But NOT for ACM PACM journals which are legitimately named "Proceedings of...".
    """
    if entry.get("type") == "article" and fields.get("journal"):
        jnl = fields["journal"].strip()
        jnl_lower = jnl.lower()
        is_pacm = any(jnl_lower.startswith(pj) or jnl_lower == pj for pj in ACM_JOURNAL_PROCEEDINGS)
        if mu._is_conference_journal(jnl) and not fields.get("booktitle") and not is_pacm:
            fields["booktitle"] = fields.pop("journal")
            entry["type"] = "inproceedings"
            return True
    return False


# ---------------------------------------------------------------------------
# Shared text / venue normalization rules (used by BOTH Site A and Site C)
# ---------------------------------------------------------------------------
def _rule_booktitle_fixups(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Apply booktitle cleanup patterns (verbose metadata strip, typos, spacing)."""
    bt_fix = (fields.get("booktitle") or "").strip()
    if bt_fix:
        bt_fixed = _apply_booktitle_fixups(bt_fix)
        if bt_fixed != bt_fix:
            fields["booktitle"] = bt_fixed
            return True
    return False


def _rule_strip_urls_in_venue(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip URLs embedded in booktitle/journal (keep text before URL)."""
    changed = False
    for url_field in ("booktitle", "journal"):
        url_val = (fields.get(url_field) or "").strip()
        if url_val and _URL_IN_VENUE_RE.search(url_val):
            url_cleaned = _URL_IN_VENUE_STRIP_RE.sub("", url_val).strip().rstrip(",")
            if url_cleaned and url_cleaned != url_val:
                fields[url_field] = url_cleaned
                changed = True
    return changed


def _rule_publisher_corrections(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Apply publisher corrections keyed on journal substring."""
    changed = False
    pub_journal = (fields.get("journal") or "").lower()
    if pub_journal:
        for jnl_key, correct_pub in PUBLISHER_CORRECTIONS.items():
            if jnl_key in pub_journal:
                cur_pub = fields.get("publisher", "")
                if cur_pub and cur_pub != correct_pub:
                    fields["publisher"] = correct_pub
                    changed = True
    return changed


def _rule_strip_publisher_duplicate(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip publisher when it duplicates the journal/booktitle name."""
    pub = (fields.get("publisher") or "").strip()
    container = (fields.get("journal") or fields.get("booktitle") or "").strip()
    if pub and container and pub.lower() == container.lower():
        del fields["publisher"]
        return True
    return False


def _rule_expand_abbreviated_venue(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Expand abbreviated venue names in booktitle (e.g., "NIME 2021" -> full name)."""
    bt = (fields.get("booktitle") or "").strip()
    if bt and bt.lower() in ABBREVIATED_VENUE_MAP:
        expanded_bt = ABBREVIATED_VENUE_MAP[bt.lower()]
        if expanded_bt != bt:
            fields["booktitle"] = expanded_bt
            return True
    return False


def _rule_venue_case_corrections(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Correct ALL-CAPS venue names to proper case; fix lowercase GECCO venue."""
    changed = False
    for vcf in ("journal", "booktitle"):
        vc_val = (fields.get(vcf) or "").strip()
        if vc_val and vc_val in VENUE_CASE_CORRECTIONS:
            corrected_vc = VENUE_CASE_CORRECTIONS[vc_val]
            if corrected_vc != vc_val:
                fields[vcf] = corrected_vc
                changed = True
        elif vc_val and _GECCO_LOWER in vc_val.lower():
            vc_fixed = _GECCO_RE.sub(_GECCO_PROPER, vc_val)
            if vc_fixed != vc_val:
                fields[vcf] = vc_fixed
                changed = True
    return changed


def _rule_strip_spire_suffix(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip SPIRE-style proceedings garbage suffix from booktitle."""
    bt_spire = (fields.get("booktitle") or "").strip()
    if bt_spire:
        bt_cleaned = _SPIRE_STRIP_RE.sub("", bt_spire)
        if bt_cleaned != bt_spire:
            fields["booktitle"] = bt_cleaned
            return True
    return False


def _rule_strip_osf_doi_version(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip _v[N] version suffix from OSF/PsyArXiv DOIs (and matching URL)."""
    doi_val = (fields.get("doi") or "").strip()
    if doi_val and _OSF_DOI_RE.match(doi_val):
        doi_stripped = _DOI_VERSION_RE.sub("", doi_val)
        if doi_stripped != doi_val:
            fields["doi"] = doi_stripped
            url_val = (fields.get("url") or "").strip()
            if url_val and doi_val in url_val:
                fields["url"] = url_val.replace(doi_val, doi_stripped)
            return True
    return False


def _rule_fix_zentrum_umlaut(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Fix Schloss Dagstuhl missing umlaut ("fur" -> "f{\\"u}r")."""
    pub = (fields.get("publisher") or "").strip()
    if _ZENTRUM_FUR in pub:
        fields["publisher"] = pub.replace(_ZENTRUM_FUR, _ZENTRUM_FUER)
        return True
    return False


def _rule_strip_lipics_pages(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip page numbers embedded in booktitle (LIPIcs style) and backfill pages."""
    bt_pages = (fields.get("booktitle") or "").strip()
    if bt_pages:
        bt_clean = _LIPICS_PAGES_STRIP_RE.sub("", bt_pages)
        if bt_clean != bt_pages:
            fields["booktitle"] = bt_clean
            if not fields.get("pages"):
                pages_match = _LIPICS_PAGES_EXTRACT_RE.search(bt_pages)
                if pages_match:
                    fields["pages"] = pages_match.group(1).replace(" ", "")
            return True
    return False


def _rule_strip_proceedings_wrapper(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip duplicate "Proceedings of the" wrapper from booktitle."""
    bt_dup = (fields.get("booktitle") or "").strip()
    if bt_dup.startswith(_PROC_EXT_ABSTRACTS):
        fields["booktitle"] = bt_dup.removeprefix(_PROC_OF_THE)
        return True
    return False


# ---------------------------------------------------------------------------
# Site-A-only rules (orphan/terminal sweep)
# ---------------------------------------------------------------------------
def _rule_strip_preprint_marker_title(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip a trailing [preprint] marker from the title."""
    title = fields.get("title", "")
    if isinstance(title, str) and _PREPRINT_MARKER_RE.search(title):
        fields["title"] = _PREPRINT_MARKER_RE.sub("", title).strip()
        return True
    return False


def _rule_fix_title_text(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Fix fused compounds, colon-space, hyphen-space, and acronym case in the title."""
    title = fields.get("title", "")
    if isinstance(title, str) and title:
        fixed_title = _fix_title_text(title)
        if fixed_title != title:
            fields["title"] = fixed_title
            return True
    return False


def _rule_strip_trailing_dash_venue_title(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip trailing " -"/en-dash truncation artifact from booktitle and title."""
    changed = False
    for td_field in ("booktitle", "title"):
        td_val = (fields.get(td_field) or "").strip()
        if td_val and _TRAILING_DASH_RE.search(td_val):
            fields[td_field] = _TRAILING_DASH_RE.sub("", td_val)
            changed = True
    return changed


def _rule_strip_subtitle_wrapper_title(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip ": -...-" subtitle wrapper artifact from the title."""
    title = fields.get("title", "")
    if isinstance(title, str) and ": -" in title:
        cleaned = _SUBTITLE_WRAPPER_RE.sub(r": \1", title)
        if cleaned != title:
            fields["title"] = cleaned
            return True
    return False


def _rule_remove_nondigit_pages(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Remove a pages field that contains no digits (location strings, not pages)."""
    pg = (fields.get("pages") or "").strip()
    if pg and not re.search(r"\d", pg):
        del fields["pages"]
        return True
    return False


def _rule_add_url_from_doi(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Add a URL derived from the DOI when the URL is missing."""
    doi = (fields.get("doi") or "").strip()
    if doi and not fields.get("url"):
        fields["url"] = f"https://doi.org/{doi}"
        return True
    return False


# ---------------------------------------------------------------------------
# Site-C-only rules (Phase-4 post-merge; POST_MERGE is terminal)
# ---------------------------------------------------------------------------
def _rule_article_no_journal_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Terminal: @article with no journal (enrichment exhausted) -> @misc."""
    if entry.get("type") == "article" and not fields.get("journal"):
        entry["type"] = "misc"
        return True
    return False


def _rule_inproceedings_no_booktitle_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Terminal: @inproceedings without booktitle -> @misc (invalid BibTeX otherwise)."""
    if entry.get("type") == "inproceedings" and not fields.get("booktitle"):
        entry["type"] = "misc"
        return True
    return False


def _rule_preprint_journal_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with a preprint server as journal -> @misc (journal -> howpublished)."""
    if entry.get("type") == "article":
        j_lower = (fields.get("journal") or "").lower().strip()
        if j_lower and any(ps == j_lower or ps in j_lower for ps in PREPRINT_SERVERS):
            entry["type"] = "misc"
            fields["howpublished"] = fields.pop("journal")
            return True
    return False


def _rule_strip_ellipsis_venues(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Strip trailing ellipsis from truncated journal/booktitle/title fields."""
    changed = False
    for ell_field in ("journal", "booktitle", "title"):
        ell_val = fields.get(ell_field) or ""
        if ell_val.rstrip().endswith(("...", "\u2026")):
            ell_clean = _strip_ellipsis(ell_val)
            if ell_clean != ell_val:
                fields[ell_field] = ell_clean
                changed = True
    return changed


def _rule_patent_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with a US patent number as journal -> @misc (journal -> note)."""
    if entry.get("type") == "article" and fields.get("journal"):
        patent_jnl = fields["journal"].strip()
        if _US_PATENT_RE.match(patent_jnl):
            entry["type"] = "misc"
            fields["note"] = patent_jnl
            fields.pop("journal", None)
            return True
    return False


def _rule_unpublished_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with "Unpublished" journal -> @misc."""
    if entry.get("type") == "article" and fields.get("journal") and fields["journal"].strip().lower() == "unpublished":
        entry["type"] = "misc"
        fields.pop("journal", None)
        return True
    return False


def _rule_url_booktitle_to_misc(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@inproceedings with a URL fragment as booktitle -> @misc."""
    if entry.get("type") == "inproceedings" and fields.get("booktitle"):
        bt_val = fields["booktitle"].strip()
        if _URL_BOOKTITLE_RE.match(bt_val):
            entry["type"] = "misc"
            fields.pop("booktitle", None)
            return True
    return False


def _rule_article_preprint_doi(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """@article with a preprint (secondary) DOI.

    If it has a real journal + volume/pages, keep as @article but strip the
    preprint DOI/URL. Otherwise downgrade to @misc (journal -> howpublished).
    """
    if entry.get("type") == "article":
        merged_doi = (fields.get("doi") or "").strip()
        if merged_doi and idu.is_secondary_doi(merged_doi):
            venue = fields.get("journal", "")
            if venue and (fields.get("volume") or fields.get("pages")):
                fields.pop("doi", None)
                fields.pop("url", None)
            else:
                entry["type"] = "misc"
                if venue:
                    fields["howpublished"] = fields.pop("journal")
            return True
    return False


def _rule_backfill_howpublished(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Backfill howpublished for @misc entries with a preprint DOI or arXiv eprint."""
    if entry.get("type") == "misc" and not fields.get("howpublished"):
        misc_doi = (fields.get("doi") or "").strip()
        inferred_hp = mu.infer_howpublished_from_doi(misc_doi) if misc_doi else None
        if inferred_hp:
            fields["howpublished"] = inferred_hp
            return True
        if (fields.get("archiveprefix") or "").lower() == "arxiv":
            fields["howpublished"] = "arXiv"
            return True
    return False


def _rule_normalize_title_chain(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Post-merge title normalization chain (ALL-CAPS, [J]/[preprint], fused, dashes)."""
    title = fields.get("title", "")
    if isinstance(title, str) and title:
        fixed = trim_title_default(title)
        fixed = _BRACKET_J_RE.sub("", fixed).strip()
        fixed = _PREPRINT_MARKER_RE.sub("", fixed).strip()
        fixed = _fix_title_text(fixed)
        fixed = _TRAILING_DASH_RE.sub("", fixed)
        fixed = _SUBTITLE_WRAPPER_RE.sub(r": \1", fixed)
        if fixed != title:
            fields["title"] = fixed
            return True
    return False


def _rule_fix_author_casing(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Fix author casing + capital "And" separators from API sources."""
    auth = fields.get("author", "")
    if isinstance(auth, str) and auth:
        auth_fixed, _ = mu._fix_author_casing(auth)
        if auth_fixed != auth:
            fields["author"] = auth_fixed
            return True
    return False


def _rule_normalize_howpublished(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Normalize howpublished casing after all journal->howpublished moves."""
    before = fields.get("howpublished")
    mu._normalize_howpublished(fields)
    return fields.get("howpublished") != before


def _rule_howpublished_to_inproceedings(entry: dict[str, Any], fields: dict[str, Any]) -> bool:
    """Upgrade @misc with a conference/workshop howpublished -> @inproceedings.

    When howpublished is a venue name (not a preprint server or repository),
    the entry is a conference/workshop paper that should be @inproceedings.
    """
    if entry.get("type") == "misc" and fields.get("howpublished"):
        hp_val = (fields.get("howpublished") or "").strip()
        hp_lower = hp_val.lower()
        is_preprint_hp = (
            any(ps == hp_lower or ps in hp_lower for ps in PREPRINT_SERVERS) or hp_lower in _R20_PREPRINT_HOWPUBLISHED
        )
        is_repository_hp = any(rj in hp_lower for rj in REPOSITORY_AS_JOURNAL)
        if not is_preprint_hp and not is_repository_hp and hp_val:
            entry["type"] = "inproceedings"
            fields["booktitle"] = fields.pop("howpublished")
            return True
    return False


# ---------------------------------------------------------------------------
# Per-stage ordered rule sequences
# ---------------------------------------------------------------------------
# Site A (orphan/terminal sweep). Reproduces _fixup_bib_entry's exact rule order.
_POSTRUN_ORPHAN_REPAIR_RULES = (
    _rule_procedia_to_inproceedings,
    _rule_pacm_booktitle_to_article,
    _rule_named_proceedings_to_article,
    _rule_institutional_repo_to_phdthesis,
    _rule_repo_booktitle_to_misc,
    _rule_repo_journal_to_misc,
    _rule_preprint_booktitle_to_misc,
    _rule_university_to_phdthesis,
    _rule_journal_prefix_to_article,
    _rule_handbook_to_incollection,
    _rule_book_chapter_doi_to_incollection,
    _rule_conference_journal_to_inproceedings,
    _rule_strip_preprint_marker_title,
    _rule_fix_title_text,
    _rule_booktitle_fixups,
    _rule_strip_urls_in_venue,
    _rule_publisher_corrections,
    _rule_strip_publisher_duplicate,
    _rule_expand_abbreviated_venue,
    _rule_venue_case_corrections,
    _rule_strip_trailing_dash_venue_title,
    _rule_strip_subtitle_wrapper_title,
    _rule_strip_spire_suffix,
    _rule_strip_osf_doi_version,
    _rule_remove_nondigit_pages,
    _rule_fix_zentrum_umlaut,
    _rule_strip_lipics_pages,
    _rule_strip_proceedings_wrapper,
    _rule_add_url_from_doi,
)

# Site C (Phase-4 post-merge). Reproduces the inline post-merge block's exact order.
_POST_MERGE_RULES = (
    _rule_article_no_journal_to_misc,
    _rule_inproceedings_no_booktitle_to_misc,
    _rule_preprint_journal_to_misc,
    _rule_strip_ellipsis_venues,
    _rule_conference_journal_to_inproceedings,
    _rule_procedia_to_inproceedings,
    _rule_pacm_booktitle_to_article,
    _rule_named_proceedings_to_article,
    _rule_institutional_repo_to_phdthesis,
    _rule_patent_to_misc,
    _rule_unpublished_to_misc,
    _rule_url_booktitle_to_misc,
    _rule_preprint_booktitle_to_misc,
    _rule_journal_prefix_to_article,
    _rule_handbook_to_incollection,
    _rule_book_chapter_doi_to_incollection,
    _rule_repo_journal_to_misc,
    _rule_repo_booktitle_to_misc,
    _rule_university_to_phdthesis,
    _rule_article_preprint_doi,
    _rule_backfill_howpublished,
    _rule_normalize_title_chain,
    _rule_booktitle_fixups,
    _rule_expand_abbreviated_venue,
    _rule_venue_case_corrections,
    _rule_strip_publisher_duplicate,
    _rule_strip_spire_suffix,
    _rule_strip_osf_doi_version,
    _rule_fix_zentrum_umlaut,
    _rule_strip_lipics_pages,
    _rule_strip_proceedings_wrapper,
    _rule_strip_urls_in_venue,
    _rule_publisher_corrections,
    _rule_fix_author_casing,
    _rule_normalize_howpublished,
    _rule_howpublished_to_inproceedings,
)

_STAGE_RULES = {
    CanonicalStage.POST_MERGE: _POST_MERGE_RULES,
    CanonicalStage.POSTRUN_ORPHAN_REPAIR: _POSTRUN_ORPHAN_REPAIR_RULES,
}


def canonicalize(entry: dict[str, Any], *, stage: CanonicalStage) -> bool:
    """Apply the entry-type reclassification + text normalization rules for a stage.

    Mutates ``entry`` in place and returns True if any rule changed something.
    The per-stage rule set and order are single-sourced in ``_STAGE_RULES``; the
    shared rule bodies live as ``_rule_*`` helpers so Site A (orphan repair) and
    Site C (Phase-4 post-merge) share identical logic without copy-paste.
    """
    rules = _STAGE_RULES.get(stage)
    if rules is None:
        raise NotImplementedError(f"canonicalize() does not yet implement stage {stage!r}")
    fields = entry.get("fields") or {}
    changed = False
    for rule in rules:
        if rule(entry, fields):
            changed = True
    return changed


def _fixup_bib_entry(entry: dict[str, Any]) -> bool:
    """Apply entry type and field corrections to a parsed BibTeX entry.

    Returns True if any changes were made. Used by both the per-article fixup
    and the post-run orphan fixup. Thin wrapper over the single-sourced
    ``canonicalize`` dispatch at the POSTRUN_ORPHAN_REPAIR stage.
    """
    return canonicalize(entry, stage=CanonicalStage.POSTRUN_ORPHAN_REPAIR)
