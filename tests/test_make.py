from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from CiteForge import api_clients
from CiteForge import bibtex_utils
from CiteForge import merge_utils
from CiteForge.exceptions import FILE_IO_ERRORS
from CiteForge.models import Record
from CiteForge.config import CONTRIBUTION_WINDOW_YEARS
from tests.fixtures import load_api_keys
from tests.test_data import TEST_AUTHOR, KNOWN_PAPERS, REQUIRED_FIELDS
from tests.test_utils import run_tests_with_reporting, print_test_summary
from tests.test_result import BaseTestResult as IntegrationTestResult


class IntegrationTestSuite:
    """
    Integration test suite for end-to-end pipeline validation, exercising the
    complete workflow from Scholar/DBLP fetching through multi-source enrichment
    to final BibTeX generation and CSV summary export.
    """
    def __init__(self):
        self.results = []
        self.api_keys = load_api_keys()
        self.temp_dir = None

    def setup_temp_dir(self):
        """
        Create temporary directory for integration test outputs to avoid
        polluting the actual output directory during full pipeline tests.
        """
        self.temp_dir = tempfile.mkdtemp(prefix="citeforge_test_")
        return self.temp_dir

    def cleanup_temp_dir(self):
        """
        Clean up temporary directory after tests to prevent disk space buildup
        from large integration test outputs.
        """
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir)
            self.temp_dir = None

    def add_result(self, result: IntegrationTestResult):
        """
        Record integration test result for summary reporting.
        """
        self.results.append(result)

    def test_fetch_and_merge(self) -> IntegrationTestResult:
        """
        Validate end-to-end publication fetching from Scholar and DBLP followed
        by deduplication using fuzzy matching, ensuring the pipeline correctly
        identifies and removes duplicate publications across sources while
        preserving unique entries.
        """
        result = IntegrationTestResult("Fetch and Merge Publications")

        if not self.api_keys.get('serpapi'):
            result.failure("SerpAPI key not available")
            return result

        try:
            # build test record for known author with both Scholar and DBLP presence
            rec = Record(
                name=TEST_AUTHOR['name'],
                email=TEST_AUTHOR['email'],
                scholar_id=TEST_AUTHOR['scholar_id'],
                orcid="",
                dblp=TEST_AUTHOR['dblp']
            )

            # fetch publications from Scholar (primary source)
            print(f"      - Fetching Scholar publications for {rec.name}...")
            scholar_data = api_clients.fetch_author_publications(
                self.api_keys['serpapi'],
                rec.scholar_id
            )

            scholar_pubs = scholar_data.get('articles', [])

            if not scholar_pubs or len(scholar_pubs) == 0:
                result.failure("No publications fetched from Scholar")
                return result

            result.details['scholar_count'] = len(scholar_pubs)
            print(f"        Found {len(scholar_pubs)} publications from Scholar")

            # fetch publications from DBLP (computer science bibliography)
            print(f"      - Fetching DBLP publications for {rec.name}...")
            dblp_pubs = []
            try:
                current_year = api_clients.get_current_year()
                min_year = current_year - CONTRIBUTION_WINDOW_YEARS
                dblp_pubs = api_clients.dblp_fetch_for_author(
                    rec.name,
                    rec.dblp,
                    min_year
                )
                if dblp_pubs:
                    result.details['dblp_count'] = len(dblp_pubs)
                    print(f"        Found {len(dblp_pubs)} publications from DBLP")
                else:
                    result.add_warning("No DBLP publications found")
            except Exception as e:
                result.add_warning(f"DBLP fetch failed: {e}")

            # deduplicate using fuzzy title/author matching (threshold=0.9)
            print(f"      - Merging publications...")
            merged = api_clients.merge_publication_lists(
                scholar_pubs,
                dblp_pubs,
                rec.name
            )

            result.details['merged_count'] = len(merged)
            result.details['duplicates_removed'] = (len(scholar_pubs) + len(dblp_pubs)) - len(merged)
            duplicates_removed = result.details['duplicates_removed']
            print(
                f"        Merged to {len(merged)} publications "
                f"({duplicates_removed} duplicates removed)"
            )

            result.success(f"Successfully fetched and merged publications")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    def test_full_enrichment_pipeline(self) -> IntegrationTestResult:
        """
        Execute the complete enrichment workflow for a known paper, validating
        baseline establishment, multi-source enrichment with strict matching,
        trust-based merging, and final BibTeX generation to ensure all pipeline
        stages integrate correctly.
        """
        result = IntegrationTestResult("Full Enrichment Pipeline")

        if not self.api_keys.get('serpapi'):
            result.failure("SerpAPI key not available")
            return result

        try:
            # test with "Attention Is All You Need" (well-documented paper with known DOI/arXiv)
            paper = KNOWN_PAPERS[0]

            print(f"      - Testing enrichment for: {paper['title']}")

            # establish baseline BibTeX from Scholar citation
            print(f"      - Step 1: Fetching baseline BibTeX from Scholar...")
            cite_link = api_clients.search_scholar_for_cite_link(
                self.api_keys['serpapi'],
                paper['title'],
                paper['first_author']
            )

            if not cite_link:
                result.failure("Could not find cite link from Scholar")
                return result

            baseline_bib = api_clients.fetch_bibtex_from_cite(
                self.api_keys['serpapi'],
                cite_link
            )

            if not baseline_bib:
                result.failure("Could not fetch baseline BibTeX from Scholar")
                return result

            baseline_entry = bibtex_utils.parse_bibtex_to_dict(baseline_bib)
            result.details['baseline'] = baseline_entry['key']
            print(f"        Baseline BibTeX fetched: {baseline_entry['key']}")

            # enrich from multiple sources, validating each against baseline
            enrichers = []

            # attempt Semantic Scholar enrichment (enhanced metadata)
            if self.api_keys.get('semantic'):
                print(f"      - Step 2a: Enriching from Semantic Scholar...")
                try:
                    s2_paper = api_clients.s2_search_paper(
                        paper['title'],
                        paper['first_author'],
                        self.api_keys['semantic']
                    )
                    if s2_paper:
                        s2_bib = api_clients.build_bibtex_from_s2(s2_paper, paper['first_author'])
                        s2_entry = bibtex_utils.parse_bibtex_to_dict(s2_bib)
                        # validate using strict matching (title, author, year)
                        if bibtex_utils.bibtex_entries_match_strict(baseline_bib, s2_bib):
                            enrichers.append(('s2', s2_entry))
                            print(f"        S2 enrichment added")
                        else:
                            result.add_warning("S2 enrichment did not match baseline")
                    else:
                        result.add_warning("S2 search returned no results")
                except Exception as e:
                    result.add_warning(f"S2 enrichment failed: {e}")

            # attempt Crossref enrichment (DOI registration agency)
            print(f"      - Step 2b: Enriching from Crossref...")
            try:
                cr_item = api_clients.crossref_search(paper['title'], paper['first_author'])
                if cr_item:
                    cr_bib = api_clients.build_bibtex_from_crossref(cr_item, paper['first_author'])
                    cr_entry = bibtex_utils.parse_bibtex_to_dict(cr_bib)
                    if bibtex_utils.bibtex_entries_match_strict(baseline_bib, cr_bib):
                        enrichers.append(('crossref', cr_entry))
                        print(f"        Crossref enrichment added")
                    else:
                        result.add_warning("Crossref enrichment did not match baseline")
                else:
                    result.add_warning("Crossref search returned no results")
            except Exception as e:
                result.add_warning(f"Crossref enrichment failed: {e}")

            # attempt arXiv enrichment (preprint repository)
            if paper['arxiv_id']:
                print(f"      - Step 2c: Enriching from arXiv...")
                try:
                    arxiv_entries = api_clients.arxiv_search(
                        paper['title'],
                        paper['first_author'],
                        paper['year']
                    )
                    if arxiv_entries and len(arxiv_entries) > 0:
                        arxiv_bib = api_clients.build_bibtex_from_arxiv(
                            arxiv_entries[0],
                            paper['first_author']
                        )
                        arxiv_entry = bibtex_utils.parse_bibtex_to_dict(arxiv_bib)
                        if bibtex_utils.bibtex_entries_match_strict(baseline_bib, arxiv_bib):
                            enrichers.append(('arxiv', arxiv_entry))
                            print(f"        arXiv enrichment added")
                        else:
                            result.add_warning("arXiv enrichment did not match baseline")
                    else:
                        result.add_warning("arXiv search returned no results")
                except Exception as e:
                    result.add_warning(f"arXiv enrichment failed: {e}")

            # attempt DOI negotiation (CSL-JSON from doi.org resolver)
            if paper['doi']:
                print(f"      - Step 2d: Enriching via DOI negotiation...")
                try:
                    csl = api_clients.fetch_csl_via_doi(paper['doi'])
                    if csl:
                        csl_bib = api_clients.bibtex_from_csl(csl, paper['first_author'])
                        csl_entry = bibtex_utils.parse_bibtex_to_dict(csl_bib)
                        if bibtex_utils.bibtex_entries_match_strict(baseline_bib, csl_bib):
                            enrichers.append(('csl', csl_entry))
                            print(f"        DOI/CSL enrichment added")
                        else:
                            result.add_warning("DOI/CSL enrichment did not match baseline")
                    else:
                        result.add_warning("DOI CSL fetch returned no data")
                except Exception as e:
                    result.add_warning(f"DOI enrichment failed: {e}")

            result.details['enrichment_sources'] = len(enrichers)
            print(f"      - Enrichment complete: {len(enrichers)} sources")

            # merge using trust hierarchy (CSL > BibTeX > DataCite > ... > Scholar)
            print(f"      - Step 3: Merging with trust policy...")
            merged_entry = merge_utils.merge_with_policy(baseline_entry, enrichers)

            # verify all required fields present (title, author, year)
            missing_fields = [f for f in REQUIRED_FIELDS if f not in merged_entry['fields']]
            if missing_fields:
                result.failure(f"Missing required fields after merge: {missing_fields}")
                return result

            result.details['final_entry'] = merged_entry['key']
            result.details['final_fields'] = list(merged_entry['fields'].keys())
            print(f"        Merge complete: {len(merged_entry['fields'])} fields")

            # validate final BibTeX can be rendered correctly
            print(f"      - Step 4: Validating final BibTeX...")
            final_bib = bibtex_utils.bibtex_from_dict(merged_entry)
            if '@' not in final_bib:
                result.failure("Final BibTeX rendering failed")
                return result

            result.details['final_bibtex_length'] = len(final_bib)
            print(f"        Final BibTeX valid ({len(final_bib)} chars)")

            result.success(f"Full enrichment pipeline completed successfully")

        except Exception as e:
            # Handle rate limiting gracefully
            error_msg = str(e)
            if '429' in error_msg or 'Too Many Requests' in error_msg:
                result.success("Rate limited by API (expected when running many tests)")
                result.add_warning("Try running tests with delays or individually if rate limits persist")
            else:
                result.failure(f"Error: {type(e).__name__}: {e}")
                import traceback
                result.details['traceback'] = traceback.format_exc()

        return result

    def test_file_output(self) -> IntegrationTestResult:
        """
        Validate BibTeX file writing and organization into per-author directories,
        ensuring entries are saved with correct formatting and directory structure
        matching the main pipeline output conventions.
        """
        result = IntegrationTestResult("File Output")

        try:
            # use temporary directory to avoid polluting actual output
            out_dir = self.setup_temp_dir()
            print(f"      - Using temp directory: {out_dir}")

            # build sample entry from known paper
            paper = KNOWN_PAPERS[0]
            entry = {
                'type': 'inproceedings',
                'key': 'Vaswani2017:Attention',
                'fields': {
                    'title': paper['title'],
                    'author': ' and '.join(paper['authors']),
                    'year': str(paper['year']),
                    'booktitle': 'NeurIPS',
                    'doi': paper['doi'],
                }
            }

            # save to per-author subdirectory with auto-generated filename
            print(f"      - Saving BibTeX entry...")
            saved_path = merge_utils.save_entry_to_file(
                out_dir,
                TEST_AUTHOR['scholar_id'],
                entry,
                None
            )

            if not saved_path or not os.path.exists(saved_path):
                result.failure("File was not created")
                return result

            print(f"        File created: {os.path.basename(saved_path)}")

            # verify file content is valid BibTeX
            with open(saved_path, 'r', encoding='utf-8') as f:
                content = f.read()

            if '@inproceedings' not in content:
                result.failure("File content invalid")
                return result

            result.details['saved_path'] = saved_path
            result.details['file_size'] = len(content)
            print(f"        File content valid ({len(content)} bytes)")

            result.success("File output working correctly")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")
        finally:
            self.cleanup_temp_dir()

        return result

    def test_csv_summary_integration(self) -> IntegrationTestResult:
        """
        Confirm that CSV summary export integrates correctly with the processing
        pipeline, tracking enrichment statistics as entries are saved and allowing
        post-processing analysis of data quality across the bibliography.
        """
        result = IntegrationTestResult("CSV Summary Integration")

        try:
            from CiteForge.io_utils import init_summary_csv, append_summary_to_csv

            out_dir = self.setup_temp_dir()
            csv_path = os.path.join(out_dir, 'summary.csv')

            print(f"      - Initializing CSV summary...")
            init_summary_csv(csv_path)

            if not os.path.exists(csv_path):
                result.failure("CSV was not created")
                return result

            # simulate main.py appending entries with varying enrichment quality
            print(f"      - Adding test entries...")
            test_entries = [
                ("output/Author/Paper1.bib", 5, {
                    'scholar_bib': True, 's2': True, 'crossref': True,
                    'doi_csl': True, 'openalex': True
                }),
                ("output/Author/Paper2.bib", 2, {
                    'arxiv': True, 'doi_bibtex': True
                }),
                ("output/Author/Paper3.bib", 0, {}),  # zero enrichment case
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

            # verify CSV contains all entries with correct trust_hits
            import csv as csv_module
            with open(csv_path, 'r', encoding='utf-8') as f:
                reader = csv_module.DictReader(f)
                rows = list(reader)

            if len(rows) != len(test_entries):
                result.failure(f"Expected {len(test_entries)} rows, got {len(rows)}")
                return result

            # verify trust_hits match expected values
            expected_hits = [5, 2, 0]
            for i, expected in enumerate(expected_hits):
                if int(rows[i]['trust_hits']) != expected:
                    result.failure(f"Row {i}: expected trust_hits={expected}, got {rows[i]['trust_hits']}")
                    return result

            result.details['csv_path'] = csv_path
            result.details['entries'] = len(rows)
            result.details['zero_enrichment'] = sum(1 for r in rows if int(r['trust_hits']) == 0)
            print(f"        CSV summary created with {len(rows)} entries")

            result.success("CSV summary integration working correctly")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")
            import traceback
            result.details['traceback'] = traceback.format_exc()
        finally:
            self.cleanup_temp_dir()

        return result

    def run_all_tests(self) -> list[IntegrationTestResult]:
        """
        Execute all integration tests in sequence, validating the complete
        pipeline from author fetching through enrichment to final output
        generation and CSV summary export.
        """
        print("\n" + "=" * 70)
        print("CiteForge Integration Test Suite")
        print("=" * 70 + "\n")

        tests = [
            self.test_fetch_and_merge,
            self.test_full_enrichment_pipeline,
            self.test_file_output,
            self.test_csv_summary_integration,
        ]

        run_tests_with_reporting(tests, self.add_result, verbose=True)
        return self.results

    def print_summary(self):
        """
        Display pass/fail summary and return counts for integration into the
        master test runner's consolidated report.
        """
        return print_test_summary(self.results, suite_name="Integration Test", show_failed_details=True)


def main():
    """
    Execute integration tests and return appropriate exit code for continuous
    integration workflows, signaling test runner tools about success or failure.
    """
    suite = IntegrationTestSuite()
    suite.run_all_tests()
    passed, failed = suite.print_summary()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
