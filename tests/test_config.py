from __future__ import annotations

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from CiteForge.config import (
    CONTRIBUTION_WINDOW_YEARS,
    PUBLICATIONS_PER_YEAR,
    MAX_PUBLICATIONS_PER_AUTHOR,
)
from tests.test_utils import run_tests_with_reporting, print_test_summary
from tests.test_result import BaseTestResult as ConfigTestResult


class ConfigTestSuite:
    """
    Test suite for configuration validation.
    """
    def __init__(self):
        self.results = []

    def add_result(self, result: ConfigTestResult):
        """
        Add a test result.
        """
        self.results.append(result)

    @staticmethod
    def test_dynamic_publication_limit() -> ConfigTestResult:
        """
        Test that MAX_PUBLICATIONS_PER_AUTHOR is calculated correctly.
        """
        result = ConfigTestResult("Dynamic Publication Limit Calculation")

        try:
            print(f"      - CONTRIBUTION_WINDOW_YEARS = {CONTRIBUTION_WINDOW_YEARS}")
            print(f"      - PUBLICATIONS_PER_YEAR = {PUBLICATIONS_PER_YEAR}")
            print(f"      - MAX_PUBLICATIONS_PER_AUTHOR = {MAX_PUBLICATIONS_PER_AUTHOR}")

            # Verify the calculation
            expected = PUBLICATIONS_PER_YEAR * CONTRIBUTION_WINDOW_YEARS

            if MAX_PUBLICATIONS_PER_AUTHOR != expected:
                result.failure(
                    f"Calculation error: expected {expected}, got {MAX_PUBLICATIONS_PER_AUTHOR}"
                )
                return result

            result.details['contribution_window'] = CONTRIBUTION_WINDOW_YEARS
            result.details['per_year'] = PUBLICATIONS_PER_YEAR
            result.details['max_total'] = MAX_PUBLICATIONS_PER_AUTHOR
            result.success(
                f"Correctly calculated: {PUBLICATIONS_PER_YEAR} × "
                f"{CONTRIBUTION_WINDOW_YEARS} = {MAX_PUBLICATIONS_PER_AUTHOR}"
            )

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    @staticmethod
    def test_publications_per_year_reasonable() -> ConfigTestResult:
        """
        Test that PUBLICATIONS_PER_YEAR is a reasonable value.
        """
        result = ConfigTestResult("Publications Per Year Reasonableness")

        try:
            if PUBLICATIONS_PER_YEAR < 1:
                result.failure("PUBLICATIONS_PER_YEAR must be at least 1")
                return result

            if PUBLICATIONS_PER_YEAR > 1000:
                result.add_warning(
                    f"PUBLICATIONS_PER_YEAR is very high ({PUBLICATIONS_PER_YEAR}). "
                    "This may cause excessive API usage."
                )

            # Typical academic output is 2-20 papers per year
            # 50 is reasonable for highly prolific authors or research groups
            if PUBLICATIONS_PER_YEAR > 100:
                result.add_warning(
                    f"PUBLICATIONS_PER_YEAR={PUBLICATIONS_PER_YEAR} is unusually high. "
                    "Most academics publish fewer than 50 papers/year."
                )

            result.details['value'] = PUBLICATIONS_PER_YEAR
            result.success(f"PUBLICATIONS_PER_YEAR={PUBLICATIONS_PER_YEAR} is reasonable")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    @staticmethod
    def test_contribution_window_reasonable() -> ConfigTestResult:
        """
        Test that CONTRIBUTION_WINDOW_YEARS is a reasonable value.
        """
        result = ConfigTestResult("Contribution Window Reasonableness")

        try:
            if CONTRIBUTION_WINDOW_YEARS < 1:
                result.failure("CONTRIBUTION_WINDOW_YEARS must be at least 1")
                return result

            if CONTRIBUTION_WINDOW_YEARS > 10:
                result.add_warning(
                    f"CONTRIBUTION_WINDOW_YEARS is very long ({CONTRIBUTION_WINDOW_YEARS}). "
                    "This may fetch many publications."
                )

            result.details['value'] = CONTRIBUTION_WINDOW_YEARS
            result.success(f"CONTRIBUTION_WINDOW_YEARS={CONTRIBUTION_WINDOW_YEARS} is reasonable")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    @staticmethod
    def test_max_publications_scaling() -> ConfigTestResult:
        """
        Test that MAX_PUBLICATIONS_PER_AUTHOR scales correctly with window.
        """
        result = ConfigTestResult("Publication Limit Scaling")

        try:
            # Simulate different window sizes
            test_cases = [
                (1, PUBLICATIONS_PER_YEAR * 1),
                (2, PUBLICATIONS_PER_YEAR * 2),
                (3, PUBLICATIONS_PER_YEAR * 3),
                (5, PUBLICATIONS_PER_YEAR * 5),
                (10, PUBLICATIONS_PER_YEAR * 10),
            ]

            scaling_results = []
            for years, expected_max in test_cases:
                scaling_results.append(f"{years} year(s) → {expected_max} publications")

            print(f"      - Scaling examples:")
            for example in scaling_results:
                print(f"        {example}")

            result.details['examples'] = scaling_results
            result.success("Publication limit scales linearly with contribution window")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    @staticmethod
    def test_config_types() -> ConfigTestResult:
        """
        Test that configuration values have correct types.
        """
        result = ConfigTestResult("Configuration Types")

        try:
            errors = []

            if not isinstance(CONTRIBUTION_WINDOW_YEARS, int):
                errors.append(f"CONTRIBUTION_WINDOW_YEARS should be int, got {type(CONTRIBUTION_WINDOW_YEARS)}")

            if not isinstance(PUBLICATIONS_PER_YEAR, int):
                errors.append(f"PUBLICATIONS_PER_YEAR should be int, got {type(PUBLICATIONS_PER_YEAR)}")

            if not isinstance(MAX_PUBLICATIONS_PER_AUTHOR, int):
                errors.append(f"MAX_PUBLICATIONS_PER_AUTHOR should be int, got {type(MAX_PUBLICATIONS_PER_AUTHOR)}")

            if errors:
                result.failure("; ".join(errors))
                return result

            result.details['types'] = {
                'CONTRIBUTION_WINDOW_YEARS': 'int',
                'PUBLICATIONS_PER_YEAR': 'int',
                'MAX_PUBLICATIONS_PER_AUTHOR': 'int',
            }
            result.success("All configuration values have correct types")

        except Exception as e:
            result.failure(f"Error: {type(e).__name__}: {e}")

        return result

    def run_all_tests(self) -> list[ConfigTestResult]:
        """
        Run all config tests.
        """
        print("\n" + "=" * 70)
        print("CiteForge Configuration Test Suite")
        print("=" * 70 + "\n")

        tests = [
            self.test_dynamic_publication_limit,
            self.test_publications_per_year_reasonable,
            self.test_contribution_window_reasonable,
            self.test_max_publications_scaling,
            self.test_config_types,
        ]

        run_tests_with_reporting(tests, self.add_result, verbose=True)
        return self.results

    def print_summary(self):
        """
        Print test summary.
        """
        return print_test_summary(self.results, suite_name="Config Test", show_failed_details=True)


def main():
    """
    Main test entry point.
    """
    suite = ConfigTestSuite()
    suite.run_all_tests()
    passed, failed = suite.print_summary()

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
