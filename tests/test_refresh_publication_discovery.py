from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from citeforge.api_configs import (
    ARXIV_FIELD_MAPPING,
    CROSSREF_FIELD_MAPPING,
    OPENALEX_FIELD_MAPPING,
    OPENREVIEW_FIELD_MAPPING,
    S2_FIELD_MAPPING,
)
from citeforge.api_generics import build_bibtex_from_response, project_entry_from_response
from citeforge.bibtex_utils import bibtex_from_dict
from citeforge.refresh.authority import EvidenceKind, IntentKind, PublicationSeedEvidence, evidence_digest
from citeforge.refresh.capabilities import build_request, capability_for
from citeforge.refresh.discovery import (
    ApplicabilityReason,
    DiscoveryCredentials,
    DiscoveryDecision,
    DiscoveryObservation,
    DiscoveryPolicy,
    DiscoveryWave,
    DoiReduction,
    plan_broad_discovery,
    resolve_discovery_authority,
)
from citeforge.refresh.ledger import RequestSpec, TaskSpec
from citeforge.refresh.publication_discovery import (
    CitationKeyFragmentEvidence,
    CorpusOutputEvidence,
    LateDoiEvidence,
    LateIdentifierCandidate,
    LateIdentifierEvidence,
    MergedPublicationEvidence,
    MergeSourceEvidence,
    NamingEvidence,
    SurvivorDecision,
    SurvivorDisposition,
    SurvivorReduction,
    _accepted_venue_candidates,
    _deterministic_citation_fragment,
    _html_probe_candidates_by_member,
    _project_unmapped_record,
    derive_late_doi_evidence,
    derive_late_identifier_evidence,
    derive_materialization_intents,
    derive_provenance_evidence,
    derive_survivor_reduction,
    merge_publication_evidence,
    plan_crossref_venue_fallback,
    plan_gemini_naming,
    plan_html_probe_wave,
    plan_late_doi_bibtex,
    plan_late_doi_csl,
    plan_openalex_venue_fallback,
    project_merge_sources,
    reduce_gemini_naming,
    reduce_late_doi_observations,
    resolve_html_probe_url,
)
from citeforge.refresh.types import TaskDisposition


@pytest.mark.parametrize(
    ("mapping", "record"),
    (
        (
            S2_FIELD_MAPPING,
            {
                "title": "Analytical Engine",
                "authors": [{"name": "Ada Lovelace"}],
                "year": 2024,
                "venue": "Transactions",
                "externalIds": {"DOI": "10.1234/engine", "ArXiv": "2401.00001"},
                "url": "https://semanticscholar.org/paper/x",
                "publicationTypes": ["JournalArticle"],
            },
        ),
        (
            CROSSREF_FIELD_MAPPING,
            {
                "title": ["Analytical Engine"],
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "issued": {"date-parts": [[2024]]},
                "container-title": ["Transactions"],
                "DOI": "10.1234/engine",
                "URL": "https://doi.org/10.1234/engine",
                "type": "journal-article",
                "volume": "12",
                "issue": "3",
                "page": "44-51",
                "publisher": "Engine Press",
            },
        ),
        (
            OPENALEX_FIELD_MAPPING,
            {
                "title": "Analytical Engine",
                "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
                "publication_year": 2024,
                "primary_location": {"source": {"display_name": "Transactions"}},
                "doi": "https://doi.org/10.1234/engine",
                "id": "https://openalex.org/W123",
                "type": "article",
            },
        ),
        (
            ARXIV_FIELD_MAPPING,
            {
                "title": "Analytical Engine",
                "authors": ["Ada Lovelace"],
                "year": 2024,
                "arxiv_id": "2401.00001",
                "abs_url": "https://arxiv.org/abs/2401.00001",
                "primary_class": "cs.AI",
            },
        ),
        (
            OPENREVIEW_FIELD_MAPPING,
            {
                "content": {
                    "title": "Analytical Engine",
                    "authors": ["Ada Lovelace"],
                    "venue": "ICLR",
                    "doi": "10.1234/engine",
                    "pdf": "https://openreview.net/pdf?id=x",
                },
                "cdate": 1710000000000,
            },
        ),
    ),
)
def test_pure_mapped_projection_serializes_exactly_like_legacy_builder(
    mapping: object, record: dict[str, object]
) -> None:
    projected = project_entry_from_response(record, "pub-one", mapping)  # type: ignore[arg-type]
    assert projected is not None
    assert bibtex_from_dict(projected) == build_bibtex_from_response(record, "pub-one", mapping)  # type: ignore[arg-type]


def _policy() -> DiscoveryPolicy:
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
        max_scholar_pages=10,
        max_html_probe_waves=8,
    )


def _seed(publication: str = "Journal of Engines 12(3), 44-51, 2024") -> PublicationSeedEvidence:
    entry = {
        "type": "article",
        "key": "pub-one",
        "fields": {
            "author": "Lovelace, Ada",
            "title": "Analytical Engine",
            "year": "2024",
            "journal": publication,
        },
    }
    seed = PublicationSeedEvidence(
        "generation",
        "author-ada",
        "pub-one",
        EvidenceKind.PUBLICATION,
        f"inventory:author-ada:1:{'a' * 64}",
        "b" * 64,
        evidence_digest(entry),
        {},
        "0" * 64,
        entry,
    )
    return replace(seed, seed_digest=seed.derived_seed_digest)


def _seed_member(publication_key: str, *, doi: str | None = None) -> PublicationSeedEvidence:
    baseline = {
        "type": "article",
        "key": publication_key,
        "fields": {
            "author": "Lovelace, Ada",
            "title": f"Analytical Engine {publication_key}",
            "year": "2024",
        },
    }
    seed = PublicationSeedEvidence(
        "generation",
        "author-ada",
        publication_key,
        EvidenceKind.PUBLICATION,
        f"inventory:author-ada:1:{publication_key}",
        evidence_digest(("origin", publication_key)),
        evidence_digest(baseline),
        {"doi": doi} if doi is not None else {},
        "0" * 64,
        baseline,
    )
    return replace(seed, seed_digest=seed.derived_seed_digest)


def _broad(seed: PublicationSeedEvidence) -> tuple[object, DiscoveryWave]:
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only"))
    broad = plan_broad_discovery(
        (seed,),
        {"author-ada": "Ada Lovelace"},
        authority,
        (DoiReduction("author-ada", "pub-one", "no_identifier", "0" * 64),),
    )
    return authority, broad


def _no_doi(seed: PublicationSeedEvidence) -> tuple[DoiReduction, ...]:
    return (DoiReduction(seed.author_key, seed.publication_key, "no_identifier", "0" * 64),)


def _empty_observations(wave: DiscoveryWave) -> tuple[DiscoveryObservation, ...]:
    schema = {
        "arxiv": "arxiv-atom-v1",
        "crossref": "crossref-search-v1",
        "europepmc": "europepmc-search-v1",
        "openalex": "openalex-search-v1",
        "openreview": "openreview-notes-v1",
        "pubmed": "pubmed-esearch-v1",
        "s2": "s2-search-v2",
    }
    return tuple(
        DiscoveryObservation(
            decision.task,
            TaskDisposition.CONFIRMED_EMPTY,
            {},
            authoritative_empty=True,
            schema_version=schema[decision.task.provider],
        )
        for decision in wave.decisions
        if decision.task.request is not None
    )


