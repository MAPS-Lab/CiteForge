from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from citeforge.refresh.authority import evidence_digest
from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.discovery import DiscoveryCredentials, DiscoveryPolicy
from citeforge.refresh.engine import RefreshEngine
from citeforge.refresh.inventory import (
    InventoryPolicy,
    RefreshCredentials,
    build_claimed_inventory_operation,
)
from citeforge.refresh.ledger import FaultInjectedError, Ledger, PlannedTask, ProviderObservation, RequestSpec, TaskSpec
from citeforge.refresh.transport import LedgerTransport, SendOperation
from citeforge.refresh.types import GenerationSpec, GenerationState, RunStatus, TaskDisposition


def _spec() -> GenerationSpec:
    census = AuthorCensus(
        (
            AuthorCensusRow(
                2,
                "author-ada",
                "Ada Lovelace",
                "ada lovelace",
                "Scholar123",
                "",
                True,
                "",
                TaskDisposition.PENDING,
            ),
        )
    )
    return GenerationSpec(
        census,
        "policy-v1",
        {"doi_csl": "1", "s2": "1", "scholar": "1"},
        "abc123",
    )


def test_discovery_engine_advances_earliest_incomplete_wave_and_scopes_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = SimpleNamespace(key="a" * 64)

    class FakeLedger:
        def __init__(self) -> None:
            self.committed: list[str] = []
            self.sent = False
            self.claimed_scopes: list[frozenset[str]] = []

        def bind_discovery_policy(self, _policy: object, _credentials: object) -> str:
            return "b" * 64

        def create_or_resume(self, _spec: object, _census: object) -> str:
            return "generation"

        def generation_state(self) -> object:
            return GenerationState.RUNNING

        def assert_c3_discovery_ready(self) -> None:
            return None

        def load_discovery_authority(self) -> object:
            return object()

        def manifest(self) -> object:
            return SimpleNamespace(
                data={
                    "generation": {"generation_id": "generation"},
                    "tasks": [{"task_key": claim.key, "provider": "doi_csl"}],
                }
            )

        def discovery_phase_status(self, pass_id: str, *, now: datetime) -> str:
            if pass_id not in self.committed:
                return "uncommitted"
            if pass_id == "known_doi" and not self.sent:
                return "pending"
            return "complete"

        def execute_and_commit_discovery_wave(self, pass_id: str, _policy: object, *, now: datetime) -> object:
            self.committed.append(pass_id)
            return object()

        def execute_and_commit_venue_fallback(self, _policy: object, *, now: datetime) -> object:
            self.committed.append("venue_fallback")
            return object()

        def execute_and_commit_late_identifiers(self, _policy: object, *, now: datetime) -> object:
            self.committed.append("late_identifiers")
            return object()

        def execute_and_commit_html_probe(self, _policy: object, *, now: datetime) -> object:
            self.committed.append("html_probe")
            return object()

        def discovery_wave_due_tasks(self, pass_id: str, *, now: datetime) -> dict[str, str]:
            return {claim.key: "doi_csl"} if pass_id == "known_doi" and not self.sent else {}

        def claim_due_for_operations(
            self, _owner: object, _now: object, _lease: object, keys: frozenset[str]
        ) -> object:
            self.claimed_scopes.append(keys)
            return claim

    class Transport:
        def send(self, _operation: object, *, task_claim: object) -> None:
            ledger.sent = True

    ledger = FakeLedger()
    built: list[str] = []
    monkeypatch.setattr(
        "citeforge.refresh.engine.build_claimed_discovery_operation",
        lambda *_args, **_kwargs: built.append(claim.key) or object(),
    )
    engine = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), Transport())  # type: ignore[arg-type]
    policy = SimpleNamespace(openreview_mode="anonymous")
    result = engine.run_discovery(  # type: ignore[arg-type]
        SimpleNamespace(id="generation", census=object()), policy, DiscoveryCredentials(), lambda: False
    )
    assert result.status is RunStatus.CONTINUATION
    assert ledger.committed == [
        "known_doi",
        "broad_discovery",
        "dynamic_expansion",
        "venue_fallback",
        "late_identifiers",
        "html_probe",
    ]
    assert built == [claim.key]
    assert ledger.claimed_scopes == [frozenset({claim.key})]


