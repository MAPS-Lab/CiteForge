import os
import sys
from pathlib import Path
from textwrap import dedent
import pytest
from src import text_utils, id_utils, config, bibtex_utils as bt, io_utils, merge_utils
from src import exceptions, http_utils
from src.models import Record

# ===== TEXT NORMALIZATION =====

def test_title_normalization():
    """
    Test title normalization with all variations.
    """
    test_cases = [
        # Basic
        ("Attention Is All You Need", "attention is all you need"),
        ("Deep Residual Learning for Image Recognition", "deep residual learning for image recognition"),
        ("BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding", "bert pre training of deep bidirectional transformers for language understanding"),
        ("Title   Spaces", "title spaces"),
        # LaTeX
        ("Analysis of $\\phi$ distribution", "analysis of distribution"),
        ("\\textbf{Bold Title}", "bold title"),
        ("\\emph{Text} here", "text here"),
        # Accents
        ("Café Society", "cafe society"),
        ("Naïve Bayes", "naive bayes"),
        # Complex/Edge Cases
        ("On the $\\sqrt{2}$ approximation", "on the 2 approximation"),
        ("A very long title that goes on and on", "a very long title that goes on and on"),
        # Empty
        ("", ""),
        (None, ""),
    ]

    for input_val, expected in test_cases:
        output = text_utils.normalize_title(input_val)
        assert output == expected, f"Expected '{expected}', got '{output}'"

def test_title_similarity():
    """
    Test title similarity scoring.
    """
    test_cases = [
        ("Attention Is All You Need", "Attention Is All You Need", True),
        ("Attention Is All You Need", "attention is all you need", True),
        ("Deep Learning", "Machine Learning", False),
    ]

    for title1, title2, should_be_similar in test_cases:
        score = text_utils.title_similarity(title1, title2)
        is_similar = score >= 0.8
        assert is_similar == should_be_similar, f"Expected similarity {should_be_similar}, got score {score}"

# ===== AUTHOR PARSING =====

def test_author_parsing():
    """
    Test author parsing with all formats.
    """
    test_cases = [
        ("Ashish Vaswani and Noam Shazeer", ["Ashish Vaswani", "Noam Shazeer"]),
        ("Kaiming He; Xiangyu Zhang", ["Kaiming He", "Xiangyu Zhang"]),
        ("J Devlin, M Chang", ["J Devlin", "M Chang"]),
        ("Vaswani, Ashish", ["Vaswani, Ashish"]),
        ("Hinton, LeCun, Bengio", ["Hinton", "LeCun", "Bengio"]),
        # Complex/Edge Cases
        ("Jürgen Müller; François Dubois", ["Jürgen Müller", "François Dubois"]),
        ("Georges Aad et al.", ["Georges Aad", "et al."]),
        ("", []),
        (None, []),
    ]

    for input_val, expected in test_cases:
        output = text_utils.extract_authors_from_any(input_val)
        assert output == expected, f"Expected {expected}, got {output}"

def test_author_matching():
    """
    Test author name matching with initials.
    """
    test_cases = [
        ("Ashish Vaswani", "Ashish Vaswani", True),
        ("ASHISH VASWANI", "ashish vaswani", True),
        ("Geoffrey Hinton", "G Hinton", True),
        ("Kaiming He", "K He", True),
        ("Ashish Vaswani", "A Vaswani", True),
        ("Ashish Vaswani", "Noam Shazeer", False),
        ("A Vaswani", "B Vaswani", False),
    ]

    for name1, name2, should_match in test_cases:
        matches = text_utils.author_name_matches(name1, name2)
        assert matches == should_match, f"Expected match {should_match} for '{name1}' vs '{name2}'"

def test_authors_overlap():
    """
    Test author list overlap detection.
    """
    test_cases = [
        ("Ashish Vaswani and Noam Shazeer", "Ashish Vaswani", True),
        ("Hinton; LeCun; Bengio", "LeCun", True),
        ("Ashish Vaswani", "Noam Shazeer", False),
        ("", "", False),
    ]

    for authors1, authors2, should_overlap in test_cases:
        overlap = text_utils.authors_overlap(authors1, authors2)
        assert overlap == should_overlap, f"Expected overlap {should_overlap} for '{authors1}' vs '{authors2}'"