def test_crossref_venue_fallback_covers_every_seed_with_exact_conditional_decision() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    wave = plan_crossref_venue_fallback(
        (seed,), {"author-ada": "Ada Lovelace"}, broad, _empty_observations(broad), _no_doi(seed), authority
    )
    assert len(wave.decisions) == 1
    decision = wave.decisions[0]
    assert decision.reason is None
    assert decision.task.provider == "crossref"
    assert decision.task.operation == "venue_search"
    assert decision.task.request is not None
    assert dict(decision.task.request.normalized_payload) == {
        "author": "Ada Lovelace",
        "author_key": "author-ada",
        "query": "Analytical Engine",
        "rows": 10,
        "venue": "Journal of Engines",
    }

    succeeded = next(item for item in broad.decisions if item.task.provider == "crossref")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == succeeded.task.key)
    observations[index] = DiscoveryObservation(
        succeeded.task,
        TaskDisposition.SUCCEEDED,
        {"results": ({"title": "Analytical Engine", "author": "Lovelace, Ada"},)},
        schema_version="crossref-search-v1",
    )
    suppressed = plan_crossref_venue_fallback(
        (seed,), {"author-ada": "Ada Lovelace"}, broad, observations, _no_doi(seed), authority
    )
    assert suppressed.decisions[0].task.request is None
    assert suppressed.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED


def test_openalex_venue_fallback_requires_authoritative_crossref_empty() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    crossref = plan_crossref_venue_fallback(
        (seed,), {"author-ada": "Ada Lovelace"}, broad, _empty_observations(broad), _no_doi(seed), authority
    )
    decision = crossref.decisions[0]
    assert decision.task.request is not None
    empty = DiscoveryObservation(
        decision.task,
        TaskDisposition.CONFIRMED_EMPTY,
        {},
        authoritative_empty=True,
        schema_version="crossref-venue-v1",
    )
    wave = plan_openalex_venue_fallback((seed,), {"author-ada": "Ada Lovelace"}, crossref, (empty,), authority)
    assert wave.decisions[0].task.provider == "openalex"
    assert wave.decisions[0].task.operation == "venue_search"
    assert wave.decisions[0].task.request is not None
    assert dict(wave.decisions[0].task.request.normalized_payload) == {
        "author_key": "author-ada",
        "per_page": 10,
        "query": "Analytical Engine",
        "venue": "Journal of Engines",
    }

    succeeded = DiscoveryObservation(
        decision.task,
        TaskDisposition.SUCCEEDED,
        {"results": ({"title": "Analytical Engine", "author": "Lovelace, Ada"},)},
        schema_version="crossref-venue-v1",
    )
    suppressed = plan_openalex_venue_fallback(
        (seed,), {"author-ada": "Ada Lovelace"}, crossref, (succeeded,), authority
    )
    assert suppressed.decisions[0].task.request is None
    assert suppressed.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED


def test_venue_fallback_rejects_missing_or_extra_terminal_membership() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    observations = _empty_observations(broad)
    with pytest.raises(ValueError, match="terminal broad evidence membership"):
        plan_crossref_venue_fallback(
            (seed,), {"author-ada": "Ada Lovelace"}, broad, observations[:-1], _no_doi(seed), authority
        )
    with pytest.raises(ValueError, match="terminal broad evidence membership"):
        plan_crossref_venue_fallback(
            (seed,),
            {"author-ada": "Ada Lovelace"},
            broad,
            (*observations, observations[0]),
            _no_doi(seed),
            authority,
        )


def test_venue_fallback_consumes_doi_completeness_and_mapped_inventory_venue_fields() -> None:
    seed = _seed("ignored")
    entry = {**seed.baseline_entry, "fields": {**seed.baseline_entry["fields"], "journal": "", "school": "MIT"}}
    seed = replace(seed, baseline_entry=entry, baseline_digest=evidence_digest(entry))
    seed = replace(seed, seed_digest=seed.derived_seed_digest)
    authority, _unused = _broad(seed)
    complete = DoiReduction(
        seed.author_key,
        seed.publication_key,
        "identity_matched",
        "1" * 64,
        {
            "DOI": "10.1234/engine",
            "author": ({"family": "Lovelace"},),
            "container-title": "Transactions on Engines",
            "issued": {"date-parts": ((2024,),)},
            "title": "Analytical Engine",
        },
    )
    broad = plan_broad_discovery((seed,), {"author-ada": "Ada Lovelace"}, authority, (complete,))
    wave = plan_crossref_venue_fallback((seed,), {"author-ada": "Ada Lovelace"}, broad, (), (complete,), authority)
    assert wave.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED
    assert wave.decisions[0].task.request is None

    incomplete = DoiReduction(seed.author_key, seed.publication_key, "no_identifier", "0" * 64)
    broad = plan_broad_discovery((seed,), {"author-ada": "Ada Lovelace"}, authority, (incomplete,))
    wave = plan_crossref_venue_fallback(
        (seed,), {"author-ada": "Ada Lovelace"}, broad, _empty_observations(broad), (incomplete,), authority
    )
    assert wave.decisions[0].task.request is None
    assert wave.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED


def test_venue_admission_uses_legacy_weighted_score_and_stable_top_five() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    crossref = plan_crossref_venue_fallback(
        (seed,), {"author-ada": "Ada Lovelace"}, broad, _empty_observations(broad), _no_doi(seed), authority
    )
    task = crossref.decisions[0].task
    titles = (
        "Analytical Engines",
        "Analytical Engine",
        "Analytical Engine Study",
        "The Analytical Engine",
        "Analytical Engine Design",
        "Analytical Engine Analysis",
    )
    observation = DiscoveryObservation(
        task,
        TaskDisposition.SUCCEEDED,
        {
            "results": tuple(
                {
                    "title": [title],
                    "author": ({"given": "Ada", "family": "Lovelace"},),
                    "issued": {"date-parts": ((2024,),)},
                }
                for title in titles
            )
        },
        schema_version="crossref-venue-v1",
    )
    admitted = _accepted_venue_candidates(seed, observation, "Ada Lovelace")
    assert len(admitted) == 5
    assert admitted[0]["title"] == ("Analytical Engine",)


def test_crossref_nonempty_wrong_identity_still_triggers_openalex() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    crossref = plan_crossref_venue_fallback(
        (seed,), {"author-ada": "Ada Lovelace"}, broad, _empty_observations(broad), _no_doi(seed), authority
    )
    decision = crossref.decisions[0]
    wrong = DiscoveryObservation(
        decision.task,
        TaskDisposition.SUCCEEDED,
        {"results": ({"title": "Unrelated", "author": "Other, Person"},)},
        schema_version="crossref-venue-v1",
    )
    openalex = plan_openalex_venue_fallback((seed,), {"author-ada": "Ada Lovelace"}, crossref, (wrong,), authority)
    assert openalex.decisions[0].task.request is not None


def test_late_identifier_evidence_is_exact_normalized_and_never_changes_publication_key() -> None:
    seed = _seed()
    _authority, broad = _broad(seed)
    target = next(item for item in broad.decisions if item.task.provider == "s2")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
    observations[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {
            "results": (
                {"title": "Unrelated one"},
                {"title": "Unrelated two"},
                {"title": "Unrelated three"},
                {"title": "Unrelated four"},
                {
                    "paperId": "S2-paper",
                    "title": "Analytical Engine",
                    "authors": ({"name": "Ada Lovelace"},),
                    "externalIds": {
                        "ArXiv": "2401.00001v2",
                        "CorpusId": "12345",
                        "DOI": "https://doi.org/10.1234/ABC",
                        "PubMed": "987654",
                    },
                    "url": "https://www.semanticscholar.org/paper/S2-paper",
                },
                {
                    "paperId": "wrong",
                    "title": "Unrelated work",
                    "externalIds": {"DOI": "10.9999/wrong"},
                },
            )
        },
        schema_version="s2-search-v2",
    )
    evidence = derive_late_identifier_evidence((seed,), (broad,), observations)
    assert len(evidence) == 1
    item = evidence[0]
    assert (item.author_key, item.publication_key) == ("author-ada", "pub-one")
    accepted = {(candidate.kind, candidate.value) for candidate in item.candidates if candidate.identity_accepted}
    assert accepted == {
        ("arxiv", "2401.00001"),
        ("doi", "10.1234/abc"),
        ("pmid", "987654"),
        ("s2_corpus_id", "12345"),
        ("s2_paper_id", "S2-paper"),
        (
            "url_sha256",
            evidence_digest({"scheme": "https", "url": "https://www.semanticscholar.org/paper/S2-paper"}),
        ),
    }
    assert any(
        candidate.kind == "doi" and candidate.value == "10.9999/wrong" and not candidate.identity_accepted
        for candidate in item.candidates
    )


