from __future__ import annotations

import csv
import os
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent
from typing import Any

import pytest

from citeforge import api_configs, api_generics, bibtex_utils, merge_utils
from citeforge.clients import scholar, search_apis
from citeforge.config import get_min_year
from citeforge.identity import IdentityContext, evaluate_identity
from citeforge.models import Record
from tests.test_data import KNOWN_PAPERS, REQUIRED_FIELDS, TEST_AUTHOR


@pytest.mark.live
def test_fetch_and_merge(api_keys: dict[str, Any]) -> None:
    """
    Validate end-to-end publication fetching from Scholar and DBLP followed
    by deduplication.
    """
    if not api_keys.get("serpapi"):
        pytest.skip("SerpAPI key not available")

    rec = Record(
        name=TEST_AUTHOR["name"],
        scholar_id=TEST_AUTHOR["scholar_id"],
        dblp=TEST_AUTHOR["dblp"],
    )

    scholar_data = scholar.fetch_author_publications(
        api_keys["serpapi"],
        rec.scholar_id,
        rec.name,
    )

    scholar_pubs = scholar_data.get("articles", [])
    assert scholar_pubs, "Scholar returned no usable publications"

    min_year = get_min_year()
    dblp_pubs = search_apis.dblp_fetch_for_author(
        rec.name,
        rec.dblp,
        min_year,
    )
    assert dblp_pubs, "DBLP returned no usable publications"

    merged = scholar.merge_publication_lists(
        scholar_pubs,
        dblp_pubs,
        rec.name,
    )

    assert merged, "Scholar and DBLP returned no mergeable publications"


def _require_enrichment(
    source_name: str,
    fetch_bibtex: Callable[[], str | None],
    baseline_entry: dict[str, Any],
    enrichers: list[tuple[str, dict[str, Any]]],
) -> None:
    """Require one live enrichment source to return a matching BibTeX entry."""
    bib = fetch_bibtex()
    assert bib, f"{source_name} returned no usable BibTeX"
    entry = bibtex_utils.parse_bibtex_to_dict(bib)
    assert entry, f"{source_name} returned invalid BibTeX"
    identity = evaluate_identity(baseline_entry, entry, context=IdentityContext.ENRICHMENT)
    assert identity.verdict, f"{source_name} returned a non-matching paper"
    enrichers.append((source_name, entry))


def _require_candidate_enrichment(
    source_name: str,
    search_fn: Callable[[], list[dict[str, Any]]],
    build_fn: Callable[[dict[str, Any], str], str | None],
    first_author: str,
    baseline_entry: dict[str, Any],
    enrichers: list[tuple[str, dict[str, Any]]],
) -> None:
    """Admit the first candidate that passes the enrichment identity gate."""
    candidates = search_fn()
    assert candidates, f"{source_name} returned no usable candidates"
    for candidate in candidates:
        bib = build_fn(candidate, first_author)
        if not bib:
            continue
        entry = bibtex_utils.parse_bibtex_to_dict(bib)
        if not entry:
            continue
        identity = evaluate_identity(baseline_entry, entry, context=IdentityContext.ENRICHMENT)
        if identity.verdict:
            enrichers.append((source_name, entry))
            return
    pytest.fail(f"{source_name} returned no candidate matching the enrichment identity gate")


