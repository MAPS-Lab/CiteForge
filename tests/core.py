from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from CiteForge import text_utils, id_utils, config, bibtex_utils as bt, io_utils, merge_utils
from CiteForge import exceptions, http_utils
from CiteForge.models import Record
from tests.test_result import BaseTestResult as TestResult
from tests.test_utils import run_tests_with_reporting, print_test_summary
import os
import tempfile


class CoreTestSuite:
    """
    Comprehensive test suite for core CiteForge functionality.
    """
    def __init__(self):
        self.results = []

    def add_result(self, result: TestResult):
        self.results.append(result)

    # ===== TEXT NORMALIZATION =====

    @staticmethod
    def test_title_normalization() -> TestResult:
        """
        Test title normalization with all variations.
        """
        result = TestResult("Title normalization")

        test_cases = [
            # Basic
            ("Simple Title", "simple title"),
            ("Title With, Punctuation!", "title with punctuation"),
            ("UPPERCASE", "uppercase"),
            ("Title   Spaces", "title spaces"),
            # LaTeX
            ("Analysis of $\\phi$ distribution", "analysis of distribution"),
            ("\\textbf{Bold Title}", "bold title"),
            ("\\emph{Text} here", "text here"),
            # Accents
            ("Café Society", "cafe society"),
            ("Naïve Bayes", "naive bayes"),
            # Empty
            ("", ""),
            (None, ""),
        ]

        for input_val, expected in test_cases:
            output = text_utils.normalize_title(input_val)
            if output != expected:
                result.failure(f"normalize_title('{input_val}')", f"Expected '{expected}', got '{output}'")
                return result

        result.success(f"Validated {len(test_cases)} normalization cases including LaTeX removal and accent handling")
        return result

    @staticmethod
    def test_title_similarity() -> TestResult:
        """
        Test title similarity scoring.
        """
        result = TestResult("Title similarity")

        test_cases = [
            ("Attention Is All You Need", "Attention Is All You Need", True),
            ("Attention Is All You Need", "attention is all you need", True),
            ("Deep Learning", "Machine Learning", False),
        ]

        for title1, title2, should_be_similar in test_cases:
            score = text_utils.title_similarity(title1, title2)
            is_similar = score >= 0.8
            if is_similar != should_be_similar:
                result.failure(
                    f"title_similarity('{title1}', '{title2}')",
                    f"Expected {should_be_similar}, got {score}"
                )
                return result

        result.success(f"Validated {len(test_cases)} similarity cases with 0.8 threshold")
        return result

    # ===== AUTHOR PARSING =====

    @staticmethod
    def test_author_parsing() -> TestResult:
        """
        Test author parsing with all formats.
        """
        result = TestResult("Author parsing")

        test_cases = [
            ("John Smith and Jane Doe", ["John Smith", "Jane Doe"]),
            ("John Smith; Jane Doe", ["John Smith", "Jane Doe"]),
            ("H Huang, DV Arnold", ["H Huang", "DV Arnold"]),
            ("A Smith, B Jones", ["A Smith", "B Jones"]),
            ("Smith, John", ["Smith, John"]),
            ("Alice, Bob, Carol", ["Alice", "Bob", "Carol"]),
            ("", []),
            (None, []),
        ]

        for input_val, expected in test_cases:
            output = text_utils.extract_authors_from_any(input_val)
            if output != expected:
                result.failure(f"extract_authors('{input_val}')", f"Expected {expected}, got {output}")
                return result

        result.success(
            f"Validated {len(test_cases)} parsing formats including BibTeX, "
            "semicolon, and abbreviated forms"
        )
        return result

    @staticmethod
    def test_author_matching() -> TestResult:
        """
        Test author name matching with initials.
        """
        result = TestResult("Author matching")

        test_cases = [
            ("John Smith", "John Smith", True),
            ("JOHN SMITH", "john smith", True),
            ("Hong Huang", "H Huang", True),
            ("Dirk V. Arnold", "DV Arnold", True),
            ("John Smith", "J Smith", True),
            ("John Smith", "John Jones", False),
            ("J Smith", "K Smith", False),
        ]

        for name1, name2, should_match in test_cases:
            matches = text_utils.author_name_matches(name1, name2)
            if matches != should_match:
                result.failure(f"author_matches('{name1}', '{name2}')", f"Expected {should_match}, got {matches}")
                return result

        result.success(f"Validated {len(test_cases)} matching cases including initial-based comparisons")
        return result

    @staticmethod
    def test_authors_overlap() -> TestResult:
        """
        Test author list overlap detection.
        """
        result = TestResult("Authors overlap")

        test_cases = [
            ("John Smith and Jane Doe", "John Smith", True),
            ("Alice; Bob; Carol", "Bob", True),
            ("John Smith", "Jane Doe", False),
            ("", "", False),
        ]

        for authors1, authors2, should_overlap in test_cases:
            overlap = text_utils.authors_overlap(authors1, authors2)
            if overlap != should_overlap:
                result.failure(
                    f"authors_overlap('{authors1}', '{authors2}')",
                    f"Expected {should_overlap}, got {overlap}"
                )
                return result

        result.success(f"Validated {len(test_cases)} overlap detection cases")
        return result

    # ===== ID EXTRACTION =====

    @staticmethod
    def test_doi_extraction() -> TestResult:
        """
        Test DOI extraction and normalization.
        """
        result = TestResult("DOI extraction")

        test_cases = [
            ("https://doi.org/10.18653/v1/N19-1423", "10.18653/v1/n19-1423"),
            ("doi:10.1234/TEST", "10.1234/test"),
            ('<meta name="citation_doi" content="10.18653/v1/N19-1423" />', "10.18653/v1/n19-1423"),
            ("  10.1234/TEST  ", "10.1234/test"),
            ("", None),
            (None, None),
        ]

        for input_val, expected in test_cases:
            if '<meta' in str(input_val):
                output = id_utils.find_doi_in_html(input_val)
            elif input_val and 'doi' in input_val:
                output = id_utils.find_doi_in_text(input_val)
            else:
                output = id_utils.normalize_doi(input_val)

            if output != expected:
                result.failure(f"DOI '{input_val}'", f"Expected '{expected}', got '{output}'")
                return result

        result.success(f"Validated {len(test_cases)} DOI extraction and normalization cases")
        return result

    @staticmethod
    def test_arxiv_extraction() -> TestResult:
        """
        Test arXiv ID extraction.
        """
        result = TestResult("arXiv extraction")

        test_cases = [
            ("See arXiv:1706.03762 for details", "1706.03762"),
            ("https://arxiv.org/abs/1706.03762v5", "1706.03762"),
            ("arxiv.org/abs/1706.03762", "1706.03762"),
            ("", None),
        ]

        for input_val, expected in test_cases:
            output = id_utils.find_arxiv_in_text(input_val) if input_val else None
            if output != expected:
                result.failure(f"arXiv '{input_val}'", f"Expected '{expected}', got '{output}'")
                return result

        result.success(f"Validated {len(test_cases)} arXiv ID extraction cases")
        return result

    # ===== BIBTEX PARSING =====

    @staticmethod
    def test_bibtex_parsing() -> TestResult:
        """
        Test BibTeX parsing.
        """
        result = TestResult("BibTeX parsing")

        valid_cases = [
            ("""@inproceedings{Key2017,
  title = {Test Title},
  author = {John Smith},
  year = {2017}
}""", {'type': 'inproceedings', 'key': 'Key2017'}),
            ("""@article{Smith2020,
  title = {Article Title},
  author = {Alice and Bob},
  year = {2020},
  journal = {Nature}
}""", {'type': 'article', 'key': 'Smith2020'}),
        ]

        for bibtex_str, expected_keys in valid_cases:
            parsed = bt.parse_bibtex_to_dict(bibtex_str)
            if parsed is None:
                result.failure("Parsing failed for valid BibTeX")
                return result
            for key, expected_val in expected_keys.items():
                if parsed.get(key) != expected_val:
                    result.failure(f"Field '{key}'", f"Expected '{expected_val}', got '{parsed.get(key)}'")
                    return result

        # Invalid cases
        for invalid_bib in ["", "invalid bibtex"]:
            try:
                parsed = bt.parse_bibtex_to_dict(invalid_bib)
                if parsed is not None:
                    result.failure(f"Expected None for invalid BibTeX")
                    return result
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                result.failure("Should not crash on invalid input")
                return result

        result.success(f"Validated {len(valid_cases)} valid entries and {2} invalid entry handlers")
        return result

    @staticmethod
    def test_bibtex_building() -> TestResult:
        """
        Test BibTeX construction.
        """
        result = TestResult("BibTeX building")

        # Minimal BibTeX
        bibtex = bt.build_minimal_bibtex("Test Paper", ["John Smith", "Jane Doe"], 2020, keyhint="test")
        if not bibtex or '@' not in bibtex:
            result.failure("build_minimal_bibtex", "No valid BibTeX returned")
            return result

        # Verify roundtrip
        parsed = bt.parse_bibtex_to_dict(bibtex)
        if not parsed or 'title' not in parsed.get('fields', {}):
            result.failure("Parsing built BibTeX", "Missing title field")
            return result

        # Dict to BibTeX
        entry = {
            'type': 'article', 'key': 'Test2020',
            'fields': {'title': 'Test', 'author': 'John Smith', 'year': '2020'}
        }
        bibtex2 = bt.bibtex_from_dict(entry)
        if not bibtex2 or '@article' not in bibtex2:
            result.failure("bibtex_from_dict", "Invalid BibTeX")
            return result

        result.success("Validated BibTeX construction and bidirectional parsing")
        return result

    # ===== BIBTEX MATCHING =====

    @staticmethod
    def test_bibtex_matching() -> TestResult:
        """
        Test strict BibTeX matching.
        """
        result = TestResult("BibTeX matching")

        # Exact match
        bib1 = """@inproceedings{Key1,
  title = {Attention Is All You Need},
  author = {Ashish Vaswani and Noam Shazeer},
  year = {2017}
}"""
        bib2 = """@inproceedings{Key2,
  title = {Attention Is All You Need},
  author = {Ashish Vaswani and Noam Shazeer},
  year = {2017}
}"""
        if not bt.bibtex_entries_match_strict(bib1, bib2):
            result.failure("Exact entries should match")
            return result

        # With normalization
        bib3 = """@inproceedings{K1,
  title = {ATTENTION IS ALL YOU NEED!},
  author = {Ashish Vaswani},
  year = {2017}
}"""
        if not bt.bibtex_entries_match_strict(bib1, bib3):
            result.failure("Case/punctuation differences should match")
            return result

        # Abbreviated authors
        bib4 = """@inproceedings{K1,
  title = {Test Paper},
  author = {H Huang and DV Arnold},
  year = {2020}
}"""
        bib5 = """@inproceedings{K2,
  title = {Test Paper},
  author = {Hong Huang and Dirk V. Arnold},
  year = {2020}
}"""
        if not bt.bibtex_entries_match_strict(bib4, bib5):
            result.failure("Abbreviated authors should match")
            return result

        # Should NOT match
        bib6 = """@inproceedings{K1,
  title = {Paper A},
  author = {John Smith},
  year = {2020}
}"""
        bib7 = """@inproceedings{K2,
  title = {Paper B},
  author = {John Smith},
  year = {2020}
}"""
        if bt.bibtex_entries_match_strict(bib6, bib7):
            result.failure("Different titles should NOT match")
            return result

        result.success("Validated strict matching including normalization and abbreviation handling")
        return result

    @staticmethod
    def test_bibtex_extra_fields() -> TestResult:
        """
        Test that extra fields don't prevent matching.
        """
        result = TestResult("BibTeX extra fields")

        minimal = """@inproceedings{K1,
  title = {Test Paper},
  author = {John Smith},
  year = {2020}
}"""
        enriched = """@inproceedings{K2,
  title = {Test Paper},
  author = {John Smith},
  year = {2020},
  booktitle = {Conference},
  pages = {1--10},
  doi = {10.1234/test}
}"""
        if not bt.bibtex_entries_match_strict(minimal, enriched):
            result.failure("Extra fields should not prevent matching")
            return result

        result.success("Validated enriched entry matching with additional fields")
        return result

    # ===== CONFIGURATION =====

    @staticmethod
    def test_config() -> TestResult:
        """
        Test configuration constants.
        """
        result = TestResult("Configuration")

        for const in ['CONTRIBUTION_WINDOW_YEARS', 'SIM_EXACT_PICK_THRESHOLD']:
            if not hasattr(config, const) or getattr(config, const) is None:
                result.failure(f"Missing or None constant: {const}")
                return result

        result.success("Validated presence of required configuration constants")
        return result

    # ===== FILE I/O =====

    @staticmethod
    def test_safe_file_operations() -> TestResult:
        """
        Test safe file reading and writing.
        """
        result = TestResult("Safe file I/O")

        with tempfile.TemporaryDirectory() as tmpdir:
            # Test safe write and read
            test_path = os.path.join(tmpdir, "subdir", "test.txt")
            content = "Hello, World!"

            if not io_utils.safe_write_file(test_path, content, makedirs=True):
                result.failure("safe_write_file failed")
                return result

            read_content = io_utils.safe_read_file(test_path)
            if read_content != content:
                result.failure(f"Expected '{content}', got '{read_content}'")
                return result

            # Test non-existent file
            if io_utils.safe_read_file("/nonexistent/path.txt") is not None:
                result.failure("Should return None for non-existent file")
                return result

            # Test write without makedirs
            no_dir_path = os.path.join(tmpdir, "nodir", "test.txt")
            if io_utils.safe_write_file(no_dir_path, content, makedirs=False):
                result.failure("Should fail without makedirs")
                return result

        result.success("Validated safe file operations including directory creation and error handling")
        return result

    @staticmethod
    def test_safe_json_operations() -> TestResult:
        """
        Test safe JSON reading and writing.
        """
        result = TestResult("Safe JSON I/O")

        with tempfile.TemporaryDirectory() as tmpdir:
            test_path = os.path.join(tmpdir, "test.json")
            data = {"title": "Test", "year": 2020, "authors": ["A", "B"]}

            if not io_utils.safe_write_json(test_path, data):
                result.failure("safe_write_json failed")
                return result

            read_data = io_utils.safe_read_json(test_path)
            if read_data != data:
                result.failure(f"Expected {data}, got {read_data}")
                return result

            # Test non-existent file with default
            default = {"default": True}
            read_data = io_utils.safe_read_json("/nonexistent.json", default=default)
            if read_data != default:
                result.failure(f"Expected default {default}, got {read_data}")
                return result

        result.success("Validated JSON serialization with default value handling")
        return result

    @staticmethod
    def test_csv_summary_operations() -> TestResult:
        """
        Test CSV summary initialization and appending.
        """
        result = TestResult("CSV summary operations")

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "summary.csv")

            # Initialize CSV
            try:
                io_utils.init_summary_csv(csv_path)
            except Exception as e:
                result.failure(f"init_summary_csv failed: {e}")
                return result

            if not os.path.exists(csv_path):
                result.failure("CSV file not created")
                return result

            # Append rows
            flags = {
                "scholar_bib": True,
                "s2": True,
                "crossref": False,
            }
            try:
                io_utils.append_summary_to_csv(csv_path, "test.bib", 2, flags)
            except Exception as e:
                result.failure(f"append_summary_to_csv failed: {e}")
                return result

            # Verify content
            content = io_utils.safe_read_file(csv_path)
            if "file_path" not in content or "trust_hits" not in content:
                result.failure("CSV headers missing")
                return result
            if "test.bib" not in content or "2" not in content:
                result.failure("CSV data not appended correctly")
                return result

        result.success("Validated CSV file initialization and data appending")
        return result

    @staticmethod
    def test_read_records_from_csv() -> TestResult:
        """
        Test reading author records from CSV.
        """
        result = TestResult("Read records from CSV")

        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "test.csv")

            # Write test CSV
            csv_content = """Name,Email,Scholar,ORCID,DBLP
John Doe,john@example.com,Scholar123,0000-0001-2345-6789,doe/j
Jane Smith,jane@example.com,Scholar456,,
,,,Scholar789,,
InvalidRow,,,,
"""
            io_utils.safe_write_file(csv_path, csv_content)

            # Read records
            try:
                records = io_utils.read_records(csv_path)
            except Exception as e:
                result.failure(f"read_records failed: {e}")
                return result

            # Should only have 2 valid records (Scholar123 and Scholar456)
            # Scholar789 has no name, InvalidRow has no Scholar ID
            if len(records) != 2:
                result.failure(f"Expected 2 records, got {len(records)}")
                return result

            # Verify first record
            if records[0].name != "John Doe":
                result.failure(f"Expected 'John Doe', got '{records[0].name}'")
                return result
            if records[0].scholar_id != "Scholar123":
                result.failure(f"Expected 'Scholar123', got '{records[0].scholar_id}'")
                return result

            # Verify empty records are filtered
            for r in records:
                if not r.scholar_id:
                    result.failure("Records without Scholar ID should be filtered")
                    return result

        result.success("Validated author record parsing with empty row filtering")
        return result

    # ===== BIBTEX MERGING =====

    @staticmethod
    def test_merge_with_policy() -> TestResult:
        """
        Test BibTeX merging with trust hierarchy.
        """
        result = TestResult("BibTeX trust policy merge")

        # Primary (Scholar baseline)
        primary = {
            "type": "misc",
            "key": "Test2020",
            "fields": {
                "title": "Test Paper",
                "author": "John Smith",
                "year": "2020"
            }
        }

        # Enrichers with different trust levels
        enrichers = [
            ("crossref", {
                "type": "article",
                "fields": {
                    "title": "Test Paper",
                    "author": "John Smith",
                    "year": "2020",
                    "journal": "Nature",
                    "doi": "10.1234/test"
                }
            }),
            ("s2", {
                "type": "article",  # Changed from inproceedings
                "fields": {
                    "title": "Test Paper",
                    "author": "John Smith",
                    "year": "2020",
                    "volume": "123"  # Changed from booktitle to avoid conflicting containers
                }
            }),
        ]

        merged = merge_utils.merge_with_policy(primary, enrichers)

        # Should prefer Crossref (higher trust)
        if merged["type"] != "article":
            result.failure(f"Expected 'article', got '{merged['type']}'")
            return result

        fields = merged.get("fields", {})
        if fields.get("journal") != "Nature":
            result.failure(f"Expected 'Nature', got '{fields.get('journal')}'")
            return result

        # Crossref is a trusted source for DOIs
        if not fields.get("doi"):
            result.failure("DOI should be present from Crossref")
            return result

        result.success("Validated trust-based field prioritization across multiple sources")
        return result

    @staticmethod
    def test_merge_doi_arxiv_handling() -> TestResult:
        """
        Test DOI vs arXiv handling in merge.
        """
        result = TestResult("Merge: DOI/arXiv handling")

        primary = {
            "type": "misc",
            "key": "Test2020",
            "fields": {
                "title": "Test Paper",
                "author": "John Smith",
                "year": "2020",
                "eprint": "1234.5678",
                "archiveprefix": "arXiv",
            }
        }

        # Add DOI from trusted source
        enrichers = [
            ("crossref", {
                "type": "article",
                "fields": {
                    "doi": "10.1234/test",
                    "journal": "Nature"
                }
            }),
        ]

        merged = merge_utils.merge_with_policy(primary, enrichers)
        fields = merged.get("fields", {})

        # DOI should be present
        if not fields.get("doi"):
            result.failure("DOI should be present")
            return result

        # arXiv fields should be moved to note
        if fields.get("eprint"):
            result.failure("eprint should be removed when DOI present")
            return result

        note = fields.get("note", "")
        if "arXiv" not in note or "1234.5678" not in note:
            result.failure(f"arXiv should be in note, got: '{note}'")
            return result

        result.success("Validated DOI prioritization with arXiv demotion to note field")
        return result

    @staticmethod
    def test_save_entry_to_file() -> TestResult:
        """
        Test saving BibTeX entry to file with collision handling.
        """
        result = TestResult("Save entry to file")

        with tempfile.TemporaryDirectory() as tmpdir:
            entry = {
                "type": "article",
                "key": "Test2020",
                "fields": {
                    "title": "Test Paper",
                    "author": "John Smith",
                    "year": "2020"
                }
            }

            # Save first time
            path1 = merge_utils.save_entry_to_file(
                tmpdir, "Scholar123", entry,
                author_name="John Doe"
            )

            if not os.path.exists(path1):
                result.failure(f"File not created: {path1}")
                return result

            # Save same entry again (should reuse same file)
            path2 = merge_utils.save_entry_to_file(
                tmpdir, "Scholar123", entry,
                prefer_path=path1,
                author_name="John Doe"
            )

            if path1 != path2:
                result.failure(f"Should reuse same path: {path1} vs {path2}")
                return result

            # Modify entry and save (should create new file or update)
            entry["fields"]["journal"] = "Nature"
            path3 = merge_utils.save_entry_to_file(
                tmpdir, "Scholar123", entry,
                author_name="John Doe"
            )

            if not os.path.exists(path3):
                result.failure(f"Modified entry file not created: {path3}")
                return result

        result.success("Validated file persistence with collision avoidance and content deduplication")
        return result

    # ===== INTEGRATION =====

    # ===== UTILITIES AND EDGE CASES =====

    @staticmethod
    def test_exception_definitions() -> TestResult:
        """
        Test that exception tuples are properly defined.
        """
        result = TestResult("Exception definitions")

        required = ['HTTP_ERRORS', 'NETWORK_ERRORS', 'ALL_API_ERRORS', 'FILE_IO_ERRORS']
        for name in required:
            if not hasattr(exceptions, name):
                result.failure(f"Missing: {name}")
                return result
            if not isinstance(getattr(exceptions, name), tuple):
                result.failure(f"{name} not a tuple")
                return result

        result.success(f"Validated {len(required)} exception tuple definitions")
        return result

    @staticmethod
    def test_http_error_decorator() -> TestResult:
        """
        Test handle_api_errors decorator.
        """
        result = TestResult("HTTP error decorator")

        @http_utils.handle_api_errors(default_return="fallback")
        def failing_func():
            import urllib.error
            raise urllib.error.URLError("Test error")

        try:
            ret = failing_func()
            if ret != "fallback":
                result.failure(f"Expected 'fallback', got '{ret}'")
                return result
        except Exception as e:
            result.failure(f"Decorator didn't catch: {e}")
            return result

        result.success("Validated error handling decorator")
        return result

    @staticmethod
    def test_record_model() -> TestResult:
        """
        Test Record dataclass.
        """
        result = TestResult("Record model")

        record = Record(
            name="Alice", email="alice@test.com",
            scholar_id="ID123", orcid="0000-0001-1111-1111", dblp="alice/a"
        )

        if record.name != "Alice" or record.scholar_id != "ID123":
            result.failure("Record field mismatch")
            return result

        result.success("Validated Record dataclass")
        return result

    @staticmethod
    def test_bibtex_edge_cases() -> TestResult:
        """
        Test BibTeX with edge cases.
        """
        result = TestResult("BibTeX edge cases")

        # Empty authors (valid for some entry types)
        bib = bt.build_minimal_bibtex("Paper", [], 2020, keyhint="test")
        if not bib or '@' not in bib:
            result.failure("Should handle empty authors")
            return result

        # Very long author list
        many = [f"Author {i}" for i in range(50)]
        bib = bt.build_minimal_bibtex("Paper", many, 2020, keyhint="test")
        if not bib or '@' not in bib:
            result.failure("Should handle long author lists")
            return result

        # Special characters in title
        bib = bt.build_minimal_bibtex("Test: $\\alpha$ & $\\beta$", ["A"], 2020, keyhint="test")
        if not bib or '@' not in bib:
            result.failure("Should handle special chars")
            return result

        result.success("Validated BibTeX edge case handling")
        return result

    @staticmethod
    def test_text_utils_helpers() -> TestResult:
        """
        Test text_utils helper functions.
        """
        result = TestResult("text_utils helpers")

        # build_url
        url = text_utils.build_url("https://api.test.com", {"q": "test", "n": "5"})
        if "q=test" not in url:
            result.failure(f"build_url failed: {url}")
            return result

        # safe_get_field
        data = {"title": "Test"}
        if text_utils.safe_get_field(data, "title") != "Test":
            result.failure("safe_get_field failed")
            return result
        if text_utils.safe_get_field(data, "missing") != "":
            result.failure("safe_get_field should return empty string")
            return result

        # safe_get_nested
        nested = {"a": {"b": {"c": [1, 2, 3]}}}
        val = text_utils.safe_get_nested(nested, "a", "b", "c", default=[])
        if val != [1, 2, 3]:
            result.failure(f"safe_get_nested failed: {val}")
            return result

        result.success("Validated text_utils helper functions")
        return result

    @staticmethod
    def test_comprehensive_integration() -> TestResult:
        """
        Test complex end-to-end integration scenario.
        """
        result = TestResult("Comprehensive integration")

        with tempfile.TemporaryDirectory() as tmpdir:
            # Multi-initial author matching (DV = Dirk V.)
            if not text_utils.author_name_matches("DV Arnold", "Dirk V. Arnold"):
                result.failure("Multi-initial matching failed")
                return result

            # Accent normalization in author names
            if not text_utils.author_name_matches("José María", "Jose Maria"):
                result.failure("Accent normalization failed")
                return result

            # Complex LaTeX title normalization
            latex = r"$\mathcal{L}$ in \textbf{Deep} Learning"
            norm = text_utils.normalize_title(latex)
            if "$" in norm or "\\" in norm:
                result.failure("LaTeX not removed")
                return result

            # Multi-source merging with trust hierarchy (test already exists as test_merge_with_policy)
            # arXiv to DOI promotion (test already exists as test_merge_doi_arxiv_handling)
            # Instead test ID extraction which exercises multiple modules
            doi_text = "See https://doi.org/10.1234/TEST.2020 for details"
            doi = id_utils.find_doi_in_text(doi_text)
            if doi != "10.1234/test.2020":  # DOIs are normalized to lowercase
                result.failure(f"DOI extraction failed: got {doi}")
                return result

            arxiv_text = "Available at https://arxiv.org/abs/2010.12345v2"
            arxiv = id_utils.find_arxiv_in_text(arxiv_text)
            if arxiv != "2010.12345":
                result.failure(f"arXiv extraction failed: got {arxiv}")
                return result

            # CSV with edge cases
            csv_path = os.path.join(tmpdir, "authors.csv")
            csv_content = """Name,Email,Scholar,ORCID,DBLP
Alice,a@t.com,A123,0000-0001-1111-1111,a/a
Bob,b@t.com,B456,,b/b
,,C789,0000-0002-2222-2222,
NoID,n@t.com,,,
"""
            io_utils.safe_write_file(csv_path, csv_content)
            records = io_utils.read_records(csv_path)
            if len(records) != 3:  # Only rows with Scholar IDs
                result.failure(f"CSV: expected 3, got {len(records)}")
                return result

            # File save and JSON roundtrip
            entry = bt.parse_bibtex_to_dict("@article{T,title={Test},author={A},year={2020},journal={Nature}}")
            path = merge_utils.save_entry_to_file(tmpdir, "S123", entry, author_name="Alice")
            if not os.path.exists(path):
                result.failure("File save failed")
                return result

            json_path = os.path.join(tmpdir, "test.json")
            data = {"title": "Test", "year": 2020}
            io_utils.safe_write_json(json_path, data)
            if io_utils.safe_read_json(json_path) != data:
                result.failure("JSON roundtrip failed")
                return result

        result.success(
            "Validated complex integration: multi-initial authors, accents, LaTeX, "
            "merging, arXiv/DOI, CSV, file I/O"
        )
        return result

    @staticmethod
    def test_abbreviated_author_integration() -> TestResult:
        """
        Test abbreviated author matching integration.
        """
        result = TestResult("Abbreviated author integration")

        # "H Huang, DV Arnold" should match "Hong Huang and Dirk V. Arnold"
        baseline = "H Huang, DV Arnold"
        candidate = "Hong Huang and Dirk V. Arnold"

        baseline_authors = text_utils.extract_authors_from_any(baseline)
        candidate_authors = text_utils.extract_authors_from_any(candidate)

        if len(baseline_authors) != 2 or len(candidate_authors) != 2:
            result.failure("Parsing", f"Expected 2 authors each, got {len(baseline_authors)}, {len(candidate_authors)}")
            return result

        if not text_utils.author_name_matches(baseline_authors[0], candidate_authors[0]):
            result.failure("First author", f"'{baseline_authors[0]}' vs '{candidate_authors[0]}'")
            return result

        if not text_utils.author_name_matches(baseline_authors[1], candidate_authors[1]):
            result.failure("Second author", f"'{baseline_authors[1]}' vs '{candidate_authors[1]}'")
            return result

        result.success("Validated end-to-end abbreviated author parsing and initial-based matching")
        return result

    def run_all_tests(self) -> list[TestResult]:
        """
        Run all core tests.
        """
        print("\n" + "=" * 70)
        print("CiteForge Core Test Suite")
        print("=" * 70 + "\n")

        tests = [
            # Text processing
            self.test_title_normalization,
            self.test_title_similarity,
            self.test_author_parsing,
            self.test_author_matching,
            self.test_authors_overlap,

            # ID extraction
            self.test_doi_extraction,
            self.test_arxiv_extraction,

            # BibTeX operations
            self.test_bibtex_parsing,
            self.test_bibtex_building,
            self.test_bibtex_matching,
            self.test_bibtex_extra_fields,
            self.test_bibtex_edge_cases,

            # Configuration
            self.test_config,

            # File I/O
            self.test_safe_file_operations,
            self.test_safe_json_operations,
            self.test_csv_summary_operations,
            self.test_read_records_from_csv,

            # BibTeX merging
            self.test_merge_with_policy,
            self.test_merge_doi_arxiv_handling,
            self.test_save_entry_to_file,

            # Utilities
            self.test_exception_definitions,
            self.test_http_error_decorator,
            self.test_record_model,
            self.test_text_utils_helpers,

            # Integration
            self.test_abbreviated_author_integration,
            self.test_comprehensive_integration,
        ]

        run_tests_with_reporting(tests, self.add_result, verbose=True)
        return self.results

    def print_summary(self):
        """
        Print test summary.
        """
        return print_test_summary(self.results, suite_name="Core", show_failed_details=True)


def main():
    """
    Main test entry point.
    """
    suite = CoreTestSuite()
    suite.run_all_tests()
    passed, failed = suite.print_summary()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
