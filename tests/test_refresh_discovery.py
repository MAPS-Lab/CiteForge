from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from citeforge.refresh.authority import (
    EvidenceKind,
    PublicationSeedEvidence,
    evidence_digest,
)
from citeforge.refresh.capabilities import (
    GEMINI_GENERATION_CONFIG,
    GEMINI_MODEL_ID,
    GEMINI_PROMPT_VERSION,
    build_request,
)
from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.discovery import (
    ApplicabilityReason,
    DiscoveryAuthority,
    DiscoveryCredentials,
    DiscoveryObservation,
    DiscoveryPolicy,
    DoiReduction,
    build_claimed_discovery_operation,
    plan_broad_discovery,
    plan_doi_bibtex,
    plan_dynamic_expansion,
    plan_known_doi,
    reduce_current_doi_observations,
    reduce_doi_observations,
    resolve_discovery_authority,
)
from citeforge.refresh.engine import RefreshEngine
from citeforge.refresh.inventory import InventoryPolicy, RefreshCredentials
from citeforge.refresh.ledger import FaultInjectedError, Ledger, ProviderObservation, RequestSpec, TaskClaim, TaskSpec
from citeforge.refresh.transport import LedgerTransport, SendOperation
from citeforge.refresh.types import GenerationSpec, TaskDisposition

NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)
_GIT = shutil.which("git") or "git"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run((_GIT, *args), cwd=repo, check=True, capture_output=True, text=True).stdout.strip()  # noqa: S603


def _real_corpus_authority(
    tmp_path: Path, *, empty: bool = False, venue_without_doi: bool = False
) -> tuple[Path, str, AuthorCensus]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    author_dir.mkdir(parents=True)
    if not empty:
        entry = (
            "@article{Key,title={A title},author={Lovelace, Ada},year={2026},"
            "journal={Journal of Engines 12(3), 44-51, 2026}}\n"
            if venue_without_doi
            else "@article{Key,title={A title},author={Lovelace, Ada},year={2026},doi={10.1000/X}}\n"
        )
        (author_dir / "paper.bib").write_text(
            entry,
            encoding="utf-8",
        )
    (repo / "output" / "baseline.json").write_text(
        ('{"total":0,"authors":{}}\n' if empty else '{"total":1,"authors":{"Lovelace (Scholar123)":1}}\n'),
        encoding="utf-8",
    )
    (repo / "output" / "summary.csv").write_text("title\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "a2i2.csv").write_text("Name,Scholar Link,DBLP Link\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "fixture")
    commit = _git(repo, "rev-parse", "HEAD")
    row = AuthorCensusRow(
        2, "author-ada", "Ada Lovelace", "ada lovelace", "Scholar123", "", True, "", TaskDisposition.PENDING
    )
    return repo, commit, AuthorCensus((row,))


def _policy(*, max_scholar_pages: int = 10, max_html_probe_waves: int = 8) -> DiscoveryPolicy:
    return DiscoveryPolicy(
        freshness_epoch="2026-08",
        adapter_versions={
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
        },
        candidate_limits={
            "arxiv": 10,
            "crossref": 20,
            "europepmc": 20,
            "openalex": 20,
            "openreview": 20,
            "pubmed": 5,
            "s2": 15,
            "serply": 20,
        },
        provider_modes={"gemini": "disabled", "s2": "required", "serply": "if_configured"},
        openreview_mode="anonymous",
        crossref_contact_enabled=False,
        openalex_contact_enabled=False,
        max_scholar_pages=max_scholar_pages,
        max_html_probe_waves=max_html_probe_waves,
    )


def _seed(author: str, publication: str, doi: str | None) -> PublicationSeedEvidence:
    fields = {"title": f"Title {publication}", "year": "2024"}
    identifiers: dict[str, str] = {}
    if doi is not None:
        fields["doi"] = doi
        identifiers["doi"] = doi
    entry = {"type": "article", "key": publication, "fields": fields}
    seed = PublicationSeedEvidence(
        "generation",
        author,
        publication,
        EvidenceKind.PUBLICATION,
        f"inventory:{author}:1:{'a' * 64}",
        "b" * 64,
        evidence_digest(entry),
        identifiers,
        "0" * 64,
        entry,
    )
    return replace(seed, seed_digest=seed.derived_seed_digest)


def _authority(policy: DiscoveryPolicy | None = None) -> DiscoveryAuthority:
    return resolve_discovery_authority(policy or _policy(), DiscoveryCredentials(s2_key="wire-only"))


def test_known_doi_wave_covers_exact_seed_union_and_coalesces_only_request() -> None:
    wave = plan_known_doi(
        (
            _seed("author-ada", "pub-one", "HTTPS://doi.org/10.1234/SHARED"),
            _seed("author-grace", "pub-two", "10.1234/shared"),
            _seed("author-mary", "pub-three", None),
        ),
        _authority(),
    )
    assert len(wave.decisions) == 3
    applicable = [decision.task for decision in wave.decisions if decision.task.request is not None]
    assert len(applicable) == 2
    assert applicable[0].key != applicable[1].key
    assert applicable[0].request is not None and applicable[1].request is not None
    assert applicable[0].request.key == applicable[1].request.key
    assert applicable[0].request.normalized_payload == {"doi": "10.1234/shared"}
    absent = next(decision for decision in wave.decisions if decision.task.publication_key == "pub-three")
    assert absent.reason is ApplicabilityReason.NO_APPLICABLE_IDENTIFIER
    assert absent.task.applicability == "not_applicable" and absent.task.request is None


def test_known_doi_wave_rejects_missing_substituted_or_incomplete_seed() -> None:
    seed = _seed("author-ada", "pub-one", "10.1234/one")
    with pytest.raises(ValueError, match="seed"):
        plan_known_doi((replace(seed, seed_digest="f" * 64),), _authority())
    conflicting = replace(seed, exact_identifiers={"doi": "10.1234/two"}, seed_digest="0" * 64)
    conflicting = replace(conflicting, seed_digest=conflicting.derived_seed_digest)
    with pytest.raises(ValueError, match="DOI"):
        plan_known_doi((conflicting,), _authority())
    with pytest.raises(ValueError, match="duplicate"):
        plan_known_doi((seed, seed), _authority())


def test_known_doi_wave_accepts_url_derived_seed_doi() -> None:
    seed = _seed("author-ada", "pub-one", None)
    entry = {
        "type": "article",
        "key": "pub-one",
        "fields": {"title": "URL DOI", "url": "https://doi.org/10.1234/derived", "year": "2024"},
    }
    seed = replace(
        seed,
        baseline_digest=evidence_digest(entry),
        baseline_entry=entry,
        exact_identifiers={"doi": "10.1234/derived"},
        seed_digest="0" * 64,
    )
    seed = replace(seed, seed_digest=seed.derived_seed_digest)
    decision = plan_known_doi((seed,), _authority()).decisions[0]
    assert decision.task.request is not None
    assert decision.task.request.normalized_payload == {"doi": "10.1234/derived"}


def test_known_doi_wave_adopts_exact_task5b_csl_identity() -> None:
    seed = _seed("author-ada", "pub-one", "10.1234/existing")
    existing_request = RequestSpec(
        "doi_csl",
        "csl_lookup",
        "GET",
        {"doi": "10.1234/existing"},
        ("metadata",),
        "1",
        "2026-08",
        "doi",
    )
    existing = TaskSpec("author-ada", "pub-one", "doi_csl", "csl_lookup", existing_request)
    planned = plan_known_doi((seed,), _authority()).decisions[0].task
    assert planned == existing
    assert planned.key == existing.key and planned.request == existing.request