def test_discovery_engine_does_not_spin_on_html_backoff_without_due_work() -> None:
    class FakeLedger:
        html_calls = 0

        def manifest(self) -> object:
            return SimpleNamespace(data={"generation": {"generation_id": "generation"}})

        def create_or_resume(self, _spec: object, _census: object) -> str:
            return "generation"

        def generation_state(self) -> GenerationState:
            return GenerationState.RUNNING

        def assert_c3_discovery_ready(self) -> None:
            return None

        def bind_discovery_policy(self, _policy: object, _credentials: object) -> str:
            return "a" * 64

        def load_discovery_authority(self) -> object:
            return object()

        def discovery_phase_status(self, pass_id: str, *, now: datetime) -> str:
            return "pending" if pass_id == "html_probe" else "complete"

        def discovery_wave_due_tasks(self, _pass_id: str, *, now: datetime) -> dict[str, str]:
            return {}

        def execute_and_commit_html_probe(self, _policy: object, *, now: datetime) -> object:
            self.html_calls += 1
            return object()

    ledger = FakeLedger()
    engine = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), None)  # type: ignore[arg-type]
    result = engine.run_discovery(  # type: ignore[arg-type]
        SimpleNamespace(id="generation", census=object()),
        SimpleNamespace(openreview_mode="anonymous"),
        DiscoveryCredentials(),
        lambda: False,
    )
    assert result.status is RunStatus.CONTINUATION
    assert ledger.html_calls == 1


def test_discovery_engine_stops_cached_wave_after_first_blocking_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = tuple(SimpleNamespace(key=character * 64) for character in ("a", "b"))

    class FakeLedger:
        def __init__(self) -> None:
            self.claimed = 0

        def manifest(self) -> object:
            return SimpleNamespace(data={"generation": {"generation_id": "generation"}})

        def create_or_resume(self, _spec: object, _census: object) -> str:
            return "generation"

        def generation_state(self) -> GenerationState:
            return GenerationState.RUNNING

        def assert_c3_discovery_ready(self) -> None:
            return None

        def bind_discovery_policy(self, _policy: object, _credentials: object) -> str:
            return "b" * 64

        def load_discovery_authority(self) -> object:
            return object()

        def discovery_phase_status(self, _pass_id: str, *, now: datetime) -> str:
            return "pending"

        def discovery_wave_due_tasks(self, _pass_id: str, *, now: datetime) -> dict[str, str]:
            return {claim.key: "doi_csl" for claim in claims}

        def claim_due_for_operations(self, *_args: object) -> object:
            claim = claims[self.claimed]
            self.claimed += 1
            return claim

        def transition_generation(self, *_args: object, **_kwargs: object) -> None:
            return None

    class Transport:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, _operation: object, *, task_claim: object) -> object:
            self.sent.append(task_claim.key)  # type: ignore[attr-defined]
            return SimpleNamespace(disposition=TaskDisposition.SCHEMA_CHANGED)

    ledger = FakeLedger()
    transport = Transport()
    monkeypatch.setattr("citeforge.refresh.engine.build_claimed_discovery_operation", lambda *_a, **_k: object())
    result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), transport).run_discovery(  # type: ignore[arg-type]
        SimpleNamespace(id="generation", census=object()),
        SimpleNamespace(openreview_mode="anonymous"),
        DiscoveryCredentials(),
        lambda: False,
    )
    assert result.status is RunStatus.BLOCKED
    assert transport.sent == [claims[0].key]


def test_discovery_engine_rechecks_durable_phase_after_lost_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = tuple(SimpleNamespace(key=character * 64) for character in ("a", "b"))

    class FakeLedger:
        def __init__(self) -> None:
            self.claimed = 0
            self.blocking = False

        def manifest(self) -> object:
            return SimpleNamespace(data={"generation": {"generation_id": "generation"}})

        def create_or_resume(self, _spec: object, _census: object) -> str:
            return "generation"

        def generation_state(self) -> GenerationState:
            return GenerationState.RUNNING

        def assert_c3_discovery_ready(self) -> None:
            return None

        def bind_discovery_policy(self, _policy: object, _credentials: object) -> str:
            return "b" * 64

        def load_discovery_authority(self) -> object:
            return object()

        def discovery_phase_status(self, _pass_id: str, *, now: datetime) -> str:
            return "blocking" if self.blocking else "pending"

        def discovery_wave_due_tasks(self, _pass_id: str, *, now: datetime) -> dict[str, str]:
            return {claim.key: "doi_csl" for claim in claims}

        def claim_due_for_operations(self, *_args: object) -> object:
            claim = claims[self.claimed]
            self.claimed += 1
            return claim

        def transition_generation(self, *_args: object, **_kwargs: object) -> None:
            return None

    class Transport:
        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, _operation: object, *, task_claim: object) -> object:
            self.sent.append(task_claim.key)  # type: ignore[attr-defined]
            ledger.blocking = True
            return SimpleNamespace(disposition=TaskDisposition.LEASED)

    ledger = FakeLedger()
    transport = Transport()
    monkeypatch.setattr("citeforge.refresh.engine.build_claimed_discovery_operation", lambda *_a, **_k: object())
    result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), transport).run_discovery(  # type: ignore[arg-type]
        SimpleNamespace(id="generation", census=object()),
        SimpleNamespace(openreview_mode="anonymous"),
        DiscoveryCredentials(),
        lambda: False,
    )
    assert result.status is RunStatus.BLOCKED
    assert transport.sent == [claims[0].key]


