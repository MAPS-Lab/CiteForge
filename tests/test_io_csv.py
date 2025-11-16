from __future__ import annotations

import csv
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from CiteForge.io_utils import init_summary_csv, append_summary_to_csv
from tests.test_utils import run_tests_with_reporting, print_test_summary
from tests.test_result import BaseTestResult as CSVTestResult


class CSVTestSuite:
    """
    Test suite for CSV summary export functionality that tracks enrichment
    statistics for every processed publication, providing critical visibility
    into data quality and source coverage across the entire bibliography.
    """
    def __init__(self):
        self.results = []
        self.temp_dir = None

    def setup_temp_dir(self):
        """
        Create temporary directory for test outputs to avoid polluting the
        actual output directory during test execution.
        """
        self.temp_dir = tempfile.mkdtemp(prefix="citeforge_csv_test_")
        return self.temp_dir

    def cleanup_temp_dir(self):
        """
        Clean up temporary directory after tests to prevent disk space buildup
        from repeated test runs.
        """
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None

    def add_result(self, result: CSVTestResult):
        """
        Record test result for summary reporting.
        """
        self.results.append(result)

    def test_csv_initialization(self) -> CSVTestResult:
        """
        Verify that CSV summary initialization creates a file with the correct
        header structure, establishing the 13-column format that tracks file
        paths, total enrichment hits, and individual source success flags for
        comprehensive quality analysis.
        """
        result = CSVTestResult("CSV Initialization")

        try:
            out_dir = self.setup_temp_dir()
            csv_path = os.path.join(out_dir, 'summary.csv')

            print(f"      - Creating CSV at: {csv_path}")
            init_summary_csv(csv_path)

            if not os.path.exists(csv_path):
                result.failure("CSV file was not created")
                return result

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

            if header != expected_columns:
                result.failure(f"Header mismatch. Expected {len(expected_columns)} columns, got {len(header)}")
                result.details['expected'] = expected_columns
                result.details['actual'] = header
                return result

            result.details['csv_path'] = csv_path
            result.details['columns'] = len(header)
            result.success("CSV initialized with correct headers")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")
        finally:
            self.cleanup_temp_dir()

        return result

    def test_csv_append_single_entry(self) -> CSVTestResult:
        """
        Confirm that appending a single entry correctly encodes the file path,
        trust hit count, and boolean source flags into CSV format, ensuring
        each enrichment result is accurately recorded for quality analysis.
        """
        result = CSVTestResult("CSV Append Single Entry")

        try:
            out_dir = self.setup_temp_dir()
            csv_path = os.path.join(out_dir, 'summary.csv')

            init_summary_csv(csv_path)

            # simulate entry enriched by 5 sources (scholar_bib, s2, crossref, openalex, doi_csl)
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

            print(f"      - Appending entry with trust_hits={trust_hits}")
            append_summary_to_csv(csv_path, file_path, trust_hits, flags)

            # verify CSV contains header + 1 data row
            with open(csv_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if len(lines) != 2:  # header + 1 row
                result.failure(f"Expected 2 lines, got {len(lines)}")
                return result

            # verify data row encoding
            data_row = lines[1].strip().split(',')

            if data_row[0] != file_path:
                result.failure("File path mismatch")
                return result

            if data_row[1] != str(trust_hits):
                result.failure(f"Trust hits mismatch: expected {trust_hits}, got {data_row[1]}")
                return result

            # verify boolean flags encoded as 1/0 (scholar_bib=True → '1', scholar_page=False → '0')
            if data_row[2] != '1' or data_row[3] != '0':
                result.failure("Flags not encoded correctly")
                return result

            result.details['row_count'] = len(lines) - 1
            result.details['trust_hits'] = trust_hits
            result.success("Entry appended correctly")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")
        finally:
            self.cleanup_temp_dir()

        return result

    def test_csv_append_multiple_entries(self) -> CSVTestResult:
        """
        Verify that multiple entries can be appended sequentially, building a
        complete enrichment report that allows filtering by trust_hits to
        identify low-quality entries requiring manual review or re-processing.
        """
        result = CSVTestResult("CSV Append Multiple Entries")

        try:
            out_dir = self.setup_temp_dir()
            csv_path = os.path.join(out_dir, 'summary.csv')

            init_summary_csv(csv_path)

            # simulate 3 entries with varying enrichment quality: high (5), zero (0), medium (3)
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
                # build complete flags dict with False defaults
                flags = {
                    'scholar_bib': False, 'scholar_page': False, 's2': False,
                    'crossref': False, 'openreview': False, 'arxiv': False,
                    'openalex': False, 'pubmed': False, 'europepmc': False,
                    'doi_csl': False, 'doi_bibtex': False,
                }
                flags.update(partial_flags)
                append_summary_to_csv(csv_path, file_path, trust_hits, flags)

            print(f"      - Appended {len(test_entries)} entries")

            # verify all entries recorded correctly
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            if len(rows) != len(test_entries):
                result.failure(f"Expected {len(test_entries)} rows, got {len(rows)}")
                return result

            # verify each entry's file path and trust hit count
            for i, (expected_path, expected_hits, _) in enumerate(test_entries):
                if rows[i]['file_path'] != expected_path:
                    result.failure(f"Row {i}: file path mismatch")
                    return result
                if int(rows[i]['trust_hits']) != expected_hits:
                    result.failure(f"Row {i}: trust hits mismatch")
                    return result

            # verify zero enrichment entry (critical for identifying incomplete data)
            if int(rows[1]['trust_hits']) != 0:
                result.failure("Zero enrichment entry not handled correctly")
                return result

            result.details['total_entries'] = len(rows)
            result.details['zero_enrichment_count'] = sum(1 for r in rows if int(r['trust_hits']) == 0)
            result.success("Multiple entries appended correctly")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")
        finally:
            self.cleanup_temp_dir()

        return result

    def test_csv_edge_cases(self) -> CSVTestResult:
        """
        Validate robustness against edge cases including very long file paths
        and special characters in author directories, ensuring the CSV writer
        preserves all path characters without corruption or truncation.
        """
        result = CSVTestResult("CSV Edge Cases")

        try:
            out_dir = self.setup_temp_dir()
            csv_path = os.path.join(out_dir, 'summary.csv')

            init_summary_csv(csv_path)

            # test with very long path (200+ chars) to ensure no truncation
            long_path = "output/" + "a" * 200 + "/Paper.bib"
            flags = {'scholar_bib': True}
            flags_complete = {k: flags.get(k, False) for k in [
                'scholar_bib', 'scholar_page', 's2', 'crossref', 'openreview',
                'arxiv', 'openalex', 'pubmed', 'europepmc', 'doi_csl', 'doi_bibtex'
            ]}

            print(f"      - Testing long path ({len(long_path)} chars)")
            append_summary_to_csv(csv_path, long_path, 1, flags_complete)

            # test with special characters common in Scholar IDs and filenames
            special_path = "output/Author (ID123)/Paper-2024_v2.bib"
            print(f"      - Testing special characters in path")
            append_summary_to_csv(csv_path, special_path, 2, flags_complete)

            # verify both paths preserved exactly
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                rows = list(reader)

            if len(rows) != 2:
                result.failure(f"Expected 2 rows, got {len(rows)}")
                return result

            if rows[0]['file_path'] != long_path:
                result.failure("Long path not preserved correctly")
                return result

            if rows[1]['file_path'] != special_path:
                result.failure("Special characters not preserved correctly")
                return result

            result.details['tested_cases'] = ['long_path', 'special_characters']
            result.success("Edge cases handled correctly")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")
        finally:
            self.cleanup_temp_dir()

        return result

    def test_csv_directory_creation(self) -> CSVTestResult:
        """
        Confirm that init_summary_csv automatically creates parent directories
        as needed, preventing write failures when output paths have nested
        subdirectories that don't yet exist.
        """
        result = CSVTestResult("CSV Directory Creation")

        try:
            out_dir = self.setup_temp_dir()
            # use nested path that doesn't exist to test directory creation
            csv_path = os.path.join(out_dir, 'deep', 'nested', 'path', 'summary.csv')

            print(f"      - Creating CSV in nested directory")
            init_summary_csv(csv_path)

            if not os.path.exists(csv_path):
                result.failure("CSV file was not created in nested directory")
                return result

            # verify all parent directories were created
            parent_dir = os.path.dirname(csv_path)
            if not os.path.exists(parent_dir):
                result.failure("Parent directory was not created")
                return result

            result.details['csv_path'] = csv_path
            result.details['depth'] = len(Path(csv_path).parts) - len(Path(out_dir).parts)
            result.success("Parent directories created successfully")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")
        finally:
            self.cleanup_temp_dir()

        return result

    def run_all_tests(self) -> list[CSVTestResult]:
        """
        Execute all CSV summary tests in sequence, validating that the
        enrichment statistics export pipeline correctly handles initialization,
        appending, edge cases, and directory creation.
        """
        print("\n" + "=" * 70)
        print("CiteForge CSV Summary Test Suite")
        print("=" * 70 + "\n")

        tests = [
            self.test_csv_initialization,
            self.test_csv_append_single_entry,
            self.test_csv_append_multiple_entries,
            self.test_csv_edge_cases,
            self.test_csv_directory_creation,
        ]

        run_tests_with_reporting(tests, self.add_result, verbose=True)
        return self.results

    def print_summary(self):
        """
        Display pass/fail summary and return counts for integration into the
        master test runner's consolidated report.
        """
        return print_test_summary(self.results, suite_name="CSV Test", show_failed_details=True)


def main():
    """
    Execute CSV summary tests and return appropriate exit code for continuous
    integration workflows, signaling test runner tools about success or failure.
    """
    suite = CSVTestSuite()
    suite.run_all_tests()
    passed, failed = suite.print_summary()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
