"""Pure venue, late-identifier, merge, and intent authority for Task 5C.5."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import cast

from ..api_configs import (
    ARXIV_FIELD_MAPPING,
    CROSSREF_FIELD_MAPPING,
    CROSSREF_VENUE_SEARCH_CONFIG,
    OPENALEX_FIELD_MAPPING,
    OPENALEX_VENUE_SEARCH_CONFIG,
    OPENREVIEW_FIELD_MAPPING,
    S2_FIELD_MAPPING,
)
from ..api_generics import project_entry_from_response
from ..bibtex_build import create_scoring_function, format_author_field
from ..canonicalize import CanonicalStage, canonicalize
from ..config import BIBTEX_KEY_MAX_WORDS, PUB_PARSE_TIER1_MIN_CONFIDENCE, TRUST_ORDER
from ..id_utils import find_doi_in_text, normalize_doi
from ..identity import IdentityContext, evaluate_identity
from ..merge_utils import merge_with_policy
from ..publication_parser import parse_publication_string
from ..text_utils import extract_authors_from_any, extract_year_from_any, safe_get_field
from .authority import (
    EvidenceKind,
    IntentKind,
    MaterializationIntent,
    ProvenanceContribution,
    ProvenanceDecision,
    PublicationSeedEvidence,
    evidence_digest,
)
from .capabilities import (
    GEMINI_GENERATION_CONFIG,
    GEMINI_MODEL_ID,
    GEMINI_PROMPT_VERSION,
    build_request,
    capability_for,
)
from .discovery import (
    ApplicabilityReason,
    DiscoveryAuthority,
    DiscoveryDecision,
    DiscoveryObservation,
    DiscoveryWave,
    DoiReduction,
    doi_reduction_is_authoritatively_complete,
)
from .ledger import RequestSpec, TaskSpec
from .privacy import ensure_public_https_url, ensure_safe_durable_key, ensure_safe_durable_text
from .types import TaskDisposition

_BROAD_OPERATIONS = frozenset(
    {
        ("arxiv", "fuzzy_search"),
        ("crossref", "fuzzy_search"),
        ("europepmc", "fuzzy_search"),
        ("openalex", "fuzzy_search"),
        ("openreview", "term_search"),
        ("pubmed", "title_search"),
        ("s2", "fuzzy_search"),
        ("serply", "scholar_search"),
    }
)
_SATISFIED = frozenset(
    {
        TaskDisposition.SUCCEEDED,
        TaskDisposition.CONFIRMED_EMPTY,
        TaskDisposition.NOT_APPLICABLE,
        TaskDisposition.DOMINATED,
    }
)
_VENUE_FETCH_LIMIT = 10
_VENUE_ADMISSION_LIMIT = 5
_VENUE_SCORE_THRESHOLD = 0.8
_VENUE_IDENTITY_POLICY_VERSION = "enrichment-v1"
_MERGE_REDUCER_VERSION = "task5c5-merge-v1"
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_LATE_IDENTIFIER_KINDS = frozenset({"arxiv", "doi", "openalex_id", "pmid", "s2_corpus_id", "s2_paper_id", "url_sha256"})
_NAMING_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "of",
        "on",
        "the",
        "through",
        "to",
        "using",
        "via",
        "with",
    }
)
_NAMING_REDUCER_VERSION = "citation-key-fragment-v1"


def _validate_durable_strings(value: object) -> None:
    if isinstance(value, str):
        ensure_safe_durable_text(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            ensure_safe_durable_key(str(key))
            _validate_durable_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _validate_durable_strings(item)


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _freeze_json(value: object) -> object:
    """Recursively detach and freeze strict JSON evidence."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("merge evidence requires string JSON keys")
        return MappingProxyType({key: _freeze_json(item) for key, item in sorted(value.items())})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError("merge evidence requires strict JSON")


@dataclass(frozen=True)
class MergeSourceEvidence:
    """One normalized, identity-scoped candidate admitted to the pure merge."""

    author_key: str
    publication_key: str
    provider: str
    schema_version: str
    request_key: str | None
    observation_digest: str
    record_ordinal: int
    identity_accepted: bool
    entry: Mapping[str, object]
    projection_status: str = "projected"
    projection_reason: str | None = None
    task_identity_digest: str | None = None
    applicability_reason: ApplicabilityReason | None = None
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            (self.request_key is not None and not _DIGEST_RE.fullmatch(self.request_key))
            or not _DIGEST_RE.fullmatch(self.observation_digest)
            or (self.task_identity_digest is not None and not _DIGEST_RE.fullmatch(self.task_identity_digest))
        ):
            raise ValueError("merge source digest changed")
        if isinstance(self.record_ordinal, bool) or self.record_ordinal < 0:
            raise ValueError("merge source ordinal changed")
        if not isinstance(self.identity_accepted, bool):
            raise TypeError("merge source identity verdict changed")
        if self.projection_status not in {"projected", "rejected"} or (self.projection_status == "projected") == (
            self.projection_reason is not None
        ):
            raise ValueError("merge source projection status changed")
        if (self.request_key is None) != (self.applicability_reason is not None):
            raise ValueError("merge source applicability changed")
        if self.applicability_reason is not None and not isinstance(self.applicability_reason, ApplicabilityReason):
            raise TypeError("merge source applicability reason changed")
        for value in (self.author_key, self.publication_key, self.provider, self.schema_version):
            ensure_safe_durable_text(value)
        _validate_durable_strings(self.entry)
        frozen = _freeze_json(self.entry)
        if not isinstance(frozen, Mapping) or not isinstance(frozen.get("fields"), Mapping):
            raise ValueError("merge source entry changed")
        entry = _freeze_json(
            {
                "type": str(frozen.get("type") or "misc"),
                "key": str(frozen.get("key") or "candidate"),
                "fields": frozen["fields"],
            }
        )
        if not isinstance(entry, Mapping):
            raise AssertionError("frozen merge source must be a mapping")
        object.__setattr__(self, "entry", entry)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "author_key": self.author_key,
                    "entry": entry,
                    "observation_digest": self.observation_digest,
                    "record_ordinal": self.record_ordinal,
                    "identity_accepted": self.identity_accepted,
                    "projection_status": self.projection_status,
                    "projection_reason": self.projection_reason,
                    "provider": self.provider,
                    "publication_key": self.publication_key,
                    "request_key": self.request_key,
                    "schema_version": self.schema_version,
                    "task_identity_digest": self.task_identity_digest,
                    "applicability_reason": (
                        self.applicability_reason.value if self.applicability_reason is not None else None
                    ),
                }
            ),
        )


def project_merge_sources(
    seeds: Sequence[PublicationSeedEvidence],
    source_waves: Sequence[DiscoveryWave],
    observations: Sequence[DiscoveryObservation],
) -> tuple[MergeSourceEvidence, ...]:
    """Project every normalized provider record into immutable merge evidence."""
    seed_map = {(seed.author_key, seed.publication_key): seed for seed in seeds}
    decisions = {decision.task.key: decision for wave in source_waves for decision in wave.decisions}
    tasks = {key: decision.task for key, decision in decisions.items() if decision.task.request is not None}
    observed = {item.task.key: item for item in observations}
    if (
        len(seed_map) != len(seeds)
        or len(decisions) != sum(len(wave.decisions) for wave in source_waves)
        or len(tasks) != sum(decision.task.request is not None for decision in decisions.values())
        or len(observed) != len(observations)
        or set(observed) != set(tasks)
    ):
        raise ValueError("merge projection source membership changed")
    projected: list[MergeSourceEvidence] = []
    for decision in sorted(decisions.values(), key=lambda item: item.task.key):
        task = decision.task
        if task.request is not None:
            continue
        if decision.reason is None or task.publication_key is None:
            raise ValueError("merge projection logical applicability changed")
        if (task.author_key, task.publication_key) not in seed_map:
            raise ValueError("merge projection publication membership changed")
        projected.append(
            MergeSourceEvidence(
                task.author_key,
                task.publication_key,
                task.provider,
                "logical-na-v1",
                None,
                task.identity_digest,
                0,
                False,
                {"type": "misc", "key": task.publication_key, "fields": {}},
                "rejected",
                "not_applicable",
                task.identity_digest,
                decision.reason,
            )
        )
    for task_key, task in sorted(tasks.items()):
        observation = observed[task_key]
        if observation.task != task or observation.disposition not in _SATISFIED or task.publication_key is None:
            raise ValueError("merge projection terminal evidence changed")
        seed = seed_map.get((task.author_key, task.publication_key))
        if seed is None or task.request is None:
            raise ValueError("merge projection publication membership changed")
        if observation.disposition is not TaskDisposition.SUCCEEDED:
            projected.append(
                MergeSourceEvidence(
                    task.author_key,
                    task.publication_key,
                    task.provider,
                    observation.schema_version,
                    task.request.key,
                    observation.response_digest,
                    0,
                    False,
                    {"type": "misc", "key": task.publication_key, "fields": {}},
                    "rejected",
                    observation.disposition.value,
                )
            )
            continue
        values = _normalized_response_records(task.provider, observation.response)
        baseline = {
            "type": seed.baseline_entry.get("type"),
            "key": seed.baseline_entry.get("key"),
            "fields": dict(_seed_fields(seed)),
        }
        for ordinal, record in enumerate(values):
            if not isinstance(record, Mapping):
                raise ValueError("merge projection record changed")
            entry = _project_provider_record(task.provider, record, task.publication_key)
            rejected = entry is None
            if entry is None:
                entry = {"type": "misc", "key": task.publication_key, "fields": {}}
            projected.append(
                MergeSourceEvidence(
                    task.author_key,
                    task.publication_key,
                    task.provider,
                    observation.schema_version,
                    task.request.key,
                    observation.response_digest,
                    ordinal,
                    False
                    if rejected
                    else evaluate_identity(baseline, entry, context=IdentityContext.ENRICHMENT).verdict,
                    entry,
                    "rejected" if rejected else "projected",
                    "unsupported_or_malformed_provider_record" if rejected else None,
                    task.identity_digest,
                )
            )
    return tuple(
        sorted(
            projected,
            key=lambda item: (
                item.author_key,
                item.publication_key,
                item.provider,
                item.request_key or "",
                item.record_ordinal,
                item.digest,
            ),
        )
    )