def test_discovery_engine_does_not_login_without_pending_openreview(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broker:
        def acquire(self, _credentials: tuple[str, str]) -> object:
            raise AssertionError("OpenReview login requires an exact pending OpenReview claim")

    class FakeLedger:
        def bind_discovery_policy(self, _policy: object, _credentials: object) -> str:
            return "b" * 64

        def create_or_resume(self, _spec: object, _census: object) -> str:
            return "generation"

        def generation_state(self) -> object:
            return GenerationState.RUNNING

        def assert_c3_discovery_ready(self) -> None:
            return None

        def load_discovery_authority(self) -> object:
            return object()

        def manifest(self) -> object:
            return SimpleNamespace(data={"generation": {"generation_id": "generation"}})

        def discovery_phase_status(self, pass_id: str, *, now: datetime) -> str:
            return "complete"

    engine = RefreshEngine(  # type: ignore[arg-type]
        FakeLedger(), InventoryPolicy(2020, 1000, 10), transport=object(), openreview_broker=Broker()
    )
    result = engine.run_discovery(  # type: ignore[arg-type]
        SimpleNamespace(id="generation", census=object()),
        SimpleNamespace(openreview_mode="authenticated"),
        DiscoveryCredentials(openreview_username="user", openreview_password="password"),
        lambda: False,
    )
    assert result.status is RunStatus.CONTINUATION


def test_discovery_engine_rejects_generation_mismatch_before_policy_or_send() -> None:
    class FakeLedger:
        def manifest(self) -> object:
            return SimpleNamespace(data={"generation": {"generation_id": "different"}})

        def bind_discovery_policy(self, _policy: object, _credentials: object) -> str:
            raise AssertionError("mismatched generation must fail before policy binding")

    engine = RefreshEngine(FakeLedger(), InventoryPolicy(2020, 1000, 10), transport=object())  # type: ignore[arg-type]
    result = engine.run_discovery(  # type: ignore[arg-type]
        SimpleNamespace(id="supplied", census=object()),
        SimpleNamespace(openreview_mode="anonymous"),
        DiscoveryCredentials(),
        lambda: False,
    )
    assert result.status is RunStatus.INVALID_CONFIGURATION
    assert result.generation_id == "supplied"


def test_discovery_engine_requires_committed_c3_before_policy_binding() -> None:
    class FakeLedger:
        def manifest(self) -> object:
            return SimpleNamespace(data={"generation": {"generation_id": "generation"}})

        def create_or_resume(self, _spec: object, _census: object) -> str:
            return "generation"

        def generation_state(self) -> object:
            return GenerationState.RUNNING

        def assert_c3_discovery_ready(self) -> None:
            raise ValueError("C3 discovery readiness requires the corpus seed binding pass")

        def bind_discovery_policy(self, _policy: object, _credentials: object) -> str:
            raise AssertionError("C4 policy must not bind before committed C3")

    engine = RefreshEngine(FakeLedger(), InventoryPolicy(2020, 1000, 10), transport=object())  # type: ignore[arg-type]
    result = engine.run_discovery(  # type: ignore[arg-type]
        SimpleNamespace(id="generation", census=object()),
        SimpleNamespace(openreview_mode="anonymous"),
        DiscoveryCredentials(),
        lambda: False,
    )
    assert result.status is RunStatus.INVALID_CONFIGURATION


def test_engine_missing_scholar_credential_fails_before_claim(tmp_path: Path) -> None:
    spec = _spec()
    ledger_path = tmp_path / "ledger.db"
    with Ledger.open(ledger_path) as ledger:
        result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(spec, RefreshCredentials(), lambda: False)
        assert result.status is RunStatus.INVALID_CONFIGURATION
        assert ledger.manifest().data["tasks"] == []


def test_generation_start_binds_discovery_preflight_before_inventory_send(tmp_path: Path) -> None:
    spec = _spec()
    calls = 0

    def send_once(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("invalid discovery preflight must prevent inventory sockets")

    adapters = {
        "arxiv": "1",
        "crossref": "1",
        "doi_bibtex": "1",
        "doi_csl": "1",
        "europepmc": "1",
        "gemini": "1",
        "openalex": "1",
        "openreview": "1",
        "pubmed": "1",
        "s2": "2",
        "serply": "1",
    }
    policy = DiscoveryPolicy(
        "2026-08",
        adapters,
        {
            "arxiv": 10,
            "crossref": 20,
            "europepmc": 20,
            "openalex": 20,
            "openreview": 20,
            "pubmed": 5,
            "s2": 15,
            "serply": 20,
        },
        {"gemini": "disabled", "s2": "required", "serply": "disabled"},
        "anonymous",
        False,
        False,
        10,
        8,
    )
    full_spec = GenerationSpec(
        spec.census,
        spec.refresh_policy_version,
        {**adapters, "scholar": "1"},
        spec.base_commit,
    )
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10, s2_adapter_version="2", freshness_epoch="2026-08"),
            LedgerTransport(ledger, send_once=send_once),
        ).run(
            full_spec,
            RefreshCredentials(serpapi_key="inventory-secret"),
            lambda: False,
            discovery_policy=policy,
            discovery_credentials=DiscoveryCredentials(),
        )
        assert result.status is RunStatus.INVALID_CONFIGURATION
        assert calls == 0
        assert ledger.manifest().data["tasks"] == []


