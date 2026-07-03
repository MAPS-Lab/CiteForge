from __future__ import annotations

import os
from pathlib import Path

import pytest

from citeforge import merge_utils, text_utils
from citeforge.id_utils import doi_bases_match


def _count_bib_files(directory: str) -> int:
    """Count .bib files in a directory."""
    return sum(1 for f in os.listdir(directory) if f.endswith(".bib"))


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
    assert _count_bib_files(os.path.dirname(path_a)) == 1


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
    assert _count_bib_files(os.path.dirname(path_a)) == 2


# --- DOI version matching ---


def test_doi_bases_match_preprints_org_versions() -> None:
    """Preprints.org DOIs with .v1/.v2 suffixes must be recognized as the same work."""
    assert doi_bases_match(
        "10.20944/preprints202304.0409.v1",
        "10.20944/preprints202304.0409.v2",
    )


def test_doi_bases_match_different_dois() -> None:
    """Completely different DOIs must NOT match."""
    assert not doi_bases_match("10.1016/j.ins.2020.09.024", "10.48550/arxiv.1909.04605")


def test_doi_bases_match_same_without_version() -> None:
    """DOIs without version suffixes only match if identical."""
    assert doi_bases_match("10.1234/paper.2024", "10.1234/paper.2024")
    assert not doi_bases_match("10.1234/paper.2024", "10.1234/other.2024")


def test_doi_version_dedup_in_save(tmp_path: Path) -> None:
    """save_entry_to_file should deduplicate DOI version variants (e.g. v1/v2)."""
    out_dir = str(tmp_path)
    author_id = "TestAuthor"
    author_name = "Test Author"

    v1 = {
        "type": "misc",
        "key": "Author2023:EthicalFrontier",
        "fields": {
            "title": "The Ethical Frontier: Navigating the Metaverse in Modern Farming",
            "author": "Test Author",
            "year": "2023",
            "doi": "10.20944/preprints202304.0409.v1",
        },
    }
    path_v1, _ = merge_utils.save_entry_to_file(out_dir, author_id, v1, author_name=author_name)
    assert os.path.exists(path_v1)

    v2 = {
        "type": "misc",
        "key": "Author2023:HarnessingMetaverse",
        "fields": {
            "title": "Harnessing the Metaverse for Livestock Welfare: Unleashing Sensor Data",
            "author": "Test Author",
            "year": "2023",
            "doi": "10.20944/preprints202304.0409.v2",
        },
    }
    path_v2, _ = merge_utils.save_entry_to_file(out_dir, author_id, v2, author_name=author_name)

    assert path_v2 == path_v1, "v2 should be matched to v1 via DOI version matching"
    assert _count_bib_files(os.path.dirname(path_v1)) == 1
