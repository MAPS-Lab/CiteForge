"""Pure Task 5C discovery planning authority."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from ..config import HTTP_TIMEOUT_DEFAULT
from ..id_utils import find_doi_in_text, is_secondary_doi, normalize_doi
from ..identity import IdentityContext, evaluate_identity
from ..text_utils import extract_authors_from_any, extract_year_from_any, safe_get_field
from .authority import PASS_WAVE_COUNT, PublicationSeedEvidence, evidence_digest
from .capabilities import REGISTRY_DIGEST, build_request, capability_for, validate_capability_wire
from .ledger import ApplicabilityReason, Ledger, RequestSpec, TaskClaim, TaskSpec
from .privacy import ensure_safe_durable_text
from .types import TaskDisposition

if TYPE_CHECKING:
    from ..clients.search_apis import OpenReviewRuntimeSession
    from .transport import SendOperation

_MAX_PLAN_ROUNDS = 64
_FIXED_EXPANSION_ROUNDS = 3
_ADAPTERS = frozenset(
    {
        "arxiv",
        "crossref",
        "doi_bibtex",
        "doi_csl",
        "europepmc",
        "gemini",
        "openalex",
        "openreview",
        "pubmed",
        "s2",
        "serply",
    }
)
_BROAD_PROVIDERS = frozenset({"arxiv", "crossref", "europepmc", "openalex", "openreview", "pubmed", "s2", "serply"})
_CONDITIONAL_PROVIDERS = frozenset({"gemini", "s2", "serply"})
_PROVIDER_MODES = frozenset({"required", "if_configured", "disabled"})
_CANDIDATE_LIMITS = MappingProxyType(
    {
        "arxiv": 10,
        "crossref": 20,
        "europepmc": 20,
        "openalex": 20,
        "openreview": 20,
        "pubmed": 5,
        "s2": 15,
        "serply": 20,
    }
)
_AUTHORITY_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}")


def _freeze_str_mapping(value: Mapping[str, str], name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and key and isinstance(item, str) and item for key, item in value.items()
    ):
        raise ValueError(f"{name} must be a nonblank string mapping")
    return MappingProxyType(dict(sorted(value.items())))


def _freeze_limit_mapping(value: Mapping[str, int]) -> Mapping[str, int]:
    if not isinstance(value, Mapping) or not all(
        isinstance(key, str) and key and isinstance(item, int) and not isinstance(item, bool) and item > 0
        for key, item in value.items()
    ):
        raise ValueError("candidate limits must be positive integers")
    return MappingProxyType(dict(sorted(value.items())))


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("discovery evidence requires finite JSON")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("discovery evidence requires string JSON keys")
        return MappingProxyType({key: _freeze_json(item) for key, item in sorted(value.items())})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError("discovery evidence requires strict JSON")


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _pure_csl_entry(metadata: Mapping[str, object], publication_key: str, requested_doi: str) -> dict[str, object]:
    thawed = _thaw_json(metadata)
    if not isinstance(thawed, dict):
        raise TypeError("CSL metadata must be a JSON object")
    plain = thawed
    title = safe_get_field(plain, "title") or ""
    subtitle_raw = metadata.get("subtitle")
    subtitle = subtitle_raw[0] if isinstance(subtitle_raw, tuple) and subtitle_raw else subtitle_raw
    if isinstance(subtitle, str) and subtitle:
        title = f"{title}: {subtitle}" if title else subtitle
    authors = extract_authors_from_any(plain, field_names=["author"])
    year = extract_year_from_any(plain, fallback=None)
    fields: dict[str, object] = {"title": title, "doi": requested_doi}
    if authors:
        fields["author"] = authors
    if year is not None:
        fields["year"] = str(year)
    return {"type": "misc", "key": publication_key, "fields": fields}


@dataclass(frozen=True)
class DiscoveryCredentials:
    """Wire-only C4 secrets, excluded from durable policy and representation."""

    serply_key: str | None = field(default=None, repr=False)
    s2_key: str | None = field(default=None, repr=False)
    gemini_key: str | None = field(default=None, repr=False)
    openreview_username: str | None = field(default=None, repr=False)
    openreview_password: str | None = field(default=None, repr=False)
    crossref_contact: str | None = field(default=None, repr=False)
    openalex_contact: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        values = tuple(getattr(self, name) for name in self.__dataclass_fields__)
        if any(
            value is not None
            and (
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or any(unicodedata.category(char) == "Cc" for char in value)
            )
            for value in values
        ):
            raise ValueError("discovery credentials must be absent or nonblank strings")
        if (self.openreview_username is None) != (self.openreview_password is None):
            raise ValueError("OpenReview credentials must be supplied as a pair")

    def configured(self, provider: str) -> bool:
        return {
            "gemini": self.gemini_key is not None,
            "s2": self.s2_key is not None,
            "serply": self.serply_key is not None,
        }[provider]


def resolved_provider_modes(policy: DiscoveryPolicy, credentials: DiscoveryCredentials) -> Mapping[str, str]:
    resolved = {}
    for provider, mode in policy.provider_modes.items():
        configured = credentials.configured(provider)
        if mode == "required" and not configured:
            raise ValueError(f"required {provider} discovery credential is unavailable")
        resolved[provider] = "applicable" if configured and mode != "disabled" else mode
    return MappingProxyType(dict(sorted(resolved.items())))


@dataclass(frozen=True)
class DiscoveryAuthority:
    policy: DiscoveryPolicy
    resolved_provider_modes: Mapping[str, str]
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        modes = _freeze_str_mapping(self.resolved_provider_modes, "resolved provider modes")
        if set(modes) != _CONDITIONAL_PROVIDERS or any(
            mode not in {"applicable", "disabled", "if_configured"} for mode in modes.values()
        ):
            raise ValueError("resolved provider mode authority is invalid")
        object.__setattr__(self, "resolved_provider_modes", modes)
        object.__setattr__(self, "digest", evidence_digest(self.canonical_content()))

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "capability_registry_digest": REGISTRY_DIGEST,
                "policy": self.policy.canonical_content(),
                "resolved_provider_modes": self.resolved_provider_modes,
            }
        )


def resolve_discovery_authority(policy: DiscoveryPolicy, credentials: DiscoveryCredentials) -> DiscoveryAuthority:
    return DiscoveryAuthority(policy, resolved_provider_modes(policy, credentials))


@dataclass(frozen=True)
class DiscoveryPolicy:
    """Frozen nonsecret policy for all C4 discovery decisions."""

    freshness_epoch: str
    adapter_versions: Mapping[str, str]
    candidate_limits: Mapping[str, int]
    provider_modes: Mapping[str, str]
    openreview_mode: str
    crossref_contact_enabled: bool
    openalex_contact_enabled: bool
    max_scholar_pages: int
    max_html_probe_waves: int
    planner_version: str = "1"
    reducer_version: str = "1"
    round_budget: int = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.freshness_epoch, str) or not _AUTHORITY_IDENTIFIER.fullmatch(self.freshness_epoch):
            raise ValueError("freshness epoch must be a safe identifier")
        ensure_safe_durable_text(self.freshness_epoch)
        adapters = _freeze_str_mapping(self.adapter_versions, "adapter versions")
        limits = _freeze_limit_mapping(self.candidate_limits)
        modes = _freeze_str_mapping(self.provider_modes, "provider modes")
        if set(adapters) != _ADAPTERS:
            raise ValueError("adapter version matrix is incomplete")
        if set(limits) != _BROAD_PROVIDERS or any(
            limits[provider] != fixed for provider, fixed in _CANDIDATE_LIMITS.items()
        ):
            raise ValueError("candidate limit matrix must match fixed provider bounds")
        if set(modes) != _CONDITIONAL_PROVIDERS or any(mode not in _PROVIDER_MODES for mode in modes.values()):
            raise ValueError("provider mode matrix is invalid")
        if self.openreview_mode not in {"anonymous", "authenticated"}:
            raise ValueError("OpenReview mode is invalid")
        if not isinstance(self.crossref_contact_enabled, bool) or not isinstance(self.openalex_contact_enabled, bool):
            raise TypeError("contact modes must be booleans")
        if (
            isinstance(self.max_scholar_pages, bool)
            or not isinstance(self.max_scholar_pages, int)
            or self.max_scholar_pages < 1
            or isinstance(self.max_html_probe_waves, bool)
            or not isinstance(self.max_html_probe_waves, int)
            or self.max_html_probe_waves < 0
        ):
            raise ValueError("round budget values must be nonnegative integers")
        if self.planner_version != "1" or self.reducer_version != "1":
            raise ValueError("discovery planner or reducer version is unsupported")
        operations = (
            ("doi_csl", "csl_lookup"),
            ("doi_bibtex", "bibtex_lookup"),
            ("serply", "scholar_search"),
            ("s2", "fuzzy_search"),
            ("crossref", "fuzzy_search"),
            ("openreview", "term_search"),
            ("openreview", "fallback_search"),
            ("arxiv", "fuzzy_search"),
            ("openalex", "fuzzy_search"),
            ("pubmed", "title_search"),
            ("pubmed", "summary"),
            ("europepmc", "fuzzy_search"),
            ("gemini", "short_title"),
        )
        for provider, operation in operations:
            capability = capability_for(provider, operation, adapters[provider])
            if provider != "gemini" and not capability.planner_emittable:
                raise ValueError(f"{provider} discovery capability is not planner-emittable")
        # One initial inventory page plus at most S-1 continuations is exactly S.
        fixed = PASS_WAVE_COUNT + _FIXED_EXPANSION_ROUNDS
        round_budget = fixed + self.max_scholar_pages + self.max_html_probe_waves
        if round_budget > _MAX_PLAN_ROUNDS:
            raise ValueError("discovery round budget exceeds the fixed generation maximum")
        object.__setattr__(self, "adapter_versions", adapters)
        object.__setattr__(self, "candidate_limits", limits)
        object.__setattr__(self, "provider_modes", modes)
        object.__setattr__(self, "round_budget", round_budget)

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "adapter_versions": self.adapter_versions,
                "candidate_limits": self.candidate_limits,
                "crossref_contact_enabled": self.crossref_contact_enabled,
                "freshness_epoch": self.freshness_epoch,
                "max_html_probe_waves": self.max_html_probe_waves,
                "max_scholar_pages": self.max_scholar_pages,
                "openalex_contact_enabled": self.openalex_contact_enabled,
                "openreview_mode": self.openreview_mode,
                "planner_version": self.planner_version,
                "provider_modes": self.provider_modes,
                "reducer_version": self.reducer_version,
                "round_budget": self.round_budget,
            }
        )

    @property
    def digest(self) -> str:
        return evidence_digest(self.canonical_content())


@dataclass(frozen=True)
class DiscoveryDecision:
    task: TaskSpec
    reason: ApplicabilityReason | None = None


@dataclass(frozen=True)
class DiscoveryWave:
    decisions: tuple[DiscoveryDecision, ...]
    input_digest: str
    policy_digest: str


@dataclass(frozen=True)
class DoiReduction:
    author_key: str
    publication_key: str
    status: str
    source_task_key: str
    selected_metadata: Mapping[str, object] = field(default_factory=dict)
    digest: str = field(init=False)

    def __post_init__(self) -> None:
        if self.status not in {"identity_matched", "fallback_required", "no_identifier"}:
            raise ValueError("DOI reduction status is invalid")
        metadata = _freeze_json(self.selected_metadata)
        if not isinstance(metadata, Mapping):
            raise TypeError("DOI reduction metadata must be an object")
        object.__setattr__(self, "selected_metadata", metadata)
        object.__setattr__(
            self,
            "digest",
            evidence_digest(
                {
                    "author_key": self.author_key,
                    "publication_key": self.publication_key,
                    "selected_metadata": metadata,
                    "source_task_key": self.source_task_key,
                    "status": self.status,
                }
            ),
        )


def doi_reduction_is_authoritatively_complete(reduction: DoiReduction, baseline_fields: Mapping[str, object]) -> bool:
    if reduction.status != "identity_matched":
        return False
    metadata = reduction.selected_metadata
    title = metadata.get("title") or baseline_fields.get("title")
    authors = metadata.get("author")
    if not authors:
        baseline_authors = baseline_fields.get("author")
        if isinstance(baseline_authors, str) and baseline_authors.strip():
            authors = ({"literal": baseline_authors.strip()},)
    issued = metadata.get("issued")
    if not issued:
        baseline_year = baseline_fields.get("year")
        if isinstance(baseline_year, str) and baseline_year.isdigit():
            issued = {"date-parts": ((int(baseline_year),),)}
    date_parts = issued.get("date-parts") if isinstance(issued, Mapping) else None
    venue = (
        metadata.get("container-title")
        or metadata.get("publisher")
        or baseline_fields.get("journal")
        or baseline_fields.get("booktitle")
        or baseline_fields.get("publisher")
    )
    doi = normalize_doi(str(metadata.get("DOI") or baseline_fields.get("doi") or ""))
    return bool(
        isinstance(title, str)
        and title.strip()
        and isinstance(authors, tuple)
        and authors
        and all(
            isinstance(author, Mapping) and any(author.get(key) for key in ("family", "given", "literal"))
            for author in authors
        )
        and isinstance(date_parts, tuple)
        and date_parts
        and isinstance(date_parts[0], tuple)
        and date_parts[0]
        and isinstance(date_parts[0][0], int)
        and isinstance(venue, (str, tuple))
        and bool(venue)
        and doi
        and not is_secondary_doi(doi)
    )


@dataclass(frozen=True)
class DiscoveryObservation:
    """Pure normalized terminal evidence for one discovery task."""

    task: TaskSpec
    disposition: TaskDisposition
    response: Mapping[str, object]
    authoritative_empty: bool = False
    schema_version: str = "1"
    request_key: str = ""
    response_digest: str = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskSpec) or not isinstance(self.disposition, TaskDisposition):
            raise TypeError("discovery observation requires typed task evidence")
        if not isinstance(self.schema_version, str) or not self.schema_version:
            raise ValueError("discovery observation schema version is invalid")
        expected_request_key = self.task.request.key if self.task.request is not None else ""
        if self.request_key and self.request_key != expected_request_key:
            raise ValueError("discovery observation request identity changed")
        object.__setattr__(self, "request_key", expected_request_key)
        if not isinstance(self.response, Mapping):
            raise TypeError("discovery observation response must be an object")
        frozen = _freeze_json(self.response)
        if not isinstance(frozen, Mapping):
            raise TypeError("discovery observation response must be an object")
        digest = evidence_digest(frozen)
        if self.disposition is TaskDisposition.CONFIRMED_EMPTY:
            if not self.authoritative_empty or frozen:
                raise ValueError("confirmed-empty discovery evidence must be exact and empty")
        elif self.disposition is TaskDisposition.SUCCEEDED:
            if self.authoritative_empty or not frozen:
                raise ValueError("successful discovery evidence must be exact and nonempty")
            if self.task.operation == "csl_lookup":
                metadata = frozen.get("metadata")
                title = metadata.get("title") if isinstance(metadata, Mapping) else None
                valid_title = (isinstance(title, str) and bool(title.strip())) or (
                    isinstance(title, tuple)
                    and bool(title)
                    and all(isinstance(item, str) and bool(item.strip()) for item in title)
                )
                if not valid_title:
                    raise ValueError("successful CSL observation lacks normalized title evidence")
        elif frozen or self.authoritative_empty:
            raise ValueError("blocking discovery evidence cannot carry successful response data")
        object.__setattr__(self, "response", frozen)
        object.__setattr__(self, "response_digest", digest)


def _seed_doi(seed: PublicationSeedEvidence) -> str | None:
    if seed.seed_digest != seed.derived_seed_digest:
        raise ValueError("publication seed digest changed")
    if seed.baseline_digest is None:
        raise ValueError("publication seed baseline changed")
    if seed.origin_kind.value == "publication" and seed.baseline_digest != evidence_digest(seed.baseline_entry):
        raise ValueError("publication seed baseline changed")
    if set(seed.baseline_entry) != {"type", "key", "fields"}:
        raise ValueError("publication seed baseline is incomplete")
    fields = seed.baseline_entry.get("fields")
    if not isinstance(fields, Mapping):
        raise ValueError("publication seed baseline fields are incomplete")
    title = fields.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("publication seed baseline title is incomplete")
    exact = seed.exact_identifiers.get("doi")
    if exact is None:
        return None
    if not isinstance(exact, str):
        raise ValueError("publication seed DOI evidence is incomplete")
    normalized_exact = normalize_doi(exact)
    if normalized_exact is None or find_doi_in_text(normalized_exact) != normalized_exact:
        raise ValueError("publication seed DOI evidence conflicts")
    field_doi = fields.get("doi")
    if field_doi is not None and (not isinstance(field_doi, str) or normalize_doi(field_doi) != normalized_exact):
        raise ValueError("publication seed DOI evidence conflicts")
    return normalized_exact


def plan_known_doi(seeds: Sequence[PublicationSeedEvidence], authority: DiscoveryAuthority) -> DiscoveryWave:
    """Derive one exact logical CSL decision for every immutable seed."""
    if not isinstance(authority, DiscoveryAuthority):
        raise TypeError("known DOI planning requires discovery authority")
    policy = authority.policy
    ordered = tuple(sorted(seeds, key=lambda seed: (seed.author_key, seed.publication_key)))
    if len({seed.generation_id for seed in ordered}) > 1:
        raise ValueError("publication seed generation changed")
    identities = tuple((seed.author_key, seed.publication_key) for seed in ordered)
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate publication seed")
    capability = capability_for("doi_csl", "csl_lookup", policy.adapter_versions["doi_csl"])
    decisions: list[DiscoveryDecision] = []
    for seed in ordered:
        doi = _seed_doi(seed)
        if doi is None:
            task = TaskSpec(
                seed.author_key,
                seed.publication_key,
                capability.logical_source,
                capability.operation,
                None,
                applicability="not_applicable",
            )
            decisions.append(DiscoveryDecision(task, ApplicabilityReason.NO_APPLICABLE_IDENTIFIER))
            continue
        request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {"doi": doi},
            capability.requested_fields,
            capability.adapter_version,
            policy.freshness_epoch,
            capability.quota_scope,
        )
        task = TaskSpec(
            seed.author_key,
            seed.publication_key,
            capability.logical_source,
            capability.operation,
            request,
        )
        decisions.append(DiscoveryDecision(task))
    return DiscoveryWave(
        tuple(decisions),
        evidence_digest([seed.canonical_content() for seed in ordered]),
        authority.digest,
    )


def plan_broad_discovery(
    seeds: Sequence[PublicationSeedEvidence],
    author_names: Mapping[str, str],
    authority: DiscoveryAuthority,
    doi_reductions: Sequence[DoiReduction],
) -> DiscoveryWave:
    """Emit the exact eight-operation broad matrix for every publication seed."""
    policy = authority.policy
    resolved_modes = authority.resolved_provider_modes
    ordered = tuple(sorted(seeds, key=lambda seed: (seed.author_key, seed.publication_key)))
    if len({(seed.author_key, seed.publication_key) for seed in ordered}) != len(ordered):
        raise ValueError("duplicate publication seed")
    reduction_map = {(item.author_key, item.publication_key): item for item in doi_reductions}
    expected_members = {(seed.author_key, seed.publication_key) for seed in ordered}
    if len(reduction_map) != len(doi_reductions) or set(reduction_map) != expected_members:
        raise ValueError("broad DOI reduction membership changed")
    decisions: list[DiscoveryDecision] = []
    capabilities = (
        ("serply", "scholar_search"),
        ("s2", "fuzzy_search"),
        ("crossref", "fuzzy_search"),
        ("openreview", "term_search"),
        ("arxiv", "fuzzy_search"),
        ("openalex", "fuzzy_search"),
        ("pubmed", "title_search"),
        ("europepmc", "fuzzy_search"),
    )
    for seed in ordered:
        _seed_doi(seed)
        fields = seed.baseline_entry["fields"]
        if not isinstance(fields, Mapping):
            raise ValueError("publication baseline fields changed")
        title = fields.get("title")
        author = author_names.get(seed.author_key)
        if not isinstance(title, str) or not title.strip() or not isinstance(author, str) or not author.strip():
            raise ValueError("broad discovery identity is incomplete")
        year_raw = fields.get("year")
        year = int(year_raw) if isinstance(year_raw, str) and year_raw.isdigit() else None
        for provider, operation in capabilities:
            capability = capability_for(provider, operation, policy.adapter_versions[provider])
            reduction = reduction_map[(seed.author_key, seed.publication_key)]
            complete = doi_reduction_is_authoritatively_complete(reduction, fields)
            mode = "complete" if complete else resolved_modes.get(provider, "applicable")
            if mode == "complete":
                task = TaskSpec(
                    seed.author_key,
                    seed.publication_key,
                    capability.logical_source,
                    capability.operation,
                    None,
                    applicability="not_applicable",
                )
                decisions.append(DiscoveryDecision(task, ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE))
                continue
            if mode in {"disabled", "if_configured"}:
                reason = (
                    ApplicabilityReason.PROVIDER_DISABLED
                    if mode == "disabled"
                    else ApplicabilityReason.PROVIDER_NOT_CONFIGURED
                )
                task = TaskSpec(
                    seed.author_key,
                    seed.publication_key,
                    capability.logical_source,
                    capability.operation,
                    None,
                    applicability="not_applicable",
                )
                decisions.append(DiscoveryDecision(task, reason))
                continue
            limit = policy.candidate_limits[provider]
            europepmc_title = title.replace('"', "")
            payloads: dict[str, Mapping[str, object]] = {
                "serply": {"author_key": seed.author_key, "query": f'"{title}" {author}', "start": 0},
                "s2": {"author_key": seed.author_key, "author": author, "title": title, "year": year},
                "crossref": {
                    "author_key": seed.author_key,
                    "query": title,
                    "author": author,
                    "rows": limit,
                },
                "openreview": {"author_key": seed.author_key, "term": title, "limit": limit},
                "arxiv": {
                    "author_key": seed.author_key,
                    "query": f'ti:"{title}"+AND+au:"{author}"',
                    "start": 0,
                    "max_results": 10,
                    "sort_by": "relevance",
                    "sort_order": "descending",
                },
                "openalex": {"author_key": seed.author_key, "query": title, "per_page": limit},
                "pubmed": {
                    "author_key": seed.author_key,
                    "query": f"{title}[Title] AND {author}[Author]",
                    "retmax": 5,
                },
                "europepmc": {
                    "author_key": seed.author_key,
                    "query": f'TITLE:"{europepmc_title}" AND AUTH:"{author}"',
                    "page_size": limit,
                },
            }
            request = RequestSpec(
                capability.logical_source,
                capability.operation,
                capability.method,
                payloads[provider],
                capability.requested_fields,
                capability.adapter_version,
                policy.freshness_epoch,
                capability.quota_scope,
            )
            task = TaskSpec(
                seed.author_key,
                seed.publication_key,
                capability.logical_source,
                capability.operation,
                request,
            )
            decisions.append(DiscoveryDecision(task))
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "authors": dict(sorted(author_names.items())),
                "doi_reductions": [item.digest for item in sorted(doi_reductions, key=lambda item: item.digest)],
                "seeds": [seed.canonical_content() for seed in ordered],
            }
        ),
        authority.digest,
    )


def plan_doi_bibtex(
    seeds: Sequence[PublicationSeedEvidence],
    known_wave: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Expand terminal CSL evidence into exact BibTeX or no-fallback decisions."""
    policy = authority.policy
    canonical_known = plan_known_doi(seeds, authority)
    if canonical_known != known_wave:
        raise ValueError("known DOI wave authority changed")
    reductions = reduce_doi_observations(seeds, canonical_known, observations, authority)
    reduction_map = {(item.author_key, item.publication_key): item for item in reductions}
    applicable = {decision.task.key: decision.task for decision in canonical_known.decisions if decision.task.request}
    observed = {item.task.key: item for item in observations}
    capability = capability_for("doi_bibtex", "bibtex_lookup", policy.adapter_versions["doi_bibtex"])
    decisions: list[DiscoveryDecision] = []
    for decision in canonical_known.decisions:
        reduction = reduction_map[(decision.task.author_key, str(decision.task.publication_key))]
        if reduction.status == "no_identifier":
            task = TaskSpec(
                decision.task.author_key,
                decision.task.publication_key,
                capability.logical_source,
                capability.operation,
                None,
                applicability="not_applicable",
            )
            decisions.append(DiscoveryDecision(task, ApplicabilityReason.NO_APPLICABLE_IDENTIFIER))
    for task_key in sorted(applicable):
        source_task = applicable[task_key]
        reduction = reduction_map[(source_task.author_key, str(source_task.publication_key))]
        request = source_task.request
        if request is None:
            raise ValueError("terminal CSL request is missing")
        doi = request.normalized_payload.get("doi")
        if not isinstance(doi, str):
            raise ValueError("terminal CSL DOI identity is missing")
        if reduction.status == "identity_matched":
            task = TaskSpec(
                source_task.author_key,
                source_task.publication_key,
                capability.logical_source,
                capability.operation,
                None,
                applicability="not_applicable",
            )
            decisions.append(DiscoveryDecision(task, ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE))
            continue
        bib_request = RequestSpec(
            capability.logical_source,
            capability.operation,
            capability.method,
            {"doi": doi},
            capability.requested_fields,
            capability.adapter_version,
            policy.freshness_epoch,
            capability.quota_scope,
        )
        task = TaskSpec(
            source_task.author_key,
            source_task.publication_key,
            capability.logical_source,
            capability.operation,
            bib_request,
        )
        decisions.append(DiscoveryDecision(task))
    known_evidence = [
        {
            "reason": decision.reason.value if decision.reason is not None else None,
            "task": {
                "applicability": decision.task.applicability,
                "author_key": decision.task.author_key,
                "identity_digest": decision.task.identity_digest,
                "operation": decision.task.operation,
                "provider": decision.task.provider,
                "publication_key": decision.task.publication_key,
                "request_key": decision.task.request.key if decision.task.request is not None else None,
                "required": decision.task.required,
                "task_key": decision.task.key,
            },
        }
        for decision in canonical_known.decisions
    ]
    observation_evidence: list[dict[str, object]] = []
    for key in sorted(observed):
        item = observed[key]
        item_request = item.task.request
        observation_evidence.append(
            {
                "authoritative_empty": item.authoritative_empty,
                "disposition": item.disposition.value,
                "request_key": item_request.key if item_request is not None else None,
                "response_digest": item.response_digest,
                "schema_version": item.schema_version,
                "task_key": key,
            }
        )
    return DiscoveryWave(
        tuple(decisions),
        evidence_digest({"known_decisions": known_evidence, "observations": observation_evidence}),
        authority.digest,
    )