def test_generation_start_rejects_stale_discovery_epoch_before_binding(tmp_path: Path) -> None:
    base = _spec()
    adapters = {
        "arxiv": "1",
        "crossref": "1",
        "doi_bibtex": "1",
        "doi_csl": "1",
        "europepmc": "1",
        "gemini": "1",
        "openalex": "1",
        "openreview": "1",
        "pubmed": "1",
        "s2": "2",
        "serply": "1",
    }
    spec = GenerationSpec(base.census, base.refresh_policy_version, {**adapters, "scholar": "1"}, base.base_commit)
    policy = DiscoveryPolicy(
        "2026-07",
        adapters,
        {
            "arxiv": 10,
            "crossref": 20,
            "europepmc": 20,
            "openalex": 20,
            "openreview": 20,
            "pubmed": 5,
            "s2": 15,
            "serply": 20,
        },
        {"gemini": "disabled", "s2": "required", "serply": "disabled"},
        "anonymous",
        False,
        False,
        10,
        8,
    )
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10, s2_adapter_version="2", freshness_epoch="2026-08"),
        ).run(
            spec,
            RefreshCredentials(serpapi_key="inventory-secret"),
            lambda: True,
            discovery_policy=policy,
            discovery_credentials=DiscoveryCredentials(s2_key="wire-only"),
        )
        assert result.status is RunStatus.INVALID_CONFIGURATION
        assert ledger._connection.execute("SELECT COUNT(*) FROM discovery_policy_authority").fetchone()[0] == 0
        assert ledger.plan_status().revision == 0


def test_engine_commits_inventory_round_but_never_closes_discovery(tmp_path: Path) -> None:
    spec = _spec()
    ledger_path = tmp_path / "ledger.db"
    with Ledger.open(ledger_path) as ledger:
        result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(
            spec, RefreshCredentials(serpapi_key="secret"), lambda: True
        )
        assert result.status is RunStatus.CONTINUATION
        status = ledger.plan_status()
        assert status.revision == 1
        assert not status.closed and not status.discovery_closed
        assert "secret" not in repr(RefreshCredentials(serpapi_key="secret"))
        manifest = ledger.manifest()
        assert "secret" not in manifest.canonical_json
        authority = manifest.data["inventory_policy_authority"]
        assert authority["authority"]["generation"] == spec.id
        assert authority["authority_digest"] == manifest.data["plan_rounds"][0]["source_evidence_digest"]
        assert dict(ledger.closure_content())["inventory_policy_authority"] == authority


def test_stop_before_claim_does_not_start_physical_work(tmp_path: Path) -> None:
    spec = _spec()
    calls = 0

    def send_once(_operation: SendOperation) -> requests.Response:
        nonlocal calls
        calls += 1
        raise AssertionError("stop must prevent new physical work")

    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(
            ledger, InventoryPolicy(2020, 1000, 10), LedgerTransport(ledger, send_once=send_once)
        ).run(spec, RefreshCredentials(serpapi_key="secret"), lambda: True)
        assert result.status is RunStatus.CONTINUATION
        assert calls == 0
        task = ledger.manifest().data["tasks"][0]
        assert task["state"] == "pending"
        assert task["attempt_count"] == 0


def test_resume_rejects_policy_change_before_physical_work(tmp_path: Path) -> None:
    spec = _spec()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        first = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(
            spec, RefreshCredentials(serpapi_key="secret"), lambda: True
        )
        assert first.status is RunStatus.CONTINUATION
        changed = RefreshEngine(ledger, InventoryPolicy(2021, 1000, 10)).run(
            spec, RefreshCredentials(serpapi_key="secret"), lambda: False
        )
        assert changed.status is RunStatus.INVALID_CONFIGURATION
        task = ledger.manifest().data["tasks"][0]
        assert task["state"] == "pending" and task["attempt_count"] == 0