def test_late_identifier_evidence_rejects_missing_duplicate_or_cross_publication_sources() -> None:
    seed = _seed()
    _authority_value, broad = _broad(seed)
    observations = _empty_observations(broad)
    with pytest.raises(ValueError, match="late identifier observation membership"):
        derive_late_identifier_evidence((seed,), (broad,), observations[:-1])
    with pytest.raises(ValueError, match="late identifier observation membership"):
        derive_late_identifier_evidence((seed,), (broad,), (*observations, observations[0]))


def test_late_identifier_conflicts_are_distinct_and_observation_order_is_irrelevant() -> None:
    seed = _seed()
    _authority, broad = _broad(seed)
    observations = list(_empty_observations(broad))
    targets = [decision for decision in broad.decisions if decision.task.provider in {"crossref", "s2"}]
    dois = ("10.1234/one", "10.1234/two")
    for target, doi in zip(targets, dois, strict=True):
        index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
        observations[index] = DiscoveryObservation(
            target.task,
            TaskDisposition.SUCCEEDED,
            {"results": ({"title": "Analytical Engine", "author": "Lovelace, Ada", "DOI": doi},)},
            schema_version="crossref-search-v1" if target.task.provider == "crossref" else "s2-search-v2",
        )
    first = derive_late_identifier_evidence((seed,), (broad,), observations)
    second = derive_late_identifier_evidence((seed,), (broad,), tuple(reversed(observations)))
    assert first == second
    assert [(item.kind, item.value) for item in first[0].candidates if item.kind == "doi"] == [
        ("doi", "10.1234/one"),
        ("doi", "10.1234/two"),
    ]


def test_late_identifier_candidate_rejects_unknown_private_or_unbound_values() -> None:
    with pytest.raises(ValueError, match="candidate"):
        LateIdentifierCandidate("unknown", "value", "1" * 64, "2" * 64, 0, True)
    with pytest.raises(ValueError, match="candidate"):
        LateIdentifierCandidate("doi", "10.1234/x", "bad", "2" * 64, 0, True)
    with pytest.raises(ValueError, match="private contact"):
        LateIdentifierCandidate("s2_paper_id", "person@example.test", "1" * 64, "2" * 64, 0, True)


def test_html_probe_plans_exact_indexed_candidate_without_persisting_raw_url() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    target = next(item for item in broad.decisions if item.task.provider == "s2")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
    raw_url = "https://papers.example.test/publication/engine"
    observations[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {
            "results": (
                {"title": ("Unrelated one",)},
                {"title": ("Unrelated two",)},
                {"title": ("Unrelated three",)},
                {"title": ("Unrelated four",)},
                {
                    "paperId": "paper-one",
                    "title": "Analytical Engine",
                    "authors": ({"name": "Ada Lovelace"},),
                    "url": raw_url,
                },
            )
        },
        schema_version="s2-search-v2",
    )
    late = derive_late_identifier_evidence((seed,), (broad,), observations)
    wave = plan_html_probe_wave((seed,), late, (broad,), observations, 0, authority)
    assert len(wave.decisions) == 1
    decision = wave.decisions[0]
    assert decision.task.request is not None
    assert decision.task.provider == "web" and decision.task.operation == "doi_probe"
    payload = dict(decision.task.request.normalized_payload)
    assert payload == {
        "url_digest": hashlib.sha256(raw_url.encode()).hexdigest(),
        "scheme": "https",
    }
    assert raw_url not in repr(decision)
    resolved = resolve_html_probe_url(decision.task, (seed,), late, (broad,), observations, 0, authority)
    built = build_request("web.doi_probe.v1", {"url": resolved})
    assert built.identity_payload == decision.task.request.normalized_payload
    assert resolved == raw_url

    with pytest.raises(ValueError, match="HTML probe task identity"):
        resolve_html_probe_url(
            replace(decision.task, author_key="other"),
            (seed,),
            late,
            (broad,),
            observations,
            0,
            authority,
        )

    exhausted = plan_html_probe_wave((seed,), late, (broad,), observations, 1, authority)
    assert exhausted.decisions[0].task.request is None
    assert exhausted.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED


def test_html_probe_rejects_candidate_outside_exact_c1_wire_policy() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    target = next(item for item in broad.decisions if item.task.provider == "s2")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
    observations[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {
            "results": (
                {
                    "paperId": "paper-one",
                    "title": "Analytical Engine",
                    "authors": ({"name": "Ada Lovelace"},),
                    "url": "https://papers.example.test/publication/engine?view=full",
                },
            )
        },
        schema_version="s2-search-v2",
    )
    late = derive_late_identifier_evidence((seed,), (broad,), observations)
    wave = plan_html_probe_wave((seed,), late, (broad,), observations, 0, authority)
    assert wave.decisions[0].task.request is None
    assert wave.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED


def test_html_probe_deduplicates_one_wire_url_across_distinct_source_records() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    raw_url = "https://papers.example.test/publication/engine"
    observations = list(_empty_observations(broad))
    for provider in ("s2", "crossref"):
        target = next(item for item in broad.decisions if item.task.provider == provider)
        index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
        response = (
            {
                "results": (
                    {"title": "Unrelated one"},
                    {"title": "Unrelated two"},
                    {"title": "Unrelated three"},
                    {"title": "Unrelated four"},
                    {
                        "authors": ({"name": "Ada Lovelace"},),
                        "paperId": "paper-one",
                        "title": "Analytical Engine",
                        "url": raw_url,
                    },
                )
            }
            if provider == "s2"
            else {
                "results": (
                    {"title": ("Unrelated one",)},
                    {"title": ("Unrelated two",)},
                    {"title": ("Unrelated three",)},
                    {"title": ("Unrelated four",)},
                    {
                        "author": ({"given": "Ada", "family": "Lovelace"},),
                        "title": ("Analytical Engine",),
                        "URL": raw_url,
                    },
                )
            }
        )
        observations[index] = DiscoveryObservation(
            target.task,
            TaskDisposition.SUCCEEDED,
            response,
            schema_version="s2-search-v2" if provider == "s2" else "crossref-search-v1",
        )
    late = derive_late_identifier_evidence((seed,), (broad,), observations)
    first = plan_html_probe_wave((seed,), late, (broad,), observations, 0, authority)
    assert first.decisions[0].task.request is not None
    candidates = _html_probe_candidates_by_member((seed,), (broad,), observations, late)
    assert candidates[(seed.author_key, seed.publication_key)][0].locators == tuple(
        sorted(
            (observation.response_digest, observation.task.request.key, 4)
            for observation in observations
            if observation.task.provider in {"s2", "crossref"} and observation.task.request is not None
        )
    )
    second = plan_html_probe_wave((seed,), late, (broad,), observations, 1, authority)
    assert second.decisions[0].task.request is None