def test_doi_bibtex_expansion_uses_identity_not_completeness() -> None:
    seeds = (
        _seed("author-ada", "pub-match", "10.1234/match"),
        _seed("author-grace", "pub-mismatch", "10.1234/mismatch"),
        _seed("author-mary", "pub-empty", "10.1234/empty"),
    )
    authority = _authority()
    known = plan_known_doi(seeds, authority)
    tasks = {decision.task.publication_key: decision.task for decision in known.decisions}
    evidence = (
        DiscoveryObservation(
            tasks["pub-match"],
            TaskDisposition.SUCCEEDED,
            {"metadata": {"title": "Title pub-match", "DOI": "10.1234/match"}},
            schema_version="doi-csl-v1",
        ),
        DiscoveryObservation(
            tasks["pub-mismatch"],
            TaskDisposition.SUCCEEDED,
            {"metadata": {"title": "A completely unrelated title", "DOI": "10.1234/mismatch"}},
            schema_version="doi-csl-v1",
        ),
        DiscoveryObservation(
            tasks["pub-empty"],
            TaskDisposition.CONFIRMED_EMPTY,
            {},
            authoritative_empty=True,
            schema_version="doi-csl-v1",
        ),
    )
    wave = plan_doi_bibtex(seeds, known, evidence, authority)
    by_publication = {decision.task.publication_key: decision for decision in wave.decisions}
    matched = by_publication["pub-match"]
    assert matched.task.request is None
    assert matched.reason is ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE
    for publication in ("pub-mismatch", "pub-empty"):
        decision = by_publication[publication]
        assert decision.task.request is not None
        assert decision.task.provider == "doi_bibtex"
        assert decision.task.request.normalized_payload == {"doi": f"10.1234/{publication.removeprefix('pub-')}"}
    with pytest.raises(ValueError, match="blocking"):
        replace(evidence[0], disposition=TaskDisposition.SCHEMA_CHANGED)
    blocked = DiscoveryObservation(
        evidence[0].task,
        TaskDisposition.SCHEMA_CHANGED,
        {},
        schema_version="doi-csl-v1",
    )
    with pytest.raises(ValueError, match="terminal CSL"):
        plan_doi_bibtex(seeds, known, (blocked, *evidence[1:]), authority)
    reductions = reduce_doi_observations(seeds, known, evidence, authority)
    assert {item.publication_key: item.status for item in reductions} == {
        "pub-empty": "fallback_required",
        "pub-match": "identity_matched",
        "pub-mismatch": "fallback_required",
    }
    with pytest.raises(ValueError, match="membership"):
        reduce_doi_observations(seeds, known, evidence[:-1], authority)


def test_doi_bibtex_digest_binds_no_identifier_members() -> None:
    authority = _authority()
    one_seed = (_seed("author-ada", "pub-one", None),)
    two_seeds = (*one_seed, _seed("author-grace", "pub-two", None))
    one = plan_doi_bibtex(one_seed, plan_known_doi(one_seed, authority), (), authority)
    two = plan_doi_bibtex(two_seeds, plan_known_doi(two_seeds, authority), (), authority)
    assert one.input_digest != two.input_digest
    assert len(one.decisions) == 1
    assert one.decisions[0].task.provider == "doi_bibtex"
    assert one.decisions[0].task.request is None
    assert one.decisions[0].reason is ApplicabilityReason.NO_APPLICABLE_IDENTIFIER


@pytest.mark.parametrize(
    "freshness_epoch",
    (" api_key=value", "api_key=value", "bad epoch", "bad\nvalue"),
)
def test_discovery_policy_rejects_unsafe_freshness_epoch(freshness_epoch: str) -> None:
    with pytest.raises(ValueError, match=r"freshness epoch|secret|control"):
        replace(_policy(), freshness_epoch=freshness_epoch)


def test_discovery_policy_rejects_non_planner_emittable_s2_v1() -> None:
    adapters = dict(_policy().adapter_versions)
    adapters["s2"] = "1"
    with pytest.raises(ValueError, match="planner-emittable"):
        replace(_policy(), adapter_versions=adapters)


@pytest.mark.parametrize("provider", ("s2", "openreview", "serply"))
def test_discovery_policy_rejects_output_irrelevant_lower_candidate_limit(provider: str) -> None:
    limits = dict(_policy().candidate_limits)
    limits[provider] -= 1
    with pytest.raises(ValueError, match="fixed provider bounds"):
        replace(_policy(), candidate_limits=limits)


def test_c4_round_budget_preflight_is_exact() -> None:
    # 44 Scholar pages + 9 pass waves + 3 expansions + 8 HTML waves.
    assert _policy(max_scholar_pages=44, max_html_probe_waves=8).round_budget == 64
    with pytest.raises(ValueError, match="round budget"):
        _policy(max_scholar_pages=45, max_html_probe_waves=8)


def test_broad_wave_emits_exact_capability_matrix_or_applicability() -> None:
    seed = _seed("author-ada", "pub-one", None)
    policy = _policy()
    wave = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        _authority(policy),
        (DoiReduction("author-ada", "pub-one", "no_identifier", "0" * 64),),
    )
    assert len(wave.decisions) == 8
    operations = {(item.task.provider, item.task.operation) for item in wave.decisions}
    assert operations == {
        ("arxiv", "fuzzy_search"),
        ("crossref", "fuzzy_search"),
        ("europepmc", "fuzzy_search"),
        ("openalex", "fuzzy_search"),
        ("openreview", "term_search"),
        ("pubmed", "title_search"),
        ("s2", "fuzzy_search"),
        ("serply", "scholar_search"),
    }
    serply = next(item for item in wave.decisions if item.task.provider == "serply")
    assert serply.reason is ApplicabilityReason.PROVIDER_NOT_CONFIGURED
    assert serply.task.request is None and serply.task.applicability == "not_applicable"
    assert all(
        item.task.author_key == "author-ada" and item.task.publication_key == "pub-one" for item in wave.decisions
    )
    assert not any(item.task.provider in {"gemini", "web", "dblp"} for item in wave.decisions)


def test_broad_identity_binds_every_wire_affecting_value() -> None:
    seed = _seed("author-ada", "pub-one", None)
    credentials = DiscoveryCredentials(s2_key="s2-wire", serply_key="serply-wire")
    authority = resolve_discovery_authority(_policy(), credentials)
    wave = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (DoiReduction("author-ada", "pub-one", "no_identifier", "0" * 64),),
    )

    class ClaimLedger:
        def __init__(self, task: TaskSpec) -> None:
            self.task = task

        def reconstruct_claimed_task(self, claim: TaskClaim, _now: datetime) -> TaskSpec:
            assert claim.key == self.task.key
            return self.task

        def assert_discovery_authority(self, supplied: DiscoveryAuthority) -> DiscoveryAuthority:
            assert supplied == authority
            return authority

    built = {}
    for decision in wave.decisions:
        task = decision.task
        if task.request is None:
            continue
        claim = TaskClaim(task.key, task.request.key, "worker", NOW + timedelta(minutes=1))
        operation = build_claimed_discovery_operation(  # type: ignore[arg-type]
            ClaimLedger(task),  # type: ignore[arg-type]  # duck-typed ledger stub, proves builder needs no real Ledger
            claim,
            credentials,
            authority,
            now=NOW,
        )
        built[task.provider] = operation
        assert operation.request == task.request
        assert operation.capability_id is not None
        assert task.request.key not in repr(credentials)
    assert set(built) == {"arxiv", "crossref", "europepmc", "openalex", "openreview", "pubmed", "s2", "serply"}
    assert "TITLE%3A%22Title+pub-one%22+AND+AUTH%3A%22Ada+Lovelace%22" in built["europepmc"].url
    assert built["s2"].url.endswith(
        "query=%22Title+pub-one%22+Ada+Lovelace&limit=15&fields="
        "paperId%2Ctitle%2Cyear%2Cvenue%2CpublicationTypes%2Cauthors%2Curl%2Cjournal%2CexternalIds%2C"
        "publicationDate%2Cabstract"
    )
    assert built["s2"].headers == {"x-api-key": "s2-wire"}
    assert built["serply"].headers == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "X-API-KEY": "serply-wire",
        "X-Proxy-Location": "US",
    }