def test_engine_executes_exact_inventory_and_commits_union_seed(tmp_path: Path) -> None:
    spec = _spec()
    body = {
        "search_metadata": {
            "status": "Success",
            "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
        },
        "search_parameters": {
            "engine": "google_scholar_author",
            "author_id": "Scholar123",
            "cstart": 0,
        },
        "author": {"name": "Ada Lovelace"},
        "articles": [
            {
                "title": "Analytical Engine",
                "authors": "Ada Lovelace",
                "year": 2024,
                "citation_id": "Scholar123:one",
                "link": "https://scholar.google.com/one",
            }
        ],
    }

    def send_once(_operation: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(body).encode()
        return response

    with Ledger.open(tmp_path / "ledger.db") as ledger:
        transport = LedgerTransport(ledger, send_once=send_once)
        result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), transport).run(
            spec, RefreshCredentials(serpapi_key="wire-only-secret"), lambda: False
        )
        assert result.status is RunStatus.CONTINUATION
        manifest = ledger.manifest()
        assert len(manifest.data["inventory_authorities"]) == 1
        assert len(manifest.data["publications"]) == 1
        assert any(item["operation"] == "fuzzy_search" for item in manifest.data["tasks"])
        assert "wire-only-secret" not in manifest.canonical_json
        assert not ledger.plan_status().discovery_closed


def test_unused_inventory_adapter_version_does_not_change_bound_capabilities(tmp_path: Path) -> None:
    base = _spec()
    spec = GenerationSpec(
        base.census,
        base.refresh_policy_version,
        {**dict(base.adapter_versions), "dblp": "1"},
        base.base_commit,
    )
    body = {
        "search_metadata": {
            "status": "Success",
            "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
        },
        "search_parameters": {"engine": "google_scholar_author", "author_id": "Scholar123"},
        "author": {"name": "Ada Lovelace"},
        "articles": [],
    }

    def send_once(_operation: object) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(body).encode()
        return response

    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10),
            LedgerTransport(ledger, send_once=send_once),
        ).run(spec, RefreshCredentials(serpapi_key="wire-only-secret"), lambda: False)
        assert result.status is RunStatus.CONTINUATION
        authority = ledger.manifest().data["inventory_policy_authority"]["authority"]
        assert {item["logical_source"] for item in authority["capabilities"]} == {"scholar", "doi_csl", "s2"}


def test_engine_paginates_in_durable_waves_without_repeating_success(tmp_path: Path) -> None:
    spec = _spec()
    calls: list[int] = []

    def send_once(operation: SendOperation) -> requests.Response:
        request = operation.request
        start = dict(request.normalized_payload)["start"]
        calls.append(start)
        envelope = {
            "search_metadata": {
                "status": "Success",
                "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
            },
            "search_parameters": {
                "engine": "google_scholar_author",
                "author_id": "Scholar123",
                "cstart": start,
            },
            "author": {"name": "Ada Lovelace"},
            "articles": [
                {
                    "title": f"Paper {start}",
                    "authors": "Ada Lovelace",
                    "year": 2024,
                    "citation_id": f"Scholar123:{start}",
                    "link": f"https://scholar.google.com/{start}",
                }
            ],
        }
        if start == 0:
            envelope["serpapi_pagination"] = {
                "next": "https://serpapi.com/search.json?engine=google_scholar_author&author_id=Scholar123"
                "&cstart=100&num=100&sort=pubdate&hl=en&api_key=provider-secret"
            }
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(envelope).encode()
        return response

    with Ledger.open(tmp_path / "ledger.db") as ledger:
        engine = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), LedgerTransport(ledger, send_once=send_once))
        first = engine.run(spec, RefreshCredentials(serpapi_key="secret"), lambda: False)
        assert first.status is RunStatus.CONTINUATION
        assert calls == [0]
        second = engine.run(spec, RefreshCredentials(serpapi_key="secret"), lambda: False)
        assert second.status is RunStatus.CONTINUATION
        assert calls == [0, 100]
        third = engine.run(spec, RefreshCredentials(serpapi_key="secret"), lambda: False)
        assert third.status is RunStatus.CONTINUATION
        assert calls == [0, 100]
        assert len(ledger.manifest().data["inventory_contributions"]) == 2
        assert "provider-secret" not in ledger.manifest().canonical_json