def test_html_probe_stops_after_first_terminal_html_doi() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    target = next(item for item in broad.decisions if item.task.provider == "s2")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
    observations[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {
            "results": (
                {
                    "authors": ({"name": "Ada Lovelace"},),
                    "paperId": "paper-one",
                    "title": "Analytical Engine",
                    "url": "https://papers.example.test/one",
                },
                {
                    "authors": ({"name": "Ada Lovelace"},),
                    "paperId": "paper-two",
                    "title": "Analytical Engine",
                    "url": "https://papers.example.test/two",
                },
            )
        },
        schema_version="s2-search-v2",
    )
    late = derive_late_identifier_evidence((seed,), (broad,), observations)
    first = plan_html_probe_wave((seed,), late, (broad,), observations, 0, authority)
    first_task = first.decisions[0].task
    assert first_task.request is not None
    html = DiscoveryObservation(
        first_task,
        TaskDisposition.SUCCEEDED,
        {"doi": "10.1234/engine"},
        schema_version="html-doi-v1",
    )
    all_waves = (broad, first)
    all_observations = (*observations, html)
    current = derive_late_identifier_evidence((seed,), all_waves, all_observations)
    second = plan_html_probe_wave((seed,), current, all_waves, all_observations, 1, authority)
    assert second.decisions[0].task.request is None
    assert second.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED


def test_html_probe_is_typed_na_when_late_doi_already_exists_or_budget_is_zero() -> None:
    seed = _seed()
    authority, broad = _broad(seed)
    target = next(item for item in broad.decisions if item.task.provider == "s2")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
    observations[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {
            "results": (
                {
                    "paperId": "paper-one",
                    "title": "Analytical Engine",
                    "authors": ({"name": "Ada Lovelace"},),
                    "externalIds": {"DOI": "10.1234/found"},
                    "url": "https://papers.example.test/publication/engine",
                },
            )
        },
        schema_version="s2-search-v2",
    )
    late = derive_late_identifier_evidence((seed,), (broad,), observations)
    wave = plan_html_probe_wave((seed,), late, (broad,), observations, 0, authority)
    assert wave.decisions[0].task.request is None
    assert wave.decisions[0].reason is ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED

    zero_policy = replace(_policy(), max_html_probe_waves=0)
    zero_authority = resolve_discovery_authority(zero_policy, DiscoveryCredentials(s2_key="wire-only"))
    with pytest.raises(ValueError, match="HTML probe wave bound"):
        plan_html_probe_wave((seed,), late, (broad,), observations, 0, zero_authority)


def _late_doi(
    seed: PublicationSeedEvidence,
    doi: str,
    *,
    source_digest: str,
    request_key: str | None = None,
) -> LateIdentifierEvidence:
    return LateIdentifierEvidence(
        seed.author_key,
        seed.publication_key,
        (LateIdentifierCandidate("doi", doi, source_digest, request_key, 0, True),),
    )


def _html_doi_wave(
    seeds: tuple[PublicationSeedEvidence, ...],
    selected: PublicationSeedEvidence,
    authority: object,
) -> tuple[DiscoveryWave, DiscoveryObservation]:
    capability = capability_for("web", "doi_probe", "1")
    request = RequestSpec(
        capability.logical_source,
        capability.operation,
        capability.method,
        {"scheme": "https", "url_digest": "9" * 64},
        capability.requested_fields,
        capability.adapter_version,
        _policy().freshness_epoch,
        capability.quota_scope,
    )
    decisions = []
    for seed in seeds:
        if seed == selected:
            task = TaskSpec(seed.author_key, seed.publication_key, "web", "doi_probe", request)
            decisions.append(DiscoveryDecision(task))
        else:
            task = TaskSpec(
                seed.author_key,
                seed.publication_key,
                "web",
                "doi_probe",
                None,
                applicability="not_applicable",
            )
            decisions.append(DiscoveryDecision(task, ApplicabilityReason.CONDITIONAL_NOT_TRIGGERED))
    policy_digest = authority.digest
    wave = DiscoveryWave(tuple(sorted(decisions, key=lambda item: item.task.key)), "8" * 64, policy_digest)
    selected_task = next(item.task for item in wave.decisions if item.task.request is not None)
    observation = DiscoveryObservation(
        selected_task,
        TaskDisposition.SUCCEEDED,
        {"doi": "https://doi.org/10.1234/HTML"},
        schema_version="html-doi-v1",
    )
    return wave, observation


def test_late_doi_union_plans_only_unseen_normalized_csl_and_known_typed_na() -> None:
    known = _seed_member("known", doi="10.1234/known")
    deterministic = _seed_member("deterministic")
    html = _seed_member("html")
    seeds = (known, deterministic, html)
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only"))
    late = (
        _late_doi(known, "https://doi.org/10.1234/KNOWN", source_digest="1" * 64),
        _late_doi(deterministic, "https://doi.org/10.1234/NEW", source_digest="2" * 64),
        LateIdentifierEvidence(html.author_key, html.publication_key, ()),
    )
    html_wave, html_observation = _html_doi_wave(seeds, html, authority)

    evidence = derive_late_doi_evidence(seeds, late, (html_wave,), (html_observation,))
    assert {(item.publication_key, item.doi) for item in evidence} == {
        ("known", "10.1234/known"),
        ("deterministic", "10.1234/new"),
        ("html", "10.1234/html"),
    }
    assert isinstance(evidence[0], LateDoiEvidence)

    wave = plan_late_doi_csl(seeds, evidence, authority)
    decisions = {item.task.publication_key: item for item in wave.decisions}
    assert decisions["known"].task.request is None
    assert decisions["known"].reason is ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE
    assert decisions["deterministic"].task.request is not None
    assert decisions["deterministic"].task.request.normalized_payload == {"doi": "10.1234/new"}
    assert decisions["html"].task.request is not None
    assert decisions["html"].task.request.normalized_payload == {"doi": "10.1234/html"}


def test_late_doi_html_no_match_is_satisfied_bound_absent_evidence() -> None:
    seed = _seed_member("html-empty")
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only"))
    wave, matched = _html_doi_wave((seed,), seed, authority)
    empty = DiscoveryObservation(
        matched.task,
        TaskDisposition.SUCCEEDED,
        {"doi": None},
        schema_version="html-doi-v1",
    )
    evidence = derive_late_doi_evidence(
        (seed,),
        (LateIdentifierEvidence(seed.author_key, seed.publication_key, ()),),
        (wave,),
        (empty,),
    )
    assert evidence[0].doi is None
    assert len(evidence[0].sources) == 1
    assert evidence[0].sources[0].source_kind == "html"
    assert evidence[0].sources[0].doi is None
    decision = plan_late_doi_csl((seed,), evidence, authority).decisions[0]
    assert decision.task.request is None
    assert decision.reason is ApplicabilityReason.NO_APPLICABLE_IDENTIFIER

    confirmed_empty = DiscoveryObservation(
        matched.task,
        TaskDisposition.CONFIRMED_EMPTY,
        {},
        authoritative_empty=True,
        schema_version="html-doi-v1",
    )
    with pytest.raises(ValueError, match="HTML observation authority"):
        derive_late_doi_evidence(
            (seed,),
            (LateIdentifierEvidence(seed.author_key, seed.publication_key, ()),),
            (wave,),
            (confirmed_empty,),
        )


def test_late_doi_shared_request_coalesces_without_changing_publication_keys() -> None:
    first = _seed_member("pub-first")
    second = _seed_member("pub-second")
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only"))
    evidence = derive_late_doi_evidence(
        (second, first),
        (
            _late_doi(first, "10.1234/shared", source_digest="3" * 64),
            _late_doi(second, "https://doi.org/10.1234/SHARED", source_digest="4" * 64),
        ),
        (),
        (),
    )
    wave = plan_late_doi_csl((second, first), tuple(reversed(evidence)), authority)
    assert wave == plan_late_doi_csl((first, second), evidence, authority)
    tasks = tuple(item.task for item in wave.decisions)
    assert {task.publication_key for task in tasks} == {"pub-first", "pub-second"}
    assert len({task.request.key for task in tasks if task.request is not None}) == 1
    assert len({task.key for task in tasks}) == 2