def reduce_doi_observations(
    seeds: Sequence[PublicationSeedEvidence],
    known_wave: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> tuple[DoiReduction, ...]:
    """Re-derive immutable DOI evidence status from exact seed and observation membership."""
    canonical_known = plan_known_doi(seeds, authority)
    if canonical_known != known_wave:
        raise ValueError("known DOI wave authority changed")
    applicable = {decision.task.key: decision.task for decision in canonical_known.decisions if decision.task.request}
    observed = {item.task.key: item for item in observations}
    if len(observed) != len(observations) or set(observed) != set(applicable):
        raise ValueError("terminal CSL evidence membership changed")
    seed_map = {(seed.author_key, seed.publication_key): seed for seed in seeds}
    schema = capability_for("doi_csl", "csl_lookup", authority.policy.adapter_versions["doi_csl"]).decoder_schema
    reductions: list[DoiReduction] = []
    for decision in canonical_known.decisions:
        source_task = decision.task
        publication_key = str(source_task.publication_key)
        if source_task.request is None:
            reductions.append(DoiReduction(source_task.author_key, publication_key, "no_identifier", source_task.key))
            continue
        observation = observed[source_task.key]
        if observation.task != source_task:
            raise ValueError("terminal CSL task identity changed")
        if observation.schema_version != schema:
            raise ValueError("terminal CSL schema authority changed")
        if observation.disposition not in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}:
            raise ValueError("terminal CSL evidence is blocking")
        doi = source_task.request.normalized_payload.get("doi")
        if not isinstance(doi, str):
            raise ValueError("terminal CSL DOI identity is missing")
        metadata: Mapping[str, object] = {}
        status = "fallback_required"
        if observation.disposition is TaskDisposition.SUCCEEDED:
            value = observation.response.get("metadata")
            if not isinstance(value, Mapping):
                raise ValueError("terminal CSL response is malformed")
            returned_doi = value.get("DOI")
            if returned_doi is not None and normalize_doi(str(returned_doi)) != doi:
                raise ValueError("terminal CSL DOI identity conflicts")
            candidate = _pure_csl_entry(value, publication_key, doi)
            if evaluate_identity(
                _thaw_json(seed_map[(source_task.author_key, publication_key)].baseline_entry),  # type: ignore[arg-type]
                candidate,
                context=IdentityContext.ENRICHMENT,
            ).verdict:
                status = "identity_matched"
                metadata = value
        reductions.append(DoiReduction(source_task.author_key, publication_key, status, source_task.key, metadata))
    return tuple(sorted(reductions, key=lambda item: (item.author_key, item.publication_key)))


