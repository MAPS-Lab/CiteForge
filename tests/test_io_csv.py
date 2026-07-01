from __future__ import annotations

import csv
from pathlib import Path
from textwrap import dedent

from src import io_utils
from src.models import Record

_SOURCE_FLAG_KEYS = [
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
    csv_path = tmp_path / "summary.csv"
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)
    assert csv_path.exists(), "CSV file was not created"

    with open(csv_path, encoding="utf-8") as f:
        header = next(csv.reader(f))

    assert header == ["file_path", "trust_hits", *_SOURCE_FLAG_KEYS], f"Header mismatch: got {len(header)} columns"


def test_csv_append_single_entry(tmp_path: Path) -> None:
    """Confirm that a single appended entry encodes path, trust hits, and flags correctly."""
    csv_path = tmp_path / "summary.csv"
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    file_path = "output/Author/Paper2024.bib"
    trust_hits = 5
    flags = _make_flags(
        scholar_bib=True,
        s2=True,
        crossref=True,
        openalex=True,
        doi_csl=True,
    )

    io_utils.append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    with open(csv_path, encoding="utf-8") as f:
        lines = f.readlines()

    assert len(lines) == 2, f"Expected 2 lines, got {len(lines)}"

    data_row = lines[1].strip().split(",")
    assert data_row[0] == file_path, "File path mismatch"
    assert data_row[1] == str(trust_hits), f"Trust hits mismatch: expected {trust_hits}, got {data_row[1]}"
    assert data_row[2] == "1", "scholar_bib should be 1"
    assert data_row[3] == "0", "scholar_page should be 0"


def test_csv_append_multiple_entries(tmp_path: Path) -> None:
    """Verify that multiple entries can be appended sequentially."""
    csv_path = tmp_path / "summary.csv"
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    test_entries = [
        (
            "output/Author1/Paper2024.bib",
            5,
            {"s2": True, "crossref": True, "doi_csl": True, "openalex": True, "scholar_bib": True},
        ),
        ("output/Author2/Paper2023.bib", 0, {}),
        ("output/Author3/Paper2025.bib", 3, {"arxiv": True, "doi_bibtex": True, "pubmed": True}),
    ]

    for file_path, trust_hits, partial_flags in test_entries:
        flags = _make_flags(**partial_flags)
        io_utils.append_summary_to_csv(csv_path_str, file_path, trust_hits, flags)

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == len(test_entries), f"Expected {len(test_entries)} rows, got {len(rows)}"

    for i, (expected_path, expected_hits, _) in enumerate(test_entries):
        assert rows[i]["file_path"] == expected_path, f"Row {i}: file path mismatch"
        assert int(rows[i]["trust_hits"]) == expected_hits, f"Row {i}: trust hits mismatch"


