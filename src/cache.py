from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import CACHE_DIR, CACHE_ENABLED, CACHE_TTL_NEGATIVE_DAYS
from .log_utils import LogCategory, logger

_CACHE_POS_HITS: int = 0
_CACHE_NEG_HITS: int = 0
_CACHE_MISSES: int = 0
_CACHE_COUNTER_LOCK = threading.Lock()


def _month_boundary() -> float:
    """Return the timestamp of midnight on the 1st of the current month (AST).

    Every cache entry written before this boundary is considered stale,
    forcing a fresh API request at the start of each month.
    Atlantic Standard Time (UTC-4) is used as the reference timezone.
    """
    ast = timezone(timedelta(hours=-4))
    now = datetime.now(tz=ast)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


class ResponseCache:
    """Thread-safe, file-based response cache with monthly expiry.

    All entries expire on the 1st of each calendar month (AST/UTC-4),
    ensuring a full refresh of every API source at least once per month.
    """

    def __init__(self, cache_dir: str = CACHE_DIR) -> None:
        self._cache_dir = cache_dir
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()
        self._month_boundary = _month_boundary()

    def _lock_for(self, namespace: str) -> threading.Lock:
        with self._meta_lock:
            return self._locks.setdefault(namespace, threading.Lock())

    @staticmethod
    def _key_hash(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    def _ns_dir(self, namespace: str) -> str:
        return os.path.join(self._cache_dir, namespace)

    def _entry_path(self, namespace: str, key: str) -> str:
        return os.path.join(self._ns_dir(namespace), f"{self._key_hash(key)}.json")

    def get(self, namespace: str, key: str) -> dict[str, Any] | None:
        global _CACHE_POS_HITS, _CACHE_NEG_HITS, _CACHE_MISSES
        if not CACHE_ENABLED:
            return None
        lock = self._lock_for(namespace)
        with lock:
            path = self._entry_path(namespace, key)
            if not os.path.isfile(path):
                with _CACHE_COUNTER_LOCK:
                    _CACHE_MISSES += 1
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    entry = json.load(f)
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug(
                    f"CACHE_CORRUPT | namespace={namespace} | path={path} | error={exc}",
                    category=LogCategory.CACHE,
                )
                with _CACHE_COUNTER_LOCK:
                    _CACHE_MISSES += 1
                return None
            ts = entry.get("timestamp", 0)
            if ts < self._month_boundary:
                with _CACHE_COUNTER_LOCK:
                    _CACHE_MISSES += 1
                return None
            # Honour TTL for short-lived negative/error cache entries only.
            # Positive entries expire solely at the monthly boundary above.
            data = entry.get("data", {})
            ttl = entry.get("ttl_days", 0)
            if ttl > 0 and data.get("_negative") and time.time() - ts > ttl * 86400:
                with _CACHE_COUNTER_LOCK:
                    _CACHE_MISSES += 1
                return None
            if data.get("_negative"):
                with _CACHE_COUNTER_LOCK:
                    _CACHE_NEG_HITS += 1
            else:
                with _CACHE_COUNTER_LOCK:
                    _CACHE_POS_HITS += 1
            return dict(data)

    def put(self, namespace: str, key: str, value: dict[str, Any], ttl_days: int = 30) -> None:
        if not CACHE_ENABLED:
            return
        khash = self._key_hash(key)[:12]
        logger.debug(
            f"PUT | namespace={namespace} | key_hash={khash} | ttl_days={ttl_days}",
            category=LogCategory.CACHE,
        )
        lock = self._lock_for(namespace)
        with lock:
            ns_dir = self._ns_dir(namespace)
            os.makedirs(ns_dir, exist_ok=True)
            entry = {"timestamp": time.time(), "ttl_days": ttl_days, "data": value}
            path = self._entry_path(namespace, key)
            try:
                fd, tmp_path = tempfile.mkstemp(dir=ns_dir, suffix=".tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump(entry, f)
                    os.replace(tmp_path, path)
                except BaseException:
                    with contextlib.suppress(OSError):
                        os.remove(tmp_path)
                    raise
            except OSError as exc:
                logger.warn(
                    f"CACHE_WRITE_FAILED | namespace={namespace} | key_hash={khash} | error={exc}",
                    category=LogCategory.CACHE,
                )

    def put_negative(self, namespace: str, key: str, ttl_days: int | None = None) -> None:
        """Store a negative (no-result) cache entry with a short default TTL."""
        self.put(namespace, key, {"_negative": True}, ttl_days=ttl_days or CACHE_TTL_NEGATIVE_DAYS)

    def has(self, namespace: str, key: str) -> bool:
        return self.get(namespace, key) is not None

    def invalidate(self, namespace: str, key: str) -> None:
        khash = self._key_hash(key)[:12]
        logger.debug(
            f"INVALIDATE | namespace={namespace} | key_hash={khash}",
            category=LogCategory.CACHE,
        )
        lock = self._lock_for(namespace)
        with lock:
            path = self._entry_path(namespace, key)
            with contextlib.suppress(OSError):
                os.remove(path)


def get_cache_hit_counts() -> dict[str, int]:
    """Return current cache hit/miss counters."""
    with _CACHE_COUNTER_LOCK:
        return {"positive": _CACHE_POS_HITS, "negative": _CACHE_NEG_HITS, "miss": _CACHE_MISSES}


def reset_cache_hit_counts() -> None:
    """Reset all cache counters to zero."""
    global _CACHE_POS_HITS, _CACHE_NEG_HITS, _CACHE_MISSES
    with _CACHE_COUNTER_LOCK:
        _CACHE_POS_HITS = 0
        _CACHE_NEG_HITS = 0
        _CACHE_MISSES = 0


# Module-level singleton
response_cache = ResponseCache()