def reduce_current_doi_observations(
    seeds: Sequence[PublicationSeedEvidence],
    known_wave: DiscoveryWave,
    csl_observations: Sequence[DiscoveryObservation],
    bibtex_wave: DiscoveryWave,
    bibtex_observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> tuple[DoiReduction, ...]:
    """Merge terminal CSL and conditional BibTeX evidence under exact membership."""
    base = reduce_doi_observations(seeds, known_wave, csl_observations, authority)
    canonical_bibtex = plan_doi_bibtex(seeds, known_wave, csl_observations, authority)
    if canonical_bibtex != bibtex_wave:
        raise ValueError("DOI BibTeX wave authority changed")
    applicable = {decision.task.key: decision.task for decision in canonical_bibtex.decisions if decision.task.request}
    observed = {item.task.key: item for item in bibtex_observations}
    if len(observed) != len(bibtex_observations) or set(observed) != set(applicable):
        raise ValueError("terminal BibTeX evidence membership changed")
    seed_map = {(seed.author_key, seed.publication_key): seed for seed in seeds}
    schema = capability_for(
        "doi_bibtex", "bibtex_lookup", authority.policy.adapter_versions["doi_bibtex"]
    ).decoder_schema
    tasks_by_publication = {(task.author_key, task.publication_key): task for task in applicable.values()}
    merged: list[DoiReduction] = []
    for reduction in base:
        task = tasks_by_publication.get((reduction.author_key, reduction.publication_key))
        if reduction.status != "fallback_required" or task is None:
            merged.append(reduction)
            continue
        observation = observed[task.key]
        if observation.task != task or observation.schema_version != schema:
            raise ValueError("terminal BibTeX task or schema authority changed")
        if observation.disposition not in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}:
            raise ValueError("terminal BibTeX evidence is blocking")
        if observation.disposition is TaskDisposition.CONFIRMED_EMPTY:
            merged.append(reduction)
            continue
        metadata = observation.response.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ValueError("terminal BibTeX response is malformed")
        candidate = _thaw_json(metadata)
        baseline = _thaw_json(seed_map[(reduction.author_key, reduction.publication_key)].baseline_entry)
        if (
            not isinstance(baseline, Mapping)
            or not isinstance(candidate, Mapping)
            or not evaluate_identity(dict(baseline), dict(candidate), context=IdentityContext.ENRICHMENT).verdict
        ):
            merged.append(reduction)
            continue
        fields = metadata.get("fields")
        if not isinstance(fields, Mapping):
            raise ValueError("terminal BibTeX metadata fields are malformed")
        author = fields.get("author")
        selected: dict[str, object] = {
            "DOI": fields.get("doi"),
            "container-title": fields.get("journal") or fields.get("booktitle"),
            "issued": {"date-parts": ((int(fields["year"]),),)}
            if isinstance(fields.get("year"), str) and str(fields["year"]).isdigit()
            else None,
            "publisher": fields.get("publisher"),
            "title": fields.get("title"),
        }
        if isinstance(author, str) and author.strip():
            selected["author"] = ({"literal": author.strip()},)
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