def _normalized_response_records(provider: str, response: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    if provider == "web" and set(response) == {"doi"}:
        return (response,)
    records = response.get("records")
    if records is not None:
        if provider != "pubmed" or not isinstance(records, Mapping):
            raise ValueError("normalized record map changed")
        values: list[Mapping[str, object]] = []
        for uid, record in sorted(records.items()):
            if not isinstance(uid, str) or not isinstance(record, Mapping) or record.get("uid") != uid:
                raise ValueError("PubMed record identity changed")
            values.append(record)
        return tuple(values)
    members = next(
        (
            value
            for key in ("metadata", "entry", "results", "entries", "articles", "notes")
            if isinstance((value := response.get(key)), (Mapping, tuple, list))
        ),
        (),
    )
    return (members,) if isinstance(members, Mapping) else tuple(members)


def _project_provider_record(provider: str, record: Mapping[str, object], keyhint: str) -> dict[str, object] | None:
    mapping = {
        "arxiv": ARXIV_FIELD_MAPPING,
        "crossref": CROSSREF_FIELD_MAPPING,
        "openalex": OPENALEX_FIELD_MAPPING,
        "openreview": OPENREVIEW_FIELD_MAPPING,
        "s2": S2_FIELD_MAPPING,
    }.get(provider)
    try:
        return (
            project_entry_from_response(dict(record), keyhint, mapping)
            if mapping is not None
            else _project_unmapped_record(provider, record, keyhint)
        )
    except (AttributeError, TypeError, ValueError):
        return None


def _pubmed_article_doi(record: Mapping[str, object]) -> str | None:
    article_ids = record.get("articleids")
    if not isinstance(article_ids, (list, tuple)):
        return None
    for item in article_ids:
        if not isinstance(item, Mapping) or str(item.get("idtype") or "").casefold() != "doi":
            continue
        value = normalize_doi(str(item.get("value") or ""))
        if value and find_doi_in_text(value) == value:
            return value
    return None


def _project_unmapped_record(provider: str, record: Mapping[str, object], keyhint: str) -> dict[str, object] | None:
    """Project retained normalized providers not covered by APIFieldMapping."""
    if provider == "doi_bibtex":
        entry = record.get("entry", record)
        return dict(entry) if isinstance(entry, Mapping) and isinstance(entry.get("fields"), Mapping) else None
    if provider == "doi_csl":
        title = record.get("title")
        if isinstance(title, (list, tuple)):
            title = title[0] if title else None
        if not isinstance(title, str) or not title.strip():
            return None
        plain = _thaw(record)
        if not isinstance(plain, Mapping):
            return None
        fields: dict[str, object] = {"title": title.strip()}
        author_members = plain.get("author")
        authors = []
        if isinstance(author_members, list):
            for author in author_members:
                if not isinstance(author, Mapping):
                    continue
                literal = str(author.get("literal") or "").strip()
                given = str(author.get("given") or "").strip()
                family = str(author.get("family") or "").strip()
                value = literal or " ".join(item for item in (given, family) if item)
                if value:
                    authors.append(value)
        if authors:
            fields["author"] = format_author_field(authors) or ""
        year = extract_year_from_any(
            plain,
            field_names=["issued", "published-print", "published-online"],
            fallback=None,
        )
        if year is not None:
            fields["year"] = str(year)
        doi = normalize_doi(str(record.get("DOI") or ""))
        if doi is not None:
            fields["doi"] = doi
        for source, target in (("publisher", "publisher"), ("container-title", "journal")):
            field_value = record.get(source)
            if isinstance(field_value, (list, tuple)):
                field_value = field_value[0] if field_value else None
            if isinstance(field_value, str) and field_value.strip():
                fields[target] = field_value.strip()
        return {"type": "misc", "key": keyhint, "fields": fields}
    if provider == "pubmed":
        pubmed_authors = record.get("authors")
        normalized = dict(record)
        normalized["authors"] = (
            [
                item["name"]
                for item in pubmed_authors
                if isinstance(item, Mapping) and isinstance(item.get("name"), str) and item["name"].strip()
            ]
            if isinstance(pubmed_authors, (list, tuple))
            else []
        )
        normalized["year"] = record.get("pubdate")
        normalized["doi"] = _pubmed_article_doi(record)
        normalized["pmid"] = record.get("uid")
        entry = _candidate_entry(normalized)
        fields = cast(dict[str, object], entry["fields"])
        if isinstance(fields.get("author"), list):
            fields["author"] = format_author_field(cast(list[str], fields["author"])) or ""
        for source, target in (
            ("uid", "pmid"),
            ("fulljournalname", "journal"),
            ("source", "journal"),
            ("volume", "volume"),
            ("issue", "number"),
            ("pages", "pages"),
        ):
            field_value = record.get(source)
            if isinstance(field_value, str) and field_value.strip():
                fields.setdefault(target, field_value.strip())
        doi = _pubmed_article_doi(record)
        if doi is not None:
            fields["doi"] = doi
        return entry
    if provider == "europepmc":
        normalized = dict(record)
        normalized["authors"] = record.get("authorString")
        normalized["year"] = record.get("pubYear")
        entry = _candidate_entry(normalized)
        fields = cast(dict[str, object], entry["fields"])
        if isinstance(fields.get("author"), list):
            fields["author"] = format_author_field(cast(list[str], fields["author"])) or ""
        for source, target in (("pmid", "pmid"), ("journalTitle", "journal"), ("doi", "doi")):
            field_value = record.get(source)
            if isinstance(field_value, str) and field_value.strip():
                fields[target] = field_value.strip()
        return entry
    if provider == "serply":
        entry = _candidate_entry(record)
        fields = cast(dict[str, object], entry["fields"])
        if isinstance(fields.get("author"), list):
            fields["author"] = format_author_field(cast(list[str], fields["author"])) or ""
        for source, target in (("publication", "journal"), ("link", "url")):
            field_value = record.get(source)
            if isinstance(field_value, str) and field_value.strip():
                fields[target] = field_value.strip()
        return entry
    return None


@dataclass(frozen=True)
class MergedPublicationEvidence:
    """Canonical final entry and deterministic field-source selections."""

    author_key: str
    publication_key: str
    final_entry: Mapping[str, object]
    selected_source_digests: Mapping[str, str]
    source_digests: tuple[str, ...]
    reducer_version: str = _MERGE_REDUCER_VERSION
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        _validate_durable_strings(self.final_entry)
        frozen = _freeze_json(self.final_entry)
        if not isinstance(frozen, Mapping) or not isinstance(frozen.get("fields"), Mapping):
            raise ValueError("merged publication entry changed")
        final_entry = _freeze_json(
            {
                "type": str(frozen.get("type") or "misc"),
                "key": self.publication_key,
                "fields": frozen["fields"],
            }
        )
        if not isinstance(final_entry, Mapping):
            raise AssertionError("frozen merged publication must be a mapping")
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in self.selected_source_digests.items()
        ):
            raise TypeError("selected source digests must be strings")
        selected = MappingProxyType(dict(sorted(self.selected_source_digests.items())))
        sources = tuple(sorted(self.source_digests))
        object.__setattr__(self, "final_entry", final_entry)
        object.__setattr__(self, "selected_source_digests", selected)
        object.__setattr__(self, "source_digests", sources)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "author_key": self.author_key,
                    "final_entry": final_entry,
                    "publication_key": self.publication_key,
                    "reducer_version": self.reducer_version,
                    "selected_source_digests": selected,
                    "source_digests": sources,
                }
            ),
        )


@dataclass(frozen=True)
class CorpusOutputEvidence:
    """Exact existing output path and byte digest for one publication member."""

    author_key: str
    publication_key: str
    source_path: str
    before_digest: str


@dataclass(frozen=True)
class NamingEvidence:
    """Terminal C6 naming and serialization evidence for one emitted survivor."""

    author_key: str
    publication_key: str
    bibtex_key: str
    target_path: str
    content_digest: str
    final_fields: tuple[str, ...]
    source: str
    source_receipt_digest: str

    def __post_init__(self) -> None:
        if not _DIGEST_RE.fullmatch(self.content_digest) or not _DIGEST_RE.fullmatch(self.source_receipt_digest):
            raise ValueError("naming evidence digest changed")
        for value in (self.author_key, self.publication_key, self.bibtex_key, self.target_path, self.source):
            ensure_safe_durable_text(value)
        if self.source not in {"deterministic", "gemini", "gemini-fallback"}:
            raise ValueError("naming evidence source changed")
        fields = tuple(sorted(self.final_fields))
        if not fields or len(fields) != len(set(fields)):
            raise ValueError("naming evidence fields changed")
        object.__setattr__(self, "final_fields", fields)


@dataclass(frozen=True)
class CitationKeyFragmentEvidence:
    """Terminal C6 citation-key fragment authority, deliberately excluding filenames."""

    author_key: str
    publication_key: str
    fragment: str
    source: str
    source_task_key: str
    source_receipt_digest: str
    reducer_version: str = _NAMING_REDUCER_VERSION
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.source not in {"deterministic", "gemini", "gemini-fallback"}:
            raise ValueError("citation fragment source changed")
        if (
            not _DIGEST_RE.fullmatch(self.source_task_key)
            or not _DIGEST_RE.fullmatch(self.source_receipt_digest)
            or self.reducer_version != _NAMING_REDUCER_VERSION
        ):
            raise ValueError("citation fragment authority changed")
        _validate_citation_fragment(self.fragment, enforce_word_bound=self.source == "gemini")
        for value in (self.author_key, self.publication_key, self.fragment, self.source):
            ensure_safe_durable_text(value)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "author_key": self.author_key,
                    "fragment": self.fragment,
                    "publication_key": self.publication_key,
                    "reducer_version": self.reducer_version,
                    "source": self.source,
                    "source_receipt_digest": self.source_receipt_digest,
                    "source_task_key": self.source_task_key,
                }
            ),
        )


def _validate_citation_fragment(fragment: str, *, enforce_word_bound: bool = True) -> None:
    if (
        not isinstance(fragment, str)
        or not fragment
        or len(fragment) > 100
        or not re.fullmatch(r"[A-Z][A-Za-z0-9]*", fragment)
        or (enforce_word_bound and sum(character.isupper() for character in fragment) > BIBTEX_KEY_MAX_WORDS)
    ):
        raise ValueError("Gemini citation fragment is not canonical")


def _deterministic_citation_fragment(title: str) -> str:
    words = [word for word in re.split(r"[^A-Za-z0-9]+", title) if word]
    selected = [word for word in words if word.casefold() not in _NAMING_STOP_WORDS][:BIBTEX_KEY_MAX_WORDS]
    if not selected:
        selected = words[:BIBTEX_KEY_MAX_WORDS]
    fragment = "".join(word[:1].upper() + word[1:] for word in selected) or "Title"
    _validate_citation_fragment(fragment, enforce_word_bound=False)
    return fragment


def _naming_members(
    merged: Sequence[MergedPublicationEvidence], survivor_reduction: SurvivorReduction
) -> tuple[tuple[MergedPublicationEvidence, ...], dict[tuple[str, str], SurvivorDecision]]:
    ordered = tuple(sorted(merged, key=lambda item: (item.author_key, item.publication_key)))
    merged_map = {(item.author_key, item.publication_key): item for item in ordered}
    decisions = {(item.author_key, item.publication_key): item for item in survivor_reduction.decisions}
    if (
        len(merged_map) != len(ordered)
        or len(decisions) != len(survivor_reduction.decisions)
        or set(decisions) != set(merged_map)
        or any(decisions[key].merged_digest != item.digest for key, item in merged_map.items())
    ):
        raise ValueError("Gemini naming survivor membership changed")
    return ordered, decisions


def _merged_title(item: MergedPublicationEvidence) -> str:
    fields = item.final_entry.get("fields")
    title = fields.get("title") if isinstance(fields, Mapping) else None
    if not isinstance(title, str) or not title.strip():
        raise ValueError("Gemini naming title authority changed")
    return title.strip()