def test_engine_rejects_nonzero_scholar_page_without_echoed_offset(tmp_path: Path) -> None:
    spec = _spec()

    def send_once(operation: SendOperation) -> requests.Response:
        start = dict(operation.request.normalized_payload)["start"]
        envelope: dict[str, object] = {
            "search_metadata": {
                "status": "Success",
                "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
            },
            "search_parameters": {
                "engine": "google_scholar_author",
                "author_id": "Scholar123",
            },
            "author": {"name": "Ada Lovelace"},
            "articles": [
                {
                    "title": f"Paper {start}",
                    "authors": "Ada Lovelace",
                    "year": 2024,
                    "citation_id": f"Scholar123:{start}",
                }
            ],
        }
        if start == 0:
            envelope["serpapi_pagination"] = {
                "next": "https://serpapi.com/search.json?engine=google_scholar_author&author_id=Scholar123"
                "&cstart=100&num=100&sort=pubdate"
            }
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(envelope).encode()
        return response

    with Ledger.open(tmp_path / "ledger.db") as ledger:
        engine = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), LedgerTransport(ledger, send_once=send_once))
        assert (
            engine.run(spec, RefreshCredentials(serpapi_key="secret"), lambda: False).status is RunStatus.CONTINUATION
        )
        assert engine.run(spec, RefreshCredentials(serpapi_key="secret"), lambda: False).status is RunStatus.BLOCKED
        manifest = ledger.manifest().data
        inventory_tasks = [item for item in manifest["tasks"] if item["operation"] == "inventory"]
        assert sorted(item["state"] for item in inventory_tasks) == ["schema_changed", "succeeded"]
        assert manifest["inventory_authorities"] == []
        assert manifest["inventory_contributions"] == []
        assert manifest["publications"] == []


def test_inventory_union_authority_and_seed_round_are_atomic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "output").mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / "output" / "baseline.json").write_text('{"total":0,"authors":{}}\n', encoding="utf-8")
    (repo / "output" / "summary.csv").write_text("title\n", encoding="utf-8")
    (repo / "data" / "a2i2.csv").write_text("Name,Scholar Link,DBLP Link\n", encoding="utf-8")
    git = shutil.which("git")
    assert git is not None
    for args in (
        ("init", "-q"),
        ("config", "user.email", "test@example.test"),
        ("config", "user.name", "Test"),
        ("add", "-A"),
        ("commit", "-qm", "test: empty corpus"),
    ):
        subprocess.run((git, *args), cwd=repo, check=True)  # noqa: S603
    commit = subprocess.run(  # noqa: S603
        (git, "rev-parse", "HEAD"), cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    original = _spec()
    spec = GenerationSpec(original.census, original.refresh_policy_version, original.adapter_versions, commit)
    ledger_path = tmp_path / "ledger.db"
    envelope = {
        "search_metadata": {
            "status": "Success",
            "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
        },
        "search_parameters": {
            "engine": "google_scholar_author",
            "author_id": "Scholar123",
            "cstart": 0,
        },
        "author": {"name": "Ada Lovelace"},
        "articles": [
            {
                "title": "Atomic Work",
                "authors": "Ada Lovelace",
                "year": 2024,
                "citation_id": "Scholar123:atomic",
                "link": "https://scholar.google.com/atomic",
            }
        ],
    }

    def send_once(_operation: SendOperation) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(envelope).encode()
        return response

    with Ledger.open(ledger_path) as ledger:
        engine = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), LedgerTransport(ledger, send_once=send_once))
        ledger.set_fault("after_reduction_receipt")
        with pytest.raises(FaultInjectedError):
            engine.run(spec, RefreshCredentials(serpapi_key="secret"), lambda: False)
        manifest = ledger.manifest().data
        assert manifest["inventory_authorities"] == []
        assert manifest["publications"] == []
        assert ledger.plan_status().revision == 1
        assert (
            engine.run(spec, RefreshCredentials(serpapi_key="secret"), lambda: False).status is RunStatus.CONTINUATION
        )
        assert len(ledger.manifest().data["inventory_authorities"]) == 1
        publication = ledger.manifest().data["publications"][0]
        seed = ledger._inventory_publication_seed(
            ledger._connection,
            spec.id,
            publication["author_key"],
            publication["publication_key"],
        )
        assert seed.baseline_entry["fields"] == {
            "author": "Ada Lovelace",
            "title": "atomic work",
            "url": "https://scholar.google.com/atomic",
            "year": "2024",
        }
        assert seed.baseline_digest == evidence_digest(seed.baseline_entry)
        corpus = ledger.scan_and_commit_corpus(repo)
        assert corpus.publications == () and corpus.seeds == ()
        durable_seed = ledger.load_seed_snapshot()
        assert len(durable_seed) == 1
        assert durable_seed[0] == seed
        with pytest.raises(ValueError, match=r"substitution|schema is not code-owned"):
            ledger.commit_inventory_union_wave(
                (replace(spec.census.enabled_rows[0], name="Wrong Person"),),
                InventoryPolicy(
                    2020, 1000, 10, "1", "1", ledger.manifest().data["generation"]["inventory_freshness_epoch"]
                ),
                reducer_version="1",
                now=datetime.now(timezone.utc),
            )
        with pytest.raises(ValueError, match=r"substitution|schema is not code-owned"):
            ledger.commit_inventory_union_wave(
                (replace(spec.census.enabled_rows[0], scholar_id="OtherProfile"),),
                InventoryPolicy(
                    2020, 1000, 10, "1", "1", ledger.manifest().data["generation"]["inventory_freshness_epoch"]
                ),
                reducer_version="1",
                now=datetime.now(timezone.utc),
            )
        with pytest.raises(ValueError, match=r"generation authority|schema is not code-owned"):
            ledger.commit_inventory_union_wave(
                spec.census.enabled_rows,
                InventoryPolicy(2020, 1000, 10, "1", "1", "wrong-epoch"),
                reducer_version="1",
                now=datetime.now(timezone.utc),
            )
        with pytest.raises(ValueError, match=r"generation authority|schema is not code-owned"):
            ledger.commit_inventory_union_wave(
                spec.census.enabled_rows,
                InventoryPolicy(
                    2020,
                    1000,
                    10,
                    "wrong-version",
                    "1",
                    ledger.manifest().data["generation"]["inventory_freshness_epoch"],
                ),
                reducer_version="1",
                now=datetime.now(timezone.utc),
            )
    with Ledger.open(ledger_path, corpus_repo_root=repo) as reopened:
        assert reopened.load_seed_snapshot() == (seed,)


