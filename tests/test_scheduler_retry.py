from __future__ import annotations

import threading
from concurrent.futures import TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any

import pytest
import requests

from citeforge.models import Record
from citeforge.pipeline import scheduler


@pytest.mark.parametrize(
    ("responses", "expected_calls", "expected_sleeps", "warning_count", "terminal_warning"),
    [
        ([{"articles": [{"title": "Ready"}]}], 1, [], 0, None),
        (
            [{"articles": []}, {"articles": [{"title": "Ready"}]}],
            2,
            [2.0],
            1,
            None,
        ),
        (
            [{}, {"articles": []}, {"articles": [{"title": "Ready"}]}],
            3,
            [2.0, 4.0],
            2,
            None,
        ),
        (
            [{}, {"articles": []}, {"articles": []}],
            3,
            [2.0, 4.0],
            4,
            "Scholar API failed after 3 attempts; continuing with DBLP only",
        ),
    ],
)
def test_scholar_result_retry_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    responses: list[dict[str, Any]],
    expected_calls: int,
    expected_sleeps: list[float],
    warning_count: int,
    terminal_warning: str | None,
) -> None:
    calls = 0
    dblp_calls = 0
    sleeps: list[float] = []
    warnings: list[str] = []

    def fetch(*args: object, **kwargs: object) -> dict[str, Any]:
        nonlocal calls
        response = responses[calls]
        calls += 1
        return response

    def fetch_dblp(*args: object, **kwargs: object) -> list[dict[str, Any]]:
        nonlocal dblp_calls
        dblp_calls += 1
        return []

    monkeypatch.setattr(scheduler, "fetch_author_publications", fetch)
    monkeypatch.setattr(scheduler, "dblp_fetch_for_author", fetch_dblp)
    monkeypatch.setattr(scheduler.time, "sleep", sleeps.append)
    monkeypatch.setattr(scheduler.logger, "warn", lambda message, **_kwargs: warnings.append(message))
    monkeypatch.setattr(scheduler, "get_min_year", lambda: 2020)
    monkeypatch.setattr(scheduler, "merge_publication_lists", lambda scholar, _dblp, **_kwargs: scholar)
    monkeypatch.setattr(scheduler, "sort_articles_by_year_current_first", lambda articles: articles)

    result = scheduler.process_record(
        "key", None, Record("Ada", scholar_id="author-id", dblp="dblp-id"), str(tmp_path), max_pubs=0
    )

    assert result == 0
    assert calls == expected_calls
    assert dblp_calls == 1
    assert sleeps == expected_sleeps
    assert len(warnings) == warning_count
    if terminal_warning is not None:
        assert terminal_warning in warnings


def test_scholar_result_policy_does_not_retry_exceptions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = 0

    def fetch(*args: object, **kwargs: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise requests.exceptions.Timeout("upstream timed out")

    monkeypatch.setattr(scheduler, "fetch_author_publications", fetch)
    with pytest.raises(requests.exceptions.Timeout):
        scheduler.process_record("key", None, Record("Ada", scholar_id="author-id"), str(tmp_path), max_pubs=0)
    assert calls == 1


def test_scholar_result_retry_uses_configured_policy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls = 0
    sleeps: list[float] = []
    warnings: list[str] = []

    def fetch(*args: object, **kwargs: object) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"articles": []}

    monkeypatch.setattr(scheduler, "SCHOLAR_FETCH_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(scheduler, "SCHOLAR_FETCH_BACKOFF_INITIAL", 0.5)
    monkeypatch.setattr(scheduler, "SCHOLAR_FETCH_BACKOFF_MAX", 0.5)
    monkeypatch.setattr(scheduler, "fetch_author_publications", fetch)
    monkeypatch.setattr(scheduler, "dblp_fetch_for_author", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(scheduler.time, "sleep", sleeps.append)
    monkeypatch.setattr(scheduler.logger, "warn", lambda message, **_kwargs: warnings.append(message))
    monkeypatch.setattr(scheduler, "get_min_year", lambda: 2020)
    monkeypatch.setattr(scheduler, "merge_publication_lists", lambda scholar, _dblp, **_kwargs: scholar)
    monkeypatch.setattr(scheduler, "sort_articles_by_year_current_first", lambda articles: articles)

    scheduler.process_record("key", None, Record("Ada", scholar_id="author-id"), str(tmp_path), max_pubs=0)

    assert calls == 2
    assert sleeps == [0.5]
    assert "attempt 1/2" in warnings[0]
    assert any("failed after 2 attempts" in message for message in warnings)


def test_nonempty_scholar_error_status_still_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    response = {
        "articles": [{"title": "Ready", "year": "2026"}],
        "search_metadata": {"status": "error"},
        "error": "quota exhausted",
    }
    monkeypatch.setattr(scheduler, "fetch_author_publications", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match="quota exhausted"):
        scheduler.process_record("key", None, Record("Ada", scholar_id="author-id"), str(tmp_path), max_pubs=0)


def test_completed_future_result_is_retrieved_without_fake_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    result_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    completion_warning_thresholds: list[float] = []

    class RecordingFuture:
        def result(self, *args: object, **kwargs: object) -> int:
            result_calls.append((args, kwargs))
            return 3

        def done(self) -> bool:
            return True

    class RecordingExecutor:
        def __init__(self, **_kwargs: object) -> None:
            self.future = RecordingFuture()

        def __enter__(self) -> RecordingExecutor:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def submit(self, *_args: object, **_kwargs: object) -> RecordingFuture:
            return self.future

    def recording_as_completed(future_to_author: dict[RecordingFuture, Record], *, timeout: float):
        completion_warning_thresholds.append(timeout)
        yield next(iter(future_to_author))

    monkeypatch.setattr(scheduler, "ThreadPoolExecutor", RecordingExecutor)
    monkeypatch.setattr(scheduler, "as_completed", recording_as_completed)
    monkeypatch.setattr(scheduler.logger, "step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler.logger, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler.logger, "success", lambda *_args, **_kwargs: None)

    result = scheduler.run_all(
        "key",
        None,
        None,
        None,
        None,
        [Record("Ada", scholar_id="author-id")],
        str(tmp_path),
        None,
        False,
    )

    assert result == (3, 1)
    assert result_calls == [((), {})]
    assert completion_warning_thresholds == [1800]


def test_completion_after_warning_threshold_is_counted_once(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    threshold_logged = threading.Event()
    warnings: list[str] = []
    successes: list[str] = []

    def delayed_result(*_args: object, **_kwargs: object) -> int:
        assert threshold_logged.wait(timeout=1.0)
        return 5

    def hit_warning_threshold(*_args: object, **_kwargs: object) -> None:
        raise FuturesTimeoutError

    def record_warning(message: str, **_kwargs: object) -> None:
        warnings.append(message)
        threshold_logged.set()

    monkeypatch.setattr(scheduler, "process_record", delayed_result)
    monkeypatch.setattr(scheduler, "as_completed", hit_warning_threshold)
    monkeypatch.setattr(scheduler.logger, "step", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler.logger, "info", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(scheduler.logger, "warn", record_warning)
    monkeypatch.setattr(scheduler.logger, "success", lambda message, **_kwargs: successes.append(message))

    result = scheduler.run_all(
        "key",
        None,
        None,
        None,
        None,
        [Record("Ada", scholar_id="author-id")],
        str(tmp_path),
        None,
        False,
    )

    assert result == (5, 1)
    assert len(warnings) == 1
    assert "completion warning threshold" in warnings[0].lower()
    assert "wait" in warnings[0].lower()
    assert len(successes) == 1
