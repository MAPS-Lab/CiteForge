from __future__ import annotations

import csv
from pathlib import Path
from textwrap import dedent

from src import io_utils

_SOURCE_FLAG_KEYS = [
    'scholar_bib', 'scholar_page', 's2', 'crossref', 'openreview',
    'arxiv', 'openalex', 'pubmed', 'europepmc', 'doi_csl', 'doi_bibtex',
]


def _make_flags(**overrides: bool) -> dict[str, bool]:
    """Build a complete source-flags dict with all flags defaulting to False."""
    flags = dict.fromkeys(_SOURCE_FLAG_KEYS, False)
    flags.update(overrides)
    return flags


def test_read_records_from_csv(tmp_path: Path) -> None:
    """Test reading author records from CSV."""
    csv_path = tmp_path / "test.csv"
    csv_path_str = str(csv_path)

    csv_content = dedent("""
        Name,Scholar Link,DBLP Link
        Ashish Vaswani,https://scholar.google.com/citations?user=Scholar123,https://dblp.org/pid/vaswani/a
        Noam Shazeer,https://scholar.google.com/citations?user=Scholar456,
        ,https://scholar.google.com/citations?user=Scholar789,
        InvalidRow,,
    """).strip()
    io_utils.safe_write_file(csv_path_str, csv_content)

    records = io_utils.read_records(csv_path_str)

    # Scholar789 has empty Name (skipped); InvalidRow has no IDs (filtered)
    assert len(records) == 2, f"Expected 2 records, got {len(records)}"

    assert records[0].name == "Ashish Vaswani"
    assert records[0].scholar_id == "Scholar123"
    assert records[0].dblp == "vaswani/a"

    for r in records:
        assert r.scholar_id or r.dblp, "Records without any ID should be filtered"


def test_csv_initialization(tmp_path: Path) -> None:
    """Verify CSV summary initialization creates a file with the correct header."""
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)
    assert csv_path.exists(), "CSV file was not created"

    with open(csv_path, encoding='utf-8') as f:
        reader = csv.reader(f)
        header = next(reader)

    expected_columns = ['file_path', 'trust_hits', *_SOURCE_FLAG_KEYS]
    assert header == expected_columns, (
        f"Header mismatch. Expected {len(expected_columns)} columns, got {len(header)}"
    )


def test_csv_append_single_entry(tmp_path: Path) -> None:
    """Confirm that a single appended entry encodes path, trust hits, and flags correctly."""
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    file_path = "output/Author/Paper2024.bib"
    trust_hits = 5
    flags = _make_flags(
        scholar_bib=True, s2=True, crossref=True, openalex=True, doi_csl=True,
    )

    io_utils.append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    with open(csv_path, encoding='utf-8') as f:
        lines = f.readlines()

    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

    data_row = lines[1].strip().split(',')
    assert data_row[0] == file_path, "File path mismatch"
    assert data_row[1] == str(trust_hits), (
        f"Trust hits mismatch: expected {trust_hits}, got {data_row[1]}"
    )
    assert data_row[2] == '1', "scholar_bib should be 1"
    assert data_row[3] == '0', "scholar_page should be 0"


def test_csv_append_multiple_entries(tmp_path: Path) -> None:
    """Verify that multiple entries can be appended sequentially."""
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    test_entries = [
        (
            "output/Author1/Paper2024.bib", 5,
            {'s2': True, 'crossref': True, 'doi_csl': True,
             'openalex': True, 'scholar_bib': True},
        ),
        ("output/Author2/Paper2023.bib", 0, {}),
        ("output/Author3/Paper2025.bib", 3, {'arxiv': True, 'doi_bibtex': True, 'pubmed': True}),
    ]

    for file_path, trust_hits, partial_flags in test_entries:
        flags = _make_flags(**partial_flags)
        io_utils.append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    with open(csv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == len(test_entries), f"Expected {len(test_entries)} rows, got {len(rows)}"

    for i, (expected_path, expected_hits, _) in enumerate(test_entries):
        assert rows[i]['file_path'] == expected_path, f"Row {i}: file path mismatch"
        assert int(rows[i]['trust_hits']) == expected_hits, f"Row {i}: trust hits mismatch"

    assert int(rows[1]['trust_hits']) == 0, "Zero enrichment entry not handled correctly"


def test_csv_edge_cases(tmp_path: Path) -> None:
    """Edge cases: very long file paths and special characters."""
    csv_path = tmp_path / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    long_path = "output/" + "a" * 200 + "/Paper.bib"
    flags = _make_flags(scholar_bib=True)

    io_utils.append_summary_to_csv(csv_path_str, long_path, 1, flags)

    special_path = "output/Author (ID123)/Paper-2024_v2.bib"
    io_utils.append_summary_to_csv(csv_path_str, special_path, 2, flags)

    with open(csv_path, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    assert rows[0]['file_path'] == long_path, "Long path not preserved correctly"
    assert rows[1]['file_path'] == special_path, "Special characters not preserved correctly"


def test_csv_directory_creation(tmp_path: Path) -> None:
    """Confirm that init_summary_csv automatically creates parent directories."""
    csv_path = tmp_path / 'deep' / 'nested' / 'path' / 'summary.csv'
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    assert csv_path.exists(), "CSV file was not created in nested directory"
    assert csv_path.parent.exists(), "Parent directory was not created"