def build_claimed_discovery_operation(
    ledger: Ledger,
    claim: TaskClaim,
    credentials: DiscoveryCredentials,
    authority: DiscoveryAuthority,
    *,
    now: datetime,
    openreview_session: OpenReviewRuntimeSession | None = None,
) -> SendOperation:
    """Reconstruct one claimed C4 task and inject only declared runtime wire values."""
    from .decoders import decode_response
    from .transport import SendOperation

    authority = ledger.assert_discovery_authority(authority)
    task = ledger.reconstruct_claimed_task(claim, now)
    if task.request is None:
        raise ValueError("claimed discovery task lacks exact request")
    request = task.request
    expected_version = authority.policy.adapter_versions.get(task.provider)
    if expected_version != request.adapter_version:
        raise ValueError("claimed discovery adapter authority changed")
    capability = capability_for(task.provider, task.operation, request.adapter_version)
    if (
        request.method != capability.method
        or request.requested_fields != capability.requested_fields
        or request.quota_scope != capability.quota_scope
    ):
        raise ValueError("claimed discovery request does not match capability")
    built = build_request(capability.capability_id, request.normalized_payload)
    if built.method != request.method or built.identity_payload != request.normalized_payload:
        raise ValueError("claimed discovery builder identity changed")
    query = dict(built.query)
    headers = dict(built.required_headers)
    injection = built.credential_injection
    if injection == "header:X-API-KEY":
        if not credentials.serply_key:
            raise ValueError("configured discovery credential is unavailable")
        headers["X-API-KEY"] = credentials.serply_key
    elif injection == "header:x-api-key":
        if not credentials.s2_key:
            raise ValueError("configured discovery credential is unavailable")
        headers["x-api-key"] = credentials.s2_key
    elif injection == "query:mailto_if_configured":
        contact = credentials.crossref_contact if task.provider == "crossref" else credentials.openalex_contact
        enabled = (
            authority.policy.crossref_contact_enabled
            if task.provider == "crossref"
            else authority.policy.openalex_contact_enabled
        )
        if enabled != (contact is not None):
            raise ValueError("configured discovery contact is unavailable")
        if contact is not None:
            query["mailto"] = contact
    elif injection == "cookie:runtime_session_if_selected":
        if authority.policy.openreview_mode == "authenticated":
            identity = (credentials.openreview_username, credentials.openreview_password)
            if openreview_session is None or not all(isinstance(value, str) for value in identity):
                raise ValueError("configured OpenReview session is unavailable")
            cookie = openreview_session.cookie_for((str(identity[0]), str(identity[1])))
            if not cookie or cookie != cookie.strip() or any(unicodedata.category(char) == "Cc" for char in cookie):
                raise ValueError("configured OpenReview session is unavailable")
            headers["Cookie"] = cookie
        elif openreview_session is not None:
            raise ValueError("anonymous OpenReview mode rejects runtime session material")
    elif injection != "none":
        raise ValueError("unsupported discovery credential injection")
    url = built.endpoint
    if query:
        url = f"{url}?{urlencode(query, doseq=True)}"
    validate_capability_wire(capability.capability_id, request.normalized_payload, url, headers, built.body)
    context: dict[str, object] = {}
    if task.provider in {"doi_csl", "doi_bibtex"}:
        context["doi"] = request.normalized_payload["doi"]
    elif task.provider == "pubmed" and task.operation == "title_search":
        context["retmax"] = request.normalized_payload["retmax"]
    elif task.provider == "pubmed" and task.operation == "summary":
        context["requested_pmids"] = request.normalized_payload["requested_pmids"]
    return SendOperation(
        request,
        url,
        HTTP_TIMEOUT_DEFAULT,
        lambda _value: {},
        lambda _value: False,
        headers=headers or None,
        json_payload=built.body,
        idempotent=capability.idempotent,
        max_attempts=capability.max_attempts,
        response_decoder=lambda raw: decode_response(capability.decoder_id, raw, context),
        decoder_schema=capability.decoder_schema,
        max_body_bytes=capability.body_limit,
        capability_id=capability.capability_id,
    )