def test_late_doi_conditional_bibtex_reduction_is_permutation_invariant() -> None:
    matched = _seed_member("matched")
    fallback = _seed_member("fallback")
    seeds = (matched, fallback)
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only"))
    evidence = derive_late_doi_evidence(
        seeds,
        (
            _late_doi(matched, "10.1234/matched", source_digest="5" * 64),
            _late_doi(fallback, "10.1234/fallback", source_digest="6" * 64),
        ),
        (),
        (),
    )
    csl = plan_late_doi_csl(seeds, evidence, authority)
    tasks = {item.task.publication_key: item.task for item in csl.decisions}
    csl_observations = (
        DiscoveryObservation(
            tasks["matched"],
            TaskDisposition.SUCCEEDED,
            {
                "metadata": {
                    "DOI": "10.1234/matched",
                    "title": "Analytical Engine matched",
                    "author": ({"literal": "Lovelace, Ada"},),
                }
            },
            schema_version="doi-csl-v1",
        ),
        DiscoveryObservation(
            tasks["fallback"],
            TaskDisposition.CONFIRMED_EMPTY,
            {},
            authoritative_empty=True,
            schema_version="doi-csl-v1",
        ),
    )
    bibtex = plan_late_doi_bibtex(seeds, evidence, csl, csl_observations, authority)
    decisions = {item.task.publication_key: item for item in bibtex.decisions}
    assert decisions["matched"].task.request is None
    assert decisions["matched"].reason is ApplicabilityReason.REDUNDANT_AUTHORITATIVE_EVIDENCE
    assert decisions["fallback"].task.request is not None
    assert decisions["fallback"].task.request.normalized_payload == {"doi": "10.1234/fallback"}

    bib_observation = DiscoveryObservation(
        decisions["fallback"].task,
        TaskDisposition.SUCCEEDED,
        {
            "metadata": {
                "type": "article",
                "key": "fallback",
                "fields": {
                    "author": "Lovelace, Ada",
                    "doi": "10.1234/fallback",
                    "title": "Analytical Engine fallback",
                    "year": "2024",
                },
            }
        },
        schema_version="doi-bibtex-v1",
    )
    first = reduce_late_doi_observations(
        seeds,
        evidence,
        csl,
        csl_observations,
        bibtex,
        (bib_observation,),
        authority,
    )
    second = reduce_late_doi_observations(
        tuple(reversed(seeds)),
        tuple(reversed(evidence)),
        csl,
        tuple(reversed(csl_observations)),
        bibtex,
        (bib_observation,),
        authority,
    )
    assert first == second
    assert [(item.publication_key, item.status) for item in first] == [
        ("fallback", "identity_matched"),
        ("matched", "identity_matched"),
    ]


def _merged_member(publication_key: str, title: str) -> MergedPublicationEvidence:
    entry = {
        "type": "article",
        "key": publication_key,
        "fields": {
            "author": "Lovelace, Ada",
            "title": title,
            "year": "2024",
        },
    }
    return MergedPublicationEvidence(
        "author-ada",
        publication_key,
        entry,
        {field: evidence_digest((publication_key, field)) for field in entry["fields"]},
        (evidence_digest(("source", publication_key)),),
    )


def _emitted(*merged: MergedPublicationEvidence) -> SurvivorReduction:
    return SurvivorReduction(
        tuple(
            SurvivorDecision(
                item.author_key,
                item.publication_key,
                SurvivorDisposition.EMITTED,
                item.digest,
                None,
            )
            for item in merged
        )
    )


@pytest.mark.parametrize(
    ("mode", "configured", "applicable", "reason"),
    (
        ("required", True, True, None),
        ("if_configured", True, True, None),
        ("if_configured", False, False, ApplicabilityReason.PROVIDER_NOT_CONFIGURED),
        ("disabled", False, False, ApplicabilityReason.PROVIDER_DISABLED),
    ),
)
def test_gemini_naming_plans_exact_policy_membership(
    mode: str,
    configured: bool,
    applicable: bool,
    reason: ApplicabilityReason | None,
) -> None:
    merged = _merged_member("pub-one", "The Analytical Engine for Modern Computing")
    policy = replace(_policy(), provider_modes={"gemini": mode, "s2": "required", "serply": "if_configured"})
    credentials = DiscoveryCredentials(
        s2_key="wire-only",
        gemini_key="gemini-wire-only" if configured else None,
    )
    authority = resolve_discovery_authority(policy, credentials)
    wave = plan_gemini_naming((merged,), _emitted(merged), authority)
    assert len(wave.decisions) == 1
    decision = wave.decisions[0]
    assert decision.task.publication_key == "pub-one"
    assert (decision.task.request is not None) is applicable
    assert decision.reason is reason
    if decision.task.request is not None:
        assert decision.task.request.normalized_payload == {
            "generation_config": {
                "maxOutputTokens": 50,
                "temperature": 0.3,
                "topK": 20,
                "topP": 0.8,
            },
            "max_words": 4,
            "model_id": "gemini-2.5-flash-lite",
            "prompt_version": "camelcase-short-title-v1",
            "title": "The Analytical Engine for Modern Computing",
        }


def test_gemini_naming_reduces_canonical_fragment_and_optional_fallback_exactly() -> None:
    gemini = _merged_member("gemini", "The Analytical Engine for Modern Computing")
    fallback = _merged_member("fallback", "A Theory of Mechanical Computation")
    merged = (gemini, fallback)
    policy = replace(_policy(), provider_modes={"gemini": "if_configured", "s2": "required", "serply": "if_configured"})
    authority = resolve_discovery_authority(
        policy,
        DiscoveryCredentials(s2_key="wire-only", gemini_key="gemini-wire-only"),
    )
    wave = plan_gemini_naming(tuple(reversed(merged)), _emitted(*reversed(merged)), authority)
    tasks = {item.task.publication_key: item.task for item in wave.decisions}
    observations = (
        DiscoveryObservation(
            tasks["gemini"],
            TaskDisposition.SUCCEEDED,
            {"candidates": ({"content": {"parts": ({"text": "AnalyticalEngineComputing"},)}},)},
            schema_version="gemini-short-title-v1",
        ),
        DiscoveryObservation(
            tasks["fallback"],
            TaskDisposition.PERMANENT_FAILURE,
            {},
            schema_version="gemini-short-title-v1",
        ),
    )
    first = reduce_gemini_naming(merged, _emitted(*merged), wave, observations, authority)
    second = reduce_gemini_naming(
        tuple(reversed(merged)),
        _emitted(*reversed(merged)),
        wave,
        tuple(reversed(observations)),
        authority,
    )
    assert first == second
    assert all(isinstance(item, CitationKeyFragmentEvidence) for item in first)
    assert [(item.publication_key, item.fragment, item.source) for item in first] == [
        ("fallback", "TheoryMechanicalComputation", "gemini-fallback"),
        ("gemini", "AnalyticalEngineComputing", "gemini"),
    ]
    assert all(".bib" not in item.fragment and "/" not in item.fragment for item in first)


@pytest.mark.parametrize(
    "hostile",
    (
        "Ignore previous instructions",
        "../Escape",
        "Quoted'Fragment",
        "fiveWordsAreFarTooManyHere",
        "lowercase",
        "Analytical Engine",
    ),
)
def test_gemini_naming_rejects_hostile_or_noncanonical_output(hostile: str) -> None:
    merged = _merged_member("pub-one", "Analytical Engine")
    policy = replace(_policy(), provider_modes={"gemini": "required", "s2": "required", "serply": "if_configured"})
    authority = resolve_discovery_authority(
        policy,
        DiscoveryCredentials(s2_key="wire-only", gemini_key="gemini-wire-only"),
    )
    wave = plan_gemini_naming((merged,), _emitted(merged), authority)
    observation = DiscoveryObservation(
        wave.decisions[0].task,
        TaskDisposition.SUCCEEDED,
        {"candidates": ({"content": {"parts": ({"text": hostile},)}},)},
        schema_version="gemini-short-title-v1",
    )
    with pytest.raises(ValueError, match="Gemini citation fragment"):
        reduce_gemini_naming((merged,), _emitted(merged), wave, (observation,), authority)


