"""Strict, bounded response decoders for durable provider capabilities."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from types import MappingProxyType
from typing import cast
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

import bibtexparser
from bibtexparser.bibdatabase import UndefinedString
from bibtexparser.bparser import BibTexParser
from defusedxml.ElementTree import fromstring as safe_xml_fromstring

from ..bibtex_utils import parse_bibtex_to_dict
from ..id_utils import find_arxiv_in_text, find_doi_in_text, normalize_doi
from .capabilities import ResponseMediaType
from .transport import RawProviderResponse, SchemaChangedError

DecodedResponse = tuple[Mapping[str, object], bool]
DecoderCallback = Callable[[RawProviderResponse, Mapping[str, object]], DecodedResponse]


@dataclass(frozen=True)
class DecoderDefinition:
    callback_id: str
    version: str
    schema: str
    media_type: ResponseMediaType
    callback: DecoderCallback | None = None


def _json_object(raw: RawProviderResponse) -> dict[str, object]:

    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"strict JSON has duplicate key {key!r}")
            result[key] = value
        return result

    try:
        value = json.loads(
            raw.body.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"strict JSON constant {value}")),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("strict JSON is malformed") from exc
    if not isinstance(value, dict):
        raise SchemaChangedError("provider JSON root is not an object")
    if "error" in value:
        raise SchemaChangedError("provider JSON contains an error envelope")
    return value


def _path(value: object, *components: str) -> object:
    current = value
    for component in components:
        if not isinstance(current, Mapping) or component not in current:
            raise SchemaChangedError(f"missing provider envelope {'.'.join(components)}")
        current = current[component]
    return current


def _list_decoder(
    field: str,
    *path: str,
    string_members: bool = False,
    allowed_fields: frozenset[str] | None = None,
    required_fields: frozenset[str] = frozenset(),
    string_fields: frozenset[str] = frozenset(),
    record_validator: Callable[[dict[str, object]], bool] | None = None,
) -> DecoderCallback:
    def decode(raw: RawProviderResponse, _context: Mapping[str, object]) -> DecodedResponse:
        value = _json_object(raw)
        items = _path(value, *path)
        member_type: type[dict] | type[str] = str if string_members else dict
        if not isinstance(items, list) or not all(isinstance(item, member_type) for item in items):
            raise SchemaChangedError(f"provider envelope {'.'.join(path)} is not a supported list")
        if allowed_fields is not None:
            projected: list[dict[str, object]] = []
            for item in items:
                if (
                    not isinstance(item, dict)
                    or any(not item.get(name) for name in required_fields)
                    or any(not isinstance(item.get(name), str) for name in string_fields)
                    or (record_validator is not None and not record_validator(item))
                ):
                    raise SchemaChangedError("provider record lacks required reducer evidence")
                projected.append({name: item[name] for name in allowed_fields if name in item})
            items = projected
        return {field: items}, not items

    return decode


def _counted_list_decoder(
    field: str,
    item_path: tuple[str, ...],
    count_path: tuple[str, ...],
    *,
    string_members: bool = False,
    success_path: tuple[str, ...] | None = None,
    allowed_fields: frozenset[str] | None = None,
    required_fields: frozenset[str] = frozenset(),
    string_fields: frozenset[str] = frozenset(),
    record_validator: Callable[[dict[str, object]], bool] | None = None,
) -> DecoderCallback:
    base = _list_decoder(
        field,
        *item_path,
        string_members=string_members,
        allowed_fields=allowed_fields,
        required_fields=required_fields,
        string_fields=string_fields,
        record_validator=record_validator,
    )

    def decode(raw: RawProviderResponse, context: Mapping[str, object]) -> DecodedResponse:
        value = _json_object(raw)
        if success_path is not None and _path(value, *success_path) != "ok":
            raise SchemaChangedError("provider response status is not successful")
        items = _path(value, *item_path)
        if not isinstance(items, list):
            raise SchemaChangedError("provider result members are not a list")
        normalized, _ = base(raw, context)
        count_raw = _path(value, *count_path)
        if isinstance(count_raw, bool) or not isinstance(count_raw, (str, int)):
            raise SchemaChangedError("provider result count is malformed")
        try:
            count = int(count_raw)
        except (TypeError, ValueError) as exc:
            raise SchemaChangedError("provider result count is malformed") from exc
        if count < len(items) or (count == 0) != (len(items) == 0):
            raise SchemaChangedError("provider result count conflicts with members")
        return normalized, count == 0

    return decode


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(member, str) for member in value)


def _date_parts(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    parts = value.get("date-parts")
    return isinstance(parts, list) and all(
        isinstance(part, list)
        and bool(part)
        and all(isinstance(member, int) and not isinstance(member, bool) for member in part)
        for part in parts
    )


def _crossref_title_is_valid(item: dict[str, object]) -> bool:
    title = item.get("title")
    authors = item.get("author")
    string_fields = ("DOI", "publisher", "type", "URL", "volume", "issue", "page")
    sequence_fields = ("container-title",)
    date_fields = ("issued", "published-print", "published-online")
    return (
        isinstance(title, list)
        and bool(title)
        and all(isinstance(value, str) and value.strip() for value in title)
        and all(key not in item or isinstance(item[key], str) for key in string_fields)
        and all(key not in item or _string_list(item[key]) for key in sequence_fields)
        and all(key not in item or _date_parts(item[key]) for key in date_fields)
        and (
            authors is None
            or (
                isinstance(authors, list)
                and all(
                    isinstance(author, dict)
                    and all(key not in author or isinstance(author[key], str) for key in ("given", "family"))
                    for author in authors
                )
            )
        )
    )


def _s2_record_is_valid(item: dict[str, object]) -> bool:
    authors = item.get("authors")
    publication_types = item.get("publicationTypes")
    external_ids = item.get("externalIds")
    journal = item.get("journal")
    return (
        (
            authors is None
            or (
                isinstance(authors, list)
                and all(isinstance(author, dict) and isinstance(author.get("name"), str) for author in authors)
            )
        )
        and (
            publication_types is None
            or (isinstance(publication_types, list) and all(isinstance(value, str) for value in publication_types))
        )
        and (
            external_ids is None
            or (isinstance(external_ids, dict) and all(isinstance(value, str) for value in external_ids.values()))
        )
        and (
            journal is None
            or (
                isinstance(journal, dict)
                and all(key not in journal or isinstance(journal[key], str) for key in ("name", "volume", "pages"))
            )
        )
        and all(
            key not in item or isinstance(item[key], str) for key in ("venue", "url", "abstract", "publicationDate")
        )
        and all(
            key not in item or (isinstance(item[key], int) and not isinstance(item[key], bool))
            for key in ("year", "citationCount")
        )
    )


def _openalex_record_is_valid(item: dict[str, object]) -> bool:
    authorships = item.get("authorships")
    primary_location = item.get("primary_location")
    return (
        (
            authorships is None
            or (
                isinstance(authorships, list)
                and all(
                    isinstance(authorship, dict)
                    and isinstance(authorship.get("author"), dict)
                    and isinstance(authorship["author"].get("display_name"), str)
                    for authorship in authorships
                )
            )
        )
        and all(key not in item or isinstance(item[key], str) for key in ("doi", "type"))
        and (
            "publication_year" not in item
            or (isinstance(item["publication_year"], int) and not isinstance(item["publication_year"], bool))
        )
        and (
            primary_location is None
            or (
                isinstance(primary_location, dict)
                and (
                    primary_location.get("source") is None
                    or (
                        isinstance(primary_location.get("source"), dict)
                        and isinstance(primary_location["source"].get("display_name"), str)
                    )
                )
            )
        )
    )


def _europepmc_record_is_valid(item: dict[str, object]) -> bool:
    return all(key not in item or isinstance(item[key], str) for key in _EUROPEPMC_FIELDS)


def _doi_csl(raw: RawProviderResponse, context: Mapping[str, object]) -> DecodedResponse:
    value = _json_object(raw)
    title = value.get("title")
    if (
        not value
        or not isinstance(title, (str, list))
        or not title
        or (isinstance(title, str) and not title.strip())
        or (isinstance(title, list) and not all(isinstance(item, str) and item.strip() for item in title))
    ):
        raise SchemaChangedError("DOI CSL response lacks title metadata")
    requested = normalize_doi(str(context.get("doi", "")))
    returned = normalize_doi(str(value.get("DOI", "")))
    if requested and returned and requested != returned:
        raise SchemaChangedError("DOI CSL response identity conflicts with request")
    for key in ("type", "DOI", "URL", "volume", "issue", "page", "publisher"):
        if key in value and not isinstance(value[key], str):
            raise SchemaChangedError("DOI CSL response member types are invalid")
    for key in ("subtitle", "container-title", "event"):
        if key in value and not (isinstance(value[key], str) or _string_list(value[key])):
            raise SchemaChangedError("DOI CSL response member types are invalid")
    csl_authors = value.get("author")
    if csl_authors is not None and not (
        isinstance(csl_authors, list)
        and all(
            isinstance(author, dict)
            and all(key not in author or isinstance(author[key], str) for key in ("given", "family", "literal"))
            for author in csl_authors
        )
    ):
        raise SchemaChangedError("DOI CSL response member types are invalid")
    for key in ("issued", "published-print", "published-online"):
        if key in value and not _date_parts(value[key]):
            raise SchemaChangedError("DOI CSL response member types are invalid")
    allowed = frozenset(
        {
            "title",
            "subtitle",
            "author",
            "issued",
            "published-print",
            "published-online",
            "container-title",
            "event",
            "type",
            "DOI",
            "URL",
            "volume",
            "issue",
            "page",
            "publisher",
        }
    )
    return {"metadata": {key: value[key] for key in allowed if key in value}}, False


def _scholar_inventory(raw: RawProviderResponse, context: Mapping[str, object]) -> DecodedResponse:
    from .inventory import decode_scholar_inventory

    return decode_scholar_inventory(
        raw.body,
        str(context["profile_id"]),
        _strict_integer(context["offset"], "Scholar offset"),
        _strict_integer(context["page_size"], "Scholar page size"),
        _strict_integer(context["min_year"], "Scholar minimum year"),
    )


def _dblp_inventory(raw: RawProviderResponse, context: Mapping[str, object]) -> DecodedResponse:
    from .inventory import decode_dblp_inventory

    return decode_dblp_inventory(raw.body, str(context["pid"]))


def _strict_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _pubmed_summary(raw: RawProviderResponse, context: Mapping[str, object]) -> DecodedResponse:
    value = _json_object(raw)
    requested_raw = context.get("requested_pmids")
    if not isinstance(requested_raw, (tuple, list)) or not all(isinstance(item, str) for item in requested_raw):
        raise ValueError("PubMed summary decoder requires exact requested PMIDs")
    requested = tuple(requested_raw)
    result = _path(value, "result")
    if not isinstance(result, dict):
        raise SchemaChangedError("PubMed ESummary result is not an object")
    uids = result.get("uids")
    if not isinstance(uids, list) or not all(isinstance(uid, str) for uid in uids):
        raise SchemaChangedError("PubMed ESummary lacks string uids")
    if len(set(uids)) != len(uids) or set(uids) != set(requested):
        raise SchemaChangedError("PubMed ESummary membership mismatch")
    record_keys = {key for key in result if key != "uids" and key.isdigit()}
    if record_keys != set(requested):
        raise SchemaChangedError("PubMed ESummary has unexpected or missing record keys")
    records: dict[str, object] = {}
    for uid in requested:
        member = result.get(uid)
        if (
            not isinstance(member, dict)
            or member.get("uid") != uid
            or not isinstance(member.get("title"), str)
            or not member["title"].strip()
            or not isinstance(member.get("authors"), list)
            or not isinstance(member.get("pubdate"), str)
            or not member["pubdate"].strip()
        ):
            raise SchemaChangedError("PubMed ESummary record identity mismatch")
        authors = cast(list[object], member["authors"])
        articleids = member.get("articleids")
        if not all(isinstance(author, dict) and isinstance(author.get("name"), str) for author in authors) or (
            articleids is not None
            and (
                not isinstance(articleids, list)
                or not all(
                    isinstance(article_id, dict)
                    and isinstance(article_id.get("idtype"), str)
                    and isinstance(article_id.get("value"), str)
                    for article_id in articleids
                )
            )
        ):
            raise SchemaChangedError("PubMed ESummary record member types are invalid")
        for field_name in ("fulljournalname", "source", "volume", "issue", "pages"):
            if field_name in member and not isinstance(member[field_name], str):
                raise SchemaChangedError("PubMed ESummary record member types are invalid")
        allowed = frozenset(
            {
                "uid",
                "title",
                "authors",
                "pubdate",
                "fulljournalname",
                "source",
                "articleids",
                "volume",
                "issue",
                "pages",
            }
        )
        records[uid] = {key: member[key] for key in allowed if key in member}
    return {"records": records}, False


def _pubmed_search(raw: RawProviderResponse, context: Mapping[str, object]) -> DecodedResponse:
    value = _json_object(raw)
    result = _path(value, "esearchresult")
    if isinstance(result, Mapping) and any(result.get(key) for key in ("warninglist", "errorlist")):
        raise SchemaChangedError("PubMed ESearch contains warning or error evidence")
    normalized, empty = _counted_list_decoder(
        "pmids", ("esearchresult", "idlist"), ("esearchresult", "count"), string_members=True
    )(raw, context)
    pmids = normalized["pmids"]
    if not isinstance(pmids, list) or any(not pmid.isdigit() for pmid in pmids) or len(pmids) != len(set(pmids)):
        raise SchemaChangedError("PubMed ESearch contains invalid or duplicate PMIDs")
    return normalized, empty


def _openreview(raw: RawProviderResponse, _context: Mapping[str, object]) -> DecodedResponse:
    value = _json_object(raw)
    notes = value.get("notes")
    if not isinstance(notes, list) or not all(isinstance(note, dict) for note in notes):
        raise SchemaChangedError("OpenReview response lacks notes")
    projected: list[dict[str, object]] = []
    for note in notes:
        note_id = note.get("id")
        content = note.get("content")
        title = content.get("title") if isinstance(content, dict) else None
        if not isinstance(note_id, str) or not note_id or not isinstance(title, str) or not title.strip():
            raise SchemaChangedError("OpenReview note lacks stable ID or title")
        allowed_content = {
            key: content[key]
            for key in ("title", "authors", "authorids", "venue", "venueid", "doi", "pdf", "link", "homepage")
            if key in content
        }
        allowed_content["title"] = title
        projected.append(
            {key: note[key] for key in ("id", "cdate", "tcdate", "authors", "content") if key in note}
            | {"id": note_id, "content": allowed_content}
        )
    return {"notes": projected}, not notes


_OPENALEX_FIELDS = frozenset({"id", "title", "authorships", "publication_year", "primary_location", "doi", "type"})
_EUROPEPMC_FIELDS = frozenset(
    {
        "id",
        "pmid",
        "pmcid",
        "title",
        "authorString",
        "pubYear",
        "journalTitle",
        "bookTitle",
        "pubType",
        "doi",
        "journalVolume",
        "issue",
        "pageInfo",
    }
)


def _gemini(raw: RawProviderResponse, _context: Mapping[str, object]) -> DecodedResponse:
    value = _json_object(raw)
    candidates = value.get("candidates")
    if not isinstance(candidates, list) or not candidates or not all(isinstance(item, dict) for item in candidates):
        raise SchemaChangedError("Gemini response lacks candidates")
    projected: list[dict[str, object]] = []
    for candidate in candidates:
        content = candidate.get("content")
        parts = content.get("parts") if isinstance(content, dict) else None
        if not isinstance(parts, list) or not parts or not all(isinstance(part, dict) for part in parts):
            raise SchemaChangedError("Gemini candidate lacks content parts")
        texts = [part.get("text") for part in parts]
        if not all(isinstance(text, str) and text.strip() for text in texts):
            raise SchemaChangedError("Gemini candidate lacks usable text")
        if candidate.get("finishReason") not in {None, "STOP"}:
            raise SchemaChangedError("Gemini candidate did not finish safely")
        projected.append({"content": {"parts": [{"text": text} for text in texts]}})
    return {"candidates": projected}, False


def _xml_root(raw: RawProviderResponse) -> Element:
    upper = raw.body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("XML contains forbidden declaration")
    try:
        return cast(Element, safe_xml_fromstring(raw.body))
    except Exception as exc:
        raise ValueError("XML is malformed or unsafe") from exc


def _arxiv(raw: RawProviderResponse, _context: Mapping[str, object]) -> DecodedResponse:
    _expected_media(raw, frozenset({"application/atom+xml", "application/xml", "text/xml"}))
    root = _xml_root(raw)
    atom = "http://www.w3.org/2005/Atom"
    if root.tag != f"{{{atom}}}feed":
        raise SchemaChangedError("arXiv response has wrong Atom root")
    entries: list[dict[str, object]] = []
    for entry in root.findall(f"{{{atom}}}entry"):
        title = "".join(entry.findtext(f"{{{atom}}}title", default="")).strip()
        entry_id = entry.findtext(f"{{{atom}}}id", default="").strip()
        published = entry.findtext(f"{{{atom}}}published", default="").strip()
        authors = [node.findtext(f"{{{atom}}}name", default="").strip() for node in entry.findall(f"{{{atom}}}author")]
        if not title or not entry_id or not published or not authors or any(not author for author in authors):
            raise SchemaChangedError("arXiv Atom entry is malformed")
        if published and not re.fullmatch(r"\d{4}-\d{2}-\d{2}T[^\s]+", published):
            raise SchemaChangedError("arXiv Atom entry has malformed publication date")
        alternate = ""
        for link in entry.findall(f"{{{atom}}}link"):
            if link.attrib.get("rel") == "alternate":
                alternate = link.attrib.get("href", "")
        doi = ""
        primary_class = ""
        for node in entry.iter():
            local = node.tag.rsplit("}", 1)[-1]
            if local == "doi" and node.text:
                doi = find_doi_in_text(node.text) or ""
            elif local == "primary_category":
                primary_class = node.attrib.get("term", "")
        arxiv_id = find_arxiv_in_text(alternate or entry_id) or ""
        if not arxiv_id:
            raise SchemaChangedError("arXiv Atom entry lacks canonical identifier")
        entries.append(
            {
                "arxiv_id": arxiv_id,
                "authors": authors,
                "doi": doi,
                "primary_class": primary_class,
                "title": title,
                "abs_url": alternate or entry_id,
                "year": int(published[:4]) if published else None,
            }
        )
    total: int | None = None
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == "totalResults" and node.text:
            try:
                total = int(node.text.strip())
            except ValueError as exc:
                raise SchemaChangedError("arXiv totalResults is malformed") from exc
    if total is None or total < len(entries) or (not entries and total != 0):
        raise SchemaChangedError("arXiv totalResults conflicts with feed entries")
    return {"entries": entries}, not entries


def _expected_media(raw: RawProviderResponse, allowed: frozenset[str]) -> None:
    if raw.content_type not in allowed:
        raise SchemaChangedError("provider returned incompatible media type")


def _doi_bibtex(raw: RawProviderResponse, context: Mapping[str, object]) -> DecodedResponse:
    _expected_media(raw, frozenset({"application/x-bibtex", "text/x-bibtex"}))
    try:
        text = raw.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("DOI BibTeX is not UTF-8") from exc
    parser = BibTexParser(common_strings=False)
    parser.expect_multiple_parse = True
    try:
        database = bibtexparser.loads(text, parser=parser)
    except (TypeError, UndefinedString, ValueError) as exc:
        raise ValueError("DOI BibTeX is malformed") from exc
    if database.comments or database.preambles or database.strings:
        raise ValueError("DOI BibTeX contains unparsed text or directives")
    entries = database.entries
    if len(entries) != 1:
        raise SchemaChangedError("DOI BibTeX must contain exactly one entry")
    normalized = parse_bibtex_to_dict(text)
    if normalized is None or not normalized.get("fields", {}).get("title"):
        raise SchemaChangedError("DOI BibTeX lacks normalized title metadata")
    requested = normalize_doi(str(context.get("doi", "")))
    fields = normalized.get("fields", {})
    returned = normalize_doi(str(fields.get("doi", ""))) if isinstance(fields, Mapping) else ""
    if requested and returned and requested != returned:
        raise SchemaChangedError("DOI BibTeX response identity conflicts with request")
    return {"metadata": normalized}, False


class _DoiHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.evidence: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name.casefold(): value or "" for name, value in attrs}
        if tag.casefold() == "meta" and values.get("name", "").casefold() in {
            "citation_doi",
            "dc.identifier",
        }:
            self._add(values.get("content", ""))
        if tag.casefold() == "meta" and values.get("property", "").casefold() == "og:doi":
            self._add(values.get("content", ""))
        if tag.casefold() == "link" and values.get("rel", "").casefold() in {"canonical", "alternate"}:
            href = values.get("href", "")
            if urlsplit(href).hostname in {"doi.org", "dx.doi.org"}:
                self._add(href)

    def _add(self, value: str) -> None:
        doi = normalize_doi(find_doi_in_text(value) or value)
        if doi:
            self.evidence.add(doi)


def _html_doi(raw: RawProviderResponse, _context: Mapping[str, object]) -> DecodedResponse:
    _expected_media(raw, frozenset({"text/html", "application/xhtml+xml"}))
    try:
        text = raw.body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("HTML DOI response is not UTF-8") from exc
    parser = _DoiHTMLParser()
    if urlsplit(raw.final_url).hostname in {"doi.org", "dx.doi.org"}:
        parser._add(raw.final_url)
    try:
        parser.feed(text)
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise ValueError("HTML DOI response is malformed") from exc
    if len(parser.evidence) > 1:
        raise SchemaChangedError("HTML contains conflicting DOI evidence")
    return {"doi": next(iter(parser.evidence), None)}, False


def _definition(
    callback_id: str, schema: str, media_type: ResponseMediaType, callback: DecoderCallback
) -> DecoderDefinition:
    return DecoderDefinition(callback_id, "1", schema, media_type, callback)


def _build_definitions() -> tuple[DecoderDefinition, ...]:
    entries: list[tuple[str, str, ResponseMediaType, DecoderCallback]] = [
        ("scholar.inventory.v1.decoder", "serpapi-scholar-author-v1", ResponseMediaType.JSON, _scholar_inventory),
        ("dblp.inventory.v1.decoder", "dblpperson-v1", ResponseMediaType.XML, _dblp_inventory),
        ("doi_csl.csl_lookup.v1.decoder", "doi-csl-v1", ResponseMediaType.JSON, _doi_csl),
        ("doi_bibtex.bibtex_lookup.v1.decoder", "doi-bibtex-v1", ResponseMediaType.BIBTEX, _doi_bibtex),
        (
            "serply.scholar_search.v1.decoder",
            "serply-scholar-v1",
            ResponseMediaType.JSON,
            _list_decoder(
                "articles",
                "articles",
                allowed_fields=frozenset({"title", "link", "id", "description", "author", "extras"}),
                required_fields=frozenset({"title"}),
                string_fields=frozenset({"title"}),
            ),
        ),
        (
            "s2.fuzzy_search.v1.decoder",
            "s2-search-v1",
            ResponseMediaType.JSON,
            _counted_list_decoder(
                "results",
                ("data",),
                ("total",),
                allowed_fields=frozenset(
                    {
                        "paperId",
                        "title",
                        "authors",
                        "year",
                        "venue",
                        "journal",
                        "externalIds",
                        "url",
                        "abstract",
                        "citationCount",
                        "publicationDate",
                        "publicationTypes",
                    }
                ),
                required_fields=frozenset({"paperId", "title"}),
                string_fields=frozenset({"paperId", "title"}),
                record_validator=_s2_record_is_valid,
            ),
        ),
        (
            "s2.fuzzy_search.v2.decoder",
            "s2-search-v2",
            ResponseMediaType.JSON,
            _counted_list_decoder(
                "results",
                ("data",),
                ("total",),
                allowed_fields=frozenset(
                    {
                        "paperId",
                        "title",
                        "authors",
                        "year",
                        "venue",
                        "journal",
                        "externalIds",
                        "url",
                        "abstract",
                        "citationCount",
                        "publicationDate",
                        "publicationTypes",
                    }
                ),
                required_fields=frozenset({"paperId", "title"}),
                string_fields=frozenset({"paperId", "title"}),
                record_validator=_s2_record_is_valid,
            ),
        ),
        (
            "crossref.fuzzy_search.v1.decoder",
            "crossref-search-v1",
            ResponseMediaType.JSON,
            _counted_list_decoder(
                "results",
                ("message", "items"),
                ("message", "total-results"),
                success_path=("status",),
                allowed_fields=frozenset(
                    {
                        "DOI",
                        "title",
                        "author",
                        "issued",
                        "published-print",
                        "published-online",
                        "container-title",
                        "publisher",
                        "type",
                        "URL",
                        "volume",
                        "issue",
                        "page",
                    }
                ),
                required_fields=frozenset({"title"}),
                record_validator=_crossref_title_is_valid,
            ),
        ),
        ("openreview.term_search.v1.decoder", "openreview-notes-v1", ResponseMediaType.JSON, _openreview),
        ("openreview.fallback_search.v1.decoder", "openreview-search-v1", ResponseMediaType.JSON, _openreview),
        ("arxiv.fuzzy_search.v1.decoder", "arxiv-atom-v1", ResponseMediaType.XML, _arxiv),
        (
            "openalex.fuzzy_search.v1.decoder",
            "openalex-search-v1",
            ResponseMediaType.JSON,
            _counted_list_decoder(
                "results",
                ("results",),
                ("meta", "count"),
                allowed_fields=_OPENALEX_FIELDS,
                required_fields=frozenset({"id", "title"}),
                string_fields=frozenset({"id", "title"}),
                record_validator=_openalex_record_is_valid,
            ),
        ),
        (
            "pubmed.title_search.v1.decoder",
            "pubmed-esearch-v1",
            ResponseMediaType.JSON,
            _pubmed_search,
        ),
        ("pubmed.summary.v1.decoder", "pubmed-esummary-v1", ResponseMediaType.JSON, _pubmed_summary),
        (
            "europepmc.fuzzy_search.v1.decoder",
            "europepmc-search-v1",
            ResponseMediaType.JSON,
            _counted_list_decoder(
                "results",
                ("resultList", "result"),
                ("hitCount",),
                allowed_fields=_EUROPEPMC_FIELDS,
                required_fields=frozenset({"title"}),
                string_fields=frozenset({"title"}),
                record_validator=_europepmc_record_is_valid,
            ),
        ),
        (
            "crossref.venue_search.v1.decoder",
            "crossref-venue-v1",
            ResponseMediaType.JSON,
            _counted_list_decoder(
                "results",
                ("message", "items"),
                ("message", "total-results"),
                success_path=("status",),
                allowed_fields=frozenset(
                    {
                        "DOI",
                        "title",
                        "author",
                        "issued",
                        "published-print",
                        "published-online",
                        "container-title",
                        "publisher",
                        "type",
                        "URL",
                        "volume",
                        "issue",
                        "page",
                    }
                ),
                required_fields=frozenset({"title"}),
                record_validator=_crossref_title_is_valid,
            ),
        ),
        (
            "openalex.venue_search.v1.decoder",
            "openalex-venue-v1",
            ResponseMediaType.JSON,
            _counted_list_decoder(
                "results",
                ("results",),
                ("meta", "count"),
                allowed_fields=_OPENALEX_FIELDS,
                required_fields=frozenset({"id", "title"}),
                string_fields=frozenset({"id", "title"}),
                record_validator=_openalex_record_is_valid,
            ),
        ),
        ("web.doi_probe.v1.decoder", "html-doi-v1", ResponseMediaType.HTML, _html_doi),
        ("gemini.short_title.v1.decoder", "gemini-short-title-v1", ResponseMediaType.JSON, _gemini),
    ]
    return tuple(_definition(*entry) for entry in entries)


def _definitions() -> Mapping[str, DecoderDefinition]:
    values = _build_definitions()
    if len({item.callback_id for item in values}) != len(values):
        raise RuntimeError("duplicate durable decoder ID")
    return MappingProxyType({item.callback_id: item for item in values})


_DECODER_AUTHORITY: Mapping[str, DecoderDefinition] = _definitions()
DECODERS: Mapping[str, DecoderDefinition] = MappingProxyType(
    {
        key: DecoderDefinition(value.callback_id, value.version, value.schema, value.media_type)
        for key, value in _DECODER_AUTHORITY.items()
    }
)


def _validate_registry_bindings() -> None:
    from .capabilities import CAPABILITIES

    expected = {item.decoder_id for item in CAPABILITIES.values()}
    if set(_DECODER_AUTHORITY) != expected:
        raise RuntimeError("durable capability decoder registry is incomplete")
    for capability in CAPABILITIES.values():
        decoder = _DECODER_AUTHORITY[capability.decoder_id]
        if (
            decoder.callback_id != capability.decoder_id
            or decoder.version != capability.decoder_version
            or decoder.schema != capability.decoder_schema
            or decoder.media_type is not capability.media_type
        ):
            raise RuntimeError("durable capability decoder binding mismatch")


_validate_registry_bindings()


def decode_response(
    decoder_id: str, raw: RawProviderResponse, context: Mapping[str, object] | None = None
) -> DecodedResponse:
    try:
        decoder = _DECODER_AUTHORITY[decoder_id]
    except KeyError as exc:
        raise ValueError("unknown durable decoder") from exc
    allowed_media = {
        ResponseMediaType.JSON: frozenset({"application/json", "application/vnd.citationstyles.csl+json"}),
        ResponseMediaType.XML: frozenset({"application/atom+xml", "application/xml", "text/xml"}),
        ResponseMediaType.BIBTEX: frozenset({"application/x-bibtex", "text/x-bibtex"}),
        ResponseMediaType.HTML: frozenset({"text/html", "application/xhtml+xml"}),
    }
    if decoder_id.startswith("crossref."):
        allowed_media[ResponseMediaType.JSON] = allowed_media[ResponseMediaType.JSON] | {
            "application/vnd.crossref-api-message+json"
        }
    _expected_media(raw, allowed_media[decoder.media_type])
    if decoder.callback is None:
        raise RuntimeError("authoritative decoder callback is missing")
    return decoder.callback(raw, MappingProxyType(dict(context or {})))


__all__ = ["DECODERS", "DecodedResponse", "DecoderDefinition", "decode_response"]
