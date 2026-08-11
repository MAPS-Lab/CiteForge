from __future__ import annotations

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


def test_nonempty_scholar_error_status_still_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    response = {
        "articles": [{"title": "Ready", "year": "2026"}],
        "search_metadata": {"status": "error"},
        "error": "quota exhausted",
    }
    monkeypatch.setattr(scheduler, "fetch_author_publications", lambda *args, **kwargs: response)
    with pytest.raises(RuntimeError, match="quota exhausted"):
        scheduler.process_record("key", None, Record("Ada", scholar_id="author-id"), str(tmp_path), max_pubs=0)
