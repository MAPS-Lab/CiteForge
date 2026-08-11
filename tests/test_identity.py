"""Publication identity policy contracts."""

from __future__ import annotations

from typing import Any

from citeforge.identity import IdentityContext, IdentityReason, evaluate_identity


def _entry(title: str, doi: str | None = None) -> dict[str, Any]:
    fields: dict[str, str] = {"title": title, "author": "Alpha, Alice", "year": "2024"}
    if doi:
        fields["doi"] = doi
    return {"type": "misc", "key": "Alpha2024", "fields": fields}


def test_candidate_net_matches_doi_base_version_with_title_guard() -> None:
    """The candidate net recognizes a real DOI version and returns its provenance."""
    disk = _entry("The Ethical Frontier: Navigating the Metaverse", "10.20944/preprints202304.0409.v1")
    incoming = _entry("The Ethical Frontier, Navigating the Metaverse")

    evidence = evaluate_identity(
        disk,
        incoming,
        context=IdentityContext.CANDIDATE_DOI_NET,
        candidate_doi="10.20944/preprints202304.0409.v2",
    )

    assert evidence.verdict is True
    assert evidence.reason is IdentityReason.DOI_VERSION
    assert evidence.matched_candidate_doi == "10.20944/preprints202304.0409.v2"


def test_candidate_net_rejects_doi_base_version_title_conflict() -> None:
    """DOI version evidence remains subject to the nonempty-title conflict veto."""
    disk = _entry("The Ethical Frontier: Navigating the Metaverse", "10.20944/preprints202304.0409.v1")
    incoming = _entry("Completely Unrelated Coastal Radar Study")

    evidence = evaluate_identity(
        disk,
        incoming,
        context=IdentityContext.CANDIDATE_DOI_NET,
        candidate_doi="10.20944/preprints202304.0409.v2",
    )

    assert evidence.verdict is False
    assert evidence.reason is IdentityReason.IDENTIFIER_TITLE_CONFLICT
    assert evidence.matched_candidate_doi == "10.20944/preprints202304.0409.v2"


def test_disk_same_key_exact_title_preserves_legacy_key_title_precedence() -> None:
    """The disk ladder intentionally trusts same-key exact-title before DOI-less guards."""
    left = _entry("Shared Exact Publication Title")
    left["fields"].update(author="Alpha, Alice", year="2020")
    right = _entry("Shared Exact Publication Title")
    right["fields"].update(author="Zulu, Zoe", year="2024")

    evidence = evaluate_identity(left, right, context=IdentityContext.DISK_SURVIVOR)

    assert evidence.verdict is True
    assert evidence.reason is IdentityReason.KEY_TITLE
