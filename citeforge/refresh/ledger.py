"""Transactional SQLite authority for resumable refresh generations."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qsl, unquote, urlsplit

from ..config import PREPRINT_ONLY_PUBLISHERS, PREPRINT_SERVERS, SIM_IDENTIFIER_TITLE_MIN
from ..id_utils import (
    extract_arxiv_eprint,
    find_doi_in_text,
    find_dois_in_text,
    is_secondary_doi,
    normalize_doi,
    normalize_strict_arxiv_id,
)
from ..merge_utils import merge_with_policy
from ..text_utils import (
    extract_year_from_any,
    format_author_dirname,
    has_placeholder,
    normalize_title,
    title_similarity,
)
from .authority import (
    PASS_WAVE_COUNT,
    PASSES,
    AggregateInput,
    CorpusItemEvidence,
    CorpusSnapshot,
    EvidenceKind,
    IntentKind,
    MaterializationIntent,
    PlannerPassReceipt,
    ProvenanceContribution,
    ProvenanceDecision,
    PublicationSeedEvidence,
    _execute_authoritative_pass,
    evidence_digest,
    pass_for,
)
from .authority import (
    canonical_json as evidence_json,
)
from .authority import (
    publication_key_for as _publication_key_authority,
)
from .census import AuthorCensus, is_valid_dblp_id, is_valid_scholar_id
from .privacy import ensure_safe_durable_text
from .types import GenerationSpec, GenerationState, PlanPhase, TaskDisposition

if TYPE_CHECKING:
    from .census import AuthorCensusRow
    from .corpus import ExistingCorpusEvidence
    from .discovery import DiscoveryAuthority, DiscoveryObservation, DiscoveryWave

_SCHEMA_VERSION = "9"
_SCHEMA_V4_FINGERPRINT = "ad516a324198dcb1816ab3c8c0191932405f210a32af122cdf3d225141305c13"
_SCHEMA_V5_FINGERPRINT = "be14f7bc658bf347c5f519d0483311ff23118e0c9569f5328939b546b1fe2f46"
_MAX_PLAN_ROUNDS = 64
_SCHEMA_V6_FINGERPRINT = "9bf51dac21ab9a519ff8461a030d0a87c7211191554f1c06024996bd4e95ff3a"
_SCHEMA_V7_FINGERPRINT = "4391a86ee7f96c62c42280042b09de5e7b2fe0b59006ab58e3abbe6f77545bdf"
_SCHEMA_V8_FINGERPRINT = "c57f9536975e14391ccad53d2d49ccecca60b052e9249e695d6f5af3cb4f2f71"
_EXPECTED_SCHEMA_FINGERPRINT = "c27b65db08f4ff37121f33d024560cebb7aa3b62805807da032d2a8d233d6751"
_LEGACY_C3_PASS_REGISTRY_DIGEST = (  # immutable registry fingerprint
    "f41a0b514dcf65e30a1fd4cab17cd3a151146f3c753786bd769e2a96e52026ae"  # noqa: S105
)
_LEGACY_C4_PASS_REGISTRY_DIGEST = (  # immutable registry fingerprint
    "4aca44ec61c5f081b1fa372705434adb4413b6e02b5f981166829cd5d41d5696"  # noqa: S105
)
_LEGACY_C5_PASS_REGISTRY_DIGEST = (  # immutable registry fingerprint
    "ac1a11deb6ea9c519b58638ac870a54d401b0ba50682fb6c82d744670a56bc7f"  # noqa: S105
)
_LEGACY_C4_PASS_IDS = frozenset({"bind_corpus_seed", "known_doi", "broad_discovery", "dynamic_expansion"})
_LEGACY_C5_PASS_IDS = frozenset(
    {"bind_corpus_seed", "known_doi", "broad_discovery", "dynamic_expansion", "venue_fallback", "late_identifiers"}
)
_SNAPSHOT_DOMAIN_SEPARATOR = "citeforge-task5c2-planner-snapshot-v1"
_CORPUS_S2_ID = re.compile(r"[0-9a-f]{40}", re.I)
_CORPUS_OPENALEX_ID = re.compile(r"(?:https://openalex\.org/)?(W\d+)", re.I)
_CORPUS_ARXIV_URL_ID = r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"
_V6_AUTHORITY_TABLES = frozenset(
    {
        "aggregate_inputs",
        "corpus_items",
        "corpus_snapshots",
        "corpus_scan_receipts",
        "discovery_policy_authority",
        "intent_provenance",
        "html_probe_waves",
        "html_probe_wave_items",
        "html_probe_terminal_receipts",
        "materialization_intents",
        "planner_pass_expected_items",
        "planner_passes",
        "provenance_contributions",
        "provenance_decisions",
        "publication_seed_evidence",
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
_TERMINAL = _SATISFIED | frozenset(
    {
        TaskDisposition.MALFORMED,
        TaskDisposition.AUTHENTICATION_FAILED,
        TaskDisposition.SCHEMA_CHANGED,
        TaskDisposition.PERMANENT_FAILURE,
        TaskDisposition.CIRCUIT_OPEN,
        TaskDisposition.AMBIGUOUS,
        TaskDisposition.BLOCKED,
        TaskDisposition.UNKNOWN,
    }
)
_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:authorization|api[_-]?key|token|secret|password|credential(?:_path)?)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+|-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"(?:authorization|api[_-]?key|token|secret|password|credential)\s*[:=])",
    re.IGNORECASE,
)
_FAULT_POINTS = frozenset(
    {
        "after_claim_commit",
        "after_attempt_commit",
        "after_response_commit",
        "after_task_terminalization",
        "after_manifest_commit",
        "after_initial_round_publications",
        "after_initial_round_tasks",
        "after_initial_round_obligations",
        "after_initial_round_round",
        "after_initial_round_commit",
        "after_reduction_publications",
        "after_reduction_tasks",
        "after_reduction_obligations",
        "after_reduction_round",
        "after_reduction_receipt",
        "after_reduction_commit",
        "after_plan_close_validations",
        "after_plan_close_commit",
        "after_v6_migration_ddl",
        "after_v6_aggregate_inputs",
        "after_v6_provenance_decisions",
        "after_v6_provenance_contributions",
        "after_v6_materialization_intents",
        "after_v6_intent_provenance",
        "after_v6_corpus_snapshot",
        "after_v6_corpus_items",
        "after_v6_seed_evidence",
        "after_c3_corpus_snapshot",
        "after_c3_corpus_items",
        "after_c3_corpus_publications",
        "after_c3_corpus_seeds",
        "after_v6_planner_pass",
        "after_v6_planner_expected_items",
        "after_v6_migration_meta",
        "after_c4_pass_receipt",
        "after_c4_expected_items",
        "after_c4_requests",
        "after_c4_consumers",
        "after_c4_tasks",
        "after_c4_obligations",
        "after_c4_round",
        "after_c4_expansion",
    }
) | frozenset(f"after_v6_migration_statement_{index}" for index in range(55))
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}")
_PROVIDER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,127}")
_HEX_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_LEGAL_GENERATION_TRANSITIONS = {
    GenerationState.PLANNING: frozenset({GenerationState.RUNNING, GenerationState.SUPERSEDED}),
    GenerationState.RUNNING: frozenset(
        {GenerationState.WAITING, GenerationState.BLOCKED, GenerationState.VALIDATING, GenerationState.SUPERSEDED}
    ),
    GenerationState.WAITING: frozenset({GenerationState.RUNNING, GenerationState.BLOCKED, GenerationState.SUPERSEDED}),
    GenerationState.BLOCKED: frozenset({GenerationState.RUNNING, GenerationState.SUPERSEDED}),
    GenerationState.VALIDATING: frozenset(
        {GenerationState.COMPLETE, GenerationState.BLOCKED, GenerationState.SUPERSEDED}
    ),
    GenerationState.COMPLETE: frozenset({GenerationState.PUBLISHED, GenerationState.SUPERSEDED}),
    GenerationState.PUBLISHED: frozenset(),
    GenerationState.SUPERSEDED: frozenset(),
}


class FaultInjectedError(RuntimeError):
    """Test-only interruption raised after a named durable boundary."""


class StaleClaimError(ValueError):
    """A task or request fencing token no longer owns durable work."""


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain_json(item) for item in value]
    return value


def _canonical(value: object) -> str:
    return json.dumps(_plain_json(value), allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _receipt_matches_authority(stored: PlannerPassReceipt, current: PlannerPassReceipt) -> bool:
    if stored == current:
        return True
    return (
        stored.pass_id in _LEGACY_C4_PASS_IDS
        and stored.registry_digest == _LEGACY_C4_PASS_REGISTRY_DIGEST
        and stored == replace(current, registry_digest=_LEGACY_C4_PASS_REGISTRY_DIGEST)
    ) or (
        stored.pass_id in _LEGACY_C5_PASS_IDS
        and stored.registry_digest == _LEGACY_C5_PASS_REGISTRY_DIGEST
        and stored == replace(current, registry_digest=_LEGACY_C5_PASS_REGISTRY_DIGEST)
    )


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _contains_secret(value: object, *, key: str = "") -> bool:
    if key and _SECRET_KEY.search(key):
        return True
    if isinstance(value, Mapping):
        return any(_contains_secret(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret(item) for item in value)
    if isinstance(value, str):
        if _SECRET_VALUE.search(value):
            return True
        parsed = urlsplit(value)
        if parsed.username is not None or parsed.password is not None:
            return True
        if parsed.query and any(_SECRET_KEY.search(query_key) for query_key, _ in parse_qsl(parsed.query)):
            return True
        normalized = value.casefold().replace("\\", "/")
        if re.search(
            r"(?:^|/)(?:\.aws/credentials|\.ssh(?:/|$)|[^/]*key\.pem$|[^/]*(?:credential|secret)[^/]*\.(?:json|pem|key)$)",
            normalized,
        ):
            return True
    return False


def _inventory_authority_contains_secret(value: object, *, key: str = "") -> bool:
    """Inspect typed authority while allowing its non-secret credential-kind field."""
    if key != "credential_kind" and key and _SECRET_KEY.search(key):
        return True
    if isinstance(value, Mapping):
        return any(_inventory_authority_contains_secret(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_inventory_authority_contains_secret(item) for item in value)
    return isinstance(value, str) and _contains_secret(value)


def _inventory_authority_content(value: Mapping[str, object], generation_id: str) -> dict[str, object]:
    """Validate the one canonical, public-data-only inventory authority schema."""
    if _inventory_authority_contains_secret(value):
        raise ValueError("secret material cannot be persisted in inventory authority")
    if set(value) != {"capabilities", "generation", "planner_version", "policy", "reducer_version"}:
        raise ValueError("invalid typed inventory authority schema")
    if value.get("generation") != generation_id:
        raise ValueError("typed inventory authority generation mismatch")
    planner_version = value.get("planner_version")
    reducer_version = value.get("reducer_version")
    if not isinstance(planner_version, str) or not isinstance(reducer_version, str):
        raise ValueError("invalid typed inventory authority versions")
    _identifier(planner_version, "inventory planner version")
    _identifier(reducer_version, "inventory reducer version")
    raw_policy = value.get("policy")
    if not isinstance(raw_policy, Mapping) or set(raw_policy) != {
        "max_publications",
        "max_scholar_pages",
        "min_year",
        "seed_adapter_versions",
    }:
        raise ValueError("invalid typed inventory authority policy")
    for field_name in ("max_publications", "max_scholar_pages", "min_year"):
        field_value = raw_policy.get(field_name)
        if not isinstance(field_value, int) or isinstance(field_value, bool) or field_value < 1:
            raise ValueError("invalid typed inventory authority policy")
    seed_versions = raw_policy.get("seed_adapter_versions")
    if not isinstance(seed_versions, Mapping) or set(seed_versions) != {"doi_csl", "s2"}:
        raise ValueError("invalid typed inventory seed authority")
    for source in ("doi_csl", "s2"):
        version = seed_versions.get(source)
        if not isinstance(version, str):
            raise ValueError("invalid typed inventory seed authority")
        _identifier(version, "inventory seed adapter version")
    raw_capabilities = value.get("capabilities")
    if not isinstance(raw_capabilities, Sequence) or isinstance(raw_capabilities, (str, bytes, bytearray)):
        raise ValueError("invalid typed inventory capabilities")
    capability_keys = {
        "adapter_version",
        "capability_id",
        "credential_kind",
        "decoder_schema",
        "logical_source",
        "media_type",
        "operation",
        "quota_scope",
        "requested_fields",
        "wire_provider",
    }
    capabilities: list[dict[str, object]] = []
    for raw_capability in raw_capabilities:
        if not isinstance(raw_capability, Mapping) or set(raw_capability) != capability_keys:
            raise ValueError("invalid typed inventory capability schema")
        capability = dict(raw_capability)
        for field_name in capability_keys - {"requested_fields"}:
            field_value = capability[field_name]
            if not isinstance(field_value, str) or not field_value:
                raise ValueError("invalid typed inventory capability value")
        if capability["media_type"] not in {"json", "xml"} or capability["credential_kind"] not in {
            "none",
            "serpapi_key",
            "s2_api_key",
        }:
            raise ValueError("invalid typed inventory capability value")
        requested_fields = capability["requested_fields"]
        if not isinstance(requested_fields, Sequence) or isinstance(requested_fields, (str, bytes, bytearray)):
            raise ValueError("invalid typed inventory requested fields")
        if not all(isinstance(item, str) and item for item in requested_fields):
            raise ValueError("invalid typed inventory requested fields")
        capability["requested_fields"] = list(requested_fields)
        capabilities.append(capability)
    ids = [str(item["capability_id"]) for item in capabilities]
    if len(ids) != len(set(ids)) or ids != sorted(ids):
        raise ValueError("typed inventory capabilities must be unique and canonical")
    return {
        "capabilities": capabilities,
        "generation": generation_id,
        "planner_version": planner_version,
        "policy": {
            "max_publications": raw_policy["max_publications"],
            "max_scholar_pages": raw_policy["max_scholar_pages"],
            "min_year": raw_policy["min_year"],
            "seed_adapter_versions": {"doi_csl": seed_versions["doi_csl"], "s2": seed_versions["s2"]},
        },
        "reducer_version": reducer_version,
    }


def _identifier(value: str, purpose: str) -> str:
    if _SECRET_KEY.search(value) or _contains_secret(value):
        raise ValueError(f"secret material cannot be persisted as {purpose}")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"invalid {purpose}")
    return value


def _provider(value: str) -> str:
    if _SECRET_KEY.search(value) or _contains_secret(value):
        raise ValueError("secret material cannot be persisted as provider")
    if not _PROVIDER_RE.fullmatch(value):
        raise ValueError("invalid provider")
    return value


def _free_text(value: str, purpose: str, *, required: bool = False) -> str:
    if required and not value.strip():
        raise ValueError(f"{purpose} is required")
    if len(value) > 2000 or "\x00" in value:
        raise ValueError(f"invalid {purpose}")
    if _contains_secret(value):
        raise ValueError(f"secret material cannot be persisted as {purpose}")
    return value


def _corpus_identifiers_from_fields(fields: Mapping[str, object]) -> dict[str, str]:
    """Independently derive durable corpus identifiers at the ledger boundary."""
    for value in fields.values():
        ensure_safe_durable_text(str(value))
    explicit_doi = normalize_doi(str(fields.get("doi", "")))
    if str(fields.get("doi", "")).strip() and normalize_doi(find_doi_in_text(explicit_doi or "")) != explicit_doi:
        raise ValueError("corpus normalized entry has invalid explicit DOI")
    dois = set(find_dois_in_text(str(fields.get("doi", ""))))
    for field_name in ("url", "howpublished"):
        dois.update(find_dois_in_text(unquote(str(fields.get(field_name, "")))))
    primary = {doi for doi in dois if not is_secondary_doi(doi)}
    secondary = dois - primary
    if len(primary) > 1 or len(secondary) > 1:
        raise ValueError("corpus normalized entry has conflicting DOI evidence")
    result: dict[str, str] = {}
    canonical_doi = next(iter(primary or secondary), None)
    if canonical_doi:
        result["doi"] = canonical_doi
    if primary and secondary:
        result["secondary_doi"] = next(iter(secondary))
    arxiv_candidates = set()
    if canonical_doi:
        doi_arxiv = re.fullmatch(r"10\.48550/arxiv\.(.+)", canonical_doi, re.I)
        if doi_arxiv:
            value = re.sub(r"v\d+$", "", doi_arxiv.group(1), flags=re.I)
            prefix, separator, suffix = value.partition("/")
            arxiv_candidates.add(f"{prefix.casefold()}/{suffix}" if separator else value)
    for candidate_fields in (
        {"archiveprefix": fields.get("archiveprefix"), "eprint": fields.get("eprint")},
        {"doi": fields.get("doi")},
        {"journal": fields.get("journal")},
        {"journal": fields.get("howpublished")},
    ):
        if arxiv := extract_arxiv_eprint({"fields": candidate_fields}):
            prefix, separator, suffix = arxiv.partition("/")
            arxiv_candidates.add(f"{prefix.casefold()}/{suffix}" if separator else arxiv)
    for field_name in ("url", "howpublished"):
        for match in re.finditer(
            rf"(?i)arxiv\.org/(?:abs|pdf)/{_CORPUS_ARXIV_URL_ID}(?:v\d+)?",
            str(fields.get(field_name, "")),
        ):
            arxiv_candidates.add(match[1])
    if len(arxiv_candidates) > 1:
        raise ValueError("corpus normalized entry has conflicting arXiv evidence")
    if len(arxiv_candidates) == 1:
        arxiv = normalize_strict_arxiv_id(next(iter(arxiv_candidates)))
        if arxiv is None:
            raise ValueError("corpus normalized entry has invalid arXiv evidence")
        result["arxiv"] = arxiv
    pmids = {str(fields[key]).strip() for key in ("pmid", "x_pmid") if str(fields.get(key, "")).strip()}
    if len(pmids) > 1 or (pmids and not next(iter(pmids)).isdigit()):
        raise ValueError("corpus normalized entry has invalid PMID evidence")
    if len(pmids) == 1:
        result["pmid"] = next(iter(pmids))
    s2 = str(fields.get("x_s2_paper_id", "")).strip()
    if s2 and not _CORPUS_S2_ID.fullmatch(s2):
        raise ValueError("corpus normalized entry has invalid Semantic Scholar evidence")
    if _CORPUS_S2_ID.fullmatch(s2):
        result["s2"] = s2.lower()
    openalex = _CORPUS_OPENALEX_ID.fullmatch(str(fields.get("x_openalex_id", "")).strip())
    if fields.get("x_openalex_id") and openalex is None:
        raise ValueError("corpus normalized entry has invalid OpenAlex evidence")
    if openalex:
        result["openalex"] = openalex[1].upper()
    return dict(sorted(result.items()))


def _digest_text(value: str, purpose: str) -> str:
    if not _HEX_DIGEST_RE.fullmatch(value):
        raise ValueError(f"invalid {purpose}")
    return value


def _freeze_json(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        _canonical(value)
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON object keys must be strings")
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def _has_observation_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and not has_placeholder(value)
    if isinstance(value, Mapping):
        return bool(value) and any(_has_observation_value(item) for item in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return bool(value) and any(_has_observation_value(item) for item in value)
    return True


def _is_preprint_observation(response: Mapping[str, object]) -> bool:
    doi = response.get("doi")
    journal = str(response.get("journal") or "").casefold()
    publisher = str(response.get("publisher") or "").casefold().strip()
    return bool(
        (doi and is_secondary_doi(str(doi)))
        or any(server in journal for server in PREPRINT_SERVERS)
        or publisher in PREPRINT_ONLY_PUBLISHERS
    )


def _is_published_observation(response: Mapping[str, object]) -> bool:
    if _is_preprint_observation(response):
        return False
    doi = response.get("doi")
    journal = str(response.get("journal") or "").casefold()
    return bool(
        (doi and not is_secondary_doi(str(doi))) or (journal and not any(s in journal for s in PREPRINT_SERVERS))
    )


def _merge_proves_dominance(
    lower_provider: str,
    lower_response: Mapping[str, object],
    stronger: Sequence[tuple[str, Mapping[str, object]]],
    covered_fields: Sequence[str],
    rule: DominanceRule,
) -> bool:
    # merge_with_policy assigns primary fields to scholar_min. Until the reducer
    # exposes provenance-aware incumbents, other lower providers are unprovable.
    if lower_provider != "scholar_min":
        return False
    enrichers = [(provider, {"type": "misc", "fields": dict(response)}) for provider, response in stronger]
    primary = {"type": "misc", "fields": dict(lower_response)}
    merged = merge_with_policy(primary, enrichers)["fields"]
    lower_normalized = merge_with_policy(primary, [])["fields"]
    stronger_normalized = merge_with_policy({"type": "misc", "fields": {}}, enrichers)["fields"]
    if rule is DominanceRule.PUBLISHED_OVER_PREPRINT and (
        not _is_preprint_observation(lower_response) or not _is_published_observation(stronger_normalized)
    ):
        return False
    return all(
        field_name in stronger_normalized
        and merged.get(field_name) == stronger_normalized[field_name]
        and merged.get(field_name) != lower_normalized.get(field_name)
        for field_name in covered_fields
    )


class ApplicabilityReason(str, Enum):
    NO_APPLICABLE_IDENTIFIER = "no_applicable_identifier"
    PROVIDER_NOT_SUPPORTED = "provider_not_supported"
    PROVIDER_DISABLED = "provider_disabled"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    REDUNDANT_AUTHORITATIVE_EVIDENCE = "redundant_authoritative_evidence"
    CONDITIONAL_NOT_TRIGGERED = "conditional_not_triggered"


class DominanceRule(str, Enum):
    PUBLISHED_OVER_PREPRINT = "published_over_preprint"
    AUTHORITATIVE_METADATA = "authoritative_metadata"


class ProvenanceRule(str, Enum):
    TRUST_POLICY = "trust_policy"
    PUBLISHED_OVER_PREPRINT = "published_over_preprint"


class EvidenceState(str, Enum):
    PENDING = "pending"
    FAILED = "failed"
    SUCCEEDED = "succeeded"
    VALIDATED = "validated"


@dataclass(frozen=True)
class ProviderObservation:
    provider: str
    schema_version: str
    response: Mapping[str, object]
    authoritative_empty: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider", _provider(self.provider))
        object.__setattr__(self, "schema_version", _identifier(self.schema_version, "schema version"))
        if _contains_secret(self.response):
            raise ValueError("secret material cannot be persisted in provider observation")
        frozen = _freeze_json(self.response)
        if not isinstance(frozen, Mapping):
            raise TypeError("provider response must be a JSON object")
        object.__setattr__(self, "response", frozen)

    @property
    def digest(self) -> str:
        return _digest(dict(self.response))


@dataclass(frozen=True)
class DominanceEvidence:
    stronger_observation_keys: tuple[str, ...]
    rule: DominanceRule
    covered_fields: tuple[str, ...]
    dominated_observation_key: str

    def __post_init__(self) -> None:
        if not self.stronger_observation_keys or not self.covered_fields:
            raise ValueError("dominance evidence requires observations and covered fields")
        for key in self.stronger_observation_keys:
            _digest_text(key, "observation key")
        _digest_text(self.dominated_observation_key, "dominated observation key")
        if not isinstance(self.rule, DominanceRule):
            raise ValueError("invalid dominance rule")
        for field_name in self.covered_fields:
            if not _FIELD_RE.fullmatch(field_name):
                raise ValueError("invalid dominated field")


@dataclass(frozen=True)
class ValidationSpec:
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "validation name"))


@dataclass(frozen=True)
class MaterializationEvidence:
    staged_path: str
    manifest_digest: str
    corpus_counts: Mapping[str, int]
    validation_state: EvidenceState


@dataclass(frozen=True)
class PublicationMetadata:
    author_key: str
    publication_key: str
    discovery_source: str
    normalized_title: str
    year: int | None
    exact_identifiers: Mapping[str, str]
    baseline_output_path: str
    freshness_policy: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "author_key", _identifier(self.author_key, "author key"))
        object.__setattr__(self, "publication_key", _identifier(self.publication_key, "publication key"))
        object.__setattr__(self, "discovery_source", _provider(self.discovery_source))
        object.__setattr__(
            self, "normalized_title", _free_text(self.normalized_title, "normalized title", required=True)
        )
        if self.year is not None and (
            isinstance(self.year, bool) or not isinstance(self.year, int) or not 1000 <= self.year <= 9999
        ):
            raise ValueError("invalid publication year")
        identifiers = dict(self.exact_identifiers)
        if _contains_secret(identifiers):
            raise ValueError("secret material cannot be persisted in publication identifiers")
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in identifiers.items()):
            raise TypeError("publication identifiers must be strings")
        object.__setattr__(self, "exact_identifiers", _freeze_json(identifiers))
        object.__setattr__(self, "baseline_output_path", _free_text(self.baseline_output_path, "baseline output path"))
        object.__setattr__(self, "freshness_policy", _identifier(self.freshness_policy, "freshness policy"))


@dataclass(frozen=True)
class PlannedTask:
    task: TaskSpec
    expands_plan: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.task, TaskSpec) or not isinstance(self.expands_plan, bool):
            raise TypeError("planned task requires a task and boolean expansion flag")


@dataclass(frozen=True)
class PlanRound:
    key: str
    sequence: int
    phase: PlanPhase
    planner_id: str
    planner_version: str
    source_task_keys: tuple[str, ...]
    source_evidence_digest: str
    publications: tuple[PublicationMetadata, ...]
    tasks: tuple[PlannedTask, ...]
    task_set_digest: str
    content_digest: str


@dataclass(frozen=True)
class ReductionReceipt:
    source_task_keys: tuple[str, ...]
    round_key: str
    source_dispositions: tuple[TaskDisposition, ...]
    source_evidence_digests: tuple[str, ...]
    reduction_digest: str


@dataclass(frozen=True)
class PlanStatus:
    revision: int
    closed: bool
    discovery_closed: bool
    authority_mode: str
    plan_digest: str | None
    closure_digest: str | None
    open_expanders: int
    unbound_tasks: int


@dataclass(frozen=True)
class RequestSpec:
    """Canonical, non-secret identity of one exact provider request."""

    provider: str
    operation: str
    method: str
    normalized_payload: Mapping[str, object]
    requested_fields: tuple[str, ...]
    adapter_version: str
    freshness_epoch: str
    quota_scope: str
    key: str = field(init=False)
    _payload_json: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        payload = dict(self.normalized_payload)
        if _contains_secret(payload):
            raise ValueError("secret material cannot be persisted in a request")
        fields = tuple(sorted(set(self.requested_fields)))
        provider = _provider(self.provider)
        operation = _identifier(self.operation, "operation")
        if self.method.upper() not in {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH"}:
            raise ValueError("invalid HTTP method")
        for requested_field in fields:
            if _SECRET_KEY.search(requested_field) or _contains_secret(requested_field):
                raise ValueError("secret material cannot be persisted as requested field")
            if not _FIELD_RE.fullmatch(requested_field):
                raise ValueError("invalid requested field")
        adapter_version = _identifier(self.adapter_version, "adapter version")
        freshness_epoch = _identifier(self.freshness_epoch, "freshness epoch")
        quota_scope = _identifier(self.quota_scope, "quota scope")
        canonical = {
            "adapter_version": adapter_version,
            "freshness_epoch": freshness_epoch,
            "method": self.method.upper(),
            "normalized_payload": payload,
            "operation": operation,
            "provider": provider,
            "quota_scope": quota_scope,
            "requested_fields": fields,
        }
        object.__setattr__(self, "method", self.method.upper())
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "adapter_version", adapter_version)
        object.__setattr__(self, "freshness_epoch", freshness_epoch)
        object.__setattr__(self, "quota_scope", quota_scope)
        payload_json = _canonical(payload)
        object.__setattr__(self, "normalized_payload", _freeze_json(payload))
        object.__setattr__(self, "requested_fields", fields)
        object.__setattr__(self, "key", _digest(canonical))
        object.__setattr__(self, "_payload_json", payload_json)

    def canonical_content(self) -> dict[str, object]:
        return {
            "adapter_version": self.adapter_version,
            "freshness_epoch": self.freshness_epoch,
            "method": self.method,
            "normalized_payload": json.loads(self._payload_json),
            "operation": self.operation,
            "provider": self.provider,
            "quota_scope": self.quota_scope,
            "requested_fields": list(self.requested_fields),
        }


@dataclass(frozen=True)
class TaskSpec:
    """One author-scoped logical consumer of an exact request."""

    author_key: str
    publication_key: str | None
    provider: str
    operation: str
    request: RequestSpec | None
    required: bool = True
    applicability: str = "applicable"
    key: str = field(init=False)
    identity_digest: str = field(init=False)

    def __post_init__(self) -> None:
        author_key = _identifier(self.author_key, "author key")
        publication_key = (
            _identifier(self.publication_key, "publication key") if self.publication_key is not None else None
        )
        provider = _provider(self.provider)
        operation = _identifier(self.operation, "operation")
        applicability = _identifier(self.applicability, "applicability")
        if self.request is not None and (
            self.provider != self.request.provider or self.operation != self.request.operation
        ):
            raise ValueError("task and request provider operation must match")
        if applicability == "not_applicable" and self.request is not None:
            raise ValueError("not applicable task cannot have an applicable request")
        if applicability != "not_applicable" and self.request is None:
            raise ValueError("applicable task requires an exact request")
        identity = {
            "applicability": applicability,
            "author_key": author_key,
            "operation": operation,
            "provider": provider,
            "publication_key": publication_key,
            "request_key": self.request.key if self.request is not None else None,
            "required": self.required,
        }
        identity_digest = _digest(identity)
        object.__setattr__(self, "author_key", author_key)
        object.__setattr__(self, "publication_key", publication_key)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "applicability", applicability)
        object.__setattr__(self, "identity_digest", identity_digest)
        object.__setattr__(self, "key", identity_digest)


def inventory_tasks(
    census: AuthorCensus, adapter_versions: Mapping[str, str], freshness_epoch: str
) -> tuple[TaskSpec, ...]:
    """Derive mandatory exact inventory obligations from configured enabled census sources."""
    tasks: list[TaskSpec] = []
    for row in census.enabled_rows:
        for provider, provider_id in (("dblp", row.dblp_id), ("scholar", row.scholar_id)):
            if not provider_id:
                continue
            if provider not in adapter_versions:
                raise ValueError(f"missing adapter version for inventory provider {provider}")
            request = RequestSpec(
                provider,
                "inventory",
                "GET",
                {"profile_id": provider_id},
                ("publications",),
                adapter_versions[provider],
                freshness_epoch,
                provider,
            )
            tasks.append(TaskSpec(row.row_key, None, provider, "inventory", request))
    return tuple(sorted(tasks, key=lambda task: (task.author_key, task.provider)))


@dataclass(frozen=True)
class TaskClaim:
    key: str
    request_key: str | None
    owner: str
    lease_expires: datetime


@dataclass(frozen=True)
class RequestClaim:
    key: str
    owner: str
    lease_expires: datetime


@dataclass(frozen=True)
class RequestResult:
    key: str
    disposition: TaskDisposition
    normalized_response: Mapping[str, object] | None
    response_digest: str | None
    outcome: str | None
    http_status: int | None


@dataclass(frozen=True)
class LedgerManifest:
    data: Mapping[str, Any]
    canonical_json: str
    digest: str


class Ledger:
    @staticmethod
    def _discovery_request_consumers(decisions: Sequence[object]) -> dict[str, tuple[str, ...]]:
        from .discovery import DiscoveryDecision

        grouped: dict[str, list[str]] = {}
        for value in decisions:
            if not isinstance(value, DiscoveryDecision):
                raise TypeError("discovery consumer grouping requires typed decisions")
            request = value.task.request
            if request is not None:
                grouped.setdefault(request.key, []).append(value.task.key)
        return {key: tuple(sorted(values)) for key, values in grouped.items()}

    """Single-generation SQLite ledger with explicit durable transitions."""

    def __init__(self, path: Path, connection: sqlite3.Connection, corpus_repo_root: Path | None = None) -> None:
        self.path = path
        self._connection = connection
        self._fault: str | None = None
        self._manifest_probe: Callable[[], None] | None = None
        self.__authority_write_depth = 0
        self._corpus_repo_root = corpus_repo_root.resolve() if corpus_repo_root is not None else None
        self.__trusted_corpus_cache: tuple[str, object] | None = None
        connection.create_function(
            "citeforge_authority_write_enabled",
            0,
            lambda: int(self.__authority_write_depth > 0),
        )
        connection.set_authorizer(self._authorize_sqlite)

    def _authorize_sqlite(
        self,
        action_code: int,
        table_name: str | None,
        _column_name: str | None,
        _database_name: str | None,
        _trigger_name: str | None,
    ) -> int:
        if (
            action_code == sqlite3.SQLITE_INSERT
            and table_name in _V6_AUTHORITY_TABLES
            and self.__authority_write_depth == 0
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    @classmethod
    def open(cls, path: Path, *, corpus_repo_root: Path | None = None) -> Ledger:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=30, isolation_level=None, cached_statements=0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        journal_mode = str(connection.execute("PRAGMA journal_mode = DELETE").fetchone()[0]).lower()
        if journal_mode != "delete":
            connection.close()
            raise ValueError(f"unsupported SQLite journal mode: {journal_mode}")
        ledger = cls(path, connection, corpus_repo_root)
        try:
            ledger._initialize_schema()
            if connection.execute("SELECT COUNT(*) FROM corpus_scan_receipts").fetchone()[0]:
                if corpus_repo_root is None:
                    raise ValueError("scanner-owned corpus reopen requires a trusted Git repository root")
                ledger._verify_trusted_corpus()
        except Exception:
            connection.close()
            raise
        return ledger

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self._connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self._connection
        except BaseException:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @contextmanager
    def _authority_write(self) -> Iterator[None]:
        self.__authority_write_depth += 1
        try:
            yield
        finally:
            self.__authority_write_depth -= 1

    @staticmethod
    def _schema_v6_statements() -> tuple[str, ...]:
        tables = (
            "CREATE TABLE corpus_snapshots (generation_id TEXT NOT NULL, snapshot_digest TEXT NOT NULL, "
            "base_commit TEXT NOT NULL, output_tree_digest TEXT NOT NULL, baseline_digest TEXT NOT NULL, "
            "scanner_id TEXT NOT NULL, scanner_version TEXT NOT NULL, parser_id TEXT NOT NULL, "
            "parser_version TEXT NOT NULL, item_set_digest TEXT NOT NULL, derived_a2i2_digest TEXT, "
            "evidence_json TEXT NOT NULL, PRIMARY KEY (generation_id, snapshot_digest), "
            "UNIQUE (generation_id), FOREIGN KEY (generation_id) REFERENCES generations(generation_id))",
            "CREATE TABLE corpus_items (generation_id TEXT NOT NULL, snapshot_digest TEXT NOT NULL, "
            "source_path TEXT NOT NULL COLLATE NOCASE, author_key TEXT NOT NULL, before_digest TEXT NOT NULL, "
            "parse_digest TEXT NOT NULL, publication_keys_json TEXT NOT NULL, disposition TEXT NOT NULL, "
            "exact_identifiers_json TEXT NOT NULL, evidence_digest TEXT NOT NULL, evidence_json TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, source_path), "
            "UNIQUE (generation_id, evidence_digest), FOREIGN KEY (generation_id, snapshot_digest) "
            "REFERENCES corpus_snapshots(generation_id, snapshot_digest), FOREIGN KEY (generation_id, author_key) "
            "REFERENCES authors(generation_id, row_key))",
            "CREATE TABLE publication_seed_evidence (generation_id TEXT NOT NULL, author_key TEXT NOT NULL, "
            "publication_key TEXT NOT NULL, origin_kind TEXT NOT NULL, origin_evidence_key TEXT NOT NULL, "
            "origin_evidence_digest TEXT NOT NULL, baseline_digest TEXT, exact_identifiers_json TEXT NOT NULL, "
            "seed_digest TEXT NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY (generation_id, author_key, "
            "publication_key), UNIQUE (generation_id, origin_kind, origin_evidence_key, publication_key), "
            "FOREIGN KEY (generation_id, author_key) REFERENCES authors(generation_id, row_key))",
            "CREATE TABLE planner_passes (generation_id TEXT NOT NULL, pass_key TEXT NOT NULL, pass_id TEXT NOT NULL, "
            "pass_version TEXT NOT NULL, registry_digest TEXT NOT NULL, snapshot_digest TEXT NOT NULL, "
            "output_digest TEXT NOT NULL, receipt_json TEXT NOT NULL, snapshot_authority_digest TEXT NOT NULL, "
            "predecessor_output_digest TEXT, PRIMARY KEY (generation_id, pass_key), "
            "UNIQUE (generation_id, pass_id), FOREIGN KEY (generation_id) REFERENCES generations(generation_id))",
            "CREATE TABLE planner_pass_expected_items (generation_id TEXT NOT NULL, pass_key TEXT NOT NULL, "
            "item_key TEXT NOT NULL, kind TEXT NOT NULL, source_digest TEXT NOT NULL, input_json TEXT NOT NULL, "
            "unseen INTEGER NOT NULL CHECK(unseen IN (0, 1)), PRIMARY KEY (generation_id, "
            "pass_key, item_key), FOREIGN KEY (generation_id, pass_key) REFERENCES planner_passes(generation_id, "
            "pass_key))",
            "CREATE TABLE aggregate_inputs (generation_id TEXT NOT NULL, pass_key TEXT NOT NULL, "
            "reduction_id TEXT NOT NULL, kind TEXT NOT NULL, stable_key TEXT NOT NULL, source_digest TEXT NOT NULL, "
            "ordinal INTEGER NOT NULL CHECK(ordinal >= 0), input_digest TEXT NOT NULL, input_json TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, pass_key, reduction_id, kind, stable_key), UNIQUE (generation_id, "
            "pass_key, reduction_id, ordinal), FOREIGN KEY (generation_id, pass_key) REFERENCES "
            "planner_passes(generation_id, pass_key))",
            "CREATE TABLE provenance_decisions (generation_id TEXT NOT NULL, decision_key TEXT NOT NULL, "
            "pass_key TEXT NOT NULL, author_key TEXT NOT NULL, publication_key TEXT NOT NULL, "
            "field_name TEXT NOT NULL, "
            "selected_value_digest TEXT NOT NULL, rule TEXT NOT NULL, contribution_set_digest TEXT NOT NULL, "
            "reducer_id TEXT NOT NULL, reducer_version TEXT NOT NULL, evidence_json TEXT NOT NULL, PRIMARY KEY "
            "(generation_id, decision_key), UNIQUE (generation_id, pass_key, author_key, publication_key, field_name), "
            "FOREIGN KEY (generation_id, pass_key) REFERENCES planner_passes(generation_id, pass_key), FOREIGN KEY "
            "(generation_id, author_key, publication_key) REFERENCES publications(generation_id, author_key, "
            "publication_key))",
            "CREATE TABLE provenance_contributions (generation_id TEXT NOT NULL, contribution_key TEXT NOT NULL, "
            "decision_key TEXT NOT NULL, source_kind TEXT NOT NULL, provider TEXT, schema_version TEXT, "
            "request_key TEXT, "
            "observation_digest TEXT NOT NULL, value_digest TEXT, selected INTEGER NOT NULL CHECK(selected IN (0, 1)), "
            "rejection_reason TEXT NOT NULL, evidence_json TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, contribution_key), "
            "FOREIGN KEY (generation_id, decision_key) REFERENCES provenance_decisions(generation_id, decision_key), "
            "FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key))",
            "CREATE TABLE materialization_intents (generation_id TEXT NOT NULL, intent_key TEXT NOT NULL, "
            "pass_key TEXT NOT NULL, author_key TEXT NOT NULL, publication_key TEXT NOT NULL, "
            "source_path TEXT NOT NULL COLLATE NOCASE, "
            "target_path TEXT NOT NULL COLLATE NOCASE, kind TEXT NOT NULL CHECK(kind IN ('keep', 'upsert', 'remove')), "
            "before_digest TEXT, after_digest TEXT, reducer_id TEXT NOT NULL, reducer_version TEXT NOT NULL, "
            "provenance_set_digest TEXT NOT NULL, evidence_json TEXT NOT NULL, "
            "final_fields_json TEXT NOT NULL, final_content_digest TEXT, removal_reason TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, intent_key), "
            "UNIQUE (generation_id, target_path), UNIQUE (generation_id, author_key, publication_key), FOREIGN KEY "
            "(generation_id, pass_key) REFERENCES planner_passes(generation_id, pass_key), FOREIGN KEY (generation_id, "
            "author_key, publication_key) REFERENCES publications(generation_id, author_key, publication_key))",
            "CREATE TABLE intent_provenance (generation_id TEXT NOT NULL, intent_key TEXT NOT NULL, decision_key TEXT "
            "NOT NULL, PRIMARY KEY (generation_id, intent_key, decision_key), FOREIGN KEY (generation_id, intent_key) "
            "REFERENCES materialization_intents(generation_id, intent_key), FOREIGN KEY (generation_id, decision_key) "
            "REFERENCES provenance_decisions(generation_id, decision_key))",
        )
        indexes = (
            "CREATE INDEX corpus_items_author_idx ON corpus_items(generation_id, author_key)",
            "CREATE INDEX seeds_origin_idx ON publication_seed_evidence"
            "(generation_id, origin_kind, origin_evidence_key)",
            "CREATE INDEX aggregate_inputs_kind_idx ON aggregate_inputs(generation_id, pass_key, kind, ordinal)",
            "CREATE INDEX provenance_contributions_decision_idx ON provenance_contributions"
            "(generation_id, decision_key)",
            "CREATE INDEX intents_source_idx ON materialization_intents(generation_id, source_path)",
        )
        triggers: list[str] = []
        for table in (
            "corpus_snapshots",
            "corpus_items",
            "publication_seed_evidence",
            "aggregate_inputs",
            "planner_passes",
            "planner_pass_expected_items",
            "provenance_decisions",
            "provenance_contributions",
            "materialization_intents",
            "intent_provenance",
        ):
            triggers.extend(
                (
                    f"CREATE TRIGGER {table}_append_only_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, "
                    f"'{table} is append-only'); END",
                    f"CREATE TRIGGER {table}_append_only_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT, "
                    f"'{table} is append-only'); END",
                    f"CREATE TRIGGER {table}_post_close_insert BEFORE INSERT ON {table} WHEN "  # noqa: S608
                    f"(SELECT plan_closed FROM generations WHERE generation_id = NEW.generation_id) != 0 "
                    f"BEGIN SELECT RAISE(ABORT, '{table} rejects post-close evidence'); END",
                    f"CREATE TRIGGER {table}_authority_insert BEFORE INSERT ON {table} WHEN "
                    "citeforge_authority_write_enabled() != 1 BEGIN SELECT RAISE(ABORT, "
                    f"'{table} requires guarded authority API'); END",
                )
            )
        return (*tables, *indexes, *triggers)

    def _install_schema_v6(self, connection: sqlite3.Connection) -> None:
        for index, statement in enumerate(self._schema_v6_statements()):
            connection.execute(statement)
            self._inject(f"after_v6_migration_statement_{index}")
        self._inject("after_v6_migration_ddl")

    @staticmethod
    def _install_schema_v7(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE corpus_scan_receipts (generation_id TEXT PRIMARY KEY, snapshot_digest TEXT NOT NULL, "
            "receipt_digest TEXT NOT NULL, FOREIGN KEY (generation_id, snapshot_digest) "
            "REFERENCES corpus_snapshots(generation_id, snapshot_digest))"
        )
        for suffix, action in (("append_only_update", "UPDATE"), ("append_only_delete", "DELETE")):
            connection.execute(
                f"CREATE TRIGGER corpus_scan_receipts_{suffix} BEFORE {action} ON corpus_scan_receipts "
                "BEGIN SELECT RAISE(ABORT, 'corpus_scan_receipts is append-only'); END"
            )
        connection.execute(
            "CREATE TRIGGER corpus_scan_receipts_post_close_insert BEFORE INSERT ON corpus_scan_receipts WHEN "
            "(SELECT plan_closed FROM generations WHERE generation_id = NEW.generation_id) != 0 "
            "BEGIN SELECT RAISE(ABORT, 'corpus_scan_receipts rejects post-close evidence'); END"
        )
        connection.execute(
            "CREATE TRIGGER corpus_scan_receipts_authority_insert BEFORE INSERT ON corpus_scan_receipts WHEN "
            "citeforge_authority_write_enabled() != 1 BEGIN SELECT RAISE(ABORT, "
            "'corpus_scan_receipts requires guarded authority API'); END"
        )

    @staticmethod
    def _install_schema_v8(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE discovery_policy_authority (generation_id TEXT PRIMARY KEY, policy_json TEXT NOT NULL, "
            "policy_digest TEXT NOT NULL, FOREIGN KEY (generation_id) REFERENCES generations(generation_id))"
        )
        for suffix, action in (("append_only_update", "UPDATE"), ("append_only_delete", "DELETE")):
            connection.execute(
                f"CREATE TRIGGER discovery_policy_authority_{suffix} BEFORE {action} ON "
                "discovery_policy_authority BEGIN SELECT RAISE(ABORT, "
                "'discovery_policy_authority is append-only'); END"
            )
        connection.execute(
            "CREATE TRIGGER discovery_policy_authority_post_close_insert BEFORE INSERT ON "
            "discovery_policy_authority WHEN (SELECT plan_closed FROM generations WHERE generation_id = "
            "NEW.generation_id) != 0 BEGIN SELECT RAISE(ABORT, "
            "'discovery_policy_authority rejects post-close evidence'); END"
        )
        connection.execute(
            "CREATE TRIGGER discovery_policy_authority_authority_insert BEFORE INSERT ON discovery_policy_authority "
            "WHEN citeforge_authority_write_enabled() != 1 BEGIN SELECT RAISE(ABORT, "
            "'discovery_policy_authority requires guarded authority API'); END"
        )

    @staticmethod
    def _install_schema_v9(connection: sqlite3.Connection) -> None:
        marker_columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(physical_send_markers)")}
        if "resume_url" not in marker_columns:
            connection.execute("ALTER TABLE physical_send_markers ADD COLUMN resume_url TEXT")
        if "resume_url_digest" not in marker_columns:
            connection.execute("ALTER TABLE physical_send_markers ADD COLUMN resume_url_digest TEXT")
        statements = (
            "CREATE TABLE html_probe_waves (generation_id TEXT NOT NULL, parent_pass_key TEXT NOT NULL, "
            "ordinal INTEGER NOT NULL CHECK(ordinal >= 0), wave_input_digest TEXT NOT NULL, "
            "predecessor_digest TEXT NOT NULL, decision_set_digest TEXT NOT NULL, terminal INTEGER NOT NULL "
            "CHECK(terminal IN (0, 1)), round_key TEXT, receipt_digest TEXT NOT NULL, committed_at TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, parent_pass_key, ordinal), UNIQUE (generation_id, receipt_digest), "
            "FOREIGN KEY (generation_id, parent_pass_key) REFERENCES planner_passes(generation_id, pass_key))",
            "CREATE TABLE html_probe_wave_items (generation_id TEXT NOT NULL, parent_pass_key TEXT NOT NULL, "
            "ordinal INTEGER NOT NULL, author_key TEXT NOT NULL, publication_key TEXT NOT NULL, task_key TEXT, "
            "applicability TEXT NOT NULL, reason TEXT, evidence_json TEXT NOT NULL, item_digest TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, parent_pass_key, ordinal, author_key, publication_key), "
            "UNIQUE (generation_id, parent_pass_key, ordinal, item_digest), "
            "FOREIGN KEY (generation_id, parent_pass_key, ordinal) REFERENCES "
            "html_probe_waves(generation_id, parent_pass_key, ordinal), FOREIGN KEY (generation_id, author_key, "
            "publication_key) REFERENCES publications(generation_id, author_key, publication_key), "
            "FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key))",
            "CREATE INDEX html_probe_wave_items_task_idx ON html_probe_wave_items(generation_id, task_key)",
            "CREATE TABLE html_probe_terminal_receipts (generation_id TEXT NOT NULL, parent_pass_key TEXT NOT NULL, "
            "completed_after_ordinal INTEGER CHECK(completed_after_ordinal IS NULL OR completed_after_ordinal >= 0), "
            "reason TEXT NOT NULL, evidence_digest TEXT NOT NULL, committed_at TEXT NOT NULL, "
            "PRIMARY KEY (generation_id, parent_pass_key), UNIQUE (generation_id, evidence_digest), "
            "FOREIGN KEY (generation_id, parent_pass_key) REFERENCES planner_passes(generation_id, pass_key))",
        )
        for statement in statements:
            connection.execute(statement)
        for table in ("html_probe_waves", "html_probe_wave_items", "html_probe_terminal_receipts"):
            for suffix, action in (("append_only_update", "UPDATE"), ("append_only_delete", "DELETE")):
                connection.execute(
                    f"CREATE TRIGGER {table}_{suffix} BEFORE {action} ON {table} "
                    f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END"
                )
            connection.execute(
                f"CREATE TRIGGER {table}_post_close_insert BEFORE INSERT ON {table} WHEN "  # noqa: S608
                "(SELECT plan_closed FROM generations WHERE generation_id = NEW.generation_id) != 0 "
                f"BEGIN SELECT RAISE(ABORT, '{table} rejects post-close evidence'); END"
            )
            connection.execute(
                f"CREATE TRIGGER {table}_authority_insert BEFORE INSERT ON {table} WHEN "
                "citeforge_authority_write_enabled() != 1 BEGIN SELECT RAISE(ABORT, "
                f"'{table} requires guarded authority API'); END"
            )

    def _initialize_schema(self) -> None:
        with self._transaction(immediate=True) as connection:
            user_objects = connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            names = {str(row[0]) for row in user_objects}
            if user_objects and connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("refusing to migrate database with foreign-key violations")
            if user_objects and "schema_meta" not in names:
                raise ValueError("refusing to initialize nonempty database without schema metadata")
            if not user_objects:
                connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                existing_version = None
            else:
                existing_version = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if existing_version is None:
                    raise ValueError("refusing to initialize nonempty database without schema version")
            if existing_version is not None and existing_version[0] == "4":
                stored = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_fingerprint'").fetchone()
                if (
                    stored is None
                    or stored[0] != _SCHEMA_V4_FINGERPRINT
                    or self._schema_fingerprint(connection) != _SCHEMA_V4_FINGERPRINT
                ):
                    raise ValueError("refusing to migrate structurally inconsistent schema version 4")
                connection.execute(
                    # resume_url and resume_url_digest belong to v9 and are added by
                    # _install_schema_v9. A historical migration reproduces its own
                    # historical schema, or the v5 fingerprint it is checked against
                    # can never match.
                    "CREATE TABLE physical_send_markers (generation_id TEXT NOT NULL, request_key TEXT NOT NULL, "
                    "owner TEXT NOT NULL, started_at TEXT NOT NULL, idempotent INTEGER NOT NULL "
                    "CHECK(idempotent IN (0, 1)), resolved_at TEXT, "
                    "PRIMARY KEY (generation_id, request_key), "
                    "FOREIGN KEY (generation_id, request_key) "
                    "REFERENCES requests(generation_id, request_key))"
                )
                connection.execute("UPDATE schema_meta SET value = '5' WHERE key = 'schema_version'")
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_fingerprint'",
                    (_SCHEMA_V5_FINGERPRINT,),
                )
                existing_version = ("5",)
            if existing_version is not None and existing_version[0] == "5":
                self._validate_schema_v5(connection)
                self._install_schema_v6(connection)
                actual = self._schema_fingerprint(connection)
                expected = _SCHEMA_V6_FINGERPRINT
                if actual != expected:
                    raise ValueError("structurally inconsistent schema version 6 fingerprint")
                connection.execute("UPDATE schema_meta SET value = '6' WHERE key = 'schema_version'")
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_fingerprint'",
                    (expected,),
                )
                self._inject("after_v6_migration_meta")
                existing_version = ("6",)
            if existing_version is not None and existing_version[0] == "6":
                self._validate_schema_v6(connection)
                if connection.execute("SELECT COUNT(*) FROM corpus_snapshots").fetchone()[0]:
                    raise ValueError("schema version 6 corpus lacks scanner-owned receipt authority")
                self._install_schema_v7(connection)
                actual = self._schema_fingerprint(connection)
                expected = _SCHEMA_V7_FINGERPRINT
                if actual != expected:
                    raise ValueError("structurally inconsistent schema version 7 fingerprint")
                connection.execute("UPDATE schema_meta SET value = '7' WHERE key = 'schema_version'")
                connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_fingerprint'", (expected,))
                existing_version = ("7",)
            if existing_version is not None and existing_version[0] == "7":
                self._validate_schema_v7(connection)
                self._install_schema_v8(connection)
                actual = self._schema_fingerprint(connection)
                expected = _SCHEMA_V8_FINGERPRINT
                if actual != expected:
                    raise ValueError("structurally inconsistent schema version 8 fingerprint")
                connection.execute("UPDATE schema_meta SET value = '8' WHERE key = 'schema_version'")
                connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_fingerprint'", (expected,))
                existing_version = ("8",)
            if existing_version is not None and existing_version[0] == "8":
                self._validate_schema_v8(connection)
                self._install_schema_v9(connection)
                actual = self._schema_fingerprint(connection)
                expected = _EXPECTED_SCHEMA_FINGERPRINT
                if actual != expected:
                    raise ValueError("structurally inconsistent schema version 9 fingerprint")
                connection.execute("UPDATE schema_meta SET value = '9' WHERE key = 'schema_version'")
                connection.execute("UPDATE schema_meta SET value = ? WHERE key = 'schema_fingerprint'", (expected,))
                existing_version = ("9",)
            if existing_version is not None and existing_version[0] != _SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported or structurally inconsistent ledger schema version: {existing_version[0]}"
                )
            if existing_version is not None:
                self._validate_schema_v9(connection)
                return
            schema = """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS generations (
                    generation_id TEXT PRIMARY KEY,
                    identity_json TEXT NOT NULL,
                    census_digest TEXT NOT NULL,
                    authors_digest TEXT NOT NULL,
                    base_commit TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    adapter_digest TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT,
                    published_at TEXT,
                    checkpoint_sequence INTEGER NOT NULL DEFAULT 0,
                    blocking_reason TEXT NOT NULL DEFAULT '',
                    plan_sealed INTEGER NOT NULL DEFAULT 0 CHECK(plan_sealed IN (0, 1)),
                    plan_digest TEXT,
                    completed_manifest_digest TEXT,
                    inventory_freshness_epoch TEXT
                    ,plan_closed INTEGER NOT NULL DEFAULT 0 CHECK(plan_closed IN (0, 1))
                    ,plan_revision INTEGER NOT NULL DEFAULT 0
                    ,closure_digest TEXT
                    ,discovery_closed INTEGER NOT NULL DEFAULT 0 CHECK(discovery_closed IN (0, 1))
                    ,plan_authority_mode TEXT NOT NULL DEFAULT 'phased'
                );
                CREATE TABLE IF NOT EXISTS authors (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    row_key TEXT NOT NULL,
                    physical_row INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    scholar_id TEXT NOT NULL,
                    dblp_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                    exclusion_reason TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    PRIMARY KEY (generation_id, row_key)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    request_key TEXT NOT NULL,
                    identity_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    next_attempt_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    response_digest TEXT,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    task_key TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    request_key TEXT,
                    identity_digest TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK(required IN (0, 1)),
                    applicability TEXT NOT NULL,
                    applicability_reason TEXT NOT NULL DEFAULT '',
                    dominance_reason TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_error_class TEXT,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    next_attempt_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    PRIMARY KEY (generation_id, task_key),
                    UNIQUE (generation_id, identity_digest),
                    FOREIGN KEY (generation_id, author_key) REFERENCES authors(generation_id, row_key),
                    FOREIGN KEY (generation_id, author_key, publication_key)
                        REFERENCES publications(generation_id, author_key, publication_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS request_consumers (
                    generation_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    PRIMARY KEY (generation_id, request_key, task_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    generation_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    http_status INTEGER,
                    retry_delay_seconds REAL,
                    response_digest TEXT,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, request_key, attempt_number),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS physical_send_markers (generation_id TEXT NOT NULL,
                    request_key TEXT NOT NULL, owner TEXT NOT NULL, started_at TEXT NOT NULL,
                    idempotent INTEGER NOT NULL CHECK(idempotent IN (0, 1)), resolved_at TEXT,
                    resume_url TEXT, resume_url_digest TEXT,
                    PRIMARY KEY (generation_id, request_key), FOREIGN KEY (generation_id, request_key)
                    REFERENCES requests(generation_id, request_key));
                CREATE TABLE IF NOT EXISTS observations (
                    generation_id TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    response_json TEXT,
                    response_digest TEXT,
                    provider TEXT NOT NULL,
                    schema_version TEXT NOT NULL,
                    authoritative_empty INTEGER NOT NULL CHECK(authoritative_empty IN (0, 1)),
                    observed_at TEXT NOT NULL,
                    safe_diagnostic TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, request_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    sequence INTEGER NOT NULL,
                    ciphertext_digest TEXT NOT NULL,
                    key_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS publications (
                    generation_id TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT NOT NULL,
                    discovery_source TEXT NOT NULL DEFAULT '',
                    normalized_title TEXT NOT NULL DEFAULT '',
                    year INTEGER,
                    exact_identifiers_json TEXT NOT NULL DEFAULT '{}',
                    baseline_output_path TEXT NOT NULL DEFAULT '',
                    freshness_policy TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (generation_id, author_key, publication_key),
                    FOREIGN KEY (generation_id, author_key) REFERENCES authors(generation_id, row_key)
                );
                CREATE TABLE IF NOT EXISTS publication_evidence (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    kind TEXT NOT NULL,
                    commit_sha TEXT NOT NULL,
                    candidate_digest TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, kind, commit_sha)
                );
                CREATE TABLE IF NOT EXISTS manifests (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    digest TEXT NOT NULL,
                    canonical_json TEXT NOT NULL,
                    PRIMARY KEY (generation_id, digest)
                );
                CREATE TABLE IF NOT EXISTS plan_obligations (
                    generation_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    identity_digest TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK(required IN (0, 1)),
                    applicability TEXT NOT NULL,
                    round_sequence INTEGER,
                    expands_plan INTEGER NOT NULL DEFAULT 0 CHECK(expands_plan IN (0, 1)),
                    PRIMARY KEY (generation_id, task_key),
                    UNIQUE (generation_id, identity_digest),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key)
                );
                CREATE TABLE IF NOT EXISTS plan_rounds (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    sequence INTEGER NOT NULL CHECK(sequence > 0),
                    round_key TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    planner_id TEXT NOT NULL,
                    planner_version TEXT NOT NULL,
                    source_task_keys_json TEXT NOT NULL,
                    source_evidence_digest TEXT NOT NULL,
                    task_set_digest TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, sequence),
                    UNIQUE (generation_id, round_key)
                );
                CREATE TABLE IF NOT EXISTS reduction_receipts (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    reduction_digest TEXT NOT NULL,
                    round_key TEXT NOT NULL,
                    source_task_keys_json TEXT NOT NULL,
                    source_dispositions_json TEXT NOT NULL,
                    source_evidence_digests_json TEXT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (generation_id, reduction_digest),
                    UNIQUE (generation_id, round_key),
                    FOREIGN KEY (generation_id, round_key) REFERENCES plan_rounds(generation_id, round_key)
                );
                CREATE TABLE IF NOT EXISTS round_publications (
                    generation_id TEXT NOT NULL,
                    round_sequence INTEGER NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT NOT NULL,
                    PRIMARY KEY (generation_id, round_sequence, author_key, publication_key),
                    FOREIGN KEY (generation_id, round_sequence)
                        REFERENCES plan_rounds(generation_id, sequence),
                    FOREIGN KEY (generation_id, author_key, publication_key)
                        REFERENCES publications(generation_id, author_key, publication_key)
                );
                CREATE TABLE IF NOT EXISTS reduction_sources (
                    generation_id TEXT NOT NULL,
                    source_task_key TEXT NOT NULL,
                    reduction_digest TEXT NOT NULL,
                    PRIMARY KEY (generation_id, source_task_key),
                    FOREIGN KEY (generation_id, source_task_key) REFERENCES tasks(generation_id, task_key),
                    FOREIGN KEY (generation_id, reduction_digest)
                        REFERENCES reduction_receipts(generation_id, reduction_digest)
                );
                CREATE TABLE IF NOT EXISTS inventory_authorities (
                    generation_id TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    reducer_version TEXT NOT NULL,
                    policy_digest TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    reduction_digest TEXT NOT NULL,
                    round_key TEXT NOT NULL,
                    PRIMARY KEY (generation_id, author_key, reducer_version),
                    UNIQUE (generation_id, reduction_digest),
                    FOREIGN KEY (generation_id, author_key) REFERENCES authors(generation_id, row_key),
                    FOREIGN KEY (generation_id, round_key) REFERENCES plan_rounds(generation_id, round_key)
                );
                CREATE TABLE IF NOT EXISTS inventory_policy_authority (
                    generation_id TEXT PRIMARY KEY REFERENCES generations(generation_id),
                    authority_json TEXT NOT NULL,
                    authority_digest TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS inventory_contributions (
                    generation_id TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    reducer_version TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    capability_id TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    decoder_schema TEXT NOT NULL,
                    observation_digest TEXT NOT NULL,
                    page_offset INTEGER,
                    next_offset INTEGER,
                    topology_digest TEXT NOT NULL,
                    PRIMARY KEY (generation_id, author_key, reducer_version, task_key),
                    FOREIGN KEY (generation_id, author_key, reducer_version)
                        REFERENCES inventory_authorities(generation_id, author_key, reducer_version),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS validation_obligations (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    check_name TEXT NOT NULL,
                    required INTEGER NOT NULL CHECK(required IN (0, 1)),
                    PRIMARY KEY (generation_id, check_name)
                );
                CREATE TABLE IF NOT EXISTS field_provenance (
                    generation_id TEXT NOT NULL,
                    author_key TEXT NOT NULL,
                    publication_key TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    selected_value_digest TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    request_key TEXT NOT NULL,
                    decision_rule TEXT NOT NULL,
                    PRIMARY KEY (generation_id, author_key, publication_key, field_name),
                    FOREIGN KEY (generation_id, author_key, publication_key)
                        REFERENCES publications(generation_id, author_key, publication_key),
                    FOREIGN KEY (generation_id, request_key) REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS dominance_evidence (
                    generation_id TEXT NOT NULL,
                    task_key TEXT NOT NULL,
                    stronger_observations_json TEXT NOT NULL,
                    dominated_observation_key TEXT NOT NULL,
                    rule TEXT NOT NULL,
                    covered_fields_json TEXT NOT NULL,
                    PRIMARY KEY (generation_id, task_key),
                    FOREIGN KEY (generation_id, task_key) REFERENCES tasks(generation_id, task_key),
                    FOREIGN KEY (generation_id, dominated_observation_key)
                        REFERENCES requests(generation_id, request_key)
                );
                CREATE TABLE IF NOT EXISTS provider_state (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    provider TEXT NOT NULL,
                    quota_pool TEXT NOT NULL,
                    current_concurrency INTEGER NOT NULL,
                    rate_limit_deadline TEXT,
                    circuit_state TEXT NOT NULL,
                    success_count INTEGER NOT NULL,
                    failure_count INTEGER NOT NULL,
                    async_job_id TEXT,
                    request_digest TEXT,
                    PRIMARY KEY (generation_id, provider, quota_pool)
                );
                CREATE TABLE IF NOT EXISTS materializations (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    staged_path TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    corpus_counts_json TEXT NOT NULL,
                    validation_state TEXT NOT NULL,
                    PRIMARY KEY (generation_id, staged_path)
                );
                CREATE TABLE IF NOT EXISTS validations (
                    generation_id TEXT NOT NULL REFERENCES generations(generation_id),
                    check_name TEXT NOT NULL,
                    state TEXT NOT NULL,
                    evidence_digest TEXT NOT NULL,
                    safe_detail TEXT NOT NULL,
                    PRIMARY KEY (generation_id, check_name)
                );
                """
            for statement in schema.split(";"):
                if statement.strip():
                    connection.execute(statement)
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS attempts_no_update BEFORE UPDATE ON attempts "
                "BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END"
            )
            for table in ("plan_obligations", "validation_obligations"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'sealed planning obligations are append-only'); END"
                    )
                for operation in ("INSERT",):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_sealed_{operation.lower()} BEFORE {operation} "  # noqa: S608
                        f"ON {table} WHEN (SELECT plan_sealed FROM generations WHERE generation_id = "
                        f"{'OLD' if operation != 'INSERT' else 'NEW'}.generation_id) = 1 "
                        "BEGIN SELECT RAISE(ABORT, 'sealed obligations are immutable'); END"
                    )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS tasks_append_only_identity_update BEFORE UPDATE OF task_key, author_key, "
                "publication_key, provider, operation, request_key, identity_digest, required, applicability ON tasks "
                "BEGIN SELECT RAISE(ABORT, 'sealed task identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS tasks_append_only_delete BEFORE DELETE ON tasks "
                "BEGIN SELECT RAISE(ABORT, 'sealed task identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS tasks_sealed_insert BEFORE INSERT ON tasks WHEN "
                "(SELECT plan_sealed FROM generations WHERE generation_id = NEW.generation_id) = 1 "
                "BEGIN SELECT RAISE(ABORT, 'sealed task identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS requests_append_only_identity_update BEFORE UPDATE OF request_key, "
                "identity_json ON requests BEGIN SELECT RAISE(ABORT, 'sealed request identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS requests_append_only_delete BEFORE DELETE ON requests "
                "BEGIN SELECT RAISE(ABORT, 'sealed request identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS requests_sealed_insert BEFORE INSERT ON requests WHEN "
                "(SELECT plan_sealed FROM generations WHERE generation_id = NEW.generation_id) = 1 "
                "BEGIN SELECT RAISE(ABORT, 'sealed request identity is immutable'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON attempts "
                "BEGIN SELECT RAISE(ABORT, 'attempts are append-only'); END"
            )
            for table in ("observations", "dominance_evidence"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'terminal evidence is append-only'); END"
                    )
            for table in (
                "plan_rounds",
                "reduction_receipts",
                "reduction_sources",
                "round_publications",
                "inventory_authorities",
                "inventory_contributions",
                "inventory_policy_authority",
            ):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'phased planning evidence is append-only'); END"
                    )
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS {table}_closed_insert BEFORE INSERT ON {table} "  # noqa: S608
                    "WHEN (SELECT plan_closed FROM generations WHERE generation_id = NEW.generation_id) = 1 "
                    "BEGIN SELECT RAISE(ABORT, 'closed plan rejects planning insert'); END"
                )
            for table in ("publications", "request_consumers"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_append_only_{operation.lower()} BEFORE {operation} "
                        f"ON {table} BEGIN SELECT RAISE(ABORT, 'planning identity is append-only'); END"
                    )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS publications_closed_insert BEFORE INSERT ON publications "
                "WHEN (SELECT plan_closed FROM generations WHERE generation_id = NEW.generation_id) = 1 "
                "BEGIN SELECT RAISE(ABORT, 'closed plan rejects publication identity insert'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS generations_task5a_authority_update "
                "BEFORE UPDATE OF discovery_closed, plan_authority_mode ON generations "
                "WHEN NEW.discovery_closed != 0 OR NEW.plan_authority_mode = 'phased_authoritative' "
                "BEGIN SELECT RAISE(ABORT, 'Task 5A cannot establish discovery authority'); END"
            )
            connection.execute(
                "CREATE TRIGGER IF NOT EXISTS generations_task5a_authority_insert BEFORE INSERT ON generations "
                "WHEN NEW.discovery_closed != 0 OR NEW.plan_authority_mode = 'phased_authoritative' "
                "BEGIN SELECT RAISE(ABORT, 'Task 5A cannot establish discovery authority'); END"
            )
            if existing_version is None:
                self._install_schema_v6(connection)
                self._install_schema_v7(connection)
                self._install_schema_v8(connection)
                self._install_schema_v9(connection)
                actual = self._schema_fingerprint(connection)
                expected = _EXPECTED_SCHEMA_FINGERPRINT
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?)", (_SCHEMA_VERSION,)
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES ('schema_fingerprint', ?)",
                    (expected,),
                )
            self._validate_schema_v9(connection)

    @staticmethod
    def _schema_fingerprint(connection: sqlite3.Connection) -> str:
        objects = [
            {
                "name": row[1],
                "sql": " ".join(str(row[3]).split()) if row[3] is not None else None,
                "table": row[2],
                "type": row[0],
            }
            for row in connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
            )
        ]
        return _digest(objects)

    @staticmethod
    def _validate_schema_v5(connection: sqlite3.Connection) -> None:
        required_columns = {
            "generations": {
                "plan_closed",
                "plan_revision",
                "closure_digest",
                "discovery_closed",
                "plan_authority_mode",
            },
            "plan_obligations": {"round_sequence", "expands_plan"},
            "plan_rounds": {
                "sequence",
                "round_key",
                "phase",
                "planner_id",
                "planner_version",
                "source_task_keys_json",
                "source_evidence_digest",
                "task_set_digest",
                "content_digest",
            },
            "reduction_receipts": {
                "reduction_digest",
                "round_key",
                "source_task_keys_json",
                "source_dispositions_json",
                "source_evidence_digests_json",
            },
            "reduction_sources": {"source_task_key", "reduction_digest"},
            "round_publications": {"round_sequence", "author_key", "publication_key"},
            "inventory_authorities": {
                "author_key",
                "reducer_version",
                "policy_digest",
                "snapshot_digest",
                "reduction_digest",
                "round_key",
            },
            "inventory_contributions": {
                "author_key",
                "reducer_version",
                "task_key",
                "request_key",
                "capability_id",
                "disposition",
                "decoder_schema",
                "observation_digest",
                "page_offset",
                "next_offset",
                "topology_digest",
            },
            "inventory_policy_authority": {"authority_json", "authority_digest"},
            "physical_send_markers": {
                "request_key",
                "owner",
                "started_at",
                "idempotent",
                "resolved_at",
            },
        }
        for table, required in required_columns.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                raise ValueError(f"structurally inconsistent schema version 5 table: {table}")
        fingerprint = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_fingerprint'").fetchone()
        actual = Ledger._schema_fingerprint(connection)
        if fingerprint is None or fingerprint[0] != _SCHEMA_V5_FINGERPRINT or actual != _SCHEMA_V5_FINGERPRINT:
            raise ValueError("structurally inconsistent schema version 5 fingerprint")
        Ledger._assert_task5a_authority_invariant(connection)

    @staticmethod
    def _validate_schema_v6(connection: sqlite3.Connection) -> None:
        required_columns = {
            "corpus_snapshots": {"snapshot_digest", "item_set_digest", "evidence_json"},
            "corpus_items": {"source_path", "author_key", "evidence_digest", "evidence_json"},
            "publication_seed_evidence": {"publication_key", "origin_evidence_digest", "seed_digest"},
            "planner_passes": {
                "pass_key",
                "pass_id",
                "registry_digest",
                "snapshot_digest",
                "output_digest",
                "snapshot_authority_digest",
                "predecessor_output_digest",
            },
            "planner_pass_expected_items": {"pass_key", "item_key", "kind", "source_digest", "input_json", "unseen"},
            "aggregate_inputs": {"reduction_id", "kind", "stable_key", "source_digest", "ordinal"},
            "provenance_decisions": {"decision_key", "contribution_set_digest", "evidence_json"},
            "provenance_contributions": {"contribution_key", "decision_key", "observation_digest"},
            "materialization_intents": {
                "intent_key",
                "source_path",
                "target_path",
                "kind",
                "final_fields_json",
                "final_content_digest",
                "removal_reason",
            },
            "intent_provenance": {"intent_key", "decision_key"},
        }
        for table, required in required_columns.items():
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if not required <= columns:
                raise ValueError(f"structurally inconsistent schema version 6 table: {table}")
            triggers = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
                )
            }
            expected_triggers = {
                f"{table}_append_only_update",
                f"{table}_append_only_delete",
                f"{table}_post_close_insert",
                f"{table}_authority_insert",
            }
            if not expected_triggers <= triggers:
                raise ValueError(f"structurally inconsistent schema version 6 triggers: {table}")
        fingerprint = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_fingerprint'").fetchone()
        actual = Ledger._schema_fingerprint(connection)
        if fingerprint is None or fingerprint[0] != _SCHEMA_V6_FINGERPRINT or actual != _SCHEMA_V6_FINGERPRINT:
            raise ValueError("structurally inconsistent schema version 6 fingerprint")
        Ledger._assert_task5a_authority_invariant(connection)
        for row in connection.execute("SELECT generation_id FROM generations ORDER BY generation_id"):
            generation_id = str(row[0])
            Ledger._v6_evidence_content(connection, generation_id)
            Ledger._verify_v6_relationships(connection, generation_id)

    @staticmethod
    def _validate_schema_v7(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(corpus_scan_receipts)")}
        if {"generation_id", "snapshot_digest", "receipt_digest"} - columns:
            raise ValueError("structurally inconsistent schema version 7 receipt table")
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'corpus_scan_receipts'"
            )
        }
        if len(triggers) != 4:
            raise ValueError("structurally inconsistent schema version 7 receipt triggers")
        fingerprint = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_fingerprint'").fetchone()
        actual = Ledger._schema_fingerprint(connection)
        expected = _SCHEMA_V7_FINGERPRINT
        if fingerprint is None or fingerprint[0] != expected or actual != expected:
            raise ValueError("structurally inconsistent schema version 7 fingerprint")
        Ledger._assert_task5a_authority_invariant(connection)
        for row in connection.execute("SELECT generation_id FROM generations ORDER BY generation_id"):
            generation_id = str(row[0])
            Ledger._v6_evidence_content(connection, generation_id)
            Ledger._verify_v6_relationships(connection, generation_id)

    @staticmethod
    def _validate_schema_v8(connection: sqlite3.Connection) -> None:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(discovery_policy_authority)")}
        if {"generation_id", "policy_json", "policy_digest"} - columns:
            raise ValueError("structurally inconsistent schema version 8 policy table")
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = 'discovery_policy_authority'"
            )
        }
        if len(triggers) != 4:
            raise ValueError("structurally inconsistent schema version 8 policy triggers")
        fingerprint = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_fingerprint'").fetchone()
        actual = Ledger._schema_fingerprint(connection)
        expected = _SCHEMA_V8_FINGERPRINT
        if fingerprint is None or fingerprint[0] != expected or actual != expected:
            raise ValueError("structurally inconsistent schema version 8 fingerprint")
        Ledger._assert_task5a_authority_invariant(connection)
        for row in connection.execute("SELECT generation_id FROM generations ORDER BY generation_id"):
            generation_id = str(row[0])
            Ledger._v6_evidence_content(connection, generation_id)
            Ledger._verify_v6_relationships(connection, generation_id)

    @staticmethod
    def _validate_schema_v9(connection: sqlite3.Connection) -> None:
        required = {
            "html_probe_waves": {
                "parent_pass_key",
                "ordinal",
                "wave_input_digest",
                "predecessor_digest",
                "decision_set_digest",
                "terminal",
                "round_key",
                "receipt_digest",
                "committed_at",
            },
            "html_probe_wave_items": {
                "parent_pass_key",
                "ordinal",
                "author_key",
                "publication_key",
                "task_key",
                "applicability",
                "reason",
                "evidence_json",
                "item_digest",
            },
            "html_probe_terminal_receipts": {
                "parent_pass_key",
                "completed_after_ordinal",
                "reason",
                "evidence_digest",
                "committed_at",
            },
        }
        for table, columns in required.items():
            actual_columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
            if columns - actual_columns:
                raise ValueError(f"structurally inconsistent schema version 9 table: {table}")
            triggers = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
                )
            }
            if len(triggers) != 4:
                raise ValueError(f"structurally inconsistent schema version 9 triggers: {table}")
        fingerprint = connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_fingerprint'").fetchone()
        actual = Ledger._schema_fingerprint(connection)
        if (
            fingerprint is None
            or fingerprint[0] != _EXPECTED_SCHEMA_FINGERPRINT
            or actual != _EXPECTED_SCHEMA_FINGERPRINT
        ):
            raise ValueError("structurally inconsistent schema version 9 fingerprint")
        Ledger._assert_task5a_authority_invariant(connection)
        for row in connection.execute("SELECT generation_id FROM generations ORDER BY generation_id"):
            generation_id = str(row[0])
            Ledger._v6_evidence_content(connection, generation_id)
            Ledger._verify_v6_relationships(connection, generation_id)

    @staticmethod
    def _assert_task5a_authority_invariant(connection: sqlite3.Connection) -> None:
        invalid = connection.execute(
            "SELECT COUNT(*) FROM generations WHERE discovery_closed != 0 "
            "OR plan_authority_mode = 'phased_authoritative'"
        ).fetchone()[0]
        if invalid:
            raise ValueError("Task 5A authority invariant violated")

    def _generation_id(self) -> str:
        rows = self._connection.execute("SELECT generation_id FROM generations").fetchall()
        if len(rows) != 1:
            raise ValueError("ledger must contain exactly one generation")
        return str(rows[0][0])

    def _a2i2_year_window(self, generation_id: str) -> tuple[int, int]:
        authority = self._load_inventory_policy_authority(self._connection, generation_id)
        freshness = self._connection.execute(
            "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if authority is None or freshness is None:
            raise ValueError("a2i2 policy requires durable inventory policy authority")
        policy = cast(Mapping[str, object], authority["authority"]).get("policy")
        match = re.match(r"^(\d{4})", str(freshness[0]))
        if not isinstance(policy, Mapping) or match is None:
            raise ValueError("a2i2 policy authority is malformed")
        return int(policy["min_year"]), int(match.group(1))

    @staticmethod
    def _trusted_authority_token(connection: sqlite3.Connection, generation_id: str) -> str:
        generation = connection.execute(
            "SELECT base_commit, census_digest, inventory_freshness_epoch FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        policy = Ledger._load_inventory_policy_authority(connection, generation_id)
        authors = [
            dict(row)
            for row in connection.execute(
                "SELECT physical_row, row_key, name, normalized_name, scholar_id, dblp_id, enabled, "
                "exclusion_reason, disposition FROM authors WHERE generation_id = ? ORDER BY physical_row",
                (generation_id,),
            )
        ]
        if generation is None or policy is None:
            raise ValueError("trusted corpus authority token is incomplete")
        return evidence_digest(
            {
                "a2i2_policy": "1",
                "authors": authors,
                "base_commit": str(generation[0]),
                "census_digest": str(generation[1]),
                "freshness_epoch": str(generation[2]),
                "generation_id": generation_id,
                "inventory_policy_digest": str(policy["authority_digest"]),
                "scanner_authority": ("citeforge.committed-corpus", "1", "citeforge.strict-bibtex", "1"),
            }
        )

    def scan_and_commit_corpus(self, repo_root: Path) -> ExistingCorpusEvidence:
        """Scan the generation's committed Git corpus and atomically bind all C3 evidence."""
        from .corpus import _scan_existing_corpus_authority

        generation_id = self._generation_id()
        generation = self._connection.execute(
            "SELECT base_commit, state FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if generation is None or str(generation[1]) != GenerationState.RUNNING.value:
            raise ValueError("existing corpus scan requires a running generation")
        enabled_count = self._connection.execute(
            "SELECT COUNT(*) FROM authors WHERE generation_id = ? AND enabled = 1", (generation_id,)
        ).fetchone()[0]
        authority_count = self._connection.execute(
            "SELECT COUNT(DISTINCT author_key) FROM inventory_authorities WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        if authority_count != enabled_count:
            raise ValueError("existing corpus requires the complete inventory union")
        rows = self._connection.execute(
            "SELECT physical_row, row_key, name, normalized_name, scholar_id, dblp_id, enabled, exclusion_reason, "
            "disposition FROM authors WHERE generation_id = ? ORDER BY physical_row",
            (generation_id,),
        ).fetchall()
        from .census import AuthorCensusRow

        census = AuthorCensus(
            tuple(
                AuthorCensusRow(
                    int(row[0]),
                    str(row[1]),
                    str(row[2]),
                    str(row[3]),
                    str(row[4]),
                    str(row[5]),
                    bool(row[6]),
                    str(row[7]),
                    TaskDisposition(str(row[8])),
                )
                for row in rows
            )
        )
        authority_token = self._trusted_authority_token(self._connection, generation_id)
        evidence = _scan_existing_corpus_authority(
            repo_root,
            census,
            generation_id=generation_id,
            base_commit=str(generation[0]),
            a2i2_year_window=self._a2i2_year_window(generation_id),
        )
        self._commit_existing_corpus(evidence, authority_token=authority_token)
        self._corpus_repo_root = repo_root.resolve()
        self.__trusted_corpus_cache = (authority_token, evidence)
        return evidence

    def _trusted_corpus_expected(self) -> object:
        """Scan immutable Git authority without holding a SQLite writer lock."""
        from .corpus import ExistingCorpusEvidence, _scan_existing_corpus_authority, attest_existing_corpus

        if self._corpus_repo_root is None:
            raise ValueError("scanner-owned corpus evidence requires a trusted Git repository root")
        generation_id = self._generation_id()
        base_commit = self._connection.execute(
            "SELECT base_commit FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if base_commit is None:
            raise ValueError("trusted corpus generation is absent")
        token = self._trusted_authority_token(self._connection, generation_id)
        if self.__trusted_corpus_cache is not None:
            cached_token, cached = self.__trusted_corpus_cache
            if cached_token != token or not isinstance(cached, ExistingCorpusEvidence):
                raise StaleClaimError("trusted corpus authority changed after scan")
            attest_existing_corpus(self._corpus_repo_root, cached.git_proof)
            return cached
        census = AuthorCensus(self._author_census_rows(self._connection, generation_id))
        expected = _scan_existing_corpus_authority(
            self._corpus_repo_root,
            census,
            generation_id=generation_id,
            base_commit=str(base_commit[0]),
            a2i2_year_window=self._a2i2_year_window(generation_id),
        )
        self.__trusted_corpus_cache = (token, expected)
        return expected

    def _verify_trusted_corpus(
        self, connection: sqlite3.Connection | None = None, expected_authority: object | None = None
    ) -> None:
        """Rebuild C3 evidence from trusted Git and compare every durable scanner claim."""
        from .corpus import ExistingCorpusEvidence

        if self._corpus_repo_root is None:
            raise ValueError("scanner-owned corpus evidence requires a trusted Git repository root")
        expected = expected_authority if expected_authority is not None else self._trusted_corpus_expected()
        if not isinstance(expected, ExistingCorpusEvidence):
            raise TypeError("trusted corpus scan returned an invalid authority value")
        if connection is None:
            with self._transaction(immediate=True) as locked:
                self._verify_trusted_corpus(locked, expected)
            return
        generation_id = self._generation_id()
        base_commit = connection.execute(
            "SELECT base_commit FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if base_commit is None:
            raise ValueError("trusted corpus generation is absent")
        if expected.snapshot.generation_id != generation_id or expected.snapshot.base_commit != str(base_commit[0]):
            raise StaleClaimError("trusted corpus authority changed during verification")
        if self.__trusted_corpus_cache is not None and self.__trusted_corpus_cache[0] != self._trusted_authority_token(
            connection, generation_id
        ):
            raise StaleClaimError("trusted corpus authority changed during verification")
        stored_snapshot = connection.execute(
            "SELECT evidence_json FROM corpus_snapshots WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        stored_items = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT evidence_json FROM corpus_items WHERE generation_id = ? ORDER BY source_path COLLATE NOCASE",
                (generation_id,),
            )
        )
        stored_corpus_seeds = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
                "AND origin_kind = ? ORDER BY author_key, publication_key",
                (generation_id, EvidenceKind.CORPUS.value),
            )
        )
        corpus_publications = {
            (publication.author_key, publication.publication_key): publication for publication in expected.publications
        }
        expected_seed_union = {(seed.author_key, seed.publication_key): seed for seed in expected.seeds}
        inventory_cache, inventory_publications = self._inventory_authority_maps(connection, generation_id)
        for member, inventory_seed in inventory_cache.items():
            if member not in corpus_publications:
                expected_seed_union[member] = inventory_seed
        stored_seed_union = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
                "ORDER BY author_key, publication_key",
                (generation_id,),
            )
        )
        stored_publications = {
            (str(row[0]), str(row[1])): tuple(row[2:])
            for row in connection.execute(
                "SELECT author_key, publication_key, discovery_source, normalized_title, year, "
                "exact_identifiers_json, baseline_output_path, freshness_policy FROM publications "
                "WHERE generation_id = ?",
                (generation_id,),
            )
        }
        expected_members = set(corpus_publications) | set(inventory_publications)
        if set(stored_publications) != expected_members:
            raise ValueError("publication membership changed from trusted authorities")
        for member in expected_members:
            publication = inventory_publications.get(member) or corpus_publications[member]
            expected_row = (
                publication.discovery_source,
                publication.normalized_title,
                publication.year,
                evidence_json(publication.exact_identifiers),
                publication.baseline_output_path,
                publication.freshness_policy,
            )
            if stored_publications[member] != expected_row:
                raise ValueError("publication row changed from trusted authority")
        receipt = connection.execute(
            "SELECT snapshot_digest, receipt_digest FROM corpus_scan_receipts WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        expected_receipt = evidence_digest(
            {"domain": "citeforge-committed-corpus-scan-v1", "snapshot_digest": expected.snapshot.digest}
        )
        if (
            stored_snapshot is None
            or str(stored_snapshot[0]) != evidence_json(expected.snapshot.canonical_content())
            or stored_items != tuple(evidence_json(item.canonical_content()) for item in expected.items)
            or stored_corpus_seeds != tuple(evidence_json(seed.canonical_content()) for seed in expected.seeds)
            or stored_seed_union
            != tuple(evidence_json(seed.canonical_content()) for _member, seed in sorted(expected_seed_union.items()))
            or receipt is None
            or tuple(receipt) != (expected.snapshot.digest, expected_receipt)
        ):
            raise ValueError("durable corpus evidence does not match trusted Git authority")

    def _commit_existing_corpus(self, evidence: object, *, authority_token: str | None = None) -> None:
        from .corpus import ExistingCorpusEvidence, corpus_author_set_digest

        if not isinstance(evidence, ExistingCorpusEvidence):
            raise TypeError("existing corpus commit requires scanner-owned evidence")
        generation_id = self._generation_id()
        snapshot = evidence.snapshot
        if (
            snapshot.scanner_id != "citeforge.committed-corpus"
            or snapshot.scanner_version != "1"
            or snapshot.parser_id != "citeforge.strict-bibtex"
            or snapshot.parser_version != "1"
            or snapshot.mapper_id != "citeforge.author-directory"
            or snapshot.mapper_version != "1"
            or snapshot.identity_id != "citeforge.publication-key"
            or snapshot.identity_version != "1"
            or snapshot.extractor_id != "citeforge.corpus-identifiers"
            or snapshot.extractor_version != "1"
            or snapshot.a2i2_policy_id != "citeforge.a2i2"
            or snapshot.a2i2_policy_version != "1"
        ):
            raise ValueError("existing corpus code-owned authority identity mismatch")
        items = tuple(sorted(evidence.items, key=lambda item: item.source_path.casefold()))
        publications = tuple(sorted(evidence.publications, key=lambda item: (item.author_key, item.publication_key)))
        seeds = tuple(sorted(evidence.seeds, key=lambda item: (item.author_key, item.publication_key)))
        if snapshot.generation_id != generation_id or any(item.generation_id != generation_id for item in items):
            raise ValueError("existing corpus generation mismatch")
        if len({item.source_path.casefold() for item in items}) != len(items):
            raise ValueError("duplicate existing corpus source path")
        if any(item.snapshot_digest != snapshot.digest for item in items):
            raise ValueError("existing corpus item snapshot mismatch")
        if snapshot.item_set_digest != evidence_digest([item.digest for item in items]):
            raise ValueError("existing corpus item-set digest mismatch")
        for item in items:
            if item.disposition == "parsed":
                if not item.normalized_entry or item.parse_digest != evidence_digest(item.normalized_entry):
                    raise ValueError("existing corpus normalized parse digest mismatch")
            elif item.disposition != "absent" or item.publication_keys or item.normalized_entry:
                raise ValueError("production corpus evidence contains blocked or malformed disposition")
        publication_members = {(item.author_key, item.publication_key) for item in publications}
        item_members = {
            (item.author_key, publication_key) for item in items for publication_key in item.publication_keys
        }
        seed_members = {(item.author_key, item.publication_key) for item in seeds}
        if publication_members != item_members or seed_members != item_members:
            raise ValueError("existing corpus publication or seed membership mismatch")
        publication_by_member = {
            (publication.author_key, publication.publication_key): publication for publication in publications
        }
        for item in items:
            if item.disposition != "parsed":
                continue
            fields = item.normalized_entry.get("fields")
            if not isinstance(fields, Mapping):
                raise ValueError("corpus normalized entry fields are absent")
            ensure_safe_durable_text(str(item.normalized_entry.get("key", "")))
            expected_identifiers = _corpus_identifiers_from_fields(fields)
            expected_title = normalize_title(str(fields.get("title", "")))
            expected_year = extract_year_from_any(fields.get("year"), fallback=0) or None
            for publication_key in item.publication_keys:
                publication = publication_by_member[(item.author_key, publication_key)]
                expected_key = _publication_key_authority(
                    item.author_key,
                    expected_title,
                    expected_year,
                    str(expected_identifiers.get("doi", "")) or None,
                )
                if (
                    publication.publication_key != expected_key
                    or publication.normalized_title != expected_title
                    or publication.year != expected_year
                    or dict(item.exact_identifiers) != expected_identifiers
                    or dict(publication.exact_identifiers) != dict(item.exact_identifiers)
                    or publication.discovery_source != "corpus"
                    or publication.baseline_output_path != item.source_path
                    or publication.freshness_policy != "monthly"
                ):
                    raise ValueError("corpus publication metadata is not independently derived")
        snapshot_content = evidence_json(snapshot.canonical_content())
        with self._transaction(immediate=True) as connection, self._authority_write():
            if authority_token is not None and authority_token != self._trusted_authority_token(
                connection, generation_id
            ):
                raise StaleClaimError("trusted corpus authority changed during scan")
            base = connection.execute(
                "SELECT base_commit, state FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if base is None or str(base[0]) != snapshot.base_commit:
                raise ValueError("existing corpus base commit mismatch")
            if str(base[1]) != GenerationState.RUNNING.value:
                raise ValueError("existing corpus commit requires a running generation")
            enabled = {
                str(row[0])
                for row in connection.execute(
                    "SELECT row_key FROM authors WHERE generation_id = ? AND enabled = 1", (generation_id,)
                )
            }
            if {item.author_key for item in items} != enabled:
                raise ValueError("existing corpus does not cover exact enabled census")
            inventory_authorities = {
                str(row[0])
                for row in connection.execute(
                    "SELECT author_key FROM inventory_authorities WHERE generation_id = ?", (generation_id,)
                )
            }
            if inventory_authorities != enabled:
                raise ValueError("existing corpus requires the complete inventory union")
            inventory_seed_cache, _inventory_publications = self._inventory_authority_maps(connection, generation_id)
            author_rows = self._author_census_rows(connection, generation_id)
            if snapshot.author_set_digest != corpus_author_set_digest(author_rows):
                raise ValueError("existing corpus author-set authority mismatch")
            existing_snapshot = connection.execute(
                "SELECT evidence_json FROM corpus_snapshots WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if existing_snapshot is not None:
                expected_receipt_digest = evidence_digest(
                    {"domain": "citeforge-committed-corpus-scan-v1", "snapshot_digest": snapshot.digest}
                )
                stored_receipt = connection.execute(
                    "SELECT snapshot_digest, receipt_digest FROM corpus_scan_receipts WHERE generation_id = ?",
                    (generation_id,),
                ).fetchone()
                stored_items = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT evidence_json FROM corpus_items WHERE generation_id = ? "
                        "ORDER BY source_path COLLATE NOCASE",
                        (generation_id,),
                    )
                )
                stored_seeds = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
                        "ORDER BY author_key, publication_key",
                        (generation_id,),
                    )
                )
                expected_seeds = list(seeds)
                corpus_members = {(seed.author_key, seed.publication_key) for seed in seeds}
                for row in connection.execute(
                    "SELECT author_key, publication_key FROM publications WHERE generation_id = ? "
                    "ORDER BY author_key, publication_key",
                    (generation_id,),
                ):
                    member = (str(row[0]), str(row[1]))
                    if member not in corpus_members:
                        expected_seeds.append(
                            self._inventory_publication_seed(
                                connection, generation_id, member[0], member[1], inventory_seed_cache
                            )
                        )
                expected_seeds = [replace(seed, seed_digest=seed.derived_seed_digest) for seed in expected_seeds]
                if (
                    str(existing_snapshot[0]) != snapshot_content
                    or stored_receipt is None
                    or tuple(stored_receipt) != (snapshot.digest, expected_receipt_digest)
                    or stored_items != tuple(evidence_json(item.canonical_content()) for item in items)
                    or stored_seeds
                    != tuple(
                        evidence_json(seed.canonical_content())
                        for seed in sorted(expected_seeds, key=lambda item: (item.author_key, item.publication_key))
                    )
                ):
                    raise ValueError("conflicting existing corpus replay")
                return
            partial = connection.execute(
                "SELECT (SELECT COUNT(*) FROM corpus_items WHERE generation_id = ?) + "
                "(SELECT COUNT(*) FROM publication_seed_evidence WHERE generation_id = ?)",
                (generation_id, generation_id),
            ).fetchone()[0]
            if partial:
                raise ValueError("partial existing corpus authority conflicts")
            connection.execute(
                "INSERT INTO corpus_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    snapshot.digest,
                    snapshot.base_commit,
                    snapshot.output_tree_digest,
                    snapshot.baseline_digest,
                    snapshot.scanner_id,
                    snapshot.scanner_version,
                    snapshot.parser_id,
                    snapshot.parser_version,
                    snapshot.item_set_digest,
                    snapshot.derived_a2i2_digest,
                    snapshot_content,
                ),
            )
            self._inject("after_c3_corpus_snapshot")
            for item in items:
                connection.execute(
                    "INSERT INTO corpus_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        snapshot.digest,
                        item.source_path,
                        item.author_key,
                        item.before_digest,
                        item.parse_digest,
                        evidence_json(item.publication_keys),
                        item.disposition,
                        evidence_json(item.exact_identifiers),
                        item.digest,
                        evidence_json(item.canonical_content()),
                    ),
                )
            self._inject("after_c3_corpus_items")
            for publication in publications:
                expected_publication_key = _publication_key_authority(
                    publication.author_key,
                    publication.normalized_title,
                    publication.year,
                    str(publication.exact_identifiers.get("doi", "")) or None,
                )
                if publication.publication_key != expected_publication_key:
                    raise ValueError("corpus publication key is not code-derived")
                ambiguous_split = connection.execute(
                    "SELECT normalized_title, year, exact_identifiers_json FROM publications "
                    "WHERE generation_id = ? AND author_key = ? AND publication_key != ?",
                    (
                        generation_id,
                        publication.author_key,
                        publication.publication_key,
                    ),
                ).fetchall()
                if any(
                    (row[1] is None or publication.year is None or int(row[1]) == publication.year)
                    and str(row[0]) == publication.normalized_title
                    and bool(json.loads(str(row[2])).get("doi")) != bool(publication.exact_identifiers.get("doi"))
                    for row in ambiguous_split
                ):
                    raise ValueError("corpus and inventory contain a cross-source late-identifier split")
                existing = connection.execute(
                    "SELECT normalized_title, year, exact_identifiers_json FROM publications "
                    "WHERE generation_id = ? AND author_key = ? AND publication_key = ?",
                    (generation_id, publication.author_key, publication.publication_key),
                ).fetchone()
                if existing is None:
                    self._insert_publication(connection, generation_id, publication)
                else:
                    existing_identifiers = json.loads(str(existing[2]))
                    if (
                        existing_identifiers.get("doi") != publication.exact_identifiers.get("doi")
                        or (
                            existing[1] is not None
                            and publication.year is not None
                            and int(existing[1]) != publication.year
                        )
                        or title_similarity(str(existing[0]), publication.normalized_title) < SIM_IDENTIFIER_TITLE_MIN
                    ):
                        raise ValueError("shared DOI publication lacks coherent year, identifiers, or title similarity")
            self._inject("after_c3_corpus_publications")
            item_by_key = {item.key: item for item in items}
            for seed in seeds:
                origin = item_by_key.get(seed.origin_evidence_key)
                if (
                    seed.generation_id != generation_id
                    or seed.origin_kind is not EvidenceKind.CORPUS
                    or origin is None
                    or seed.publication_key not in origin.publication_keys
                    or seed.author_key != origin.author_key
                    or seed.origin_evidence_digest != origin.digest
                    or seed.baseline_digest != origin.before_digest
                    or evidence_json(seed.exact_identifiers) != evidence_json(origin.exact_identifiers)
                    or evidence_json(seed.baseline_entry) != evidence_json(origin.normalized_entry)
                    or seed.seed_digest != seed.derived_seed_digest
                ):
                    raise ValueError("existing corpus seed is not derived from exact item authority")
                connection.execute(
                    "INSERT INTO publication_seed_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        seed.author_key,
                        seed.publication_key,
                        seed.origin_kind.value,
                        seed.origin_evidence_key,
                        seed.origin_evidence_digest,
                        seed.baseline_digest,
                        evidence_json(seed.exact_identifiers),
                        seed.seed_digest,
                        evidence_json(seed.canonical_content()),
                    ),
                )
            corpus_members = {(seed.author_key, seed.publication_key) for seed in seeds}
            inventory_seed_cache = {}
            for row in connection.execute(
                "SELECT author_key, publication_key, discovery_source, normalized_title, year, "
                "exact_identifiers_json, baseline_output_path, freshness_policy FROM publications "
                "WHERE generation_id = ? ORDER BY author_key, publication_key",
                (generation_id,),
            ):
                member = (str(row[0]), str(row[1]))
                if member in corpus_members:
                    continue
                publication_seed = self._inventory_publication_seed(
                    connection, generation_id, member[0], member[1], inventory_seed_cache
                )
                publication_seed = replace(
                    publication_seed,
                    seed_digest=publication_seed.derived_seed_digest,
                )
                connection.execute(
                    "INSERT INTO publication_seed_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        publication_seed.author_key,
                        publication_seed.publication_key,
                        publication_seed.origin_kind.value,
                        publication_seed.origin_evidence_key,
                        publication_seed.origin_evidence_digest,
                        publication_seed.baseline_digest,
                        evidence_json(publication_seed.exact_identifiers),
                        publication_seed.seed_digest,
                        evidence_json(publication_seed.canonical_content()),
                    ),
                )
            self._inject("after_c3_corpus_seeds")
            receipt_digest = evidence_digest(
                {"domain": "citeforge-committed-corpus-scan-v1", "snapshot_digest": snapshot.digest}
            )
            connection.execute(
                "INSERT INTO corpus_scan_receipts VALUES (?, ?, ?)",
                (generation_id, snapshot.digest, receipt_digest),
            )

    @staticmethod
    def _author_census_rows(connection: sqlite3.Connection, generation_id: str) -> tuple[AuthorCensusRow, ...]:
        from .census import AuthorCensusRow

        return tuple(
            AuthorCensusRow(
                int(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                str(row[5]),
                bool(row[6]),
                str(row[7]),
                TaskDisposition(str(row[8])),
            )
            for row in connection.execute(
                "SELECT physical_row, row_key, name, normalized_name, scholar_id, dblp_id, enabled, "
                "exclusion_reason, disposition FROM authors WHERE generation_id = ? ORDER BY physical_row",
                (generation_id,),
            )
        )

    @staticmethod
    def _shape_inventory_seed_tasks(
        connection: sqlite3.Connection,
        generation_id: str,
        tasks: Sequence[TaskSpec],
    ) -> tuple[tuple[TaskSpec, ...], dict[str, ApplicabilityReason]]:
        policy_row = connection.execute(
            "SELECT 1 FROM discovery_policy_authority WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if policy_row is None:
            return tuple(tasks), {}
        authority = Ledger._load_discovery_authority(connection, generation_id)
        mode = authority.resolved_provider_modes["s2"]
        if mode == "applicable":
            return tuple(tasks), {}
        reason = (
            ApplicabilityReason.PROVIDER_DISABLED if mode == "disabled" else ApplicabilityReason.PROVIDER_NOT_CONFIGURED
        )
        shaped = []
        reasons = {}
        for task in tasks:
            if task.provider != "s2":
                shaped.append(task)
                continue
            value = TaskSpec(
                task.author_key,
                task.publication_key,
                task.provider,
                task.operation,
                None,
                task.required,
                "not_applicable",
            )
            shaped.append(value)
            reasons[value.key] = reason
        return tuple(shaped), reasons

    @staticmethod
    def _reconstruct_inventory_authority(
        connection: sqlite3.Connection,
        generation_id: str,
        author_key: str,
    ) -> tuple[dict[tuple[str, str], PublicationSeedEvidence], dict[tuple[str, str], PublicationMetadata]]:
        """Rederive one author's complete publication and seed maps from immutable inventory evidence."""
        from .census import AuthorCensusRow
        from .inventory import (
            InventoryPolicy,
            InventorySnapshot,
            SnapshotContribution,
            inventory_baseline_entries,
            reduce_author_inventory,
        )

        author = connection.execute(
            "SELECT physical_row, row_key, name, normalized_name, scholar_id, dblp_id, enabled, exclusion_reason, "
            "disposition FROM authors WHERE generation_id = ? AND row_key = ?",
            (generation_id, author_key),
        ).fetchone()
        authority = connection.execute(
            "SELECT reducer_version, policy_digest, snapshot_digest, reduction_digest, round_key "
            "FROM inventory_authorities "
            "WHERE generation_id = ? AND author_key = ? ORDER BY reducer_version",
            (generation_id, author_key),
        ).fetchall()
        policy_authority = Ledger._load_inventory_policy_authority(connection, generation_id)
        if author is None or len(authority) != 1 or policy_authority is None:
            raise ValueError("publication seed lacks exact immutable inventory authority")
        policy_content = cast(Mapping[str, object], policy_authority["authority"])["policy"]
        if not isinstance(policy_content, Mapping):
            raise ValueError("inventory policy authority is malformed")
        generation_freshness = connection.execute(
            "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        policy = InventoryPolicy(
            int(policy_content["min_year"]),
            int(policy_content["max_publications"]),
            int(policy_content["max_scholar_pages"]),
            str(cast(Mapping[str, object], policy_content["seed_adapter_versions"])["doi_csl"]),
            str(cast(Mapping[str, object], policy_content["seed_adapter_versions"])["s2"]),
            str(generation_freshness),
        )
        expected_policy_digest = _digest(
            {
                "doi_adapter_version": policy.doi_adapter_version,
                "freshness_epoch": policy.freshness_epoch,
                "max_publications": policy.max_publications,
                "max_scholar_pages": policy.max_scholar_pages,
                "min_year": policy.min_year,
                "s2_adapter_version": policy.s2_adapter_version,
            }
        )
        authority_row = authority[0]
        if str(authority_row[1]) != expected_policy_digest:
            raise ValueError("inventory authority policy digest changed")
        census_row = AuthorCensusRow(
            int(author[0]),
            str(author[1]),
            str(author[2]),
            str(author[3]),
            str(author[4]),
            str(author[5]),
            bool(author[6]),
            str(author[7]),
            TaskDisposition(str(author[8])),
        )
        contribution_rows = connection.execute(
            "SELECT contribution.task_key, task.provider, task.state, observation.schema_version, "
            "observation.response_digest, observation.response_json, contribution.page_offset, "
            "contribution.next_offset, contribution.request_key, contribution.capability_id, "
            "contribution.topology_digest FROM inventory_contributions AS contribution "
            "JOIN tasks AS task ON task.generation_id = contribution.generation_id "
            "AND task.task_key = contribution.task_key JOIN observations AS observation "
            "ON observation.generation_id = contribution.generation_id "
            "AND observation.request_key = contribution.request_key WHERE contribution.generation_id = ? "
            "AND contribution.author_key = ? AND contribution.reducer_version = ? "
            "ORDER BY task.provider, contribution.task_key",
            (generation_id, author_key, str(authority_row[0])),
        ).fetchall()
        contributions = []
        for row in contribution_rows:
            response = json.loads(str(row[5]))
            if not isinstance(response, Mapping) or evidence_digest(response) != str(row[4]):
                raise ValueError("publication seed inventory observation changed")
            articles = response.get("articles", [])
            if not isinstance(articles, list):
                raise ValueError("publication seed inventory articles are malformed")
            contributions.append(
                SnapshotContribution(
                    str(row[0]),
                    str(row[1]),
                    TaskDisposition(str(row[2])),
                    str(row[3]),
                    str(row[4]),
                    tuple(articles),
                    row[6],
                    row[7],
                    str(row[8]),
                    str(row[9]),
                    str(row[10]),
                )
            )
        snapshot = InventorySnapshot(author_key, tuple(contributions))
        if not isinstance(snapshot, InventorySnapshot):
            raise TypeError("ledger failed to reconstruct inventory snapshot")
        if snapshot.digest != str(authority_row[2]):
            raise ValueError("publication seed inventory snapshot changed")
        baseline_entries = inventory_baseline_entries(census_row, snapshot, policy)
        reduction = reduce_author_inventory(census_row, snapshot, policy)
        shaped_seed_tasks, _reasons = Ledger._shape_inventory_seed_tasks(
            connection, generation_id, reduction.seed_tasks
        )
        expected_reduction_digest = _digest(
            {
                "policy_digest": expected_policy_digest,
                "publications": [Ledger._publication_content(item) for item in reduction.publications],
                "reducer_version": str(authority_row[0]),
                "seed_tasks": [item.identity_digest for item in shaped_seed_tasks],
                "snapshot_digest": snapshot.digest,
            }
        )
        if str(authority_row[3]) != expected_reduction_digest:
            raise ValueError("inventory authority reduction digest changed")
        terminal = tuple(
            contribution
            for contribution in snapshot.contributions
            if contribution.logical_source != "scholar" or contribution.next_offset is None
        )
        receipt = connection.execute(
            "SELECT round.phase, round.planner_id, round.planner_version, receipt.source_task_keys_json, "
            "receipt.source_evidence_digests_json FROM plan_rounds AS round JOIN reduction_receipts AS receipt "
            "ON receipt.generation_id = round.generation_id AND receipt.round_key = round.round_key "
            "WHERE round.generation_id = ? AND round.round_key = ?",
            (generation_id, str(authority_row[4])),
        ).fetchall()
        if len(receipt) != 1:
            raise ValueError("inventory authority lacks exact reduction receipt")
        receipt_row = receipt[0]
        receipt_tasks = tuple(json.loads(str(receipt_row[3])))
        receipt_digests = tuple(json.loads(str(receipt_row[4])))
        if len(receipt_tasks) != len(receipt_digests) or len(receipt_tasks) != len(set(receipt_tasks)):
            raise ValueError("inventory authority reduction receipt membership changed")
        receipt_evidence = dict(zip(receipt_tasks, receipt_digests, strict=True))
        expected_tasks = tuple(sorted(item.task_key for item in terminal))
        expected_evidence = {
            item.task_key: item.observation_digest for item in sorted(terminal, key=lambda item: item.task_key)
        }
        if (
            str(receipt_row[0]) != PlanPhase.DISCOVERY.value
            or str(receipt_row[1]) != "inventory_union"
            or str(receipt_row[2]) != str(authority_row[0])
            or any(receipt_evidence.get(task_key) != expected_evidence[task_key] for task_key in expected_tasks)
        ):
            raise ValueError("inventory authority reduction receipt changed")
        expected_publications = {publication.publication_key: publication for publication in reduction.publications}
        publication_cache = {(author_key, key): metadata for key, metadata in expected_publications.items()}
        result_cache: dict[tuple[str, str], PublicationSeedEvidence] = {}
        for key, metadata in expected_publications.items():
            entry = baseline_entries.get(key)
            if entry is None:
                continue
            origin_content = {
                "baseline_entry": entry,
                "reduction_digest": str(authority_row[3]),
                "snapshot_digest": snapshot.digest,
            }
            seed = PublicationSeedEvidence(
                generation_id,
                author_key,
                key,
                EvidenceKind.PUBLICATION,
                f"inventory:{author_key}:{authority_row[0]}:{snapshot.digest}",
                evidence_digest(origin_content),
                evidence_digest(entry),
                metadata.exact_identifiers,
                "0" * 64,
                entry,
            )
            result_cache[(author_key, key)] = replace(seed, seed_digest=seed.derived_seed_digest)
        return result_cache, publication_cache

    @staticmethod
    def _inventory_publication_seed(
        connection: sqlite3.Connection,
        generation_id: str,
        author_key: str,
        publication_key: str,
        cache: dict[tuple[str, str], PublicationSeedEvidence] | None = None,
        publication_cache: dict[tuple[str, str], PublicationMetadata] | None = None,
    ) -> PublicationSeedEvidence:
        cache_key = (author_key, publication_key)
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        seeds, publications = Ledger._reconstruct_inventory_authority(connection, generation_id, author_key)
        if cache is not None:
            cache.update(seeds)
        if publication_cache is not None:
            publication_cache.update(publications)
        if cache_key not in seeds:
            raise ValueError("publication seed is absent from immutable inventory reduction")
        return seeds[cache_key]

    @staticmethod
    def _inventory_authority_maps(
        connection: sqlite3.Connection,
        generation_id: str,
    ) -> tuple[
        dict[tuple[str, str], PublicationSeedEvidence],
        dict[tuple[str, str], PublicationMetadata],
    ]:
        seed_cache: dict[tuple[str, str], PublicationSeedEvidence] = {}
        publication_cache: dict[tuple[str, str], PublicationMetadata] = {}
        authors = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT row_key FROM authors WHERE generation_id = ? AND enabled = 1 ORDER BY row_key",
                (generation_id,),
            )
        )
        for author_key in authors:
            authority = connection.execute(
                "SELECT COUNT(*) FROM inventory_authorities WHERE generation_id = ? AND author_key = ?",
                (generation_id, author_key),
            ).fetchone()[0]
            if authority != 1:
                raise ValueError("inventory authority is incomplete for enabled census")
            seeds, publications = Ledger._reconstruct_inventory_authority(connection, generation_id, author_key)
            seed_cache.update(seeds)
            publication_cache.update(publications)
        round_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT round_key FROM inventory_authorities WHERE generation_id = ?",
                (generation_id,),
            )
        }
        if authors and len(round_keys) != 1:
            raise ValueError("inventory authority aggregate round membership changed")
        if round_keys:
            receipt = connection.execute(
                "SELECT source_task_keys_json, source_evidence_digests_json FROM reduction_receipts "
                "WHERE generation_id = ? AND round_key = ?",
                (generation_id, next(iter(round_keys))),
            ).fetchall()
            if len(receipt) != 1:
                raise ValueError("inventory authority aggregate receipt changed")
            tasks = tuple(json.loads(str(receipt[0][0])))
            digests = tuple(json.loads(str(receipt[0][1])))
            if len(tasks) != len(digests) or len(tasks) != len(set(tasks)):
                raise ValueError("inventory authority aggregate receipt membership changed")
            aggregate = dict(zip(tasks, digests, strict=True))
            expected = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT contribution.task_key, contribution.observation_digest "
                    "FROM inventory_contributions AS contribution JOIN tasks AS task "
                    "ON task.generation_id = contribution.generation_id AND task.task_key = contribution.task_key "
                    "WHERE contribution.generation_id = ? AND "
                    "(task.provider != 'scholar' OR contribution.next_offset IS NULL)",
                    (generation_id,),
                )
            }
            if aggregate != expected:
                raise ValueError("inventory authority aggregate receipt membership changed")
        return seed_cache, publication_cache

    def commit_corpus_snapshot(self, snapshot: CorpusSnapshot, items: Sequence[CorpusItemEvidence]) -> str:
        """Reject caller-attested corpus authority in the supported API."""
        del snapshot, items
        raise ValueError("corpus authority must be created by scan_and_commit_corpus")

    def _commit_corpus_snapshot_fixture(self, snapshot: CorpusSnapshot, items: Sequence[CorpusItemEvidence]) -> str:
        """Persist value-object fixtures for legacy planner unit tests only."""
        generation_id = self._generation_id()
        if snapshot.generation_id != generation_id:
            raise ValueError("corpus snapshot generation mismatch")
        ordered = tuple(sorted(items, key=lambda item: item.source_path.casefold()))
        if len({item.source_path.casefold() for item in ordered}) != len(ordered):
            raise ValueError("duplicate corpus source path")
        if any(item.generation_id != generation_id or item.snapshot_digest != snapshot.digest for item in ordered):
            raise ValueError("corpus item snapshot mismatch")
        if snapshot.item_set_digest != evidence_digest([item.digest for item in ordered]):
            raise ValueError("corpus item-set digest mismatch")
        snapshot_content = evidence_json(snapshot.canonical_content())
        with self._transaction(immediate=True) as connection, self._authority_write():
            generation = connection.execute(
                "SELECT base_commit FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or str(generation[0]) != snapshot.base_commit:
                raise ValueError("corpus snapshot base commit mismatch")
            enabled_authors = {
                str(row[0])
                for row in connection.execute(
                    "SELECT row_key FROM authors WHERE generation_id = ? AND enabled = 1", (generation_id,)
                )
            }
            if {item.author_key for item in ordered} != enabled_authors:
                raise ValueError("corpus snapshot does not match exact enabled census membership")
            existing = connection.execute(
                "SELECT snapshot_digest, evidence_json FROM corpus_snapshots WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                stored_items = connection.execute(
                    "SELECT evidence_json FROM corpus_items WHERE generation_id = ? "
                    "ORDER BY source_path COLLATE NOCASE",
                    (generation_id,),
                ).fetchall()
                if (
                    str(existing[0]) != snapshot.digest
                    or str(existing[1]) != snapshot_content
                    or tuple(str(row[0]) for row in stored_items)
                    != tuple(evidence_json(item.canonical_content()) for item in ordered)
                ):
                    raise ValueError("conflicting corpus snapshot replay")
                return snapshot.digest
            connection.execute(
                "INSERT INTO corpus_snapshots VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    snapshot.digest,
                    snapshot.base_commit,
                    snapshot.output_tree_digest,
                    snapshot.baseline_digest,
                    snapshot.scanner_id,
                    snapshot.scanner_version,
                    snapshot.parser_id,
                    snapshot.parser_version,
                    snapshot.item_set_digest,
                    snapshot.derived_a2i2_digest,
                    snapshot_content,
                ),
            )
            self._inject("after_v6_corpus_snapshot")
            for item in ordered:
                connection.execute(
                    "INSERT INTO corpus_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        snapshot.digest,
                        item.source_path,
                        item.author_key,
                        item.before_digest,
                        item.parse_digest,
                        evidence_json(item.publication_keys),
                        item.disposition,
                        evidence_json(item.exact_identifiers),
                        item.digest,
                        evidence_json(item.canonical_content()),
                    ),
                )
            self._inject("after_v6_corpus_items")
        return snapshot.digest

    def commit_publication_seed_evidence(self, seeds: Sequence[PublicationSeedEvidence]) -> None:
        """Persist exact seed origins without implying discovery completeness."""
        generation_id = self._generation_id()
        ordered = tuple(sorted(seeds, key=lambda seed: (seed.author_key, seed.publication_key)))
        if len({(seed.author_key, seed.publication_key) for seed in ordered}) != len(ordered):
            raise ValueError("duplicate publication seed")
        if any(seed.generation_id != generation_id for seed in ordered):
            raise ValueError("publication seed generation mismatch")
        with self._transaction(immediate=True) as connection, self._authority_write():
            inventory_seed_cache: dict[tuple[str, str], PublicationSeedEvidence] = {}
            for seed in ordered:
                author = connection.execute(
                    "SELECT enabled FROM authors WHERE generation_id = ? AND row_key = ?",
                    (generation_id, seed.author_key),
                ).fetchone()
                if author is None or int(author[0]) != 1:
                    raise ValueError("publication seed author is absent or disabled")
                if seed.origin_kind is EvidenceKind.CORPUS:
                    origin_rows = connection.execute(
                        "SELECT evidence_digest, evidence_json FROM corpus_items WHERE generation_id = ?",
                        (generation_id,),
                    ).fetchall()
                    valid_origin = False
                    for origin in origin_rows:
                        content = json.loads(str(origin[1]))
                        reconstructed = CorpusItemEvidence(**content)
                        if (
                            reconstructed.key == seed.origin_evidence_key
                            and str(origin[0]) == seed.origin_evidence_digest
                            and seed.publication_key in reconstructed.publication_keys
                            and seed.author_key == reconstructed.author_key
                        ):
                            valid_origin = True
                    if not valid_origin:
                        raise ValueError("publication seed lacks exact corpus origin")
                    if seed.baseline_digest != next(
                        (
                            CorpusItemEvidence(**json.loads(str(origin[1]))).before_digest
                            for origin in origin_rows
                            if CorpusItemEvidence(**json.loads(str(origin[1]))).key == seed.origin_evidence_key
                        ),
                        None,
                    ):
                        raise ValueError("publication seed baseline does not match corpus origin")
                    origin_item = next(
                        CorpusItemEvidence(**json.loads(str(origin[1])))
                        for origin in origin_rows
                        if CorpusItemEvidence(**json.loads(str(origin[1]))).key == seed.origin_evidence_key
                    )
                    if evidence_json(seed.exact_identifiers) != evidence_json(origin_item.exact_identifiers):
                        raise ValueError("corpus seed identifiers do not match immutable parsed authority")
                    if evidence_json(seed.baseline_entry) != evidence_json(origin_item.normalized_entry):
                        raise ValueError("corpus seed baseline entry does not match immutable parsed authority")
                elif seed.origin_kind is EvidenceKind.PUBLICATION:
                    expected = self._inventory_publication_seed(
                        connection,
                        generation_id,
                        seed.author_key,
                        seed.publication_key,
                        inventory_seed_cache,
                    )
                    if evidence_json(seed.canonical_content()) != evidence_json(expected.canonical_content()):
                        raise ValueError("publication seed does not match immutable inventory evidence")
                if seed.seed_digest != seed.derived_seed_digest:
                    raise ValueError("publication seed digest is not derived from immutable evidence")
                content = evidence_json(seed.canonical_content())
                existing = connection.execute(
                    "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
                    "AND author_key = ? AND publication_key = ?",
                    (generation_id, seed.author_key, seed.publication_key),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != content:
                        raise ValueError("conflicting seed evidence replay")
                    continue
                connection.execute(
                    "INSERT INTO publication_seed_evidence VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        seed.author_key,
                        seed.publication_key,
                        seed.origin_kind.value,
                        seed.origin_evidence_key,
                        seed.origin_evidence_digest,
                        seed.baseline_digest,
                        evidence_json(seed.exact_identifiers),
                        seed.seed_digest,
                        content,
                    ),
                )
            self._inject("after_v6_seed_evidence")

    def load_seed_snapshot(self) -> tuple[PublicationSeedEvidence, ...]:
        generation_id = self._generation_id()
        rows = self._connection.execute(
            "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
            "ORDER BY author_key, publication_key",
            (generation_id,),
        ).fetchall()
        result: list[PublicationSeedEvidence] = []
        for row in rows:
            try:
                content = json.loads(str(row[0]))
                content["origin_kind"] = EvidenceKind(content["origin_kind"])
                result.append(PublicationSeedEvidence(**content))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("corrupt publication seed evidence") from exc
        return tuple(result)

    @staticmethod
    def _snapshot_for_pass(connection: sqlite3.Connection, generation_id: str, pass_id: str) -> Mapping[str, object]:
        definition = pass_for(pass_id)
        items: list[dict[str, object]] = []

        def add(kind: EvidenceKind, key: str, digest: str, payload: object) -> None:
            _identifier(key, "planner snapshot item key")
            if not _HEX_DIGEST_RE.fullmatch(digest):
                raise ValueError("invalid planner snapshot source digest")
            items.append({"kind": kind.value, "key": key, "digest": digest, "payload": payload})

        for row in connection.execute(
            "SELECT source_path, evidence_digest, evidence_json FROM corpus_items "
            "WHERE generation_id = ? ORDER BY source_path COLLATE NOCASE",
            (generation_id,),
        ):
            try:
                payload = json.loads(str(row[2]))
            except json.JSONDecodeError as exc:
                raise ValueError("corrupt corpus item evidence") from exc
            if evidence_json(payload) != str(row[2]):
                raise ValueError("noncanonical corpus item evidence")
            add(EvidenceKind.CORPUS, f"corpus:{evidence_digest(str(row[0]))}", str(row[1]), payload)
        for row in connection.execute(
            "SELECT author_key, publication_key, seed_digest, evidence_json FROM publication_seed_evidence "
            "WHERE generation_id = ? ORDER BY author_key, publication_key",
            (generation_id,),
        ):
            try:
                payload = json.loads(str(row[3]))
            except json.JSONDecodeError as exc:
                raise ValueError("corrupt publication seed evidence") from exc
            add(EvidenceKind.SEED, f"seed:{row[0]}:{row[1]}", str(row[2]), payload)
        for row in connection.execute(
            "SELECT author_key, publication_key, discovery_source, normalized_title, year, exact_identifiers_json, "
            "baseline_output_path, freshness_policy FROM publications WHERE generation_id = ? "
            "ORDER BY author_key, publication_key",
            (generation_id,),
        ):
            payload = dict(row)
            payload["exact_identifiers"] = json.loads(str(payload.pop("exact_identifiers_json")))
            key = f"publication:{row['author_key']}:{row['publication_key']}"
            add(EvidenceKind.PUBLICATION, key, evidence_digest(payload), payload)
        for row in connection.execute(
            "SELECT request_key, provider, schema_version, disposition, response_json, response_digest, "
            "authoritative_empty, safe_diagnostic "
            "FROM observations WHERE generation_id = ? ORDER BY request_key",
            (generation_id,),
        ):
            payload = dict(row)
            if payload.get("response_json") is not None:
                payload["response"] = json.loads(str(payload.pop("response_json")))
            digest = str(row["response_digest"] or evidence_digest(payload))
            add(EvidenceKind.OBSERVATION, f"observation:{row['request_key']}", digest, payload)
        for row in connection.execute(
            "SELECT task_key, author_key, publication_key, provider, operation, request_key, required, applicability, "
            "applicability_reason, state, identity_digest FROM tasks "
            "WHERE generation_id = ? ORDER BY task_key",
            (generation_id,),
        ):
            payload = dict(row)
            add(EvidenceKind.APPLICABILITY, f"applicability:{row['task_key']}", str(row["identity_digest"]), payload)
        for row in connection.execute(
            "SELECT request_key, identity_json, state, response_digest FROM requests "
            "WHERE generation_id = ? ORDER BY request_key",
            (generation_id,),
        ):
            try:
                identity = json.loads(str(row[1]))
            except json.JSONDecodeError as exc:
                raise ValueError("corrupt request identity") from exc
            add(
                EvidenceKind.APPLICABILITY,
                f"request:{row[0]}",
                evidence_digest(identity),
                {
                    "request_key": str(row[0]),
                    "identity": identity,
                    "state": str(row[2]),
                    "response_digest": row[3],
                    "consumers": tuple(
                        str(consumer[0])
                        for consumer in connection.execute(
                            "SELECT task_key FROM request_consumers WHERE generation_id = ? AND request_key = ? "
                            "ORDER BY task_key",
                            (generation_id, str(row[0])),
                        )
                    ),
                },
            )
        for row in connection.execute(
            "SELECT reduction_digest, round_key, source_task_keys_json, source_dispositions_json, "
            "source_evidence_digests_json FROM reduction_receipts WHERE generation_id = ? "
            "ORDER BY reduction_digest",
            (generation_id,),
        ):
            try:
                payload = {
                    "round_key": str(row[1]),
                    "source_task_keys": json.loads(str(row[2])),
                    "source_dispositions": json.loads(str(row[3])),
                    "source_evidence_digests": json.loads(str(row[4])),
                }
            except json.JSONDecodeError as exc:
                raise ValueError("corrupt reduction receipt") from exc
            add(EvidenceKind.REDUCTION_RECEIPT, f"reduction:{row[0]}", str(row[0]), payload)
        for row in connection.execute(
            "SELECT decision_key, evidence_json FROM provenance_decisions WHERE generation_id = ? AND pass_key != "
            "COALESCE((SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = ?), '') "
            "ORDER BY decision_key",
            (generation_id, generation_id, pass_id),
        ):
            payload = json.loads(str(row[1]))
            add(EvidenceKind.PROVENANCE, f"provenance:{row[0]}", evidence_digest(payload), payload)
        for row in connection.execute(
            "SELECT intent_key, evidence_json FROM materialization_intents WHERE generation_id = ? AND pass_key != "
            "COALESCE((SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = ?), '') "
            "ORDER BY intent_key",
            (generation_id, generation_id, pass_id),
        ):
            payload = json.loads(str(row[1]))
            add(EvidenceKind.INTENT, f"intent:{row[0]}", evidence_digest(payload), payload)
        ordered = tuple(sorted(items, key=lambda item: str(item["key"])))
        if len({str(item["key"]) for item in ordered}) != len(ordered):
            raise ValueError("duplicate planner snapshot item")
        frozen = _freeze_json(
            {
                "generation_id": generation_id,
                "pass_id": definition.pass_id,
                "pass_version": definition.version,
                "items": ordered,
            }
        )
        if not isinstance(frozen, Mapping):
            raise AssertionError("planner snapshot must be a mapping")
        return frozen

    def snapshot_for_pass(self, pass_id: str) -> Mapping[str, object]:
        expected = None
        if self._connection.execute("SELECT COUNT(*) FROM corpus_scan_receipts").fetchone()[0]:
            expected = self._trusted_corpus_expected()
        with self._transaction(immediate=True) as connection:
            if expected is not None:
                self._verify_trusted_corpus(connection, expected)
            return self._snapshot_for_pass(connection, self._generation_id(), pass_id)

    @staticmethod
    def _snapshot_for_discovery_pass(
        connection: sqlite3.Connection,
        generation_id: str,
        pass_id: str,
        *,
        authority: object | None = None,
        decisions: Sequence[object] = (),
        adopted_keys: frozenset[str] = frozenset(),
        source_items: Sequence[Mapping[str, object]] = (),
    ) -> Mapping[str, object]:
        definition = pass_for(pass_id)
        if pass_id not in {
            "known_doi",
            "broad_discovery",
            "dynamic_expansion",
            "venue_fallback",
            "late_identifiers",
            "html_probe",
            "merge_intents",
        }:
            raise ValueError("unsupported discovery snapshot")
        items: list[dict[str, object]] = []
        for row in connection.execute(
            "SELECT author_key, publication_key, seed_digest, evidence_json FROM publication_seed_evidence "
            "WHERE generation_id = ? ORDER BY author_key, publication_key",
            (generation_id,),
        ):
            payload = json.loads(str(row[3]))
            items.append(
                {
                    "digest": str(row[2]),
                    "key": f"seed:{row[0]}:{row[1]}",
                    "kind": EvidenceKind.SEED.value,
                    "payload": payload,
                }
            )
        for source in source_items:
            if set(source) != {"digest", "key", "kind", "payload"}:
                raise ValueError("discovery source envelope is malformed")
            payload = source["payload"]
            if not isinstance(payload, Mapping) or evidence_digest(payload) != source["digest"]:
                raise ValueError("discovery source envelope digest changed")
            items.append(dict(source))
        if authority is not None:
            from .discovery import DiscoveryAuthority, DiscoveryDecision

            if not isinstance(authority, DiscoveryAuthority) or not all(
                isinstance(item, DiscoveryDecision) for item in decisions
            ):
                raise TypeError("discovery snapshot requires typed authority and decisions")
            items.append(
                {
                    "digest": authority.digest,
                    "key": f"authority:{authority.digest}",
                    "kind": EvidenceKind.REDUCTION_RECEIPT.value,
                    "payload": authority.canonical_content(),
                }
            )
            consumers_by_request = Ledger._discovery_request_consumers(decisions)
            for value in cast(Sequence[DiscoveryDecision], decisions):
                task = value.task
                decision_payload: dict[str, object] = {
                    "adopted": task.key in adopted_keys,
                    "applicability": task.applicability,
                    "author_key": task.author_key,
                    "identity_digest": task.identity_digest,
                    "request_consumers": (consumers_by_request[task.request.key] if task.request is not None else ()),
                    "operation": task.operation,
                    "provider": task.provider,
                    "publication_key": task.publication_key,
                    "reason": value.reason.value if value.reason is not None else None,
                    "request_key": task.request.key if task.request is not None else None,
                    "required": task.required,
                    "task_key": task.key,
                }
                items.append(
                    {
                        "digest": evidence_digest(decision_payload),
                        "key": f"decision:{task.key}",
                        "kind": EvidenceKind.APPLICABILITY.value,
                        "payload": decision_payload,
                    }
                )
        ordered_items = tuple(sorted(items, key=lambda item: str(item["key"])))
        frozen = _freeze_json(
            {
                "generation_id": generation_id,
                "pass_id": pass_id,
                "pass_version": definition.version,
                "items": ordered_items,
            }
        )
        if not isinstance(frozen, Mapping):
            raise AssertionError("discovery planner snapshot must be a mapping")
        return frozen

    def execute_registered_pass(self, pass_id: str) -> PlannerPassReceipt:
        if pass_id in {"known_doi", "broad_discovery", "dynamic_expansion"}:
            raise ValueError("C4 discovery passes require the atomic discovery wave API")
        if pass_id in {"venue_fallback", "late_identifiers", "merge_intents"}:
            raise ValueError("C5 passes require the atomic publication discovery API")
        return self._execute_registered_pass_compatibility_fixture(pass_id)

    def _execute_registered_pass_compatibility_fixture(self, pass_id: str) -> PlannerPassReceipt:
        """Execute the pre-C4 generic pass contract for historical test fixtures."""
        generation_id = self._generation_id()
        receipt_count = self._connection.execute(
            "SELECT COUNT(*) FROM corpus_scan_receipts WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        inventory_count = self._connection.execute(
            "SELECT COUNT(*) FROM inventory_authorities WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        if inventory_count and receipt_count != 1:
            raise ValueError("planner passes require trusted committed-corpus authority")
        trusted_expected = self._trusted_corpus_expected() if receipt_count else None
        with self._transaction(immediate=True) as connection:
            if trusted_expected is not None:
                self._verify_trusted_corpus(connection, trusted_expected)
            initial_snapshot = self._snapshot_for_pass(connection, generation_id, pass_id)
        legacy_c4 = pass_id in {
            "known_doi",
            "broad_discovery",
            "dynamic_expansion",
            "venue_fallback",
            "late_identifiers",
            "html_probe",
        }
        if legacy_c4:
            legacy_snapshot = _freeze_json({**dict(initial_snapshot), "pass_version": "1"})
            if not isinstance(legacy_snapshot, Mapping):
                raise AssertionError("legacy planner snapshot must be a mapping")
            initial_snapshot = legacy_snapshot
            legacy_items = tuple(
                sorted(str(item["key"]) for item in cast(Sequence[Mapping[str, object]], initial_snapshot["items"]))
            )
            snapshot_digest = evidence_digest(initial_snapshot)
            receipt = PlannerPassReceipt(
                generation_id,
                pass_id,
                "1",
                evidence_digest((generation_id, pass_id, "1", snapshot_digest)),
                _LEGACY_C3_PASS_REGISTRY_DIGEST,
                snapshot_digest,
                legacy_items,
                legacy_items,
                evidence_digest((legacy_items, legacy_items)),
            )
        else:
            receipt = _execute_authoritative_pass(pass_id, initial_snapshot)
        receipt_content = evidence_json(receipt.canonical_content())
        with self._transaction(immediate=True) as connection, self._authority_write():
            if trusted_expected is not None:
                self._verify_trusted_corpus(connection, trusted_expected)
            current_snapshot = self._snapshot_for_pass(connection, generation_id, pass_id)
            if legacy_c4:
                legacy_snapshot = _freeze_json({**dict(current_snapshot), "pass_version": "1"})
                if not isinstance(legacy_snapshot, Mapping):
                    raise AssertionError("legacy planner snapshot must be a mapping")
                current_snapshot = legacy_snapshot
            if evidence_digest(current_snapshot) != receipt.snapshot_digest:
                raise StaleClaimError("planner input membership changed before commit")
            if not legacy_c4 and _execute_authoritative_pass(pass_id, current_snapshot) != receipt:
                raise ValueError("planner pass receipt does not match code-owned authority")
            existing = connection.execute(
                "SELECT receipt_json FROM planner_passes WHERE generation_id = ? AND pass_id = ?",
                (generation_id, pass_id),
            ).fetchone()
            if existing is not None:
                stored_receipt = PlannerPassReceipt(**json.loads(str(existing[0])))
                if (
                    stored_receipt.pass_version == "1"  # noqa: S105 - immutable schema version
                    and stored_receipt.registry_digest == _LEGACY_C3_PASS_REGISTRY_DIGEST
                ):
                    legacy_items = tuple(
                        sorted(
                            str(item["key"]) for item in cast(Sequence[Mapping[str, object]], current_snapshot["items"])
                        )
                    )
                    legacy_snapshot_digest = evidence_digest(current_snapshot)
                    expected_legacy = PlannerPassReceipt(
                        generation_id,
                        pass_id,
                        "1",
                        evidence_digest((generation_id, pass_id, "1", legacy_snapshot_digest)),
                        _LEGACY_C3_PASS_REGISTRY_DIGEST,
                        legacy_snapshot_digest,
                        legacy_items,
                        legacy_items,
                        evidence_digest((legacy_items, legacy_items)),
                    )
                    if stored_receipt != expected_legacy:
                        raise ValueError("conflicting legacy planner pass replay")
                    return stored_receipt
                if not _receipt_matches_authority(stored_receipt, receipt):
                    raise ValueError("conflicting planner pass replay")
                return stored_receipt
            definition = pass_for(pass_id)
            earlier = connection.execute(
                "SELECT pass_id FROM planner_passes WHERE generation_id = ? ORDER BY rowid",
                (generation_id,),
            ).fetchall()
            expected_earlier = tuple(
                value.pass_id
                for value in sorted((pass_for(key) for key in PASSES), key=lambda value: value.ordinal)
                if value.ordinal < definition.ordinal
            )
            if tuple(str(row[0]) for row in earlier) != expected_earlier:
                raise ValueError("planner pass phase sequence is skipped or backward")
            predecessor = connection.execute(
                "SELECT output_digest FROM planner_passes WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            predecessor_digest = str(predecessor[0]) if predecessor is not None else None
            snapshot_authority_digest = evidence_digest(
                {
                    "domain": _SNAPSHOT_DOMAIN_SEPARATOR,
                    "generation_id": generation_id,
                    "pass_id": pass_id,
                    "snapshot": current_snapshot,
                    "predecessor_output_digest": predecessor_digest,
                }
            )
            connection.execute(
                "INSERT INTO planner_passes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    receipt.pass_key,
                    receipt.pass_id,
                    receipt.pass_version,
                    receipt.registry_digest,
                    receipt.snapshot_digest,
                    receipt.output_digest,
                    receipt_content,
                    snapshot_authority_digest,
                    predecessor_digest,
                ),
            )
            self._inject("after_v6_planner_pass")
            unseen = set(receipt.unseen_keys)
            snapshot_items = {
                str(item["key"]): item for item in cast(Sequence[Mapping[str, object]], current_snapshot["items"])
            }
            connection.executemany(
                "INSERT INTO planner_pass_expected_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (
                        generation_id,
                        receipt.pass_key,
                        key,
                        snapshot_items[key]["kind"],
                        snapshot_items[key]["digest"],
                        evidence_json(snapshot_items[key]),
                        int(key in unseen),
                    )
                    for key in receipt.expected_items
                ),
            )
            self._inject("after_v6_planner_expected_items")
        return receipt

    def commit_aggregate_inputs(self, pass_key: str, reduction_id: str, inputs: Sequence[AggregateInput]) -> None:
        generation_id = self._generation_id()
        _identifier(pass_key, "planner pass key")
        _identifier(reduction_id, "reduction ID")
        ordered = tuple(sorted(inputs, key=lambda item: item.ordinal))
        with self._transaction(immediate=True) as connection, self._authority_write():
            self._commit_aggregate_inputs(connection, generation_id, pass_key, reduction_id, ordered)

    def _commit_aggregate_inputs(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        pass_key: str,
        reduction_id: str,
        ordered: Sequence[AggregateInput],
    ) -> None:
        if any(
            item.generation_id != generation_id or item.pass_key != pass_key or item.reduction_id != reduction_id
            for item in ordered
        ):
            raise ValueError("aggregate input authority mismatch")
        pass_row = connection.execute(
            "SELECT pass_id, pass_version, snapshot_digest, receipt_json FROM planner_passes "
            "WHERE generation_id = ? AND pass_key = ?",
            (generation_id, pass_key),
        ).fetchone()
        if pass_row is None:
            raise ValueError("aggregate input planner pass is absent")
        stored_items = connection.execute(
            "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? AND pass_key = ? "
            "ORDER BY item_key",
            (generation_id, pass_key),
        ).fetchall()
        snapshot = _freeze_json(
            {
                "generation_id": generation_id,
                "pass_id": str(pass_row[0]),
                "pass_version": str(pass_row[1]),
                "items": tuple(json.loads(str(row[0])) for row in stored_items),
            }
        )
        if not isinstance(snapshot, Mapping) or evidence_digest(snapshot) != str(pass_row[2]):
            raise ValueError("stored planner snapshot membership is corrupt")
        stored_receipt = json.loads(str(pass_row[3]))
        authoritative = _execute_authoritative_pass(str(pass_row[0]), snapshot)
        if evidence_json(authoritative.canonical_content()) != evidence_json(stored_receipt):
            raise ValueError("stored planner receipt is not code-authoritative")
        expected_items = tuple(cast(Sequence[Mapping[str, object]], snapshot["items"]))
        if len(ordered) != len(expected_items):
            raise ValueError("aggregate input membership is incomplete")
        for ordinal, (item, expected) in enumerate(zip(ordered, expected_items, strict=True)):
            if (
                item.ordinal != ordinal
                or item.kind.value != expected["kind"]
                or item.stable_key != expected["key"]
                or item.source_digest != expected["digest"]
                or evidence_json(item.payload) != evidence_json(expected)
            ):
                raise ValueError("aggregate input membership mismatch")
            expected_payload = expected.get("payload")
            if (
                item.kind is EvidenceKind.APPLICABILITY
                and isinstance(expected_payload, Mapping)
                and str(item.stable_key).startswith("applicability:")
                and expected_payload.get("state") not in {state.value for state in _TERMINAL}
            ):
                raise ValueError("aggregate input includes nonterminal task evidence")
            consumed = connection.execute(
                "SELECT reduction_id FROM aggregate_inputs WHERE generation_id = ? AND pass_key = ? "
                "AND kind = ? AND stable_key = ? AND reduction_id != ?",
                (generation_id, pass_key, item.kind.value, item.stable_key, reduction_id),
            ).fetchone()
            if consumed is not None:
                raise ValueError("aggregate input source was already consumed by another reduction")
            content = evidence_json(item.canonical_content())
            existing = connection.execute(
                "SELECT input_json FROM aggregate_inputs WHERE generation_id = ? AND pass_key = ? "
                "AND reduction_id = ? AND kind = ? AND stable_key = ?",
                (generation_id, pass_key, reduction_id, item.kind.value, item.stable_key),
            ).fetchone()
            if existing is not None:
                if str(existing[0]) != content:
                    raise ValueError("conflicting aggregate input replay")
                continue
            connection.execute(
                "INSERT INTO aggregate_inputs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    pass_key,
                    reduction_id,
                    item.kind.value,
                    item.stable_key,
                    item.source_digest,
                    item.ordinal,
                    item.key,
                    content,
                ),
            )

    def commit_provenance_and_intents(
        self,
        pass_key: str,
        inputs: Sequence[AggregateInput],
        decisions: Sequence[ProvenanceDecision],
        contributions: Sequence[ProvenanceContribution],
        intents: Sequence[MaterializationIntent],
        intent_provenance: Sequence[tuple[str, str]],
    ) -> None:
        """Persist reducer evidence and file intents atomically without applying them."""
        generation_id = self._generation_id()
        if not inputs:
            raise ValueError("provenance requires complete aggregate inputs")
        reduction_ids = {item.reduction_id for item in inputs}
        if len(reduction_ids) != 1:
            raise ValueError("provenance inputs require one reduction ID")
        ordered_inputs = tuple(sorted(inputs, key=lambda item: item.ordinal))
        ordered_decisions = tuple(sorted(decisions, key=lambda item: item.key))
        ordered_contributions = tuple(sorted(contributions, key=lambda item: item.key))
        ordered_intents = tuple(sorted(intents, key=lambda item: item.key))
        links = tuple(sorted(intent_provenance))
        if any(item.generation_id != generation_id or item.pass_key != pass_key for item in ordered_decisions):
            raise ValueError("provenance decision authority mismatch")
        if any(item.generation_id != generation_id for item in ordered_contributions):
            raise ValueError("provenance contribution generation mismatch")
        if any(item.generation_id != generation_id or item.pass_key != pass_key for item in ordered_intents):
            raise ValueError("materialization intent authority mismatch")
        for values, message in (
            (tuple(item.key for item in ordered_decisions), "duplicate provenance decision"),
            (tuple(item.key for item in ordered_contributions), "duplicate provenance contribution"),
            (tuple(item.key for item in ordered_intents), "duplicate materialization intent"),
        ):
            if len(values) != len(set(values)):
                raise ValueError(message)
        if len({item.target_path.casefold() for item in ordered_intents}) != len(ordered_intents):
            raise ValueError("materialization target path collision")
        if len({item.source_path.casefold() for item in ordered_intents}) != len(ordered_intents):
            raise ValueError("materialization source path collision")
        publication_members = {
            (
                str(cast(Mapping[str, object], item.payload["payload"])["author_key"]),
                str(cast(Mapping[str, object], item.payload["payload"])["publication_key"]),
            )
            for item in ordered_inputs
            if item.kind is EvidenceKind.PUBLICATION and isinstance(item.payload.get("payload"), Mapping)
        }
        if publication_members != {(item.author_key, item.publication_key) for item in ordered_intents}:
            raise ValueError("materialization intents do not cover exact publication membership")
        corpus_paths = {
            str(cast(Mapping[str, object], item.payload["payload"])["source_path"]).casefold()
            for item in ordered_inputs
            if item.kind is EvidenceKind.CORPUS and isinstance(item.payload.get("payload"), Mapping)
        }
        if not corpus_paths <= {item.source_path.casefold() for item in ordered_intents}:
            raise ValueError("materialization intents omit a bound corpus path")
        contribution_by_decision: dict[str, list[ProvenanceContribution]] = {}
        for contribution in ordered_contributions:
            contribution_by_decision.setdefault(contribution.decision_key, []).append(contribution)
        inputs_by_identity = {(item.kind.value, item.stable_key, item.source_digest): item for item in ordered_inputs}
        if set(contribution_by_decision) != {item.key for item in ordered_decisions}:
            raise ValueError("contribution references unknown provenance decision")
        for decision in ordered_decisions:
            members = contribution_by_decision.get(decision.key, [])
            if not members or evidence_digest(sorted(item.key for item in members)) != decision.contribution_set_digest:
                raise ValueError("incomplete provenance contribution set")

            def bound_contribution(
                contribution: ProvenanceContribution,
                provenance_decision: ProvenanceDecision,
            ) -> tuple[tuple[str, str, str], AggregateInput]:
                if contribution.request_key is not None:
                    identity = (
                        contribution.source_kind,
                        f"observation:{contribution.request_key}",
                        contribution.observation_digest,
                    )
                    bound = inputs_by_identity.get(identity)
                    if bound is None:
                        raise ValueError("provenance contribution is not bound to exact aggregate input")
                    return identity, bound
                candidates = [
                    (identity, item)
                    for identity, item in inputs_by_identity.items()
                    if identity[0] == contribution.source_kind and identity[2] == contribution.observation_digest
                ]
                if contribution.source_kind == EvidenceKind.PUBLICATION.value:
                    candidates = [
                        (identity, item)
                        for identity, item in candidates
                        if isinstance(item.payload.get("payload"), Mapping)
                        and cast(Mapping[str, object], item.payload["payload"]).get("author_key")
                        == provenance_decision.author_key
                        and cast(Mapping[str, object], item.payload["payload"]).get("publication_key")
                        == provenance_decision.publication_key
                    ]
                if len(candidates) != 1:
                    raise ValueError("provenance contribution lacks exact stable source identity")
                return candidates[0]

            bound_members = [(contribution, *bound_contribution(contribution, decision)) for contribution in members]
            member_identities = {identity for _, identity, _ in bound_members}
            if len(member_identities) != len(members):
                raise ValueError("duplicate provenance contribution source identity")
            selected = [item for item in members if item.selected]
            if len(selected) != 1 or selected[0].value_digest != decision.selected_value_digest:
                raise ValueError("selected provenance is not bound to aggregate input")
            for contribution, _source_identity, bound_input in bound_members:
                bound_payload = bound_input.payload.get("payload")
                if bound_input.kind is EvidenceKind.PUBLICATION and (
                    not isinstance(bound_payload, Mapping)
                    or bound_payload.get("author_key") != decision.author_key
                    or bound_payload.get("publication_key") != decision.publication_key
                ):
                    raise ValueError("publication provenance belongs to another publication")
                if bound_input.kind is EvidenceKind.PUBLICATION:
                    field_key = "normalized_title" if decision.field_name == "title" else decision.field_name
                    publication_value = (
                        cast(Mapping[str, object], bound_payload.get("exact_identifiers", {})).get(decision.field_name)
                        if isinstance(bound_payload, Mapping)
                        and isinstance(bound_payload.get("exact_identifiers"), Mapping)
                        and decision.field_name in cast(Mapping[str, object], bound_payload["exact_identifiers"])
                        else bound_payload.get(field_key)
                        if isinstance(bound_payload, Mapping)
                        else None
                    )
                    if publication_value is None:
                        raise ValueError("publication provenance field is absent")
                    if contribution.value_digest != evidence_digest(publication_value):
                        raise ValueError("publication provenance value digest mismatch")
                if bound_input.kind is EvidenceKind.OBSERVATION:
                    if (
                        not isinstance(bound_payload, Mapping)
                        or contribution.request_key is None
                        or bound_input.stable_key != f"observation:{contribution.request_key}"
                        or bound_payload.get("provider") != contribution.provider
                        or bound_payload.get("schema_version") != contribution.schema_version
                    ):
                        raise ValueError("observation provenance is not exact provider evidence")
                    response = bound_payload.get("response")
                    successful_field = (
                        bound_payload.get("disposition") == TaskDisposition.SUCCEEDED.value
                        and not bool(bound_payload.get("authoritative_empty"))
                        and isinstance(response, Mapping)
                        and decision.field_name in response
                    )
                    if successful_field and isinstance(response, Mapping):
                        if contribution.value_digest != evidence_digest(response[decision.field_name]):
                            raise ValueError("observation provenance value digest mismatch")
                        if contribution.selected != (contribution.rejection_reason == "selected"):
                            raise ValueError("observation selection reason mismatch")
                    elif contribution.selected:
                        raise ValueError("selected observation provenance is not successful field evidence")
                    elif contribution.value_digest is not None or contribution.rejection_reason == "selected":
                        raise ValueError("rejected or absent observation must have no value digest and a reason")
                    consumer_tasks = [
                        item.payload.get("payload")
                        for item in ordered_inputs
                        if item.kind is EvidenceKind.APPLICABILITY
                        and item.stable_key.startswith("applicability:")
                        and isinstance(item.payload.get("payload"), Mapping)
                        and cast(Mapping[str, object], item.payload["payload"]).get("request_key")
                        == contribution.request_key
                    ]
                    if not any(
                        isinstance(task, Mapping)
                        and task.get("author_key") == decision.author_key
                        and task.get("publication_key") == decision.publication_key
                        and task.get("provider") == contribution.provider
                        for task in consumer_tasks
                    ):
                        raise ValueError("observation provenance lacks exact publication consumer")
                if contribution.request_key is not None:
                    observation_inputs = [
                        item
                        for item in ordered_inputs
                        if item.kind is EvidenceKind.OBSERVATION
                        and item.stable_key == f"observation:{contribution.request_key}"
                    ]
                    if len(observation_inputs) != 1:
                        raise ValueError("provenance request lacks exact observation input")
                    envelope = observation_inputs[0].payload.get("payload")
                    if not isinstance(envelope, Mapping) or (
                        envelope.get("provider") != contribution.provider
                        or envelope.get("schema_version") != contribution.schema_version
                    ):
                        raise ValueError("provenance provider schema does not match observation input")
            eligible_identities: set[tuple[str, str, str]] = set()
            for candidate in ordered_inputs:
                candidate_payload = candidate.payload.get("payload")
                if candidate.kind is EvidenceKind.PUBLICATION and isinstance(candidate_payload, Mapping):
                    field_key = "normalized_title" if decision.field_name == "title" else decision.field_name
                    identifiers = candidate_payload.get("exact_identifiers", {})
                    if (
                        candidate_payload.get("author_key") == decision.author_key
                        and candidate_payload.get("publication_key") == decision.publication_key
                        and (
                            field_key in candidate_payload
                            or (isinstance(identifiers, Mapping) and decision.field_name in identifiers)
                        )
                    ):
                        eligible_identities.add((candidate.kind.value, candidate.stable_key, candidate.source_digest))
                elif candidate.kind is EvidenceKind.OBSERVATION and isinstance(candidate_payload, Mapping):
                    candidate_request = str(candidate_payload.get("request_key", ""))
                    has_consumer = any(
                        item.kind is EvidenceKind.APPLICABILITY
                        and item.stable_key.startswith("applicability:")
                        and isinstance(item.payload.get("payload"), Mapping)
                        and cast(Mapping[str, object], item.payload["payload"]).get("request_key") == candidate_request
                        and cast(Mapping[str, object], item.payload["payload"]).get("author_key") == decision.author_key
                        and cast(Mapping[str, object], item.payload["payload"]).get("publication_key")
                        == decision.publication_key
                        for item in ordered_inputs
                    )
                    request_inputs = [
                        item.payload.get("payload")
                        for item in ordered_inputs
                        if item.kind is EvidenceKind.APPLICABILITY
                        and item.stable_key == f"request:{candidate_request}"
                        and isinstance(item.payload.get("payload"), Mapping)
                    ]
                    requested = (
                        cast(Mapping[str, object], request_inputs[0]).get("identity")
                        if len(request_inputs) == 1
                        else None
                    )
                    relevant_field = isinstance(requested, Mapping) and decision.field_name in cast(
                        Sequence[object], requested.get("requested_fields", ())
                    )
                    if has_consumer and relevant_field:
                        eligible_identities.add((candidate.kind.value, candidate.stable_key, candidate.source_digest))
            if member_identities != eligible_identities:
                raise ValueError("provenance alternatives are incomplete or include ineligible evidence")
        expected_links = {
            (intent.key, decision.key)
            for intent in ordered_intents
            if intent.kind not in {IntentKind.REMOVE}
            for decision in ordered_decisions
            if (decision.author_key, decision.publication_key) == (intent.author_key, intent.publication_key)
        }
        if set(links) != expected_links or len(links) != len(set(links)):
            raise ValueError("intent provenance membership mismatch")
        for intent in ordered_intents:
            intent_decisions = [
                decision
                for decision in ordered_decisions
                if (decision.author_key, decision.publication_key) == (intent.author_key, intent.publication_key)
            ]
            if intent.kind in {IntentKind.REMOVE} and (intent_decisions or intent.final_fields):
                raise ValueError("no-output intent cannot carry emitted-field provenance")
            if intent.kind not in {IntentKind.REMOVE} and {decision.field_name for decision in intent_decisions} != set(
                intent.final_fields
            ):
                raise ValueError("provenance decisions do not cover exact final emitted field set")
            publication_inputs = [
                item.payload.get("payload")
                for item in ordered_inputs
                if item.kind is EvidenceKind.PUBLICATION
                and isinstance(item.payload.get("payload"), Mapping)
                and cast(Mapping[str, object], item.payload["payload"]).get("author_key") == intent.author_key
                and cast(Mapping[str, object], item.payload["payload"]).get("publication_key") == intent.publication_key
            ]
            if len(publication_inputs) != 1:
                raise ValueError("intent lacks exact final publication input")
            publication_payload = cast(Mapping[str, object], publication_inputs[0])
            required_fields = {
                key
                for key, value in {
                    "title": publication_payload.get("normalized_title"),
                    "year": publication_payload.get("year"),
                }.items()
                if value not in {None, ""}
            }
            identifiers = publication_payload.get("exact_identifiers", {})
            if isinstance(identifiers, Mapping):
                required_fields.update(str(key) for key, value in identifiers.items() if value not in {None, ""})
            if intent.kind not in {IntentKind.REMOVE} and set(intent.final_fields) != required_fields:
                raise ValueError("intent final fields do not match code-owned publication field set")
            linked = sorted(decision_key for intent_key, decision_key in links if intent_key == intent.key)
            if intent.kind in {IntentKind.REMOVE}:
                if linked or intent.provenance_set_digest != evidence_digest(()):
                    raise ValueError("no-output intent requires empty emitted-field provenance")
            elif (intent.final_fields and not linked) or evidence_digest(linked) != intent.provenance_set_digest:
                raise ValueError("intent provenance-set digest mismatch")
            if intent.kind is IntentKind.REMOVE:
                if intent.source_path.casefold() != intent.target_path.casefold():
                    raise ValueError("REMOVE intent requires the same source and target path")
                matching_removals = [
                    cast(Mapping[str, object], item.payload["payload"])
                    for item in ordered_inputs
                    if item.kind is EvidenceKind.CORPUS
                    and isinstance(item.payload.get("payload"), Mapping)
                    and str(cast(Mapping[str, object], item.payload["payload"]).get("source_path", "")).casefold()
                    == intent.source_path.casefold()
                    and intent.publication_key
                    in cast(
                        Sequence[object],
                        cast(Mapping[str, object], item.payload["payload"]).get("publication_keys", ()),
                    )
                ]
                if (
                    len(matching_removals) != 1
                    or matching_removals[0].get("author_key") != intent.author_key
                    or matching_removals[0].get("before_digest") != intent.before_digest
                ):
                    raise ValueError("REMOVE intent lacks exact corpus removal proof")
            matching_corpus = [
                cast(Mapping[str, object], item.payload["payload"])
                for item in ordered_inputs
                if item.kind is EvidenceKind.CORPUS
                and isinstance(item.payload.get("payload"), Mapping)
                and str(cast(Mapping[str, object], item.payload["payload"])["source_path"]).casefold()
                == intent.source_path.casefold()
            ]
            if intent.kind in {IntentKind.KEEP, IntentKind.UPSERT}:
                expected_before = matching_corpus[0].get("before_digest") if matching_corpus else None
                if intent.before_digest != expected_before:
                    raise ValueError("materialization before digest does not match corpus evidence")
        with self._transaction(immediate=True) as connection, self._authority_write():
            self._commit_aggregate_inputs(
                connection, generation_id, pass_key, next(iter(reduction_ids)), ordered_inputs
            )
            self._inject("after_v6_aggregate_inputs")
            for decision in ordered_decisions:
                content = evidence_json(decision.canonical_content())
                existing = connection.execute(
                    "SELECT evidence_json FROM provenance_decisions WHERE generation_id = ? AND decision_key = ?",
                    (generation_id, decision.key),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != content:
                        raise ValueError("conflicting provenance decision replay")
                    continue
                connection.execute(
                    "INSERT INTO provenance_decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        decision.key,
                        decision.pass_key,
                        decision.author_key,
                        decision.publication_key,
                        decision.field_name,
                        decision.selected_value_digest,
                        decision.rule,
                        decision.contribution_set_digest,
                        decision.reducer_id,
                        decision.reducer_version,
                        content,
                    ),
                )
            self._inject("after_v6_provenance_decisions")
            for contribution in ordered_contributions:
                content = evidence_json(contribution.canonical_content())
                existing = connection.execute(
                    "SELECT evidence_json FROM provenance_contributions WHERE generation_id = ? "
                    "AND contribution_key = ?",
                    (generation_id, contribution.key),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != content:
                        raise ValueError("conflicting provenance contribution replay")
                    continue
                connection.execute(
                    "INSERT INTO provenance_contributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        contribution.key,
                        contribution.decision_key,
                        contribution.source_kind,
                        contribution.provider,
                        contribution.schema_version,
                        contribution.request_key,
                        contribution.observation_digest,
                        contribution.value_digest,
                        int(contribution.selected),
                        contribution.rejection_reason,
                        content,
                    ),
                )
            self._inject("after_v6_provenance_contributions")
            for intent in ordered_intents:
                content = evidence_json(intent.canonical_content())
                existing = connection.execute(
                    "SELECT evidence_json FROM materialization_intents WHERE generation_id = ? AND intent_key = ?",
                    (generation_id, intent.key),
                ).fetchone()
                if existing is not None:
                    if str(existing[0]) != content:
                        raise ValueError("conflicting materialization intent replay")
                    continue
                connection.execute(
                    "INSERT INTO materialization_intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        intent.key,
                        intent.pass_key,
                        intent.author_key,
                        intent.publication_key,
                        intent.source_path,
                        intent.target_path,
                        intent.kind.value,
                        intent.before_digest,
                        intent.after_digest,
                        intent.reducer_id,
                        intent.reducer_version,
                        intent.provenance_set_digest,
                        content,
                        evidence_json(intent.final_fields),
                        intent.final_content_digest,
                        intent.removal_reason,
                    ),
                )
            self._inject("after_v6_materialization_intents")
            for intent_key, decision_key in links:
                existing = connection.execute(
                    "SELECT 1 FROM intent_provenance WHERE generation_id = ? AND intent_key = ? AND decision_key = ?",
                    (generation_id, intent_key, decision_key),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO intent_provenance VALUES (?, ?, ?)",
                        (generation_id, intent_key, decision_key),
                    )
            self._inject("after_v6_intent_provenance")

    def create_or_resume(self, spec: GenerationSpec, census: AuthorCensus) -> None:
        if spec.census.canonical_content() != census.canonical_content():
            raise ValueError("generation census does not match supplied census")
        identity = {
            "adapter_versions": dict(spec.adapter_versions),
            "base_commit": spec.base_commit.strip(),
            "census": census.canonical_content(),
            "refresh_policy_version": spec.refresh_policy_version.strip(),
        }
        identity_json = _canonical(identity)
        census_digest = _digest(census.canonical_content())
        durable_census = self._census_rows(census)
        for row in durable_census:
            _identifier(str(row["row_key"]), "census row key")
            _free_text(str(row["name"]), "author name", required=True)
            _free_text(str(row["normalized_name"]), "normalized author name", required=True)
            if row["scholar_id"] and not is_valid_scholar_id(str(row["scholar_id"])):
                raise ValueError("invalid Scholar identifier")
            if row["dblp_id"] and not is_valid_dblp_id(str(row["dblp_id"])):
                raise ValueError("invalid DBLP identifier")
            _free_text(str(row["exclusion_reason"]), "exclusion reason")
        authors_digest = _digest(durable_census)
        base_commit = _identifier(spec.base_commit.strip(), "base commit")
        policy_version = _identifier(spec.refresh_policy_version.strip(), "refresh policy version")
        adapters = dict(spec.adapter_versions)
        for provider_name, adapter_version in adapters.items():
            _provider(provider_name)
            _identifier(adapter_version, "adapter version")
        policy_digest = _digest(policy_version)
        adapter_digest = _digest(dict(sorted(adapters.items())))
        created_at = _timestamp(datetime.now(timezone.utc))
        with self._transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT generation_id, identity_json, census_digest, authors_digest FROM generations"
            ).fetchall()
            if existing:
                row = existing[0]
                if len(existing) != 1 or row[0] != spec.id or row[1] != identity_json:
                    raise ValueError("generation identity mismatch")
                if row[2] != census_digest:
                    raise ValueError("census mismatch")
                stored_rows = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT row_key, physical_row, name, normalized_name, scholar_id, dblp_id, enabled, "
                        "exclusion_reason, disposition FROM authors WHERE generation_id = ? ORDER BY row_key",
                        (spec.id,),
                    )
                ]
                if row[3] != authors_digest or _digest(stored_rows) != authors_digest or stored_rows != durable_census:
                    raise ValueError("durable census mismatch")
                self._verify_plan_integrity(connection, spec.id)
                return
            connection.execute(
                "INSERT INTO generations(generation_id, identity_json, census_digest, authors_digest, base_commit, "
                "input_digest, policy_digest, adapter_digest, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    spec.id,
                    identity_json,
                    census_digest,
                    authors_digest,
                    base_commit,
                    census_digest,
                    policy_digest,
                    adapter_digest,
                    GenerationState.PLANNING.value,
                    created_at,
                    created_at,
                ),
            )
            connection.executemany(
                "INSERT INTO authors(generation_id, row_key, physical_row, name, normalized_name, scholar_id, "
                "dblp_id, enabled, exclusion_reason, disposition) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        spec.id,
                        row.row_key,
                        row.physical_row,
                        row.name,
                        row.normalized_name,
                        row.scholar_id,
                        row.dblp_id,
                        int(row.enabled),
                        row.exclusion_reason,
                        row.disposition.value,
                    )
                    for row in census.rows
                ],
            )

    def transition_generation(
        self,
        expected: GenerationState,
        new: GenerationState,
        now: datetime,
        *,
        blocking_reason: str = "",
    ) -> None:
        if new not in _LEGAL_GENERATION_TRANSITIONS[expected]:
            raise ValueError(f"illegal generation transition from {expected.value} to {new.value}")
        safe_reason = _free_text(blocking_reason, "blocking reason", required=new is GenerationState.BLOCKED)
        if new is not GenerationState.BLOCKED and safe_reason:
            raise ValueError("blocking reason only applies to blocked generation state")
        now_text = _timestamp(now)
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            self._verify_plan_integrity(connection, generation_id)
            if expected is GenerationState.PLANNING and new is GenerationState.RUNNING:
                initial_round = connection.execute(
                    "SELECT COUNT(*) FROM plan_rounds WHERE generation_id = ? AND sequence = 1", (generation_id,)
                ).fetchone()[0]
                if not initial_round:
                    raise ValueError("generation cannot run before committed initial round")
            if new is GenerationState.VALIDATING and not self._all_required_satisfied(connection, generation_id):
                raise ValueError("generation cannot validate before authoritative discovery closure")
            if new is GenerationState.COMPLETE:
                incomplete = connection.execute(
                    "SELECT COUNT(*) FROM validation_obligations AS obligation LEFT JOIN validations AS validation "
                    "ON validation.generation_id = obligation.generation_id AND validation.check_name = "
                    "obligation.check_name WHERE obligation.generation_id = ? AND obligation.required = 1 "
                    "AND (validation.check_name IS NULL OR validation.state != 'succeeded')",
                    (generation_id,),
                ).fetchone()[0]
                if incomplete:
                    raise ValueError("generation cannot complete without successful validation evidence")
                unexpected_failures = connection.execute(
                    "SELECT COUNT(*) FROM validations WHERE generation_id = ? AND state != 'succeeded'",
                    (generation_id,),
                ).fetchone()[0]
                if unexpected_failures:
                    raise ValueError("generation has failed or pending validation evidence")
                current_binding = connection.execute(
                    "SELECT digest FROM manifests WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
                    (generation_id,),
                ).fetchone()
                materialization = connection.execute(
                    "SELECT COUNT(*) FROM materializations WHERE generation_id = ? AND validation_state = ? "
                    "AND manifest_digest = ?",
                    (generation_id, EvidenceState.VALIDATED.value, current_binding[0] if current_binding else ""),
                ).fetchone()[0]
                if not materialization:
                    raise ValueError("generation cannot complete without bound validated materialization")
                completed_manifest_digest = current_binding[0]
            else:
                completed_manifest_digest = None
            if new is GenerationState.PUBLISHED:
                published = connection.execute(
                    "SELECT COUNT(*) FROM publication_evidence AS publication JOIN materializations AS materialization "
                    "ON materialization.generation_id = publication.generation_id AND materialization.manifest_digest "
                    "= publication.manifest_digest JOIN generations AS generation ON generation.generation_id = "
                    "publication.generation_id WHERE publication.generation_id = ? AND publication.kind = "
                    "'verified_merge' AND materialization.validation_state = ? AND publication.manifest_digest = "
                    "generation.completed_manifest_digest AND publication.candidate_digest = "
                    "generation.completed_manifest_digest",
                    (generation_id, EvidenceState.VALIDATED.value),
                ).fetchone()[0]
                if not published:
                    raise ValueError("generation cannot publish without exact verified merge evidence")
            completed_at = now_text if new is GenerationState.COMPLETE else None
            published_at = now_text if new is GenerationState.PUBLISHED else None
            cursor = connection.execute(
                "UPDATE generations SET state = ?, updated_at = ?, completed_at = COALESCE(?, completed_at), "
                "published_at = COALESCE(?, published_at), blocking_reason = ?, "
                "completed_manifest_digest = COALESCE(?, completed_manifest_digest) "
                "WHERE generation_id = ? AND state = ?",
                (
                    new.value,
                    now_text,
                    completed_at,
                    published_at,
                    safe_reason,
                    completed_manifest_digest,
                    generation_id,
                    expected.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("generation state transition rejected")

    @staticmethod
    def _census_rows(census: AuthorCensus) -> list[dict[str, object]]:
        return [
            {
                "row_key": row.row_key,
                "physical_row": row.physical_row,
                "name": row.name,
                "normalized_name": row.normalized_name,
                "scholar_id": row.scholar_id,
                "dblp_id": row.dblp_id,
                "enabled": int(row.enabled),
                "exclusion_reason": row.exclusion_reason,
                "disposition": row.disposition.value,
            }
            for row in sorted(census.rows, key=lambda item: item.row_key)
        ]

    @staticmethod
    def _publication_content(publication: PublicationMetadata) -> dict[str, object]:
        return {
            "author_key": publication.author_key,
            "publication_key": publication.publication_key,
            "discovery_source": publication.discovery_source,
            "normalized_title": publication.normalized_title,
            "year": publication.year,
            "exact_identifiers": dict(sorted(publication.exact_identifiers.items())),
            "baseline_output_path": publication.baseline_output_path,
            "freshness_policy": publication.freshness_policy,
        }

    @classmethod
    def _round_content(
        cls,
        sequence: int,
        phase: PlanPhase,
        planner_id: str,
        planner_version: str,
        source_task_keys: Sequence[str],
        source_evidence_digest: str,
        publications: Sequence[PublicationMetadata],
        tasks: Sequence[PlannedTask],
    ) -> dict[str, object]:
        task_content = [
            {"expands_plan": item.expands_plan, "identity": item.task.identity_digest, "task_key": item.task.key}
            for item in sorted(tasks, key=lambda item: item.task.key)
        ]
        publication_content = [
            cls._publication_content(item)
            for item in sorted(publications, key=lambda item: (item.author_key, item.publication_key))
        ]
        return {
            "phase": phase.value,
            "planner_id": _identifier(planner_id, "planner identifier"),
            "planner_version": _identifier(planner_version, "planner version"),
            "publications": publication_content,
            "sequence": sequence,
            "source_evidence_digest": _digest_text(source_evidence_digest, "source evidence digest"),
            "source_task_keys": sorted(_digest_text(key, "source task key") for key in source_task_keys),
            "task_set_digest": _digest(task_content),
            "tasks": task_content,
        }

    @staticmethod
    def _insert_task(
        connection: sqlite3.Connection,
        generation_id: str,
        task: TaskSpec,
        fault_callback: Callable[[str], None] | None = None,
    ) -> None:
        request_key = task.request.key if task.request is not None else None
        if task.request is not None:
            identity = _canonical(task.request.canonical_content())
            connection.execute(
                "INSERT OR IGNORE INTO requests(generation_id, request_key, identity_json, state) VALUES (?, ?, ?, ?)",
                (generation_id, request_key, identity, TaskDisposition.PENDING.value),
            )
            stored = connection.execute(
                "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_key),
            ).fetchone()
            if stored is None or stored[0] != identity:
                raise ValueError("exact request identity collision")
            if fault_callback is not None:
                fault_callback("after_c4_requests")
        if task.publication_key is not None:
            publication = connection.execute(
                "SELECT 1 FROM publications WHERE generation_id = ? AND author_key = ? AND publication_key = ?",
                (generation_id, task.author_key, task.publication_key),
            ).fetchone()
            if publication is None:
                raise ValueError("phased publication task requires committed publication metadata")
        connection.execute(
            "INSERT INTO tasks(generation_id, task_key, author_key, publication_key, provider, operation, "
            "request_key, required, applicability, identity_digest, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generation_id,
                task.key,
                task.author_key,
                task.publication_key,
                task.provider,
                task.operation,
                request_key,
                int(task.required),
                task.applicability,
                task.identity_digest,
                TaskDisposition.PENDING.value,
            ),
        )
        if fault_callback is not None:
            fault_callback("after_c4_tasks")
        if request_key is not None:
            connection.execute(
                "INSERT INTO request_consumers(generation_id, request_key, task_key) VALUES (?, ?, ?)",
                (generation_id, request_key, task.key),
            )
            if fault_callback is not None:
                fault_callback("after_c4_consumers")

    def _validate_mandatory_inventory(
        self, connection: sqlite3.Connection, generation_id: str, tasks: Sequence[TaskSpec]
    ) -> str | None:
        generation_identity = json.loads(
            connection.execute(
                "SELECT identity_json FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()[0]
        )
        mandatory = []
        for row in connection.execute(
            "SELECT row_key, scholar_id, dblp_id FROM authors WHERE generation_id = ? AND enabled = 1",
            (generation_id,),
        ):
            for provider, profile_id in (("scholar", row["scholar_id"]), ("dblp", row["dblp_id"])):
                if profile_id:
                    mandatory.append((row["row_key"], provider, profile_id))
        declared_inventory = [task for task in tasks if task.operation == "inventory"]
        if not mandatory:
            if declared_inventory:
                raise ValueError("declared tasks do not match full canonical inventory obligations")
            return None
        epochs = {task.request.freshness_epoch for task in declared_inventory if task.request is not None}
        if len(epochs) != 1:
            raise ValueError("canonical inventory obligations require one explicit freshness epoch")
        epoch = next(iter(epochs))
        canonical_keys = []
        for author_key, provider, profile_id in mandatory:
            version = generation_identity["adapter_versions"].get(provider)
            if version is None:
                raise ValueError(f"generation lacks adapter version for inventory provider {provider}")
            matches = [
                task
                for task in declared_inventory
                if task.author_key == author_key and task.provider == provider and task.publication_key is None
            ]
            if len(matches) != 1 or matches[0].request is None:
                raise ValueError("declared tasks do not match full canonical inventory obligations")
            task = matches[0]
            request = task.request
            if request is None:
                raise ValueError("declared inventory lacks exact request")
            payload = dict(request.normalized_payload)
            legacy_request = RequestSpec(
                provider,
                "inventory",
                "GET",
                {"profile_id": profile_id},
                ("publications",),
                version,
                epoch,
                provider,
            )
            if request.key == legacy_request.key:
                canonical_keys.append(task.key)
                continue
            identifier_matches = (
                payload.get("profile_id") == profile_id if provider == "scholar" else payload.get("pid") == profile_id
            )
            expected_fields = ("articles",)
            if (
                request.adapter_version != version
                or request.freshness_epoch != epoch
                or request.quota_scope != ("serpapi" if provider == "scholar" else "dblp")
                or request.requested_fields != expected_fields
                or payload.get("author_key") != author_key
                or not identifier_matches
                or (
                    provider == "scholar"
                    and (
                        payload.get("start") != 0
                        or payload.get("num") != 100
                        or payload.get("sort") != "pubdate"
                        or not isinstance(payload.get("min_year"), int)
                    )
                )
            ):
                raise ValueError("declared tasks do not match full canonical inventory obligations")
            canonical_keys.append(task.key)
        if sorted(task.key for task in declared_inventory) != sorted(canonical_keys):
            raise ValueError("declared tasks do not match full canonical inventory obligations")
        return epoch

    def commit_initial_round(
        self,
        tasks: Sequence[PlannedTask],
        *,
        source_evidence_digest: str,
        publications: Sequence[PublicationMetadata] = (),
        now: datetime,
        inventory_authority: Mapping[str, object] | None = None,
    ) -> PlanRound:
        generation_id = self._generation_id()
        canonical_authority: dict[str, object] | None = None
        if inventory_authority is not None:
            canonical_authority = _inventory_authority_content(inventory_authority, generation_id)
            if _digest(canonical_authority) != source_evidence_digest:
                raise ValueError("initial round source and typed authority digests differ")
        if len({item.task.key for item in tasks}) != len(tasks):
            raise ValueError("duplicate initial round task")
        content = self._round_content(
            1,
            PlanPhase.INVENTORIES,
            "inventory_planner",
            "1",
            (),
            source_evidence_digest,
            publications,
            tasks,
        )
        content_digest = _digest(content)
        round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state, plan_closed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.PLANNING.value or generation[1]:
                raise ValueError("initial round requires open planning generation")
            existing = connection.execute(
                "SELECT round_key, content_digest FROM plan_rounds WHERE generation_id = ? AND sequence = 1",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                if existing[0] != round_key or existing[1] != content_digest:
                    raise ValueError("conflicting initial round replay")
                return self._load_round(connection, generation_id, 1)
            if any(item.task.operation == "inventory" and not item.expands_plan for item in tasks):
                raise ValueError("every mandatory inventory must expand the plan")
            epoch = self._validate_mandatory_inventory(connection, generation_id, [item.task for item in tasks])
            if canonical_authority is not None:
                self._validate_inventory_authority_registry(connection, generation_id, canonical_authority)
            discovery = connection.execute(
                "SELECT policy_json, policy_digest FROM discovery_policy_authority WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if discovery is not None:
                try:
                    discovery_content = json.loads(str(discovery[0]))
                except json.JSONDecodeError as exc:
                    raise ValueError("stored discovery policy authority is malformed") from exc
                discovery_policy = discovery_content.get("policy") if isinstance(discovery_content, Mapping) else None
                inventory_policy = canonical_authority.get("policy") if canonical_authority is not None else None
                seed_versions = (
                    inventory_policy.get("seed_adapter_versions") if isinstance(inventory_policy, Mapping) else None
                )
                adapters = discovery_policy.get("adapter_versions") if isinstance(discovery_policy, Mapping) else None
                if (
                    not isinstance(discovery_policy, Mapping)
                    or _digest(discovery_content) != discovery[1]
                    or discovery_policy.get("freshness_epoch") != epoch
                    or not isinstance(inventory_policy, Mapping)
                    or discovery_policy.get("max_scholar_pages") != inventory_policy.get("max_scholar_pages")
                    or not isinstance(seed_versions, Mapping)
                    or not isinstance(adapters, Mapping)
                    or seed_versions.get("doi_csl") != adapters.get("doi_csl")
                    or seed_versions.get("s2") != adapters.get("s2")
                ):
                    raise ValueError("inventory authority conflicts with bound discovery policy")
            for publication in publications:
                self._insert_publication(connection, generation_id, publication)
            self._inject("after_initial_round_publications")
            for item in sorted(tasks, key=lambda value: value.task.key):
                self._insert_task(connection, generation_id, item.task)
            self._inject("after_initial_round_tasks")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability, round_sequence, expands_plan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                [
                    (
                        generation_id,
                        item.task.key,
                        item.task.identity_digest,
                        item.task.author_key,
                        item.task.provider,
                        item.task.operation,
                        int(item.task.required),
                        item.task.applicability,
                        int(item.expands_plan),
                    )
                    for item in sorted(tasks, key=lambda value: value.task.key)
                ],
            )
            self._inject("after_initial_round_obligations")
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, 1, ?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                (
                    generation_id,
                    round_key,
                    PlanPhase.INVENTORIES.value,
                    "inventory_planner",
                    "1",
                    source_evidence_digest,
                    cast(str, content["task_set_digest"]),
                    content_digest,
                    _timestamp(now),
                ),
            )
            if canonical_authority is not None:
                authority_json = _canonical(canonical_authority)
                connection.execute(
                    "INSERT INTO inventory_policy_authority(generation_id, authority_json, authority_digest) "
                    "VALUES (?, ?, ?)",
                    (generation_id, authority_json, _digest(json.loads(authority_json))),
                )
            connection.executemany(
                "INSERT INTO round_publications(generation_id, round_sequence, author_key, publication_key) "
                "VALUES (?, 1, ?, ?)",
                [(generation_id, publication.author_key, publication.publication_key) for publication in publications],
            )
            self._inject("after_initial_round_round")
            connection.execute(
                "UPDATE generations SET plan_revision = 1, plan_digest = ?, inventory_freshness_epoch = ?, "
                "updated_at = ? WHERE generation_id = ?",
                (_digest([content_digest]), epoch, _timestamp(now), generation_id),
            )
        self._inject("after_initial_round_commit")
        return self._load_round(self._connection, generation_id, 1)

    @staticmethod
    def _insert_publication(
        connection: sqlite3.Connection, generation_id: str, publication: PublicationMetadata
    ) -> None:
        connection.execute(
            "INSERT INTO publications(generation_id, author_key, publication_key, discovery_source, normalized_title, "
            "year, exact_identifiers_json, baseline_output_path, freshness_policy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                generation_id,
                publication.author_key,
                publication.publication_key,
                publication.discovery_source,
                publication.normalized_title,
                publication.year,
                _canonical(dict(publication.exact_identifiers)),
                publication.baseline_output_path,
                publication.freshness_policy,
            ),
        )

    def _load_round(self, connection: sqlite3.Connection, generation_id: str, sequence: int) -> PlanRound:
        row = connection.execute(
            "SELECT * FROM plan_rounds WHERE generation_id = ? AND sequence = ?", (generation_id, sequence)
        ).fetchone()
        if row is None:
            raise ValueError("missing plan round")
        planned = []
        for task_row in connection.execute(
            "SELECT task_key, expands_plan FROM plan_obligations WHERE generation_id = ? AND round_sequence = ? "
            "ORDER BY task_key",
            (generation_id, sequence),
        ):
            planned.append(PlannedTask(self._load_task(connection, generation_id, task_row[0]), bool(task_row[1])))
        publications = tuple(
            PublicationMetadata(
                publication["author_key"],
                publication["publication_key"],
                publication["discovery_source"],
                publication["normalized_title"],
                publication["year"],
                MappingProxyType(json.loads(publication["exact_identifiers_json"])),
                publication["baseline_output_path"],
                publication["freshness_policy"],
            )
            for publication in connection.execute(
                "SELECT publication.* FROM round_publications AS binding JOIN publications AS publication ON "
                "publication.generation_id = binding.generation_id AND publication.author_key = binding.author_key "
                "AND publication.publication_key = binding.publication_key WHERE binding.generation_id = ? "
                "AND binding.round_sequence = ? ORDER BY publication.author_key, publication.publication_key",
                (generation_id, sequence),
            )
        )
        return PlanRound(
            row["round_key"],
            row["sequence"],
            PlanPhase(row["phase"]),
            row["planner_id"],
            row["planner_version"],
            tuple(json.loads(row["source_task_keys_json"])),
            row["source_evidence_digest"],
            publications,
            tuple(planned),
            row["task_set_digest"],
            row["content_digest"],
        )

    @staticmethod
    def _load_task(connection: sqlite3.Connection, generation_id: str, task_key: str) -> TaskSpec:
        row = connection.execute(
            "SELECT task.*, request.identity_json FROM tasks AS task LEFT JOIN requests AS request ON "
            "request.generation_id = task.generation_id AND request.request_key = task.request_key "
            "WHERE task.generation_id = ? AND task.task_key = ?",
            (generation_id, task_key),
        ).fetchone()
        if row is None:
            raise ValueError("missing task")
        request = RequestSpec(**json.loads(row["identity_json"])) if row["identity_json"] else None
        return TaskSpec(
            row["author_key"],
            row["publication_key"],
            row["provider"],
            row["operation"],
            request,
            bool(row["required"]),
            row["applicability"],
        )

    def plan_status(self) -> PlanStatus:
        generation_id = self._generation_id()
        self._assert_task5a_authority_invariant(self._connection)
        row = self._connection.execute(
            "SELECT plan_revision, plan_closed, discovery_closed, plan_authority_mode, plan_digest, closure_digest "
            "FROM generations "
            "WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        open_expanders = self._connection.execute(
            "SELECT COUNT(*) FROM plan_obligations AS obligation LEFT JOIN reduction_sources AS source ON "
            "source.generation_id = obligation.generation_id AND source.source_task_key = obligation.task_key "
            "WHERE obligation.generation_id = ? AND obligation.expands_plan = 1 AND source.source_task_key IS NULL",
            (generation_id,),
        ).fetchone()[0]
        unbound = self._connection.execute(
            "SELECT COUNT(*) FROM tasks AS task LEFT JOIN plan_obligations AS obligation ON obligation.generation_id = "
            "task.generation_id AND obligation.task_key = task.task_key WHERE task.generation_id = ? "
            "AND obligation.task_key IS NULL",
            (generation_id,),
        ).fetchone()[0]
        return PlanStatus(row[0], bool(row[1]), bool(row[2]), row[3], row[4], row[5], int(open_expanders), int(unbound))

    def generation_state(self) -> GenerationState:
        row = self._connection.execute(
            "SELECT state FROM generations WHERE generation_id = ?", (self._generation_id(),)
        ).fetchone()
        if row is None:
            raise ValueError("generation state is missing")
        return GenerationState(str(row[0]))

    def assert_initial_inventory_authority(self, evidence_digest: str) -> None:
        """Require the initial round to bind the exact current policy and capabilities."""
        evidence_digest = _digest_text(evidence_digest, "initial inventory authority digest")
        row = self._connection.execute(
            "SELECT source_evidence_digest FROM plan_rounds WHERE generation_id = ? AND sequence = 1",
            (self._generation_id(),),
        ).fetchone()
        if row is None or row[0] != evidence_digest:
            raise ValueError("inventory policy or capability authority mismatch")

    def assert_typed_inventory_authority(self, authority: Mapping[str, object]) -> None:
        canonical = _canonical(_inventory_authority_content(authority, self._generation_id()))
        stored = self._load_inventory_policy_authority(self._connection, self._generation_id())
        if stored is None or _canonical(stored["authority"]) != canonical:
            raise ValueError("typed inventory policy authority mismatch")

    @staticmethod
    def _validate_inventory_authority_registry(
        connection: sqlite3.Connection, generation_id: str, authority: Mapping[str, object]
    ) -> None:
        from .inventory import capability_for

        identity_row = connection.execute(
            "SELECT identity_json FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if identity_row is None:
            raise ValueError("generation authority is missing")
        adapter_versions = json.loads(identity_row[0])["adapter_versions"]
        policy = cast(Mapping[str, object], authority["policy"])
        seed_versions = cast(Mapping[str, str], policy["seed_adapter_versions"])
        if any(adapter_versions.get(source) != seed_versions[source] for source in ("doi_csl", "s2")):
            raise ValueError("typed inventory seed authority does not match generation")
        sources: set[str] = set()
        for row in connection.execute(
            "SELECT scholar_id, dblp_id FROM authors WHERE generation_id = ? AND enabled = 1", (generation_id,)
        ):
            if row[0]:
                sources.add("scholar")
            if row[1]:
                sources.add("dblp")
        if any(source not in adapter_versions for source in sources):
            raise ValueError("typed inventory source authority lacks an adapter version")
        expected = [capability_for(source, "inventory", adapter_versions[source]) for source in sorted(sources)] + [
            capability_for("doi_csl", "csl_lookup", seed_versions["doi_csl"]),
            capability_for("s2", "fuzzy_search", seed_versions["s2"]),
        ]
        expected_content = [
            cast(dict[str, object], _plain_json(capability.canonical_content()))
            for capability in sorted(expected, key=lambda item: item.capability_id)
        ]
        if authority["capabilities"] != expected_content:
            raise ValueError("typed inventory capabilities do not match the code-owned registry")
        if authority["planner_version"] != "1" or authority["reducer_version"] != "1":
            raise ValueError("typed inventory planner or reducer version is unsupported")

    @staticmethod
    def _load_inventory_policy_authority(
        connection: sqlite3.Connection, generation_id: str
    ) -> dict[str, object] | None:
        row = connection.execute(
            "SELECT authority_json, authority_digest FROM inventory_policy_authority WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            raw = json.loads(row["authority_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("stored inventory policy authority is malformed") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("stored inventory policy authority is malformed")
        authority = _inventory_authority_content(raw, generation_id)
        Ledger._validate_inventory_authority_registry(connection, generation_id, authority)
        digest = _digest(authority)
        initial = connection.execute(
            "SELECT source_evidence_digest FROM plan_rounds WHERE generation_id = ? AND sequence = 1",
            (generation_id,),
        ).fetchone()
        if row["authority_json"] != _canonical(authority) or row["authority_digest"] != digest:
            raise ValueError("stored inventory policy authority integrity mismatch")
        if initial is None or initial[0] != digest:
            raise ValueError("stored inventory policy authority is not bound to the initial round")
        return {"authority": authority, "authority_digest": digest}

    @staticmethod
    def _source_evidence(
        connection: sqlite3.Connection, generation_id: str, task_key: str
    ) -> tuple[TaskDisposition, str]:
        row = connection.execute(
            "SELECT task.state, task.applicability_reason, observation.response_digest, observation.response_json, "
            "dominance.rule, "
            "dominance.stronger_observations_json, dominance.dominated_observation_key, dominance.covered_fields_json "
            "FROM tasks AS task LEFT JOIN observations AS observation ON "
            "observation.generation_id = task.generation_id "
            "AND observation.request_key = task.request_key LEFT JOIN dominance_evidence AS dominance ON "
            "dominance.generation_id = task.generation_id AND dominance.task_key = task.task_key "
            "WHERE task.generation_id = ? AND task.task_key = ?",
            (generation_id, task_key),
        ).fetchone()
        if row is None or TaskDisposition(row[0]) not in _SATISFIED:
            raise ValueError("reduction source is not satisfied")
        disposition = TaskDisposition(row[0])
        if disposition in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}:
            if not row[2]:
                raise ValueError("reduction source lacks durable observation evidence")
            if row[3] is None or _digest(json.loads(row[3])) != row[2]:
                raise ValueError("reduction source observation digest mismatch")
            evidence_digest = row[2]
        elif disposition is TaskDisposition.NOT_APPLICABLE:
            evidence_digest = _digest({"applicability_reason": row[1], "task_key": task_key})
        else:
            if not row[4]:
                raise ValueError("dominated reduction source lacks durable evidence")
            evidence_digest = _digest(
                {
                    "covered_fields": json.loads(row[7]),
                    "dominated_observation_key": row[6],
                    "rule": row[4],
                    "stronger_observations": json.loads(row[5]),
                    "task_key": task_key,
                }
            )
        return disposition, evidence_digest

    def commit_reduction(
        self,
        source_task_key: str | Sequence[str],
        *,
        source_evidence_digest: str,
        publications: Sequence[PublicationMetadata],
        tasks: Sequence[PlannedTask],
        now: datetime,
        phase: PlanPhase = PlanPhase.DISCOVERY,
        reducer_id: str = "discovery_reducer",
        reducer_version: str = "1",
        _inventory_authorities: Sequence[tuple[object, str, str, str]] = (),
        _applicability_reasons: Mapping[str, ApplicabilityReason] = MappingProxyType({}),
        _connection: sqlite3.Connection | None = None,
        _allow_empty_sources: bool = False,
        _fault_callback: Callable[[str], None] | None = None,
    ) -> ReductionReceipt:
        generation_id = self._generation_id()
        source_keys = (
            (source_task_key,)
            if isinstance(source_task_key, str)
            else tuple(sorted(_digest_text(key, "source task key") for key in source_task_key))
        )
        if (not source_keys and not _allow_empty_sources) or len(set(source_keys)) != len(source_keys):
            raise ValueError("reduction requires unique source tasks")
        if len({item.task.key for item in tasks}) != len(tasks):
            raise ValueError("duplicate reduction task")
        if set(_applicability_reasons) != {item.task.key for item in tasks if item.task.request is None}:
            raise ValueError("reduction applicability evidence membership changed")
        supplied_digest = _digest_text(source_evidence_digest, "source evidence digest") if source_keys else _digest([])
        manager = self._transaction(immediate=True) if _connection is None else nullcontext(_connection)
        with manager as connection:
            if _connection is None:
                reserved = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT json_extract(item.input_json, '$.payload.task_key') "
                        "FROM planner_passes AS pass JOIN planner_pass_expected_items AS item "
                        "ON item.generation_id = pass.generation_id AND item.pass_key = pass.pass_key "
                        "WHERE pass.generation_id = ? AND pass.pass_id IN "
                        "('known_doi','broad_discovery','dynamic_expansion') AND item.kind = ?",
                        (generation_id, EvidenceKind.APPLICABILITY.value),
                    )
                    if row[0] is not None
                }
                if reserved.intersection(source_keys):
                    raise ValueError("C4 discovery sources require the private atomic reducer")
                potential_adoptees = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT task.task_key FROM tasks AS task JOIN plan_obligations AS obligation "
                        "ON obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE task.generation_id = ? "
                        "AND round.planner_id = 'inventory_union' AND "
                        "((task.provider = 'doi_csl' AND task.operation = 'csl_lookup') OR "
                        "(task.provider = 's2' AND task.operation = 'fuzzy_search'))",
                        (generation_id,),
                    )
                }
                if potential_adoptees.intersection(source_keys):
                    raise ValueError("C4 discovery adoptees require the private atomic reducer")
            generation = connection.execute(
                "SELECT state, plan_closed, plan_revision FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or generation[1]:
                raise ValueError("reduction requires open running plan")
            if int(generation[2]) >= _MAX_PLAN_ROUNDS:
                raise ValueError("plan round policy limit exceeded")
            durable = []
            source_round_phases = set()
            for key in source_keys:
                source = connection.execute(
                    "SELECT obligation.expands_plan, obligation.round_sequence, round.phase FROM plan_obligations AS "
                    "obligation JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id AND "
                    "round.sequence = obligation.round_sequence WHERE obligation.generation_id = ? AND "
                    "obligation.task_key = ?",
                    (generation_id, key),
                ).fetchone()
                if source is None or not source[0]:
                    raise ValueError("reduction requires committed expanding source")
                source_round_phases.add(PlanPhase(source[2]))
                durable.append(self._source_evidence(connection, generation_id, key))
            durable_digests = tuple(item[1] for item in durable)
            aggregate_digest = durable_digests[0] if len(durable_digests) == 1 else _digest(list(durable_digests))
            if supplied_digest != aggregate_digest:
                raise ValueError("reduction evidence digest mismatch")
            allowed_edges = {
                PlanPhase.INVENTORIES: {PlanPhase.DISCOVERY, PlanPhase.AUTHORITATIVE},
                PlanPhase.DISCOVERY: {
                    PlanPhase.DISCOVERY,
                    PlanPhase.AUTHORITATIVE,
                    PlanPhase.LATE_IDENTIFIERS,
                    PlanPhase.REDUCERS,
                },
                PlanPhase.LATE_IDENTIFIERS: {
                    PlanPhase.LATE_IDENTIFIERS,
                    PlanPhase.AUTHORITATIVE,
                    PlanPhase.REDUCERS,
                },
                PlanPhase.AUTHORITATIVE: {
                    PlanPhase.AUTHORITATIVE,
                    PlanPhase.LATE_IDENTIFIERS,
                    PlanPhase.REDUCERS,
                },
                PlanPhase.REDUCERS: {PlanPhase.REDUCERS},
                PlanPhase.CLOSED: set(),
            }
            if any(phase not in allowed_edges[source_phase] for source_phase in source_round_phases):
                raise ValueError("invalid plan phase edge")
            prior = [
                row
                for source_key in source_keys
                if (
                    row := connection.execute(
                        "SELECT source.source_task_key, source.reduction_digest, round.sequence "
                        "FROM reduction_sources AS source JOIN reduction_receipts AS receipt ON "
                        "receipt.generation_id = source.generation_id AND "
                        "receipt.reduction_digest = source.reduction_digest JOIN plan_rounds AS round ON "
                        "round.generation_id = receipt.generation_id AND round.round_key = receipt.round_key "
                        "WHERE source.generation_id = ? AND source.source_task_key = ?",
                        (generation_id, source_key),
                    ).fetchone()
                )
                is not None
            ]
            if prior:
                if len(prior) != len(source_keys) or len({row[1] for row in prior}) != 1:
                    raise ValueError("conflicting reduction replay")
                replay_sequence = int(prior[0][2])
                replay_content = self._round_content(
                    replay_sequence,
                    phase,
                    reducer_id,
                    reducer_version,
                    source_keys,
                    supplied_digest,
                    publications,
                    tasks,
                )
                replay_reduction = _digest(
                    {
                        "content_digest": _digest(replay_content),
                        "source_dispositions": [item[0].value for item in durable],
                        "source_evidence_digests": list(durable_digests),
                        "source_task_keys": list(source_keys),
                    }
                )
                if replay_reduction != prior[0][1]:
                    raise ValueError("conflicting reduction replay")
                return self._load_receipt(connection, generation_id, replay_reduction)
            sequence = int(generation[2]) + 1
            content = self._round_content(
                sequence,
                phase,
                reducer_id,
                reducer_version,
                source_keys,
                supplied_digest,
                publications,
                tasks,
            )
            content_digest = _digest(content)
            round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            reduction_content = {
                "content_digest": content_digest,
                "source_dispositions": [item[0].value for item in durable],
                "source_evidence_digests": list(durable_digests),
                "source_task_keys": list(source_keys),
            }
            reduction_digest = _digest(reduction_content)
            existing_task = any(
                connection.execute(
                    "SELECT 1 FROM tasks WHERE generation_id = ? AND task_key = ?",
                    (generation_id, item.task.key),
                ).fetchone()
                is not None
                for item in tasks
            )
            if existing_task:
                raise ValueError("reduction must append unseen task keys")
            for publication in publications:
                self._insert_publication(connection, generation_id, publication)
            self._inject("after_reduction_publications")
            for item in sorted(tasks, key=lambda value: value.task.key):
                self._insert_task(connection, generation_id, item.task, _fault_callback)
            self._inject("after_reduction_tasks")
            for task_key, reason in sorted(_applicability_reasons.items()):
                updated = connection.execute(
                    "UPDATE tasks SET state = ?, applicability_reason = ? "
                    "WHERE generation_id = ? AND task_key = ? AND request_key IS NULL AND state = ?",
                    (
                        TaskDisposition.NOT_APPLICABLE.value,
                        reason.value,
                        generation_id,
                        task_key,
                        TaskDisposition.PENDING.value,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("reduction applicability terminalization changed")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability, round_sequence, expands_plan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        generation_id,
                        item.task.key,
                        item.task.identity_digest,
                        item.task.author_key,
                        item.task.provider,
                        item.task.operation,
                        int(item.task.required),
                        item.task.applicability,
                        sequence,
                        int(item.expands_plan),
                    )
                    for item in sorted(tasks, key=lambda value: value.task.key)
                ],
            )
            self._inject("after_reduction_obligations")
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    sequence,
                    round_key,
                    phase.value,
                    reducer_id,
                    reducer_version,
                    _canonical(source_keys),
                    supplied_digest,
                    content["task_set_digest"],
                    content_digest,
                    _timestamp(now),
                ),
            )
            connection.executemany(
                "INSERT INTO round_publications(generation_id, round_sequence, author_key, publication_key) "
                "VALUES (?, ?, ?, ?)",
                [(generation_id, sequence, item.author_key, item.publication_key) for item in publications],
            )
            self._inject("after_reduction_round")
            connection.execute(
                "INSERT INTO reduction_receipts(generation_id, reduction_digest, round_key, source_task_keys_json, "
                "source_dispositions_json, source_evidence_digests_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    reduction_digest,
                    round_key,
                    _canonical(source_keys),
                    _canonical([item[0].value for item in durable]),
                    _canonical(durable_digests),
                    _timestamp(now),
                ),
            )
            connection.executemany(
                "INSERT INTO reduction_sources(generation_id, source_task_key, reduction_digest) VALUES (?, ?, ?)",
                [(generation_id, key, reduction_digest) for key in source_keys],
            )
            for snapshot, author_key, policy_digest, semantic_digest in _inventory_authorities:
                from .inventory import InventorySnapshot

                if not isinstance(snapshot, InventorySnapshot):
                    raise TypeError("inventory authority requires a typed snapshot")
                live_snapshot = self.load_inventory_snapshot(author_key)
                if not isinstance(live_snapshot, InventorySnapshot) or live_snapshot.digest != snapshot.digest:
                    raise ValueError("inventory snapshot changed during union commit")
                connection.execute(
                    "INSERT INTO inventory_authorities(generation_id, author_key, reducer_version, policy_digest, "
                    "snapshot_digest, reduction_digest, round_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        author_key,
                        reducer_version,
                        policy_digest,
                        snapshot.digest,
                        semantic_digest,
                        round_key,
                    ),
                )
                connection.executemany(
                    "INSERT INTO inventory_contributions(generation_id, author_key, reducer_version, task_key, "
                    "request_key, capability_id, disposition, decoder_schema, observation_digest, page_offset, "
                    "next_offset, topology_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            generation_id,
                            author_key,
                            reducer_version,
                            item.task_key,
                            item.request_key,
                            item.capability_id,
                            item.disposition.value,
                            item.decoder_schema,
                            item.observation_digest,
                            item.offset,
                            item.next_offset,
                            item.topology_digest,
                        )
                        for item in snapshot.contributions
                    ],
                )
            self._inject("after_reduction_receipt")
            cumulative = _digest(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT content_digest FROM plan_rounds WHERE generation_id = ? ORDER BY sequence",
                        (generation_id,),
                    )
                ]
            )
            connection.execute(
                "UPDATE generations SET plan_revision = ?, plan_digest = ?, updated_at = ? WHERE generation_id = ?",
                (sequence, cumulative, _timestamp(now), generation_id),
            )
        self._inject("after_reduction_commit")
        return self._load_receipt(self._connection, generation_id, reduction_digest)

    def commit_inventory_union(
        self,
        census_row: object,
        policy: object,
        *,
        reducer_version: str,
        now: datetime,
    ) -> ReductionReceipt:
        """Compatibility wrapper for one-author aggregate inventory union."""
        return self.commit_inventory_union_wave((census_row,), policy, reducer_version=reducer_version, now=now)

    def commit_inventory_union_wave(
        self,
        census_rows: Sequence[object],
        policy: object,
        *,
        reducer_version: str,
        now: datetime,
    ) -> ReductionReceipt:
        """Atomically bind every ready author union in one phase wave and round."""
        from .census import AuthorCensusRow
        from .inventory import InventoryPolicy, InventorySnapshot, capability_for, reduce_author_inventory

        if (
            not census_rows
            or not all(isinstance(row, AuthorCensusRow) for row in census_rows)
            or not isinstance(policy, InventoryPolicy)
        ):
            raise TypeError("inventory union requires typed census and policy")
        typed_rows = tuple(cast(AuthorCensusRow, row) for row in census_rows)
        if len({row.row_key for row in typed_rows}) != len(typed_rows):
            raise ValueError("inventory union wave requires unique authors")
        durable_enabled = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT row_key FROM authors WHERE generation_id = ? AND enabled = 1",
                (self._generation_id(),),
            )
        }
        if {row.row_key for row in typed_rows} != durable_enabled:
            raise ValueError("inventory union wave must include every enabled author exactly once")
        reducer_version = _identifier(reducer_version, "inventory reducer version")
        generation_identity = json.loads(
            self._connection.execute(
                "SELECT identity_json FROM generations WHERE generation_id = ?", (self._generation_id(),)
            ).fetchone()[0]
        )
        generation_freshness = self._connection.execute(
            "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?",
            (self._generation_id(),),
        ).fetchone()[0]
        if (
            policy.freshness_epoch != generation_freshness
            or policy.doi_adapter_version != generation_identity["adapter_versions"].get("doi_csl")
            or policy.s2_adapter_version != generation_identity["adapter_versions"].get("s2")
        ):
            raise ValueError("inventory union policy does not match generation authority")
        durable_sources: set[str] = set()
        for author in self._connection.execute(
            "SELECT scholar_id, dblp_id FROM authors WHERE generation_id = ? AND enabled = 1",
            (self._generation_id(),),
        ):
            if author[0]:
                durable_sources.add("scholar")
            if author[1]:
                durable_sources.add("dblp")
        bound_capabilities = [
            capability_for(source, "inventory", generation_identity["adapter_versions"][source])
            for source in sorted(durable_sources)
        ] + [
            capability_for("doi_csl", "csl_lookup", policy.doi_adapter_version),
            capability_for("s2", "fuzzy_search", policy.s2_adapter_version),
        ]
        self.assert_typed_inventory_authority(
            {
                "capabilities": [
                    dict(item.canonical_content())
                    for item in sorted(bound_capabilities, key=lambda item: item.capability_id)
                ],
                "generation": self._generation_id(),
                "planner_version": "1",
                "policy": {
                    "max_publications": policy.max_publications,
                    "max_scholar_pages": policy.max_scholar_pages,
                    "min_year": policy.min_year,
                    "seed_adapter_versions": {
                        "doi_csl": policy.doi_adapter_version,
                        "s2": policy.s2_adapter_version,
                    },
                },
                "reducer_version": reducer_version,
            }
        )
        for census_row in typed_rows:
            durable = self._connection.execute(
                "SELECT physical_row, row_key, name, normalized_name, scholar_id, dblp_id, enabled, "
                "exclusion_reason, disposition FROM authors WHERE generation_id = ? AND row_key = ?",
                (self._generation_id(), census_row.row_key),
            ).fetchone()
            supplied = (
                census_row.physical_row,
                census_row.row_key,
                census_row.name,
                census_row.normalized_name,
                census_row.scholar_id,
                census_row.dblp_id,
                int(census_row.enabled),
                census_row.exclusion_reason,
                census_row.disposition.value,
            )
            if durable is None or tuple(durable) != supplied:
                raise ValueError("inventory union census row substitution rejected")
        policy_digest = _digest(
            {
                "doi_adapter_version": policy.doi_adapter_version,
                "freshness_epoch": policy.freshness_epoch,
                "max_publications": policy.max_publications,
                "max_scholar_pages": policy.max_scholar_pages,
                "min_year": policy.min_year,
                "s2_adapter_version": policy.s2_adapter_version,
            }
        )
        authorities = []
        publications: list[PublicationMetadata] = []
        seed_tasks: list[TaskSpec] = []
        seed_reasons: dict[str, ApplicabilityReason] = {}
        evidence_by_task: dict[str, str] = {}
        existing_rounds = set()
        existing_count = 0
        for census_row in sorted(typed_rows, key=lambda item: item.row_key):
            snapshot = self.load_inventory_snapshot(census_row.row_key)
            if not isinstance(snapshot, InventorySnapshot):
                raise TypeError("ledger failed to reconstruct inventory snapshot")
            reduction = reduce_author_inventory(census_row, snapshot, policy)
            shaped_tasks, shaped_reasons = self._shape_inventory_seed_tasks(
                self._connection, self._generation_id(), reduction.seed_tasks
            )
            if len(reduction.publications) != len(shaped_tasks) or len(
                {item.publication_key for item in shaped_tasks}
            ) != len(shaped_tasks):
                raise ValueError("inventory union must emit exactly one seed per publication")
            for task in shaped_tasks:
                if task.request is None:
                    if task.key not in shaped_reasons:
                        raise ValueError("inventory union seed lacks applicability authority")
                    continue
                capability = capability_for(task.provider, task.operation, task.request.adapter_version)
                if (
                    task.request.requested_fields != capability.requested_fields
                    or generation_identity["adapter_versions"].get(task.provider) != task.request.adapter_version
                ):
                    raise ValueError("inventory seed lacks one exact durable capability")
            semantic_digest = _digest(
                {
                    "policy_digest": policy_digest,
                    "publications": [self._publication_content(item) for item in reduction.publications],
                    "reducer_version": reducer_version,
                    "seed_tasks": [item.identity_digest for item in shaped_tasks],
                    "snapshot_digest": snapshot.digest,
                }
            )
            existing = self._connection.execute(
                "SELECT reduction_digest, round_key FROM inventory_authorities WHERE generation_id = ? "
                "AND author_key = ? AND reducer_version = ?",
                (self._generation_id(), census_row.row_key, reducer_version),
            ).fetchone()
            if existing is not None:
                if existing[0] != semantic_digest:
                    raise ValueError("conflicting inventory union replay")
                existing_rounds.add(str(existing[1]))
                existing_count += 1
            authorities.append((snapshot, census_row.row_key, policy_digest, semantic_digest))
            publications.extend(reduction.publications)
            seed_tasks.extend(shaped_tasks)
            seed_reasons.update(shaped_reasons)
            terminal = [
                item for item in snapshot.contributions if item.logical_source != "scholar" or item.next_offset is None
            ]
            evidence_by_task.update({item.task_key: item.observation_digest for item in terminal})
        if existing_rounds:
            if len(existing_rounds) != 1 or existing_count != len(typed_rows):
                raise ValueError("partial or conflicting inventory union wave replay")
            round_key = next(iter(existing_rounds))
            receipt = self._connection.execute(
                "SELECT reduction_digest FROM reduction_receipts WHERE generation_id = ? AND round_key = ?",
                (self._generation_id(), round_key),
            ).fetchone()
            if receipt is None:
                raise ValueError("inventory authority lacks reduction receipt")
            return self._load_receipt(self._connection, self._generation_id(), str(receipt[0]))
        source_keys = tuple(sorted(evidence_by_task))
        source_digest = (
            evidence_by_task[source_keys[0]]
            if len(source_keys) == 1
            else _digest([evidence_by_task[key] for key in source_keys])
        )
        return self.commit_reduction(
            source_keys,
            source_evidence_digest=source_digest,
            publications=tuple(publications),
            tasks=tuple(PlannedTask(task, expands_plan=True) for task in seed_tasks),
            now=now,
            phase=PlanPhase.DISCOVERY,
            reducer_id="inventory_union",
            reducer_version=reducer_version,
            _inventory_authorities=tuple(authorities),
            _applicability_reasons=seed_reasons,
        )

    @staticmethod
    def _load_receipt(connection: sqlite3.Connection, generation_id: str, reduction_digest: str) -> ReductionReceipt:
        row = connection.execute(
            "SELECT * FROM reduction_receipts WHERE generation_id = ? AND reduction_digest = ?",
            (generation_id, reduction_digest),
        ).fetchone()
        if row is None:
            raise ValueError("missing reduction receipt")
        return ReductionReceipt(
            tuple(json.loads(row["source_task_keys_json"])),
            row["round_key"],
            tuple(TaskDisposition(value) for value in json.loads(row["source_dispositions_json"])),
            tuple(json.loads(row["source_evidence_digests_json"])),
            row["reduction_digest"],
        )

    def closure_content(self, required_validations: Sequence[ValidationSpec] = ()) -> Mapping[str, object]:
        generation_id = self._generation_id()
        connection = self._connection
        self._verify_v6_relationships(connection, generation_id)
        rounds = [
            {
                "content_digest": row["content_digest"],
                "phase": row["phase"],
                "planner_id": row["planner_id"],
                "planner_version": row["planner_version"],
                "round_key": row["round_key"],
                "sequence": row["sequence"],
                "source_evidence_digest": row["source_evidence_digest"],
                "source_task_keys": json.loads(row["source_task_keys_json"]),
                "task_set_digest": row["task_set_digest"],
            }
            for row in connection.execute(
                "SELECT * FROM plan_rounds WHERE generation_id = ? ORDER BY sequence", (generation_id,)
            )
        ]
        obligations = [
            dict(row)
            for row in connection.execute(
                "SELECT task_key, identity_digest, author_key, provider, operation, required, applicability, "
                "round_sequence, expands_plan FROM plan_obligations WHERE generation_id = ? ORDER BY task_key",
                (generation_id,),
            )
        ]
        publications = [
            {
                **{key: value for key, value in dict(row).items() if key != "exact_identifiers_json"},
                "exact_identifiers": json.loads(row["exact_identifiers_json"]),
            }
            for row in connection.execute(
                "SELECT author_key, publication_key, discovery_source, normalized_title, year, exact_identifiers_json, "
                "baseline_output_path, freshness_policy FROM publications WHERE generation_id = ? "
                "ORDER BY author_key, publication_key",
                (generation_id,),
            )
        ]
        receipts = [
            {
                "reduction_digest": row["reduction_digest"],
                "round_key": row["round_key"],
                "source_dispositions": json.loads(row["source_dispositions_json"]),
                "source_evidence_digests": json.loads(row["source_evidence_digests_json"]),
                "source_task_keys": json.loads(row["source_task_keys_json"]),
            }
            for row in connection.execute(
                "SELECT * FROM reduction_receipts WHERE generation_id = ? ORDER BY round_key", (generation_id,)
            )
        ]
        task_outcomes = [
            dict(row)
            for row in connection.execute(
                "SELECT task_key, state, applicability_reason, dominance_reason FROM tasks "
                "WHERE generation_id = ? ORDER BY task_key",
                (generation_id,),
            )
        ]
        observations = [
            dict(row)
            for row in connection.execute(
                "SELECT request_key, disposition, response_digest, provider, schema_version, authoritative_empty "
                "FROM observations WHERE generation_id = ? ORDER BY request_key",
                (generation_id,),
            )
        ]
        dominance = [
            {
                "covered_fields": json.loads(row[4]),
                "dominated_observation_key": row[3],
                "rule": row[2],
                "stronger_observations": json.loads(row[1]),
                "task_key": row[0],
            }
            for row in connection.execute(
                "SELECT task_key, stronger_observations_json, rule, dominated_observation_key, covered_fields_json "
                "FROM dominance_evidence WHERE generation_id = ? ORDER BY task_key",
                (generation_id,),
            )
        ]
        existing_validations = [
            row[0]
            for row in connection.execute(
                "SELECT check_name FROM validation_obligations WHERE generation_id = ? ORDER BY check_name",
                (generation_id,),
            )
        ]
        validation_names = (
            sorted(validation.name for validation in required_validations)
            if required_validations
            else existing_validations
        )
        freshness = connection.execute(
            "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        inventory_policy_authority = self._load_inventory_policy_authority(connection, generation_id)
        return MappingProxyType(
            {
                "generation_id": generation_id,
                "inventory_freshness_epoch": freshness,
                "inventory_policy_authority": inventory_policy_authority,
                "obligations": obligations,
                "observations": observations,
                "publications": publications,
                "receipts": receipts,
                "required_validations": validation_names,
                "rounds": rounds,
                "structural_authority_version": "1",
                "task_outcomes": task_outcomes,
                "task5c_evidence": self._v6_evidence_content(connection, generation_id),
                "typed_dominance": dominance,
            }
        )

    @staticmethod
    def preflight_round_budget(max_scholar_pages: int, max_html_probe_waves: int) -> int:
        """Reject an impossible fixed discovery budget before any claim occurs."""
        if (
            isinstance(max_scholar_pages, bool)
            or not isinstance(max_scholar_pages, int)
            or max_scholar_pages < 1
            or isinstance(max_html_probe_waves, bool)
            or not isinstance(max_html_probe_waves, int)
            or max_html_probe_waves < 0
        ):
            raise ValueError("discovery round budget values must be nonnegative integers")
        # Initial inventory plus S-1 continuations is S, followed by nine
        # registered pass waves, three expansions, and H HTML probe waves.
        total = PASS_WAVE_COUNT + 3 + max_scholar_pages + max_html_probe_waves
        if total > _MAX_PLAN_ROUNDS:
            raise ValueError("discovery round budget exceeds the fixed generation maximum")
        return total

    def bind_discovery_policy(self, policy: object, credentials: object) -> str:
        """Bind one immutable nonsecret discovery policy before inventory planning."""
        from .discovery import DiscoveryCredentials, DiscoveryPolicy, resolve_discovery_authority

        if not isinstance(policy, DiscoveryPolicy) or not isinstance(credentials, DiscoveryCredentials):
            raise TypeError("discovery policy authority requires typed policy and credentials")
        authority = resolve_discovery_authority(policy, credentials)
        if policy.openreview_mode == "authenticated" and credentials.openreview_username is None:
            raise ValueError("authenticated OpenReview credentials are unavailable")
        if policy.crossref_contact_enabled != (credentials.crossref_contact is not None):
            raise ValueError("Crossref contact mode does not match runtime configuration")
        if policy.openalex_contact_enabled != (credentials.openalex_contact is not None):
            raise ValueError("OpenAlex contact mode does not match runtime configuration")
        generation_id = self._generation_id()
        content = dict(authority.canonical_content())
        canonical = _canonical(content)
        digest = _digest(content)
        if digest != authority.digest:
            raise ValueError("discovery authority digest changed")
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state, plan_revision, identity_json, inventory_freshness_epoch FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None or generation[0] not in {
                GenerationState.PLANNING.value,
                GenerationState.RUNNING.value,
            }:
                raise ValueError("discovery policy requires an open generation")
            identity = json.loads(str(generation[2]))
            adapter_versions = identity.get("adapter_versions")
            if not isinstance(adapter_versions, Mapping) or any(
                policy.adapter_versions[provider] != version
                for provider, version in adapter_versions.items()
                if provider in policy.adapter_versions
            ):
                raise ValueError("discovery policy adapter matrix does not match generation authority")
            if int(generation[1]) == 0 and any(
                adapter_versions.get(provider) != version for provider, version in policy.adapter_versions.items()
            ):
                raise ValueError("fresh generation lacks the complete discovery adapter matrix")
            if generation[3] is not None and generation[3] != policy.freshness_epoch:
                raise ValueError("discovery policy freshness does not match inventory authority")
            inventory_authority = self._load_inventory_policy_authority(connection, generation_id)
            if inventory_authority is not None:
                inventory_content = inventory_authority["authority"]
                inventory_policy = inventory_content.get("policy") if isinstance(inventory_content, Mapping) else None
                seed_versions = (
                    inventory_policy.get("seed_adapter_versions") if isinstance(inventory_policy, Mapping) else None
                )
                if (
                    not isinstance(inventory_policy, Mapping)
                    or inventory_policy.get("max_scholar_pages") != policy.max_scholar_pages
                    or not isinstance(seed_versions, Mapping)
                    or seed_versions.get("doi_csl") != policy.adapter_versions["doi_csl"]
                    or seed_versions.get("s2") != policy.adapter_versions["s2"]
                ):
                    raise ValueError("discovery policy does not match durable inventory policy")
            existing = connection.execute(
                "SELECT policy_json, policy_digest FROM discovery_policy_authority WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                loaded = self._load_discovery_authority(connection, generation_id)
                if loaded.canonical_content() != authority.canonical_content() or loaded.digest != digest:
                    raise ValueError("conflicting discovery policy replay")
                return digest
            if int(generation[1]) != 0:
                if (
                    connection.execute(
                        "SELECT 1 FROM corpus_scan_receipts WHERE generation_id = ?", (generation_id,)
                    ).fetchone()
                    is None
                ):
                    raise ValueError("late discovery policy bind requires trusted corpus authority")
                if (
                    connection.execute(
                        "SELECT 1 FROM planner_passes WHERE generation_id = ? AND pass_id IN "
                        "('known_doi','broad_discovery','dynamic_expansion','venue_fallback','late_identifiers',"
                        "'html_probe','late_doi','merge_intents') LIMIT 1",
                        (generation_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("discovery policy must precede C4 planner passes")
                resolved = authority.resolved_provider_modes
                if (
                    resolved["s2"] != "applicable"
                    and connection.execute(
                        "SELECT 1 FROM tasks WHERE generation_id = ? AND provider = 's2' AND "
                        "operation = 'fuzzy_search' AND applicability = 'applicable' LIMIT 1",
                        (generation_id,),
                    ).fetchone()
                    is not None
                ):
                    raise ValueError("legacy S2 tasks conflict with resolved discovery policy")
            with self._authority_write():
                connection.execute(
                    "INSERT INTO discovery_policy_authority(generation_id, policy_json, policy_digest) "
                    "VALUES (?, ?, ?)",
                    (generation_id, canonical, digest),
                )
            self._inject("after_discovery_policy_authority")
        return digest

    def load_discovery_authority(self) -> DiscoveryAuthority:
        """Return the detached, typed nonsecret discovery authority for this generation."""
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            return self._load_discovery_authority(connection, generation_id)

    def assert_c3_discovery_ready(self) -> None:
        """Fail closed unless trusted C3 evidence and seed binding are ready."""
        generation_id = self._generation_id()
        expected = self._trusted_corpus_expected()
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state, plan_closed FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or int(generation[1]):
                raise ValueError("C3 discovery readiness requires an open running generation")
            self._verify_trusted_corpus(connection, expected)
            bind = connection.execute(
                "SELECT 1 FROM planner_passes WHERE generation_id = ? AND pass_id = 'bind_corpus_seed'",
                (generation_id,),
            ).fetchone()
            if bind is None:
                raise ValueError("C3 discovery readiness requires the corpus seed binding pass")
            self._verify_v6_relationships(connection, generation_id)

    def assert_discovery_authority(self, supplied: object) -> DiscoveryAuthority:
        """Reject caller authority values that differ from the durable generation binding."""
        authoritative = self.load_discovery_authority()
        if supplied != authoritative:
            raise ValueError("discovery authority does not match durable generation binding")
        return authoritative

    def discovery_wave_task_keys(self, pass_id: str, *, now: datetime) -> frozenset[str]:
        """Return exact pending request-backed membership for one committed C4 wave."""
        return frozenset(self.discovery_wave_due_tasks(pass_id, now=now))

    def discovery_wave_due_tasks(self, pass_id: str, *, now: datetime) -> Mapping[str, str]:
        """Return one validated task-key to provider map for due C4 work."""
        if pass_id not in {
            "known_doi",
            "broad_discovery",
            "dynamic_expansion",
            "venue_fallback",
            "late_identifiers",
            "html_probe",
        }:
            raise ValueError("unsupported discovery wave")
        generation_id = self._generation_id()
        now_text = _timestamp(now)
        trusted_expected = self._trusted_corpus_expected()
        with self._transaction(immediate=True) as connection:
            self._verify_trusted_corpus(connection, trusted_expected)
            self._verify_v6_relationships(connection, generation_id)
            pass_row = connection.execute(
                "SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = ?",
                (generation_id, pass_id),
            ).fetchone()
            if pass_row is None:
                raise ValueError("discovery wave is not committed")
            rows = connection.execute(
                "SELECT task.task_key, task.provider, task.request_key, task.state, task.next_attempt_at, "
                "task.lease_expires_at, json_extract(item.input_json, '$.payload.request_key') "
                "FROM planner_pass_expected_items AS item JOIN tasks AS task "
                "ON task.generation_id = item.generation_id AND "
                "task.task_key = json_extract(item.input_json, '$.payload.task_key') "
                "WHERE item.generation_id = ? AND item.pass_key = ? AND item.kind = ?",
                (generation_id, str(pass_row[0]), EvidenceKind.APPLICABILITY.value),
            ).fetchall()
            if pass_id == "known_doi":  # noqa: S105 - planner pass identifier
                rows.extend(
                    connection.execute(
                        "SELECT task.task_key, task.provider, task.request_key, task.state, task.next_attempt_at, "
                        "task.lease_expires_at FROM tasks AS task JOIN plan_obligations AS obligation "
                        "ON obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE task.generation_id = ? "
                        "AND round.planner_id = 'doi_bibtex'",
                        (generation_id,),
                    ).fetchall()
                )
            elif pass_id == "venue_fallback":  # noqa: S105 - planner pass identifier
                rows.extend(
                    connection.execute(
                        "SELECT task.task_key, task.provider, task.request_key, task.state, task.next_attempt_at, "
                        "task.lease_expires_at FROM tasks AS task JOIN plan_obligations AS obligation "
                        "ON obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE task.generation_id = ? "
                        "AND round.planner_id = 'openalex_venue_expansion'",
                        (generation_id,),
                    ).fetchall()
                )
            elif pass_id == "html_probe":  # noqa: S105 - planner pass identifier
                rows.extend(
                    connection.execute(
                        "SELECT task.task_key, task.provider, task.request_key, task.state, task.next_attempt_at, "
                        "task.lease_expires_at FROM html_probe_wave_items AS item JOIN tasks AS task "
                        "ON task.generation_id = item.generation_id AND task.task_key = item.task_key "
                        "WHERE item.generation_id = ? AND item.parent_pass_key = ?",
                        (generation_id, str(pass_row[0])),
                    ).fetchall()
                )
            due: dict[str, str] = {}
            for row in rows:
                if row[2] is None:
                    continue
                if len(row) == 7 and str(row[2]) != str(row[6]):
                    raise ValueError("discovery wave task membership changed")
                state = str(row[3])
                if (
                    state == TaskDisposition.PENDING.value
                    or (state == TaskDisposition.RETRY_WAIT.value and row[4] is not None and str(row[4]) <= now_text)
                    or (state == TaskDisposition.LEASED.value and row[5] is not None and str(row[5]) < now_text)
                ):
                    due[str(row[0])] = str(row[1])
            return MappingProxyType(dict(sorted(due.items())))

    def discovery_phase_status(self, pass_id: str, *, now: datetime) -> str:
        """Classify one C4 phase without exposing mutable planner internals."""
        if pass_id not in {
            "known_doi",
            "broad_discovery",
            "dynamic_expansion",
            "venue_fallback",
            "late_identifiers",
            "html_probe",
        }:
            raise ValueError("unsupported discovery wave")
        generation_id = self._generation_id()
        if (
            self._connection.execute(
                "SELECT 1 FROM planner_passes WHERE generation_id = ? AND pass_id = ?",
                (generation_id, pass_id),
            ).fetchone()
            is None
        ):
            return "uncommitted"
        with self._transaction(immediate=True) as connection:
            rows = connection.execute(
                "SELECT task.state FROM planner_passes AS pass JOIN planner_pass_expected_items AS item "
                "ON item.generation_id = pass.generation_id AND item.pass_key = pass.pass_key "
                "JOIN tasks AS task ON task.generation_id = item.generation_id "
                "AND task.task_key = json_extract(item.input_json, '$.payload.task_key') "
                "WHERE pass.generation_id = ? AND pass.pass_id = ? AND item.kind = ?",
                (generation_id, pass_id, EvidenceKind.APPLICABILITY.value),
            ).fetchall()
            states = [TaskDisposition(str(row[0])) for row in rows]
            if any(state in _TERMINAL - _SATISFIED for state in states):
                return "blocking"
            if pass_id == "late_identifiers":  # noqa: S105 - planner pass identifier
                return "complete"
            if pass_id == "known_doi":  # noqa: S105 - planner pass identifier
                if (
                    connection.execute(
                        "SELECT 1 FROM plan_rounds WHERE generation_id = ? AND planner_id = 'doi_bibtex'",
                        (generation_id,),
                    ).fetchone()
                    is None
                ):
                    return "pending"
                states.extend(
                    TaskDisposition(str(row[0]))
                    for row in connection.execute(
                        "SELECT task.state FROM tasks AS task JOIN plan_obligations AS obligation "
                        "ON obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE task.generation_id = ? "
                        "AND round.planner_id = 'doi_bibtex'",
                        (generation_id,),
                    )
                )
            elif pass_id == "venue_fallback":  # noqa: S105 - planner pass identifier
                if (
                    connection.execute(
                        "SELECT 1 FROM plan_rounds WHERE generation_id = ? AND planner_id = 'openalex_venue_expansion'",
                        (generation_id,),
                    ).fetchone()
                    is None
                ):
                    return "pending"
                states.extend(
                    TaskDisposition(str(row[0]))
                    for row in connection.execute(
                        "SELECT task.state FROM tasks AS task JOIN plan_obligations AS obligation "
                        "ON obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE task.generation_id = ? "
                        "AND round.planner_id = 'openalex_venue_expansion'",
                        (generation_id,),
                    )
                )
            elif pass_id == "html_probe":  # noqa: S105 - planner pass identifier
                pass_row = connection.execute(
                    "SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = 'html_probe'",
                    (generation_id,),
                ).fetchone()
                control = (
                    connection.execute(
                        "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? "
                        "AND pass_key = ? AND item_key LIKE 'html-control:%'",
                        (generation_id, str(pass_row[0])),
                    ).fetchone()
                    if pass_row is not None
                    else None
                )
                envelope = json.loads(str(control[0])) if control is not None else None
                payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
                if isinstance(payload, Mapping) and payload.get("terminal") is True:
                    return "complete"
                child_states = [
                    TaskDisposition(str(row[0]))
                    for row in connection.execute(
                        "SELECT task.state FROM html_probe_wave_items AS item JOIN tasks AS task "
                        "ON task.generation_id = item.generation_id AND task.task_key = item.task_key "
                        "WHERE item.generation_id = ? AND item.parent_pass_key = ?",
                        (generation_id, str(pass_row[0])),
                    )
                ]
                states.extend(child_states)
                if any(state in _TERMINAL - _SATISFIED for state in child_states):
                    return "blocking"
                if any(state not in _SATISFIED for state in child_states):
                    return "pending"
                if (
                    connection.execute(
                        "SELECT 1 FROM html_probe_terminal_receipts WHERE generation_id = ? AND parent_pass_key = ?",
                        (generation_id, str(pass_row[0])),
                    ).fetchone()
                    is None
                ):
                    return "pending"
            if any(state in _TERMINAL - _SATISFIED for state in states):
                return "blocking"
            if any(state not in _SATISFIED for state in states):
                return "pending"
            return "complete"

    @staticmethod
    def _load_discovery_authority(connection: sqlite3.Connection, generation_id: str) -> DiscoveryAuthority:
        from .discovery import DiscoveryAuthority, DiscoveryPolicy

        row = connection.execute(
            "SELECT policy_json, policy_digest FROM discovery_policy_authority WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if row is None:
            raise ValueError("discovery policy authority is absent")
        try:
            content = json.loads(str(row[0]))
            if not isinstance(content, Mapping) or set(content) != {
                "capability_registry_digest",
                "policy",
                "resolved_provider_modes",
            }:
                raise ValueError("stored discovery policy authority is malformed")
            policy_content = content["policy"]
            modes = content["resolved_provider_modes"]
            if not isinstance(policy_content, Mapping) or not isinstance(modes, Mapping):
                raise ValueError("stored discovery policy authority is malformed")
            policy_values = dict(policy_content)
            stored_round_budget = policy_values.pop("round_budget", None)
            policy = DiscoveryPolicy(**policy_values)
            if stored_round_budget != policy.round_budget:
                raise ValueError("stored discovery round budget changed")
            authority = DiscoveryAuthority(policy, cast(Mapping[str, str], modes))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("stored discovery policy authority is malformed") from exc
        if str(row[0]) != _canonical(authority.canonical_content()) or str(row[1]) != authority.digest:
            raise ValueError("stored discovery policy authority integrity mismatch")
        return authority

    def execute_and_commit_discovery_wave(self, pass_id: str, policy: object, *, now: datetime) -> PlannerPassReceipt:
        """Derive and atomically append one supported C4 discovery wave."""
        from .discovery import (
            DiscoveryObservation,
            DiscoveryPolicy,
            plan_broad_discovery,
            plan_doi_bibtex,
            plan_dynamic_expansion,
            plan_known_doi,
            reduce_current_doi_observations,
        )
        from .publication_discovery import plan_crossref_venue_fallback

        if pass_id not in {"known_doi", "broad_discovery", "dynamic_expansion", "venue_fallback"}:
            raise ValueError("unsupported discovery wave")
        if not isinstance(policy, DiscoveryPolicy):
            raise TypeError("discovery wave requires a typed policy")
        committed_at = _timestamp(now)
        generation_id = self._generation_id()
        trusted_expected = self._trusted_corpus_expected()
        with self._transaction(immediate=True) as connection:
            self._verify_trusted_corpus(connection, trusted_expected)
            authority = self._load_discovery_authority(connection, generation_id)
            if authority.policy != policy:
                raise ValueError("discovery wave policy does not match bound authority")
            generation = connection.execute(
                "SELECT state, plan_closed, plan_revision, updated_at FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or int(generation[1]):
                raise ValueError("discovery wave requires an open running generation")
            seeds = []
            for row in connection.execute(
                "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
                "ORDER BY author_key, publication_key",
                (generation_id,),
            ):
                content = json.loads(str(row[0]))
                content["origin_kind"] = EvidenceKind(str(content["origin_kind"]))
                seeds.append(PublicationSeedEvidence(**content))
            authors = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "SELECT row_key, name FROM authors WHERE generation_id = ? AND enabled = 1",
                    (generation_id,),
                )
            }
            known = plan_known_doi(seeds, authority)
            source_items: list[Mapping[str, object]] = []
            if pass_id == "known_doi":  # noqa: S105 - planner pass identifier
                wave = known
            else:
                bib_round = connection.execute(
                    "SELECT 1 FROM plan_rounds WHERE generation_id = ? AND planner_id = 'doi_bibtex'",
                    (generation_id,),
                ).fetchone()
                open_known = connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE generation_id = ? AND provider IN ('doi_csl','doi_bibtex') "
                    "AND state NOT IN ('succeeded','confirmed_empty','not_applicable','dominated')",
                    (generation_id,),
                ).fetchone()[0]
                if bib_round is None or open_known:
                    raise ValueError("broad discovery requires complete DOI expansion")
                known_observations = []
                for decision in known.decisions:
                    task = decision.task
                    if task.request is None:
                        continue
                    stored = connection.execute(
                        "SELECT observation.disposition, observation.response_json, observation.schema_version, "
                        "observation.authoritative_empty FROM tasks AS task JOIN observations AS observation "
                        "ON observation.generation_id = task.generation_id AND observation.request_key = "
                        "task.request_key WHERE task.generation_id = ? AND task.task_key = ?",
                        (generation_id, task.key),
                    ).fetchone()
                    if stored is None or TaskDisposition(str(stored[0])) not in _SATISFIED:
                        raise ValueError("broad discovery requires terminal CSL evidence")
                    known_observations.append(
                        DiscoveryObservation(
                            task,
                            TaskDisposition(str(stored[0])),
                            json.loads(str(stored[1])) if stored[1] is not None else {},
                            bool(stored[3]),
                            str(stored[2]),
                        )
                    )
                bibtex = plan_doi_bibtex(seeds, known, known_observations, authority)
                bibtex_observations = []
                for decision in bibtex.decisions:
                    task = decision.task
                    if task.request is None:
                        continue
                    stored = connection.execute(
                        "SELECT observation.disposition, observation.response_json, observation.schema_version, "
                        "observation.authoritative_empty FROM tasks AS task JOIN observations AS observation "
                        "ON observation.generation_id = task.generation_id AND observation.request_key = "
                        "task.request_key WHERE task.generation_id = ? AND task.task_key = ?",
                        (generation_id, task.key),
                    ).fetchone()
                    if stored is None or TaskDisposition(str(stored[0])) not in _SATISFIED:
                        raise ValueError("broad discovery requires terminal BibTeX evidence")
                    bibtex_observations.append(
                        DiscoveryObservation(
                            task,
                            TaskDisposition(str(stored[0])),
                            json.loads(str(stored[1])) if stored[1] is not None else {},
                            bool(stored[3]),
                            str(stored[2]),
                        )
                    )
                reductions = reduce_current_doi_observations(
                    seeds,
                    known,
                    known_observations,
                    bibtex,
                    bibtex_observations,
                    authority,
                )
                for author_key, name in sorted(authors.items()):
                    payload = {"author_key": author_key, "name": name}
                    source_items.append(
                        {
                            "digest": evidence_digest(payload),
                            "key": f"author:{author_key}",
                            "kind": EvidenceKind.CORPUS.value,
                            "payload": payload,
                        }
                    )
                for reduction in reductions:
                    reduction_payload: dict[str, object] = {
                        "author_key": reduction.author_key,
                        "publication_key": reduction.publication_key,
                        "selected_metadata": json.loads(evidence_json(reduction.selected_metadata)),
                        "source_task_key": reduction.source_task_key,
                        "status": reduction.status,
                    }
                    source_items.append(
                        {
                            "digest": reduction.digest,
                            "key": f"doi-reduction:{reduction.author_key}:{reduction.publication_key}",
                            "kind": EvidenceKind.REDUCTION_RECEIPT.value,
                            "payload": reduction_payload,
                        }
                    )
                for observation in known_observations:
                    observation_payload: dict[str, object] = {
                        "authoritative_empty": observation.authoritative_empty,
                        "disposition": observation.disposition.value,
                        "request_key": observation.request_key,
                        "response": json.loads(evidence_json(observation.response)),
                        "response_digest": observation.response_digest,
                        "schema_version": observation.schema_version,
                        "task_key": observation.task.key,
                    }
                    source_items.append(
                        {
                            "digest": evidence_digest(observation_payload),
                            "key": f"csl-observation:{observation.task.key}",
                            "kind": EvidenceKind.OBSERVATION.value,
                            "payload": observation_payload,
                        }
                    )
                for observation in bibtex_observations:
                    observation_payload = {
                        "authoritative_empty": observation.authoritative_empty,
                        "disposition": observation.disposition.value,
                        "request_key": observation.request_key,
                        "response": json.loads(evidence_json(observation.response)),
                        "response_digest": observation.response_digest,
                        "schema_version": observation.schema_version,
                        "task_key": observation.task.key,
                    }
                    source_items.append(
                        {
                            "digest": evidence_digest(observation_payload),
                            "key": f"bibtex-observation:{observation.task.key}",
                            "kind": EvidenceKind.OBSERVATION.value,
                            "payload": observation_payload,
                        }
                    )
                broad = plan_broad_discovery(seeds, authors, authority, reductions)
                if pass_id == "broad_discovery":  # noqa: S105 - planner pass identifier
                    wave = broad
                else:
                    broad_observations = []
                    for decision in broad.decisions:
                        task = decision.task
                        if task.request is None:
                            continue
                        stored = connection.execute(
                            "SELECT observation.disposition, observation.response_json, observation.schema_version, "
                            "observation.authoritative_empty FROM tasks AS task JOIN observations AS observation "
                            "ON observation.generation_id = task.generation_id AND observation.request_key = "
                            "task.request_key WHERE task.generation_id = ? AND task.task_key = ?",
                            (generation_id, task.key),
                        ).fetchone()
                        if stored is None or TaskDisposition(str(stored[0])) not in _SATISFIED:
                            raise ValueError("dynamic expansion requires terminal broad evidence")
                        broad_observations.append(
                            DiscoveryObservation(
                                task,
                                TaskDisposition(str(stored[0])),
                                json.loads(str(stored[1])) if stored[1] is not None else {},
                                bool(stored[3]),
                                str(stored[2]),
                            )
                        )
                    if pass_id == "dynamic_expansion":  # noqa: S105 - planner pass identifier
                        source_items = []
                    for decision in broad.decisions:
                        task = decision.task
                        decision_payload: dict[str, object] = {
                            "applicability": task.applicability,
                            "identity_digest": task.identity_digest,
                            "reason": decision.reason.value if decision.reason is not None else None,
                            "request_key": task.request.key if task.request is not None else None,
                            "task_key": task.key,
                        }
                        source_items.append(
                            {
                                "digest": evidence_digest(decision_payload),
                                "key": f"broad-decision:{task.key}",
                                "kind": EvidenceKind.APPLICABILITY.value,
                                "payload": decision_payload,
                            }
                        )
                    for observation in broad_observations:
                        observation_payload = {
                            "authoritative_empty": observation.authoritative_empty,
                            "disposition": observation.disposition.value,
                            "request_key": observation.request_key,
                            "response": json.loads(evidence_json(observation.response)),
                            "response_digest": observation.response_digest,
                            "schema_version": observation.schema_version,
                            "task_key": observation.task.key,
                        }
                        source_items.append(
                            {
                                "digest": evidence_digest(observation_payload),
                                "key": f"broad-observation:{observation.task.key}",
                                "kind": EvidenceKind.OBSERVATION.value,
                                "payload": observation_payload,
                            }
                        )
                    if pass_id == "venue_fallback":  # noqa: S105 - planner pass identifier
                        wave = plan_crossref_venue_fallback(
                            seeds,
                            authors,
                            broad,
                            broad_observations,
                            reductions,
                            authority,
                        )
                    else:
                        wave = plan_dynamic_expansion(broad, broad_observations, authority)
            existing_pass = connection.execute(
                "SELECT pass_key, receipt_json FROM planner_passes WHERE generation_id = ? AND pass_id = ?",
                (generation_id, pass_id),
            ).fetchone()
            if existing_pass is not None:
                receipt = PlannerPassReceipt(**json.loads(str(existing_pass[1])))
                adopted_values: set[str] = set()
                for row in connection.execute(
                    "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? "
                    "AND pass_key = ? AND kind = ?",
                    (generation_id, str(existing_pass[0]), EvidenceKind.APPLICABILITY.value),
                ):
                    envelope_value = json.loads(str(row[0]))
                    payload_value = envelope_value.get("payload") if isinstance(envelope_value, Mapping) else None
                    if (
                        isinstance(payload_value, Mapping)
                        and payload_value.get("adopted") is True
                        and isinstance(payload_value.get("task_key"), str)
                    ):
                        adopted_values.add(str(payload_value["task_key"]))
                adopted_keys = frozenset(adopted_values)
                replay_snapshot = self._snapshot_for_discovery_pass(
                    connection,
                    generation_id,
                    pass_id,
                    authority=authority,
                    decisions=wave.decisions,
                    adopted_keys=adopted_keys,
                    source_items=source_items,
                )
                authoritative_receipt = _execute_authoritative_pass(pass_id, replay_snapshot)
                stored_round = connection.execute(
                    "SELECT source_evidence_digest FROM plan_rounds WHERE generation_id = ? AND planner_id = ?",
                    (generation_id, pass_id),
                ).fetchone()
                if stored_round is None or not _receipt_matches_authority(receipt, authoritative_receipt):
                    raise ValueError("conflicting discovery wave replay")
                if pass_id == "known_doi":  # noqa: S105 - planner pass identifier
                    self._commit_known_doi_expansion(policy, receipt, now=now, _connection=connection)
                elif pass_id == "venue_fallback":  # noqa: S105 - planner pass identifier
                    self._commit_venue_expansion(
                        policy,
                        receipt,
                        seeds,
                        authors,
                        wave,
                        now=now,
                        _connection=connection,
                    )
                else:
                    return receipt
                return receipt
            definition = pass_for(pass_id)
            earlier = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT pass_id FROM planner_passes WHERE generation_id = ? ORDER BY rowid",
                    (generation_id,),
                )
            )
            expected_earlier = tuple(
                value.pass_id
                for value in sorted((pass_for(key) for key in PASSES), key=lambda value: value.ordinal)
                if value.ordinal < definition.ordinal
            )
            if earlier != expected_earlier:
                raise ValueError("discovery wave phase sequence is skipped or backward")
            if int(generation[2]) >= _MAX_PLAN_ROUNDS or policy.round_budget > _MAX_PLAN_ROUNDS:
                raise ValueError("discovery wave exceeds the fixed plan round budget")
            latest_round = connection.execute(
                "SELECT committed_at FROM plan_rounds WHERE generation_id = ? ORDER BY sequence DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            if committed_at < str(generation[3]) or (latest_round is not None and committed_at < str(latest_round[0])):
                raise ValueError("discovery wave timestamp precedes durable generation history")
            sequence = int(generation[2]) + 1
            new_tasks: list[PlannedTask] = []
            adopted_tasks: list[PlannedTask] = []
            expected_consumers = self._discovery_request_consumers(wave.decisions)
            for decision in wave.decisions:
                task = decision.task
                stored = connection.execute(
                    "SELECT author_key, publication_key, provider, operation, request_key, required, applicability, "
                    "identity_digest, state, applicability_reason FROM tasks "
                    "WHERE generation_id = ? AND task_key = ?",
                    (generation_id, task.key),
                ).fetchone()
                if stored is not None:
                    expected_request = task.request.key if task.request is not None else None
                    expected_reason = decision.reason.value if decision.reason is not None else ""
                    expected_state = TaskDisposition.NOT_APPLICABLE.value if task.request is None else str(stored[8])
                    if tuple(stored[:8]) != (
                        task.author_key,
                        task.publication_key,
                        task.provider,
                        task.operation,
                        expected_request,
                        int(task.required),
                        task.applicability,
                        task.identity_digest,
                    ) or (
                        task.request is None and (str(stored[8]) != expected_state or str(stored[9]) != expected_reason)
                    ):
                        raise ValueError("adopted discovery task identity changed")
                    obligation = connection.execute(
                        "SELECT obligation.identity_digest, round.planner_id FROM plan_obligations AS obligation "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE obligation.generation_id = ? "
                        "AND obligation.task_key = ?",
                        (generation_id, task.key),
                    ).fetchone()
                    if (
                        obligation is None
                        or str(obligation[0]) != task.identity_digest
                        or str(obligation[1]) != "inventory_union"
                    ):
                        raise ValueError("adopted discovery task lacks permitted authoritative origin")
                    if task.request is not None:
                        if expected_request is None:
                            raise AssertionError("request-backed task lacks a request key")
                        request_row = connection.execute(
                            "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                            (generation_id, expected_request),
                        ).fetchone()
                        consumers = connection.execute(
                            "SELECT task_key FROM request_consumers WHERE generation_id = ? AND request_key = ? "
                            "ORDER BY task_key",
                            (generation_id, expected_request),
                        ).fetchall()
                        prior_consumers = tuple(
                            task_key
                            for task_key in expected_consumers[expected_request]
                            if connection.execute(
                                "SELECT 1 FROM tasks WHERE generation_id = ? AND task_key = ?",
                                (generation_id, task_key),
                            ).fetchone()
                            is not None
                        )
                        if (
                            request_row is None
                            or str(request_row[0]) != _canonical(task.request.canonical_content())
                            or tuple(str(row[0]) for row in consumers) != prior_consumers
                        ):
                            raise ValueError("adopted discovery task authority changed")
                    adopted_tasks.append(PlannedTask(task, expands_plan=True))
                    continue
                new_tasks.append(PlannedTask(task, expands_plan=True))
            content = self._round_content(
                sequence,
                PlanPhase.DISCOVERY,
                pass_id,
                policy.planner_version,
                (),
                wave.input_digest,
                (),
                new_tasks,
            )
            content_digest = _digest(content)
            round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            snapshot = self._snapshot_for_discovery_pass(
                connection,
                generation_id,
                pass_id,
                authority=authority,
                decisions=wave.decisions,
                adopted_keys=frozenset(item.task.key for item in adopted_tasks),
                source_items=source_items,
            )
            receipt = _execute_authoritative_pass(pass_id, snapshot)
            predecessor = connection.execute(
                "SELECT output_digest FROM planner_passes WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            predecessor_digest = str(predecessor[0]) if predecessor is not None else None
            with self._authority_write():
                connection.execute(
                    "INSERT INTO planner_passes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        receipt.pass_key,
                        receipt.pass_id,
                        receipt.pass_version,
                        receipt.registry_digest,
                        receipt.snapshot_digest,
                        receipt.output_digest,
                        evidence_json(receipt.canonical_content()),
                        evidence_digest(
                            {
                                "domain": _SNAPSHOT_DOMAIN_SEPARATOR,
                                "generation_id": generation_id,
                                "pass_id": pass_id,
                                "snapshot": snapshot,
                                "predecessor_output_digest": predecessor_digest,
                            }
                        ),
                        predecessor_digest,
                    ),
                )
                self._inject("after_c4_pass_receipt")
                connection.executemany(
                    "INSERT INTO planner_pass_expected_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            generation_id,
                            receipt.pass_key,
                            str(item["key"]),
                            str(item["kind"]),
                            str(item["digest"]),
                            evidence_json(item),
                            int(str(item["key"]) in receipt.unseen_keys),
                        )
                        for item in cast(Sequence[Mapping[str, object]], snapshot["items"])
                        if str(item["key"]) in receipt.expected_items
                    ),
                )
                self._inject("after_c4_expected_items")
            if pass_id not in {"known_doi", "venue_fallback"}:
                if adopted_tasks and pass_id != "broad_discovery":  # noqa: S105 - planner pass identifier
                    raise ValueError("dynamic discovery cannot adopt preexisting tasks")
                if pass_id == "broad_discovery":  # noqa: S105 - planner pass identifier
                    source_keys = tuple(
                        str(row[0])
                        for row in connection.execute(
                            "SELECT obligation.task_key FROM plan_obligations AS obligation "
                            "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                            "AND round.sequence = obligation.round_sequence WHERE obligation.generation_id = ? "
                            "AND round.planner_id = 'doi_bibtex' ORDER BY obligation.task_key",
                            (generation_id,),
                        )
                    )
                else:
                    broad_pass = connection.execute(
                        "SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = 'broad_discovery'",
                        (generation_id,),
                    ).fetchone()
                    if broad_pass is None:
                        raise ValueError("dynamic expansion requires the authoritative broad pass")
                    source_values: list[str] = []
                    for row in connection.execute(
                        "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? "
                        "AND pass_key = ? AND kind = ?",
                        (generation_id, str(broad_pass[0]), EvidenceKind.APPLICABILITY.value),
                    ):
                        envelope_value = json.loads(str(row[0]))
                        payload_value = envelope_value.get("payload") if isinstance(envelope_value, Mapping) else None
                        if isinstance(payload_value, Mapping) and isinstance(payload_value.get("task_key"), str):
                            source_values.append(str(payload_value["task_key"]))
                    source_keys = tuple(sorted(source_values))
                if not source_keys and (seeds or wave.decisions):
                    raise ValueError("discovery reduction source membership is empty")
                durable_digests = tuple(self._source_evidence(connection, generation_id, key)[1] for key in source_keys)
                source_digest = durable_digests[0] if len(durable_digests) == 1 else _digest(list(durable_digests))
                new_task_keys = {item.task.key for item in new_tasks}
                self.commit_reduction(
                    source_keys,
                    source_evidence_digest=source_digest,
                    publications=(),
                    tasks=tuple(
                        PlannedTask(
                            item.task,
                            expands_plan=pass_id == "broad_discovery",  # noqa: S105 - planner pass identifier
                        )
                        for item in new_tasks
                    ),
                    now=now,
                    reducer_id=pass_id,
                    reducer_version=policy.reducer_version,
                    _applicability_reasons={
                        decision.task.key: decision.reason
                        for decision in wave.decisions
                        if decision.task.key in new_task_keys
                        and decision.task.request is None
                        and decision.reason is not None
                    },
                    _connection=connection,
                    _allow_empty_sources=True,
                    _fault_callback=self._inject,
                )
                self._inject("after_c4_expansion")
                return receipt
            for item in new_tasks:
                self._insert_task(connection, generation_id, item.task, self._inject)
            for request_key, expected in expected_consumers.items():
                durable = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT task_key FROM request_consumers WHERE generation_id = ? AND request_key = ? "
                        "ORDER BY task_key",
                        (generation_id, request_key),
                    )
                )
                if durable != expected:
                    raise ValueError("discovery request consumer membership changed")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability, round_sequence, expands_plan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (
                    (
                        generation_id,
                        item.task.key,
                        item.task.identity_digest,
                        item.task.author_key,
                        item.task.provider,
                        item.task.operation,
                        int(item.task.required),
                        item.task.applicability,
                        sequence,
                    )
                    for item in new_tasks
                ),
            )
            self._inject("after_c4_obligations")
            new_task_keys = {item.task.key for item in new_tasks}
            for decision in wave.decisions:
                if decision.task.request is None:
                    if decision.task.key not in new_task_keys:
                        continue
                    updated = connection.execute(
                        "UPDATE tasks SET state = ?, applicability_reason = ? WHERE generation_id = ? AND task_key = ?",
                        (
                            TaskDisposition.NOT_APPLICABLE.value,
                            decision.reason.value if decision.reason is not None else "",
                            generation_id,
                            decision.task.key,
                        ),
                    )
                    if updated.rowcount != 1:
                        raise ValueError("not-applicable discovery task terminalization changed")
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                (
                    generation_id,
                    sequence,
                    round_key,
                    PlanPhase.DISCOVERY.value,
                    pass_id,
                    policy.planner_version,
                    wave.input_digest,
                    content["task_set_digest"],
                    content_digest,
                    committed_at,
                ),
            )
            self._inject("after_c4_round")
            cumulative = _digest(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT content_digest FROM plan_rounds WHERE generation_id = ? ORDER BY sequence",
                        (generation_id,),
                    )
                ]
            )
            connection.execute(
                "UPDATE generations SET plan_revision = ?, plan_digest = ?, updated_at = ? WHERE generation_id = ?",
                (sequence, cumulative, committed_at, generation_id),
            )
        return receipt

    def execute_and_commit_venue_fallback(self, policy: object, *, now: datetime) -> PlannerPassReceipt:
        """Derive and atomically append the operation-specific venue fallback wave."""
        return self.execute_and_commit_discovery_wave("venue_fallback", policy, now=now)

    @staticmethod
    def _stored_discovery_wave(
        connection: sqlite3.Connection, generation_id: str, planner_id: str, policy_digest: str
    ) -> tuple[DiscoveryWave, tuple[DiscoveryObservation, ...]]:
        from .discovery import DiscoveryDecision, DiscoveryObservation, DiscoveryWave

        decisions: list[DiscoveryDecision] = []
        observations: list[DiscoveryObservation] = []
        pass_row = connection.execute(
            "SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = ?",
            (generation_id, planner_id),
        ).fetchone()
        rows = connection.execute(
            "SELECT task.task_key, task.request_key, task.state, task.applicability_reason "
            "FROM tasks AS task JOIN plan_obligations AS obligation "
            "ON obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
            "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
            "AND round.sequence = obligation.round_sequence WHERE task.generation_id = ? "
            "AND round.planner_id = ? ORDER BY task.task_key",
            (generation_id, planner_id),
        ).fetchall()
        if pass_row is not None:
            keys = [
                str(payload[0])
                for row in connection.execute(
                    "SELECT json_extract(input_json, '$.payload.task_key') FROM planner_pass_expected_items "
                    "WHERE generation_id = ? AND pass_key = ? AND kind = ? AND item_key LIKE 'decision:%' "
                    "ORDER BY item_key",
                    (generation_id, str(pass_row[0]), EvidenceKind.APPLICABILITY.value),
                )
                if (payload := row)[0] is not None
            ]
            rows = [
                connection.execute(
                    "SELECT task_key, request_key, state, applicability_reason FROM tasks "
                    "WHERE generation_id = ? AND task_key = ?",
                    (generation_id, key),
                ).fetchone()
                for key in keys
            ]
        for row in rows:
            task = Ledger._load_task(connection, generation_id, str(row[0]))
            reason = ApplicabilityReason(str(row[3])) if row[3] else None
            decisions.append(DiscoveryDecision(task, reason))
            if task.request is None:
                continue
            observation = connection.execute(
                "SELECT disposition, response_json, schema_version, authoritative_empty FROM observations "
                "WHERE generation_id = ? AND request_key = ?",
                (generation_id, str(row[1])),
            ).fetchone()
            if observation is None or TaskDisposition(str(observation[0])) not in _SATISFIED:
                raise ValueError("late identifiers require complete terminal predecessor evidence")
            observations.append(
                DiscoveryObservation(
                    task,
                    TaskDisposition(str(observation[0])),
                    json.loads(str(observation[1])) if observation[1] is not None else {},
                    bool(observation[3]),
                    str(observation[2]),
                )
            )
        round_row = connection.execute(
            "SELECT source_evidence_digest FROM plan_rounds WHERE generation_id = ? AND planner_id = ?",
            (generation_id, planner_id),
        ).fetchone()
        if round_row is None:
            raise ValueError("late identifiers require complete predecessor rounds")
        return DiscoveryWave(tuple(decisions), str(round_row[0]), policy_digest), tuple(observations)

    def execute_and_commit_late_identifiers(self, policy: object, *, now: datetime) -> PlannerPassReceipt:
        """Commit exact late identifier evidence from all terminal discovery candidates."""
        from .discovery import DiscoveryPolicy
        from .publication_discovery import derive_late_identifier_evidence

        if not isinstance(policy, DiscoveryPolicy):
            raise TypeError("late identifiers require a typed discovery policy")
        generation_id = self._generation_id()
        committed_at = _timestamp(now)
        trusted_expected = self._trusted_corpus_expected()
        with self._transaction(immediate=True) as connection:
            self._verify_trusted_corpus(connection, trusted_expected)
            authority = self._load_discovery_authority(connection, generation_id)
            if authority.policy != policy:
                raise ValueError("late identifier policy does not match bound authority")
            existing_pass_row = connection.execute(
                "SELECT receipt_json FROM planner_passes WHERE generation_id = ? AND pass_id = 'late_identifiers'",
                (generation_id,),
            ).fetchone()
            generation = connection.execute(
                "SELECT state, plan_closed, plan_revision, updated_at FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or int(generation[1]):
                raise ValueError("late identifiers require an open running generation")
            if existing_pass_row is None and (
                int(generation[2]) >= _MAX_PLAN_ROUNDS or policy.round_budget > _MAX_PLAN_ROUNDS
            ):
                raise ValueError("late identifiers exceed the fixed plan round budget")
            latest_round = connection.execute(
                "SELECT committed_at FROM plan_rounds WHERE generation_id = ? ORDER BY sequence DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            if existing_pass_row is None and (
                committed_at < str(generation[3]) or (latest_round is not None and committed_at < str(latest_round[0]))
            ):
                raise ValueError("late identifier timestamp precedes durable generation history")
            open_venue = connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE generation_id = ? AND operation = 'venue_search' "
                "AND state NOT IN ('succeeded','confirmed_empty','not_applicable','dominated')",
                (generation_id,),
            ).fetchone()[0]
            if (
                open_venue
                or connection.execute(
                    "SELECT 1 FROM plan_rounds WHERE generation_id = ? AND planner_id = 'openalex_venue_expansion'",
                    (generation_id,),
                ).fetchone()
                is None
            ):
                raise ValueError("late identifiers require complete venue fallback evidence")
            seeds: list[PublicationSeedEvidence] = []
            for row in connection.execute(
                "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
                "ORDER BY author_key, publication_key",
                (generation_id,),
            ):
                content = json.loads(str(row[0]))
                content["origin_kind"] = EvidenceKind(str(content["origin_kind"]))
                seeds.append(PublicationSeedEvidence(**content))
            broad, broad_observations = self._stored_discovery_wave(
                connection, generation_id, "broad_discovery", authority.digest
            )
            crossref, crossref_observations = self._stored_discovery_wave(
                connection, generation_id, "venue_fallback", authority.digest
            )
            openalex, openalex_observations = self._stored_discovery_wave(
                connection, generation_id, "openalex_venue_expansion", authority.digest
            )
            waves = (broad, crossref, openalex)
            observations_by_task = {
                item.task.key: item for item in (*broad_observations, *crossref_observations, *openalex_observations)
            }
            observations = tuple(observations_by_task[key] for key in sorted(observations_by_task))
            evidence = derive_late_identifier_evidence(seeds, waves, observations)
            source_keys = tuple(sorted({decision.task.key for wave in waves for decision in wave.decisions}))
            source_items: list[Mapping[str, object]] = []
            for item in evidence:
                payload = {
                    "author_key": item.author_key,
                    "candidates": [
                        {
                            "digest": candidate.digest,
                            "identity_accepted": candidate.identity_accepted,
                            "kind": candidate.kind,
                            "ordinal": candidate.ordinal,
                            "request_key": candidate.request_key,
                            "source_digest": candidate.source_digest,
                            "value": candidate.value,
                        }
                        for candidate in item.candidates
                    ],
                    "publication_key": item.publication_key,
                }
                source_items.append(
                    {
                        "digest": evidence_digest(payload),
                        "key": f"late-output:{item.author_key}:{item.publication_key}",
                        "kind": EvidenceKind.PROVENANCE.value,
                        "payload": payload,
                    }
                )
            for key in source_keys:
                task = self._load_task(connection, generation_id, key)
                observation = observations_by_task.get(key)
                source_payload: dict[str, object] = {
                    "applicability": task.applicability,
                    "author_key": task.author_key,
                    "identity_digest": task.identity_digest,
                    "operation": task.operation,
                    "provider": task.provider,
                    "publication_key": task.publication_key,
                    "request_key": task.request.key if task.request is not None else None,
                    "task_key": key,
                    "terminal": (
                        {
                            "authoritative_empty": observation.authoritative_empty,
                            "disposition": observation.disposition.value,
                            "request_key": observation.request_key,
                            "response": json.loads(evidence_json(observation.response)),
                            "response_digest": observation.response_digest,
                            "schema_version": observation.schema_version,
                        }
                        if observation is not None
                        else {
                            "applicability_reason": connection.execute(
                                "SELECT applicability_reason FROM tasks WHERE generation_id = ? AND task_key = ?",
                                (generation_id, key),
                            ).fetchone()[0]
                        }
                    ),
                }
                source_items.append(
                    {
                        "digest": evidence_digest(source_payload),
                        "key": f"late-source:{key}",
                        "kind": EvidenceKind.OBSERVATION.value,
                        "payload": source_payload,
                    }
                )
            snapshot = self._snapshot_for_discovery_pass(
                connection, generation_id, "late_identifiers", source_items=source_items
            )
            receipt = _execute_authoritative_pass("late_identifiers", snapshot)
            self._verify_late_identifier_snapshot(connection, generation_id, snapshot)
            existing = existing_pass_row
            if existing is not None:
                stored = PlannerPassReceipt(**json.loads(str(existing[0])))
                if not _receipt_matches_authority(stored, receipt):
                    raise ValueError("conflicting late identifier replay")
                return stored
            predecessor = connection.execute(
                "SELECT output_digest FROM planner_passes WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            predecessor_digest = str(predecessor[0]) if predecessor else None
            with self._authority_write():
                connection.execute(
                    "INSERT INTO planner_passes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        receipt.pass_key,
                        receipt.pass_id,
                        receipt.pass_version,
                        receipt.registry_digest,
                        receipt.snapshot_digest,
                        receipt.output_digest,
                        evidence_json(receipt.canonical_content()),
                        evidence_digest(
                            {
                                "domain": _SNAPSHOT_DOMAIN_SEPARATOR,
                                "generation_id": generation_id,
                                "pass_id": "late_identifiers",
                                "snapshot": snapshot,
                                "predecessor_output_digest": predecessor_digest,
                            }
                        ),
                        predecessor_digest,
                    ),
                )
                self._inject("after_c4_pass_receipt")
                connection.executemany(
                    "INSERT INTO planner_pass_expected_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            generation_id,
                            receipt.pass_key,
                            str(item["key"]),
                            str(item["kind"]),
                            str(item["digest"]),
                            evidence_json(item),
                            int(str(item["key"]) in receipt.unseen_keys),
                        )
                        for item in cast(Sequence[Mapping[str, object]], snapshot["items"])
                    ),
                )
                self._inject("after_c4_expected_items")
            sequence = int(generation[2]) + 1
            content = self._round_content(
                sequence,
                PlanPhase.LATE_IDENTIFIERS,
                "late_identifiers",
                policy.planner_version,
                source_keys,
                evidence_digest([item.digest for item in evidence]),
                (),
                (),
            )
            content_digest = _digest(content)
            round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    sequence,
                    round_key,
                    PlanPhase.LATE_IDENTIFIERS.value,
                    "late_identifiers",
                    policy.planner_version,
                    evidence_json(source_keys),
                    content["source_evidence_digest"],
                    content["task_set_digest"],
                    content_digest,
                    committed_at,
                ),
            )
            late_sources = tuple(sorted(decision.task.key for decision in openalex.decisions))
            late_durable = tuple(self._source_evidence(connection, generation_id, key) for key in late_sources)
            late_reduction_content = {
                "content_digest": content_digest,
                "source_dispositions": [item[0].value for item in late_durable],
                "source_evidence_digests": [item[1] for item in late_durable],
                "source_task_keys": list(late_sources),
            }
            late_reduction_digest = _digest(late_reduction_content)
            connection.execute(
                "INSERT INTO reduction_receipts(generation_id, reduction_digest, round_key, source_task_keys_json, "
                "source_dispositions_json, source_evidence_digests_json, committed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    late_reduction_digest,
                    round_key,
                    _canonical(late_sources),
                    _canonical([item[0].value for item in late_durable]),
                    _canonical([item[1] for item in late_durable]),
                    committed_at,
                ),
            )
            connection.executemany(
                "INSERT INTO reduction_sources(generation_id, source_task_key, reduction_digest) VALUES (?, ?, ?)",
                [(generation_id, key, late_reduction_digest) for key in late_sources],
            )
            self._inject("after_c4_round")
            cumulative = _digest(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT content_digest FROM plan_rounds WHERE generation_id = ? ORDER BY sequence",
                        (generation_id,),
                    )
                ]
            )
            connection.execute(
                "UPDATE generations SET plan_revision = ?, plan_digest = ?, updated_at = ? WHERE generation_id = ?",
                (sequence, cumulative, committed_at, generation_id),
            )
            return receipt

    def execute_and_commit_html_probe(self, policy: object, *, now: datetime) -> PlannerPassReceipt:
        """Commit the versioned HTML parent authority before bounded candidate waves."""
        from .capabilities import REGISTRY_DIGEST as CAPABILITY_REGISTRY_DIGEST
        from .discovery import DiscoveryPolicy

        if not isinstance(policy, DiscoveryPolicy):
            raise TypeError("HTML probe requires a typed discovery policy")
        generation_id = self._generation_id()
        committed_at = _timestamp(now)
        trusted_expected = self._trusted_corpus_expected()
        with self._transaction(immediate=True) as connection:
            self._verify_trusted_corpus(connection, trusted_expected)
            authority = self._load_discovery_authority(connection, generation_id)
            if authority.policy != policy:
                raise ValueError("HTML probe policy does not match bound authority")
            generation = connection.execute(
                "SELECT state, plan_closed, plan_revision, updated_at FROM generations WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value or int(generation[1]):
                raise ValueError("HTML probe requires an open running generation")
            late_row = connection.execute(
                "SELECT pass_key, output_digest FROM planner_passes WHERE generation_id = ? "
                "AND pass_id = 'late_identifiers'",
                (generation_id,),
            ).fetchone()
            if late_row is None:
                raise ValueError("HTML probe requires complete late identifier authority")
            if int(generation[2]) >= _MAX_PLAN_ROUNDS or policy.round_budget > _MAX_PLAN_ROUNDS:
                raise ValueError("HTML probe exceeds the fixed plan round budget")
            existing = connection.execute(
                "SELECT receipt_json FROM planner_passes WHERE generation_id = ? AND pass_id = 'html_probe'",
                (generation_id,),
            ).fetchone()
            latest_round = connection.execute(
                "SELECT committed_at FROM plan_rounds WHERE generation_id = ? ORDER BY sequence DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            if existing is None and (
                committed_at < str(generation[3]) or (latest_round is not None and committed_at < str(latest_round[0]))
            ):
                raise ValueError("HTML probe timestamp precedes durable generation history")
            source_items = [
                cast(Mapping[str, object], json.loads(str(row[0])))
                for row in connection.execute(
                    "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? AND pass_key = ? "
                    "AND item_key LIKE 'late-output:%' ORDER BY item_key",
                    (generation_id, str(late_row[0])),
                )
            ]
            seed_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM publication_seed_evidence WHERE generation_id = ?", (generation_id,)
                ).fetchone()[0]
            )
            if len(source_items) != seed_count:
                raise ValueError("HTML probe late identifier membership changed")
            unresolved_members = 0
            for envelope in source_items:
                payload = envelope.get("payload")
                candidates = payload.get("candidates") if isinstance(payload, Mapping) else None
                if not isinstance(candidates, Sequence):
                    raise ValueError("HTML probe late identifier output changed")
                has_doi = any(
                    isinstance(candidate, Mapping)
                    and candidate.get("kind") == "doi"
                    and candidate.get("identity_accepted") is True
                    for candidate in candidates
                )
                has_url = any(
                    isinstance(candidate, Mapping)
                    and candidate.get("kind") == "url_sha256"
                    and candidate.get("identity_accepted") is True
                    for candidate in candidates
                )
                unresolved_members += int(has_url and not has_doi)
            control_payload = {
                "authority_digest": authority.digest,
                "capability_registry_digest": CAPABILITY_REGISTRY_DIGEST,
                "late_output_digest": str(late_row[1]),
                "max_html_probe_waves": policy.max_html_probe_waves,
                "terminal": unresolved_members == 0 or policy.max_html_probe_waves == 0,
                "unresolved_members": unresolved_members,
                "web_adapter_version": "1",
            }
            source_items.append(
                {
                    "digest": evidence_digest(control_payload),
                    "key": f"html-control:{generation_id}",
                    "kind": EvidenceKind.REDUCTION_RECEIPT.value,
                    "payload": control_payload,
                }
            )
            snapshot = self._snapshot_for_discovery_pass(
                connection,
                generation_id,
                "html_probe",
                source_items=source_items,
            )
            self._verify_html_probe_snapshot(connection, generation_id, snapshot)
            receipt = _execute_authoritative_pass("html_probe", snapshot)
            if existing is not None:
                stored = PlannerPassReceipt(**json.loads(str(existing[0])))
                if not _receipt_matches_authority(stored, receipt):
                    raise ValueError("conflicting HTML probe replay")
                if not control_payload["terminal"]:
                    self._commit_html_probe_child(connection, generation_id, authority, stored, committed_at)
                return stored
            predecessor = connection.execute(
                "SELECT output_digest FROM planner_passes WHERE generation_id = ? ORDER BY rowid DESC LIMIT 1",
                (generation_id,),
            ).fetchone()
            predecessor_digest = str(predecessor[0]) if predecessor else None
            with self._authority_write():
                connection.execute(
                    "INSERT INTO planner_passes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        receipt.pass_key,
                        receipt.pass_id,
                        receipt.pass_version,
                        receipt.registry_digest,
                        receipt.snapshot_digest,
                        receipt.output_digest,
                        evidence_json(receipt.canonical_content()),
                        evidence_digest(
                            {
                                "domain": _SNAPSHOT_DOMAIN_SEPARATOR,
                                "generation_id": generation_id,
                                "pass_id": "html_probe",
                                "snapshot": snapshot,
                                "predecessor_output_digest": predecessor_digest,
                            }
                        ),
                        predecessor_digest,
                    ),
                )
                self._inject("after_c4_pass_receipt")
                connection.executemany(
                    "INSERT INTO planner_pass_expected_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        (
                            generation_id,
                            receipt.pass_key,
                            str(item["key"]),
                            str(item["kind"]),
                            str(item["digest"]),
                            evidence_json(item),
                            int(str(item["key"]) in receipt.unseen_keys),
                        )
                        for item in cast(Sequence[Mapping[str, object]], snapshot["items"])
                    ),
                )
                self._inject("after_c4_expected_items")
            sequence = int(generation[2]) + 1
            content = self._round_content(
                sequence,
                PlanPhase.LATE_IDENTIFIERS,
                "html_probe",
                policy.planner_version,
                (),
                receipt.snapshot_digest,
                (),
                (),
            )
            content_digest = _digest(content)
            round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                (
                    generation_id,
                    sequence,
                    round_key,
                    PlanPhase.LATE_IDENTIFIERS.value,
                    "html_probe",
                    policy.planner_version,
                    receipt.snapshot_digest,
                    content["task_set_digest"],
                    content_digest,
                    committed_at,
                ),
            )
            self._inject("after_c4_round")
            cumulative = _digest(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT content_digest FROM plan_rounds WHERE generation_id = ? ORDER BY sequence",
                        (generation_id,),
                    )
                ]
            )
            connection.execute(
                "UPDATE generations SET plan_revision = ?, plan_digest = ?, updated_at = ? WHERE generation_id = ?",
                (sequence, cumulative, committed_at, generation_id),
            )
            if control_payload["terminal"]:
                terminal_reason = (
                    "candidate_bound_exhausted"
                    if unresolved_members and policy.max_html_probe_waves == 0
                    else "no_probeable_members"
                )
                terminal_content = {
                    "completed_after_ordinal": None,
                    "parent_pass_key": receipt.pass_key,
                    "reason": terminal_reason,
                    "unresolved_members": unresolved_members,
                }
                with self._authority_write():
                    connection.execute(
                        "INSERT INTO html_probe_terminal_receipts VALUES (?, ?, NULL, ?, ?, ?)",
                        (
                            generation_id,
                            receipt.pass_key,
                            terminal_content["reason"],
                            evidence_digest(terminal_content),
                            committed_at,
                        ),
                    )
            return receipt

    @staticmethod
    def _html_task_content(task: TaskSpec) -> Mapping[str, object]:
        return {
            "applicability": task.applicability,
            "author_key": task.author_key,
            "operation": task.operation,
            "provider": task.provider,
            "publication_key": task.publication_key,
            "request": task.request.canonical_content() if task.request is not None else None,
            "required": task.required,
        }

    @staticmethod
    def _html_task_from_content(content: Mapping[str, object]) -> TaskSpec:
        request_value = content.get("request")
        request = RequestSpec(**dict(request_value)) if isinstance(request_value, Mapping) else None
        return TaskSpec(
            str(content["author_key"]),
            str(content["publication_key"]),
            str(content["provider"]),
            str(content["operation"]),
            request,
            bool(content["required"]),
            str(content["applicability"]),
        )

    @staticmethod
    def _derive_html_probe_wave(
        connection: sqlite3.Connection,
        generation_id: str,
        authority: object,
        ordinal: int,
    ) -> tuple[tuple[PublicationSeedEvidence, ...], object, Mapping[tuple[str, str], object]]:
        """Rederive one child wave and its private indexed URL source authority."""
        from .discovery import DiscoveryAuthority, DiscoveryDecision, DiscoveryObservation, DiscoveryWave
        from .publication_discovery import (
            _html_probe_candidates_by_member,
            derive_late_identifier_evidence,
            plan_html_probe_wave,
        )

        if not isinstance(authority, DiscoveryAuthority):
            raise TypeError("HTML child requires typed discovery authority")
        seeds: list[PublicationSeedEvidence] = []
        for row in connection.execute(
            "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
            "ORDER BY author_key, publication_key",
            (generation_id,),
        ):
            content = json.loads(str(row[0]))
            content["origin_kind"] = EvidenceKind(str(content["origin_kind"]))
            seeds.append(PublicationSeedEvidence(**content))
        waves: list[DiscoveryWave] = []
        observations: list[DiscoveryObservation] = []
        for planner_id in ("broad_discovery", "venue_fallback", "openalex_venue_expansion"):
            wave, terminal = Ledger._stored_discovery_wave(connection, generation_id, planner_id, authority.digest)
            waves.append(wave)
            observations.extend(terminal)
        parent = connection.execute(
            "SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = 'html_probe'",
            (generation_id,),
        ).fetchone()
        if parent is None:
            raise ValueError("HTML child requires parent authority")
        for prior in range(ordinal):
            wave_row = connection.execute(
                "SELECT wave_input_digest FROM html_probe_waves WHERE generation_id = ? AND parent_pass_key = ? "
                "AND ordinal = ?",
                (generation_id, str(parent[0]), prior),
            ).fetchone()
            if wave_row is None:
                raise ValueError("HTML child chronology changed")
            decisions: list[DiscoveryDecision] = []
            for item_row in connection.execute(
                "SELECT evidence_json, task_key FROM html_probe_wave_items WHERE generation_id = ? "
                "AND parent_pass_key = ? AND ordinal = ? ORDER BY author_key, publication_key",
                (generation_id, str(parent[0]), prior),
            ):
                item = json.loads(str(item_row[0]))
                task_content = item.get("task") if isinstance(item, Mapping) else None
                if not isinstance(task_content, Mapping):
                    raise ValueError("HTML child item authority changed")
                task = Ledger._html_task_from_content(task_content)
                reason_value = item.get("reason")
                reason = ApplicabilityReason(str(reason_value)) if reason_value else None
                decisions.append(DiscoveryDecision(task, reason))
                if task.request is None:
                    continue
                if item_row[1] != task.key:
                    raise ValueError("HTML child task membership changed")
                observation = connection.execute(
                    "SELECT disposition, response_json, schema_version, authoritative_empty FROM observations "
                    "WHERE generation_id = ? AND request_key = ?",
                    (generation_id, task.request.key),
                ).fetchone()
                if observation is None or TaskDisposition(str(observation[0])) not in _SATISFIED:
                    raise ValueError("HTML child requires complete predecessor evidence")
                observations.append(
                    DiscoveryObservation(
                        task,
                        TaskDisposition(str(observation[0])),
                        json.loads(str(observation[1])) if observation[1] is not None else {},
                        bool(observation[3]),
                        str(observation[2]),
                    )
                )
            waves.append(
                DiscoveryWave(
                    tuple(sorted(decisions, key=lambda item: item.task.key)), str(wave_row[0]), authority.digest
                )
            )
        ordered_observations = tuple(sorted(observations, key=lambda item: item.task.key))
        late = derive_late_identifier_evidence(seeds, waves, ordered_observations)
        wave = plan_html_probe_wave(seeds, late, waves, ordered_observations, ordinal, authority)
        candidates = _html_probe_candidates_by_member(seeds, waves, ordered_observations, late)
        return tuple(seeds), wave, candidates

    def _commit_html_probe_child(
        self,
        connection: sqlite3.Connection,
        generation_id: str,
        authority: object,
        parent: PlannerPassReceipt,
        committed_at: str,
    ) -> None:
        """Commit one ordinal's logical decisions and optional physical task round."""
        from .discovery import DiscoveryAuthority, DiscoveryWave

        if not isinstance(authority, DiscoveryAuthority):
            raise TypeError("HTML child requires typed discovery authority")
        existing_ordinals = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT ordinal FROM html_probe_waves WHERE generation_id = ? AND parent_pass_key = ? ORDER BY ordinal",
                (generation_id, parent.pass_key),
            )
        )
        if existing_ordinals != tuple(range(len(existing_ordinals))):
            raise ValueError("HTML child chronology changed")
        if (
            connection.execute(
                "SELECT 1 FROM html_probe_terminal_receipts WHERE generation_id = ? AND parent_pass_key = ?",
                (generation_id, parent.pass_key),
            ).fetchone()
            is not None
        ):
            return
        ordinal = len(existing_ordinals)
        if ordinal:
            states = [
                TaskDisposition(str(row[0]))
                for row in connection.execute(
                    "SELECT task.state FROM html_probe_wave_items AS item JOIN tasks AS task "
                    "ON task.generation_id = item.generation_id AND task.task_key = item.task_key "
                    "WHERE item.generation_id = ? AND item.parent_pass_key = ? AND item.ordinal = ?",
                    (generation_id, parent.pass_key, ordinal - 1),
                )
            ]
            if any(state in _TERMINAL - _SATISFIED for state in states):
                return
            if any(state not in _SATISFIED for state in states):
                return
        generation = connection.execute(
            "SELECT plan_revision, updated_at FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        latest = connection.execute(
            "SELECT committed_at FROM plan_rounds WHERE generation_id = ? ORDER BY sequence DESC LIMIT 1",
            (generation_id,),
        ).fetchone()
        if generation is None or committed_at < str(generation[1]) or (latest and committed_at < str(latest[0])):
            raise ValueError("HTML child timestamp precedes durable history")
        if ordinal >= authority.policy.max_html_probe_waves:
            terminal_content = {
                "completed_after_ordinal": ordinal - 1 if ordinal else None,
                "parent_pass_key": parent.pass_key,
                "reason": "candidate_bound_exhausted",
            }
            with self._authority_write():
                connection.execute(
                    "INSERT INTO html_probe_terminal_receipts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        parent.pass_key,
                        terminal_content["completed_after_ordinal"],
                        terminal_content["reason"],
                        evidence_digest(terminal_content),
                        committed_at,
                    ),
                )
            return
        _seeds, wave_value, candidates = self._derive_html_probe_wave(connection, generation_id, authority, ordinal)
        if not isinstance(wave_value, DiscoveryWave):
            raise TypeError("HTML child planner returned invalid wave")
        wave = wave_value
        planner_id = f"html_probe_candidate:{ordinal}"
        item_rows: list[tuple[str, str, str | None, str, str | None, str, str]] = []
        applicable: list[PlannedTask] = []
        decision_payloads: list[Mapping[str, object]] = []
        for decision in sorted(
            wave.decisions, key=lambda item: (item.task.author_key, item.task.publication_key or "")
        ):
            task = decision.task
            member = (task.author_key, task.publication_key or "")
            selected = cast(Sequence[object], candidates[member])[ordinal : ordinal + 1]
            selected_candidate = selected[0] if selected else None
            candidate_content = (
                {key: getattr(selected_candidate, key) for key in ("candidate_digest", "locators", "url_digest")}
                if selected_candidate is not None
                else None
            )
            payload: dict[str, object] = {
                "candidate": candidate_content,
                "ordinal": ordinal,
                "reason": decision.reason.value if decision.reason is not None else None,
                "task": self._html_task_content(task),
                "wave_input_digest": wave.input_digest,
            }
            item_digest = evidence_digest(payload)
            decision_payloads.append(payload)
            item_rows.append(
                (
                    task.author_key,
                    task.publication_key or "",
                    task.key if task.request is not None else None,
                    task.applicability,
                    decision.reason.value if decision.reason is not None else None,
                    evidence_json(payload),
                    item_digest,
                )
            )
            if task.request is not None:
                applicable.append(PlannedTask(task, expands_plan=False))
        predecessor_content = {
            "ordinal": ordinal,
            "parent_output_digest": parent.output_digest,
            "prior_receipts": [
                str(row[0])
                for row in connection.execute(
                    "SELECT receipt_digest FROM html_probe_waves WHERE generation_id = ? AND parent_pass_key = ? "
                    "ORDER BY ordinal",
                    (generation_id, parent.pass_key),
                )
            ],
        }
        predecessor_digest = evidence_digest(predecessor_content)
        decision_set_digest = evidence_digest(decision_payloads)
        terminal = not applicable
        round_key: str | None = None
        sequence = int(generation[0]) + 1
        if int(generation[0]) >= _MAX_PLAN_ROUNDS and applicable:
            raise ValueError("HTML child exceeds the fixed plan round budget")
        if applicable:
            content = self._round_content(
                sequence,
                PlanPhase.LATE_IDENTIFIERS,
                planner_id,
                authority.policy.planner_version,
                (),
                wave.input_digest,
                (),
                applicable,
            )
            content_digest = _digest(content)
            round_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            for planned in applicable:
                self._insert_task(connection, generation_id, planned.task, self._inject)
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, "
                "required, applicability, round_sequence, expands_plan) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    (
                        generation_id,
                        planned.task.key,
                        planned.task.identity_digest,
                        planned.task.author_key,
                        planned.task.provider,
                        planned.task.operation,
                        int(planned.task.required),
                        planned.task.applicability,
                        sequence,
                    )
                    for planned in applicable
                ),
            )
            connection.execute(
                "INSERT INTO plan_rounds VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)",
                (
                    generation_id,
                    sequence,
                    round_key,
                    PlanPhase.LATE_IDENTIFIERS.value,
                    planner_id,
                    authority.policy.planner_version,
                    wave.input_digest,
                    content["task_set_digest"],
                    content_digest,
                    committed_at,
                ),
            )
        receipt_content = {
            "decision_set_digest": decision_set_digest,
            "ordinal": ordinal,
            "parent_pass_key": parent.pass_key,
            "predecessor_digest": predecessor_digest,
            "round_key": round_key,
            "terminal": terminal,
            "wave_input_digest": wave.input_digest,
        }
        receipt_digest = evidence_digest(receipt_content)
        with self._authority_write():
            connection.execute(
                "INSERT INTO html_probe_waves VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    parent.pass_key,
                    ordinal,
                    wave.input_digest,
                    predecessor_digest,
                    decision_set_digest,
                    int(terminal),
                    round_key,
                    receipt_digest,
                    committed_at,
                ),
            )
            connection.executemany(
                "INSERT INTO html_probe_wave_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                ((generation_id, parent.pass_key, ordinal, *row) for row in item_rows),
            )
            if terminal:
                terminal_content = {
                    "completed_after_ordinal": ordinal,
                    "parent_pass_key": parent.pass_key,
                    "reason": "no_applicable_candidate",
                    "wave_receipt_digest": receipt_digest,
                }
                connection.execute(
                    "INSERT INTO html_probe_terminal_receipts VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        parent.pass_key,
                        ordinal,
                        terminal_content["reason"],
                        evidence_digest(terminal_content),
                        committed_at,
                    ),
                )
        if applicable:
            cumulative = _digest(
                [
                    row[0]
                    for row in connection.execute(
                        "SELECT content_digest FROM plan_rounds WHERE generation_id = ? ORDER BY sequence",
                        (generation_id,),
                    )
                ]
            )
            connection.execute(
                "UPDATE generations SET plan_revision = ?, plan_digest = ?, updated_at = ? WHERE generation_id = ?",
                (sequence, cumulative, committed_at, generation_id),
            )

    @staticmethod
    def _verify_html_probe_snapshot(
        connection: sqlite3.Connection, generation_id: str, snapshot: Mapping[str, object]
    ) -> None:
        """Prove the HTML parent is an exact projection of late identifier authority."""
        from .capabilities import REGISTRY_DIGEST as CAPABILITY_REGISTRY_DIGEST

        items = snapshot.get("items")
        if not isinstance(items, Sequence):
            raise ValueError("HTML probe snapshot items changed")
        actual_late = {
            str(item.get("key")): item
            for item in items
            if isinstance(item, Mapping) and str(item.get("key", "")).startswith("late-output:")
        }
        controls = [
            item for item in items if isinstance(item, Mapping) and str(item.get("key", "")).startswith("html-control:")
        ]
        predecessor = connection.execute(
            "SELECT pass_key, output_digest FROM planner_passes WHERE generation_id = ? "
            "AND pass_id = 'late_identifiers'",
            (generation_id,),
        ).fetchone()
        if predecessor is None or len(controls) != 1:
            raise ValueError("HTML probe predecessor authority changed")
        expected_late = {
            str(envelope["key"]): envelope
            for row in connection.execute(
                "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? AND pass_key = ? "
                "AND item_key LIKE 'late-output:%' ORDER BY item_key",
                (generation_id, str(predecessor[0])),
            )
            if isinstance((envelope := json.loads(str(row[0]))), Mapping)
        }
        if evidence_json(actual_late) != evidence_json(expected_late):
            raise ValueError("HTML probe late identifier membership changed")
        unresolved = 0
        for envelope in expected_late.values():
            payload = envelope.get("payload")
            candidates = payload.get("candidates") if isinstance(payload, Mapping) else None
            if not isinstance(candidates, Sequence):
                raise ValueError("HTML probe late identifier output changed")
            has_doi = any(
                isinstance(candidate, Mapping)
                and candidate.get("kind") == "doi"
                and candidate.get("identity_accepted") is True
                for candidate in candidates
            )
            has_url = any(
                isinstance(candidate, Mapping)
                and candidate.get("kind") == "url_sha256"
                and candidate.get("identity_accepted") is True
                for candidate in candidates
            )
            unresolved += int(has_url and not has_doi)
        authority = Ledger._load_discovery_authority(connection, generation_id)
        control_payload = {
            "authority_digest": authority.digest,
            "capability_registry_digest": CAPABILITY_REGISTRY_DIGEST,
            "late_output_digest": str(predecessor[1]),
            "max_html_probe_waves": authority.policy.max_html_probe_waves,
            "terminal": unresolved == 0 or authority.policy.max_html_probe_waves == 0,
            "unresolved_members": unresolved,
            "web_adapter_version": "1",
        }
        expected_control = {
            "digest": evidence_digest(control_payload),
            "key": f"html-control:{generation_id}",
            "kind": EvidenceKind.REDUCTION_RECEIPT.value,
            "payload": control_payload,
        }
        if evidence_json(controls[0]) != evidence_json(expected_control):
            raise ValueError("HTML probe control authority changed")

    @staticmethod
    def _verify_late_identifier_snapshot(
        connection: sqlite3.Connection, generation_id: str, snapshot: Mapping[str, object]
    ) -> None:
        """Rederive late candidates from complete immutable terminal source envelopes."""
        from .discovery import DiscoveryDecision, DiscoveryObservation, DiscoveryWave
        from .publication_discovery import derive_late_identifier_evidence

        items = snapshot.get("items")
        if not isinstance(items, Sequence):
            raise ValueError("late identifier snapshot items changed")
        seeds: list[PublicationSeedEvidence] = []
        sources: dict[str, Mapping[str, object]] = {}
        outputs: dict[tuple[str, str], Mapping[str, object]] = {}
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("payload"), Mapping):
                raise ValueError("late identifier snapshot envelope changed")
            key = str(item.get("key", ""))
            payload = cast(Mapping[str, object], item["payload"])
            if key.startswith("seed:"):
                content = dict(payload)
                content["origin_kind"] = EvidenceKind(str(content["origin_kind"]))
                seeds.append(PublicationSeedEvidence(**cast(dict, content)))
            elif key.startswith("late-source:"):
                task_key = str(payload.get("task_key", ""))
                if key != f"late-source:{task_key}" or task_key in sources:
                    raise ValueError("late identifier source membership changed")
                sources[task_key] = payload
            elif key.startswith("late-output:"):
                member = (str(payload.get("author_key", "")), str(payload.get("publication_key", "")))
                if member in outputs:
                    raise ValueError("late identifier output membership changed")
                outputs[member] = payload
        expected_sources: set[str] = set()
        for pass_id in ("broad_discovery", "venue_fallback"):
            pass_row = connection.execute(
                "SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = ?",
                (generation_id, pass_id),
            ).fetchone()
            if pass_row is None:
                raise ValueError("late identifier predecessor pass is absent")
            expected_sources.update(
                str(row[0])
                for row in connection.execute(
                    "SELECT json_extract(input_json, '$.payload.task_key') FROM planner_pass_expected_items "
                    "WHERE generation_id = ? AND pass_key = ? AND item_key LIKE 'decision:%'",
                    (generation_id, str(pass_row[0])),
                )
                if row[0] is not None
            )
        expected_sources.update(
            str(row[0])
            for row in connection.execute(
                "SELECT obligation.task_key FROM plan_obligations AS obligation JOIN plan_rounds AS round "
                "ON round.generation_id = obligation.generation_id AND round.sequence = obligation.round_sequence "
                "WHERE obligation.generation_id = ? AND round.planner_id = 'openalex_venue_expansion'",
                (generation_id,),
            )
        )
        if set(sources) != expected_sources:
            raise ValueError("late identifier source membership is incomplete")
        waves: dict[str, list[DiscoveryDecision]] = {"broad": [], "crossref": [], "openalex": []}
        observations: list[DiscoveryObservation] = []
        for task_key, payload in sorted(sources.items()):
            task = Ledger._load_task(connection, generation_id, task_key)
            live_request = task.request.key if task.request is not None else None
            expected_identity = {
                "applicability": task.applicability,
                "author_key": task.author_key,
                "identity_digest": task.identity_digest,
                "operation": task.operation,
                "provider": task.provider,
                "publication_key": task.publication_key,
                "request_key": live_request,
                "task_key": task.key,
            }
            if any(payload.get(key) != value for key, value in expected_identity.items()):
                raise ValueError("late identifier source identity changed")
            terminal = payload.get("terminal")
            if not isinstance(terminal, Mapping):
                raise ValueError("late identifier terminal evidence changed")
            reason = None
            if task.request is None:
                reason_value = terminal.get("applicability_reason")
                reason = ApplicabilityReason(str(reason_value)) if reason_value else None
            else:
                observation = DiscoveryObservation(
                    task,
                    TaskDisposition(str(terminal.get("disposition", ""))),
                    cast(Mapping[str, object], terminal.get("response", {})),
                    bool(terminal.get("authoritative_empty")),
                    str(terminal.get("schema_version", "")),
                )
                if (
                    terminal.get("request_key") != observation.request_key
                    or terminal.get("response_digest") != observation.response_digest
                ):
                    raise ValueError("late identifier observation evidence changed")
                observations.append(observation)
            bucket = (
                "crossref"
                if task.provider == "crossref" and task.operation == "venue_search"
                else "openalex"
                if task.provider == "openalex" and task.operation == "venue_search"
                else "broad"
            )
            waves[bucket].append(DiscoveryDecision(task, reason))
        authority = Ledger._load_discovery_authority(connection, generation_id)
        typed_waves = tuple(
            DiscoveryWave(tuple(waves[name]), evidence_digest(name), authority.digest)
            for name in ("broad", "crossref", "openalex")
        )
        derived = derive_late_identifier_evidence(seeds, typed_waves, observations)
        expected_outputs = {
            (item.author_key, item.publication_key): {
                "author_key": item.author_key,
                "candidates": [
                    {
                        "digest": candidate.digest,
                        "identity_accepted": candidate.identity_accepted,
                        "kind": candidate.kind,
                        "ordinal": candidate.ordinal,
                        "request_key": candidate.request_key,
                        "source_digest": candidate.source_digest,
                        "value": candidate.value,
                    }
                    for candidate in item.candidates
                ],
                "publication_key": item.publication_key,
            }
            for item in derived
        }
        if evidence_json(outputs) != evidence_json(expected_outputs):
            raise ValueError("late identifier output evidence is not independently derived")

    def _commit_venue_expansion(
        self,
        policy: object,
        receipt: PlannerPassReceipt,
        seeds: Sequence[PublicationSeedEvidence],
        authors: Mapping[str, str],
        crossref: object,
        *,
        now: datetime,
        _connection: sqlite3.Connection,
    ) -> None:
        """Commit the conditional OpenAlex expansion from exact Crossref decisions."""
        from .discovery import DiscoveryObservation, DiscoveryPolicy, DiscoveryWave
        from .publication_discovery import plan_openalex_venue_fallback

        if not isinstance(policy, DiscoveryPolicy) or not isinstance(crossref, DiscoveryWave):
            raise TypeError("venue expansion requires typed discovery evidence")
        connection = _connection
        generation_id = self._generation_id()
        authority = self._load_discovery_authority(connection, generation_id)
        if authority.policy != policy:
            raise ValueError("venue expansion policy does not match bound authority")
        observations: list[DiscoveryObservation] = []
        for decision in crossref.decisions:
            task = decision.task
            if task.request is None:
                continue
            stored = connection.execute(
                "SELECT observation.disposition, observation.response_json, observation.schema_version, "
                "observation.authoritative_empty FROM tasks AS task JOIN observations AS observation "
                "ON observation.generation_id = task.generation_id AND observation.request_key = task.request_key "
                "WHERE task.generation_id = ? AND task.task_key = ?",
                (generation_id, task.key),
            ).fetchone()
            if stored is None or TaskDisposition(str(stored[0])) not in _SATISFIED:
                raise ValueError("OpenAlex venue expansion requires terminal Crossref evidence")
            observations.append(
                DiscoveryObservation(
                    task,
                    TaskDisposition(str(stored[0])),
                    json.loads(str(stored[1])) if stored[1] is not None else {},
                    bool(stored[3]),
                    str(stored[2]),
                )
            )
        wave = plan_openalex_venue_fallback(seeds, authors, crossref, observations, authority)
        source_keys = tuple(sorted(decision.task.key for decision in crossref.decisions))
        durable_digests = tuple(self._source_evidence(connection, generation_id, key)[1] for key in source_keys)
        source_digest = durable_digests[0] if len(durable_digests) == 1 else _digest(list(durable_digests))
        tasks = tuple(PlannedTask(decision.task, expands_plan=True) for decision in wave.decisions)
        self.commit_reduction(
            source_keys,
            source_evidence_digest=source_digest,
            publications=(),
            tasks=tasks,
            now=now,
            reducer_id="openalex_venue_expansion",
            reducer_version=policy.reducer_version,
            _applicability_reasons={
                decision.task.key: decision.reason
                for decision in wave.decisions
                if decision.task.request is None and decision.reason is not None
            },
            _connection=connection,
            _allow_empty_sources=True,
            _fault_callback=self._inject,
        )
        self._inject("after_c4_expansion")

    def _commit_known_doi_expansion(
        self,
        policy: object,
        receipt: PlannerPassReceipt,
        *,
        now: datetime,
        _connection: sqlite3.Connection | None = None,
    ) -> None:
        from .discovery import (
            DiscoveryObservation,
            DiscoveryPolicy,
            plan_doi_bibtex,
            plan_known_doi,
        )

        if not isinstance(policy, DiscoveryPolicy):
            raise TypeError("discovery wave requires a typed policy")
        generation_id = self._generation_id()
        manager = self._transaction(immediate=True) if _connection is None else nullcontext(_connection)
        with manager as connection:
            authority = self._load_discovery_authority(connection, generation_id)
            if authority.policy != policy:
                raise ValueError("discovery wave policy does not match bound authority")
            seeds = []
            for row in connection.execute(
                "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
                "ORDER BY author_key, publication_key",
                (generation_id,),
            ):
                content = json.loads(str(row[0]))
                content["origin_kind"] = EvidenceKind(str(content["origin_kind"]))
                seeds.append(PublicationSeedEvidence(**content))
            known = plan_known_doi(seeds, authority)
            observations = []
            source_keys = []
            for decision in known.decisions:
                task = decision.task
                source_keys.append(task.key)
                if task.request is None:
                    continue
                stored = connection.execute(
                    "SELECT task.state, observation.disposition, observation.response_json, "
                    "observation.schema_version, observation.authoritative_empty "
                    "FROM tasks AS task LEFT JOIN observations AS observation "
                    "ON observation.generation_id = task.generation_id AND observation.request_key = task.request_key "
                    "WHERE task.generation_id = ? AND task.task_key = ?",
                    (generation_id, task.key),
                ).fetchone()
                if stored is None:
                    raise ValueError("known DOI task membership changed")
                disposition = TaskDisposition(str(stored[0]))
                if disposition not in _TERMINAL:
                    return
                if disposition not in _SATISFIED or stored[1] is None:
                    raise ValueError("terminal CSL evidence is blocking")
                response = json.loads(str(stored[2])) if stored[2] is not None else {}
                observations.append(
                    DiscoveryObservation(
                        task,
                        TaskDisposition(str(stored[1])),
                        response,
                        bool(stored[4]),
                        str(stored[3]),
                    )
                )
            existing = connection.execute(
                "SELECT 1 FROM plan_rounds WHERE generation_id = ? AND planner_id = 'doi_bibtex'",
                (generation_id,),
            ).fetchone()
            if existing is not None:
                return
            wave = plan_doi_bibtex(seeds, known, observations, authority)
            durable = [self._source_evidence(connection, generation_id, key)[1] for key in sorted(source_keys)]
            source_digest = durable[0] if len(durable) == 1 else _digest(durable)
            self.commit_reduction(
                tuple(source_keys),
                source_evidence_digest=source_digest,
                publications=(),
                tasks=tuple(PlannedTask(decision.task, expands_plan=True) for decision in wave.decisions),
                now=now,
                reducer_id="doi_bibtex",
                reducer_version=policy.reducer_version,
                _applicability_reasons={
                    decision.task.key: decision.reason
                    for decision in wave.decisions
                    if decision.task.request is None and decision.reason is not None
                },
                _connection=connection,
                _allow_empty_sources=True,
                _fault_callback=self._inject,
            )
            self._inject("after_c4_expansion")

    def _verify_structural_closure(self, connection: sqlite3.Connection, generation_id: str) -> None:
        sequences = [
            row[0]
            for row in connection.execute(
                "SELECT sequence FROM plan_rounds WHERE generation_id = ? ORDER BY sequence", (generation_id,)
            )
        ]
        if not sequences or sequences != list(range(1, len(sequences) + 1)):
            raise ValueError("plan rounds are missing or noncontiguous")
        for sequence in sequences:
            round_value = self._load_round(connection, generation_id, sequence)
            content = self._round_content(
                sequence,
                round_value.phase,
                round_value.planner_id,
                round_value.planner_version,
                round_value.source_task_keys,
                round_value.source_evidence_digest,
                round_value.publications,
                round_value.tasks,
            )
            content_digest = _digest(content)
            expected_key = _digest({"generation_id": generation_id, "content_digest": content_digest})
            if (
                content_digest != round_value.content_digest
                or content["task_set_digest"] != round_value.task_set_digest
                or expected_key != round_value.key
            ):
                raise ValueError("plan round content integrity mismatch")
        unbound = connection.execute(
            "SELECT COUNT(*) FROM tasks AS task LEFT JOIN plan_obligations AS obligation ON obligation.generation_id = "
            "task.generation_id AND obligation.task_key = task.task_key WHERE task.generation_id = ? AND "
            "(obligation.task_key IS NULL OR obligation.round_sequence IS NULL)",
            (generation_id,),
        ).fetchone()[0]
        if unbound:
            raise ValueError("plan contains unbound task")
        placeholders = ",".join("?" for _ in _SATISFIED)
        unsatisfied = connection.execute(
            f"SELECT COUNT(*) FROM plan_obligations AS obligation JOIN tasks AS task ON "  # noqa: S608
            f"task.generation_id = obligation.generation_id AND task.task_key = obligation.task_key WHERE "
            f"obligation.generation_id = ? AND task.state NOT IN ({placeholders})",
            (generation_id, *(state.value for state in sorted(_SATISFIED, key=lambda state: state.value))),
        ).fetchone()[0]
        if unsatisfied:
            raise ValueError("plan contains open or blocking work")
        for observation in connection.execute(
            "SELECT disposition, response_json, response_digest FROM observations WHERE generation_id = ?",
            (generation_id,),
        ):
            if observation[0] in {
                TaskDisposition.SUCCEEDED.value,
                TaskDisposition.CONFIRMED_EMPTY.value,
            } and (
                observation[1] is None
                or observation[2] is None
                or _digest(json.loads(observation[1])) != observation[2]
            ):
                raise ValueError("terminal observation content digest mismatch")
        expanding = {
            row[0]
            for row in connection.execute(
                "SELECT task_key FROM plan_obligations WHERE generation_id = ? AND expands_plan = 1",
                (generation_id,),
            )
        }
        consumed = [
            row[0]
            for row in connection.execute(
                "SELECT source_task_key FROM reduction_sources WHERE generation_id = ?", (generation_id,)
            )
        ]
        if len(consumed) != len(set(consumed)) or set(consumed) != expanding:
            raise ValueError("expanding task is unconsumed or multiply consumed")
        for key in consumed:
            disposition, evidence_digest = self._source_evidence(connection, generation_id, key)
            row = connection.execute(
                "SELECT receipt.source_dispositions_json, receipt.source_evidence_digests_json, "
                "receipt.source_task_keys_json FROM reduction_sources AS source JOIN reduction_receipts AS receipt ON "
                "receipt.generation_id = source.generation_id AND receipt.reduction_digest = source.reduction_digest "
                "WHERE source.generation_id = ? AND source.source_task_key = ?",
                (generation_id, key),
            ).fetchone()
            keys = json.loads(row[2])
            index = keys.index(key)
            if json.loads(row[0])[index] != disposition.value or json.loads(row[1])[index] != evidence_digest:
                raise ValueError("reduction receipt evidence mismatch")
        later_round_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT round_key FROM plan_rounds WHERE generation_id = ? AND sequence > 1",
                (generation_id,),
            )
        }
        reduction_round_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT round_key FROM reduction_receipts WHERE generation_id = ?",
                (generation_id,),
            )
        }
        discovery_round_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT round_key FROM plan_rounds WHERE generation_id = ? AND planner_id IN "
                "('known_doi', 'broad_discovery', 'dynamic_expansion', 'venue_fallback', "
                "'late_identifiers', 'html_probe')",
                (generation_id,),
            )
        }
        html_child_round_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT round_key FROM html_probe_waves WHERE generation_id = ? AND round_key IS NOT NULL",
                (generation_id,),
            )
        }
        if later_round_keys != reduction_round_keys | discovery_round_keys | html_child_round_keys:
            raise ValueError("noninitial round lacks exact reduction receipt")
        for pass_id in (
            "known_doi",
            "broad_discovery",
            "dynamic_expansion",
            "venue_fallback",
            "late_identifiers",
            "html_probe",
        ):
            pass_count = connection.execute(
                "SELECT COUNT(*) FROM planner_passes WHERE generation_id = ? AND pass_id = ?",
                (generation_id, pass_id),
            ).fetchone()[0]
            round_count = connection.execute(
                "SELECT COUNT(*) FROM plan_rounds WHERE generation_id = ? AND planner_id = ?",
                (generation_id, pass_id),
            ).fetchone()[0]
            if pass_count != round_count:
                raise ValueError("discovery pass and round membership is not bijective")
            if not pass_count:
                continue
            discovery_round = connection.execute(
                "SELECT round_key FROM plan_rounds WHERE generation_id = ? AND planner_id = ?",
                (generation_id, pass_id),
            ).fetchone()
            if discovery_round is None:
                raise ValueError("discovery pass lacks its exact round")
            round_key = str(discovery_round[0])
            has_reduction = round_key in reduction_round_keys
            if pass_id in {"known_doi", "venue_fallback", "html_probe"}:
                if has_reduction:
                    raise ValueError("pass-only discovery round cannot be a reduction round")
                continue
            if not has_reduction:
                raise ValueError("later discovery pass lacks its exact reduction receipt")
            receipt_row = connection.execute(
                "SELECT source_task_keys_json FROM reduction_receipts WHERE generation_id = ? AND round_key = ?",
                (generation_id, round_key),
            ).fetchone()
            if receipt_row is None:
                raise ValueError("later discovery reduction receipt is absent")
            actual_sources = tuple(json.loads(str(receipt_row[0])))
            if pass_id == "broad_discovery":  # noqa: S105 - planner pass identifier
                expected_sources = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT obligation.task_key FROM plan_obligations AS obligation "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE obligation.generation_id = ? "
                        "AND round.planner_id = 'doi_bibtex' ORDER BY obligation.task_key",
                        (generation_id,),
                    )
                )
            elif pass_id == "dynamic_expansion":  # noqa: S105 - planner pass identifier
                broad_pass = connection.execute(
                    "SELECT pass_key FROM planner_passes WHERE generation_id = ? AND pass_id = 'broad_discovery'",
                    (generation_id,),
                ).fetchone()
                if broad_pass is None:
                    raise ValueError("dynamic expansion lacks its broad predecessor")
                expected_sources = tuple(
                    sorted(
                        str(payload["task_key"])
                        for row in connection.execute(
                            "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? "
                            "AND pass_key = ? AND kind = ?",
                            (generation_id, str(broad_pass[0]), EvidenceKind.APPLICABILITY.value),
                        )
                        if isinstance((envelope := json.loads(str(row[0]))), Mapping)
                        and isinstance((payload := envelope.get("payload")), Mapping)
                        and isinstance(payload.get("task_key"), str)
                    )
                )
            else:
                expected_sources = tuple(
                    str(row[0])
                    for row in connection.execute(
                        "SELECT obligation.task_key FROM plan_obligations AS obligation "
                        "JOIN plan_rounds AS round ON round.generation_id = obligation.generation_id "
                        "AND round.sequence = obligation.round_sequence WHERE obligation.generation_id = ? "
                        "AND round.planner_id = 'openalex_venue_expansion' ORDER BY obligation.task_key",
                        (generation_id,),
                    )
                )
            if actual_sources != expected_sources:
                raise ValueError("discovery reduction predecessor membership changed")
        generation = connection.execute(
            "SELECT plan_closed, closure_digest FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()
        if generation[0]:
            actual_closure = _digest(dict(self.closure_content()))
            if generation[1] != actual_closure:
                raise ValueError("structural closure digest mismatch")

    def close_plan(
        self,
        *,
        expected_closure_digest: str,
        required_validations: Sequence[ValidationSpec] = (),
        inventory_freshness_epoch: str | None = None,
        now: datetime,
    ) -> PlanStatus:
        generation_id = self._generation_id()
        expected = _digest_text(expected_closure_digest, "closure digest")
        if len({item.name for item in required_validations}) != len(required_validations):
            raise ValueError("duplicate required validation")
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state, plan_closed, closure_digest, plan_authority_mode FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value:
                raise ValueError("structural plan closure requires running generation")
            if generation[1]:
                if generation[2] != expected or generation[3] != "phased_structural":
                    raise ValueError("conflicting plan closure replay")
                return self.plan_status()
            self._verify_structural_closure(connection, generation_id)
            if inventory_freshness_epoch is not None:
                current = connection.execute(
                    "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
                ).fetchone()[0]
                if current != inventory_freshness_epoch:
                    raise ValueError("inventory freshness epoch mismatch")
            candidate = _digest(dict(self.closure_content(required_validations)))
            if candidate != expected:
                raise ValueError("closure digest mismatch")
            connection.executemany(
                "INSERT INTO validation_obligations(generation_id, check_name, required) VALUES (?, ?, 1)",
                [(generation_id, item.name) for item in sorted(required_validations, key=lambda item: item.name)],
            )
            self._inject("after_plan_close_validations")
            connection.execute(
                "UPDATE generations SET plan_closed = 1, plan_sealed = 1, closure_digest = ?, plan_digest = ?, "
                "plan_authority_mode = 'phased_structural', updated_at = ? WHERE generation_id = ?",
                (expected, expected, _timestamp(now), generation_id),
            )
        self._inject("after_plan_close_commit")
        return self.plan_status()

    def plan_task(self, task: TaskSpec) -> TaskClaim:
        generation_id = self._generation_id()
        request_key = task.request.key if task.request is not None else None
        request_identity = _canonical(task.request.canonical_content()) if task.request is not None else None
        with self._transaction(immediate=True) as connection:
            sealed = connection.execute(
                "SELECT plan_sealed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if sealed is None or sealed[0]:
                raise ValueError("generation plan is sealed")
            if task.request is not None:
                connection.execute(
                    "INSERT OR IGNORE INTO requests(generation_id, request_key, identity_json, state) "
                    "VALUES (?, ?, ?, ?)",
                    (generation_id, request_key, request_identity, TaskDisposition.PENDING.value),
                )
                stored_request = connection.execute(
                    "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                    (generation_id, request_key),
                ).fetchone()
                if stored_request is None or stored_request[0] != request_identity:
                    raise ValueError("exact request identity collision")
            existing = connection.execute(
                "SELECT author_key, publication_key, provider, operation, request_key, required, applicability, "
                "identity_digest FROM tasks "
                "WHERE generation_id = ? AND task_key = ?",
                (generation_id, task.key),
            ).fetchone()
            expected = (
                task.author_key,
                task.publication_key,
                task.provider,
                task.operation,
                request_key,
                int(task.required),
                task.applicability,
                task.identity_digest,
            )
            if existing is not None and tuple(existing) != expected:
                raise ValueError("task identity mismatch")
            if existing is None:
                if task.publication_key is not None:
                    connection.execute(
                        "INSERT OR IGNORE INTO publications(generation_id, author_key, publication_key) "
                        "VALUES (?, ?, ?)",
                        (generation_id, task.author_key, task.publication_key),
                    )
                connection.execute(
                    "INSERT INTO tasks(generation_id, task_key, author_key, publication_key, provider, operation, "
                    "request_key, required, applicability, identity_digest, state) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (generation_id, task.key, *expected, TaskDisposition.PENDING.value),
                )
                if request_key is not None:
                    connection.execute(
                        "INSERT INTO request_consumers(generation_id, request_key, task_key) VALUES (?, ?, ?)",
                        (generation_id, request_key, task.key),
                    )
        return TaskClaim(task.key, request_key, "", datetime.min.replace(tzinfo=timezone.utc))

    def seal_plan(
        self,
        expected_tasks: list[TaskSpec],
        *,
        required_validations: tuple[ValidationSpec, ...] = (),
        inventory_freshness_epoch: str | None = None,
    ) -> None:
        generation_id = self._generation_id()
        declared = {task.key: task for task in expected_tasks}
        if len(declared) != len(expected_tasks):
            raise ValueError("duplicate expected obligations")
        with self._transaction(immediate=True) as connection:
            sealed = connection.execute(
                "SELECT plan_sealed FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if sealed is None or sealed[0]:
                raise ValueError("generation plan is sealed")
            actual = {
                row[0]: tuple(row[1:])
                for row in connection.execute(
                    "SELECT task_key, identity_digest, author_key, provider, operation, required, applicability "
                    "FROM tasks WHERE generation_id = ? ORDER BY task_key",
                    (generation_id,),
                )
            }
            expected = {
                task.key: (
                    task.identity_digest,
                    task.author_key,
                    task.provider,
                    task.operation,
                    int(task.required),
                    task.applicability,
                )
                for task in expected_tasks
            }
            if actual != expected:
                raise ValueError("expected obligations do not exactly match planned tasks")
            census_rows = list(
                connection.execute(
                    "SELECT row_key, scholar_id, dblp_id FROM authors WHERE generation_id = ? AND enabled = 1",
                    (generation_id,),
                )
            )
            generation_identity = json.loads(
                connection.execute(
                    "SELECT identity_json FROM generations WHERE generation_id = ?", (generation_id,)
                ).fetchone()[0]
            )
            mandatory_sources = [
                (row["row_key"], provider, provider_id)
                for row in census_rows
                for provider, provider_id in (("scholar", row["scholar_id"]), ("dblp", row["dblp_id"]))
                if provider_id
            ]
            if mandatory_sources and inventory_freshness_epoch is None:
                raise ValueError("canonical inventory obligations require an explicit freshness epoch")
            if inventory_freshness_epoch is not None:
                inventory_freshness_epoch = _identifier(inventory_freshness_epoch, "inventory freshness epoch")
            canonical_inventory: list[TaskSpec] = []
            for author_key, provider, profile_id in mandatory_sources:
                adapter_version = generation_identity["adapter_versions"].get(provider)
                if adapter_version is None:
                    raise ValueError(f"generation lacks adapter version for inventory provider {provider}")
                request = RequestSpec(
                    provider,
                    "inventory",
                    "GET",
                    {"profile_id": profile_id},
                    ("publications",),
                    adapter_version,
                    cast(str, inventory_freshness_epoch),
                    provider,
                )
                canonical_inventory.append(TaskSpec(author_key, None, provider, "inventory", request))
            declared_inventory = [task for task in expected_tasks if task.operation == "inventory"]
            if [task.key for task in sorted(declared_inventory, key=lambda item: item.key)] != [
                task.key for task in sorted(canonical_inventory, key=lambda item: item.key)
            ]:
                raise ValueError("declared tasks do not match full canonical inventory obligations")
            connection.executemany(
                "INSERT INTO plan_obligations(generation_id, task_key, identity_digest, author_key, provider, "
                "operation, required, applicability, round_sequence, expands_plan) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0)",
                [
                    (
                        generation_id,
                        task.key,
                        task.identity_digest,
                        task.author_key,
                        task.provider,
                        task.operation,
                        int(task.required),
                        task.applicability,
                    )
                    for task in sorted(expected_tasks, key=lambda item: item.key)
                ],
            )
            connection.executemany(
                "INSERT INTO validation_obligations(generation_id, check_name, required) VALUES (?, ?, 1)",
                [
                    (generation_id, validation.name)
                    for validation in sorted(required_validations, key=lambda item: item.name)
                ],
            )
            connection.execute(
                "UPDATE generations SET inventory_freshness_epoch = ? WHERE generation_id = ?",
                (inventory_freshness_epoch, generation_id),
            )
            plan_content = self._plan_content(connection, generation_id)
            content_digest = _digest(plan_content)
            round_key = _digest({"generation_id": generation_id, "legacy_plan": content_digest})
            connection.execute(
                "INSERT INTO plan_rounds(generation_id, sequence, round_key, phase, planner_id, planner_version, "
                "source_task_keys_json, source_evidence_digest, task_set_digest, content_digest, committed_at) "
                "VALUES (?, 1, ?, ?, 'legacy_adapter', '1', '[]', ?, ?, ?, ?)",
                (
                    generation_id,
                    round_key,
                    PlanPhase.INVENTORIES.value,
                    "0" * 64,
                    _digest(sorted(task.key for task in expected_tasks)),
                    content_digest,
                    _timestamp(datetime.now(timezone.utc)),
                ),
            )
            connection.execute(
                "UPDATE generations SET plan_sealed = 1, plan_closed = 1, plan_revision = 1, "
                "plan_authority_mode = 'legacy_compatibility', plan_digest = ?, closure_digest = ?, updated_at = ? "
                "WHERE generation_id = ?",
                (content_digest, content_digest, _timestamp(datetime.now(timezone.utc)), generation_id),
            )

    @staticmethod
    def _plan_content(connection: sqlite3.Connection, generation_id: str) -> dict[str, object]:
        tasks = []
        for row in connection.execute(
            "SELECT obligation.task_key, obligation.identity_digest, obligation.author_key, obligation.provider, "
            "obligation.operation, obligation.required, obligation.applicability, task.publication_key, "
            "request.identity_json FROM plan_obligations AS obligation JOIN tasks AS task ON task.generation_id = "
            "obligation.generation_id AND task.task_key = obligation.task_key LEFT JOIN requests AS request ON "
            "request.generation_id = task.generation_id AND request.request_key = task.request_key "
            "WHERE obligation.generation_id = ? ORDER BY obligation.task_key",
            (generation_id,),
        ):
            item = dict(row)
            identity_json = item.pop("identity_json")
            item["request"] = json.loads(identity_json) if identity_json is not None else None
            tasks.append(item)
        validations = [
            {"name": row[0], "required": bool(row[1])}
            for row in connection.execute(
                "SELECT check_name, required FROM validation_obligations WHERE generation_id = ? ORDER BY check_name",
                (generation_id,),
            )
        ]
        freshness = connection.execute(
            "SELECT inventory_freshness_epoch FROM generations WHERE generation_id = ?", (generation_id,)
        ).fetchone()[0]
        return {"inventory_freshness_epoch": freshness, "tasks": tasks, "validations": validations}

    @classmethod
    def _verify_plan_integrity(cls, connection: sqlite3.Connection, generation_id: str) -> None:
        generation = connection.execute(
            "SELECT plan_sealed, plan_digest, plan_authority_mode FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if generation is None or not generation[0]:
            return
        live_tasks = connection.execute(
            "SELECT task.task_key, task.identity_digest, task.author_key, task.publication_key, task.provider, "
            "task.operation, task.request_key, task.required, task.applicability, obligation.identity_digest AS "
            "obligation_digest, obligation.author_key AS obligation_author, obligation.provider AS "
            "obligation_provider, obligation.operation AS obligation_operation, obligation.required AS "
            "obligation_required, obligation.applicability AS obligation_applicability, request.identity_json "
            "FROM tasks AS task JOIN plan_obligations AS obligation ON obligation.generation_id = task.generation_id "
            "AND obligation.task_key = task.task_key LEFT JOIN requests AS request ON request.generation_id = "
            "task.generation_id AND request.request_key = task.request_key WHERE task.generation_id = ?",
            (generation_id,),
        ).fetchall()
        for task in live_tasks:
            request_key = task["request_key"]
            if request_key is not None and (
                task["identity_json"] is None or _digest(json.loads(task["identity_json"])) != request_key
            ):
                raise ValueError("sealed request identity mismatch")
            canonical_task = {
                "applicability": task["applicability"],
                "author_key": task["author_key"],
                "operation": task["operation"],
                "provider": task["provider"],
                "publication_key": task["publication_key"],
                "request_key": request_key,
                "required": bool(task["required"]),
            }
            if (
                _digest(canonical_task) != task["task_key"]
                or task["identity_digest"] != task["task_key"]
                or task["identity_digest"] != task["obligation_digest"]
                or task["author_key"] != task["obligation_author"]
                or task["provider"] != task["obligation_provider"]
                or task["operation"] != task["obligation_operation"]
                or task["required"] != task["obligation_required"]
                or task["applicability"] != task["obligation_applicability"]
            ):
                raise ValueError("sealed live task identity mismatch")
        if generation[2] == "legacy_compatibility" and generation[1] != _digest(
            cls._plan_content(connection, generation_id)
        ):
            raise ValueError("sealed plan obligation digest mismatch")

    def claim_due(self, owner: str, now: datetime, lease_for: timedelta) -> TaskClaim | None:
        return self.claim_due_for_operations(owner, now, lease_for, None)

    def claim_due_for_operations(
        self,
        owner: str,
        now: datetime,
        lease_for: timedelta,
        eligible: frozenset[str] | None,
    ) -> TaskClaim | None:
        """Claim one task only from the code-owned provider-operation phase set."""
        owner = _identifier(owner, "lease owner")
        if lease_for <= timedelta(0):
            raise ValueError("claim owner and positive lease are required")
        if eligible is not None:
            for task_key in eligible:
                _digest_text(task_key, "eligible task key")
            if not eligible:
                return None
        generation_id = self._generation_id()
        now_text = _timestamp(now)
        expires = now + lease_for
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value:
                return None
            eligibility_sql = ""
            eligibility_values: list[str] = []
            if eligible is not None:
                eligibility_sql = "task.task_key IN (" + ",".join("?" for _ in eligible) + ") AND "
                eligibility_values = sorted(eligible)
            query = (
                "SELECT task.task_key FROM tasks AS task JOIN plan_obligations AS obligation ON "  # noqa: S608
                "obligation.generation_id = task.generation_id AND obligation.task_key = task.task_key "
                "WHERE task.generation_id = ? AND obligation.round_sequence IS NOT NULL AND "
                "task.request_key IS NOT NULL AND " + eligibility_sql + "((task.state IN (?, ?) AND "
                "(task.next_attempt_at IS NULL OR task.next_attempt_at <= ?)) OR "
                "(task.state = ? AND task.lease_expires_at <= ?)) "
                "ORDER BY task.task_key LIMIT 1"
            )
            candidate = connection.execute(
                query,
                (
                    generation_id,
                    *eligibility_values,
                    TaskDisposition.PENDING.value,
                    TaskDisposition.RETRY_WAIT.value,
                    now_text,
                    TaskDisposition.LEASED.value,
                    now_text,
                ),
            ).fetchone()
            if candidate is None:
                return None
            cursor = connection.execute(
                "UPDATE tasks SET state = ?, lease_owner = ?, lease_expires_at = ? WHERE generation_id = ? "
                "AND task_key = ? AND ((state IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
                "OR (state = ? AND lease_expires_at <= ?))",
                (
                    TaskDisposition.LEASED.value,
                    owner,
                    _timestamp(expires),
                    generation_id,
                    candidate[0],
                    TaskDisposition.PENDING.value,
                    TaskDisposition.RETRY_WAIT.value,
                    now_text,
                    TaskDisposition.LEASED.value,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT request_key FROM tasks WHERE generation_id = ? AND task_key = ?",
                (generation_id, candidate[0]),
            ).fetchone()
        self._inject("after_claim_commit")
        return TaskClaim(str(candidate[0]), str(row[0]), owner, expires)

    def claim_request(self, task_key: str, owner: str, now: datetime, lease_for: timedelta) -> RequestClaim | None:
        task_key = _digest_text(task_key, "task key")
        owner = _identifier(owner, "lease owner")
        if lease_for <= timedelta(0):
            raise ValueError("positive request lease is required")
        generation_id = self._generation_id()
        now_text = _timestamp(now)
        expires = now + lease_for
        with self._transaction(immediate=True) as connection:
            generation = connection.execute(
                "SELECT state FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if generation is None or generation[0] != GenerationState.RUNNING.value:
                raise ValueError("request claims require a running generation")
            task = connection.execute(
                "SELECT request_key, lease_owner, lease_expires_at, state FROM tasks WHERE generation_id = ? "
                "AND task_key = ?",
                (generation_id, task_key),
            ).fetchone()
            if task is None or task[1] != owner or task[3] != TaskDisposition.LEASED.value or task[2] < now_text:
                raise ValueError("stale owner cannot claim request")
            cursor = connection.execute(
                "UPDATE requests SET state = ?, lease_owner = ?, lease_expires_at = ? WHERE generation_id = ? "
                "AND request_key = ? AND ((state IN (?, ?) AND (next_attempt_at IS NULL OR next_attempt_at <= ?)) "
                "OR (state = ? AND lease_expires_at <= ?))",
                (
                    TaskDisposition.LEASED.value,
                    owner,
                    _timestamp(expires),
                    generation_id,
                    task[0],
                    TaskDisposition.PENDING.value,
                    TaskDisposition.RETRY_WAIT.value,
                    now_text,
                    TaskDisposition.LEASED.value,
                    now_text,
                ),
            )
            if cursor.rowcount != 1:
                return None
        return RequestClaim(str(task[0]), owner, expires)

    def reconstruct_claimed_task(self, claim: TaskClaim, now: datetime) -> TaskSpec:
        """Reconstruct immutable task identity only for its current committed owner."""
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("tasks", "task_key", claim.key, claim.owner, now)
            stored_lease = connection.execute(
                "SELECT lease_expires_at FROM tasks WHERE generation_id = ? AND task_key = ?",
                (generation_id, claim.key),
            ).fetchone()
            if stored_lease is None or stored_lease[0] != _timestamp(claim.lease_expires):
                raise StaleClaimError("stale claim fencing token")
            row = connection.execute(
                "SELECT task.request_key, obligation.round_sequence FROM tasks AS task "
                "JOIN plan_obligations AS obligation ON obligation.generation_id = task.generation_id "
                "AND obligation.task_key = task.task_key WHERE task.generation_id = ? AND task.task_key = ?",
                (generation_id, claim.key),
            ).fetchone()
            if row is None or row[1] is None or row[0] != claim.request_key:
                raise ValueError("claim is not bound to an exact committed request")
            task = self._load_task(connection, generation_id, claim.key)
            if task.request is None or task.request.key != claim.request_key:
                raise ValueError("claimed request identity mismatch")
            return task

    def resolve_claimed_web_probe_url(self, claim: TaskClaim, *, now: datetime) -> str:
        """Resolve a leased web probe only from its committed private source authority."""
        from .capabilities import build_request
        from .publication_discovery import _accepted_identifiers, _normalized_response_records

        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("tasks", "task_key", claim.key, claim.owner, now)
            stored_lease = connection.execute(
                "SELECT lease_expires_at FROM tasks WHERE generation_id = ? AND task_key = ?",
                (generation_id, claim.key),
            ).fetchone()
            if stored_lease is None or stored_lease[0] != _timestamp(claim.lease_expires):
                raise StaleClaimError("stale claim fencing token")
            task = self._load_task(connection, generation_id, claim.key)
            if (
                task.provider != "web"
                or task.operation != "doi_probe"
                or task.request is None
                or task.request.key != claim.request_key
            ):
                raise ValueError("claim is not an exact committed web probe")
            item_row = connection.execute(
                "SELECT item.evidence_json, wave.wave_input_digest FROM html_probe_wave_items AS item "
                "JOIN html_probe_waves AS wave ON wave.generation_id = item.generation_id "
                "AND wave.parent_pass_key = item.parent_pass_key AND wave.ordinal = item.ordinal "
                "WHERE item.generation_id = ? AND item.task_key = ?",
                (generation_id, claim.key),
            ).fetchone()
            if item_row is None:
                raise ValueError("claimed web probe lacks committed private source authority")
            evidence = json.loads(str(item_row[0]))
            if not isinstance(evidence, Mapping) or evidence.get("wave_input_digest") != str(item_row[1]):
                raise ValueError("claimed web probe source authority changed")
            task_content = evidence.get("task")
            candidate = evidence.get("candidate")
            if (
                not isinstance(task_content, Mapping)
                or self._html_task_from_content(task_content) != task
                or not isinstance(candidate, Mapping)
                or candidate.get("url_digest") != task.request.normalized_payload.get("url_digest")
            ):
                raise ValueError("claimed web probe task authority changed")
            locators = candidate.get("locators")
            candidate_digest = candidate.get("candidate_digest")
            if not isinstance(locators, Sequence) or not isinstance(candidate_digest, str):
                raise ValueError("claimed web probe locator authority changed")
            matches: set[str] = set()
            for locator in locators:
                if (
                    not isinstance(locator, Sequence)
                    or isinstance(locator, (str, bytes))
                    or len(locator) != 3
                    or not isinstance(locator[0], str)
                    or not isinstance(locator[1], str)
                    or isinstance(locator[2], bool)
                    or not isinstance(locator[2], int)
                ):
                    raise ValueError("claimed web probe locator authority changed")
                response_digest, source_request_key, record_ordinal = locator
                source = connection.execute(
                    "SELECT observation.response_json, observation.response_digest, source.provider "
                    "FROM request_consumers AS consumer JOIN tasks AS source "
                    "ON source.generation_id = consumer.generation_id AND source.task_key = consumer.task_key "
                    "JOIN observations AS observation ON observation.generation_id = consumer.generation_id "
                    "AND observation.request_key = consumer.request_key "
                    "WHERE observation.generation_id = ? AND observation.request_key = ? "
                    "AND source.author_key = ? AND source.publication_key = ? LIMIT 1",
                    (
                        generation_id,
                        source_request_key,
                        task.author_key,
                        task.publication_key,
                    ),
                ).fetchone()
                if source is None or str(source[1]) != response_digest:
                    raise ValueError("claimed web probe source observation changed")
                response = json.loads(str(source[0])) if source[0] is not None else {}
                records = _normalized_response_records(str(source[2]), response)
                if record_ordinal < 0 or record_ordinal >= len(records):
                    raise ValueError("claimed web probe record ordinal changed")
                _identifiers, urls = _accepted_identifiers(records[record_ordinal])
                for raw_url in urls:
                    if evidence_digest({"scheme": "https", "url": raw_url}) != candidate_digest:
                        continue
                    built = build_request("web.doi_probe.v1", {"url": raw_url})
                    if built.identity_payload != task.request.normalized_payload:
                        continue
                    matches.add(raw_url)
            if len(matches) != 1:
                raise ValueError("claimed web probe raw URL authority changed")
            return next(iter(matches))

    def validate_claimed_inventory_request(self, task: TaskSpec) -> None:
        """Fail closed before wire construction when a claimed inventory is not census-authorized."""
        from .inventory import capability_for

        if task.operation != "inventory" or task.provider not in {"scholar", "dblp"} or task.request is None:
            raise ValueError("claim is not a canonical inventory request")
        generation_id = self._generation_id()
        author = self._connection.execute(
            "SELECT scholar_id, dblp_id, enabled FROM authors WHERE generation_id = ? AND row_key = ?",
            (generation_id, task.author_key),
        ).fetchone()
        generation = self._connection.execute(
            "SELECT identity_json, inventory_freshness_epoch FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if author is None or not author[2] or generation is None:
            raise ValueError("claimed inventory author is not enabled in the durable census")
        authority_record = self._load_inventory_policy_authority(self._connection, generation_id)
        if authority_record is None:
            raise ValueError("claimed inventory lacks typed policy authority")
        authority = cast(Mapping[str, object], authority_record["authority"])
        policy = cast(Mapping[str, object], authority["policy"])
        identity = json.loads(generation[0])
        request = task.request
        capability = capability_for(task.provider, "inventory", request.adapter_version)
        registered = cast(Sequence[Mapping[str, object]], authority["capabilities"])
        canonical_capability = cast(dict[str, object], _plain_json(capability.canonical_content()))
        if (
            request.provider != task.provider
            or request.operation != "inventory"
            or request.adapter_version != identity["adapter_versions"].get(task.provider)
            or request.freshness_epoch != generation[1]
            or request.requested_fields != capability.requested_fields
            or request.quota_scope != capability.quota_scope
            or canonical_capability not in registered
        ):
            raise ValueError("claimed inventory request does not match durable capability authority")
        payload = dict(request.normalized_payload)
        expected_identifier = author[0] if task.provider == "scholar" else author[1]
        expected_keys = (
            {"author_key", "profile_id", "start", "num", "sort", "min_year"}
            if task.provider == "scholar"
            else {"author_key", "pid"}
        )
        identifier = payload.get("profile_id") if task.provider == "scholar" else payload.get("pid")
        if (
            set(payload) != expected_keys
            or payload.get("author_key") != task.author_key
            or identifier != expected_identifier
        ):
            raise ValueError("claimed inventory request substitutes durable author identity")
        if task.provider == "scholar":
            start = payload.get("start")
            if (
                not isinstance(start, int)
                or isinstance(start, bool)
                or start < 0
                or start % 100
                or payload.get("num") != 100
                or payload.get("sort") != "pubdate"
                or payload.get("min_year") != policy["min_year"]
            ):
                raise ValueError("claimed Scholar request does not match durable policy authority")

    def load_inventory_snapshot(self, author_key: str) -> object:
        """Rebuild immutable inventory evidence exclusively from durable live rows."""
        from .inventory import InventorySnapshot, SnapshotContribution, capability_for

        author_key = _identifier(author_key, "author key")
        generation_id = self._generation_id()
        author = self._connection.execute(
            "SELECT scholar_id, dblp_id FROM authors WHERE generation_id = ? AND row_key = ? AND enabled = 1",
            (generation_id, author_key),
        ).fetchone()
        if author is None:
            raise ValueError("inventory snapshot requires an enabled census author")
        generation = self._connection.execute(
            "SELECT identity_json, inventory_freshness_epoch FROM generations WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        if generation is None or generation[1] is None:
            raise ValueError("inventory snapshot lacks generation authority")
        adapter_versions = json.loads(generation[0])["adapter_versions"]
        freshness_epoch = str(generation[1])
        expected_sources = {name for name, value in (("scholar", author[0]), ("dblp", author[1])) if value}
        rows = self._connection.execute(
            "SELECT task.task_key, task.provider, task.operation, task.request_key, task.state, "
            "request.identity_json, observation.response_json, observation.response_digest, "
            "observation.schema_version, observation.disposition FROM tasks AS task "
            "JOIN plan_obligations AS obligation ON obligation.generation_id = task.generation_id "
            "AND obligation.task_key = task.task_key JOIN requests AS request ON "
            "request.generation_id = task.generation_id AND request.request_key = task.request_key "
            "LEFT JOIN observations AS observation ON observation.generation_id = task.generation_id "
            "AND observation.request_key = task.request_key WHERE task.generation_id = ? AND task.author_key = ? "
            "AND task.operation = 'inventory' ORDER BY task.provider, task.task_key",
            (generation_id, author_key),
        ).fetchall()
        if {str(row[1]) for row in rows} != expected_sources:
            raise ValueError("inventory snapshot is missing an applicable source")
        if sum(row[1] == "dblp" for row in rows) > 1:
            raise ValueError("inventory snapshot has duplicate DBLP contributions")
        contributions = []
        scholar_topology: dict[int, int | None] = {}
        scholar_min_year: int | None = None
        for row in rows:
            disposition = TaskDisposition(str(row[4]))
            if disposition not in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY}:
                raise ValueError("inventory snapshot contains open or blocking work")
            if row[6] is None or row[7] is None or row[9] != disposition.value:
                raise ValueError("terminal inventory lacks exact observation evidence")
            response = json.loads(row[6])
            if not isinstance(response, dict) or _digest(response) != row[7]:
                raise ValueError("inventory observation digest mismatch")
            request = RequestSpec(**json.loads(row[5]))
            capability = capability_for(str(row[1]), "inventory", request.adapter_version)
            payload = dict(request.normalized_payload)
            expected_identifier = author[0] if row[1] == "scholar" else author[1]
            identifier_matches = (
                payload.get("profile_id") == expected_identifier
                if row[1] == "scholar"
                else payload.get("pid") == expected_identifier
            )
            if (
                request.requested_fields != capability.requested_fields
                or request.quota_scope != capability.quota_scope
                or adapter_versions.get(str(row[1])) != request.adapter_version
                or request.freshness_epoch != freshness_epoch
                or payload.get("author_key") != author_key
                or not identifier_matches
            ):
                raise ValueError("inventory request capability mismatch")
            if row[8] != capability.decoder_schema:
                raise ValueError("inventory observation decoder schema mismatch")
            start_value = payload.get("start")
            if row[1] == "scholar" and (isinstance(start_value, bool) or not isinstance(start_value, int)):
                raise ValueError("Scholar inventory offset is malformed")
            if row[1] == "scholar":
                page_min_year = payload.get("min_year")
                if (
                    payload.get("num") != 100
                    or payload.get("sort") != "pubdate"
                    or isinstance(page_min_year, bool)
                    or not isinstance(page_min_year, int)
                    or (scholar_min_year is not None and scholar_min_year != page_min_year)
                ):
                    raise ValueError("Scholar inventory request policy mismatch")
                scholar_min_year = page_min_year
            offset = start_value if isinstance(start_value, int) else None
            next_offset = response.get("next_offset") if row[1] == "scholar" else None
            if next_offset is not None and not isinstance(next_offset, int):
                raise ValueError("inventory continuation is malformed")
            if row[1] == "scholar":
                if offset is None:
                    raise ValueError("Scholar inventory offset is missing")
                if offset in scholar_topology:
                    raise ValueError("duplicate Scholar page offset")
                scholar_topology[offset] = next_offset
            articles = response.get("articles", [])
            if not isinstance(articles, list):
                raise ValueError("inventory evidence lacks normalized articles")
            topology = _digest({"offset": offset, "next_offset": next_offset, "task_key": row[0]})
            contributions.append(
                SnapshotContribution(
                    str(row[0]),
                    str(row[1]),
                    disposition,
                    capability.decoder_schema,
                    str(row[7]),
                    tuple(articles),
                    offset,
                    next_offset,
                    str(row[3]),
                    capability.capability_id,
                    topology,
                )
            )
        if scholar_topology:
            offset = 0
            visited = set()
            while offset in scholar_topology:
                if offset in visited:
                    raise ValueError("Scholar page chain cycles")
                visited.add(offset)
                next_offset = scholar_topology[offset]
                if next_offset is None:
                    break
                if next_offset != offset + 100:
                    raise ValueError("Scholar page chain is non-contiguous")
                offset = next_offset
            if visited != set(scholar_topology):
                raise ValueError("Scholar page chain has gaps or forks")
            if scholar_topology[next(iter(sorted(visited, reverse=True)))] is not None:
                raise ValueError("Scholar page chain is incomplete")
        return InventorySnapshot(author_key, tuple(contributions))

    def load_pending_scholar_wave(self) -> Mapping[str, tuple[object, ...]]:
        """Return terminal continuing Scholar pages not yet expansion-consumed."""
        from .inventory import SnapshotContribution, capability_for

        generation_id = self._generation_id()
        grouped: dict[str, list[SnapshotContribution]] = {}
        rows = self._connection.execute(
            "SELECT task.author_key, task.task_key, task.request_key, request.identity_json, "
            "observation.response_json, observation.response_digest, observation.schema_version, "
            "observation.disposition, author.scholar_id, generation.identity_json, "
            "generation.inventory_freshness_epoch FROM tasks AS task "
            "JOIN plan_obligations AS obligation ON obligation.generation_id = task.generation_id "
            "AND obligation.task_key = task.task_key JOIN requests AS request ON request.generation_id = "
            "task.generation_id AND request.request_key = task.request_key JOIN observations AS observation ON "
            "observation.generation_id = task.generation_id AND observation.request_key = task.request_key "
            "JOIN authors AS author ON author.generation_id = task.generation_id AND author.row_key = task.author_key "
            "JOIN generations AS generation ON generation.generation_id = task.generation_id "
            "LEFT JOIN reduction_sources AS consumed ON consumed.generation_id = task.generation_id "
            "AND consumed.source_task_key = task.task_key WHERE task.generation_id = ? AND task.provider = 'scholar' "
            "AND task.operation = 'inventory' AND consumed.source_task_key IS NULL AND task.state = ?",
            (generation_id, TaskDisposition.SUCCEEDED.value),
        ).fetchall()
        for row in rows:
            response = json.loads(row[4])
            if (
                row[7] != TaskDisposition.SUCCEEDED.value
                or _digest(response) != row[5]
                or not isinstance(row[8], str)
                or not isinstance(row[9], str)
            ):
                raise ValueError("Scholar page observation authority is malformed")
            next_offset = response.get("next_offset")
            if next_offset is None:
                continue
            request = RequestSpec(**json.loads(row[3]))
            capability = capability_for("scholar", "inventory", request.adapter_version)
            payload = dict(request.normalized_payload)
            generation_adapters = json.loads(row[9])["adapter_versions"]
            if (
                row[6] != capability.decoder_schema
                or request.requested_fields != capability.requested_fields
                or request.quota_scope != capability.quota_scope
                or request.adapter_version != generation_adapters.get("scholar")
                or request.freshness_epoch != row[10]
                or payload.get("author_key") != row[0]
                or payload.get("profile_id") != row[8]
                or payload.get("num") != 100
                or payload.get("sort") != "pubdate"
                or isinstance(payload.get("min_year"), bool)
                or not isinstance(payload.get("min_year"), int)
            ):
                raise ValueError("Scholar page request authority mismatch")
            topology = _digest({"offset": payload["start"], "next_offset": next_offset, "task_key": row[1]})
            start = payload.get("start")
            if isinstance(start, bool) or not isinstance(start, int) or not isinstance(next_offset, int):
                raise ValueError("Scholar page topology is malformed")
            articles = response.get("articles", [])
            if not isinstance(articles, list):
                raise ValueError("Scholar page evidence is malformed")
            contribution = SnapshotContribution(
                str(row[1]),
                "scholar",
                TaskDisposition.SUCCEEDED,
                capability.decoder_schema,
                str(row[5]),
                tuple(articles),
                start,
                next_offset,
                str(row[2]),
                capability.capability_id,
                topology,
            )
            grouped.setdefault(str(row[0]), []).append(contribution)
        return MappingProxyType(
            {key: tuple(sorted(value, key=lambda item: item.task_key)) for key, value in grouped.items()}
        )

    def _assert_owner(self, table: str, key_column: str, key: str, owner: str, at: datetime) -> sqlite3.Row:
        generation_id = self._generation_id()
        row = self._connection.execute(
            f"SELECT state, lease_owner, lease_expires_at FROM {table} "  # noqa: S608 - identifiers are internal constants
            f"WHERE generation_id = ? AND {key_column} = ?",
            (generation_id, key),
        ).fetchone()
        if row is None or row[0] != TaskDisposition.LEASED.value or row[1] != owner or row[2] < _timestamp(at):
            raise ValueError("stale owner cannot mutate leased work")
        return cast(sqlite3.Row, row)

    def record_attempt(
        self,
        request_key: str,
        owner: str,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        *,
        http_status: int | None = None,
        retry_delay: float | None = None,
        response_digest: str | None = None,
        safe_diagnostic: str = "",
    ) -> int:
        request_key = _digest_text(request_key, "request key")
        owner = _identifier(owner, "lease owner")
        diagnostic = _free_text(safe_diagnostic, "attempt diagnostic")
        outcome = _identifier(outcome, "attempt outcome")
        if http_status is not None and not 100 <= http_status <= 599:
            raise ValueError("invalid HTTP status")
        if retry_delay is not None and retry_delay < 0:
            raise ValueError("invalid retry delay")
        if response_digest is not None:
            _digest_text(response_digest, "response digest")
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("requests", "request_key", request_key, owner, finished_at)
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE generation_id = ? "
                    "AND request_key = ?",
                    (generation_id, request_key),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO attempts(generation_id, request_key, attempt_number, started_at, finished_at, outcome, "
                "http_status, retry_delay_seconds, response_digest, safe_diagnostic) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    request_key,
                    number,
                    _timestamp(started_at),
                    _timestamp(finished_at),
                    outcome,
                    http_status,
                    retry_delay,
                    response_digest,
                    diagnostic,
                ),
            )
            connection.execute(
                "UPDATE tasks SET attempt_count = attempt_count + 1 WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_key),
            )
        self._inject("after_attempt_commit")
        return number

    def mark_physical_send(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        started_at: datetime,
        *,
        idempotent: bool,
        resume_url: str | None = None,
    ) -> None:
        """Fence and persist intent immediately before a physical socket send."""
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("tasks", "task_key", task_claim.key, task_claim.owner, started_at)
            self._assert_owner("requests", "request_key", request_claim.key, request_claim.owner, started_at)
            existing = connection.execute(
                "SELECT idempotent, resolved_at FROM physical_send_markers WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_claim.key),
            ).fetchone()
            if existing is not None and existing[1] is None and not bool(existing[0]):
                raise ValueError("unresolved non-idempotent physical send cannot be repeated")
            connection.execute(
                "INSERT INTO physical_send_markers(generation_id, request_key, owner, started_at, idempotent, "
                "resume_url, resume_url_digest) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(generation_id, request_key) DO UPDATE SET "
                "owner = excluded.owner, started_at = excluded.started_at, idempotent = excluded.idempotent, "
                "resolved_at = NULL, resume_url = excluded.resume_url, resume_url_digest = excluded.resume_url_digest",
                (
                    generation_id,
                    request_claim.key,
                    task_claim.owner,
                    _timestamp(started_at),
                    int(idempotent),
                    resume_url,
                    hashlib.sha256(resume_url.encode()).hexdigest() if resume_url is not None else None,
                ),
            )

    def unresolved_physical_send(self, request_key: str) -> tuple[datetime, bool, str | None] | None:
        row = self._connection.execute(
            "SELECT started_at, idempotent, resume_url, resume_url_digest FROM physical_send_markers "
            "WHERE generation_id = ? AND request_key = ? "
            "AND resolved_at IS NULL",
            (self._generation_id(), _digest_text(request_key, "request key")),
        ).fetchone()
        if row is None:
            return None
        resume_url = str(row[2]) if row[2] is not None else None
        if resume_url is not None and hashlib.sha256(resume_url.encode()).hexdigest() != str(row[3]):
            raise ValueError("physical send resume authority changed")
        return datetime.fromisoformat(str(row[0])), bool(row[1]), resume_url

    def record_intermediate_attempt(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        http_status: int | None,
    ) -> None:
        """Record one completed network hop while retaining both exact leases."""
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("tasks", "task_key", task_claim.key, task_claim.owner, finished_at)
            self._assert_owner("requests", "request_key", request_claim.key, request_claim.owner, finished_at)
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts WHERE generation_id = ? "
                    "AND request_key = ?",
                    (generation_id, request_claim.key),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO attempts(generation_id, request_key, attempt_number, started_at, finished_at, outcome, "
                "http_status, safe_diagnostic) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    request_claim.key,
                    number,
                    _timestamp(started_at),
                    _timestamp(finished_at),
                    _identifier(outcome, "attempt outcome"),
                    http_status,
                    "validated DOI redirect hop",
                ),
            )
            connection.execute(
                "UPDATE tasks SET attempt_count = attempt_count + 1 WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_claim.key),
            )
            connection.execute(
                "UPDATE physical_send_markers SET resolved_at = ? WHERE generation_id = ? AND request_key = ?",
                (_timestamp(finished_at), generation_id, request_claim.key),
            )

    def complete_physical_attempt(
        self,
        task_claim: TaskClaim,
        request_claim: RequestClaim,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        disposition: TaskDisposition,
        *,
        http_status: int | None = None,
        retry_at: datetime | None = None,
        retry_delay: float | None = None,
        response_digest: str | None = None,
        observation: ProviderObservation | None = None,
        safe_diagnostic: str = "",
        task_reason: str = "",
        persist_attempt: bool = True,
    ) -> int:
        """Atomically fence and persist one physical request completion."""
        task_key = _digest_text(task_claim.key, "task key")
        request_key = _digest_text(request_claim.key, "request key")
        owner = _identifier(task_claim.owner, "lease owner")
        if request_claim.owner != owner or task_claim.request_key != request_key:
            raise ValueError("task and request claims do not identify the same leased work")
        outcome = _identifier(outcome, "attempt outcome")
        diagnostic = _free_text(safe_diagnostic, "attempt diagnostic")
        task_reason = _free_text(task_reason, "task reason")
        if disposition not in _TERMINAL | {TaskDisposition.RETRY_WAIT}:
            raise ValueError("invalid physical completion disposition")
        if disposition is TaskDisposition.RETRY_WAIT and retry_at is None:
            raise ValueError("retry wait requires a durable retry deadline")
        if http_status is not None and not 100 <= http_status <= 599:
            raise ValueError("invalid HTTP status")
        if retry_delay is not None and retry_delay < 0:
            raise ValueError("invalid retry delay")
        if disposition in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY} and observation is None:
            raise ValueError("successful physical completion requires provider evidence")
        if disposition is TaskDisposition.CONFIRMED_EMPTY and (
            observation is None or not observation.authoritative_empty or observation.response
        ):
            raise ValueError("confirmed empty requires authoritative empty evidence")
        if disposition is TaskDisposition.SUCCEEDED and observation is not None and observation.authoritative_empty:
            raise ValueError("successful request cannot use authoritative empty evidence")
        response_json: str | None = None
        if observation is not None:
            response_json = _canonical(dict(observation.response))
            if response_digest is not None and response_digest != observation.digest:
                raise ValueError("response digest does not match normalized response")
            response_digest = observation.digest
        if response_digest is not None:
            _digest_text(response_digest, "response digest")

        generation_id = self._generation_id()
        finished_text = _timestamp(finished_at)
        with self._transaction(immediate=True) as connection:
            task = connection.execute(
                "SELECT state, lease_owner, lease_expires_at, request_key FROM tasks "
                "WHERE generation_id = ? AND task_key = ?",
                (generation_id, task_key),
            ).fetchone()
            request = connection.execute(
                "SELECT state, lease_owner, lease_expires_at, identity_json FROM requests "
                "WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_key),
            ).fetchone()
            if (
                task is None
                or request is None
                or task[0] != TaskDisposition.LEASED.value
                or request[0] != TaskDisposition.LEASED.value
                or task[1] != owner
                or request[1] != owner
                or task[2] != _timestamp(task_claim.lease_expires)
                or request[2] != _timestamp(request_claim.lease_expires)
                or task[2] < finished_text
                or request[2] < finished_text
                or task[3] != request_key
            ):
                raise StaleClaimError("stale claim fencing token")
            identity = json.loads(request[3])
            if observation is not None and observation.provider != identity["provider"]:
                raise ValueError("observation provider does not match request")
            if disposition is TaskDisposition.SUCCEEDED and observation is not None:
                missing = [
                    field_name
                    for field_name in identity["requested_fields"]
                    if field_name not in observation.response
                    or (
                        not _has_observation_value(observation.response[field_name])
                        and not (
                            identity["provider"] == "web"
                            and identity["operation"] == "doi_probe"
                            and field_name == "doi"
                            and observation.response[field_name] is None
                        )
                    )
                ]
                if missing:
                    raise ValueError(f"successful observation lacks requested field evidence: {', '.join(missing)}")

            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM attempts "
                    "WHERE generation_id = ? AND request_key = ?",
                    (generation_id, request_key),
                ).fetchone()[0]
            )
            if persist_attempt:
                connection.execute(
                    "INSERT INTO attempts(generation_id, request_key, attempt_number, started_at, finished_at, "
                    "outcome, http_status, retry_delay_seconds, response_digest, safe_diagnostic) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        request_key,
                        number,
                        _timestamp(started_at),
                        finished_text,
                        outcome,
                        http_status,
                        retry_delay,
                        response_digest,
                        diagnostic,
                    ),
                )
                connection.execute(
                    "UPDATE tasks SET attempt_count = attempt_count + 1 WHERE generation_id = ? AND request_key = ?",
                    (generation_id, request_key),
                )
            connection.execute(
                "UPDATE requests SET state = ?, next_attempt_at = ?, lease_owner = NULL, lease_expires_at = NULL, "
                "response_digest = ?, safe_diagnostic = ? WHERE generation_id = ? AND request_key = ?",
                (
                    disposition.value,
                    _timestamp(retry_at) if retry_at else None,
                    response_digest,
                    diagnostic,
                    generation_id,
                    request_key,
                ),
            )
            if disposition in _TERMINAL:
                connection.execute(
                    "INSERT INTO observations(generation_id, request_key, disposition, response_json, response_digest, "
                    "provider, schema_version, authoritative_empty, observed_at, safe_diagnostic) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        request_key,
                        disposition.value,
                        response_json,
                        response_digest,
                        observation.provider if observation is not None else identity["provider"],
                        observation.schema_version if observation is not None else identity["adapter_version"],
                        int(observation.authoritative_empty) if observation is not None else 0,
                        finished_text,
                        diagnostic,
                    ),
                )
            connection.execute(
                "UPDATE tasks SET state = ?, reason = ?, next_attempt_at = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, last_error_class = ?, safe_diagnostic = ? "
                "WHERE generation_id = ? AND task_key = ?",
                (
                    disposition.value,
                    task_reason,
                    _timestamp(retry_at) if retry_at else None,
                    disposition.value if disposition not in _SATISFIED else None,
                    task_reason,
                    generation_id,
                    task_key,
                ),
            )
            connection.execute(
                "UPDATE physical_send_markers SET resolved_at = ? WHERE generation_id = ? AND request_key = ?",
                (finished_text, generation_id, request_key),
            )
        return number

    def finish_request(
        self,
        request_key: str,
        owner: str,
        disposition: TaskDisposition,
        now: datetime,
        *,
        retry_at: datetime | None = None,
        response_digest: str | None = None,
        observation: ProviderObservation | None = None,
        safe_diagnostic: str = "",
    ) -> None:
        request_key = _digest_text(request_key, "request key")
        owner = _identifier(owner, "lease owner")
        if disposition not in _TERMINAL | {TaskDisposition.RETRY_WAIT}:
            raise ValueError("invalid request finish disposition")
        if disposition is TaskDisposition.RETRY_WAIT and retry_at is None:
            raise ValueError("retry wait requires a durable retry deadline")
        diagnostic = _free_text(safe_diagnostic, "request diagnostic")
        if disposition in {TaskDisposition.SUCCEEDED, TaskDisposition.CONFIRMED_EMPTY} and observation is None:
            raise ValueError("terminal request requires a validated provider observation")
        if (
            disposition is TaskDisposition.CONFIRMED_EMPTY
            and observation is not None
            and not observation.authoritative_empty
        ):
            raise ValueError("confirmed empty requires authoritative empty observation")
        if disposition is TaskDisposition.SUCCEEDED and observation is not None and observation.authoritative_empty:
            raise ValueError("successful request cannot use authoritative empty observation")
        response_json: str | None = None
        if observation is not None:
            response_json = _canonical(dict(observation.response))
            calculated_digest = observation.digest
            if response_digest is not None and response_digest != calculated_digest:
                raise ValueError("response digest does not match normalized response")
            response_digest = calculated_digest
        generation_id = self._generation_id()
        with self._transaction(immediate=True) as connection:
            self._assert_owner("requests", "request_key", request_key, owner, now)
            request_identity = connection.execute(
                "SELECT identity_json FROM requests WHERE generation_id = ? AND request_key = ?",
                (generation_id, request_key),
            ).fetchone()
            if request_identity is None:
                raise ValueError("request identity missing")
            identity = json.loads(request_identity[0])
            if observation is not None and observation.provider != identity["provider"]:
                raise ValueError("observation provider does not match request")
            if disposition is TaskDisposition.SUCCEEDED and observation is not None:
                missing = [
                    field_name
                    for field_name in identity["requested_fields"]
                    if field_name not in observation.response
                    or (
                        not _has_observation_value(observation.response[field_name])
                        and not (
                            identity["provider"] == "web"
                            and identity["operation"] == "doi_probe"
                            and field_name == "doi"
                            and observation.response[field_name] is None
                        )
                    )
                ]
                if missing:
                    raise ValueError(f"successful observation lacks requested field evidence: {', '.join(missing)}")
            if disposition is TaskDisposition.CONFIRMED_EMPTY and observation is not None and observation.response:
                raise ValueError("confirmed empty requires an empty response")
            connection.execute(
                "UPDATE requests SET state = ?, next_attempt_at = ?, lease_owner = NULL, lease_expires_at = NULL, "
                "response_digest = ?, safe_diagnostic = ? WHERE generation_id = ? AND request_key = ?",
                (
                    disposition.value,
                    _timestamp(retry_at) if retry_at else None,
                    response_digest,
                    diagnostic,
                    generation_id,
                    request_key,
                ),
            )
            if disposition in _TERMINAL:
                connection.execute(
                    "INSERT INTO observations(generation_id, request_key, disposition, response_json, response_digest, "
                    "provider, schema_version, authoritative_empty, observed_at, safe_diagnostic) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        request_key,
                        disposition.value,
                        response_json,
                        response_digest,
                        observation.provider if observation is not None else identity["provider"],
                        observation.schema_version if observation is not None else identity["adapter_version"],
                        int(observation.authoritative_empty) if observation is not None else 0,
                        _timestamp(now),
                        diagnostic,
                    ),
                )
        self._inject("after_response_commit")

    def request_result(self, request_key: str) -> RequestResult | None:
        row = self._connection.execute(
            "SELECT observation.disposition, observation.response_json, observation.response_digest, "
            "attempt.outcome, attempt.http_status FROM observations AS observation LEFT JOIN attempts AS attempt "
            "ON attempt.generation_id = observation.generation_id AND attempt.request_key = observation.request_key "
            "AND attempt.attempt_number = (SELECT MAX(latest.attempt_number) FROM attempts AS latest WHERE "
            "latest.generation_id = observation.generation_id AND latest.request_key = observation.request_key) "
            "WHERE observation.generation_id = ? AND observation.request_key = ?",
            (self._generation_id(), request_key),
        ).fetchone()
        if row is None:
            return None
        response = MappingProxyType(json.loads(row[1])) if row[1] is not None else None
        return RequestResult(request_key, TaskDisposition(row[0]), response, row[2], row[3], row[4])

    def request_attempt_count(self, request_key: str) -> int:
        """Return the durable physical-attempt count for one exact request."""
        request_key = _digest_text(request_key, "request key")
        return int(
            self._connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE generation_id = ? AND request_key = ?",
                (self._generation_id(), request_key),
            ).fetchone()[0]
        )

    def finish_task(
        self,
        task_key: str,
        owner: str,
        disposition: TaskDisposition,
        now: datetime,
        *,
        retry_at: datetime | None = None,
        evidence: ApplicabilityReason | DominanceEvidence | None = None,
        reason: str = "",
    ) -> None:
        task_key = _digest_text(task_key, "task key")
        owner = _identifier(owner, "lease owner")
        generation_id = self._generation_id()
        current = self._connection.execute(
            "SELECT task.state, request.state FROM tasks AS task LEFT JOIN requests AS request "
            "ON request.generation_id = task.generation_id AND request.request_key = task.request_key "
            "WHERE task.generation_id = ? AND task.task_key = ?",
            (generation_id, task_key),
        ).fetchone()
        if current is not None and TaskDisposition(current[0]) in _TERMINAL:
            raise ValueError("terminal task state is immutable")
        if disposition not in _TERMINAL | {TaskDisposition.RETRY_WAIT}:
            raise ValueError("invalid task finish disposition")
        if disposition is TaskDisposition.RETRY_WAIT and retry_at is None:
            raise ValueError("retry wait requires a durable retry deadline")
        safe_reason = _free_text(reason, "task reason")
        with self._transaction(immediate=True) as connection:
            task_evidence = connection.execute(
                "SELECT applicability, request_key, state FROM tasks WHERE generation_id = ? AND task_key = ?",
                (generation_id, task_key),
            ).fetchone()
            if disposition is TaskDisposition.NOT_APPLICABLE:
                if (
                    task_evidence is None
                    or task_evidence[0] != "not_applicable"
                    or not isinstance(evidence, ApplicabilityReason)
                ):
                    raise ValueError("not applicable terminalization requires matching typed applicability evidence")
                if task_evidence[1] is not None or task_evidence[2] != TaskDisposition.PENDING.value:
                    raise ValueError("not applicable task must be pending without a request")
                applicability_reason = evidence.value
            else:
                self._assert_owner("tasks", "task_key", task_key, owner, now)
                applicability_reason = ""
            if disposition is TaskDisposition.DOMINATED:
                if not isinstance(evidence, DominanceEvidence):
                    raise ValueError("dominated terminalization requires typed dominance evidence")
                terminal_observations = [
                    connection.execute(
                        "SELECT request_key, provider, disposition, response_json FROM observations "
                        "WHERE generation_id = ? AND request_key = ?",
                        (generation_id, observation_key),
                    ).fetchone()
                    for observation_key in evidence.stronger_observation_keys
                ]
                terminal_observations = [item for item in terminal_observations if item is not None]
                if len(terminal_observations) != len(evidence.stronger_observation_keys) or any(
                    item["disposition"] != TaskDisposition.SUCCEEDED.value for item in terminal_observations
                ):
                    raise ValueError("dominance evidence must reference succeeded observations")
                lower_observation = connection.execute(
                    "SELECT observation.request_key, observation.provider, observation.disposition, "
                    "observation.response_json, request.identity_json FROM observations AS observation "
                    "JOIN requests AS request ON request.generation_id = observation.generation_id "
                    "AND request.request_key = observation.request_key "
                    "WHERE observation.generation_id = ? AND observation.request_key = ?",
                    (generation_id, evidence.dominated_observation_key),
                ).fetchone()
                if lower_observation is None or lower_observation["disposition"] != TaskDisposition.SUCCEEDED.value:
                    raise ValueError("dominance evidence requires a succeeded dominated observation")
                request_identity = connection.execute(
                    "SELECT request.identity_json, task.request_key, task.provider, task.author_key, "
                    "task.publication_key, task.operation FROM tasks AS task JOIN requests AS request ON "
                    "request.generation_id = task.generation_id AND request.request_key = task.request_key "
                    "WHERE task.generation_id = ? AND task.task_key = ?",
                    (generation_id, task_key),
                ).fetchone()
                if request_identity is None:
                    raise ValueError("dominated task requires a persisted exact request")
                lower_request_identity = json.loads(lower_observation["identity_json"])
                if (
                    evidence.dominated_observation_key != request_identity["request_key"]
                    or lower_observation["provider"] != request_identity["provider"]
                    or lower_request_identity["provider"] != request_identity["provider"]
                    or lower_request_identity["operation"] != request_identity["operation"]
                    or connection.execute(
                        "SELECT 1 FROM request_consumers WHERE generation_id = ? AND request_key = ? AND task_key = ?",
                        (generation_id, evidence.dominated_observation_key, task_key),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("dominated observation must be the terminalized logical task request")
                requested_fields = tuple(json.loads(request_identity["identity_json"])["requested_fields"])
                if tuple(sorted(evidence.covered_fields)) != tuple(sorted(requested_fields)):
                    raise ValueError("dominance covered fields must exactly match dominated requested fields")
                if not _merge_proves_dominance(
                    lower_observation["provider"],
                    json.loads(lower_observation["response_json"]),
                    [(item["provider"], json.loads(item["response_json"])) for item in terminal_observations],
                    evidence.covered_fields,
                    evidence.rule,
                ):
                    raise ValueError("live merge policy does not prove dominance for every covered field")
                connection.execute(
                    "INSERT INTO dominance_evidence(generation_id, task_key, stronger_observations_json, "
                    "dominated_observation_key, rule, covered_fields_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        task_key,
                        _canonical(sorted(evidence.stronger_observation_keys)),
                        evidence.dominated_observation_key,
                        evidence.rule.value,
                        _canonical(sorted(evidence.covered_fields)),
                    ),
                )
            if (
                current is not None
                and disposition not in {TaskDisposition.NOT_APPLICABLE, TaskDisposition.DOMINATED}
                and current[1] != disposition.value
            ):
                raise ValueError("task disposition does not match durable request disposition")
            connection.execute(
                "UPDATE tasks SET state = ?, reason = ?, applicability_reason = ?, next_attempt_at = ?, "
                "lease_owner = NULL, "
                "lease_expires_at = NULL, last_error_class = ?, safe_diagnostic = ? "
                "WHERE generation_id = ? AND task_key = ?",
                (
                    disposition.value,
                    safe_reason,
                    applicability_reason,
                    _timestamp(retry_at) if retry_at else None,
                    disposition.value if disposition not in _SATISFIED else None,
                    safe_reason,
                    generation_id,
                    task_key,
                ),
            )
        if disposition in _TERMINAL:
            self._inject("after_task_terminalization")

    def all_required_satisfied(self) -> bool:
        generation_id = self._generation_id()
        return self._all_required_satisfied(self._connection, generation_id)

    @staticmethod
    def _all_required_satisfied(connection: sqlite3.Connection, generation_id: str) -> bool:
        Ledger._verify_plan_integrity(connection, generation_id)
        # Task 5A deliberately owns structural planning only. Task 5B must add
        # independently executable planner/capability authority before any
        # generation can satisfy production discovery completeness.
        return False

    def record_checkpoint(self, sequence: int, ciphertext_digest: str, key_id: str, created_at: datetime) -> None:
        if sequence < 1:
            raise ValueError("checkpoint sequence must be positive")
        _digest_text(ciphertext_digest, "ciphertext digest")
        key_id = _identifier(key_id, "checkpoint key identifier")
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            self._verify_plan_integrity(connection, generation_id)
            self._verify_v6_relationships(connection, generation_id)
            connection.execute(
                "INSERT INTO checkpoints(generation_id, sequence, ciphertext_digest, key_id, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (generation_id, sequence, ciphertext_digest, key_id, _timestamp(created_at)),
            )
            connection.execute(
                "UPDATE generations SET checkpoint_sequence = ?, updated_at = ? WHERE generation_id = ? "
                "AND checkpoint_sequence < ?",
                (sequence, _timestamp(created_at), generation_id, sequence),
            )

    def record_publication(
        self,
        kind: str,
        commit_sha: str,
        created_at: datetime,
        *,
        candidate_digest: str,
        manifest_digest: str,
    ) -> None:
        kind = _identifier(kind, "publication evidence kind")
        if not re.fullmatch(r"[0-9a-f]{7,64}", commit_sha):
            raise ValueError("invalid publication commit SHA")
        _digest_text(candidate_digest, "candidate digest")
        _digest_text(manifest_digest, "publication manifest digest")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO publication_evidence(generation_id, kind, commit_sha, candidate_digest, "
                "manifest_digest, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    self._generation_id(),
                    kind,
                    commit_sha,
                    candidate_digest,
                    manifest_digest,
                    _timestamp(created_at),
                ),
            )

    def record_validation(
        self, check_name: str, state: EvidenceState, evidence_digest: str, safe_detail: str = ""
    ) -> None:
        check_name = _identifier(check_name, "validation check name")
        if not isinstance(state, EvidenceState) or state not in {
            EvidenceState.PENDING,
            EvidenceState.FAILED,
            EvidenceState.SUCCEEDED,
        }:
            raise ValueError("invalid validation state")
        _digest_text(evidence_digest, "validation evidence digest")
        detail = _free_text(safe_detail, "validation detail")
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            declared = connection.execute(
                "SELECT required FROM validation_obligations WHERE generation_id = ? AND check_name = ?",
                (generation_id, check_name),
            ).fetchone()
            if declared is None:
                raise ValueError("validation check is not a sealed obligation")
            connection.execute(
                "INSERT INTO validations(generation_id, check_name, state, evidence_digest, safe_detail) "
                "VALUES (?, ?, ?, ?, ?)",
                (generation_id, check_name, state.value, evidence_digest, detail),
            )

    def current_manifest_binding(self) -> str:
        return self.manifest().digest

    def record_materialization(self, evidence: MaterializationEvidence) -> None:
        staged_path = _free_text(evidence.staged_path, "staged path", required=True)
        _digest_text(evidence.manifest_digest, "materialization manifest digest")
        if evidence.validation_state is not EvidenceState.VALIDATED:
            raise ValueError("invalid materialization validation state")
        validation_state = evidence.validation_state.value
        counts = dict(evidence.corpus_counts)
        if any(not isinstance(value, int) or value < 0 for value in counts.values()):
            raise ValueError("invalid corpus counts")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT INTO materializations(generation_id, staged_path, manifest_digest, corpus_counts_json, "
                "validation_state) VALUES (?, ?, ?, ?, ?)",
                (self._generation_id(), staged_path, evidence.manifest_digest, _canonical(counts), validation_state),
            )

    def record_publication_metadata(self, metadata: PublicationMetadata) -> None:
        identifiers = dict(metadata.exact_identifiers)
        if _contains_secret(identifiers):
            raise ValueError("secret material cannot be persisted in publication identifiers")
        _freeze_json(identifiers)
        with self._transaction(immediate=True) as connection:
            closed = connection.execute(
                "SELECT plan_closed FROM generations WHERE generation_id = ?", (self._generation_id(),)
            ).fetchone()
            if closed is None or closed[0]:
                raise ValueError("closed plan rejects publication identity changes")
            connection.execute(
                "INSERT INTO publications(generation_id, author_key, publication_key, discovery_source, "
                "normalized_title, year, exact_identifiers_json, baseline_output_path, freshness_policy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(generation_id, author_key, publication_key) "
                "DO UPDATE SET discovery_source = excluded.discovery_source, "
                "normalized_title = excluded.normalized_title, "
                "year = excluded.year, exact_identifiers_json = excluded.exact_identifiers_json, "
                "baseline_output_path = excluded.baseline_output_path, freshness_policy = excluded.freshness_policy",
                (
                    self._generation_id(),
                    _identifier(metadata.author_key, "author key"),
                    _identifier(metadata.publication_key, "publication key"),
                    _provider(metadata.discovery_source),
                    _free_text(metadata.normalized_title, "normalized title", required=True),
                    metadata.year,
                    _canonical(identifiers),
                    _free_text(metadata.baseline_output_path, "baseline output path"),
                    _identifier(metadata.freshness_policy, "freshness policy"),
                ),
            )

    def record_provider_state(
        self,
        provider: str,
        quota_pool: str,
        current_concurrency: int,
        circuit_state: str,
        success_count: int,
        failure_count: int,
    ) -> None:
        if min(current_concurrency, success_count, failure_count) < 0:
            raise ValueError("provider state counts cannot be negative")
        with self._transaction(immediate=True) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO provider_state(generation_id, provider, quota_pool, current_concurrency, "
                "circuit_state, success_count, failure_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    self._generation_id(),
                    _provider(provider),
                    _identifier(quota_pool, "quota pool"),
                    current_concurrency,
                    _identifier(circuit_state, "circuit state"),
                    success_count,
                    failure_count,
                ),
            )

    def record_field_provenance(
        self,
        author_key: str,
        publication_key: str,
        field_name: str,
        selected_value_digest: str,
        provider: str,
        request_key: str,
        decision_rule: ProvenanceRule,
    ) -> None:
        if not _FIELD_RE.fullmatch(field_name):
            raise ValueError("invalid provenance field")
        _digest_text(selected_value_digest, "selected value digest")
        _digest_text(request_key, "request key")
        if not isinstance(decision_rule, ProvenanceRule):
            raise ValueError("invalid field provenance decision rule")
        with self._transaction(immediate=True) as connection:
            generation_id = self._generation_id()
            grounded = connection.execute(
                "SELECT observation.provider FROM observations AS observation JOIN tasks AS task ON "
                "task.generation_id = observation.generation_id AND task.request_key = observation.request_key "
                "JOIN publications AS publication ON publication.generation_id = task.generation_id AND "
                "publication.author_key = task.author_key AND publication.publication_key = task.publication_key "
                "WHERE observation.generation_id = ? AND observation.request_key = ? AND observation.disposition = ? "
                "AND publication.author_key = ? AND publication.publication_key = ?",
                (
                    generation_id,
                    request_key,
                    TaskDisposition.SUCCEEDED.value,
                    author_key,
                    publication_key,
                ),
            ).fetchone()
            if grounded is None:
                raise ValueError("field provenance requires a persisted succeeded observation and matching task")
            validated_provider = _provider(provider)
            if grounded[0] != validated_provider:
                raise ValueError("field provenance provider does not match succeeded observation")
            connection.execute(
                "INSERT INTO field_provenance(generation_id, author_key, publication_key, field_name, "
                "selected_value_digest, provider, request_key, decision_rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    generation_id,
                    _identifier(author_key, "author key"),
                    _identifier(publication_key, "publication key"),
                    field_name,
                    selected_value_digest,
                    validated_provider,
                    request_key,
                    decision_rule.value,
                ),
            )

    def manifest(self) -> LedgerManifest:
        trusted_expected = None
        if self._connection.execute("SELECT COUNT(*) FROM corpus_scan_receipts").fetchone()[0]:
            trusted_expected = self._trusted_corpus_expected()
        with self._transaction(immediate=True) as connection:
            if trusted_expected is not None:
                self._verify_trusted_corpus(connection, trusted_expected)
            self._assert_task5a_authority_invariant(connection)
            generation_id = self._generation_id()
            self._verify_plan_integrity(connection, generation_id)
            self._verify_v6_relationships(connection, generation_id)
            closure = connection.execute(
                "SELECT plan_closed, plan_authority_mode FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if closure[0] and closure[1] in {"phased_structural", "phased_authoritative"}:
                self._verify_structural_closure(connection, generation_id)
            generation = connection.execute(
                "SELECT generation_id, identity_json, census_digest, authors_digest, base_commit, input_digest, "
                "policy_digest, adapter_digest, state, created_at, updated_at, completed_at, published_at, "
                "checkpoint_sequence, blocking_reason, plan_sealed, plan_digest, completed_manifest_digest, "
                "inventory_freshness_epoch, plan_closed, discovery_closed, plan_revision, closure_digest, "
                "plan_authority_mode "
                "FROM generations "
                "WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            if self._manifest_probe is not None:
                probe = self._manifest_probe
                probe()
                self._manifest_probe = None
            data = self._manifest_data(connection, generation_id, generation)
            canonical_json = _canonical(data)
            digest = hashlib.sha256(canonical_json.encode()).hexdigest()
            connection.execute(
                "INSERT OR IGNORE INTO manifests(generation_id, digest, canonical_json) VALUES (?, ?, ?)",
                (generation_id, digest, canonical_json),
            )
        self._inject("after_manifest_commit")
        return LedgerManifest(data, canonical_json, digest)

    @staticmethod
    def _manifest_data(
        connection: sqlite3.Connection, generation_id: str, generation: sqlite3.Row
    ) -> dict[str, object]:
        census = [
            dict(row)
            for row in connection.execute(
                "SELECT row_key, physical_row, name, normalized_name, scholar_id, dblp_id, enabled, exclusion_reason, "
                "disposition FROM authors WHERE generation_id = ? ORDER BY row_key",
                (generation_id,),
            )
        ]
        requests = []
        for row in connection.execute(
            "SELECT request_key, identity_json, state, next_attempt_at, response_digest, safe_diagnostic "
            "FROM requests WHERE generation_id = ? ORDER BY request_key",
            (generation_id,),
        ):
            item = dict(row)
            item["identity"] = json.loads(item.pop("identity_json"))
            item["consumers"] = [
                consumer[0]
                for consumer in connection.execute(
                    "SELECT task_key FROM request_consumers WHERE generation_id = ? AND request_key = ? "
                    "ORDER BY task_key",
                    (generation_id, row["request_key"]),
                )
            ]
            requests.append(item)
        inventory_policy_authority = Ledger._load_inventory_policy_authority(connection, generation_id)
        data: dict[str, object] = {
            "attempts": [
                {
                    **{key: value for key, value in dict(row).items() if key != "attempt_number"},
                    "number": row["attempt_number"],
                }
                for row in connection.execute(
                    "SELECT request_key, attempt_number, started_at, finished_at, outcome, http_status, "
                    "retry_delay_seconds, response_digest, safe_diagnostic FROM attempts WHERE generation_id = ? "
                    "ORDER BY request_key, attempt_number",
                    (generation_id,),
                )
            ],
            "census": census,
            "checkpoints": [
                dict(row)
                for row in connection.execute(
                    "SELECT sequence, ciphertext_digest, key_id, created_at FROM checkpoints WHERE generation_id = ? "
                    "ORDER BY sequence",
                    (generation_id,),
                )
            ],
            "field_provenance": [
                dict(row)
                for row in connection.execute(
                    "SELECT author_key, publication_key, field_name, selected_value_digest, provider, request_key, "
                    "decision_rule FROM field_provenance WHERE generation_id = ? "
                    "ORDER BY author_key, publication_key, field_name",
                    (generation_id,),
                )
            ],
            "physical_send_markers": [
                dict(row)
                for row in connection.execute(
                    "SELECT request_key, owner, started_at, idempotent, resolved_at FROM physical_send_markers "
                    "WHERE generation_id = ? ORDER BY request_key",
                    (generation_id,),
                )
            ],
            "dominance_evidence": [
                {
                    "task_key": row["task_key"],
                    "stronger_observation_keys": json.loads(row["stronger_observations_json"]),
                    "dominated_observation_key": row["dominated_observation_key"],
                    "rule": row["rule"],
                    "covered_fields": json.loads(row["covered_fields_json"]),
                }
                for row in connection.execute(
                    "SELECT task_key, stronger_observations_json, dominated_observation_key, rule, "
                    "covered_fields_json FROM dominance_evidence WHERE generation_id = ? ORDER BY task_key",
                    (generation_id,),
                )
            ],
            "generation": {
                **{key: value for key, value in dict(generation).items() if key != "identity_json"},
                "identity": json.loads(generation["identity_json"]),
            },
            "inventory_policy_authority": inventory_policy_authority,
            "publications": [
                dict(row)
                for row in connection.execute(
                    "SELECT author_key, publication_key, discovery_source, normalized_title, year, "
                    "exact_identifiers_json, baseline_output_path, freshness_policy FROM publications "
                    "WHERE generation_id = ? "
                    "ORDER BY author_key, publication_key",
                    (generation_id,),
                )
            ],
            "publication_evidence": [
                dict(row)
                for row in connection.execute(
                    "SELECT kind, commit_sha, candidate_digest, manifest_digest, created_at "
                    "FROM publication_evidence WHERE generation_id = ? "
                    "ORDER BY kind, commit_sha",
                    (generation_id,),
                )
            ],
            "materializations": [
                dict(row)
                for row in connection.execute(
                    "SELECT staged_path, manifest_digest, corpus_counts_json, validation_state FROM materializations "
                    "WHERE generation_id = ? ORDER BY staged_path",
                    (generation_id,),
                )
            ],
            "inventory_authorities": [
                dict(row)
                for row in connection.execute(
                    "SELECT author_key, reducer_version, policy_digest, snapshot_digest, reduction_digest, "
                    "round_key FROM inventory_authorities WHERE generation_id = ? "
                    "ORDER BY author_key, reducer_version",
                    (generation_id,),
                )
            ],
            "inventory_contributions": [
                dict(row)
                for row in connection.execute(
                    "SELECT author_key, reducer_version, task_key, request_key, capability_id, disposition, "
                    "decoder_schema, observation_digest, page_offset, next_offset, topology_digest "
                    "FROM inventory_contributions WHERE generation_id = ? "
                    "ORDER BY author_key, reducer_version, task_key",
                    (generation_id,),
                )
            ],
            "observations": [
                dict(row)
                for row in connection.execute(
                    "SELECT request_key, disposition, response_digest, provider, schema_version, observed_at, "
                    "authoritative_empty, safe_diagnostic FROM observations WHERE generation_id = ? "
                    "ORDER BY request_key",
                    (generation_id,),
                )
            ],
            "plan_obligations": [
                dict(row)
                for row in connection.execute(
                    "SELECT task_key, identity_digest, author_key, provider, operation, required, applicability, "
                    "round_sequence, expands_plan "
                    "FROM plan_obligations WHERE generation_id = ? ORDER BY task_key",
                    (generation_id,),
                )
            ],
            "plan_rounds": [
                {
                    **{key: value for key, value in dict(row).items() if not key.endswith("_json")},
                    "source_task_keys": json.loads(row["source_task_keys_json"]),
                }
                for row in connection.execute(
                    "SELECT sequence, round_key, phase, planner_id, planner_version, source_task_keys_json, "
                    "source_evidence_digest, task_set_digest, content_digest, committed_at FROM plan_rounds "
                    "WHERE generation_id = ? ORDER BY sequence",
                    (generation_id,),
                )
            ],
            "reduction_receipts": [
                {
                    "reduction_digest": row["reduction_digest"],
                    "round_key": row["round_key"],
                    "source_task_keys": json.loads(row["source_task_keys_json"]),
                    "source_dispositions": json.loads(row["source_dispositions_json"]),
                    "source_evidence_digests": json.loads(row["source_evidence_digests_json"]),
                    "committed_at": row["committed_at"],
                }
                for row in connection.execute(
                    "SELECT * FROM reduction_receipts WHERE generation_id = ? ORDER BY round_key",
                    (generation_id,),
                )
            ],
            "provider_state": [
                dict(row)
                for row in connection.execute(
                    "SELECT provider, quota_pool, current_concurrency, rate_limit_deadline, circuit_state, "
                    "success_count, failure_count, async_job_id, request_digest FROM provider_state "
                    "WHERE generation_id = ? ORDER BY provider, quota_pool",
                    (generation_id,),
                )
            ],
            "requests": requests,
            "tasks": [
                dict(row)
                for row in connection.execute(
                    "SELECT task_key, identity_digest, author_key, publication_key, provider, operation, request_key, "
                    "required, applicability, applicability_reason, dominance_reason, state, reason, attempt_count, "
                    "last_error_class, safe_diagnostic, next_attempt_at, lease_owner, lease_expires_at "
                    "FROM tasks WHERE generation_id = ? "
                    "ORDER BY task_key",
                    (generation_id,),
                )
            ],
            "validations": [
                dict(row)
                for row in connection.execute(
                    "SELECT check_name, state, evidence_digest, safe_detail FROM validations WHERE generation_id = ? "
                    "ORDER BY check_name",
                    (generation_id,),
                )
            ],
            "validation_obligations": [
                dict(row)
                for row in connection.execute(
                    "SELECT check_name, required FROM validation_obligations WHERE generation_id = ? "
                    "ORDER BY check_name",
                    (generation_id,),
                )
            ],
            "task5c_evidence": Ledger._v6_evidence_content(connection, generation_id),
        }
        return data

    @staticmethod
    def _v6_evidence_content(connection: sqlite3.Connection, generation_id: str) -> dict[str, object]:
        specs = {
            "corpus_scan_receipts": "snapshot_digest",
            "corpus_snapshots": "snapshot_digest",
            "discovery_policy_authority": "policy_digest",
            "corpus_items": "source_path COLLATE NOCASE",
            "publication_seed_evidence": "author_key, publication_key",
            "aggregate_inputs": "pass_key, reduction_id, ordinal",
            "planner_passes": "pass_key",
            "planner_pass_expected_items": "pass_key, item_key",
            "provenance_decisions": "decision_key",
            "provenance_contributions": "contribution_key",
            "materialization_intents": "intent_key",
            "intent_provenance": "intent_key, decision_key",
            "html_probe_waves": "parent_pass_key, ordinal",
            "html_probe_wave_items": "parent_pass_key, ordinal, author_key, publication_key",
            "html_probe_terminal_receipts": "parent_pass_key",
        }
        result: dict[str, object] = {}
        for table, order_by in specs.items():
            if (
                connection.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
                is None
            ):
                if table in {
                    "corpus_scan_receipts",
                    "discovery_policy_authority",
                    "html_probe_waves",
                    "html_probe_wave_items",
                    "html_probe_terminal_receipts",
                }:
                    result[table] = []
                    continue
                raise ValueError(f"missing Task5C evidence table: {table}")
            values: list[dict[str, object]] = []
            for row in connection.execute(
                f"SELECT * FROM {table} WHERE generation_id = ? ORDER BY {order_by}",  # noqa: S608
                (generation_id,),
            ):
                item = {key: value for key, value in dict(row).items() if key != "generation_id"}
                for key, value in tuple(item.items()):
                    if key.endswith("_json") and value is not None:
                        try:
                            parsed = json.loads(str(value))
                        except json.JSONDecodeError as exc:
                            raise ValueError(f"corrupt Task5C evidence JSON in {table}.{key}") from exc
                        if _canonical(parsed) != str(value):
                            raise ValueError(f"noncanonical Task5C evidence JSON in {table}.{key}")
                        item[key.removesuffix("_json")] = parsed
                        del item[key]
                Ledger._validate_v6_evidence_row(table, item)
                values.append(item)
            result[table] = values
        return result

    @staticmethod
    def _verify_v6_relationships(connection: sqlite3.Connection, generation_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM inventory_authorities WHERE generation_id = ? LIMIT 1", (generation_id,)
            ).fetchone()
            is not None
        ):
            Ledger._inventory_authority_maps(connection, generation_id)
        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'discovery_policy_authority'"
            ).fetchone()
            is not None
            and connection.execute(
                "SELECT 1 FROM discovery_policy_authority WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            is not None
        ):
            Ledger._load_discovery_authority(connection, generation_id)
        Ledger._verify_html_probe_children(connection, generation_id)
        snapshot = connection.execute(
            "SELECT snapshot_digest, item_set_digest, evidence_json FROM corpus_snapshots WHERE generation_id = ?",
            (generation_id,),
        ).fetchone()
        snapshot_evidence: CorpusSnapshot | None = None
        if snapshot is not None:
            from .corpus import corpus_author_set_digest

            try:
                snapshot_evidence = CorpusSnapshot(**json.loads(str(snapshot[2])))
            except (json.JSONDecodeError, TypeError, ValueError) as exc:
                raise ValueError("corrupt corpus snapshot evidence JSON") from exc
            generation_base = connection.execute(
                "SELECT base_commit FROM generations WHERE generation_id = ?", (generation_id,)
            ).fetchone()
            if (
                snapshot_evidence.generation_id != generation_id
                or generation_base is None
                or snapshot_evidence.base_commit != str(generation_base[0])
            ):
                raise ValueError("corpus snapshot generation or base commit authority changed")
            if snapshot_evidence.author_set_digest != "0" * 64 and snapshot_evidence.author_set_digest != (
                corpus_author_set_digest(Ledger._author_census_rows(connection, generation_id))
            ):
                raise ValueError("corpus snapshot author-set authority changed")
            enabled = {
                str(row[0])
                for row in connection.execute(
                    "SELECT row_key FROM authors WHERE generation_id = ? AND enabled = 1", (generation_id,)
                )
            }
            items = connection.execute(
                "SELECT author_key, evidence_digest, source_path, disposition, evidence_json FROM corpus_items "
                "WHERE generation_id = ? "
                "ORDER BY source_path COLLATE NOCASE",
                (generation_id,),
            ).fetchall()
            if {str(row[0]) for row in items} != enabled or evidence_digest([str(row[1]) for row in items]) != str(
                snapshot[1]
            ):
                raise ValueError("corpus snapshot membership is incomplete or substituted")
            expected_dirs = {
                row.row_key: format_author_dirname(row.name, row.scholar_id or row.dblp_id)
                for row in Ledger._author_census_rows(connection, generation_id)
                if row.enabled
            }
            for row in items if snapshot_evidence.author_set_digest != "0" * 64 else ():
                if str(row[3]) not in {"parsed", "absent"}:
                    raise ValueError("corpus item disposition is not scanner-owned")
                expected_prefix = f"output/{expected_dirs[str(row[0])]}"
                expected_path = (
                    f"{expected_prefix}/.citeforge-absent-directory" if str(row[3]) == "absent" else str(row[2])
                )
                path_parts = PurePosixPath(str(row[2])).parts
                if expected_path != str(row[2]) or (
                    str(row[3]) == "parsed"
                    and (
                        path_parts[:2] != ("output", expected_dirs[str(row[0])])
                        or len(path_parts) != 3
                        or path_parts[2] == ".bib"
                        or not path_parts[2].endswith(".bib")
                    )
                ):
                    raise ValueError("corpus item path does not match code-owned author directory")
                if str(row[3]) == "absent":
                    absent = CorpusItemEvidence(**json.loads(str(row[4])))
                    absent_digest = evidence_digest({"disposition": "absent", "path": expected_prefix, "version": "1"})
                    if (
                        absent.before_digest != absent_digest
                        or absent.parse_digest != absent_digest
                        or absent.publication_keys
                        or absent.exact_identifiers
                        or absent.normalized_entry
                    ):
                        raise ValueError("corpus absence evidence is not code-derived")
        seed_members: dict[tuple[str, str], EvidenceKind] = {}
        inventory_seed_cache: dict[tuple[str, str], PublicationSeedEvidence] = {}
        corpus_origins: dict[str, CorpusItemEvidence] = {}
        if snapshot_evidence is not None:
            corpus_origins = {
                item.key: item
                for row in connection.execute(
                    "SELECT evidence_json FROM corpus_items WHERE generation_id = ?", (generation_id,)
                )
                for item in (CorpusItemEvidence(**json.loads(str(row[0]))),)
            }
            stored_publications = {
                (str(row[0]), str(row[1])): tuple(row[2:])
                for row in connection.execute(
                    "SELECT author_key, publication_key, discovery_source, normalized_title, year, "
                    "exact_identifiers_json, baseline_output_path, freshness_policy FROM publications "
                    "WHERE generation_id = ?",
                    (generation_id,),
                )
            }
            for item in corpus_origins.values() if snapshot_evidence.author_set_digest != "0" * 64 else ():
                if item.disposition != "parsed":
                    continue
                if set(item.normalized_entry) != {"type", "key", "fields"}:
                    raise ValueError("corpus normalized entry schema is not code-owned")
                fields = item.normalized_entry.get("fields")
                if not isinstance(fields, Mapping):
                    raise ValueError("corpus normalized entry fields are absent")
                expected_identifiers = _corpus_identifiers_from_fields(fields)
                if dict(item.exact_identifiers) != expected_identifiers:
                    raise ValueError("corpus item identifiers are not independently derived")
                expected_title = normalize_title(str(fields.get("title", "")))
                expected_year = extract_year_from_any(fields.get("year"), fallback=0) or None
                expected_key = _publication_key_authority(
                    item.author_key,
                    expected_title,
                    expected_year,
                    str(expected_identifiers.get("doi", "")) or None,
                )
                expected_publication_row = (
                    "corpus",
                    expected_title,
                    expected_year,
                    evidence_json(expected_identifiers),
                    item.source_path,
                    "monthly",
                )
                stored_row = stored_publications.get((item.author_key, expected_key))
                inventory_row: tuple[object, ...] | None = None
                try:
                    inventory_seed = Ledger._inventory_publication_seed(
                        connection, generation_id, item.author_key, expected_key, {}
                    )
                except ValueError:
                    pass
                else:
                    baseline_fields = inventory_seed.baseline_entry.get("fields")
                    if isinstance(baseline_fields, Mapping):
                        matching_providers = set()
                        for provider_row in connection.execute(
                            "SELECT task.provider, observation.response_json FROM inventory_contributions contribution "
                            "JOIN tasks task ON task.generation_id = contribution.generation_id "
                            "AND task.task_key = contribution.task_key JOIN observations observation "
                            "ON observation.generation_id = contribution.generation_id "
                            "AND observation.request_key = contribution.request_key "
                            "WHERE contribution.generation_id = ? AND contribution.author_key = ?",
                            (generation_id, item.author_key),
                        ):
                            response = json.loads(str(provider_row[1]))
                            for article in response.get("articles", ()) if isinstance(response, Mapping) else ():
                                if (
                                    isinstance(article, Mapping)
                                    and normalize_title(str(article.get("title", "")))
                                    == normalize_title(str(baseline_fields.get("title", "")))
                                    and extract_year_from_any(article.get("year"), fallback=0)
                                    == extract_year_from_any(baseline_fields.get("year"), fallback=0)
                                ):
                                    matching_providers.add(str(provider_row[0]))
                        expected_source = "scholar" if "scholar" in matching_providers else "dblp"
                        inventory_row = (
                            expected_source,
                            normalize_title(str(baseline_fields.get("title", ""))),
                            extract_year_from_any(baseline_fields.get("year"), fallback=0) or None,
                            evidence_json(inventory_seed.exact_identifiers),
                            "",
                            "monthly",
                        )
                if item.publication_keys != (expected_key,) or (
                    stored_row != expected_publication_row and stored_row != inventory_row
                ):
                    raise ValueError("corpus publication row is not independently derived")
        for row in connection.execute(
            "SELECT evidence_json FROM publication_seed_evidence WHERE generation_id = ? "
            "ORDER BY author_key, publication_key",
            (generation_id,),
        ):
            content = json.loads(str(row[0]))
            content["origin_kind"] = EvidenceKind(content["origin_kind"])
            seed = PublicationSeedEvidence(**content)
            seed_members[(seed.author_key, seed.publication_key)] = seed.origin_kind
            if seed.seed_digest != seed.derived_seed_digest:
                raise ValueError("publication seed digest is not derived from baseline authority")
            if seed.origin_kind is EvidenceKind.CORPUS:
                origin = corpus_origins.get(seed.origin_evidence_key)
                if (
                    origin is None
                    or seed.origin_evidence_digest != origin.digest
                    or seed.author_key != origin.author_key
                    or seed.publication_key not in origin.publication_keys
                    or evidence_json(seed.baseline_entry) != evidence_json(origin.normalized_entry)
                ):
                    raise ValueError("publication seed corpus baseline authority changed")
            else:
                expected_seed = Ledger._inventory_publication_seed(
                    connection,
                    generation_id,
                    seed.author_key,
                    seed.publication_key,
                    inventory_seed_cache,
                )
                if evidence_json(seed.canonical_content()) != evidence_json(expected_seed.canonical_content()):
                    raise ValueError("publication seed inventory baseline authority changed")
        if snapshot_evidence is not None and snapshot_evidence.author_set_digest != "0" * 64:
            receipt = connection.execute(
                "SELECT snapshot_digest, receipt_digest FROM corpus_scan_receipts WHERE generation_id = ?",
                (generation_id,),
            ).fetchone()
            expected_receipt = evidence_digest(
                {"domain": "citeforge-committed-corpus-scan-v1", "snapshot_digest": snapshot_evidence.digest}
            )
            if receipt is None or tuple(receipt) != (snapshot_evidence.digest, expected_receipt):
                raise ValueError("corpus snapshot lacks scanner-owned receipt authority")
            publication_members = {
                (str(row[0]), str(row[1]))
                for row in connection.execute(
                    "SELECT author_key, publication_key FROM publications WHERE generation_id = ?",
                    (generation_id,),
                )
            }
            corpus_members = {
                (str(row[0]), str(publication_key))
                for row in connection.execute(
                    "SELECT author_key, publication_keys_json FROM corpus_items WHERE generation_id = ?",
                    (generation_id,),
                )
                for publication_key in json.loads(str(row[1]))
            }
            if set(seed_members) != publication_members or any(
                seed_members[member] is not EvidenceKind.CORPUS for member in corpus_members
            ):
                raise ValueError("publication seed union membership is incomplete or not corpus-preferred")
        pass_rows = connection.execute(
            "SELECT pass_key, pass_id, receipt_json, snapshot_authority_digest, predecessor_output_digest "
            "FROM planner_passes WHERE generation_id = ? ORDER BY rowid",
            (generation_id,),
        ).fetchall()
        ordinals = [pass_for(str(row[1])).ordinal for row in pass_rows]
        if ordinals != list(range(len(ordinals))):
            raise ValueError("planner pass phase sequence is skipped or backward")
        previous_output: str | None = None
        for pass_row in pass_rows:
            receipt = json.loads(str(pass_row[2]))
            expected = tuple(str(value) for value in receipt["expected_items"])
            unseen = {str(value) for value in receipt["unseen_keys"]}
            stored = connection.execute(
                "SELECT item_key, kind, source_digest, input_json, unseen FROM planner_pass_expected_items "
                "WHERE generation_id = ? AND pass_key = ? "
                "ORDER BY item_key",
                (generation_id, str(pass_row[0])),
            ).fetchall()
            if (
                tuple(str(row[0]) for row in stored) != expected
                or {str(row[0]) for row in stored if bool(row[4])} != unseen
            ):
                raise ValueError("planner pass expected-item membership mismatch")
            stored_items: list[object] = []
            for row in stored:
                input_value = json.loads(str(row[3]))
                if (
                    not isinstance(input_value, Mapping)
                    or input_value.get("key") != row[0]
                    or input_value.get("kind") != row[1]
                    or input_value.get("digest") != row[2]
                ):
                    raise ValueError("planner pass input envelope mismatch")
                stored_items.append(input_value)
            historical_snapshot = _freeze_json(
                {
                    "generation_id": generation_id,
                    "pass_id": str(pass_row[1]),
                    "pass_version": receipt["pass_version"],
                    "items": stored_items,
                }
            )
            if not isinstance(historical_snapshot, Mapping):
                raise AssertionError("historical planner snapshot must be a mapping")
            if (
                str(pass_row[1]) == "late_identifiers"
                and receipt.get("pass_version") == "2"
                and receipt.get("registry_digest") != _LEGACY_C3_PASS_REGISTRY_DIGEST
            ):
                Ledger._verify_late_identifier_snapshot(connection, generation_id, historical_snapshot)
            if str(pass_row[1]) == "html_probe" and receipt.get("pass_version") == "2":
                Ledger._verify_html_probe_snapshot(connection, generation_id, historical_snapshot)
            if receipt.get("pass_version") == "1" and receipt.get("registry_digest") == (
                _LEGACY_C3_PASS_REGISTRY_DIGEST
            ):
                legacy_items = tuple(
                    sorted(str(item["key"]) for item in cast(Sequence[Mapping[str, object]], stored_items))
                )
                legacy_snapshot_digest = evidence_digest(historical_snapshot)
                authoritative = PlannerPassReceipt(
                    generation_id,
                    str(pass_row[1]),
                    "1",
                    evidence_digest((generation_id, str(pass_row[1]), "1", legacy_snapshot_digest)),
                    _LEGACY_C3_PASS_REGISTRY_DIGEST,
                    legacy_snapshot_digest,
                    legacy_items,
                    legacy_items,
                    evidence_digest((legacy_items, legacy_items)),
                )
            else:
                authoritative = _execute_authoritative_pass(str(pass_row[1]), historical_snapshot)
            stored_receipt = PlannerPassReceipt(**receipt)
            if not _receipt_matches_authority(stored_receipt, authoritative):
                raise ValueError("planner pass receipt is not code-authoritative")
            expected_authority = evidence_digest(
                {
                    "domain": _SNAPSHOT_DOMAIN_SEPARATOR,
                    "generation_id": generation_id,
                    "pass_id": str(pass_row[1]),
                    "snapshot": historical_snapshot,
                    "predecessor_output_digest": previous_output,
                }
            )
            if pass_row[3] != expected_authority or pass_row[4] != previous_output:
                raise ValueError("planner pass snapshot authority chain mismatch")
            previous_output = authoritative.output_digest
            policy_table_exists = (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'discovery_policy_authority'"
                ).fetchone()
                is not None
            )
            has_discovery_policy = (
                policy_table_exists
                and connection.execute(
                    "SELECT 1 FROM discovery_policy_authority WHERE generation_id = ?", (generation_id,)
                ).fetchone()
                is not None
            )
            if str(pass_row[1]) == "known_doi" and has_discovery_policy:
                from .discovery import plan_known_doi

                authority = Ledger._load_discovery_authority(connection, generation_id)
                seed_values = []
                decision_payloads: dict[str, Mapping[str, object]] = {}
                for stored_item in stored_items:
                    if not isinstance(stored_item, Mapping):
                        raise ValueError("known DOI pass source evidence is malformed")
                    if stored_item.get("kind") == EvidenceKind.APPLICABILITY.value:
                        payload = stored_item.get("payload")
                        if not isinstance(payload, Mapping) or not isinstance(payload.get("task_key"), str):
                            raise ValueError("known DOI pass decision evidence is malformed")
                        decision_payloads[str(payload["task_key"])] = payload
                        continue
                    if stored_item.get("kind") != EvidenceKind.SEED.value:
                        continue
                    payload = stored_item.get("payload")
                    if not isinstance(payload, Mapping):
                        raise ValueError("known DOI pass seed payload is malformed")
                    seed_content = dict(payload)
                    seed_content["origin_kind"] = EvidenceKind(str(seed_content["origin_kind"]))
                    seed_values.append(PublicationSeedEvidence(**seed_content))
                wave = plan_known_doi(tuple(seed_values), authority)
                expected_tasks = {decision.task.key: decision for decision in wave.decisions}
                if set(decision_payloads) != set(expected_tasks):
                    raise ValueError("known DOI decision membership changed")
                stored_tasks = {
                    str(row[0]): row
                    for row in connection.execute(
                        "SELECT task_key, author_key, publication_key, provider, operation, request_key, required, "
                        "applicability, identity_digest, state, applicability_reason FROM tasks "
                        "WHERE generation_id = ?",
                        (generation_id,),
                    )
                    if str(row[0]) in expected_tasks
                }
                if set(stored_tasks) != set(expected_tasks):
                    raise ValueError("known DOI output task membership changed")
                for task_key, decision in expected_tasks.items():
                    task = decision.task
                    row = stored_tasks[task_key]
                    expected_reason = decision.reason.value if decision.reason is not None else ""
                    decision_payload = decision_payloads[task_key]
                    if decision_payload.get("identity_digest") != task.identity_digest or decision_payload.get(
                        "reason"
                    ) != (expected_reason or None):
                        raise ValueError("known DOI decision authority changed")
                    if task.request is not None:
                        durable_consumers = tuple(
                            str(value[0])
                            for value in connection.execute(
                                "SELECT task_key FROM request_consumers WHERE generation_id = ? AND request_key = ? "
                                "ORDER BY task_key",
                                (generation_id, task.request.key),
                            )
                        )
                        claimed_consumers = decision_payload.get("request_consumers")
                        if not isinstance(claimed_consumers, Sequence) or isinstance(
                            claimed_consumers, (str, bytes, bytearray)
                        ):
                            raise ValueError("known DOI request consumer evidence is malformed")
                        if tuple(str(value) for value in claimed_consumers) != durable_consumers:
                            raise ValueError("known DOI request consumer membership changed")
                    if tuple(row[1:9]) != (
                        task.author_key,
                        task.publication_key,
                        task.provider,
                        task.operation,
                        task.request.key if task.request is not None else None,
                        int(task.required),
                        task.applicability,
                        task.identity_digest,
                    ) or (
                        task.request is None
                        and (str(row[9]) != TaskDisposition.NOT_APPLICABLE.value or str(row[10]) != expected_reason)
                    ):
                        raise ValueError("known DOI output task authority changed")
        duplicate_consumption = connection.execute(
            "SELECT 1 FROM aggregate_inputs WHERE generation_id = ? GROUP BY pass_key, kind, stable_key "
            "HAVING COUNT(DISTINCT reduction_id) > 1 LIMIT 1",
            (generation_id,),
        ).fetchone()
        if duplicate_consumption is not None:
            raise ValueError("aggregate input was consumed by multiple reductions")
        for aggregate in connection.execute(
            "SELECT a.pass_key, a.kind, a.stable_key, a.source_digest, a.input_json, "
            "p.kind, p.source_digest, p.input_json FROM aggregate_inputs a "
            "LEFT JOIN planner_pass_expected_items p ON p.generation_id = a.generation_id "
            "AND p.pass_key = a.pass_key AND p.item_key = a.stable_key WHERE a.generation_id = ?",
            (generation_id,),
        ):
            try:
                aggregate_content = json.loads(str(aggregate[4]))
                expected_input = json.loads(str(aggregate[7]))
            except (json.JSONDecodeError, TypeError) as exc:
                raise ValueError("aggregate input JSON is corrupt") from exc
            if (
                aggregate[5] is None
                or aggregate[1] != aggregate[5]
                or aggregate[3] != aggregate[6]
                or not isinstance(aggregate_content, Mapping)
                or evidence_json(aggregate_content.get("payload")) != evidence_json(expected_input)
            ):
                raise ValueError("aggregate input does not match stored pass membership")
        for intent in connection.execute(
            "SELECT intent_key, author_key, publication_key, provenance_set_digest, kind "
            "FROM materialization_intents "
            "WHERE generation_id = ? ORDER BY intent_key",
            (generation_id,),
        ):
            decisions = connection.execute(
                "SELECT p.decision_key, p.author_key, p.publication_key FROM intent_provenance i "
                "JOIN provenance_decisions p ON p.generation_id = i.generation_id "
                "AND p.decision_key = i.decision_key WHERE i.generation_id = ? AND i.intent_key = ? "
                "ORDER BY p.decision_key",
                (generation_id, str(intent[0])),
            ).fetchall()
            valid_no_output = (
                str(intent[4]) in {IntentKind.REMOVE.value} and not decisions and str(intent[3]) == evidence_digest(())
            )
            valid_emitted = (
                str(intent[4]) not in {IntentKind.REMOVE.value}
                and bool(decisions)
                and all((str(row[1]), str(row[2])) == (str(intent[1]), str(intent[2])) for row in decisions)
                and evidence_digest([str(row[0]) for row in decisions]) == str(intent[3])
            )
            if not valid_no_output and not valid_emitted:
                raise ValueError("intent provenance relationship is incomplete or substituted")

    @staticmethod
    def _verify_html_probe_children(connection: sqlite3.Connection, generation_id: str) -> None:
        """Rederive every numeric HTML child and prove its exact durable links."""
        from .discovery import DiscoveryWave

        if (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'html_probe_waves'"
            ).fetchone()
            is None
        ):
            return
        parent_row = connection.execute(
            "SELECT pass_key, receipt_json FROM planner_passes WHERE generation_id = ? AND pass_id = 'html_probe'",
            (generation_id,),
        ).fetchone()
        if parent_row is None:
            if (
                connection.execute(
                    "SELECT 1 FROM html_probe_waves WHERE generation_id = ? LIMIT 1", (generation_id,)
                ).fetchone()
                is not None
            ):
                raise ValueError("HTML child lacks parent authority")
            return
        parent = PlannerPassReceipt(**json.loads(str(parent_row[1])))
        if parent.pass_version != "2":  # noqa: S105 - planner pass version
            return
        authority = Ledger._load_discovery_authority(connection, generation_id)
        rows = connection.execute(
            "SELECT ordinal, wave_input_digest, predecessor_digest, decision_set_digest, terminal, round_key, "
            "receipt_digest FROM html_probe_waves WHERE generation_id = ? AND parent_pass_key = ? ORDER BY ordinal",
            (generation_id, parent.pass_key),
        ).fetchall()
        if tuple(int(row[0]) for row in rows) != tuple(range(len(rows))):
            raise ValueError("HTML child chronology changed")
        prior_receipts: list[str] = []
        for row in rows:
            ordinal = int(row[0])
            _seeds, wave_value, candidates = Ledger._derive_html_probe_wave(
                connection, generation_id, authority, ordinal
            )
            if not isinstance(wave_value, DiscoveryWave):
                raise ValueError("HTML child planner output changed")
            wave = wave_value
            payloads: list[Mapping[str, object]] = []
            expected_tasks: list[TaskSpec] = []
            for decision in sorted(
                wave.decisions, key=lambda item: (item.task.author_key, item.task.publication_key or "")
            ):
                task = decision.task
                member = (task.author_key, task.publication_key or "")
                selected = cast(Sequence[object], candidates[member])[ordinal : ordinal + 1]
                candidate = selected[0] if selected else None
                candidate_content = (
                    {key: getattr(candidate, key) for key in ("candidate_digest", "locators", "url_digest")}
                    if candidate is not None
                    else None
                )
                payload: Mapping[str, object] = {
                    "candidate": candidate_content,
                    "ordinal": ordinal,
                    "reason": decision.reason.value if decision.reason is not None else None,
                    "task": Ledger._html_task_content(task),
                    "wave_input_digest": wave.input_digest,
                }
                payloads.append(payload)
                stored = connection.execute(
                    "SELECT task_key, applicability, reason, evidence_json, item_digest FROM html_probe_wave_items "
                    "WHERE generation_id = ? AND parent_pass_key = ? AND ordinal = ? AND author_key = ? "
                    "AND publication_key = ?",
                    (generation_id, parent.pass_key, ordinal, task.author_key, task.publication_key),
                ).fetchone()
                expected_task_key = task.key if task.request is not None else None
                expected_reason = decision.reason.value if decision.reason is not None else None
                if (
                    stored is None
                    or stored[0] != expected_task_key
                    or str(stored[1]) != task.applicability
                    or stored[2] != expected_reason
                    or str(stored[3]) != evidence_json(payload)
                    or str(stored[4]) != evidence_digest(payload)
                ):
                    raise ValueError("HTML child item authority changed")
                if task.request is not None:
                    if Ledger._load_task(connection, generation_id, task.key) != task:
                        raise ValueError("HTML child task authority changed")
                    expected_tasks.append(task)
            stored_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM html_probe_wave_items WHERE generation_id = ? AND parent_pass_key = ? "
                    "AND ordinal = ?",
                    (generation_id, parent.pass_key, ordinal),
                ).fetchone()[0]
            )
            predecessor_digest = evidence_digest(
                {
                    "ordinal": ordinal,
                    "parent_output_digest": parent.output_digest,
                    "prior_receipts": prior_receipts,
                }
            )
            terminal = not expected_tasks
            if (
                stored_count != len(payloads)
                or str(row[1]) != wave.input_digest
                or str(row[2]) != predecessor_digest
                or str(row[3]) != evidence_digest(payloads)
                or bool(row[4]) != terminal
            ):
                raise ValueError("HTML child wave authority changed")
            round_key = str(row[5]) if row[5] is not None else None
            if terminal != (round_key is None):
                raise ValueError("HTML child physical round authority changed")
            if round_key is not None:
                round_row = connection.execute(
                    "SELECT sequence, phase, planner_id, source_evidence_digest, task_set_digest, content_digest "
                    "FROM plan_rounds WHERE generation_id = ? AND round_key = ?",
                    (generation_id, round_key),
                ).fetchone()
                if (
                    round_row is None
                    or str(round_row[1]) != PlanPhase.LATE_IDENTIFIERS.value
                    or str(round_row[2]) != f"html_probe_candidate:{ordinal}"
                ):
                    raise ValueError("HTML child round linkage changed")
                content = Ledger._round_content(
                    int(round_row[0]),
                    PlanPhase.LATE_IDENTIFIERS,
                    f"html_probe_candidate:{ordinal}",
                    authority.policy.planner_version,
                    (),
                    wave.input_digest,
                    (),
                    tuple(PlannedTask(task, expands_plan=False) for task in expected_tasks),
                )
                if tuple(str(value) for value in round_row[3:]) != (
                    wave.input_digest,
                    str(content["task_set_digest"]),
                    _digest(content),
                ):
                    raise ValueError("HTML child round content changed")
            receipt_content = {
                "decision_set_digest": evidence_digest(payloads),
                "ordinal": ordinal,
                "parent_pass_key": parent.pass_key,
                "predecessor_digest": predecessor_digest,
                "round_key": round_key,
                "terminal": terminal,
                "wave_input_digest": wave.input_digest,
            }
            if str(row[6]) != evidence_digest(receipt_content):
                raise ValueError("HTML child receipt changed")
            prior_receipts.append(str(row[6]))
        terminal_row = connection.execute(
            "SELECT completed_after_ordinal, reason, evidence_digest FROM html_probe_terminal_receipts "
            "WHERE generation_id = ? AND parent_pass_key = ?",
            (generation_id, parent.pass_key),
        ).fetchone()
        if terminal_row is not None:
            reason = str(terminal_row[1])
            completed = int(terminal_row[0]) if terminal_row[0] is not None else None
            if reason == "no_applicable_candidate":
                if not rows or not bool(rows[-1][4]) or completed != int(rows[-1][0]):
                    raise ValueError("HTML terminal receipt changed")
                content = {
                    "completed_after_ordinal": completed,
                    "parent_pass_key": parent.pass_key,
                    "reason": reason,
                    "wave_receipt_digest": str(rows[-1][6]),
                }
            elif reason == "candidate_bound_exhausted":
                if completed != (len(rows) - 1 if rows else None):
                    raise ValueError("HTML terminal receipt changed")
                content = {
                    "completed_after_ordinal": completed,
                    "parent_pass_key": parent.pass_key,
                    "reason": reason,
                }
                if not rows:
                    control = connection.execute(
                        "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? "
                        "AND pass_key = ? AND item_key LIKE 'html-control:%'",
                        (generation_id, parent.pass_key),
                    ).fetchone()
                    envelope = json.loads(str(control[0])) if control is not None else None
                    terminal_payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
                    content["unresolved_members"] = (
                        terminal_payload.get("unresolved_members") if isinstance(terminal_payload, Mapping) else None
                    )
            elif reason == "no_probeable_members":
                if rows or completed is not None:
                    raise ValueError("HTML terminal receipt changed")
                content = {
                    "completed_after_ordinal": None,
                    "parent_pass_key": parent.pass_key,
                    "reason": reason,
                    "unresolved_members": 0,
                }
            else:
                raise ValueError("HTML terminal receipt reason changed")
            if str(terminal_row[2]) != evidence_digest(content):
                raise ValueError("HTML terminal receipt digest changed")

    @staticmethod
    def _validate_v6_evidence_row(table: str, item: Mapping[str, object]) -> None:
        evidence = item.get("evidence")

        def require_matching_columns(
            value: CorpusSnapshot
            | CorpusItemEvidence
            | PublicationSeedEvidence
            | AggregateInput
            | PlannerPassReceipt
            | ProvenanceDecision
            | ProvenanceContribution
            | MaterializationIntent,
        ) -> None:
            canonical_content = value.canonical_content()
            for key, expected in canonical_content.items():
                actual = item.get(key)
                if isinstance(expected, bool) and isinstance(actual, int):
                    actual = bool(actual)
                if key in item and evidence_json(expected) != evidence_json(actual):
                    raise ValueError(f"Task5C evidence column mismatch: {key}")

        try:
            if table == "discovery_policy_authority":
                policy = item.get("policy")
                if not isinstance(policy, Mapping) or evidence_digest(policy) != item.get("policy_digest"):
                    raise ValueError("corrupt discovery policy authority")
            elif table == "corpus_snapshots":
                if not isinstance(evidence, Mapping):
                    raise ValueError("missing corpus snapshot evidence")
                snapshot_value = CorpusSnapshot(**evidence)
                require_matching_columns(snapshot_value)
                if snapshot_value.digest != item.get("snapshot_digest"):
                    raise ValueError("corrupt corpus snapshot digest")
            elif table == "corpus_items":
                if not isinstance(evidence, Mapping):
                    raise ValueError("missing corpus item evidence")
                corpus_item = CorpusItemEvidence(**evidence)
                require_matching_columns(corpus_item)
                if corpus_item.digest != item.get("evidence_digest"):
                    raise ValueError("corrupt corpus item digest")
                if corpus_item.normalized_entry and corpus_item.parse_digest != evidence_digest(
                    corpus_item.normalized_entry
                ):
                    raise ValueError("corrupt corpus normalized parse digest")
            elif table == "publication_seed_evidence":
                if not isinstance(evidence, Mapping):
                    raise ValueError("missing publication seed evidence")
                content = dict(evidence)
                content["origin_kind"] = EvidenceKind(content["origin_kind"])
                seed_value = PublicationSeedEvidence(**content)
                require_matching_columns(seed_value)
                if seed_value.seed_digest != item.get("seed_digest"):
                    raise ValueError("corrupt publication seed digest")
            elif table == "aggregate_inputs":
                if not isinstance(item.get("input"), Mapping):
                    raise ValueError("missing aggregate input evidence")
                content = dict(cast("Mapping[str, Any]", item["input"]))
                content["kind"] = EvidenceKind(content["kind"])
                # Validated immediately below by require_matching_columns, which
                # is what makes the keyword expansion safe.
                aggregate_value = AggregateInput(**content)
                require_matching_columns(aggregate_value)
                if aggregate_value.key != item.get("input_digest"):
                    raise ValueError("corrupt aggregate input digest")
            elif table == "planner_passes":
                if not isinstance(item.get("receipt"), Mapping):
                    raise ValueError("missing planner pass receipt")
                receipt = cast(Mapping[str, object], item["receipt"])
                pass_value = PlannerPassReceipt(
                    str(receipt["generation_id"]),
                    str(receipt["pass_id"]),
                    str(receipt["pass_version"]),
                    str(receipt["pass_key"]),
                    str(receipt["registry_digest"]),
                    str(receipt["snapshot_digest"]),
                    tuple(str(value) for value in cast(Sequence[object], receipt["expected_items"])),
                    tuple(str(value) for value in cast(Sequence[object], receipt["unseen_keys"])),
                    str(receipt["output_digest"]),
                )
                require_matching_columns(pass_value)
                if pass_value.pass_key != item.get("pass_key") or pass_value.output_digest != item.get("output_digest"):
                    raise ValueError("corrupt planner pass receipt")
            elif table == "provenance_decisions":
                if not isinstance(evidence, Mapping):
                    raise ValueError("missing provenance decision evidence")
                decision_value = ProvenanceDecision(**evidence)
                require_matching_columns(decision_value)
                if decision_value.key != item.get("decision_key"):
                    raise ValueError("corrupt provenance decision key")
            elif table == "provenance_contributions":
                if not isinstance(evidence, Mapping):
                    raise ValueError("missing provenance contribution evidence")
                contribution_value = ProvenanceContribution(**evidence)
                require_matching_columns(contribution_value)
                if contribution_value.key != item.get("contribution_key"):
                    raise ValueError("corrupt provenance contribution key")
            elif table == "materialization_intents":
                if not isinstance(evidence, Mapping):
                    raise ValueError("missing materialization intent evidence")
                content = dict(evidence)
                content["kind"] = IntentKind(content["kind"])
                intent_value = MaterializationIntent(**content)
                require_matching_columns(intent_value)
                if intent_value.key != item.get("intent_key"):
                    raise ValueError("corrupt materialization intent key")
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"corrupt Task5C evidence row in {table}") from exc

    def _set_manifest_probe_for_test(self, probe: Callable[[], None]) -> None:
        if not callable(probe):
            raise TypeError("manifest probe must be callable")
        self._manifest_probe = probe

    def set_fault(self, name: str) -> None:
        if name not in _FAULT_POINTS:
            raise ValueError(f"unknown fault point: {name}")
        self._fault = name

    def _inject(self, name: str) -> None:
        if self._fault == name:
            self._fault = None
            raise FaultInjectedError(name)

    def pragma(self, name: str) -> object:
        if name not in {"journal_mode", "synchronous", "foreign_keys", "integrity_check"}:
            raise ValueError("unsupported pragma")
        value: object = self._connection.execute(f"PRAGMA {name}").fetchone()[0]
        return value


__all__ = [
    "ApplicabilityReason",
    "DominanceEvidence",
    "DominanceRule",
    "EvidenceState",
    "FaultInjectedError",
    "Ledger",
    "LedgerManifest",
    "MaterializationEvidence",
    "PlanRound",
    "PlanStatus",
    "PlannedTask",
    "ProvenanceRule",
    "ProviderObservation",
    "PublicationMetadata",
    "ReductionReceipt",
    "RequestClaim",
    "RequestResult",
    "RequestSpec",
    "StaleClaimError",
    "TaskClaim",
    "TaskSpec",
    "ValidationSpec",
    "inventory_tasks",
]