def plan_gemini_naming(
    merged: Sequence[MergedPublicationEvidence],
    survivor_reduction: SurvivorReduction,
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Plan one exact Gemini task or typed NA for every emitted survivor."""
    ordered, survivor_map = _naming_members(merged, survivor_reduction)
    capability = capability_for("gemini", "short_title", authority.policy.adapter_versions["gemini"])
    mode = authority.resolved_provider_modes["gemini"]
    decisions: list[DiscoveryDecision] = []
    emitted: list[MergedPublicationEvidence] = []
    for item in ordered:
        if survivor_map[(item.author_key, item.publication_key)].disposition is not SurvivorDisposition.EMITTED:
            continue
        emitted.append(item)
        if mode in {"disabled", "if_configured"}:
            reason = (
                ApplicabilityReason.PROVIDER_DISABLED
                if mode == "disabled"
                else ApplicabilityReason.PROVIDER_NOT_CONFIGURED
            )
            decisions.append(
                DiscoveryDecision(
                    TaskSpec(
                        item.author_key,
                        item.publication_key,
                        capability.logical_source,
                        capability.operation,
                        None,
                        applicability="not_applicable",
                    ),
                    reason,
                )
            )
            continue
        request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {
                "generation_config": GEMINI_GENERATION_CONFIG,
                "max_words": BIBTEX_KEY_MAX_WORDS,
                "model_id": GEMINI_MODEL_ID,
                "prompt_version": GEMINI_PROMPT_VERSION,
                "title": _merged_title(item),
            },
            capability.requested_fields,
            capability.adapter_version,
            authority.policy.freshness_epoch,
            capability.quota_scope,
        )
        decisions.append(
            DiscoveryDecision(
                TaskSpec(
                    item.author_key,
                    item.publication_key,
                    capability.logical_source,
                    capability.operation,
                    request,
                )
            )
        )
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "emitted": [item.digest for item in emitted],
                "naming_policy": {
                    "max_words": BIBTEX_KEY_MAX_WORDS,
                    "reducer_version": _NAMING_REDUCER_VERSION,
                },
                "survivors": survivor_reduction.digest,
            }
        ),
        authority.digest,
    )


def _gemini_fragment(response: Mapping[str, object]) -> str:
    candidates = response.get("candidates")
    if not isinstance(candidates, tuple) or len(candidates) != 1 or not isinstance(candidates[0], Mapping):
        raise ValueError("Gemini citation fragment candidate membership changed")
    content = candidates[0].get("content")
    parts = content.get("parts") if isinstance(content, Mapping) else None
    if not isinstance(parts, tuple) or len(parts) != 1 or not isinstance(parts[0], Mapping):
        raise ValueError("Gemini citation fragment part membership changed")
    fragment = parts[0].get("text")
    if not isinstance(fragment, str):
        raise ValueError("Gemini citation fragment is not canonical")
    _validate_citation_fragment(fragment)
    return fragment


def reduce_gemini_naming(
    merged: Sequence[MergedPublicationEvidence],
    survivor_reduction: SurvivorReduction,
    wave: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> tuple[CitationKeyFragmentEvidence, ...]:
    """Reduce exact Gemini output or a policy-bound deterministic fragment fallback."""
    ordered, survivor_map = _naming_members(merged, survivor_reduction)
    canonical = plan_gemini_naming(ordered, survivor_reduction, authority)
    if canonical != wave:
        raise ValueError("Gemini naming wave authority changed")
    applicable = {item.task.key: item.task for item in canonical.decisions if item.task.request is not None}
    observed = {item.task.key: item for item in observations}
    if len(observed) != len(observations) or set(observed) != set(applicable):
        raise ValueError("Gemini naming observation membership changed")
    decisions = {(item.task.author_key, item.task.publication_key): item for item in canonical.decisions}
    schema = capability_for("gemini", "short_title", authority.policy.adapter_versions["gemini"]).decoder_schema
    policy_mode = authority.policy.provider_modes["gemini"]
    evidence: list[CitationKeyFragmentEvidence] = []
    for item in ordered:
        member = (item.author_key, item.publication_key)
        if survivor_map[member].disposition is not SurvivorDisposition.EMITTED:
            continue
        decision = decisions[member]
        task = decision.task
        if task.request is None:
            fragment = _deterministic_citation_fragment(_merged_title(item))
            source = "deterministic"
            receipt = evidence_digest(
                {
                    "decision_reason": decision.reason.value if decision.reason is not None else None,
                    "fragment": fragment,
                    "task_identity": task.identity_digest,
                }
            )
        else:
            observation = observed[task.key]
            if observation.task != task or observation.schema_version != schema:
                raise ValueError("Gemini naming observation authority changed")
            if observation.disposition is TaskDisposition.SUCCEEDED:
                fragment = _gemini_fragment(observation.response)
                source = "gemini"
            else:
                if observation.disposition in {
                    TaskDisposition.PENDING,
                    TaskDisposition.LEASED,
                    TaskDisposition.RETRY_WAIT,
                }:
                    raise ValueError("Gemini naming evidence is not terminal")
                if policy_mode == "required":
                    raise ValueError("required Gemini naming evidence is blocking")
                fragment = _deterministic_citation_fragment(_merged_title(item))
                source = "gemini-fallback"
            receipt = evidence_digest(
                {
                    "disposition": observation.disposition.value,
                    "fragment": fragment,
                    "response_digest": observation.response_digest,
                    "schema_version": observation.schema_version,
                    "source": source,
                    "task_identity": task.identity_digest,
                }
            )
        evidence.append(
            CitationKeyFragmentEvidence(
                item.author_key,
                item.publication_key,
                fragment,
                source,
                task.key,
                receipt,
            )
        )
    return tuple(sorted(evidence, key=lambda item: (item.author_key, item.publication_key)))


class SurvivorDisposition(str, Enum):
    """Exact materialization partition for one merged publication."""

    EMITTED = "emitted"
    EXISTING_REMOVE = "existing_remove"
    ABSENT_DUPLICATE_SUPPRESSED = "absent_duplicate_suppressed"


@dataclass(frozen=True)
class SurvivorDecision:
    author_key: str
    publication_key: str
    disposition: SurvivorDisposition
    merged_digest: str
    corpus_digest: str | None
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.disposition, SurvivorDisposition):
            raise TypeError("survivor disposition changed")
        if not _DIGEST_RE.fullmatch(self.merged_digest) or (
            self.corpus_digest is not None and not _DIGEST_RE.fullmatch(self.corpus_digest)
        ):
            raise ValueError("survivor authority digest changed")
        ensure_safe_durable_text(self.author_key)
        ensure_safe_durable_text(self.publication_key)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "author_key": self.author_key,
                    "corpus_digest": self.corpus_digest,
                    "disposition": self.disposition.value,
                    "merged_digest": self.merged_digest,
                    "publication_key": self.publication_key,
                }
            ),
        )


@dataclass(frozen=True)
class SurvivorReduction:
    decisions: tuple[SurvivorDecision, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        decisions = tuple(sorted(self.decisions, key=lambda item: (item.author_key, item.publication_key)))
        if len({(item.author_key, item.publication_key) for item in decisions}) != len(decisions):
            raise ValueError("duplicate survivor decision")
        object.__setattr__(self, "decisions", decisions)
        object.__setattr__(self, "digest", evidence_digest([item.digest for item in decisions]))


@dataclass(frozen=True)
class ProvenanceEvidence:
    decisions: tuple[ProvenanceDecision, ...]
    contributions: tuple[ProvenanceContribution, ...]


def derive_provenance_evidence(
    generation_id: str,
    pass_key: str,
    seeds: Sequence[PublicationSeedEvidence],
    sources: Sequence[MergeSourceEvidence],
    merged: Sequence[MergedPublicationEvidence],
) -> ProvenanceEvidence:
    """Derive complete field decisions and every baseline/provider alternative."""
    seed_map = {(item.author_key, item.publication_key): item for item in seeds}
    merged_map = {(item.author_key, item.publication_key): item for item in merged}
    if len(seed_map) != len(seeds) or len(merged_map) != len(merged) or set(seed_map) != set(merged_map):
        raise ValueError("provenance publication membership changed")
    source_by_member: dict[tuple[str, str], list[MergeSourceEvidence]] = {}
    for source in sources:
        key = (source.author_key, source.publication_key)
        if key not in seed_map:
            raise ValueError("provenance source membership changed")
        source_by_member.setdefault(key, []).append(source)
    decisions: list[ProvenanceDecision] = []
    contributions: list[ProvenanceContribution] = []
    for key, value in sorted(merged_map.items()):
        seed = seed_map[key]
        fields = value.final_entry.get("fields")
        baseline_fields = _seed_fields(seed)
        if not isinstance(fields, Mapping) or set(value.selected_source_digests) != set(fields):
            raise ValueError("provenance selected-field membership changed")
        for field_name, final_value in sorted(fields.items()):
            selected_digest = value.selected_source_digests[field_name]
            template = ProvenanceDecision(
                generation_id,
                pass_key,
                value.author_key,
                value.publication_key,
                field_name,
                evidence_digest(final_value),
                "trust_policy",
                "0" * 64,
                "publication_merge",
                _MERGE_REDUCER_VERSION,
            )
            members: list[ProvenanceContribution] = []
            if field_name in baseline_fields:
                selected = selected_digest == seed.seed_digest
                members.append(
                    ProvenanceContribution(
                        generation_id,
                        template.key,
                        EvidenceKind.SEED.value,
                        None,
                        None,
                        None,
                        seed.seed_digest,
                        evidence_digest(baseline_fields[field_name]),
                        selected,
                        "selected" if selected else "trust_policy_rejected",
                    )
                )
            for source in sorted(source_by_member.get(key, ()), key=lambda item: item.digest):
                source_fields = source.entry.get("fields")
                if source.projection_status == "rejected" or not source.identity_accepted:
                    members.append(
                        ProvenanceContribution(
                            generation_id,
                            template.key,
                            EvidenceKind.OBSERVATION.value,
                            source.provider,
                            source.schema_version,
                            source.request_key,
                            source.observation_digest,
                            None,
                            False,
                            source.projection_reason or "identity_rejected",
                        )
                    )
                    continue
                if not isinstance(source_fields, Mapping) or field_name not in source_fields:
                    members.append(
                        ProvenanceContribution(
                            generation_id,
                            template.key,
                            EvidenceKind.OBSERVATION.value,
                            source.provider,
                            source.schema_version,
                            source.request_key,
                            source.observation_digest,
                            None,
                            False,
                            "field_absent",
                        )
                    )
                    continue
                selected = selected_digest == source.digest
                members.append(
                    ProvenanceContribution(
                        generation_id,
                        template.key,
                        EvidenceKind.OBSERVATION.value,
                        source.provider,
                        source.schema_version,
                        source.request_key,
                        source.observation_digest,
                        evidence_digest(source_fields[field_name]),
                        selected,
                        "selected" if selected else "trust_policy_rejected",
                    )
                )
            if not any(item.selected for item in members):
                derived_digest = evidence_digest(
                    {
                        "field": field_name,
                        "reducer": _MERGE_REDUCER_VERSION,
                        "sources": value.source_digests,
                        "value": final_value,
                    }
                )
                if derived_digest != selected_digest:
                    raise ValueError("provenance derived source changed")
                members.append(
                    ProvenanceContribution(
                        generation_id,
                        template.key,
                        EvidenceKind.PROVENANCE.value,
                        None,
                        _MERGE_REDUCER_VERSION,
                        None,
                        derived_digest,
                        evidence_digest(final_value),
                        True,
                        "selected",
                    )
                )
            if len([item for item in members if item.selected]) != 1:
                raise ValueError("provenance selected source changed")
            decisions.append(
                replace(template, contribution_set_digest=evidence_digest(sorted(item.key for item in members)))
            )
            contributions.extend(members)
    return ProvenanceEvidence(
        tuple(sorted(decisions, key=lambda item: item.key)),
        tuple(sorted(contributions, key=lambda item: item.key)),
    )


def _seed_fields(seed: PublicationSeedEvidence) -> Mapping[str, object]:
    if seed.seed_digest != seed.derived_seed_digest:
        raise ValueError("publication seed digest changed")
    fields = seed.baseline_entry.get("fields")
    if not isinstance(fields, Mapping):
        raise ValueError("publication seed baseline fields changed")
    return fields


def _terminal_observations(
    wave: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    *,
    label: str,
) -> dict[str, DiscoveryObservation]:
    applicable = {decision.task.key: decision.task for decision in wave.decisions if decision.task.request is not None}
    observed = {item.task.key: item for item in observations}
    if len(observed) != len(observations) or set(observed) != set(applicable):
        raise ValueError(f"terminal {label} evidence membership changed")
    for task_key, task in applicable.items():
        item = observed[task_key]
        if item.task != task or item.disposition not in _SATISFIED:
            raise ValueError(f"terminal {label} evidence is incomplete or blocking")
    return observed


def _validate_broad_membership(seeds: Sequence[PublicationSeedEvidence], broad: DiscoveryWave) -> None:
    expected = {
        (seed.author_key, seed.publication_key, provider, operation)
        for seed in seeds
        for provider, operation in _BROAD_OPERATIONS
    }
    actual = {
        (decision.task.author_key, decision.task.publication_key, decision.task.provider, decision.task.operation)
        for decision in broad.decisions
    }
    if len(actual) != len(broad.decisions) or actual != expected:
        raise ValueError("broad discovery decision membership changed")


def _candidate_entry(candidate: Mapping[str, object]) -> dict[str, object]:
    title = candidate.get("title")
    if isinstance(title, (tuple, list)):
        title = title[0] if title else ""
    fields: dict[str, object] = {"title": title if isinstance(title, str) else ""}
    authors_raw = candidate.get("authors")
    if isinstance(authors_raw, tuple) and all(isinstance(item, Mapping) for item in authors_raw):
        authors = [str(item.get("name") or "").strip() for item in authors_raw if str(item.get("name") or "").strip()]
    else:
        authors = extract_authors_from_any(dict(candidate), field_names=["author", "authors", "authorships"])
    if authors:
        fields["author"] = authors
    year = extract_year_from_any(
        candidate,
        field_names=["year", "publication_year", "issued", "published-print", "published-online"],
        fallback=None,
    )
    if year is not None:
        fields["year"] = str(year)
    doi = normalize_doi(str(candidate.get("DOI") or candidate.get("doi") or ""))
    if doi:
        fields["doi"] = doi
    return {"type": "misc", "key": "candidate", "fields": fields}


def _accepted_candidates(
    seed: PublicationSeedEvidence, observation: DiscoveryObservation
) -> tuple[Mapping[str, object], ...]:
    response = observation.response
    members = next(
        (
            value
            for key in ("results", "entries", "articles", "notes")
            if isinstance((value := response.get(key)), tuple)
        ),
        (),
    )
    baseline = {
        "type": seed.baseline_entry.get("type"),
        "key": seed.baseline_entry.get("key"),
        "fields": dict(_seed_fields(seed)),
    }
    return tuple(
        candidate
        for candidate in members
        if isinstance(candidate, Mapping)
        and evaluate_identity(baseline, _candidate_entry(candidate), context=IdentityContext.ENRICHMENT).verdict
    )


def _accepted_venue_candidates(
    seed: PublicationSeedEvidence, observation: DiscoveryObservation, author_name: str
) -> tuple[Mapping[str, object], ...]:
    response = observation.response
    members = next(
        (value for key in ("results", "entries") if isinstance((value := response.get(key)), tuple)),
        (),
    )
    fields = _seed_fields(seed)
    title = fields.get("title")
    if not isinstance(title, str) or not title.strip():
        return ()
    year = extract_year_from_any(fields.get("year"))
    config = CROSSREF_VENUE_SEARCH_CONFIG if observation.task.provider == "crossref" else OPENALEX_VENUE_SEARCH_CONFIG
    if observation.task.provider == "crossref":

        def title_getter(candidate: Mapping[str, object]) -> str:
            value = candidate.get("title")
            if isinstance(value, (list, tuple)) and value and isinstance(value[0], str):
                return value[0]
            return value if isinstance(value, str) else ""

        def authors_getter(candidate: Mapping[str, object]) -> object:
            value = candidate.get("author")
            if isinstance(value, (list, tuple)):
                return [
                    " ".join(
                        part.strip()
                        for part in (str(item.get("given") or ""), str(item.get("family") or ""))
                        if part.strip()
                    )
                    for item in value
                    if isinstance(item, Mapping)
                ]
            return value or ()

        def year_getter(candidate: Mapping[str, object]) -> int | None:
            issued = candidate.get("issued")
            if isinstance(issued, Mapping):
                parts = issued.get("date-parts")
                if isinstance(parts, (list, tuple)) and parts and isinstance(parts[0], (list, tuple)) and parts[0]:
                    return extract_year_from_any(parts[0][0])
            return None
    else:

        def title_getter(candidate: Mapping[str, object]) -> str:
            return safe_get_field(dict(candidate), config.title_field) or ""

        def authors_getter(candidate: Mapping[str, object]) -> object:
            if config.authors_getter is not None:
                return config.authors_getter(dict(candidate))
            return candidate.get(config.author_field) or []

        def year_getter(candidate: Mapping[str, object]) -> int | None:
            if config.year_getter is not None:
                return config.year_getter(dict(candidate))
            return extract_year_from_any(candidate.get("year"))

    score = create_scoring_function(
        title,
        author_name,
        year,
        title_getter,
        authors_getter,
        year_getter,
        emit_logs=False,
    )
    ranked = sorted(
        (
            (score(candidate), index, candidate)
            for index, candidate in enumerate(members)
            if isinstance(candidate, Mapping)
        ),
        key=lambda item: (-item[0], item[1]),
    )
    return tuple(candidate for value, _index, candidate in ranked if value >= _VENUE_SCORE_THRESHOLD)[
        :_VENUE_ADMISSION_LIMIT
    ]


def _venue(seed: PublicationSeedEvidence) -> str | None:
    fields = _seed_fields(seed)
    for key in ("publication", "howpublished", "journal", "booktitle"):
        raw = fields.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        parsed = parse_publication_string(raw)
        if (
            parsed is not None
            and parsed.confidence >= PUB_PARSE_TIER1_MIN_CONFIDENCE
            and parsed.venue_type in {"journal", "conference"}
            and parsed.venue_name.strip()
        ):
            return parsed.venue_name.strip()
    return None


def _doi_reductions(
    seeds: Sequence[PublicationSeedEvidence], reductions: Sequence[DoiReduction]
) -> dict[tuple[str, str], DoiReduction]:
    result = {(item.author_key, item.publication_key): item for item in reductions}
    expected = {(item.author_key, item.publication_key) for item in seeds}
    if len(result) != len(reductions) or set(result) != expected:
        raise ValueError("DOI reduction membership changed")
    return result


def _na(seed: PublicationSeedEvidence, provider: str, operation: str, reason: ApplicabilityReason) -> DiscoveryDecision:
    return DiscoveryDecision(
        TaskSpec(seed.author_key, seed.publication_key, provider, operation, None, applicability="not_applicable"),
        reason,
    )


def plan_crossref_venue_fallback(
    seeds: Sequence[PublicationSeedEvidence],
    author_names: Mapping[str, str],
    broad: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    doi_reductions: Sequence[DoiReduction],
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Plan one exact Crossref venue decision for every immutable seed."""
    ordered = tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key)))
    if len({(item.author_key, item.publication_key) for item in ordered}) != len(ordered):
        raise ValueError("duplicate publication seed")
    if broad.policy_digest != authority.digest:
        raise ValueError("broad discovery policy authority changed")
    _validate_broad_membership(ordered, broad)
    reductions = _doi_reductions(ordered, doi_reductions)
    observed = _terminal_observations(broad, observations, label="broad")
    capability = capability_for("crossref", "venue_search", authority.policy.adapter_versions["crossref"])
    decisions: list[DiscoveryDecision] = []
    for seed in ordered:
        reduction = reductions[(seed.author_key, seed.publication_key)]
        if doi_reduction_is_authoritatively_complete(reduction, _seed_fields(seed)):
            decisions.append(
                _na(
                    seed, capability.logical_source, capability.operation, ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED
                )
            )
            continue
        member_decisions = [
            item
            for item in broad.decisions
            if (item.task.author_key, item.task.publication_key) == (seed.author_key, seed.publication_key)
        ]
        if any(
            item.task.request is not None
            and observed[item.task.key].disposition is TaskDisposition.SUCCEEDED
            and _accepted_candidates(seed, observed[item.task.key])
            for item in member_decisions
        ):
            decisions.append(
                _na(
                    seed, capability.logical_source, capability.operation, ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED
                )
            )
            continue
        venue = _venue(seed)
        fields = _seed_fields(seed)
        title = fields.get("title")
        author = author_names.get(seed.author_key)
        if (
            venue is None
            or not isinstance(title, str)
            or not title.strip()
            or not isinstance(author, str)
            or not author.strip()
        ):
            decisions.append(
                _na(
                    seed, capability.logical_source, capability.operation, ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED
                )
            )
            continue
        request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {
                "author": author,
                "author_key": seed.author_key,
                "query": title,
                "rows": _VENUE_FETCH_LIMIT,
                "venue": venue,
            },
            capability.requested_fields,
            capability.adapter_version,
            authority.policy.freshness_epoch,
            capability.quota_scope,
        )
        decisions.append(
            DiscoveryDecision(
                TaskSpec(
                    seed.author_key,
                    seed.publication_key,
                    capability.logical_source,
                    capability.operation,
                    request,
                )
            )
        )
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "authors": dict(sorted(author_names.items())),
                "broad": broad.input_digest,
                "doi_reductions": [item.digest for item in sorted(reductions.values(), key=lambda item: item.digest)],
                "venue_policy": {
                    "admission_limit": _VENUE_ADMISSION_LIMIT,
                    "fetch_limit": _VENUE_FETCH_LIMIT,
                    "identity_policy_version": _VENUE_IDENTITY_POLICY_VERSION,
                    "score_threshold": _VENUE_SCORE_THRESHOLD,
                },
                "observations": [item.response_digest for item in sorted(observations, key=lambda item: item.task.key)],
                "seeds": [item.canonical_content() for item in ordered],
            }
        ),
        authority.digest,
    )


