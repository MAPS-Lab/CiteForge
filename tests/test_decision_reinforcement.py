"""Reinforcement tests for the bibliometric decision mechanism.

Each test asserts an invariant of the trust-based merge / dedup logic across MANY
synthetic authors, venues, and preprint/published pairs rather than a single example,
so the rules are provably general and never hinge on one title, DOI, author, or venue.

The invariants covered span a single canonical preprint-DOI predicate (EGU, OSTI,
Qeios, and Zenodo are recognized while published journals under the same registrant
stay published), the preprint/published split counted exactly once in the dedup
composite, a published-DOI paper never relabeled as a preprint by a stale journal, a
truncated author list never overwriting a more complete one by rank, a record's own
preprint self-DOI surviving the registry trust gate, published superseding preprint in
the save-time candidate-DOI net, and a low-trust specific booktitle never overwriting a
trusted generic one.
"""

from __future__ import annotations

from typing import Any

import pytest

from src import bibtex_utils as bt
from src import id_utils as idu
from src import merge_utils as mu
from src import text_utils as tu
from src.canonicalize import CanonicalStage, canonicalize
from src.config import SIM_DEDUP_COMPOSITE_THRESHOLD


def _entry(etype: str, **fields: str) -> dict[str, Any]:
    return {"type": etype, "key": "K", "fields": dict(fields)}


def _art(**fields: str) -> dict[str, Any]:
    return _entry("article", **fields)


def _inp(**fields: str) -> dict[str, Any]:
    return _entry("inproceedings", **fields)


# ---------------------------------------------------------------------------
# One canonical predicate: every preprint/grey/data DOI is "secondary" everywhere;
# a published journal DOI under the same registrant as a preprint stays published.

PREPRINT_DOIS = [
    "10.48550/arxiv.2401.00001",
    "10.1101/2021.01.01.400001",
    "10.21203/rs.3.rs-100001",
    "10.31234/osf.io/abcde",
    "10.26434/chemrxiv-2024-aaaaa",
    "10.2139/ssrn.4000001",
    "10.36227/techrxiv.20000001",
    "10.5194/egusphere-2024-1000",
    "10.2172/1900001",
    "10.32388/QEIOS01",
    "10.31220/agrirxiv.2024.00001",
    "10.48448/underline-1",
    "10.32920/inst-1",
    "10.5281/zenodo.9000001",
]

PUBLISHED_DOIS = [
    "10.5194/acp-24-1-2024",  # published EGU journal, SAME registrant as egusphere preprint
    "10.1145/3580305",
    "10.1038/s41586-024-00001",
    "10.1109/tpami.2024.0000001",
    "10.1016/j.patcog.2024.100001",
    "10.1371/journal.pcbi.1000001",
]


@pytest.mark.parametrize("doi", PREPRINT_DOIS)
def test_preprint_and_grey_dois_are_secondary(doi: str) -> None:
    assert mu._is_preprint_doi(doi) is True
    assert idu.is_secondary_doi(doi) is True


@pytest.mark.parametrize("doi", PUBLISHED_DOIS)
def test_published_dois_are_primary(doi: str) -> None:
    # 10.5194/acp must NOT be classified preprint even though 10.5194/egusphere is:
    # the predicate keys on the specific sub-prefix, not the registrant.
    assert mu._is_preprint_doi(doi) is False


@pytest.mark.parametrize("pre_doi", ["10.5194/egusphere-2024-1000", "10.2172/1900001", "10.32388/QEIOS01"])
def test_published_doi_beats_extended_preprint_doi(pre_doi: str) -> None:
    """A published DOI must win over EGU/OSTI/Qeios preprint DOIs, identically to arXiv."""
    primary = _art(title="A General Method for Everything", doi=pre_doi)
    enrichers = [
        ("crossref", _art(title="A General Method for Everything", doi="10.1038/s41586-024-00001", journal="Nature"))
    ]
    merged = mu.merge_with_policy(primary, enrichers)
    assert merged["fields"].get("doi") == "10.1038/s41586-024-00001"


# ---------------------------------------------------------------------------
# The preprint/published (XOR) split is a single explicit signal, never doubled.

PREPRINT_SERVER_NAMES = ["bioRxiv", "medRxiv", "arXiv", "arXiv e-prints", "Research Square", "SSRN", "ChemRxiv"]


@pytest.mark.parametrize("server", PREPRINT_SERVER_NAMES)
def test_venue_similarity_has_no_preprint_xor_bonus(server: str) -> None:
    """venue_similarity is pure string similarity, so a preprint-vs-journal pair
    never returns a disguised 0.5 XOR bonus."""
    sim = tu.venue_similarity({"journal": server}, {"journal": "Nature Communications"})
    assert sim != 0.5
    assert sim < 0.5


