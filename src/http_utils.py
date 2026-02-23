from __future__ import annotations

import json
import logging
import random
import socket
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
    GLOBAL_CONCURRENCY_LIMIT,
    HTTP_BACKOFF_INITIAL,
    HTTP_BACKOFF_MAX,
    HTTP_MAX_RETRIES,
    HTTP_RETRY_STATUS_CODES,
    HTTP_TIMEOUT_FAST,
    RATE_LIMITS,
    SESSION_ROTATION_THRESHOLD,
)
from .exceptions import ALL_API_ERRORS, DECODE_ERRORS, NUMERIC_ERRORS

T = TypeVar('T')

# Safety net: cap all socket operations at 60s to prevent indefinite hangs
# from DNS resolution, SSL handshake, or connection pool waits
socket.setdefaulttimeout(60.0)

# Standard HTTP headers for API requests
DEFAULT_JSON_HEADERS = {
    "User-Agent": "Mozilla/5.0 (CiteForge Client)",
    "Accept": "application/json"
}


def _generate_user_agent_pool() -> list[str]:
    """Build a diverse pool of realistic User-Agent strings.

    Uses current browser versions across multiple platforms to avoid
    fingerprinting through stale version numbers.
    """
    chrome_versions = ["131.0.6778", "132.0.6834", "133.0.6917", "134.0.6998"]
    firefox_versions = ["132.0", "133.0", "134.0"]
    safari_version = "17.6"

    agents: list[str] = []

    # Chrome on Windows
    for cv in chrome_versions:
        agents.append(
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{cv} Safari/537.36"
        )
    # Chrome on macOS
    for cv in chrome_versions:
        agents.append(
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{cv} Safari/537.36"
        )
    # Chrome on Linux
    for cv in chrome_versions:
        agents.append(
            f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{cv} Safari/537.36"
        )
    # Firefox on Windows
    for fv in firefox_versions:
        agents.append(
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{fv}) "
            f"Gecko/20100101 Firefox/{fv}"
        )
    # Firefox on Linux
    for fv in firefox_versions:
        agents.append(
            f"Mozilla/5.0 (X11; Linux x86_64; rv:{fv}) "
            f"Gecko/20100101 Firefox/{fv}"
        )
    # Safari on macOS
    agents.append(
        f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
        f"(KHTML, like Gecko) Version/{safari_version} Safari/605.1.15"
    )
    # Edge on Windows
    agents.append(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/133.0.6917 Safari/537.36 Edg/133.0.6917"
    )

    return agents


_USER_AGENT_POOL = _generate_user_agent_pool()

_ACCEPT_LANGUAGE_POOL = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.9",
    "en-US,en;q=0.9,fr;q=0.8",
    "en-US,en;q=0.8",
    "en;q=0.9",
]

_ACCEPT_ENCODING_POOL = [
    "gzip, deflate, br",
    "gzip, deflate",
    "gzip, deflate, br, zstd",
]

DEFAULT_BROWSER_HEADERS = {
    "User-Agent": random.choice(_USER_AGENT_POOL),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _randomize_headers(headers: dict[str, str]) -> dict[str, str]:
    """Apply subtle header randomization to reduce request fingerprinting."""
    h = dict(headers)
    h["User-Agent"] = random.choice(_USER_AGENT_POOL)
    # Randomly include Accept-Language/Accept-Encoding if not already set by caller
    if "Accept-Language" not in h and random.random() < 0.7:
        h["Accept-Language"] = random.choice(_ACCEPT_LANGUAGE_POOL)
    if "Accept-Encoding" not in h and random.random() < 0.5:
        h["Accept-Encoding"] = random.choice(_ACCEPT_ENCODING_POOL)
    return h

_API_CALL_COUNTS: dict[str, int] = {}
_CALL_COUNT_LOCK = threading.Lock()

_URL_NAMESPACE_MAP = {
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
    "api.serply.io": "serply",
    "serpapi.com": "serpapi",
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


_THREAD_LOCAL = threading.local()

_RETRY_STRATEGY = Retry(
    total=HTTP_MAX_RETRIES,
    backoff_factor=HTTP_BACKOFF_INITIAL,
    backoff_max=HTTP_BACKOFF_MAX,
    # Exclude 429/503 from urllib3 status_forcelist to avoid double-backoff
    # with our manual Retry-After handling in _http_request
    status_forcelist=tuple(c for c in HTTP_RETRY_STATUS_CODES if c not in (429, 503)),
    allowed_methods=["GET", "POST"],
    # Disable urllib3's own Retry-After handling so it doesn't sleep for
    # minutes when a server sends a long Retry-After header.  CiteForge's
    # _http_request already handles Retry-After with a capped backoff.
    respect_retry_after_header=False,
)

_GLOBAL_SEMAPHORE = threading.Semaphore(GLOBAL_CONCURRENCY_LIMIT)


# Per-API token bucket rate limiter


class TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter.

    Each API gets its own bucket with a configured rate (tokens/sec) and
    burst size.  ``acquire()`` blocks until a token is available.
    """

    def __init__(self, rate: float, burst: int = 1) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a token is available, with jitter to avoid thundering herd."""
        while True:
            with self._lock:
                now = time.monotonic()
                elapsed = now - self._last_refill
                self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
                self._last_refill = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # How long until a token is available?
                wait = (1.0 - self._tokens) / self._rate
            # Add jitter (up to 30% extra) to prevent thundering herd
            time.sleep(wait + random.uniform(0, wait * 0.3))


_RATE_LIMITER_REGISTRY: dict[str, TokenBucketRateLimiter] = {}
_RATE_LIMITER_LOCK = threading.Lock()


def _get_rate_limiter(namespace: str) -> TokenBucketRateLimiter | None:
    """Return the rate limiter for *namespace*, creating it on first access."""
    if namespace not in RATE_LIMITS:
        return None
    limiter = _RATE_LIMITER_REGISTRY.get(namespace)
    if limiter is not None:
        return limiter
    with _RATE_LIMITER_LOCK:
        # Double-check after acquiring lock
        limiter = _RATE_LIMITER_REGISTRY.get(namespace)
        if limiter is not None:
            return limiter
        rate, burst = RATE_LIMITS[namespace]
        limiter = TokenBucketRateLimiter(rate, burst)
        _RATE_LIMITER_REGISTRY[namespace] = limiter
        return limiter


def _new_session() -> requests.Session:
    """Create a fresh requests.Session with retry/adapter config."""
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=_RETRY_STRATEGY)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _get_session() -> requests.Session:
    """Return a per-thread requests.Session with retry/adapter config.

    Rotates the session after ``SESSION_ROTATION_THRESHOLD`` requests to
    prevent long-lived connection correlation.
    """
    session = getattr(_THREAD_LOCAL, "session", None)
    count = getattr(_THREAD_LOCAL, "session_request_count", 0)
    if session is None or count >= SESSION_ROTATION_THRESHOLD:
        if session is not None:
            session.close()
        session = _new_session()
        _THREAD_LOCAL.session = session
        _THREAD_LOCAL.session_request_count = 0
    return session


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
    try:
        return float(ra)
    except NUMERIC_ERRORS:
        try:
            dt = parsedate_to_datetime(ra)
            if getattr(dt, "tzinfo", None) is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (dt - datetime.now(timezone.utc)).total_seconds())
        except NUMERIC_ERRORS:
            return 0.0


