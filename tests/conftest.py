from __future__ import annotations

import re
from typing import Any

import pytest

from citeforge import io_utils
from tests.test_data import API_CONFIGS


@pytest.fixture(scope="session")
def api_keys() -> dict[str, Any]:
    return {
        "serpapi": io_utils.read_serpapi_api_key(API_CONFIGS["serpapi"]["key_file"]),
        "serply": io_utils.read_serply_api_key(API_CONFIGS["serply"]["key_file"]),
        "semantic": io_utils.read_semantic_api_key(API_CONFIGS["semantic_scholar"]["key_file"]),
        "openreview": io_utils.read_openreview_credentials(API_CONFIGS["openreview"]["key_file"]),
        "gemini": io_utils.read_gemini_api_key(str(API_CONFIGS.get("gemini", {}).get("key_file", "keys/Gemini.key"))),
    }


def extract_bibtex_field(bibtex_str: str, field_name: str) -> str | None:
    """Extract a field value from BibTeX output, handling nested braces."""
    pattern = rf"{field_name}\s*=\s*\{{"
    match = re.search(pattern, bibtex_str)
    if not match:
        return None
    start = match.end() - 1
    depth = 0
    for i in range(start, len(bibtex_str)):
        if bibtex_str[i] == "{":
            depth += 1
        elif bibtex_str[i] == "}":
            depth -= 1
            if depth == 0:
                return bibtex_str[start + 1 : i]
    return None