@pytest.mark.parametrize("server", PREPRINT_SERVER_NAMES)
def test_preprint_xor_contributes_exactly_once(server: str) -> None:
    """Excluding the XOR signal drops the composite by exactly 0.10 (Signal 6 only),
    proving venue_similarity adds no second, hidden XOR contribution."""
    a = {"title": "Shared Title", "author": "Ada Byron and Carl Ohm", "year": "2020", "journal": server}
    b = {
        "title": "Shared Title",
        "author": "Ada Byron and Carl Ohm",
        "year": "2020",
        "journal": "Nature Communications",
    }
    with_xor = tu.compute_dedup_score(a, b, count_preprint_xor=True)
    without_xor = tu.compute_dedup_score(a, b, count_preprint_xor=False)
    assert abs((with_xor - without_xor) - 0.10) < 1e-9


@pytest.mark.parametrize("suffix", range(6))
def test_distinct_preprint_published_works_do_not_false_merge(suffix: int) -> None:
    """Two DISTINCT works (different last title word, only partial author overlap, one
    preprint and one published) must not be merged. Counting the XOR signal only once
    keeps their composite below 0.60. Parametrised over independent author sets and titles."""
    a = _art(
        title=f"Robust Graph Kernels for Seismic Inference Case{suffix}",
        author=f"Ann Lee and Bob Ng{suffix} and Cara Poe{suffix}",
        journal="medRxiv",
        doi=f"10.1101/23{suffix:02d}.20010",
        year="2016",
    )
    b = _art(
        title=f"Robust Graph Kernels for Seismic Detection Case{suffix}",
        author=f"Ann Lee and Dan Ray{suffix} and Eve Sol{suffix}",
        journal="PLOS Computational Biology",
        doi=f"10.1371/journal.pcbi.20160{suffix}",
        year="2017",
    )
    # The evidence without the circular XOR credit is genuinely below threshold ...
    assert tu.compute_dedup_score(a["fields"], b["fields"], count_preprint_xor=False) < SIM_DEDUP_COMPOSITE_THRESHOLD
    # ... so the strict matcher (which excludes XOR once the pair gate opens) rejects them.
    assert bt.bibtex_entries_match_strict(a, b) is False


def test_genuine_twin_with_leaked_preprint_journal_still_clears_threshold() -> None:
    """A real preprint/published twin whose published side still carries a leaked preprint
    journal must NOT be dropped. The merge PREPRINT_PAIR site excludes the XOR signal
    exactly (count_preprint_xor=False), so the effective score stays above threshold."""
    existing = {
        "title": "Deep Nets for Ocean State",
        "author": "Ann Lee and Bob Ng",
        "year": "2020",
        "journal": "arXiv",
    }
    published = {
        "title": "Deep Nets for Ocean State",
        "author": "Ann Lee and Bob Ng",
        "year": "2020",
        "journal": "arXiv",
    }
    effective = tu.compute_dedup_score(existing, published, count_preprint_xor=False)
    assert effective >= SIM_DEDUP_COMPOSITE_THRESHOLD


# ---------------------------------------------------------------------------
# A published-DOI paper is never relabeled as a preprint by a stale journal string.


@pytest.mark.parametrize("server", ["bioRxiv", "medRxiv", "arXiv", "Research Square", "SSRN"])
@pytest.mark.parametrize("pub_doi", ["10.1145/3580305", "10.1038/s41586-024-00001", "10.1109/tpami.2024.0000001"])
def test_published_doi_not_downgraded_to_preprint_by_journal(server: str, pub_doi: str) -> None:
    entry = _art(title="A Published Result", author="Ada Byron", journal=server, doi=pub_doi, year="2021")
    canonicalize(entry, stage=CanonicalStage.POST_MERGE)
    # The published DOI is retained and the record is NOT stamped as a preprint.
    assert entry["fields"].get("doi") == pub_doi
    assert "howpublished" not in entry["fields"]
    assert entry["fields"].get("journal", "").lower() not in {
        s.lower() for s in ["bioRxiv", "medRxiv", "arXiv", "Research Square", "SSRN"]
    }


@pytest.mark.parametrize("server", ["bioRxiv", "arXiv", "SSRN"])
def test_genuine_preprint_still_relabeled_to_misc(server: str) -> None:
    """Contrast: a real preprint (secondary DOI) IS relabeled @misc with howpublished."""
    entry = _art(title="A Preprint", author="Ada Byron", journal=server, doi="10.48550/arxiv.2101.00001", year="2021")
    canonicalize(entry, stage=CanonicalStage.POST_MERGE)
    assert entry["type"] == "misc"
    assert server.lower() in entry["fields"].get("howpublished", "").lower()


# ---------------------------------------------------------------------------
# A truncated author list must not overwrite a more complete one by trust alone.