def plan_dynamic_expansion(
    broad_wave: DiscoveryWave,
    observations: Sequence[DiscoveryObservation],
    authority: DiscoveryAuthority,
) -> DiscoveryWave:
    """Derive singleton PubMed summaries and exact OpenReview fallback tasks."""
    if broad_wave.policy_digest != authority.digest:
        raise ValueError("broad discovery policy authority changed")
    all_decisions = {decision.task.key: decision for decision in broad_wave.decisions}
    if len(all_decisions) != len(broad_wave.decisions):
        raise ValueError("duplicate broad discovery decision")
    for decision in all_decisions.values():
        if decision.task.request is None and (
            decision.task.applicability != "not_applicable" or decision.reason is None
        ):
            raise ValueError("broad applicability evidence is incomplete")
    sources = {key: decision.task for key, decision in all_decisions.items() if decision.task.request is not None}
    observed = {item.task.key: item for item in observations}
    if len(observed) != len(observations) or set(observed) != set(sources):
        raise ValueError("dynamic expansion source membership changed")
    decisions: list[DiscoveryDecision] = []
    fallback_capability = capability_for(
        "openreview", "fallback_search", authority.policy.adapter_versions["openreview"]
    )
    for decision in all_decisions.values():
        task = decision.task
        if task.provider != "openreview" or task.operation != "term_search" or task.request is not None:
            continue
        decisions.append(
            DiscoveryDecision(
                TaskSpec(
                    task.author_key,
                    task.publication_key,
                    fallback_capability.logical_source,
                    fallback_capability.operation,
                    None,
                    applicability="not_applicable",
                ),
                decision.reason,
            )
        )
    for task_key in sorted(sources):
        task = sources[task_key]
        observation = observed[task_key]
        if observation.task != task:
            raise ValueError("dynamic expansion task identity changed")
        capability = capability_for(task.provider, task.operation, authority.policy.adapter_versions[task.provider])
        if observation.schema_version != capability.decoder_schema:
            raise ValueError("dynamic expansion schema authority changed")
        if observation.disposition not in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}:
            raise ValueError("broad discovery source is blocking")
        result_field = {
            "arxiv": "entries",
            "crossref": "results",
            "europepmc": "results",
            "openalex": "results",
            "s2": "results",
            "serply": "articles",
        }.get(task.provider)
        if result_field is not None and observation.disposition is TaskDisposition.SUCCEEDED:
            values = observation.response.get(result_field)
            if (
                not isinstance(values, tuple)
                or not values
                or len(values) > authority.policy.candidate_limits[task.provider]
            ):
                raise ValueError(f"{task.provider} result membership exceeds the bound limit")
        if task.provider == "openreview" and observation.disposition is TaskDisposition.SUCCEEDED:
            notes = observation.response.get("notes")
            if (
                not isinstance(notes, tuple)
                or not notes
                or len(notes) > authority.policy.candidate_limits["openreview"]
            ):
                raise ValueError("OpenReview result membership exceeds the bound limit")
        if task.provider not in {"pubmed", "openreview"}:
            continue
        if task.provider == "pubmed":
            if observation.disposition is TaskDisposition.CONFIRMED_EMPTY:
                continue
            if observation.disposition is not TaskDisposition.SUCCEEDED:
                raise ValueError("PubMed expansion source is blocking")
            pmids = observation.response.get("pmids")
            limit = authority.policy.candidate_limits["pubmed"]
            if (
                not isinstance(pmids, tuple)
                or not pmids
                or len(pmids) > limit
                or not all(isinstance(pmid, str) and pmid.isdigit() and pmid for pmid in pmids)
                or len(set(pmids)) != len(pmids)
            ):
                raise ValueError("PubMed PMID membership is malformed")
            capability = capability_for("pubmed", "summary", authority.policy.adapter_versions["pubmed"])
            for pmid in sorted(pmids):
                request = RequestSpec(
                    capability.logical_source,
                    capability.operation,
                    capability.method,
                    {"requested_pmids": (pmid,)},
                    capability.requested_fields,
                    capability.adapter_version,
                    authority.policy.freshness_epoch,
                    capability.quota_scope,
                )
                decisions.append(
                    DiscoveryDecision(TaskSpec(task.author_key, task.publication_key, "pubmed", "summary", request))
                )
            continue
        if observation.disposition is TaskDisposition.CONFIRMED_EMPTY:
            source_request = task.request
            if source_request is None:
                raise ValueError("OpenReview source request is missing")
            term = source_request.normalized_payload.get("term")
            if not isinstance(term, str):
                raise ValueError("OpenReview source term is missing")
            request = RequestSpec(
                fallback_capability.logical_source,
                fallback_capability.operation,
                fallback_capability.method,
                {"author_key": task.author_key, "query": term},
                fallback_capability.requested_fields,
                fallback_capability.adapter_version,
                authority.policy.freshness_epoch,
                fallback_capability.quota_scope,
            )
            decisions.append(
                DiscoveryDecision(
                    TaskSpec(task.author_key, task.publication_key, "openreview", "fallback_search", request)
                )
            )
        elif observation.disposition is TaskDisposition.SUCCEEDED:
            decisions.append(
                DiscoveryDecision(
                    TaskSpec(
                        task.author_key,
                        task.publication_key,
                        fallback_capability.logical_source,
                        fallback_capability.operation,
                        None,
                        applicability="not_applicable",
                    ),
                    ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED,
                )
            )
        else:
            raise ValueError("OpenReview expansion source is blocking")
    decision_evidence = []
    for key in sorted(all_decisions):
        decision = all_decisions[key]
        reason = decision.reason
        decision_request = decision.task.request
        decision_evidence.append(
            {
                "applicability": decision.task.applicability,
                "reason": reason.value if reason is not None else None,
                "request_key": decision_request.key if decision_request is not None else None,
                "task_key": key,
            }
        )
    return DiscoveryWave(
        tuple(sorted(decisions, key=lambda item: item.task.key)),
        evidence_digest(
            {
                "decisions": decision_evidence,
                "observations": [
                    {
                        "authoritative_empty": observed[key].authoritative_empty,
                        "disposition": observed[key].disposition.value,
                        "request_key": observed[key].request_key,
                        "response_digest": observed[key].response_digest,
                        "schema_version": observed[key].schema_version,
                        "task_key": key,
                    }
                    for key in sorted(observed)
                ],
            }
        ),
        authority.digest,
    )


__all__ = [
    "ApplicabilityReason",
    "DiscoveryAuthority",
    "DiscoveryCredentials",
    "DiscoveryDecision",
    "DiscoveryObservation",
    "DiscoveryPolicy",
    "DiscoveryWave",
    "DoiReduction",
    "build_claimed_discovery_operation",
    "plan_broad_discovery",
    "plan_doi_bibtex",
    "plan_dynamic_expansion",
    "plan_known_doi",
    "reduce_doi_observations",
    "resolve_discovery_authority",
]