_MAX_RATE_LIMIT_RETRIES = 3


def _http_request(
        method: str,
        url: str,
        headers: dict[str, str],
        timeout: float,
        json_payload: dict[str, Any] | None = None,
) -> bytes:
    """Execute an HTTP request with the full CiteForge infrastructure.

    Applies, in order: namespace classification, API call tracking,
    per-API rate limiting, header randomization, global concurrency
    gating, session management with rotation, Retry-After back-off,
    and exponential retry on transient errors.

    Args:
        method: HTTP method -- ``"GET"`` or ``"POST"``.
        url: Target URL.
        headers: Base headers (will be copied and randomized).
        timeout: Read timeout in seconds; connect timeout is capped at 10 s.
        json_payload: JSON body for POST requests (ignored for GET).
    """
    namespace = _classify_url(url)
    track_api_call(namespace)

    limiter = _get_rate_limiter(namespace)
    if limiter is not None:
        limiter.acquire()

    headers = _randomize_headers(headers)
    connect_timeout = min(timeout, 10.0)

    for attempt in range(_MAX_RATE_LIMIT_RETRIES):
        rate_limited = False
        rate_wait = 0.0

        with _GLOBAL_SEMAPHORE:
            try:
                session = _get_session()
                _THREAD_LOCAL.session_request_count += 1

                if method == "POST":
                    resp = session.post(
                        url, json=json_payload, headers=headers,
                        timeout=(connect_timeout, timeout),
                    )
                else:
                    resp = session.get(
                        url, headers=headers,
                        timeout=(connect_timeout, timeout),
                    )
            except requests.exceptions.RequestException:
                if attempt == _MAX_RATE_LIMIT_RETRIES - 1:
                    raise
            else:
                if resp.status_code in (429, 503) and attempt < _MAX_RATE_LIMIT_RETRIES - 1:
                    retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                    rate_wait = retry_after if retry_after > 0 else (2 ** attempt) + random.uniform(0, 1)
                    rate_limited = True
                else:
                    resp.raise_for_status()
                    return resp.content

        # All sleeps happen outside the semaphore
        if rate_limited:
            time.sleep(min(rate_wait, HTTP_BACKOFF_MAX))
        else:
            time.sleep((2 ** attempt) + random.uniform(0, 1))

    # Unreachable -- the loop always returns or raises -- but satisfies mypy.
    raise requests.exceptions.RequestException(f"Failed to {method} {url}")


def http_fetch_bytes(
        url: str,
        headers: dict[str, str],
        timeout: float,
) -> bytes:
    """Perform an HTTP GET and return the raw response body.

    Delegates to ``_http_request`` for rate limiting, concurrency control,
    retries, and header randomization.
    """
    return _http_request("GET", url, headers, timeout)


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


def http_post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
    timeout: float = HTTP_TIMEOUT_FAST,
) -> dict[str, Any]:
    """POST JSON to a URL and return the parsed JSON response.

    Delegates to ``_http_request`` for the full infrastructure pipeline
    (rate limiting, concurrency, retries, header randomization).
    """
    h = (headers or DEFAULT_JSON_HEADERS).copy()
    # Explicit Content-Type as a defensive default; requests sets it when
    # json= is used, but callers may pass custom header dicts without it.
    if "Content-Type" not in h:
        h["Content-Type"] = "application/json"
    raw = _http_request("POST", url, h, timeout, json_payload=payload)
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
    try:
        return raw.decode("utf-8")
    except DECODE_ERRORS:
        return raw.decode("latin-1", errors="replace")