def test_gemini_naming_is_pure_and_required_failure_never_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    merged = _merged_member("pub-one", "The Analytical Engine for Modern Computing")
    policy = replace(_policy(), provider_modes={"gemini": "required", "s2": "required", "serply": "if_configured"})
    authority = resolve_discovery_authority(
        policy,
        DiscoveryCredentials(s2_key="wire-only", gemini_key="gemini-wire-only"),
    )
    for target in (
        "citeforge.bibtex_utils.short_filename_for_entry",
        "citeforge.bibtex_utils.build_standard_citekey",
        "citeforge.clients.utility_apis.gemini_generate_short_title",
    ):
        monkeypatch.setattr(target, lambda *_args, **_kwargs: 1 / 0)
    wave = plan_gemini_naming((merged,), _emitted(merged), authority)
    failure = DiscoveryObservation(
        wave.decisions[0].task,
        TaskDisposition.PERMANENT_FAILURE,
        {},
        schema_version="gemini-short-title-v1",
    )
    with pytest.raises(ValueError, match="required Gemini naming evidence is blocking"):
        reduce_gemini_naming((merged,), _emitted(merged), wave, (failure,), authority)


@pytest.mark.parametrize(
    ("title", "expected"),
    (
        ("Dairy DigiD: Edge-Cloud Keypoint Detection", "DairyDigiDEdgeCloud"),
        ("API Design for HTTP Clients", "APIDesignHTTPClients"),
        (r"A {LaTeX} Guide to AI", "LaTeXGuideAI"),
        ("Étude naïve des océans", "TudeNaVeDes"),
        ("Graph-based / Learning: Systems!", "GraphBasedLearningSystems"),
        ("the and of to", "TheAndOfTo"),
    ),
)
def test_deterministic_citation_fragment_preserves_exact_legacy_case_and_splitting(
    title: str, expected: str
) -> None:
    assert _deterministic_citation_fragment(title) == expected


def test_merge_source_projection_retains_nested_records_ordinals_and_identity_verdicts() -> None:
    seed = _seed()
    _authority_value, broad = _broad(seed)
    target = next(decision for decision in broad.decisions if decision.task.provider == "crossref")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
    observations[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {
            "results": (
                {
                    "title": "Analytical Engine",
                    "author": ({"given": "Ada", "family": "Lovelace"},),
                    "DOI": "10.1234/right",
                },
                {
                    "title": "Unrelated",
                    "author": ({"given": "Other", "family": "Person"},),
                    "DOI": "10.1234/wrong",
                },
            )
        },
        schema_version="crossref-search-v1",
    )
    projected = project_merge_sources((seed,), (broad,), observations)
    crossref = [item for item in projected if item.provider == "crossref"]
    assert [item.record_ordinal for item in crossref] == [0, 1]
    assert [item.identity_accepted for item in crossref] == [True, False]
    assert all(item.request_key == target.task.request.key for item in crossref if target.task.request is not None)
    assert all(item.observation_digest == observations[index].response_digest for item in crossref)

    malformed = list(observations)
    malformed[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {"results": ({"title": "Analytical Engine", "author": "not-normalized"},)},
        schema_version="crossref-search-v1",
    )
    rejected = next(item for item in project_merge_sources((seed,), (broad,), malformed) if item.provider == "crossref")
    assert rejected.projection_status == "rejected"
    assert rejected.projection_reason == "unsupported_or_malformed_provider_record"
    assert not rejected.identity_accepted and rejected.record_ordinal == 0


def test_pubmed_record_map_expands_in_merge_and_late_identifier_evidence() -> None:
    seed = _seed()
    _authority_value, broad = _broad(seed)
    target = next(decision for decision in broad.decisions if decision.task.provider == "pubmed")
    observations = list(_empty_observations(broad))
    index = next(index for index, item in enumerate(observations) if item.task.key == target.task.key)
    observations[index] = DiscoveryObservation(
        target.task,
        TaskDisposition.SUCCEEDED,
        {
            "records": {
                "123": {
                    "uid": "123",
                    "title": "Analytical Engine",
                    "authors": ({"name": "Ada Lovelace"},),
                    "pubdate": "2024 Jan",
                    "fulljournalname": "Transactions",
                    "articleids": ({"idtype": "doi", "value": "10.1234/pubmed"},),
                }
            }
        },
        schema_version="pubmed-esummary-v1",
    )
    projected = [item for item in project_merge_sources((seed,), (broad,), observations) if item.provider == "pubmed"]
    assert len(projected) == 1
    assert projected[0].entry["fields"]["doi"] == "10.1234/pubmed"
    assert projected[0].entry["fields"]["pmid"] == "123"
    late = derive_late_identifier_evidence((seed,), (broad,), observations)
    assert {(item.kind, item.value) for item in late[0].candidates} >= {
        ("doi", "10.1234/pubmed"),
        ("pmid", "123"),
    }

    bad = list(observations)
    bad[index] = replace(
        observations[index], response={"records": {"123": {"uid": "456", "title": "Analytical Engine"}}}
    )
    with pytest.raises(ValueError, match="PubMed record identity"):
        project_merge_sources((seed,), (broad,), bad)
    with pytest.raises(ValueError, match="PubMed record identity"):
        derive_late_identifier_evidence((seed,), (broad,), bad)


def test_empty_terminal_provider_evidence_is_retained_as_absent_provenance() -> None:
    seed = _seed()
    _authority_value, broad = _broad(seed)
    observations = _empty_observations(broad)
    projected = project_merge_sources((seed,), (broad,), observations)
    assert len(projected) == len(broad.decisions)
    assert all(item.projection_status == "rejected" for item in projected)
    assert {item.projection_reason for item in projected} == {"confirmed_empty", "not_applicable"}
    logical_na = [item for item in projected if item.request_key is None]
    assert len(logical_na) == 1
    assert logical_na[0].task_identity_digest == next(
        item.task.identity_digest for item in broad.decisions if item.task.request is None
    )
    assert logical_na[0].applicability_reason is ApplicabilityReason.PROVIDER_NOT_CONFIGURED
    merged = merge_publication_evidence((seed,), projected)
    provenance = derive_provenance_evidence("generation", "pass", (seed,), projected, merged)
    assert all(
        len([item for item in provenance.contributions if item.decision_key == decision.key])
        == 1 + len(broad.decisions)
        for decision in provenance.decisions
    )


def test_all_not_applicable_logical_decisions_are_exact_negative_provenance() -> None:
    seed = _seed()
    authority = resolve_discovery_authority(_policy(), DiscoveryCredentials(s2_key="wire-only"))
    complete = DoiReduction(
        seed.author_key,
        seed.publication_key,
        "identity_matched",
        "1" * 64,
        {
            "DOI": "10.1234/engine",
            "author": ({"family": "Lovelace"},),
            "container-title": "Transactions on Engines",
            "issued": {"date-parts": ((2024,),)},
            "title": "Analytical Engine",
        },
    )
    broad = plan_broad_discovery((seed,), {"author-ada": "Ada Lovelace"}, authority, (complete,))
    assert all(item.task.request is None for item in broad.decisions)
    projected = project_merge_sources((seed,), (broad,), ())
    assert len(projected) == len(broad.decisions) == 8
    assert len({item.digest for item in projected}) == 8
    assert {item.task_identity_digest for item in projected} == {item.task.identity_digest for item in broad.decisions}
    merged = merge_publication_evidence((seed,), projected)
    provenance = derive_provenance_evidence("generation", "pass", (seed,), projected, merged)
    for decision in provenance.decisions:
        negative = [
            item
            for item in provenance.contributions
            if item.decision_key == decision.key and item.source_kind == EvidenceKind.OBSERVATION.value
        ]
        assert len(negative) == 8
        assert len({item.key for item in negative}) == 8