def test_claimed_html_probe_uses_only_ledger_resolved_raw_url() -> None:
    authority = _authority()
    raw_url = "https://papers.example.test/publication/engine"
    request = RequestSpec(
        "web",
        "doi_probe",
        "GET",
        {"scheme": "https", "url_digest": hashlib.sha256(raw_url.encode()).hexdigest()},
        ("doi",),
        "1",
        authority.policy.freshness_epoch,
        "web",
    )
    task = TaskSpec("author-ada", "pub-one", "web", "doi_probe", request)
    claim = TaskClaim(task.key, request.key, "worker", NOW + timedelta(minutes=1))

    class ClaimLedger:
        def reconstruct_claimed_task(self, supplied: TaskClaim, _now: datetime) -> TaskSpec:
            assert supplied == claim
            return task

        def assert_discovery_authority(self, supplied: DiscoveryAuthority) -> DiscoveryAuthority:
            assert supplied == authority
            return authority

        def resolve_claimed_web_probe_url(self, supplied: TaskClaim, *, now: datetime) -> str:
            assert supplied == claim and now == NOW
            return raw_url

    operation = build_claimed_discovery_operation(  # type: ignore[arg-type]
        ClaimLedger(),  # type: ignore[arg-type]  # duck-typed ledger stub
        claim,
        DiscoveryCredentials(s2_key="wire-only"),
        authority,
        now=NOW,
    )
    assert operation.request == request
    assert raw_url not in repr(operation)

    class SubstitutingLedger(ClaimLedger):
        def resolve_claimed_web_probe_url(self, supplied: TaskClaim, *, now: datetime) -> str:
            return "https://papers.example.test/publication/other"

    with pytest.raises(ValueError, match="builder identity"):
        build_claimed_discovery_operation(  # type: ignore[arg-type]
            SubstitutingLedger(),  # type: ignore[arg-type]  # duck-typed ledger stub that substitutes the probe URL
            claim,
            DiscoveryCredentials(s2_key="wire-only"),
            authority,
            now=NOW,
        )


def test_claimed_gemini_operation_injects_only_runtime_key() -> None:
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire", gemini_key="secret"))
    built = build_request(
        "gemini.short_title.v1",
        {
            "title": "Analytical Engine",
            "max_words": 4,
            "prompt_version": GEMINI_PROMPT_VERSION,
            "model_id": GEMINI_MODEL_ID,
            "generation_config": dict(GEMINI_GENERATION_CONFIG),
        },
    )
    request = RequestSpec(
        "gemini",
        "short_title",
        built.method,
        built.identity_payload,
        ("candidates",),
        "1",
        authority.policy.freshness_epoch,
        "gemini",
    )
    task = TaskSpec("author-ada", "pub-one", "gemini", "short_title", request)
    claim = TaskClaim(task.key, request.key, "worker", NOW + timedelta(minutes=1))

    class ClaimLedger:
        def reconstruct_claimed_task(self, supplied: TaskClaim, _now: datetime) -> TaskSpec:
            assert supplied == claim
            return task

        def assert_discovery_authority(self, supplied: DiscoveryAuthority) -> DiscoveryAuthority:
            assert supplied == authority
            return authority

    operation = build_claimed_discovery_operation(  # type: ignore[arg-type]
        ClaimLedger(),  # type: ignore[arg-type]  # duck-typed ledger stub
        claim,
        DiscoveryCredentials(s2_key="wire", gemini_key="secret"),
        authority,
        now=NOW,
    )
    assert operation.headers == {"x-goog-api-key": "secret"}
    assert "secret" not in repr(operation) and "secret" not in repr(request)
    with pytest.raises(ValueError, match="credential"):
        build_claimed_discovery_operation(  # type: ignore[arg-type]
            ClaimLedger(),  # type: ignore[arg-type]  # duck-typed ledger stub
            claim,
            DiscoveryCredentials(s2_key="wire"),
            authority,
            now=NOW,
        )


def test_europepmc_query_matches_legacy_embedded_quote_sanitization() -> None:
    seed = _seed("author-ada", "pub-one", None)
    entry = {
        "type": "article",
        "key": "pub-one",
        "fields": {"title": 'Effects of "AI"', "year": "2024"},
    }
    seed = replace(seed, baseline_digest=evidence_digest(entry), baseline_entry=entry, seed_digest="0" * 64)
    seed = replace(seed, seed_digest=seed.derived_seed_digest)
    wave = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        _authority(),
        (DoiReduction("author-ada", "pub-one", "no_identifier", "0" * 64),),
    )
    task = next(item.task for item in wave.decisions if item.task.provider == "europepmc")
    assert task.request is not None
    assert task.request.normalized_payload["query"] == 'TITLE:"Effects of AI" AND AUTH:"Ada Lovelace"'


def test_successful_identity_matching_bibtex_can_suppress_broad_search() -> None:
    seed = _seed("author-ada", "pub-one", "10.1000/x")
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only"))
    known = plan_known_doi((seed,), authority)
    csl_task = known.decisions[0].task
    csl = DiscoveryObservation(csl_task, TaskDisposition.CONFIRMED_EMPTY, {}, True, "doi-csl-v1")
    bibtex = plan_doi_bibtex((seed,), known, (csl,), authority)
    bibtex_task = bibtex.decisions[0].task
    observation = DiscoveryObservation(
        bibtex_task,
        TaskDisposition.SUCCEEDED,
        {
            "metadata": {
                "type": "article",
                "key": "pub-one",
                "fields": {
                    "author": "Ada Lovelace",
                    "doi": "10.1000/x",
                    "journal": "Proceedings",
                    "title": "Title pub-one",
                    "year": "2024",
                },
            }
        },
        False,
        "doi-bibtex-v1",
    )
    reductions = reduce_current_doi_observations((seed,), known, (csl,), bibtex, (observation,), authority)
    assert reductions[0].status == "identity_matched"
    broad = plan_broad_discovery((seed,), {"author-ada": "Ada Lovelace"}, authority, reductions)
    assert len(broad.decisions) == 8
    assert {decision.reason for decision in broad.decisions} == {ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE}


def test_pubmed_expansion_is_singleton_complete_and_correlated() -> None:
    seed = _seed("author-ada", "pub-one", None)
    authority = _authority()
    broad = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (DoiReduction("author-ada", "pub-one", "no_identifier", "0" * 64),),
    )
    pubmed = next(item.task for item in broad.decisions if item.task.provider == "pubmed")
    openreview = next(item.task for item in broad.decisions if item.task.provider == "openreview")
    observations = [
        DiscoveryObservation(
            pubmed,
            TaskDisposition.SUCCEEDED,
            {"pmids": ("123", "456")},
            schema_version="pubmed-esearch-v1",
        ),
        DiscoveryObservation(
            openreview,
            TaskDisposition.CONFIRMED_EMPTY,
            {},
            authoritative_empty=True,
            schema_version="openreview-notes-v1",
        ),
    ]
    schema_by_provider = {
        "arxiv": "arxiv-atom-v1",
        "crossref": "crossref-search-v1",
        "europepmc": "europepmc-search-v1",
        "openalex": "openalex-search-v1",
        "s2": "s2-search-v2",
    }
    result_field = {"arxiv": "entries", "serply": "articles"}
    for decision in broad.decisions:
        if decision.task.request is None or decision.task.provider in {"pubmed", "openreview"}:
            continue
        observations.append(
            DiscoveryObservation(
                decision.task,
                TaskDisposition.SUCCEEDED,
                {result_field.get(decision.task.provider, "results"): ({"title": "candidate"},)},
                schema_version=schema_by_provider[decision.task.provider],
            )
        )
    dynamic = plan_dynamic_expansion(broad, observations, authority)
    summaries = [item for item in dynamic.decisions if item.task.provider == "pubmed"]
    assert len(summaries) == 2
    assert all(item.task.request is not None for item in summaries)
    summary_members = {
        tuple(item.task.request.normalized_payload["requested_pmids"])  # type: ignore[union-attr,arg-type]
        for item in summaries
    }
    assert summary_members == {
        ("123",),
        ("456",),
    }
    fallback = next(item for item in dynamic.decisions if item.task.provider == "openreview")
    assert fallback.task.operation == "fallback_search" and fallback.task.request is not None
    duplicate = replace(observations[0], response={"pmids": ("123", "123")})
    with pytest.raises(ValueError, match="PMID"):
        plan_dynamic_expansion(broad, (duplicate, *observations[1:]), authority)
    with pytest.raises(ValueError, match="membership"):
        plan_dynamic_expansion(broad, tuple(observations[:-1]), authority)


