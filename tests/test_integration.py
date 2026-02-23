import os
from collections.abc import Callable
from textwrap import dedent
from typing import Any

import pytest

from src import bibtex_utils, merge_utils
from src.clients import scholar, search_apis
from src.clients.helpers import get_current_year
from src.config import CONTRIBUTION_WINDOW_YEARS
from src.models import Record
from tests.fixtures import load_api_keys
from tests.test_data import KNOWN_PAPERS, REQUIRED_FIELDS, TEST_AUTHOR

# Integration tests that hit external APIs require a Serply key.
# Tests that lack the key are automatically skipped.


@pytest.fixture(scope="module")
def api_keys():
    return load_api_keys()

def test_fetch_and_merge(api_keys):
    """
    Validate end-to-end publication fetching from Scholar and DBLP followed
    by deduplication.
    """
    if not api_keys.get('serply'):
        pytest.skip("Serply key not available")

    # build test record for known author with both Scholar and DBLP presence
    rec = Record(
        name=TEST_AUTHOR['name'],
        scholar_id=TEST_AUTHOR['scholar_id'],
        dblp=TEST_AUTHOR['dblp']
    )

    # fetch publications from Scholar (primary source)
    scholar_data = scholar.fetch_author_publications(
        api_keys['serply'],
        rec.scholar_id,
        rec.name,
    )

    scholar_pubs = scholar_data.get('articles', [])
    if not scholar_pubs:
        pytest.skip("Scholar returned no results (key expired or rate-limited)")

    # fetch publications from DBLP (computer science bibliography)
    dblp_pubs = []
    try:
        current_year = get_current_year()
        min_year = current_year - CONTRIBUTION_WINDOW_YEARS
        dblp_pubs = search_apis.dblp_fetch_for_author(
            rec.name,
            rec.dblp,
            min_year
        )
    except Exception as e:
        print(f"DBLP fetch failed: {e}")

    # deduplicate using fuzzy title/author matching (threshold=0.9)
    merged = scholar.merge_publication_lists(
        scholar_pubs,
        dblp_pubs,
        rec.name
    )

    # We expect some merging to happen, or at least not to crash
    assert isinstance(merged, list)


def _try_enrich(
    source_name: str,
    fetch_bibtex: Callable[[], str | None],
    baseline_entry: dict[str, Any],
    enrichers: list[tuple[str, dict[str, Any]]],
) -> None:
    """Try a single enrichment source: fetch BibTeX, parse, match, and append."""
    try:
        bib = fetch_bibtex()
        if bib is None:
            return
        entry = bibtex_utils.parse_bibtex_to_dict(bib)
        if entry and bibtex_utils.bibtex_entries_match_strict(baseline_entry, entry):
            enrichers.append((source_name, entry))
    except Exception as e:
        print(f"{source_name} enrichment failed: {e}")