@pytest.mark.parametrize(
    ("provider", "record", "expected"),
    (
        (
            "doi_csl",
            {
                "title": "Analytical Engine",
                "author": [{"family": "Lovelace", "given": "Ada"}],
                "issued": {"date-parts": [[2024]]},
                "DOI": "10.1234/engine",
                "publisher": "Engine Press",
            },
            {
                "title": "Analytical Engine",
                "author": "Ada Lovelace",
                "year": "2024",
                "doi": "10.1234/engine",
                "publisher": "Engine Press",
            },
        ),
        (
            "doi_bibtex",
            {
                "entry": {
                    "type": "article",
                    "key": "remote",
                    "fields": {
                        "title": "Analytical Engine",
                        "author": "Lovelace, Ada",
                        "year": "2024",
                        "doi": "10.1234/engine",
                        "journal": "Transactions",
                    },
                }
            },
            {
                "title": "Analytical Engine",
                "author": "Lovelace, Ada",
                "year": "2024",
                "doi": "10.1234/engine",
                "journal": "Transactions",
            },
        ),
        (
            "pubmed",
            {
                "title": "Analytical Engine",
                "uid": "123",
                "authors": [{"name": "Ada Lovelace"}],
                "pubdate": "2024 Jan",
                "fulljournalname": "Transactions",
                "articleids": [{"idtype": "doi", "value": "10.1234/engine"}],
            },
            {
                "title": "Analytical Engine",
                "author": "Ada Lovelace",
                "year": "2024",
                "pmid": "123",
                "journal": "Transactions",
                "doi": "10.1234/engine",
            },
        ),
        (
            "europepmc",
            {
                "title": "Analytical Engine",
                "authorString": "Ada Lovelace",
                "pubYear": "2024",
                "pmid": "123",
                "journalTitle": "Transactions",
            },
            {
                "title": "Analytical Engine",
                "author": "Ada Lovelace",
                "year": "2024",
                "pmid": "123",
                "journal": "Transactions",
            },
        ),
        (
            "serply",
            {
                "title": "Analytical Engine",
                "authors": "Ada Lovelace",
                "year": "2024",
                "link": "https://example.test/paper",
                "publication": "Transactions",
            },
            {
                "title": "Analytical Engine",
                "author": "Ada Lovelace",
                "year": "2024",
                "journal": "Transactions",
                "url": "https://example.test/paper",
            },
        ),
    ),
)
def test_explicit_normalized_provider_projectors_preserve_golden_fields(
    provider: str, record: dict[str, object], expected: dict[str, object]
) -> None:
    entry = _project_unmapped_record(provider, record, "pub-one")
    assert entry is not None
    assert entry["fields"] == expected


def test_unknown_provider_projection_fails_closed() -> None:
    assert _project_unmapped_record("unknown", {"title": "Analytical Engine"}, "pub-one") is None


