"""Immutable Task 5C discovery evidence and planner-pass authority."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import PurePosixPath
from types import MappingProxyType

from .capabilities import REGISTRY_DIGEST

_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,255}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(?:authorization|api[_-]?key|cookie|token|secret|password|credential)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"(?:bearer\s+|(?:authorization|api[_-]?key|cookie|token|secret|password|credential)\s*[:=])",
    re.IGNORECASE,
)


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def _digest(value: str, name: str = "digest") -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {name}")
    return value


def _optional_digest(value: str | None, name: str) -> str | None:
    return None if value is None else _digest(value, name)


def _path(value: str, name: str = "path") -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(unicodedata.category(char) == "Cc" for char in value)
        or _SECRET_VALUE_RE.search(value)
    ):
        raise ValueError(f"invalid or secret {name}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid {name}")
    return path.as_posix()


def _freeze(value: object, *, key: str = "") -> object:
    if key and _SECRET_KEY_RE.search(key):
        raise ValueError("secret material is forbidden in evidence")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("evidence requires finite JSON")
        return value
    if isinstance(value, str):
        if _SECRET_VALUE_RE.search(value):
            raise ValueError("secret material is forbidden in evidence")
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(item_key, str) for item_key in value):
            raise TypeError("evidence JSON keys must be strings")
        return MappingProxyType({item_key: _freeze(item, key=item_key) for item_key, item in sorted(value.items())})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    raise TypeError("evidence requires strict JSON")


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    return value


def canonical_json(value: object) -> str:
    return json.dumps(_plain(value), allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def evidence_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class EvidenceKind(str, Enum):
    CORPUS = "corpus"
    SEED = "seed"
    PUBLICATION = "publication"
    OBSERVATION = "observation"
    APPLICABILITY = "applicability"
    REDUCTION_RECEIPT = "reduction_receipt"
    PROVENANCE = "provenance"
    INTENT = "intent"


class IntentKind(str, Enum):
    KEEP = "keep"
    UPSERT = "upsert"
    REMOVE = "remove"


@dataclass(frozen=True)
class CorpusSnapshot:
    generation_id: str
    base_commit: str
    output_tree_digest: str
    baseline_digest: str
    scanner_id: str
    scanner_version: str
    parser_id: str
    parser_version: str
    item_set_digest: str
    derived_a2i2_digest: str | None = None
    mapper_id: str = "citeforge.author-directory"
    mapper_version: str = "1"
    identity_id: str = "citeforge.publication-key"
    identity_version: str = "1"
    extractor_id: str = "citeforge.corpus-identifiers"
    extractor_version: str = "1"
    a2i2_policy_id: str = "citeforge.a2i2"
    a2i2_policy_version: str = "1"
    author_set_digest: str = "0" * 64

    def __post_init__(self) -> None:
        for name in (
            "generation_id",
            "base_commit",
            "scanner_id",
            "scanner_version",
            "parser_id",
            "parser_version",
            "mapper_id",
            "mapper_version",
            "identity_id",
            "identity_version",
            "extractor_id",
            "extractor_version",
            "a2i2_policy_id",
            "a2i2_policy_version",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name.replace("_", " ")))
        for name in ("output_tree_digest", "baseline_digest", "item_set_digest"):
            object.__setattr__(self, name, _digest(getattr(self, name), name.replace("_", " ")))
        object.__setattr__(
            self, "derived_a2i2_digest", _optional_digest(self.derived_a2i2_digest, "derived a2i2 digest")
        )
        object.__setattr__(self, "author_set_digest", _digest(self.author_set_digest, "author set digest"))

    @property
    def digest(self) -> str:
        return evidence_digest(self.canonical_content())

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True)
class CorpusItemEvidence:
    generation_id: str
    snapshot_digest: str
    source_path: str
    author_key: str
    before_digest: str
    parse_digest: str
    publication_keys: tuple[str, ...]
    disposition: str
    exact_identifiers: Mapping[str, object] = field(default_factory=dict)
    normalized_entry: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation_id", _identifier(self.generation_id, "generation ID"))
        object.__setattr__(self, "snapshot_digest", _digest(self.snapshot_digest, "snapshot digest"))
        object.__setattr__(self, "source_path", _path(self.source_path, "source path"))
        object.__setattr__(self, "author_key", _identifier(self.author_key, "author key"))
        object.__setattr__(self, "before_digest", _digest(self.before_digest, "before digest"))
        object.__setattr__(self, "parse_digest", _digest(self.parse_digest, "parse digest"))
        keys = tuple(sorted(_identifier(item, "publication key") for item in self.publication_keys))
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate publication membership")
        object.__setattr__(self, "publication_keys", keys)
        if self.disposition not in {"parsed", "absent", "blocked_symlink", "blocked_parse", "blocked_mapping"}:
            raise ValueError("invalid corpus disposition")
        identifiers = _freeze(self.exact_identifiers)
        if not isinstance(identifiers, Mapping):
            raise TypeError("corpus exact identifiers must be an object")
        object.__setattr__(self, "exact_identifiers", identifiers)
        normalized = _freeze(self.normalized_entry)
        if not isinstance(normalized, Mapping):
            raise TypeError("corpus normalized entry must be an object")
        object.__setattr__(self, "normalized_entry", normalized)

    @property
    def key(self) -> str:
        return evidence_digest((self.author_key, self.source_path))

    @property
    def digest(self) -> str:
        return evidence_digest(
            {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "snapshot_digest"}
        )

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})


def publication_key_for(author_key: str, title: str, year: int | None, doi: str | None) -> str:
    """Derive the stable Task 5B/C author-scoped publication identity."""
    author = _identifier(author_key, "author key")
    normalized_title = " ".join(title.split()).casefold()
    if not normalized_title:
        raise ValueError("publication title is blank")
    stable_identity = doi or f"{normalized_title}\0{year or ''}"
    return hashlib.sha256(f"{author}\0{stable_identity}".encode()).hexdigest()


@dataclass(frozen=True)
class PublicationSeedEvidence:
    generation_id: str
    author_key: str
    publication_key: str
    origin_kind: EvidenceKind
    origin_evidence_key: str
    origin_evidence_digest: str
    baseline_digest: str | None
    exact_identifiers: Mapping[str, object]
    seed_digest: str
    baseline_entry: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("generation_id", "author_key", "publication_key", "origin_evidence_key"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name.replace("_", " ")))
        if self.origin_kind not in {EvidenceKind.CORPUS, EvidenceKind.PUBLICATION}:
            raise ValueError("invalid seed origin kind")
        object.__setattr__(self, "origin_evidence_digest", _digest(self.origin_evidence_digest))
        object.__setattr__(self, "baseline_digest", _optional_digest(self.baseline_digest, "baseline digest"))
        identifiers = _freeze(self.exact_identifiers)
        if not isinstance(identifiers, Mapping):
            raise TypeError("exact identifiers must be an object")
        object.__setattr__(self, "exact_identifiers", identifiers)
        object.__setattr__(self, "seed_digest", _digest(self.seed_digest, "seed digest"))
        baseline_entry = _freeze(self.baseline_entry)
        if not isinstance(baseline_entry, Mapping):
            raise TypeError("seed baseline entry must be an object")
        object.__setattr__(self, "baseline_entry", baseline_entry)

    @property
    def key(self) -> str:
        return evidence_digest((self.author_key, self.publication_key))

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})

    @property
    def derived_seed_digest(self) -> str:
        return evidence_digest(
            {name: getattr(self, name) for name in self.__dataclass_fields__ if name != "seed_digest"}
        )


@dataclass(frozen=True)
class AggregateInput:
    generation_id: str
    pass_key: str
    reduction_id: str
    kind: EvidenceKind
    stable_key: str
    source_digest: str
    ordinal: int
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("generation_id", "pass_key", "reduction_id", "stable_key"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name.replace("_", " ")))
        object.__setattr__(self, "source_digest", _digest(self.source_digest, "source digest"))
        if not isinstance(self.kind, EvidenceKind):
            raise ValueError("invalid aggregate input kind")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or self.ordinal < 0:
            raise ValueError("invalid aggregate input ordinal")
        payload = _freeze(self.payload)
        if not isinstance(payload, Mapping):
            raise TypeError("aggregate input payload must be an object")
        object.__setattr__(self, "payload", payload)

    @property
    def key(self) -> str:
        return evidence_digest((self.pass_key, self.reduction_id, self.kind.value, self.stable_key))

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True)
class PlannerPassReceipt:
    generation_id: str
    pass_id: str
    pass_version: str
    pass_key: str
    registry_digest: str
    snapshot_digest: str
    expected_items: tuple[str, ...]
    unseen_keys: tuple[str, ...]
    output_digest: str

    def __post_init__(self) -> None:
        for name in ("generation_id", "pass_id", "pass_version", "pass_key"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name.replace("_", " ")))
        for name in ("registry_digest", "snapshot_digest", "output_digest"):
            object.__setattr__(self, name, _digest(getattr(self, name), name.replace("_", " ")))
        for name in ("expected_items", "unseen_keys"):
            values = tuple(sorted(_identifier(item, "planner item") for item in getattr(self, name)))
            if len(values) != len(set(values)):
                raise ValueError("duplicate planner item")
            object.__setattr__(self, name, values)
        if not set(self.unseen_keys) <= set(self.expected_items):
            raise ValueError("unseen planner keys must be expected")

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True)
class ProvenanceDecision:
    generation_id: str
    pass_key: str
    author_key: str
    publication_key: str
    field_name: str
    selected_value_digest: str
    rule: str
    contribution_set_digest: str
    reducer_id: str
    reducer_version: str

    def __post_init__(self) -> None:
        for name in (
            "generation_id",
            "pass_key",
            "author_key",
            "publication_key",
            "field_name",
            "rule",
            "reducer_id",
            "reducer_version",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name.replace("_", " ")))
        object.__setattr__(self, "selected_value_digest", _digest(self.selected_value_digest))
        object.__setattr__(self, "contribution_set_digest", _digest(self.contribution_set_digest))

    @property
    def key(self) -> str:
        return evidence_digest((self.pass_key, self.author_key, self.publication_key, self.field_name))

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True)
class ProvenanceContribution:
    generation_id: str
    decision_key: str
    source_kind: str
    provider: str | None
    schema_version: str | None
    request_key: str | None
    observation_digest: str
    value_digest: str | None
    selected: bool
    rejection_reason: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "generation_id", _identifier(self.generation_id, "generation ID"))
        object.__setattr__(self, "decision_key", _digest(self.decision_key, "decision key"))
        object.__setattr__(self, "source_kind", _identifier(self.source_kind, "source kind"))
        for name in ("provider", "schema_version", "request_key"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _identifier(value, name.replace("_", " ")))
        object.__setattr__(self, "observation_digest", _digest(self.observation_digest))
        object.__setattr__(self, "value_digest", _optional_digest(self.value_digest, "value digest"))
        if not isinstance(self.selected, bool) or (self.selected and self.value_digest is None):
            raise ValueError("selected contribution requires a value digest")
        object.__setattr__(self, "rejection_reason", _identifier(self.rejection_reason, "rejection reason"))

    @property
    def key(self) -> str:
        return evidence_digest(self.canonical_content())

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True)
class MaterializationIntent:
    generation_id: str
    pass_key: str
    author_key: str
    publication_key: str
    source_path: str
    target_path: str
    kind: IntentKind
    before_digest: str | None
    after_digest: str | None
    reducer_id: str
    reducer_version: str
    provenance_set_digest: str
    final_fields: tuple[str, ...] = ()
    final_content_digest: str | None = None
    removal_reason: str = ""

    def __post_init__(self) -> None:
        for name in ("generation_id", "pass_key", "author_key", "publication_key", "reducer_id", "reducer_version"):
            object.__setattr__(self, name, _identifier(getattr(self, name), name.replace("_", " ")))
        object.__setattr__(self, "source_path", _path(self.source_path, "source path"))
        object.__setattr__(self, "target_path", _path(self.target_path, "target path"))
        object.__setattr__(self, "before_digest", _optional_digest(self.before_digest, "before digest"))
        object.__setattr__(self, "after_digest", _optional_digest(self.after_digest, "after digest"))
        object.__setattr__(self, "provenance_set_digest", _digest(self.provenance_set_digest))
        fields = tuple(sorted(_identifier(value, "final field") for value in self.final_fields))
        if len(fields) != len(set(fields)):
            raise ValueError("duplicate final field")
        object.__setattr__(self, "final_fields", fields)
        object.__setattr__(
            self, "final_content_digest", _optional_digest(self.final_content_digest, "final content digest")
        )
        reason = _identifier(self.removal_reason, "removal reason") if self.removal_reason else ""
        object.__setattr__(self, "removal_reason", reason)
        if not isinstance(self.kind, IntentKind):
            raise ValueError("invalid materialization intent kind")
        if self.kind is IntentKind.KEEP and (self.before_digest is None or self.after_digest != self.before_digest):
            raise ValueError("KEEP intent requires identical before and after digests")
        if self.kind is IntentKind.UPSERT and self.after_digest is None:
            raise ValueError("UPSERT intent requires after digest")
        if self.kind is IntentKind.REMOVE and (self.before_digest is None or self.after_digest is not None):
            raise ValueError("REMOVE intent requires before and no after digest")
        if self.kind is IntentKind.REMOVE and self.source_path.casefold() != self.target_path.casefold():
            raise ValueError("REMOVE intent requires the same source and target path")
        if self.kind is IntentKind.REMOVE and not reason:
            raise ValueError("REMOVE intent requires an explicit removal reason")
        if self.kind is not IntentKind.REMOVE and reason:
            raise ValueError("only REMOVE intent accepts a removal reason")
        if self.kind is IntentKind.UPSERT and (not fields or self.final_content_digest != self.after_digest):
            raise ValueError("UPSERT intent requires exact final fields and content digest")
        if self.kind is IntentKind.KEEP and (not fields or self.final_content_digest != self.before_digest):
            raise ValueError("KEEP intent requires exact baseline fields and content digest")
        if self.kind is IntentKind.REMOVE and (fields or self.final_content_digest is not None):
            raise ValueError("REMOVE intent cannot emit final fields")

    @property
    def key(self) -> str:
        return evidence_digest((self.pass_key, self.author_key, self.publication_key, self.target_path))

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__})


@dataclass(frozen=True)
class PassDefinition:
    pass_id: str
    version: str
    callback_id: str
    callback_version: str
    phase: str
    ordinal: int
    phase_graph_version: str
    policy_version: str
    snapshot_schema_version: str
    provenance_schema_version: str
    intent_schema_version: str
    capability_registry_digest: str
    callback: Callable[[Mapping[str, object]], tuple[tuple[str, ...], tuple[str, ...]]] | None = field(
        default=None, repr=False, compare=False
    )

    def canonical_content(self) -> Mapping[str, object]:
        return MappingProxyType({name: getattr(self, name) for name in self.__dataclass_fields__ if name != "callback"})


def _expected_items(snapshot: Mapping[str, object]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    items = snapshot.get("items", ())
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        raise ValueError("pass snapshot items must be a sequence")
    expected: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError("pass snapshot item must be an object")
        expected.append(_identifier(str(item.get("key", "")), "pass item key"))
    values = tuple(sorted(expected))
    if len(values) != len(set(values)):
        raise ValueError("duplicate pass snapshot item")
    return values, values


_PASS_IDS = (
    "bind_corpus_seed",
    "known_doi",
    "broad_discovery",
    "dynamic_expansion",
    "venue_fallback",
    "late_identifiers",
    "html_probe",
    "late_doi",
    "merge_intents",
)


def _callback_for(
    pass_id: str,
) -> Callable[[Mapping[str, object]], tuple[tuple[str, ...], tuple[str, ...]]]:
    def callback(snapshot: Mapping[str, object]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        expected, _unseen = _expected_items(snapshot)
        if pass_id in {"known_doi", "broad_discovery", "dynamic_expansion", "venue_fallback"}:
            items = snapshot.get("items", ())
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
                raise ValueError("known DOI snapshot items must be a sequence")
            seed_keys = tuple(
                sorted(
                    _identifier(str(item.get("key", "")), "known DOI seed item")
                    for item in items
                    if isinstance(item, Mapping) and item.get("kind") == EvidenceKind.SEED.value
                )
            )
            if len(seed_keys) != len(set(seed_keys)):
                raise ValueError("known DOI pass requires the exact seed union")
            authority_items = [
                item
                for item in items
                if isinstance(item, Mapping)
                and item.get("kind") == EvidenceKind.REDUCTION_RECEIPT.value
                and str(item.get("key", "")).startswith("authority:")
            ]
            decisions = [
                item
                for item in items
                if isinstance(item, Mapping)
                and item.get("kind") == EvidenceKind.APPLICABILITY.value
                and str(item.get("key", "")).startswith("decision:")
            ]
            if not authority_items and not decisions and pass_id == "known_doi":  # noqa: S105
                return expected, expected
            if len(authority_items) != 1:
                raise ValueError("discovery pass authority is incomplete")
            if pass_id == "known_doi":  # noqa: S105 - planner pass identifier
                if len(decisions) != len(seed_keys):
                    raise ValueError("known DOI pass output authority is incomplete")
                return expected, seed_keys
            if pass_id == "venue_fallback":  # noqa: S105 - planner pass identifier
                venue_sources = tuple(
                    sorted(
                        _identifier(str(item.get("key", "")), "venue source item")
                        for item in items
                        if isinstance(item, Mapping)
                        and str(item.get("key", "")).startswith(
                            ("author:", "broad-decision:", "broad-observation:", "doi-reduction:")
                        )
                    )
                )
                broad_decisions = [key for key in venue_sources if key.startswith("broad-decision:")]
                reductions = [key for key in venue_sources if key.startswith("doi-reduction:")]
                if seed_keys and (
                    len(decisions) != len(seed_keys)
                    or len(reductions) != len(seed_keys)
                    or len(broad_decisions) != 8 * len(seed_keys)
                ):
                    raise ValueError("venue fallback source or output authority is incomplete")
                return expected, venue_sources
            source_prefix = (
                "doi-reduction:"
                if pass_id == "broad_discovery"  # noqa: S105 - planner pass identifier
                else "broad-decision:"
            )
            source_keys = tuple(
                sorted(
                    _identifier(str(item.get("key", "")), "discovery source item")
                    for item in items
                    if isinstance(item, Mapping) and str(item.get("key", "")).startswith(source_prefix)
                )
            )
            if (not source_keys or not decisions) and seed_keys:
                raise ValueError("discovery pass source or output authority is incomplete")
            return expected, source_keys
        if pass_id == "late_identifiers":  # noqa: S105 - planner pass identifier
            items = snapshot.get("items", ())
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
                raise ValueError("late identifier snapshot items must be a sequence")
            seed_keys = tuple(
                str(item.get("key"))
                for item in items
                if isinstance(item, Mapping) and item.get("kind") == EvidenceKind.SEED.value
            )
            sources = tuple(
                str(item.get("key"))
                for item in items
                if isinstance(item, Mapping) and str(item.get("key", "")).startswith("late-source:")
            )
            outputs = tuple(
                str(item.get("key"))
                for item in items
                if isinstance(item, Mapping) and str(item.get("key", "")).startswith("late-output:")
            )
            if len(outputs) != len(seed_keys) or (seed_keys and not sources):
                raise ValueError("late identifier source or output authority is incomplete")
            return expected, tuple(sorted(sources))
        if pass_id == "html_probe":  # noqa: S105 - planner pass identifier
            items = snapshot.get("items", ())
            if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
                raise ValueError("HTML probe snapshot items must be a sequence")
            seed_keys = tuple(
                str(item.get("key"))
                for item in items
                if isinstance(item, Mapping) and item.get("kind") == EvidenceKind.SEED.value
            )
            late_outputs = tuple(
                str(item.get("key"))
                for item in items
                if isinstance(item, Mapping) and str(item.get("key", "")).startswith("late-output:")
            )
            controls = tuple(
                str(item.get("key"))
                for item in items
                if isinstance(item, Mapping) and str(item.get("key", "")).startswith("html-control:")
            )
            if len(late_outputs) != len(seed_keys) or len(controls) != 1:
                raise ValueError("HTML probe source authority is incomplete")
            return expected, tuple(sorted(late_outputs))
        return expected, expected

    return callback


_CALLBACKS = MappingProxyType({pass_id: _callback_for(pass_id) for pass_id in _PASS_IDS})
_PRIVATE_PASSES = MappingProxyType(
    {
        pass_id: PassDefinition(
            pass_id,
            "2"
            if pass_id
            in {"known_doi", "broad_discovery", "dynamic_expansion", "venue_fallback", "late_identifiers", "html_probe"}
            else "1",
            f"{pass_id}.callback",
            "2"
            if pass_id
            in {"known_doi", "broad_discovery", "dynamic_expansion", "venue_fallback", "late_identifiers", "html_probe"}
            else "1",
            pass_id,
            ordinal,
            "task5c-fixed-waves-v1",
            (
                "task5c4-discovery-policy-v2"
                if pass_id
                in {
                    "known_doi",
                    "broad_discovery",
                    "dynamic_expansion",
                    "venue_fallback",
                    "late_identifiers",
                    "html_probe",
                }
                else "task5c-evidence-policy-v1"
            ),
            (
                "task5c4-discovery-snapshot-v2"
                if pass_id
                in {
                    "known_doi",
                    "broad_discovery",
                    "dynamic_expansion",
                    "venue_fallback",
                    "late_identifiers",
                    "html_probe",
                }
                else "task5c-snapshot-v1"
            ),
            "task5c-provenance-v1",
            "task5c-intent-v1",
            REGISTRY_DIGEST,
            _CALLBACKS[pass_id],
        )
        for ordinal, pass_id in enumerate(_PASS_IDS)
    }
)
PASSES = MappingProxyType(
    {pass_id: replace(definition, callback=None) for pass_id, definition in _PRIVATE_PASSES.items()}
)


def registry_digest(values: Iterable[PassDefinition]) -> str:
    content = [dict(item.canonical_content()) for item in sorted(values, key=lambda item: item.pass_id)]
    return evidence_digest(content)


PASS_REGISTRY_DIGEST = registry_digest(_PRIVATE_PASSES.values())
PASS_WAVE_COUNT = len(_PRIVATE_PASSES)


def _validate_registry() -> None:
    if tuple(_PRIVATE_PASSES) != _PASS_IDS:
        raise RuntimeError("planner pass registry order drift")
    for ordinal, pass_id in enumerate(_PASS_IDS):
        definition = _PRIVATE_PASSES[pass_id]
        if (
            definition.pass_id != pass_id
            or definition.ordinal != ordinal
            or definition.callback_id != f"{pass_id}.callback"
            or definition.callback is not _CALLBACKS[pass_id]
            or definition.capability_registry_digest != REGISTRY_DIGEST
        ):
            raise RuntimeError("planner pass registry binding drift")
    if registry_digest(_PRIVATE_PASSES.values()) != PASS_REGISTRY_DIGEST:
        raise RuntimeError("planner pass registry digest drift")


_validate_registry()


def pass_for(pass_id: str) -> PassDefinition:
    try:
        return replace(_PRIVATE_PASSES[pass_id], callback=None)
    except KeyError as exc:
        raise ValueError("unknown planner pass") from exc


def execute_pass(pass_id: str, snapshot: Mapping[str, object]) -> PlannerPassReceipt:
    definition = _PRIVATE_PASSES.get(pass_id)
    if definition is None or definition.callback is None:
        raise ValueError("unknown planner pass")
    frozen = _freeze(snapshot)
    if not isinstance(frozen, Mapping):
        raise TypeError("planner pass snapshot must be an object")
    generation_id = _identifier(str(frozen.get("generation_id", "")), "generation ID")
    expected, unseen = definition.callback(frozen)
    snapshot_digest = evidence_digest(frozen)
    pass_key = evidence_digest((generation_id, pass_id, definition.version, snapshot_digest))
    output_digest = evidence_digest((expected, unseen))
    return PlannerPassReceipt(
        generation_id,
        pass_id,
        definition.version,
        pass_key,
        PASS_REGISTRY_DIGEST,
        snapshot_digest,
        expected,
        unseen,
        output_digest,
    )


def validate_pass_receipt(
    pass_id: str, snapshot: Mapping[str, object], receipt: PlannerPassReceipt
) -> PlannerPassReceipt:
    """Recompute private pass output and reject substituted callback receipts."""
    definition = _PRIVATE_PASSES.get(pass_id)
    if definition is None or definition.callback is None:
        raise ValueError("unknown planner pass")
    frozen = _freeze(snapshot)
    if not isinstance(frozen, Mapping):
        raise TypeError("planner pass snapshot must be an object")
    expected, unseen = definition.callback(frozen)
    generation_id = _identifier(str(frozen.get("generation_id", "")), "generation ID")
    snapshot_digest = evidence_digest(frozen)
    expected_receipt = PlannerPassReceipt(
        generation_id,
        pass_id,
        definition.version,
        evidence_digest((generation_id, pass_id, definition.version, snapshot_digest)),
        PASS_REGISTRY_DIGEST,
        snapshot_digest,
        expected,
        unseen,
        evidence_digest((expected, unseen)),
    )
    if receipt != expected_receipt:
        raise ValueError("planner pass receipt does not match code-owned authority")
    return expected_receipt


def _execute_authoritative_pass(pass_id: str, snapshot: Mapping[str, object]) -> PlannerPassReceipt:
    """Private single boundary used by the durable ledger authority."""
    definition = _PRIVATE_PASSES.get(pass_id)
    if definition is None or definition.callback is None:
        raise ValueError("unknown planner pass")
    frozen = _freeze(snapshot)
    if not isinstance(frozen, Mapping):
        raise TypeError("planner pass snapshot must be an object")
    generation_id = _identifier(str(frozen.get("generation_id", "")), "generation ID")
    expected, unseen = definition.callback(frozen)
    snapshot_digest = evidence_digest(frozen)
    return PlannerPassReceipt(
        generation_id,
        pass_id,
        definition.version,
        evidence_digest((generation_id, pass_id, definition.version, snapshot_digest)),
        PASS_REGISTRY_DIGEST,
        snapshot_digest,
        expected,
        unseen,
        evidence_digest((expected, unseen)),
    )


__all__ = [
    "PASSES",
    "PASS_REGISTRY_DIGEST",
    "PASS_WAVE_COUNT",
    "AggregateInput",
    "CorpusItemEvidence",
    "CorpusSnapshot",
    "EvidenceKind",
    "IntentKind",
    "MaterializationIntent",
    "PassDefinition",
    "PlannerPassReceipt",
    "ProvenanceContribution",
    "ProvenanceDecision",
    "PublicationSeedEvidence",
    "canonical_json",
    "evidence_digest",
    "execute_pass",
    "pass_for",
    "publication_key_for",
    "registry_digest",
    "validate_pass_receipt",
]
