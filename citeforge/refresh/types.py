"""Immutable values shared by durable refresh components."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .census import AuthorCensus


class TaskDisposition(str, Enum):
    """A durable work disposition with no ambiguous success state."""

    PENDING = "pending"
    LEASED = "leased"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    CONFIRMED_EMPTY = "confirmed_empty"
    NOT_APPLICABLE = "not_applicable"
    DOMINATED = "dominated"
    MALFORMED = "malformed"
    AUTHENTICATION_FAILED = "authentication_failed"
    PERMANENT_FAILURE = "permanent_failure"


class GenerationState(str, Enum):
    """Durable lifecycle state for a refresh generation."""

    PLANNED = "planned"
    RUNNING = "running"
    COMPLETE = "complete"
    BLOCKED = "blocked"


class RunStatus(str, Enum):
    """Process-level result of one bounded refresh execution."""

    COMPLETE = "complete"
    CONTINUATION = "continuation"
    BLOCKED = "blocked"
    INVALID_CONFIGURATION = "invalid_configuration"


@dataclass(frozen=True)
class GenerationSpec:
    """All material inputs that identify one refresh generation."""

    census: AuthorCensus
    refresh_policy_version: str
    adapter_versions: Mapping[str, str]
    base_commit: str
    id: str = field(init=False)

    def __post_init__(self) -> None:
        adapters = MappingProxyType(dict(sorted(self.adapter_versions.items())))
        object.__setattr__(self, "adapter_versions", adapters)
        canonical = {
            "adapter_versions": dict(adapters),
            "base_commit": self.base_commit.strip(),
            "census": self.census.canonical_content(),
            "refresh_policy_version": self.refresh_policy_version.strip(),
        }
        encoded = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
        object.__setattr__(self, "id", hashlib.sha256(encoded).hexdigest())


@dataclass(frozen=True)
class RunResult:
    """Structured result from one bounded refresh execution."""

    status: RunStatus
    generation_id: str
    completed_tasks: int = 0
    remaining_tasks: int = 0
    detail: str = ""
