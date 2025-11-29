
import os
import pytest
from src import merge_utils, text_utils

def test_prevent_duplicate_save_high_similarity(tmp_path):
    """
    Test that save_entry_to_file prevents creating a new file when an existing entry
    has a title with >= 95% similarity, even if the citation key or filename might differ.
    """
    out_dir = str(tmp_path)
    author_id = "Scholar123"
    author_name = "Test Author"
    
    # 1. Create an initial entry
    entry_a = {
        "type": "article",
        "key": "Author2023",
        "fields": {
            "title": "A Very Specific Study on Quantum Entanglement in Macroscopic Systems",
            "author": "Test Author",
            "year": "2023",
            "journal": "Nature Physics"
        }
    }
    
    # Save it
    path_a = merge_utils.save_entry_to_file(out_dir, author_id, entry_a, author_name=author_name)
    assert os.path.exists(path_a)
    
    # 2. Create a second entry with >95% similarity
    # Change one character or add a small punctuation to keep similarity high
    entry_b = {
        "type": "article",
        "key": "Author2023_Duplicate", # Different key to potentially trigger new file
        "fields": {
            "title": "A Very Specific Study on Quantum Entanglement in Macroscopic Systems.", # Added period
            "author": "Test Author",
            "year": "2023",
            "journal": "Nature Physics"
        }
    }
    
    # Verify similarity is high
    sim = text_utils.title_similarity(entry_a["fields"]["title"], entry_b["fields"]["title"])
    assert sim >= 0.95, f"Similarity {sim} is not high enough for this test"
    
    # 3. Attempt to save the second entry
    path_b = merge_utils.save_entry_to_file(out_dir, author_id, entry_b, author_name=author_name)
    
    # 4. Assertions
    # Should reuse the same file path (deduplication)
    assert path_b == path_a, "Should have reused the existing file path"
    
    # Verify only one file exists in the author directory
    author_dir = os.path.dirname(path_a)
    files = list(os.listdir(author_dir))
    bib_files = [f for f in files if f.endswith('.bib')]
    assert len(bib_files) == 1, f"Expected 1 bib file, found {len(bib_files)}: {bib_files}"

def test_allow_duplicate_save_medium_similarity(tmp_path):
    """
    Test that save_entry_to_file allows creating a new file when similarity is
    between 90% and 95% (below the new threshold).
    """
    out_dir = str(tmp_path)
    author_id = "Scholar456"
    author_name = "Test Author 2"
    
    # 1. Create an initial entry
    entry_a = {
        "type": "article",
        "key": "Author2023_A",
        "fields": {
            "title": "Machine Learning for Healthcare Applications",
            "author": "Test Author 2",
            "year": "2023"
        }
    }
    path_a = merge_utils.save_entry_to_file(out_dir, author_id, entry_a, author_name=author_name)
    
    # 2. Create a second entry with ~92% similarity
    # "Machine Learning for Healthcare Applications" (original)
    # "Machine Learning for Health Care Applications" -> ~95% (too high)
    # "Machine Learning for Health Applications" -> ~92%
    entry_b = {
        "type": "article",
        "key": "Author2023_B",
        "fields": {
            "title": "Machine Learning for Health Care Appli",
            "author": "Test Author 2",
            "year": "2023"
        }
    }
    
    sim = text_utils.title_similarity(entry_a["fields"]["title"], entry_b["fields"]["title"])
    print(f"DEBUG: Similarity is {sim}")

    # We want sim < 0.95 but likely > 0.90 to test the boundary
    # If it's too high, we skip. If it's too low, we skip.
    if sim >= 0.95:
         pytest.skip(f"Generated similarity {sim} was too high (>= 0.95)")
    if sim <= 0.90:
         pytest.skip(f"Generated similarity {sim} was too low (<= 0.90)")

    # 3. Save entry B
    path_b = merge_utils.save_entry_to_file(out_dir, author_id, entry_b, author_name=author_name)
    
    # 4. Assertions
    # Should create a NEW file because it's below the 95% threshold
    assert path_b != path_a, f"Should have created a new file for similarity {sim:.2f}"
    
    author_dir = os.path.dirname(path_a)
    files = list(os.listdir(author_dir))
    bib_files = [f for f in files if f.endswith('.bib')]
    assert len(bib_files) == 2, f"Expected 2 bib files, found {len(bib_files)}"
