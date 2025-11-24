import csv
import os
from pathlib import Path
from textwrap import dedent
import pytest
from CiteForge import io_utils

def test_read_records_from_csv(tmp_path):
    """
    Test reading author records from CSV.
    """
    csv_path = tmp_path / "test.csv"
    csv_path_str = str(csv_path)

    # Write test CSV
    csv_content = dedent("""
        Name,Email,Scholar,ORCID,DBLP
        Ashish Vaswani,avaswani@google.com,Scholar123,0000-0001-2345-6789,vaswani/a
        Noam Shazeer,noam@google.com,Scholar456,,
        ,,,Scholar789,,
        InvalidRow,,,,
    """).strip()
    io_utils.safe_write_file(csv_path_str, csv_content)

    # Read records
    records = io_utils.read_records(csv_path_str)

    # Should only have 2 valid records (Scholar123 and Scholar456)
    # Scholar789 has no name, InvalidRow has no Scholar ID
    assert len(records) == 2, f"Expected 2 records, got {len(records)}"

    # Verify first record
    assert records[0].name == "Ashish Vaswani"
    assert records[0].scholar_id == "Scholar123"

    # Verify empty records are filtered
    for r in records:
        assert r.scholar_id, "Records without Scholar ID should be filtered"

def test_csv_initialization(tmp_path):
    """
    Verify that CSV summary initialization creates a file with the correct
    header structure.
    """
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    assert csv_path.exists(), "CSV file was not created"

    # verify header matches the 13-column format required by main.py
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)

    # expected format: file_path, trust_hits, then 11 source flags
    expected_columns = [
        'file_path', 'trust_hits', 'scholar_bib', 'scholar_page',
        's2', 'crossref', 'openreview', 'arxiv', 'openalex',
        'pubmed', 'europepmc', 'doi_csl', 'doi_bibtex'
    ]

    assert header == expected_columns, \
        f"Header mismatch. Expected {len(expected_columns)} columns, got {len(header)}"

def test_csv_append_single_entry(tmp_path):
    """
    Confirm that appending a single entry correctly encodes the file path,
    trust hit count, and boolean source flags into CSV format.
    """
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    # simulate entry enriched by 5 sources
    file_path = "output/Author/Paper2024.bib"
    trust_hits = 5
    flags = {
        'scholar_bib': True,
        'scholar_page': False,
        's2': True,
        'crossref': True,
        'openreview': False,
        'arxiv': False,
        'openalex': True,
        'pubmed': False,
        'europepmc': False,
        'doi_csl': True,
        'doi_bibtex': False,
    }

    io_utils.append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    # verify CSV contains header + 1 data row
    with open(csv_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

    # verify data row encoding
    data_row = lines[1].strip().split(',')

    assert data_row[0] == file_path, "File path mismatch"
    assert data_row[1] == str(trust_hits), f"Trust hits mismatch: expected {trust_hits}, got {data_row[1]}"

    # verify boolean flags encoded as 1/0
    assert data_row[2] == '1', "scholar_bib should be 1"
    assert data_row[3] == '0', "scholar_page should be 0"

def test_csv_append_multiple_entries(tmp_path):
    """
    Verify that multiple entries can be appended sequentially.
    """
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    # simulate 3 entries
    test_entries = [
        (
            "output/Author1/Paper2024.bib", 5,
            {'s2': True, 'crossref': True, 'doi_csl': True,
             'openalex': True, 'scholar_bib': True}
        ),
        ("output/Author2/Paper2023.bib", 0, {}),  # zero enrichment case
        ("output/Author3/Paper2025.bib", 3, {'arxiv': True, 'doi_bibtex': True, 'pubmed': True}),
    ]

    for file_path, trust_hits, partial_flags in test_entries:
        flags = {
            'scholar_bib': False, 'scholar_page': False, 's2': False,
            'crossref': False, 'openreview': False, 'arxiv': False,
            'openalex': False, 'pubmed': False, 'europepmc': False,
            'doi_csl': False, 'doi_bibtex': False,
        }
        flags.update(partial_flags)
        io_utils.append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    # verify all entries recorded correctly
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == len(test_entries), f"Expected {len(test_entries)} rows, got {len(rows)}"

    # verify each entry's file path and trust hit count
    for i, (expected_path, expected_hits, _) in enumerate(test_entries):
        assert rows[i]['file_path'] == expected_path, f"Row {i}: file path mismatch"
        assert int(rows[i]['trust_hits']) == expected_hits, f"Row {i}: trust hits mismatch"

    # verify zero enrichment entry
    assert int(rows[1]['trust_hits']) == 0, "Zero enrichment entry not handled correctly"

def test_csv_edge_cases(tmp_path):
    """
    Validate robustness against edge cases including very long file paths
    and special characters.
    """
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    # test with very long path
    long_path = "output/" + "a" * 200 + "/Paper.bib"
    flags = {'scholar_bib': True}
    flags_complete = {k: flags.get(k, False) for k in [
        'scholar_bib', 'scholar_page', 's2', 'crossref', 'openreview',
        'arxiv', 'openalex', 'pubmed', 'europepmc', 'doi_csl', 'doi_bibtex'
    ]}

    io_utils.append_summary_to_csv(csv_path_str, long_path, 1, flags_complete)

    # test with special characters
    special_path = "output/Author (ID123)/Paper-2024_v2.bib"
    io_utils.append_summary_to_csv(csv_path_str, special_path, 2, flags_complete)

    # verify both paths preserved exactly
    with open(csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    assert rows[0]['file_path'] == long_path, "Long path not preserved correctly"
    assert rows[1]['file_path'] == special_path, "Special characters not preserved correctly"

def test_csv_directory_creation(tmp_path):
    """
    Confirm that init_summary_csv automatically creates parent directories.
    """
    # use nested path that doesn't exist
    csv_path = tmp_path / 'deep' / 'nested' / 'path' / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    assert csv_path.exists(), "CSV file was not created in nested directory"
    assert csv_path.parent.exists(), "Parent directory was not created"