# ===== ID EXTRACTION =====

def test_doi_extraction():
    """
    Test DOI extraction and normalization.
    """
    test_cases = [
        ("https://doi.org/10.18653/v1/N19-1423", "10.18653/v1/n19-1423"),
        ("doi:10.1234/TEST", "10.1234/test"),
        ('<meta name="citation_doi" content="10.18653/v1/N19-1423" />', "10.18653/v1/n19-1423"),
        ("  10.1234/TEST  ", "10.1234/test"),
        ("", None),
        (None, None),
    ]

    for input_val, expected in test_cases:
        if input_val and '<meta' in str(input_val):
            output = id_utils.find_doi_in_html(input_val)
        elif input_val and 'doi' in input_val:
            output = id_utils.find_doi_in_text(input_val)
        else:
            output = id_utils.normalize_doi(input_val)

        assert output == expected, f"Expected '{expected}', got '{output}'"

def test_arxiv_extraction():
    """
    Test arXiv ID extraction.
    """
    test_cases = [
        ("See arXiv:1706.03762 for details", "1706.03762"),
        ("https://arxiv.org/abs/1706.03762v5", "1706.03762"),
        ("arxiv.org/abs/1706.03762", "1706.03762"),
        ("", None),
    ]

    for input_val, expected in test_cases:
        output = id_utils.find_arxiv_in_text(input_val) if input_val else None
        assert output == expected, f"Expected '{expected}', got '{output}'"

# ===== BIBTEX PARSING =====

def test_bibtex_parsing():
    """
    Test BibTeX parsing.
    """
    valid_cases = [
        (dedent("""
            @inproceedings{Vaswani2017,
              title = {Attention Is All You Need},
              author = {Ashish Vaswani},
              year = {2017}
            }
        """).strip(), {'type': 'inproceedings', 'key': 'Vaswani2017'}),
        (dedent("""
            @article{He2016,
              title = {Deep Residual Learning for Image Recognition},
              author = {Kaiming He and Xiangyu Zhang},
              year = {2016},
              journal = {CVPR}
            }
        """).strip(), {'type': 'article', 'key': 'He2016'}),
    ]

    for bibtex_str, expected_keys in valid_cases:
        parsed = bt.parse_bibtex_to_dict(bibtex_str)
        assert parsed is not None, "Parsing failed for valid BibTeX"
        for key, expected_val in expected_keys.items():
            assert parsed.get(key) == expected_val, f"Expected field '{key}' to be '{expected_val}'"

    # Invalid cases
    for invalid_bib in ["", "invalid bibtex"]:
        try:
            parsed = bt.parse_bibtex_to_dict(invalid_bib)
            assert parsed is None, "Expected None for invalid BibTeX"
        except (ValueError, TypeError, KeyError, AttributeError, IndexError):
            pytest.fail("Should not crash on invalid input")

def test_bibtex_building():
    """
    Test BibTeX construction.
    """
    # Minimal BibTeX
    bibtex = bt.build_minimal_bibtex("Attention Is All You Need", ["Ashish Vaswani", "Noam Shazeer"], 2017, keyhint="Vaswani2017")
    assert bibtex and '@' in bibtex, "No valid BibTeX returned"

    # Verify roundtrip
    parsed = bt.parse_bibtex_to_dict(bibtex)
    assert parsed and 'title' in parsed.get('fields', {}), "Parsing built BibTeX failed"

    # Dict to BibTeX
    entry = {
        'type': 'article', 'key': 'Vaswani2017',
        'fields': {'title': 'Attention Is All You Need', 'author': 'Ashish Vaswani', 'year': '2017'}
    }
    bibtex2 = bt.bibtex_from_dict(entry)
    assert bibtex2 and '@article' in bibtex2, "Invalid BibTeX from dict"