def _canonical_entry(value: Mapping[str, object], stage: CanonicalStage) -> dict[str, object]:
    entry = _thaw(value)
    if not isinstance(entry, dict) or not isinstance(entry.get("fields"), dict):
        raise ValueError("publication entry changed")
    canonicalize(entry, stage=stage)
    return entry


def merge_publication_evidence(
    seeds: Sequence[PublicationSeedEvidence], sources: Sequence[MergeSourceEvidence]
) -> tuple[MergedPublicationEvidence, ...]:
    """Apply LOAD_REPAIR, trust merge, then POST_MERGE without mutating evidence."""
    ordered_seeds = tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key)))
    members = {(item.author_key, item.publication_key) for item in ordered_seeds}
    if len(members) != len(ordered_seeds):
        raise ValueError("duplicate publication seed")
    if any((item.author_key, item.publication_key) not in members for item in sources):
        raise ValueError("merge source membership changed")
    if len({item.digest for item in sources}) != len(sources):
        raise ValueError("duplicate merge source")
    trust_rank = {provider: index for index, provider in enumerate(TRUST_ORDER)}
    by_member: dict[tuple[str, str], list[MergeSourceEvidence]] = {}
    for source in sources:
        by_member.setdefault((source.author_key, source.publication_key), []).append(source)
    merged_publications: list[MergedPublicationEvidence] = []
    for seed in ordered_seeds:
        baseline = _canonical_entry(seed.baseline_entry, CanonicalStage.LOAD_REPAIR)
        accepted = []
        for source in sorted(
            by_member.get((seed.author_key, seed.publication_key), ()),
            key=lambda item: (trust_rank.get(item.provider, len(trust_rank)), item.digest),
        ):
            if source.projection_status == "rejected" or not source.identity_accepted:
                continue
            candidate = _thaw(source.entry)
            if not isinstance(candidate, dict):
                raise ValueError("merge source entry changed")
            if not evaluate_identity(baseline, candidate, context=IdentityContext.ENRICHMENT).verdict:
                raise ValueError("merge source identity changed")
            accepted.append((source, candidate))
        merged = merge_with_policy(
            baseline,
            [(source.provider, candidate) for source, candidate in accepted],
            emit_logs=False,
        )
        canonicalize(merged, stage=CanonicalStage.POST_MERGE)
        merged["key"] = seed.publication_key
        final_fields = merged.get("fields")
        if not isinstance(final_fields, dict):
            raise ValueError("merged publication fields changed")
        baseline_only = merge_with_policy(baseline, [], emit_logs=False)
        baseline_fields = baseline_only.get("fields")
        if not isinstance(baseline_fields, dict):
            raise ValueError("publication baseline fields changed")
        selected: dict[str, str] = {}
        for field_name, value in final_fields.items():
            candidate_sources: list[MergeSourceEvidence] = []
            for source, candidate in accepted:
                isolated = merge_with_policy(
                    {"type": "misc", "fields": {}},
                    [(source.provider, candidate)],
                    emit_logs=False,
                )
                isolated_fields = isolated.get("fields")
                if isinstance(isolated_fields, dict) and isolated_fields.get(field_name) == value:
                    candidate_sources.append(source)
            if candidate_sources:
                selected[field_name] = candidate_sources[0].digest
            elif baseline_fields.get(field_name) == value:
                selected[field_name] = seed.seed_digest
            else:
                selected[field_name] = evidence_digest(
                    {
                        "field": field_name,
                        "reducer": _MERGE_REDUCER_VERSION,
                        "sources": (seed.seed_digest, *(source.digest for source, _candidate in accepted)),
                        "value": value,
                    }
                )
        merged_publications.append(
            MergedPublicationEvidence(
                seed.author_key,
                seed.publication_key,
                merged,
                selected,
                (seed.seed_digest, *(source.digest for source, _candidate in accepted)),
            )
        )
    return tuple(merged_publications)