def test_dynamic_openreview_cap_and_conditional_applicability_are_exact() -> None:
    seed = _seed("author-ada", "pub-one", None)
    authority = _authority()
    broad = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (DoiReduction("author-ada", "pub-one", "no_identifier", "0" * 64),),
    )
    observations = []
    schema_by_provider = {
        "arxiv": "arxiv-atom-v1",
        "crossref": "crossref-search-v1",
        "europepmc": "europepmc-search-v1",
        "openalex": "openalex-search-v1",
        "openreview": "openreview-notes-v1",
        "pubmed": "pubmed-esearch-v1",
        "s2": "s2-search-v2",
        "serply": "serply-scholar-v1",
    }
    for decision in broad.decisions:
        task = decision.task
        if task.request is None:
            continue
        response = {"results": ({"title": "candidate"},)}
        if task.provider == "arxiv":
            response = {"entries": ({"title": "candidate"},)}
        elif task.provider == "serply":
            response = {"articles": ({"title": "candidate"},)}
        if task.provider == "pubmed":
            response = {"pmids": ("123",)}
        elif task.provider == "openreview":
            response = {"notes": tuple({"id": str(index)} for index in range(21))}
        observations.append(
            DiscoveryObservation(
                task,
                TaskDisposition.SUCCEEDED,
                response,
                schema_version=schema_by_provider[task.provider],
            )
        )
    with pytest.raises(ValueError, match=r"OpenReview.*limit"):
        plan_dynamic_expansion(broad, observations, authority)
    openreview_index = next(index for index, item in enumerate(observations) if item.task.provider == "openreview")
    empty_success = replace(observations[openreview_index], response={"notes": ()})
    with pytest.raises(ValueError, match=r"OpenReview.*limit"):
        plan_dynamic_expansion(
            broad,
            (*observations[:openreview_index], empty_success, *observations[openreview_index + 1 :]),
            authority,
        )

    complete = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (
            DoiReduction(
                "author-ada",
                "pub-one",
                "identity_matched",
                "0" * 64,
                {
                    "DOI": "10.1234/published",
                    "author": ({"family": "Lovelace", "given": "Ada"},),
                    "container-title": "Proceedings",
                    "issued": {"date-parts": ((2024,),)},
                    "title": "Title",
                },
            ),
        ),
    )
    dynamic = plan_dynamic_expansion(complete, (), authority)
    fallback = next(item for item in dynamic.decisions if item.task.provider == "openreview")
    assert fallback.task.operation == "fallback_search"
    assert fallback.task.request is None
    assert fallback.reason is ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE

    incomplete = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (
            DoiReduction(
                "author-ada",
                "pub-one",
                "identity_matched",
                "0" * 64,
                {"DOI": "10.1234/published", "author": (), "issued": {"date-parts": ()}, "title": "Title"},
            ),
        ),
    )
    assert all(item.task.request is not None for item in incomplete.decisions if item.task.provider != "serply")

    complete_entry = dict(seed.baseline_entry)
    baseline_fields = seed.baseline_entry["fields"]
    assert isinstance(baseline_fields, Mapping)
    complete_entry["fields"] = {
        **baseline_fields,
        "author": "Lovelace, Ada",
        "journal": "Proceedings",
    }
    complete_seed = replace(
        seed,
        baseline_entry=complete_entry,
        baseline_digest=evidence_digest(complete_entry),
    )
    complete_seed = replace(complete_seed, seed_digest=complete_seed.derived_seed_digest)
    baseline_complete = plan_broad_discovery(
        (complete_seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (
            DoiReduction(
                "author-ada",
                "pub-one",
                "identity_matched",
                "0" * 64,
                {"DOI": "10.1234/published", "title": "Title"},
            ),
        ),
    )
    assert all(item.task.request is None for item in baseline_complete.decisions)


@pytest.mark.parametrize(
    ("provider", "field"),
    (
        ("arxiv", "entries"),
        ("crossref", "results"),
        ("europepmc", "results"),
        ("openalex", "results"),
        ("s2", "results"),
        ("serply", "articles"),
    ),
)
def test_dynamic_revalidates_every_broad_candidate_bound(provider: str, field: str) -> None:
    seed = _seed("author-ada", "pub-one", None)
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only", serply_key="wire-only"))
    broad = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (DoiReduction("author-ada", "pub-one", "no_identifier", "0" * 64),),
    )
    schema_by_provider = {
        "arxiv": "arxiv-atom-v1",
        "crossref": "crossref-search-v1",
        "europepmc": "europepmc-search-v1",
        "openalex": "openalex-search-v1",
        "openreview": "openreview-notes-v1",
        "pubmed": "pubmed-esearch-v1",
        "s2": "s2-search-v2",
        "serply": "serply-scholar-v1",
    }
    observations = []
    for decision in broad.decisions:
        task = decision.task
        if task.request is None:
            continue
        response = {"results": ({"title": "candidate"},)}
        if task.provider == "arxiv":
            response = {"entries": ({"title": "candidate"},)}
        elif task.provider == "serply":
            response = {"articles": ({"title": "candidate"},)}
        elif task.provider == "openreview":
            response = {"notes": ({"id": "note"},)}
        elif task.provider == "pubmed":
            response = {"pmids": ("123",)}
        if task.provider == provider:
            response = {
                field: tuple({"title": "candidate"} for _ in range(authority.policy.candidate_limits[provider] + 1))
            }
        observations.append(
            DiscoveryObservation(
                task,
                TaskDisposition.SUCCEEDED,
                response,
                schema_version=schema_by_provider[task.provider],
            )
        )
    with pytest.raises(ValueError, match="bound limit"):
        plan_dynamic_expansion(broad, observations, authority)


def test_discovery_policy_authority_is_append_only_and_replays_exactly(tmp_path: Path) -> None:
    row = AuthorCensusRow(
        2, "author-ada", "Ada Lovelace", "ada lovelace", "Scholar123", "", True, "", TaskDisposition.PENDING
    )
    census = AuthorCensus((row,))
    spec = GenerationSpec(
        census,
        "policy-v1",
        dict(_policy().adapter_versions),
        "abc123",
    )
    ledger_path = tmp_path / "policy.db"
    with Ledger.open(ledger_path) as ledger:
        ledger.create_or_resume(spec, census)
        credentials = DiscoveryCredentials(s2_key="wire-only")
        authority_digest = ledger.bind_discovery_policy(_policy(), credentials)
        assert ledger.bind_discovery_policy(_policy(), credentials) == authority_digest
        changed = replace(_policy(), max_html_probe_waves=7)
        with pytest.raises(ValueError, match="discovery policy"):
            ledger.bind_discovery_policy(changed, credentials)
        with pytest.raises(Exception, match=r"append-only|not authorized"):
            ledger._connection.execute(
                "UPDATE discovery_policy_authority SET policy_digest = ?",
                ("0" * 64,),
            )


def test_bound_disabled_s2_shapes_inventory_union_as_typed_applicability(tmp_path: Path) -> None:
    repo, commit, census = _real_corpus_authority(tmp_path)
    policy = replace(_policy(), provider_modes={"gemini": "disabled", "s2": "disabled", "serply": "disabled"})
    spec = GenerationSpec(census, "policy-v1", {**dict(policy.adapter_versions), "scholar": "1"}, commit)

    def send_once(_operation: SendOperation) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "search_metadata": {
                    "status": "Success",
                    "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
                },
                "search_parameters": {"engine": "google_scholar_author", "author_id": "Scholar123", "cstart": 0},
                "author": {"name": "Ada Lovelace"},
                "articles": [
                    {
                        "title": "No DOI publication",
                        "authors": "Ada Lovelace",
                        "year": 2024,
                        "citation_id": "Scholar123:no-doi",
                        "publication": "Proceedings, 2024",
                        "link": "https://scholar.google.com/no-doi",
                    }
                ],
            }
        ).encode()
        return response

    with Ledger.open(tmp_path / "disabled-s2.db", corpus_repo_root=repo) as ledger:
        ledger.create_or_resume(spec, census)
        ledger.bind_discovery_policy(policy, DiscoveryCredentials())
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10, s2_adapter_version="2", freshness_epoch="2026-08"),
            LedgerTransport(ledger, send_once=send_once),
        ).run(spec, RefreshCredentials(serpapi_key="wire-only"), lambda: False)
        assert result.status.value == "continuation", result.detail
        row = ledger._connection.execute(
            "SELECT state, applicability_reason, request_key FROM tasks "
            "WHERE provider='s2' AND operation='fuzzy_search'"
        ).fetchone()
        assert tuple(row) == ("not_applicable", "provider_disabled", None)