def test_bibtex_latex_stripping():
    """
    Test LaTeX formatting removal in BibTeX output.
    Tests the _strip_latex_formatting function indirectly via bibtex_from_dict.
    """
    import re

    def extract_field(bibtex_str, field_name):
        """Helper to extract a field value from BibTeX output (handles nested braces)."""
        # Find the field start
        pattern = rf'{field_name}\s*=\s*\{{'
        match = re.search(pattern, bibtex_str)
        if not match:
            return None
        # Extract content with balanced braces
        start = match.end() - 1  # Position of opening brace
        depth = 0
        for i in range(start, len(bibtex_str)):
            if bibtex_str[i] == '{':
                depth += 1
            elif bibtex_str[i] == '}':
                depth -= 1
                if depth == 0:
                    return bibtex_str[start + 1:i]
        return None

    # Test cases: (input_title, expected_title)
    test_cases = [
        # Basic formatting commands
        (r"\textit{Machine Learning} for NLP", "Machine Learning for NLP"),
        (r"\textbf{Deep} Neural Networks", "Deep Neural Networks"),
        (r"\emph{Important} Findings", "Important Findings"),
        (r"\textsc{Small Caps} Text", "Small Caps Text"),
        (r"\texttt{Monospace} Code", "Monospace Code"),
        (r"\textrm{Roman} Text", "Roman Text"),
        (r"\textsf{Sans Serif} Font", "Sans Serif Font"),
        (r"\underline{Underlined} Word", "Underlined Word"),
        (r"\mbox{No Break}", "No Break"),

        # Old-style LaTeX formatting
        (r"{\it Italic} text here", "Italic text here"),
        (r"{\bf Bold} text here", "Bold text here"),
        (r"{\em Emphasized} text", "Emphasized text"),
        (r"{\sc Small Caps} style", "Small Caps style"),
        (r"{\tt Typewriter} font", "Typewriter font"),
        (r"{\rm Roman} font", "Roman font"),
        (r"{\sf Sans} font", "Sans font"),

        # Nested formatting commands
        (r"\textbf{\textit{Nested}} formatting", "Nested formatting"),
        (r"\emph{\textbf{Double}} nested", "Double nested"),

        # Special escaped characters
        (r"Research \& Development", "Research & Development"),
        (r"50\% Improvement", "50% Improvement"),
        (r"Price is \$100", "Price is $100"),
        (r"Item \#1", "Item #1"),
        (r"Under\_score", "Under_score"),
        (r"Curly \{brace\}", "Curly {brace}"),

        # Dashes
        ("Long---dash", "Long-dash"),
        ("Medium--dash", "Medium-dash"),
        ("En---and em--dashes together", "En-and em-dashes together"),

        # Tilde (non-breaking space)
        # Note: trailing period is stripped by _sanitize_title for titles
        ("Smith~et~al.", "Smith et al"),

        # Combined cases
        (r"\textit{Deep Learning}---A \textbf{Survey}", "Deep Learning-A Survey"),
        (r"The \emph{Art} of \textbf{Programming}: 50\% Complete", "The Art of Programming: 50% Complete"),

        # Edge cases - no LaTeX (should pass through unchanged)
        ("Plain text title", "Plain text title"),
        ("Title with: colon and punctuation!", "Title with: colon and punctuation!"),

        # Multiple spaces should be collapsed
        (r"\textit{Word}   multiple   spaces", "Word multiple spaces"),
    ]

    for input_title, expected_title in test_cases:
        entry = {"type": "article", "key": "test", "fields": {"title": input_title}}
        result = bt.bibtex_from_dict(entry)
        actual_title = extract_field(result, "title")

        assert actual_title == expected_title, (
            f"LaTeX stripping failed:\n"
            f"  Input:    {input_title!r}\n"
            f"  Expected: {expected_title!r}\n"
            f"  Got:      {actual_title!r}"
        )


