"""Typed, pure author-inventory capabilities, decoders, and union reduction."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from urllib.parse import parse_qs, urlencode, urlsplit

from defusedxml.ElementTree import fromstring as safe_xml_fromstring

from ..clients.helpers import _sanitize_dblp_author
from ..config import DBLP_PERSON_BASE, HTTP_TIMEOUT_DEFAULT, PREPRINT_SERVERS, SERPAPI_BASE
from ..id_utils import find_doi_in_text, is_secondary_doi, normalize_doi
from ..identity import IdentityContext, evaluate_identity
from ..text_utils import normalize_title
from .census import AuthorCensusRow
from .ledger import Ledger, PublicationMetadata, RequestSpec, TaskClaim, TaskSpec
from .transport import SchemaChangedError, SendOperation
from .types import TaskDisposition


class ResponseMediaType(str, Enum):
    JSON = "json"
    XML = "xml"


class CredentialKind(str, Enum):
    NONE = "none"
    SERPAPI_KEY = "serpapi_key"


@dataclass(frozen=True)
class AdapterCapability:
    logical_source: str
    operation: str
    adapter_version: str
    capability_id: str
    wire_provider: str
    quota_scope: str
    media_type: ResponseMediaType
    credential_kind: CredentialKind
    decoder_schema: str
    requested_fields: tuple[str, ...]

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "adapter_version": self.adapter_version,
                "capability_id": self.capability_id,
                "credential_kind": self.credential_kind.value,
                "decoder_schema": self.decoder_schema,
                "logical_source": self.logical_source,
                "media_type": self.media_type.value,
                "operation": self.operation,
                "quota_scope": self.quota_scope,
                "requested_fields": self.requested_fields,
                "wire_provider": self.wire_provider,
            }
        )


@dataclass(frozen=True)
class RefreshCredentials:
    serpapi_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class InventoryPolicy:
    min_year: int
    max_publications: int
    max_scholar_pages: int
    doi_adapter_version: str = "1"
    s2_adapter_version: str = "1"
    freshness_epoch: str = "current"

    def __post_init__(self) -> None:
        if self.min_year < 1000 or self.max_publications < 1 or self.max_scholar_pages < 1:
            raise ValueError("invalid inventory policy")
        if not self.doi_adapter_version or not self.s2_adapter_version or not self.freshness_epoch:
            raise ValueError("inventory policy requires seed adapter and freshness identity")


def _freeze(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    raise TypeError("inventory evidence must be strict JSON")


@dataclass(frozen=True)
class SnapshotContribution:
    task_key: str
    logical_source: str
    disposition: TaskDisposition
    decoder_schema: str
    observation_digest: str
    articles: tuple[Mapping[str, object], ...]
    offset: int | None = None
    next_offset: int | None = None
    request_key: str = ""
    capability_id: str = ""
    topology_digest: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "articles", tuple(_freeze(dict(item)) for item in self.articles))


@dataclass(frozen=True)
class InventorySnapshot:
    author_key: str
    contributions: tuple[SnapshotContribution, ...]

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(self.contributions, key=lambda item: (item.logical_source, item.offset or 0, item.task_key))
        )
        object.__setattr__(self, "contributions", ordered)

    @property
    def digest(self) -> str:
        content = [
            {
                "articles": [dict(article) for article in contribution.articles],
                "decoder_schema": contribution.decoder_schema,
                "disposition": contribution.disposition.value,
                "logical_source": contribution.logical_source,
                "next_offset": contribution.next_offset,
                "observation_digest": contribution.observation_digest,
                "offset": contribution.offset,
                "request_key": contribution.request_key,
                "capability_id": contribution.capability_id,
                "topology_digest": contribution.topology_digest,
                "task_key": contribution.task_key,
            }
            for contribution in self.contributions
        ]
        return hashlib.sha256(
            json.dumps(content, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        ).hexdigest()


@dataclass(frozen=True)
class InventoryReduction:
    author_key: str
    publications: tuple[PublicationMetadata, ...]
    seed_tasks: tuple[TaskSpec, ...]
    snapshot_digest: str


@dataclass(frozen=True)
class PageWave:
    tasks: tuple[TaskSpec, ...]
    source_task_keys: tuple[str, ...]
    digest: str


_CAPABILITIES = (
    AdapterCapability(
        "scholar",
        "inventory",
        "1",
        "scholar.inventory.v1",
        "serpapi",
        "serpapi",
        ResponseMediaType.JSON,
        CredentialKind.SERPAPI_KEY,
        "serpapi-scholar-author-v1",
        ("articles",),
    ),
    AdapterCapability(
        "dblp",
        "inventory",
        "1",
        "dblp.inventory.v1",
        "dblp",
        "dblp",
        ResponseMediaType.XML,
        CredentialKind.NONE,
        "dblpperson-v1",
        ("articles",),
    ),
    AdapterCapability(
        "doi_csl",
        "csl_lookup",
        "1",
        "doi_csl.csl_lookup.v1",
        "doi",
        "doi",
        ResponseMediaType.JSON,
        CredentialKind.NONE,
        "doi-csl-v1",
        ("metadata",),
    ),
    AdapterCapability(
        "s2",
        "fuzzy_search",
        "1",
        "s2.fuzzy_search.v1",
        "s2",
        "s2",
        ResponseMediaType.JSON,
        CredentialKind.NONE,
        "s2-search-v1",
        ("results",),
    ),
)


def capability_for(logical_source: str, operation: str, adapter_version: str) -> AdapterCapability:
    matches = [
        item
        for item in _CAPABILITIES
        if (item.logical_source, item.operation, item.adapter_version) == (logical_source, operation, adapter_version)
    ]
    if len(matches) != 1:
        raise ValueError("exactly one adapter capability is required")
    return matches[0]


def build_inventory_task(
    row: AuthorCensusRow,
    capability: AdapterCapability,
    freshness_epoch: str,
    policy: InventoryPolicy,
    *,
    offset: int = 0,
) -> TaskSpec:
    if capability.logical_source == "scholar":
        profile_id = row.scholar_id
        payload: dict[str, object] = {
            "author_key": row.row_key,
            "min_year": policy.min_year,
            "num": 100,
            "profile_id": profile_id,
            "sort": "pubdate",
            "start": offset,
        }
    elif capability.logical_source == "dblp":
        profile_id = row.dblp_id
        payload = {"author_key": row.row_key, "pid": profile_id}
    else:
        raise ValueError("capability is not an inventory capability")
    if not profile_id:
        raise ValueError("inventory capability requires an exact profile identifier")
    request = RequestSpec(
        capability.logical_source,
        capability.operation,
        "GET",
        payload,
        capability.requested_fields,
        capability.adapter_version,
        freshness_epoch,
        capability.quota_scope,
    )
    return TaskSpec(row.row_key, None, capability.logical_source, capability.operation, request)


def _strict_object(body: bytes) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(
        body.decode("utf-8"),
        object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"invalid constant {value}")),
    )
    if not isinstance(value, dict):
        raise ValueError("inventory response must be an object")
    return value


def _safe_url(value: object) -> str:
    if not value:
        return ""
    if not isinstance(value, str):
        raise ValueError("article link must be a string")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("article link is unsafe")
    return value


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _names(value: object) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)) or not all(isinstance(item, str) for item in value):
        return ()
    return tuple(value)


def decode_scholar_inventory(
    body: bytes, profile_id: str, offset: int, page_size: int, min_year: int
) -> tuple[Mapping[str, object], bool]:
    value = _strict_object(body)
    if "error" in value:
        raise SchemaChangedError("provider error envelope")
    metadata = value.get("search_metadata")
    parameters = value.get("search_parameters")
    author = value.get("author")
    articles = value.get("articles")
    pagination = value.get("serpapi_pagination", {})
    if not isinstance(metadata, dict) or metadata.get("status") != "Success":
        raise SchemaChangedError("Scholar response lacks successful metadata")
    profile_url = metadata.get("google_scholar_author_url")
    if not isinstance(profile_url, str):
        raise SchemaChangedError("Scholar response lacks exact profile URL evidence")
    parsed_profile = urlsplit(profile_url)
    profile_query = parse_qs(parsed_profile.query)
    if (
        parsed_profile.scheme != "https"
        or parsed_profile.hostname not in {"scholar.google.com", "scholar.google.ca"}
        or parsed_profile.path not in {"/citations", "/citations/"}
        or parsed_profile.username
        or parsed_profile.password
        or profile_query.get("user") != [profile_id]
    ):
        raise SchemaChangedError("Scholar response profile URL does not match requested author")
    parameter_offset = parameters.get("cstart") if isinstance(parameters, dict) else None
    if parameter_offset is not None and (
        isinstance(parameter_offset, bool) or not isinstance(parameter_offset, int) or parameter_offset != offset
    ):
        raise SchemaChangedError("Scholar response has malformed offset evidence")
    if not isinstance(parameters, dict) or (
        parameters.get("engine") != "google_scholar_author" or parameters.get("author_id") != profile_id
    ):
        raise SchemaChangedError("Scholar response lacks exact request evidence")
    if not isinstance(author, dict) or not isinstance(author.get("name"), str) or not author["name"].strip():
        raise SchemaChangedError("Scholar response lacks author profile metadata")
    if not isinstance(articles, list) or not isinstance(pagination, dict):
        raise SchemaChangedError("Scholar response has wrong envelope")
    normalized = []
    for item in articles:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not item["title"].strip():
            raise SchemaChangedError("Scholar response contains malformed article")
        year_raw = item.get("year")
        if year_raw in {None, ""}:
            year = None
        elif isinstance(year_raw, int) and not isinstance(year_raw, bool):
            year = year_raw
        elif isinstance(year_raw, str) and year_raw.isdigit():
            year = int(year_raw)
        else:
            raise SchemaChangedError("Scholar article has malformed year")
        citation_id = item.get("citation_id")
        if not isinstance(citation_id, str) or not citation_id:
            raise SchemaChangedError("Scholar article lacks citation identity")
        authors = item.get("authors", "")
        if not isinstance(authors, str):
            raise SchemaChangedError("Scholar article authors are malformed")
        normalized.append(
            {
                "authors": [part.strip() for part in authors.split(",") if part.strip()],
                "citation_id": citation_id,
                "publication": str(item.get("publication") or ""),
                "title": item["title"].strip(),
                "url": _safe_url(item.get("link")),
                "year": year,
            }
        )
    next_url = pagination.get("next")
    next_offset = None
    if next_url:
        parsed = urlsplit(_safe_url(next_url))
        query = parse_qs(parsed.query)
        try:
            candidate = int(query["cstart"][0])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ValueError("Scholar continuation lacks trusted offset") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"serpapi.com", "www.serpapi.com"}
            or parsed.path not in {"/search", "/search.json"}
            or candidate != offset + page_size
            or query.get("engine") != ["google_scholar_author"]
            or query.get("author_id") != [profile_id]
            or query.get("num", [str(page_size)]) != [str(page_size)]
            or query.get("sort", ["pubdate"]) != ["pubdate"]
            or not set(query) <= {"api_key", "author_id", "cstart", "engine", "hl", "num", "sort"}
        ):
            raise SchemaChangedError("Scholar continuation identity is inconsistent")
        next_offset = candidate
    if not normalized and next_offset is not None:
        raise SchemaChangedError("Scholar empty page cannot continue")
    if normalized and next_offset is not None:
        years = [item["year"] for item in normalized]
        if min_year and all(isinstance(year, int) and year < min_year for year in years):
            next_offset = None
        elif min_year and not any(isinstance(year, int) for year in years):
            raise SchemaChangedError("Scholar page cannot prove safe year stop")
    return {"articles": normalized, "next_offset": next_offset, "offset": offset}, not normalized


_DBLP_TAGS = frozenset(
    {"article", "inproceedings", "incollection", "book", "proceedings", "phdthesis", "mastersthesis"}
)
_YEAR = re.compile(r"\d{4}")


def decode_dblp_inventory(body: bytes, pid: str) -> tuple[Mapping[str, object], bool]:
    upper = body.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise ValueError("DBLP returned forbidden XML declarations")
    try:
        root = safe_xml_fromstring(body)
    except Exception as exc:
        raise ValueError("DBLP returned unsafe or malformed XML") from exc
    exact_pid = root.attrib.get("pid") == pid or root.attrib.get("key") == f"homepages/{pid}"
    if root.tag != "dblpperson" or not exact_pid:
        raise SchemaChangedError("DBLP root or PID mismatch")
    articles = []
    saw_person = False
    for wrapper in root:
        if wrapper.tag == "person":
            if saw_person or wrapper.attrib.get("key") not in {None, f"homepages/{pid}"}:
                raise SchemaChangedError("DBLP person metadata is malformed")
            saw_person = True
            continue
        if wrapper.tag == "coauthors":
            continue
        if wrapper.tag != "r" or len(wrapper) != 1:
            raise SchemaChangedError("DBLP response has unexpected structure")
        record = wrapper[0]
        if record.tag not in _DBLP_TAGS or not record.attrib.get("key"):
            raise SchemaChangedError("DBLP response contains unsupported record")
        title_node = record.find("title")
        title = "".join(title_node.itertext()).strip() if title_node is not None else ""
        if not title:
            raise SchemaChangedError("DBLP record lacks title")
        year_text = (record.findtext("year") or "").strip()
        if year_text and not _YEAR.fullmatch(year_text):
            raise SchemaChangedError("DBLP record has malformed year")
        authors = [_sanitize_dblp_author("".join(node.itertext()).strip()) for node in record.findall("author")]
        editors = [_sanitize_dblp_author("".join(node.itertext()).strip()) for node in record.findall("editor")]
        ee = (record.findtext("ee") or "").strip()
        url = (record.findtext("url") or "").strip()
        safe_record_url = f"https://dblp.org/rec/{record.attrib['key']}" if url and not urlsplit(url).scheme else url
        doi = normalize_doi(find_doi_in_text(ee) or find_doi_in_text(url))
        articles.append(
            {
                "authors": [name for name in authors if name],
                "doi": doi or "",
                "editors": [name for name in editors if name],
                "publication": (record.findtext("journal") or record.findtext("booktitle") or "").strip(),
                "record_key": record.attrib["key"],
                "record_type": record.tag,
                "title": title,
                "url": _safe_url(ee or safe_record_url),
                "year": int(year_text) if year_text else None,
            }
        )
    return {"articles": articles, "pid": pid}, not articles


def build_claimed_inventory_operation(
    ledger: Ledger,
    claim: TaskClaim,
    credentials: RefreshCredentials,
    policy: InventoryPolicy,
    *,
    now: datetime,
) -> SendOperation:
    """Reconstruct a claimed logical inventory and add wire-only credentials."""
    task = ledger.reconstruct_claimed_task(claim, now)
    if task.request is None:
        raise ValueError("claimed inventory lacks exact request")
    request = task.request
    capability = capability_for(task.provider, task.operation, request.adapter_version)
    if request.requested_fields != capability.requested_fields or request.quota_scope != capability.quota_scope:
        raise ValueError("claimed request does not match capability")
    payload = dict(request.normalized_payload)
    if capability.logical_source == "scholar":
        if (
            capability.wire_provider != "serpapi"
            or capability.media_type is not ResponseMediaType.JSON
            or capability.credential_kind is not CredentialKind.SERPAPI_KEY
        ):
            raise ValueError("Scholar capability has invalid physical transport binding")
        if not credentials.serpapi_key:
            raise ValueError("missing SerpAPI credential")
        query = {
            "api_key": credentials.serpapi_key,
            "author_id": payload["profile_id"],
            "engine": "google_scholar_author",
            "num": payload["num"],
            "sort": payload["sort"],
            "start": payload["start"],
        }
        url = f"{SERPAPI_BASE}?{urlencode(query)}"

        def decoder(body: bytes) -> tuple[Mapping[str, object], bool]:
            return decode_scholar_inventory(
                body,
                str(payload["profile_id"]),
                _integer(payload["start"], "Scholar start"),
                _integer(payload["num"], "Scholar page size"),
                policy.min_year,
            )

    elif capability.logical_source == "dblp":
        if (
            capability.wire_provider != "dblp"
            or capability.media_type is not ResponseMediaType.XML
            or capability.credential_kind is not CredentialKind.NONE
        ):
            raise ValueError("DBLP capability has invalid physical transport binding")
        pid = str(payload["pid"])
        url = f"{DBLP_PERSON_BASE}/{pid}.xml"

        def decoder(body: bytes) -> tuple[Mapping[str, object], bool]:
            return decode_dblp_inventory(body, pid)
    else:
        raise ValueError("claim is not an inventory capability")
    return SendOperation(
        request,
        url,
        HTTP_TIMEOUT_DEFAULT,
        lambda _value: {},
        lambda _value: False,
        response_decoder=decoder,
        decoder_schema=capability.decoder_schema,
    )


def plan_scholar_page_wave(
    rows: Mapping[str, AuthorCensusRow],
    contributions: Mapping[str, Sequence[SnapshotContribution]],
    freshness_epoch: str,
    adapter_version: str,
    policy: InventoryPolicy,
) -> PageWave:
    """Derive one deterministic aggregate pagination wave from terminal pages."""
    tasks: list[TaskSpec] = []
    sources: list[str] = []
    seen_pairs: set[tuple[str, int]] = set()
    capability = capability_for("scholar", "inventory", adapter_version)
    flattened = sorted(
        ((author_key, contribution) for author_key, pages in contributions.items() for contribution in pages),
        key=lambda item: (item[0], item[1].task_key),
    )
    for author_key, contribution in flattened:
        if contribution.logical_source != "scholar" or contribution.offset is None:
            raise ValueError("page wave accepts only exact Scholar page evidence")
        if not author_key or author_key not in rows:
            raise ValueError("Scholar page lacks exact author topology")
        pair = (author_key, contribution.offset)
        if pair in seen_pairs:
            raise ValueError("Scholar page topology repeats an offset")
        seen_pairs.add(pair)
        sources.append(contribution.task_key)
        if contribution.next_offset is None:
            continue
        if contribution.next_offset != contribution.offset + 100:
            raise ValueError("Scholar page topology has a gap, fork, or cycle")
        page_number = contribution.next_offset // 100
        if page_number >= policy.max_scholar_pages:
            raise ValueError("Scholar continuation reached configured page bound")
        tasks.append(
            build_inventory_task(rows[author_key], capability, freshness_epoch, policy, offset=contribution.next_offset)
        )
    ordered = tuple(sorted(tasks, key=lambda item: item.key))
    content = {"sources": sorted(sources), "tasks": [item.key for item in ordered]}
    return PageWave(
        ordered,
        tuple(sorted(sources)),
        hashlib.sha256(json.dumps(content, separators=(",", ":"), sort_keys=True).encode()).hexdigest(),
    )


def _record(article: Mapping[str, object]) -> dict[str, object]:
    return {
        "authors": " and ".join(_names(article.get("authors"))),
        "doi": article.get("doi", ""),
        "title": article.get("title", ""),
        "year": article.get("year"),
    }


def reduce_author_inventory(
    census_row: AuthorCensusRow, snapshot: InventorySnapshot, policy: InventoryPolicy
) -> InventoryReduction:
    if snapshot.author_key != census_row.row_key:
        raise ValueError("inventory snapshot belongs to another author")
    articles = [
        dict(article)
        for contribution in snapshot.contributions
        if contribution.disposition is TaskDisposition.SUCCEEDED
        for article in contribution.articles
        if isinstance(article.get("year"), int) and _integer(article["year"], "publication year") >= policy.min_year
    ]
    merged: list[dict[str, object]] = []
    for candidate in sorted(articles, key=lambda item: (normalize_title(str(item.get("title") or "")), str(item))):
        match = next(
            (
                existing
                for existing in merged
                if not _preprint_published_pair(existing, candidate)
                if not (
                    normalize_doi(_optional_text(existing.get("doi")))
                    and normalize_doi(_optional_text(candidate.get("doi")))
                    and normalize_doi(_optional_text(existing.get("doi")))
                    != normalize_doi(_optional_text(candidate.get("doi")))
                )
                if evaluate_identity(
                    _record(existing),
                    _record(candidate),
                    context=IdentityContext.IMPORT_LIST,
                    target_author=census_row.name,
                ).verdict
            ),
            None,
        )
        if match is None:
            merged.append(candidate)
            continue
        for key, value in candidate.items():
            if not match.get(key) or (key == "authors" and len(_names(value)) > len(_names(match.get(key)))):
                match[key] = value
    if len(merged) > policy.max_publications:
        raise ValueError("inventory exceeds configured publication bound")
    publications = []
    seeds = []
    for article in sorted(
        merged,
        key=lambda item: (
            -(_integer(item.get("year"), "publication year") if isinstance(item.get("year"), int) else 0),
            normalize_title(str(item["title"])),
        ),
    ):
        doi = normalize_doi(_optional_text(article.get("doi")))
        stable_identity = doi or f"{normalize_title(str(article['title']))}\0{article.get('year') or ''}"
        publication_key = hashlib.sha256(f"{census_row.row_key}\0{stable_identity}".encode()).hexdigest()
        identifiers = {"doi": doi} if doi else {}
        metadata = PublicationMetadata(
            census_row.row_key,
            publication_key,
            "scholar" if article.get("citation_id") else "dblp",
            normalize_title(str(article["title"])),
            _integer(article["year"], "publication year") if isinstance(article.get("year"), int) else None,
            identifiers,
            "",
            "monthly",
        )
        publications.append(metadata)
        if doi:
            request = RequestSpec(
                "doi_csl",
                "csl_lookup",
                "GET",
                {"doi": doi},
                ("metadata",),
                policy.doi_adapter_version,
                policy.freshness_epoch,
                "doi",
            )
            seeds.append(TaskSpec(census_row.row_key, publication_key, "doi_csl", "csl_lookup", request))
        else:
            payload = {
                "author_key": census_row.row_key,
                "title": metadata.normalized_title,
                "year": metadata.year,
            }
            request = RequestSpec(
                "s2",
                "fuzzy_search",
                "GET",
                payload,
                ("results",),
                policy.s2_adapter_version,
                policy.freshness_epoch,
                "s2",
            )
            seeds.append(TaskSpec(census_row.row_key, publication_key, "s2", "fuzzy_search", request))
    return InventoryReduction(census_row.row_key, tuple(publications), tuple(seeds), snapshot.digest)


def _preprint_published_pair(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    def preprint(item: Mapping[str, object]) -> bool:
        doi = normalize_doi(_optional_text(item.get("doi")))
        venue = str(item.get("publication") or "").casefold()
        return bool(doi and is_secondary_doi(doi)) or any(name.casefold() in venue for name in PREPRINT_SERVERS)

    return preprint(left) != preprint(right)


__all__ = [
    "AdapterCapability",
    "CredentialKind",
    "InventoryPolicy",
    "InventoryReduction",
    "InventorySnapshot",
    "PageWave",
    "RefreshCredentials",
    "ResponseMediaType",
    "SnapshotContribution",
    "build_claimed_inventory_operation",
    "build_inventory_task",
    "capability_for",
    "decode_dblp_inventory",
    "decode_scholar_inventory",
    "plan_scholar_page_wave",
    "reduce_author_inventory",
]
