from __future__ import annotations

from datetime import datetime, timezone

import pytest

from citeforge import config, io_utils
from citeforge.config import (
    _CONTRIBUTION_WINDOW_FALLBACK,
    CONTRIBUTION_WINDOW_YEARS,
    MAX_PUBLICATIONS_PER_AUTHOR,
    MIN_YEAR,
    PUBLICATIONS_PER_YEAR,
    get_min_year,
)
from tests.conftest import api_keys as api_keys_fixture
from tests.test_data import API_CONFIGS


def test_publications_per_year_reasonable() -> None:
    """Test that PUBLICATIONS_PER_YEAR is a reasonable value."""
    assert PUBLICATIONS_PER_YEAR >= 1, "PUBLICATIONS_PER_YEAR must be at least 1"
    assert PUBLICATIONS_PER_YEAR <= 1000, (
        f"PUBLICATIONS_PER_YEAR is very high ({PUBLICATIONS_PER_YEAR}). This may cause excessive API usage."
    )


def test_api_keys_fixture_allows_missing_gemini_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shared live-test fixture preserves Gemini's optional legacy path."""
    monkeypatch.delitem(API_CONFIGS, "gemini", raising=False)
    monkeypatch.setattr(io_utils, "read_serpapi_api_key", lambda _path: None)
    monkeypatch.setattr(io_utils, "read_serply_api_key", lambda _path: None)
    monkeypatch.setattr(io_utils, "read_semantic_api_key", lambda _path: None)
    monkeypatch.setattr(io_utils, "read_openreview_credentials", lambda _path: None)
    gemini_paths: list[str] = []

    def fake_gemini(path: str) -> str:
        gemini_paths.append(path)
        return "gemini-key"

    monkeypatch.setattr(io_utils, "read_gemini_api_key", fake_gemini)

    keys = api_keys_fixture.__wrapped__()  # type: ignore[missing-attribute]  # pytest wraps the fixture fn

    assert keys["gemini"] == "gemini-key"
    assert gemini_paths == ["keys/Gemini.key"]


def test_contribution_window_reasonable() -> None:
    """Test that CONTRIBUTION_WINDOW_YEARS is a reasonable value."""
    assert CONTRIBUTION_WINDOW_YEARS >= 1, "CONTRIBUTION_WINDOW_YEARS must be at least 1"
    assert CONTRIBUTION_WINDOW_YEARS <= 20, f"CONTRIBUTION_WINDOW_YEARS is very long ({CONTRIBUTION_WINDOW_YEARS})."


def test_max_publications_positive() -> None:
    """Test that MAX_PUBLICATIONS_PER_AUTHOR is a positive value."""
    assert MAX_PUBLICATIONS_PER_AUTHOR > 0, (
        f"MAX_PUBLICATIONS_PER_AUTHOR must be positive, got {MAX_PUBLICATIONS_PER_AUTHOR}"
    )


def test_min_year_valid() -> None:
    """Test that MIN_YEAR is either None or a sensible year."""
    assert MIN_YEAR is None or 1900 <= MIN_YEAR <= 2100


def test_get_min_year_fixed(monkeypatch: object) -> None:
    """Test that get_min_year returns the fixed MIN_YEAR when set."""
    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "MIN_YEAR", 2020)
    assert get_min_year() == 2020
    mp.undo()


def test_get_min_year_rolling(monkeypatch: object) -> None:
    """Test that get_min_year falls back to rolling window when MIN_YEAR is None."""
    import pytest

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "MIN_YEAR", None)
    expected = datetime.now(timezone.utc).year - (_CONTRIBUTION_WINDOW_FALLBACK - 1)
    assert get_min_year() == expected
    mp.undo()
