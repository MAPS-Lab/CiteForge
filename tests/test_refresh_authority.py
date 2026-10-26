from __future__ import annotations

from dataclasses import replace

import pytest

from citeforge.refresh.authority import (
    PASS_REGISTRY_DIGEST,
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
    execute_pass,
    pass_for,
    registry_digest,
)

D = "a" * 64
E = "b" * 64


def test_public_pass_registry_is_metadata_only_deterministic_and_detached() -> None:
    assert PASSES
    assert all(item.callback is None for item in PASSES.values())
    assert registry_digest(reversed(tuple(PASSES.values()))) == PASS_REGISTRY_DIGEST
    first = next(iter(PASSES.values()))
    object.__setattr__(first, "version", "forged")
    try:
        assert pass_for(first.pass_id).version != "forged"
        assert registry_digest(PASSES.values()) != PASS_REGISTRY_DIGEST
    finally:
        object.__setattr__(first, "version", pass_for(first.pass_id).version)


def test_typed_evidence_is_canonical_frozen_and_secret_safe() -> None:
    snapshot = CorpusSnapshot("gen", "abc123", D, E, "scanner", "1", "parser", "1", D)
    item = CorpusItemEvidence(
        "gen", snapshot.digest, "output/Ada/paper.bib", "author-ada", D, E, ("pub-b", "pub-a"), "parsed"
    )
    seed = PublicationSeedEvidence(
        "gen", "author-ada", "pub-a", EvidenceKind.CORPUS, item.key, item.digest, E, {"doi": "10.1/x"}, D
    )
    assert item.publication_keys == ("pub-a", "pub-b")
    assert seed.exact_identifiers == {"doi": "10.1/x"}
    with pytest.raises(TypeError):
        seed.exact_identifiers["doi"] = "changed"  # type: ignore[index]
    with pytest.raises(ValueError, match="path"):
        replace(item, source_path="../escape.bib")
    with pytest.raises(ValueError, match="secret"):
        AggregateInput("gen", "pass", "reducer", EvidenceKind.SEED, "x", D, 0, {"api_key": "x"})


def test_provenance_and_intent_values_reject_incoherent_shapes() -> None:
    decision = ProvenanceDecision("gen", "pass", "author", "pub", "title", D, "rule", E, "reduce", "1")
    contribution = ProvenanceContribution("gen", decision.key, "baseline", None, None, None, D, E, True, "selected")
    intent = MaterializationIntent(
        "gen",
        "pass",
        "author",
        "pub",
        "output/Ada/paper.bib",
        "stage/Ada/paper.bib",
        IntentKind.UPSERT,
        None,
        D,
        "reduce",
        "1",
        E,
        ("title",),
        D,
    )
    assert contribution.selected and intent.kind is IntentKind.UPSERT
    keep = replace(intent, kind=IntentKind.KEEP, before_digest=D, after_digest=D)
    assert keep.before_digest == keep.after_digest == keep.final_content_digest
    with pytest.raises(ValueError):
        replace(intent, kind=IntentKind.KEEP, before_digest=None)
    with pytest.raises(ValueError, match="removal reason"):
        replace(
            intent,
            kind=IntentKind.REMOVE,
            target_path=intent.source_path,
            before_digest=D,
            after_digest=None,
            final_fields=(),
            final_content_digest=None,
        )
    with pytest.raises(ValueError):
        replace(contribution, selected=True, value_digest=None)


def test_private_pass_execution_is_pure_deterministic_evidence_only() -> None:
    definition = next(iter(PASSES.values()))
    snapshot = {"generation_id": "gen", "items": ({"kind": "publication", "key": "b", "digest": D},)}
    first = execute_pass(definition.pass_id, snapshot)
    second = execute_pass(definition.pass_id, snapshot)
    assert isinstance(first, PlannerPassReceipt)
    assert first == second
    assert first.unseen_keys == first.expected_items
    assert not hasattr(first, "discovery_closed")


def test_benign_token_word_is_not_secret_material() -> None:
    receipt = execute_pass(
        "bind_corpus_seed",
        {
            "generation_id": "gen",
            "items": ({"kind": "publication", "key": "paper", "digest": D, "title": "Token classification"},),
        },
    )
    assert receipt.expected_items == ("paper",)
