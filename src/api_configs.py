from __future__ import annotations

import os
from typing import Any

from .api_generics import APIFieldMapping, APISearchConfig
from .config import CROSSREF_BASE, EUROPEPMC_BASE, OPENALEX_BASE, PUBMED_BASE, S2_BASE
from .text_utils import extract_year_from_any


def _year_from_date_parts(source: dict[str, Any]) -> int | None:
    """Extract year from a CSL date-parts structure, or ``None`` if absent."""
    date_parts = source.get("date-parts")
    if date_parts and date_parts[0] and isinstance(date_parts[0][0], int):
        return date_parts[0][0]
    return None


def _extract_csl_year(container: dict[str, Any]) -> int | None:
    """Extract year from CSL date-parts structure."""
    return _year_from_date_parts(container.get("issued") or {})


def _extract_crossref_year(item: dict[str, Any]) -> int:
    """Extract year from Crossref date-parts (tries issued, published-print, published-online)."""
    for field in ("issued", "published-print", "published-online"):
        year = _year_from_date_parts(item.get(field) or {})
        if year is not None:
            return year
    return 0


def _extract_europepmc_year(article: dict[str, Any]) -> int:
    """Extract year from Europe PMC pubYear field."""
    year_str = article.get("pubYear") or ""
    return int(year_str) if year_str.isdigit() else 0

S2_SEARCH_CONFIG = APISearchConfig(
    api_name="semantic_scholar",
    base_url=f"{S2_BASE}/paper/search",
    query_param_name="query",
    result_path=["data"],
    title_field="title",
    author_field="authors",
    requires_api_key=True,
    additional_params={
        "limit": 15,
        "fields": "paperId,title,year,venue,publicationTypes,authors,url,journal,externalIds,publicationDate"
    }
)

CROSSREF_SEARCH_CONFIG = APISearchConfig(
    api_name="crossref",
    base_url=CROSSREF_BASE,
    query_param_name="query.bibliographic",
    result_path=["message", "items"],
    title_field="title",
    author_field="author",
    additional_params={
        "rows": 20,
        "select": ("title,author,issued,container-title,type,URL,DOI,"
                   "published-print,published-online,publisher,volume,issue,page")
    },
    title_getter=lambda c: (
        (c.get("title") or [""])[0]
        if isinstance(c.get("title"), list) and c.get("title")
        else ""
    ),
    year_getter=_extract_csl_year,
)

OPENALEX_SEARCH_CONFIG = APISearchConfig(
    api_name="openalex",
    base_url=OPENALEX_BASE,
    query_param_name="search",
    result_path=["results"],
    title_field="title",
    author_field="authorships",
    additional_params={
        "per-page": 20,
        "mailto": os.getenv("CROSSREF_MAILTO", "")
    },
    authors_getter=lambda w: [
        authorship.get("author", {}).get("display_name", "")
        for authorship in w.get("authorships") or []
        if authorship.get("author", {}).get("display_name")
    ]
)

PUBMED_SEARCH_CONFIG = APISearchConfig(
    api_name="pubmed",
    base_url=f"{PUBMED_BASE}/esearch.fcgi",
    query_param_name="term",
    result_path=["esearchresult", "idlist"],  # Returns PMIDs, need second request
    title_field="title",
    author_field="authors",
    additional_params={
        "db": "pubmed",
        "retmax": 10,
        "retmode": "json"
    }
)

EUROPEPMC_SEARCH_CONFIG = APISearchConfig(
    api_name="europepmc",
    base_url=f"{EUROPEPMC_BASE}/search",
    query_param_name="query",
    result_path=["resultList", "result"],
    title_field="title",
    author_field="authorString",
    additional_params={
        "format": "json",
        "pageSize": 20
    }
)


S2_FIELD_MAPPING = APIFieldMapping(
    api_name="semantic_scholar",
    title_fields=["title"],
    author_fields=["authors"],
    year_fields=["year", "publicationDate"],
    venue_fields=["venue", "journal.name", "publicationVenue.name"],
    doi_fields=["doi", "externalIds.DOI"],
    url_fields=["url"],
    arxiv_fields=["externalIds.ArXiv", "externalIds.arXiv"],
    author_name_key="name",
    entry_type_list_field="publicationTypes",
    custom_author_extractor=lambda paper: [
        a.get("name", "").strip()
        for a in paper.get("authors") or []
        if a.get("name", "").strip()
    ]
)

