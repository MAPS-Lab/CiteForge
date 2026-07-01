from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest

from src import bibtex_utils as bt
from src import config, exceptions, http_utils, id_utils, io_utils, merge_utils, text_utils
from tests.conftest import extract_bibtex_field


def test_title_normalization() -> None:
    """Test title normalization with all variations."""
    test_cases = [
        # Basic
        ("Attention Is All You Need", "attention is all you need"),
        ("Deep Residual Learning for Image Recognition", "deep residual learning for image recognition"),
        (
            "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
            "bert pre training of deep bidirectional transformers for language understanding",
        ),
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


def test_title_similarity() -> None:
    """Test title similarity scoring."""
    test_cases = [
        ("Attention Is All You Need", "Attention Is All You Need", True),
        ("Attention Is All You Need", "attention is all you need", True),
        ("Deep Learning", "Machine Learning", False),
    ]

    for title1, title2, should_be_similar in test_cases:
        score = text_utils.title_similarity(title1, title2)
        is_similar = score >= 0.8
        assert is_similar == should_be_similar, f"Expected similarity {should_be_similar}, got score {score}"


def test_author_parsing() -> None:
    """Test author parsing with all formats."""
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


def test_author_matching() -> None:
    """Test author name matching with initials."""
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


def test_authors_overlap() -> None:
    """Test author list overlap detection."""
    test_cases = [
        ("Ashish Vaswani and Noam Shazeer", "Ashish Vaswani", True),
        ("Hinton; LeCun; Bengio", "LeCun", True),
        ("Ashish Vaswani", "Noam Shazeer", False),
        ("", "", False),
    ]

    for authors1, authors2, should_overlap in test_cases:
        overlap = text_utils.authors_overlap(authors1, authors2)
        assert overlap == should_overlap, f"Expected overlap {should_overlap} for '{authors1}' vs '{authors2}'"


def test_doi_normalization() -> None:
    """Test DOI normalization (URL stripping, prefix removal, lowercasing)."""
    normalize_cases = [
        ("https://doi.org/10.18653/v1/N19-1423", "10.18653/v1/n19-1423"),
        ("doi:10.1234/TEST", "10.1234/test"),
        ("  10.1234/TEST  ", "10.1234/test"),
        ("", None),
        (None, None),
    ]
    for input_val, expected in normalize_cases:
        output = id_utils.normalize_doi(input_val)
        assert output == expected, f"normalize_doi({input_val!r}): expected {expected!r}, got {output!r}"


def test_doi_extraction_from_html() -> None:
    """Test DOI extraction from HTML meta tags."""
    html_cases = [
        ('<meta name="citation_doi" content="10.18653/v1/N19-1423" />', "10.18653/v1/n19-1423"),
        ('<meta name="dc.identifier" content="doi:10.1234/TEST" />', "10.1234/test"),
        ("<p>No DOI here</p>", None),
        ("", None),
    ]
    for input_val, expected in html_cases:
        output = id_utils.find_doi_in_html(input_val)
        assert output == expected, f"find_doi_in_html({input_val!r}): expected {expected!r}, got {output!r}"


def test_doi_extraction_from_text() -> None:
    """Test DOI extraction from free text."""
    text_cases = [
        ("See doi:10.1234/TEST for details", "10.1234/test"),
        ("DOI is 10.18653/v1/N19-1423 here", "10.18653/v1/n19-1423"),
        ("no doi present", None),
        ("", None),
    ]
    for input_val, expected in text_cases:
        output = id_utils.find_doi_in_text(input_val)
        assert output == expected, f"find_doi_in_text({input_val!r}): expected {expected!r}, got {output!r}"


def test_arxiv_extraction() -> None:
    """Test arXiv ID extraction."""
    test_cases = [
        ("See arXiv:1706.03762 for details", "1706.03762"),
        ("https://arxiv.org/abs/1706.03762v5", "1706.03762"),
        ("arxiv.org/abs/1706.03762", "1706.03762"),
        ("10.48550/arxiv.2301.01234", "2301.01234"),
        ("no arxiv id here", None),
        ("", None),
    ]

    for input_val, expected in test_cases:
        output = id_utils.find_arxiv_in_text(input_val)
        assert output == expected, f"find_arxiv_in_text({input_val!r}): expected {expected!r}, got {output!r}"


def test_bibtex_parsing() -> None:
    """Test BibTeX parsing."""
    valid_cases = [
        (
            dedent("""
            @inproceedings{Vaswani2017,
              title = {Attention Is All You Need},
              author = {Ashish Vaswani},
              year = {2017}
            }
        """).strip(),
            {"type": "inproceedings", "key": "Vaswani2017"},
        ),
        (
            dedent("""
            @article{He2016,
              title = {Deep Residual Learning for Image Recognition},
              author = {Kaiming He and Xiangyu Zhang},
              year = {2016},
              journal = {CVPR}
            }
        """).strip(),
            {"type": "article", "key": "He2016"},
        ),
    ]

    for bibtex_str, expected_keys in valid_cases:
        parsed = bt.parse_bibtex_to_dict(bibtex_str)
        assert parsed is not None, "Parsing failed for valid BibTeX"
        for key, expected_val in expected_keys.items():
            assert parsed.get(key) == expected_val, f"Expected field '{key}' to be '{expected_val}'"

    for invalid_bib in ["", "invalid bibtex"]:
        parsed = bt.parse_bibtex_to_dict(invalid_bib)
        assert parsed is None, "Expected None for invalid BibTeX"


def test_bibtex_building() -> None:
    """Test BibTeX construction."""
    # Minimal BibTeX
    bibtex = bt.build_minimal_bibtex(
        "Attention Is All You Need",
        ["Ashish Vaswani", "Noam Shazeer"],
        2017,
        keyhint="Vaswani2017",
    )
    assert bibtex and "@" in bibtex, "No valid BibTeX returned"

    # Verify roundtrip
    parsed = bt.parse_bibtex_to_dict(bibtex)
    assert parsed and "title" in parsed.get("fields", {}), "Parsing built BibTeX failed"

    # Dict to BibTeX
    entry = {
        "type": "article",
        "key": "Vaswani2017",
        "fields": {"title": "Attention Is All You Need", "author": "Ashish Vaswani", "year": "2017"},
    }
    bibtex2 = bt.bibtex_from_dict(entry)
    assert bibtex2 and "@article" in bibtex2, "Invalid BibTeX from dict"


def test_bibtex_latex_stripping() -> None:
    """Test LaTeX formatting removal in BibTeX output via bibtex_from_dict."""
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
        (r"Research \& Development", r"Research \& Development"),
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
        actual_title = extract_bibtex_field(result, "title")

        assert actual_title == expected_title, (
            f"LaTeX stripping failed:\n"
            f"  Input:    {input_title!r}\n"
            f"  Expected: {expected_title!r}\n"
            f"  Got:      {actual_title!r}"
        )


def test_bibtex_unicode_normalization() -> None:
    """Test Unicode to ASCII normalization in BibTeX output via bibtex_from_dict."""
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
        ("It\u2019s a \u201ctest\u201d", 'It\'s a "test"'),
        ("\u2018Single\u2019 quotes", "'Single' quotes"),
        ("\u201cDouble\u201d quotes", '"Double" quotes'),
        # Unicode dashes
        ("En\u2013dash", "En-dash"),
        ("Em—dash", "Em--dash"),
        # Ellipsis
        ("Trailing…", "Trailing..."),
        # Non-breaking space
        ("Non\u00a0breaking", "Non breaking"),
        # Year abbreviation fix
        ("Class of '21", "Class of'21"),
        ("Back in '99", "Back in'99"),
        # Combined Unicode and special chars
        ("José's café—open 24/7", "Jose's cafe--open 24/7"),
    ]

    for input_val, expected_val in test_cases:
        entry = {"type": "article", "key": "test", "fields": {"author": input_val}}
        result = bt.bibtex_from_dict(entry)
        actual_val = extract_bibtex_field(result, "author")

        assert actual_val == expected_val, (
            f"Unicode normalization failed:\n"
            f"  Input:    {input_val!r}\n"
            f"  Expected: {expected_val!r}\n"
            f"  Got:      {actual_val!r}"
        )


