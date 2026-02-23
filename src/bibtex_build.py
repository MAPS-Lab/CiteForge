from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .config import ABBREVIATED_VENUE_MAP, KNOWN_CONFERENCE_VENUES, SIM_TITLE_SIM_MIN
from .log_utils import LogCategory, logger
from .text_utils import extract_year_from_any

_ARTICLE_TYPES = {"journal-article", "journal_article", "article"}
_CONFERENCE_TYPES = {"proceedings-article", "paper-conference", "inproceedings", "conference"}
_CHAPTER_TYPES = {"book-chapter", "book_chapter", "incollection"}
_BOOK_TYPES = {"book", "edited-book", "monograph", "reference-book"}

_CONFERENCE_KEYWORDS = (
    "proceedings", "conference", "symposium", "workshop",
    "meeting", "summit", "congress", "colloquium",
    "chapter of the association",  # NAACL, EACL, AACL, etc.
    "findings of",  # ACL/EMNLP workshop findings
    "lecture notes in computer science",  # LNCS is a conference proceedings series
    "medinfo",  # Medical informatics (IOS Press SHTI series)
    "studies in health technology and informatics",  # IOS Press (SHTI)
)

_BOOK_SERIES_KEYWORDS = (
    "lecture notes", "series", "handbook", "advances in", "studies in", "chapter",
)

_BOOK_PUBLISHER_KEYWORDS = ("springer", "elsevier", "wiley", "crc press", "cambridge", "oxford")


def get_container_field(entry_type: str) -> str:
    """
    Choose the BibTeX field that should store the venue for this entry type,
    such as journal for articles, booktitle for conference papers and book
    chapters, or howpublished for miscellaneous entries.
    """
    if entry_type == "article":
        return "journal"
    if entry_type in ("inproceedings", "incollection"):
        return "booktitle"
    return "howpublished"


def format_author_field(authors: list[str]) -> str | None:
    """
    Combine a list of author names into the BibTeX author format using " and "
    between names, or return None when the list is empty.
    """
    return " and ".join(authors) if authors else None


def normalize_year(year: Any) -> int:
    """
    Try to extract a four-digit publication year from different input formats
    and return 0 when no valid year can be found.
    """
    return extract_year_from_any(year, fallback=0) or 0


def build_bibtex_entry(
        entry_type: str,
        title: str,
        authors: list[str],
        year: int,
        keyhint: str,
        venue: str | None = None,
        doi: str | None = None,
        url: str | None = None,
        arxiv_id: str | None = None,
        extra_fields: dict[str, str] | None = None
) -> str:
    """
    Build a complete BibTeX entry from the main publication details and optional
    identifiers, skipping fields that are missing or empty.
    """
    from .bibtex_utils import bibtex_from_dict, make_bibkey
    from .id_utils import _norm_arxiv_id

    key = make_bibkey(title, authors, year, fallback=re.sub(r"\W+", "", keyhint) or "entry")
    container_field = get_container_field(entry_type)
    logger.debug(
        f"BUILD_ENTRY | type={entry_type} | key={key} | title={title[:60]}"
        f" | authors={len(authors)} | year={year} | venue={str(venue or '')[:40]}"
        f" | doi={doi or 'none'} | arxiv={arxiv_id or 'none'}",
        category=LogCategory.SCORE,
    )

    fields: dict[str, str | None] = {
        "title": title or None,
        "author": format_author_field(authors),
        "year": str(year) if year else None,
        container_field: venue or None,
        "doi": doi or None,
        "url": url or None,
    }

    if arxiv_id:
        fields["eprint"] = _norm_arxiv_id(arxiv_id)
        fields["archiveprefix"] = "arXiv"

    if extra_fields:
        fields.update(extra_fields)

    entry = {
        "type": entry_type,
        "key": key,
        "fields": {k: v for k, v in fields.items() if v}
    }
    return bibtex_from_dict(entry)


