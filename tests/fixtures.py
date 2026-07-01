from __future__ import annotations

import functools
from typing import Any

from src import io_utils
from tests.test_data import API_CONFIGS


@functools.lru_cache(maxsize=1)
def _load_keys_once() -> dict[str, Any]:
    """Load all API keys once, caching the result for reuse across test suites."""
    return {
        "serpapi": io_utils.read_serpapi_api_key(API_CONFIGS["serpapi"]["key_file"]),
        "serply": io_utils.read_serply_api_key(API_CONFIGS["serply"]["key_file"]),
        "semantic": io_utils.read_semantic_api_key(API_CONFIGS["semantic_scholar"]["key_file"]),
        "openreview": io_utils.read_openreview_credentials(API_CONFIGS["openreview"]["key_file"]),
        "gemini": io_utils.read_gemini_api_key(API_CONFIGS.get("gemini", {}).get("key_file", "keys/Gemini.key")),
    }


def load_api_keys() -> dict[str, Any]:
    """Load all available API keys for testing."""
    return _load_keys_once().copy()