def test_bibtex_unicode_normalization():
    """
    Test Unicode to ASCII normalization in BibTeX output.
    Tests the _normalize_to_ascii function indirectly via bibtex_from_dict.
    """
    import re

    def extract_field(bibtex_str, field_name):
        """Helper to extract a field value from BibTeX output (handles nested braces)."""
        pattern = rf'{field_name}\s*=\s*\{{'
        match = re.search(pattern, bibtex_str)
        if not match:
            return None
        start = match.end() - 1
        depth = 0
        for i in range(start, len(bibtex_str)):
            if bibtex_str[i] == '{':
                depth += 1
            elif bibtex_str[i] == '}':
                depth -= 1
                if depth == 0:
                    return bibtex_str[start + 1:i]
        return None

    # Test cases: (input_value, expected_value)
    test_cases = [
        # Accented characters (via unidecode)
        ("Café Society", "Cafe Society"),
        ("Naïve Bayes", "Naive Bayes"),
        ("José García", "Jose Garcia"),
        ("Müller and Schröder", "Muller and Schroder"),
        ("François Dubois", "Francois Dubois"),
        ("Jørgen Ødegård", "Jorgen Odegard"),
        ("Łukasz Kowalski", "Lukasz Kowalski"),

        # Nordic characters
        ("Søren Kierkegaard", "Soren Kierkegaard"),
        ("Bjørn Borg", "Bjorn Borg"),
        ("Ærodynamics", "AErodynamics"),

        # Unicode quotation marks
        ("It\u2019s a \u201Ctest\u201D", "It's a \"test\""),
        ("\u2018Single\u2019 quotes", "'Single' quotes"),
        ("\u201CDouble\u201D quotes", "\"Double\" quotes"),

        # Unicode dashes
        ("En–dash", "En-dash"),
        ("Em—dash", "Em--dash"),

        # Ellipsis
        ("Trailing…", "Trailing..."),

        # Non-breaking space
        ("Non\u00A0breaking", "Non breaking"),

        # Year abbreviation fix
        ("Class of '21", "Class of'21"),
        ("Back in '99", "Back in'99"),

        # Combined Unicode and special chars
        ("José's café—open 24/7", "Jose's cafe--open 24/7"),
    ]

    for input_val, expected_val in test_cases:
        entry = {"type": "article", "key": "test", "fields": {"author": input_val}}
        result = bt.bibtex_from_dict(entry)
        actual_val = extract_field(result, "author")

        assert actual_val == expected_val, (
            f"Unicode normalization failed:\n"
            f"  Input:    {input_val!r}\n"
            f"  Expected: {expected_val!r}\n"
            f"  Got:      {actual_val!r}"
        )


def test_bibtex_latex_and_unicode_combined():
    """
    Test that LaTeX stripping and Unicode normalization work together.
    """
    import re

    def extract_field(bibtex_str, field_name):
        """Helper to extract a field value from BibTeX output (handles nested braces)."""
        pattern = rf'{field_name}\s*=\s*\{{'
        match = re.search(pattern, bibtex_str)
        if not match:
            return None
        start = match.end() - 1
        depth = 0
        for i in range(start, len(bibtex_str)):
            if bibtex_str[i] == '{':
                depth += 1
            elif bibtex_str[i] == '}':
                depth -= 1
                if depth == 0:
                    return bibtex_str[start + 1:i]
        return None

    # Combined test cases
    test_cases = [
        # LaTeX + accents
        (r"\textit{Café} Culture", "Cafe Culture"),
        (r"The \emph{naïve} approach", "The naive approach"),

        # LaTeX + Unicode quotes
        ("\\textbf{\u201CImportant\u201D} finding", "\"Important\" finding"),

        # LaTeX + dashes + accents
        (r"\emph{José}—A \textbf{Survey}", "Jose--A Survey"),

        # Special chars + accents
        (r"50\% of café visitors", "50% of cafe visitors"),

        # Full complex case
        ("\\textit{François}'s \\textbf{café}—50\\% \u201Cdiscount\u201D",
         "Francois's cafe--50% \"discount\""),
    ]

    for input_title, expected_title in test_cases:
        entry = {"type": "article", "key": "test", "fields": {"title": input_title}}
        result = bt.bibtex_from_dict(entry)
        actual_title = extract_field(result, "title")

        assert actual_title == expected_title, (
            f"Combined LaTeX+Unicode normalization failed:\n"
            f"  Input:    {input_title!r}\n"
            f"  Expected: {expected_title!r}\n"
            f"  Got:      {actual_title!r}"
        )

# ===== BIBTEX MATCHING =====

