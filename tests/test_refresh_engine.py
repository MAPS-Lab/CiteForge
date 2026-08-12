from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.engine import RefreshEngine
from citeforge.refresh.inventory import (
    InventoryPolicy,
    RefreshCredentials,
    build_claimed_inventory_operation,
)
from citeforge.refresh.ledger import FaultInjectedError, Ledger, PlannedTask, ProviderObservation, RequestSpec, TaskSpec
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
    spec = _spec()
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
        with pytest.raises(ValueError, match="substitution"):
            ledger.commit_inventory_union_wave(
                (replace(spec.census.enabled_rows[0], name="Wrong Person"),),
                InventoryPolicy(
                    2020, 1000, 10, "1", "1", ledger.manifest().data["generation"]["inventory_freshness_epoch"]
                ),
                reducer_version="1",
                now=datetime.now(timezone.utc),
            )
        with pytest.raises(ValueError, match="substitution"):
            ledger.commit_inventory_union_wave(
                (replace(spec.census.enabled_rows[0], scholar_id="OtherProfile"),),
                InventoryPolicy(
                    2020, 1000, 10, "1", "1", ledger.manifest().data["generation"]["inventory_freshness_epoch"]
                ),
                reducer_version="1",
                now=datetime.now(timezone.utc),
            )
        with pytest.raises(ValueError, match="generation authority"):
            ledger.commit_inventory_union_wave(
                spec.census.enabled_rows,
                InventoryPolicy(2020, 1000, 10, "1", "1", "wrong-epoch"),
                reducer_version="1",
                now=datetime.now(timezone.utc),
            )
        with pytest.raises(ValueError, match="generation authority"):
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