@pytest.mark.parametrize("full_src,trunc_src", [("openalex", "crossref"), ("s2", "openalex"), ("europepmc", "pubmed")])
@pytest.mark.parametrize("n_full,n_trunc", [(6, 3), (4, 2), (10, 4), (3, 1)])
def test_truncated_author_list_does_not_overwrite_complete(
    full_src: str, trunc_src: str, n_full: int, n_trunc: int
) -> None:
    full = " and ".join(f"Given{i} Family{i}" for i in range(n_full))
    trunc = " and ".join(f"Given{i} Family{i}" for i in range(n_trunc))
    primary = _art(title="Shared Title")
    # full list from a slightly-less-trusted source; truncated list from a source only
    # 1 rank more trusted (< TRUST_DIFF_OVERRIDE_THRESHOLD) must not win.
    enrichers = [
        (full_src, _art(title="Shared Title", author=full)),
        (trunc_src, _art(title="Shared Title", author=trunc)),
    ]
    merged = mu.merge_with_policy(primary, enrichers)
    assert len(tu.parse_authors_any(merged["fields"]["author"])) == n_full


def test_much_more_trusted_source_may_still_correct_authors() -> None:
    """The guard does not over-protect: a source >= TRUST_DIFF_OVERRIDE_THRESHOLD more
    trusted may still install a shorter (corrected) list."""
    primary = _art(title="Shared Title", author="A and B and C and D and E and F")  # scholar_min baseline
    enrichers = [("csl", _art(title="Shared Title", author="Real Author and Second Author"))]  # csl is 12 ranks up
    merged = mu.merge_with_policy(primary, enrichers)
    assert len(tu.parse_authors_any(merged["fields"]["author"])) == 2


# ---------------------------------------------------------------------------
# A record's own preprint self-DOI is definitionally correct and survives the gate.


@pytest.mark.parametrize(
    "pre_doi", ["10.48550/arxiv.2401.00001", "10.1101/2021.01.01.1", "10.5194/egusphere-2024-1000"]
)
def test_preprint_self_doi_kept_without_registry_echo(pre_doi: str) -> None:
    primary = _art(title="A Preprint Work")
    enrichers = [("arxiv", _art(title="A Preprint Work", doi=pre_doi))]  # 'arxiv' is not a trusted DOI source
    merged = mu.merge_with_policy(primary, enrichers)
    assert merged["fields"].get("doi") == pre_doi


def test_unverified_published_doi_still_dropped() -> None:
    """Contrast: an unechoed *published* DOI from a non-registry source is still dropped
    (the gate is only relaxed for preprint self-DOIs)."""
    primary = _art(title="A Published Work")
    enrichers = [("openalex", _art(title="A Published Work", doi="10.1145/3580305"))]
    merged = mu.merge_with_policy(primary, enrichers)
    assert "doi" not in merged["fields"]


# ---------------------------------------------------------------------------
# Published supersedes preprint in the candidate-DOI net's survivor decision.


@pytest.mark.parametrize("pre_doi", PREPRINT_DOIS)
@pytest.mark.parametrize("pub_doi", ["10.1145/3580305", "10.1038/s41586-024-00001"])
def test_net_published_supersedes_preprint_decision(pre_doi: str, pub_doi: str) -> None:
    """The exact predicate the net uses before removing a file: an incoming published
    entry must be recognised as superseding an on-disk preprint of the same work."""
    merged_doi = idu.normalize_doi(pub_doi)
    on_disk_doi = idu.normalize_doi(pre_doi)
    merged_is_published = bool(merged_doi and not idu.is_secondary_doi(merged_doi))
    assert merged_is_published is True
    assert idu.is_secondary_doi(on_disk_doi or "") is True


# ---------------------------------------------------------------------------
# A low-trust specific booktitle must not overwrite a trusted generic one.


def test_low_trust_specific_booktitle_does_not_overwrite_trusted_generic() -> None:
    primary = _inp(title="Shared Title")
    enrichers = [
        ("crossref", _inp(title="Shared Title", booktitle="Lecture Notes in Computer Science")),  # generic, rank 5
        ("arxiv", _inp(title="Shared Title", booktitle="Advances in Neural Information Processing Systems")),  # rank 10
    ]
    merged = mu.merge_with_policy(primary, enrichers)
    assert merged["fields"]["booktitle"] == "Lecture Notes in Computer Science"


def test_trusted_specific_booktitle_upgrades_generic() -> None:
    primary = _inp(title="Shared Title")
    enrichers = [
        ("crossref", _inp(title="Shared Title", booktitle="Lecture Notes in Computer Science")),  # generic, rank 5
        ("pubmed", _inp(title="Shared Title", booktitle="IEEE Conference on Computer Vision")),  # specific, rank 3
    ]
    merged = mu.merge_with_policy(primary, enrichers)
    assert merged["fields"]["booktitle"] == "IEEE Conference on Computer Vision"