def derive_survivor_reduction(
    merged: Sequence[MergedPublicationEvidence],
    corpus: Sequence[CorpusOutputEvidence],
) -> SurvivorReduction:
    """Partition every merged member into emitted, removal, or absent suppression."""
    ordered = tuple(sorted(merged, key=lambda item: (item.author_key, item.publication_key)))
    members = {(item.author_key, item.publication_key) for item in ordered}
    if len(members) != len(ordered):
        raise ValueError("survivor publication membership changed")
    existing = {(item.author_key, item.publication_key): item for item in corpus}
    if len(existing) != len(corpus) or not set(existing) <= members:
        raise ValueError("survivor corpus membership changed")
    doi_groups: dict[tuple[str, str], list[MergedPublicationEvidence]] = {}
    for item in ordered:
        fields = item.final_entry.get("fields")
        if not isinstance(fields, Mapping):
            raise ValueError("merged publication fields changed")
        doi = normalize_doi(str(fields.get("doi") or ""))
        if doi:
            doi_groups.setdefault((item.author_key, doi), []).append(item)
    survivors: dict[tuple[str, str], str] = {}
    for group, values in doi_groups.items():
        survivor = min(
            values,
            key=lambda item: (
                0 if (item.author_key, item.publication_key) in existing else 1,
                existing[(item.author_key, item.publication_key)].source_path.casefold()
                if (item.author_key, item.publication_key) in existing
                else "",
                item.publication_key,
            ),
        )
        survivors[group] = survivor.publication_key
    decisions: list[SurvivorDecision] = []
    for item in ordered:
        key = (item.author_key, item.publication_key)
        fields = item.final_entry.get("fields")
        if not isinstance(fields, Mapping):
            raise ValueError("merged publication fields changed")
        doi = normalize_doi(str(fields.get("doi") or ""))
        loser = bool(doi and survivors[(item.author_key, doi)] != item.publication_key)
        prior = existing.get(key)
        disposition = (
            SurvivorDisposition.EXISTING_REMOVE
            if loser and prior is not None
            else SurvivorDisposition.ABSENT_DUPLICATE_SUPPRESSED
            if loser
            else SurvivorDisposition.EMITTED
        )
        decisions.append(
            SurvivorDecision(
                item.author_key,
                item.publication_key,
                disposition,
                item.digest,
                evidence_digest(
                    {
                        "author_key": prior.author_key,
                        "before_digest": prior.before_digest,
                        "publication_key": prior.publication_key,
                        "source_path": prior.source_path,
                    }
                )
                if prior is not None
                else None,
            )
        )
    return SurvivorReduction(tuple(decisions))


def derive_materialization_intents(
    generation_id: str,
    pass_key: str,
    merged: Sequence[MergedPublicationEvidence],
    corpus: Sequence[CorpusOutputEvidence],
    survivor_reduction: SurvivorReduction,
    naming: Sequence[NamingEvidence],
    provenance_decision_keys: Mapping[tuple[str, str], Sequence[str]],
) -> tuple[MaterializationIntent, ...]:
    """Derive intents from one exact authority-bound survivor partition."""
    ordered = tuple(sorted(merged, key=lambda item: (item.author_key, item.publication_key)))
    members = {(item.author_key, item.publication_key) for item in ordered}
    reduction_members = {(item.author_key, item.publication_key) for item in survivor_reduction.decisions}
    if reduction_members != members:
        raise ValueError("survivor reduction publication membership changed")
    if len(members) != len(ordered) or set(provenance_decision_keys) != members:
        raise ValueError("materialization publication membership changed")
    existing = {(item.author_key, item.publication_key): item for item in corpus}
    expected_reduction = derive_survivor_reduction(ordered, corpus)
    if survivor_reduction != expected_reduction:
        raise ValueError("survivor reduction authority changed")
    partition = {(item.author_key, item.publication_key): item.disposition for item in survivor_reduction.decisions}
    emitted_members = {key for key, disposition in partition.items() if disposition is SurvivorDisposition.EMITTED}
    naming_map = {(item.author_key, item.publication_key): item for item in naming}
    if len(naming_map) != len(naming) or set(naming_map) != emitted_members:
        raise ValueError("terminal naming evidence membership changed")
    intents: list[MaterializationIntent] = []
    for item in ordered:
        key = (item.author_key, item.publication_key)
        fields = item.final_entry.get("fields")
        if not isinstance(fields, Mapping) or not fields:
            raise ValueError("merged publication fields changed")
        prior = existing.get(key)
        disposition = partition[key]
        if disposition is SurvivorDisposition.EXISTING_REMOVE:
            if prior is None:
                raise ValueError("survivor reduction removal authority changed")
            intents.append(
                MaterializationIntent(
                    generation_id,
                    pass_key,
                    item.author_key,
                    item.publication_key,
                    prior.source_path,
                    prior.source_path,
                    IntentKind.REMOVE,
                    prior.before_digest,
                    None,
                    "publication-merge",
                    _MERGE_REDUCER_VERSION,
                    evidence_digest(()),
                    removal_reason="duplicate-doi-loser",
                )
            )
            continue
        if disposition is SurvivorDisposition.ABSENT_DUPLICATE_SUPPRESSED:
            continue
        final_entry = _thaw(item.final_entry)
        if not isinstance(final_entry, dict):
            raise ValueError("merged publication entry changed")
        name = naming_map[key]
        content_digest = name.content_digest
        target = name.target_path
        if set(name.final_fields) != {str(field) for field in fields}:
            raise ValueError("terminal naming fields changed")
        decision_keys = tuple(sorted(provenance_decision_keys[key]))
        if len(decision_keys) != len(set(decision_keys)) or not decision_keys:
            raise ValueError("materialization provenance membership changed")
        kind = (
            IntentKind.KEEP
            if prior is not None and prior.before_digest == content_digest and prior.source_path == target
            else IntentKind.UPSERT
        )
        intents.append(
            MaterializationIntent(
                generation_id,
                pass_key,
                item.author_key,
                item.publication_key,
                prior.source_path if prior is not None else target,
                target,
                kind,
                prior.before_digest if prior is not None else None,
                content_digest,
                "publication-merge",
                _MERGE_REDUCER_VERSION,
                evidence_digest(decision_keys),
                tuple(str(field) for field in fields),
                content_digest,
            )
        )
    return tuple(sorted(intents, key=lambda item: item.key))


@dataclass(frozen=True)
class LateIdentifierCandidate:
    """One normalized identifier claim retained with its exact source authority."""

    kind: str
    value: str
    source_digest: str
    request_key: str | None
    ordinal: int
    identity_accepted: bool
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if (
            self.kind not in _LATE_IDENTIFIER_KINDS
            or not self.value
            or self.ordinal < 0
            or isinstance(self.ordinal, bool)
            or not _DIGEST_RE.fullmatch(self.source_digest)
            or (self.request_key is not None and not _DIGEST_RE.fullmatch(self.request_key))
        ):
            raise ValueError("late identifier candidate changed")
        if not isinstance(self.identity_accepted, bool):
            raise TypeError("late identifier identity verdict changed")
        ensure_safe_durable_text(self.value)
        normalized = self.value
        if self.kind == "doi":
            normalized = normalize_doi(self.value) or ""
            if not normalized or find_doi_in_text(normalized) != normalized:
                raise ValueError("late DOI candidate changed")
        elif self.kind == "pmid" and not self.value.isdigit():
            raise ValueError("late PMID candidate changed")
        elif self.kind == "arxiv":
            normalized = re.sub(r"v\d+$", "", self.value).casefold()
            if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})", normalized):
                raise ValueError("late arXiv candidate changed")
        elif self.kind == "url_sha256" and not _DIGEST_RE.fullmatch(self.value):
            raise ValueError("late URL digest changed")
        object.__setattr__(self, "value", normalized)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "identity_accepted": self.identity_accepted,
                    "kind": self.kind,
                    "ordinal": self.ordinal,
                    "request_key": self.request_key,
                    "source_digest": self.source_digest,
                    "value": self.value,
                }
            ),
        )


