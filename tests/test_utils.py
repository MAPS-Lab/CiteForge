from __future__ import annotations

from typing import Any, Callable, List


def run_tests_with_reporting(
        test_functions: List[Callable],
        add_result_fn: Callable[[Any], None],
        verbose: bool = True
) -> List[Any]:
    """
    Run a list of test functions and report their results, optionally printing
    progress during execution.
    """
    results = []

    for idx, test_fn in enumerate(test_functions, 1):
        if verbose:
            print(f"[{idx}/{len(test_functions)}] {test_fn.__name__}")

        result = test_fn()
        add_result_fn(result)
        results.append(result)

        if verbose:
            # Print result
            status = "PASS" if result.passed else "FAIL"
            status_symbol = "[+]" if result.passed else "[X]"
            print(f"      {status_symbol} {status}: {result.name}")

            if result.message:
                print(f"          {result.message}")

            # Handle warnings if present
            if hasattr(result, 'warnings') and result.warnings:
                for warning in result.warnings:
                    print(f"          WARNING: {warning}")

            # Handle details
            if result.details:
                detail_str = str(result.details)
                if len(detail_str) > 100:
                    detail_str = detail_str[:100] + "..."
                print(f"          Details: {detail_str}")

            print()

    return results


def print_test_summary(
        results: List[Any],
        suite_name: str = "Test",
        show_failed_details: bool = True
) -> tuple[int, int]:
    """
    Print a summary of test results including passed and failed counts, optionally
    showing details of failed tests.
    """
    total = len(results)
    passed = sum(1 for r in results if r.passed)
    failed = total - passed

    print("\n" + "=" * 70)
    print(f"{suite_name} Summary")
    print("=" * 70)
    print(f"Total Tests:    {total}")
    print(f"Passed:         {passed}")
    print(f"Failed:         {failed}")
    print(f"Success Rate:   {(passed / total * 100):.1f}%" if total > 0 else "Success Rate:   N/A")

    if failed > 0 and show_failed_details:
        print("\nFailed Tests:")
        for r in results:
            if not r.passed:
                print(f"  - {r.name}: {r.message}")

    # Count warnings if present
    total_warnings = sum(len(getattr(r, 'warnings', [])) for r in results)
    if total_warnings > 0:
        print(f"\nTotal Warnings: {total_warnings}")

    print("=" * 70 + "\n")

    return passed, failed