def test_bibtex_matching():
    """
    Test strict BibTeX matching.
    """
    # Exact match
    bib1 = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()
    bib2 = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani and Noam Shazeer},
          year = {2017}
        }
    """).strip()
    assert bt.bibtex_entries_match_strict(bt.parse_bibtex_to_dict(bib1), bt.parse_bibtex_to_dict(bib2)), "Exact entries should match"
    
    # With normalization
    bib3 = dedent("""
        @inproceedings{Vaswani2017_Caps,
          title = {ATTENTION IS ALL YOU NEED!},
          author = {Ashish Vaswani},
          year = {2017}
        }
    """).strip()
    assert bt.bibtex_entries_match_strict(bt.parse_bibtex_to_dict(bib1), bt.parse_bibtex_to_dict(bib3)), "Case/punctuation differences should match"

    # Abbreviated authors
    bib4 = dedent("""
        @inproceedings{He2016,
          title = {Deep Residual Learning for Image Recognition},
          author = {K He and X Zhang},
          year = {2016}
        }
    """).strip()
    bib5 = dedent("""
        @inproceedings{He2016_Full,
          title = {Deep Residual Learning for Image Recognition},
          author = {Kaiming He and Xiangyu Zhang},
          year = {2016}
        }
    """).strip()
    assert bt.bibtex_entries_match_strict(bt.parse_bibtex_to_dict(bib4), bt.parse_bibtex_to_dict(bib5)), "Abbreviated authors should match"

    # Should NOT match
    bib6 = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani},
          year = {2017}
        }
    """).strip()
    bib7 = dedent("""
        @inproceedings{He2016,
          title = {Deep Residual Learning for Image Recognition},
          author = {Kaiming He},
          year = {2016}
        }
    """).strip()
    assert not bt.bibtex_entries_match_strict(bt.parse_bibtex_to_dict(bib6), bt.parse_bibtex_to_dict(bib7)), "Different titles should NOT match"

def test_bibtex_extra_fields():
    """
    Test that extra fields don't prevent matching.
    """
    minimal = dedent("""
        @inproceedings{Vaswani2017,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani},
          year = {2017}
        }
    """).strip()
    enriched = dedent("""
        @inproceedings{Vaswani2017_Enriched,
          title = {Attention Is All You Need},
          author = {Ashish Vaswani},
          year = {2017},
          booktitle = {NeurIPS},
          pages = {5998--6008},
          doi = {10.5555/3295222.3295349}
        }
    """).strip()
    assert bt.bibtex_entries_match_strict(bt.parse_bibtex_to_dict(minimal), bt.parse_bibtex_to_dict(enriched)), "Extra fields should not prevent matching"

# ===== CONFIGURATION =====

def test_config():
    """
    Test configuration constants.
    """
    for const in ['CONTRIBUTION_WINDOW_YEARS', 'SIM_EXACT_PICK_THRESHOLD']:
        assert hasattr(config, const) and getattr(config, const) is not None, f"Missing constant: {const}"

# ===== FILE I/O =====

def test_safe_file_operations(tmp_path):
    """
    Test safe file reading and writing.
    """
    # Test safe write and read
    test_path = tmp_path / "subdir" / "test.txt"
    content = "Hello, World!"

    assert io_utils.safe_write_file(str(test_path), content, makedirs=True), "safe_write_file failed"

    read_content = io_utils.safe_read_file(str(test_path))
    assert read_content == content, f"Expected '{content}', got '{read_content}'"

    # Test non-existent file
    assert io_utils.safe_read_file("/nonexistent/path.txt") is None, "Should return None for non-existent file"

    # Test write without makedirs
    no_dir_path = tmp_path / "nodir" / "test.txt"
    assert not io_utils.safe_write_file(str(no_dir_path), content, makedirs=False), "Should fail without makedirs"

def test_safe_json_operations(tmp_path):
    """
    Test safe JSON reading and writing.
    """
    test_path = tmp_path / "test.json"
    data = {"title": "Attention Is All You Need", "year": 2017, "authors": ["Vaswani", "Shazeer"]}

    assert io_utils.safe_write_json(str(test_path), data), "safe_write_json failed"

    read_data = io_utils.safe_read_json(str(test_path))
    assert read_data == data, f"Expected {data}, got {read_data}"

    # Test non-existent file with default
    default = {"default": True}
    read_data = io_utils.safe_read_json("/nonexistent.json", default=default)
    assert read_data == default, f"Expected default {default}, got {read_data}"

