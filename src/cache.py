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

from .config import CACHE_DIR, CACHE_ENABLED
from .log_utils import LogCategory, logger


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
        if not CACHE_ENABLED:
            return None
        lock = self._lock_for(namespace)
        with lock:
            path = self._entry_path(namespace, key)
            if not os.path.isfile(path):
                return None
            try:
                with open(path, encoding="utf-8") as f:
                    entry = json.load(f)
            except (json.JSONDecodeError, OSError):
                return None
            # Permanent entries (published papers) survive the monthly boundary
            if not entry.get("permanent"):
                ts = entry.get("timestamp", 0)
                if ts < self._month_boundary:
                    return None
            return dict(entry.get("data", {}))

    def put(
        self, namespace: str, key: str, value: dict[str, Any],
        ttl_days: int = 30, *, permanent: bool = False,
    ) -> None:
        if not CACHE_ENABLED:
            return
        khash = self._key_hash(key)[:12]
        logger.debug(
            f"PUT | namespace={namespace} | key_hash={khash} | ttl_days={ttl_days}"
            f"{' | permanent' if permanent else ''}",
            category=LogCategory.CACHE,
        )
        lock = self._lock_for(namespace)
        with lock:
            ns_dir = self._ns_dir(namespace)
            os.makedirs(ns_dir, exist_ok=True)
            entry: dict[str, Any] = {"timestamp": time.time(), "ttl_days": ttl_days, "data": value}
            if permanent:
                entry["permanent"] = True
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


# Module-level singleton
response_cache = ResponseCache()