def test_dblp_http_410_is_blocking_and_never_confirmed_empty(tmp_path: Path) -> None:
    census = AuthorCensus(
        (
            AuthorCensusRow(
                2,
                "author-ada",
                "Ada Lovelace",
                "ada lovelace",
                "",
                "12/345",
                True,
                "",
                TaskDisposition.PENDING,
            ),
        )
    )
    spec = GenerationSpec(
        census,
        "policy-v1",
        {"dblp": "1", "doi_csl": "1", "s2": "1"},
        "abc123",
    )

    def gone(_operation: SendOperation) -> requests.Response:
        response = requests.Response()
        response.status_code = 410
        response._content = b"gone"
        return response

    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), LedgerTransport(ledger, send_once=gone)).run(
            spec, RefreshCredentials(), lambda: False
        )
        assert result.status is RunStatus.BLOCKED
        tasks = ledger.manifest().data["tasks"]
        assert tasks[0]["state"] == "permanent_failure"
        blocked_manifest = ledger.manifest().data
        assert blocked_manifest["generation"]["state"] == "blocked"
        assert blocked_manifest["generation"]["blocking_reason"]
        observations = blocked_manifest["observations"]
        assert observations[0]["disposition"] == "permanent_failure"
        assert observations[0]["authoritative_empty"] == 0
        resumed = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), LedgerTransport(ledger, send_once=gone)).run(
            spec, RefreshCredentials(), lambda: False
        )
        assert resumed.status is RunStatus.BLOCKED


def test_forged_inventory_payload_is_rejected_before_physical_send(tmp_path: Path) -> None:
    census = AuthorCensus(
        (
            AuthorCensusRow(
                2,
                "author-ada",
                "Ada Lovelace",
                "ada lovelace",
                "",
                "12/345",
                True,
                "",
                TaskDisposition.PENDING,
            ),
        )
    )
    spec = GenerationSpec(census, "policy-v1", {"dblp": "1", "doi_csl": "1", "s2": "1"}, "abc123")
    now = datetime.now(timezone.utc)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(spec, RefreshCredentials(), lambda: True)
        source_claim = ledger.claim_due("source", now, timedelta(minutes=5))
        assert source_claim and source_claim.request_key
        source_task = ledger.reconstruct_claimed_task(source_claim, now)
        request_claim = ledger.claim_request(source_claim.key, "source", now, timedelta(minutes=5))
        assert request_claim
        observation = ProviderObservation("dblp", "dblp-person-v1", {}, authoritative_empty=True)
        ledger.finish_request(
            request_claim.key,
            "source",
            TaskDisposition.CONFIRMED_EMPTY,
            now,
            observation=observation,
        )
        ledger.finish_task(source_claim.key, "source", TaskDisposition.CONFIRMED_EMPTY, now)
        canonical_request = source_task.request
        assert canonical_request
        forged_request = RequestSpec(
            "dblp",
            "inventory",
            "GET",
            {"author_key": "foreign-author", "pid": "99/999"},
            canonical_request.requested_fields,
            "1",
            canonical_request.freshness_epoch,
            canonical_request.quota_scope,
        )
        forged = TaskSpec("author-ada", None, "dblp", "inventory", forged_request)
        ledger.commit_reduction(
            (source_claim.key,),
            source_evidence_digest="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            publications=(),
            tasks=(PlannedTask(forged, expands_plan=True),),
            now=now,
        )
        forged_claim = ledger.claim_due("forged", now, timedelta(minutes=5))
        assert forged_claim and forged_claim.key == forged.key
        with pytest.raises(ValueError, match="substitutes"):
            build_claimed_inventory_operation(
                ledger,
                forged_claim,
                RefreshCredentials(),
                InventoryPolicy(2020, 1000, 10),
                now=now,
            )
        forged_row = next(item for item in ledger.manifest().data["tasks"] if item["task_key"] == forged.key)
        assert forged_row["attempt_count"] == 0