def test_bibtex_latex_and_unicode_combined() -> None:
    """Test that LaTeX stripping and Unicode normalization work together."""
    test_cases = [
        # LaTeX + accents
        (r"\textit{Café} Culture", "Cafe Culture"),
        (r"The \emph{naïve} approach", "The naive approach"),
        # LaTeX + Unicode quotes
        ("\\textbf{\u201cImportant\u201d} finding", '"Important" finding'),
        # LaTeX + dashes + accents
        (r"\emph{José}—A \textbf{Survey}", "Jose--A Survey"),
        # Special chars + accents
        (r"50\% of café visitors", "50% of cafe visitors"),
        # Full complex case
        ("\\textit{François}'s \\textbf{café}—50\\% \u201cdiscount\u201d", 'Francois\'s cafe--50% "discount"'),
    ]

    for input_title, expected_title in test_cases:
        entry = {"type": "article", "key": "test", "fields": {"title": input_title}}
        result = bt.bibtex_from_dict(entry)
        actual_title = extract_bibtex_field(result, "title")

        assert actual_title == expected_title, (
            f"Combined LaTeX+Unicode normalization failed:\n"
            f"  Input:    {input_title!r}\n"
            f"  Expected: {expected_title!r}\n"
            f"  Got:      {actual_title!r}"
        )


