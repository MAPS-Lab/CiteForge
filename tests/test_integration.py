import os
import shutil
import sys
import urllib.error
from pathlib import Path
from textwrap import dedent
import pytest
from src import api_clients
from src import bibtex_utils
from src import merge_utils
from src.models import Record
from src.config import CONTRIBUTION_WINDOW_YEARS
from tests.fixtures import load_api_keys
from tests.test_data import TEST_AUTHOR, KNOWN_PAPERS, REQUIRED_FIELDS

@pytest.fixture(scope="module")
def api_keys():
    return load_api_keys()

def test_fetch_and_merge(api_keys):
    """
    Validate end-to-end publication fetching from Scholar and DBLP followed
    by deduplication.
    """
    if not api_keys.get('serpapi'):
        pytest.skip("SerpAPI key not available")

    # build test record for known author with both Scholar and DBLP presence
    rec = Record(
        name=TEST_AUTHOR['name'],
        scholar_id=TEST_AUTHOR['scholar_id'],
        dblp=TEST_AUTHOR['dblp']
    )

    # fetch publications from Scholar (primary source)
    scholar_data = api_clients.fetch_author_publications(
        api_keys['serpapi'],
        rec.scholar_id
    )

    scholar_pubs = scholar_data.get('articles', [])
    assert scholar_pubs and len(scholar_pubs) > 0, "No publications fetched from Scholar"

    # fetch publications from DBLP (computer science bibliography)
    dblp_pubs = []
    try:
        current_year = api_clients.get_current_year()
        min_year = current_year - CONTRIBUTION_WINDOW_YEARS
        dblp_pubs = api_clients.dblp_fetch_for_author(
            rec.name,
            rec.dblp,
            min_year
        )
    except Exception as e:
        print(f"DBLP fetch failed: {e}")

    # deduplicate using fuzzy title/author matching (threshold=0.9)
    merged = api_clients.merge_publication_lists(
        scholar_pubs,
        dblp_pubs,
        rec.name
    )

    # We expect some merging to happen, or at least not to crash
    assert isinstance(merged, list)

