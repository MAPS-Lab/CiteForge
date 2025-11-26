from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

from .config import SIM_TITLE_SIM_MIN
from .text_utils import extract_year_from_any


def get_container_field(entry_type: str) -> str:
    """
    Choose the BibTeX field that should store the venue for this entry type,
    such as journal for articles, booktitle for conference papers and book
    chapters, or howpublished for miscellaneous entries.
    """
    return (
        "journal" if entry_type == "article"
        else "booktitle" if entry_type in ("inproceedings", "incollection")
        else "howpublished"
    )


def format_author_field(authors: List[str]) -> Optional[str]:
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
        authors: List[str],
        year: int,
        keyhint: str,
        venue: Optional[str] = None,
        doi: Optional[str] = None,
        url: Optional[str] = None,
        arxiv_id: Optional[str] = None,
        extra_fields: Optional[Dict[str, str]] = None
) -> str:
    """
    Build a complete BibTeX entry from the main publication details and optional
    identifiers, skipping fields that are missing or empty.
    """
    # avoid circular imports
    from .bibtex_utils import bibtex_from_dict, make_bibkey
    from .id_utils import _norm_arxiv_id

    key = make_bibkey(title, authors, year, fallback=re.sub(r"\W+", "", keyhint) or "entry")
    container_field = get_container_field(entry_type)

    fields: Dict[str, Optional[str]] = {
        "title": title or None,
        "author": format_author_field(authors),
        "year": str(year) if year else None,
        container_field: venue or None,
        "doi": doi or None,
        "url": url or None,
    }

    # add arXiv fields if applicable
    if arxiv_id:
        fields["eprint"] = _norm_arxiv_id(arxiv_id)
        fields["archiveprefix"] = "arXiv"

    # add extra fields like volume, pages, etc
    if extra_fields:
        fields.update(extra_fields)

    # filter out None values
    entry = {
        "type": entry_type,
        "key": key,
        "fields": {k: v for k, v in fields.items() if v}
    }
    return bibtex_from_dict(entry)


def create_scoring_function(
        title: str,
        author_name: Optional[str],
        year_hint: Optional[int],
        title_getter: Callable[[Any], str],
        authors_getter: Callable[[Any], Any],
        year_getter: Optional[Callable[[Any], Optional[int]]] = None,
        author_match_fn: Optional[Callable] = None
) -> Callable[[Any], float]:
    """
    Create a scoring function that ranks search results against a target title,
    author, and year using the supplied accessors and matching logic.
    """
    # avoid circular imports
    from .api_clients import _score_candidate_generic
    from .text_utils import title_similarity, author_name_matches

    if author_match_fn is None:
        author_match_fn = author_name_matches

    def score_fn(candidate: Any) -> float:
        """
        Compare a single candidate against the target description and return a
        score that reflects how well title, author, and year agree.
        """
        cand_title = title_getter(candidate)
        tsim = title_similarity(title, cand_title)

        # skip if title doesn't match well enough
        if tsim < SIM_TITLE_SIM_MIN:
            return 0.0

        # skip if author doesn't match (when we care about author)
        cand_authors = authors_getter(candidate)
        if author_name and not author_match_fn(author_name, cand_authors):
            return 0.0

        # extract year if available
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


def determine_entry_type(
        obj: Any,
        type_field: str = "type",
        publication_types_field: Optional[str] = None,
        venue_hints: Optional[Dict[str, str]] = None
) -> str:
    """
    Guess whether a publication should be treated as a journal article,
    conference paper, book chapter, or miscellaneous entry by inspecting type
    fields and venue hints.
    """
    if obj is None:
        return "misc"

    # plain string - look for keywords
    if isinstance(obj, str):
        typ = obj.lower()
        if "journal" in typ or typ in ("journal-article", "journal_article", "article"):
            return "article"
        if "proceed" in typ or typ in ("proceedings-article", "paper-conference", "inproceedings", "conference"):
            return "inproceedings"
        if "chapter" in typ or typ in ("book-chapter", "book_chapter", "incollection"):
            return "incollection"
        return "misc"

    # dict - try multiple strategies
    if isinstance(obj, dict):
        # check publicationTypes array (Semantic Scholar)
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

        # check type field (Crossref/CSL)
        typ = (obj.get(type_field) or "").lower()
        if typ:
            if "journal" in typ or typ in ("journal-article", "journal_article", "article"):
                return "article"
            if "proceed" in typ or typ in ("proceedings-article", "paper-conference", "inproceedings", "conference"):
                return "inproceedings"
            if "chapter" in typ or typ in ("book-chapter", "book_chapter", "incollection"):
                return "incollection"

        # check for book chapter indicators
        # The combination of howpublished + publisher + pages (without journal/booktitle)
        # is a strong indicator of a book chapter, as these fields together suggest
        # a chapter within a published book rather than a journal article or conference paper
        howpublished = obj.get("howpublished")
        publisher = obj.get("publisher")
        pages = obj.get("pages")
        has_journal = obj.get("journal")
        has_booktitle = obj.get("booktitle")

        if howpublished and publisher and pages and not has_journal and not has_booktitle:
            # First check for explicit book series/chapter keywords
            howpub_lower = str(howpublished).lower()
            book_series_keywords = [
                "lecture notes", "series", "handbook", "advances in",
                "studies in", "chapter"
            ]
            if any(keyword in howpub_lower for keyword in book_series_keywords):
                return "incollection"

            # Also check publisher name patterns common for book publishers
            pub_lower = str(publisher).lower() if publisher else ""
            book_publisher_keywords = ["springer", "elsevier", "wiley", "crc press", "cambridge", "oxford"]
            if any(keyword in pub_lower for keyword in book_publisher_keywords):
                return "incollection"

        # check venue content for conference keywords before trusting venue_hints
        # this catches cases where "journal" field contains conference proceedings
        for venue_field in ["journal", "container-title", "venue", "booktitle"]:
            venue = obj.get(venue_field)
            if venue and isinstance(venue, str):
                venue_lower = venue.lower()
                # conference indicators
                conference_keywords = [
                    "proceedings", "conference", "symposium", "workshop",
                    "meeting", "summit", "congress", "colloquium",
                    "chapter of the association",  # NAACL, EACL, AACL, etc.
                    "findings of",  # ACL/EMNLP workshop findings
                    "lecture notes in computer science",  # LNCS is a conference proceedings series
                ]
                if any(keyword in venue_lower for keyword in conference_keywords):
                    return "inproceedings"

        # check venue hints (e.g. if there's a journal field, probably an article)
        if venue_hints:
            for venue_field, preferred_type in venue_hints.items():
                if obj.get(venue_field):
                    return preferred_type

    return "misc"