def test_csv_summary_operations(tmp_path):
    """
    Test CSV summary initialization and appending.
    """
    csv_path = tmp_path / "summary.csv"
    csv_path_str = str(csv_path)

    # Initialize CSV
    io_utils.init_summary_csv(csv_path_str)
    assert csv_path.exists(), "CSV file not created"

    # Append rows
    flags = {
        "scholar_bib": True,
        "s2": True,
        "crossref": False,
    }
    io_utils.append_summary_to_csv(csv_path_str, "test.bib", 2, flags)

    # Verify content
    content = io_utils.safe_read_file(csv_path_str)
    assert "file_path" in content and "trust_hits" in content, "CSV headers missing"
    assert "test.bib" in content and "2" in content, "CSV data not appended correctly"

def test_read_records_from_csv(tmp_path):
    """
    Test reading author records from CSV.
    """
    csv_path = tmp_path / "test.csv"
    csv_path_str = str(csv_path)

    # Write test CSV
    csv_content = dedent("""
        Name,Scholar Link,DBLP Link
        Ashish Vaswani,https://scholar.google.com/citations?user=Scholar123,https://dblp.org/pid/vaswani/a
        Noam Shazeer,https://scholar.google.com/citations?user=Scholar456,
        ,https://scholar.google.com/citations?user=Scholar789,
        InvalidRow,,
    """).strip()
    io_utils.safe_write_file(csv_path_str, csv_content)

    # Read records
    records = io_utils.read_records(csv_path_str)

    # Should have 3 valid records (Scholar123, Scholar456, Scholar789)
    # InvalidRow has no IDs, so it should be filtered
    assert len(records) == 3, f"Expected 3 records, got {len(records)}"

    # Verify first record
    assert records[0].name == "Ashish Vaswani"
    assert records[0].scholar_id == "Scholar123"
    assert records[0].dblp == "vaswani/a"

    # Verify records with missing IDs are filtered
    # (InvalidRow should be filtered out)
    for r in records:
        assert r.scholar_id or r.dblp, "Records without any ID should be filtered"

# ===== BIBTEX MERGING =====

def test_merge_with_policy():
    """
    Test BibTeX merging with trust hierarchy.
    """
    # Primary (Scholar baseline)
    primary = {
        "type": "misc",
        "key": "Vaswani2017",
        "fields": {
            "title": "Attention Is All You Need",
            "author": "Ashish Vaswani",
            "year": "2017"
        }
    }

    # Enrichers with different trust levels
    enrichers = [
        ("crossref", {
            "type": "inproceedings",
            "fields": {
                "title": "Attention Is All You Need",
                "author": "Ashish Vaswani",
                "year": "2017",
                "booktitle": "NeurIPS",
                "doi": "10.5555/3295222.3295349"
            }
        }),
        ("s2", {
            "type": "article",
            "fields": {
                "title": "Attention Is All You Need",
                "author": "Ashish Vaswani",
                "year": "2017",
                "volume": "30"
            }
        }),
    ]

    merged = merge_utils.merge_with_policy(primary, enrichers)

    # Should prefer Crossref (higher trust)
    assert merged["type"] == "inproceedings"
    fields = merged.get("fields", {})
    assert fields.get("booktitle") == "NeurIPS"
    assert fields.get("doi"), "DOI should be present from Crossref"

def test_merge_doi_arxiv_handling():
    """
    Test DOI vs arXiv handling in merge.
    When a published DOI is present alongside arXiv, the arXiv fields should be removed
    since DOI is the primary identifier for published papers.
    """
    primary = {
        "type": "misc",
        "key": "Vaswani2017",
        "fields": {
            "title": "Attention Is All You Need",
            "author": "Ashish Vaswani",
            "year": "2017",
            "eprint": "1706.03762",
            "archiveprefix": "arXiv",
        }
    }

    # Add DOI from trusted source
    enrichers = [
        ("crossref", {
            "type": "inproceedings",
            "fields": {
                "doi": "10.5555/3295222.3295349",
                "booktitle": "NeurIPS"
            }
        }),
    ]

    merged = merge_utils.merge_with_policy(primary, enrichers)
    fields = merged.get("fields", {})

    # DOI should be present
    assert fields.get("doi"), "DOI should be present"

    # arXiv fields should be removed when DOI present
    assert not fields.get("eprint"), "eprint should be removed when DOI present"
    assert not fields.get("archiveprefix"), "archiveprefix should be removed when DOI present"

