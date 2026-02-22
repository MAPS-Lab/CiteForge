from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from typing import Any, TypeVar

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import (
    HTTP_BACKOFF_INITIAL,
    HTTP_BACKOFF_MAX,
    HTTP_MAX_RETRIES,
    HTTP_RETRY_STATUS_CODES,
    HTTP_TIMEOUT_FAST,
)
from .exceptions import ALL_API_ERRORS, DECODE_ERRORS, NUMERIC_ERRORS

T = TypeVar('T')

# Standard HTTP headers for API requests
DEFAULT_JSON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (CiteForge Client)",
    "Accept": "application/json"
}

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/119.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# API call tracking
_API_CALL_COUNTS: dict[str, int] = {}
_CALL_COUNT_LOCK = threading.Lock()

# URL prefix to namespace mapping for call tracking
_URL_NAMESPACE_MAP = {
    "serpapi.com": "serpapi",
    "api.semanticscholar.org": "s2",
    "api.crossref.org": "crossref",
    "export.arxiv.org": "arxiv",
    "api.openreview.net": "openreview",
    "api.openalex.org": "openalex",
    "eutils.ncbi.nlm.nih.gov": "pubmed",
    "www.ebi.ac.uk/europepmc": "europepmc",
    "doi.org": "doi",
    "api.datacite.org": "datacite",
    "pub.orcid.org": "orcid",
    "generativelanguage.googleapis.com": "gemini",
    "dblp.org": "dblp",
    "scholar.google.com": "scholar_browser",
}


def _classify_url(url: str) -> str:
    for prefix, namespace in _URL_NAMESPACE_MAP.items():
        if prefix in url:
            return namespace
    return "other"


def track_api_call(namespace: str) -> None:
    with _CALL_COUNT_LOCK:
        _API_CALL_COUNTS[namespace] = _API_CALL_COUNTS.get(namespace, 0) + 1


def get_api_call_counts() -> dict[str, int]:
    with _CALL_COUNT_LOCK:
        return dict(_API_CALL_COUNTS)


def reset_api_call_counts() -> None:
    with _CALL_COUNT_LOCK:
        _API_CALL_COUNTS.clear()


# Global session for connection pooling
_SESSION = requests.Session()

# Configure retries
_RETRY_STRATEGY = Retry(
    total=HTTP_MAX_RETRIES,
    backoff_factor=HTTP_BACKOFF_INITIAL,
    # Exclude 429/503 from urllib3 status_forcelist to avoid double-backoff
    # with our manual Retry-After handling in http_fetch_bytes
    status_forcelist=tuple(c for c in HTTP_RETRY_STATUS_CODES if c not in (429, 503)),
    allowed_methods=["GET", "POST"]
)
_ADAPTER = HTTPAdapter(max_retries=_RETRY_STRATEGY)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)


def handle_api_errors(default_return: Any = None) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """
    Decorator to handle API errors consistently across all API client functions, returning a default value on error.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return func(*args, **kwargs)
            except ALL_API_ERRORS as e:
                logging.getLogger("CiteForge.http").debug(
                    "API error in %s: %s", func.__qualname__, e
                )
                return default_return
        return wrapper
    return decorator


def _parse_retry_after(ra: str | None) -> float:
    """
    Interpret a Retry-After header value and return how many seconds to wait,
    handling both numeric delays and HTTP date formats.
    """
    if not ra:
        return 0.0
    # try as a number first
    try:
        return float(ra)
    except NUMERIC_ERRORS:
        # maybe it's a date
        try:
            dt = parsedate_to_datetime(ra)
            if getattr(dt, "tzinfo", None) is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except NUMERIC_ERRORS:
            return 0.0


def http_fetch_bytes(
        url: str,
        headers: dict[str, str],
        timeout: float,
) -> bytes:
    """
    Perform an HTTP GET request with retries, exponential backoff, and basic
    rate limit awareness, returning the response body as raw bytes.

    Retry logic is handled at the session level via urllib3.Retry. This function
    adds Retry-After header handling for rate-limited responses (429/503).
    """
    track_api_call(_classify_url(url))
    max_rate_limit_retries = 3

    for attempt in range(max_rate_limit_retries):
        try:
            resp = _SESSION.get(url, headers=headers, timeout=timeout)

            # Handle rate limiting with Retry-After header
            if resp.status_code in (429, 503) and attempt < max_rate_limit_retries - 1:
                retry_after = _parse_retry_after(resp.headers.get('Retry-After'))
                wait_time = retry_after if retry_after > 0 else (2 ** attempt) + random.uniform(0, 1)
                time.sleep(min(wait_time, HTTP_BACKOFF_MAX))
                continue

            resp.raise_for_status()
            return resp.content
        except requests.exceptions.RequestException:
            # Let session-level retry handle transient errors
            # Only re-raise if all retries exhausted
            if attempt == max_rate_limit_retries - 1:
                raise
            time.sleep((2 ** attempt) + random.uniform(0, 1))

    # Should not reach here, but satisfy type checker
    raise requests.exceptions.RequestException(f"Failed to fetch {url}")


def _decode_json_bytes(raw: bytes, url: str) -> dict[str, Any]:
    """
    Decode a UTF-8 JSON response and parse it into a Python object, including a
    short preview of invalid data in error messages.
    """
    try:
        result: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return result
    except json.JSONDecodeError as ex:
        # include a preview for debugging
        preview = raw[:256].decode("utf-8", errors="replace")
        raise ValueError(f"Invalid JSON from {url!r}: {ex.msg} at pos {ex.pos}; preview={preview!r}") from ex


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT_FAST) -> dict[str, Any]:
    """
    Fetch JSON from a URL using a generic User-Agent and JSON Accept header,
    returning the parsed response as a dictionary.
    """
    headers = DEFAULT_JSON_HEADERS.copy()
    raw = http_fetch_bytes(url, headers, timeout)
    return _decode_json_bytes(raw, url)


def s2_http_get_json(url: str, api_key: str, timeout: float = HTTP_TIMEOUT_FAST) -> dict[str, Any]:
    """
    Fetch JSON from the Semantic Scholar API using the provided key, adding the
    required headers and returning the parsed response.
    """
    headers = DEFAULT_JSON_HEADERS.copy()
    headers["x-api-key"] = api_key
    raw = http_fetch_bytes(url, headers, timeout)
    return _decode_json_bytes(raw, url)


def http_get_text(url: str, timeout: float = HTTP_TIMEOUT_FAST) -> str:
    """
    Download an HTML or text page and choose a suitable decoding by inspecting
    byte order marks, trying UTF-8 first, and falling back to Latin-1 when
    needed.
    """
    headers = DEFAULT_BROWSER_HEADERS.copy()
    raw = http_fetch_bytes(url, headers, timeout)
    # check for byte order marks
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")
    if raw.startswith(b"\xff\xfe"):
        try:
            return raw.decode("utf-16le")
        except DECODE_ERRORS:
            pass
    if raw.startswith(b"\xfe\xff"):
        try:
            return raw.decode("utf-16be")
        except DECODE_ERRORS:
            pass
    # no BOM - try UTF-8, fall back to Latin-1
    try:
        return raw.decode("utf-8")
    except DECODE_ERRORS:
        return raw.decode("latin-1", errors="replace")
