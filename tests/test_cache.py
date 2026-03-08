from __future__ import annotations

import json
import time
from pathlib import Path

from src.cache import ResponseCache, get_cache_hit_counts, reset_cache_hit_counts


def test_put_and_get(tmp_path: Path) -> None:
    """Test basic cache put and get operations."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"value": "hello"}, ttl_days=30)
    result = cache.get("test_ns", "key1")
    assert result == {"value": "hello"}


def test_get_missing_key(tmp_path: Path) -> None:
    """Test that get returns None for missing keys."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    result = cache.get("test_ns", "nonexistent")
    assert result is None


def test_has(tmp_path: Path) -> None:
    """Test has() checks for key existence."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    assert not cache.has("test_ns", "key1")
    cache.put("test_ns", "key1", {"v": 1})
    assert cache.has("test_ns", "key1")


def test_invalidate(tmp_path: Path) -> None:
    """Test that invalidate removes a cached entry."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    assert cache.has("test_ns", "key1")
    cache.invalidate("test_ns", "key1")
    assert not cache.has("test_ns", "key1")


def test_invalidate_missing_key(tmp_path: Path) -> None:
    """Test that invalidating a non-existent key doesn't raise."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.invalidate("test_ns", "nonexistent")


def test_monthly_expiry(tmp_path: Path) -> None:
    """Test that stale entries return None but the file is preserved on disk."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})

    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = cache._month_boundary - 1
    path.write_text(json.dumps(entry), encoding="utf-8")

    result = cache.get("test_ns", "key1")
    assert result is None
    assert path.exists(), "Stale entry file should be preserved until overwritten by put()"


def test_stale_entry_refreshed_by_put(tmp_path: Path) -> None:
    """Test that put() overwrites a stale entry with fresh data."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": "old"})

    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = cache._month_boundary - 1
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert cache.get("test_ns", "key1") is None
    cache.put("test_ns", "key1", {"v": "fresh"})
    assert cache.get("test_ns", "key1") == {"v": "fresh"}


def test_namespace_isolation(tmp_path: Path) -> None:
    """Test that different namespaces are isolated."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("ns_a", "key1", {"v": "a"})
    cache.put("ns_b", "key1", {"v": "b"})

    assert cache.get("ns_a", "key1") == {"v": "a"}
    assert cache.get("ns_b", "key1") == {"v": "b"}


def test_overwrite(tmp_path: Path) -> None:
    """Test that putting the same key overwrites the value."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    cache.put("test_ns", "key1", {"v": 2})
    assert cache.get("test_ns", "key1") == {"v": 2}


def test_creates_namespace_directory(tmp_path: Path) -> None:
    """Test that put creates the namespace directory if needed."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns_dir = tmp_path / "new_ns"
    assert not ns_dir.exists()
    cache.put("new_ns", "key1", {"v": 1})
    assert ns_dir.is_dir()


def test_corrupted_cache_file(tmp_path: Path) -> None:
    """Test that corrupted cache files return None."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    path = Path(cache._entry_path("test_ns", "key1"))
    path.write_text("not valid json{{{", encoding="utf-8")
    assert cache.get("test_ns", "key1") is None


def test_negative_cache_ttl_expires(tmp_path: Path) -> None:
    """Test that negative cache entries expire after their TTL."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"_negative": True}, ttl_days=1)

    # Entry exists and is served
    result = cache.get("test_ns", "key1")
    assert result == {"_negative": True}

    # Backdate the timestamp to make it older than 1 day
    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = time.time() - 2 * 86400  # 2 days ago
    path.write_text(json.dumps(entry), encoding="utf-8")

    # Now it should be expired
    assert cache.get("test_ns", "key1") is None


def test_positive_cache_survives_past_ttl(tmp_path: Path) -> None:
    """Test that positive cache entries are NOT expired by TTL (only monthly boundary expires them)."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": "hello"}, ttl_days=30)

    # Backdate to 31 days ago — past the ttl_days=30 window
    path = Path(cache._entry_path("test_ns", "key1"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["timestamp"] = time.time() - 31 * 86400
    # Keep it within the monthly boundary so only TTL could expire it
    entry["timestamp"] = max(entry["timestamp"], cache._month_boundary + 1)
    path.write_text(json.dumps(entry), encoding="utf-8")

    result = cache.get("test_ns", "key1")
    assert result == {"v": "hello"}, "Positive entries must survive past their ttl_days"


def test_cache_counter_positive_hit(tmp_path: Path) -> None:
    """Test that a positive cache hit increments the positive counter."""
    reset_cache_hit_counts()
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    cache.get("test_ns", "key1")
    counts = get_cache_hit_counts()
    assert counts["positive"] == 1
    assert counts["negative"] == 0


def test_cache_counter_negative_hit(tmp_path: Path) -> None:
    """Test that a negative cache hit increments the negative counter."""
    reset_cache_hit_counts()
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"_negative": True}, ttl_days=7)
    cache.get("test_ns", "key1")
    counts = get_cache_hit_counts()
    assert counts["negative"] == 1
    assert counts["positive"] == 0


def test_cache_counter_miss(tmp_path: Path) -> None:
    """Test that a cache miss increments the miss counter."""
    reset_cache_hit_counts()
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.get("test_ns", "nonexistent")
    counts = get_cache_hit_counts()
    assert counts["miss"] == 1
    assert counts["positive"] == 0


def test_cache_counter_reset(tmp_path: Path) -> None:
    """Test that reset_cache_hit_counts zeroes all counters."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    cache.get("test_ns", "key1")
    reset_cache_hit_counts()
    counts = get_cache_hit_counts()
    assert counts == {"positive": 0, "negative": 0, "miss": 0}
