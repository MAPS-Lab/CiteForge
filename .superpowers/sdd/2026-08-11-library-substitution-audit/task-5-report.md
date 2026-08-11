# Task 5 report

## Outcome

Task 5 is implemented. `run_all()` now retrieves completed futures with `future.result()` and retains the whole-pool `as_completed(..., timeout=author_timeout * len(records))` deadline and pending-author logging. The medium-similarity fixture now has a hard deterministic bound.

## TDD evidence

- Red scheduler regression: `pytest tests/test_scheduler_retry.py::test_completed_future_result_is_retrieved_without_fake_timeout -v --tb=short` failed with the recording future observing `{'timeout': 30}` instead of no arguments.
- Red run was corrected for the repository environment by using `.venv/bin/pytest`; the system interpreter lacked `bibtexparser`.
- Green scheduler regression: same focused command passed, with `result_calls == [((), {})]` and aggregate timeout `1800`.
- Green similarity gate: `pytest tests/test_deduplication.py -k similarity -v --tb=short` passed both similarity tests.

The scheduler test uses a behavior-oriented recording Future and `as_completed` seam rather than source introspection. This directly proves the completed future receives no artificial per-result timeout while asserting the aggregate deadline remains `1800` seconds for one record.

## Verification

- `.venv/bin/pytest tests/test_scheduler_retry.py tests/test_deduplication.py tests/test_pipeline.py -v --tb=short` passed, 25 tests.
- `.venv/bin/ruff check citeforge/pipeline/scheduler.py tests/test_scheduler_retry.py tests/test_deduplication.py` passed.
- `.venv/bin/mypy citeforge/ main.py` passed with no issues in 37 source files.
- `git diff --check` passed.

## Files

- `citeforge/pipeline/scheduler.py`
- `tests/test_scheduler_retry.py`
- `tests/test_deduplication.py`

## Commit

`fix: make timeout and similarity gates truthful` (the final commit hash is reported by `git log --oneline -1` after commit verification).

## Concerns

No known concerns. The repository's system Python is missing project dependencies, so verification uses the checked-in `.venv`.
