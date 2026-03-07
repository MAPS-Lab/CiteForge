from __future__ import annotations

import json
from pathlib import Path

from src.cache import ResponseCache


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