def test_save_entry_to_file(tmp_path):
    """
    Test saving BibTeX entry to file with collision handling.
    """
    entry = {
        "type": "inproceedings",
        "key": "Vaswani2017",
        "fields": {
            "title": "Attention Is All You Need",
            "author": "Ashish Vaswani",
            "year": "2017"
        }
    }
    tmpdir_str = str(tmp_path)

    # Save first time
    path1 = merge_utils.save_entry_to_file(
        tmpdir_str, "Scholar123", entry,
        author_name="Ashish Vaswani"
    )

    assert os.path.exists(path1), f"File not created: {path1}"

    # Save same entry again (should reuse same file)
    path2 = merge_utils.save_entry_to_file(
        tmpdir_str, "Scholar123", entry,
        prefer_path=path1,
        author_name="Ashish Vaswani"
    )

    assert path1 == path2, f"Should reuse same path: {path1} vs {path2}"

    # Modify entry and save (should create new file or update)
    entry["fields"]["booktitle"] = "NeurIPS"
    path3 = merge_utils.save_entry_to_file(
        tmpdir_str, "Scholar123", entry,
        author_name="Ashish Vaswani"
    )

    assert os.path.exists(path3), f"Modified entry file not created: {path3}"

# ===== UTILITIES AND EDGE CASES =====

def test_exception_definitions():
    """
    Test that exception tuples are properly defined.
    """
    required = ['HTTP_ERRORS', 'NETWORK_ERRORS', 'ALL_API_ERRORS', 'FILE_IO_ERRORS']
    for name in required:
        assert hasattr(exceptions, name), f"Missing: {name}"
        assert isinstance(getattr(exceptions, name), tuple), f"{name} not a tuple"

def test_http_error_decorator():
    """
    Test handle_api_errors decorator.
    """
    @http_utils.handle_api_errors(default_return="fallback")
    def failing_func():
        import urllib.error
        raise urllib.error.URLError("Test error")

    ret = failing_func()
    assert ret == "fallback", f"Expected 'fallback', got '{ret}'"


# ===== DATA QUALITY TESTS =====

def test_no_duplicate_titles_per_author():
    """
    Test that no author has two publications with title similarity >= 90%.

    This catches preprint/published duplicates and other duplicate entries
    that should have been deduplicated during processing.
    """
    output_dir = Path(__file__).parent.parent / "output"

    if not output_dir.exists():
        pytest.skip("Output directory does not exist")

    duplicates = []

    for author_dir in sorted(output_dir.iterdir()):
        if not author_dir.is_dir():
            continue

        bib_files = sorted(author_dir.glob("*.bib"))
        entries = []

        for bib_file in bib_files:
            try:
                content = bib_file.read_text(encoding="utf-8")
                entry = bt.parse_bibtex_to_dict(content)
                if entry:
                    entry["_filename"] = bib_file.name
                    entries.append(entry)
            except Exception:
                pass

        # Compare all pairs within this author
        for i, e1 in enumerate(entries):
            for e2 in entries[i + 1:]:
                t1 = e1.get("fields", {}).get("title", "")
                t2 = e2.get("fields", {}).get("title", "")

                if not t1 or not t2:
                    continue

                sim = text_utils.title_similarity(t1, t2)

                if sim >= 0.95:
                    # Check if DOIs are different (different papers with similar titles)
                    d1 = e1.get("fields", {}).get("doi", "").strip().lower()
                    d2 = e2.get("fields", {}).get("doi", "").strip().lower()

                    # If both have DOIs and they differ, these are different papers
                    if d1 and d2 and d1 != d2:
                        continue

                    duplicates.append({
                        "author": author_dir.name,
                        "file1": e1["_filename"],
                        "file2": e2["_filename"],
                        "similarity": sim,
                        "title1": t1[:60] + "..." if len(t1) > 60 else t1,
                        "title2": t2[:60] + "..." if len(t2) > 60 else t2,
                    })

    if duplicates:
        msg_lines = ["Found duplicate entries that should be deduplicated:"]
        for d in duplicates:
            msg_lines.append(f"\n  Author: {d['author']}")
            msg_lines.append(f"    {d['file1']}: {d['title1']}")
            msg_lines.append(f"    {d['file2']}: {d['title2']}")
            msg_lines.append(f"    Similarity: {d['similarity']:.1%}")

        pytest.fail("\n".join(msg_lines))