@dataclass(frozen=True)
class LateIdentifierEvidence:
    """All normalized identifier claims for one stable publication."""

    author_key: str
    publication_key: str
    candidates: tuple[LateIdentifierCandidate, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        candidates = tuple(
            sorted(
                self.candidates,
                key=lambda item: (
                    item.kind,
                    item.value,
                    item.source_digest,
                    item.request_key or "",
                    item.ordinal,
                    item.identity_accepted,
                ),
            )
        )
        if len({item.digest for item in candidates}) != len(candidates):
            raise ValueError("duplicate late identifier candidate")
        object.__setattr__(self, "candidates", candidates)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "author_key": self.author_key,
                    "candidates": [item.digest for item in candidates],
                    "publication_key": self.publication_key,
                }
            ),
        )


def _accepted_identifiers(candidate: Mapping[str, object]) -> tuple[dict[str, str], tuple[str, ...]]:
    identifiers: dict[str, str] = {}
    external = candidate.get("externalIds")
    external_values = external if isinstance(external, Mapping) else {}
    doi = normalize_doi(
        str(
            candidate.get("DOI")
            or candidate.get("doi")
            or external_values.get("DOI")
            or _pubmed_article_doi(candidate)
            or ""
        )
    )
    if doi and find_doi_in_text(doi) == doi:
        identifiers["doi"] = doi
    arxiv = str(external_values.get("ArXiv") or candidate.get("arxiv_id") or "").strip()
    if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[A-Za-z.-]+/\d{7})(?:v\d+)?", arxiv):
        identifiers["arxiv"] = re.sub(r"v\d+$", "", arxiv).casefold()
    pmid = str(external_values.get("PubMed") or candidate.get("pmid") or candidate.get("uid") or "").strip()
    if pmid.isdigit():
        identifiers["pmid"] = pmid
    paper_id = candidate.get("paperId")
    if isinstance(paper_id, str) and paper_id:
        identifiers["s2_paper_id"] = paper_id
    corpus_id = external_values.get("CorpusId")
    if isinstance(corpus_id, str) and corpus_id:
        identifiers["s2_corpus_id"] = corpus_id
    openalex_id = candidate.get("id")
    if isinstance(openalex_id, str) and openalex_id.startswith("https://openalex.org/W"):
        identifiers["openalex_id"] = openalex_id.removeprefix("https://openalex.org/")
    urls: list[str] = []
    for value in (candidate.get("URL"), candidate.get("url"), candidate.get("abs_url"), candidate.get("id")):
        if isinstance(value, str):
            try:
                ensure_public_https_url(value)
            except ValueError:
                continue
            urls.append(value)
    return identifiers, tuple(urls)


def derive_late_identifier_evidence(
    seeds: Sequence[PublicationSeedEvidence],
    source_waves: Sequence[DiscoveryWave],
    observations: Sequence[DiscoveryObservation],
) -> tuple[LateIdentifierEvidence, ...]:
    """Rederive exact late identifiers from identity-accepted normalized candidates."""
    ordered = tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key)))
    tasks = {
        decision.task.key: decision.task
        for wave in source_waves
        for decision in wave.decisions
        if decision.task.request is not None
    }
    observed = {item.task.key: item for item in observations}
    if (
        len(tasks)
        != sum(1 for wave in source_waves for decision in wave.decisions if decision.task.request is not None)
        or len(observed) != len(observations)
        or set(observed) != set(tasks)
    ):
        raise ValueError("late identifier observation membership changed")
    by_member: dict[tuple[str, str], list[DiscoveryObservation]] = {}
    for key, task in tasks.items():
        item = observed[key]
        if item.task != task or item.disposition not in _SATISFIED or task.publication_key is None:
            raise ValueError("late identifier observation membership changed")
        by_member.setdefault((task.author_key, task.publication_key), []).append(item)
    evidence: list[LateIdentifierEvidence] = []
    for seed in ordered:
        candidates: list[LateIdentifierCandidate] = []
        for ordinal, (key, value) in enumerate(sorted(seed.exact_identifiers.items())):
            if not isinstance(value, str):
                raise ValueError("publication seed exact identifiers changed")
            candidates.append(LateIdentifierCandidate(key, value, seed.seed_digest, None, ordinal, True))
        for observation in sorted(
            by_member.get((seed.author_key, seed.publication_key), []), key=lambda item: item.task.key
        ):
            if observation.disposition is not TaskDisposition.SUCCEEDED:
                continue
            response_members = _normalized_response_records(observation.task.provider, observation.response)
            baseline = {
                "type": seed.baseline_entry.get("type"),
                "key": seed.baseline_entry.get("key"),
                "fields": dict(_seed_fields(seed)),
            }
            request_key = observation.task.request.key if observation.task.request is not None else None
            for ordinal, candidate in enumerate(response_members):
                if not isinstance(candidate, Mapping):
                    continue
                projected = _project_provider_record(observation.task.provider, candidate, seed.publication_key)
                verdict = bool(
                    projected is not None
                    and evaluate_identity(baseline, projected, context=IdentityContext.ENRICHMENT).verdict
                )
                identifiers, urls = _accepted_identifiers(candidate)
                if observation.task.provider == "web" and "doi" in identifiers:
                    verdict = True
                for kind, value in sorted(identifiers.items()):
                    candidates.append(
                        LateIdentifierCandidate(
                            kind,
                            value,
                            observation.response_digest,
                            request_key,
                            ordinal,
                            verdict,
                        )
                    )
                candidates.extend(
                    LateIdentifierCandidate(
                        "url_sha256",
                        evidence_digest({"scheme": "https", "url": value}),
                        observation.response_digest,
                        request_key,
                        ordinal,
                        verdict,
                    )
                    for value in urls
                )
        evidence.append(LateIdentifierEvidence(seed.author_key, seed.publication_key, tuple(candidates)))
    return tuple(evidence)


@dataclass(frozen=True)
class LateDoiSourceEvidence:
    """One normalized deterministic or HTML DOI claim retained for revalidation."""

    doi: str | None
    source_kind: str
    source_digest: str
    request_key: str | None
    ordinal: int
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        doi = normalize_doi(self.doi) if self.doi is not None else None
        if (doi is None and self.doi is not None) or (doi is not None and find_doi_in_text(doi) != doi):
            raise ValueError("late DOI source changed")
        if self.source_kind not in {"deterministic", "html"}:
            raise ValueError("late DOI source kind changed")
        if doi is None and self.source_kind != "html":
            raise ValueError("only HTML DOI evidence may be absent")
        if (
            not _DIGEST_RE.fullmatch(self.source_digest)
            or (self.request_key is not None and not _DIGEST_RE.fullmatch(self.request_key))
            or isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
        ):
            raise ValueError("late DOI source authority changed")
        object.__setattr__(self, "doi", doi)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "doi": doi,
                    "ordinal": self.ordinal,
                    "request_key": self.request_key,
                    "source_digest": self.source_digest,
                    "source_kind": self.source_kind,
                }
            ),
        )


@dataclass(frozen=True)
class LateDoiEvidence:
    """Exact normalized late DOI union for one stable publication member."""

    author_key: str
    publication_key: str
    doi: str | None
    sources: tuple[LateDoiSourceEvidence, ...]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        sources = tuple(
            sorted(
                self.sources,
                key=lambda item: (
                    item.doi or "",
                    item.source_kind,
                    item.source_digest,
                    item.request_key or "",
                    item.ordinal,
                ),
            )
        )
        if len({item.digest for item in sources}) != len(sources):
            raise ValueError("duplicate late DOI source")
        normalized = normalize_doi(self.doi) if self.doi is not None else None
        source_dois = {item.doi for item in sources if item.doi is not None}
        if (normalized is None and source_dois) or (normalized is not None and source_dois != {normalized}):
            raise ValueError("late DOI source union changed")
        object.__setattr__(self, "doi", normalized)
        object.__setattr__(self, "sources", sources)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "author_key": self.author_key,
                    "doi": normalized,
                    "publication_key": self.publication_key,
                    "sources": [item.digest for item in sources],
                }
            ),
        )


def derive_late_doi_evidence(
    seeds: Sequence[PublicationSeedEvidence],
    late_identifiers: Sequence[LateIdentifierEvidence],
    html_waves: Sequence[DiscoveryWave],
    html_observations: Sequence[DiscoveryObservation],
) -> tuple[LateDoiEvidence, ...]:
    """Union accepted deterministic and terminal HTML DOI evidence exactly once."""
    ordered = tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key)))
    members = {(item.author_key, item.publication_key) for item in ordered}
    late_map = {(item.author_key, item.publication_key): item for item in late_identifiers}
    if len(members) != len(ordered) or len(late_map) != len(late_identifiers) or set(late_map) != members:
        raise ValueError("late DOI member authority changed")

    html_tasks: dict[str, TaskSpec] = {}
    for wave in html_waves:
        decisions = {(item.task.author_key, item.task.publication_key): item for item in wave.decisions}
        if len(decisions) != len(wave.decisions) or set(decisions) != members:
            raise ValueError("late DOI HTML decision membership changed")
        for decision in wave.decisions:
            task = decision.task
            if task.provider != "web" or task.operation != "doi_probe":
                raise ValueError("late DOI HTML task authority changed")
            if task.request is not None:
                if task.key in html_tasks:
                    raise ValueError("duplicate late DOI HTML task")
                html_tasks[task.key] = task
    observed = {item.task.key: item for item in html_observations}
    if len(observed) != len(html_observations) or set(observed) != set(html_tasks):
        raise ValueError("late DOI HTML observation membership changed")

    sources_by_member: dict[tuple[str, str], list[LateDoiSourceEvidence]] = {member: [] for member in members}
    for member, late in sorted(late_map.items()):
        for candidate in late.candidates:
            if candidate.kind == "doi" and candidate.identity_accepted:
                sources_by_member[member].append(
                    LateDoiSourceEvidence(
                        candidate.value,
                        "deterministic",
                        candidate.source_digest,
                        candidate.request_key,
                        candidate.ordinal,
                    )
                )
    html_schema = capability_for("web", "doi_probe", "1").decoder_schema
    for task_key, task in sorted(html_tasks.items()):
        observation = observed[task_key]
        if (
            observation.task != task
            or observation.schema_version != html_schema
            or observation.disposition is not TaskDisposition.SUCCEEDED
        ):
            raise ValueError("late DOI HTML observation authority changed")
        value = observation.response.get("doi")
        doi = normalize_doi(value) if isinstance(value, str) else None
        if (
            set(observation.response) != {"doi"}
            or (value is not None and (doi is None or find_doi_in_text(doi) != doi))
            or task.request is None
            or task.publication_key is None
        ):
            raise ValueError("late DOI HTML response changed")
        sources_by_member[(task.author_key, task.publication_key)].append(
            LateDoiSourceEvidence(doi, "html", observation.response_digest, task.request.key, 0)
        )

    evidence: list[LateDoiEvidence] = []
    for seed in ordered:
        sources = tuple(sources_by_member[(seed.author_key, seed.publication_key)])
        dois = {item.doi for item in sources if item.doi is not None}
        if len(dois) > 1:
            raise ValueError("late DOI candidates conflict")
        evidence.append(
            LateDoiEvidence(
                seed.author_key,
                seed.publication_key,
                next(iter(dois)) if dois else None,
                sources,
            )
        )
    return tuple(evidence)


