from __future__ import annotations

from src import io_utils
from src.exceptions import FILE_IO_ERRORS
from tests.test_data import API_CONFIGS


class APIKeyManager:
    """Singleton manager for API keys across test suites."""

    _instance: APIKeyManager | None = None
    _keys: dict[str, str | None] | None = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._keys = {}
            cls._load_keys()
        return cls._instance

    @classmethod
    def _load_keys(cls):
        if cls._keys is None:
            cls._keys = {}

        try:
            cls._keys['serpapi'] = io_utils.read_api_key(
                API_CONFIGS['serpapi']['key_file']
            )
        except Exception as e:
            print(f"Warning: Could not load SerpAPI key: {e}")
            cls._keys['serpapi'] = None

        try:
            cls._keys['semantic'] = io_utils.read_semantic_api_key(
                API_CONFIGS['semantic_scholar']['key_file']
            )
        except FILE_IO_ERRORS:
            cls._keys['semantic'] = None

        try:
            cls._keys['openreview'] = io_utils.read_openreview_credentials(
                API_CONFIGS['openreview']['key_file']
            )
        except FILE_IO_ERRORS:
            cls._keys['openreview'] = None

        try:
            cls._keys['gemini'] = io_utils.read_gemini_api_key(
                API_CONFIGS.get('gemini', {}).get('key_file', 'keys/Gemini.key')
            )
        except FILE_IO_ERRORS:
            cls._keys['gemini'] = None

    @classmethod
    def get_key(cls, key_name: str) -> str | None:
        if cls._keys is None:
            cls._load_keys()
        return cls._keys.get(key_name)

    @classmethod
    def get_all_keys(cls) -> dict[str, str | None]:
        if cls._keys is None:
            cls._load_keys()
        return cls._keys.copy()

    @classmethod
    def has_key(cls, key_name: str) -> bool:
        return cls.get_key(key_name) is not None


def load_api_keys() -> dict[str, str | None]:
    """Load all available API keys for testing."""
    return APIKeyManager().get_all_keys()