CROSSREF_FIELD_MAPPING = APIFieldMapping(
    api_name="crossref",
    title_fields=["title"],
    author_fields=["author"],
    year_fields=["issued", "published-print", "published-online"],
    venue_fields=["container-title"],
    doi_fields=["DOI"],
    url_fields=["URL"],
    author_given_key="given",
    author_family_key="family",
    entry_type_field="type",
    extra_field_mappings={
        "volume": "volume",
        "issue": "number",
        "page": "pages",
        "publisher": "publisher"
    },
    custom_author_extractor=lambda item: [
        f"{author.get('given', '').strip()} {author.get('family', '').strip()}".strip()
        for author in item.get("author") or []
        if f"{author.get('given', '').strip()} {author.get('family', '').strip()}".strip()
    ] if item.get("author") else [],
    custom_year_extractor=_extract_crossref_year,
)


OPENALEX_FIELD_MAPPING = APIFieldMapping(
    api_name="openalex",
    title_fields=["title"],
    author_fields=["authorships"],
    year_fields=["publication_year"],
    venue_fields=["primary_location.source.display_name"],
    doi_fields=["doi"],
    url_fields=["id"],
    entry_type_field="type",
    venue_hints={"journal": "article"},
    custom_author_extractor=lambda work: [
        authorship.get("author", {}).get("display_name", "").strip()
        for authorship in work.get("authorships") or []
        if authorship.get("author", {}).get("display_name", "").strip()
    ],
    custom_year_extractor=lambda work: work.get("publication_year") or 0
)

PUBMED_FIELD_MAPPING = APIFieldMapping(
    api_name="pubmed",
    title_fields=["title"],
    author_fields=["authors"],
    year_fields=["pubdate"],
    venue_fields=["fulljournalname", "source"],
    doi_fields=["articleids"],  # Special handling needed
    url_fields=["uid", "pmid"],  # Will build URL from PMID
    author_name_key="name",
    venue_hints={"fulljournalname": "article", "source": "article"},
    extra_field_mappings={
        "volume": "volume",
        "issue": "number",
        "pages": "pages"
    },
    custom_author_extractor=lambda article: [
        author.get("name", "").strip()
        for author in article.get("authors") or []
        if author.get("name", "").strip()
    ],
    custom_year_extractor=lambda article: extract_year_from_any(article.get("pubdate"), fallback=0) or 0
)

EUROPEPMC_FIELD_MAPPING = APIFieldMapping(
    api_name="europepmc",
    title_fields=["title"],
    author_fields=["authorString"],
    year_fields=["pubYear"],
    venue_fields=["journalTitle", "bookTitle"],
    doi_fields=["doi"],
    url_fields=["pmcid", "pmid"],  # Will build URL from PMCID/PMID
    entry_type_field="pubType",
    venue_hints={"journalTitle": "article", "bookTitle": "inproceedings"},
    extra_field_mappings={
        "journalVolume": "volume",
        "issue": "number",
        "pageInfo": "pages"
    },
    custom_author_extractor=lambda article: [
        name.strip()
        for name in (article.get("authorString") or "").split(",")
        if name.strip()
    ],
    custom_year_extractor=_extract_europepmc_year,
)

ARXIV_FIELD_MAPPING = APIFieldMapping(
    api_name="arxiv",
    title_fields=["title"],
    author_fields=["authors"],
    year_fields=["year"],
    venue_fields=[],  # arXiv doesn't have venues
    doi_fields=["doi", "abs_url"],
    url_fields=["abs_url"],
    arxiv_fields=["arxiv_id", "abs_url"],
    extra_field_mappings={
        "primary_class": "primaryclass"
    }
)

OPENREVIEW_FIELD_MAPPING = APIFieldMapping(
    api_name="openreview",
    title_fields=["content.title", "title"],
    author_fields=["content.authors", "content.authorids", "authors"],
    year_fields=["cdate", "tcdate"],  # Unix timestamps
    venue_fields=["content.venue", "content.venueid"],
    doi_fields=["content.doi"],
    url_fields=["content.pdf", "content.link", "content.homepage"],
    custom_author_extractor=lambda note: [
        str(a).strip()
        for a in ((note.get("content") or {}).get("authors") or
                  (note.get("content") or {}).get("authorids") or
                  note.get("authors") or [])
        if str(a).strip()
    ]
)

DATACITE_FIELD_MAPPING = APIFieldMapping(
    api_name="datacite",
    title_fields=["attributes.titles"],
    author_fields=["attributes.creators"],
    year_fields=["attributes.publicationYear"],
    venue_fields=["attributes.publisher"],
    doi_fields=["attributes.doi"],
    url_fields=["attributes.url"],
    custom_author_extractor=lambda record: [
        creator.get("name", "").strip()
        for creator in (record.get("attributes") or {}).get("creators") or []
        if creator.get("name", "").strip()
    ]
)