def test_bibtex_matching() -> None:
    """Test strict BibTeX matching."""
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
    parsed_bib1 = bt.parse_bibtex_to_dict(bib1)
    parsed_bib2 = bt.parse_bibtex_to_dict(bib2)
    assert parsed_bib1 is not None and parsed_bib2 is not None
    assert bt.bibtex_entries_match_strict(parsed_bib1, parsed_bib2), "Exact entries should match"

    # With normalization
    bib3 = dedent("""
        @inproceedings{Vaswani2017_Caps,
          title = {ATTENTION IS ALL YOU NEED!},
          author = {Ashish Vaswani},
          year = {2017}
        }
    """).strip()
    parsed_bib3 = bt.parse_bibtex_to_dict(bib3)
    assert parsed_bib3 is not None
    assert bt.bibtex_entries_match_strict(parsed_bib1, parsed_bib3), "Case/punctuation differences should match"

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
    parsed_bib4 = bt.parse_bibtex_to_dict(bib4)
    parsed_bib5 = bt.parse_bibtex_to_dict(bib5)
    assert parsed_bib4 is not None and parsed_bib5 is not None
    assert bt.bibtex_entries_match_strict(parsed_bib4, parsed_bib5), "Abbreviated authors should match"

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
    parsed_bib6 = bt.parse_bibtex_to_dict(bib6)
    parsed_bib7 = bt.parse_bibtex_to_dict(bib7)
    assert parsed_bib6 is not None and parsed_bib7 is not None
    assert not bt.bibtex_entries_match_strict(parsed_bib6, parsed_bib7), "Different titles should NOT match"


def test_bibtex_extra_fields() -> None:
    """Test that extra fields don't prevent matching."""
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
    parsed_minimal = bt.parse_bibtex_to_dict(minimal)
    parsed_enriched = bt.parse_bibtex_to_dict(enriched)
    assert parsed_minimal is not None and parsed_enriched is not None
    assert bt.bibtex_entries_match_strict(parsed_minimal, parsed_enriched), "Extra fields should not prevent matching"


def test_config() -> None:
    """Test configuration constants."""
    for const in ("CONTRIBUTION_WINDOW_YEARS", "SIM_EXACT_PICK_THRESHOLD"):
        assert getattr(config, const, None) is not None, f"Missing constant: {const}"


def test_safe_file_operations(tmp_path: Path) -> None:
    """Test safe file reading and writing."""
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


def test_safe_json_operations(tmp_path: Path) -> None:
    """Test safe JSON reading and writing."""
    test_path = tmp_path / "test.json"
    data = {"title": "Attention Is All You Need", "year": 2017, "authors": ["Vaswani", "Shazeer"]}

    assert io_utils.safe_write_json(str(test_path), data), "safe_write_json failed"

    read_data = io_utils.safe_read_json(str(test_path))
    assert read_data == data, f"Expected {data}, got {read_data}"

    # Test non-existent file with default
    default = {"default": True}
    read_data = io_utils.safe_read_json("/nonexistent.json", default=default)
    assert read_data == default, f"Expected default {default}, got {read_data}"


def test_csv_summary_operations(tmp_path: Path) -> None:
    """Test CSV summary initialization and appending."""
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
    assert content is not None, "CSV file could not be read"
    assert "file_path" in content and "trust_hits" in content, "CSV headers missing"
    assert "test.bib" in content and "2" in content, "CSV data not appended correctly"


def test_merge_with_policy() -> None:
    """Test BibTeX merging with trust hierarchy."""
    # Primary (Scholar baseline)
    primary = {
        "type": "misc",
        "key": "Vaswani2017",
        "fields": {"title": "Attention Is All You Need", "author": "Ashish Vaswani", "year": "2017"},
    }

    # Enrichers with different trust levels
    enrichers = [
        (
            "crossref",
            {
                "type": "inproceedings",
                "fields": {
                    "title": "Attention Is All You Need",
                    "author": "Ashish Vaswani",
                    "year": "2017",
                    "booktitle": "NeurIPS",
                    "doi": "10.5555/3295222.3295349",
                },
            },
        ),
        (
            "s2",
            {
                "type": "article",
                "fields": {
                    "title": "Attention Is All You Need",
                    "author": "Ashish Vaswani",
                    "year": "2017",
                    "volume": "30",
                },
            },
        ),
    ]

    merged = merge_utils.merge_with_policy(primary, enrichers)

    # Should prefer Crossref (higher trust)
    assert merged["type"] == "inproceedings"
    fields = merged.get("fields", {})
    assert fields.get("booktitle") == "NeurIPS"
    assert fields.get("doi"), "DOI should be present from Crossref"