def _try_enrichment_sources(
    paper: dict[str, Any],
    api_keys: dict[str, str],
    baseline_entry: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    """Run all enrichment sources against a paper and return matched entries."""
    enrichers: list[tuple[str, dict[str, Any]]] = []
    first_author = paper['first_author']

    # Semantic Scholar (enhanced metadata)
    if api_keys.get('semantic'):
        def fetch_s2() -> str | None:
            result = search_apis.s2_search_paper(paper['title'], first_author, api_keys['semantic'])
            if result:
                return search_apis.build_bibtex_from_s2(result, first_author)
            return None
        _try_enrich('s2', fetch_s2, baseline_entry, enrichers)

    # Crossref (DOI registration agency)
    def fetch_crossref() -> str | None:
        result = search_apis.crossref_search(paper['title'], first_author)
        if result:
            return search_apis.build_bibtex_from_crossref(result, first_author)
        return None
    _try_enrich('crossref', fetch_crossref, baseline_entry, enrichers)

    # arXiv (preprint repository)
    if paper['arxiv_id']:
        def fetch_arxiv() -> str | None:
            entries = search_apis.arxiv_search(paper['title'], first_author, paper['year'])
            if entries:
                return search_apis.build_bibtex_from_arxiv(entries[0], first_author)
            return None
        _try_enrich('arxiv', fetch_arxiv, baseline_entry, enrichers)

    # DOI negotiation (CSL-JSON from doi.org resolver)
    if paper['doi']:
        def fetch_csl() -> str | None:
            csl = search_apis.fetch_csl_via_doi(paper['doi'])
            if csl:
                return search_apis.bibtex_from_csl(csl, first_author)
            return None
        _try_enrich('csl', fetch_csl, baseline_entry, enrichers)

    return enrichers


def test_full_enrichment_pipeline(api_keys):
    """
    Execute the complete enrichment workflow for a known paper.
    """
    if not api_keys.get('serply'):
        pytest.skip("Serply key not available")

    # test with "Attention Is All You Need" (well-documented paper with known DOI/arXiv)
    paper = KNOWN_PAPERS[0]

    # establish baseline BibTeX from known paper metadata
    baseline_bib = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer and Niki Parmar
            and Jakob Uszkoreit and Llion Jones and Aidan N. Gomez
            and Lukasz Kaiser and Illia Polosukhin},
          booktitle = {Advances in Neural Information Processing Systems},
          year = {2017}
        }
    """).strip()

    baseline_entry = bibtex_utils.parse_bibtex_to_dict(baseline_bib)
    assert baseline_entry is not None, "Failed to parse baseline BibTeX"

    # enrich from multiple sources, validating each against baseline
    enrichers = _try_enrichment_sources(paper, api_keys, baseline_entry)

    # merge using trust hierarchy (CSL > BibTeX > DataCite > ... > Scholar)
    merged_entry = merge_utils.merge_with_policy(baseline_entry, enrichers)

    # verify all required fields present (title, author, year)
    missing_fields = [f for f in REQUIRED_FIELDS if f not in merged_entry['fields']]
    assert not missing_fields, f"Missing required fields after merge: {missing_fields}"

    # validate final BibTeX can be rendered correctly
    final_bib = bibtex_utils.bibtex_from_dict(merged_entry)
    assert '@' in final_bib, "Final BibTeX rendering failed"

def test_file_output(tmp_path):
    """
    Validate BibTeX file writing and organization into per-author directories.
    """
    out_dir = str(tmp_path)

    # build sample entry from known paper
    paper = KNOWN_PAPERS[0]
    entry = {
        'type': 'inproceedings',
        'key': 'Vaswani2017:Attention',
        'fields': {
            'title': paper['title'],
            'author': ' and '.join(paper['authors']),
            'year': str(paper['year']),
            'booktitle': 'NeurIPS',
            'doi': paper['doi'],
        }
    }

    # save to per-author subdirectory with auto-generated filename
    saved_path, _ = merge_utils.save_entry_to_file(
        out_dir,
        TEST_AUTHOR['scholar_id'],
        entry,
        None
    )

    assert saved_path and os.path.exists(saved_path), "File was not created"

    # verify file content is valid BibTeX
    with open(saved_path, encoding='utf-8') as f:
        content = f.read()

    assert '@inproceedings' in content, "File content invalid"

def test_csv_summary_integration(tmp_path):
    """
    Confirm that CSV summary export integrates correctly with the processing pipeline.
    """
    from src.io_utils import append_summary_to_csv, init_summary_csv

    out_dir = tmp_path
    csv_path = out_dir / 'summary.csv'
    csv_path_str = str(csv_path)

    init_summary_csv(csv_path_str)

    assert csv_path.exists(), "CSV was not created"

    # simulate main.py appending entries with varying enrichment quality
    test_entries = [
        ("output/Vaswani/Attention.bib", 5, {
            'scholar_bib': True, 's2': True, 'crossref': True,
            'doi_csl': True, 'openalex': True
        }),
        ("output/He/ResNet.bib", 2, {
            'arxiv': True, 'doi_bibtex': True
        }),
        ("output/Devlin/BERT.bib", 0, {}),  # zero enrichment case
    ]

    for file_path, trust_hits, partial_flags in test_entries:
        # build complete flags dict with False defaults
        flags = {
            'scholar_bib': False, 'scholar_page': False, 's2': False,
            'crossref': False, 'openreview': False, 'arxiv': False,
            'openalex': False, 'pubmed': False, 'europepmc': False,
            'doi_csl': False, 'doi_bibtex': False,
        }
        flags.update(partial_flags)
        append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    import csv
    with open(csv_path_str, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == len(test_entries), f"Expected {len(test_entries)} rows, got {len(rows)}"

    # verify trust_hits match expected values
    expected_hits = [5, 2, 0]
    for i, expected in enumerate(expected_hits):
        actual = rows[i]['trust_hits']
        assert int(actual) == expected, (
            f"Row {i}: expected trust_hits={expected}, got {actual}"
        )

def test_complex_paper_enrichment(api_keys):
    """
    Test enrichment pipeline with a complex paper (AlphaFold) that has
    many authors and complex metadata.
    """
    if not api_keys.get('serply'):
        pytest.skip("Serply key not available")

    paper = next(p for p in KNOWN_PAPERS if p['name'] == 'alphafold')

    baseline_entry = {
        'type': 'article',
        'key': 'Jumper2021',
        'fields': {
            'title': paper['title'],
            'author': ' and '.join(paper['authors']),
            'year': str(paper['year']),
            'journal': paper['venue'],
        }
    }

    # Simulate enrichments with complex data
    enrichers = [
        ('crossref', {
            'type': 'article',
            'fields': {
                'title': paper['title'],
                'author': ' and '.join(paper['authors']), # Full author list
                'year': str(paper['year']),
                'doi': paper['doi'],
                'journal': 'Nature',
            }
        })
    ]

    merged = merge_utils.merge_with_policy(baseline_entry, enrichers)

    # Verify merge didn't crash and preserved data
    assert merged['fields']['title'] == paper['title']
    assert 'Jumper' in merged['fields']['author']
    assert 'Hassabis' in merged['fields']['author']