def test_discovery_policy_reopen_rejects_stale_registry_even_when_rehashed(tmp_path: Path) -> None:
    row = AuthorCensusRow(2, "author-ada", "Ada Lovelace", "ada lovelace", "", "", True, "", TaskDisposition.PENDING)
    census = AuthorCensus((row,))
    spec = GenerationSpec(census, "policy-v1", dict(_policy().adapter_versions), "abc123")
    ledger_path = tmp_path / "stale-registry.db"
    with Ledger.open(ledger_path) as ledger:
        ledger.create_or_resume(spec, census)
        ledger.bind_discovery_policy(_policy(), DiscoveryCredentials(s2_key="wire-only"))
        raw = ledger._connection.execute("SELECT policy_json FROM discovery_policy_authority").fetchone()[0]
        content = json.loads(raw)
        content["capability_registry_digest"] = "0" * 64
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        ledger._connection.execute("DROP TRIGGER discovery_policy_authority_append_only_update")
        ledger._connection.execute(
            "UPDATE discovery_policy_authority SET policy_json = ?, policy_digest = ?",
            (canonical, evidence_digest(content)),
        )
        ledger._connection.execute(
            "CREATE TRIGGER discovery_policy_authority_append_only_update BEFORE UPDATE ON "
            "discovery_policy_authority BEGIN SELECT RAISE(ABORT, "
            "'discovery_policy_authority is append-only'); END"
        )
        ledger._connection.commit()
    with pytest.raises(ValueError, match="discovery policy authority"):
        Ledger.open(ledger_path)


def test_atomic_known_doi_wave_commits_complete_round_and_replays(tmp_path: Path) -> None:
    repo, commit, census = _real_corpus_authority(tmp_path)
    policy = _policy()
    spec = GenerationSpec(census, "policy-v1", {**dict(policy.adapter_versions), "scholar": "1"}, commit)

    def send_once(_operation: SendOperation) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "search_metadata": {
                    "status": "Success",
                    "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
                },
                "search_parameters": {"engine": "google_scholar_author", "author_id": "Scholar123", "cstart": 0},
                "author": {"name": "Ada Lovelace"},
                "articles": [],
            }
        ).encode()
        return response

    ledger_path = tmp_path / "atomic-known.db"
    with Ledger.open(ledger_path, corpus_repo_root=repo) as ledger:
        ledger.create_or_resume(spec, census)
        credentials = DiscoveryCredentials(s2_key="wire-only")
        ledger.bind_discovery_policy(policy, credentials)
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10, s2_adapter_version="2", freshness_epoch="2026-08"),
            LedgerTransport(ledger, send_once=send_once),
        ).run(spec, RefreshCredentials(serpapi_key="wire-only"), lambda: False)
        assert result.status.value == "continuation"
        with pytest.raises(ValueError, match=r"trusted (?:committed-corpus|authorities)"):
            ledger.assert_c3_discovery_ready()
        ledger.scan_and_commit_corpus(repo)
        with pytest.raises(ValueError, match="corpus seed binding"):
            ledger.assert_c3_discovery_ready()
        ledger.execute_registered_pass("bind_corpus_seed")
        ledger.assert_c3_discovery_ready()
        before_rounds = ledger._connection.execute("SELECT COUNT(*) FROM plan_rounds").fetchone()[0]
        wave_now = datetime.now(timezone.utc)
        before_counts = tuple(
            ledger._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in (
                "planner_passes",
                "planner_pass_expected_items",
                "requests",
                "request_consumers",
                "tasks",
                "plan_obligations",
                "plan_rounds",
            )
        )
        for fault in (
            "after_c4_pass_receipt",
            "after_c4_expected_items",
            "after_c4_requests",
            "after_c4_tasks",
            "after_c4_consumers",
            "after_c4_obligations",
            "after_c4_round",
        ):
            fault_path = tmp_path / f"atomic-{fault}.db"
            with sqlite3.connect(fault_path) as destination:
                ledger._connection.backup(destination)
            with Ledger.open(fault_path, corpus_repo_root=repo) as faulted:
                faulted.set_fault(fault)
                with pytest.raises(FaultInjectedError, match=fault):
                    faulted.execute_and_commit_discovery_wave("known_doi", policy, now=wave_now)
                after_counts = tuple(
                    faulted._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    for table in (
                        "planner_passes",
                        "planner_pass_expected_items",
                        "requests",
                        "request_consumers",
                        "tasks",
                        "plan_obligations",
                        "plan_rounds",
                    )
                )
                assert after_counts == before_counts
            with Ledger.open(fault_path, corpus_repo_root=repo) as replayed:
                replayed.execute_and_commit_discovery_wave("known_doi", policy, now=wave_now)
                replayed.manifest()
        receipt = ledger.execute_and_commit_discovery_wave("known_doi", policy, now=wave_now)
        assert ledger.execute_and_commit_discovery_wave("known_doi", policy, now=NOW) == receipt
        assert ledger.load_discovery_authority() == resolve_discovery_authority(policy, credentials)
        assert ledger.discovery_phase_status("known_doi", now=wave_now) == "pending"
        assert len(ledger.discovery_wave_task_keys("known_doi", now=wave_now)) == 1
        assert (
            ledger._connection.execute("SELECT COUNT(*) FROM planner_passes WHERE pass_id='known_doi'").fetchone()[0]
            == 1
        )
        assert ledger._connection.execute("SELECT COUNT(*) FROM plan_rounds").fetchone()[0] == before_rounds + 1
        assert ledger._connection.execute("SELECT COUNT(*) FROM tasks WHERE provider='doi_csl'").fetchone()[0] == 1
        ledger.manifest()
        eligible = ledger.discovery_wave_task_keys("known_doi", now=wave_now)
        claim = ledger.claim_due_for_operations("worker", wave_now, timedelta(minutes=1), eligible)
        assert claim is not None
        request_claim = ledger.claim_request(claim.key, "worker", wave_now, timedelta(minutes=1))
        assert request_claim is not None
        ledger.finish_request(
            request_claim.key,
            "worker",
            TaskDisposition.SUCCEEDED,
            wave_now,
            observation=ProviderObservation(
                "doi_csl",
                "doi-csl-v1",
                {"metadata": {"DOI": "10.1000/x", "title": "A title"}},
            ),
        )
        ledger.finish_task(claim.key, "worker", TaskDisposition.SUCCEEDED, wave_now)
        assert ledger.discovery_phase_status("known_doi", now=wave_now) == "pending"
        expansion_fault_path = tmp_path / "atomic-after_c4_expansion.db"
        with sqlite3.connect(expansion_fault_path) as destination:
            ledger._connection.backup(destination)
        expansion_tables = (
            "tasks",
            "requests",
            "request_consumers",
            "plan_obligations",
            "plan_rounds",
            "reduction_receipts",
            "reduction_sources",
        )
        expansion_before = tuple(
            ledger._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in expansion_tables
        )
        with Ledger.open(expansion_fault_path, corpus_repo_root=repo) as faulted:
            faulted.set_fault("after_c4_expansion")
            with pytest.raises(FaultInjectedError, match="after_c4_expansion"):
                faulted.execute_and_commit_discovery_wave("known_doi", policy, now=wave_now + timedelta(seconds=1))
            assert (
                tuple(
                    faulted._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    for table in expansion_tables
                )
                == expansion_before
            )
        with Ledger.open(expansion_fault_path, corpus_repo_root=repo) as replayed:
            replayed.execute_and_commit_discovery_wave("known_doi", policy, now=wave_now + timedelta(seconds=1))
            replayed.manifest()
        ledger.execute_and_commit_discovery_wave("known_doi", policy, now=wave_now + timedelta(seconds=1))
        assert (
            ledger._connection.execute("SELECT COUNT(*) FROM plan_rounds WHERE planner_id='doi_bibtex'").fetchone()[0]
            == 1
        )
        assert ledger._connection.execute("SELECT COUNT(*) FROM tasks WHERE provider='doi_bibtex'").fetchone()[0] == 1
        expansion_now = wave_now + timedelta(seconds=1)
        assert ledger.discovery_phase_status("known_doi", now=expansion_now) == "complete"
        assert not ledger.discovery_wave_task_keys("known_doi", now=expansion_now)
        broad_receipt = ledger.execute_and_commit_discovery_wave(
            "broad_discovery", policy, now=wave_now + timedelta(seconds=2)
        )
        assert broad_receipt.pass_id == "broad_discovery"
        assert ledger.discovery_phase_status("broad_discovery", now=wave_now + timedelta(seconds=2)) == "pending"
        assert len(ledger.discovery_wave_task_keys("broad_discovery", now=wave_now + timedelta(seconds=2))) == 7
        broad_now = wave_now + timedelta(seconds=2)
        while eligible := ledger.discovery_wave_task_keys("broad_discovery", now=broad_now):
            claim = ledger.claim_due_for_operations("worker", broad_now, timedelta(minutes=1), eligible)
            assert claim is not None
            task = ledger.reconstruct_claimed_task(claim, broad_now)
            request_claim = ledger.claim_request(claim.key, "worker", broad_now, timedelta(minutes=1))
            assert request_claim is not None and task.request is not None
            response: dict[str, object] = {task.request.requested_fields[0]: ({"title": "candidate"},)}
            if task.provider == "pubmed":
                response = {"pmids": ("123",)}
            elif task.provider == "openreview":
                response = {"notes": ({"id": "note"},)}
            schema = {
                "arxiv": "arxiv-atom-v1",
                "crossref": "crossref-search-v1",
                "europepmc": "europepmc-search-v1",
                "openalex": "openalex-search-v1",
                "openreview": "openreview-notes-v1",
                "pubmed": "pubmed-esearch-v1",
                "s2": "s2-search-v2",
            }[task.provider]
            ledger.finish_request(
                request_claim.key,
                "worker",
                TaskDisposition.SUCCEEDED,
                broad_now,
                observation=ProviderObservation(task.provider, schema, response),
            )
            ledger.finish_task(claim.key, "worker", TaskDisposition.SUCCEEDED, broad_now)
        dynamic = ledger.execute_and_commit_discovery_wave(
            "dynamic_expansion", policy, now=wave_now + timedelta(seconds=3)
        )
        assert dynamic.pass_id == "dynamic_expansion"
        assert (
            ledger._connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE provider='pubmed' AND operation='summary'"
            ).fetchone()[0]
            == 1
        )
    with Ledger.open(ledger_path, corpus_repo_root=repo) as reopened:
        assert reopened.execute_and_commit_discovery_wave("known_doi", policy, now=NOW) == receipt
        assert reopened.execute_and_commit_discovery_wave("broad_discovery", policy, now=NOW) == broad_receipt
        assert reopened.execute_and_commit_discovery_wave("dynamic_expansion", policy, now=NOW) == dynamic
        reopened.manifest()


