from typing import Any, Dict

from .config import S2_BASE, CROSSREF_BASE, OPENALEX_BASE, PUBMED_BASE, EUROPEPMC_BASE
from .api_generics import APISearchConfig, APIFieldMapping
from .text_utils import extract_year_from_any

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
        # Note: DOI is inside externalIds.DOI, not a top-level field
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
    # Custom getters for Crossref's format
    title_getter=lambda c: (
        (c.get("title") or [""])[0]
        if isinstance(c.get("title"), list) and c.get("title")
        else ""
    ),
    year_getter=lambda c: (
        ((c.get("issued") or {}).get("date-parts") or [[None]])[0][0]
        if ((c.get("issued") or {}).get("date-parts") and
            (c.get("issued") or {}).get("date-parts")[0] and
            isinstance(((c.get("issued") or {}).get("date-parts") or [[None]])[0][0], int))
        else None
    )
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
        "mailto": "citeforge@example.com"
    },
    # Custom getters for OpenAlex's nested structure
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


# ============================================================================================
# Field Mapping Configurations
# ============================================================================================

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
    extra_field_mappings={},
    # Custom extractors for nested fields
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
    # Custom extractors for Crossref's format
    custom_author_extractor=lambda item: [
        f"{author.get('given', '').strip()} {author.get('family', '').strip()}".strip()
        for author in item.get("author") or []
        if f"{author.get('given', '').strip()} {author.get('family', '').strip()}".strip()
    ] if item.get("author") else [],
    custom_year_extractor=lambda item: (
        ((item.get("issued") or item.get("published-print") or item.get("published-online") or {})
         .get("date-parts") or [[None]])[0][0]
        if ((item.get("issued") or item.get("published-print") or item.get("published-online") or {})
            .get("date-parts") or [[None]])[0]
        and isinstance(((item.get("issued") or item.get("published-print") or item.get("published-online") or {})
                       .get("date-parts") or [[None]])[0][0], int)
        else 0
    )
)


# Helper for Crossref title extraction
def _extract_crossref_title(item: Dict[str, Any]) -> str:
    """
    Extract title from Crossref's list format.
    """
    tlist = item.get("title") or []
    return (tlist[0] if tlist else "").strip()


# Update Crossref mapping to use custom title extractor
CROSSREF_FIELD_MAPPING.title_fields = ["title"]
# We'll handle the list extraction in a custom way

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
    extra_field_mappings={},
    # Custom author extractor for OpenAlex's nested structure
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
    # Custom extractors for PubMed's format
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
    # Custom extractors
    custom_author_extractor=lambda article: [
        name.strip()
        for name in (article.get("authorString") or "").split(",")
        if name.strip()
    ],
    custom_year_extractor=lambda article: (
        (lambda ys: int(ys) if ys and ys.isdigit() else 0)
        (article.get("pubYear") or "")
    )
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
    # Custom handling for nested content
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
    # Custom extractors for DataCite's nested structure
    custom_author_extractor=lambda record: [
        creator.get("name", "").strip()
        for creator in (record.get("attributes") or {}).get("creators") or []
        if creator.get("name", "").strip()
    ]
)