def test_merge_doi_arxiv_handling() -> None:
    """Test DOI vs arXiv handling in merge.

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
        },
    }

    # Add DOI from trusted source
    enrichers = [
        ("crossref", {"type": "inproceedings", "fields": {"doi": "10.5555/3295222.3295349", "booktitle": "NeurIPS"}}),
    ]

    merged = merge_utils.merge_with_policy(primary, enrichers)
    fields = merged.get("fields", {})

    # DOI should be present
    assert fields.get("doi"), "DOI should be present"

    # arXiv fields should be removed when DOI present
    assert not fields.get("eprint"), "eprint should be removed when DOI present"
    assert not fields.get("archiveprefix"), "archiveprefix should be removed when DOI present"


def test_save_entry_to_file(tmp_path: Path) -> None:
    """Test saving BibTeX entry to file with collision handling."""
    entry = {
        "type": "inproceedings",
        "key": "Vaswani2017",
        "fields": {"title": "Attention Is All You Need", "author": "Ashish Vaswani", "year": "2017"},
    }
    tmpdir_str = str(tmp_path)

    # Save first time
    path1, written1 = merge_utils.save_entry_to_file(tmpdir_str, "Scholar123", entry, author_name="Ashish Vaswani")

    assert os.path.exists(path1), f"File not created: {path1}"
    assert written1, "First save should report was_written=True"

    # Save same entry again (should reuse same file)
    path2, _ = merge_utils.save_entry_to_file(
        tmpdir_str, "Scholar123", entry, prefer_path=path1, author_name="Ashish Vaswani"
    )

    assert path1 == path2, f"Should reuse same path: {path1} vs {path2}"

    # Modify entry and save (should create new file or update)
    entry["fields"]["booktitle"] = "NeurIPS"
    path3, _ = merge_utils.save_entry_to_file(tmpdir_str, "Scholar123", entry, author_name="Ashish Vaswani")

    assert os.path.exists(path3), f"Modified entry file not created: {path3}"


def test_exception_definitions() -> None:
    """Test that exception tuples are properly defined."""
    for name in ("HTTP_ERRORS", "NETWORK_ERRORS", "ALL_API_ERRORS", "FILE_IO_ERRORS"):
        assert isinstance(getattr(exceptions, name, None), tuple), f"{name} missing or not a tuple"


def test_http_error_decorator() -> None:
    """Test handle_api_errors decorator returns default on API error."""
    import urllib.error

    @http_utils.handle_api_errors(default_return="fallback")
    def failing_func():  # type: ignore[no-untyped-def]
        raise urllib.error.URLError("Test error")

    assert failing_func() == "fallback"


def test_no_duplicate_titles_per_author() -> None:
    """Test that no author has two publications with title similarity >= 90%.

    This catches preprint/published duplicates and other duplicate entries
    that should have been deduplicated during processing.
    """
    output_dir = Path(__file__).parent.parent / "output"

    # The output directory is gitignored and only populated by pipeline runs.
    # Treat a missing directory as "no output to check" rather than skipping,
    # so the test always runs and exercises its logic.
    if not output_dir.exists():
        output_dir.mkdir(parents=True, exist_ok=True)

    duplicates = []

    for author_dir in sorted(output_dir.iterdir()):
        if not author_dir.is_dir():
            continue

        entries = []
        for bib_file in sorted(author_dir.glob("*.bib")):
            try:
                entry = bt.parse_bibtex_to_dict(bib_file.read_text(encoding="utf-8"))
                if entry:
                    entry["_filename"] = bib_file.name
                    entries.append(entry)
            except Exception:  # noqa: S110
                pass  # intentionally skip unparseable .bib files

        for i, e1 in enumerate(entries):
            for e2 in entries[i + 1 :]:
                t1 = e1.get("fields", {}).get("title", "")
                t2 = e2.get("fields", {}).get("title", "")
                if not t1 or not t2:
                    continue

                sim = text_utils.title_similarity(t1, t2)
                if sim < 0.95:
                    continue

                d1 = e1.get("fields", {}).get("doi", "").strip().lower()
                d2 = e2.get("fields", {}).get("doi", "").strip().lower()
                if d1 and d2 and d1 != d2:
                    continue

                duplicates.append(
                    {
                        "author": author_dir.name,
                        "file1": e1["_filename"],
                        "file2": e2["_filename"],
                        "similarity": sim,
                        "title1": t1[:60],
                        "title2": t2[:60],
                    }
                )

    if duplicates:
        msg_lines = ["Found duplicate entries that should be deduplicated:"]
        for d in duplicates:
            msg_lines.append(f"\n  Author: {d['author']}")
            msg_lines.append(f"    {d['file1']}: {d['title1']}")
            msg_lines.append(f"    {d['file2']}: {d['title2']}")
            msg_lines.append(f"    Similarity: {d['similarity']:.1%}")

        pytest.fail("\n".join(msg_lines))