def test_zero_seed_generation_commits_and_replays_complete_c4_chain(tmp_path: Path) -> None:
    repo, commit, census = _real_corpus_authority(tmp_path, empty=True)
    policy = replace(_policy(), provider_modes={"gemini": "disabled", "s2": "disabled", "serply": "disabled"})
    spec = GenerationSpec(census, "policy-v1", {**dict(policy.adapter_versions), "scholar": "1"}, commit)

    def send_empty(_operation: SendOperation) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
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
                "articles": [],
            }
        ).encode()
        return response

    path = tmp_path / "zero-c4.db"
    with Ledger.open(path, corpus_repo_root=repo) as ledger:
        ledger.create_or_resume(spec, census)
        ledger.bind_discovery_policy(policy, DiscoveryCredentials())
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10, s2_adapter_version="2", freshness_epoch="2026-08"),
            LedgerTransport(ledger, send_once=send_empty),
        ).run(spec, RefreshCredentials(serpapi_key="wire-only"), lambda: False)
        assert result.status.value == "continuation"
        ledger.scan_and_commit_corpus(repo)
        ledger.execute_registered_pass("bind_corpus_seed")
        now = datetime.now(timezone.utc)
        known = ledger.execute_and_commit_discovery_wave("known_doi", policy, now=now)
        ledger.execute_and_commit_discovery_wave("known_doi", policy, now=now + timedelta(seconds=1))
        broad = ledger.execute_and_commit_discovery_wave("broad_discovery", policy, now=now + timedelta(seconds=2))
        dynamic = ledger.execute_and_commit_discovery_wave("dynamic_expansion", policy, now=now + timedelta(seconds=3))
        historical_path = tmp_path / "populated-c4-registry.db"
        with sqlite3.connect(historical_path) as historical_connection:
            ledger._connection.backup(historical_connection)
        historical_connection = sqlite3.connect(historical_path)
        update_trigger = historical_connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'planner_passes_append_only_update'"
        ).fetchone()[0]
        historical_connection.execute("DROP TRIGGER planner_passes_append_only_update")
        for pass_id in ("bind_corpus_seed", "known_doi", "broad_discovery", "dynamic_expansion"):
            content = json.loads(
                historical_connection.execute(
                    "SELECT receipt_json FROM planner_passes WHERE pass_id = ?", (pass_id,)
                ).fetchone()[0]
            )
            content["registry_digest"] = "4aca44ec61c5f081b1fa372705434adb4413b6e02b5f981166829cd5d41d5696"
            historical_connection.execute(
                "UPDATE planner_passes SET registry_digest = ?, receipt_json = ? WHERE pass_id = ?",
                (
                    content["registry_digest"],
                    json.dumps(content, sort_keys=True, separators=(",", ":")),
                    pass_id,
                ),
            )
        historical_connection.execute(str(update_trigger))
        historical_connection.commit()
        historical_connection.close()
        with Ledger.open(historical_path, corpus_repo_root=repo) as historical:
            historical.manifest()
            assert historical.execute_and_commit_discovery_wave("known_doi", policy, now=NOW) == replace(
                known,
                registry_digest="4aca44ec61c5f081b1fa372705434adb4413b6e02b5f981166829cd5d41d5696",
            )
            assert historical.execute_and_commit_discovery_wave("broad_discovery", policy, now=NOW) == replace(
                broad,
                registry_digest="4aca44ec61c5f081b1fa372705434adb4413b6e02b5f981166829cd5d41d5696",
            )
            assert historical.execute_and_commit_discovery_wave("dynamic_expansion", policy, now=NOW) == replace(
                dynamic,
                registry_digest="4aca44ec61c5f081b1fa372705434adb4413b6e02b5f981166829cd5d41d5696",
            )
        venue = ledger.execute_and_commit_venue_fallback(policy, now=now + timedelta(seconds=4))
        assert ledger.discovery_phase_status("venue_fallback", now=now) == "pending"
        ledger.execute_and_commit_venue_fallback(policy, now=now + timedelta(seconds=5))
        late = ledger.execute_and_commit_late_identifiers(policy, now=now + timedelta(seconds=6))
        historical_c5_path = tmp_path / "populated-c5-registry.db"
        with sqlite3.connect(historical_c5_path) as historical_c5_connection:
            ledger._connection.backup(historical_c5_connection)
        historical_c5_connection = sqlite3.connect(historical_c5_path)
        update_trigger = historical_c5_connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'planner_passes_append_only_update'"
        ).fetchone()[0]
        historical_c5_connection.execute("DROP TRIGGER planner_passes_append_only_update")
        c5_digest = "ac1a11deb6ea9c519b58638ac870a54d401b0ba50682fb6c82d744670a56bc7f"
        for pass_id in (
            "bind_corpus_seed",
            "known_doi",
            "broad_discovery",
            "dynamic_expansion",
            "venue_fallback",
            "late_identifiers",
        ):
            content = json.loads(
                historical_c5_connection.execute(
                    "SELECT receipt_json FROM planner_passes WHERE pass_id = ?", (pass_id,)
                ).fetchone()[0]
            )
            content["registry_digest"] = c5_digest
            historical_c5_connection.execute(
                "UPDATE planner_passes SET registry_digest = ?, receipt_json = ? WHERE pass_id = ?",
                (c5_digest, json.dumps(content, sort_keys=True, separators=(",", ":")), pass_id),
            )
        historical_c5_connection.execute(str(update_trigger))
        historical_c5_connection.commit()
        historical_c5_connection.close()
        with Ledger.open(historical_c5_path, corpus_repo_root=repo) as historical_c5:
            historical_c5.manifest()
            assert historical_c5.execute_and_commit_late_identifiers(policy, now=NOW) == replace(
                late, registry_digest=c5_digest
            )
        html = ledger.execute_and_commit_html_probe(policy, now=now + timedelta(seconds=7))
        assert ledger.discovery_phase_status("known_doi", now=now) == "complete"
        assert ledger.discovery_phase_status("broad_discovery", now=now) == "complete"
        assert ledger.discovery_phase_status("dynamic_expansion", now=now) == "complete"
        assert ledger.discovery_phase_status("venue_fallback", now=now) == "complete"
        assert ledger.discovery_phase_status("html_probe", now=now) == "complete"
        ledger.manifest()
    with Ledger.open(path, corpus_repo_root=repo) as reopened:
        assert reopened.execute_and_commit_discovery_wave("known_doi", policy, now=NOW) == known
        assert reopened.execute_and_commit_discovery_wave("broad_discovery", policy, now=NOW) == broad
        assert reopened.execute_and_commit_discovery_wave("dynamic_expansion", policy, now=NOW) == dynamic
        assert reopened.execute_and_commit_venue_fallback(policy, now=NOW) == venue
        assert reopened.execute_and_commit_late_identifiers(policy, now=NOW) == late
        assert reopened.execute_and_commit_html_probe(policy, now=NOW) == html
        reopened.manifest()