def test_pure_merge_uses_load_repair_then_trust_merge_then_post_merge_without_mutating_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _seed()
    original = dict(seed.baseline_entry["fields"])
    source = MergeSourceEvidence(
        seed.author_key,
        seed.publication_key,
        "crossref",
        "crossref-search-v1",
        "1" * 64,
        "2" * 64,
        0,
        True,
        {
            "type": "article",
            "key": "provider-owned-key",
            "fields": {
                "author": "Lovelace, Ada",
                "doi": "https://doi.org/10.1234/ENGINE",
                "journal": "Transactions on Engines",
                "title": "Analytical Engine",
                "year": "2024",
            },
        },
    )
    stages: list[str] = []
    from citeforge.refresh import publication_discovery as module

    real = module.canonicalize

    def record(entry: dict[str, object], *, stage: object) -> bool:
        stages.append(str(getattr(stage, "value", stage)))
        return real(entry, stage=stage)

    monkeypatch.setattr(module, "canonicalize", record)

    def reject_log(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure merge must not log")

    monkeypatch.setattr("citeforge.merge_utils.logger.debug", reject_log)
    result = merge_publication_evidence((seed,), (source,))
    assert stages == ["load_repair", "post_merge"]
    assert dict(seed.baseline_entry["fields"]) == original
    assert result[0].publication_key == "pub-one"
    assert result[0].final_entry["key"] == "pub-one"
    assert result[0].final_entry["fields"]["doi"] == "10.1234/engine"
    assert result[0].selected_source_digests["journal"] == source.digest


def test_merge_rejects_cross_publication_or_duplicate_source_evidence() -> None:
    seed = _seed()
    source = MergeSourceEvidence(
        seed.author_key,
        "other-publication",
        "crossref",
        "crossref-search-v1",
        "1" * 64,
        "2" * 64,
        0,
        True,
        {"type": "article", "key": "x", "fields": {"title": "Analytical Engine"}},
    )
    with pytest.raises(ValueError, match="merge source membership"):
        merge_publication_evidence((seed,), (source,))
    valid = replace(source, publication_key=seed.publication_key)
    with pytest.raises(ValueError, match="duplicate merge source"):
        merge_publication_evidence((seed,), (valid, valid))
    with pytest.raises(ValueError, match="digest"):
        replace(valid, observation_digest="not-a-digest")
    with pytest.raises(ValueError, match="private contact"):
        replace(
            valid,
            entry={"type": "article", "key": "x", "fields": {"note": "person@example.test"}},
        )


def test_merge_evidence_is_recursively_immutable_and_digest_stable() -> None:
    nested = {"keywords": ["engine", {"areas": ["analysis"]}]}
    entry = {"type": "article", "key": "candidate", "fields": {"title": "Analytical Engine", "note": nested}}
    source = MergeSourceEvidence(
        "author-ada", "pub-one", "crossref", "crossref-search-v1", "1" * 64, "2" * 64, 0, True, entry
    )
    source_digest = source.digest
    nested["keywords"][1]["areas"].append("drift")
    assert source.digest == source_digest
    assert source.entry["fields"]["note"]["keywords"][1]["areas"] == ("analysis",)
    with pytest.raises(TypeError):
        source.entry["fields"]["note"]["keywords"][1]["areas"] += ("drift",)

    final = {"type": "article", "key": "pub-one", "fields": {"title": "Analytical Engine", "note": nested}}
    selected = {"title": "3" * 64, "note": "4" * 64}
    merged = MergedPublicationEvidence("author-ada", "pub-one", final, selected, ("5" * 64,))
    merged_digest = merged.digest
    nested["keywords"].append("later")
    selected["title"] = "6" * 64
    assert merged.digest == merged_digest
    assert merged.final_entry["fields"]["note"]["keywords"][-1] != "later"
    assert merged.selected_source_digests["title"] == "3" * 64
    with pytest.raises(TypeError):
        merged.final_entry["fields"]["note"]["new"] = "drift"


@pytest.mark.parametrize(
    "secret_key",
    (
        "api_key",
        "apiKey",
        "Authorization",
        "cookie",
        "access_token",
        "accessToken",
        "clientSecret",
        "refreshToken",
        "sessionCookie",
        "clientsecret",
        "accesstoken",
        "refreshtoken",
        "sessioncookie",
        "proxyauthorization",
    ),
)
def test_merge_evidence_rejects_nested_secret_keys_but_allows_benign_token_prose(secret_key: str) -> None:
    unsafe = {
        "type": "article",
        "key": "candidate",
        "fields": {"title": "Token classification", "metadata": {secret_key: "wire-secret"}},
    }
    with pytest.raises(ValueError, match="secret"):
        MergeSourceEvidence(
            "author-ada", "pub-one", "crossref", "crossref-search-v1", "1" * 64, "2" * 64, 0, True, unsafe
        )
    with pytest.raises(ValueError, match="secret"):
        MergedPublicationEvidence(
            "author-ada",
            "pub-one",
            unsafe,
            {"title": "3" * 64, "metadata": "4" * 64},
            ("5" * 64,),
        )

    safe = {
        **unsafe,
        "fields": {
            "title": "Token classification improves language models",
            "metadata": {"tokenization": "wordpiece"},
        },
    }
    value = MergeSourceEvidence(
        "author-ada", "pub-one", "crossref", "crossref-search-v1", "1" * 64, "2" * 64, 0, True, safe
    )
    assert value.entry["fields"]["title"] == "Token classification improves language models"


def test_identity_rejected_projection_is_retained_but_never_merged() -> None:
    seed = _seed()
    rejected = MergeSourceEvidence(
        seed.author_key,
        seed.publication_key,
        "crossref",
        "crossref-search-v1",
        "1" * 64,
        "2" * 64,
        0,
        False,
        {"type": "article", "key": "candidate", "fields": {"title": "Unrelated", "doi": "10.9999/wrong"}},
    )
    merged = merge_publication_evidence((seed,), (rejected,))[0]
    assert "doi" not in merged.final_entry["fields"]
    provenance = derive_provenance_evidence("generation", "pass", (seed,), (rejected,), (merged,))
    assert all(
        any(
            item.observation_digest == rejected.observation_digest
            and not item.selected
            and item.rejection_reason == "identity_rejected"
            for item in provenance.contributions
            if item.decision_key == decision.key
        )
        for decision in provenance.decisions
    )


def test_author_global_intents_choose_one_doi_path_survivor_remove_existing_and_omit_absent() -> None:
    def merged(publication_key: str) -> MergedPublicationEvidence:
        entry = {
            "type": "article",
            "key": publication_key,
            "fields": {
                "author": "Lovelace, Ada",
                "doi": "10.1234/shared",
                "journal": "Transactions on Engines",
                "title": "Analytical Engine",
                "year": "2024",
            },
        }
        return MergedPublicationEvidence(
            "author-ada",
            publication_key,
            entry,
            {field: evidence_digest((publication_key, field)) for field in entry["fields"]},
            (evidence_digest(publication_key),),
        )

    values = tuple(merged(key) for key in ("pub-z", "pub-a", "pub-m"))
    corpus = (
        CorpusOutputEvidence("author-ada", "pub-z", "output/Ada/z.bib", "1" * 64),
        CorpusOutputEvidence("author-ada", "pub-a", "output/Ada/a.bib", "2" * 64),
    )
    decision_keys = {
        (value.author_key, value.publication_key): tuple(
            evidence_digest((value.publication_key, field)) for field in value.final_entry["fields"]
        )
        for value in values
    }
    reduction = derive_survivor_reduction(values, corpus)
    assert {(item.publication_key, item.disposition) for item in reduction.decisions} == {
        ("pub-a", SurvivorDisposition.EMITTED),
        ("pub-z", SurvivorDisposition.EXISTING_REMOVE),
        ("pub-m", SurvivorDisposition.ABSENT_DUPLICATE_SUPPRESSED),
    }
    assert derive_survivor_reduction(tuple(reversed(values)), tuple(reversed(corpus))) == reduction
    intents = derive_materialization_intents(
        "generation",
        "pass",
        values,
        corpus,
        reduction,
        (
            NamingEvidence(
                "author-ada",
                "pub-a",
                "Lovelace2024Engine",
                "output/Ada/a.bib",
                "3" * 64,
                tuple(values[1].final_entry["fields"]),
                "deterministic",
                "4" * 64,
            ),
        ),
        decision_keys,
    )
    by_publication = {item.publication_key: item for item in intents}
    assert by_publication["pub-a"].kind in {IntentKind.KEEP, IntentKind.UPSERT}
    assert by_publication["pub-z"].kind is IntentKind.REMOVE
    assert by_publication["pub-z"].removal_reason == "duplicate-doi-loser"
    assert "pub-m" not in by_publication
    assert len([item for item in intents if item.kind in {IntentKind.KEEP, IntentKind.UPSERT}]) == 1
    assert {item.publication_key for item in intents} == {
        item.publication_key
        for item in reduction.decisions
        if item.disposition in {SurvivorDisposition.EMITTED, SurvivorDisposition.EXISTING_REMOVE}
    }

    with pytest.raises(ValueError, match="survivor reduction"):
        derive_materialization_intents("generation", "pass", values[:-1], corpus, reduction, (), decision_keys)
    substituted = replace(
        reduction,
        decisions=tuple(
            replace(item, disposition=SurvivorDisposition.EMITTED) if item.publication_key == "pub-m" else item
            for item in reduction.decisions
        ),
    )
    with pytest.raises(ValueError, match="survivor reduction"):
        derive_materialization_intents("generation", "pass", values, corpus, substituted, (), decision_keys)


def test_c5_intents_require_terminal_naming_and_never_call_naming_or_serialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = MergedPublicationEvidence(
        "author-ada",
        "pub-one",
        _seed().baseline_entry,
        {"title": "1" * 64},
        ("2" * 64,),
    )
    monkeypatch.setattr("citeforge.bibtex_utils.short_filename_for_entry", lambda *_args, **_kwargs: 1 / 0)
    monkeypatch.setattr("citeforge.bibtex_utils.bibtex_from_dict", lambda *_args, **_kwargs: 1 / 0)
    with pytest.raises(ValueError, match="terminal naming"):
        derive_materialization_intents(
            "generation",
            "pass",
            (value,),
            (),
            derive_survivor_reduction((value,), ()),
            (),
            {("author-ada", "pub-one"): ("3" * 64,)},
        )


def test_provenance_derivation_covers_every_final_field_and_every_considered_source() -> None:
    seed = _seed()
    source = MergeSourceEvidence(
        seed.author_key,
        seed.publication_key,
        "crossref",
        "crossref-search-v1",
        "1" * 64,
        "2" * 64,
        0,
        True,
        {
            "type": "article",
            "key": "candidate",
            "fields": {
                "author": "Lovelace, Ada",
                "journal": "Transactions on Engines",
                "title": "Analytical Engine",
                "year": "2024",
            },
        },
    )
    merged = merge_publication_evidence((seed,), (source,))[0]
    bundle = derive_provenance_evidence("generation", "pass", (seed,), (source,), (merged,))
    assert {item.field_name for item in bundle.decisions} == set(merged.final_entry["fields"])
    for decision in bundle.decisions:
        members = [item for item in bundle.contributions if item.decision_key == decision.key]
        assert len([item for item in members if item.selected]) == 1
        assert evidence_digest(sorted(item.key for item in members)) == decision.contribution_set_digest
    journal = next(item for item in bundle.decisions if item.field_name == "journal")
    selected = next(item for item in bundle.contributions if item.decision_key == journal.key and item.selected)
    assert selected.provider == "crossref"
    assert selected.request_key == source.request_key
    rejected = replace(
        source,
        observation_digest="4" * 64,
        entry={"type": "misc", "key": "candidate", "fields": {}},
        projection_status="rejected",
        projection_reason="unsupported_or_malformed_provider_record",
        identity_accepted=False,
    )
    absent = replace(
        source,
        observation_digest="5" * 64,
        entry={"type": "misc", "key": "candidate", "fields": {"title": "Analytical Engine"}},
        identity_accepted=True,
    )
    expanded = derive_provenance_evidence("generation", "pass", (seed,), (source, rejected, absent), (merged,))
    for decision in expanded.decisions:
        members = [item for item in expanded.contributions if item.decision_key == decision.key]
        assert any(
            item.observation_digest == rejected.observation_digest and item.value_digest is None for item in members
        )
        if decision.field_name != "title":
            assert any(
                item.observation_digest == absent.observation_digest
                and item.value_digest is None
                and item.rejection_reason == "field_absent"
                for item in members
            )
