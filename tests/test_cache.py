import json
import os

from src.cache import ResponseCache


def test_put_and_get(tmp_path):
    """Test basic cache put and get operations."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"value": "hello"}, ttl_days=30)
    result = cache.get("test_ns", "key1")
    assert result == {"value": "hello"}


def test_get_missing_key(tmp_path):
    """Test that get returns None for missing keys."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    result = cache.get("test_ns", "nonexistent")
    assert result is None


def test_has(tmp_path):
    """Test has() checks for key existence."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    assert not cache.has("test_ns", "key1")
    cache.put("test_ns", "key1", {"v": 1})
    assert cache.has("test_ns", "key1")


def test_invalidate(tmp_path):
    """Test that invalidate removes a cached entry."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    assert cache.has("test_ns", "key1")
    cache.invalidate("test_ns", "key1")
    assert not cache.has("test_ns", "key1")


def test_invalidate_missing_key(tmp_path):
    """Test that invalidating a non-existent key doesn't raise."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.invalidate("test_ns", "nonexistent")  # Should not raise


def test_monthly_expiry(tmp_path):
    """Test that entries from before the 1st of the current month are expired."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})

    # Backdate timestamp to before the monthly boundary
    path = cache._entry_path("test_ns", "key1")
    with open(path, encoding="utf-8") as f:
        entry = json.load(f)
    entry["timestamp"] = cache._month_boundary - 1  # 1 second before boundary
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f)

    result = cache.get("test_ns", "key1")
    assert result is None
    assert not os.path.exists(path), "Expired entry file should be removed"


def test_namespace_isolation(tmp_path):
    """Test that different namespaces are isolated."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("ns_a", "key1", {"v": "a"})
    cache.put("ns_b", "key1", {"v": "b"})

    assert cache.get("ns_a", "key1") == {"v": "a"}
    assert cache.get("ns_b", "key1") == {"v": "b"}


def test_overwrite(tmp_path):
    """Test that putting the same key overwrites the value."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    cache.put("test_ns", "key1", {"v": 2})
    assert cache.get("test_ns", "key1") == {"v": 2}


def test_creates_namespace_directory(tmp_path):
    """Test that put creates the namespace directory if needed."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    ns_dir = os.path.join(str(tmp_path), "new_ns")
    assert not os.path.exists(ns_dir)
    cache.put("new_ns", "key1", {"v": 1})
    assert os.path.isdir(ns_dir)


def test_corrupted_cache_file(tmp_path):
    """Test that corrupted cache files return None."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.put("test_ns", "key1", {"v": 1})
    path = cache._entry_path("test_ns", "key1")
    with open(path, "w", encoding="utf-8") as f:
        f.write("not valid json{{{")
    assert cache.get("test_ns", "key1") is None