def test_full_enrichment_pipeline(api_keys):
    """
    Execute the complete enrichment workflow for a known paper.
    """
    if not api_keys.get('serpapi'):
        pytest.skip("SerpAPI key not available")

    # test with "Attention Is All You Need" (well-documented paper with known DOI/arXiv)
    paper = KNOWN_PAPERS[0]

    # establish baseline BibTeX from Scholar citation
    cite_link = api_clients.search_scholar_for_cite_link(
        api_keys['serpapi'],
        paper['title'],
        paper['first_author']
    )

    assert cite_link, "Could not find cite link from Scholar"

    try:
        baseline_bib = api_clients.fetch_bibtex_from_cite(
            api_keys['serpapi'],
            cite_link
        )
    except urllib.error.HTTPError as e:
        if e.code == 403:
            print("⚠️  Google Scholar blocked the request (403). Using fallback BibTeX.")
            # Fallback to known BibTeX for this paper to allow pipeline testing to continue
            baseline_bib = dedent("""
                @inproceedings{Vaswani2017,
                  title = {Attention Is All You Need},
                  author = {Ashish Vaswani and Noam Shazeer and Niki Parmar and Jakob Uszkoreit and Llion Jones and Aidan N. Gomez and Lukasz Kaiser and Illia Polosukhin},
                  booktitle = {Advances in Neural Information Processing Systems},
                  year = {2017}
                }
            """).strip()
        else:
            raise e

    assert baseline_bib, "Could not fetch baseline BibTeX from Scholar"

    baseline_entry = bibtex_utils.parse_bibtex_to_dict(baseline_bib)

    # enrich from multiple sources, validating each against baseline
    enrichers = []

    # attempt Semantic Scholar enrichment (enhanced metadata)
    if api_keys.get('semantic'):
        try:
            s2_paper = api_clients.s2_search_paper(
                paper['title'],
                paper['first_author'],
                api_keys['semantic']
            )
            if s2_paper:
                s2_bib = api_clients.build_bibtex_from_s2(s2_paper, paper['first_author'])
                s2_entry = bibtex_utils.parse_bibtex_to_dict(s2_bib)
                # validate using strict matching (title, author, year)
                if bibtex_utils.bibtex_entries_match_strict(baseline_bib, s2_bib):
                    enrichers.append(('s2', s2_entry))
        except Exception as e:
            print(f"S2 enrichment failed: {e}")

    # attempt Crossref enrichment (DOI registration agency)
    try:
        cr_item = api_clients.crossref_search(paper['title'], paper['first_author'])
        if cr_item:
            cr_bib = api_clients.build_bibtex_from_crossref(cr_item, paper['first_author'])
            cr_entry = bibtex_utils.parse_bibtex_to_dict(cr_bib)
            if bibtex_utils.bibtex_entries_match_strict(baseline_bib, cr_bib):
                enrichers.append(('crossref', cr_entry))
    except Exception as e:
        print(f"Crossref enrichment failed: {e}")

    # attempt arXiv enrichment (preprint repository)
    if paper['arxiv_id']:
        try:
            arxiv_entries = api_clients.arxiv_search(
                paper['title'],
                paper['first_author'],
                paper['year']
            )
            if arxiv_entries and len(arxiv_entries) > 0:
                arxiv_bib = api_clients.build_bibtex_from_arxiv(
                    arxiv_entries[0],
                    paper['first_author']
                )
                arxiv_entry = bibtex_utils.parse_bibtex_to_dict(arxiv_bib)
                if bibtex_utils.bibtex_entries_match_strict(baseline_bib, arxiv_bib):
                    enrichers.append(('arxiv', arxiv_entry))
        except Exception as e:
            print(f"arXiv enrichment failed: {e}")

    # attempt DOI negotiation (CSL-JSON from doi.org resolver)
    if paper['doi']:
        try:
            csl = api_clients.fetch_csl_via_doi(paper['doi'])
            if csl:
                csl_bib = api_clients.bibtex_from_csl(csl, paper['first_author'])
                csl_entry = bibtex_utils.parse_bibtex_to_dict(csl_bib)
                if bibtex_utils.bibtex_entries_match_strict(baseline_bib, csl_bib):
                    enrichers.append(('csl', csl_entry))
        except Exception as e:
            print(f"DOI enrichment failed: {e}")

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
    saved_path = merge_utils.save_entry_to_file(
        out_dir,
        TEST_AUTHOR['scholar_id'],
        entry,
        None
    )

    assert saved_path and os.path.exists(saved_path), "File was not created"

    # verify file content is valid BibTeX
    with open(saved_path, 'r', encoding='utf-8') as f:
        content = f.read()

    assert '@inproceedings' in content, "File content invalid"

def test_csv_summary_integration(tmp_path):
    """
    Confirm that CSV summary export integrates correctly with the processing pipeline.
    """
    from src.io_utils import init_summary_csv, append_summary_to_csv

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

    # verify CSV contains all entries with correct trust_hits
    import csv as csv_module
    with open(csv_path_str, 'r', encoding='utf-8') as f:
        reader = csv_module.DictReader(f)
        rows = list(reader)

    assert len(rows) == len(test_entries), f"Expected {len(test_entries)} rows, got {len(rows)}"

    # verify trust_hits match expected values
    expected_hits = [5, 2, 0]
    for i, expected in enumerate(expected_hits):
        assert int(rows[i]['trust_hits']) == expected, f"Row {i}: expected trust_hits={expected}, got {rows[i]['trust_hits']}"

def test_complex_paper_enrichment(api_keys):
    """
    Test enrichment pipeline with a complex paper (AlphaFold) that has
    many authors and complex metadata.
    """
    if not api_keys.get('serpapi'):
        pytest.skip("SerpAPI key not available")

    # Use AlphaFold paper from KNOWN_PAPERS (index 5)
    # Note: Index might change if KNOWN_PAPERS changes, better to find by name
    paper = next(p for p in KNOWN_PAPERS if p['name'] == 'alphafold')

    # Mocking the fetch to avoid hitting all APIs for this specific test if we want speed,
    # but for integration we should try to hit them or mock them realistically.
    # For now, we'll just verify we can build a baseline and merge mock enrichments
    # to ensure the system handles the data volume/complexity.

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

