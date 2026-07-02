"""Venue classification and howpublished canonicalization.

Disambiguates conferences from journals, canonicalizes the ``howpublished``
label for preprint entries, and infers a howpublished value from a DOI prefix
when the venue itself is unknown.
"""

from __future__ import annotations

import re
from typing import Any

from .config import CONFERENCE_AS_JOURNAL, JOURNALS_NAMED_PROCEEDINGS

_DAGSTUHL_DOI_RE = re.compile(
    r"^10\.4230/(lipics|oasics)\.([a-z0-9]+)\.(\d+)(?:\.\d+)?$",
    re.IGNORECASE,
)

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