def _late_doi_evidence_map(
    seeds: Sequence[PublicationSeedEvidence], evidence: Sequence[LateDoiEvidence]
) -> tuple[tuple[PublicationSeedEvidence, ...], dict[tuple[str, str], LateDoiEvidence]]:
    ordered = tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key)))
    evidence_map = {(item.author_key, item.publication_key): item for item in evidence}
    members = {(item.author_key, item.publication_key) for item in ordered}
    if len(members) != len(ordered) or len(evidence_map) != len(evidence) or set(evidence_map) != members:
        raise ValueError("late DOI evidence membership changed")
    return ordered, evidence_map


def _known_seed_doi(seed: PublicationSeedEvidence) -> str | None:
    value = seed.exact_identifiers.get("doi")
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("known DOI seed authority changed")
    doi = normalize_doi(value)
    if doi is None or find_doi_in_text(doi) != doi:
        raise ValueError("known DOI seed authority changed")
    return doi


def plan_late_doi_csl(
    seeds: Sequence[PublicationSeedEvidence],
    evidence: Sequence[LateDoiEvidence],
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Plan one exact CSL decision for every stable late-DOI publication member."""
    ordered, evidence_map = _late_doi_evidence_map(seeds, evidence)
    capability = capability_for("doi_csl", "csl_lookup", authority.policy.adapter_versions["doi_csl"])
    decisions: list[DiscoveryDecision] = []
    for seed in ordered:
        item = evidence_map[(seed.author_key, seed.publication_key)]
        known = _known_seed_doi(seed)
        if item.doi is None:
            reason = (
                ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE
                if known is not None
                else ApplicabilityReason.NO_APPLICABLE_IDENTIFIER
            )
            decisions.append(_na(seed, capability.logical_source, capability.operation, reason))
            continue
        if item.doi == known:
            decisions.append(
                _na(
                    seed,
                    capability.logical_source,
                    capability.operation,
                    ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE,
                )
            )
            continue
        request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {"doi": item.doi},
            capability.requested_fields,
            capability.adapter_version,
            authority.policy.freshness_epoch,
            capability.quota_scope,
        )
        decisions.append(
            DiscoveryDecision(
                TaskSpec(
                    seed.author_key,
                    seed.publication_key,
                    capability.logical_source,
                    capability.operation,
                    request,
                )
            )
        )
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "late_doi": [evidence_map[(seed.author_key, seed.publication_key)].digest for seed in ordered],
                "seeds": [seed.canonical_content() for seed in ordered],
            }
        ),
        authority.digest,
    )


def _reduce_late_doi_csl(
    seeds: Sequence[PublicationSeedEvidence],
    evidence: Sequence[LateDoiEvidence],
    csl_wave: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> tuple[DoiReduction, ...]:
    ordered, _evidence_map = _late_doi_evidence_map(seeds, evidence)
    canonical = plan_late_doi_csl(ordered, evidence, authority)
    if csl_wave != canonical:
        raise ValueError("late DOI CSL wave authority changed")
    applicable = {item.task.key: item.task for item in canonical.decisions if item.task.request is not None}
    observed = {item.task.key: item for item in observations}
    if len(observed) != len(observations) or set(observed) != set(applicable):
        raise ValueError("late DOI CSL observation membership changed")
    seed_map = {(item.author_key, item.publication_key): item for item in ordered}
    schema = capability_for("doi_csl", "csl_lookup", authority.policy.adapter_versions["doi_csl"]).decoder_schema
    reductions: list[DoiReduction] = []
    for decision in canonical.decisions:
        task = decision.task
        publication_key = task.publication_key or ""
        if task.request is None:
            reductions.append(DoiReduction(task.author_key, publication_key, "no_identifier", task.key))
            continue
        observation = observed[task.key]
        if (
            observation.task != task
            or observation.schema_version != schema
            or observation.disposition not in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}
        ):
            raise ValueError("late DOI CSL observation authority changed")
        status = "fallback_required"
        metadata: Mapping[str, object] = {}
        if observation.disposition is TaskDisposition.SUCCEEDED:
            value = observation.response.get("metadata")
            if not isinstance(value, Mapping):
                raise ValueError("late DOI CSL response changed")
            requested = task.request.normalized_payload.get("doi")
            returned = normalize_doi(str(value.get("DOI") or requested or ""))
            if not isinstance(requested, str) or returned != requested:
                raise ValueError("late DOI CSL identity changed")
            projected = _project_provider_record("doi_csl", value, publication_key)
            baseline_value = _thaw(seed_map[(task.author_key, publication_key)].baseline_entry)
            if not isinstance(baseline_value, Mapping):
                raise ValueError("late DOI baseline authority changed")
            baseline = dict(baseline_value)
            if (
                projected is not None
                and evaluate_identity(baseline, projected, context=IdentityContext.ENRICHMENT).verdict
            ):
                status = "identity_matched"
                metadata = value
        reductions.append(DoiReduction(task.author_key, publication_key, status, task.key, metadata))
    return tuple(sorted(reductions, key=lambda item: (item.author_key, item.publication_key)))


def plan_late_doi_bibtex(
    seeds: Sequence[PublicationSeedEvidence],
    evidence: Sequence[LateDoiEvidence],
    csl_wave: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Plan exact conditional BibTeX decisions after terminal late CSL evidence."""
    ordered, _evidence_map = _late_doi_evidence_map(seeds, evidence)
    reductions = _reduce_late_doi_csl(ordered, evidence, csl_wave, observations, authority)
    reduction_map = {(item.author_key, item.publication_key): item for item in reductions}
    csl_map = {(item.task.author_key, item.task.publication_key): item for item in csl_wave.decisions}
    capability = capability_for("doi_bibtex", "bibtex_lookup", authority.policy.adapter_versions["doi_bibtex"])
    decisions: list[DiscoveryDecision] = []
    for seed in ordered:
        csl_decision = csl_map[(seed.author_key, seed.publication_key)]
        reduction = reduction_map[(seed.author_key, seed.publication_key)]
        if csl_decision.task.request is None:
            decisions.append(
                _na(
                    seed,
                    capability.logical_source,
                    capability.operation,
                    csl_decision.reason or ApplicabilityReason.NO_APPLICABLE_IDENTIFIER,
                )
            )
            continue
        if reduction.status == "identity_matched":
            decisions.append(
                _na(
                    seed,
                    capability.logical_source,
                    capability.operation,
                    ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE,
                )
            )
            continue
        doi = csl_decision.task.request.normalized_payload.get("doi")
        if not isinstance(doi, str):
            raise ValueError("late DOI CSL request identity changed")
        request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {"doi": doi},
            capability.requested_fields,
            capability.adapter_version,
            authority.policy.freshness_epoch,
            capability.quota_scope,
        )
        decisions.append(
            DiscoveryDecision(
                TaskSpec(
                    seed.author_key,
                    seed.publication_key,
                    capability.logical_source,
                    capability.operation,
                    request,
                )
            )
        )
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "csl_input": csl_wave.input_digest,
                "late_doi": [item.digest for item in sorted(evidence, key=lambda item: item.digest)],
                "observations": [item.response_digest for item in sorted(observations, key=lambda item: item.task.key)],
                "reductions": [item.digest for item in reductions],
            }
        ),
        authority.digest,
    )


def reduce_late_doi_observations(
    seeds: Sequence[PublicationSeedEvidence],
    evidence: Sequence[LateDoiEvidence],
    csl_wave: DiscoveryWave,
    csl_observations: Sequence[DiscoveryObservation],
    bibtex_wave: DiscoveryWave,
    bibtex_observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> tuple[DoiReduction, ...]:
    """Reduce terminal late CSL and conditional BibTeX evidence exactly."""
    ordered, _evidence_map = _late_doi_evidence_map(seeds, evidence)
    base = _reduce_late_doi_csl(ordered, evidence, csl_wave, csl_observations, authority)
    canonical = plan_late_doi_bibtex(ordered, evidence, csl_wave, csl_observations, authority)
    if bibtex_wave != canonical:
        raise ValueError("late DOI BibTeX wave authority changed")
    applicable = {item.task.key: item.task for item in canonical.decisions if item.task.request is not None}
    observed = {item.task.key: item for item in bibtex_observations}
    if len(observed) != len(bibtex_observations) or set(observed) != set(applicable):
        raise ValueError("late DOI BibTeX observation membership changed")
    seed_map = {(item.author_key, item.publication_key): item for item in ordered}
    tasks_by_member = {(item.author_key, item.publication_key): item for item in applicable.values()}
    schema = capability_for(
        "doi_bibtex", "bibtex_lookup", authority.policy.adapter_versions["doi_bibtex"]
    ).decoder_schema
    merged: list[DoiReduction] = []
    for reduction in base:
        task = tasks_by_member.get((reduction.author_key, reduction.publication_key))
        if task is None or reduction.status != "fallback_required":
            merged.append(reduction)
            continue
        observation = observed[task.key]
        if (
            observation.task != task
            or observation.schema_version != schema
            or observation.disposition not in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}
        ):
            raise ValueError("late DOI BibTeX observation authority changed")
        if observation.disposition is TaskDisposition.CONFIRMED_EMPTY:
            merged.append(reduction)
            continue
        metadata = observation.response.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("late DOI BibTeX response changed")
        baseline_value = _thaw(seed_map[(reduction.author_key, reduction.publication_key)].baseline_entry)
        if not isinstance(baseline_value, Mapping):
            raise ValueError("late DOI baseline authority changed")
        baseline = dict(baseline_value)
        projected = _project_provider_record("doi_bibtex", metadata, reduction.publication_key)
        projected_value = _thaw(projected) if projected is not None else None
        if (
            not isinstance(projected_value, Mapping)
            or not evaluate_identity(baseline, dict(projected_value), context=IdentityContext.ENRICHMENT).verdict
        ):
            merged.append(reduction)
            continue
        fields = metadata.get("fields")
        if not isinstance(fields, Mapping):
            raise ValueError("late DOI BibTeX fields changed")
        selected: dict[str, object] = {
            "DOI": fields.get("doi"),
            "container-title": fields.get("journal") or fields.get("booktitle"),
            "publisher": fields.get("publisher"),
            "title": fields.get("title"),
        }
        author = fields.get("author")
        if isinstance(author, str) and author.strip():
            selected["author"] = ({"literal": author.strip()},)
        year = fields.get("year")
        if isinstance(year, str) and year.isdigit():
            selected["issued"] = {"date-parts": ((int(year),),)}
        merged.append(
            DoiReduction(
                reduction.author_key,
                reduction.publication_key,
                "identity_matched",
                task.key,
                selected,
            )
        )
    return tuple(sorted(merged, key=lambda item: (item.author_key, item.publication_key)))