def test_engine_blocks_durably_when_claimed_inventory_authority_is_forged(tmp_path: Path) -> None:
    census = AuthorCensus(
        (AuthorCensusRow(2, "author-ada", "Ada", "ada", "", "12/345", True, "", TaskDisposition.PENDING),)
    )
    spec = GenerationSpec(census, "policy-v1", {"dblp": "1", "doi_csl": "1", "s2": "1"}, "abc123")
    now = datetime.now(timezone.utc)
    physical_calls = 0

    def no_send(_operation: SendOperation) -> requests.Response:
        nonlocal physical_calls
        physical_calls += 1
        raise AssertionError("forged inventory must fail before send")

    with Ledger.open(tmp_path / "ledger.db") as ledger:
        RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(spec, RefreshCredentials(), lambda: True)
        source_claim = ledger.claim_due("source", now, timedelta(minutes=5))
        assert source_claim and source_claim.request_key
        source_task = ledger.reconstruct_claimed_task(source_claim, now)
        request_claim = ledger.claim_request(source_claim.key, "source", now, timedelta(minutes=5))
        assert request_claim
        observation = ProviderObservation("dblp", "dblp-person-v1", {}, authoritative_empty=True)
        ledger.finish_request(
            request_claim.key, "source", TaskDisposition.CONFIRMED_EMPTY, now, observation=observation
        )
        ledger.finish_task(source_claim.key, "source", TaskDisposition.CONFIRMED_EMPTY, now)
        assert source_task.request
        forged_request = replace(
            source_task.request,
            normalized_payload={"author_key": "foreign-author", "pid": "99/999"},
        )
        forged = TaskSpec("author-ada", None, "dblp", "inventory", forged_request)
        ledger.commit_reduction(
            (source_claim.key,),
            source_evidence_digest="44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
            publications=(),
            tasks=(PlannedTask(forged, expands_plan=True),),
            now=now,
        )
        engine = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10),
            LedgerTransport(ledger, send_once=no_send),
        )
        result = engine.run(spec, RefreshCredentials(), lambda: False)
        assert result.status is RunStatus.BLOCKED
        manifest = ledger.manifest().data
        assert manifest["generation"]["state"] == "blocked"
        forged_row = next(item for item in manifest["tasks"] if item["task_key"] == forged.key)
        assert forged_row["attempt_count"] == 0
        assert physical_calls == 0
        assert engine.run(spec, RefreshCredentials(), lambda: False).status is RunStatus.BLOCKED
        assert physical_calls == 0


def test_sixty_four_author_unions_commit_in_one_phase_wave(tmp_path: Path) -> None:
    census = AuthorCensus(
        tuple(
            AuthorCensusRow(
                index + 2,
                f"author-{index}",
                f"Author {index}",
                f"author {index}",
                "",
                f"12/{index}",
                True,
                "",
                TaskDisposition.PENDING,
            )
            for index in range(64)
        )
    )
    spec = GenerationSpec(
        census,
        "policy-v1",
        {"dblp": "1", "doi_csl": "1", "s2": "1"},
        "abc123",
    )

    def confirmed_empty(operation: SendOperation) -> requests.Response:
        calls.append(dict(operation.request.normalized_payload)["pid"])
        pid = dict(operation.request.normalized_payload)["pid"]
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/xml"
        response._content = f'<dblpperson key="homepages/{pid}"/>'.encode()
        return response

    calls: list[object] = []
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10),
            LedgerTransport(ledger, send_once=confirmed_empty),
        ).run(spec, RefreshCredentials(), lambda: False)
        assert result.status is RunStatus.CONTINUATION
        manifest = ledger.manifest().data
        assert len(manifest["inventory_authorities"]) == 64
        assert len(manifest["inventory_contributions"]) == 64
        assert len(manifest["plan_rounds"]) == 2
        replay = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10),
            LedgerTransport(ledger, send_once=confirmed_empty),
        ).run(spec, RefreshCredentials(), lambda: False)
        assert replay.status is RunStatus.CONTINUATION
        assert len(calls) == 64
        assert len(ledger.manifest().data["plan_rounds"]) == 2