def create_scoring_function(
        title: str,
        author_name: str | None,
        year_hint: int | None,
        title_getter: Callable[[Any], str],
        authors_getter: Callable[[Any], Any],
        year_getter: Callable[[Any], int | None] | None = None,
        author_match_fn: Callable[[str, Any], bool] | None = None
) -> Callable[[Any], float]:
    """
    Create a scoring function that ranks search results against a target title,
    author, and year using the supplied accessors and matching logic.
    """
    from .clients.helpers import _score_candidate_generic
    from .text_utils import author_name_matches, title_similarity

    if author_match_fn is None:
        author_match_fn = author_name_matches

    def score_fn(candidate: Any) -> float:
        """
        Compare a single candidate against the target description and return a
        score that reflects how well title, author, and year agree.
        """
        cand_title = title_getter(candidate)
        tsim = title_similarity(title, cand_title)

        if tsim < SIM_TITLE_SIM_MIN:
            return 0.0

        cand_authors = authors_getter(candidate)
        if author_name and not author_match_fn(author_name, cand_authors):
            return 0.0

        cand_year = year_getter(candidate) if year_getter else None

        return _score_candidate_generic(
            target_title=title,
            target_author=author_name,
            target_year=year_hint,
            cand_title=cand_title,
            cand_authors=cand_authors,
            cand_year=cand_year,
            title_sim=title_similarity,
            author_match=author_match_fn,
        )

    return score_fn


def _classify_type_string(typ: str) -> str | None:
    """
    Map a publication type string to a BibTeX entry type, returning None if
    no match is found.
    """
    if "journal" in typ or typ in _ARTICLE_TYPES:
        return "article"
    if "proceed" in typ or typ in _CONFERENCE_TYPES:
        return "inproceedings"
    if "chapter" in typ or typ in _CHAPTER_TYPES:
        return "incollection"
    if typ in _BOOK_TYPES:
        return "book"
    return None


def _is_conference_venue(venue: str) -> bool:
    """Check whether a venue string indicates a conference or workshop."""
    venue_lower = venue.lower()
    if any(kw in venue_lower for kw in _CONFERENCE_KEYWORDS):
        return True
    if any(known in venue_lower for known in KNOWN_CONFERENCE_VENUES):
        return True
    venue_stripped = venue_lower.strip()
    if venue_stripped in ABBREVIATED_VENUE_MAP:
        return True
    return any(venue_stripped == full.lower() for full in ABBREVIATED_VENUE_MAP.values())


def determine_entry_type(
        obj: Any,
        type_field: str = "type",
        publication_types_field: str | None = None,
        venue_hints: dict[str, str] | None = None
) -> str:
    """
    Guess whether a publication should be treated as a journal article,
    conference paper, book chapter, or miscellaneous entry by inspecting type
    fields and venue hints.
    """
    if obj is None:
        return "misc"

    if isinstance(obj, str):
        return _classify_type_string(obj.lower()) or "misc"

    if isinstance(obj, dict):
        if publication_types_field:
            pub_types = obj.get(publication_types_field) or []
            if isinstance(pub_types, list):
                pub_types_lower = [str(t).lower() for t in pub_types if t]
                if any("journal" in t or t in ("journalarticle", "review") for t in pub_types_lower):
                    return "article"
                if any("conference" in t or "proceed" in t or t == "inproceedings" for t in pub_types_lower):
                    return "inproceedings"
                if any("chapter" in t or t in ("bookchapter", "incollection") for t in pub_types_lower):
                    return "incollection"

        typ = (obj.get(type_field) or "").lower()
        if typ:
            classified = _classify_type_string(typ)
            if classified:
                return classified

        # Book chapter heuristic: howpublished + publisher + pages without journal/booktitle
        howpublished = obj.get("howpublished")
        publisher = obj.get("publisher")
        pages = obj.get("pages")

        if howpublished and publisher and pages and not obj.get("journal") and not obj.get("booktitle"):
            howpub_lower = str(howpublished).lower()
            if any(kw in howpub_lower for kw in _BOOK_SERIES_KEYWORDS):
                return "incollection"
            pub_lower = str(publisher).lower()
            if any(kw in pub_lower for kw in _BOOK_PUBLISHER_KEYWORDS):
                return "incollection"

        for venue_field in ("journal", "container-title", "venue", "booktitle"):
            venue = obj.get(venue_field)
            if venue and isinstance(venue, str) and _is_conference_venue(venue):
                return "inproceedings"

        if venue_hints:
            for venue_field, preferred_type in venue_hints.items():
                if obj.get(venue_field):
                    return preferred_type

    return "misc"