def test_atomic_venue_fallback_consumes_crossref_and_expands_openalex(tmp_path: Path) -> None:
    repo, commit, census = _real_corpus_authority(tmp_path, venue_without_doi=True)
    policy = _policy()
    spec = GenerationSpec(census, "policy-v1", {**dict(policy.adapter_versions), "scholar": "1"}, commit)

    def send_empty(_operation: SendOperation) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
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
                "articles": [],
            }
        ).encode()
        return response

    path = tmp_path / "venue.db"
    with Ledger.open(path, corpus_repo_root=repo) as ledger:
        ledger.create_or_resume(spec, census)
        ledger.bind_discovery_policy(policy, DiscoveryCredentials(s2_key="wire-only"))
        result = RefreshEngine(
            ledger,
            InventoryPolicy(2020, 1000, 10, s2_adapter_version="2", freshness_epoch="2026-08"),
            LedgerTransport(ledger, send_once=send_empty),
        ).run(spec, RefreshCredentials(serpapi_key="wire-only"), lambda: False)
        assert result.status.value == "continuation"
        ledger.scan_and_commit_corpus(repo)
        ledger.execute_registered_pass("bind_corpus_seed")
        now = datetime.now(timezone.utc)
        ledger.execute_and_commit_discovery_wave("known_doi", policy, now=now)
        ledger.execute_and_commit_discovery_wave("known_doi", policy, now=now + timedelta(seconds=1))
        ledger.execute_and_commit_discovery_wave("broad_discovery", policy, now=now + timedelta(seconds=2))
        broad_now = now + timedelta(seconds=2)
        empty_response = {
            "arxiv": ("arxiv-atom-v1", {"entries": ()}),
            "crossref": ("crossref-search-v1", {"results": ()}),
            "europepmc": ("europepmc-search-v1", {"results": ()}),
            "openalex": ("openalex-search-v1", {"results": ()}),
            "openreview": ("openreview-notes-v1", {"notes": ()}),
            "pubmed": ("pubmed-esearch-v1", {"pmids": ()}),
            "s2": ("s2-search-v2", {"results": ()}),
        }
        while eligible := ledger.discovery_wave_task_keys("broad_discovery", now=broad_now):
            claim = ledger.claim_due_for_operations("worker", broad_now, timedelta(minutes=1), eligible)
            assert claim is not None
            task = ledger.reconstruct_claimed_task(claim, broad_now)
            request_claim = ledger.claim_request(claim.key, "worker", broad_now, timedelta(minutes=1))
            assert request_claim is not None
            schema, _response = empty_response[task.provider]
            ledger.finish_request(
                request_claim.key,
                "worker",
                TaskDisposition.CONFIRMED_EMPTY,
                broad_now,
                observation=ProviderObservation(task.provider, schema, {}, authoritative_empty=True),
            )
            ledger.finish_task(claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, broad_now)
        ledger.execute_and_commit_discovery_wave("dynamic_expansion", policy, now=now + timedelta(seconds=3))
        dynamic_now = now + timedelta(seconds=3)
        while eligible := ledger.discovery_wave_task_keys("dynamic_expansion", now=dynamic_now):
            claim = ledger.claim_due_for_operations("worker", dynamic_now, timedelta(minutes=1), eligible)
            assert claim is not None
            request_claim = ledger.claim_request(claim.key, "worker", dynamic_now, timedelta(minutes=1))
            assert request_claim is not None
            ledger.finish_request(
                request_claim.key,
                "worker",
                TaskDisposition.CONFIRMED_EMPTY,
                dynamic_now,
                observation=ProviderObservation("openreview", "openreview-notes-v1", {}, authoritative_empty=True),
            )
            ledger.finish_task(claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, dynamic_now)

        before = tuple(
            ledger._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("planner_passes", "planner_pass_expected_items", "tasks", "plan_rounds")
        )
        fault_path = tmp_path / "venue-fault.db"
        with sqlite3.connect(fault_path) as destination:
            ledger._connection.backup(destination)
        with Ledger.open(fault_path, corpus_repo_root=repo) as faulted:
            faulted.set_fault("after_c4_round")
            with pytest.raises(FaultInjectedError, match="after_c4_round"):
                faulted.execute_and_commit_venue_fallback(policy, now=now + timedelta(seconds=4))
            assert (
                tuple(
                    faulted._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    for table in ("planner_passes", "planner_pass_expected_items", "tasks", "plan_rounds")
                )
                == before
            )

        receipt = ledger.execute_and_commit_venue_fallback(policy, now=now + timedelta(seconds=4))
        crossref_keys = ledger.discovery_wave_task_keys("venue_fallback", now=now + timedelta(seconds=4))
        assert len(crossref_keys) == 1
        claim = ledger.claim_due_for_operations(
            "worker", now + timedelta(seconds=4), timedelta(minutes=1), crossref_keys
        )
        assert claim is not None
        request_claim = ledger.claim_request(claim.key, "worker", now + timedelta(seconds=4), timedelta(minutes=1))
        assert request_claim is not None
        ledger.finish_request(
            request_claim.key,
            "worker",
            TaskDisposition.CONFIRMED_EMPTY,
            now + timedelta(seconds=4),
            observation=ProviderObservation("crossref", "crossref-venue-v1", {}, authoritative_empty=True),
        )
        ledger.finish_task(claim.key, "worker", TaskDisposition.CONFIRMED_EMPTY, now + timedelta(seconds=4))
        expansion_tables = ("tasks", "requests", "request_consumers", "plan_obligations", "plan_rounds")
        expansion_before = tuple(
            ledger._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in expansion_tables
        )
        expansion_fault_path = tmp_path / "venue-expansion-fault.db"
        with sqlite3.connect(expansion_fault_path) as destination:
            ledger._connection.backup(destination)
        with Ledger.open(expansion_fault_path, corpus_repo_root=repo) as faulted:
            faulted.set_fault("after_c4_expansion")
            with pytest.raises(FaultInjectedError, match="after_c4_expansion"):
                faulted.execute_and_commit_venue_fallback(policy, now=now + timedelta(seconds=5))
            assert (
                tuple(
                    faulted._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    for table in expansion_tables
                )
                == expansion_before
            )
        ledger.execute_and_commit_venue_fallback(policy, now=now + timedelta(seconds=5))
        assert len(ledger.discovery_wave_task_keys("venue_fallback", now=now + timedelta(seconds=5))) == 1
        assert (
            ledger._connection.execute(
                "SELECT COUNT(*) FROM reduction_sources WHERE source_task_key = ?", (claim.key,)
            ).fetchone()[0]
            == 1
        )
        openalex_keys = ledger.discovery_wave_task_keys("venue_fallback", now=now + timedelta(seconds=5))
        openalex_claim = ledger.claim_due_for_operations(
            "worker", now + timedelta(seconds=5), timedelta(minutes=1), openalex_keys
        )
        assert openalex_claim is not None
        openalex_request = ledger.claim_request(
            openalex_claim.key, "worker", now + timedelta(seconds=5), timedelta(minutes=1)
        )
        assert openalex_request is not None
        ledger.finish_request(
            openalex_request.key,
            "worker",
            TaskDisposition.SUCCEEDED,
            now + timedelta(seconds=5),
            observation=ProviderObservation(
                "openalex",
                "openalex-venue-v1",
                {
                    "results": (
                        {
                            "authorships": ({"author": {"display_name": "Ada Lovelace"}},),
                            "id": "https://openalex.org/W123",
                            "publication_year": 2026,
                            "title": "A title",
                        },
                    )
                },
            ),
        )
        ledger.finish_task(
            openalex_claim.key,
            "worker",
            TaskDisposition.SUCCEEDED,
            now + timedelta(seconds=5),
        )
        late_before = tuple(
            ledger._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("planner_passes", "planner_pass_expected_items", "plan_rounds")
        )
        late_fault_path = tmp_path / "late-fault.db"
        with sqlite3.connect(late_fault_path) as destination:
            ledger._connection.backup(destination)
        with Ledger.open(late_fault_path, corpus_repo_root=repo) as faulted:
            faulted.set_fault("after_c4_expected_items")
            with pytest.raises(FaultInjectedError, match="after_c4_expected_items"):
                faulted.execute_and_commit_late_identifiers(policy, now=now + timedelta(seconds=6))
            assert (
                tuple(
                    faulted._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
                    for table in ("planner_passes", "planner_pass_expected_items", "plan_rounds")
                )
                == late_before
            )
        late = ledger.execute_and_commit_late_identifiers(policy, now=now + timedelta(seconds=6))
        late_row = ledger._connection.execute(
            "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? "
            "AND pass_key = ? AND item_key LIKE 'late-source:%' LIMIT 1",
            (ledger._generation_id(), late.pass_key),
        ).fetchone()
        assert late_row is not None
        omitted = json.loads(str(late_row[0]))
        items = [
            json.loads(str(row[0]))
            for row in ledger._connection.execute(
                "SELECT input_json FROM planner_pass_expected_items WHERE generation_id = ? AND pass_key = ?",
                (ledger._generation_id(), late.pass_key),
            )
            if json.loads(str(row[0]))["key"] != omitted["key"]
        ]
        with pytest.raises(ValueError, match="source membership is incomplete"):
            ledger._verify_late_identifier_snapshot(
                ledger._connection,
                ledger._generation_id(),
                {
                    "generation_id": ledger._generation_id(),
                    "pass_id": "late_identifiers",
                    "pass_version": "2",
                    "items": items,
                },
            )
        html = ledger.execute_and_commit_html_probe(policy, now=now + timedelta(seconds=7))
        assert ledger.discovery_phase_status("html_probe", now=now + timedelta(seconds=7)) == "pending"
        ledger.execute_and_commit_html_probe(policy, now=now + timedelta(seconds=8))
        due = ledger.discovery_wave_task_keys("html_probe", now=now + timedelta(seconds=8))
        assert len(due) == 1
        html_claim = ledger.claim_due_for_operations("worker", now + timedelta(seconds=8), timedelta(minutes=1), due)
        assert html_claim is not None
        assert (
            ledger.resolve_claimed_web_probe_url(html_claim, now=now + timedelta(seconds=8))
            == "https://openalex.org/W123"
        )
        html_request = ledger.claim_request(html_claim.key, "worker", now + timedelta(seconds=8), timedelta(minutes=1))
        assert html_request is not None
        ledger.finish_request(
            html_request.key,
            "worker",
            TaskDisposition.SUCCEEDED,
            now + timedelta(seconds=8),
            observation=ProviderObservation("web", "html-doi-v1", {"doi": None}),
        )
        ledger.finish_task(html_claim.key, "worker", TaskDisposition.SUCCEEDED, now + timedelta(seconds=8))
        ledger.execute_and_commit_html_probe(policy, now=now + timedelta(seconds=9))
        assert ledger.discovery_phase_status("html_probe", now=now + timedelta(seconds=9)) == "complete"
        assert ledger._connection.execute("SELECT COUNT(*) FROM html_probe_waves").fetchone()[0] == 2
        assert ledger._connection.execute("SELECT COUNT(*) FROM html_probe_terminal_receipts").fetchone()[0] == 1
        ledger.manifest()
        closure_path = tmp_path / "html-closure.db"
        with sqlite3.connect(closure_path) as destination:
            ledger._connection.backup(destination)
        with Ledger.open(closure_path, corpus_repo_root=repo) as closable:
            closure_digest = evidence_digest(dict(closable.closure_content()))
            closable.close_plan(expected_closure_digest=closure_digest, now=now + timedelta(seconds=10))
            closable.manifest()
    with Ledger.open(path, corpus_repo_root=repo) as reopened:
        assert reopened.execute_and_commit_venue_fallback(policy, now=NOW) == receipt
        assert reopened.execute_and_commit_late_identifiers(policy, now=NOW) == late
        assert reopened.execute_and_commit_html_probe(policy, now=NOW) == html
        reopened.manifest()


