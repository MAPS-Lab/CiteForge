from __future__ import annotations

from typing import Dict, Optional

from src import io_utils
from src.exceptions import FILE_IO_ERRORS
from tests.test_data import API_CONFIGS


class APIKeyManager:
    """
    Singleton manager for API keys across test suites.
    """
    _instance: Optional['APIKeyManager'] = None
    _keys: Optional[Dict[str, Optional[str]]] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._keys = {}
            cls._load_keys()
        return cls._instance

    @classmethod
    def _load_keys(cls):
        """
        Load all available API keys.
        """
        if cls._keys is None:
            cls._keys = {}

        # SerpAPI (required for most tests)
        try:
            cls._keys['serpapi'] = io_utils.read_api_key(
                API_CONFIGS['serpapi']['key_file']
            )
        except Exception as e:
            print(f"⚠️  Warning: Could not load SerpAPI key: {e}")
            cls._keys['serpapi'] = None

        # Semantic Scholar (optional)
        try:
            cls._keys['semantic'] = io_utils.read_semantic_api_key(
                API_CONFIGS['semantic_scholar']['key_file']
            )
        except FILE_IO_ERRORS:
            cls._keys['semantic'] = None

        # OpenReview (optional)
        try:
            cls._keys['openreview'] = io_utils.read_openreview_credentials(
                API_CONFIGS['openreview']['key_file']
            )
        except FILE_IO_ERRORS:
            cls._keys['openreview'] = None

        # Gemini (optional)
        try:
            cls._keys['gemini'] = io_utils.read_gemini_api_key(
                API_CONFIGS.get('gemini', {}).get('key_file', 'keys/Gemini.key')
            )
        except FILE_IO_ERRORS:
            cls._keys['gemini'] = None

    @classmethod
    def get_key(cls, key_name: str) -> Optional[str]:
        """
        Get a specific API key by name.
        """
        if cls._keys is None:
            cls._load_keys()
        return cls._keys.get(key_name)

    @classmethod
    def get_all_keys(cls) -> Dict[str, Optional[str]]:
        """
        Get all loaded API keys.
        """
        if cls._keys is None:
            cls._load_keys()
        return cls._keys.copy()

    @classmethod
    def has_key(cls, key_name: str) -> bool:
        """
        Check if a specific API key is available.
        """
        return cls.get_key(key_name) is not None


def load_api_keys() -> Dict[str, Optional[str]]:
    """
    Load API keys for testing.

    Returns dictionary with keys:
    - 'serpapi': SerpAPI key (required)
    - 'semantic': Semantic Scholar key (optional)
    - 'openreview': OpenReview credentials tuple (optional)
    - 'gemini': Gemini API key (optional)
    """
    return APIKeyManager().get_all_keys()
