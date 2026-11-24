"""Publication identity policy contracts."""

from __future__ import annotations

from typing import Any

import pytest

from citeforge.identity import IdentityContext, IdentityReason, evaluate_identity


def _entry(title: str | None, doi: str | None = None) -> dict[str, Any]:
    fields: dict[str, str] = {"author": "Alpha, Alice", "year": "2024"}
    if title:
        fields["title"] = title
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


@pytest.mark.parametrize(
    "context",
    [IdentityContext.ENRICHMENT, IdentityContext.CANDIDATE_DOI_NET, IdentityContext.DISK_SURVIVOR],
)
@pytest.mark.parametrize(
    "titles", [("Shared Publication Title", None), (None, "Shared Publication Title"), (None, None)]
)
def test_doi_match_requires_titles_for_similarity_guard(
    context: IdentityContext,
    titles: tuple[str | None, str | None],
) -> None:
    """A DOI cannot bypass the required title-similarity check when titles are absent."""
    doi = "10.1234/example"
    left = _entry(titles[0], doi)
    right = _entry(titles[1], doi)

    evidence = evaluate_identity(
        left,
        right,
        context=context,
        candidate_doi=doi if context is IdentityContext.CANDIDATE_DOI_NET else None,
    )

    assert evidence.verdict is False
    assert evidence.reason is IdentityReason.MISSING_TITLE


@pytest.mark.parametrize("context", [IdentityContext.CANDIDATE_DOI_NET, IdentityContext.DISK_SURVIVOR])
def test_doi_version_match_also_requires_titles(context: IdentityContext) -> None:
    """DOI base-version equivalence cannot bypass the title-presence guard."""
    left_doi = "10.20944/preprints202304.0409.v1"
    right_doi = "10.20944/preprints202304.0409.v2"
    evidence = evaluate_identity(
        _entry(None, left_doi),
        _entry(None, right_doi),
        context=context,
        candidate_doi=right_doi if context is IdentityContext.CANDIDATE_DOI_NET else None,
    )

    assert evidence.verdict is False
    assert evidence.reason is IdentityReason.MISSING_TITLE


def test_disk_same_key_exact_title_preserves_legacy_key_title_precedence() -> None:
    """The disk ladder intentionally trusts same-key exact-title before DOI-less guards."""
    left = _entry("Shared Exact Publication Title")
    left["fields"].update(author="Alpha, Alice", year="2020")
    right = _entry("Shared Exact Publication Title")
    right["fields"].update(author="Zulu, Zoe", year="2024")

    evidence = evaluate_identity(left, right, context=IdentityContext.DISK_SURVIVOR)

    assert evidence.verdict is True
    assert evidence.reason is IdentityReason.KEY_TITLE


def _disk(title: str, doi: str, year: str, *, journal: str = "", howpublished: str = "") -> dict[str, Any]:
    fields = {"title": title, "author": "Jet D Termorshuizen and Helena L Davies and Cynthia M Bulik", "year": year}
    fields.update({"doi": doi, **({"journal": journal} if journal else {})})
    if howpublished:
        fields["howpublished"] = howpublished
    return {"type": "article" if journal else "misc", "key": f"Termorshuizen{year}", "fields": fields}


def test_unlinked_preprint_is_not_the_published_record() -> None:
    """The November refresh deleted a medRxiv preprint for a journal paper whose title
    scores 0.91 against it, with no relation between the DOIs. Both must be kept."""
    preprint = _disk(
        "Genome-wide association studies of binge-eating behaviour and anorexia nervosa yield insights "
        "into the unique and shared biology of eating disorder phenotypes",
        "10.1101/2025.01.31.25321397",
        "2025",
        howpublished="medRxiv",
    )
    published = _disk(
        "Genomic meta-analyses of binge-eating behavior and anorexia nervosa yield insights "
        "into the unique and shared biology of eating disorder phenotypes",
        "10.1038/s44220-026-00698-2",
        "2026",
        journal="Nature Mental Health",
    )

    assert evaluate_identity(published, preprint, context=IdentityContext.DISK_SURVIVOR).verdict is False
    assert evaluate_identity(preprint, published, context=IdentityContext.DISK_SURVIVOR).verdict is False


def test_preprint_with_the_published_title_is_still_superseded() -> None:
    """A near-identical title stays a link, so the routine preprint-to-journal case still dedups."""
    title = "Genomic meta-analyses of binge-eating behavior and anorexia nervosa"
    preprint = _disk(title, "10.1101/2025.01.31.25321397", "2025", howpublished="medRxiv")
    published = _disk(title, "10.1038/s44220-026-00698-2", "2026", journal="Nature Mental Health")

    assert evaluate_identity(published, preprint, context=IdentityContext.DISK_SURVIVOR).verdict is True