def test_generic_registered_route_rejects_c4_passes_before_policy_bind(tmp_path: Path) -> None:
    census = AuthorCensus(
        (
            AuthorCensusRow(
                2,
                "author-ada",
                "Ada Lovelace",
                "ada lovelace",
                "",
                "",
                False,
                "excluded",
                TaskDisposition.NOT_APPLICABLE,
            ),
        )
    )
    spec = GenerationSpec(census, "policy-v1", {}, "abc123")
    with Ledger.open(tmp_path / "route.db") as ledger:
        ledger.create_or_resume(spec, census)
        for pass_id in ("known_doi", "broad_discovery", "dynamic_expansion"):
            with pytest.raises(ValueError, match="atomic discovery wave API"):
                ledger.execute_registered_pass(pass_id)
        assert ledger._connection.execute("SELECT COUNT(*) FROM planner_passes").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault",
    (
        "after_c4_pass_receipt",
        "after_c4_expected_items",
        "after_c4_requests",
        "after_c4_consumers",
        "after_c4_tasks",
        "after_c4_obligations",
        "after_c4_round",
        "after_c4_expansion",
    ),
)
def test_every_c4_atomic_boundary_is_an_admitted_fault_point(tmp_path: Path, fault: str) -> None:
    census = AuthorCensus(
        (
            AuthorCensusRow(
                2,
                "author-ada",
                "Ada Lovelace",
                "ada lovelace",
                "",
                "",
                False,
                "excluded",
                TaskDisposition.NOT_APPLICABLE,
            ),
        )
    )
    with Ledger.open(tmp_path / f"{fault}.db") as ledger:
        ledger.create_or_resume(GenerationSpec(census, "policy-v1", {}, "abc123"), census)
        ledger.set_fault(fault)
        assert ledger._fault == fault