def test_csv_edge_cases(tmp_path: Path) -> None:
    """Edge cases: very long file paths and special characters."""
    csv_path = tmp_path / "summary.csv"
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    long_path = "output/" + "a" * 200 + "/Paper.bib"
    flags = _make_flags(scholar_bib=True)

    io_utils.append_summary_to_csv(csv_path_str, long_path, 1, flags)

    special_path = "output/Author (ID123)/Paper-2024_v2.bib"
    io_utils.append_summary_to_csv(csv_path_str, special_path, 2, flags)

    with open(csv_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 2, f"Expected 2 rows, got {len(rows)}"
    assert rows[0]["file_path"] == long_path, "Long path not preserved correctly"
    assert rows[1]["file_path"] == special_path, "Special characters not preserved correctly"


def test_csv_directory_creation(tmp_path: Path) -> None:
    """Confirm that init_summary_csv automatically creates parent directories."""
    csv_path = tmp_path / "deep" / "nested" / "path" / "summary.csv"
    csv_path_str = str(csv_path)

    io_utils.init_summary_csv(csv_path_str)

    assert csv_path.exists(), "CSV file was not created in nested directory"
    assert csv_path.parent.exists(), "Parent directory was not created"


# ---------------------------------------------------------------------------
# build_a2i2_folder tests
# ---------------------------------------------------------------------------


def _write_bib(path: Path, entry_type: str, key: str, fields: dict[str, str]) -> None:
    """Write a minimal .bib file for testing."""
    lines = [f"@{entry_type}{{{key},"]
    for k, v in fields.items():
        lines.append(f"  {k} = {{{v}}},")
    lines.append("}")
    path.write_text("\n".join(lines), encoding="utf-8")


def _make_a2i2_csv(path: Path, names: list[str]) -> None:
    """Write a minimal a2i2.csv."""
    rows = ["Name,Scholar Link,DBLP Link"]
    for n in names:
        rows.append(f"{n},,")
    path.write_text("\n".join(rows), encoding="utf-8")


def _make_records(names_and_ids: list[tuple[str, str]]) -> list[Record]:
    """Create Record objects for testing."""
    return [Record(name=n, scholar_id=sid) for n, sid in names_and_ids]


class TestBuildA2i2Folder:
    """Tests for the automated a2i2 build step."""

    def test_missing_csv_returns_zero(self, tmp_path: Path) -> None:
        result = io_utils.build_a2i2_folder(str(tmp_path / "nonexistent.csv"), [], str(tmp_path / "output"))
        assert result == 0

    def test_basic_collection(self, tmp_path: Path) -> None:
        out = tmp_path / "output"
        author_dir = out / "Smith (A1)"
        author_dir.mkdir(parents=True)
        _write_bib(
            author_dir / "Alice2024-TestPaper.bib",
            "article",
            "Alice2024:TestPaper",
            {
                "title": "A Test Paper on Neural Networks",
                "author": "Alice Smith",
                "year": "2024",
                "journal": "Nature",
            },
        )

        csv_path = tmp_path / "a2i2.csv"
        _make_a2i2_csv(csv_path, ["Alice Smith"])
        records = _make_records([("Alice Smith", "A1")])

        count = io_utils.build_a2i2_folder(str(csv_path), records, str(out))
        assert count == 1
        assert (out / "a2i2" / "Alice2024-TestPaper.bib").exists()

    def test_year_filtering(self, tmp_path: Path) -> None:
        out = tmp_path / "output"
        author_dir = out / "Jones (B1)"
        author_dir.mkdir(parents=True)
        _write_bib(
            author_dir / "Bob2024-Recent.bib",
            "article",
            "Bob2024:Recent",
            {
                "title": "Recent Work",
                "author": "Bob Jones",
                "year": "2024",
                "journal": "Science",
            },
        )
        _write_bib(
            author_dir / "Bob2005-Old.bib",
            "article",
            "Bob2005:Old",
            {
                "title": "Old Work",
                "author": "Bob Jones",
                "year": "2005",
                "journal": "Science",
            },
        )

        csv_path = tmp_path / "a2i2.csv"
        _make_a2i2_csv(csv_path, ["Bob Jones"])
        records = _make_records([("Bob Jones", "B1")])

        count = io_utils.build_a2i2_folder(str(csv_path), records, str(out))
        assert count == 1
        assert (out / "a2i2" / "Bob2024-Recent.bib").exists()
        assert not (out / "a2i2" / "Bob2005-Old.bib").exists()

    def test_dedup_by_doi(self, tmp_path: Path) -> None:
        out = tmp_path / "output"
        dir_a = out / "Smith (A1)"
        dir_b = out / "Jones (B1)"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        # Alice has 3 fields, Bob has 4 — Bob's version is richer
        _write_bib(
            dir_a / "Smith2024-SharedPaper.bib",
            "article",
            "Smith2024:SharedPaper",
            {
                "title": "A Shared Paper",
                "author": "Alice Smith and Bob Jones",
                "year": "2024",
                "doi": "10.1234/shared",
            },
        )
        _write_bib(
            dir_b / "Smith2024-SharedPaper.bib",
            "article",
            "Smith2024:SharedPaper",
            {
                "title": "A Shared Paper",
                "author": "Alice Smith and Bob Jones",
                "year": "2024",
                "doi": "10.1234/shared",
                "journal": "Nature",
            },
        )

        csv_path = tmp_path / "a2i2.csv"
        _make_a2i2_csv(csv_path, ["Alice Smith", "Bob Jones"])
        records = _make_records([("Alice Smith", "A1"), ("Bob Jones", "B1")])

        count = io_utils.build_a2i2_folder(str(csv_path), records, str(out))
        assert count == 1

        # Verify the richer version was kept (has journal field)
        content = (out / "a2i2" / "Smith2024-SharedPaper.bib").read_text()
        assert "Nature" in content

    def test_dedup_by_title(self, tmp_path: Path) -> None:
        out = tmp_path / "output"
        dir_a = out / "Smith (A1)"
        dir_b = out / "Jones (B1)"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        # Same title, no DOI — should dedup by title similarity
        _write_bib(
            dir_a / "Smith2024-NeuralNet.bib",
            "article",
            "Smith2024:NeuralNet",
            {
                "title": "Deep Neural Networks for Image Classification",
                "author": "Alice Smith",
                "year": "2024",
            },
        )
        _write_bib(
            dir_b / "Smith2024-NeuralNet.bib",
            "article",
            "Smith2024:NeuralNet",
            {
                "title": "Deep Neural Networks for Image Classification",
                "author": "Alice Smith and Bob Jones",
                "year": "2024",
                "journal": "CVPR",
            },
        )

        csv_path = tmp_path / "a2i2.csv"
        _make_a2i2_csv(csv_path, ["Alice Smith", "Bob Jones"])
        records = _make_records([("Alice Smith", "A1"), ("Bob Jones", "B1")])

        count = io_utils.build_a2i2_folder(str(csv_path), records, str(out))
        assert count == 1

    def test_complete_rebuild(self, tmp_path: Path) -> None:
        out = tmp_path / "output"
        a2i2_dir = out / "a2i2"
        a2i2_dir.mkdir(parents=True)
        # Plant a stale file
        (a2i2_dir / "Stale2020-OldPaper.bib").write_text("@misc{old, title={Old}}")

        author_dir = out / "Smith (A1)"
        author_dir.mkdir(parents=True)
        _write_bib(
            author_dir / "Alice2024-Fresh.bib",
            "article",
            "Alice2024:Fresh",
            {
                "title": "Fresh Paper",
                "author": "Alice Smith",
                "year": "2024",
                "journal": "Nature",
            },
        )

        csv_path = tmp_path / "a2i2.csv"
        _make_a2i2_csv(csv_path, ["Alice Smith"])
        records = _make_records([("Alice Smith", "A1")])

        count = io_utils.build_a2i2_folder(str(csv_path), records, str(out))
        assert count == 1
        assert not (a2i2_dir / "Stale2020-OldPaper.bib").exists()
        assert (a2i2_dir / "Alice2024-Fresh.bib").exists()

    def test_deterministic_output(self, tmp_path: Path) -> None:
        out = tmp_path / "output"
        dir_a = out / "Smith (A1)"
        dir_b = out / "Jones (B1)"
        dir_a.mkdir(parents=True)
        dir_b.mkdir(parents=True)

        _write_bib(
            dir_a / "Alice2024-Paper.bib",
            "article",
            "Alice2024:Paper",
            {
                "title": "Alice Paper",
                "author": "Alice Smith",
                "year": "2024",
                "journal": "Nature",
            },
        )
        _write_bib(
            dir_b / "Bob2024-Paper.bib",
            "article",
            "Bob2024:Paper",
            {
                "title": "Bob Paper",
                "author": "Bob Jones",
                "year": "2024",
                "journal": "Science",
            },
        )

        csv_path = tmp_path / "a2i2.csv"
        _make_a2i2_csv(csv_path, ["Alice Smith", "Bob Jones"])
        records = _make_records([("Alice Smith", "A1"), ("Bob Jones", "B1")])

        io_utils.build_a2i2_folder(str(csv_path), records, str(out))
        files_1 = sorted((out / "a2i2").iterdir())
        contents_1 = [f.read_text() for f in files_1]

        io_utils.build_a2i2_folder(str(csv_path), records, str(out))
        files_2 = sorted((out / "a2i2").iterdir())
        contents_2 = [f.read_text() for f in files_2]

        assert [f.name for f in files_1] == [f.name for f in files_2]
        assert contents_1 == contents_2
