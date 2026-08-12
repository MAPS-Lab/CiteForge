from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.engine import RefreshEngine
from citeforge.refresh.inventory import InventoryPolicy, RefreshCredentials
from citeforge.refresh.ledger import FaultInjectedError, Ledger
from citeforge.refresh.transport import LedgerTransport, SendOperation
from citeforge.refresh.types import GenerationSpec, RunStatus, TaskDisposition


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


def test_engine_missing_scholar_credential_fails_before_claim(tmp_path: Path) -> None:
    spec = _spec()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(spec, RefreshCredentials(), lambda: False)
        assert result.status is RunStatus.INVALID_CONFIGURATION
        assert ledger.manifest().data["tasks"] == []


def test_engine_commits_inventory_round_but_never_closes_discovery(tmp_path: Path) -> None:
    spec = _spec()
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(
            spec, RefreshCredentials(serpapi_key="secret"), lambda: True
        )
        assert result.status is RunStatus.CONTINUATION
        status = ledger.plan_status()
        assert status.revision == 1
        assert not status.closed and not status.discovery_closed
        assert "secret" not in repr(RefreshCredentials(serpapi_key="secret"))
        assert "secret" not in ledger.manifest().canonical_json


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


def test_engine_executes_exact_inventory_and_commits_union_seed(tmp_path: Path) -> None:
    spec = _spec()
    body = {
        "search_metadata": {"status": "Success"},
        "search_parameters": {
            "engine": "google_scholar_author",
            "author_id": "Scholar123",
            "start": 0,
        },
        "author": {"author_id": "Scholar123"},
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


def test_engine_paginates_in_durable_waves_without_repeating_success(tmp_path: Path) -> None:
    spec = _spec()
    calls: list[int] = []

    def send_once(operation: SendOperation) -> requests.Response:
        request = operation.request
        start = dict(request.normalized_payload)["start"]
        calls.append(start)
        envelope = {
            "search_metadata": {"status": "Success"},
            "search_parameters": {
                "engine": "google_scholar_author",
                "author_id": "Scholar123",
                "start": start,
            },
            "author": {"author_id": "Scholar123"},
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
                "next": "https://serpapi.com/search?engine=google_scholar_author&author_id=Scholar123"
                "&start=100&num=100&sort=pubdate&api_key=provider-secret"
            }
        response = requests.Response()
        response.status_code = 200
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


def test_inventory_union_authority_and_seed_round_are_atomic(tmp_path: Path) -> None:
    spec = _spec()
    envelope = {
        "search_metadata": {"status": "Success"},
        "search_parameters": {
            "engine": "google_scholar_author",
            "author_id": "Scholar123",
            "start": 0,
        },
        "author": {"author_id": "Scholar123"},
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
        response._content = json.dumps(envelope).encode()
        return response

    with Ledger.open(tmp_path / "ledger.db") as ledger:
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
        observations = ledger.manifest().data["observations"]
        assert observations[0]["disposition"] == "permanent_failure"
        assert observations[0]["authoritative_empty"] == 0
