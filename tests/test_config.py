import pytest
from CiteForge.config import (
    CONTRIBUTION_WINDOW_YEARS,
    PUBLICATIONS_PER_YEAR,
    MAX_PUBLICATIONS_PER_AUTHOR,
)

def test_dynamic_publication_limit():
    """
    Test that MAX_PUBLICATIONS_PER_AUTHOR is calculated correctly.
    """
    expected = PUBLICATIONS_PER_YEAR * CONTRIBUTION_WINDOW_YEARS
    assert MAX_PUBLICATIONS_PER_AUTHOR == expected, \
        f"Calculation error: expected {expected}, got {MAX_PUBLICATIONS_PER_AUTHOR}"

def test_publications_per_year_reasonable():
    """
    Test that PUBLICATIONS_PER_YEAR is a reasonable value.
    """
    assert PUBLICATIONS_PER_YEAR >= 1, "PUBLICATIONS_PER_YEAR must be at least 1"
    
    # Just check it's not absurdly high, warnings are fine but not failures
    assert PUBLICATIONS_PER_YEAR <= 1000, \
        f"PUBLICATIONS_PER_YEAR is very high ({PUBLICATIONS_PER_YEAR}). This may cause excessive API usage."

def test_contribution_window_reasonable():
    """
    Test that CONTRIBUTION_WINDOW_YEARS is a reasonable value.
    """
    assert CONTRIBUTION_WINDOW_YEARS >= 1, "CONTRIBUTION_WINDOW_YEARS must be at least 1"
    
    # Just check it's not absurdly high
    assert CONTRIBUTION_WINDOW_YEARS <= 20, \
        f"CONTRIBUTION_WINDOW_YEARS is very long ({CONTRIBUTION_WINDOW_YEARS})."

def test_max_publications_scaling():
    """
    Test that MAX_PUBLICATIONS_PER_AUTHOR scales correctly with window.
    """
    # Verify the relationship holds for the current config
    assert MAX_PUBLICATIONS_PER_AUTHOR == PUBLICATIONS_PER_YEAR * CONTRIBUTION_WINDOW_YEARS

def test_config_types():
    """
    Test that configuration values have correct types.
    """
    assert isinstance(CONTRIBUTION_WINDOW_YEARS, int), \
        f"CONTRIBUTION_WINDOW_YEARS should be int, got {type(CONTRIBUTION_WINDOW_YEARS)}"
    
    assert isinstance(PUBLICATIONS_PER_YEAR, int), \
        f"PUBLICATIONS_PER_YEAR should be int, got {type(PUBLICATIONS_PER_YEAR)}"
    
    assert isinstance(MAX_PUBLICATIONS_PER_AUTHOR, int), \
        f"MAX_PUBLICATIONS_PER_AUTHOR should be int, got {type(MAX_PUBLICATIONS_PER_AUTHOR)}"