def _try_enrichment_sources(
    paper: dict[str, Any],
    api_keys: dict[str, str],
    baseline_entry: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Run all enrichment sources against a paper and return matched entries."""
    enrichers: list[tuple[str, dict[str, Any]]] = []
    first_author = paper["first_author"]
    title = paper["title"]

    if api_keys.get("semantic"):
        _require_candidate_enrichment(
            "s2",
            lambda: search_apis.s2_search_papers_multiple(title, first_author, api_keys["semantic"], max_results=5),
            lambda record, keyhint: api_generics.build_bibtex_from_response(
                record, keyhint, api_configs.S2_FIELD_MAPPING
            ),
            first_author,
            baseline_entry,
            enrichers,
        )

    _require_candidate_enrichment(
        "crossref",
        lambda: search_apis.crossref_search_multiple(title, first_author, max_results=5),
        lambda record, keyhint: api_generics.build_bibtex_from_response(
            record, keyhint, api_configs.CROSSREF_FIELD_MAPPING
        ),
        first_author,
        baseline_entry,
        enrichers,
    )

    if paper["arxiv_id"]:
        _require_candidate_enrichment(
            "arxiv",
            lambda: search_apis.arxiv_search(title, first_author, paper["year"]),
            lambda record, keyhint: api_generics.build_bibtex_from_response(
                record, keyhint, api_configs.ARXIV_FIELD_MAPPING
            ),
            first_author,
            baseline_entry,
            enrichers,
        )

    if paper["doi"]:

        def fetch_csl() -> str | None:
            csl = search_apis.fetch_csl_via_doi(paper["doi"])
            return search_apis.bibtex_from_csl(csl, first_author) if csl else None

        _require_enrichment("csl", fetch_csl, baseline_entry, enrichers)

    return enrichers


@pytest.mark.live
def test_full_enrichment_pipeline(api_keys: dict[str, Any]) -> None:
    """
    Execute the complete enrichment workflow for a known paper.
    """
    if not api_keys.get("semantic"):
        pytest.skip("Semantic Scholar key not available")

    paper = KNOWN_PAPERS[0]

    baseline_bib = dedent("""\
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer and Niki Parmar
            and Jakob Uszkoreit and Llion Jones and Aidan N. Gomez
            and Lukasz Kaiser and Illia Polosukhin},
          booktitle = {Advances in Neural Information Processing Systems},
          year = {2017}
        }""")

    baseline_entry = bibtex_utils.parse_bibtex_to_dict(baseline_bib)
    assert baseline_entry is not None, "Failed to parse baseline BibTeX"

    enrichers = _try_enrichment_sources(paper, api_keys, baseline_entry)

    merged_entry = merge_utils.merge_with_policy(baseline_entry, enrichers)

    missing_fields = [f for f in REQUIRED_FIELDS if f not in merged_entry["fields"]]
    assert not missing_fields, f"Missing required fields after merge: {missing_fields}"

    final_bib = bibtex_utils.bibtex_from_dict(merged_entry)
    assert "@" in final_bib, "Final BibTeX rendering failed"


def test_file_output(tmp_path: Path) -> None:
    """
    Validate BibTeX file writing and organization into per-author directories.
    """
    out_dir = str(tmp_path)

    paper = KNOWN_PAPERS[0]
    entry: dict[str, Any] = {
        "type": "inproceedings",
        "key": "Vaswani2017:Attention",
        "fields": {
            "title": paper["title"],
            "author": " and ".join(paper["authors"]),
            "year": str(paper["year"]),
            "booktitle": "NeurIPS",
            "doi": paper["doi"],
        },
    }

    saved_path, _ = merge_utils.save_entry_to_file(
        out_dir,
        TEST_AUTHOR["scholar_id"],
        entry,
        None,
    )

    assert saved_path is not None, "save_entry_to_file returned no path"
    assert os.path.exists(saved_path), f"File was not created at {saved_path}"
    assert "@inproceedings" in Path(saved_path).read_text(encoding="utf-8"), "File content invalid"


def test_csv_summary_integration(tmp_path: Path) -> None:
    """
    Confirm that CSV summary export integrates correctly with the processing pipeline.
    """
    from citeforge.io_utils import append_summary_to_csv, init_summary_csv

    csv_path = tmp_path / "summary.csv"
    csv_path_str = str(csv_path)

    init_summary_csv(csv_path_str)
    assert csv_path.exists(), "CSV was not created"

    test_entries = [
        (
            "output/Vaswani/Attention.bib",
            5,
            {
                "scholar_bib": True,
                "s2": True,
                "crossref": True,
                "doi_csl": True,
                "openalex": True,
            },
        ),
        (
            "output/He/ResNet.bib",
            2,
            {
                "arxiv": True,
                "doi_bibtex": True,
            },
        ),
        ("output/Devlin/BERT.bib", 0, {}),
    ]

    _flag_keys = [
        "scholar_bib",
        "scholar_page",
        "s2",
        "crossref",
        "openreview",
        "arxiv",
        "openalex",
        "pubmed",
        "europepmc",
        "doi_csl",
        "doi_bibtex",
    ]
    for file_path, trust_hits, partial_flags in test_entries:
        flags = {k: partial_flags.get(k, False) for k in _flag_keys}
        append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    with open(csv_path_str, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == len(test_entries), f"Expected {len(test_entries)} rows, got {len(rows)}"

    for i, (_, expected_hits, _) in enumerate(test_entries):
        actual = int(rows[i]["trust_hits"])
        assert actual == expected_hits, f"Row {i}: expected trust_hits={expected_hits}, got {actual}"


def test_complex_paper_enrichment() -> None:
    """
    Test enrichment pipeline with a complex paper (AlphaFold) that has
    many authors and complex metadata.
    """
    paper = next(p for p in KNOWN_PAPERS if p["name"] == "alphafold")

    baseline_entry: dict[str, Any] = {
        "type": "article",
        "key": "Jumper2021",
        "fields": {
            "title": paper["title"],
            "author": " and ".join(paper["authors"]),
            "year": str(paper["year"]),
            "journal": paper["venue"],
        },
    }

    enrichers: list[tuple[str, dict[str, Any]]] = [
        (
            "crossref",
            {
                "type": "article",
                "fields": {
                    "title": paper["title"],
                    "author": " and ".join(paper["authors"]),
                    "year": str(paper["year"]),
                    "doi": paper["doi"],
                    "journal": "Nature",
                },
            },
        ),
    ]

    merged = merge_utils.merge_with_policy(baseline_entry, enrichers)

    assert merged["fields"]["title"] == paper["title"]
    assert "Jumper" in merged["fields"]["author"]
    assert "Hassabis" in merged["fields"]["author"]
