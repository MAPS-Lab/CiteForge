from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from tests.core import CoreTestSuite
from tests.apis import APITestSuite
from tests.pipeline import PipelineTestSuite


def print_summary(suites: list[tuple[str, int, int]]):
    """
    Print a consolidated summary table showing pass/fail counts for all test
    suites and calculate the overall success rate, returning a non-zero count
    when any tests fail to signal test runner tools.
    """
    print("\n" + "=" * 70)
    print("TEST SUMMARY")
    print("=" * 70)

    total_passed = 0
    total_failed = 0

    # aggregate results from all test suites
    for name, passed, failed in suites:
        total_passed += passed
        total_failed += failed
        status = "✓" if failed == 0 else "✗"
        print(f"  {status} {name:20} {passed:3} passed, {failed:3} failed")

    # calculate overall statistics
    print("=" * 70)
    total = total_passed + total_failed
    rate = (total_passed / total * 100) if total > 0 else 0
    print(f"TOTAL: {total_passed}/{total} tests passed ({rate:.1f}%)")

    # print final status
    if total_failed == 0:
        print("\n✅ ALL TESTS PASSED!")
    else:
        print(f"\n❌ {total_failed} TEST(S) FAILED")

    print("=" * 70 + "\n")
    return total_failed


def main():
    """
    Execute all CiteForge test suites in sequence and report results, including
    core utilities, API integrations, and critical pipeline components, then
    return an exit code suitable for continuous integration workflows.
    """
    print("\n" + "=" * 70)
    print("RUNNING CITEFORGE TESTS")
    print("=" * 70 + "\n")

    results = []

    # run core utilities tests (text, BibTeX, I/O, merging)
    print("▶ Running Core Tests...")
    core = CoreTestSuite()
    core.run_all_tests()
    core_passed, core_failed = core.print_summary()
    results.append(("Core", core_passed, core_failed))

    # run API integration tests (SerpAPI, Crossref, OpenAlex, etc.)
    print("\n▶ Running API Tests...")
    apis = APITestSuite()
    apis.run_all_tests()
    api_passed, api_failed = apis.print_summary()
    results.append(("API", api_passed, api_failed))

    # run pipeline component tests (DOI validation, multi-candidate)
    print("\n▶ Running Pipeline Tests...")
    pipeline = PipelineTestSuite()
    pipeline.run_all_tests()
    pipeline_passed, pipeline_failed = pipeline.print_summary()
    results.append(("Pipeline", pipeline_passed, pipeline_failed))

    # print consolidated summary and return exit code
    total_failed = print_summary(results)
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
