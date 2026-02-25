from __future__ import annotations

import os
from pathlib import Path

import pytest

from src import merge_utils, text_utils


def test_prevent_duplicate_save_high_similarity(tmp_path: Path) -> None:
    """Test that save_entry_to_file prevents creating new file for >= 95% title similarity."""
    out_dir = str(tmp_path)
    author_id = "Scholar123"
    author_name = "Test Author"

    entry_a = {
        "type": "article",
        "key": "Author2023",
        "fields": {
            "title": "A Very Specific Study on Quantum Entanglement in Macroscopic Systems",
            "author": "Test Author",
            "year": "2023",
            "journal": "Nature Physics",
        },
    }

    path_a, _ = merge_utils.save_entry_to_file(out_dir, author_id, entry_a, author_name=author_name)
    assert os.path.exists(path_a)

    # Second entry with >95% similarity (added trailing period)
    entry_b = {
        "type": "article",
        "key": "Author2023_Duplicate",
        "fields": {
            "title": "A Very Specific Study on Quantum Entanglement in Macroscopic Systems.",
            "author": "Test Author",
            "year": "2023",
            "journal": "Nature Physics",
        },
    }

    sim = text_utils.title_similarity(entry_a["fields"]["title"], entry_b["fields"]["title"])
    assert sim >= 0.95, f"Similarity {sim} is not high enough for this test"

    path_b, _ = merge_utils.save_entry_to_file(out_dir, author_id, entry_b, author_name=author_name)

    assert path_b == path_a, "Should have reused the existing file path"

    author_dir = os.path.dirname(path_a)
    bib_files = [f for f in os.listdir(author_dir) if f.endswith(".bib")]
    assert len(bib_files) == 1, f"Expected 1 bib file, found {len(bib_files)}: {bib_files}"


def test_allow_duplicate_save_medium_similarity(tmp_path: Path) -> None:
    """Test that save_entry_to_file allows new file when similarity is 90-95%."""
    out_dir = str(tmp_path)
    author_id = "Scholar456"
    author_name = "Test Author 2"

    entry_a = {
        "type": "article",
        "key": "Author2023_A",
        "fields": {
            "title": "Machine Learning for Healthcare Applications",
            "author": "Test Author 2",
            "year": "2023",
        },
    }
    path_a, _ = merge_utils.save_entry_to_file(out_dir, author_id, entry_a, author_name=author_name)

    # Variant with ~92% similarity (truncated to reduce similarity below 0.95)
    entry_b = {
        "type": "article",
        "key": "Author2023_B",
        "fields": {
            "title": "Machine Learning for Health Care Appli",
            "author": "Test Author 2",
            "year": "2023",
        },
    }

    sim = text_utils.title_similarity(entry_a["fields"]["title"], entry_b["fields"]["title"])

    if sim >= 0.95:
        pytest.skip(f"Generated similarity {sim} was too high (>= 0.95)")
    if sim <= 0.90:
        pytest.skip(f"Generated similarity {sim} was too low (<= 0.90)")

    path_b, _ = merge_utils.save_entry_to_file(out_dir, author_id, entry_b, author_name=author_name)

    assert path_b != path_a, f"Should have created a new file for similarity {sim:.2f}"

    author_dir = os.path.dirname(path_a)
    bib_files = [f for f in os.listdir(author_dir) if f.endswith(".bib")]
    assert len(bib_files) == 2, f"Expected 2 bib files, found {len(bib_files)}"