@dataclass(frozen=True)
class _HtmlProbeCandidate:
    candidate_digest: str
    url_digest: str
    locators: tuple[tuple[str, str, int], ...]
    raw_url: str = field(repr=False)


def _html_probe_candidates_by_member(
    seeds: Sequence[PublicationSeedEvidence],
    source_waves: Sequence[DiscoveryWave],
    observations: Sequence[DiscoveryObservation],
    late_identifiers: Sequence[LateIdentifierEvidence],
) -> Mapping[tuple[str, str], tuple[_HtmlProbeCandidate, ...]]:
    members = {(seed.author_key, seed.publication_key) for seed in seeds}
    accepted_by_member = {
        (late.author_key, late.publication_key): {
            (item.source_digest, item.request_key, item.ordinal, item.value)
            for item in late.candidates
            if item.kind == "url_sha256" and item.identity_accepted and item.request_key is not None
        }
        for late in late_identifiers
    }
    tasks = {
        decision.task.key: decision.task
        for wave in source_waves
        for decision in wave.decisions
        if decision.task.request is not None
        and (decision.task.author_key, decision.task.publication_key or "") in members
    }
    observed = {item.task.key: item for item in observations if item.task.key in tasks}
    values_by_member: dict[tuple[str, str], list[tuple[str, str, str, str, int, str]]] = {
        member: [] for member in members
    }
    for task_key, task in sorted(tasks.items()):
        observation = observed.get(task_key)
        if observation is None or observation.task != task or observation.disposition is not TaskDisposition.SUCCEEDED:
            continue
        member = (task.author_key, task.publication_key or "")
        accepted = accepted_by_member.get(member, set())
        request_key = task.request.key if task.request is not None else None
        if request_key is None:
            continue
        for ordinal, candidate in enumerate(_normalized_response_records(task.provider, observation.response)):
            _identifiers, urls = _accepted_identifiers(candidate)
            for url in urls:
                candidate_digest = evidence_digest({"scheme": "https", "url": url})
                if (observation.response_digest, request_key, ordinal, candidate_digest) not in accepted:
                    continue
                try:
                    built = build_request("web.doi_probe.v1", {"url": url})
                except ValueError:
                    continue
                url_digest = built.identity_payload.get("url_digest")
                if not isinstance(url_digest, str):
                    raise ValueError("HTML probe builder identity changed")
                values_by_member[member].append(
                    (candidate_digest, url_digest, observation.response_digest, request_key, ordinal, url)
                )
    result: dict[tuple[str, str], tuple[_HtmlProbeCandidate, ...]] = {}
    for member, values in sorted(values_by_member.items()):
        by_url_digest: dict[str, list[tuple[str, str, str, str, int, str]]] = {}
        for value in sorted(values):
            by_url_digest.setdefault(value[1], []).append(value)
        result[member] = tuple(
            _HtmlProbeCandidate(
                values[0][0],
                url_digest,
                tuple(sorted((value[2], value[3], value[4]) for value in values)),
                values[0][5],
            )
            for url_digest, values in sorted(by_url_digest.items())
        )
    return result


def plan_html_probe_wave(
    seeds: Sequence[PublicationSeedEvidence],
    late_identifiers: Sequence[LateIdentifierEvidence],
    source_waves: Sequence[DiscoveryWave],
    observations: Sequence[DiscoveryObservation],
    candidate_ordinal: int,
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Plan one aggregate indexed HTML candidate wave without persisting raw URLs."""
    if (
        isinstance(candidate_ordinal, bool)
        or candidate_ordinal < 0
        or candidate_ordinal >= authority.policy.max_html_probe_waves
    ):
        raise ValueError("HTML probe wave bound changed")
    ordered = tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key)))
    late_map = {(item.author_key, item.publication_key): item for item in late_identifiers}
    if len(late_map) != len(late_identifiers) or set(late_map) != {
        (item.author_key, item.publication_key) for item in ordered
    }:
        raise ValueError("HTML probe late-identifier membership changed")
    rederived = derive_late_identifier_evidence(ordered, source_waves, observations)
    if tuple(sorted(late_identifiers, key=lambda item: (item.author_key, item.publication_key))) != rederived:
        raise ValueError("HTML probe late-identifier authority changed")
    capability = capability_for("web", "doi_probe", "1")
    candidates_by_member = _html_probe_candidates_by_member(ordered, source_waves, observations, rederived)
    decisions: list[DiscoveryDecision] = []
    for seed in ordered:
        late = late_map[(seed.author_key, seed.publication_key)]
        has_doi = any(item.kind == "doi" and item.identity_accepted for item in late.candidates)
        candidates = candidates_by_member[(seed.author_key, seed.publication_key)]
        if has_doi or candidate_ordinal >= len(candidates):
            decisions.append(
                _na(
                    seed,
                    capability.logical_source,
                    capability.operation,
                    ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED,
                )
            )
            continue
        candidate = candidates[candidate_ordinal]
        request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {"scheme": "https", "url_digest": candidate.url_digest},
            capability.requested_fields,
            capability.adapter_version,
            authority.policy.freshness_epoch,
            capability.quota_scope,
        )
        decisions.append(
            DiscoveryDecision(
                TaskSpec(
                    seed.author_key,
                    seed.publication_key,
                    capability.logical_source,
                    capability.operation,
                    request,
                )
            )
        )
    candidate_sources: dict[str, dict[str, object]] = {}
    for seed in ordered:
        member = (seed.author_key, seed.publication_key)
        selected = candidates_by_member[member][candidate_ordinal : candidate_ordinal + 1]
        for candidate in selected:
            candidate_sources[f"{seed.author_key}:{seed.publication_key}"] = {
                "candidate_digest": candidate.candidate_digest,
                "locators": candidate.locators,
                "url_digest": candidate.url_digest,
            }
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "candidate_ordinal": candidate_ordinal,
                "candidate_sources": candidate_sources,
                "late_identifiers": [item.digest for item in rederived],
                "policy_digest": authority.digest,
            }
        ),
        authority.digest,
    )


def resolve_html_probe_url(
    task: TaskSpec,
    seeds: Sequence[PublicationSeedEvidence],
    late_identifiers: Sequence[LateIdentifierEvidence],
    source_waves: Sequence[DiscoveryWave],
    observations: Sequence[DiscoveryObservation],
    candidate_ordinal: int,
    authority: DiscoveryAuthority,
) -> str:
    """Privately rederive one web task raw URL and prove its exact C1 identity."""
    member = (task.author_key, task.publication_key or "")
    planned = plan_html_probe_wave(
        seeds,
        late_identifiers,
        source_waves,
        observations,
        candidate_ordinal,
        authority,
    )
    decisions = {
        (decision.task.author_key, decision.task.publication_key or ""): decision.task for decision in planned.decisions
    }
    if decisions.get(member) != task or task.request is None:
        raise ValueError("HTML probe task identity changed")
    candidates = _html_probe_candidates_by_member(
        seeds,
        source_waves,
        observations,
        late_identifiers,
    )[member]
    raw_url = candidates[candidate_ordinal].raw_url
    return raw_url


def plan_openalex_venue_fallback(
    seeds: Sequence[PublicationSeedEvidence],
    author_names: Mapping[str, str],
    crossref: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Plan one exact OpenAlex venue decision after terminal Crossref evidence."""
    ordered = tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key)))
    if crossref.policy_digest != authority.digest or len(crossref.decisions) != len(ordered):
        raise ValueError("Crossref venue decision membership changed")
    decision_map = {(item.task.author_key, item.task.publication_key): item for item in crossref.decisions}
    if len(decision_map) != len(crossref.decisions) or set(decision_map) != {
        (item.author_key, item.publication_key) for item in ordered
    }:
        raise ValueError("Crossref venue decision membership changed")
    observed = _terminal_observations(crossref, observations, label="Crossref venue")
    capability = capability_for("openalex", "venue_search", authority.policy.adapter_versions["openalex"])
    decisions: list[DiscoveryDecision] = []
    for seed in ordered:
        predecessor = decision_map[(seed.author_key, seed.publication_key)]
        if predecessor.task.request is None:
            decisions.append(
                _na(
                    seed, capability.logical_source, capability.operation, ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED
                )
            )
            continue
        predecessor_observation = observed[predecessor.task.key]
        if (
            predecessor_observation.disposition is TaskDisposition.SUCCEEDED
            and _accepted_venue_candidates(seed, predecessor_observation, author_names[seed.author_key])
        ) or predecessor_observation.disposition not in {
            TaskDisposition.SUCCEEDED,
            TaskDisposition.CONFIRMED_EMPTY,
        }:
            decisions.append(
                _na(
                    seed, capability.logical_source, capability.operation, ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED
                )
            )
            continue
        fields = _seed_fields(seed)
        title = fields.get("title")
        venue = _venue(seed)
        if (
            venue is None
            or not isinstance(title, str)
            or not title.strip()
            or not author_names.get(seed.author_key, "").strip()
        ):
            decisions.append(
                _na(
                    seed, capability.logical_source, capability.operation, ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED
                )
            )
            continue
        request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {
                "author_key": seed.author_key,
                "per_page": _VENUE_FETCH_LIMIT,
                "query": title,
                "venue": venue,
            },
            capability.requested_fields,
            capability.adapter_version,
            authority.policy.freshness_epoch,
            capability.quota_scope,
        )
        decisions.append(
            DiscoveryDecision(
                TaskSpec(
                    seed.author_key, seed.publication_key, capability.logical_source, capability.operation, request
                )
            )
        )
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "authors": dict(sorted(author_names.items())),
                "crossref": crossref.input_digest,
                "observations": [item.response_digest for item in sorted(observations, key=lambda item: item.task.key)],
                "seeds": [item.canonical_content() for item in ordered],
                "venue_policy": {
                    "admission_limit": _VENUE_ADMISSION_LIMIT,
                    "fetch_limit": _VENUE_FETCH_LIMIT,
                    "identity_policy_version": _VENUE_IDENTITY_POLICY_VERSION,
                    "score_threshold": _VENUE_SCORE_THRESHOLD,
                },
            }
        ),
        authority.digest,
    )


__all__ = [
    "CitationKeyFragmentEvidence",
    "CorpusOutputEvidence",
    "LateDoiEvidence",
    "LateDoiSourceEvidence",
    "LateIdentifierCandidate",
    "LateIdentifierEvidence",
    "MergeSourceEvidence",
    "MergedPublicationEvidence",
    "NamingEvidence",
    "ProvenanceEvidence",
    "derive_late_doi_evidence",
    "derive_late_identifier_evidence",
    "derive_materialization_intents",
    "derive_provenance_evidence",
    "merge_publication_evidence",
    "plan_crossref_venue_fallback",
    "plan_gemini_naming",
    "plan_late_doi_bibtex",
    "plan_late_doi_csl",
    "plan_openalex_venue_fallback",
    "project_merge_sources",
    "reduce_gemini_naming",
    "reduce_late_doi_observations",
]
