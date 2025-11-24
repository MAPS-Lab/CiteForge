from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from functools import wraps
from typing import Dict, Any, Optional, Callable, TypeVar

from .exceptions import DECODE_ERRORS, NUMERIC_ERRORS, NETWORK_ERRORS, ALL_API_ERRORS
from .config import (
    HTTP_TIMEOUT_DEFAULT,
    HTTP_BACKOFF_INITIAL,
    HTTP_BACKOFF_MAX,
    HTTP_MAX_RETRIES,
    HTTP_RETRY_STATUS_CODES,
)

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


def handle_api_errors(default_return=None):
    """
    Decorator to handle API errors consistently across all API client functions, returning a default value on error.
    """
    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except ALL_API_ERRORS:
                return default_return
        return wrapper
    return decorator


def _parse_retry_after(ra: Optional[str]) -> float:
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
        headers: Dict[str, str],
        timeout: float,
        attempts: int = HTTP_MAX_RETRIES,
        retry_for_status: tuple = HTTP_RETRY_STATUS_CODES,
        backoff_initial: float = HTTP_BACKOFF_INITIAL,
        backoff_max: float = HTTP_BACKOFF_MAX,
        overall_deadline: Optional[float] = None,
) -> bytes:
    """
    Perform an HTTP GET request with retries, exponential backoff, and basic
    rate limit awareness, returning the response body as raw bytes.
    """
    backoff = max(0.0, backoff_initial)
    last_err: Optional[Exception] = None

    def _sleep_for_with_deadline(delay_secs: float, err: Exception) -> float:
        if overall_deadline is not None:
            remaining = overall_deadline - time.time()
            if remaining <= 0:
                raise err
            return min(delay_secs, max(0.0, remaining))
        return delay_secs

    for attempt in range(1, max(1, attempts) + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            # only retry transient errors like rate limits and server issues
            if e.code in retry_for_status:
                # check if server told us when to retry
                ra_secs = _parse_retry_after(e.headers.get("Retry-After") if getattr(e, "headers", None) else None)

                # check rate limit reset headers (unix timestamp)
                reset_secs = 0.0
                if getattr(e, "headers", None):
                    for h in ("X-RateLimit-Reset", "RateLimit-Reset"):
                        val = e.headers.get(h)
                        if val and val.isdigit():
                            try:
                                reset_at = float(val)
                                reset_secs = max(0.0, reset_at - time.time())
                                break
                            except NUMERIC_ERRORS:
                                reset_secs = 0.0

                # use the longest wait time (server hint vs our backoff)
                sleep_for = max(ra_secs, reset_secs, backoff + random.uniform(0, max(0.01, backoff / 2)))
                sleep_for = _sleep_for_with_deadline(sleep_for, e)

                time.sleep(sleep_for)
                backoff = min(backoff * 2, backoff_max)  # exponential backoff
                last_err = e
                continue
            # other HTTP errors probably aren't transient
            raise
        except NETWORK_ERRORS as e:
            # network issues or timeouts - worth retrying
            sleep_for = backoff + random.uniform(0, max(0.01, backoff / 2))
            sleep_for = _sleep_for_with_deadline(sleep_for, e)
            time.sleep(sleep_for)
            backoff = min(backoff * 2, backoff_max)
            last_err = e
            continue

    if last_err:
        raise last_err
    raise RuntimeError("HTTP fetch failed without exception")


def _fetch_bytes_simple(url: str, headers: Dict[str, str], timeout: float) -> bytes:
    """
    Convenience wrapper around http_fetch_bytes that uses the given headers and
    timeout with default retry behavior.
    """
    return http_fetch_bytes(url, headers, timeout)


def _decode_json_bytes(raw: bytes, url: str) -> Dict[str, Any]:
    """
    Decode a UTF-8 JSON response and parse it into a Python object, including a
    short preview of invalid data in error messages.
    """
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as ex:
        # include a preview for debugging
        preview = raw[:256].decode("utf-8", errors="replace")
        raise ValueError(f"Invalid JSON from {url!r}: {ex.msg} at pos {ex.pos}; preview={preview!r}") from ex


def http_get_json(url: str, timeout: float = HTTP_TIMEOUT_DEFAULT) -> Dict[str, Any]:
    """
    Fetch JSON from a URL using a generic User-Agent and JSON Accept header,
    returning the parsed response as a dictionary.
    """
    headers = DEFAULT_JSON_HEADERS.copy()
    raw = _fetch_bytes_simple(url, headers, timeout)
    return _decode_json_bytes(raw, url)


def s2_http_get_json(url: str, api_key: str, timeout: float = HTTP_TIMEOUT_DEFAULT) -> Dict[str, Any]:
    """
    Fetch JSON from the Semantic Scholar API using the provided key, adding the
    required headers and returning the parsed response.
    """
    headers = DEFAULT_JSON_HEADERS.copy()
    headers["x-api-key"] = api_key
    raw = _fetch_bytes_simple(url, headers, timeout)
    return _decode_json_bytes(raw, url)


def http_get_text(url: str, timeout: float = HTTP_TIMEOUT_DEFAULT) -> str:
    """
    Download an HTML or text page and choose a suitable decoding by inspecting
    byte order marks, trying UTF-8 first, and falling back to Latin-1 when
    needed.
    """
    headers = DEFAULT_BROWSER_HEADERS.copy()
    raw = _fetch_bytes_simple(url, headers, timeout)
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
